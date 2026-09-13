from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.v3.resource_store import ResourceStore


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _media_kind(asset_type: str, url: str = "") -> str:
    value = _clean(asset_type).upper()
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if value == "IMAGE" or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if value == "VIDEO" or suffix in {".mp4", ".webm", ".mov", ".mkv"}:
        return "video"
    if value == "AUDIO" or suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}:
        return "audio"
    return "text"


class ProjectAssetExplorer:
    """Read-only cross-stage view over formal assets, candidates and V3 resources."""

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.production = legacy_runtime.director.production
        self.resources = ResourceStore(settings.data_dir)
        self.data_root = Path(settings.data_dir).resolve()

    def _file_url(self, raw_path: Any) -> str:
        value = _clean(raw_path)
        if not value:
            return ""
        try:
            path = Path(value).resolve()
            if path != self.data_root and self.data_root not in path.parents:
                return ""
            if not path.is_file() or path.stat().st_size <= 0:
                return ""
            return "/files/" + path.relative_to(self.data_root).as_posix()
        except Exception:
            return ""

    def _formal_assets(self, project_id: str) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.production.list_assets(project_id, active_only=False)]
        reverse: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            child_id = _clean(row.get("asset_id"))
            child_name = _clean(row.get("name")) or child_id
            for parent_id in row.get("parent_asset_ids") or []:
                parent = _clean(parent_id)
                if parent:
                    reverse.setdefault(parent, []).append({"type": "asset", "id": child_id, "name": child_name})

        result: list[dict[str, Any]] = []
        for row in rows:
            asset_id = _clean(row.get("asset_id"))
            try:
                url = _clean(self.production.asset_url(project_id, asset_id))
            except Exception:
                url = ""
            kind = _media_kind(_clean(row.get("asset_type")), url)
            text = ""
            if kind == "text":
                try:
                    text = _clean(self.production.read_text_asset(project_id, asset_id, max_chars=60000))
                except Exception:
                    text = ""
            result.append(
                {
                    "entry_id": f"asset:{asset_id}",
                    "source_type": "正式资产",
                    "asset_id": asset_id,
                    "name": _clean(row.get("name")) or asset_id,
                    "stage": _clean(row.get("stage")),
                    "skill": _clean(row.get("skill")),
                    "kind": kind,
                    "asset_type": _clean(row.get("asset_type")),
                    "asset_role": _clean(row.get("asset_role")),
                    "logical_key": _clean(row.get("logical_key")),
                    "version": int(row.get("version") or 0),
                    "status": _clean(row.get("status")),
                    "dependency_state": _clean(row.get("dependency_state")),
                    "entity_ids": [_clean(x) for x in row.get("entity_ids") or [] if _clean(x)],
                    "parent_asset_ids": [_clean(x) for x in row.get("parent_asset_ids") or [] if _clean(x)],
                    "url": url,
                    "text": text,
                    "source": row.get("source") if isinstance(row.get("source"), dict) else {},
                    "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                    "used_by": reverse.get(asset_id, []),
                    "created_at": _clean(row.get("created_at")),
                    "updated_at": _clean(row.get("updated_at")),
                }
            )
        return result

    def _candidate_entries(self, project_id: str) -> list[dict[str, Any]]:
        loader = getattr(self.legacy, "_wb_load_candidates", None)
        if not callable(loader):
            return []
        try:
            rows = loader(project_id)
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            outputs = [_clean(x) for x in row.get("output_files") or [] if _clean(x)]
            candidate_id = _clean(row.get("candidate_id"))
            kind = _media_kind(_clean(row.get("output_asset_type")), outputs[0] if outputs else "")
            result.append(
                {
                    "entry_id": f"candidate:{candidate_id}",
                    "source_type": "生成候选",
                    "candidate_id": candidate_id,
                    "name": _clean(row.get("title")) or _clean(row.get("capability")) or candidate_id,
                    "stage": "05",
                    "kind": kind,
                    "asset_type": _clean(row.get("output_asset_type")),
                    "asset_role": _clean(row.get("capability")) + "_candidate",
                    "version": 0,
                    "status": _clean(row.get("status")),
                    "dependency_state": "current",
                    "parent_asset_ids": [_clean(x) for x in row.get("dependency_asset_ids") or [] if _clean(x)],
                    "url": outputs[0] if outputs else "",
                    "text": "",
                    "source": {"producer": _clean(row.get("producer"))},
                    "metadata": {
                        "prompt_asset_id": _clean(row.get("prompt_asset_id")),
                        "task_id": _clean(row.get("task_id")),
                        "confirmed_asset_id": _clean(row.get("confirmed_asset_id")),
                        "v3_resource_id": _clean(row.get("v3_resource_id")),
                        "params": row.get("params") if isinstance(row.get("params"), dict) else {},
                    },
                    "used_by": [],
                    "created_at": _clean(row.get("created_at")),
                    "updated_at": _clean(row.get("updated_at")),
                }
            )
        return result

    def _resource_entries(self, project_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self.resources.list_all(project_id):
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            url = self._file_url(metadata.get("artifact_path"))
            kind = _media_kind(_clean(metadata.get("media_kind")), url)
            resource_id = _clean(row.get("resource_id"))
            result.append(
                {
                    "entry_id": f"resource:{resource_id}",
                    "source_type": "新版资源",
                    "resource_id": resource_id,
                    "name": _clean(row.get("logical_key")) or resource_id,
                    "stage": "05",
                    "kind": kind,
                    "asset_type": _clean(metadata.get("media_kind")).upper(),
                    "asset_role": _clean(metadata.get("quality_stage")) or "generated_resource",
                    "logical_key": _clean(row.get("logical_key")),
                    "version": int(row.get("version") or 0),
                    "status": _clean(row.get("state")),
                    "dependency_state": "current",
                    "parent_asset_ids": [],
                    "reference_ids": [_clean(x) for x in row.get("reference_ids") or [] if _clean(x)],
                    "url": url,
                    "text": "",
                    "source": {
                        "provider_id": _clean(row.get("provider_id")),
                        "model_id": _clean(row.get("model_id")),
                        "generation_task_id": _clean(row.get("generation_task_id")),
                    },
                    "metadata": metadata,
                    "used_by": [],
                    "created_at": _clean(row.get("created_at")),
                    "updated_at": _clean(row.get("updated_at")),
                }
            )
        return result

    def snapshot(self, project_id: str) -> dict[str, Any]:
        self.legacy.director.get_project(project_id)
        formal = self._formal_assets(project_id)
        candidates = self._candidate_entries(project_id)
        resources = self._resource_entries(project_id)
        entries = formal + candidates + resources
        entries.sort(key=lambda item: (_clean(item.get("updated_at")) or _clean(item.get("created_at"))), reverse=True)
        counts: dict[str, int] = {"all": len(entries), "image": 0, "video": 0, "audio": 0, "text": 0}
        for entry in entries:
            kind = _clean(entry.get("kind"))
            if kind in counts:
                counts[kind] += 1
        return {
            "project_id": project_id,
            "entries": entries,
            "counts": counts,
            "read_only": True,
            "cross_stage": True,
        }


def create_asset_explorer_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    explorer = ProjectAssetExplorer(settings, legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/asset-explorer")
    async def asset_explorer(project_id: str) -> dict[str, Any]:
        try:
            return explorer.snapshot(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


__all__ = ["ProjectAssetExplorer", "create_asset_explorer_router"]
