from __future__ import annotations

import json
import re
import statistics
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any

from fastapi import APIRouter, HTTPException


_PROGRESS_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar(
    "xiaoduan_stage_progress_scope", default=None
)

_STAGE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "01": {
        "title": "① 剧本 · 故事生产圣经",
        "baseline_seconds": 210,
        "steps": [
            ("准备生产上下文", 0, 8),
            ("故事事实解析", 8, 26),
            ("剧情节点", 26, 44),
            ("角色 / 地点 / 道具实体", 44, 62),
            ("因果与连续性", 62, 80),
            ("创作计划", 80, 92),
            ("资产登记与完成校验", 92, 100),
        ],
    },
    "02": {
        "title": "② 角色 · 角色资产",
        "baseline_seconds": 180,
        "steps": [
            ("准备角色生产上下文", 0, 8),
            ("角色事实定位", 8, 24),
            ("稳定身份设计", 24, 43),
            ("服装 / 配色 / 身份锚点", 43, 62),
            ("剧情形象版本", 62, 79),
            ("参考图生成要求", 79, 92),
            ("角色资产登记与校验", 92, 100),
        ],
    },
    "03": {
        "title": "③ 视觉 · 视觉资产",
        "baseline_seconds": 190,
        "steps": [
            ("准备视觉生产上下文", 0, 8),
            ("视觉基调与美术规则", 8, 24),
            ("地点空间结构", 24, 43),
            ("道具结构与材质", 43, 61),
            ("固定锚点与可变状态", 61, 78),
            ("参考图生成要求", 78, 92),
            ("视觉资产登记与校验", 92, 100),
        ],
    },
    "04": {
        "title": "④ 分镜 · 正式镜头合同",
        "baseline_seconds": 260,
        "steps": [
            ("准备分镜生产上下文", 0, 8),
            ("剧情节点映射", 8, 20),
            ("镜头拆解", 20, 36),
            ("角色 / 地点 / 道具版本绑定", 36, 52),
            ("构图 / 机位 / 运镜", 52, 68),
            ("连续状态与镜头继承", 68, 80),
            ("图片 / 视频生成要求", 80, 92),
            ("分镜合同校验与落库", 92, 100),
        ],
    },
}

_PHASE_RANGES: tuple[tuple[str, float, float], ...] = (
    ("skill_contract", 2.0, 6.0),
    ("native_plan", 5.0, 10.0),
    ("reference_", 7.0, 12.0),
    ("director_orchestrator_content", 12.0, 84.0),
    ("director_orchestrator_control", 84.0, 94.0),
    ("handoff", 94.0, 98.0),
)

_SAFE_PROJECT_ID = re.compile(r"[A-Za-z0-9._-]{3,128}")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _seconds_since(value: Any) -> float:
    parsed = _parse_time(value)
    if parsed is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _bucket(chars: int) -> str:
    if chars <= 4000:
        return "small"
    if chars <= 15000:
        return "medium"
    return "large"


def _phase_range(phase: str) -> tuple[float, float]:
    name = str(phase or "").strip()
    for marker, start, end in _PHASE_RANGES:
        if marker in name:
            return start, end
    return 0.0, 12.0


class StageProgressTracker:
    """Persistent, machine-calibrated progress for the four authoring stages.

    The tracker never claims token-level semantic certainty.  It records real
    execution boundaries and uses elapsed time plus local historical durations
    to estimate progress while a long model call is in flight.  User-facing
    substeps are the production deliverable order of each native Skill.
    """

    schema_version = "xiaoduan_stage_progress_v1"

    def __init__(self, settings: Any, director: Any) -> None:
        self.settings = settings
        self.director = director
        self.root = Path(settings.data_dir) / "v3" / "stage-progress"
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.root / "history.json"
        self._mutex = threading.RLock()
        self._original_message = getattr(director, "message")
        self._original_tracked_llm_chat = getattr(director, "_tracked_llm_chat")

    def _path(self, project_id: str) -> Path:
        value = str(project_id or "").strip()
        if not _SAFE_PROJECT_ID.fullmatch(value):
            raise ValueError("非法作品编号")
        return self.root / f"{value}.json"

    def _read(self, project_id: str) -> dict[str, Any] | None:
        path = self._path(project_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") != self.schema_version:
                return None
            return value
        except Exception:
            return None

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path(str(record["project_id"]))
        record["updated_at"] = _utcnow()
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)

    def _history(self) -> dict[str, Any]:
        if not self.history_path.is_file():
            return {"schema_version": "xiaoduan_stage_timing_history_v1", "samples": []}
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            if data.get("schema_version") != "xiaoduan_stage_timing_history_v1":
                raise ValueError("wrong schema")
            if not isinstance(data.get("samples"), list):
                data["samples"] = []
            return data
        except Exception:
            return {"schema_version": "xiaoduan_stage_timing_history_v1", "samples": []}

    def _write_history(self, data: dict[str, Any]) -> None:
        temp = self.history_path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.history_path)

    def _estimate(self, stage: str, input_chars: int) -> tuple[float, str, int]:
        definition = _STAGE_DEFINITIONS.get(stage) or {}
        baseline = float(definition.get("baseline_seconds") or 180)
        size_factor = 1.0 + min(1.0, max(0, input_chars - 1800) / 18000.0)
        fallback = baseline * size_factor
        data = self._history()
        bucket = _bucket(input_chars)
        matches = [
            float(row.get("duration_seconds") or 0)
            for row in data.get("samples") or []
            if str(row.get("stage") or "") == stage
            and str(row.get("size_bucket") or "") == bucket
            and 5 <= float(row.get("duration_seconds") or 0) <= 3600
        ][-20:]
        if len(matches) >= 2:
            return max(10.0, float(statistics.median(matches))), "history", len(matches)
        return max(10.0, fallback), "baseline", len(matches)

    def begin(self, project_id: str, stage: str, *, input_chars: int = 0) -> dict[str, Any]:
        if stage not in _STAGE_DEFINITIONS:
            return {}
        with self._mutex:
            existing = self._read(project_id)
            if (
                existing
                and existing.get("stage") == stage
                and existing.get("status") in {"running", "waiting"}
                and _seconds_since(existing.get("started_at")) < 7200
            ):
                record = dict(existing)
                record["status"] = "running"
                record["turn_count"] = int(record.get("turn_count") or 0) + 1
                record["input_chars"] = max(int(record.get("input_chars") or 0), int(input_chars or 0))
                record["current_phase"] = "preparing"
                record["phase_started_at"] = _utcnow()
                self._write(record)
                return record

            estimate, source, samples = self._estimate(stage, int(input_chars or 0))
            record = {
                "schema_version": self.schema_version,
                "project_id": project_id,
                "stage": stage,
                "status": "running",
                "started_at": _utcnow(),
                "phase_started_at": _utcnow(),
                "current_phase": "preparing",
                "observed_floor": 0.0,
                "turn_count": 1,
                "input_chars": int(input_chars or 0),
                "estimated_total_seconds": round(estimate, 3),
                "estimate_source": source,
                "history_samples": samples,
                "error": "",
                "completed_at": "",
            }
            self._write(record)
            return record

    def phase_start(self, project_id: str, stage: str, phase: str) -> None:
        if stage not in _STAGE_DEFINITIONS:
            return
        with self._mutex:
            record = self._read(project_id)
            if not record or record.get("stage") != stage:
                return
            start, _ = _phase_range(phase)
            record["status"] = "running"
            record["current_phase"] = str(phase or "")
            record["phase_started_at"] = _utcnow()
            record["observed_floor"] = max(float(record.get("observed_floor") or 0), start)
            self._write(record)

    def phase_end(self, project_id: str, stage: str, phase: str, *, failed: bool = False) -> None:
        if stage not in _STAGE_DEFINITIONS:
            return
        with self._mutex:
            record = self._read(project_id)
            if not record or record.get("stage") != stage:
                return
            start, end = _phase_range(phase)
            record["observed_floor"] = max(
                float(record.get("observed_floor") or 0),
                start if failed else end,
            )
            record["current_phase"] = str(phase or "")
            self._write(record)

    def waiting(self, project_id: str, stage: str) -> None:
        with self._mutex:
            record = self._read(project_id)
            if not record or record.get("stage") != stage:
                return
            record["status"] = "waiting"
            record["current_phase"] = "waiting_next_internal_step"
            self._write(record)

    def fail(self, project_id: str, stage: str, error: Exception) -> None:
        with self._mutex:
            record = self._read(project_id) or {
                "schema_version": self.schema_version,
                "project_id": project_id,
                "stage": stage,
                "started_at": _utcnow(),
                "input_chars": 0,
                "estimated_total_seconds": 0,
                "estimate_source": "baseline",
                "history_samples": 0,
            }
            record["status"] = "failed"
            record["error"] = f"{type(error).__name__}: {error}"[:1000]
            record["completed_at"] = _utcnow()
            self._write(record)

    def complete(self, project_id: str, stage: str) -> None:
        with self._mutex:
            record = self._read(project_id)
            if not record or record.get("stage") != stage:
                return
            record["status"] = "completed"
            record["observed_floor"] = 100.0
            record["current_phase"] = "completed"
            record["completed_at"] = _utcnow()
            self._write(record)
            duration = _seconds_since(record.get("started_at"))
            if duration < 5 or duration > 7200:
                return
            data = self._history()
            samples = list(data.get("samples") or [])
            samples.append(
                {
                    "stage": stage,
                    "size_bucket": _bucket(int(record.get("input_chars") or 0)),
                    "input_chars": int(record.get("input_chars") or 0),
                    "duration_seconds": round(duration, 3),
                    "completed_at": _utcnow(),
                }
            )
            data["samples"] = samples[-120:]
            self._write_history(data)

    def _overall_percent(self, record: dict[str, Any]) -> float:
        status = str(record.get("status") or "")
        if status == "completed":
            return 100.0
        floor = max(0.0, min(99.0, float(record.get("observed_floor") or 0)))
        if status != "running":
            return floor

        phase = str(record.get("current_phase") or "")
        phase_start, phase_end = _phase_range(phase)
        total_estimate = max(10.0, float(record.get("estimated_total_seconds") or 180))
        segment_share = max(0.04, (phase_end - phase_start) / 100.0)
        segment_estimate = max(8.0, total_estimate * segment_share)
        phase_elapsed = _seconds_since(record.get("phase_started_at"))
        local_ratio = min(0.94, phase_elapsed / segment_estimate)
        phase_value = phase_start + (phase_end - phase_start) * local_ratio

        elapsed = _seconds_since(record.get("started_at"))
        time_value = min(95.0, elapsed / total_estimate * 95.0)
        # The active phase is an actual execution boundary; wall-clock progress
        # may not run beyond that boundary until the backend enters the next one.
        bounded_time = min(time_value, max(phase_start, phase_end - 0.5))
        return round(max(floor, phase_value, bounded_time), 1)

    @staticmethod
    def _step_rows(stage: str, overall: float) -> list[dict[str, Any]]:
        definition = _STAGE_DEFINITIONS.get(stage) or {}
        result: list[dict[str, Any]] = []
        for name, start, end in definition.get("steps") or []:
            width = max(1.0, float(end) - float(start))
            local = max(0.0, min(100.0, (overall - float(start)) / width * 100.0))
            state = "completed" if local >= 100 else ("running" if local > 0 else "pending")
            result.append(
                {
                    "name": name,
                    "start_percent": start,
                    "end_percent": end,
                    "percent": round(local, 1),
                    "state": state,
                }
            )
        return result

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        current_stage = str(project.get("current_stage") or "").strip()
        record = self._read(project_id)
        if not record or str(record.get("stage") or "") != current_stage:
            definition = _STAGE_DEFINITIONS.get(current_stage)
            if not definition:
                return {
                    "project_id": project_id,
                    "stage": current_stage,
                    "supported": False,
                    "status": "idle",
                }
            return {
                "project_id": project_id,
                "stage": current_stage,
                "supported": True,
                "status": "idle",
                "title": definition["title"],
                "overall_percent": 0.0,
                "elapsed_seconds": 0,
                "estimated_remaining_seconds": None,
                "estimated_remaining_low_seconds": None,
                "estimated_remaining_high_seconds": None,
                "estimate_source": "baseline",
                "history_samples": 0,
                "steps": self._step_rows(current_stage, 0.0),
            }

        stage = str(record.get("stage") or "")
        definition = _STAGE_DEFINITIONS.get(stage) or {}
        overall = self._overall_percent(record)
        elapsed = _seconds_since(record.get("started_at"))
        estimate = max(0.0, float(record.get("estimated_total_seconds") or 0))
        remaining: float | None = None
        low: float | None = None
        high: float | None = None
        if record.get("status") == "running" and estimate > 0:
            remaining = max(0.0, estimate - elapsed)
            if str(record.get("estimate_source")) == "history":
                low = remaining * 0.75
                high = remaining * 1.35
            else:
                low = remaining * 0.65
                high = remaining * 1.75
        steps = self._step_rows(stage, overall)
        current_step = next((row for row in steps if row["state"] == "running"), None)
        if current_step is None and steps:
            current_step = next((row for row in steps if row["state"] == "pending"), steps[-1])

        return {
            "project_id": project_id,
            "stage": stage,
            "supported": True,
            "status": record.get("status"),
            "title": definition.get("title", stage),
            "overall_percent": overall,
            "elapsed_seconds": round(elapsed, 1),
            "estimated_total_seconds": round(estimate, 1),
            "estimated_remaining_seconds": round(remaining, 1) if remaining is not None else None,
            "estimated_remaining_low_seconds": round(low, 1) if low is not None else None,
            "estimated_remaining_high_seconds": round(high, 1) if high is not None else None,
            "estimate_source": record.get("estimate_source"),
            "history_samples": int(record.get("history_samples") or 0),
            "turn_count": int(record.get("turn_count") or 0),
            "current_phase": record.get("current_phase"),
            "current_step": current_step,
            "steps": steps,
            "error": str(record.get("error") or ""),
            "started_at": record.get("started_at"),
            "updated_at": record.get("updated_at"),
            "eta_is_estimate": True,
        }

    def install(self) -> None:
        if getattr(self.director, "_xiaoduan_stage_progress_installed", False):
            return
        tracker = self
        original_message = self._original_message
        original_tracked = self._original_tracked_llm_chat

        async def tracked_llm_chat(
            instance: Any,
            *,
            phase: str,
            messages: list[dict[str, str]],
            system_prompt: str,
            temperature: float,
            max_tokens: int,
        ) -> dict[str, Any]:
            scope = _PROGRESS_SCOPE.get()
            if scope:
                tracker.phase_start(scope[0], scope[1], phase)
            failed = False
            try:
                return await original_tracked(
                    phase=phase,
                    messages=messages,
                    system_prompt=system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception:
                failed = True
                raise
            finally:
                if scope:
                    tracker.phase_end(scope[0], scope[1], phase, failed=failed)

        async def message(
            instance: Any,
            project_id: str,
            user_text: str,
            *,
            native_control_action: str = "",
        ) -> dict[str, Any]:
            before = instance.get_project(project_id)
            stage = str(before.get("current_stage") or "").strip()
            if stage in _STAGE_DEFINITIONS:
                tracker.begin(project_id, stage, input_chars=len(str(user_text or "")))
            token = _PROGRESS_SCOPE.set((project_id, stage))
            try:
                result = await original_message(
                    project_id,
                    user_text,
                    native_control_action=native_control_action,
                )
            except Exception as exc:
                if stage in _STAGE_DEFINITIONS:
                    tracker.fail(project_id, stage, exc)
                raise
            finally:
                _PROGRESS_SCOPE.reset(token)

            if stage in _STAGE_DEFINITIONS:
                after = instance.get_project(project_id)
                completed = {str(x) for x in after.get("completed_stages") or []}
                moved = str(after.get("current_stage") or "").strip() != stage
                if stage in completed or moved:
                    tracker.complete(project_id, stage)
                else:
                    state = ((after.get("stage_state") or {}).get(stage) or {})
                    runtime = state.get("skill_runtime") if isinstance(state.get("skill_runtime"), dict) else {}
                    completion = runtime.get("completion") if isinstance(runtime.get("completion"), dict) else {}
                    if bool(state.get("stage_ready")) or bool(completion.get("ready")):
                        tracker.complete(project_id, stage)
                    else:
                        tracker.waiting(project_id, stage)
            return result

        self.director._tracked_llm_chat = MethodType(tracked_llm_chat, self.director)
        self.director.message = MethodType(message, self.director)
        self.director._xiaoduan_stage_progress_installed = True


def create_stage_progress_router(tracker: StageProgressTracker) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v3/studio/projects/{project_id}/stage-progress")
    async def stage_progress(project_id: str) -> dict[str, Any]:
        try:
            return tracker.snapshot(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取阶段进度失败：{type(exc).__name__}: {exc}") from exc

    return router


__all__ = ["StageProgressTracker", "create_stage_progress_router"]
