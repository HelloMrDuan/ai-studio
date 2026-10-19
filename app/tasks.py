"""持久化任务队列：所有生成任务排队、逐个执行（单 GPU），进度实时写入数据库。"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Awaitable, Callable

from . import db

log = logging.getLogger("studio.tasks")
Handler = Callable[["TaskContext"], Awaitable[dict | None]]
HANDLERS: dict[str, Handler] = {}
ACTIVE = ("queued", "running")


class Cancelled(Exception):
    pass


class TaskContext:
    def __init__(self, task: dict) -> None:
        self.task = task
        self.id = task["id"]
        self.params: dict[str, Any] = task.get("params") or {}
        self.project_id = task.get("project_id") or ""
        self._last = 0.0

    async def progress(self, value: int, message: str = "") -> None:
        now = time.monotonic()
        if now - self._last < 0.8 and value < 100:
            return
        self._last = now
        current = db.get("tasks", self.id)
        if current and current["status"] == "cancelling":
            raise Cancelled()
        db.update("tasks", self.id, {"progress": max(0, min(100, int(value))), "message": message[:300]})

    async def log(self, message: str) -> None:
        db.update("tasks", self.id, {"message": message[:300]})


def handler(name: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return deco


class TaskQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.current: str | None = None

    def start(self) -> None:
        for t in db.query("SELECT * FROM tasks WHERE status IN ('running','cancelling')"):
            db.update("tasks", t["id"], {"status": "failed", "message": "服务重启，任务中断，可点击重试"})
        for t in db.query("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at"):
            self.queue.put_nowait(t["id"])
        self.worker = asyncio.create_task(self._loop())

    def submit(self, type_: str, *, project_id: str = "", title: str = "", owner_type: str = "",
               owner_id: str = "", params: dict | None = None, dedupe: bool = True) -> dict:
        if type_ not in HANDLERS:
            raise ValueError(f"未知任务类型：{type_}")
        if dedupe and owner_id:
            same = db.query("SELECT * FROM tasks WHERE type=? AND owner_id=? AND status IN ('queued','running')",
                            [type_, owner_id])
            if same:
                return same[0]
        task = db.insert("tasks", {"type": type_, "project_id": project_id, "title": title or type_,
                                   "owner_type": owner_type, "owner_id": owner_id, "params": params or {},
                                   "status": "queued", "message": "排队中"})
        self.queue.put_nowait(task["id"])
        return task

    def cancel(self, task_id: str) -> dict | None:
        t = db.get("tasks", task_id)
        if not t:
            return None
        if t["status"] == "queued":
            return db.update("tasks", task_id, {"status": "cancelled", "message": "已取消"})
        if t["status"] == "running":
            return db.update("tasks", task_id, {"status": "cancelling", "message": "正在取消…"})
        return t

    def retry(self, task_id: str) -> dict | None:
        t = db.get("tasks", task_id)
        if not t or t["status"] in ACTIVE:
            return t
        return self.submit(t["type"], project_id=t["project_id"], title=t["title"], owner_type=t["owner_type"],
                           owner_id=t["owner_id"], params=t["params"])

    async def _loop(self) -> None:
        while True:
            task_id = await self.queue.get()
            task = db.get("tasks", task_id)
            if not task or task["status"] != "queued":
                continue
            self.current = task_id
            db.update("tasks", task_id, {"status": "running", "progress": 1, "message": "开始执行"})
            ctx = TaskContext(db.get("tasks", task_id))
            try:
                result = await HANDLERS[task["type"]](ctx)
                db.update("tasks", task_id, {"status": "done", "progress": 100, "message": "完成",
                                             "result": result or {}})
            except Cancelled:
                db.update("tasks", task_id, {"status": "cancelled", "message": "已取消"})
            except Exception as exc:  # noqa: BLE001
                log.error("任务失败 %s: %s\n%s", task_id, exc, traceback.format_exc())
                db.update("tasks", task_id, {"status": "failed", "message": str(exc)[:500]})
            finally:
                self.current = None


queue = TaskQueue()
