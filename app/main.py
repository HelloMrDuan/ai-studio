"""小段映画组合应用入口。

产品外壳保留原来的漫剧工作台：阶段编辑、候选版本、人工采用、逐镜头制作
全部继续使用原交互。底层统一为故事生产上下文、专业生产 Skill、版本资产、
Temporal、参考图优先、连续镜头、智能质量、媒体复用和完整后期生产。
"""

from __future__ import annotations

from app.config import get_settings
from app.legacy_snapshot import load_original_workbench_runtime


settings = get_settings()
legacy_runtime = load_original_workbench_runtime()
app = legacy_runtime.app

app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", "") != "/"
]

from app.v3.legacy_authoring_retirement import retire_legacy_authoring_jobs
from app.v3.production_skill_registry import ProductionSkillRegistry
from app.v3.production_runtime_optimization import ProductionRuntimeOptimizer
from app.v3.production_authoring_assets import ProductionAuthoringAssetService
from app.v3.postproduction_prefetch import PostProductionPrefetch
from app.v3.bgm_prefetch import BGMPrefetchService
from app.v3.shot_continuity_linker import ShotContinuityLinker
from app.v3.authoring_progress import create_authoring_progress_tracker
from app.v3.canonical_entity_reconciler import CanonicalEntityReconciler
from app.v3.authoring_execution_timing import AuthoringExecutionTimingFix
from app.v3.front_half_quality_gate import install_front_half_quality_gate
from app.v3.story_source_coverage import install_story_source_coverage
from app.v3.story_entity_sanitizer import install_story_entity_sanitizer
from app.v3.professional_source_grounding import install_professional_source_grounding
from app.v3.llama_structured_output import install_llama_structured_output
from app.v3.professional_output_runtime import install_professional_output_runtime
from app.v3.project_source_snapshot import install_project_source_snapshot
from app.v3.professional_output_cache_epoch import install_professional_output_cache_epoch
from app.v3.character_prompt_integration import install_character_prompt_integration
from app.v3.reference_role_policy import install_reference_role_policy
from app.v3.character_package_integrity import install_character_package_integrity

legacy_authoring_retirement = retire_legacy_authoring_jobs(settings)

install_front_half_quality_gate(legacy_runtime.director)
story_source_coverage = install_story_source_coverage(legacy_runtime.director)
story_entity_sanitizer = install_story_entity_sanitizer(legacy_runtime.director)
# Legacy/history source parsing remains available only as a migration fallback.
professional_source_grounding = install_professional_source_grounding()
# Follow llama.cpp's own JSON-Schema response_format contract before the strict
# professional runtime captures its lower-level tracked LLM call boundary.
llama_structured_output = install_llama_structured_output(legacy_runtime.director)
professional_output_runtime = install_professional_output_runtime(settings, legacy_runtime.director)
# Wao-style source boundary: the first real Stage01 source becomes an immutable,
# versioned project resource. Regeneration and provenance no longer depend on
# chat-history recovery; source_evidence is bound by the server to this snapshot.
project_source_snapshot = install_project_source_snapshot(settings, legacy_runtime.director)
character_prompt_contract = install_character_prompt_integration()
reference_role_contract = install_reference_role_policy()
character_package_contract = install_character_package_integrity()

production_skill_registry = ProductionSkillRegistry(legacy_runtime.director)
production_skill_registry.install()
production_runtime_optimizer = ProductionRuntimeOptimizer(settings, legacy_runtime.director)
production_runtime_optimizer.install()
professional_output_cache_epoch = install_professional_output_cache_epoch(legacy_runtime.director)

canonical_entity_reconciler = CanonicalEntityReconciler(settings, legacy_runtime.director)
canonical_entity_reconciler.install()

stage_progress_tracker = create_authoring_progress_tracker(settings, legacy_runtime.director)
authoring_execution_timing = AuthoringExecutionTimingFix(stage_progress_tracker)
authoring_execution_timing.install()

authoring_asset_service = ProductionAuthoringAssetService(settings, legacy_runtime)
authoring_asset_service.install_confirmation_hook()
authoring_asset_reconciliation = authoring_asset_service.reconcile_existing_projects()

shot_continuity_linker = ShotContinuityLinker(settings, legacy_runtime)
shot_continuity_linker.install_confirmation_hook()
postproduction_prefetch = PostProductionPrefetch(settings, legacy_runtime)
postproduction_prefetch.install_confirmation_hook()
bgm_prefetch = BGMPrefetchService(settings, legacy_runtime)
bgm_prefetch.install_confirmation_hook()

from app.v3.unified_production_bridge import UnifiedProductionBridge
from app.v3.reference_generation_optimization import (
    ReferenceGenerationOptimizer,
    create_reference_generation_optimization_router,
)
from app.v3.character_reference_package import CharacterReferencePackageBootstrap
from app.v3.runtime_model_contract import (
    V3RuntimeModelContract,
    create_runtime_model_contract_router,
)

legacy_v3_bridge = UnifiedProductionBridge(settings, legacy_runtime)
legacy_v3_bridge.install()
# Text is Qwen. Reference-free image requests are Z-Image-Turbo. Reference-
# conditioned image requests remain on the proven SDXL FaceID/IP-Adapter graph.
runtime_model_contract = V3RuntimeModelContract(settings, legacy_runtime, legacy_v3_bridge)
runtime_model_contract.install()
legacy_v3_bridge.reference_bootstrap = CharacterReferencePackageBootstrap(
    legacy_runtime,
    submit_candidate=runtime_model_contract.execute_candidate,
)
reference_generation_optimizer = ReferenceGenerationOptimizer(legacy_v3_bridge, max_concurrency=2)
reference_generation_optimizer.install()

from app.v3.main import app as v3_app
from app.v3.web_routes import router as web_workflow_router
from app.v3.legacy_postproduction import router as legacy_postproduction_router
from app.v3.original_workbench_overlay import router as original_workbench_router
from app.v3.project_management import create_project_management_router
from app.v3.character_reference_package import create_character_reference_package_router
from app.v3.stage_revision import create_stage_revision_router
from app.v3.production_authoring_assets import create_production_authoring_asset_router
from app.v3.shot_authoring import create_shot_authoring_router
from app.v3.character_appearances import create_character_appearance_router
from app.v3.shot_refinement import create_shot_refinement_router
from app.v3.bgm_prefetch import create_bgm_prefetch_router
from app.v3.asset_explorer import create_asset_explorer_router
from app.v3.authoring_progress import create_authoring_progress_router
from app.v3.single_pass_finalizer import create_single_pass_finalizer_router

_SKIP_V3_PATHS = {"/", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
_existing_paths = {getattr(route, "path", "") for route in app.router.routes}
for route in v3_app.router.routes:
    path = getattr(route, "path", "")
    if path in _SKIP_V3_PATHS:
        continue
    if path and path in _existing_paths:
        continue
    app.router.routes.append(route)
    if path:
        _existing_paths.add(path)

app.include_router(web_workflow_router)
app.include_router(legacy_postproduction_router)
app.include_router(create_project_management_router(settings, legacy_runtime))
app.include_router(create_character_reference_package_router(legacy_runtime))
app.include_router(create_reference_generation_optimization_router(reference_generation_optimizer))
app.include_router(create_runtime_model_contract_router(runtime_model_contract))
app.include_router(create_stage_revision_router(settings, legacy_runtime))
app.include_router(create_production_authoring_asset_router(settings, legacy_runtime))
app.include_router(create_character_appearance_router(legacy_runtime))
app.include_router(create_shot_authoring_router(settings, legacy_runtime))
app.include_router(create_shot_refinement_router(legacy_runtime))
app.include_router(create_bgm_prefetch_router(settings, legacy_runtime))
app.include_router(create_asset_explorer_router(settings, legacy_runtime))
app.include_router(create_authoring_progress_router(stage_progress_tracker))
app.include_router(create_single_pass_finalizer_router(legacy_runtime, stage_progress_tracker))
app.include_router(original_workbench_router)
app.title = "小段映画 · 漫剧工作台"

__all__ = [
    "app",
    "legacy_runtime",
    "legacy_authoring_retirement",
    "story_source_coverage",
    "story_entity_sanitizer",
    "professional_source_grounding",
    "llama_structured_output",
    "professional_output_runtime",
    "project_source_snapshot",
    "professional_output_cache_epoch",
    "character_prompt_contract",
    "reference_role_contract",
    "character_package_contract",
    "production_skill_registry",
    "production_runtime_optimizer",
    "canonical_entity_reconciler",
    "stage_progress_tracker",
    "authoring_execution_timing",
    "authoring_asset_service",
    "authoring_asset_reconciliation",
    "shot_continuity_linker",
    "postproduction_prefetch",
    "bgm_prefetch",
    "legacy_v3_bridge",
    "runtime_model_contract",
    "reference_generation_optimizer",
]
