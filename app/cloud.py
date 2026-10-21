"""P4-4 云端混合：海螺（MiniMax Hailuo）/ 可灵（Kling）作为可选视频引擎。
- 按镜头选择：镜头「引擎」> 项目默认引擎；项目默认可选「混合」，批量出视频时按比例把一部分镜头分给云端；
- 云端任务走并行通道（不占本机显卡），每个引擎同时跑 cloud_concurrency 个；
- 远端任务号写进任务参数：服务重启 / 网络抖动重试时继续查询原任务，不会重复扣费；
- 云端视频没有声音，台词靠配音（IndexTTS2）补上；时长按镜头裁剪。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter

from . import db, media
from .config import get_settings
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")

ENGINES = {"local": "本地（显卡）", "hailuo": "海螺 MiniMax", "kling": "可灵 Kling"}
PROJECT_ENGINES = {**ENGINES, "mix": "混合（本地 + 云端并行）"}
POLL = 6.0                     # 查询间隔（秒）
NEGATIVE = "模糊, 变形, 多余的手指, 文字水印, 字幕, 画面闪烁, 低画质"


def configured() -> dict[str, bool]:
    s = get_settings()
    return {"hailuo": bool(s.minimax_api_key), "kling": bool(s.kling_access_key and s.kling_secret_key)}


def status() -> dict:
    s, c = get_settings(), configured()
    return {"engines": {k: {"label": v, "ready": k == "local" or c.get(k, False)} for k, v in ENGINES.items()},
            "models": {"hailuo": s.minimax_model, "kling": f"{s.kling_model} · {s.kling_mode}"},
            "concurrency": s.cloud_concurrency}


def _client() -> httpx.AsyncClient:          # 测试里替换成 MockTransport
    return httpx.AsyncClient(timeout=120)


def _image_b64(path: Path, max_side: int = 2048) -> str:
    """转成 JPEG（≤2048px，体积小，满足两家的大小限制）。"""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode()


def pick_duration(engine: str, seconds: float) -> int:
    s = get_settings()
    if engine == "hailuo":
        return 6 if seconds <= 6.5 or s.minimax_resolution.upper() == "1080P" else 10
    return 5 if seconds <= 5.5 else 10


# ---------------- 海螺 MiniMax ----------------
def _mm_check(data: dict, what: str) -> dict:
    br = data.get("base_resp") or {}
    if br.get("status_code") not in (0, None):
        code, msg = br.get("status_code"), br.get("status_msg") or ""
        hint = {1002: "触发限流，稍后重试", 1004: "API Key 无效", 1008: "账户余额不足", 1026: "提示词涉及敏感内容",
                1027: "生成内容涉及敏感内容"}.get(code, "")
        raise RuntimeError(f"海螺{what}失败（{code} {msg}）{hint}")
    return data


async def hailuo_submit(prompt: str, image: Path | None, end: Path | None, seconds: float, aspect: str) -> str:
    s = get_settings()
    if not image:
        raise ValueError("海螺需要分镜图做首帧，请先出分镜图（或把这个镜头改用可灵 / 本地）")
    body: dict[str, Any] = {"model": s.minimax_model, "prompt": prompt[:2000], "duration": pick_duration("hailuo", seconds),
                            "resolution": s.minimax_resolution, "prompt_optimizer": True,
                            "first_frame_image": "data:image/jpeg;base64," + _image_b64(image)}
    if end:
        body["last_frame_image"] = "data:image/jpeg;base64," + _image_b64(end)
    async with _client() as c:
        r = await c.post(f"{s.minimax_base_url.rstrip('/')}/v1/video_generation", json=body,
                         headers={"Authorization": f"Bearer {s.minimax_api_key}"})
    if r.status_code >= 400:
        raise RuntimeError(f"海螺提交失败：HTTP {r.status_code} {r.text[:300]}")
    data = _mm_check(r.json(), "提交")
    if not data.get("task_id"):
        raise RuntimeError(f"海螺没有返回任务号：{json.dumps(data, ensure_ascii=False)[:300]}")
    return str(data["task_id"])


async def hailuo_poll(task_id: str) -> tuple[str, str]:
    """返回 (状态 running/done/failed, 下载地址或错误信息)。"""
    s = get_settings()
    base, hdr = s.minimax_base_url.rstrip("/"), {"Authorization": f"Bearer {s.minimax_api_key}"}
    async with _client() as c:
        r = await c.get(f"{base}/v1/query/video_generation", params={"task_id": task_id}, headers=hdr)
        if r.status_code >= 400:
            raise RuntimeError(f"海螺查询失败：HTTP {r.status_code} {r.text[:300]}")
        data = _mm_check(r.json(), "查询")
        st = str(data.get("status") or "")
        if st == "Fail":
            return "failed", str(data.get("error_message") or (data.get("base_resp") or {}).get("status_msg") or "生成失败")
        if st != "Success":
            return "running", st
        r = await c.get(f"{base}/v1/files/retrieve", params={"file_id": data.get("file_id")}, headers=hdr)
        f = _mm_check(r.json(), "取文件").get("file") or {}
        if not f.get("download_url"):
            raise RuntimeError("海螺没有返回下载地址")
        return "done", f["download_url"]


# ---------------- 可灵 Kling ----------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def kling_token(ak: str, sk: str, now: float | None = None) -> str:
    """可灵开放平台鉴权：HS256 JWT（iss=AccessKey，30 分钟有效）。"""
    now = int(now or time.time())
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iss": ak, "exp": now + 1800, "nbf": now - 5}, separators=(",", ":")).encode())
    sig = hmac.new(sk.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def _kl_headers() -> dict:
    s = get_settings()
    return {"Authorization": f"Bearer {kling_token(s.kling_access_key, s.kling_secret_key)}"}


def _kl_check(r: httpx.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or data.get("code") not in (0, None):
        code, msg = data.get("code", r.status_code), data.get("message") or r.text[:300]
        hint = {1000: "鉴权失败，检查 AccessKey/SecretKey", 1102: "账户余额不足或资源包用完", 1302: "触发限流，稍后重试",
                1303: "并发超限，稍后重试", 1301: "内容涉及敏感信息"}.get(code, "")
        raise RuntimeError(f"可灵{what}失败（{code} {msg}）{hint}")
    return data.get("data") or {}


async def kling_submit(prompt: str, image: Path | None, end: Path | None, seconds: float, aspect: str) -> str:
    s = get_settings()
    body: dict[str, Any] = {"model_name": s.kling_model, "prompt": prompt[:2500], "negative_prompt": NEGATIVE,
                            "mode": s.kling_mode, "duration": str(pick_duration("kling", seconds)), "cfg_scale": 0.5}
    if image:
        kind = "image2video"
        body["image"] = _image_b64(image)
        if end:
            body["image_tail"] = _image_b64(end)
    else:
        kind = "text2video"
        body["aspect_ratio"] = aspect if aspect in ("16:9", "9:16", "1:1") else "16:9"
    async with _client() as c:
        r = await c.post(f"{s.kling_base_url.rstrip('/')}/v1/videos/{kind}", json=body, headers=_kl_headers())
    data = _kl_check(r, "提交")
    if not data.get("task_id"):
        raise RuntimeError("可灵没有返回任务号")
    return f"{kind}/{data['task_id']}"


async def kling_poll(task_id: str) -> tuple[str, str]:
    s = get_settings()
    async with _client() as c:
        r = await c.get(f"{s.kling_base_url.rstrip('/')}/v1/videos/{task_id}", headers=_kl_headers())
    data = _kl_check(r, "查询")
    st = data.get("task_status")
    if st == "failed":
        return "failed", str(data.get("task_status_msg") or "生成失败")
    if st != "succeed":
        return "running", str(st or "")
    vids = (data.get("task_result") or {}).get("videos") or []
    if not vids or not vids[0].get("url"):
        raise RuntimeError("可灵没有返回视频地址")
    return "done", vids[0]["url"]


SUBMIT = {"hailuo": hailuo_submit, "kling": kling_submit}
POLLERS = {"hailuo": hailuo_poll, "kling": kling_poll}


async def download(url: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    async with _client() as c:
        async with c.stream("GET", url) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"下载云端视频失败：HTTP {r.status_code}")
            with out.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    f.write(chunk)
    return out


# ---------------- 引擎分配 ----------------
def engine_for(shot: dict, project: dict, index: int = 0, total: int = 1) -> str:
    """镜头最终用哪个引擎：镜头设置 > 项目默认；「混合」按比例把靠后的镜头分给云端；云端没配置就回落本地。"""
    ok = configured()
    e = (shot.get("engine") or "").strip()
    if e in ("hailuo", "kling"):
        return e if ok.get(e) else "local"
    if e == "local":
        return "local"
    pe = project.get("video_engine") or "local"
    if pe in ("hailuo", "kling"):
        return pe if ok.get(pe) else "local"
    if pe == "mix":
        cloud = [k for k in ("hailuo", "kling") if ok.get(k)]
        share = float(project.get("cloud_share") if project.get("cloud_share") is not None else 0.5)
        if cloud and total and index >= round(total * (1 - share)):
            return cloud[index % len(cloud)]
    return "local"


def submit_video(shot: dict, project: dict, params: dict, engine: str = "", index: int = 0, total: int = 1,
                 batch: bool = False) -> dict:
    """统一的视频任务提交：本地走 GPU 队列，云端走并行通道。"""
    from .pipeline import video_after
    e = engine if engine in ENGINES else engine_for(shot, project, index, total)
    if e in ("hailuo", "kling") and not configured().get(e):
        raise ValueError(f"{ENGINES[e]} 还没有配置 API Key（.env）")
    p = {"shot_id": shot["id"], **params, **video_after(shot)}
    if batch:
        p["_batch"] = p.get("_batch") or True
    if e == "local":
        return queue.submit("video", project_id=project["id"], title=f"镜头视频 #{shot['sort'] + 1}", owner_type="shot",
                            owner_id=shot["id"], params=p)
    return queue.submit("cloud_video", project_id=project["id"], title=f"{ENGINES[e]} #{shot['sort'] + 1}",
                        owner_type="shot", owner_id=shot["id"], params={**p, "engine": e})


# ---------------- 任务 ----------------
@handler("cloud_video")
async def task_cloud_video(ctx: TaskContext) -> dict:
    from .pipeline import (_qa_faces, _shot_context, absolute, add_asset, chain_first_frame, chain_source,
                           dub_lines, exists, need, project_dir, rel, video_plan)
    from . import qa, vlm, voice
    engine = ctx.params.get("engine")
    if engine not in SUBMIT:
        raise ValueError(f"未知云端引擎：{engine}")
    if not configured().get(engine):
        raise ValueError(f"{ENGINES[engine]} 还没有配置 API Key（.env）")
    shot = need("shots", ctx.params.get("shot_id"), "镜头")
    project = need("projects", shot["project_id"], "项目")
    plan = video_plan(shot, "i2v")
    seconds = float(ctx.params.get("seconds") or shot["duration"] or 5)
    prompt = (ctx.params.get("prompt") or "").strip() or plan["prompt"]
    image = absolute(shot["keyframe"]) if exists(shot.get("keyframe")) else None
    notes: list[str] = []
    src = chain_source(shot) if ctx.params.get("chain", True) else None
    remote = ctx.params.get("_remote") or ""
    if src and not remote:
        image, notes = await chain_first_frame(ctx, shot, src)
        if not (ctx.params.get("prompt") or "").strip():
            prompt = video_plan({**shot, "_chain": True, "_end_frame": exists(shot.get("end_frame"))}, "i2v")["prompt"]
    end = absolute(shot["end_frame"]) if exists(shot.get("end_frame")) else None
    if end and not src and not (ctx.params.get("prompt") or "").strip():
        prompt = video_plan({**shot, "_end_frame": True}, "i2v")["prompt"]
    if not remote:
        await ctx.progress(3, f"提交到{ENGINES[engine]}…")
        remote = await SUBMIT[engine](prompt, image, end, seconds, project["aspect_ratio"])
        ctx.params = {**ctx.params, "_remote": remote}
        db.update("tasks", ctx.id, {"params": ctx.params})          # 重启 / 重试时接着查，不重复扣费
    else:
        notes.append("接着查询之前提交的云端任务")
    t0, timeout = time.monotonic(), max(60, int(get_settings().cloud_timeout))
    url, n = "", 0
    while True:
        st, info = await POLLERS[engine](remote)
        if st == "done":
            url = info
            break
        if st == "failed":
            ctx.params = {k: v for k, v in ctx.params.items() if k != "_remote"}
            db.update("tasks", ctx.id, {"params": ctx.params})
            raise RuntimeError(f"{ENGINES[engine]}生成失败：{info}")
        el = time.monotonic() - t0
        if el > timeout:
            raise RuntimeError(f"{ENGINES[engine]}超过 {timeout // 60} 分钟还没出结果（远端任务 {remote}），可稍后点重试继续查询")
        n += 1
        await ctx.progress(min(90, 5 + int(el / 3)), f"{ENGINES[engine]}生成中（{info or '排队'}，已等 {int(el)} 秒）")
        await asyncio.sleep(POLL)
    out_dir = project_dir(project["id"], "videos")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    raw = await download(url, out_dir / f"cloud_{engine}_{shot['id']}_{stamp}_raw.mp4")
    try:
        info = await media.probe(raw)
    except RuntimeError:
        info = {"width": 0}
    if not info.get("width"):
        raw.unlink(missing_ok=True)
        raise RuntimeError("云端返回的不是有效的视频文件")
    final = raw
    if info["duration"] > seconds + 0.3:                 # 云端只能出 5/6/10 秒，按镜头时长裁剪
        final = out_dir / f"cloud_{engine}_{shot['id']}_{stamp}.mp4"
        await media._run("ffmpeg", "-y", "-v", "error", "-i", str(raw), "-t", f"{seconds:.3f}", "-c:v", "libx264",
                         "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(final))
        raw.unlink(missing_ok=True)
        notes.append(f"云端出片 {info['duration']:.1f} 秒，已裁到 {seconds:.1f} 秒")
    else:
        final = raw.rename(raw.with_name(raw.name.replace("_raw", "")))
    await ctx.progress(94, "自动质检中…")
    _, _, chars, _ = _shot_context(shot)
    result = await qa.check(final, reference=image, faces=_qa_faces(chars), check_text=True,
                            style=project.get("style") or "")
    result = vlm.merge_qa(result, await vlm.quick_check(shot, final))
    model = get_settings().minimax_model if engine == "hailuo" else f"{get_settings().kling_model}/{get_settings().kling_mode}"
    asset = add_asset(project["id"], "video", final, "shot", shot["id"],
                      {"prompt": prompt, "mode": "i2v" if image else "t2v", "engine": engine, "model": model,
                       "remote_id": remote, "seconds": seconds, "qa": result, "silent": True,
                       "chain_from": src["path"] if src else "", "end_frame": rel(end) if end else "", "notes": notes})
    db.update("shots", shot["id"], {"video": asset["path"]})
    out = {"asset": asset["id"], "engine": engine, "remote_id": remote, "qa": qa.summary(result), "notes": notes}
    fresh = db.get("shots", shot["id"]) or shot
    if project.get("dub_mode") and voice.tts_available() and dub_lines(fresh)[0]:
        t = queue.submit("dub", project_id=project["id"], title=f"配音 #{shot['sort'] + 1}", owner_type="shot",
                         owner_id=shot["id"], params={"shot_id": shot["id"], "lipsync": "auto"})
        out["dub_task"] = t["id"]
    return out


# ---------------- 接口 ----------------
@router.get("/cloud/status")
async def cloud_status() -> dict:
    return status()
