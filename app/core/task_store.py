import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import TaskRecord, TaskStatus


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records: dict[str, TaskRecord] = {}
        self._load()

    def _load(self) -> None:
        for path in self.root.glob("*/task.json"):
            try:
                record = TaskRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if record.status in {
                    TaskStatus.queued,
                    TaskStatus.switching_gpu,
                    TaskStatus.running,
                }:
                    record.status = TaskStatus.failed
                    record.error = "平台重启导致任务中断"
                    record.message = "任务已中断"
                    record.updated_at = now_iso()
                self.records[record.task_id] = record
                self._persist(record)
            except Exception:
                continue

    def create(
        self,
        *,
        task_id: str,
        module: str,
        operation: str,
        title: str,
        params: dict[str, Any],
        input_files: list[str],
    ) -> TaskRecord:
        record = TaskRecord(
            task_id=task_id,
            module=module,
            operation=operation,
            title=title,
            params=params,
            input_files=input_files,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        with self.lock:
            self.records[task_id] = record
            self._persist(record)
        return record.model_copy(deep=True)

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        with self.lock:
            record = self.records[task_id]
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = now_iso()
            self._persist(record)
            return record.model_copy(deep=True)

    def add_log(self, task_id: str, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self.lock:
            record = self.records[task_id]
            record.logs.append(line)
            record.logs = record.logs[-300:]
            record.updated_at = now_iso()
            self._persist(record)

    def get(self, task_id: str) -> TaskRecord | None:
        with self.lock:
            record = self.records.get(task_id)
            return record.model_copy(deep=True) if record else None

    def list(self, limit: int = 100) -> list[TaskRecord]:
        with self.lock:
            values = [item.model_copy(deep=True) for item in self.records.values()]
        values.sort(key=lambda item: item.created_at, reverse=True)
        return values[:limit]

    def task_dir(self, task_id: str) -> Path:
        path = self.root / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist(self, record: TaskRecord) -> None:
        path = self.task_dir(record.task_id) / "task.json"
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
