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


# ---------------- 存档点：整个项目的一键回退 ----------------
SNAP_TABLES = ("characters", "locations", "props", "outfits", "shots")
SNAP_SKIP_PROJECT = {"id", "created_at", "updated_at", "autopilot", "series_id", "episode"}
MAX_AUTO_SNAPSHOTS = 20
MAX_SNAPSHOTS = 100


def _summary(data: dict) -> dict:
    shots = [s for s in data.get("shots") or [] if not s.get("branch_of")]
    return {"shots": len(shots), "keyframes": sum(1 for s in shots if s.get("keyframe")),
            "videos": sum(1 for s in shots if s.get("video")), "characters": len(data.get("characters") or []),
            "final": bool((data.get("project") or {}).get("final_video")),
            "script_len": len((data.get("project") or {}).get("script") or "")}


def take_snapshot(pid: str, title: str = "", auto: bool = False) -> dict:
    p = db.get("projects", pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    data: dict[str, Any] = {"project": {k: v for k, v in p.items() if k not in SNAP_SKIP_PROJECT}}
    for t in SNAP_TABLES:
        data[t] = db.query(f"SELECT * FROM {t} WHERE project_id=?", [pid])
    data["assets"] = db.query("SELECT * FROM assets WHERE project_id=?", [pid])
    row = db.insert("snapshots", {"project_id": pid, "title": (title or time.strftime("存档 %m-%d %H:%M"))[:80],
                                  "auto": 1 if auto else 0, "data": data, "meta": _summary(data)})
    olds = db.query("SELECT id FROM snapshots WHERE project_id=? AND auto=? ORDER BY created_at DESC", [pid, 1 if auto else 0])
    for o in olds[MAX_AUTO_SNAPSHOTS if auto else MAX_SNAPSHOTS:]:
        db.execute("DELETE FROM snapshots WHERE id=?", [o["id"]])
    return {k: v for k, v in row.items() if k != "data"}


def restore_snapshot(sid: str) -> dict:
    snap = db.get("snapshots", sid)
    if not snap:
        raise HTTPException(404, "存档点不存在")
    pid = snap["project_id"]
    busy = db.query("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND status IN ('queued','running')", [pid])[0]["n"]
    if busy:
        raise HTTPException(409, f"项目还有 {busy} 个任务在排队或运行，先等它们完成或暂停后再回退")
    before = take_snapshot(pid, f"回退到「{snap['title']}」前自动存档", auto=True)
    data = snap["data"] or {}
    for t in SNAP_TABLES:
        want = {r["id"]: r for r in data.get(t) or []}
        have = {r["id"] for r in db.query(f"SELECT id FROM {t} WHERE project_id=?", [pid])}
        for rid in have - set(want):
            db.execute(f"DELETE FROM {t} WHERE id=?", [rid])
        for rid, r in want.items():
            if rid in have:
                db.update(t, rid, {k: v for k, v in r.items() if k != "id"})
            else:
                db.insert(t, r)
    have_assets = {r["id"] for r in db.query("SELECT id FROM assets WHERE project_id=?", [pid])}
    for a in data.get("assets") or []:              # 被删掉的候选记录补回来（文件有存档点保护，不会被清理）
        if a["id"] not in have_assets:
            db.insert("assets", a)
    proj = {k: v for k, v in (data.get("project") or {}).items() if k not in SNAP_SKIP_PROJECT}
    if proj:
        db.update("projects", pid, proj)
    return {"ok": True, "restored": snap["title"], "backup": before, "summary": snap.get("meta") or {}}


@router.get("/projects/{project_id}/snapshots")
async def list_snapshots(project_id: str) -> list[dict]:
    return db.query("SELECT id, project_id, title, auto, meta, created_at FROM snapshots WHERE project_id=? "
                    "ORDER BY created_at DESC", [project_id])


@router.post("/projects/{project_id}/snapshots")
async def create_snapshot(project_id: str, body: dict[str, Any]) -> dict:
    return take_snapshot(project_id, str(body.get("title") or ""), bool(body.get("auto")))


@router.post("/snapshots/{sid}/restore")
async def restore(sid: str) -> dict:
    return restore_snapshot(sid)


@router.patch("/snapshots/{sid}")
async def rename_snapshot(sid: str, body: dict[str, Any]) -> dict:
    if not db.get("snapshots", sid):
        raise HTTPException(404, "存档点不存在")
    db.execute("UPDATE snapshots SET title=?, auto=0 WHERE id=?", [str(body.get("title") or "存档")[:80], sid])
    return {"ok": True}


@router.delete("/snapshots/{sid}")
async def delete_snapshot(sid: str) -> dict:
    db.execute("DELETE FROM snapshots WHERE id=?", [sid])
    return {"ok": True}


# ---------------- 镜头分支：试另一版，满意了再采用 ----------------
BRANCH_SKIP = {"id", "project_id", "sort", "created_at", "branch_of"}


@router.post("/shots/{shot_id}/branch")
async def branch_shot(shot_id: str, body: dict[str, Any] | None = None) -> dict:
    shot = db.get("shots", shot_id)
    if not shot:
        raise HTTPException(404, "镜头不存在")
    if shot.get("branch_of"):
        shot_id, shot = shot["branch_of"], db.get("shots", shot["branch_of"]) or shot   # 分支的分支 = 原镜头的分支
    keep_media = (body or {}).get("keep_media", True)
    vals = {k: v for k, v in shot.items() if k not in BRANCH_SKIP}
    if not keep_media:
        vals.update({"keyframe": "", "video": "", "end_frame": "", "review": {}})
    return db.insert("shots", {**vals, "project_id": shot["project_id"], "sort": shot["sort"], "branch_of": shot_id})


@router.post("/shots/{shot_id}/adopt")
async def adopt_branch(shot_id: str) -> dict:
    """采用分支：内容写回原镜头（原镜头 id、顺序不变），分支的候选图/视频并入原镜头，原来的版本仍可在候选里切回。"""
    br = db.get("shots", shot_id)
    if not br or not br.get("branch_of"):
        raise HTTPException(400, "这不是分支镜头")
    orig = db.get("shots", br["branch_of"])
    if not orig:
        raise HTTPException(404, "原镜头已删除")
    take_snapshot(br["project_id"], f"采用镜头分支前自动存档", auto=True)
    db.update("shots", orig["id"], {k: v for k, v in br.items() if k not in BRANCH_SKIP})
    db.execute("UPDATE assets SET owner_id=? WHERE owner_type='shot' AND owner_id=?", [orig["id"], br["id"]])
    db.execute("DELETE FROM shots WHERE id=?", [br["id"]])
    return db.get("shots", orig["id"])
