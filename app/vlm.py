"""P2-6 视觉大模型审片：分镜图 / 视频抽帧交给视觉语言模型（Qwen3.6 + mmproj 或任意 OpenAI 兼容视觉接口）逐镜审查，
音画核对（可选 ASR），整片审片报告与一键返工。"""
from __future__ import annotations

import asyncio
import base64
import difflib
import io
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, media
from .config import get_settings
from .gpu import gpu
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")
ROOT = Path(__file__).resolve().parent.parent

CHECKS = {"count": "人数", "identity": "角色", "action": "动作表情", "anatomy": "肢体畸形", "artifact": "画面崩坏",
          "props": "道具", "text": "乱码文字", "flicker": "闪烁跳变", "camera": "运镜", "audio": "音画"}

REVIEW_SYSTEM = """你是严格的影视质检员（审片）。你会看到一个镜头的画面（单张分镜图，或视频按时间顺序抽出的若干帧拼成的网格，左上→右下），
以及这个镜头应有的内容。逐项检查，只报告你在画面里确实看到的问题，不要臆测；没问题的项不要写。
检查项（type）：
- count：画面里的主要人物数量是否与要求一致（多人/少人/同一人重复出现）
- identity：人物外貌、发型、服装是否与角色设定一致；性别、年龄明显不对
- action：动作、表情、视线是否符合描述
- anatomy：手指数量/形状异常、肢体扭曲、多手多脚、五官崩坏
- artifact：画面崩坏、严重模糊、物体穿模、不合理的结构、穿帮（现代物品出现在古装戏等）
- props：指定道具缺失或样子不对
- text：画面里出现了不该有的文字、乱码、水印、字幕
- flicker：（仅视频）帧与帧之间人物/背景突变、闪烁、人脸变成另一个人
- camera：（仅视频）运镜明显不符合要求
严重程度：high 只用于观众一眼就会出戏、必须返工的问题——人数不对、换了一个人（性别/年龄/发型/服装主色明显不同）、
明显的肢体畸形或五官崩坏、画面崩坏/严重模糊、出现字幕/水印/乱码、动作与描述完全相反；其余细节偏差（饰品、褶皱、妆容、皮肤状态、
光线、动作幅度不够）一律 low。被画框裁掉或被遮挡、本来就看不到的部位（如中景看不到鞋子）不算问题，不要报。
角色外貌只核对：性别、年龄段、发型发色、服装主色和款式；设定里的情绪/汗水/潮红等状态描写不作为外貌标准。
输出 JSON：{"score": 0~10 的整数（10=可直接用，6=勉强可用，<6 需要返工）, "people": 看到的主要人物数,
"issues": [{"type": 检查项, "severity": "high"|"low", "detail": "具体描述，指出在哪"}],
"fix": "如果需要返工：改进后的画面描述（中文，直接可用作出图/视频提示词的补充，写清应该是什么样），否则空字符串"}"""


def provider() -> str:
    s = get_settings()
    p = (s.vlm_provider or "auto").lower()
    if p == "auto":
        if s.vlm_base_url:
            return "api"
        if s.llm_provider == "local" and s.llm_mmproj and Path(s.llm_mmproj).is_file():
            return "llm"
        return "off"
    return p


def available() -> bool:
    p = provider()
    if p == "api":
        return bool(get_settings().vlm_base_url)
    if p == "llm":
        s = get_settings()
        return s.llm_provider != "local" or bool(s.llm_mmproj and Path(s.llm_mmproj).is_file())
    return False


def auto_enabled() -> bool:
    """出图后即时审：只在远程视觉接口时开启（本地要和 ComfyUI 抢显存，改为批量审片）。"""
    return get_settings().vlm_auto and provider() == "api" and available()


def _b64(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def load_image(path: Path, max_side: int = 1024):
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((max_side, max_side))
    return im.convert("RGB")


def contact_sheet(frames: list[Path], cols: int = 3, cell: int = 448):
    """把抽出的帧按时间顺序拼成网格（一张图里看时序，省 token），每格左上角标序号。"""
    from PIL import Image, ImageDraw
    ims = [load_image(f, cell) for f in frames]
    w = max(i.width for i in ims)
    h = max(i.height for i in ims)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), (0, 0, 0))
    d = ImageDraw.Draw(sheet)
    for n, im in enumerate(ims):
        x, y = (n % cols) * w, (n // cols) * h
        sheet.paste(im, (x, y))
        d.rectangle([x, y, x + 26, y + 20], fill=(0, 0, 0))
        d.text((x + 6, y + 4), str(n + 1), fill=(255, 255, 0))
    return sheet


async def sample_frames(video: Path, out_dir: Path, n: int = 6) -> list[Path]:
    dur = (await media.probe(video))["duration"] or 1.0
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(n):
        t = min(dur - 0.05, dur * (i + 0.5) / n)
        f = out_dir / f"f{i}.jpg"
        await media._run("ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                         "-frames:v", "1", "-vf", "scale=448:-2", "-q:v", "3", str(f))
        if f.is_file():
            frames.append(f)
    return frames


async def ask(images: list, text: str, system: str = REVIEW_SYSTEM, max_tokens: int = 1500) -> dict:
    """调用视觉模型（OpenAI 兼容 chat/completions，image_url 用 data URI），返回 JSON。"""
    from .llm import extract_json
    s = get_settings()
    p = provider()
    if p == "api":
        url, key, model = s.vlm_base_url, s.vlm_api_key, s.vlm_model or s.llm_model
    else:
        url, key, model = s.llm_base_url, s.llm_api_key, s.llm_model
    content = [{"type": "image_url", "image_url": {"url": _b64(im)}} for im in images] + [{"type": "text", "text": text}]
    body: dict[str, Any] = {"model": model, "temperature": 0.1, "max_tokens": max_tokens,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    if p == "llm" and s.llm_provider == "local":
        body["chat_template_kwargs"] = {"enable_thinking": False}

    async def call() -> str:
        async with httpx.AsyncClient(timeout=s.llm_timeout, trust_env=False) as c:
            r = await c.post(url.rstrip("/") + "/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
            if r.status_code >= 400:
                raise RuntimeError(f"视觉模型返回错误 {r.status_code}：{r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"] or ""
    if p == "llm":
        async with gpu.use("llm", "视觉模型审片"):
            raw = await call()
    else:
        raw = await call()
    return normalize(extract_json(raw))


def normalize(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("审片结果不是 JSON 对象")
    try:
        score = max(0, min(10, int(round(float(data.get("score", 5))))))
    except (TypeError, ValueError):
        score = 5
    issues = []
    for i in data.get("issues") or []:
        if isinstance(i, dict) and (i.get("detail") or "").strip():
            t = i.get("type") if i.get("type") in CHECKS else "artifact"
            issues.append({"type": t, "label": CHECKS[t], "severity": "low" if i.get("severity") == "low" else "high",
                           "detail": str(i["detail"]).strip()[:200]})
    return {"score": score, "people": data.get("people"), "issues": issues, "fix": str(data.get("fix") or "").strip()[:400]}


def shot_brief(shot: dict, target: str) -> str:
    from .pipeline import _shot_context, shot_props
    project, _, chars, loc = _shot_context(shot)
    lines = [f"这是{'视频按时间抽出的帧（网格）' if target == 'video' else '分镜图（视频首帧）'}。画风：{project.get('style') or ''}。",
             f"景别：{shot.get('shot_size')}；角度：{shot.get('camera_angle')}；运镜：{shot.get('camera_move')}。",
             "应有画面：" + (shot.get("image_prompt") or shot.get("description") or "")]
    if target == "video" and shot.get("action"):
        lines.append("镜头内动作：" + shot["action"])
    if shot.get("performance"):
        lines.append("表演：" + shot["performance"])
    if chars:
        lines.append(f"主要人物应为 {len(chars)} 人：" + "；".join(f"「{c['name']}」{c.get('appearance') or ''}" for c in chars))
    else:
        lines.append("这是空镜头，不应有主要人物。")
    if loc:
        lines.append(f"场景：{loc['name']}。{loc.get('description') or ''}")
    props = shot_props(shot)
    if props:
        lines.append("应出现的道具：" + "；".join(f"{p['name']}（{p.get('description') or ''}）" for p in props))
    wants_text = any(k in (shot.get("description") or "") for k in ("招牌", "写着", "文字", "字幕", "标语"))
    if not wants_text:
        lines.append("画面里不应出现任何文字。")
    return "\n".join(lines)


# ---------------- 音画核对（可选 ASR） ----------------
def asr_available() -> bool:
    py = get_settings().asr_python
    return bool(py) and Path(py).is_file()


async def transcribe(video: Path) -> str:
    s = get_settings()
    proc = await asyncio.create_subprocess_exec(s.asr_python, str(ROOT / "scripts" / "asr.py"), str(video), s.asr_model,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("语音识别失败：" + err.decode("utf-8", "replace")[-300:])
    return json.loads(out.decode("utf-8") or "{}").get("text", "")


def _norm(t: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:…—“”\"'（）()]", "", t or "")


def dialogue_match(shot: dict, heard: str) -> dict | None:
    want = _norm("".join(d.get("line") or "" for d in shot.get("dialogue") or [] if d.get("speaker") != "旁白"))
    got = _norm(heard)
    if not want and not got:
        return None
    ratio = difflib.SequenceMatcher(None, want, got).ratio() if want and got else 0.0
    if want and ratio < 0.6:
        return {"type": "audio", "label": CHECKS["audio"], "severity": "high",
                "detail": f"台词对不上（相似度 {ratio:.0%}）：识别到「{heard[:60]}」"}
    if not want and len(got) > 4:
        return {"type": "audio", "label": CHECKS["audio"], "severity": "low", "detail": f"无台词镜头里有人声：「{heard[:40]}」"}
    return None


# ---------------- 审一个镜头 ----------------
async def review_shot(shot: dict, target: str = "auto", work: Path | None = None) -> dict:
    from .pipeline import absolute, exists, project_dir
    if target == "auto":
        target = "video" if exists(shot.get("video")) else "keyframe"
    path = shot.get(target)
    if not exists(path):
        return {"target": target, "skipped": True}
    work = work or project_dir(shot["project_id"], "review") / shot["id"]
    if target == "video":
        frames = await sample_frames(absolute(path), work)
        images = [contact_sheet(frames)] if frames else []
    else:
        images = [load_image(absolute(path))]
    result = await ask(images, shot_brief(shot, target))
    if target == "video" and asr_available() and (await media.probe(absolute(path)))["has_audio"]:
        try:
            heard = await transcribe(absolute(path))
            result["heard"] = heard
            m = dialogue_match(shot, heard)
            if m:
                result["issues"].append(m)
                result["score"] = min(result["score"], 5)
        except Exception as exc:  # noqa: BLE001
            result["asr_error"] = str(exc)[:200]
    result.update({"target": target, "path": path, "at": time.time(),
                   "ok": result["score"] >= get_settings().vlm_pass_score and not any(
                       i["severity"] == "high" for i in result["issues"])})
    db.update("shots", shot["id"], {"review": result})
    return result


def as_qa(result: dict) -> dict:
    """转成 qa.check 的结果格式，便于和现有自动重抽逻辑合并。"""
    return {"ok": result.get("ok", True), "vlm_score": result.get("score"),
            "issues": [{"type": "vlm_" + i["type"], "detail": f"{i['label']}：{i['detail']}"} for i in result.get("issues") or []]}


def merge_qa(base: dict | None, extra: dict | None) -> dict | None:
    if not extra:
        return base
    if not base:
        return extra
    return {**base, "ok": bool(base.get("ok")) and bool(extra.get("ok")), "vlm_score": extra.get("vlm_score"),
            "issues": [*(base.get("issues") or []), *(extra.get("issues") or [])]}


async def quick_check(shot: dict, image: Path) -> dict | None:
    """出图/出视频后即时审（远程接口时）；失败不影响主流程。"""
    if not auto_enabled():
        return None
    try:
        target = "video" if image.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"} else "keyframe"
        if target == "video":
            frames = await sample_frames(image, image.parent / f"_rv_{image.stem}")
            images = [contact_sheet(frames)]
        else:
            images = [load_image(image)]
        r = await ask(images, shot_brief(shot, target))
        r["ok"] = r["score"] >= get_settings().vlm_pass_score and not any(i["severity"] == "high" for i in r["issues"])
        return as_qa(r)
    except Exception:  # noqa: BLE001
        return None


# ---------------- 整片审片 + 报告 + 一键返工 ----------------
@handler("film_review")
async def task_film_review(ctx: TaskContext) -> dict:
    from .pipeline import need, shots_of
    if not available():
        raise RuntimeError("未配置视觉模型：设置 LLM_MMPROJ（本地）或 VLM_BASE_URL（接口），见 docs/DEPLOY.md")
    project = need("projects", ctx.params.get("project_id") or ctx.project_id, "项目")
    shots = shots_of(project["id"])
    only = set(ctx.params.get("shot_ids") or [])
    todo = [s for s in shots if not only or s["id"] in only]
    rows = []
    for n, s in enumerate(todo):
        await ctx.progress(3 + 94 * n // max(1, len(todo)), f"审片 #{s['sort'] + 1}（{n + 1}/{len(todo)}）")
        try:
            r = await review_shot(s, ctx.params.get("target") or "auto")
        except Exception as exc:  # noqa: BLE001
            await ctx.log(f"#{s['sort'] + 1} 审片失败：{exc}")
            r = {"error": str(exc)[:200]}
        rows.append((s, r))
    scored = [(s, r) for s, r in rows if "score" in r]
    report = {"at": time.time(), "shots": len(rows), "reviewed": len(scored),
              "avg": round(sum(r["score"] for _, r in scored) / len(scored), 1) if scored else None,
              "bad": [s["sort"] + 1 for s, r in scored if not r["ok"]],
              "issues": {t: sum(1 for _, r in scored for i in r["issues"] if i["type"] == t) for t in CHECKS}}
    report["issues"] = {k: v for k, v in report["issues"].items() if v}
    db.update("projects", project["id"], {"review_report": report})
    return report


class ReviewIn(BaseModel):
    shot_ids: list[str] = Field(default_factory=list)
    target: str = "auto"


@router.post("/projects/{project_id}/review")
async def start_review(project_id: str, body: ReviewIn) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    if not available():
        raise HTTPException(400, "未配置视觉模型：设置 LLM_MMPROJ（本地）或 VLM_BASE_URL（接口）")
    return queue.submit("film_review", project_id=project_id, title="视觉审片", owner_type="project", owner_id=project_id,
                        params={"project_id": project_id, **body.model_dump()})


def rework_prompt(base: str, review: dict) -> str:
    probs = "；".join(f"{i['label']}：{i['detail']}" for i in review.get("issues") or [])
    extra = [f"上一版的问题（这次必须避免）：{probs}。"] if probs else []
    if review.get("fix"):
        extra.append("正确的画面应该是：" + review["fix"])
    return base + ("\n" + "\n".join(extra) if extra else "")


class ReworkIn(BaseModel):
    shot_ids: list[str] = Field(default_factory=list)     # 空 = 所有不合格镜头
    quality: str = "draft"


@router.post("/projects/{project_id}/review/rework")
async def rework(project_id: str, body: ReworkIn) -> dict:
    """不合格镜头一键返工：把审片指出的问题和修正描述写进提示词重抽（分镜图问题重出图，视频问题重出视频）。"""
    from .pipeline import keyframe_plan, shots_of, video_plan
    tasks = []
    for s in shots_of(project_id):
        rv = s.get("review") or {}
        if body.shot_ids and s["id"] not in body.shot_ids:
            continue
        if not body.shot_ids and (not rv or rv.get("ok", True)):
            continue
        if rv.get("target") == "video":
            prompt = rework_prompt(video_plan(s)["prompt"], rv)
            t = queue.submit("video", project_id=project_id, title=f"返工视频 #{s['sort'] + 1}", owner_type="shot",
                             owner_id=s["id"], params={"shot_id": s["id"], "prompt": prompt, "quality": body.quality,
                                                      "rework": True}, dedupe=False)
        else:
            prompt = rework_prompt(keyframe_plan(s)["prompt"], rv)
            t = queue.submit("keyframe", project_id=project_id, title=f"返工分镜图 #{s['sort'] + 1}", owner_type="shot",
                             owner_id=s["id"], params={"shot_id": s["id"], "prompt": prompt, "count": 2,
                                                      "auto_select": True, "rework": True}, dedupe=False)
        tasks.append(t["id"])
    return {"tasks": tasks}


@router.post("/shots/{shot_id}/review")
async def review_one(shot_id: str, body: ReviewIn | None = None) -> dict:
    s = db.get("shots", shot_id)
    if not s:
        raise HTTPException(404, "镜头不存在")
    if not available():
        raise HTTPException(400, "未配置视觉模型")
    return queue.submit("film_review", project_id=s["project_id"], title=f"审片 #{s['sort'] + 1}", owner_type="shot",
                        owner_id=shot_id, params={"project_id": s["project_id"], "shot_ids": [shot_id],
                                                  "target": (body.target if body else "auto")}, dedupe=False)
