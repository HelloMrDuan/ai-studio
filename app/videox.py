"""P2-5 视频生成增强：运镜模板 / 运镜 LoRA、视频延长（尾帧续写）、动作参考（自定义工作流）。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db, media
from .config import get_settings
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")

# 运镜：方括号指令（MiniMax 系模型训练时用过的运镜标记）+ 具体的画面变化描述，比只写「推镜」可靠得多
CAMERA_TEMPLATES = {
    "固定": ("[固定]", "固定机位，摄像机完全静止，没有推拉摇移，只有画面内的人物和物体在动"),
    "推镜": ("[推进]", "镜头缓慢匀速向前推进，主体在画面中逐渐变大，背景随之收窄，结束时比开头更近一级景别"),
    "拉镜": ("[拉远]", "镜头缓慢匀速向后拉远，主体在画面中逐渐变小，周围环境逐渐露出来"),
    "摇镜": ("[右摇]", "摄像机位置不动，镜头水平向右缓慢摇动，画面从左向右扫过场景"),
    "移镜": ("[右移]", "摄像机沿水平方向向右平移（横移），前景和背景产生视差，主体保持在画面内"),
    "跟拍": ("[跟随]", "镜头跟随主体移动，主体始终保持在画面中相近的位置，背景向后掠过"),
    "环绕": ("[左绕]", "镜头围绕主体做弧形环绕运动，主体保持在画面中心，背景随视角旋转变化"),
    "升降": ("[上升]", "镜头垂直缓慢升起，视角逐渐从平视变为俯视，展示更多环境"),
}
DIRECTIONS = {"摇镜": {"左": ("[左摇]", "向左"), "右": ("[右摇]", "向右"), "上": ("[上摇]", "向上"), "下": ("[下摇]", "向下")},
              "移镜": {"左": ("[左移]", "向左"), "右": ("[右移]", "向右")},
              "环绕": {"左": ("[左绕]", "逆时针"), "右": ("[右绕]", "顺时针")},
              "升降": {"上": ("[上升]", "升起"), "下": ("[下降]", "降下")}}


def camera_text(shot: dict) -> str:
    move = shot.get("camera_move") or "固定"
    tag, text = CAMERA_TEMPLATES.get(move, ("", move))
    d = (shot.get("camera_dir") or "").strip()
    if d and d in DIRECTIONS.get(move, {}):
        tag, word = DIRECTIONS[move][d]
        text = text.replace("向右", word).replace("向左", word).replace("升起", word)
    speed = shot.get("camera_speed") or ""
    if speed == "快":
        text = text.replace("缓慢", "快速").replace("匀速", "")
    return f"{tag} {text}".strip()


def camera_loras() -> dict[str, str]:
    raw = get_settings().camera_loras.strip()
    if not raw:
        f = get_settings().data_dir / "camera_loras.json"
        raw = f.read_text("utf-8") if f.is_file() else ""
    try:
        return {k: v for k, v in (json.loads(raw) if raw else {}).items() if v}
    except ValueError:
        return {}


def chain_lora(wf: dict, lora: str, strength: float = 1.0, node_id: str = "8") -> dict:
    """在 H3 工作流的模型链末端再串一个 LoRA（运镜 LoRA），并把采样器改接到它。"""
    guider = wf.get("20")
    if not guider or not lora:
        return wf
    wf[node_id] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": guider["inputs"]["model"], "lora_name": lora, "strength_model": strength}}
    for nid in ("20", "23"):
        if nid in wf:
            wf[nid]["inputs"]["model"] = [node_id, 0]
    return wf


# ---------------- 视频延长：抽当前视频最后一帧 → 图生视频续写 → 拼接 ----------------
def extend_prompt(base: str, instruction: str) -> str:
    return (base + "\n本段是同一镜头的延续：开头画面就是上一段的最后一帧，人物、服装、场景、光线和机位完全一致，"
            "动作从那里自然继续，不要跳变、不要重新开始。" + (f"\n接下来：{instruction}" if instruction else ""))


async def concat_videos(parts: list[Path], out: Path, skip_first: float = 1 / 24) -> Path:
    """拼接同规格的视频片段；后续片段去掉第一帧（就是上一段的最后一帧，避免定格一下）。"""
    infos = [await media.probe(p) for p in parts]
    audio = all(i["has_audio"] for i in infos)
    args = ["ffmpeg", "-y", "-v", "error"]
    for n, p in enumerate(parts):
        args += (["-ss", f"{skip_first:.3f}"] if n else []) + ["-i", str(p)]
    streams = "".join(f"[{n}:v:0]" + (f"[{n}:a:0]" if audio else "") for n in range(len(parts)))
    fc = f"{streams}concat=n={len(parts)}:v=1:a={1 if audio else 0}[v]" + ("[a]" if audio else "")
    args += ["-filter_complex", fc, "-map", "[v]"] + (["-map", "[a]", "-c:a", "aac"] if audio else []) + \
            ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(out)]
    await media._run(*args)
    return out


@handler("video_extend")
async def task_video_extend(ctx: TaskContext) -> dict:
    from . import comfy, pipeline
    from .gpu import gpu
    from .workflows import VIDEO_SIZES, video_i2v_workflow
    shot = pipeline.need("shots", ctx.params.get("shot_id"), "镜头")
    if not pipeline.exists(shot.get("video")):
        raise ValueError("先生成本镜头的视频，再延长")
    project = pipeline.need("projects", shot["project_id"], "项目")
    src = pipeline.absolute(shot["video"])
    out_dir = pipeline.project_dir(project["id"], "videos")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    frame = await media.last_frame(src, out_dir / f"ext_{shot['id']}_{stamp}.png")
    seconds = float(ctx.params.get("seconds") or 4)
    base = pipeline.video_plan(shot, "i2v")["prompt"]
    prompt = extend_prompt(base, (ctx.params.get("instruction") or "").strip())
    quality = ctx.params.get("quality") if ctx.params.get("quality") in VIDEO_SIZES else "draft"
    async with gpu.use("comfyui", ctx.task.get("title") or "视频延长"):
        await gpu.comfy_switch("video")
        up = await comfy.upload_image(frame)
        wf = video_i2v_workflow(prompt, up, project["aspect_ratio"], seconds, quality, bool(ctx.params.get("turbo", True)))
        lora = camera_loras().get(shot.get("camera_move") or "")
        if lora:
            chain_lora(wf, lora)
        files = await comfy.run(wf, out_dir, pipeline.progress_slice(ctx, 5, 85, "续写视频"))
    part = next((f for f in files if f.suffix.lower() in pipeline.VIDEO_SUFFIX), None)
    if not part:
        raise RuntimeError("ComfyUI 没有产出视频文件")
    await ctx.progress(90, "拼接")
    merged = await concat_videos([src, part], out_dir / f"{src.stem}_ext{stamp}.mp4")
    total = (await media.probe(merged))["duration"]
    old = db.query("SELECT * FROM assets WHERE path=?", [shot["video"]])
    segs = ((old[0].get("meta") or {}).get("segments") if old else None) or [shot["video"]]
    a = pipeline.add_asset(project["id"], "video", merged, "shot", shot["id"],
                           {"prompt": prompt, "mode": "extend", "extended_from": shot["video"], "seconds": round(total, 2),
                            "segments": [*segs, pipeline.rel(part)], "quality": quality})
    db.update("shots", shot["id"], {"video": a["path"], "duration": round(total, 1)})
    return {"asset": a["id"], "seconds": round(total, 2)}


class ExtendIn(BaseModel):
    seconds: float = Field(4, ge=2, le=10)
    instruction: str = ""
    quality: str = "draft"
    turbo: bool = True


@router.post("/shots/{shot_id}/video/extend")
async def extend(shot_id: str, body: ExtendIn) -> dict:
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    return queue.submit("video_extend", project_id=shot["project_id"], title=f"视频延长 #{shot['sort'] + 1}",
                        owner_type="shot", owner_id=shot_id, params={"shot_id": shot_id, **body.model_dump()}, dedupe=False)


# ---------------- 动作参考：参考视频驱动人物动作（工作流可替换，如 Wan2.2-Animate / VACE） ----------------
def motion_template() -> dict | None:
    p = get_settings().motion_workflow.strip()
    path = Path(p) if p else get_settings().data_dir / "workflows" / "motion.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text("utf-8"))


def fill(template: Any, values: dict[str, Any]) -> Any:
    """递归替换占位符：整串等于 {{KEY}} 时保留原类型（数字），否则做字符串替换。"""
    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [fill(v, values) for v in template]
    if isinstance(template, str):
        for k, v in values.items():
            if template == "{{" + k + "}}":
                return v
        for k, v in values.items():
            template = template.replace("{{" + k + "}}", str(v))
    return template


def motion_available() -> bool:
    return motion_template() is not None


@handler("video_motion")
async def task_video_motion(ctx: TaskContext) -> dict:
    from . import comfy, pipeline
    from .gpu import gpu
    from .workflows import VIDEO_SIZES, seed_or_random
    tpl = motion_template()
    if tpl is None:
        raise RuntimeError("未配置动作参考工作流：把 ComfyUI API 格式的工作流放到 data/workflows/motion.json（见 docs/DEPLOY.md）")
    shot = pipeline.need("shots", ctx.params.get("shot_id"), "镜头")
    if not pipeline.exists(shot.get("motion_ref")) or not pipeline.exists(shot.get("keyframe")):
        raise ValueError("需要分镜图和动作参考视频")
    project = pipeline.need("projects", shot["project_id"], "项目")
    w, h = VIDEO_SIZES["final" if ctx.params.get("quality") == "final" else "draft"].get(project["aspect_ratio"], (864, 480))
    seconds = float(shot.get("duration") or 5)
    prompt = pipeline.video_plan(shot, "i2v")["prompt"] + "\n人物的动作、姿态和节奏与参考视频一致。"
    out_dir = pipeline.project_dir(project["id"], "videos")
    async with gpu.use("comfyui", ctx.task.get("title") or "动作参考视频"):
        await gpu.comfy_switch("video")
        img = await comfy.upload_image(pipeline.absolute(shot["keyframe"]))
        vid = await comfy.upload_image(pipeline.absolute(shot["motion_ref"]))
        wf = fill(tpl, {"PROMPT": prompt, "IMAGE": img, "VIDEO": vid, "WIDTH": w, "HEIGHT": h, "SECONDS": seconds,
                        "LENGTH": int(seconds * 16) + 1, "SEED": seed_or_random(None)})
        files = await comfy.run(wf, out_dir, pipeline.progress_slice(ctx, 5, 95, "动作参考生成"))
    v = next((f for f in files if f.suffix.lower() in pipeline.VIDEO_SUFFIX), None)
    if not v:
        raise RuntimeError("工作流没有产出视频文件")
    a = pipeline.add_asset(project["id"], "video", v, "shot", shot["id"],
                           {"prompt": prompt, "mode": "motion", "motion_ref": shot["motion_ref"]})
    db.update("shots", shot["id"], {"video": a["path"]})
    return {"asset": a["id"]}


@router.post("/shots/{shot_id}/motion")
async def upload_motion(shot_id: str, file: UploadFile = File(...)) -> dict:
    from .api import _save_upload
    from .pipeline import add_asset
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    target = await _save_upload(file, shot["project_id"], "uploads")
    a = add_asset(shot["project_id"], "motion", target, "shot", shot_id, {"uploaded": True})
    return db.update("shots", shot_id, {"motion_ref": a["path"]})


@router.post("/shots/{shot_id}/video/motion")
async def gen_motion(shot_id: str, body: dict[str, Any] | None = None) -> dict:
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    if not motion_available():
        raise HTTPException(400, "未配置动作参考工作流（data/workflows/motion.json）")
    return queue.submit("video_motion", project_id=shot["project_id"], title=f"动作参考视频 #{shot['sort'] + 1}",
                        owner_type="shot", owner_id=shot_id, params={"shot_id": shot_id, **(body or {})}, dedupe=False)
