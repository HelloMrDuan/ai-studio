from __future__ import annotations

import statistics
from datetime import datetime, timezone
from types import MethodType
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: Any) -> datetime | None:
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


def _seconds_between(start: Any, end: Any | None = None) -> float:
    first = _parse(start)
    second = _parse(end) if end else datetime.now(timezone.utc)
    if first is None or second is None:
        return 0.0
    return max(0.0, (second - first).total_seconds())


class AuthoringExecutionTimingFix:
    """Track active authoring execution instead of wall-clock waiting time.

    The original progress tracker measured ``now - started_at``.  If a result
    waited for user action/local reconciliation, that idle time incorrectly
    inflated the displayed duration and poisoned ETA history.  This wrapper
    accumulates only active execution intervals and freezes them on waiting,
    failure or completion.
    """

    def __init__(self, tracker: Any) -> None:
        self.tracker = tracker
        self._original_begin = tracker.begin
        self._original_phase_start = tracker.phase_start
        self._original_snapshot = tracker.snapshot

    @staticmethod
    def _accumulate(record: dict[str, Any]) -> float:
        total = float(record.get("execution_elapsed_seconds") or 0.0)
        active = str(record.get("active_started_at") or "").strip()
        if active:
            total += _seconds_between(active)
        record["execution_elapsed_seconds"] = round(total, 3)
        record["active_started_at"] = ""
        return total

    @staticmethod
    def _legacy_reconstruction(record: dict[str, Any]) -> float:
        wall = _seconds_between(record.get("started_at"), record.get("completed_at"))
        phase = _seconds_between(record.get("started_at"), record.get("phase_started_at"))
        if phase > 0:
            # The final observed model/control phase is close to the real end;
            # add a small local-finalization allowance but never idle minutes.
            return min(wall, phase + 30.0) if wall > 0 else phase + 30.0
        estimate = float(record.get("estimated_total_seconds") or 0.0)
        if estimate > 0 and wall > estimate * 2.0:
            return estimate
        return wall

    def _execution_elapsed(self, record: dict[str, Any]) -> float:
        if "execution_elapsed_seconds" in record:
            total = float(record.get("execution_elapsed_seconds") or 0.0)
            if str(record.get("status") or "") == "running" and str(record.get("active_started_at") or ""):
                total += _seconds_between(record.get("active_started_at"))
            return max(0.0, total)
        if str(record.get("status") or "") in {"completed", "failed"}:
            return self._legacy_reconstruction(record)
        return _seconds_between(record.get("started_at"))

    def install(self) -> None:
        tracker = self.tracker
        if getattr(tracker, "_xiaoduan_execution_timing_fix_installed", False):
            return
        service = self
        original_begin = self._original_begin
        original_phase_start = self._original_phase_start
        original_snapshot = self._original_snapshot

        def begin(instance: Any, project_id: str, stage: str, *, input_chars: int = 0) -> dict[str, Any]:
            record = original_begin(project_id, stage, input_chars=input_chars)
            if not record:
                return record
            persisted = instance._read(project_id) or record
            if "execution_elapsed_seconds" not in persisted:
                persisted["execution_elapsed_seconds"] = 0.0
            if not str(persisted.get("active_started_at") or ""):
                persisted["active_started_at"] = _utcnow()
            persisted["timing_mode"] = "active_execution_only"
            instance._write(persisted)
            return persisted

        def phase_start(instance: Any, project_id: str, stage: str, phase: str) -> None:
            original_phase_start(project_id, stage, phase)
            record = instance._read(project_id)
            if not record or record.get("stage") != stage:
                return
            if not str(record.get("active_started_at") or ""):
                record["active_started_at"] = _utcnow()
                instance._write(record)

        def waiting(instance: Any, project_id: str, stage: str) -> None:
            record = instance._read(project_id)
            if not record or record.get("stage") != stage:
                return
            service._accumulate(record)
            record["status"] = "waiting"
            record["current_phase"] = "waiting_for_user_or_local_finalize"
            record["waiting_started_at"] = _utcnow()
            record["timing_mode"] = "active_execution_only"
            instance._write(record)

        def fail(instance: Any, project_id: str, stage: str, error: Exception) -> None:
            record = instance._read(project_id) or {
                "schema_version": instance.schema_version,
                "project_id": project_id,
                "stage": stage,
                "started_at": _utcnow(),
                "input_chars": 0,
                "estimated_total_seconds": 0,
                "estimate_source": "baseline",
                "history_samples": 0,
                "execution_elapsed_seconds": 0.0,
            }
            service._accumulate(record)
            record["status"] = "failed"
            record["error"] = f"{type(error).__name__}: {error}"[:1000]
            record["completed_at"] = _utcnow()
            record["timing_mode"] = "active_execution_only"
            instance._write(record)

        def complete(instance: Any, project_id: str, stage: str) -> None:
            record = instance._read(project_id)
            if not record or record.get("stage") != stage:
                return
            duration = service._accumulate(record)
            if duration <= 0:
                duration = service._legacy_reconstruction(record)
                record["execution_elapsed_seconds"] = round(duration, 3)
                record["timing_source"] = "legacy_reconstructed"
            else:
                record["timing_source"] = "active_execution_intervals"
            record["status"] = "completed"
            record["observed_floor"] = 100.0
            record["current_phase"] = "completed"
            record["completed_at"] = _utcnow()
            record["timing_mode"] = "active_execution_only"
            already_recorded = bool(record.get("history_recorded"))
            if not already_recorded and 5 <= duration <= 7200:
                data = instance._history()
                samples = list(data.get("samples") or [])
                samples.append({
                    "stage": stage,
                    "size_bucket": "small" if int(record.get("input_chars") or 0) <= 4000 else "medium" if int(record.get("input_chars") or 0) <= 15000 else "large",
                    "input_chars": int(record.get("input_chars") or 0),
                    "duration_seconds": round(duration, 3),
                    "completed_at": record["completed_at"],
                    "timing_mode": "active_execution_only",
                })
                data["samples"] = samples[-120:]
                instance._write_history(data)
                record["history_recorded"] = True
            instance._write(record)

        def snapshot(instance: Any, project_id: str) -> dict[str, Any]:
            result = original_snapshot(project_id)
            record = instance._read(project_id)
            if not record or str(record.get("stage") or "") != str(result.get("stage") or ""):
                return result
            elapsed = service._execution_elapsed(record)
            result["elapsed_seconds"] = round(elapsed, 1)
            result["timing_mode"] = "active_execution_only"
            result["timing_source"] = record.get("timing_source") or "active_execution_intervals"
            estimate = max(0.0, float(record.get("estimated_total_seconds") or 0.0))
            if result.get("status") == "running" and estimate > 0:
                remaining = max(0.0, estimate - elapsed)
                result["estimated_remaining_seconds"] = round(remaining, 1)
                if str(record.get("estimate_source")) == "history":
                    result["estimated_remaining_low_seconds"] = round(remaining * 0.75, 1)
                    result["estimated_remaining_high_seconds"] = round(remaining * 1.35, 1)
                else:
                    result["estimated_remaining_low_seconds"] = round(remaining * 0.65, 1)
                    result["estimated_remaining_high_seconds"] = round(remaining * 1.75, 1)
            else:
                result["estimated_remaining_seconds"] = None
                result["estimated_remaining_low_seconds"] = None
                result["estimated_remaining_high_seconds"] = None
            return result

        tracker.begin = MethodType(begin, tracker)
        tracker.phase_start = MethodType(phase_start, tracker)
        tracker.waiting = MethodType(waiting, tracker)
        tracker.fail = MethodType(fail, tracker)
        tracker.complete = MethodType(complete, tracker)
        tracker.snapshot = MethodType(snapshot, tracker)
        tracker._xiaoduan_execution_timing_fix_installed = True


__all__ = ["AuthoringExecutionTimingFix"]
