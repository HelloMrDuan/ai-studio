"""P4-2 多集与系列：系列设定（世界观/人物圣经）、AI 分集规划、新建一集时继承上一集的角色/场景/道具/服装（连同定妆图、音色）、
前情摘要（写进本集剧本提示词，保证剧情连续）、批量一键成片。"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")
INHERIT = ("style", "style_note", "aspect_ratio", "genre", "target_seconds", "style_ref", "style_lora",
           "style_lora_strength", "grade", "post", "dub_mode", "writer")


def episodes(series_id: str) -> list[dict]:
    return db.query("SELECT * FROM projects WHERE series_id=? ORDER BY episode, created_at", [series_id])


def _series(sid: str) -> dict:
    s = db.get("series", sid)
    if not s:
        raise HTTPException(404, "系列不存在")
    return s


def series_text(project: dict) -> str:
    """写进本集剧本提示词：系列设定 + 前情摘要 + 上一集结尾。"""
    if not project.get("series_id"):
        return ""
    s = db.get("series", project["series_id"])
    if not s:
        return ""
    ep = int(project.get("episode") or 0)
    parts = [f"【系列】《{s['title']}》第 {ep} 集。剧情必须与前面各集连续：人物关系、称呼、性格、已发生的事件不能矛盾。"]
    if (s.get("bible") or "").strip():
        parts.append("【系列设定】\n" + s["bible"].strip()[:4000])
    plan = next((p for p in s.get("plan") or [] if int(p.get("episode") or 0) == ep), None)
    if plan and plan.get("hook"):
        parts.append(f"【本集结尾钩子】{plan['hook']}")
    if (project.get("recap") or "").strip():
        parts.append("【前情摘要】\n" + project["recap"].strip())
    else:
        prev = [e for e in episodes(s["id"]) if 0 < int(e.get("episode") or 0) < ep]
        if prev:
            parts.append("【前情（各集梗概）】\n" + "\n".join(f"第{e['episode']}集：{(e.get('synopsis') or '')[:200]}" for e in prev[-5:]))
    return "\n\n".join(parts)


# ---------------- 复制角色 / 场景 / 道具 / 服装（文件一并复制，删除旧集不影响新集） ----------------
def _copy_media(value: Any, src_pid: str, dst_pid: str) -> Any:
    from .pipeline import absolute, exists, project_dir, rel
    if isinstance(value, dict):
        return {k: _copy_media(v, src_pid, dst_pid) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_media(v, src_pid, dst_pid) for v in value]
    if isinstance(value, str) and value.startswith(src_pid + "/") and exists(value):
        dst = project_dir(dst_pid, "cast") / f"{Path(value).parent.name}_{Path(value).name}"
        if not dst.exists():
            shutil.copy2(absolute(value), dst)
        return rel(dst)
    return value


def copy_cast(src_pid: str, dst_pid: str) -> dict:
    from .pipeline import characters_of, locations_of, props_of
    skip = {"id", "project_id", "created_at"}
    cmap: dict[str, str] = {}
    n = {"characters": 0, "locations": 0, "props": 0, "outfits": 0}
    have = {c["name"] for c in characters_of(dst_pid)}
    for c in characters_of(src_pid):
        if c["name"] in have:
            continue
        row = db.insert("characters", {**{k: _copy_media(v, src_pid, dst_pid) for k, v in c.items() if k not in skip},
                                       "project_id": dst_pid})
        cmap[c["id"]] = row["id"]
        n["characters"] += 1
    for table, rows in (("locations", locations_of(src_pid)), ("props", props_of(src_pid))):
        names = {r["name"] for r in db.query(f"SELECT name FROM {table} WHERE project_id=?", [dst_pid])}
        for r in rows:
            if r["name"] not in names:
                db.insert(table, {**{k: _copy_media(v, src_pid, dst_pid) for k, v in r.items() if k not in skip},
                                  "project_id": dst_pid})
                n[table] += 1
    for o in db.query("SELECT * FROM outfits WHERE project_id=?", [src_pid]):
        if o["character_id"] in cmap:
            db.insert("outfits", {**{k: _copy_media(v, src_pid, dst_pid) for k, v in o.items() if k not in skip},
                                  "project_id": dst_pid, "character_id": cmap[o["character_id"]]})
            n["outfits"] += 1
    return n


def create_episode(series: dict, title: str = "", source_text: str = "", copy_from: str = "", episode: int = 0,
                   source_type: str = "idea") -> dict:
    eps = episodes(series["id"])
    num = episode or (max([int(e.get("episode") or 0) for e in eps] or [0]) + 1)
    base = db.get("projects", copy_from) if copy_from else (eps[-1] if eps else None)
    values = {k: base[k] for k in INHERIT if base and k in base}
    if not base:
        values.update({k: series[k] for k in ("style", "aspect_ratio", "genre") if series.get(k)})
    values.update({"title": title or f"{series['title']} 第{num}集", "source_text": source_text, "source_type": source_type,
                   "series_id": series["id"], "episode": num})
    p = db.insert("projects", values)
    copied = copy_cast(base["id"], p["id"]) if base else {}
    return {**p, "copied": copied}


# ---------------- AI 分集规划 ----------------
PLAN_SYSTEM = """你是短剧总编剧。把用户给的长篇素材（小说/大纲）规划成若干集竖屏短剧。
要求：
- 每集一个完整的小起伏，结尾必须留钩子（悬念/反转），让观众想看下一集；
- 集与集之间剧情连续，前面埋的伏笔后面要回收；
- source 是这一集要改编的原文片段或详细梗概（200~800 字，保留关键对白和细节）；
- 同时整理一份系列设定 bible：世界观、主要人物（姓名、身份、性格、外貌要点、人物关系），控制在 600 字内；
- 只输出 JSON：{"bible": "...", "episodes": [{"episode": 1, "title": "集名", "synopsis": "一句话梗概", "source": "...", "hook": "结尾钩子"}]}"""


def _validate_plan(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("episodes"), list) or not data["episodes"]:
        raise ValueError('需要 {"bible": ..., "episodes": [...]}')
    eps = []
    for i, e in enumerate(data["episodes"]):
        if not isinstance(e, dict) or not str(e.get("source") or e.get("synopsis") or "").strip():
            raise ValueError(f"第 {i + 1} 集缺少 source")
        eps.append({"episode": i + 1, "title": str(e.get("title") or "").strip(), "synopsis": str(e.get("synopsis") or "").strip(),
                    "source": str(e.get("source") or e.get("synopsis")).strip(), "hook": str(e.get("hook") or "").strip()})
    return {"bible": str(data.get("bible") or "").strip(), "episodes": eps}


@handler("series_plan")
async def task_series_plan(ctx: TaskContext) -> dict:
    from . import llm
    s = db.get("series", ctx.params.get("series_id"))
    if not s:
        raise ValueError("系列不存在")
    src = (s.get("source_text") or "").strip()
    if not src:
        raise ValueError("请先在系列里填写小说/大纲素材")
    n = int(ctx.params.get("episodes") or 0)
    sec = int(ctx.params.get("seconds") or 60)
    user = (f"系列：《{s['title']}》\n集数：{n if n else '按内容自动决定（6~20 集）'}\n每集时长：约 {sec} 秒（每集 source 的篇幅要能拍成这么长）\n"
            + (f"已有设定（在此基础上补充，不要推翻）：\n{s['bible']}\n" if (s.get("bible") or "").strip() else "")
            + f"\n素材：\n{src[:30000]}")
    await ctx.progress(10, "规划分集…")
    data = await llm.chat_json(PLAN_SYSTEM, user, validate=_validate_plan, temperature=0.6, max_tokens=16000)
    values: dict = {"plan": data["episodes"]}
    if data["bible"] and not (s.get("bible") or "").strip():
        values["bible"] = data["bible"]
    db.update("series", s["id"], values)
    created = 0
    if ctx.params.get("create", True):
        have = {int(e.get("episode") or 0) for e in episodes(s["id"])}
        s = db.get("series", s["id"])
        for e in data["episodes"]:
            if e["episode"] not in have:
                p = create_episode(s, f"{s['title']} 第{e['episode']}集 {e['title']}".strip(), e["source"],
                                   episode=e["episode"], source_type="novel")
                db.update("projects", p["id"], {"synopsis": e["synopsis"], "target_seconds": sec})
                created += 1
    return {"episodes": len(data["episodes"]), "created": created}


# ---------------- 前情摘要 ----------------
RECAP_SYSTEM = """你是短剧剪辑师，为新的一集写「前情提要」。根据前面各集的剧情，写出观众必须记住的关键信息：
主要人物关系、核心矛盾、上一集结尾的悬念。120~200 字，按时间顺序，口语化，不剧透本集。
只输出 JSON：{"recap": "前情摘要", "last_hook": "上一集结尾的悬念（一句话）"}"""


@handler("recap")
async def task_recap(ctx: TaskContext) -> dict:
    from . import llm
    from .pipeline import need
    p = need("projects", ctx.project_id, "项目")
    if not p.get("series_id"):
        raise ValueError("这个项目不属于任何系列")
    ep = int(p.get("episode") or 0)
    prev = [e for e in episodes(p["series_id"]) if 0 < int(e.get("episode") or 0) < ep]
    if not prev:
        raise ValueError("这是第一集，没有前情")
    blocks = []
    for e in prev[-6:]:
        body = (e.get("script") or e.get("source_text") or "").strip()
        body = re.sub(r"\n{2,}", "\n", body)
        tail = body[-1200:] if e is prev[-1] else body[:600]
        blocks.append(f"第{e['episode']}集《{e['title']}》梗概：{e.get('synopsis') or ''}\n{'结尾部分' if e is prev[-1] else '开头'}：{tail}")
    if len(prev) > 6:
        blocks.insert(0, "更早各集：" + "；".join(f"第{e['episode']}集：{(e.get('synopsis') or '')[:60]}" for e in prev[:-6]))

    def ok(d: Any) -> dict:
        if not isinstance(d, dict) or not str(d.get("recap") or "").strip():
            raise ValueError("缺少 recap")
        return d
    await ctx.progress(10, "撰写前情摘要…")
    data = await llm.chat_json(RECAP_SYSTEM, "\n\n".join(blocks), validate=ok, temperature=0.5, max_tokens=2048)
    recap = str(data["recap"]).strip()
    if str(data.get("last_hook") or "").strip():
        recap += f"\n上集结尾：{str(data['last_hook']).strip()}"
    db.update("projects", p["id"], {"recap": recap})
    return {"chars": len(recap)}


# ---------------- 接口 ----------------
class SeriesIn(BaseModel):
    title: str = "未命名系列"
    synopsis: str = ""
    bible: str = ""
    source_text: str = ""
    style: str = ""
    aspect_ratio: str = "9:16"
    genre: str = ""


class EpisodeIn(BaseModel):
    title: str = ""
    source_text: str = ""
    source_type: str = "idea"
    copy_from: str = ""
    episode: int = 0


class PlanIn(BaseModel):
    episodes: int = 0
    seconds: int = 60
    create: bool = True


class BatchAP(BaseModel):
    episode_ids: list[str] = []
    options: dict = {}


class JoinIn(BaseModel):
    series_id: str = ""
    episode: int = 0
    copy_cast: bool = False


def _summary(p: dict) -> dict:
    from .pipeline import shots_of
    shots = shots_of(p["id"])
    ap = p.get("autopilot") or {}
    return {**{k: p.get(k) for k in ("id", "title", "episode", "synopsis", "final_video", "cover", "recap", "updated_at",
                                      "aspect_ratio", "style")},
            "has_script": bool((p.get("script") or "").strip()), "shots": len(shots),
            "videos": sum(1 for s in shots if s["video"]), "autopilot": {k: ap.get(k) for k in ("status", "stage", "message")}}


@router.get("/series")
async def list_series() -> list[dict]:
    out = []
    for s in db.query("SELECT * FROM series ORDER BY created_at DESC"):
        eps = episodes(s["id"])
        out.append({**s, "episodes": len(eps), "finished": sum(1 for e in eps if e.get("final_video")),
                    "cover": next((e["cover"] for e in eps if e.get("cover")), "")})
    return out


@router.post("/series")
async def create_series(body: SeriesIn) -> dict:
    return db.insert("series", {**body.model_dump(), "plan": []})


@router.get("/series/{sid}")
async def get_series(sid: str) -> dict:
    s = _series(sid)
    return {"series": s, "episodes": [_summary(e) for e in episodes(sid)]}


@router.patch("/series/{sid}")
async def update_series(sid: str, body: dict[str, Any]) -> dict:
    _series(sid)
    allowed = {"title", "synopsis", "bible", "source_text", "style", "aspect_ratio", "genre", "plan"}
    return db.update("series", sid, {k: v for k, v in body.items() if k in allowed})


@router.delete("/series/{sid}")
async def delete_series(sid: str) -> dict:
    _series(sid)
    db.execute("UPDATE projects SET series_id='', episode=0 WHERE series_id=?", [sid])   # 各集保留为独立项目
    db.execute("DELETE FROM series WHERE id=?", [sid])
    return {"ok": True}


@router.post("/series/{sid}/episodes")
async def add_episode(sid: str, body: EpisodeIn) -> dict:
    return create_episode(_series(sid), body.title, body.source_text, body.copy_from, body.episode, body.source_type)


@router.post("/series/{sid}/plan")
async def plan_series(sid: str, body: PlanIn) -> dict:
    _series(sid)
    return queue.submit("series_plan", title="AI 分集规划", owner_type="series", owner_id=sid,
                        params={"series_id": sid, **body.model_dump()})


@router.post("/series/{sid}/recap")
async def recap_all(sid: str, body: dict[str, Any] | None = None) -> dict:
    _series(sid)
    over = bool((body or {}).get("overwrite"))
    n = 0
    for e in episodes(sid):
        if int(e.get("episode") or 0) > 1 and (over or not (e.get("recap") or "").strip()):
            queue.submit("recap", project_id=e["id"], title=f"前情摘要 第{e['episode']}集", owner_type="project",
                         owner_id=f"{e['id']}:recap")
            n += 1
    return {"submitted": n}


@router.post("/projects/{project_id}/recap")
async def recap_one(project_id: str) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    return queue.submit("recap", project_id=project_id, title="前情摘要", owner_type="project", owner_id=f"{project_id}:recap")


@router.post("/series/{sid}/autopilot")
async def batch_autopilot(sid: str, body: BatchAP) -> dict:
    from . import autopilot
    _series(sid)
    ids = body.episode_ids or [e["id"] for e in episodes(sid) if not e.get("final_video")]
    started, errors = [], {}
    for pid in ids:
        p = db.get("projects", pid)
        if not p or p.get("series_id") != sid:
            continue
        try:
            autopilot.start(pid, body.options)
            started.append(pid)
        except ValueError as exc:
            errors[pid] = str(exc)
    return {"started": started, "errors": errors}


@router.post("/projects/{project_id}/series")
async def join_series(project_id: str, body: JoinIn) -> dict:
    p = db.get("projects", project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not body.series_id:
        return db.update("projects", project_id, {"series_id": "", "episode": 0})
    s = _series(body.series_id)
    eps = [e for e in episodes(s["id"]) if e["id"] != project_id]
    num = body.episode or (max([int(e.get("episode") or 0) for e in eps] or [0]) + 1)
    if body.copy_cast and eps:
        copy_cast(eps[-1]["id"], project_id)
    return db.update("projects", project_id, {"series_id": s["id"], "episode": num})
