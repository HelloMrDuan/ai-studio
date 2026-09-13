from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from app.v3.stage_progress import StageProgressTracker, create_stage_progress_router


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


def _seconds_between(start: Any, end: Any) -> float:
    left = _parse_time(start)
    right = _parse_time(end)
    if left is None or right is None:
        return 0.0
    return max(0.0, (right - left).total_seconds())


class AuthoringStageProgressTracker(StageProgressTracker):
    """Stage progress with execution-only timing.

    Completed authoring stages must not keep accumulating wall-clock time after
    completion, and a stage recovered from the retired 94% waiting state must
    not count user/restart idle time as model execution time.
    """

    def complete(self, project_id: str, stage: str) -> None:
        with self._mutex:
            record = self._read(project_id)
            if not record or record.get("stage") != stage:
                return
            if record.get("status") == "completed" and record.get("execution_elapsed_seconds") is not None:
                return

            prior_status = str(record.get("status") or "")
            completed_at = datetime.now(timezone.utc).isoformat()
            execution_end = completed_at
            # A waiting record means the real production call had already
            # returned. Its previous updated_at is therefore the truthful end
            # of active execution; later user/restart idle time is excluded.
            if prior_status == "waiting" and record.get("updated_at"):
                execution_end = str(record.get("updated_at"))

            duration = _seconds_between(record.get("started_at"), execution_end)
            record["status"] = "completed"
            record["observed_floor"] = 100.0
            record["current_phase"] = "completed"
            record["completed_at"] = completed_at
            record["execution_ended_at"] = execution_end
            record["execution_elapsed_seconds"] = round(duration, 3)
            record["excluded_idle_wait"] = prior_status == "waiting"
            self._write(record)

            if duration < 5 or duration > 7200:
                return
            data = self._history()
            samples = list(data.get("samples") or [])
            samples.append(
                {
                    "stage": stage,
                    "size_bucket": (
                        "small" if int(record.get("input_chars") or 0) <= 4000
                        else "medium" if int(record.get("input_chars") or 0) <= 15000
                        else "large"
                    ),
                    "input_chars": int(record.get("input_chars") or 0),
                    "duration_seconds": round(duration, 3),
                    "completed_at": completed_at,
                }
            )
            data["samples"] = samples[-120:]
            self._write_history(data)

    def snapshot(self, project_id: str) -> dict[str, Any]:
        data = super().snapshot(project_id)
        record = self._read(project_id)
        if not record or str(record.get("stage") or "") != str(data.get("stage") or ""):
            return data

        status = str(record.get("status") or "")
        frozen = record.get("execution_elapsed_seconds")
        if status in {"completed", "failed"} and frozen is not None:
            data["elapsed_seconds"] = round(max(0.0, float(frozen)), 1)
            data["elapsed_is_frozen"] = True
            data["excluded_idle_wait"] = bool(record.get("excluded_idle_wait"))
            return data

        # Compatibility for results finalized before execution-only timing was
        # introduced. If the stage was reconciled from the retired waiting
        # state, the last real phase start is a safer lower-bound than counting
        # all later idle time. Mark it as an estimate instead of pretending it
        # is exact.
        try:
            project = self.director.get_project(project_id)
            state = ((project.get("stage_state") or {}).get(str(data.get("stage") or "")) or {})
            plan = state.get("native_plan") if isinstance(state.get("native_plan"), dict) else {}
            reconciled = str(plan.get("completion_mode") or "") == "single_pass_reconcile_existing_result"
        except Exception:
            reconciled = False
        if status == "completed" and frozen is None and reconciled:
            inferred = _seconds_between(record.get("started_at"), record.get("phase_started_at"))
            if inferred > 0:
                data["elapsed_seconds"] = round(inferred, 1)
                data["elapsed_is_frozen"] = True
                data["elapsed_is_recovered_estimate"] = True
                data["excluded_idle_wait"] = True
        return data


def create_authoring_progress_tracker(settings: Any, director: Any) -> Any:
    tracker = AuthoringStageProgressTracker(settings, director)
    tracker.install()
    return tracker


def create_authoring_progress_router(tracker: Any) -> APIRouter:
    return create_stage_progress_router(tracker)


__all__ = [
    "AuthoringStageProgressTracker",
    "create_authoring_progress_tracker",
    "create_authoring_progress_router",
]
