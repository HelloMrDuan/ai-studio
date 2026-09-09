from __future__ import annotations

from typing import Any

from app.config import Settings
from app.v3.contracts import Capability
from app.v3.legacy_candidate_bridge import LegacyCandidateV3Bridge
from app.v3.provider_catalog import build_provider_registry
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap
from app.v3.quality_policy import apply_smart_candidate_params, infer_quality_tier
from app.v3.shot_authoring import ShotAuthoringService


class ReferenceAwareLegacyCandidateV3Bridge(LegacyCandidateV3Bridge):
    """Resolve adopted reusable assets, smart quality and local shot revisions."""

    def __init__(self, settings: Settings, legacy: Any) -> None:
        super().__init__(settings, legacy)
        self.providers = build_provider_registry(settings)
        self.reference_bootstrap = CanonicalReferenceAssetBootstrap(
            legacy,
            submit_candidate=self.original_execute,
        )
        self.shot_authoring = ShotAuthoringService(settings, legacy)

    def _formal_shot(self, project_id: str, target: dict[str, Any]) -> dict[str, Any]:
        shot_id = self._shot_id(target)
        loader = getattr(self.legacy, "_studio_formal_shot", None)
        if shot_id and callable(loader):
            try:
                raw = loader(project_id, shot_id)
                if isinstance(raw, dict):
                    return dict(raw)
            except Exception:
                pass
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        return dict(metadata)

    def _relevant_entity_ids(self, project_id: str, target: dict[str, Any]) -> set[str]:
        result = {str(item) for item in target.get("entity_ids") or [] if str(item)}
        formal = self._formal_shot(project_id, target)
        for field in ("character_entity_ids", "prop_entity_ids"):
            result.update(str(item) for item in formal.get(field) or [] if str(item))

        # Narrative scene itself is not a reusable visual asset. Resolve its
        # canonical location binding only when the project metadata provides it.
        scene_id = str(formal.get("scene_id") or (target.get("metadata") or {}).get("scene_id") or "").strip()
        if scene_id:
            continuity_path = self.settings.data_dir / "story_continuity" / f"{project_id}.json"
            try:
                import json
                state = json.loads(continuity_path.read_text(encoding="utf-8"))
                scene = next(
                    (row for row in state.get("scenes") or [] if str(row.get("scene_id") or "").strip() == scene_id),
                    {},
                )
                location_id = str(scene.get("location_entity_id") or "").strip()
                if location_id:
                    result.add(location_id)
            except Exception:
                pass
        canonical = set()
        for entity in self.legacy.director.production.list_entities(project_id):
            entity_id = str(entity.get("entity_id") or "").strip()
            kind = str(entity.get("entity_type") or "").strip().lower()
            if entity_id in result and kind in {"character", "location", "prop"}:
                canonical.add(entity_id)
        return canonical

    def _reference_limit(self) -> int:
        selected = self.providers.resolve(
            {Capability.image_generation, Capability.image_reference},
            provider_id="local-comfyui-image",
            model_id="configured-image-workflow",
        )
        return max(1, int(selected.spec.max_references or 1))

    def _candidate_reference_ids(self, project_id: str, target: dict[str, Any]) -> list[str]:
        relevant = self._relevant_entity_ids(project_id, target)
        preferred_roles = {
            "character_reference", "character_turnaround", "character_consistency",
            "location_reference", "prop_reference", "item_reference",
        }
        rows: list[dict[str, Any]] = []
        for item in self.legacy.director.production.list_assets(project_id, active_only=True):
            if str(item.get("asset_type") or "").upper() != "IMAGE":
                continue
            if self._status_value(item.get("status")) != "ready":
                continue
            if self._status_value(item.get("dependency_state")) == "stale":
                continue
            if str(item.get("asset_role") or "") not in preferred_roles:
                continue
            entities = {str(value) for value in item.get("entity_ids") or [] if str(value)}
            if relevant and entities and not (relevant & entities):
                continue
            if relevant and not entities:
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                declared = str(metadata.get("reference_entity_id") or "").strip()
                if declared and declared not in relevant:
                    continue
            rows.append(item)

        rows.sort(
            key=lambda item: (
                0 if relevant & {str(v) for v in item.get("entity_ids") or [] if str(v)} else 1,
                0 if str(item.get("asset_role") or "") in {"character_reference", "character_turnaround"} else 1,
                -int(item.get("version") or 0),
                str(item.get("asset_id") or ""),
            )
        )
        if not rows:
            raise ValueError(
                "当前镜头没有已采用的一致性参考图。可以不上传参考图：系统会批量生成缺失的角色、地点和道具参考图候选，采用后再生成分镜画面。"
            )

        limit = self._reference_limit()
        selected_rows: list[dict[str, Any]] = []
        used_entities: set[str] = set()
        for item in rows:
            entities = {str(value) for value in item.get("entity_ids") or [] if str(value)}
            matched = entities & relevant
            identity = sorted(matched or entities)
            entity_key = identity[0] if identity else str(item.get("asset_id") or "")
            if entity_key in used_entities:
                continue
            used_entities.add(entity_key)
            selected_rows.append(item)
            if len(selected_rows) >= limit:
                break

        refs: list[str] = []
        for item in selected_rows:
            asset_id = str(item.get("asset_id") or "")
            path = self._asset_path(project_id, asset_id)
            ref_id = f"legacy:{project_id}:{asset_id}"
            entities = {str(value) for value in item.get("entity_ids") or [] if str(value)}
            matched = sorted(entities & relevant)
            self.references.import_file(
                ref_id,
                path,
                entity_id=matched[0] if matched else (sorted(entities)[0] if entities else ""),
            )
            refs.append(ref_id)
        return refs

    async def execute_candidate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capability = str(payload.get("capability") or "").strip().lower()
        target_asset_id = str(payload.get("target_asset_id") or "").strip()
        next_payload = dict(payload)
        if target_asset_id:
            target = self.legacy.director.production.get_asset(project_id, target_asset_id)
            if self._shot_id(target):
                target = self.shot_authoring.bind_active_contract_to_target(project_id, target)
                formal = self._formal_shot(project_id, target)
                next_payload["params"] = apply_smart_candidate_params(
                    payload.get("params") if isinstance(payload.get("params"), dict) else {},
                    shot=formal,
                    capability=capability,
                )
                next_payload.setdefault("metadata", {})
                if isinstance(next_payload["metadata"], dict):
                    next_payload["metadata"]["quality_tier"] = infer_quality_tier(formal)
                    next_payload["metadata"]["quality_mode"] = "smart"

                if capability == "image":
                    try:
                        self._candidate_reference_ids(project_id, target)
                    except ValueError as missing:
                        # First entry into the image workspace prepares every
                        # missing canonical reference for the project.  This
                        # keeps ComfyUI hot and avoids character→Qwen→location
                        # workspace ping-pong.  Candidates still require manual
                        # adoption before any shot image is allowed to run.
                        prepared = await self.reference_bootstrap.generate_missing(project_id)
                        submitted = list(prepared.get("submitted_entity_ids") or [])
                        waiting = list(prepared.get("waiting_adoption_entity_ids") or [])
                        if submitted:
                            raise ValueError(
                                f"当前作品缺少已采用参考图，系统已批量开始生成 {len(submitted)} 个一致性参考候选。"
                                "候选完成后统一预览并采用，再生成分镜画面。"
                            ) from missing
                        if waiting:
                            raise ValueError(
                                f"当前有 {len(waiting)} 个一致性参考候选等待采用。请先统一预览并采用，再生成分镜画面。"
                            ) from missing
                        raise
        return await super().execute_candidate(project_id, next_payload)


__all__ = ["ReferenceAwareLegacyCandidateV3Bridge"]
