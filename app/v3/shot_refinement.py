from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .quality_policy import profile_for_shot


def _clean(value: Any) -> str:
    return str(value or "").strip()


class ShotRefinementService:
    """Turn a cheap preview into a separate high-quality candidate.

    Refinement never adopts or replaces the preview automatically.  It preserves
    the preview seed and production inputs, raises the quality budget, and sends
    a brand-new candidate through the same V3 generation/adoption path.
    """

    def __init__(self, legacy_runtime: Any) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director

    def _candidate(self, project_id: str, candidate_id: str) -> dict[str, Any]:
        loader = getattr(self.legacy, "_wb_load_candidates", None)
        if not callable(loader):
            raise RuntimeError("候选版本存储不可用")
        for row in loader(project_id) or []:
            if _clean(row.get("candidate_id")) == _clean(candidate_id):
                return dict(row)
        raise FileNotFoundError("图片候选不存在")

    def _formal_shot(self, project_id: str, target: dict[str, Any]) -> dict[str, Any]:
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        source = target.get("source") if isinstance(target.get("source"), dict) else {}
        shot_id = _clean(metadata.get("shot_id") or source.get("shot_id"))
        loader = getattr(self.legacy, "_studio_formal_shot", None)
        if shot_id and callable(loader):
            value = loader(project_id, shot_id)
            if isinstance(value, dict):
                return dict(value)
        return dict(metadata)

    async def refine(self, project_id: str, candidate_id: str) -> dict[str, Any]:
        row = self._candidate(project_id, candidate_id)
        if _clean(row.get("capability")).lower() != "image":
            raise ValueError("只有图片候选支持高质量精修")
        if _clean(row.get("status")).lower() != "completed":
            raise ValueError("图片候选尚未生成完成")
        if _clean(row.get("confirmed_asset_id")):
            raise ValueError("这个候选已经采用；请从当前正式画面重新生成新候选")

        params = dict(row.get("params") or {}) if isinstance(row.get("params"), dict) else {}
        if _clean(params.get("quality_stage")).lower() == "final":
            raise ValueError("当前已经是高质量精修候选")

        target_asset_id = _clean(row.get("target_asset_id"))
        prompt_asset_id = _clean(row.get("prompt_asset_id"))
        if not target_asset_id or not prompt_asset_id:
            raise ValueError("候选缺少正式镜头或提示词血缘，不能精修")
        target = self.director.production.get_asset(project_id, target_asset_id)
        formal = self._formal_shot(project_id, target)
        profile = profile_for_shot(formal)

        # Keep the same deterministic seed and framing inputs.  The current
        # FaceID workflow does not accept the preview as a second img2img input,
        # so the product names this accurately: same-seed high-quality rerender,
        # not pixel-perfect composition locking.
        params.update(
            {
                "quality_mode": "smart",
                "quality_stage": "final",
                "quality_tier": profile.tier,
                "steps": profile.image_final_steps,
                "final_refine_steps": profile.image_final_steps,
                "preview_candidate_id": _clean(candidate_id),
                "seed": int(params.get("seed") or 0),
            }
        )
        submit = getattr(self.legacy, "director_workbench_execute_candidate", None)
        if not callable(submit):
            raise RuntimeError("图片生产入口不可用")
        result = await submit(
            project_id,
            {
                "target_asset_id": target_asset_id,
                "capability": "image",
                "mode": _clean(row.get("mode")) or "txt2img",
                "prompt_asset_id": prompt_asset_id,
                "params": params,
                "metadata": {
                    "quality_stage": "final",
                    "quality_tier": profile.tier,
                    "preview_candidate_id": _clean(candidate_id),
                },
            },
        )
        return {
            "submitted": True,
            "preview_candidate_id": _clean(candidate_id),
            "quality_tier": profile.tier,
            "final_steps": profile.image_final_steps,
            "result": result,
            "manual_adoption_required": True,
        }


def create_shot_refinement_router(legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = ShotRefinementService(legacy_runtime)

    @router.post("/api/v3/studio/projects/{project_id}/image-candidates/{candidate_id}/refine")
    async def refine_image_candidate(project_id: str, candidate_id: str) -> dict[str, Any]:
        try:
            return await service.refine(project_id, candidate_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router


__all__ = ["ShotRefinementService", "create_shot_refinement_router"]
