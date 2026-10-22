"""自动质检：调用 scripts/qa_check.py（FaceFusion 环境，CPU 运行）。质检本身出错不影响生成结果。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from .config import ROOT, get_settings

log = logging.getLogger("studio.qa")
SCRIPT = ROOT / "scripts" / "qa_check.py"
LIGHT_SCRIPT = ROOT / "scripts" / "light_lock.py"
REAL_STYLES = {"cinematic", "cyberpunk"}   # ArcFace 只对写实人脸可靠；动漫/国风等画风只查文字


def models_dir() -> Path:
    return get_settings().facefusion_dir / ".assets" / "models"


def available() -> bool:
    s = get_settings()
    return bool(s.qa_enabled and s.qa_python and Path(s.qa_python).is_file() and SCRIPT.is_file())


def clean_env() -> dict:
    """去掉 conda 变量，避免子进程加载 conda 的 libstdc++（同 facefusion.run）。"""
    env = dict(os.environ)
    prefix = env.get("CONDA_PREFIX", "")
    for k in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "CONDA_PYTHON_EXE", "CONDA_EXE", "CONDA_PROMPT_MODIFIER"):
        env.pop(k, None)
    if prefix and env.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = ":".join(p for p in env["LD_LIBRARY_PATH"].split(":") if p and not p.startswith(prefix))
    return env


async def check(target: Path, *, reference: Path | None = None, faces: list[tuple[str, Path]] = (),
                check_text: bool = True, style: str = "cinematic", lines: list[str] = (),
                camera: str = "", camera_dir: str = "") -> dict | None:
    """返回 {"ok": bool, "issues": [{"type","detail"}], ...}；质检不可用时返回 None。"""
    if not available():
        return None
    s = get_settings()
    job = {"target": str(target), "reference": str(reference) if reference else "",
           "faces": [{"name": n, "path": str(p)} for n, p in faces], "check_text": check_text, "lines": [x for x in lines if x],
           "check_faces": style in REAL_STYLES and (models_dir() / "arcface_w600k_r50.onnx").is_file(),
           "models_dir": str(models_dir()), "face_threshold": s.qa_face_threshold, "frames": 5,
           "camera": camera, "camera_dir": camera_dir}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_path = fh.name
    try:
        proc = await asyncio.create_subprocess_exec(str(s.qa_python), str(SCRIPT), job_path, env=clean_env(), stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.startswith("{")]
        if not lines:
            raise RuntimeError(err.decode("utf-8", "replace")[-500:] or "质检脚本没有输出")
        return json.loads(lines[-1])
    except Exception as exc:  # noqa: BLE001
        log.warning("质检失败（忽略）：%s", exc)
        return {"ok": True, "issues": [], "warnings": [f"质检失败：{str(exc)[:200]}"]}
    finally:
        Path(job_path).unlink(missing_ok=True)


async def face_layout(a: Path, b: Path, style: str = "cinematic") -> dict | None:
    """两张图的人脸布局对比数据（见 scripts/qa_check.py face_layout）；不可用或失败返回 None。"""
    if not available() or not (models_dir() / "yoloface_8n.onnx").is_file():
        return None
    s = get_settings()
    identity = style in REAL_STYLES and (models_dir() / "arcface_w600k_r50.onnx").is_file()
    job = {"layout": [str(a), str(b)], "models_dir": str(models_dir()), "identity": identity}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_path = fh.name
    try:
        proc = await asyncio.create_subprocess_exec(str(s.qa_python), str(SCRIPT), job_path, env=clean_env(), stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.startswith("{")]
        if not lines:
            raise RuntimeError(err.decode("utf-8", "replace")[-300:] or "没有输出")
        return json.loads(lines[-1])
    except Exception as exc:  # noqa: BLE001
        log.warning("构图对比失败（忽略）：%s", exc)
        return None
    finally:
        Path(job_path).unlink(missing_ok=True)


def layout_mismatch(info: dict | None) -> str:
    """上一镜尾帧（a）和本镜分镜图（b）的人脸布局差太多 → 返回原因（硬接尾帧会让视频中途变构图）。
    实测 4 个接续失败的镜头：人数 1→2、1→2、2→3、同位置但换了人；同一镜头首尾帧（正常）人数和大小基本不变。"""
    if not info:
        return ""
    a, b = info.get("a") or [], info.get("b") or []
    if not a and not b:
        return ""
    if len(a) != len(b):
        return f"人数/构图不同（上一镜尾帧 {len(a)} 张脸，本镜分镜图 {len(b)} 张）"
    ratio = max(a[0]["s"], b[0]["s"]) / max(1e-3, min(a[0]["s"], b[0]["s"]))
    if ratio > 1.8:
        return f"景别不同（人脸大小差 {ratio:.1f} 倍）"
    xa, xb = sorted(f["cx"] for f in a), sorted(f["cx"] for f in b)
    shift = max(abs(x - y) for x, y in zip(xa, xb))
    if shift > 0.3:
        return f"人物位置不同（水平错开 {shift:.0%}）"
    sims = info.get("sims") or []
    if sims and len(a) == 1 and sims[0] and sims[0][0] < 0.25:
        return f"画面里不是同一个人（相似度 {sims[0][0]:.2f}）"
    if sims and len(a) > 1 and all(row for row in sims):
        best = [max(row) for row in sims]
        if min(best) < 0.25:
            return f"画面里的人物对不上（最低相似度 {min(best):.2f}）"
    return ""


def rank(result: dict | None) -> tuple:
    """越小越好：先看是否合格，再看问题数，再看最低人脸相似度。"""
    if not result:
        return (0, 0, 0.0)
    scores = list(((result.get("faces") or {}).get("scores") or {}).values())
    return (0 if result.get("ok") else 1, len(result.get("issues") or []), -(min(scores) if scores else 1.0))


def summary(result: dict | None) -> str:
    if not result:
        return ""
    return "质检通过" if result.get("ok") else "；".join(i["detail"] for i in result.get("issues") or [])


async def light_lock(video: Path, strength: float = 1.0) -> tuple[Path, dict]:
    """把逐渐变亮/变色的视频拉回首帧光线（见 scripts/light_lock.py）。不可用或失败时原样返回。"""
    s = get_settings()
    if not (s.video_light_lock and s.qa_python and Path(s.qa_python).is_file() and LIGHT_SCRIPT.is_file()):
        return video, {}
    out = video.with_name(video.stem + "_ll.mp4")
    job = {"src": str(video), "out": str(out), "ffmpeg": shutil.which("ffmpeg") or "ffmpeg", "strength": strength}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_path = fh.name
    try:
        proc = await asyncio.create_subprocess_exec(str(s.qa_python), str(LIGHT_SCRIPT), job_path, env=clean_env(),
                                                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        o, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        lines = [ln for ln in o.decode("utf-8", "replace").splitlines() if ln.startswith("{")]
        if not lines:
            raise RuntimeError(err.decode("utf-8", "replace")[-300:] or "没有输出")
        info = json.loads(lines[-1])
        if info.get("applied") and out.is_file() and out.stat().st_size > 1000:
            video.unlink(missing_ok=True)
            return out.rename(video), info
        return video, info
    except Exception as exc:  # noqa: BLE001
        log.warning("光线锁定失败（忽略）：%s", exc)
        out.unlink(missing_ok=True)
        return video, {"error": str(exc)[:200]}
    finally:
        Path(job_path).unlink(missing_ok=True)
