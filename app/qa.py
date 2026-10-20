"""自动质检：调用 scripts/qa_check.py（FaceFusion 环境，CPU 运行）。质检本身出错不影响生成结果。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from .config import ROOT, get_settings

log = logging.getLogger("studio.qa")
SCRIPT = ROOT / "scripts" / "qa_check.py"
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
                check_text: bool = True, style: str = "cinematic") -> dict | None:
    """返回 {"ok": bool, "issues": [{"type","detail"}], ...}；质检不可用时返回 None。"""
    if not available():
        return None
    s = get_settings()
    job = {"target": str(target), "reference": str(reference) if reference else "",
           "faces": [{"name": n, "path": str(p)} for n, p in faces], "check_text": check_text,
           "check_faces": style in REAL_STYLES and (models_dir() / "arcface_w600k_r50.onnx").is_file(),
           "models_dir": str(models_dir()), "face_threshold": s.qa_face_threshold, "frames": 5}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False)
        job_path = fh.name
    try:
        proc = await asyncio.create_subprocess_exec(str(s.qa_python), str(SCRIPT), job_path, env=clean_env(),
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
