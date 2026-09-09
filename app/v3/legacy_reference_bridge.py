from __future__ import annotations

from typing import Any

from app.config import Settings
from app.v3.contracts import Capability
from app.v3.legacy_candidate_bridge import LegacyCandidateV3Bridge
from app.v3.provider_catalog import build_provider_registry
from app.v3.reference_assets import ReferenceAssetBootstrap


class ReferenceAwareLegacyCandidateV3Bridge(LegacyCandidateV3Bridge):
    """Resolve shot references from the adopted reusable asset set.

    The original bridge only looked at the ephemeral shot target's entity id.
    Stage04, however, stores the reusable character/prop ids on the formal Shot.
    This class consumes those canonical ids, matching the reference/version
    discipline used by the referenced creative-asset architecture.
    """

    def __init__(self, settings: Settings, legacy: Any) -> None:
        super().__init__(settings, legacy)
        self.providers = build_provider_registry(settings)
        self.reference_bootstrap = ReferenceAssetBootstrap(
            legacy,
            submit_candidate=self.original_execute,
        )

    def _relevant_entity_ids(self, project_id: str, target: dict[str, Any]) -> set[str]:
        result = {str(item) for item in target.get("entity_ids") or [] if str(item)}
        shot_id = self._shot_id(target)
        formal: dict[str, Any] = {}
        loader = getattr(self.legacy, "_studio_formal_shot", None)
        if shot_id and callable(loader):
            try:
                raw = loader(project_id, shot_id)
                formal = raw if isinstance(raw, dict) else {}
            except Exception:
                formal = {}
        for field in ("character_entity_ids", "prop_entity_ids"):
            result.update(str(item) for item in formal.get(field) or [] if str(item))

        # Scene IDs in StoryContinuity are not guaranteed to equal production
        # entity IDs. Resolve only explicit metadata bindings; never name-guess.
        scene_id = str(formal.get("scene_id") or (target.get("metadata") or {}).get("scene_id") or "").strip()
        if scene_id:
            for entity in self.legacy.director.production.list_entities(project_id):
                metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
                if (
                    str(entity.get("entity_id") or "").strip() == scene_id
                    or str(metadata.get("scene_id") or "").strip() == scene_id
                    or str(metadata.get("continuity_scene_id") or "").strip() == scene_id
                ):
                    result.add(str(entity.get("entity_id") or "").strip())
        return {item for item in result if item}

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
            "scene_reference", "location_reference", "prop_reference", "item_reference",
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
            # Do not silently use an unrelated reference when the asset is
            # explicitly bound to a different reusable entity.
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
                "当前镜头没有已采用的一致性参考图。可以不上传参考图：系统会先自动生成角色/场景参考图候选，采用后再生成分镜画面。"
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
        if capability == "image" and target_asset_id:
            target = self.legacy.director.production.get_asset(project_id, target_asset_id)
            if self._shot_id(target):
                try:
                    self._candidate_reference_ids(project_id, target)
                except ValueError as missing:
                    relevant = self._relevant_entity_ids(project_id, target)
                    prepared = await self.reference_bootstrap.generate_first_missing_for_entities(
                        project_id,
                        sorted(relevant),
                    )
                    if prepared is not None:
                        candidate = prepared.get("candidate") if isinstance(prepared, dict) else None
                        state = str((candidate or {}).get("status") or "").strip().lower()
                        if state == "completed":
                            raise ValueError(
                                "一致性参考图候选已经生成，请先预览并点击“采用”，然后再次生成分镜画面。"
                            ) from missing
                        raise ValueError(
                            "当前镜头缺少已采用参考图，系统已自动开始生成一致性参考图候选。候选完成后先点击“采用”，再生成分镜画面。"
                        ) from missing
                    raise
        return await super().execute_candidate(project_id, payload)


__all__ = ["ReferenceAwareLegacyCandidateV3Bridge"]
