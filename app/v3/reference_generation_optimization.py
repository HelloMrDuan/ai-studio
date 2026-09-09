from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter


logger = logging.getLogger(__name__)

_REFERENCE_ROLES = {
    "character_reference",
    "scene_reference",
    "location_reference",
    "prop_reference",
}
_ACTIVE = {"queued", "switching_gpu", "running", "generating", "submitting"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class ReferenceSubmission:
    submission_id: str
    project_id: str
    target_asset_id: str
    entity_ids: list[str]
    status: str = "queued"
    progress: int = 0
    stage: str = "queued"
    message: str = "参考图生成任务已创建"
    candidate_id: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "project_id": self.project_id,
            "target_asset_id": self.target_asset_id,
            "entity_ids": list(self.entity_ids),
            "status": self.status,
            "progress": int(self.progress),
            "stage": self.stage,
            "message": self.message,
            "candidate_id": self.candidate_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ReferenceGenerationOptimizer:
    """Keep the existing reference/candidate system, but make long submissions non-blocking.

    The original reference endpoint awaited the mature legacy txt2img path. That
    made a HTTP click wait on GPU/model work and made generate-missing effectively
    submit one reference at a time. This wrapper changes only reference targets:
    the existing bridge still performs the real generation, while the request
    returns immediately and a bounded background dispatcher performs the work.
    """

    def __init__(self, bridge: Any, *, max_concurrency: int = 2) -> None:
        self.bridge = bridge
        self.legacy = bridge.legacy
        self._gate = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._tasks: set[asyncio.Task[Any]] = set()
        self._submissions: dict[str, ReferenceSubmission] = {}
        self._target_active: dict[tuple[str, str], str] = {}

    @staticmethod
    def _is_reference_target(target: dict[str, Any]) -> bool:
        role = _clean(target.get("asset_role"))
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        return bool(metadata.get("reference_asset")) or role in _REFERENCE_ROLES

    def _sync_record_from_candidate(self, record: ReferenceSubmission) -> None:
        sync = getattr(self.legacy, "_wb_sync_candidates", None)
        load = getattr(self.legacy, "_wb_load_candidates", None)
        try:
            rows = sync(record.project_id) if callable(sync) else load(record.project_id) if callable(load) else []
        except Exception:
            return
        candidates = [
            row for row in rows or []
            if _clean(row.get("target_asset_id")) == record.target_asset_id
        ]
        candidates.sort(key=lambda row: _clean(row.get("updated_at") or row.get("created_at")), reverse=True)
        if not candidates:
            return
        candidate = candidates[0]
        record.candidate_id = _clean(candidate.get("candidate_id"))
        state = _clean(candidate.get("status")).lower()
        if state:
            record.status = state
        try:
            record.progress = max(record.progress, int(candidate.get("progress") or 0))
        except Exception:
            pass
        message = _clean(candidate.get("message"))
        if message:
            record.message = message
        error = _clean(candidate.get("error"))
        if error:
            record.error = error
        if state == "completed":
            record.progress = 100
            record.stage = "candidate_ready"
        elif state == "failed":
            record.progress = 100
            record.stage = "failed"
        elif state in {"switching_gpu"}:
            record.stage = "preparing_model"
        elif state in {"running", "generating"}:
            record.stage = "comfy_running"
        elif state == "queued":
            record.stage = "queued"
        record.updated_at = time.time()

    async def _run_reference(
        self,
        record: ReferenceSubmission,
        project_id: str,
        payload: dict[str, Any],
    ) -> None:
        target_key = (project_id, record.target_asset_id)
        try:
            async with self._gate:
                record.status = "submitting"
                record.progress = max(record.progress, 1)
                record.stage = "dispatching"
                record.message = "正在创建候选并提交到图像生成器"
                record.updated_at = time.time()
                logger.info(
                    "REFERENCE_ITEM_START project_id=%s submission_id=%s target_asset_id=%s entity_ids=%s",
                    project_id,
                    record.submission_id,
                    record.target_asset_id,
                    ",".join(record.entity_ids),
                )
                started = time.monotonic()
                response = await self.bridge.execute_candidate(project_id, payload)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                record.stage = "provider_submitted"
                record.message = "生成任务已提交，正在等待模型输出"
                record.progress = max(record.progress, 5)
                record.status = "queued"
                record.updated_at = time.time()
                logger.info(
                    "REFERENCE_PROVIDER_SUBMITTED project_id=%s submission_id=%s target_asset_id=%s elapsed_ms=%s producer=%s",
                    project_id,
                    record.submission_id,
                    record.target_asset_id,
                    elapsed_ms,
                    _clean((response or {}).get("producer")) if isinstance(response, dict) else "",
                )
                self._sync_record_from_candidate(record)
        except Exception as exc:
            record.status = "failed"
            record.progress = 100
            record.stage = "failed"
            record.error = _clean(exc) or type(exc).__name__
            record.message = f"参考图生成提交失败：{record.error}"
            record.updated_at = time.time()
            logger.exception(
                "REFERENCE_ITEM_FAILED project_id=%s submission_id=%s target_asset_id=%s",
                project_id,
                record.submission_id,
                record.target_asset_id,
            )
        finally:
            active_id = self._target_active.get(target_key)
            if active_id == record.submission_id:
                self._target_active.pop(target_key, None)

    async def execute_candidate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capability = _clean(payload.get("capability")).lower()
        target_asset_id = _clean(payload.get("target_asset_id"))
        if capability != "image" or not target_asset_id:
            return await self.bridge.execute_candidate(project_id, payload)

        target = self.legacy.director.production.get_asset(project_id, target_asset_id)
        if not self._is_reference_target(target):
            return await self.bridge.execute_candidate(project_id, payload)

        key = (project_id, target_asset_id)
        active_submission_id = self._target_active.get(key)
        if active_submission_id:
            existing = self._submissions.get(active_submission_id)
            if existing and existing.status in _ACTIVE:
                self._sync_record_from_candidate(existing)
                return {
                    "submitted": False,
                    "already_pending": True,
                    "reference_submission": existing.as_dict(),
                    "producer": "reference-background-dispatch",
                }

        submission_id = "refsub_" + secrets.token_hex(10)
        entity_ids = [_clean(value) for value in target.get("entity_ids") or [] if _clean(value)]
        record = ReferenceSubmission(
            submission_id=submission_id,
            project_id=project_id,
            target_asset_id=target_asset_id,
            entity_ids=entity_ids,
        )
        self._submissions[submission_id] = record
        self._target_active[key] = submission_id
        task = asyncio.create_task(self._run_reference(record, project_id, dict(payload)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            "REFERENCE_ITEM_QUEUED project_id=%s submission_id=%s target_asset_id=%s entity_ids=%s active_background=%s",
            project_id,
            submission_id,
            target_asset_id,
            ",".join(entity_ids),
            len(self._tasks),
        )
        return {
            "submitted": True,
            "reference_submission": record.as_dict(),
            "producer": "reference-background-dispatch",
            "manual_adoption_required": True,
        }

    def status(self, project_id: str) -> dict[str, Any]:
        now = time.time()
        rows: list[dict[str, Any]] = []
        # Keep only recent diagnostics; durable candidate/task state remains in the existing stores.
        for submission_id, record in list(self._submissions.items()):
            if now - record.updated_at > 3600 and record.status not in _ACTIVE:
                self._submissions.pop(submission_id, None)
                continue
            if record.project_id != project_id:
                continue
            self._sync_record_from_candidate(record)
            rows.append(record.as_dict())
        rows.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        active = sum(1 for item in rows if _clean(item.get("status")).lower() in _ACTIVE)
        failed = sum(1 for item in rows if _clean(item.get("status")).lower() == "failed")
        completed = sum(1 for item in rows if _clean(item.get("status")).lower() == "completed")
        return {
            "project_id": project_id,
            "max_concurrency": getattr(self._gate, "_value", None),
            "active_count": active,
            "completed_count": completed,
            "failed_count": failed,
            "submissions": rows,
        }

    def install(self) -> None:
        self.legacy.director_workbench_execute_candidate = self.execute_candidate
        # The bridge's automatic missing-reference gate was created before this
        # optimizer. Point it at the same non-blocking dispatcher as the UI route.
        if getattr(self.bridge, "reference_bootstrap", None) is not None:
            self.bridge.reference_bootstrap.submit_candidate = self.execute_candidate


def create_reference_generation_optimization_router(optimizer: ReferenceGenerationOptimizer) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v3/studio/projects/{project_id}/reference-submissions")
    async def reference_submission_status(project_id: str) -> dict[str, Any]:
        return optimizer.status(project_id)

    return router


__all__ = [
    "ReferenceGenerationOptimizer",
    "create_reference_generation_optimization_router",
]
