from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .asset_authoring_refined import RefinedAuthoringAssetService, _clean, _STAGE_BY_TYPE, _STAGE_ORDER_INDEX
from .character_appearances import CharacterAppearanceService
from .character_asset_ownership_repair import CharacterAssetOwnershipRepair
from .character_identity_cleanup import CharacterIdentityCleanupService
from .stage_asset_materialization import StageOutputAssetMaterializer
from .stage_asset_materialization_guard import install_stage_asset_materialization_guard
from .stage_visual_asset_alias_recovery import install_stage_visual_asset_alias_recovery


install_stage_asset_materialization_guard()
install_stage_visual_asset_alias_recovery()


class ProductionAuthoringAssetService(RefinedAuthoringAssetService):
    """Production-ready authoring assets visible as soon as Stage②/③ is ready.

    A stage reaching 100% means the generated result has passed local readiness;
    users must be able to inspect/edit the resulting assets *before* clicking
    the manual stage-confirm button. Materialization is deterministic and uses
    the already generated stage text, so it adds zero model calls.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        super().__init__(settings, legacy_runtime)
        self.materializer = StageOutputAssetMaterializer(legacy_runtime)
        self.ownership_repair = CharacterAssetOwnershipRepair(legacy_runtime)
        self.identity_cleanup = CharacterIdentityCleanupService(legacy_runtime)
        self.appearances = CharacterAppearanceService(legacy_runtime)

    @staticmethod
    def _stage_ready(project: dict[str, Any], stage: str) -> bool:
        completed = {_clean(item) for item in project.get("completed_stages") or []}
        if stage in completed:
            return True
        if _clean(project.get("current_stage")) != stage:
            return False
        state = ((project.get("stage_state") or {}).get(stage) or {})
        runtime = state.get("skill_runtime") if isinstance(state.get("skill_runtime"), dict) else {}
        completion = runtime.get("completion") if isinstance(runtime.get("completion"), dict) else {}
        return bool(state.get("stage_ready")) or bool(completion.get("ready"))

    @classmethod
    def _stage_available(cls, project: dict[str, Any], kind: str) -> bool:
        required = _STAGE_BY_TYPE[kind]
        if cls._stage_ready(project, required):
            return True
        current = _clean(project.get("current_stage"))
        return _STAGE_ORDER_INDEX.get(current, 0) > _STAGE_ORDER_INDEX[required]

    def sync(self, project_id: str) -> dict[str, Any]:
        # First materialize the ready Stage②/③ draft. Then repair historical
        # character ownership before creating canonical profiles so one person's
        # appearance can never be attached to another person's entity id.
        materialized = self.materializer.materialize(project_id)
        ownership_before = self.ownership_repair.reconcile(project_id)
        identity_cleanup = self.identity_cleanup.reconcile_project(project_id)
        result = super().sync(project_id)
        ownership_after = self.ownership_repair.reconcile(project_id)

        # If ownership repair had to create/re-home a character profile, run the
        # idempotent profile sync once more so the authoring panel sees the fixed
        # canonical graph in the same request. No model call is involved.
        if bool(ownership_after.get("changed")):
            result = super().sync(project_id)

        # Default appearance v1 is a real asset, not a UI placeholder. Create it
        # immediately after the Stage② character profile exists. Because the
        # identity cleanup/ownership repair run first, the appearance inherits
        # the right character and never inherits shot-only background text.
        appearance_ids: list[str] = []
        project = self.director.get_project(project_id)
        if self._stage_available(project, "character"):
            for canonical, _rows in self._canonical_groups(project_id):
                if _clean(canonical.get("entity_type")).lower() != "character":
                    continue
                try:
                    appearance = self.appearances.ensure_default(project_id, _clean(canonical.get("entity_id")))
                except (FileNotFoundError, ValueError):
                    appearance = None
                if appearance and _clean(appearance.get("asset_id")):
                    appearance_ids.append(_clean(appearance.get("asset_id")))

        # One last local pass repairs any old default-appearance asset that was
        # already persisted under the wrong entity id before this build.
        ownership_final = self.ownership_repair.reconcile(project_id)

        return {
            **result,
            "stage_output_materialization": materialized,
            "character_ownership_repair": {
                "before": ownership_before,
                "after": ownership_after,
                "final": ownership_final,
            },
            "character_identity_cleanup": identity_cleanup,
            "default_character_appearance_asset_ids": appearance_ids,
            "ready_stage_assets_visible_before_confirmation": True,
            "model_calls_added": 0,
        }

    def status(self, project_id: str) -> dict[str, Any]:
        state = super().status(project_id)
        # super().status() dispatches through self.sync(), so the ready Stage②/③
        # draft has already been materialized, sanitized, ownership-repaired and
        # versioned.
        return {
            **state,
            "ready_stage_assets_visible_before_confirmation": True,
            "model_calls_added": 0,
        }

    def reconcile_existing_projects(self) -> dict[str, int]:
        scanned = 0
        changed = 0
        for project in self.director.list_projects():
            project_id = _clean(project.get("project_id"))
            if not project_id:
                continue
            scanned += 1
            try:
                result = self.sync(project_id)
            except Exception:
                continue
            materialized = result.get("stage_output_materialization") or {}
            cleanup = result.get("character_identity_cleanup") or {}
            ownership = result.get("character_ownership_repair") or {}
            ownership_changed = any(bool((ownership.get(key) or {}).get("changed")) for key in ("before", "after", "final"))
            if bool(materialized.get("materialized")) or bool(cleanup.get("changed")) or ownership_changed or result.get("profile_asset_ids"):
                changed += 1
        return {"scanned": scanned, "reconciled": changed}


def create_production_authoring_asset_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = ProductionAuthoringAssetService(settings, legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/authoring-assets")
    async def authoring_assets(project_id: str) -> dict[str, Any]:
        try:
            return service.status(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/v3/studio/projects/{project_id}/authoring-assets/{entity_id}")
    async def update_authoring_asset(project_id: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return service.update_profile(
                project_id,
                entity_id,
                stable_design=_clean(payload.get("stable_design")),
                change_reason=_clean(payload.get("change_reason")),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/authoring-assets/sync")
    async def sync_authoring_assets(project_id: str) -> dict[str, Any]:
        try:
            return service.sync(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


__all__ = ["ProductionAuthoringAssetService", "create_production_authoring_asset_router"]
