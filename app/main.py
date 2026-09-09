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

from app.v3.production_skill_registry import ProductionSkillRegistry
from app.v3.production_runtime_optimization import ProductionRuntimeOptimizer
from app.v3.asset_authoring_refined import RefinedAuthoringAssetService
from app.v3.postproduction_prefetch import PostProductionPrefetch
from app.v3.bgm_prefetch import BGMPrefetchService
from app.v3.shot_continuity_linker import ShotContinuityLinker

production_skill_registry = ProductionSkillRegistry(legacy_runtime.director)
production_skill_registry.install()
production_runtime_optimizer = ProductionRuntimeOptimizer(settings, legacy_runtime.director)
production_runtime_optimizer.install()
authoring_asset_service = RefinedAuthoringAssetService(settings, legacy_runtime)
authoring_asset_service.install_confirmation_hook()

shot_continuity_linker = ShotContinuityLinker(settings, legacy_runtime)
shot_continuity_linker.install_confirmation_hook()
postproduction_prefetch = PostProductionPrefetch(settings, legacy_runtime)
postproduction_prefetch.install_confirmation_hook()
bgm_prefetch = BGMPrefetchService(settings, legacy_runtime)
bgm_prefetch.install_confirmation_hook()

from app.v3.production_legacy_bridge import ProductionReadyLegacyBridge

legacy_v3_bridge = ProductionReadyLegacyBridge(settings, legacy_runtime)
legacy_v3_bridge.install()

from app.v3.main import app as v3_app
from app.v3.web_routes import router as web_workflow_router
from app.v3.legacy_postproduction import router as legacy_postproduction_router
from app.v3.original_workbench_overlay import router as original_workbench_router
from app.v3.project_management import create_project_management_router
from app.v3.canonical_reference_assets import create_canonical_reference_asset_router
from app.v3.stage_revision import create_stage_revision_router
from app.v3.asset_authoring_refined import create_refined_authoring_asset_router
from app.v3.shot_authoring import create_shot_authoring_router
from app.v3.character_appearances import create_character_appearance_router
from app.v3.shot_refinement import create_shot_refinement_router
from app.v3.bgm_prefetch import create_bgm_prefetch_router

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
app.include_router(create_canonical_reference_asset_router(legacy_runtime))
app.include_router(create_stage_revision_router(settings, legacy_runtime))
app.include_router(create_refined_authoring_asset_router(settings, legacy_runtime))
app.include_router(create_character_appearance_router(legacy_runtime))
app.include_router(create_shot_authoring_router(settings, legacy_runtime))
app.include_router(create_shot_refinement_router(legacy_runtime))
app.include_router(create_bgm_prefetch_router(settings, legacy_runtime))
app.include_router(original_workbench_router)
app.title = "小段映画 · 漫剧工作台"

__all__ = [
    "app",
    "legacy_runtime",
    "production_skill_registry",
    "production_runtime_optimizer",
    "authoring_asset_service",
    "shot_continuity_linker",
    "postproduction_prefetch",
    "bgm_prefetch",
    "legacy_v3_bridge",
]
