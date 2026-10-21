"""P3-1 超分 / P3-2 补帧：逐镜头生成增强版视频（新版本，可随时切回原版）。
超分：Real-ESRGAN 系（spandrel 逐帧流式，scripts/upscale.py）或 SeedVR2（ComfyUI 工作流模板）。
补帧：RIFE（ComfyUI-Frame-Interpolation 节点）；没装时用 ffmpeg 运动补偿插帧（CPU，较慢、效果一般）。"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, media
from .config import ROOT, get_settings
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")

UPSCALE_TARGETS = {"1080p": 1080, "1440p": 1440, "4k": 2160}     # 短边像素
FPS_CHOICES = (30, 48, 60)
# 成片可选输出尺寸（在 VIDEO_SIZES 的 draft/final 之外）
EXPORT_SIZES = {
    "1080p": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:3": (1440, 1080), "3:4": (1080, 1440)},
    "1440p": {"16:9": (2560, 1440), "9:16": (1440, 2560), "1:1": (1440, 1440), "4:3": (1920, 1440), "3:4": (1440, 1920)},
}


def _models_dir() -> Path:
    return get_settings().comfyui_dir / "models" / "upscale_models"


def esrgan_models() -> list[str]:
    d = _models_dir()
    return sorted(p.name for p in d.glob("*") if p.suffix in (".pth", ".safetensors")) if d.is_dir() else []


def esrgan_available() -> bool:
    py = get_settings().comfyui_python
    return bool(py and Path(py).is_file() and esrgan_models())


def seedvr2_template() -> dict | None:
    p = get_settings().data_dir / "workflows" / "upscale_seedvr2.json"
    return json.loads(p.read_text("utf-8")) if p.is_file() else None


def rife_available() -> bool:
    return (get_settings().comfyui_dir / "custom_nodes" / "ComfyUI-Frame-Interpolation").is_dir()


def status() -> dict:
    return {"esrgan": esrgan_available(), "esrgan_models": esrgan_models(), "seedvr2": seedvr2_template() is not None,
            "rife": rife_available(), "targets": list(UPSCALE_TARGETS), "fps": list(FPS_CHOICES)}


async def video_info(path: Path) -> dict:
    text = await media._run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate", "-show_entries", "format=duration", "-of", "json", str(path))
    d = json.loads(text)
    s = (d.get("streams") or [{}])[0]
    num, _, den = (s.get("r_frame_rate") or "24/1").partition("/")
    return {"width": int(s.get("width") or 0), "height": int(s.get("height") or 0),
            "fps": round(float(num) / float(den or 1), 3), "duration": float((d.get("format") or {}).get("duration") or 0)}


def target_size(w: int, h: int, short: int) -> tuple[int, int]:
    """按源画幅把短边放大到 short，宽高取偶数。"""
    k = short / max(1, min(w, h))
    return int(round(w * k / 2)) * 2, int(round(h * k / 2)) * 2


def _source(shot: dict) -> tuple[Path, dict]:
    from .pipeline import absolute, exists
    if not exists(shot.get("video")):
        raise ValueError("这个镜头还没有视频")
    rows = db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [shot["video"]])
    return absolute(shot["video"]), ((rows[0].get("meta") if rows else None) or {})


def _inherit(meta: dict) -> dict:
    """新版本继承配音/口型/字幕时间等信息，成片字幕仍按配音时间对齐。"""
    keep = ("dub", "lipsync", "dub_source", "lines", "timing", "prompt", "mode", "quality", "seconds", "enhanced", "stems")
    out = {k: meta[k] for k in keep if k in meta}
    out["enhanced"] = list(out.get("enhanced") or [])
    return out


async def _script(cmd: list[str], ctx: TaskContext, lo: int, hi: int) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    tail: list[str] = []
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        tail = (tail + [line])[-20:]
        if line.startswith("PROGRESS "):
            a, _, b = line[9:].partition("/")
            await ctx.progress(lo + int((hi - lo) * int(a) / max(1, int(b or 1))), f"超分 {a}/{b} 帧")
        elif line.startswith("INFO "):
            await ctx.log(line[5:])
    if await proc.wait() != 0:
        raise RuntimeError("超分失败：" + "\n".join(tail)[-1200:])


async def run_esrgan(src: Path, out: Path, width: int, height: int, model: str, ctx: TaskContext) -> None:
    s = get_settings()
    mp = _models_dir() / model
    if not mp.is_file():
        raise RuntimeError(f"没有超分模型 {model}：请运行 bash deploy/10_enhance.sh")
    await _script([str(s.comfyui_python), str(ROOT / "scripts" / "upscale.py"), "--input", str(src), "--output", str(out),
                   "--model", str(mp), "--width", str(width), "--height", str(height),
                   "--tile", "0" if "animevideo" in model else "512"], ctx, 10, 95)


@handler("upscale")
async def task_upscale(ctx: TaskContext) -> dict:
    from . import comfy
    from .gpu import gpu
    from .pipeline import VIDEO_SUFFIX, add_asset, need, progress_slice, project_dir, rel
    from .videox import fill
    from .workflows import seed_or_random
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    src, meta = _source(shot)
    info = await video_info(src)
    target = ctx.params.get("target") if ctx.params.get("target") in UPSCALE_TARGETS else "1080p"
    w, h = target_size(info["width"], info["height"], UPSCALE_TARGETS[target])
    if min(info["width"], info["height"]) >= UPSCALE_TARGETS[target]:
        raise ValueError(f"视频已经是 {info['width']}x{info['height']}，不需要超分到 {target}")
    method = ctx.params.get("method") or "auto"
    if method == "auto":
        method = "esrgan" if esrgan_available() else "seedvr2" if seedvr2_template() else ""
    if not method:
        raise RuntimeError("未安装超分组件：请在服务器运行 bash deploy/10_enhance.sh")
    out_dir = project_dir(shot["project_id"], "videos")
    out = out_dir / f"up_{shot['id']}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    await ctx.progress(5, f"超分 {info['width']}x{info['height']} → {w}x{h}（{method}）")
    async with gpu.use("comfyui", ctx.task.get("title") or "超分"):
        if method == "seedvr2":
            tpl = seedvr2_template()
            if tpl is None:
                raise RuntimeError("未配置 SeedVR2 工作流：data/workflows/upscale_seedvr2.json（见 docs/DEPLOY.md）")
            await gpu.comfy_switch("upscale")
            name = await comfy.upload_image(src)
            files = await comfy.run(fill(tpl, {"VIDEO": name, "WIDTH": w, "HEIGHT": h, "SHORT": min(w, h),
                                               "SEED": seed_or_random(None), "FPS": info["fps"]}),
                                    out_dir / f"up_{ctx.id}", progress_slice(ctx, 10, 90, "SeedVR2"))
            v = next((f for f in files if f.suffix.lower() in VIDEO_SUFFIX), None)
            if not v:
                raise RuntimeError("SeedVR2 工作流没有产出视频")
            await media._run("ffmpeg", "-y", "-i", str(v), "-i", str(src), "-map", "0:v", "-map", "1:a?",
                             "-vf", f"scale={w}:{h}:flags=lanczos", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
                             "-c:a", "copy", str(out))
            shutil.rmtree(v.parent, ignore_errors=True)
        else:
            await gpu.comfy_switch("upscale")        # 先让 ComfyUI 卸载模型，把显存让给超分脚本
            model = ctx.params.get("model") or get_settings().upscale_model
            await run_esrgan(src, out, w, h, model, ctx)
    m = _inherit(meta)
    m["enhanced"].append(f"超分 {target}")
    m.update({"source": rel(src), "upscale": {"method": method, "from": f"{info['width']}x{info['height']}", "to": f"{w}x{h}"}})
    a = add_asset(shot["project_id"], "video", out, "shot", shot["id"], m)
    db.update("shots", shot["id"], {"video": a["path"]})
    return {"asset": a["id"], "size": f"{w}x{h}"}


def rife_workflow(video: str, multiplier: int, fps: float) -> dict:
    s = get_settings()
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": video}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "RIFE VFI", "inputs": {"ckpt_name": s.rife_ckpt, "frames": ["2", 0], "clear_cache_after_n_frames": 10,
                                                   "multiplier": multiplier, "fast_mode": True, "ensemble": True,
                                                   "scale_factor": 1.0}},
        "4": {"class_type": "CreateVideo", "inputs": {"images": ["3", 0], "audio": ["2", 1], "fps": float(fps)}},
        "5": {"class_type": "SaveVideo", "inputs": {"video": ["4", 0], "filename_prefix": "studio/rife", "format": "auto",
                                                    "codec": "auto"}},
    }


@handler("interpolate")
async def task_interpolate(ctx: TaskContext) -> dict:
    import math
    from . import comfy
    from .gpu import gpu
    from .pipeline import VIDEO_SUFFIX, add_asset, need, progress_slice, project_dir, rel
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    src, meta = _source(shot)
    info = await video_info(src)
    fps = int(ctx.params.get("fps") or 48)
    if fps not in FPS_CHOICES:
        raise ValueError(f"帧率只支持 {FPS_CHOICES}")
    if info["fps"] >= fps - 0.5:
        raise ValueError(f"视频已经是 {info['fps']:g} 帧/秒")
    method = ctx.params.get("method") or "auto"
    if method == "auto":
        method = "rife" if rife_available() else "ffmpeg"
    out_dir = project_dir(shot["project_id"], "videos")
    out = out_dir / f"fi_{shot['id']}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    if method == "rife":
        mult = max(2, math.ceil(fps / info["fps"] - 1e-6))
        async with gpu.use("comfyui", ctx.task.get("title") or "补帧"):
            await gpu.comfy_switch("interpolate")
            name = await comfy.upload_image(src)
            files = await comfy.run(rife_workflow(name, mult, info["fps"] * mult), out_dir / f"fi_{ctx.id}",
                                    progress_slice(ctx, 5, 90, "RIFE 补帧"))
        v = next((f for f in files if f.suffix.lower() in VIDEO_SUFFIX), None)
        if not v:
            raise RuntimeError("补帧工作流没有产出视频")
        # 倍数补完再精确重采样到目标帧率（如 24×3=72 → 60），音轨取原视频
        await media._run("ffmpeg", "-y", "-i", str(v), "-i", str(src), "-map", "0:v", "-map", "1:a?", "-vf", f"fps={fps}",
                         "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out))
        shutil.rmtree(v.parent, ignore_errors=True)
    else:
        await ctx.progress(10, f"ffmpeg 运动补偿插帧 → {fps} 帧/秒（CPU）")
        await media._run("ffmpeg", "-y", "-i", str(src), "-map", "0:v", "-map", "0:a?",
                         "-vf", f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
                         "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out))
    m = _inherit(meta)
    m["enhanced"].append(f"补帧 {fps}fps")
    m.update({"source": rel(src), "interpolate": {"method": method, "from": info["fps"], "to": fps}})
    a = add_asset(shot["project_id"], "video", out, "shot", shot["id"], m)
    db.update("shots", shot["id"], {"video": a["path"]})
    return {"asset": a["id"], "fps": fps, "method": method}


# ---------------- 接口 ----------------
class UpscaleIn(BaseModel):
    target: str = "1080p"
    method: str = "auto"        # auto | esrgan | seedvr2
    model: str = ""


class InterpIn(BaseModel):
    fps: int = 48
    method: str = "auto"        # auto | rife | ffmpeg


class BatchIn(BaseModel):
    kind: str                   # upscale | interpolate
    only_missing: bool = True
    target: str = "1080p"
    method: str = "auto"
    fps: int = 48


def _shot(shot_id: str) -> dict:
    s = db.get("shots", shot_id)
    if not s:
        raise HTTPException(404, "镜头不存在")
    if not s.get("video"):
        raise HTTPException(400, "这个镜头还没有视频")
    return s


@router.get("/enhance/status")
async def enhance_status() -> dict:
    return status()


@router.post("/shots/{shot_id}/upscale")
async def upscale(shot_id: str, body: UpscaleIn) -> dict:
    s = _shot(shot_id)
    return queue.submit("upscale", project_id=s["project_id"], title=f"超分 #{s['sort'] + 1}", owner_type="shot",
                        owner_id=shot_id, params={"shot_id": shot_id, **body.model_dump()})


@router.post("/shots/{shot_id}/interpolate")
async def interpolate(shot_id: str, body: InterpIn) -> dict:
    s = _shot(shot_id)
    return queue.submit("interpolate", project_id=s["project_id"], title=f"补帧 #{s['sort'] + 1}", owner_type="shot",
                        owner_id=shot_id, params={"shot_id": shot_id, **body.model_dump()})


@router.post("/projects/{project_id}/enhance")
async def enhance_all(project_id: str, body: BatchIn) -> dict:
    from .pipeline import shots_of
    if body.kind not in ("upscale", "interpolate"):
        raise HTTPException(400, "kind 只能是 upscale / interpolate")
    tag = "超分" if body.kind == "upscale" else "补帧"
    out = []
    for s in shots_of(project_id):
        if not s.get("video"):
            continue
        rows = db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["video"]])
        done = any(e.startswith(tag) for e in ((rows[0].get("meta") or {}).get("enhanced") or [] if rows else []))
        if body.only_missing and done:
            continue
        params = {"shot_id": s["id"], "method": body.method, "_batch": f"{body.kind}-{project_id[:6]}"}
        params.update({"target": body.target} if body.kind == "upscale" else {"fps": body.fps})
        out.append(queue.submit(body.kind, project_id=project_id, title=f"{tag} #{s['sort'] + 1}", owner_type="shot",
                                owner_id=s["id"], params=params))
    return {"submitted": len(out)}
