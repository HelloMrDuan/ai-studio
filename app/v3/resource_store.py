from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ResourceState, ResourceVersion


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


class ResourceStoreError(RuntimeError):
    pass


class ResourceStore:
    """V3 candidate/adoption store.

    A newly generated version is never made canonical merely because generation
    returned successfully. Adoption is the only operation that changes the
    canonical pointer for a logical resource.
    """

    schema_version = "xiaoduan_resource_store_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        pid = _clean(project_id)
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,128}", pid):
            raise ValueError("invalid V3 project_id")
        path = self.root / pid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "resources.json"

    def _empty(self, project_id: str) -> dict[str, Any]:
        now = _utcnow()
        return {
            "schema_version": self.schema_version,
            "project_id": project_id,
            "resources": {},
            "logical_resources": {},
            "created_at": now,
            "updated_at": now,
        }

    def _load(self, project_id: str) -> dict[str, Any]:
        path = self._path(project_id)
        if not path.is_file():
            data = self._empty(project_id)
            self._save(data)
            return data
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != self.schema_version:
            raise ResourceStoreError("unsupported V3 resource store schema")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _utcnow()
        path = self._path(data["project_id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _new_resource_id() -> str:
        return "res_" + secrets.token_hex(10)

    @staticmethod
    def _next_version(logical: dict[str, Any]) -> int:
        versions = [int(v) for v in logical.get("versions") or []]
        return max(versions) + 1 if versions else 1

    def create_candidate(
        self,
        project_id: str,
        *,
        logical_key: str,
        generation_task_id: str,
        provider_id: str,
        model_id: str,
        reference_ids: list[str] | tuple[str, ...] = (),
        prompt_contract_version: str | None = None,
        continuity_version: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = _clean(logical_key)
        if not key:
            raise ValueError("logical_key is required")
        data = self._load(project_id)
        logical = data["logical_resources"].setdefault(
            key,
            {"logical_key": key, "versions": [], "resource_ids": [], "adopted_resource_id": ""},
        )
        version = self._next_version(logical)
        resource_id = self._new_resource_id()
        record = ResourceVersion(
            resource_id=resource_id,
            version=version,
            parent_version=(version - 1 if version > 1 else None),
            state=ResourceState.generated,
            generation_task_id=_clean(generation_task_id),
            provider_id=_clean(provider_id),
            model_id=_clean(model_id),
            prompt_contract_version=_clean(prompt_contract_version) or None,
            continuity_version=continuity_version,
            reference_ids=tuple(dict.fromkeys(_clean(ref) for ref in reference_ids if _clean(ref))),
            metadata=dict(metadata or {}),
        ).model_dump(mode="json")
        record.update({"logical_key": key, "created_at": _utcnow(), "adopted_at": None})
        data["resources"][resource_id] = record
        logical["versions"].append(version)
        logical["resource_ids"].append(resource_id)
        self._save(data)
        return dict(record)

    def set_audit_result(
        self,
        project_id: str,
        resource_id: str,
        *,
        passed: bool,
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._load(project_id)
        record = data["resources"].get(resource_id)
        if not isinstance(record, dict):
            raise ResourceStoreError(f"unknown resource_id: {resource_id}")
        if record["state"] not in {ResourceState.generated.value, ResourceState.audit_failed.value}:
            raise ResourceStoreError(f"audit not allowed from state {record['state']}")
        record["state"] = ResourceState.candidate_ready.value if passed else ResourceState.audit_failed.value
        record.setdefault("metadata", {})["visual_audit"] = dict(audit)
        record["updated_at"] = _utcnow()
        self._save(data)
        return dict(record)

    def adopt(self, project_id: str, resource_id: str) -> dict[str, Any]:
        data = self._load(project_id)
        record = data["resources"].get(resource_id)
        if not isinstance(record, dict):
            raise ResourceStoreError(f"unknown resource_id: {resource_id}")
        if record["state"] != ResourceState.candidate_ready.value:
            raise ResourceStoreError("only candidate_ready resource can be adopted")
        logical = data["logical_resources"][record["logical_key"]]
        previous_id = _clean(logical.get("adopted_resource_id"))
        if previous_id and previous_id != resource_id:
            previous = data["resources"].get(previous_id)
            if isinstance(previous, dict) and previous.get("state") == ResourceState.adopted.value:
                previous["state"] = ResourceState.superseded.value
                previous["superseded_by"] = resource_id
                previous["updated_at"] = _utcnow()
        record["state"] = ResourceState.adopted.value
        record["adopted_at"] = _utcnow()
        record["updated_at"] = _utcnow()
        logical["adopted_resource_id"] = resource_id
        self._save(data)
        return dict(record)

    def adopted(self, project_id: str, logical_key: str) -> dict[str, Any] | None:
        data = self._load(project_id)
        logical = data["logical_resources"].get(logical_key) or {}
        resource_id = _clean(logical.get("adopted_resource_id"))
        if not resource_id:
            return None
        record = data["resources"].get(resource_id)
        return dict(record) if isinstance(record, dict) else None

    def list_versions(self, project_id: str, logical_key: str) -> list[dict[str, Any]]:
        data = self._load(project_id)
        logical = data["logical_resources"].get(logical_key) or {}
        return [
            dict(data["resources"][resource_id])
            for resource_id in logical.get("resource_ids") or []
            if resource_id in data["resources"]
        ]
