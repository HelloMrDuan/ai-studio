"""P4-3 多比例导出：同一项目出 9:16 / 16:9 / 1:1 等不同画幅。
两种方式：
1. 智能重构图（成片合成时逐镜头处理，不需要显卡）——
   采样每个镜头的几帧，按「边缘能量 + 局部对比 + 帧间运动 + 构图站位/居中先验」找主体，
   主体集中就裁切（主体移动时平滑跟随摇镜），主体分散（如双人对话铺满全画面）就用模糊垫底完整保留画面；
   每个镜头都可以手动改方式和裁切位置。
2. 重新出图：复制成新画幅的项目（剧本、角色、分镜设定全部保留，分镜图/视频清空），按新画幅重新出图和视频。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, media

router = APIRouter(prefix="/api")

ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "3:4": 3 / 4, "4:3": 4 / 3, "2:3": 2 / 3, "3:2": 3 / 2, "21:9": 21 / 9}
MODES = {"smart": "智能（自动选裁切或垫底）", "crop": "裁切跟随主体", "pad": "完整画面 + 模糊垫底"}
SAMPLE_W = 96            # 分析用的缩略图长边
SAMPLES = 5              # 每个镜头采样帧数
PAD_BELOW = 0.5          # 智能模式：裁切窗口保留的「主体能量」低于这个比例 → 改用垫底
PAN_MIN = 0.12           # 主体中心移动超过画面这个比例才跟随摇镜，否则固定裁切
POS_PRIOR = {"左": 0.33, "左前": 0.3, "中": 0.5, "前景": 0.5, "背景": 0.5, "右": 0.67, "右前": 0.7}


def ratio(aspect: str) -> float:
    if aspect not in ASPECTS:
        raise ValueError(f"不支持的画幅：{aspect}")
    return ASPECTS[aspect]


# ---------------- 主体分析（纯 Python，缩略图很小） ----------------
async def sample_frames(clip: Path, width: int, height: int, duration: float, n: int = SAMPLES) -> tuple[int, int, list[list[int]]]:
    if width >= height:
        sw, sh = SAMPLE_W, max(8, round(SAMPLE_W * height / max(1, width) / 2) * 2)
    else:
        sw, sh = max(8, round(SAMPLE_W * width / max(1, height) / 2) * 2), SAMPLE_W
    fps = max(0.2, n / max(0.5, duration))
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", str(clip), "-vf", f"fps={fps:.4f},scale={sw}:{sh},format=gray",
        "-frames:v", str(n), "-f", "rawvideo", "pipe:1", stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 采样失败：{err.decode('utf-8', 'replace')[-500:]}")
    size = sw * sh
    frames = [list(out[i:i + size]) for i in range(0, len(out) - size + 1, size)]
    return sw, sh, frames


def saliency(frame: list[int], prev: list[int] | None, w: int, h: int) -> list[float]:
    """每个像素的显著度：梯度 + 与全图均值的差 + 与上一采样帧的差（运动）。"""
    mean = sum(frame) / max(1, len(frame))
    out = [0.0] * (w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            v = frame[i]
            gx = abs(frame[i + 1] - v) if x + 1 < w else 0
            gy = abs(frame[i + w] - v) if y + 1 < h else 0
            s = gx + gy + 0.35 * abs(v - mean)
            if prev is not None:
                s += 1.5 * abs(v - prev[i])
            out[i] = s
    return out


def profile(sal: list[float], w: int, h: int, horizontal: bool) -> list[float]:
    """沿裁切方向的能量分布（横向裁切 → 每列之和；纵向裁切 → 每行之和）。"""
    if horizontal:
        return [sum(sal[y * w + x] for y in range(h)) for x in range(w)]
    return [sum(sal[y * w:(y + 1) * w]) for y in range(h)]


def best_window(prof: list[float], win: int, prior: float = 0.5, prior_weight: float = 0.35) -> tuple[float, float]:
    """在能量分布上找长度 win 的最佳窗口；返回（窗口中心 0~1，窗口内能量占比）。先验：越靠近 prior 越加分。"""
    n = len(prof)
    total = sum(prof) or 1.0
    if win >= n:
        return 0.5, 1.0
    acc = [0.0]
    for v in prof:
        acc.append(acc[-1] + v)
    best, best_score, best_cov = 0, -1.0, 0.0
    for s in range(0, n - win + 1):
        cov = (acc[s + win] - acc[s]) / total
        c = (s + win / 2) / n
        score = cov * (1 - prior_weight * min(1.0, abs(c - prior) * 2) ** 2)
        if score > best_score:
            best, best_score, best_cov = s, score, cov
    return (best + win / 2) / n, best_cov


def window_frac(src_ratio: float, dst_ratio: float) -> tuple[bool, float]:
    """裁切方向和窗口占比：(横向裁切?, 窗口宽/高占原画面的比例)。"""
    if dst_ratio < src_ratio:                    # 目标更「瘦」：裁左右
        return True, dst_ratio / src_ratio
    return False, src_ratio / dst_ratio          # 目标更「扁」：裁上下


def prior_of(shot: dict, horizontal: bool) -> float:
    if not horizontal:
        return 0.42                               # 竖切横：人脸通常在画面偏上
    pos = [POS_PRIOR[v] for v in (shot.get("positions") or {}).values() if v in POS_PRIOR]
    return sum(pos) / len(pos) if pos else 0.5


async def analyze(clip: Path, shot: dict, dst_aspect: str, info: dict | None = None) -> dict:
    """单个镜头的重构图建议：{mode, horizontal, frac, centers:[(t, c)], center, coverage}。"""
    info = info or await media.probe(clip)
    w, h, dur = info.get("width") or 0, info.get("height") or 0, info.get("duration") or 0
    if not w or not h:
        raise RuntimeError(f"读不到视频尺寸：{clip.name}")
    horizontal, frac = window_frac(w / h, ratio(dst_aspect))
    base = {"horizontal": horizontal, "frac": round(frac, 4), "src": f"{w}x{h}", "duration": round(dur, 3)}
    if frac > 0.97:
        return {**base, "mode": "none", "center": 0.5, "centers": [], "coverage": 1.0}
    sw, sh, frames = await sample_frames(clip, w, h, dur)
    if not frames:
        return {**base, "mode": "pad", "center": 0.5, "centers": [], "coverage": 0.0}
    prior = prior_of(shot, horizontal)
    n_axis = sw if horizontal else sh
    win = max(1, round(n_axis * frac))
    centers, covs, total_prof = [], [], [0.0] * n_axis
    prev = None
    for k, f in enumerate(frames):
        prof = profile(saliency(f, prev, sw, sh), sw, sh, horizontal)
        prev = f
        c, cov = best_window(prof, win, prior)
        t = dur * (k + 0.5) / len(frames)
        centers.append((round(t, 3), round(c, 4)))
        covs.append(cov)
        total_prof = [a + b for a, b in zip(total_prof, prof)]
    c_all, cov_all = best_window(total_prof, win, prior)
    spread = max(c for _, c in centers) - min(c for _, c in centers)
    coverage = round(sum(covs) / len(covs) if spread >= PAN_MIN else cov_all, 4)     # 跟随摇镜时按逐帧覆盖率
    mode = "pad" if coverage < PAD_BELOW else "crop"
    return {**base, "mode": mode, "center": round(c_all, 4), "centers": centers if spread >= PAN_MIN else [],
            "coverage": coverage, "spread": round(spread, 4)}


# ---------------- ffmpeg 滤镜 ----------------
def _clamp_center(c: float, frac: float) -> float:
    half = frac / 2
    return min(1 - half, max(half, c))


def pan_expr(centers: list[tuple[float, float]], frac: float, span: int, win: int) -> str:
    """主体移动时的跟随摇镜：采样点之间线性插值（中心先平滑一下，避免抖动），返回 crop 的 x/y 表达式（像素）。"""
    pts = [(t, _clamp_center(c, frac)) for t, c in centers]
    if len(pts) >= 3:
        sm = [pts[0][1]] + [(pts[i - 1][1] + 2 * pts[i][1] + pts[i + 1][1]) / 4 for i in range(1, len(pts) - 1)] + [pts[-1][1]]
        pts = [(t, c) for (t, _), c in zip(pts, sm)]
    px = [(t, c * span - win / 2) for t, c in pts]
    expr = f"{px[-1][1]:.1f}"
    for i in range(len(px) - 2, -1, -1):
        (t0, x0), (t1, x1) = px[i], px[i + 1]
        seg = f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{max(0.001, t1 - t0):.3f}"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return f"if(lt(t,{px[0][0]:.3f}),{px[0][1]:.1f},{expr})"


def crop_filter(plan: dict, src_w: int, src_h: int) -> str:
    horizontal, frac = plan["horizontal"], plan["frac"]
    if horizontal:
        cw, ch = max(2, int(src_w * frac) // 2 * 2), src_h // 2 * 2
        span, win = src_w, cw
    else:
        cw, ch = src_w // 2 * 2, max(2, int(src_h * frac) // 2 * 2)
        span, win = src_h, ch
    if plan.get("centers") and plan.get("manual") is None:
        pos = f"'max(0,min({span - win},{pan_expr(plan['centers'], frac, span, win)}))'"
    else:
        c = float(plan.get("manual") if plan.get("manual") is not None else plan.get("center", 0.5))
        pos = str(max(0, min(span - win, int(round(c * span - win / 2)))))
    x, y = (pos, "0") if horizontal else ("0", pos)
    return f"crop={cw}:{ch}:{x}:{y}"


def pad_filter(width: int, height: int) -> str:
    blur = max(8, min(width, height) // 30)
    return (f"split[rfa][rfb];[rfa]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur={blur}:2,eq=brightness=-0.08[rfbg];[rfb]scale={width}:{height}:force_original_aspect_ratio=decrease[rffg];"
            f"[rfbg][rffg]overlay=(W-w)/2:(H-h)/2")


def decide(auto: dict, override: dict | None, default_mode: str) -> dict:
    """合并自动分析、项目默认方式、单镜头手动设置。"""
    o = override or {}
    plan = dict(auto)
    if plan["mode"] == "none":
        return plan
    mode = o.get("mode") or default_mode
    if mode in ("crop", "pad"):
        plan["mode"] = mode
    if o.get("center") is not None:
        plan["manual"] = max(0.0, min(1.0, float(o["center"])))
        plan["mode"] = "crop" if not o.get("mode") or o.get("mode") == "smart" else plan["mode"]
    return plan


def filter_for(plan: dict, width: int, height: int) -> str:
    w, h = (int(v) for v in plan["src"].split("x"))
    if plan["mode"] == "crop":
        return crop_filter(plan, w, h)
    if plan["mode"] == "pad":
        return pad_filter(width, height)
    return ""


def options(project: dict) -> dict:
    r = (project.get("post") or {}).get("reframe") or {}
    return {"mode": r.get("mode") if r.get("mode") in MODES else "smart", "shots": r.get("shots") or {}}


async def plan_shots(shots: list[dict], clips: list[Path], dst_aspect: str, opts: dict) -> list[dict]:
    out = []
    for s, c in zip(shots, clips):
        auto = await analyze(c, s, dst_aspect)
        out.append({**decide(auto, (opts.get("shots") or {}).get(s["id"]), opts.get("mode", "smart")),
                    "auto_mode": auto["mode"], "shot_id": s["id"]})
    return out


def summary(plans: list[dict]) -> dict:
    n = {"crop": 0, "pad": 0, "none": 0, "pan": 0}
    for p in plans:
        n[p["mode"]] = n.get(p["mode"], 0) + 1
        if p["mode"] == "crop" and p.get("centers") and p.get("manual") is None:
            n["pan"] += 1
    return n


# ---------------- 重新出图：复制成新画幅项目 ----------------
CLEAR_SHOT = {"keyframe": "", "video": "", "end_frame": "", "review": {}}
SKIP_PROJECT = {"id", "created_at", "final_video", "cover", "autopilot", "review_report", "series_id", "episode"}


def clone_project(src_id: str, aspect: str, title: str = "") -> dict:
    from .pipeline import shots_of
    from .series import _copy_media, copy_cast
    ratio(aspect)
    src = db.get("projects", src_id)
    if not src:
        raise HTTPException(404, "项目不存在")
    vals = {k: v for k, v in src.items() if k not in SKIP_PROJECT}
    vals.update({"title": title or f"{src['title']}（{aspect}）", "aspect_ratio": aspect})
    dst = db.insert("projects", vals)
    for k in ("bgm", "style_ref"):
        if src.get(k):
            db.update("projects", dst["id"], {k: _copy_media(src[k], src_id, dst["id"])})
    maps: dict[str, dict] = {}
    copied = copy_cast(src_id, dst["id"], maps)
    cm, lm, pm, om = (maps.get(k, {}) for k in ("characters", "locations", "props", "outfits"))
    n = 0
    for s in shots_of(src_id):
        row = {k: v for k, v in s.items() if k not in ("id", "project_id", "created_at")}
        row.update(CLEAR_SHOT)
        row.update({"project_id": dst["id"], "location_id": lm.get(s.get("location_id") or "", ""),
                    "character_ids": [cm[c] for c in s.get("character_ids") or [] if c in cm],
                    "prop_ids": [pm[p] for p in s.get("prop_ids") or [] if p in pm],
                    "outfits": {cm[c]: om.get(o, "") for c, o in (s.get("outfits") or {}).items() if c in cm},
                    "positions": {cm[c]: v for c, v in (s.get("positions") or {}).items() if c in cm}})
        for k in ("narration_audio", "pose_ref", "motion_ref"):
            row[k] = _copy_media(s.get(k) or "", src_id, dst["id"])
        db.insert("shots", row)
        n += 1
    return {**db.get("projects", dst["id"]), "copied": {**copied, "shots": n}}


# ---------------- 接口 ----------------
class PlanIn(BaseModel):
    aspect: str


class CloneIn(BaseModel):
    aspect: str
    title: str = ""


@router.post("/projects/{project_id}/reframe/plan")
async def reframe_plan(project_id: str, body: PlanIn) -> dict:
    from .pipeline import absolute, exists, shots_of
    project = db.get("projects", project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    try:
        ratio(body.aspect)
    except ValueError as e:
        raise HTTPException(400, str(e))
    shots = [s for s in shots_of(project_id) if exists(s.get("video"))]
    if not shots:
        raise HTTPException(400, "还没有任何镜头视频")
    plans = await plan_shots(shots, [absolute(s["video"]) for s in shots], body.aspect, options(project))
    return {"aspect": body.aspect, "source": project["aspect_ratio"], "shots": plans, "summary": summary(plans),
            "modes": MODES}


@router.post("/projects/{project_id}/clone")
async def clone(project_id: str, body: CloneIn) -> dict:
    try:
        return await asyncio.to_thread(clone_project, project_id, body.aspect, body.title)
    except ValueError as e:
        raise HTTPException(400, str(e))
