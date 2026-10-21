"""失败自动恢复：识别错误类型 → 给出中文建议 → 能自动处理的（显存不足、ComfyUI 崩溃/卡死、文字模型掉线、超时）
先修复环境再重试，最多 MAX_AUTO_RETRY 次；显存不足第二次重试时自动降级（草稿质量 / Turbo / 少出几张）。"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass

from .config import ROOT, get_settings
from .gpu import gpu

log = logging.getLogger("studio.recovery")
MAX_AUTO_RETRY = 2
PAUSE = 3.0          # 修复后稍等再重试（秒）；测试里设为 0


@dataclass
class Diagnosis:
    kind: str          # oom / comfy_down / llm_down / timeout / config / disk / input / unknown
    advice: str        # 给用户看的中文说明和建议
    retry: bool        # 是否值得自动重试


RULES: list[tuple[str, str, str, bool]] = [
    # (kind, 正则, 建议, 是否自动重试)
    ("disk", r"No space left on device|磁盘已满|Disk quota exceeded",
     "磁盘空间不足：请在系统状态页查看剩余空间，删除不用的项目或历史候选后重试", False),
    ("oom", r"out of memory|OutOfMemoryError|CUDA error: out of memory|CUBLAS_STATUS_ALLOC_FAILED|"
            r"Allocation on device|not enough memory|DefaultCPUAllocator: can't allocate|显存不足",
     "显存/内存不足：已自动释放显存后重试；仍失败可改用草稿质量、开启 Turbo、缩短镜头时长，或先点系统状态页「释放显存」", True),
    ("config", r"value_not_in_list|Value not in list|Cannot find|找不到|missing_node_type|is not a valid|"
               r"No such file or directory: .*\.(safetensors|gguf|onnx|pth|pt)|未安装",
     "模型文件或节点缺失：在服务器运行 python scripts/preflight.py 检查缺了什么，按提示补装", False),
    ("comfy_down", r"All connection attempts failed|Connection refused|ConnectError|Server disconnected|"
                   r"RemoteProtocolError|ReadError|ConnectionResetError|Broken pipe|ComfyUI 任务超时",
     "服务连接中断（可能已崩溃或卡死）：已自动重启服务后重试", True),
    ("timeout", r"TimeoutException|ReadTimeout|timed out|超时",
     "执行超时：已自动重试；如果反复超时，请把镜头时长调短或改用草稿质量", True),
    ("input", r"^请先|还没有|不能为空|不支持|没有可配音|没有需要", "", False),
]


def diagnose(task_type: str, error: str) -> Diagnosis:
    from .tasks import GROUP
    text = error or ""

    def task_group(t: str) -> str:
        return GROUP.get(t, "cpu")
    if task_group(task_type) == "cloud":           # 云端接口：只重试网络抖动 / 限流 / 服务端 5xx，不动本机服务
        if re.search(r"timed? ?out|timeout|connect|429|rate.?limit|限流|5\d\d|temporar|稍后", text, re.I):
            return Diagnosis("cloud", "云端接口暂时不可用：已自动重试；仍失败请检查网络、API Key 和账户余额", True)
        return Diagnosis("unknown", "", False)
    for kind, pattern, advice, retry in RULES:
        if re.search(pattern, text, re.I | re.M):
            if kind == "comfy_down" and task_group(task_type) == "llm":
                return Diagnosis("llm_down", "文字模型服务连接中断：已自动重启文字模型后重试；用云端 API 时请检查网络和 API Key", True)
            if kind == "comfy_down" and task_group(task_type) in {"voice", "facefusion", "cpu"}:
                return Diagnosis("unknown", "子进程异常退出：已自动重试；仍失败请查看 logs 目录下的日志", True)
            return Diagnosis(kind, advice, retry)
    return Diagnosis("unknown", "", False)


def explain(task_type: str, error: str) -> str:
    """失败信息：中文建议在前，原始错误附在后面。"""
    d = diagnose(task_type, error)
    raw = (error or "").strip().replace("\n", " ")
    if not d.advice:
        return raw[:500]
    return f"{d.advice}。原始错误：{raw[:300]}"


# ---------------- 修复动作 ----------------
async def restart_comfy(reason: str = "") -> bool:
    """结束并重新拉起 ComfyUI，等待就绪。命令可用 COMFYUI_RESTART_CMD 覆盖（测试/自定义部署）。"""
    s = get_settings()
    cmd = s.comfyui_restart_cmd or f"bash {ROOT / 'deploy' / 'start_comfyui.sh'} --restart"
    log.warning("重启 ComfyUI（%s）：%s", reason, cmd)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    gpu.comfy_kind = ""

    def _run() -> int:
        with open(s.log_dir / "recovery.log", "ab") as fh:
            return subprocess.run(cmd, shell=True, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  start_new_session=True, timeout=400).returncode
    try:
        code = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        log.error("重启 ComfyUI 失败：%s", exc)
        return False
    for _ in range(30 if PAUSE else 1):
        if await gpu.comfy_alive():
            gpu.restarts += 1
            return True
        await asyncio.sleep(2 if PAUSE else 0)
    log.error("ComfyUI 重启后仍未就绪（退出码 %s）", code)
    return False


async def ensure_comfy() -> None:
    """用 ComfyUI 前检查它活着；挂了就先拉起来（上一个任务把它弄崩、或被系统 OOM 杀掉的情况）。"""
    if not await gpu.comfy_alive():
        if not await restart_comfy("检测到 ComfyUI 不可用"):
            raise RuntimeError("ComfyUI 不可用且自动重启失败：Connection refused，请在服务器运行 bash deploy/start.sh")


async def recover(diag: Diagnosis, attempt: int) -> str:
    """执行修复动作，返回给用户看的说明。"""
    if diag.kind == "oom":
        await gpu.free_comfy()
        await gpu._stop_llm()
        await asyncio.sleep(PAUSE)
        if attempt >= 2:
            return "显存不足，已释放显存并降级参数重试"
        return "显存不足，已释放显存重试"
    if diag.kind == "comfy_down":
        ok = await restart_comfy("任务执行中连接中断")
        return "ComfyUI 已重启，重试中" if ok else "ComfyUI 重启失败，仍尝试重试"
    if diag.kind == "llm_down":
        await gpu._stop_llm()
        await asyncio.sleep(PAUSE)
        return "文字模型已重启，重试中"
    await asyncio.sleep(PAUSE)
    return "自动重试中"


def degrade(task_type: str, params: dict) -> dict:
    """显存不足第二次重试：降低参数。"""
    p = dict(params)
    if task_type == "video":
        p.update(quality="draft", turbo=True)
    if "count" in p:
        p["count"] = 1
    p["_degraded"] = True
    return p
