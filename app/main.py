"""AI Studio v3 入口：uvicorn app.main:app --host 0.0.0.0 --port 6006"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import art, autopilot, board, db, sound, videox, vlm, director, pipeline, suggest, writers  # noqa: F401  (pipeline/writers 注册任务处理器)
from .api import router
from .config import ROOT, get_settings
from .gpu import gpu
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
    gpu.lock = asyncio.Lock()
    queue.start()
    autopilot.resume_all()
    yield
    if queue.worker:
        queue.worker.cancel()


app = FastAPI(title="AI Studio v3", lifespan=lifespan)
app.include_router(router)
for _r in (writers.router, suggest.router, director.router, art.router, board.router, videox.router, vlm.router, sound.router):
    app.include_router(_r)
app.mount("/media", StaticFiles(directory=str(get_settings().media_dir)), name="media")
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


# ---------------- 访问密码 ----------------
COOKIE = "studio_auth"
OPEN_PATHS = {"/login", "/api/login", "/api/logout"}


def auth_token(password: str) -> str:
    return hmac.new(password.encode(), b"ai-studio-v3", hashlib.sha256).hexdigest()


@app.middleware("http")
async def require_login(request: Request, call_next):
    password = get_settings().access_password
    path = request.url.path
    if not password or path in OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if hmac.compare_digest(request.cookies.get(COOKIE, ""), auth_token(password)):
        return await call_next(request)
    if path.startswith(("/api/", "/media/")):
        return JSONResponse({"detail": "需要登录"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login_page():
    return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(body: dict):
    password = get_settings().access_password
    if password and not hmac.compare_digest(str(body.get("password") or ""), password):
        await asyncio.sleep(1.5)   # 拖慢暴力尝试
        return JSONResponse({"detail": "密码错误"}, status_code=401)
    resp = JSONResponse({"ok": True})
    if password:
        resp.set_cookie(COOKIE, auth_token(password), max_age=30 * 86400, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
