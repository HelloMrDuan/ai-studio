"""P2-4 分镜增强：人物站位控制、姿势参考图、局部重绘、多版本对比所需接口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")

POSITIONS = {"左": "画面左侧三分之一处", "中": "画面正中", "右": "画面右侧三分之一处",
             "前景": "前景（离镜头最近，可被画框裁切）", "背景": "背景远处（人物较小）",
             "左前": "画面左侧前景", "右前": "画面右侧前景"}


def position_lines(shot: dict, chars: list[dict]) -> list[str]:
    pos = shot.get("positions") or {}
    parts = [f"「{c['name']}」位于{POSITIONS[pos[c['id']]]}" for c in chars if pos.get(c["id"]) in POSITIONS]
    return ["构图站位：" + "，".join(parts) + "。"] if parts else []


def pose_line(idx: int) -> str:
    return (f"<image{idx}> 是姿势参考：只参考其中人物的姿势、肢体动作、朝向和在画面中的位置，"
            f"不要参考它的长相、服装、背景和画风。")


# ---------------- 局部重绘 ----------------
def _snap16(v: float) -> int:
    return max(256, int(round(v / 16)) * 16)


def crop_box(w: int, h: int, box: list[float], pad: float = 0.25) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """box = [x, y, w, h]（0~1 比例）。返回 (要重绘的区域像素框, 带外扩边距的裁切框)。"""
    x, y, bw, bh = [min(1.0, max(0.0, float(v))) for v in box]
    bw, bh = max(bw, 0.02), max(bh, 0.02)
    inner = (int(x * w), int(y * h), int(min(1, x + bw) * w), int(min(1, y + bh) * h))
    px, py = int((inner[2] - inner[0]) * pad) + 16, int((inner[3] - inner[1]) * pad) + 16
    outer = (max(0, inner[0] - px), max(0, inner[1] - py), min(w, inner[2] + px), min(h, inner[3] + py))
    return inner, outer


def gen_size(cw: int, ch: int, target: int = 1024 * 1024) -> tuple[int, int]:
    """裁切区放大到约 1MP 再生成（小区域细节更好），保持比例、16 对齐。"""
    k = (target / max(1, cw * ch)) ** 0.5
    k = min(k, 4.0)
    return _snap16(cw * k), _snap16(ch * k)


def feather_paste(base_path: Path, patch_path: Path, inner: tuple, outer: tuple, out: Path, feather: int = 0) -> Path:
    """把重绘结果缩回裁切框大小，用羽化蒙版只替换框选区域（边缘渐变过渡）。"""
    from PIL import Image, ImageDraw, ImageFilter
    base = Image.open(base_path).convert("RGB")
    ow, oh = outer[2] - outer[0], outer[3] - outer[1]
    patch = Image.open(patch_path).convert("RGB").resize((ow, oh), Image.LANCZOS)
    f = feather or max(6, min(inner[2] - inner[0], inner[3] - inner[1]) // 8)
    mask = Image.new("L", (ow, oh), 0)
    ix0, iy0, ix1, iy1 = inner[0] - outer[0], inner[1] - outer[1], inner[2] - outer[0], inner[3] - outer[1]
    ImageDraw.Draw(mask).rectangle([ix0 - f // 2, iy0 - f // 2, ix1 + f // 2, iy1 + f // 2], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(f / 2))
    base.paste(patch, (outer[0], outer[1]), mask)
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out)
    return out


def region_prompt(instruction: str, char_lines: list[str]) -> str:
    lines = [f"<image1> 是一张画面的局部特写。保持构图、透视、光线方向、色调和画风完全不变，只做以下修改：{instruction}。",
             "修改后的内容要与周围画面自然衔接，边缘没有接缝。"]
    return "\n".join(lines + char_lines + ["不要出现任何文字、字幕或水印。"])


@handler("keyframe_region")
async def task_keyframe_region(ctx: TaskContext) -> dict:
    from PIL import Image

    from . import pipeline, prompts
    from .pipeline import (MAX_IMAGE_REFS, _generate_images, absolute, add_asset, char_refs, exists, keyframe_plan,
                           need, project_dir)
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    instruction = (ctx.params.get("instruction") or "").strip()
    if not exists(shot["keyframe"]) or not instruction:
        raise ValueError("需要先有分镜图，并填写修改说明")
    src = absolute(shot["keyframe"])
    with Image.open(src) as im:
        w, h = im.size
        inner, outer = crop_box(w, h, ctx.params.get("box") or [0.25, 0.25, 0.5, 0.5])
        out_dir = project_dir(shot["project_id"], "keyframes")
        crop = out_dir / f"_crop_{ctx.task['id']}.png"
        im.convert("RGB").crop(outer).save(crop)
    # 选中的角色（重绘脸/人物时）带上其参考图
    plan = keyframe_plan(shot)
    who = [c for c in plan["chars"] if c["id"] in (ctx.params.get("character_ids") or [])]
    char_paths, _, ref_map = char_refs(who, MAX_IMAGE_REFS - 1, start=2)
    lines = [ln for ln in prompts.char_ref_lines(who, ref_map) if ln.startswith("<image")]
    prompt = region_prompt(instruction, lines)
    size = gen_size(outer[2] - outer[0], outer[3] - outer[1])
    await ctx.log(f"局部重绘：区域 {inner}，裁切 {outer}，按 {size[0]}×{size[1]} 生成")
    files = await _generate_images(ctx, prompt, pipeline.shot_aspect(shot, plan["project"]), [crop, *char_paths],
                                   ctx.params.get("count", 1), out_dir, size=size)
    assets = []
    for n, f in enumerate(files):
        merged = feather_paste(src, f, inner, outer, out_dir / f"{Path(shot['keyframe']).stem}_r{ctx.task['id'][:6]}_{n}.png")
        assets.append(add_asset(shot["project_id"], "keyframe", merged, "shot", shot["id"],
                                {"prompt": prompt, "edit": instruction, "region": ctx.params.get("box"),
                                 "from": shot["keyframe"]}))
    crop.unlink(missing_ok=True)
    db.update("shots", shot["id"], {"keyframe": assets[0]["path"]})
    return {"assets": [a["id"] for a in assets]}


class RegionIn(BaseModel):
    instruction: str
    box: list[float] = Field(min_length=4, max_length=4)
    character_ids: list[str] = Field(default_factory=list)
    count: int = Field(1, ge=1, le=4)


@router.post("/shots/{shot_id}/keyframe/region")
async def region_repaint(shot_id: str, body: RegionIn) -> dict:
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    return queue.submit("keyframe_region", project_id=shot["project_id"], title=f"局部重绘 #{shot['sort'] + 1}",
                        owner_type="shot", owner_id=shot_id, params={"shot_id": shot_id, **body.model_dump()}, dedupe=False)


@router.post("/shots/{shot_id}/pose")
async def upload_pose(shot_id: str, file: UploadFile = File(...)) -> dict:
    from .api import _save_upload
    from .pipeline import add_asset
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    target = await _save_upload(file, shot["project_id"], "uploads")
    a = add_asset(shot["project_id"], "pose", target, "shot", shot_id, {"uploaded": True})
    return db.update("shots", shot_id, {"pose_ref": a["path"]})


@router.post("/shots/{shot_id}/pose/from_keyframe")
async def pose_from_candidate(shot_id: str, body: dict[str, Any]) -> dict:
    """把某张候选图（本镜或其他镜）设为姿势参考。"""
    a = db.get("assets", body.get("asset_id") or "")
    if not a:
        raise HTTPException(404, "素材不存在")
    return db.update("shots", shot_id, {"pose_ref": a["path"]})


@router.get("/shots/{shot_id}/compare")
async def compare(shot_id: str, kind: str = "keyframe") -> dict:
    """多版本对比：所有候选 + 质检结果 + 差异（提示词 / 改图说明）。"""
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    rows = db.query("SELECT * FROM assets WHERE owner_type='shot' AND owner_id=? AND kind=? ORDER BY created_at",
                    [shot_id, kind])
    cur = shot.get("video" if kind == "video" else "keyframe")
    out = []
    for i, a in enumerate(rows):
        m = a.get("meta") or {}
        q = m.get("qa") or {}
        out.append({"id": a["id"], "path": a["path"], "n": i + 1, "current": a["path"] == cur,
                    "qa_ok": q.get("ok") if q else None, "qa": [x.get("detail") for x in q.get("issues") or []],
                    "edit": m.get("edit") or "", "region": m.get("region"), "attempt": m.get("attempt"),
                    "mode": m.get("mode") or "", "created_at": a.get("created_at")})
    return {"kind": kind, "items": out}
