from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ASSET_TYPES = {
    "TEXT", "STRUCTURED_DATA", "IMAGE", "VIDEO", "AUDIO", "FILE", "ENTITY", "COLLECTION"
}
ASSET_STATUSES = {
    "planned", "queued", "generating", "ready", "failed", "superseded", "archived"
}
TASK_STATUS_MAP = {
    "queued": "queued",
    "switching_gpu": "generating",
    "running": "generating",
    "completed": "ready",
    "failed": "failed",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(value: str, default: str = "asset") -> str:
    value = _clean(value).lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._")
    return value[:96] or default


def _asset_type(value: Any) -> str:
    item = _clean(value).upper()
    return item if item in ASSET_TYPES else "FILE"


def _status(value: Any) -> str:
    item = _clean(value).lower()
    return item if item in ASSET_STATUSES else "planned"


def _json_copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return fallback


class ProductionAssetService:
    """Persistent project production graph layered on top of existing files/tasks.

    It never duplicates existing ComfyUI/H3/FaceFusion media. Task outputs stay
    in the current AssetService/TaskStore locations and are referenced by URL.
    Director-produced text/structured artifacts are materialized under the same
    platform data_dir so the existing /files mount serves them.
    """

    schema_version = "production_graph_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "director_production"
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        pid = _clean(project_id)
        if not re.fullmatch(r"[a-f0-9]{24}", pid):
            raise ValueError("非法 project_id")
        path = self.root / pid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _graph_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "graph.json"

    def _empty_graph(self, project_id: str, title: str = "") -> dict[str, Any]:
        now = _utcnow()
        return {
            "schema_version": self.schema_version,
            "project_id": project_id,
            "title": _clean(title),
            "assets": {},
            "logical_assets": {},
            "entities": {},
            "relations": [],
            "stage_asset_ids": {},
            "created_at": now,
            "updated_at": now,
        }

    def _save(self, graph: dict[str, Any]) -> None:
        graph["updated_at"] = _utcnow()
        path = self._graph_path(graph["project_id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)

    def ensure_project(self, project_id: str, title: str = "") -> dict[str, Any]:
        path = self._graph_path(project_id)
        if path.is_file():
            graph = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            if _clean(title) and not _clean(graph.get("title")):
                graph["title"] = _clean(title)
                changed = True
            # Forward-compatible defaults for graphs created by earlier builds.
            for key, default in (
                ("assets", {}), ("logical_assets", {}), ("entities", {}),
                ("relations", []), ("stage_asset_ids", {}),
            ):
                if key not in graph:
                    graph[key] = default
                    changed = True
            if changed:
                self._save(graph)
            return graph
        graph = self._empty_graph(project_id, title)
        self._save(graph)
        return graph

    def get_graph(self, project_id: str) -> dict[str, Any]:
        return self.ensure_project(project_id)

    def _new_asset_id(self) -> str:
        return "ast_" + secrets.token_hex(10)

    def _new_entity_id(self) -> str:
        return "ent_" + secrets.token_hex(10)

    @staticmethod
    def _active_asset_id(graph: dict[str, Any], logical_key: str) -> str:
        logical = (graph.get("logical_assets") or {}).get(logical_key) or {}
        return _clean(logical.get("active_asset_id"))

    def get_asset(self, project_id: str, asset_id: str) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        item = (graph.get("assets") or {}).get(asset_id)
        if not isinstance(item, dict):
            raise FileNotFoundError(f"项目资产不存在：{asset_id}")
        return item

    def list_assets(
        self,
        project_id: str,
        *,
        stage: str = "",
        asset_type: str = "",
        asset_role: str = "",
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        graph = self.ensure_project(project_id)
        items = list((graph.get("assets") or {}).values())
        if stage:
            items = [x for x in items if _clean(x.get("stage")) == _clean(stage)]
        if asset_type:
            wanted = _asset_type(asset_type)
            items = [x for x in items if _clean(x.get("asset_type")).upper() == wanted]
        if asset_role:
            role = _clean(asset_role)
            items = [x for x in items if _clean(x.get("asset_role")) == role]
        if active_only:
            items = [x for x in items if bool(x.get("active"))]
        items.sort(key=lambda x: (_clean(x.get("stage")), _clean(x.get("logical_key")), int(x.get("version") or 0)))
        return items

    def asset_url(self, project_id: str, asset_id: str) -> str:
        item = self.get_asset(project_id, asset_id)
        return _clean((item.get("storage") or {}).get("url"))

    def read_text_asset(self, project_id: str, asset_id: str, max_chars: int = 30000) -> str:
        item = self.get_asset(project_id, asset_id)
        asset_type = _clean(item.get("asset_type")).upper()
        if asset_type not in {"TEXT", "STRUCTURED_DATA", "FILE"}:
            raise ValueError(f"资产不是文本类型：{asset_id}")
        storage = item.get("storage") or {}
        path_value = _clean(storage.get("path"))
        path = Path(path_value) if path_value else None
        if path is None or not path.is_file():
            url = _clean(storage.get("url"))
            if url.startswith("/files/"):
                path = (self.data_dir / url[len("/files/"):]).resolve()
                if self.data_dir.resolve() not in path.parents:
                    raise ValueError("非法项目资产路径")
        if path is None or not path.is_file():
            raise FileNotFoundError(f"文本资产文件不存在：{asset_id}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:max_chars]

    def list_entities(self, project_id: str, entity_type: str = "") -> list[dict[str, Any]]:
        graph = self.ensure_project(project_id)
        items = list((graph.get("entities") or {}).values())
        if entity_type:
            wanted = _clean(entity_type).lower()
            items = [x for x in items if _clean(x.get("entity_type")).lower() == wanted]
        items.sort(key=lambda x: (_clean(x.get("entity_type")), _clean(x.get("name"))))
        return items

    def create_entity(
        self,
        project_id: str,
        *,
        entity_type: str,
        name: str,
        logical_key: str = "",
        stage: str = "",
        skill: str = "",
        metadata: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        etype = _clean(entity_type).lower() or "generic"
        ekey = _clean(logical_key) or f"{etype}:{_slug(name, 'entity')}"
        # Entity keys are stable: later extraction updates the same entity.
        for item in (graph.get("entities") or {}).values():
            if _clean(item.get("logical_key")) == ekey:
                if _clean(name):
                    item["name"] = _clean(name)
                if metadata:
                    item.setdefault("metadata", {}).update(_json_copy(metadata, {}))
                if evidence:
                    item.setdefault("evidence", []).append(_json_copy(evidence, {}))
                item["updated_at"] = _utcnow()
                self._save(graph)
                return item
        now = _utcnow()
        item = {
            "entity_id": self._new_entity_id(),
            "project_id": project_id,
            "entity_type": etype,
            "logical_key": ekey,
            "name": _clean(name) or ekey,
            "stage": _clean(stage),
            "skill": _clean(skill),
            "asset_ids": [],
            "metadata": _json_copy(metadata or {}, {}),
            "evidence": [_json_copy(evidence, {})] if evidence else [],
            "created_at": now,
            "updated_at": now,
        }
        graph["entities"][item["entity_id"]] = item
        self._save(graph)
        return item

    def update_entity(
        self,
        project_id: str,
        entity_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        item = (graph.get("entities") or {}).get(entity_id)
        if not isinstance(item, dict):
            raise FileNotFoundError(f"项目实体不存在：{entity_id}")
        for key in ("name", "stage", "skill"):
            if key in patch:
                item[key] = _clean(patch.get(key))
        if isinstance(patch.get("metadata"), dict):
            item.setdefault("metadata", {}).update(_json_copy(patch["metadata"], {}))
        item["updated_at"] = _utcnow()
        self._save(graph)
        return item

    def _next_version(self, graph: dict[str, Any], logical_key: str) -> tuple[int, str]:
        logical = (graph.get("logical_assets") or {}).get(logical_key) or {}
        versions = [int(x) for x in logical.get("versions") or [] if str(x).isdigit()]
        return (max(versions) + 1 if versions else 1), _clean(logical.get("active_asset_id"))

    def _register_asset(self, graph: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
        asset_id = asset["asset_id"]
        logical_key = asset["logical_key"]
        previous_active = self._active_asset_id(graph, logical_key)
        if previous_active and previous_active in graph["assets"] and previous_active != asset_id:
            old = graph["assets"][previous_active]
            old["active"] = False
            if _clean(old.get("status")) == "ready":
                old["status"] = "superseded"
            old["superseded_by"] = asset_id
            old["updated_at"] = _utcnow()
            # A new upstream version does not destroy downstream outputs; mark
            # them as potentially stale so the UI can surface impact.
            for dep in graph["assets"].values():
                parents = dep.get("parent_asset_ids") or []
                if previous_active in parents and dep.get("active"):
                    dep["dependency_state"] = "stale"
                    stale = dep.setdefault("stale_parent_asset_ids", [])
                    if previous_active not in stale:
                        stale.append(previous_active)
        graph["assets"][asset_id] = asset
        logical = graph["logical_assets"].setdefault(logical_key, {
            "logical_key": logical_key,
            "versions": [],
            "asset_ids": [],
            "active_asset_id": "",
        })
        logical["versions"].append(int(asset["version"]))
        logical["asset_ids"].append(asset_id)
        logical["active_asset_id"] = asset_id
        stage = _clean(asset.get("stage"))
        if stage:
            graph["stage_asset_ids"].setdefault(stage, [])
            if asset_id not in graph["stage_asset_ids"][stage]:
                graph["stage_asset_ids"][stage].append(asset_id)
        for entity_id in asset.get("entity_ids") or []:
            ent = graph["entities"].get(entity_id)
            if ent is not None and asset_id not in ent.setdefault("asset_ids", []):
                ent["asset_ids"].append(asset_id)
                ent["updated_at"] = _utcnow()
        self._save(graph)
        return asset

    def _asset_base(
        self,
        *,
        project_id: str,
        stage: str,
        skill: str,
        asset_type: str,
        asset_role: str,
        logical_key: str,
        name: str,
        version: int,
        status: str,
        source: dict[str, Any] | None,
        parent_asset_ids: list[str] | None,
        entity_ids: list[str] | None,
        metadata: dict[str, Any] | None,
        contract_artifact_id: str = "",
    ) -> dict[str, Any]:
        now = _utcnow()
        return {
            "asset_id": self._new_asset_id(),
            "project_id": project_id,
            "stage": _clean(stage),
            "skill": _clean(skill),
            "asset_type": _asset_type(asset_type),
            "asset_role": _clean(asset_role) or "generic",
            "logical_key": _clean(logical_key),
            "name": _clean(name) or _clean(logical_key),
            "version": int(version),
            "active": True,
            "status": _status(status),
            "dependency_state": "current",
            "stale_parent_asset_ids": [],
            "contract_artifact_id": _clean(contract_artifact_id),
            "storage": {},
            "source": _json_copy(source or {}, {}),
            "parent_asset_ids": list(dict.fromkeys(_clean(x) for x in (parent_asset_ids or []) if _clean(x))),
            "entity_ids": list(dict.fromkeys(_clean(x) for x in (entity_ids or []) if _clean(x))),
            "metadata": _json_copy(metadata or {}, {}),
            "created_at": now,
            "updated_at": now,
        }

    def create_text_asset(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        logical_key: str,
        asset_role: str,
        name: str,
        content: str,
        asset_type: str = "TEXT",
        extension: str = ".md",
        source: dict[str, Any] | None = None,
        parent_asset_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        contract_artifact_id: str = "",
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        logical_key = _clean(logical_key)
        if not logical_key:
            raise ValueError("logical_key 不能为空")
        body = str(content or "")
        if not body.strip():
            raise ValueError("文本资产内容不能为空")
        content_hash = _sha(body)
        current_id = self._active_asset_id(graph, logical_key)
        current = graph["assets"].get(current_id) if current_id else None
        if isinstance(current, dict) and _clean((current.get("metadata") or {}).get("content_sha256")) == content_hash:
            return current
        version, _ = self._next_version(graph, logical_key)
        ext = extension if extension.startswith(".") else "." + extension
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
            ext = ".md"
        rel = Path("director_production") / project_id / "assets" / _slug(logical_key) / f"v{version}" / ("content" + ext.lower())
        path = self.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        mime = mimetypes.guess_type(path.name)[0] or ("application/json" if ext == ".json" else "text/plain")
        merged_meta = _json_copy(metadata or {}, {})
        merged_meta.update({"content_sha256": content_hash, "chars": len(body), "mime_type": mime})
        asset = self._asset_base(
            project_id=project_id, stage=stage, skill=skill, asset_type=asset_type,
            asset_role=asset_role, logical_key=logical_key, name=name, version=version,
            status="ready", source=source, parent_asset_ids=parent_asset_ids,
            entity_ids=entity_ids, metadata=merged_meta, contract_artifact_id=contract_artifact_id,
        )
        asset["storage"] = {
            "kind": "platform_file",
            "path": str(path),
            "url": "/files/" + rel.as_posix(),
            "urls": ["/files/" + rel.as_posix()],
            "mime_type": mime,
        }
        return self._register_asset(graph, asset)

    def declare_asset(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        logical_key: str,
        asset_type: str,
        asset_role: str,
        name: str,
        status: str = "planned",
        source: dict[str, Any] | None = None,
        parent_asset_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        contract_artifact_id: str = "",
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        logical_key = _clean(logical_key)
        if not logical_key:
            raise ValueError("logical_key 不能为空")
        version, _ = self._next_version(graph, logical_key)
        asset = self._asset_base(
            project_id=project_id, stage=stage, skill=skill, asset_type=asset_type,
            asset_role=asset_role, logical_key=logical_key, name=name, version=version,
            status=status, source=source, parent_asset_ids=parent_asset_ids,
            entity_ids=entity_ids, metadata=metadata, contract_artifact_id=contract_artifact_id,
        )
        return self._register_asset(graph, asset)

    def bind_task(
        self,
        project_id: str,
        asset_id: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        asset = graph["assets"].get(asset_id)
        if not isinstance(asset, dict):
            raise FileNotFoundError(f"项目资产不存在：{asset_id}")
        task_id = _clean(task.get("task_id"))
        if not task_id:
            raise ValueError("task 缺少 task_id")
        status_value = task.get("status")
        if hasattr(status_value, "value"):
            status_value = status_value.value
        task_status = _clean(status_value).lower()
        asset["status"] = TASK_STATUS_MAP.get(task_status, asset.get("status") or "planned")
        urls = [_clean(x) for x in task.get("output_files") or [] if _clean(x)]
        asset["source"] = {
            **(asset.get("source") or {}),
            "type": "task",
            "task_id": task_id,
            "module": _clean(task.get("module")),
            "operation": _clean(task.get("operation")),
        }
        if urls:
            asset["storage"] = {
                "kind": "existing_task_output",
                "url": urls[0],
                "urls": urls,
                "task_id": task_id,
            }
        asset.setdefault("metadata", {})["task_progress"] = int(task.get("progress") or 0)
        if task.get("params"):
            asset["metadata"]["task_params"] = _json_copy(task.get("params"), {})
        if _clean(task.get("error")):
            asset["metadata"]["task_error"] = _clean(task.get("error"))
        asset["updated_at"] = _utcnow()
        self._save(graph)
        return asset

    def register_existing_file(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        logical_key: str,
        asset_type: str,
        asset_role: str,
        name: str,
        url: str,
        source: dict[str, Any] | None = None,
        parent_asset_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        contract_artifact_id: str = "",
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        url = _clean(url)
        if not url.startswith("/files/"):
            raise ValueError("现有平台资产必须使用 /files/ URL")
        version, _ = self._next_version(graph, logical_key)
        asset = self._asset_base(
            project_id=project_id, stage=stage, skill=skill, asset_type=asset_type,
            asset_role=asset_role, logical_key=logical_key, name=name, version=version,
            status="ready", source=source, parent_asset_ids=parent_asset_ids,
            entity_ids=entity_ids, metadata=metadata, contract_artifact_id=contract_artifact_id,
        )
        asset["storage"] = {"kind": "existing_platform_file", "url": url, "urls": [url]}
        return self._register_asset(graph, asset)

    def fork_asset_version(
        self,
        project_id: str,
        asset_id: str,
        *,
        status: str = "planned",
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_asset(project_id, asset_id)
        merged_source = _json_copy(current.get("source") or {}, {})
        merged_source.update(_json_copy(source or {}, {}))
        merged_source["previous_asset_id"] = asset_id
        metadata = _json_copy(current.get("metadata") or {}, {})
        metadata.pop("task_progress", None)
        metadata.pop("task_error", None)
        metadata.pop("task_params", None)
        return self.declare_asset(
            project_id,
            stage=_clean(current.get("stage")),
            skill=_clean(current.get("skill")),
            logical_key=_clean(current.get("logical_key")),
            asset_type=_clean(current.get("asset_type")) or "FILE",
            asset_role=_clean(current.get("asset_role")) or "project_asset",
            name=_clean(current.get("name")) or "项目资产",
            status=status,
            source=merged_source,
            parent_asset_ids=list(current.get("parent_asset_ids") or []),
            entity_ids=list(current.get("entity_ids") or []),
            metadata=metadata,
            contract_artifact_id=_clean(current.get("contract_artifact_id")),
        )

    def set_asset_dependencies(
        self,
        project_id: str,
        asset_id: str,
        parent_asset_ids: list[str] | None,
        *,
        merge: bool = True,
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        asset = graph["assets"].get(asset_id)
        if not isinstance(asset, dict):
            raise FileNotFoundError(f"项目资产不存在：{asset_id}")
        incoming = list(dict.fromkeys(
            _clean(x) for x in (parent_asset_ids or []) if _clean(x)
        ))
        for parent_id in incoming:
            if parent_id == asset_id:
                raise ValueError("项目资产不能依赖自身")
            if parent_id not in graph["assets"]:
                raise FileNotFoundError(f"上游项目资产不存在：{parent_id}")
        current = list(asset.get("parent_asset_ids") or []) if merge else []
        parents = list(dict.fromkeys([*current, *incoming]))
        asset["parent_asset_ids"] = parents
        stale = []
        for parent_id in parents:
            parent = graph["assets"].get(parent_id) or {}
            logical_key = _clean(parent.get("logical_key"))
            active_id = self._active_asset_id(graph, logical_key) if logical_key else ""
            if active_id and active_id != parent_id:
                stale.append(parent_id)
        asset["stale_parent_asset_ids"] = stale
        asset["dependency_state"] = "stale" if stale else "current"
        asset["updated_at"] = _utcnow()
        self._save(graph)
        return asset

    def ensure_contract_placeholders(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        contract: dict[str, Any],
        runtime_state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Materialize required non-text contract outputs as planned asset slots.

        This is generic control-plane behavior: the business meaning and role
        are taken from the Skill Contract; the runtime only creates versioned
        project slots that can later bind an existing platform task/file.
        """
        graph = self.ensure_project(project_id)
        required_ids = set(
            _clean(x)
            for x in ((runtime_state.get("completion") or {}).get("required_artifact_ids") or [])
            if _clean(x)
        )
        if not required_ids:
            return []
        specs = {
            _clean(spec.get("artifact_id")): spec
            for group in (contract.get("output_groups") or [])
            for spec in (group.get("artifacts") or [])
            if _clean(spec.get("artifact_id"))
        }
        created = []
        for aid in sorted(required_ids):
            spec = specs.get(aid) or {}
            asset_type = _asset_type(spec.get("asset_type") or "TEXT")
            materialization = _clean(spec.get("materialization")).lower() or "text"
            if asset_type in {"TEXT", "STRUCTURED_DATA"} and materialization not in {
                "task_output", "external_file", "media", "file"
            }:
                continue
            existing = [
                item for item in graph["assets"].values()
                if item.get("active")
                and _clean(item.get("stage")) == _clean(stage)
                and _clean(item.get("contract_artifact_id")) == aid
            ]
            if existing:
                continue
            logical_key = f"contract:{_clean(stage)}:{aid}"
            item = self.declare_asset(
                project_id,
                stage=stage,
                skill=skill,
                logical_key=logical_key,
                asset_type=asset_type,
                asset_role=_clean(spec.get("asset_role")) or "skill_artifact",
                name=_clean(spec.get("name")) or aid,
                status="planned",
                source={
                    "type": "skill_contract_slot",
                    "producer_capability": _clean(spec.get("producer_capability")),
                    "materialization": materialization,
                },
                metadata={
                    "contract_artifact_id": aid,
                    "cardinality_min": int(spec.get("cardinality_min") or 1),
                    "cardinality_max": spec.get("cardinality_max"),
                    "source_quote": _clean(spec.get("source_quote")),
                },
                contract_artifact_id=aid,
            )
            created.append(item)
            graph = self.ensure_project(project_id)
        return created

    def set_active_version(self, project_id: str, asset_id: str) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        asset = graph["assets"].get(asset_id)
        if not isinstance(asset, dict):
            raise FileNotFoundError(f"项目资产不存在：{asset_id}")
        logical_key = _clean(asset.get("logical_key"))
        logical = graph["logical_assets"].get(logical_key) or {}
        previous_id = _clean(logical.get("active_asset_id"))
        if previous_id and previous_id in graph["assets"] and previous_id != asset_id:
            previous = graph["assets"][previous_id]
            previous["active"] = False
            if _clean(previous.get("status")) == "ready":
                previous["status"] = "superseded"
            previous["superseded_by"] = asset_id
            previous["updated_at"] = _utcnow()
        asset["active"] = True
        if _clean(asset.get("status")) == "superseded":
            asset["status"] = "ready"
        asset["superseded_by"] = ""
        asset["updated_at"] = _utcnow()
        logical["active_asset_id"] = asset_id
        graph["logical_assets"][logical_key] = logical
        self._save(graph)
        return asset

    def archive_asset(self, project_id: str, asset_id: str) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        asset = graph["assets"].get(asset_id)
        if not isinstance(asset, dict):
            raise FileNotFoundError(f"项目资产不存在：{asset_id}")
        asset["status"] = "archived"
        asset["active"] = False
        asset["updated_at"] = _utcnow()
        logical = graph["logical_assets"].get(_clean(asset.get("logical_key"))) or {}
        if _clean(logical.get("active_asset_id")) == asset_id:
            logical["active_asset_id"] = ""
        self._save(graph)
        return asset

    def add_relation(
        self,
        project_id: str,
        *,
        source_id: str,
        target_id: str,
        relation_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        relation = {
            "relation_id": "rel_" + secrets.token_hex(8),
            "source_id": _clean(source_id),
            "target_id": _clean(target_id),
            "relation_type": _clean(relation_type) or "related_to",
            "metadata": _json_copy(metadata or {}, {}),
            "created_at": _utcnow(),
        }
        for item in graph["relations"]:
            if all(_clean(item.get(k)) == relation[k] for k in ("source_id", "target_id", "relation_type")):
                return item
        graph["relations"].append(relation)
        self._save(graph)
        return relation

    def materialize_turn_output(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        turn_id: str,
        content: str,
        native_target: dict[str, Any] | None = None,
        parent_asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        target = native_target or {}
        target_name = _clean(target.get("name")) or _clean(target.get("kind")) or "turn"
        return self.create_text_asset(
            project_id,
            stage=stage,
            skill=skill,
            logical_key=f"stage:{stage}:turn:{turn_id}",
            asset_role="director_turn_output",
            name=f"Stage {stage} · {target_name}",
            content=content,
            source={"type": "director_turn", "turn_id": turn_id, "native_target": _json_copy(target, {})},
            parent_asset_ids=parent_asset_ids,
            metadata={"native_target": _json_copy(target, {})},
        )

    def materialize_contract_receipts(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        contract: dict[str, Any],
        runtime_state: dict[str, Any],
        turn_asset_id: str,
    ) -> tuple[dict[str, Any], dict[str, list[str]]]:
        graph = self.ensure_project(project_id)
        registry = runtime_state.get("artifact_registry") or {}
        readiness: dict[str, list[str]] = {}
        specs: dict[str, dict[str, Any]] = {}
        for group in contract.get("output_groups") or []:
            for spec in group.get("artifacts") or []:
                aid = _clean(spec.get("artifact_id"))
                if aid:
                    specs[aid] = spec
        for aid, spec in specs.items():
            # Existing active materialized assets remain valid and support
            # task/file artifacts that are generated outside the LLM turn.
            existing = [
                item for item in graph["assets"].values()
                if item.get("active")
                and _clean(item.get("stage")) == _clean(stage)
                and _clean(item.get("contract_artifact_id")) == aid
                and _clean(item.get("status")) == "ready"
                and _clean(item.get("dependency_state")) != "stale"
            ]
            if existing:
                readiness[aid] = [x["asset_id"] for x in existing]
            receipt = registry.get(aid) or {}
            if not bool(receipt.get("verified")):
                continue
            materialization = _clean(spec.get("materialization")).lower() or "text"
            asset_type = _asset_type(spec.get("asset_type") or "TEXT")
            # A text receipt cannot stand in for a required real media/task
            # output. It is still persisted as evidence, but completion waits
            # for a bound READY asset with the same contract_artifact_id.
            if materialization in {"task_output", "external_file", "media"} or asset_type in {"IMAGE", "VIDEO", "AUDIO"}:
                evidence_asset = self.create_text_asset(
                    project_id,
                    stage=stage,
                    skill=skill,
                    logical_key=f"contract-evidence:{stage}:{aid}:{receipt.get('content_sha256','')[:12]}",
                    asset_role="contract_evidence",
                    name=f"{_clean(spec.get('name')) or aid} · evidence",
                    content=_clean(receipt.get("evidence_quote")),
                    source={"type": "skill_runtime_receipt", "turn_id": receipt.get("turn_id"), "artifact_id": aid},
                    parent_asset_ids=[turn_asset_id],
                    metadata={"contract_artifact_id": aid, "not_completion_asset": True},
                )
                receipt["evidence_asset_id"] = evidence_asset["asset_id"]
                continue
            ext = _clean(spec.get("file_extension")) or ".md"
            if asset_type == "STRUCTURED_DATA" and not ext:
                ext = ".json"
            asset = self.create_text_asset(
                project_id,
                stage=stage,
                skill=skill,
                logical_key=f"contract:{stage}:{aid}",
                asset_role=_clean(spec.get("asset_role")) or "skill_artifact",
                name=_clean(spec.get("name")) or aid,
                content=_clean(receipt.get("evidence_quote")),
                asset_type=asset_type if asset_type in {"TEXT", "STRUCTURED_DATA", "FILE"} else "TEXT",
                extension=ext,
                source={"type": "skill_runtime_receipt", "turn_id": receipt.get("turn_id"), "artifact_id": aid},
                parent_asset_ids=[turn_asset_id],
                metadata={"contract_artifact_id": aid, "source_quote": spec.get("source_quote", "")},
                contract_artifact_id=aid,
            )
            receipt["production_asset_ids"] = [asset["asset_id"]]
            receipt["materialized"] = True
            readiness[aid] = [asset["asset_id"]]
        runtime_state["artifact_registry"] = registry
        return runtime_state, readiness

    def contract_asset_readiness(
        self,
        project_id: str,
        stage: str,
        contract: dict[str, Any],
    ) -> dict[str, list[str]]:
        graph = self.ensure_project(project_id)
        valid_ids = {
            _clean(spec.get("artifact_id"))
            for group in contract.get("output_groups") or []
            for spec in group.get("artifacts") or []
            if _clean(spec.get("artifact_id"))
        }
        out: dict[str, list[str]] = {aid: [] for aid in valid_ids}
        for item in graph["assets"].values():
            aid = _clean(item.get("contract_artifact_id"))
            if aid not in valid_ids:
                continue
            if (
                item.get("active")
                and _clean(item.get("stage")) == _clean(stage)
                and _clean(item.get("status")) == "ready"
                and _clean(item.get("dependency_state")) != "stale"
            ):
                out[aid].append(item["asset_id"])
        return out

    def record_control_entities(
        self,
        project_id: str,
        *,
        stage: str,
        skill: str,
        content: str,
        turn_id: str,
        raw_entities: list[Any] | None,
        turn_asset_id: str,
    ) -> list[dict[str, Any]]:
        result = []
        for raw in raw_entities or []:
            if not isinstance(raw, dict):
                continue
            quote = _clean(raw.get("evidence_quote"))
            name = _clean(raw.get("name"))
            if not quote or quote not in content or not name:
                continue
            entity = self.create_entity(
                project_id,
                entity_type=_clean(raw.get("entity_type")) or "generic",
                logical_key=_clean(raw.get("logical_key")),
                name=name,
                stage=stage,
                skill=skill,
                metadata=_json_copy(raw.get("metadata") or {}, {}),
                evidence={"turn_id": turn_id, "evidence_quote": quote, "source_asset_id": turn_asset_id},
            )
            result.append(entity)
        return result

    def impact(self, project_id: str, asset_id: str) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        if asset_id not in graph["assets"]:
            raise FileNotFoundError(f"项目资产不存在：{asset_id}")
        direct = [
            x for x in graph["assets"].values()
            if asset_id in (x.get("parent_asset_ids") or [])
        ]
        relation_targets = [
            r for r in graph["relations"] if _clean(r.get("source_id")) == asset_id
        ]
        return {
            "asset_id": asset_id,
            "direct_downstream_assets": direct,
            "relations": relation_targets,
        }

    def stage_status(self, project_id: str, stage: str) -> dict[str, Any]:
        graph = self.ensure_project(project_id)
        ids = list((graph.get("stage_asset_ids") or {}).get(_clean(stage)) or [])
        items = [graph["assets"][aid] for aid in ids if aid in graph["assets"]]
        active = [x for x in items if x.get("active")]
        return {
            "stage": _clean(stage),
            "asset_count": len(items),
            "active_asset_count": len(active),
            "ready_asset_count": sum(1 for x in active if _clean(x.get("status")) == "ready"),
            "stale_asset_count": sum(1 for x in active if _clean(x.get("dependency_state")) == "stale"),
            "asset_ids": [x["asset_id"] for x in active],
        }

    def context_manifest(
        self,
        project_id: str,
        *,
        stages: list[str] | None = None,
        max_chars: int = 5000,
    ) -> str:
        graph = self.ensure_project(project_id)
        stage_set = {_clean(x) for x in (stages or []) if _clean(x)}
        rows = []
        for item in graph["assets"].values():
            if not item.get("active") or _clean(item.get("status")) != "ready":
                continue
            if _clean(item.get("dependency_state")) == "stale":
                continue
            if stage_set and _clean(item.get("stage")) not in stage_set:
                continue
            rows.append({
                "asset_id": item["asset_id"],
                "stage": item.get("stage"),
                "asset_type": item.get("asset_type"),
                "asset_role": item.get("asset_role"),
                "name": item.get("name"),
                "version": item.get("version"),
                "url": (item.get("storage") or {}).get("url", ""),
                "entity_ids": item.get("entity_ids") or [],
            })
        text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text
