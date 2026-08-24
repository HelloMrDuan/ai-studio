import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from app.config import Settings
from app.core.shell import run_exec, run_shell
from app.models import GPUOwner, GPUState, SwitchPhase


class GPUOrchestrator:
    """统一调度 Gemma、ComfyUI、FaceFusion 三个 GPU 工作区。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = GPUState()
        self._state_lock = asyncio.Lock()
        self._condition = asyncio.Condition()
        self._reconcile_task: asyncio.Task | None = None
        self._switch_lock = asyncio.Lock()
        self._active = {
            GPUOwner.gemma: 0,
            GPUOwner.comfyui: 0,
            GPUOwner.facefusion: 0,
        }

    async def snapshot(self) -> GPUState:
        memory = await self._gpu_memory()
        async with self._state_lock:
            state = self.state.model_copy(deep=True)
            state.active_tasks = {
                owner.value: self._active[owner]
                for owner in (GPUOwner.gemma, GPUOwner.comfyui, GPUOwner.facefusion)
            }
            if memory:
                state.memory_used_mb = memory["used"]
                state.memory_free_mb = memory["free"]
                state.memory_total_mb = memory["total"]
            return state

    async def request(self, target: GPUOwner) -> GPUState:
        if target == GPUOwner.none:
            raise ValueError("GPU 工作区激活不接受 none")
        async with self._state_lock:
            self.state.desired_owner = target
            self.state.revision += 1
            self.state.error = None
            if self._reconcile_task is None or self._reconcile_task.done():
                self._reconcile_task = asyncio.create_task(self._reconcile())
        await self._notify()
        return await self.snapshot()

    async def ensure_ready(
        self, target: GPUOwner, timeout: float | None = None
    ) -> GPUState:
        await self.request(target)
        timeout = timeout or self.settings.gpu_switch_timeout_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            state = await self.snapshot()
            if (
                state.owner == target
                and state.desired_owner == target
                and state.phase == SwitchPhase.ready
            ):
                return state
            if state.phase == SwitchPhase.failed and state.desired_owner == target:
                raise RuntimeError(state.error or "GPU 工作区切换失败")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"等待 {target.value} 工作区就绪超时")
            async with self._condition:
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=min(2, remaining)
                    )
                except asyncio.TimeoutError:
                    pass

    async def transition(self, target: GPUOwner) -> GPUState:
        """等待目标工作区真正 READY；用于页面跳转和跨模块交接。"""
        return await self.ensure_ready(target)

    @asynccontextmanager
    async def use(self, owner: GPUOwner) -> AsyncIterator[None]:
        if owner == GPUOwner.none:
            raise ValueError("不能使用 none GPU 工作区")
        await self.ensure_ready(owner)
        async with self._state_lock:
            if (
                self.state.owner != owner
                or self.state.phase != SwitchPhase.ready
                or self.state.desired_owner != owner
            ):
                raise RuntimeError(f"{owner.value} GPU 工作区当前不可用")
            self._active[owner] += 1
        await self._notify()
        try:
            yield
        finally:
            async with self._state_lock:
                self._active[owner] = max(0, self._active[owner] - 1)
            await self._notify()

    async def _reconcile(self) -> None:
        while True:
            async with self._state_lock:
                desired = self.state.desired_owner
                owner = self.state.owner
                phase = self.state.phase
            if desired == GPUOwner.none:
                return
            if owner == desired and phase == SwitchPhase.ready:
                return
            try:
                async with self._switch_lock:
                    await self._switch(desired)
            except Exception as exc:
                await self._set_state(
                    phase=SwitchPhase.failed,
                    message="GPU 工作区切换失败",
                    error=(
                        str(exc).strip()
                        or f"{type(exc).__name__}: {exc!r}"
                    ),
                )
                return
            async with self._state_lock:
                if self.state.owner == self.state.desired_owner:
                    return

    async def _switch(self, target: GPUOwner) -> None:
        async with self._state_lock:
            previous = self.state.owner

        if previous == GPUOwner.none:
            previous = await self._detect_existing_owner()
            if previous != GPUOwner.none:
                async with self._state_lock:
                    self.state.owner = previous

        if previous != GPUOwner.none and previous != target:
            await self._set_state(
                phase=SwitchPhase.draining,
                message=f"正在等待 {self._cn(previous)} 当前任务结束",
                error=None,
            )
            await self._wait_drain(previous)
            await self._set_state(
                phase=SwitchPhase.releasing,
                message=f"正在停止 {self._cn(previous)} 并释放显存",
            )
            await self._stop_owner(previous)
            await self._wait_gpu_release()
            async with self._state_lock:
                self.state.owner = GPUOwner.none

        await self._set_state(
            phase=SwitchPhase.starting,
            message=f"正在启动 {self._cn(target)} 工作区",
            error=None,
        )
        if target == GPUOwner.gemma:
            await self._start_gemma()
        elif target == GPUOwner.comfyui:
            await self._start_comfyui()
        elif target == GPUOwner.facefusion:
            await self._prepare_facefusion()
        else:
            raise ValueError(f"不支持的 GPU 工作区：{target.value}")

        await self._set_state(
            phase=SwitchPhase.warming_up,
            message=f"正在验证 {self._cn(target)} CUDA 与服务状态",
        )
        if target == GPUOwner.gemma:
            await self._wait_gemma_ready()
        elif target == GPUOwner.comfyui:
            await self._wait_comfyui_ready()
        else:
            await self._facefusion_preflight()

        async with self._state_lock:
            self.state.owner = target
        await self._set_state(
            phase=SwitchPhase.ready,
            message=f"{self._cn(target)} 工作区已就绪",
            error=None,
        )

    async def reload_owner(self, target: GPUOwner) -> GPUState:
        # Force-reload a GPU workspace, including same-owner reloads.
        # Normal transition() rejects GPUOwner.none and treats
        # owner==target/READY as a no-op. LLM model switching needs
        # a controlled same-owner llama-server restart.
        if target == GPUOwner.none:
            raise ValueError("GPU 工作区重载不接受 none")

        async with self._switch_lock:
            async with self._state_lock:
                previous = self.state.owner
                self.state.desired_owner = target

            if previous == GPUOwner.none:
                previous = await self._detect_existing_owner()
                if previous != GPUOwner.none:
                    async with self._state_lock:
                        self.state.owner = previous

            if previous != GPUOwner.none:
                await self._set_state(
                    phase=SwitchPhase.draining,
                    message=f"正在等待 {self._cn(previous)} 当前任务结束",
                    error=None,
                )
                await self._wait_drain(previous)

                await self._set_state(
                    phase=SwitchPhase.releasing,
                    message=f"正在重载 {self._cn(previous)} 并释放显存",
                    error=None,
                )
                await self._stop_owner(previous)
                await self._wait_gpu_release()

                async with self._state_lock:
                    self.state.owner = GPUOwner.none

            try:
                await self._set_state(
                    phase=SwitchPhase.starting,
                    message=f"正在重新启动 {self._cn(target)} 工作区",
                    error=None,
                )

                if target == GPUOwner.gemma:
                    await self._start_gemma()
                elif target == GPUOwner.comfyui:
                    await self._start_comfyui()
                elif target == GPUOwner.facefusion:
                    await self._prepare_facefusion()
                else:
                    raise ValueError(f"不支持的 GPU 工作区：{target.value}")

                await self._set_state(
                    phase=SwitchPhase.warming_up,
                    message=f"正在验证 {self._cn(target)} CUDA 与服务状态",
                    error=None,
                )

                if target == GPUOwner.gemma:
                    await self._wait_gemma_ready()
                elif target == GPUOwner.comfyui:
                    await self._wait_comfyui_ready()
                else:
                    await self._facefusion_preflight()

                async with self._state_lock:
                    self.state.owner = target
                    self.state.desired_owner = target

                await self._set_state(
                    phase=SwitchPhase.ready,
                    message=f"{self._cn(target)} 工作区已就绪",
                    error=None,
                )
                return await self.snapshot()

            except Exception as exc:
                await self._set_state(
                    phase=SwitchPhase.failed,
                    message="GPU 工作区重载失败",
                    error=str(exc),
                )
                raise

    async def _detect_existing_owner(self) -> GPUOwner:
        detected: list[GPUOwner] = []
        if await self._gemma_ok() or await self._gemma_process_running():
            detected.append(GPUOwner.gemma)
        if await self._comfyui_ok():
            detected.append(GPUOwner.comfyui)
        if await self._facefusion_process_running():
            detected.append(GPUOwner.facefusion)
        if len(detected) > 1:
            labels = "、".join(self._cn(owner) for owner in detected)
            raise RuntimeError(f"检测到多个 GPU 工作区同时运行：{labels}")
        return detected[0] if detected else GPUOwner.none

    async def _gemma_process_running(self) -> bool:
        result = await run_shell(
            "pgrep -f '[l]lama-server.*--port[ =]6006' >/dev/null || "
            "pgrep -f '[l]lama-server.*gemma' >/dev/null",
            timeout=10,
        )
        return result.returncode == 0

    async def _facefusion_process_running(self) -> bool:
        result = await run_shell(
            "pgrep -f '[f]acefusion.py (headless-run|job-run|batch-run)' >/dev/null",
            timeout=10,
        )
        return result.returncode == 0

    async def _wait_drain(self, owner: GPUOwner) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.gpu_drain_timeout_seconds
        while self._active[owner] > 0:
            if loop.time() >= deadline:
                raise TimeoutError(f"等待 {owner.value} 当前任务结束超时")
            await asyncio.sleep(1)

    async def _stop_owner(self, owner: GPUOwner) -> None:
        if owner == GPUOwner.gemma:
            result = await run_shell(
                self.settings.gemma_stop_command,
                timeout=self.settings.gemma_stop_timeout_seconds + 40,
            )
            if result.returncode != 0:
                raise RuntimeError(f"停止 Gemma 失败：{result.stdout[-1600:]}")
            await self._wait_http_down(
                f"{self.settings.gemma_base_url.rstrip('/')}/models",
                self.settings.gemma_stop_timeout_seconds,
            )
            if await self._gemma_process_running():
                raise RuntimeError("Gemma 停止脚本已返回，但 llama-server 进程仍存在")
        elif owner == GPUOwner.comfyui:
            result = await run_shell(
                self.settings.comfyui_stop_command,
                timeout=self.settings.comfyui_stop_timeout_seconds + 30,
            )
            if result.returncode != 0:
                raise RuntimeError(f"停止 ComfyUI 失败：{result.stdout[-1600:]}")
            await self._wait_http_down(
                f"{self.settings.comfyui_base_url.rstrip('/')}/system_stats",
                self.settings.comfyui_stop_timeout_seconds,
            )
        elif owner == GPUOwner.facefusion:
            result = await run_shell(self.settings.facefusion_stop_command, timeout=45)
            if result.returncode != 0:
                raise RuntimeError(f"清理 FaceFusion 进程失败：{result.stdout[-1600:]}")

    async def _start_gemma(self) -> None:
        # 服务已 READY 或 llama-server 正在装载模型时，都不能再次执行启动脚本。
        # 否则启动脚本会误杀正在加载的进程，状态会长期停留在 STARTING。
        if await self._gemma_ok():
            return
        if await self._gemma_process_running():
            return
        result = await run_shell(
            self.settings.gemma_start_command,
            timeout=min(30, self.settings.gemma_start_timeout_seconds),
        )
        if result.returncode != 0:
            raise RuntimeError(f"启动 Gemma 失败：{result.stdout[-1600:]}")

    async def _wait_gemma_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.gemma_start_timeout_seconds
        while loop.time() < deadline:
            if await self._gemma_ok():
                return
            await asyncio.sleep(2)
        raise TimeoutError("Gemma 启动后未进入 READY。日志：\n" + await self._tail_log("/root/autodl-tmp/ai-studio/logs/gemma-llama-server.log"))

    async def _start_comfyui(self) -> None:
        """异步拉起 ComfyUI，READY 状态由后续健康检查统一确认。"""
        if await self._comfyui_ok():
            return

        process = await asyncio.create_subprocess_shell(
            self.settings.comfyui_start_command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        # 启动脚本只负责拉起后台服务，不在这里等待 ComfyUI READY。
        await asyncio.sleep(1)

        rc = process.returncode

        if rc is not None and rc != 0:
            # 即使启动器返回异常，也先以真实 HTTP 健康状态为准。
            if await self._comfyui_ok():
                return

            log_tail = await self._tail_log(
                "/root/autodl-tmp/ai-studio/logs/comfyui.log"
            )

            raise RuntimeError(
                f"启动 ComfyUI 失败，启动脚本返回码：{rc}；"
                f"ComfyUI 日志尾部：{log_tail}"
            )

    async def _wait_comfyui_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.comfyui_start_timeout_seconds
        while loop.time() < deadline:
            if await self._comfyui_ok():
                try:
                    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                        response = await client.get(
                            f"{self.settings.comfyui_base_url.rstrip('/')}/object_info"
                        )
                    if response.status_code == 200:
                        return
                except Exception:
                    pass
            await asyncio.sleep(2)
        raise TimeoutError("ComfyUI 启动后未进入 READY。日志：\n" + await self._tail_log("/root/autodl-tmp/ai-studio/logs/comfyui.log"))

    async def _prepare_facefusion(self) -> None:
        if not self.settings.facefusion_dir.is_dir():
            raise FileNotFoundError(f"FaceFusion 目录不存在：{self.settings.facefusion_dir}")
        if not self.settings.facefusion_python.is_file():
            raise FileNotFoundError(
                f"FaceFusion Python 不存在：{self.settings.facefusion_python}"
            )

    async def _facefusion_preflight(self) -> None:
        result = await run_exec(
            str(self.settings.facefusion_python),
            "-c",
            (
                "import json, onnxruntime as ort;"
                "p=ort.get_available_providers();"
                "print(json.dumps({'providers':p}));"
                "raise SystemExit(0 if 'CUDAExecutionProvider' in p else 2)"
            ),
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "FaceFusion CUDAExecutionProvider 不可用：" + result.stdout[-1000:]
            )
        version = await run_exec(
            str(self.settings.facefusion_python),
            "facefusion.py",
            "--version",
            cwd=str(self.settings.facefusion_dir),
            timeout=30,
        )
        if version.returncode != 0:
            raise RuntimeError("FaceFusion 版本检查失败：" + version.stdout[-1000:])

    async def _wait_gpu_release(self) -> None:
        stable = 0
        previous_used: int | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.gpu_switch_timeout_seconds
        while loop.time() < deadline:
            memory = await self._gpu_memory()
            if memory is None:
                raise RuntimeError("无法读取 nvidia-smi，不能确认显存是否释放")
            if previous_used is not None and abs(memory["used"] - previous_used) <= 128:
                stable += 1
            else:
                stable = 0
            previous_used = memory["used"]
            enough_free = memory["free"] >= self.settings.gpu_min_free_mb
            if enough_free and stable >= self.settings.gpu_release_stable_samples:
                return
            await asyncio.sleep(self.settings.gpu_release_poll_seconds)
        memory = await self._gpu_memory()
        raise TimeoutError(
            f"显存释放未达到要求，当前：{memory}，"
            f"最低空闲要求：{self.settings.gpu_min_free_mb} MB"
        )

    async def _gpu_memory(self) -> dict[str, int] | None:
        result = await run_exec(
            "nvidia-smi",
            f"--id={self.settings.gpu_device_id}",
            "--query-gpu=memory.used,memory.free,memory.total",
            "--format=csv,noheader,nounits",
            timeout=10,
        )
        if result.returncode != 0:
            return None
        try:
            used, free, total = [
                int(value.strip()) for value in result.stdout.strip().split(",")[:3]
            ]
            return {"used": used, "free": free, "total": total}
        except Exception:
            return None

    async def _gemma_ok(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    f"{self.settings.gemma_base_url.rstrip('/')}/models"
                )
                if response.status_code != 200:
                    return False
                payload = response.json()
                return bool(payload.get("data")) if isinstance(payload, dict) else False
        except Exception:
            return False

    async def _comfyui_ok(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(
                    f"{self.settings.comfyui_base_url.rstrip('/')}/system_stats"
                )
                return response.status_code == 200
        except Exception:
            return False

    async def _wait_http_down(self, url: str, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    await client.get(url)
            except Exception:
                return
            await asyncio.sleep(1)
        raise TimeoutError(f"服务未停止：{url}")

    async def _tail_log(self, path: str, lines: int = 120) -> str:
        result = await run_shell(
            f"test -f {path!r} && tail -n {int(lines)} {path!r} || true",
            timeout=10,
        )
        return result.stdout[-12000:]

    async def _set_state(
        self,
        *,
        phase: SwitchPhase,
        message: str,
        error: str | None = None,
    ) -> None:
        async with self._state_lock:
            self.state.phase = phase
            self.state.message = message
            self.state.error = error
        await self._notify()

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _cn(owner: GPUOwner) -> str:
        return {
            GPUOwner.gemma: "LLM 助手",
            GPUOwner.comfyui: "图片生成",
            GPUOwner.facefusion: "人物与画面处理",
            GPUOwner.none: "空闲",
        }[owner]
