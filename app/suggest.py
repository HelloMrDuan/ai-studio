"""修改建议：台词打磨、导演审校等产生的「逐条可接受 / 拒绝」的修改。

一条建议 = 对某行数据某个字段的修改（old_value → new_value）+ 理由。接受时写回数据库；
如果当前值已被用户改过（≠ old_value），照样写入但在结果里提示。
"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

TABLES = {"shots", "projects", "characters", "locations", "props"}
router = APIRouter(prefix="/api")


def add(project_id: str, source: str, target_type: str, target_id: str, field: str, old: Any, new: Any,
        reason: str, batch: str = "", label: str = "") -> dict | None:
    if target_type not in TABLES or json.dumps(old, ensure_ascii=False) == json.dumps(new, ensure_ascii=False):
        return None
    # 同一目标同一字段的旧建议作废（只保留最新一条）
    db.execute("UPDATE suggestions SET status='superseded' WHERE project_id=? AND target_id=? AND field=? "
               "AND source=? AND status='pending'", [project_id, target_id, field, source])
    return db.insert("suggestions", {"project_id": project_id, "source": source, "target_type": target_type,
                                     "target_id": target_id, "field": field, "old_value": old, "new_value": new,
                                     "reason": reason[:500], "batch": batch, "label": label, "status": "pending"})


def pending(project_id: str, source: str = "") -> list[dict]:
    sql, args = "SELECT * FROM suggestions WHERE project_id=? AND status='pending'", [project_id]
    if source:
        sql += " AND source=?"
        args.append(source)
    return db.query(sql + " ORDER BY created_at, rowid", args)


def accept(sid: str) -> dict:
    s = db.get("suggestions", sid)
    if not s:
        raise ValueError("建议不存在")
    if s["status"] != "pending":
        return {"suggestion": s, "changed": False}
    row = db.get(s["target_type"], s["target_id"])
    if not row:
        db.update("suggestions", sid, {"status": "stale"})
        raise ValueError("要修改的内容已被删除")
    conflict = json.dumps(row.get(s["field"]), ensure_ascii=False) != json.dumps(s["old_value"], ensure_ascii=False)
    db.update(s["target_type"], s["target_id"], {s["field"]: s["new_value"]})
    s = db.update("suggestions", sid, {"status": "accepted", "decided_at": time.time()})
    return {"suggestion": s, "changed": True, "conflict": conflict}


def reject(sid: str) -> dict:
    s = db.get("suggestions", sid)
    if not s:
        raise ValueError("建议不存在")
    return db.update("suggestions", sid, {"status": "rejected", "decided_at": time.time()})


@router.get("/projects/{project_id}/suggestions")
async def list_suggestions(project_id: str, source: str = "", status: str = "pending") -> list[dict]:
    if status == "pending":
        return pending(project_id, source)
    sql, args = "SELECT * FROM suggestions WHERE project_id=?", [project_id]
    if source:
        sql += " AND source=?"
        args.append(source)
    return db.query(sql + " ORDER BY created_at DESC LIMIT 300", args)


@router.post("/suggestions/{sid}/accept")
async def accept_one(sid: str) -> dict:
    try:
        return accept(sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/suggestions/{sid}/reject")
async def reject_one(sid: str) -> dict:
    try:
        return reject(sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class BulkIn(BaseModel):
    source: str = ""
    action: str = "accept"     # accept | reject
    ids: list[str] | None = None


@router.post("/projects/{project_id}/suggestions/bulk")
async def bulk(project_id: str, body: BulkIn) -> dict:
    rows = pending(project_id, body.source)
    if body.ids is not None:
        rows = [r for r in rows if r["id"] in set(body.ids)]
    n = 0
    for r in rows:
        try:
            accept(r["id"]) if body.action == "accept" else reject(r["id"])
            n += 1
        except ValueError:
            continue
    return {"done": n}
