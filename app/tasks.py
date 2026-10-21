"""持久化任务队列：所有生成任务排队、逐个执行（单 GPU），进度实时写入数据库。
按模型分组调度，减少显存里大模型的来回切换；同项目任务遵守依赖顺序。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import traceback
from typing import Any, Awaitable, Callable

from . import db
from .config import get_settings

log = logging.getLogger("studio.tasks")
Handler = Callable[["TaskContext"], Awaitable[dict | None]]
HANDLERS: dict[str, Handler] = {}
AFTER_HOOKS: list[Callable[[dict], None]] = []     # 任务结束（完成/失败/取消）后调用，例如一键成片推进下一步
ACTIVE = ("queued", "running", "cancelling", "paused")


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


# ---------- 调度规则 ----------
# 模型组：同组任务连续执行可避免显存里的大模型来回加载（出图 Qwen-Image ≈17GB ↔ 视频 H3 ≈50GB）。
GROUP = {
    "script": "llm", "extract": "llm", "storyboard": "llm",
    "portrait": "image", "face_ref": "image", "location_image": "image", "prop_image": "image",
    "keyframe": "image", "keyframe_edit": "image", "end_frame": "image",
    "video": "video", "voice_sample": "video",
    "dub": "voice", "face_fix": "facefusion", "facefusion": "facefusion",
    "assemble": "cpu", "animatic": "cpu",
}
# 依赖阶段：同一项目里，先提交的低阶段任务没做完，后提交的高阶段任务不会抢跑（如分镜图 → 视频 → 配音 → 合成）。
STAGE = {"portrait": 1, "face_ref": 1, "location_image": 1, "prop_image": 1,
         "keyframe": 2, "keyframe_edit": 2, "end_frame": 2, "video": 3, "voice_sample": 3,
         "animatic": 3, "dub": 4, "face_fix": 4, "facefusion": 4, "assemble": 5}
BARRIER = {"script", "extract", "storyboard"}      # 改动项目结构的文字任务：与同项目其它任务严格按提交顺序


def task_group(type_: str) -> str:
    g = GROUP.get(type_, "cpu")
    if g == "llm" and get_settings().llm_provider != "local":
        return "cpu"                                    # 云端文字模型不占本机显存
    return g


def is_batch(task: dict) -> bool:
    return bool((task.get("params") or {}).get("_batch"))


def blocked(task: dict, earlier: list[dict]) -> bool:
    """task 前面还有它依赖的任务没跑完。earlier 为更早提交、仍未结束的任务。"""
    for u in earlier:
        if task["owner_id"] and u["owner_id"] == task["owner_id"]:
            return True                                 # 同一对象（同一镜头/角色）严格按顺序
        after = (task.get("params") or {}).get("_after")
        if after and u["owner_id"] == after and u["type"] == task["type"]:
            return True                                 # 连续镜头：等上一镜的视频先做完
        if u["status"] == "paused" or not task["project_id"] or u["project_id"] != task["project_id"]:
            continue
        if u["type"] in BARRIER or task["type"] in BARRIER:
            return True
        if STAGE.get(u["type"], 0) < STAGE.get(task["type"], 0):
            return True
    return False


def pick_next(tasks: list[dict], current_group: str) -> dict | None:
    """从未结束任务（按提交时间排序）里挑下一个：
    手动触发优先于批量 → 与当前已加载模型同组优先 → 先提交先执行；并遵守依赖顺序。"""
    best, best_key = None, None
    for i, t in enumerate(tasks):
        if t["status"] != "queued" or blocked(t, tasks[:i]):
            continue
        g = task_group(t["type"])
        key = (1 if is_batch(t) else 0, 0 if g in {"cpu", current_group} else 1, i)
        if best_key is None or key < best_key:
            best, best_key = t, key
    return best


class TaskQueue:
    def __init__(self) -> None:
        self.wake = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.current: str | None = None

    def start(self) -> None:
        self.wake = asyncio.Event()      # 绑定当前事件循环（测试里会多次启动）
        self.recover_interrupted()
        self.wake.set()
        self.worker = asyncio.create_task(self._loop())

    @staticmethod
    def recover_interrupted() -> None:
        """服务重启时：上次正在跑的任务自动重新排队（每个任务最多 2 次，防止反复把服务搞崩）。"""
        for t in db.query("SELECT * FROM tasks WHERE status IN ('running','cancelling')"):
            n = int((t.get("params") or {}).get("_restarts") or 0)
            if t["status"] == "running" and n < 2 and get_settings().auto_recover:   # 服务重启：中断的任务自动重新排队
                db.update("tasks", t["id"], {"status": "queued", "progress": 0, "message": "服务重启，已自动重新排队",
                                             "params": {**(t.get("params") or {}), "_restarts": n + 1}})
            else:
                db.update("tasks", t["id"], {"status": "failed" if t["status"] == "running" else "cancelled",
                                             "message": "服务重启，任务中断，可点击重试" if t["status"] == "running" else "已取消"})

    def _notify(self) -> None:
        self.wake.set()

    def submit(self, type_: str, *, project_id: str = "", title: str = "", owner_type: str = "",
               owner_id: str = "", params: dict | None = None, dedupe: bool = True) -> dict:
        if type_ not in HANDLERS:
            raise ValueError(f"未知任务类型：{type_}")
        if dedupe and owner_id:
            same = db.query("SELECT * FROM tasks WHERE type=? AND owner_id=? AND status IN ('queued','running','paused')",
                            [type_, owner_id])
            if same:
                if same[0]["status"] == "paused":        # 再次点击 = 继续这个任务
                    same[0] = db.update("tasks", same[0]["id"], {"status": "queued", "message": "排队中"})
                    self._notify()
                return same[0]
        task = db.insert("tasks", {"type": type_, "project_id": project_id, "title": title or type_,
                                   "owner_type": owner_type, "owner_id": owner_id, "params": params or {},
                                   "status": "queued", "message": "排队中"})
        self._notify()
        return task

    def cancel(self, task_id: str) -> dict | None:
        t = db.get("tasks", task_id)
        if not t:
            return None
        if t["status"] in {"queued", "paused"}:
            t = db.update("tasks", task_id, {"status": "cancelled", "message": "已取消"})
            for hook in AFTER_HOOKS:
                try:
                    hook(t)
                except Exception:  # noqa: BLE001
                    log.error("任务后处理失败：\n%s", traceback.format_exc())
            return t
        if t["status"] == "running":
            return db.update("tasks", task_id, {"status": "cancelling", "message": "正在取消…"})
        return t

    def retry(self, task_id: str) -> dict | None:
        t = db.get("tasks", task_id)
        if not t or t["status"] in ACTIVE:
            return t
        return self.submit(t["type"], project_id=t["project_id"], title=t["title"], owner_type=t["owner_type"],
                           owner_id=t["owner_id"], params=t["params"])

    def pause(self, project_id: str = "", batch_only: bool = False) -> int:
        """暂停排队中的任务（正在运行的那个会跑完）。"""
        n = 0
        for t in self._where("queued", project_id):
            if batch_only and not is_batch(t):
                continue
            db.update("tasks", t["id"], {"status": "paused", "message": "已暂停"})
            n += 1
        return n

    def resume(self, project_id: str = "") -> int:
        rows = self._where("paused", project_id)
        for t in rows:
            db.update("tasks", t["id"], {"status": "queued", "message": "排队中"})
        self._notify()
        return len(rows)

    @staticmethod
    def _where(status: str, project_id: str) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE status=?", [status]
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        return db.query(sql, args)

    def next_task(self) -> dict | None:
        from .gpu import gpu
        rows = db.query("SELECT * FROM tasks WHERE status IN ('queued','running','cancelling','paused') "
                        "ORDER BY created_at, rowid")
        return pick_next(rows, gpu.current_group())

    async def _recover(self, ctx: TaskContext, task: dict, exc: Exception, attempt: int, notes: list[str]) -> bool:
        """能自动处理的错误：修复环境（释放显存 / 重启 ComfyUI / 重启文字模型）后返回 True 重试。"""
        from .recovery import MAX_AUTO_RETRY, degrade, diagnose, recover
        if not get_settings().auto_recover or attempt > MAX_AUTO_RETRY:
            return False
        d = diagnose(task["type"], str(exc))
        if not d.retry:
            return False
        log.warning("任务 %s 第 %d 次自动恢复（%s）：%s", task["title"], attempt, d.kind, str(exc)[:300])
        db.update("tasks", ctx.id, {"message": f"出错了，正在自动恢复（第 {attempt} 次）…"})
        msg = await recover(d, attempt)
        if d.kind == "oom" and attempt >= 2:
            ctx.params = degrade(task["type"], ctx.params)
            db.update("tasks", ctx.id, {"params": ctx.params})
        notes.append(f"{msg}（{d.kind}）")
        db.update("tasks", ctx.id, {"message": f"{msg}（第 {attempt} 次）", "progress": 1})
        return True

    async def _loop(self) -> None:
        while True:
            task = self.next_task()
            if not task:
                self.wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                continue
            task_id = task["id"]
            self.current = task_id
            db.update("tasks", task_id, {"status": "running", "progress": 1, "message": "开始执行"})
            ctx = TaskContext(db.get("tasks", task_id))
            t0 = time.monotonic()
            notes: list[str] = []
            try:
                attempt = 0
                while True:
                    try:
                        result = await HANDLERS[task["type"]](ctx)
                        break
                    except Cancelled:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        attempt += 1
                        if not await self._recover(ctx, task, exc, attempt, notes):
                            raise
                cost = round(time.monotonic() - t0, 1)
                result = {**(result or {}), "elapsed": cost}
                if notes:
                    result["recovered"] = notes
                db.update("tasks", task_id, {"status": "done", "progress": 100,
                                             "message": f"完成（用时 {_fmt(cost)}" + (f"，自动恢复 {len(notes)} 次）" if notes else "）"),
                                             "result": result})
                log.info("任务完成 %s [%s/%s] 用时 %.1fs", task["title"], task["type"], task_group(task["type"]), cost)
            except Cancelled:
                db.update("tasks", task_id, {"status": "cancelled", "message": "已取消"})
            except Exception as exc:  # noqa: BLE001
                from .recovery import diagnose, explain
                log.error("任务失败 %s: %s\n%s", task_id, exc, traceback.format_exc())
                d = diagnose(task["type"], str(exc))
                db.update("tasks", task_id, {"status": "failed", "message": explain(task["type"], str(exc)),
                                             "result": {"error": str(exc)[:2000], "error_kind": d.kind,
                                                        "recovered": notes}})
            finally:
                self.current = None
            for hook in AFTER_HOOKS:
                try:
                    hook(db.get("tasks", task_id) or task)
                except Exception:  # noqa: BLE001
                    log.error("任务后处理失败：\n%s", traceback.format_exc())


def _fmt(sec: float) -> str:
    return f"{sec:.0f} 秒" if sec < 90 else f"{sec / 60:.1f} 分钟"


queue = TaskQueue()
