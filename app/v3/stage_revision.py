from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.services.skill_runtime import empty_runtime_state


_STAGE_ORDER = ("01", "02", "03", "04")
_ACTIVE_STATES = {"starting", "warming", "queued", "switching_gpu", "running", "generating", "persisting"}
_DOWNSTREAM_MEDIA_STAGES = {"make", "edit", "final"}
_SOURCE_ROLES = {"source_brief", "source_text", "source_input", "source_upload", "original_source"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


class StageRevisionService:
    """Re-open a confirmed authoring stage while preserving old versions.

    Raw source inputs are immutable project inputs and are never invalidated.
    Generated outputs from the selected stage onward are kept for history but
    become stale so they cannot silently drive downstream production.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.data_dir = Path(settings.data_dir)

    @staticmethod
    def _stage(value: str) -> str:
        stage = _clean(value)
        if stage not in _STAGE_ORDER:
            raise ValueError("只能修改①剧本、②角色、③视觉或④分镜")
        return stage

    def _active_candidate_ids(self, project_id: str) -> list[str]:
        loader = getattr(self.legacy, "_wb_load_candidates", None)
        if not callable(loader):
            return []
        result: list[str] = []
        for row in loader(project_id) or []:
            state = _clean(row.get("status")).lower()
            if state in _ACTIVE_STATES and not _clean(row.get("confirmed_asset_id")):
                result.append(_clean(row.get("candidate_id")) or _clean(row.get("task_id")))
        return [item for item in result if item]

    def _active_job_ids(self, project_id: str) -> list[str]:
        result: list[str] = []
        for folder_name in ("studio_jobs", "studio_video_edit_jobs"):
            folder = self.data_dir / folder_name
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if _clean(row.get("project_id")) != project_id:
                    continue
                if _clean(row.get("status")).lower() in _ACTIVE_STATES:
                    result.append(_clean(row.get("job_id")) or path.stem)
        return result

    def _assert_idle(self, project_id: str) -> None:
        if self._active_candidate_ids(project_id) or self._active_job_ids(project_id):
            raise RuntimeError("当前作品仍有生成任务运行中，请等待任务结束或取消后再修改上游阶段。")

    @staticmethod
    def _reset_stage_state(revision_id: str) -> dict[str, Any]:
        return {
            "internal_step": "",
            "stage_memory": "",
            "stage_ready": False,
            "handoff": "",
            "next_expected_action": "",
            "last_required_files": [],
            "approved_steps": [],
            "last_control_action": "",
            "native_plan": {},
            "skill_contract": {},
            "skill_runtime": empty_runtime_state(),
            "last_native_target": {},
            "last_skill_runtime_control": {},
            "revision_id": revision_id,
        }

    @staticmethod
    def _is_immutable_source(item: dict[str, Any]) -> bool:
        role = _clean(item.get("asset_role")).lower()
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        source_type = _clean(source.get("type")).lower()
        return role in _SOURCE_ROLES or source_type in {"source_brief", "source_upload", "original_source"}

    def _stale_assets(self, project_id: str, affected_core: set[str], revision_id: str) -> list[str]:
        production = self.director.production
        graph = production.get_graph(project_id)
        assets = graph.get("assets") or {}
        stale_ids: set[str] = set()
        affected_stages = set(affected_core) | _DOWNSTREAM_MEDIA_STAGES

        for asset_id, item in assets.items():
            if not isinstance(item, dict) or item.get("active") is False:
                continue
            if self._is_immutable_source(item):
                continue
            if _clean(item.get("stage")) not in affected_stages:
                continue
            item["dependency_state"] = "stale"
            metadata = item.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["stale_reason"] = "上游创作阶段已重新打开修改"
                metadata["stage_revision_id"] = revision_id
            item["updated_at"] = _now()
            stale_ids.add(str(asset_id))

        changed = True
        while changed:
            changed = False
            for asset_id, item in assets.items():
                if not isinstance(item, dict) or item.get("active") is False or str(asset_id) in stale_ids:
                    continue
                if self._is_immutable_source(item):
                    continue
                parents = {_clean(value) for value in item.get("parent_asset_ids") or [] if _clean(value)}
                hits = parents & stale_ids
                if not hits:
                    continue
                item["dependency_state"] = "stale"
                stale_parents = item.setdefault("stale_parent_asset_ids", [])
                for parent_id in sorted(hits):
                    if parent_id not in stale_parents:
                        stale_parents.append(parent_id)
                metadata = item.setdefault("metadata", {})
                if isinstance(metadata, dict):
                    metadata["stale_reason"] = "上游版本已失效"
                    metadata["stage_revision_id"] = revision_id
                item["updated_at"] = _now()
                stale_ids.add(str(asset_id))
                changed = True

        production._save(graph)
        return sorted(stale_ids)

    def reopen(self, project_id: str, stage: str, *, reason: str = "") -> dict[str, Any]:
        stage = self._stage(stage)
        self._assert_idle(project_id)
        project = self.director.get_project(project_id)
        completed = [_clean(value) for value in project.get("completed_stages") or []]
        current = _clean(project.get("current_stage"))
        if stage not in completed and current != stage:
            raise ValueError("该阶段尚未完成，不需要回退修改")

        index = _STAGE_ORDER.index(stage)
        affected_core = set(_STAGE_ORDER[index:])
        revision_id = f"rev-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        stale_ids = self._stale_assets(project_id, affected_core, revision_id)

        confirmed = project.setdefault("confirmed_outputs", {})
        previous_confirmed = [item for item in _STAGE_ORDER[index:] if confirmed.get(item) is not None]
        for item in _STAGE_ORDER[index:]:
            confirmed.pop(item, None)

        project["completed_stages"] = [item for item in completed if item in _STAGE_ORDER[:index]]
        project["current_stage"] = stage
        project["status"] = "active"
        states = project.setdefault("stage_state", {})
        for item in _STAGE_ORDER[index:]:
            states[item] = self._reset_stage_state(revision_id)

        event = {
            "revision_id": revision_id,
            "action": "reopen_stage",
            "stage": stage,
            "affected_stages": list(_STAGE_ORDER[index:]),
            "stale_asset_ids": stale_ids,
            "reason": _clean(reason),
            "previous_current_stage": current,
            "previous_confirmed_output_stages": previous_confirmed,
            "created_at": _now(),
        }
        project.setdefault("revision_events", []).append(event)
        project["updated_at"] = _now()
        self.director._save_project(project)
        return {
            "reopened": True,
            "project_id": project_id,
            "stage": stage,
            "revision_id": revision_id,
            "stale_asset_count": len(stale_ids),
            "stale_asset_ids": stale_ids,
            "history_preserved": True,
            "old_asset_versions_preserved": True,
            "project": project,
        }


def create_stage_revision_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = StageRevisionService(settings, legacy_runtime)

    @router.post("/api/v3/studio/projects/{project_id}/stages/{stage}/reopen")
    async def reopen_stage(project_id: str, stage: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return service.reopen(project_id, stage, reason=_clean((payload or {}).get("reason")))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router


__all__ = ["StageRevisionService", "create_stage_revision_router"]
