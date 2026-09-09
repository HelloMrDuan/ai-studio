from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.v3.media.bgm import BGMStore, validate_audio_file


def _clean(value: Any) -> str:
    return str(value or "").strip()


class BGMPrefetchService:
    """Prepare BGM choices after storyboard confirmation without auto-adoption.

    There is intentionally no fake music generator.  The service extracts the
    storyboard's music intent and exposes validated user/default library tracks
    as reviewable candidates.  If no source exists, it says so explicitly.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.store = BGMStore(settings.data_dir)
        self.root = Path(settings.data_dir) / "v3" / "original-workbench-postproduction"
        self.root.mkdir(parents=True, exist_ok=True)
        self._tasks: set[asyncio.Task[Any]] = set()

    def _project_dir(self, project_id: str) -> Path:
        self.director.get_project(project_id)
        path = self.root / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _state_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "state.json"

    def _load(self, project_id: str) -> dict[str, Any]:
        path = self._state_path(project_id)
        if not path.is_file():
            return {"project_id": project_id}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"project_id": project_id}
        except Exception:
            return {"project_id": project_id}

    def _save(self, project_id: str, **updates: Any) -> dict[str, Any]:
        state = self._load(project_id)
        state.update(updates)
        target = self._state_path(project_id)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)
        return state

    def _url(self, path: Path) -> str:
        root = Path(self.settings.data_dir).resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            return ""
        return "/files/" + resolved.relative_to(root).as_posix()

    def _music_intent(self, project_id: str) -> str:
        path = Path(self.settings.data_dir) / "story_continuity" / f"{project_id}.json"
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        values: list[str] = []
        for shot in data.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            value = _clean(shot.get("music"))
            if value and value not in values:
                values.append(value)
        return "；".join(values[:12])

    @staticmethod
    def _candidate_id(path: Path) -> str:
        return "bgm_" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]

    def _candidate(self, path: Path, *, recommended: bool = False) -> dict[str, Any]:
        return {
            "candidate_id": self._candidate_id(path),
            "name": path.name,
            "path": str(path),
            "url": self._url(path),
            "recommended": bool(recommended),
        }

    async def prepare(self, project_id: str) -> dict[str, Any]:
        self.director.get_project(project_id)
        intent = self._music_intent(project_id)
        state = self._load(project_id)
        active = Path(_clean(state.get("bgm_path")))
        if active.is_file():
            return self._save(
                project_id,
                bgm_prefetch_status="already_selected",
                bgm_intent=intent,
            )

        paths: list[Path] = []
        default = Path(_clean(os.environ.get("XIAODUAN_DEFAULT_BGM_PATH")))
        if _clean(os.environ.get("XIAODUAN_DEFAULT_BGM_PATH")) and default.is_file():
            try:
                await asyncio.to_thread(validate_audio_file, default, timeout_seconds=30)
                paths.append(default)
            except Exception:
                pass
        for path in self.store.list_files():
            if path not in paths and path.is_file() and path.stat().st_size > 0:
                paths.append(path)

        candidates = [self._candidate(path, recommended=(index == 0 and default == path)) for index, path in enumerate(paths[:20])]
        status = "ready" if candidates else "needs_source"
        message = (
            f"已并行准备 {len(candidates)} 个背景音乐候选，等待试听采用"
            if candidates
            else "当前没有可用背景音乐曲库或默认曲；系统不会随机乱配，请上传或配置音乐来源"
        )
        return self._save(
            project_id,
            bgm_prefetch_status=status,
            bgm_prefetch_message=message,
            bgm_intent=intent,
            bgm_candidates=candidates,
        )

    def public_state(self, project_id: str) -> dict[str, Any]:
        state = self._load(project_id)
        candidates = []
        for raw in state.get("bgm_candidates") or []:
            if not isinstance(raw, dict):
                continue
            path = Path(_clean(raw.get("path")))
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            item = dict(raw)
            item["url"] = self._url(path)
            candidates.append(item)
        return {
            "project_id": project_id,
            "status": _clean(state.get("bgm_prefetch_status")) or "not_started",
            "message": _clean(state.get("bgm_prefetch_message")),
            "music_intent": _clean(state.get("bgm_intent")),
            "candidates": candidates,
            "adopted_candidate_id": _clean(state.get("bgm_prefetch_adopted_candidate_id")),
            "manual_adoption_required": True,
        }

    def adopt(self, project_id: str, candidate_id: str) -> dict[str, Any]:
        state = self._load(project_id)
        chosen = None
        for raw in state.get("bgm_candidates") or []:
            if isinstance(raw, dict) and _clean(raw.get("candidate_id")) == _clean(candidate_id):
                chosen = dict(raw)
                break
        if chosen is None:
            raise FileNotFoundError("背景音乐候选不存在或已经过期")
        path = Path(_clean(chosen.get("path")))
        validate_audio_file(path, timeout_seconds=30)
        self._save(
            project_id,
            bgm_path=str(path),
            bgm_prefetch_status="adopted",
            bgm_prefetch_adopted_candidate_id=_clean(candidate_id),
            bgm_prefetch_message="背景音乐候选已采用；仍可在成片阶段上传替换",
        )
        return self.public_state(project_id)

    def install_confirmation_hook(self) -> None:
        if getattr(self.director, "_xiaoduan_bgm_prefetch_installed", False):
            return
        original = self.director.confirm_stage

        async def wrapped(project_id: str):
            before = self.director.get_project(project_id)
            stage = _clean(before.get("current_stage"))
            result = await original(project_id)
            if stage == "04":
                task = asyncio.create_task(self.prepare(project_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return result

        self.director.confirm_stage = wrapped
        self.director._xiaoduan_bgm_prefetch_installed = True


def create_bgm_prefetch_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = BGMPrefetchService(settings, legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/bgm-prefetch")
    async def bgm_prefetch_status(project_id: str) -> dict[str, Any]:
        try:
            return service.public_state(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/bgm-prefetch/prepare")
    async def bgm_prefetch_prepare(project_id: str) -> dict[str, Any]:
        try:
            await service.prepare(project_id)
            return service.public_state(project_id)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"背景音乐候选准备失败：{exc}") from exc

    @router.post("/api/v3/studio/projects/{project_id}/bgm-prefetch/{candidate_id}/adopt")
    async def bgm_prefetch_adopt(project_id: str, candidate_id: str) -> dict[str, Any]:
        try:
            return service.adopt(project_id, candidate_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"背景音乐候选采用失败：{exc}") from exc

    return router


__all__ = ["BGMPrefetchService", "create_bgm_prefetch_router"]
