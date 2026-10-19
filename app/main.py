"""AI Studio v3 入口：uvicorn app.main:app --host 0.0.0.0 --port 6006"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, pipeline  # noqa: F401  (pipeline 注册任务处理器)
from .api import router
from .config import ROOT, get_settings
from .tasks import queue

WEB = ROOT / "web"


def _setup_logging() -> None:
    s = get_settings()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        s.log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(s.log_dir / "studio.log", maxBytes=20_000_000, backupCount=3,
                                            encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _setup_logging()
    db.conn()
    queue.start()
    yield
    if queue.worker:
        queue.worker.cancel()


app = FastAPI(title="AI Studio v3", lifespan=lifespan)
app.include_router(router)
app.mount("/media", StaticFiles(directory=str(get_settings().media_dir)), name="media")
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
