"""无限画布：剧本 / 角色 / 场景 / 道具 / 镜头 / 成片放在同一张画布上。

画布只保存「怎么摆」（节点位置、便签、分组、视口），内容本身就是项目数据，和流程视图完全同一份。
projects.canvas = {"nodes": {节点id: {"x", "y"}}, "viewport": {"x", "y", "z"}, "notes": [...], "groups": [...]}
节点 id：script / final / char:<id> / loc:<id> / prop:<id> / shot:<id> / note:<id>
"""
from __future__ import annotations

import math
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from . import db

router = APIRouter(prefix="/api")
MAX_NODES = 3000
MAX_NOTES = 300
MAX_GROUPS = 200
NOTE_COLORS = {"yellow", "blue", "green", "pink", "gray"}


def _num(v: Any, lo: float = -1e6, hi: float = 1e6, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return round(max(lo, min(hi, f)), 1)


def _pos(v: Any) -> dict | None:
    if not isinstance(v, dict):
        return None
    out = {"x": _num(v.get("x")), "y": _num(v.get("y"))}
    if v.get("w") is not None:
        out["w"] = _num(v.get("w"), 120, 1200, 240)
    return out


def clean_note(n: Any) -> dict | None:
    if not isinstance(n, dict):
        return None
    return {"id": str(n.get("id") or uuid.uuid4().hex[:10])[:24], **(_pos(n) or {"x": 0, "y": 0}),
            "w": _num(n.get("w"), 120, 1200, 220), "text": str(n.get("text") or "")[:4000],
            "color": n.get("color") if n.get("color") in NOTE_COLORS else "yellow",
            "image": str(n.get("image") or "")[:400]}


def clean_group(g: Any) -> dict | None:
    if not isinstance(g, dict):
        return None
    members = [str(m)[:64] for m in (g.get("members") or []) if isinstance(m, str)][:MAX_NODES]
    return {"id": str(g.get("id") or uuid.uuid4().hex[:10])[:24], "title": str(g.get("title") or "分组")[:80],
            "x": _num(g.get("x")), "y": _num(g.get("y")), "w": _num(g.get("w"), 80, 1e5, 400),
            "h": _num(g.get("h"), 60, 1e5, 300), "color": str(g.get("color") or "")[:16],
            "collapsed": bool(g.get("collapsed")), "auto": str(g.get("auto") or "")[:80], "members": members}


def merge(cur: dict, body: dict) -> dict:
    """nodes 按 key 合并（值为 null 删除），其余字段整体替换；replace_nodes=True 时整体替换 nodes。"""
    out = {**(cur or {})}
    if isinstance(body.get("nodes"), dict):
        nodes = {} if body.get("replace_nodes") else dict(out.get("nodes") or {})
        for k, v in body["nodes"].items():
            k = str(k)[:64]
            if v is None:
                nodes.pop(k, None)
            elif (p := _pos(v)) is not None:
                nodes[k] = p
        if len(nodes) > MAX_NODES:
            raise HTTPException(400, "画布节点太多")
        out["nodes"] = nodes
    if isinstance(body.get("viewport"), dict):
        v = body["viewport"]
        out["viewport"] = {"x": _num(v.get("x")), "y": _num(v.get("y")), "z": _num(v.get("z"), 0.05, 4, 1)}
    if isinstance(body.get("notes"), list):
        if len(body["notes"]) > MAX_NOTES:
            raise HTTPException(400, "便签太多")
        out["notes"] = [n for n in (clean_note(x) for x in body["notes"]) if n]
    if isinstance(body.get("groups"), list):
        if len(body["groups"]) > MAX_GROUPS:
            raise HTTPException(400, "分组太多")
        out["groups"] = [g for g in (clean_group(x) for x in body["groups"]) if g]
    if isinstance(body.get("prefs"), dict):
        out["prefs"] = {k: v for k, v in body["prefs"].items() if isinstance(v, (bool, int, float, str))
                        and len(str(k)) < 32}
    out["updated_at"] = time.time()
    return out


def get_canvas(project_id: str) -> dict:
    p = db.get("projects", project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p.get("canvas") or {}


@router.get("/projects/{project_id}/canvas")
async def read_canvas(project_id: str) -> dict:
    return get_canvas(project_id)


@router.put("/projects/{project_id}/canvas")
async def save_canvas(project_id: str, body: dict[str, Any]) -> dict:
    c = merge(get_canvas(project_id), body)
    db.update("projects", project_id, {"canvas": c})
    return c


@router.post("/projects/{project_id}/canvas/reset")
async def reset_layout(project_id: str) -> dict:
    """自动排列：清掉所有节点位置（便签、分组保留），前端按默认布局重排。"""
    c = {**get_canvas(project_id), "nodes": {}, "updated_at": time.time()}
    c["groups"] = [g for g in c.get("groups") or [] if not g.get("auto")]
    db.update("projects", project_id, {"canvas": c})
    return c
