from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm_name(value: Any) -> str:
    text = _clean(value).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _copy(value: Any, default: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return deepcopy(default)


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        elif value is None:
            target.pop(key, None)
        else:
            target[key] = _copy(value, value)
    return target


def _flatten_patch(value: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for key, item in (value or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            rows.extend(_flatten_patch(item, path))
        else:
            rows.append((path, item))
    return rows


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = [x for x in str(path).split(".") if x]
    if not parts:
        return
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = _copy(value, value)


class StoryContinuityService:
    """Project-level story continuity.

    The service never decides business semantics through keyword tables.
    Story/source text is interpreted by the current LLM using a fixed JSON
    contract. Deterministic code only stores, versions, orders and resolves
    those structured facts.
    """

    schema_version = "story_continuity_v3_context_budget"

    def __init__(self, settings, director) -> None:
        self.settings = settings
        self.director = director
        self.production = director.production
        self.root = Path(settings.data_dir) / "story_continuity"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, project_id: str) -> Path:
        self.director.get_project(project_id)
        return self.root / f"{project_id}.json"

    def _empty(self, project_id: str) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": project_id,
            "analysis": {
                "status": "idle",
                "message": "",
                "source_asset_id": "",
                "source_sha256": "",
                "chunks_total": 0,
                "chunks_done": 0,
                "started_at": "",
                "updated_at": _now(),
                "error": "",
                "identity_resolution": {
                    "deterministic_matches": 0,
                    "semantic_matches": 0,
                    "semantic_groups": 0,
                    "new_candidates": 0,
                    "resolver_calls": 0,
                },
            },
            "episodes": [],
            "scenes": [],
            "shots": [],
            "events": [],
            "overrides": [],
            "active_episode_id": "",
            "storyboard_source_sha256": "",
            "updated_at": _now(),
        }

    def load(self, project_id: str) -> dict[str, Any]:
        path = self._path(project_id)
        with self._lock:
            if not path.is_file():
                value = self._empty(project_id)
                self.save(project_id, value)
                return value
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                value = self._empty(project_id)
            if not isinstance(value, dict):
                value = self._empty(project_id)
            value["schema_version"] = self.schema_version
            value["project_id"] = project_id
            for key in ("episodes", "scenes", "shots", "events", "overrides"):
                if not isinstance(value.get(key), list):
                    value[key] = []
            if not isinstance(value.get("analysis"), dict):
                value["analysis"] = self._empty(project_id)["analysis"]
            return value

    def save(self, project_id: str, value: dict[str, Any]) -> dict[str, Any]:
        path = self._path(project_id)
        value = _copy(value, {})
        value["schema_version"] = self.schema_version
        value["project_id"] = project_id
        value["updated_at"] = _now()
        temp = path.with_suffix(".tmp")
        with self._lock:
            temp.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp.replace(path)
        return value

    def _source_asset(self, project_id: str) -> dict[str, Any]:
        assets = self.production.list_assets(project_id, active_only=True)
        candidates = [
            a for a in assets
            if _clean(a.get("status")).lower() == "ready"
            and _clean(a.get("asset_type")).upper() in {"TEXT", "FILE"}
            and _clean(a.get("asset_role")) in {"source_full", "source_brief"}
        ]
        if not candidates:
            raise FileNotFoundError("项目没有可分析的原始创作文本")
        candidates.sort(
            key=lambda a: (
                0 if _clean(a.get("asset_role")) == "source_full" else 1,
                -int(a.get("version") or 0),
            )
        )
        return candidates[0]

    def source_text(self, project_id: str, max_chars: int = 2_000_000) -> tuple[dict[str, Any], str]:
        asset = self._source_asset(project_id)
        text = self.production.read_text_asset(
            project_id,
            asset["asset_id"],
            max_chars=max_chars + 1,
        )
        if len(text) > max_chars:
            raise ValueError(f"当前连续性引擎最多处理 {max_chars} 字符的单个项目源文本")
        return asset, text

    def needs_analysis(self, project_id: str) -> bool:
        try:
            asset, text = self.source_text(project_id)
        except Exception:
            return False
        source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        state = self.load(project_id)
        analysis = state.get("analysis") or {}
        return not (
            analysis.get("status") == "ready"
            and analysis.get("source_asset_id") == asset.get("asset_id")
            and analysis.get("source_sha256") == source_sha
        )

    def compact_snapshot(self, project_id: str) -> dict[str, Any]:
        state = self.load(project_id)
        analysis = state.get("analysis") or {}
        return {
            "schema_version": self.schema_version,
            "analysis": {
                "status": analysis.get("status") or "idle",
                "message": analysis.get("message") or "",
                "chunks_total": int(analysis.get("chunks_total") or 0),
                "chunks_done": int(analysis.get("chunks_done") or 0),
                "error": analysis.get("error") or "",
                "identity_resolution": _copy(analysis.get("identity_resolution") or {}, {}),
            },
            "active_episode_id": state.get("active_episode_id") or "",
            "episodes": [
                {
                    "episode_id": x.get("episode_id"),
                    "title": x.get("title"),
                    "order": x.get("order"),
                    "summary": x.get("summary"),
                }
                for x in state.get("episodes") or []
            ],
            "scenes": [
                {
                    "scene_id": x.get("scene_id"),
                    "entity_id": x.get("entity_id"),
                    "episode_id": x.get("episode_id"),
                    "title": x.get("title"),
                    "order": x.get("order"),
                    "sequence": x.get("sequence"),
                    "summary": x.get("summary"),
                    "location_entity_id": x.get("location_entity_id"),
                    "character_entity_ids": x.get("character_entity_ids") or [],
                    "prop_entity_ids": x.get("prop_entity_ids") or [],
                }
                for x in state.get("scenes") or []
            ],
            "shots": [
                {
                    "shot_id": x.get("shot_id"),
                    "entity_id": x.get("entity_id"),
                    "scene_id": x.get("scene_id"),
                    "episode_id": x.get("episode_id"),
                    "title": x.get("title"),
                    "order": x.get("order"),
                    "sequence": x.get("sequence"),
                    "summary": x.get("summary"),
                    # V2.36.1 PRODUCTION FIELDS IN COMPACT SNAPSHOT
                    "global_order": x.get("global_order"),
                    "duration_seconds": x.get("duration_seconds"),
                    "composition": x.get("composition"),
                    "shot_size": x.get("shot_size"),
                    "camera": x.get("camera"),
                    "camera_move": x.get("camera_move"),
                    "action": x.get("action"),
                    "performance": x.get("performance"),
                    "environment": x.get("environment"),
                    "dialogue": x.get("dialogue"),
                    "narration": x.get("narration"),
                    "sound": x.get("sound"),
                    "music": x.get("music"),
                    "continuity": x.get("continuity"),
                    "representative_state": x.get("representative_state"),
                    "video_start_state": x.get("video_start_state"),
                    "video_end_state": x.get("video_end_state"),
                    "image_prompt": x.get("image_prompt"),
                    "video_start_prompt": x.get("video_start_prompt"),
                    "video_prompt": x.get("video_prompt"),
                    "covered_beat_orders": x.get("covered_beat_orders") or [],
                    "source_provenance": _copy(x.get("source_provenance") or {}, {}),
                    "batch_audit": _copy(x.get("batch_audit") or {}, {}),
                    "narrative_audit": _copy(x.get("narrative_audit") or {}, {}),
                    "scene_global_audit": _copy(x.get("scene_global_audit") or {}, {}),
                    "forward_overlap_audit": _copy(x.get("forward_overlap_audit") or {}, {}),
                    "character_entity_ids": x.get("character_entity_ids") or [],
                    "prop_entity_ids": x.get("prop_entity_ids") or [],
                    "stage04_contract_version": x.get("stage04_contract_version"),
                    "text_model_policy": x.get("text_model_policy"),
                    "runtime_version": x.get("runtime_version"),
                    "provisional": bool(x.get("provisional")),
                }
                for x in state.get("shots") or []
            ],
            "entities": self.entity_summary(project_id),
            "overrides": [
                {
                    "override_id": x.get("override_id"),
                    "anchor_type": x.get("anchor_type"),
                    "anchor_id": x.get("anchor_id"),
                    "scope": x.get("scope"),
                    "instruction": x.get("instruction"),
                    "locked": bool(x.get("locked")),
                    "created_at": x.get("created_at"),
                }
                for x in state.get("overrides") or []
            ],
        }

    def entity_summary(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for entity in self.production.list_entities(project_id):
            if _clean(entity.get("entity_type")).lower() not in {
                "character", "location", "prop", "item", "weapon"
            }:
                continue
            meta = entity.get("metadata") or {}
            cmeta = meta.get("continuity") or {}
            rows.append({
                "entity_id": entity.get("entity_id"),
                "entity_type": entity.get("entity_type"),
                "name": entity.get("name"),
                "aliases": cmeta.get("aliases") or [],
                "core_profile": cmeta.get("core_profile") or {},
                "default_state": cmeta.get("default_state") or {},
                "references": cmeta.get("references") or [],
            })
        return rows

    def _entity_by_id(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for item in self.production.list_entities(project_id):
            if item.get("entity_id") == entity_id:
                return item
        raise FileNotFoundError(f"连续性实体不存在：{entity_id}")

    def _exact_identity_match(
        self,
        project_id: str,
        *,
        entity_type: str,
        name: str,
        aliases: list[str] | None = None,
    ) -> dict[str, Any] | None:
        etype = _clean(entity_type).lower()
        wanted = {_norm_name(name)}
        wanted.update(_norm_name(x) for x in (aliases or []) if _norm_name(x))
        wanted.discard("")
        if not wanted:
            return None
        for item in self.production.list_entities(project_id, etype):
            meta = item.get("metadata") or {}
            cmeta = meta.get("continuity") or {}
            names = {_norm_name(item.get("name"))}
            names.update(_norm_name(x) for x in cmeta.get("aliases") or [])
            names.discard("")
            if wanted & names:
                return item
        return None

    def _merge_identity_metadata(
        self,
        project_id: str,
        item: dict[str, Any],
        *,
        candidate_name: str,
        aliases: list[str] | None,
        core_profile: dict[str, Any] | None,
        default_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        meta = _copy(item.get("metadata") or {}, {})
        cmeta = _copy(meta.get("continuity") or {}, {})
        canonical_norm = _norm_name(item.get("name"))
        merged: list[str] = []
        seen: set[str] = set()
        for value in [*_copy(cmeta.get("aliases") or [], []), candidate_name, *(aliases or [])]:
            raw = _clean(value)
            norm = _norm_name(raw)
            if not raw or not norm or norm == canonical_norm or norm in seen:
                continue
            seen.add(norm)
            merged.append(raw)
        cmeta["aliases"] = merged
        cmeta["core_profile"] = _merge(
            _copy(cmeta.get("core_profile") or {}, {}), core_profile or {}
        )
        cmeta["default_state"] = _merge(
            _copy(cmeta.get("default_state") or {}, {}), default_state or {}
        )
        meta["continuity"] = cmeta
        return self.production.update_entity(
            project_id, item["entity_id"], {"metadata": meta}
        )

    def _find_entity(
        self,
        project_id: str,
        *,
        entity_type: str,
        name: str,
        aliases: list[str] | None = None,
        core_profile: dict[str, Any] | None = None,
        default_state: dict[str, Any] | None = None,
        preferred_entity_id: str = "",
    ) -> dict[str, Any]:
        etype = _clean(entity_type).lower()
        canonical = _clean(name)
        if not canonical:
            raise ValueError("实体名称为空")

        item = None
        preferred = _clean(preferred_entity_id)
        if preferred:
            try:
                candidate = self._entity_by_id(project_id, preferred)
                if _clean(candidate.get("entity_type")).lower() == etype:
                    item = candidate
            except FileNotFoundError:
                item = None
        if item is None:
            item = self._exact_identity_match(
                project_id,
                entity_type=etype,
                name=canonical,
                aliases=aliases,
            )
        if item is not None:
            return self._merge_identity_metadata(
                project_id,
                item,
                candidate_name=canonical,
                aliases=aliases,
                core_profile=core_profile,
                default_state=default_state,
            )

        logical_hash = hashlib.sha1(
            f"{etype}:{_norm_name(canonical)}".encode("utf-8")
        ).hexdigest()[:16]
        return self.production.create_entity(
            project_id,
            entity_type=etype,
            name=canonical,
            logical_key=f"continuity:{etype}:{logical_hash}",
            metadata={
                "continuity": {
                    "aliases": list(dict.fromkeys(_clean(x) for x in (aliases or []) if _clean(x))),
                    "core_profile": _copy(core_profile or {}, {}),
                    "default_state": _copy(default_state or {}, {}),
                    "references": [],
                }
            },
        )

    def bind_reference(
        self,
        project_id: str,
        *,
        entity_id: str,
        asset_id: str,
        role: str,
        variant_key: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        entity = self._entity_by_id(project_id, entity_id)
        asset = self.production.get_asset(project_id, asset_id)
        if _clean(asset.get("status")).lower() != "ready":
            raise ValueError("参考资产必须是 READY")
        if _clean(asset.get("asset_type")).upper() not in {"IMAGE", "VIDEO"}:
            raise ValueError("连续性参考资产必须是图片或视频")
        meta = _copy(entity.get("metadata") or {}, {})
        cmeta = _copy(meta.get("continuity") or {}, {})
        refs = list(cmeta.get("references") or [])
        item = {
            "reference_id": "cref_" + secrets.token_hex(8),
            "asset_id": asset_id,
            "role": _clean(role) or "reference",
            "variant_key": _clean(variant_key),
            "label": _clean(label) or _clean(asset.get("name")),
            "created_at": _now(),
        }
        refs = [
            x for x in refs
            if not (
                _clean(x.get("role")) == item["role"]
                and _clean(x.get("variant_key")) == item["variant_key"]
            )
        ]
        refs.append(item)
        cmeta["references"] = refs
        meta["continuity"] = cmeta
        return self.production.update_entity(
            project_id, entity_id, {"metadata": meta}
        )

    def _text_chunks(
        self,
        text: str,
        *,
        max_chars: int = 2200,
        overlap: int = 0,
    ) -> list[str]:
        """Split long model inputs without dropping the tail.

        Boundary selection is structural only (newline/punctuation); it does not
        inspect business keywords. overlap is optional and callers must dedupe
        outputs when they enable it.
        """
        raw = str(text or "")
        if not raw:
            return []
        max_chars = max(512, int(max_chars))
        overlap = max(0, min(int(overlap), max_chars // 4))
        out: list[str] = []
        start = 0
        length = len(raw)
        while start < length:
            hard_end = min(length, start + max_chars)
            end = hard_end
            if hard_end < length:
                floor = start + max_chars // 2
                candidates = [
                    raw.rfind("\n", floor, hard_end),
                    raw.rfind("。", floor, hard_end),
                    raw.rfind("！", floor, hard_end),
                    raw.rfind("？", floor, hard_end),
                    raw.rfind("；", floor, hard_end),
                ]
                boundary = max(candidates)
                if boundary >= floor:
                    end = boundary + 1
            piece = raw[start:end]
            if piece:
                out.append(piece)
            if end >= length:
                break
            next_start = end - overlap if overlap else end
            if next_start <= start:
                next_start = end
            start = next_start
        return out

    @staticmethod
    def _prompt_entity_row(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "entity_id": _clean(item.get("entity_id")),
            "entity_type": _clean(item.get("entity_type")).lower(),
            "name": _clean(item.get("name")),
            "aliases": [
                _clean(x) for x in (item.get("aliases") or [])
                if _clean(x)
            ][:12],
        }

    @staticmethod
    def _json_row_batches(
        rows: list[dict[str, Any]],
        *,
        max_chars: int,
    ) -> list[list[dict[str, Any]]]:
        max_chars = max(800, int(max_chars))
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 2
        for row in rows:
            encoded = json.dumps(
                row, ensure_ascii=False, separators=(",", ":")
            )
            row_chars = len(encoded) + 1
            if current and current_chars + row_chars > max_chars:
                batches.append(current)
                current = []
                current_chars = 2
            # One pathological row is compacted rather than allowed to blow
            # the model context.
            if row_chars > max_chars:
                compact = dict(row)
                compact["aliases"] = list(compact.get("aliases") or [])[:4]
                if isinstance(compact.get("core_profile"), dict):
                    compact.pop("core_profile", None)
                encoded = json.dumps(
                    compact, ensure_ascii=False, separators=(",", ":")
                )
                if len(encoded) + 1 > max_chars:
                    compact["name"] = _clean(compact.get("name"))[:160]
                row = compact
                row_chars = min(max_chars, len(json.dumps(
                    row, ensure_ascii=False, separators=(",", ":")
                )) + 1)
            current.append(row)
            current_chars += row_chars
        if current:
            batches.append(current)
        return batches

    def _select_entities_for_prompt(
        self,
        project_id: str,
        *,
        hint: str = "",
        preferred_ids: list[str] | None = None,
        entity_types: set[str] | None = None,
        max_chars: int = 3200,
    ) -> list[dict[str, Any]]:
        """Select a bounded entity packet.

        Exact name/alias mentions and caller-provided IDs are only retrieval
        hints. They do not decide identity or business semantics.
        """
        hint_norm = _norm_name(hint)
        preferred = {
            _clean(x) for x in (preferred_ids or []) if _clean(x)
        }
        allowed = {
            _clean(x).lower() for x in (entity_types or set()) if _clean(x)
        }
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(self.entity_summary(project_id)):
            row = self._prompt_entity_row(item)
            if allowed and row["entity_type"] not in allowed:
                continue
            score = 0
            if row["entity_id"] in preferred:
                score += 10000
            names = [row["name"], *row["aliases"]]
            for name in names:
                norm = _norm_name(name)
                if norm and hint_norm and norm in hint_norm:
                    score += 2000 + min(len(norm), 80)
            ranked.append((score, -index, row))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)

        out: list[dict[str, Any]] = []
        used = 2
        for _, _, row in ranked:
            encoded = json.dumps(
                row, ensure_ascii=False, separators=(",", ":")
            )
            if out and used + len(encoded) + 1 > max_chars:
                continue
            if not out and len(encoded) + 2 > max_chars:
                row = {
                    "entity_id": row["entity_id"],
                    "entity_type": row["entity_type"],
                    "name": row["name"][:120],
                    "aliases": row["aliases"][:3],
                }
                encoded = json.dumps(
                    row, ensure_ascii=False, separators=(",", ":")
                )
            if used + len(encoded) + 1 <= max_chars:
                out.append(row)
                used += len(encoded) + 1
        return out

    def _known_entities_for_prompt(
        self,
        project_id: str,
        max_chars: int = 3600,
        hint: str = "",
    ) -> str:
        rows = self._select_entities_for_prompt(
            project_id, hint=hint, max_chars=max_chars
        )
        return "\n".join(
            json.dumps(x, ensure_ascii=False, separators=(",", ":"))
            for x in rows
        )

    def _compact_resolved_state(
        self,
        current: dict[str, Any],
        *,
        max_chars: int = 2200,
    ) -> str:
        compact = {
            "shot": {
                k: (current.get("shot") or {}).get(k)
                for k in ("shot_id", "title", "summary", "camera", "action")
                if (current.get("shot") or {}).get(k) not in (None, "")
            },
            "scene": {
                k: (current.get("scene") or {}).get(k)
                for k in ("scene_id", "title", "summary", "sequence")
                if (current.get("scene") or {}).get(k) not in (None, "")
            },
            "characters": [
                {
                    "entity_id": x.get("entity_id"),
                    "name": x.get("name"),
                    "state": x.get("state") or {},
                }
                for x in (current.get("characters") or [])
            ],
            "location": (
                {
                    "entity_id": (current.get("location") or {}).get("entity_id"),
                    "name": (current.get("location") or {}).get("name"),
                    "state": (current.get("location") or {}).get("state") or {},
                }
                if current.get("location") else None
            ),
            "props": [
                {
                    "entity_id": x.get("entity_id"),
                    "name": x.get("name"),
                    "state": x.get("state") or {},
                }
                for x in (current.get("props") or [])
            ],
        }
        raw = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(raw) <= max_chars:
            return raw
        # Keep IDs/names for every current entity, then spend remaining space
        # on state. This is deterministic context packing, not semantic judging.
        minimal = {
            "shot": compact["shot"],
            "scene": compact["scene"],
            "characters": [
                {"entity_id": x.get("entity_id"), "name": x.get("name")}
                for x in compact["characters"]
            ],
            "location": (
                {
                    "entity_id": compact["location"].get("entity_id"),
                    "name": compact["location"].get("name"),
                }
                if compact["location"] else None
            ),
            "props": [
                {"entity_id": x.get("entity_id"), "name": x.get("name")}
                for x in compact["props"]
            ],
        }
        return json.dumps(
            minimal, ensure_ascii=False, separators=(",", ":")
        )[:max_chars]

    def _identity_mentions(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        scenes = parsed.get("scenes") if isinstance(parsed, dict) else []
        if not isinstance(scenes, list):
            return rows
        for scene_index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            evidence = _clean(scene.get("source_excerpt"))[:500]
            location = scene.get("location")
            if isinstance(location, dict) and _clean(location.get("name")):
                rows.append({
                    "candidate_key": f"scene:{scene_index}:location:0",
                    "entity_type": "location",
                    "data": location,
                    "evidence": evidence,
                })
            for entity_type, field in (("character", "characters"), ("prop", "props")):
                items = scene.get(field)
                if not isinstance(items, list):
                    continue
                for item_index, data in enumerate(items):
                    if not isinstance(data, dict) or not _clean(data.get("name")):
                        continue
                    rows.append({
                        "candidate_key": f"scene:{scene_index}:{entity_type}:{item_index}",
                        "entity_type": entity_type,
                        "data": data,
                        "evidence": evidence,
                    })
        return rows

    async def _resolve_chunk_identities(
        self,
        project_id: str,
        parsed: dict[str, Any],
    ) -> dict[str, int]:
        """Resolve aliases with bounded semantic batches.

        Exact name/alias intersection is deterministic. Semantic calls are
        partitioned by entity type and bounded JSON packets, so project entity
        count can grow without creating one unbounded KNOWN_ENTITIES prompt.
        """
        stats = {
            "deterministic_matches": 0,
            "semantic_matches": 0,
            "semantic_groups": 0,
            "new_candidates": 0,
            "resolver_calls": 0,
            "resolver_batches": 0,
        }
        mentions = self._identity_mentions(parsed)
        if not mentions:
            return stats

        unresolved: list[dict[str, Any]] = []
        for row in mentions:
            data = row["data"]
            exact = self._exact_identity_match(
                project_id,
                entity_type=row["entity_type"],
                name=_clean(data.get("name")),
                aliases=(
                    data.get("aliases")
                    if isinstance(data.get("aliases"), list) else []
                ),
            )
            if exact is not None:
                data["_resolved_entity_id"] = _clean(exact.get("entity_id"))
                stats["deterministic_matches"] += 1
            else:
                unresolved.append(row)
        if not unresolved:
            return stats

        known_all = [
            {
                **self._prompt_entity_row(x),
                "core_profile": _copy(x.get("core_profile") or {}, {}),
            }
            for x in self.entity_summary(project_id)
            if _clean(x.get("entity_id"))
        ]
        known_by_id = {x["entity_id"]: x for x in known_all}

        request_rows = []
        for row in unresolved:
            data = row["data"]
            request_rows.append({
                "candidate_key": row["candidate_key"],
                "entity_type": row["entity_type"],
                "name": _clean(data.get("name")),
                "aliases": _copy(data.get("aliases") or [], [])[:8],
                "core_profile": _copy(data.get("core_profile") or {}, {}),
                "source_excerpt": row["evidence"][:360],
            })
        by_key = {x["candidate_key"]: x for x in unresolved}

        match_system = """你是故事实体身份解析器，只判断 CURRENT_CANDIDATES 是否与本批 EXISTING_ENTITIES 中某个已有实体为同一实体。
    不创作剧情，不创建实体 ID。

    规则：
    1. existing_entity_id 只能从本批 EXISTING_ENTITIES 选择。
    2. 必须实体类型一致。
    3. 可依据名称、别名、上下文指代、身份称谓和明确事实判断；不能仅凭相似外观/职业/地点强行合并。
    4. 证据不足必须 same_identity=false。
    5. confidence 必须真实反映证据强度。
    6. 返回严格 JSON，不要 Markdown。
    JSON：
    {"resolutions":[{"candidate_key":"","same_identity":false,"existing_entity_id":"","confidence":0.0}]}"""

        # Search existing entities in bounded batches. This may make several
        # small calls for a very large novel, but no single call grows with the
        # whole project.
        for entity_type in ("character", "location", "prop"):
            candidates = [
                x for x in request_rows
                if x["entity_type"] == entity_type
            ]
            if not candidates:
                continue
            known_type = [
                x for x in known_all
                if x["entity_type"] == entity_type
            ]
            if not known_type:
                continue
            candidate_batches = self._json_row_batches(
                candidates, max_chars=1500
            )
            known_batches = self._json_row_batches(
                known_type, max_chars=3200
            )
            for candidate_batch in candidate_batches:
                pending = {
                    x["candidate_key"] for x in candidate_batch
                    if not _clean(
                        by_key[x["candidate_key"]]["data"].get(
                            "_resolved_entity_id"
                        )
                    )
                }
                for known_batch in known_batches:
                    live = [
                        x for x in candidate_batch
                        if x["candidate_key"] in pending
                    ]
                    if not live:
                        break
                    prompt = (
                        "=== EXISTING_ENTITIES_BATCH ===\n"
                        + json.dumps(
                            known_batch, ensure_ascii=False,
                            separators=(",", ":")
                        )
                        + "\n\n=== CURRENT_CANDIDATES ===\n"
                        + json.dumps(
                            live, ensure_ascii=False,
                            separators=(",", ":")
                        )
                    )
                    try:
                        stats["resolver_calls"] += 1
                        stats["resolver_batches"] += 1
                        _, resolved, _ = (
                            await self.director._structured_json_call(
                                phase="story_entity_identity_resolution",
                                messages=[{
                                    "role": "user",
                                    "content": prompt,
                                }],
                                system_prompt=match_system,
                                temperature=0.0,
                                max_tokens=650,
                                contract=(
                                    '{"resolutions":[{"candidate_key":"",'
                                    '"same_identity":false,'
                                    '"existing_entity_id":"",'
                                    '"confidence":0.0}]}'
                                ),
                            )
                        )
                    except RuntimeError:
                        # Budget failure on one semantic batch is non-fatal:
                        # leave the candidate unresolved rather than overflow.
                        continue
                    except Exception:
                        continue
                    resolutions = (
                        resolved.get("resolutions")
                        if isinstance(resolved, dict) else []
                    )
                    if not isinstance(resolutions, list):
                        continue
                    batch_ids = {x["entity_id"] for x in known_batch}
                    for item in resolutions:
                        if (
                            not isinstance(item, dict)
                            or item.get("same_identity") is not True
                        ):
                            continue
                        key = _clean(item.get("candidate_key"))
                        if key not in pending or key not in by_key:
                            continue
                        try:
                            confidence = float(
                                item.get("confidence") or 0.0
                            )
                        except Exception:
                            confidence = 0.0
                        if confidence < 0.82:
                            continue
                        existing_id = _clean(
                            item.get("existing_entity_id")
                        )
                        known_item = known_by_id.get(existing_id)
                        row = by_key[key]
                        if (
                            existing_id in batch_ids
                            and known_item
                            and known_item["entity_type"]
                            == row["entity_type"]
                        ):
                            row["data"]["_resolved_entity_id"] = existing_id
                            pending.discard(key)
                            stats["semantic_matches"] += 1

        # Only the still-unresolved current mentions are grouped. This is a
        # separate fixed-schema call and never contains the project-wide entity
        # registry.
        remaining_rows = [
            x for x in request_rows
            if not _clean(
                by_key[x["candidate_key"]]["data"].get(
                    "_resolved_entity_id"
                )
            )
        ]
        group_system = """你是当前文本片段的实体同一性分组器。
    只判断 CURRENT_CANDIDATES 彼此是否明确是同一实体，例如同一人物在本片段出现本名、外号或称谓。
    不创建项目实体 ID，不跨实体类型合并，证据不足保持独立。
    返回严格 JSON：
    {"resolutions":[{"candidate_key":"","same_identity":false,"group_key":"","confidence":0.0}]}"""
        accepted_groups: dict[str, list[tuple[dict[str, Any], float]]] = {}
        for batch_index, batch in enumerate(
            self._json_row_batches(remaining_rows, max_chars=3600)
        ):
            if len(batch) < 2:
                continue
            prompt = (
                "=== CURRENT_CANDIDATES ===\n"
                + json.dumps(
                    batch, ensure_ascii=False, separators=(",", ":")
                )
            )
            try:
                stats["resolver_calls"] += 1
                stats["resolver_batches"] += 1
                _, resolved, _ = await self.director._structured_json_call(
                    phase="story_entity_identity_group",
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=group_system,
                    temperature=0.0,
                    max_tokens=650,
                    contract=(
                        '{"resolutions":[{"candidate_key":"",'
                        '"same_identity":false,"group_key":"",'
                        '"confidence":0.0}]}'
                    ),
                )
            except Exception:
                continue
            resolutions = (
                resolved.get("resolutions")
                if isinstance(resolved, dict) else []
            )
            if not isinstance(resolutions, list):
                continue
            batch_keys = {x["candidate_key"] for x in batch}
            for item in resolutions:
                if (
                    not isinstance(item, dict)
                    or item.get("same_identity") is not True
                ):
                    continue
                key = _clean(item.get("candidate_key"))
                row = by_key.get(key)
                if row is None or key not in batch_keys:
                    continue
                try:
                    confidence = float(item.get("confidence") or 0.0)
                except Exception:
                    confidence = 0.0
                if confidence < 0.82:
                    continue
                group = _clean(item.get("group_key"))
                if group:
                    accepted_groups.setdefault(
                        f"{batch_index}:{group}", []
                    ).append((row, confidence))

        for group, members in accepted_groups.items():
            types = {x[0]["entity_type"] for x in members}
            if len(members) < 2 or len(types) != 1:
                continue
            safe_group = "semantic:" + hashlib.sha1(
                (next(iter(types)) + ":" + group).encode("utf-8")
            ).hexdigest()[:16]
            for row, _ in members:
                if not _clean(
                    row["data"].get("_resolved_entity_id")
                ):
                    row["data"]["_identity_group"] = safe_group
            stats["semantic_groups"] += 1

        stats["new_candidates"] = sum(
            1 for row in unresolved
            if not _clean(row["data"].get("_resolved_entity_id"))
            and not _clean(row["data"].get("_identity_group"))
        )
        return stats


    async def analyze_project(self, project_id: str) -> dict[str, Any]:
        asset, text = self.source_text(project_id)
        source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        old = self.load(project_id)
        preserved_overrides = list(old.get("overrides") or [])
        state = self._empty(project_id)
        state["overrides"] = preserved_overrides
        state["analysis"].update({
            "status": "running",
            "message": "正在解析章节、场景、角色、地点和道具连续性",
            "source_asset_id": asset["asset_id"],
            "source_sha256": source_sha,
            "started_at": _now(),
            "updated_at": _now(),
        })
        self.save(project_id, state)

        chunk_chars = 1800
        overlap = 120
        starts = list(range(0, len(text), max(1, chunk_chars - overlap)))
        chunks = [(start, min(len(text), start + chunk_chars)) for start in starts]
        if chunks and chunks[-1][0] >= len(text):
            chunks.pop()
        state["analysis"]["chunks_total"] = len(chunks)
        self.save(project_id, state)

        carry = {"episode_title": "", "scene_title": ""}
        try:
            for index, (start, end) in enumerate(chunks):
                chunk = text[start:end]
                known = self._known_entities_for_prompt(
                    project_id, max_chars=1200, hint=chunk
                )
                system_prompt = """你是长篇故事连续性结构解析器，不进行文学创作。
只根据 SOURCE_CHUNK 中明确出现的事实抽取结构化信息。

目标：
- 识别当前片段属于哪个章节/集，以及其中发生的一个或多个 Scene；
- 识别每个 Scene 的地点、出场角色、重要武器/道具；
- 识别角色当前服装、发型、伤势、妆容、携带/装备等明确状态变化；
- 识别地点的长期变化（结构、损坏、陈设）与本场临时状态（时间、天气、灯光、人群、氛围）；
- 识别武器/道具的持有人、位置、外观版本、损坏/消耗等状态；
- 给每个 Scene 提取若干剧情 beat，供后续分镜使用。

规则：
1. 不使用关键词表推断；只按语义和上下文。
2. 没有明确变化的字段留空对象，不猜测。
3. 已知实体列表只用于复用 identity；同一人物/地点/物品应尽量返回同一名称。
4. 不把未来片段的信息倒推到当前片段。
5. source_excerpt 只摘录当前片段中支持该 Scene 的短证据。
6. 一个地点再次出现时保持同一地点实体；天气/时间/灯光属于本次 Scene 状态，不自动变成地点永久属性。
7. 角色表情/动作属于 performance；服装、伤势、装备等属于 state_patch。
8. 返回严格 JSON，不要 Markdown。

JSON：
{
  "episode":{"title":"","summary":""},
  "scenes":[{
    "title":"",
    "summary":"",
    "source_excerpt":"",
    "location":{
      "name":"",
      "aliases":[],
      "core_profile":{},
      "persistent_state_patch":{},
      "scene_state":{}
    },
    "characters":[{
      "name":"",
      "aliases":[],
      "core_profile":{},
      "state_patch":{},
      "performance":{}
    }],
    "props":[{
      "name":"",
      "aliases":[],
      "core_profile":{},
      "state_patch":{},
      "holder_name":""
    }],
    "beats":[{
      "summary":"",
      "character_names":[],
      "prop_names":[]
    }]
  }],
  "carry_forward":{"episode_title":"","scene_title":""}
}"""
                user_prompt = f"""=== PREVIOUS CARRY ===
{json.dumps(carry, ensure_ascii=False)}

=== KNOWN PROJECT ENTITIES ===
{known or "<none>"}

=== SOURCE RANGE ===
{start}:{end}

=== SOURCE_CHUNK ===
{chunk}
"""
                _, parsed, _ = await self.director._structured_json_call(
                    phase="story_continuity_extract",
                    messages=[{"role": "user", "content": user_prompt}],
                    system_prompt=system_prompt,
                    temperature=0.0,
                    max_tokens=1000,
                    contract='{"episode":{"title":"","summary":""},"scenes":[],"carry_forward":{"episode_title":"","scene_title":""}}',
                )
                parsed_obj = parsed if isinstance(parsed, dict) else {}
                identity_stats = await self._resolve_chunk_identities(project_id, parsed_obj)
                totals = state["analysis"].setdefault("identity_resolution", {
                    "deterministic_matches": 0, "semantic_matches": 0,
                    "semantic_groups": 0, "new_candidates": 0, "resolver_calls": 0,
                })
                for metric, value in identity_stats.items():
                    totals[metric] = int(totals.get(metric) or 0) + int(value or 0)
                self._merge_chunk(
                    project_id,
                    state,
                    parsed_obj,
                    chunk_start=start,
                    chunk_end=end,
                    chunk_index=index,
                )
                next_carry = parsed_obj.get("carry_forward")
                if isinstance(next_carry, dict):
                    carry = {
                        "episode_title": _clean(next_carry.get("episode_title")) or carry["episode_title"],
                        "scene_title": _clean(next_carry.get("scene_title")) or carry["scene_title"],
                    }
                state["analysis"]["chunks_done"] = index + 1
                state["analysis"]["updated_at"] = _now()
                state["analysis"]["message"] = f"已解析 {index + 1}/{len(chunks)} 个文本片段"
                self.save(project_id, state)

            if state["episodes"] and not state.get("active_episode_id"):
                state["active_episode_id"] = state["episodes"][0]["episode_id"]
            state["analysis"].update({
                "status": "ready",
                "message": "连续性分析完成",
                "updated_at": _now(),
                "error": "",
            })
            return self.save(project_id, state)
        except Exception as exc:
            state["analysis"].update({
                "status": "failed",
                "message": "连续性分析失败，可直接重试；项目原始文本和已有人工修正未丢失",
                "updated_at": _now(),
                "error": f"{type(exc).__name__}: {exc}",
            })
            self.save(project_id, state)
            raise

    def _episode(
        self,
        project_id: str,
        state: dict[str, Any],
        *,
        title: str,
        summary: str,
        chunk_start: int,
        chunk_end: int,
    ) -> dict[str, Any]:
        title = _clean(title) or "未命名章节/集"
        key = _norm_name(title)
        for ep in state["episodes"]:
            if _norm_name(ep.get("title")) == key:
                if summary and not ep.get("summary"):
                    ep["summary"] = summary
                ep["source_start"] = min(int(ep.get("source_start") or chunk_start), chunk_start)
                ep["source_end"] = max(int(ep.get("source_end") or chunk_end), chunk_end)
                return ep
        order = len(state["episodes"]) + 1
        entity = self.production.create_entity(
            project_id,
            entity_type="chapter",
            name=title,
            logical_key=f"continuity:episode:{order:05d}",
            metadata={"continuity": {"summary": summary, "order": order}},
        )
        ep = {
            "episode_id": "ep_" + secrets.token_hex(8),
            "entity_id": entity["entity_id"],
            "title": title,
            "order": order,
            "summary": summary,
            "source_start": chunk_start,
            "source_end": chunk_end,
        }
        state["episodes"].append(ep)
        return ep

    def _merge_chunk(
        self,
        project_id: str,
        state: dict[str, Any],
        parsed: dict[str, Any],
        *,
        chunk_start: int,
        chunk_end: int,
        chunk_index: int,
    ) -> None:
        ep_data = parsed.get("episode") if isinstance(parsed.get("episode"), dict) else {}
        ep_title = _clean(ep_data.get("title"))
        if not ep_title and state["episodes"]:
            ep = state["episodes"][-1]
            ep["source_end"] = max(int(ep.get("source_end") or chunk_end), chunk_end)
        else:
            ep = self._episode(
                project_id,
                state,
                title=ep_title,
                summary=_clean(ep_data.get("summary")),
                chunk_start=chunk_start,
                chunk_end=chunk_end,
            )

        scenes = parsed.get("scenes")
        if not isinstance(scenes, list):
            scenes = []
        identity_group_map: dict[str, str] = {}
        for scene_data in scenes:
            if not isinstance(scene_data, dict):
                continue
            title = _clean(scene_data.get("title")) or f"场景 {len(state['scenes']) + 1}"
            summary = _clean(scene_data.get("summary"))
            evidence = _clean(scene_data.get("source_excerpt"))[:800]
            scene_order = 1 + sum(1 for s in state["scenes"] if s.get("episode_id") == ep["episode_id"])
            sequence = len(state["scenes"]) + 1

            loc_data = scene_data.get("location") if isinstance(scene_data.get("location"), dict) else {}
            loc_entity_id = ""
            if _clean(loc_data.get("name")):
                loc_group = _clean(loc_data.get("_identity_group"))
                loc = self._find_entity(
                    project_id,
                    entity_type="location",
                    name=_clean(loc_data.get("name")),
                    aliases=loc_data.get("aliases") if isinstance(loc_data.get("aliases"), list) else [],
                    core_profile=loc_data.get("core_profile") if isinstance(loc_data.get("core_profile"), dict) else {},
                    preferred_entity_id=(
                        _clean(loc_data.get("_resolved_entity_id"))
                        or identity_group_map.get(loc_group, "")
                    ),
                )
                loc_entity_id = loc["entity_id"]
                if loc_group:
                    identity_group_map.setdefault(loc_group, loc_entity_id)

            scene_entity = self.production.create_entity(
                project_id,
                entity_type="scene",
                name=title,
                logical_key=f"continuity:scene:{sequence:06d}",
                metadata={
                    "continuity": {
                        "episode_id": ep["episode_id"],
                        "order": scene_order,
                        "sequence": sequence,
                        "summary": summary,
                        "source_excerpt": evidence,
                    }
                },
                evidence={
                    "source": "story_continuity",
                    "chunk_index": chunk_index,
                    "excerpt": evidence,
                } if evidence else None,
            )
            self.production.add_relation(
                project_id,
                source_id=ep["entity_id"],
                target_id=scene_entity["entity_id"],
                relation_type="contains",
                metadata={"source": "story_continuity"},
            )
            if loc_entity_id:
                self.production.add_relation(
                    project_id,
                    source_id=loc_entity_id,
                    target_id=scene_entity["entity_id"],
                    relation_type="location_of",
                    metadata={"source": "story_continuity"},
                )

            char_ids: list[str] = []
            char_name_map: dict[str, str] = {}
            chars = scene_data.get("characters")
            if not isinstance(chars, list):
                chars = []
            performances: dict[str, dict[str, Any]] = {}
            for char_data in chars:
                if not isinstance(char_data, dict) or not _clean(char_data.get("name")):
                    continue
                char_group = _clean(char_data.get("_identity_group"))
                char = self._find_entity(
                    project_id,
                    entity_type="character",
                    name=_clean(char_data.get("name")),
                    aliases=char_data.get("aliases") if isinstance(char_data.get("aliases"), list) else [],
                    core_profile=char_data.get("core_profile") if isinstance(char_data.get("core_profile"), dict) else {},
                    preferred_entity_id=(
                        _clean(char_data.get("_resolved_entity_id"))
                        or identity_group_map.get(char_group, "")
                    ),
                )
                cid = char["entity_id"]
                if char_group:
                    identity_group_map.setdefault(char_group, cid)
                char_ids.append(cid)
                char_meta = (char.get("metadata") or {}).get("continuity") or {}
                for identity_name in [
                    char_data.get("name"),
                    *(char_data.get("aliases") if isinstance(char_data.get("aliases"), list) else []),
                    char.get("name"),
                    *(char_meta.get("aliases") or []),
                ]:
                    norm = _norm_name(identity_name)
                    if norm:
                        char_name_map[norm] = cid
                self.production.add_relation(
                    project_id,
                    source_id=char["entity_id"],
                    target_id=scene_entity["entity_id"],
                    relation_type="appears_in",
                    metadata={"source": "story_continuity"},
                )
                patch = char_data.get("state_patch") if isinstance(char_data.get("state_patch"), dict) else {}
                if patch:
                    self._add_event(
                        state,
                        sequence=sequence,
                        episode_id=ep["episode_id"],
                        scene_id="",
                        target_type="character",
                        target_id=cid,
                        patch=patch,
                        scope="persistent",
                        source_kind="story",
                        source_ref=evidence,
                    )
                perf = char_data.get("performance") if isinstance(char_data.get("performance"), dict) else {}
                if perf:
                    performances[cid] = perf

            prop_ids: list[str] = []
            prop_name_map: dict[str, str] = {}
            props = scene_data.get("props")
            if not isinstance(props, list):
                props = []
            for prop_data in props:
                if not isinstance(prop_data, dict) or not _clean(prop_data.get("name")):
                    continue
                prop_group = _clean(prop_data.get("_identity_group"))
                prop = self._find_entity(
                    project_id,
                    entity_type="prop",
                    name=_clean(prop_data.get("name")),
                    aliases=prop_data.get("aliases") if isinstance(prop_data.get("aliases"), list) else [],
                    core_profile=prop_data.get("core_profile") if isinstance(prop_data.get("core_profile"), dict) else {},
                    preferred_entity_id=(
                        _clean(prop_data.get("_resolved_entity_id"))
                        or identity_group_map.get(prop_group, "")
                    ),
                )
                pid = prop["entity_id"]
                if prop_group:
                    identity_group_map.setdefault(prop_group, pid)
                prop_ids.append(pid)
                prop_meta = (prop.get("metadata") or {}).get("continuity") or {}
                for identity_name in [
                    prop_data.get("name"),
                    *(prop_data.get("aliases") if isinstance(prop_data.get("aliases"), list) else []),
                    prop.get("name"),
                    *(prop_meta.get("aliases") or []),
                ]:
                    norm = _norm_name(identity_name)
                    if norm:
                        prop_name_map[norm] = pid
                self.production.add_relation(
                    project_id,
                    source_id=prop["entity_id"],
                    target_id=scene_entity["entity_id"],
                    relation_type="appears_in",
                    metadata={"source": "story_continuity"},
                )
                patch = prop_data.get("state_patch") if isinstance(prop_data.get("state_patch"), dict) else {}
                holder_name = _clean(prop_data.get("holder_name"))
                if holder_name:
                    holder_id = char_name_map.get(_norm_name(holder_name))
                    if holder_id:
                        patch = _copy(patch, {})
                        patch["holder_entity_id"] = holder_id
                if patch:
                    self._add_event(
                        state,
                        sequence=sequence,
                        episode_id=ep["episode_id"],
                        scene_id="",
                        target_type="prop",
                        target_id=pid,
                        patch=patch,
                        scope="persistent",
                        source_kind="story",
                        source_ref=evidence,
                    )

            scene_id = "scn_" + secrets.token_hex(8)
            scene = {
                "scene_id": scene_id,
                "entity_id": scene_entity["entity_id"],
                "episode_id": ep["episode_id"],
                "title": title,
                "order": scene_order,
                "sequence": sequence,
                "summary": summary,
                "source_excerpt": evidence,
                "source_start": chunk_start,
                "source_end": chunk_end,
                "location_entity_id": loc_entity_id,
                "character_entity_ids": list(dict.fromkeys(char_ids)),
                "prop_entity_ids": list(dict.fromkeys(prop_ids)),
                "performances": performances,
            }
            state["scenes"].append(scene)

            # Rewrite scene_id on events created above for this sequence.
            for event in reversed(state["events"]):
                if event.get("sequence") != sequence:
                    break
                if not event.get("scene_id"):
                    event["scene_id"] = scene_id

            if loc_entity_id:
                persistent_patch = loc_data.get("persistent_state_patch") if isinstance(loc_data.get("persistent_state_patch"), dict) else {}
                scene_patch = loc_data.get("scene_state") if isinstance(loc_data.get("scene_state"), dict) else {}
                if persistent_patch:
                    self._add_event(
                        state,
                        sequence=sequence,
                        episode_id=ep["episode_id"],
                        scene_id=scene_id,
                        target_type="location",
                        target_id=loc_entity_id,
                        patch=persistent_patch,
                        scope="persistent",
                        source_kind="story",
                        source_ref=evidence,
                    )
                if scene_patch:
                    self._add_event(
                        state,
                        sequence=sequence,
                        episode_id=ep["episode_id"],
                        scene_id=scene_id,
                        target_type="location",
                        target_id=loc_entity_id,
                        patch=scene_patch,
                        scope="scene",
                        source_kind="story",
                        source_ref=evidence,
                    )

            beats = scene_data.get("beats")
            if isinstance(beats, list):
                for beat_index, beat in enumerate(beats[:12], 1):
                    if not isinstance(beat, dict) or not _clean(beat.get("summary")):
                        continue
                    shot_chars = []
                    for name in beat.get("character_names") or []:
                        cid = char_name_map.get(_norm_name(name))
                        if cid:
                            shot_chars.append(cid)
                    shot_props = []
                    for name in beat.get("prop_names") or []:
                        pid = prop_name_map.get(_norm_name(name))
                        if pid:
                            shot_props.append(pid)
                    state["shots"].append({
                        "shot_id": "seed_" + secrets.token_hex(8),
                        "entity_id": "",
                        "scene_id": scene_id,
                        "episode_id": ep["episode_id"],
                        "title": f"{title} · 节拍 {beat_index}",
                        "order": beat_index,
                        "sequence": sequence * 1000 + beat_index,
                        "summary": _clean(beat.get("summary")),
                        "character_entity_ids": shot_chars or list(dict.fromkeys(char_ids)),
                        "prop_entity_ids": shot_props or list(dict.fromkeys(prop_ids)),
                        "provisional": True,
                    })

    def _add_event(
        self,
        state: dict[str, Any],
        *,
        sequence: int,
        episode_id: str,
        scene_id: str,
        target_type: str,
        target_id: str,
        patch: dict[str, Any],
        scope: str,
        source_kind: str,
        source_ref: str,
        locked: bool = False,
        end_sequence: int | None = None,
        override_id: str = "",
    ) -> dict[str, Any]:
        event = {
            "event_id": "cev_" + secrets.token_hex(8),
            "sequence": int(sequence),
            "episode_id": episode_id,
            "scene_id": scene_id,
            "target_type": target_type,
            "target_id": target_id,
            "patch": _copy(patch, {}),
            "scope": scope,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "locked": bool(locked),
            "end_sequence": end_sequence,
            "override_id": override_id,
            "created_at": _now(),
        }
        state["events"].append(event)
        return event

    def _scene(self, state: dict[str, Any], scene_id: str) -> dict[str, Any]:
        for row in state.get("scenes") or []:
            if row.get("scene_id") == scene_id:
                return row
        raise FileNotFoundError(f"场景不存在：{scene_id}")

    def _shot(self, state: dict[str, Any], shot_id: str) -> dict[str, Any]:
        for row in state.get("shots") or []:
            if row.get("shot_id") == shot_id:
                return row
        raise FileNotFoundError(f"镜头不存在：{shot_id}")

    def _episode_by_id(self, state: dict[str, Any], episode_id: str) -> dict[str, Any]:
        for row in state.get("episodes") or []:
            if row.get("episode_id") == episode_id:
                return row
        raise FileNotFoundError(f"章节/集不存在：{episode_id}")

    def _sequence_bounds(
        self,
        state: dict[str, Any],
        *,
        anchor_type: str,
        anchor_id: str,
        scope: str,
    ) -> tuple[int, int | None, str, str]:
        if anchor_type == "shot":
            shot = self._shot(state, anchor_id)
            scene = self._scene(state, shot["scene_id"])
            sequence = int(shot.get("sequence") or scene.get("sequence") or 0)
        else:
            scene = self._scene(state, anchor_id)
            sequence = int(scene.get("sequence") or 0)

        episode_id = scene.get("episode_id") or ""
        scene_id = scene.get("scene_id") or ""
        if scope == "shot":
            return sequence, sequence, episode_id, scene_id
        if scope == "scene":
            base = int(scene.get("sequence") or 0)
            return base * 1000, base * 1000 + 999, episode_id, scene_id
        if scope == "episode":
            seqs = [
                int(x.get("sequence") or 0)
                for x in state.get("scenes") or []
                if x.get("episode_id") == episode_id
            ]
            if not seqs:
                return sequence, sequence, episode_id, scene_id
            return min(seqs) * 1000, max(seqs) * 1000 + 999, episode_id, scene_id
        return sequence, None, episode_id, scene_id

    def _active_events(
        self,
        state: dict[str, Any],
        *,
        target_id: str,
        target_type: str,
        sequence: int,
        scene_id: str,
        episode_id: str,
    ) -> list[dict[str, Any]]:
        normal: list[dict[str, Any]] = []
        locked: list[dict[str, Any]] = []
        for event in state.get("events") or []:
            if event.get("target_id") != target_id or event.get("target_type") != target_type:
                continue
            start = int(event.get("sequence") or 0)
            if start > sequence:
                continue
            end = event.get("end_sequence")
            if end is not None and sequence > int(end):
                continue
            scope = event.get("scope")
            if scope == "scene" and event.get("scene_id") != scene_id:
                continue
            if scope == "episode" and event.get("episode_id") != episode_id:
                continue
            (locked if event.get("locked") else normal).append(event)
        # Later story facts can supersede an earlier manual "from here" edit.
        # At the same sequence manual wins. Locked manual edits are applied last.
        priority = {"story": 0, "manual": 1}
        normal.sort(key=lambda x: (
            int(x.get("sequence") or 0),
            priority.get(_clean(x.get("source_kind")), 0),
            _clean(x.get("created_at")),
        ))
        locked.sort(key=lambda x: (
            int(x.get("sequence") or 0),
            _clean(x.get("created_at")),
        ))
        return normal + locked

    def _resolved_entity(
        self,
        project_id: str,
        state: dict[str, Any],
        *,
        entity_id: str,
        entity_type: str,
        sequence: int,
        scene_id: str,
        episode_id: str,
    ) -> dict[str, Any]:
        entity = self._entity_by_id(project_id, entity_id)
        cmeta = ((entity.get("metadata") or {}).get("continuity") or {})
        value = _copy(cmeta.get("default_state") or {}, {})
        trace: dict[str, dict[str, Any]] = {}
        for event in self._active_events(
            state,
            target_id=entity_id,
            target_type=entity_type,
            sequence=sequence,
            scene_id=scene_id,
            episode_id=episode_id,
        ):
            for path, item in _flatten_patch(event.get("patch") or {}):
                _set_path(value, path, item)
                trace[path] = {
                    "event_id": event.get("event_id"),
                    "source_kind": event.get("source_kind"),
                    "source_ref": event.get("source_ref"),
                    "sequence": event.get("sequence"),
                    "locked": bool(event.get("locked")),
                }
        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "name": entity.get("name"),
            "core_profile": _copy(cmeta.get("core_profile") or {}, {}),
            "state": value,
            "source_trace": trace,
            "references": _copy(cmeta.get("references") or [], []),
        }

    def resolve_scene(self, project_id: str, scene_id: str) -> dict[str, Any]:
        state = self.load(project_id)
        scene = self._scene(state, scene_id)
        sequence = int(scene.get("sequence") or 0) * 1000
        episode_id = scene.get("episode_id") or ""
        chars = [
            self._resolved_entity(
                project_id, state,
                entity_id=cid,
                entity_type="character",
                sequence=sequence,
                scene_id=scene_id,
                episode_id=episode_id,
            )
            for cid in scene.get("character_entity_ids") or []
        ]
        props = [
            self._resolved_entity(
                project_id, state,
                entity_id=pid,
                entity_type="prop",
                sequence=sequence,
                scene_id=scene_id,
                episode_id=episode_id,
            )
            for pid in scene.get("prop_entity_ids") or []
        ]
        location = None
        if scene.get("location_entity_id"):
            location = self._resolved_entity(
                project_id, state,
                entity_id=scene["location_entity_id"],
                entity_type="location",
                sequence=sequence,
                scene_id=scene_id,
                episode_id=episode_id,
            )
        return {
            "scene": _copy(scene, {}),
            "episode": _copy(self._episode_by_id(state, episode_id), {}),
            "characters": chars,
            "location": location,
            "props": props,
            "performances": _copy(scene.get("performances") or {}, {}),
        }

    def resolve_shot(self, project_id: str, shot_id: str) -> dict[str, Any]:
        state = self.load(project_id)
        shot = self._shot(state, shot_id)
        scene = self._scene(state, shot["scene_id"])
        sequence = int(shot.get("sequence") or int(scene.get("sequence") or 0) * 1000)
        episode_id = scene.get("episode_id") or ""
        char_ids = shot.get("character_entity_ids") or scene.get("character_entity_ids") or []
        prop_ids = shot.get("prop_entity_ids") or scene.get("prop_entity_ids") or []
        chars = [
            self._resolved_entity(
                project_id, state,
                entity_id=cid,
                entity_type="character",
                sequence=sequence,
                scene_id=scene["scene_id"],
                episode_id=episode_id,
            )
            for cid in char_ids
        ]
        props = [
            self._resolved_entity(
                project_id, state,
                entity_id=pid,
                entity_type="prop",
                sequence=sequence,
                scene_id=scene["scene_id"],
                episode_id=episode_id,
            )
            for pid in prop_ids
        ]
        location = None
        if scene.get("location_entity_id"):
            location = self._resolved_entity(
                project_id, state,
                entity_id=scene["location_entity_id"],
                entity_type="location",
                sequence=sequence,
                scene_id=scene["scene_id"],
                episode_id=episode_id,
            )
        return {
            "shot": _copy(shot, {}),
            "scene": _copy(scene, {}),
            "episode": _copy(self._episode_by_id(state, episode_id), {}),
            "characters": chars,
            "location": location,
            "props": props,
        }

    async def parse_manual_override(
        self,
        project_id: str,
        *,
        anchor_type: str,
        anchor_id: str,
        instruction: str,
        scope: str,
        locked: bool,
    ) -> dict[str, Any]:
        instruction = _clean(instruction)
        if not instruction:
            raise ValueError("调整说明不能为空")
        state = self.load(project_id)
        current = (
            self.resolve_shot(project_id, anchor_id)
            if anchor_type == "shot"
            else self.resolve_scene(project_id, anchor_id)
        )

        # Full registry is kept server-side only for validation. The model gets
        # a bounded, relevance-packed subset for each instruction chunk.
        all_entities = [
            self._prompt_entity_row(x)
            for x in self.entity_summary(project_id)
            if _clean(x.get("entity_id"))
        ]
        valid_entities = {
            x["entity_id"]: x for x in all_entities
        }
        preferred_ids: list[str] = []
        for group in (
            current.get("characters") or [],
            current.get("props") or [],
        ):
            preferred_ids.extend(
                _clean(x.get("entity_id"))
                for x in group if _clean(x.get("entity_id"))
            )
        if current.get("location"):
            lid = _clean(
                (current.get("location") or {}).get("entity_id")
            )
            if lid:
                preferred_ids.append(lid)

        current_packet = self._compact_resolved_state(
            current, max_chars=1900
        )
        instruction_chunks = self._text_chunks(
            instruction, max_chars=1800, overlap=120
        )
        system_prompt = """你是故事连续性的人工修正解析器。
    用户用自然语言修正当前故事状态。你的任务只是把本批修正转换为固定 JSON，不创作新剧情。

    规则：
    1. entity_id 必须从本批 KNOWN_ENTITIES 中选择；无法确定放 warnings，不猜实体。
    2. patch 只写本批 USER_ADJUSTMENT 明确要求改变的状态字段。
    3. 不使用关键词表；按语义解析。
    4. 不把省略的项目实体或历史状态假装成已知。
    5. 返回严格 JSON：
    {"changes":[{"entity_id":"","entity_type":"character|location|prop","patch":{}}],"warnings":[]}"""

        parsed_changes: list[dict[str, Any]] = []
        warnings: list[Any] = []
        for chunk_index, chunk in enumerate(instruction_chunks):
            known_prompt = self._select_entities_for_prompt(
                project_id,
                hint=chunk,
                preferred_ids=preferred_ids,
                max_chars=1900,
            )
            prompt = f"""=== KNOWN_ENTITIES_BATCH ===
    {json.dumps(known_prompt, ensure_ascii=False, separators=(",", ":"))}

    === CURRENT_RESOLVED_STATE_BOUNDED ===
    {current_packet}

    === USER_ADJUSTMENT_PART {chunk_index + 1}/{len(instruction_chunks)} ===
    {chunk}
    """
            try:
                _, parsed, _ = await self.director._structured_json_call(
                    phase="story_continuity_manual_override",
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=system_prompt,
                    temperature=0.0,
                    max_tokens=700,
                    contract=(
                        '{"changes":[{"entity_id":"",'
                        '"entity_type":"character|location|prop",'
                        '"patch":{}}],"warnings":[]}'
                    ),
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    f"人工修正第 {chunk_index + 1} 段上下文预算不足；"
                    "本次未写入任何部分修改"
                ) from exc
            changes = (
                parsed.get("changes")
                if isinstance(parsed, dict) else []
            )
            if isinstance(changes, list):
                parsed_changes.extend(
                    x for x in changes if isinstance(x, dict)
                )
            part_warnings = (
                parsed.get("warnings")
                if isinstance(parsed, dict) else []
            )
            if isinstance(part_warnings, list):
                warnings.extend(part_warnings)

        start, end, episode_id, scene_id = self._sequence_bounds(
            state,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            scope=scope,
        )
        override_id = "covr_" + secrets.token_hex(8)
        applied = []
        seen_changes: set[str] = set()
        for change in parsed_changes:
            entity_id = _clean(change.get("entity_id"))
            entity_type = _clean(
                change.get("entity_type")
            ).lower()
            patch = (
                change.get("patch")
                if isinstance(change.get("patch"), dict) else {}
            )
            ref = valid_entities.get(entity_id)
            if (
                not ref
                or ref["entity_type"] != entity_type
                or not patch
            ):
                continue
            signature = json.dumps(
                [entity_id, entity_type, patch],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if signature in seen_changes:
                continue
            seen_changes.add(signature)
            event = self._add_event(
                state,
                sequence=start,
                episode_id=episode_id,
                scene_id=scene_id,
                target_type=entity_type,
                target_id=entity_id,
                patch=patch,
                scope="persistent" if scope == "from_here" else scope,
                source_kind="manual",
                source_ref=instruction,
                locked=locked,
                end_sequence=end,
                override_id=override_id,
            )
            applied.append(event)

        record = {
            "override_id": override_id,
            "anchor_type": anchor_type,
            "anchor_id": anchor_id,
            "scope": scope,
            "instruction": instruction,
            "locked": bool(locked),
            "event_ids": [x["event_id"] for x in applied],
            "warnings": warnings,
            "input_chunks": len(instruction_chunks),
            "context_mode": "bounded_chunked_manual_override",
            "created_at": _now(),
        }
        state["overrides"].append(record)
        self.save(project_id, state)
        return {
            "override": record,
            "applied_events": applied,
            "resolved": (
                self.resolve_shot(project_id, anchor_id)
                if anchor_type == "shot"
                else self.resolve_scene(project_id, anchor_id)
            ),
        }

    def delete_override(self, project_id: str, override_id: str) -> dict[str, Any]:
        state = self.load(project_id)
        before = len(state["overrides"])
        state["overrides"] = [
            x for x in state["overrides"]
            if x.get("override_id") != override_id
        ]
        state["events"] = [
            x for x in state["events"]
            if x.get("override_id") != override_id
        ]
        if len(state["overrides"]) == before:
            raise FileNotFoundError(f"人工修正不存在：{override_id}")
        self.save(project_id, state)
        return {"override_id": override_id, "deleted": True}

    def set_active_episode(self, project_id: str, episode_id: str) -> dict[str, Any]:
        state = self.load(project_id)
        self._episode_by_id(state, episode_id)
        state["active_episode_id"] = episode_id
        self.save(project_id, state)
        return {"active_episode_id": episode_id}

    def episode_context(self, project_id: str, episode_id: str = "", max_chars: int = 9000) -> str:
        """Build a bounded packet that represents the whole chapter fairly."""
        state = self.load(project_id)
        eid = episode_id or state.get("active_episode_id") or ""
        if not eid and state["episodes"]:
            eid = state["episodes"][0]["episode_id"]
        if not eid:
            return ""
        episode = self._episode_by_id(state, eid)
        scenes = [x for x in state["scenes"] if x.get("episode_id") == eid]
        scenes.sort(key=lambda x: int(x.get("sequence") or 0))

        summary = _clean(episode.get("summary"))[:600]
        header = (
            f"章节/集：{episode.get('title')}\n"
            f"章节摘要：{summary}\n"
            f"场景总数：{len(scenes)}\n"
            "说明：以下是按原文分片解析后的全章事实索引；不是原文截断。"
            "场景描述会随场景数量自动压缩，但不会只保留前半章。"
        )
        if not scenes:
            return header[:max_chars]

        prefix = header + "\n\n=== 全章场景索引 ===\n"
        body_budget = max(0, max_chars - len(prefix))
        newline_budget = max(0, len(scenes) - 1)
        per_scene = max(8, (body_budget - newline_budget) // max(1, len(scenes)))
        rows: list[str] = []
        for index, scene in enumerate(scenes, 1):
            resolved = self.resolve_scene(project_id, scene["scene_id"])
            loc = resolved.get("location") or {}
            chars = resolved.get("characters") or []
            props = resolved.get("props") or []
            char_names = "、".join(_clean(x.get("name")) for x in chars if _clean(x.get("name")))
            prop_names = "、".join(_clean(x.get("name")) for x in props if _clean(x.get("name")))
            scene_id = f"S{index:03d}"

            if per_scene < 72:
                line = (
                    f"{scene_id}｜{_clean(scene.get('title'))}｜"
                    f"地:{_clean(loc.get('name'))}｜人:{char_names or '无'}"
                )
            else:
                state_bits: list[str] = []
                for item in chars[:4]:
                    st = item.get("state") or {}
                    if st:
                        state_bits.append(
                            f"{_clean(item.get('name'))}:{json.dumps(st, ensure_ascii=False, separators=(',', ':'))}"
                        )
                line = (
                    f"{scene_id}｜{_clean(scene.get('title'))}｜"
                    f"{_clean(scene.get('summary'))}｜地点:{_clean(loc.get('name'))}｜"
                    f"角色:{char_names or '无'}｜道具:{prop_names or '无'}"
                )
                if state_bits:
                    line += "｜状态:" + "；".join(state_bits)
                excerpt = _clean(scene.get("source_excerpt"))
                if excerpt:
                    line += "｜证据:" + excerpt[:180]

            if len(line) > per_scene:
                line = line[:per_scene]
            rows.append(line)

        packet = prefix + "\n".join(rows)
        if len(packet) > max_chars:
            compact_header = (
                f"章节/集：{episode.get('title')}\n"
                f"场景总数：{len(scenes)}\n=== 场景索引 ===\n"
            )
            compact_body_budget = max(0, max_chars - len(compact_header) - newline_budget)
            compact_each = max(8, compact_body_budget // max(1, len(scenes)))
            compact_rows = [
                (f"S{i:03d}｜{_clean(scene.get('title'))}")[:compact_each]
                for i, scene in enumerate(scenes, 1)
            ]
            packet = compact_header + "\n".join(compact_rows)
        return packet[:max_chars]

    def _reference_urls(self, project_id: str, resolved: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        seen = set()
        for group in [
            *(resolved.get("characters") or []),
            *([resolved.get("location")] if resolved.get("location") else []),
            *(resolved.get("props") or []),
        ]:
            if not isinstance(group, dict):
                continue
            for ref in group.get("references") or []:
                aid = _clean(ref.get("asset_id"))
                if not aid or aid in seen:
                    continue
                try:
                    url = self.production.asset_url(project_id, aid)
                except Exception:
                    continue
                if not url:
                    continue
                seen.add(aid)
                rows.append({
                    "asset_id": aid,
                    "url": url,
                    "role": ref.get("role"),
                    "variant_key": ref.get("variant_key"),
                    "label": ref.get("label"),
                    "entity_id": group.get("entity_id"),
                    "entity_type": group.get("entity_type"),
                    "entity_name": group.get("name"),
                })
        return rows

    def _shot_keyframe(self, project_id: str, shot: dict[str, Any]) -> str:
        entity_id = _clean(shot.get("entity_id"))
        shot_id = _clean(shot.get("shot_id"))
        candidates = []
        for asset in self.production.list_assets(project_id, active_only=True):
            if _clean(asset.get("status")).lower() != "ready" or _clean(asset.get("asset_type")).upper() != "IMAGE":
                continue
            meta = asset.get("metadata") or {}
            if (
                (entity_id and entity_id in (asset.get("entity_ids") or []))
                or _clean(meta.get("continuity_shot_id")) == shot_id
            ):
                url = self.production.asset_url(project_id, asset["asset_id"])
                if url:
                    candidates.append(url)
        return candidates[-1] if candidates else ""

    def production_context(self, project_id: str, shot_id: str) -> dict[str, Any]:
        resolved = self.resolve_shot(project_id, shot_id)
        shot = resolved["shot"]
        scene = resolved["scene"]
        characters = resolved.get("characters") or []
        location = resolved.get("location")
        props = resolved.get("props") or []
        lines = [
            f"镜头：{shot.get('summary') or shot.get('title') or ''}",
            f"场景：{scene.get('summary') or scene.get('title') or ''}",
        ]
        if location:
            lines.append(
                f"地点连续性：{location.get('name')}；"
                f"{json.dumps(location.get('state') or {}, ensure_ascii=False)}"
            )
        for char in characters:
            lines.append(
                f"角色连续性：{char.get('name')}；固定特征="
                f"{json.dumps(char.get('core_profile') or {}, ensure_ascii=False)}；"
                f"当前状态={json.dumps(char.get('state') or {}, ensure_ascii=False)}"
            )
        for prop in props:
            lines.append(
                f"道具连续性：{prop.get('name')}；"
                f"{json.dumps(prop.get('state') or {}, ensure_ascii=False)}"
            )
        constraint = "\n".join(lines)
        refs = self._reference_urls(project_id, resolved)
        primary = ""
        # Single-character identity reference is the safest automatic choice
        # for the existing one-reference workflow. Multi-character scenes are
        # not forced into one person's reference.
        if len(characters) == 1:
            cid = characters[0]["entity_id"]
            for ref in refs:
                if ref.get("entity_id") == cid and ref.get("role") in {"identity", "character_reference", "outfit"}:
                    primary = ref["url"]
                    break
            if not primary:
                for ref in refs:
                    if ref.get("entity_id") == cid:
                        primary = ref["url"]
                        break
        keyframe = self._shot_keyframe(project_id, shot)
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "resolved": resolved,
            "continuity_constraints": constraint,
            "image_prompt": constraint,
            "video_prompt": constraint,
            "references": refs,
            "primary_reference_url": primary,
            "keyframe_url": keyframe,
            "multi_character": len(characters) > 1,
        }

    def _storyboard_scene_candidates(
        self,
        state: dict[str, Any],
        text: str,
        *,
        cursor_sequence: int,
        max_chars: int = 1600,
    ) -> list[dict[str, Any]]:
        """Bound scene candidates by exact retrieval hints + sequence window."""
        hint = _norm_name(text)
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for scene in state.get("scenes") or []:
            seq = int(scene.get("sequence") or 0)
            title_norm = _norm_name(scene.get("title"))
            score = 0
            if title_norm and hint and title_norm in hint:
                score += 10000
            distance = abs(seq - max(1, int(cursor_sequence or 1)))
            if distance <= 2:
                score += 5000 - distance * 400
            elif 0 <= seq - cursor_sequence <= 18:
                score += 3000 - (seq - cursor_sequence) * 80
            elif distance <= 36:
                score += 700 - distance * 10
            row = {
                "scene_id": _clean(scene.get("scene_id")),
                "title": _clean(scene.get("title")),
                "summary": _clean(scene.get("summary"))[:140],
                "sequence": seq,
                "episode_id": _clean(scene.get("episode_id")),
                "character_entity_ids": list(
                    scene.get("character_entity_ids") or []
                )[:12],
                "prop_entity_ids": list(
                    scene.get("prop_entity_ids") or []
                )[:12],
                "location_entity_id": _clean(
                    scene.get("location_entity_id")
                ),
            }
            ranked.append((score, -seq, row))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out: list[dict[str, Any]] = []
        used = 2
        for _, _, row in ranked:
            encoded = json.dumps(
                row, ensure_ascii=False, separators=(",", ":")
            )
            if used + len(encoded) + 1 > max_chars:
                continue
            out.append(row)
            used += len(encoded) + 1
        out.sort(key=lambda x: int(x.get("sequence") or 0))
        return out

    def _storyboard_entity_candidates(
        self,
        project_id: str,
        text: str,
        scenes: list[dict[str, Any]],
        *,
        max_chars: int = 1000,
    ) -> list[dict[str, Any]]:
        preferred: list[str] = []
        for scene in scenes:
            preferred.extend(
                _clean(x)
                for x in (scene.get("character_entity_ids") or [])
                if _clean(x)
            )
            preferred.extend(
                _clean(x)
                for x in (scene.get("prop_entity_ids") or [])
                if _clean(x)
            )
            loc = _clean(scene.get("location_entity_id"))
            if loc:
                preferred.append(loc)
        return self._select_entities_for_prompt(
            project_id,
            hint=text,
            preferred_ids=preferred,
            entity_types={"character", "prop", "location"},
            max_chars=max_chars,
        )

    async def sync_storyboard(self, project_id: str) -> dict[str, Any]:
        assets = [
            a for a in self.production.list_assets(
                project_id, stage="04", active_only=True
            )
            if _clean(a.get("status")).lower() == "ready"
            and _clean(a.get("asset_type")).upper()
            in {"TEXT", "STRUCTURED_DATA", "FILE"}
        ]
        if not assets:
            return {
                "changed": False,
                "reason": "暂无 Stage04 分镜文本资产",
            }

        source_items: list[dict[str, Any]] = []
        source_hash = hashlib.sha256()
        for asset in assets:
            try:
                text = self.production.read_text_asset(
                    project_id,
                    asset["asset_id"],
                    max_chars=2_000_000,
                )
            except Exception:
                continue
            if not text:
                continue
            source_items.append({
                "asset_id": asset["asset_id"],
                "name": _clean(asset.get("name")),
                "text": text,
            })
            source_hash.update(
                (_clean(asset.get("asset_id")) + "\0" + text).encode(
                    "utf-8"
                )
            )
        if not source_items:
            return {
                "changed": False,
                "reason": "Stage04 分镜文本不可读取",
            }

        source_sha = source_hash.hexdigest()
        state = self.load(project_id)
        if state.get("storyboard_source_sha256") == source_sha:
            return {
                "changed": False,
                "reason": "分镜连续性已经是最新",
            }

        valid_scenes = {
            x["scene_id"]: x for x in state.get("scenes") or []
        }
        full_entities = [
            self._prompt_entity_row(x)
            for x in self.entity_summary(project_id)
            if _clean(x.get("entity_id"))
        ]
        valid_entities = {
            x["entity_id"]: x for x in full_entities
        }

        system_prompt = """你是项目分镜连续性对齐器。
    输入只是 Stage04 分镜文本的一小批、相关 Scene 候选和相关实体候选。
    把本批分镜里的 Shot 对齐到候选中的已有 Scene/角色/道具。

    规则：
    1. scene_id、character_entity_ids、prop_entity_ids 只能使用本批提供的已有 ID。
    2. 没有明确对应时 scene_id 为空，不猜。
    3. 不修改人物/地点状态；这里只建立 Shot 到 Scene/实体的引用。
    4. 按本批分镜原顺序返回。
    5. 不因当前只看到一批上下文而假装知道未提供的 Scene/实体。
    6. 返回严格 JSON：
    {"shots":[{"scene_id":"","title":"","summary":"","camera":"","action":"","character_entity_ids":[],"prop_entity_ids":[]}]}"""

        parsed_rows: list[dict[str, Any]] = []
        seen_rows: set[str] = set()
        cursor_sequence = 1
        batch_count = 0
        budget_skips = 0

        for source in source_items:
            chunks = self._text_chunks(
                source["text"], max_chars=1200, overlap=100
            )
            for chunk_index, chunk in enumerate(chunks):
                batch_count += 1
                scene_candidates = self._storyboard_scene_candidates(
                    state,
                    chunk,
                    cursor_sequence=cursor_sequence,
                    max_chars=1600,
                )
                entity_candidates = self._storyboard_entity_candidates(
                    project_id,
                    chunk,
                    scene_candidates,
                    max_chars=1000,
                )
                prompt = f"""=== SOURCE_ASSET ===
    {source["asset_id"]} / {source["name"]}

    === SCENE_CANDIDATES ===
    {json.dumps(scene_candidates, ensure_ascii=False, separators=(",", ":"))}

    === ENTITY_CANDIDATES ===
    {json.dumps(entity_candidates, ensure_ascii=False, separators=(",", ":"))}

    === STAGE04_BATCH {chunk_index + 1}/{len(chunks)} ===
    {chunk}
    """
                try:
                    _, parsed, _ = (
                        await self.director._structured_json_call(
                            phase="story_continuity_storyboard_sync_batch",
                            messages=[{
                                "role": "user",
                                "content": prompt,
                            }],
                            system_prompt=system_prompt,
                            temperature=0.0,
                            max_tokens=850,
                            contract=(
                                '{"shots":[{"scene_id":"","title":"",'
                                '"summary":"","camera":"","action":"",'
                                '"character_entity_ids":[],'
                                '"prop_entity_ids":[]}]}'
                            ),
                        )
                    )
                except RuntimeError:
                    # Never retry by sending a larger prompt. Record the skip
                    # and continue with later bounded batches.
                    budget_skips += 1
                    continue

                rows = (
                    parsed.get("shots")
                    if isinstance(parsed, dict) else []
                )
                if not isinstance(rows, list):
                    continue
                batch_scene_ids = {
                    x["scene_id"] for x in scene_candidates
                }
                batch_entity_ids = {
                    x["entity_id"] for x in entity_candidates
                }
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    scene_id = _clean(row.get("scene_id"))
                    if (
                        not scene_id
                        or scene_id not in batch_scene_ids
                        or scene_id not in valid_scenes
                    ):
                        continue
                    char_ids = [
                        _clean(x)
                        for x in (row.get("character_entity_ids") or [])
                        if (
                            _clean(x) in batch_entity_ids
                            and _clean(x) in valid_entities
                            and valid_entities[_clean(x)]["entity_type"]
                            == "character"
                        )
                    ]
                    prop_ids = [
                        _clean(x)
                        for x in (row.get("prop_entity_ids") or [])
                        if (
                            _clean(x) in batch_entity_ids
                            and _clean(x) in valid_entities
                            and valid_entities[_clean(x)]["entity_type"]
                            in {"prop", "item", "weapon"}
                        )
                    ]
                    normalized = {
                        "scene_id": scene_id,
                        "title": _clean(row.get("title")),
                        "summary": _clean(row.get("summary")),
                        "camera": _clean(row.get("camera")),
                        "action": _clean(row.get("action")),
                        "character_entity_ids": char_ids,
                        "prop_entity_ids": prop_ids,
                    }
                    signature = json.dumps(
                        normalized,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if signature in seen_rows:
                        continue
                    seen_rows.add(signature)
                    parsed_rows.append(normalized)
                    cursor_sequence = max(
                        cursor_sequence,
                        int(
                            valid_scenes[scene_id].get("sequence")
                            or cursor_sequence
                        ),
                    )

        if budget_skips:
            raise RuntimeError(
                f"Stage04 分镜有 {budget_skips} 个分批未通过上下文预算；"
                "本次不写入 storyboard_source_sha，可直接重试，不会把缺批结果标记为完成"
            )

        formal = []
        per_scene_order: dict[str, int] = {}
        for row in parsed_rows:
            scene_id = row["scene_id"]
            scene = valid_scenes.get(scene_id)
            if not scene:
                continue
            per_scene_order[scene_id] = (
                per_scene_order.get(scene_id, 0) + 1
            )
            order = per_scene_order[scene_id]
            entity = self.production.create_entity(
                project_id,
                entity_type="shot",
                name=(
                    row["title"]
                    or f"{scene['title']} · Shot {order:02d}"
                ),
                logical_key=(
                    f"continuity:shot:"
                    f"{int(scene['sequence']):06d}:{order:03d}"
                ),
                metadata={
                    "continuity": {
                        "scene_id": scene_id,
                        "episode_id": scene["episode_id"],
                        "order": order,
                        "camera": row["camera"],
                        "action": row["action"],
                    }
                },
            )
            self.production.add_relation(
                project_id,
                source_id=scene["entity_id"],
                target_id=entity["entity_id"],
                relation_type="contains",
                metadata={"source": "story_continuity_storyboard"},
            )
            char_ids = row["character_entity_ids"]
            prop_ids = row["prop_entity_ids"]
            for eid in [*char_ids, *prop_ids]:
                self.production.add_relation(
                    project_id,
                    source_id=eid,
                    target_id=entity["entity_id"],
                    relation_type="appears_in",
                    metadata={
                        "source": "story_continuity_storyboard"
                    },
                )
            formal.append({
                "shot_id": "shot_" + secrets.token_hex(8),
                "entity_id": entity["entity_id"],
                "scene_id": scene_id,
                "episode_id": scene["episode_id"],
                "title": entity["name"],
                "order": order,
                "sequence": int(scene["sequence"]) * 1000 + order,
                "summary": row["summary"],
                "camera": row["camera"],
                "action": row["action"],
                "character_entity_ids": (
                    char_ids
                    or list(scene.get("character_entity_ids") or [])
                ),
                "prop_entity_ids": (
                    prop_ids
                    or list(scene.get("prop_entity_ids") or [])
                ),
                "provisional": False,
            })

        state["shots"] = [
            x for x in state["shots"] if bool(x.get("provisional"))
        ] + formal
        state["storyboard_source_sha256"] = source_sha
        state["storyboard_context"] = {
            "mode": "bounded_batches",
            "batches": batch_count,
            "budget_skips": budget_skips,
            "source_assets": len(source_items),
            "formal_shots": len(formal),
            "updated_at": _now(),
        }
        self.save(project_id, state)
        return {
            "changed": True,
            "formal_shots": len(formal),
            "batches": batch_count,
            "budget_skips": budget_skips,
            "context_mode": "bounded_batches",
        }
