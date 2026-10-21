"""配音与对口型（deploy/06_voice.sh 安装）：
- IndexTTS2：用角色的「音色样本」零样本克隆，所有台词同一个声音，支持情绪文字控制。
- Demucs：把视频原声里的人声去掉，只保留环境音/音效（H3 自己配的声音每个镜头都不一样）。
- LatentSync 1.6：按新配音重做口型。
三者都在独立的 Python 环境里以子进程运行，GPU 用完即释放。
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from .config import ROOT, get_settings
from .qa import clean_env

log = logging.getLogger("studio.voice")
Log = Callable[[str], Awaitable[None]] | None


def tts_available() -> bool:
    s = get_settings()
    return Path(s.tts_python).is_file() and (Path(s.tts_dir) / "checkpoints" / "config.yaml").is_file()


def separator_available() -> bool:
    return (Path(get_settings().tts_dir) / ".demucs_ok").is_file()


def lipsync_available() -> bool:
    s = get_settings()
    return Path(s.lipsync_python).is_file() and (Path(s.lipsync_dir) / "checkpoints" / "latentsync_unet.pt").is_file()


def status() -> dict:
    return {"tts": tts_available(), "separator": separator_available(), "lipsync": lipsync_available()}


async def _run(cmd: list[str], cwd: Path, log_cb: Log = None, timeout: int | None = None) -> str:
    s = get_settings()
    env = clean_env()
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["HF_HOME"] = str(s.base_dir / ".cache" / "huggingface")
    env["TORCH_HOME"] = str(s.base_dir / ".cache" / "torch")
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    lines: list[str] = []

    async def reader() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            del lines[:-200]
            if log_cb and line.strip() and not line.startswith(" "):
                await log_cb(line[-200:])

    try:
        await asyncio.wait_for(asyncio.gather(reader(), proc.wait()), timeout=timeout or s.voice_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{Path(cmd[1]).name} 超时")
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError("\n".join(lines[-15:])[-1500:])
    return "\n".join(lines)


async def synthesize(items: list[dict], log_cb: Log = None) -> None:
    """items: [{"text", "voice": 音色样本路径, "emo": 情绪文字, "out": 输出 wav}]；一次加载模型合成全部台词。"""
    if not tts_available():
        raise RuntimeError("未安装配音模型（IndexTTS2），请运行 bash deploy/06_voice.sh")
    s = get_settings()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump({"tts_dir": str(s.tts_dir), "items": items}, fh, ensure_ascii=False)
        job = fh.name
    try:
        await _run([str(s.tts_python), str(ROOT / "scripts" / "tts_run.py"), job], Path(s.tts_dir), log_cb)
    finally:
        Path(job).unlink(missing_ok=True)
    missing = [it["out"] for it in items if not Path(it["out"]).is_file()]
    if missing:
        raise RuntimeError(f"配音没有生成：{missing[0]}")


async def remove_vocals(audio: Path, out_dir: Path) -> Path:
    """Demucs 两轨分离（CPU 即可，几秒钟），返回去掉人声后的伴奏/环境音 wav。"""
    s = get_settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    await _run([str(s.tts_python), "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-d", "cpu",
                "-o", str(out_dir), str(audio)], out_dir, timeout=600)
    found = list(out_dir.glob(f"htdemucs/{audio.stem}/no_vocals.*"))
    if not found:
        raise RuntimeError("Demucs 没有输出 no_vocals")
    return found[0]


async def isolate_vocals(audio: Path, out_dir: Path) -> Path:
    s = get_settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    await _run([str(s.tts_python), "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-d", "cpu",
                "-o", str(out_dir), str(audio)], out_dir, timeout=600)
    found = list(out_dir.glob(f"htdemucs/{audio.stem}/vocals.*"))
    if not found:
        raise RuntimeError("Demucs 没有输出 vocals")
    return found[0]


async def lipsync(video: Path, audio: Path, output: Path, log_cb: Log = None, seed: int = 1247) -> Path:
    """LatentSync 1.6：输出时长 = min(视频, 音频)，所以 audio 要先补静音到和视频一样长。"""
    if not lipsync_available():
        raise RuntimeError("未安装对口型模型（LatentSync），请运行 bash deploy/06_voice.sh")
    s = get_settings()
    d = Path(s.lipsync_dir)
    cmd = [str(s.lipsync_python), "-m", "scripts.inference",
           "--unet_config_path", "configs/unet/stage2_512.yaml",
           "--inference_ckpt_path", "checkpoints/latentsync_unet.pt",
           "--inference_steps", str(s.lipsync_steps), "--guidance_scale", "1.5", "--seed", str(seed),
           "--video_path", str(video), "--audio_path", str(audio), "--video_out_path", str(output)]
    if (d / ".deepcache_ok").is_file():
        cmd.append("--enable_deepcache")
    output.parent.mkdir(parents=True, exist_ok=True)
    await _run(cmd, d, log_cb)
    if not output.is_file():
        raise RuntimeError("LatentSync 没有输出视频")
    return output
