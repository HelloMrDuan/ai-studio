from __future__ import annotations

import httpx

from app.config import Settings
from app.v3.character_prompt_integration import install_character_prompt_integration
from app.v3.generation_executor import ComfyWorkflowBindingError, ReferenceAssetError
from app.v3.provider_gateway import ProviderResolutionError
from app.v3.reference_role_policy import install_reference_role_policy
from app.v3.resource_store import ResourceStoreError

from .contracts import StepActivityInput, StepActivityResult
from .production_cached_executor import ProductionCachedFullPipelineExecutor, _MATERIALIZED
from .unified_image_executor import UnifiedImageDomainExecutor


class ProductionWorkerExecutor(ProductionCachedFullPipelineExecutor):
    """Worker executor that checks immutable media reuse before acquiring GPU.

    The visual executor is unified: reference-free images use the explicit
    Z-Image provider and reference-conditioned images use the proven SDXL
    reference provider, while both share the same Temporal operation and stores.
    Prompt/reference policies are installed in this worker process too; the
    worker does not depend on importing the web application's app.main module.
    """

    def __init__(self, settings: Settings) -> None:
        install_character_prompt_integration()
        install_reference_role_policy()
        super().__init__(settings, visual=UnifiedImageDomainExecutor(settings))

    def _remember_failure(
        self,
        input: StepActivityInput,
        *,
        code: str,
        message: str,
    ) -> StepActivityResult:
        result = StepActivityResult(
            kind="semantic_failure",
            error_code=code,
            message=message,
            metadata={"executor": "v3-production-worker"},
        )
        self.visual.base.results.put(input.project_id, input.step.idempotency_key, result)
        return result

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        operation = input.step.operation
        try:
            if operation in _MATERIALIZED:
                # FullPipelineExecutor normally acquires the Comfy workspace before
                # delegating visual work. Exact content reuse needs no GPU at all,
                # so inspect the content-addressed cache first.
                cached_task = self.visual.base.results.get(input.project_id, input.step.idempotency_key)
                if cached_task is not None:
                    return cached_task
                payload = self.visual.base.payloads.resolve(input.project_id, input.step.payload_ref)
                signature = self.visual.signature(input.project_id, operation, payload)
                if signature:
                    record = self.visual.content_cache.get(input.project_id, signature)
                    if record is not None:
                        return self.visual._cache_hit_result(input, payload, signature, record)
            return await super().__call__(input)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            status = int(response.status_code) if response is not None else 0
            # ComfyUI 4xx responses are deterministic workflow/input rejection,
            # not transient infrastructure faults. Preserve the response body so
            # the workbench shows the actual missing node/model/input instead of
            # the useless Temporal text "Workflow execution failed".
            if 400 <= status < 500:
                body = ""
                try:
                    body = (response.text or "").strip().replace("\x00", "")
                except Exception:
                    body = ""
                detail = body[:3000] or str(exc)
                return self._remember_failure(
                    input,
                    code="COMFY_REQUEST_REJECTED",
                    message=f"ComfyUI HTTP {status}: {detail}",
                )
            raise
        except (
            ComfyWorkflowBindingError,
            ReferenceAssetError,
            ProviderResolutionError,
            ResourceStoreError,
            FileNotFoundError,
            ValueError,
        ) as exc:
            return self._remember_failure(
                input,
                code="REFERENCE_WORKFLOW_CONTRACT_ERROR",
                message=f"{type(exc).__name__}: {exc}",
            )


__all__ = ["ProductionWorkerExecutor"]
