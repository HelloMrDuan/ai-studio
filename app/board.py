"""P2-4 分镜增强：人物站位控制、姿势参考图、局部重绘、多版本对比所需接口。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")

REGION_MAX_DIFF = 40.0      # 局部重绘框外边距的平均像素差异上限

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


def crop_box(w: int, h: int, box: list[float], pad: float = 0.25,
             min_side: float = 0.4) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """box = [x, y, w, h]（0~1 比例）。返回 (要重绘的区域像素框, 带外扩边距的裁切框)。
    裁切框至少为画面短边的 min_side（实测：只框住一张脸时模型看不出这是画面局部，会画成一张白底人像）。"""
    x, y, bw, bh = [min(1.0, max(0.0, float(v))) for v in box]
    bw, bh = max(bw, 0.02), max(bh, 0.02)
    inner = (int(x * w), int(y * h), int(min(1, x + bw) * w), int(min(1, y + bh) * h))
    px, py = int((inner[2] - inner[0]) * pad) + 16, int((inner[3] - inner[1]) * pad) + 16
    side = int(min(w, h) * min_side)
    px = max(px, (side - (inner[2] - inner[0])) // 2)
    py = max(py, (side - (inner[3] - inner[1])) // 2)
    x0, y0, x1, y1 = inner[0] - px, inner[1] - py, inner[2] + px, inner[3] + py
    if x0 < 0:                                  # 贴边时往另一侧多扩，保持上下文大小
        x1, x0 = min(w, x1 - x0), 0
    if x1 > w:
        x0, x1 = max(0, x0 - (x1 - w)), w
    if y0 < 0:
        y1, y0 = min(h, y1 - y0), 0
    if y1 > h:
        y0, y1 = max(0, y0 - (y1 - h)), h
    return inner, (x0, y0, x1, y1)


def margin_diff(crop_path: Path, patch_path: Path, inner: tuple, outer: tuple) -> float:
    """重绘结果在「框外边距」上与原裁切图的平均差异（0~255）。差异大说明模型没在原图上改，而是重画了一张别的图。"""
    from PIL import Image, ImageChops, ImageStat
    crop = Image.open(crop_path).convert("L")
    patch = Image.open(patch_path).convert("L").resize(crop.size, Image.BILINEAR)
    diff = ImageChops.difference(crop, patch)
    ix0, iy0, ix1, iy1 = inner[0] - outer[0], inner[1] - outer[1], inner[2] - outer[0], inner[3] - outer[1]
    ring = diff.copy()
    ring.paste(0, (ix0, iy0, ix1, iy1))
    area = crop.size[0] * crop.size[1] - (ix1 - ix0) * (iy1 - iy0)
    return ImageStat.Stat(ring).sum[0] / max(1, area)


def gen_size(cw: int, ch: int, target: int = 1024 * 1024) -> tuple[int, int]:
    """裁切区放大到约 1MP 再生成（小区域细节更好），保持比例、16 对齐。"""
    k = (target / max(1, cw * ch)) ** 0.5
    k = min(k, 4.0)
    return _snap16(cw * k), _snap16(ch * k)


def change_mask(base, patch, inner: tuple, thr: int = 40, reach: float = 1.0, max_grow: float = 1.5):
    """框外但与框内改动相连的变化区域（如抬手后原来垂着的手臂要去掉）。
    只在框周围 reach 倍框尺寸内生长；生长面积超过框面积 max_grow 倍（整体偏移/重画）则不扩。返回 L 蒙版或 None。"""
    from collections import deque

    from PIL import Image, ImageChops, ImageFilter
    w, h = base.size
    k = 192 / max(w, h)
    sw, sh = max(1, int(w * k)), max(1, int(h * k))
    d = ImageChops.difference(base.convert("L"), patch.convert("L")).filter(ImageFilter.GaussianBlur(4))
    on = d.point(lambda v: 255 if v > thr else 0).resize((sw, sh), Image.BOX).point(lambda v: 255 if v > 100 else 0)
    px = on.load()
    ix0, iy0, ix1, iy1 = [int(v * k) for v in inner]
    rw, rh = max(ix1 - ix0, 1), max(iy1 - iy0, 1)
    ext = int(max(rw, rh) * reach)
    wx0, wy0, wx1, wy1 = max(0, ix0 - ext), max(0, iy0 - ext), min(sw, ix1 + ext), min(sh, iy1 + ext)
    seen = set()
    q = deque((x, y) for x in range(max(0, ix0), min(sw, ix1)) for y in range(max(0, iy0), min(sh, iy1)) if px[x, y])
    seen.update(q)
    grown = []
    while q:
        x, y = q.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if wx0 <= nx < wx1 and wy0 <= ny < wy1 and (nx, ny) not in seen and px[nx, ny]:
                seen.add((nx, ny))
                q.append((nx, ny))
                if not (ix0 <= nx < ix1 and iy0 <= ny < iy1):
                    grown.append((nx, ny))
    if not grown or len(grown) > rw * rh * max_grow:
        return None
    small = Image.new("L", (sw, sh), 0)
    sp = small.load()
    for x, y in grown:
        sp[x, y] = 255
    return small.resize((w, h), Image.BILINEAR).filter(ImageFilter.MaxFilter(9))


def feather_paste(base_path: Path, patch_path: Path, inner: tuple, outer: tuple, out: Path, feather: int = 0,
                  grow: bool = False) -> Path:
    """把重绘结果缩回裁切框大小，用羽化蒙版只替换框选区域（边缘渐变过渡）。
    grow=True（整张画面作为上下文时）：与框内改动相连的框外变化也一起贴回，避免原来的手臂/物体残留。"""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    base = Image.open(base_path).convert("RGB")
    ow, oh = outer[2] - outer[0], outer[3] - outer[1]
    patch = Image.open(patch_path).convert("RGB").resize((ow, oh), Image.LANCZOS)
    f = feather or max(6, min(inner[2] - inner[0], inner[3] - inner[1]) // 8)
    mask = Image.new("L", (ow, oh), 0)
    ix0, iy0, ix1, iy1 = inner[0] - outer[0], inner[1] - outer[1], inner[2] - outer[0], inner[3] - outer[1]
    ImageDraw.Draw(mask).rectangle([ix0 - f // 2, iy0 - f // 2, ix1 + f // 2, iy1 + f // 2], fill=255)
    if grow and outer == (0, 0, base.width, base.height):
        extra = change_mask(base, patch, inner)
        if extra is not None:
            mask = ImageChops.lighter(mask, extra)
    mask = mask.filter(ImageFilter.GaussianBlur(f / 2))
    base.paste(patch, (outer[0], outer[1]), mask)
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out)
    return out


def region_words(box: list[float]) -> str:
    x, y, bw, bh = [min(1.0, max(0.0, float(v))) for v in box]
    cx, cy = x + bw / 2, y + bh / 2
    h = "左侧" if cx < 0.36 else "右侧" if cx > 0.64 else "中间"
    v = "上部" if cy < 0.36 else "下部" if cy > 0.64 else "中部"
    return (f"画面{v}{h}（横向 {x * 100:.0f}%–{min(1, x + bw) * 100:.0f}%，"
            f"纵向 {y * 100:.0f}%–{min(1, y + bh) * 100:.0f}%）")


def region_prompt(instruction: str, char_lines: list[str], where: str = "") -> str:
    lines = [f"在 <image1> 上直接编辑：输出必须与 <image1> 取景、构图、人物数量位置和大小、透视、光线、色调和画风完全一致，"
             f"只修改{where or '指定区域'}：{instruction}。其余所有内容保持原样，不要新增人物或人脸。",
             "修改后的内容要与周围画面自然衔接，边缘没有接缝。不要换背景，不要生成新的构图。"]
    return "\n".join(lines + char_lines + ["不要出现任何文字、字幕或水印。"])


def face_ref_lines(chars: list[dict], start: int = 2) -> list[str]:
    return [f"<image{start + i}> 是角色「{c['name']}」的脸部参考：只参考五官长相和发型，不要把它的背景、构图、服装画进来。"
            for i, c in enumerate(chars)]


@handler("keyframe_region")
async def task_keyframe_region(ctx: TaskContext) -> dict:
    from PIL import Image

    from . import pipeline
    from .pipeline import MAX_IMAGE_REFS, _generate_images, absolute, add_asset, exists, keyframe_plan, need, project_dir
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    instruction = (ctx.params.get("instruction") or "").strip()
    if not exists(shot["keyframe"]) or not instruction:
        raise ValueError("需要先有分镜图，并填写修改说明")
    src = absolute(shot["keyframe"])
    with Image.open(src) as im:
        w, h = im.size
        box = ctx.params.get("box") or [0.25, 0.25, 0.5, 0.5]
        # 实测：只给局部裁切时模型看不出这是画面的一部分，会重画出白底人像、多出一张脸 → 整张画面作为上下文，只把框内贴回
        inner, _ = crop_box(w, h, box)
        outer = (0, 0, w, h)
        out_dir = project_dir(shot["project_id"], "keyframes")
        crop = out_dir / f"_crop_{ctx.task['id']}.png"
        im.convert("RGB").crop(outer).save(crop)
    # 选中的角色（重绘脸/人物时）带上其参考图
    plan = keyframe_plan(shot)
    who = [c for c in plan["chars"] if c["id"] in (ctx.params.get("character_ids") or [])]
    who = [c for c in who if exists(c.get("face_image")) or exists(c.get("ref_image"))][:MAX_IMAGE_REFS - 1]
    char_paths = [absolute(c["face_image"] if exists(c.get("face_image")) else c["ref_image"]) for c in who]
    prompt = region_prompt(instruction, face_ref_lines(who), region_words(box))
    size = gen_size(outer[2] - outer[0], outer[3] - outer[1])
    await ctx.log(f"局部重绘：区域 {inner}，裁切 {outer}，按 {size[0]}×{size[1]} 生成")
    aspect = pipeline.shot_aspect(shot, plan["project"])
    files = await _generate_images(ctx, prompt, aspect, [crop, *char_paths], ctx.params.get("count", 1), out_dir, size=size)
    checked = []
    for f in files:                       # 框外边距对不上原图 = 模型重画了别的构图 → 换种子再来一次
        d = margin_diff(crop, f, inner, outer)
        if d > REGION_MAX_DIFF:
            await ctx.log(f"重绘结果与原画面对不上（边缘差异 {d:.0f}），换种子重试")
            retry = await _generate_images(ctx, prompt, aspect, [crop, *char_paths], 1, out_dir, random_seed=True, size=size)
            d2 = margin_diff(crop, retry[0], inner, outer)
            f = retry[0] if d2 < d else f
        checked.append(f)
    files = checked
    assets = []
    for n, f in enumerate(files):
        merged = feather_paste(src, f, inner, outer, out_dir / f"{Path(shot['keyframe']).stem}_r{ctx.task['id'][:6]}_{n}.png",
                               grow=True)
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
