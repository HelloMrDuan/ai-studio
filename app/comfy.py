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


# 可选加速节点（旧版 ComfyUI 没有时自动去掉，不影响出片）
OPTIONAL_PASSTHROUGH = {"EasyCache": "model", "LazyCache": "model"}
_has_node: dict[str, bool] = {}


async def node_available(name: str) -> bool:
    if name not in _has_node:
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
                r = await c.get(f"{_base()}/object_info/{name}")
            _has_node[name] = r.status_code == 200 and name in (r.json() or {})
        except Exception:  # noqa: BLE001 —— 查不到就当可用，交给 ComfyUI 报错
            return True
    return _has_node[name]


_node_specs: dict[str, dict] = {}


async def node_spec(name: str) -> dict:
    if name not in _node_specs:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
            r = await c.get(f"{_base()}/object_info/{name}")
        r.raise_for_status()
        _node_specs[name] = (r.json() or {}).get(name) or {}
    return _node_specs[name]


async def fit_inputs(workflow: dict, classes: tuple[str, ...]) -> dict:
    """按 ComfyUI 实际装的节点版本对齐输入：缺的必填项用默认值补上，不认识的去掉。
    实测：新版 RIFE VFI 多了 dtype / batch_size / torch_compile 必填项，旧工作流直接被拒。"""
    for node in workflow.values():
        if node.get("class_type") not in classes:
            continue
        try:
            spec = (await node_spec(node["class_type"])).get("input") or {}
        except Exception:  # noqa: BLE001 —— 查不到就原样提交
            continue
        req, opt = spec.get("required") or {}, spec.get("optional") or {}
        inputs = node.setdefault("inputs", {})
        for k, v in req.items():
            if k in inputs:
                continue
            cfg = v[1] if len(v) > 1 and isinstance(v[1], dict) else {}
            if "default" in cfg:
                inputs[k] = cfg["default"]
            elif isinstance(v[0], list) and v[0]:
                inputs[k] = v[0][0]
        if req:
            for k in [k for k in inputs if k not in req and k not in opt]:
                inputs.pop(k)
    return workflow


async def strip_missing_optional(workflow: dict) -> dict:
    """去掉 ComfyUI 不认识的可选节点，把引用它的输入直接接到它的上游。"""
    for nid, node in list(workflow.items()):
        port = OPTIONAL_PASSTHROUGH.get(node.get("class_type"))
        if not port or await node_available(node["class_type"]):
            continue
        upstream = node["inputs"][port]
        workflow.pop(nid)
        for other in workflow.values():
            for k, v in other.get("inputs", {}).items():
                if isinstance(v, list) and len(v) == 2 and str(v[0]) == str(nid):
                    other["inputs"][k] = upstream
    return workflow


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
    workflow = await strip_missing_optional(workflow)
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
