"""单卡显存调度：文字模型(llama.cpp) / ComfyUI / FaceFusion 三者轮流使用 GPU。

规则很简单：
- 同一时间只跑一个 GPU 任务（全局锁）。
- 用文字模型前：让 ComfyUI 释放显存，再按需启动 llama-server。
- 用 ComfyUI 或 FaceFusion 前：停掉本机 llama-server（api 模式不需要）。
ComfyUI 常驻运行，只通过 /free 接口卸载模型，不反复重启。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time
from typing import AsyncIterator

import httpx

from .config import get_settings

log = logging.getLogger("studio.gpu")


class GPUManager:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner = "idle"
        self.detail = ""
        self._llm_proc: subprocess.Popen | None = None
        self.comfy_kind = ""

    # ---------- 状态 ----------
    async def status(self) -> dict:
        s = get_settings()
        return {
            "owner": self.owner,
            "busy": self.lock.locked(),
            "detail": self.detail,
            "memory": await self.memory(),
            "llm": {"provider": s.llm_provider, "running": await self.llm_alive()},
            "comfyui": {"running": await self.comfy_alive()},
        }

    async def memory(self) -> dict | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            name, used, total = [x.strip() for x in out.decode().splitlines()[0].split(",")[:3]]
            return {"name": name, "used": int(used), "total": int(total)}
        except Exception:
            return None

    async def llm_alive(self) -> bool:
        s = get_settings()
        url = s.llm_base_url.rstrip("/")
        if s.llm_provider == "local":
            url = url[:-3] if url.endswith("/v1") else url
            url += "/health"
        else:
            return True
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
                return (await c.get(url)).status_code == 200
        except Exception:
            return False

    async def comfy_alive(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
                return (await c.get(get_settings().comfyui_url + "/system_stats")).status_code == 200
        except Exception:
            return False

    # ---------- 切换 ----------
    @contextlib.asynccontextmanager
    async def use(self, workspace: str, detail: str = "") -> AsyncIterator[None]:
        async with self.lock:
            self.detail = detail
            if workspace == "llm":
                await self._prepare_llm()
            elif workspace in {"comfyui", "facefusion"}:
                await self._stop_llm()
                if workspace == "facefusion":
                    await self.free_comfy()
            self.owner = workspace
            try:
                yield
            finally:
                self.detail = ""

    async def comfy_switch(self, kind: str) -> None:
        """出图(Qwen-Image ≈17GB) 与出视频(H3 ≈50GB) 切换时先让 ComfyUI 卸载旧模型，避免 62GB 内存被缓存撑爆。"""
        if self.comfy_kind and self.comfy_kind != kind:
            await self.free_comfy()
        self.comfy_kind = kind

    async def free_comfy(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
                await c.post(get_settings().comfyui_url + "/free",
                             json={"unload_models": True, "free_memory": True})
            await asyncio.sleep(1)
        except Exception:
            pass
        self.comfy_kind = ""

    async def _prepare_llm(self) -> None:
        s = get_settings()
        if s.llm_provider != "local":
            return
        if await self.llm_alive():
            return
        await self.free_comfy()
        await self._start_llm()

    async def _start_llm(self) -> None:
        s = get_settings()
        if not s.llama_server_bin.is_file():
            raise RuntimeError(f"找不到 llama-server：{s.llama_server_bin}")
        if not s.llm_gguf.is_file():
            raise RuntimeError(f"找不到文字模型文件：{s.llm_gguf}")
        port = s.llm_base_url.rstrip("/").rsplit(":", 1)[-1].split("/")[0]
        s.log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(s.log_dir / "llama-server.log", "ab")
        cmd = [
            str(s.llama_server_bin), "-m", str(s.llm_gguf),
            "--host", "127.0.0.1", "--port", port,
            "-c", str(s.llm_ctx), "-ngl", "999", "--jinja",
            "--alias", s.llm_model, "-np", "1",
        ]
        log.info("启动 llama-server：%s", " ".join(cmd))
        self._llm_proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                          start_new_session=True)
        deadline = time.monotonic() + s.llm_start_timeout
        while time.monotonic() < deadline:
            if self._llm_proc.poll() is not None:
                raise RuntimeError("llama-server 启动失败，请查看 logs/llama-server.log")
            if await self.llm_alive():
                return
            await asyncio.sleep(2)
        raise RuntimeError("llama-server 启动超时")

    async def _stop_llm(self) -> None:
        s = get_settings()
        if s.llm_provider != "local":
            return
        proc = self._llm_proc
        if proc and proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(proc.pid, signal.SIGTERM)
            for _ in range(30):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.5)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    os.killpg(proc.pid, signal.SIGKILL)
        self._llm_proc = None
        # 兜底：清理不是本进程拉起的 llama-server
        if await self.llm_alive():
            subprocess.run(["pkill", "-f", str(s.llama_server_bin)], check=False)
            for _ in range(20):
                if not await self.llm_alive():
                    break
                await asyncio.sleep(0.5)
        await asyncio.sleep(1)


gpu = GPUManager()
