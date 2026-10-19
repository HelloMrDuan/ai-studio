"""ComfyUI HTTP/WebSocket 客户端：上传图片、提交工作流、跟踪进度、下载结果。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .config import get_settings

Progress = Callable[[int, str], Awaitable[None]]


class ComfyError(RuntimeError):
    pass


def _base() -> str:
    return get_settings().comfyui_url.rstrip("/")


async def object_info() -> dict:
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        r = await c.get(_base() + "/object_info")
        r.raise_for_status()
        return r.json()


async def upload_image(path: Path) -> str:
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        with open(path, "rb") as fh:
            r = await c.post(_base() + "/upload/image",
                             files={"image": (f"studio_{uuid.uuid4().hex[:8]}_{path.name}", fh)},
                             data={"overwrite": "true", "type": "input"})
        r.raise_for_status()
        data = r.json()
    name = data["name"]
    return f"{data['subfolder']}/{name}" if data.get("subfolder") else name


def _outputs(history: dict) -> list[dict]:
    files = []
    for node_out in (history.get("outputs") or {}).values():
        for value in node_out.values():
            if isinstance(value, list):
                files += [v for v in value if isinstance(v, dict) and v.get("filename")]
    return files


async def _watch_progress(client_id: str, prompt_id: str, progress: Progress | None, stop: asyncio.Event) -> None:
    if progress is None:
        return
    try:
        import websockets
    except ImportError:
        return
    url = _base().replace("http", "ws", 1) + f"/ws?clientId={client_id}"
    with contextlib.suppress(Exception):
        async with websockets.connect(url, max_size=None, open_timeout=10) as ws:
            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(msg, str):
                    continue
                data = json.loads(msg)
                d = data.get("data") or {}
                if data.get("type") == "progress" and d.get("prompt_id") in (None, prompt_id):
                    value, maximum = d.get("value", 0), max(1, d.get("max", 1))
                    await progress(10 + int(80 * value / maximum), f"采样 {value}/{maximum}")


async def run(workflow: dict, out_dir: Path, progress: Progress | None = None) -> list[Path]:
    """提交工作流并等待完成，返回下载到 out_dir 的结果文件。"""
    s = get_settings()
    client_id = uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        r = await c.post(_base() + "/prompt", json={"prompt": workflow, "client_id": client_id})
        if r.status_code >= 400:
            raise ComfyError(f"ComfyUI 拒绝工作流：{r.text[:2000]}")
        prompt_id = r.json()["prompt_id"]

    stop = asyncio.Event()
    watcher = asyncio.create_task(_watch_progress(client_id, prompt_id, progress, stop))
    deadline = time.monotonic() + s.comfyui_timeout
    history = None
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
            while time.monotonic() < deadline:
                r = await c.get(f"{_base()}/history/{prompt_id}")
                if r.status_code == 200 and prompt_id in (r.json() or {}):
                    history = r.json()[prompt_id]
                    status = history.get("status") or {}
                    if status.get("completed") or status.get("status_str") in {"success", "error"}:
                        break
                await asyncio.sleep(2)
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(watcher, timeout=3)

    if history is None:
        raise ComfyError("ComfyUI 任务超时")
    status = history.get("status") or {}
    if status.get("status_str") == "error":
        detail = ""
        for kind, data in status.get("messages") or []:
            if kind == "execution_error":
                detail = f"{data.get('node_type')}: {data.get('exception_message')}"
        raise ComfyError(f"ComfyUI 执行失败：{detail or '未知错误'}")

    files = _outputs(history)
    if not files:
        raise ComfyError("ComfyUI 没有产出文件")
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    async with httpx.AsyncClient(timeout=600, trust_env=False) as c:
        for f in files:
            if f.get("type") == "temp":
                continue
            r = await c.get(_base() + "/view", params={
                "filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
            r.raise_for_status()
            target = out_dir / f"{uuid.uuid4().hex[:10]}{Path(f['filename']).suffix}"
            target.write_bytes(r.content)
            saved.append(target)
    if not saved:
        raise ComfyError("ComfyUI 没有产出可下载的文件")
    return saved
