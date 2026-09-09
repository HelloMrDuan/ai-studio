from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter()
_ORIGINAL_PAGE = Path(__file__).resolve().parents[1] / "static" / "index.html"


@router.get("/", response_class=HTMLResponse)
async def original_workbench_page() -> HTMLResponse:
    """Serve the original workbench layout, not the temporary V3 dashboard.

    The original HTML/interaction code remains the source of truth. We only
    inject presentation bridges that translate technical labels, expose the V3
    production backend through the existing candidate workflow, and add project
    lifecycle controls that the historical page did not provide.
    """
    html = _ORIGINAL_PAGE.read_text(encoding="utf-8")
    html = html.replace("<title>AI 漫剧工作台</title>", "<title>小段映画工作台</title>")
    markers = [
        '<script src="/v3-static/original-workbench-overlay.js"></script>',
        '<script src="/v3-static/project-delete-overlay.js"></script>',
    ]
    missing = [marker for marker in markers if marker not in html]
    if missing:
        html = html.replace("</body>", "\n".join(missing) + "\n</body>")
    return HTMLResponse(html)


__all__ = ["router"]
