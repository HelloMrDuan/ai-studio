from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import get_settings

from .full_pipeline_workflows import FullMovieWorkflowService
from .web_workflows import WebVisualWorkflowService


settings = get_settings()
service = WebVisualWorkflowService(settings)
full_movie_service = FullMovieWorkflowService(settings)
router = APIRouter()
STATIC_DIR = Path(__file__).parent / "static"


class VisualWorkflowStartRequest(BaseModel):
    project_id: str = Field(default="web-validation", min_length=3, max_length=80)
    reference_id: str = Field(default="hero-v1", min_length=3, max_length=120)
    source_text: str = Field(min_length=1, max_length=4000)
    video_prompt: str = Field(min_length=1, max_length=4000)
    h3_width: int = Field(default=512, ge=256, le=1536)
    h3_height: int = Field(default=320, ge=256, le=1536)
    h3_length: int = Field(default=56, ge=8, le=240)
    h3_steps: int = Field(default=12, ge=1, le=60)


class FullMovieStartRequest(BaseModel):
    project_id: str = Field(default="full-movie-validation", min_length=3, max_length=80)
    story_text: str = Field(min_length=1, max_length=20000)
    reference_id: str = Field(default="hero-v1", min_length=3, max_length=120)
    bgm_path: str = Field(default="", max_length=1000)
    llm_provider_id: str = Field(default="local-qwen", min_length=3, max_length=120)
    llm_model_id: str = Field(default="", max_length=200)
    tts_provider_id: str = Field(default="edge-tts-gateway", min_length=3, max_length=120)
    tts_model_id: str = Field(default="edge-tts", min_length=2, max_length=120)
    tts_voice: str = Field(default="zh-CN-XiaoxiaoNeural", min_length=2, max_length=120)
    h3_width: int = Field(default=512, ge=256, le=1536)
    h3_height: int = Field(default=320, ge=256, le=1536)
    h3_length: int = Field(default=56, ge=8, le=240)
    h3_steps: int = Field(default=12, ge=1, le=60)


@router.get("/workflow")
async def workflow_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "workflow.html")


@router.get("/movie")
async def full_movie_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "movie.html")


@router.post("/api/v3/workflows/visual")
async def start_visual_workflow(request: VisualWorkflowStartRequest) -> dict[str, Any]:
    try:
        return await service.start(**request.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Temporal workflow start failed: {type(exc).__name__}: {exc}") from exc


@router.get("/api/v3/workflows/{workflow_id}")
async def workflow_status(workflow_id: str) -> dict[str, Any]:
    try:
        return await service.status(workflow_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Temporal workflow status failed: {type(exc).__name__}: {exc}") from exc


@router.post("/api/v3/full-pipeline/bgm")
async def upload_full_pipeline_bgm(file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        path = full_movie_service.save_bgm(file.filename or "bgm.mp3", file.file)
        return {"bgm_path": str(path), "filename": path.name, "bytes": path.stat().st_size}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()


@router.post("/api/v3/full-pipeline")
async def start_full_movie_workflow(request: FullMovieStartRequest) -> dict[str, Any]:
    try:
        return await full_movie_service.start(**request.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Full movie Temporal start failed: {type(exc).__name__}: {exc}") from exc


@router.get("/api/v3/full-pipeline/{workflow_id}")
async def full_movie_status(workflow_id: str) -> dict[str, Any]:
    try:
        return await full_movie_service.status(workflow_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Full movie workflow status failed: {type(exc).__name__}: {exc}") from exc


@router.get("/api/v3/full-pipeline/{workflow_id}/final")
async def full_movie_final(workflow_id: str) -> FileResponse:
    try:
        return FileResponse(full_movie_service.final_path(workflow_id), media_type="video/mp4", filename=f"{workflow_id}.mp4")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _media_file(kind: str, artifact_id: str) -> Path:
    if kind == "image":
        prefix = "img_"
        root = Path(settings.data_dir) / "v3" / "media" / "image"
        allowed = {".png", ".jpg", ".jpeg", ".webp"}
    elif kind == "video":
        prefix = "vid_"
        root = Path(settings.data_dir) / "v3" / "media" / "video"
        allowed = {".mp4", ".webm", ".mov", ".mkv", ".gif"}
    else:
        raise ValueError("unsupported media kind")
    if not artifact_id.startswith(prefix) or not artifact_id[len(prefix):].isalnum():
        raise ValueError("invalid artifact_id")
    matches = [path for path in root.glob(f"{artifact_id}.*") if path.suffix.lower() in allowed and path.is_file()]
    if len(matches) != 1:
        raise FileNotFoundError(f"{kind} artifact not found")
    return matches[0]


@router.get("/api/v3/media/{kind}/{artifact_id}")
async def get_visual_artifact(kind: str, artifact_id: str) -> FileResponse:
    try:
        return FileResponse(_media_file(kind, artifact_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
