"""小段映画组合应用入口。

产品外壳保留原来的漫剧工作台：阶段编辑、候选版本、人工采用、逐镜头制作
全部继续使用原交互。底层统一为资产驱动的前半段、Temporal、参考图优先、
资源版本、视频生成、配音、字幕、背景音乐和最终合成。
"""

from __future__ import annotations

from app.config import get_settings
from app.legacy_snapshot import load_original_workbench_runtime


settings = get_settings()
legacy_runtime = load_original_workbench_runtime()
app = legacy_runtime.app

# 原运行时的根页面路由会直接返回未处理的历史页面。移除这一条根路由，随后
# 重新挂载“同一份原页面 + 中文呈现桥接”；其他历史接口和页面全部保留。
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", "") != "/"
]

# ①②③继续复用原成熟 Skill，同时叠加经过开源项目验证的可复用资产规则。
# 这里不复制 waoowaoo 实现，而是把角色/场景/道具落实成小段现有
# ProductionAssetService 的版本化资产，并在阶段确认后自动同步。
from app.v3.front_half_skill_overlay import FrontHalfSkillOverlay
from app.v3.authoring_assets import AuthoringAssetService

front_half_skill_overlay = FrontHalfSkillOverlay(legacy_runtime.director)
front_half_skill_overlay.install()
authoring_asset_service = AuthoringAssetService(settings, legacy_runtime)
authoring_asset_service.install_confirmation_hook()

# ⑤制作：保留原候选/采用 UI，只把镜头图片和视频生产器替换为新版
# Temporal + ResourceStore。参考图从已采用的角色/场景/道具资产中解析；
# 缺少时自动创建参考图候选，仍由用户显式采用。
from app.v3.legacy_reference_bridge import ReferenceAwareLegacyCandidateV3Bridge

legacy_v3_bridge = ReferenceAwareLegacyCandidateV3Bridge(settings, legacy_runtime)
legacy_v3_bridge.install()

# 新版核心 API 继续保持原 `/api/v3/...` 地址，但不使用新版仪表盘替换主页。
from app.v3.main import app as v3_app
from app.v3.web_routes import router as web_workflow_router
from app.v3.legacy_postproduction import router as legacy_postproduction_router
from app.v3.original_workbench_overlay import router as original_workbench_router
from app.v3.project_management import create_project_management_router
from app.v3.reference_assets import create_reference_asset_router
from app.v3.stage_revision import create_stage_revision_router
from app.v3.authoring_assets import create_authoring_asset_router
from app.v3.shot_authoring import create_shot_authoring_router

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
app.include_router(create_reference_asset_router(legacy_runtime))
app.include_router(create_stage_revision_router(settings, legacy_runtime))
app.include_router(create_authoring_asset_router(settings, legacy_runtime))
app.include_router(create_shot_authoring_router(settings, legacy_runtime))
app.include_router(original_workbench_router)
app.title = "小段映画 · 漫剧工作台"

__all__ = [
    "app",
    "legacy_runtime",
    "front_half_skill_overlay",
    "authoring_asset_service",
    "legacy_v3_bridge",
]
