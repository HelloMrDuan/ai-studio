from __future__ import annotations

from app.config import Settings
from app.v3.character_prompt_integration import install_character_prompt_integration
from app.v3.reference_role_policy import install_reference_role_policy

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

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        operation = input.step.operation
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


__all__ = ["ProductionWorkerExecutor"]
