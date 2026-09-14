from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from fastapi import APIRouter

from app.services.comfyui import (
    ZIMAGE_TURBO_CLIP,
    ZIMAGE_TURBO_KEY,
    ZIMAGE_TURBO_UNET,
    ZIMAGE_TURBO_VAE,
)


logger = logging.getLogger(__name__)

_REFERENCE_ROLES = {
    "character_reference",
    "character_face_anchor",
    "character_costume_reference",
    "character_turnaround",
    "character_consistency",
    "character_identity",
    "scene_reference",
    "location_reference",
    "prop_reference",
}
_CHARACTER_HYBRID_PHASES = {"costume", "turnaround"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


class V3RuntimeModelContract:
    """Fail-closed V3 model routing.

    Text authoring/semantic calls are pinned to the required Qwen alias.
    Character reference-package rendering is pinned to Z-Image-Turbo for the
    actual pixels. Adopted face anchors are applied afterwards by the production
    worker's FaceFusion identity pass, so reference input no longer silently
    swaps the renderer to an unrelated SDXL checkpoint. Other reference-aware
    image domains may continue to use the configured SDXL/IP-Adapter provider.
    """

    def __init__(self, settings: Any, legacy_runtime: Any, bridge: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.bridge = bridge
        self.production = legacy_runtime.director.production
        self.llm = legacy_runtime.director.llm
        self.required_text_model = _clean(settings.stage04_required_model_alias) or "qwen3-32b"
        self._original_llm_request = getattr(self.llm, "_request_messages")
        self._original_bridge_execute = bridge.execute_candidate
        self._installed = False

    @staticmethod
    def route_image_payload(target: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        routed = deepcopy(payload)
        if _clean(routed.get("capability")).lower() != "image":
            return routed

        params = dict(routed.get("params") or {})
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        role = _clean(target.get("asset_role")).lower()
        mode = _clean(routed.get("mode") or params.get("mode") or "txt2img").lower()
        phase = _clean(params.get("reference_phase") or metadata.get("reference_phase")).lower()
        references = params.get("reference_asset_ids") or routed.get("reference_asset_ids") or []
        if not isinstance(references, (list, tuple)):
            references = []
        references = [_clean(value) for value in references if _clean(value)]
        is_reference_asset = bool(metadata.get("reference_asset")) or role in _REFERENCE_ROLES
        is_character_package_stage = (
            phase in _CHARACTER_HYBRID_PHASES
            and role in {"character_costume_reference", "character_turnaround", "character_reference"}
        )

        if references and is_character_package_stage:
            # The reference IDs remain attached for lineage/identity post-process,
            # but the primary renderer is always the real Z-Image-Turbo workflow.
            # This prevents character-package stages from silently switching to
            # the legacy SDXL reference checkpoint.
            params.update({
                "model_key": ZIMAGE_TURBO_KEY,
                "requested_model_key": ZIMAGE_TURBO_KEY,
                "steps": 9,
                "cfg": 1.0,
                "sampler": "euler",
                "scheduler": "simple",
                "runtime_image_backend": "z_image_turbo_facefusion",
                "runtime_image_backend_reason": "character_package_zimage_primary",
                "runtime_identity_postprocess": "facefusion",
            })
            routed["params"] = params
            return routed

        if references:
            # Non-character-package references keep the explicit reference-aware
            # provider until a domain-specific Z-Image control workflow exists.
            params["runtime_image_backend"] = "sdxl_reference_ipadapter"
            params["runtime_image_backend_reason"] = "non_character_reference_required"
            routed["params"] = params
            return routed

        if is_reference_asset or mode == "txt2img":
            # Z-Image-Turbo has its own sampler profile. Merely changing model_key
            # while retaining SDXL's 36 steps / CFG 6 would materially degrade it.
            params.update({
                "model_key": ZIMAGE_TURBO_KEY,
                "requested_model_key": ZIMAGE_TURBO_KEY,
                "steps": 9,
                "cfg": 1.0,
                "sampler": "euler",
                "scheduler": "simple",
                "runtime_image_backend": ZIMAGE_TURBO_KEY,
                "runtime_image_backend_reason": "pure_txt2img",
            })
            routed["params"] = params
        return routed

    async def _qwen_request_messages(self, *args: Any, **kwargs: Any):
        supplied = _clean(kwargs.get("verified_model"))
        if supplied and supplied != self.required_text_model:
            raise RuntimeError(
                "V3 文本模型契约拒绝非 Qwen 请求："
                f"requested={supplied} required={self.required_text_model}"
            )
        kwargs["verified_model"] = self.required_text_model
        result = await self._original_llm_request(*args, **kwargs)
        actual = _clean(result[1] if isinstance(result, tuple) and len(result) > 1 else "")
        if actual != self.required_text_model:
            raise RuntimeError(
                "V3 文本模型契约校验失败："
                f"actual={actual or '<empty>'} required={self.required_text_model}"
            )
        logger.info("V3_TEXT_MODEL_ROUTE required=%s actual=%s", self.required_text_model, actual)
        return result

    async def execute_candidate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        routed = payload
        if _clean(payload.get("capability")).lower() == "image":
            target_id = _clean(payload.get("target_asset_id"))
            if target_id:
                target = self.production.get_asset(project_id, target_id)
                routed = self.route_image_payload(target, payload)
                params = routed.get("params") if isinstance(routed.get("params"), dict) else {}
                logger.info(
                    "V3_IMAGE_MODEL_ROUTE project_id=%s target_asset_id=%s role=%s phase=%s backend=%s refs=%s model_key=%s",
                    project_id,
                    target_id,
                    _clean(target.get("asset_role")),
                    _clean(params.get("reference_phase") or (target.get("metadata") or {}).get("reference_phase")),
                    _clean(params.get("runtime_image_backend")),
                    len(params.get("reference_asset_ids") or []),
                    _clean(params.get("model_key")),
                )
        return await self._original_bridge_execute(project_id, routed)

    def install(self) -> None:
        if self._installed:
            return
        # Settings can be overridden by a stale .env from older Gemma-based
        # deployments. V3's production text contract is Qwen, so overwrite the
        # effective runtime values in-process rather than silently honoring a
        # legacy model selection.
        self.settings.gemma_model = self.required_text_model
        self.settings.gemma_start_command = (
            "bash /root/autodl-tmp/ai-studio/platform-v2/scripts/start_qwen_v3.sh"
        )
        runtime_settings = getattr(self.legacy, "settings", None)
        if runtime_settings is not None:
            runtime_settings.gemma_model = self.required_text_model
            runtime_settings.gemma_start_command = self.settings.gemma_start_command
        self.llm._request_messages = self._qwen_request_messages
        self.bridge.execute_candidate = self.execute_candidate

        # LegacyCandidateV3Bridge.install() previously stored a bound method on
        # legacy_runtime. Replacing bridge.execute_candidate afterwards does not
        # mutate that already-bound callable. Rebind every public/default image
        # entrypoint here so reference generation cannot bypass model routing.
        self.legacy.director_workbench_execute_candidate = self.execute_candidate
        bootstrap = getattr(self.bridge, "reference_bootstrap", None)
        if bootstrap is not None and hasattr(bootstrap, "submit_candidate"):
            bootstrap.submit_candidate = self.execute_candidate

        self._installed = True

    def status(self) -> dict[str, Any]:
        return {
            "text": {
                "required_model": self.required_text_model,
                "policy": "qwen_fail_closed",
                "start_command": self.settings.gemma_start_command,
            },
            "image_txt2img": {
                "model_key": ZIMAGE_TURBO_KEY,
                "policy": "z_image_turbo_fail_closed",
                "unet": ZIMAGE_TURBO_UNET,
                "clip": ZIMAGE_TURBO_CLIP,
                "vae": ZIMAGE_TURBO_VAE,
                "steps": 9,
                "cfg": 1.0,
                "sampler": "euler",
                "scheduler": "simple",
            },
            "character_reference_package": {
                "primary_renderer": ZIMAGE_TURBO_KEY,
                "identity_postprocess": "facefusion",
                "policy": "zimage_primary_facefusion_identity",
                "phases": ["face_anchor", "costume", "turnaround"],
            },
            "image_reference_other_domains": {
                "backend": "sdxl_reference_ipadapter",
                "policy": "explicit_non_character_reference_backend",
            },
        }


def create_runtime_model_contract_router(contract: V3RuntimeModelContract) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v3/runtime/model-contract")
    async def runtime_model_contract() -> dict[str, Any]:
        return contract.status()

    return router


__all__ = ["V3RuntimeModelContract", "create_runtime_model_contract_router"]
