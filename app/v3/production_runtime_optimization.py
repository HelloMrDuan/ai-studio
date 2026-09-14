from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any


_PROFILE_ROLES = {"character_profile", "character_appearance", "location_profile", "prop_profile"}
_REFERENCE_ROLES = {
    "character_reference",
    "character_turnaround",
    "character_consistency",
    "location_reference",
    "scene_reference",
    "prop_reference",
    "item_reference",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _json_copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return fallback


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PersistentLLMContentCache:
    """Content-addressed cache for deterministic/repeated production calls."""

    def __init__(self, data_dir: Path | str, *, max_entries: int = 1500) -> None:
        self.root = Path(data_dir) / "v3" / "llm-content-cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_entries = max(64, int(max_entries))

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def key(
        self,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        model_signature: dict[str, Any],
    ) -> str:
        return _sha({
            "schema": "xiaoduan_llm_cache_v1",
            "phase": phase,
            "messages": messages,
            "system_prompt": system_prompt,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "model": model_signature,
        })

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("result")
            return dict(value) if isinstance(value, dict) else None
        except Exception:
            return None

    def put(self, key: str, result: dict[str, Any]) -> None:
        path = self._path(key)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({
                "schema_version": "xiaoduan_llm_cache_v1",
                "key": key,
                "created_at": _utcnow(),
                "result": _json_copy(result, {}),
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
        self._prune()

    def _prune(self) -> None:
        rows = sorted(
            self.root.glob("*.json"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for path in rows[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass


class ProjectProductionContext:
    """Authoritative project context with stage-specific projections.

    A stage does not receive all prior chat.  It receives a stable story fact
    source plus only the reusable assets it is allowed to consume.  The exact
    projection is content-addressed so unchanged inputs can be reused directly.
    """

    def __init__(self, data_dir: Path | str, production: Any) -> None:
        self.production = production
        self.root = Path(data_dir) / "v3" / "production-context"
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        path = self.root / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _active_text_asset(self, project_id: str, role: str) -> dict[str, Any] | None:
        rows = [
            row for row in self.production.list_assets(project_id, active_only=True)
            if _clean(row.get("asset_role")) == role
            and _clean(row.get("status")).lower() == "ready"
            and _clean(row.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda row: (int(row.get("version") or 0), _clean(row.get("updated_at"))))
        return rows[-1] if rows else None

    def _asset_text(self, project_id: str, asset: dict[str, Any] | None, max_chars: int = 18000) -> str:
        if not asset:
            return ""
        try:
            return self.production.read_text_asset(project_id, _clean(asset.get("asset_id")), max_chars=max_chars)
        except Exception:
            return ""

    @staticmethod
    def _compact_entity(entity: dict[str, Any]) -> dict[str, Any]:
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        continuity = metadata.get("continuity") if isinstance(metadata.get("continuity"), dict) else {}
        authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
        return {
            "entity_id": _clean(entity.get("entity_id")),
            "type": _clean(entity.get("entity_type")).lower(),
            "name": _clean(entity.get("name")),
            "core_profile": _json_copy(continuity.get("core_profile") or {}, {}),
            "default_state": _json_copy(continuity.get("default_state") or {}, {}),
            "aliases": list(continuity.get("aliases") or [])[:12],
            "stable_design": _clean(authoring.get("stable_design")),
        }

    def _profile_assets(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for asset in self.production.list_assets(project_id, active_only=True):
            if _clean(asset.get("asset_role")) not in _PROFILE_ROLES:
                continue
            if _clean(asset.get("status")).lower() != "ready":
                continue
            if _clean(asset.get("dependency_state")).lower() == "stale":
                continue
            text = self._asset_text(project_id, asset, max_chars=9000)
            rows.append({
                "asset_id": _clean(asset.get("asset_id")),
                "role": _clean(asset.get("asset_role")),
                "version": int(asset.get("version") or 0),
                "entity_ids": list(asset.get("entity_ids") or []),
                "content": text,
            })
        return rows

    def _reference_index(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for asset in self.production.list_assets(project_id, active_only=True):
            if _clean(asset.get("asset_role")) not in _REFERENCE_ROLES:
                continue
            if _clean(asset.get("status")).lower() != "ready":
                continue
            if _clean(asset.get("dependency_state")).lower() == "stale":
                continue
            rows.append({
                "asset_id": _clean(asset.get("asset_id")),
                "role": _clean(asset.get("asset_role")),
                "version": int(asset.get("version") or 0),
                "entity_ids": list(asset.get("entity_ids") or []),
            })
        return rows

    def build(self, project: dict[str, Any], stage: str) -> dict[str, Any]:
        project_id = _clean(project.get("project_id"))
        confirmed = project.get("confirmed_outputs") if isinstance(project.get("confirmed_outputs"), dict) else {}
        story_asset = self._active_text_asset(project_id, "story_bible")
        story_text = self._asset_text(project_id, story_asset)
        if not story_text:
            story_text = _clean((confirmed.get("01") or {}).get("handoff"))

        entities = [self._compact_entity(row) for row in self.production.list_entities(project_id)]
        characters = [row for row in entities if row["type"] == "character"]
        locations = [row for row in entities if row["type"] == "location"]
        props = [row for row in entities if row["type"] == "prop"]
        profiles = self._profile_assets(project_id)

        payload: dict[str, Any] = {
            "schema_version": "xiaoduan_project_production_context_v1",
            "project_id": project_id,
            "stage": stage,
            "story_bible": story_text,
        }
        if stage == "02":
            payload["character_facts"] = characters
            payload["input_policy"] = "故事事实 + 角色相关事实；不重新解释视觉或分镜"
        elif stage == "03":
            payload["character_assets"] = [
                row for row in profiles
                if row["role"] in {"character_profile", "character_appearance"}
            ]
            payload["location_facts"] = locations
            payload["prop_facts"] = props
            payload["input_policy"] = "故事事实 + 已确认角色资产/形象版本 + 地点/道具事实"
        elif stage == "04":
            payload["reusable_assets"] = profiles
            payload["canonical_references"] = self._reference_index(project_id)
            payload["input_policy"] = "故事事实 + 正式角色/形象/地点/道具版本；镜头不得重新设计资产"
        elif stage == "01":
            payload["input_policy"] = "仅以用户原故事和明确要求建立事实源"
        else:
            payload["reusable_assets"] = profiles
            payload["canonical_references"] = self._reference_index(project_id)

        payload["context_hash"] = _sha(payload)
        self._persist(project_id, payload)
        return payload

    def _persist(self, project_id: str, payload: dict[str, Any]) -> None:
        context_hash = _clean(payload.get("context_hash"))
        if not context_hash:
            return
        target = self._project_dir(project_id) / f"{context_hash}.json"
        if target.is_file():
            return
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)

    def render(self, project: dict[str, Any], stage: str, max_chars: int = 9000) -> str:
        if stage == "01":
            return ""
        payload = self.build(project, stage)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text) <= max_chars:
            return text
        compact = dict(payload)
        story = _clean(compact.get("story_bible"))
        reserve = max(800, max_chars // 3)
        compact["story_bible"] = story[:reserve] + ("\n[故事事实按上下文预算截断]" if len(story) > reserve else "")
        text = json.dumps(compact, ensure_ascii=False, indent=2)
        return text[:max_chars]

    def asset_manifest(self, project: dict[str, Any], stage: str, max_chars: int = 4000) -> str:
        payload = self.build(project, stage)
        reusable = payload.get("reusable_assets") or payload.get("character_assets") or []
        slim = {
            "context_hash": payload.get("context_hash"),
            "reusable_asset_versions": [
                {
                    "asset_id": row.get("asset_id"),
                    "role": row.get("role"),
                    "version": row.get("version"),
                    "entity_ids": row.get("entity_ids"),
                }
                for row in reusable
            ],
            "canonical_references": payload.get("canonical_references", []),
        }
        return json.dumps(slim, ensure_ascii=False)[:max_chars]


class ProductionRuntimeOptimizer:
    """Install context slicing and persistent content-hash reuse on Director."""

    def __init__(self, settings: Any, director: Any) -> None:
        self.settings = settings
        self.director = director
        self.context = ProjectProductionContext(settings.data_dir, director.production)
        self.cache = PersistentLLMContentCache(
            settings.data_dir,
            max_entries=int(os.environ.get("XIAODUAN_LLM_CACHE_MAX_ENTRIES", "1500")),
        )
        self._original_prior_handoffs = getattr(director, "_prior_handoffs")
        self._original_prior_asset_manifest = getattr(director, "_prior_asset_manifest")
        self._original_tracked_llm_chat = getattr(director, "_tracked_llm_chat")

    def _model_signature(self) -> dict[str, Any]:
        llm = getattr(self.director, "llm", None)
        return {
            "base_url": _clean(getattr(llm, "base_url", "")),
            "model": _clean(getattr(llm, "model", "")),
            "required_model_id": _clean(getattr(self.settings, "stage04_required_model_id", "")),
            "required_model_alias": _clean(getattr(self.settings, "stage04_required_model_alias", "")),
        }

    def install(self) -> None:
        if getattr(self.director, "_xiaoduan_production_runtime_optimizer_installed", False):
            return

        context_service = self.context
        cache = self.cache
        original_tracked = self._original_tracked_llm_chat
        model_signature = self._model_signature

        def prior_handoffs(instance: Any, project: dict[str, Any], max_chars: int = 12000) -> str:
            stage = _clean(project.get("current_stage"))
            rendered = context_service.render(project, stage, max_chars=max_chars)
            return rendered or ""

        def prior_asset_manifest(instance: Any, project: dict[str, Any], max_chars: int = 5000) -> str:
            stage = _clean(project.get("current_stage"))
            return context_service.asset_manifest(project, stage, max_chars=max_chars)

        async def tracked_llm_chat(
            instance: Any,
            *,
            phase: str,
            messages: list[dict[str, str]],
            system_prompt: str,
            temperature: float,
            max_tokens: int,
        ) -> dict[str, Any]:
            key = cache.key(
                phase=phase,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                model_signature=model_signature(),
            )
            cached = cache.get(key)
            if cached is not None:
                recorder = getattr(instance, "record_phase_cache_hit", None)
                if callable(recorder):
                    recorder(phase)
                value = dict(cached)
                value["persistent_content_cache_hit"] = True
                value["persistent_content_cache_key"] = key
                return value
            result = await original_tracked(
                phase=phase,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            value = dict(result)
            cache.put(key, value)
            value["persistent_content_cache_key"] = key
            return value

        self.director._prior_handoffs = MethodType(prior_handoffs, self.director)
        self.director._prior_asset_manifest = MethodType(prior_asset_manifest, self.director)
        self.director._tracked_llm_chat = MethodType(tracked_llm_chat, self.director)
        self.director._xiaoduan_production_runtime_optimizer_installed = True


__all__ = [
    "PersistentLLMContentCache",
    "ProjectProductionContext",
    "ProductionRuntimeOptimizer",
]
