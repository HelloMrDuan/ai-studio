from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException


SubmitCandidate = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

_REFERENCE_ROLE = {
    "character": "character_reference",
    "scene": "scene_reference",
    "location": "location_reference",
    "prop": "prop_reference",
}
_REFERENCE_LABEL = {
    "character": "角色",
    "scene": "场景",
    "location": "场景",
    "prop": "道具",
}
_REFERENCE_TYPES = set(_REFERENCE_ROLE)
_PENDING = {"queued", "switching_gpu", "running", "generating", "completed"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _flatten_metadata(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Serialize only already-approved visible facts; never invent story facts."""
    if depth > 2:
        return []
    rows: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_metadata(item, name, depth + 1))
    elif isinstance(value, list):
        parts = [_clean(item) for item in value if not isinstance(item, (dict, list)) and _clean(item)]
        if parts and prefix:
            rows.append(f"{prefix}：{'；'.join(parts[:16])}")
    elif value is not None and _clean(value) and prefix:
        rows.append(f"{prefix}：{_clean(value)}")
    return rows


class ReferenceAssetBootstrap:
    """Build reusable consistency references through the mature V2 txt2img candidate path.

    The architecture follows the useful part of waoowaoo's asset-development
    model: stable character/location/prop references are separate project assets
    and downstream shots consume adopted versions. The actual media submission,
    candidate lifecycle and manual adoption are deliberately reused from the
    existing workbench instead of introducing a second image generator.
    """

    def __init__(self, legacy_runtime: Any, submit_candidate: SubmitCandidate | None = None) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.submit_candidate = submit_candidate or legacy_runtime.director_workbench_execute_candidate

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for item in self.director.production.list_entities(project_id):
            if _clean(item.get("entity_id")) == _clean(entity_id):
                if _clean(item.get("entity_type")).lower() not in _REFERENCE_TYPES:
                    raise ValueError("当前故事元素不需要独立一致性参考图")
                return item
        raise FileNotFoundError(f"故事元素不存在：{entity_id}")

    @staticmethod
    def _logical_key(entity_id: str) -> str:
        return f"studio:reference:{entity_id}:image"

    @staticmethod
    def _prompt_key(entity_id: str) -> str:
        return f"studio:reference:{entity_id}:prompt"

    def _ready_reference(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        entity_id = _clean(entity.get("entity_id"))
        role = _REFERENCE_ROLE[_clean(entity.get("entity_type")).lower()]
        rows = []
        for item in self.director.production.list_assets(project_id, active_only=True):
            if _clean(item.get("asset_type")).upper() != "IMAGE":
                continue
            if _clean(item.get("asset_role")) != role:
                continue
            if entity_id not in {_clean(value) for value in item.get("entity_ids") or []}:
                continue
            if _clean(item.get("status")).lower() != "ready":
                continue
            if _clean(item.get("dependency_state")).lower() == "stale":
                continue
            rows.append(item)
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _candidate_rows(self, project_id: str, target_asset_id: str = "") -> list[dict[str, Any]]:
        sync = getattr(self.legacy, "_wb_sync_candidates", None)
        load = getattr(self.legacy, "_wb_load_candidates", None)
        rows = sync(project_id) if callable(sync) else load(project_id) if callable(load) else []
        result = [dict(item) for item in rows or []]
        if target_asset_id:
            result = [item for item in result if _clean(item.get("target_asset_id")) == target_asset_id]
        result.sort(key=lambda item: _clean(item.get("updated_at") or item.get("created_at")), reverse=True)
        return result

    def _target(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        key = self._logical_key(_clean(entity.get("entity_id")))
        rows = [
            item for item in self.director.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == key
            and _clean(item.get("asset_type")).upper() == "IMAGE"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _reference_prompt(self, entity: dict[str, Any]) -> str:
        kind = _clean(entity.get("entity_type")).lower()
        label = _REFERENCE_LABEL[kind]
        name = _clean(entity.get("name")) or f"未命名{label}"
        fact_rows = _flatten_metadata(entity.get("metadata") or {})
        for evidence in entity.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            quote = _clean(evidence.get("evidence_quote"))
            if quote:
                fact_rows.append(f"已确认描述：{quote}")
        # Stable de-duplication preserves the project-approved order.
        seen: set[str] = set()
        facts = []
        for row in fact_rows:
            row = _clean(row)
            if row and row not in seen:
                seen.add(row)
                facts.append(row)
        fact_text = "\n".join(f"- {row}" for row in facts[-24:]) or "- 仅使用名称和项目现有设定；未明确细节保持中性，不添加剧情事实。"

        if kind == "character":
            format_rule = (
                "制作同一角色的一张稳定身份参考图：画面同时包含清晰面部近景和无遮挡全身，"
                "服装、发型、体型、五官与配色保持一致；中性姿态和干净背景；"
                "不加入剧情动作、其他人物、字幕、标注和水印。"
            )
        elif kind in {"scene", "location"}:
            format_rule = (
                "制作可复用场景参考图：完整展示空间结构、主要材质、固定陈设和前中后景关系；"
                "保持空间可辨识，避免剧情人物和一次性动作；不加入字幕、标注和水印。"
            )
        else:
            format_rule = (
                "制作可复用道具参考图：只展示一个完整道具，轮廓、材质、颜色和稳定细节清楚；"
                "主体居中、干净背景，不出现人物、手部、剧情场景、字幕、标注和水印。"
            )

        return (
            f"项目一致性参考资产：{label}「{name}」。\n"
            "严格依据下面已经写入项目的事实，不改变身份，不补造剧情。\n"
            f"{format_rule}\n\n"
            f"项目已确认事实：\n{fact_text}"
        )

    def _ensure_target_and_prompt(
        self,
        project_id: str,
        entity: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entity_id = _clean(entity.get("entity_id"))
        kind = _clean(entity.get("entity_type")).lower()
        name = _clean(entity.get("name")) or _REFERENCE_LABEL[kind]
        role = _REFERENCE_ROLE[kind]
        target = self._target(project_id, entity)
        if target is None or _clean(target.get("status")).lower() in {"archived", "superseded"}:
            target = self.director.production.declare_asset(
                project_id,
                stage="03",
                skill="xiaoduan-consistency-reference",
                logical_key=self._logical_key(entity_id),
                asset_type="IMAGE",
                asset_role=role,
                name=f"{name} · 一致性参考图",
                status="planned",
                source={"type": "auto_consistency_reference", "entity_id": entity_id},
                parent_asset_ids=[],
                entity_ids=[entity_id],
                metadata={
                    "reference_asset": True,
                    "reference_kind": kind,
                    "manual_adoption_required": True,
                    "design_source": "project_entity_facts",
                },
            )

        source_asset_ids = []
        for evidence in entity.get("evidence") or []:
            if isinstance(evidence, dict) and _clean(evidence.get("source_asset_id")):
                source_asset_ids.append(_clean(evidence.get("source_asset_id")))
        prompt = self.director.production.create_text_asset(
            project_id,
            stage="03",
            skill="xiaoduan-consistency-reference",
            logical_key=self._prompt_key(entity_id),
            asset_role=f"{role}_prompt",
            name=f"{name} · 一致性参考图提示词",
            content=self._reference_prompt(entity),
            asset_type="TEXT",
            extension=".txt",
            source={"type": "auto_consistency_reference_prompt", "entity_id": entity_id},
            parent_asset_ids=list(dict.fromkeys(source_asset_ids))[-16:],
            entity_ids=[entity_id],
            metadata={"reference_asset": True, "reference_kind": kind},
        )
        return target, prompt

    def status(self, project_id: str) -> dict[str, Any]:
        self.director.get_project(project_id)
        items = []
        for entity in self.director.production.list_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            if kind not in _REFERENCE_TYPES:
                continue
            ready = self._ready_reference(project_id, entity)
            target = self._target(project_id, entity)
            candidate = None
            if target is not None:
                candidate = next(
                    (
                        row for row in self._candidate_rows(project_id, _clean(target.get("asset_id")))
                        if not _clean(row.get("confirmed_asset_id"))
                        and _clean(row.get("status")).lower() not in {"rejected", "failed"}
                    ),
                    None,
                )
            items.append(
                {
                    "entity_id": _clean(entity.get("entity_id")),
                    "entity_type": kind,
                    "label": _REFERENCE_LABEL[kind],
                    "name": _clean(entity.get("name")),
                    "ready": ready is not None,
                    "reference_asset_id": _clean((ready or {}).get("asset_id")),
                    "reference_url": _clean(((ready or {}).get("storage") or {}).get("url")),
                    "target_asset_id": _clean((target or {}).get("asset_id")),
                    "candidate": candidate,
                }
            )
        return {
            "project_id": project_id,
            "items": items,
            "required_count": len(items),
            "ready_count": sum(1 for item in items if item["ready"]),
            "manual_adoption_required": True,
            "upload_required": False,
            "generation_backend": "existing_workbench_txt2img",
        }

    async def generate_candidate(
        self,
        project_id: str,
        entity_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        self.director.get_project(project_id)
        entity = self._entity(project_id, entity_id)
        ready = self._ready_reference(project_id, entity)
        if ready is not None and not force:
            return {"already_ready": True, "asset": ready, "status": self.status(project_id)}

        target, prompt = self._ensure_target_and_prompt(project_id, entity)
        for row in self._candidate_rows(project_id, _clean(target.get("asset_id"))):
            state = _clean(row.get("status")).lower()
            if not _clean(row.get("confirmed_asset_id")) and state in _PENDING and not force:
                return {"already_pending": True, "candidate": row, "status": self.status(project_id)}

        response = await self.submit_candidate(
            project_id,
            {
                "target_asset_id": _clean(target.get("asset_id")),
                "capability": "image",
                "mode": "txt2img",
                "prompt_asset_id": _clean(prompt.get("asset_id")),
                "params": {
                    "aspect_ratio": "4:3",
                    "model_key": "smart",
                    "style_name": "portrait_photo",
                    "style_strength": "standard",
                    "steps": 32,
                    "cfg": 6.5,
                    "seed": -1,
                    "sampler": "dpmpp_2m",
                    "scheduler": "karras",
                    "count": 1,
                    "semantic_compile": False,
                },
            },
        )
        return {"submitted": True, **dict(response or {}), "status": self.status(project_id)}

    async def generate_first_missing_for_entities(
        self,
        project_id: str,
        entity_ids: list[str],
    ) -> dict[str, Any] | None:
        wanted = {_clean(value) for value in entity_ids if _clean(value)}
        entities = [
            item for item in self.director.production.list_entities(project_id)
            if _clean(item.get("entity_id")) in wanted
            and _clean(item.get("entity_type")).lower() in _REFERENCE_TYPES
        ]
        priority = {"character": 0, "scene": 1, "location": 1, "prop": 2}
        entities.sort(key=lambda item: (priority.get(_clean(item.get("entity_type")).lower(), 9), _clean(item.get("name"))))
        for entity in entities:
            if self._ready_reference(project_id, entity) is None:
                return await self.generate_candidate(project_id, _clean(entity.get("entity_id")))
        return None


def create_reference_asset_router(legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = ReferenceAssetBootstrap(legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/references")
    async def reference_status(project_id: str) -> dict[str, Any]:
        try:
            return service.status(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/references/{entity_id}/generate")
    async def generate_reference(project_id: str, entity_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await service.generate_candidate(
                project_id,
                entity_id,
                force=bool((payload or {}).get("force")),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            # Preserve an existing route HTTPException (the mature candidate
            # submitter uses it for exact production errors).
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"生成一致性参考图失败：{type(exc).__name__}: {exc}") from exc

    return router


__all__ = ["ReferenceAssetBootstrap", "create_reference_asset_router"]
