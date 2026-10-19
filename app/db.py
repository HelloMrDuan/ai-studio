"""SQLite 存储：项目、角色、场景、分镜、素材、任务。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, source_type TEXT, source_text TEXT,
  aspect_ratio TEXT DEFAULT '16:9', style TEXT DEFAULT 'cinematic', style_note TEXT DEFAULT '',
  script TEXT DEFAULT '', synopsis TEXT DEFAULT '', target_seconds INTEGER DEFAULT 60,
  final_video TEXT DEFAULT '', created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS characters (
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT, role TEXT DEFAULT '', gender TEXT DEFAULT '',
  age TEXT DEFAULT '', appearance TEXT DEFAULT '', personality TEXT DEFAULT '', voice TEXT DEFAULT '',
  ref_image TEXT DEFAULT '', locked INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS locations (
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT, description TEXT DEFAULT '',
  ref_image TEXT DEFAULT '', sort INTEGER DEFAULT 0, created_at REAL
);
CREATE TABLE IF NOT EXISTS shots (
  id TEXT PRIMARY KEY, project_id TEXT, sort INTEGER DEFAULT 0, scene TEXT DEFAULT '',
  location_id TEXT DEFAULT '', shot_size TEXT DEFAULT '中景', camera_angle TEXT DEFAULT '平视',
  camera_move TEXT DEFAULT '固定', description TEXT DEFAULT '', action TEXT DEFAULT '',
  character_ids TEXT DEFAULT '[]', dialogue TEXT DEFAULT '[]', sound TEXT DEFAULT '',
  duration REAL DEFAULT 5, image_prompt TEXT DEFAULT '', video_prompt TEXT DEFAULT '',
  keyframe TEXT DEFAULT '', video TEXT DEFAULT '', created_at REAL
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, path TEXT, owner_type TEXT DEFAULT '',
  owner_id TEXT DEFAULT '', meta TEXT DEFAULT '{}', created_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, project_id TEXT DEFAULT '', type TEXT, title TEXT DEFAULT '',
  owner_type TEXT DEFAULT '', owner_id TEXT DEFAULT '', status TEXT DEFAULT 'queued',
  progress INTEGER DEFAULT 0, message TEXT DEFAULT '', params TEXT DEFAULT '{}',
  result TEXT DEFAULT '{}', created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_shots_project ON shots(project_id, sort);
CREATE INDEX IF NOT EXISTS idx_assets_owner ON assets(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
"""

JSON_FIELDS = {"character_ids", "dialogue", "meta", "params", "result"}
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            path = get_settings().db_path
            path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def reset_for_tests() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key in JSON_FIELDS & item.keys():
        try:
            item[key] = json.loads(item[key] or ("[]" if key in {"character_ids", "dialogue"} else "{}"))
        except json.JSONDecodeError:
            item[key] = [] if key in {"character_ids", "dialogue"} else {}
    return item


def _encode(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        out[key] = json.dumps(value, ensure_ascii=False) if key in JSON_FIELDS else value
    return out


def insert(table: str, values: dict[str, Any]) -> dict[str, Any]:
    values = dict(values)
    values.setdefault("id", new_id())
    values.setdefault("created_at", time.time())
    if table in {"projects", "tasks"}:
        values.setdefault("updated_at", time.time())
    data = _encode(values)
    cols = ",".join(data)
    marks = ",".join("?" for _ in data)
    with _lock:
        conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values()))
    return get(table, values["id"])


def update(table: str, row_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    if not values:
        return get(table, row_id)
    values = dict(values)
    if table in {"projects", "tasks"}:
        values["updated_at"] = time.time()
    data = _encode(values)
    sets = ",".join(f"{k}=?" for k in data)
    with _lock:
        conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", [*data.values(), row_id])
    return get(table, row_id)


def get(table: str, row_id: str) -> dict[str, Any] | None:
    with _lock:
        row = conn().execute(f"SELECT * FROM {table} WHERE id=?", [row_id]).fetchone()
    return _decode(row)


def query(sql: str, params: list[Any] | tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        rows = conn().execute(sql, list(params)).fetchall()
    return [_decode(r) for r in rows]


def execute(sql: str, params: list[Any] | tuple = ()) -> None:
    with _lock:
        conn().execute(sql, list(params))


def delete_project(project_id: str) -> None:
    with _lock:
        for table in ("characters", "locations", "shots", "assets", "tasks"):
            conn().execute(f"DELETE FROM {table} WHERE project_id=?", [project_id])
        conn().execute("DELETE FROM projects WHERE id=?", [project_id])
