"""Xiaoduan Studio combined application entrypoint.

The original AI 漫剧工作台 remains the product shell because it already owns
editable stages, candidate versions, manual adoption and per-shot production.
V3 is layered underneath/alongside that UI for durable Temporal execution,
reference-first media generation and the new post-production pipeline.
"""

from __future__ import annotations

from app.legacy_snapshot import load_original_workbench_runtime


legacy_runtime = load_original_workbench_runtime()
app = legacy_runtime.app

# Add V3 API routes without replacing the original `/` workbench page. FastAPI
# routes are reused directly so their public `/api/v3/...` paths remain stable.
from app.v3.main import app as v3_app
from app.v3.web_routes import router as web_workflow_router

_SKIP_V3_PATHS = {"/", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
_existing_paths = {getattr(route, "path", "") for route in app.router.routes}
for route in v3_app.router.routes:
    path = getattr(route, "path", "")
    if path in _SKIP_V3_PATHS:
        continue
    # V3 paths are namespaced and should normally be unique. Avoid accidental
    # double-registration if the archived runtime later gains one of them.
    if path and path in _existing_paths:
        continue
    app.router.routes.append(route)
    if path:
        _existing_paths.add(path)

app.include_router(web_workflow_router)
app.title = "小段映画 · AI 漫剧工作台"

__all__ = ["app", "legacy_runtime"]
