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
    inject a presentation bridge that translates technical labels for the
    operator and exposes the new V3 production backend through the existing
    buttons/candidate workflow.
    """
    html = _ORIGINAL_PAGE.read_text(encoding="utf-8")
    html = html.replace("<title>AI 漫剧工作台</title>", "<title>小段映画工作台</title>")
    marker = "<script src=\"/v3-static/original-workbench-overlay.js\"></script>"
    if marker not in html:
        html = html.replace("</body>", marker + "\n</body>")
    return HTMLResponse(html)


__all__ = ["router"]
