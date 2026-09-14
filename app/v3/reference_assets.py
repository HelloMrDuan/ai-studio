from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from app.services.media_generation_pipeline import MediaGenerationPipeline


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
_ACTIVE = {"queued", "switching_gpu", "running", "generating"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _flatten_metadata(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Serialize project-approved metadata without inventing new story facts."""
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
    """Reusable character/location/prop references through the mature candidate path."""

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
            if role == "character_reference":
                version = ((item.get("metadata") or {}).get("visual_context") or {}).get("appearance_version") or "v1"
                if version != (entity.get("appearance_version") or "v1"):
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
        if entity.get("appearance_version") not in {None, "", "v1"}:
            key += f":appearance:{entity['appearance_version']}"
        rows = [
            item for item in self.director.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == key
            and _clean(item.get("asset_type")).upper() == "IMAGE"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _prompt_asset(self, project_id: str, entity_id: str) -> dict[str, Any] | None:
        key = self._prompt_key(entity_id)
        rows = [
            item for item in self.director.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")).startswith(key)
            and _clean(item.get("asset_type")).upper() in {"TEXT", "STRUCTURED_DATA", "FILE"}
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    @staticmethod
    def _prompt_asset_is_user_edited(prompt_asset: dict[str, Any] | None) -> bool:
        if not isinstance(prompt_asset, dict):
            return False
        source = prompt_asset.get("source") if isinstance(prompt_asset.get("source"), dict) else {}
        metadata = prompt_asset.get("metadata") if isinstance(prompt_asset.get("metadata"), dict) else {}
        return bool(metadata.get("user_edited")) or _clean(source.get("type")) == "manual_reference_prompt"

    def _read_prompt_asset(self, project_id: str, prompt_asset: dict[str, Any] | None) -> str:
        if not isinstance(prompt_asset, dict):
            return ""
        asset_id = _clean(prompt_asset.get("asset_id"))
        if not asset_id:
            return ""
        try:
            return _clean(self.director.production.read_text_asset(project_id, asset_id))
        except Exception:
            return ""

    def _reference_prompt(self, entity: dict[str, Any]) -> str:
        kind = _clean(entity.get("entity_type")).lower()
        label = _REFERENCE_LABEL[kind]
        name = _clean(entity.get("name")) or f"未命名{label}"
        fact_rows = _flatten_metadata(entity.get("metadata") or {})
        seen: set[str] = set()
        facts: list[str] = []
        for row in fact_rows:
            row = _clean(row)
            if row and row not in seen:
                seen.add(row)
                facts.append(row)
        fact_text = "\n".join(f"- {row}" for row in facts[-24:]) or "- 项目当前只确认了名称；外观未确认部分保持中性，不添加剧情动作。"

        if kind == "character":
            format_rule = (
                "生成一张4:3横向角色三视图设定图（character turnaround sheet / model sheet），同一画布四区块："
                "一个明显较大的正面脸部近景，以及并排的正面全身、严格90度侧面全身、背面全身。"
                "三个全身视图必须从头到脚完整可见、尺度接近、中性站姿；四个区块必须是完全同一角色，"
                "不得改变已确认的年龄、脸型、发型发色、体型、肤色、服装、鞋履、配饰或配色。"
                "使用中性浅灰或米白设定稿背景；不要时尚棚拍感、动作姿势、其他人物、字幕、标签或水印。"
            )
        elif kind in {"scene", "location"}:
            format_rule = (
                "生成一张4:3横向可复用场景参考图：正面完整展示空间边界、主要结构、材质、"
                "固定陈设与前中后景关系，并保留至少三个稳定可辨识空间锚点。"
                "这是场景基础身份，不表现某个镜头瞬间，不加入主角或剧情动作，不出现字幕、标注或水印。"
            )
        else:
            format_rule = (
                "生成一张4:3横向可复用道具参考图：只出现一个完整道具，主体居中、无遮挡，"
                "轮廓、结构、材质、颜色、纹样和稳定磨损细节清楚，使用纯净浅色背景。"
                "不出现人物、手部、其他道具、剧情场景、字幕、标注或水印。"
            )

        # Confirmed identity facts intentionally precede layout. The final
        # PromptCompiler preserves the same priority so verbose sheet rules can
        # never crowd character identity/style out of the provider context.
        return (
            f"项目一致性参考资产：{label}「{name}」。\n"
            "最高优先级：严格保持下面已经确认的稳定视觉事实，不得用参考图版式重新设计角色。\n"
            f"项目已确认设定：\n{fact_text}\n\n"
            "参考图布局要求（只控制排版，不覆盖上述身份约束）：\n"
            f"{format_rule}\n"
            "忽略表情、一次性动作、镜头机位和瞬时剧情状态。"
        )

    def _ensure_target_and_prompt(
        self,
        project_id: str,
        entity: dict[str, Any],
        *,
        prompt_override: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entity_id = _clean(entity.get("entity_id"))
        kind = _clean(entity.get("entity_type")).lower()
        name = _clean(entity.get("name")) or _REFERENCE_LABEL[kind]
        role = _REFERENCE_ROLE[kind]
        version = entity.get("appearance_version") or "v1"
        suffix = f":appearance:{version}" if kind == "character" and version != "v1" else ""
        target = self._target(project_id, entity)
        if target is None or _clean(target.get("status")).lower() in {"archived", "superseded"}:
            target = self.director.production.declare_asset(
                project_id,
                stage="03",
                skill="xiaoduan-consistency-reference",
                logical_key=self._logical_key(entity_id) + suffix,
                asset_type="IMAGE",
                asset_role=role,
                name=f"{name} · 一致性参考图",
                status="planned",
                source={"type": "auto_consistency_reference", "entity_id": entity_id},
                parent_asset_ids=[
                    _clean(row.get("source_asset_id")) for row in entity.get("evidence") or []
                    if _clean(row.get("source_asset_id"))
                ],
                entity_ids=[entity_id],
                metadata={
                    "reference_asset": True,
                    "reference_kind": kind,
                    "reference_layout": "character_turnaround_v2" if kind == "character" else f"{kind}_reference_v1",
                    "manual_adoption_required": True,
                    "design_source": "formal_stable_profile",
                    "visual_context": {"appearance_version": version if kind == "character" else ""},
                },
            )

        source_asset_ids = []
        for evidence in entity.get("evidence") or []:
            if isinstance(evidence, dict) and _clean(evidence.get("source_asset_id")):
                source_asset_ids.append(_clean(evidence.get("source_asset_id")))

        requested = _clean(prompt_override)
        current_prompt_asset = self._prompt_asset(project_id, entity_id)
        current_prompt_text = self._read_prompt_asset(project_id, current_prompt_asset)
        if (
            requested
            and current_prompt_asset is not None
            and not self._prompt_asset_is_user_edited(current_prompt_asset)
            and current_prompt_text
            and requested == current_prompt_text
        ):
            requested = ""

        content = requested or self._reference_prompt(entity)
        prompt = self.director.production.create_text_asset(
            project_id,
            stage="03",
            skill="xiaoduan-consistency-reference",
            logical_key=self._prompt_key(entity_id) + suffix,
            asset_role=f"{role}_prompt",
            name=f"{name} · 一致性参考图生成要求",
            content=content,
            asset_type="TEXT",
            extension=".txt",
            source={
                "type": "manual_reference_prompt" if requested else "auto_consistency_reference_prompt",
                "entity_id": entity_id,
            },
            parent_asset_ids=list(dict.fromkeys(source_asset_ids))[-16:],
            entity_ids=[entity_id],
            metadata={
                "reference_asset": True,
                "reference_kind": kind,
                "reference_layout": "character_turnaround_v2" if kind == "character" else f"{kind}_reference_v1",
                "user_edited": bool(requested),
            },
        )
        return target, prompt

    def status(self, project_id: str) -> dict[str, Any]:
        self.director.get_project(project_id)
        items = []
        for entity in self.director.production.list_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            if kind not in _REFERENCE_TYPES:
                continue
            entity_id = _clean(entity.get("entity_id"))
            ready = self._ready_reference(project_id, entity)
            target = self._target(project_id, entity)
            prompt_asset = self._prompt_asset(project_id, entity_id)
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
            prompt_text = self._reference_prompt(entity)
            if prompt_asset is not None and self._prompt_asset_is_user_edited(prompt_asset):
                stored = self._read_prompt_asset(project_id, prompt_asset)
                if stored:
                    prompt_text = stored
            items.append({
                "entity_id": entity_id,
                "entity_type": kind,
                "label": _REFERENCE_LABEL[kind],
                "name": _clean(entity.get("name")),
                "ready": ready is not None,
                "reference_asset_id": _clean((ready or {}).get("asset_id")),
                "reference_url": _clean(((ready or {}).get("storage") or {}).get("url")),
                "target_asset_id": _clean((target or {}).get("asset_id")),
                "prompt_asset_id": _clean((prompt_asset or {}).get("asset_id")),
                "prompt_text": prompt_text,
                "candidate": candidate,
            })
        return {
            "project_id": project_id,
            "items": items,
            "required_count": len(items),
            "ready_count": sum(1 for item in items if item["ready"]),
            "manual_adoption_required": True,
            "upload_required": False,
            "generation_backend": "existing_workbench_txt2img",
            "asset_policy": "character_location_prop_stable_reference_then_manual_adoption",
        }

    async def generate_candidate(
        self,
        project_id: str,
        entity_id: str,
        *,
        force: bool = False,
        prompt_override: str = "",
        appearance_version: str = "v1",
    ) -> dict[str, Any]:
        self.director.get_project(project_id)
        entity = self._entity(project_id, entity_id)
        if appearance_version not in {"", "v1", "default"}:
            entity = self._appearance_entity(project_id, entity, appearance_version)
        ready = self._ready_reference(project_id, entity)
        if ready is not None and not force:
            return {"already_ready": True, "asset": ready, "status": self.status(project_id)}

        target, prompt = self._ensure_target_and_prompt(
            project_id,
            entity,
            prompt_override=prompt_override,
        )
        for row in self._candidate_rows(project_id, _clean(target.get("asset_id"))):
            state = _clean(row.get("status")).lower()
            if not _clean(row.get("confirmed_asset_id")) and state in _ACTIVE and force:
                raise ValueError("当前参考图仍在生成，请完成后再重新生成")
            if not _clean(row.get("confirmed_asset_id")) and state in _PENDING and not force:
                return {"already_pending": True, "candidate": row, "status": self.status(project_id)}

        generation_payload = MediaGenerationPipeline().prepare_candidate(
            self.director.production,
            project_id,
            {
                "target_asset_id": _clean(target.get("asset_id")),
                "capability": "image",
                "mode": "txt2img",
                "prompt_asset_id": _clean(prompt.get("asset_id")),
                "params": {
                    "aspect_ratio": "4:3",
                    "model_key": "smart",
                    "steps": 32,
                    "cfg": 6.5,
                    "seed": -1,
                    "sampler": "dpmpp_2m",
                    "scheduler": "karras",
                    "count": 1,
                    "semantic_compile": "auto",
                },
            },
        )
        response = await self.submit_candidate(project_id, generation_payload)
        return {"submitted": True, **dict(response or {}), "status": self.status(project_id)}

    def _appearance_entity(self, project_id: str, entity: dict[str, Any], version: str) -> dict[str, Any]:
        raise ValueError("当前参考图入口不支持此形象版本")

    async def generate_missing(self, project_id: str) -> dict[str, Any]:
        state = self.status(project_id)
        submitted: list[str] = []
        waiting_adoption: list[str] = []
        for item in state["items"]:
            if item["ready"]:
                continue
            candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
            candidate_state = _clean(candidate.get("status")).lower()
            if candidate_state == "completed":
                waiting_adoption.append(_clean(item.get("entity_id")))
                continue
            if candidate_state in _ACTIVE:
                continue
            await self.generate_candidate(project_id, _clean(item.get("entity_id")))
            submitted.append(_clean(item.get("entity_id")))
        return {
            "submitted_entity_ids": submitted,
            "waiting_adoption_entity_ids": waiting_adoption,
            "status": self.status(project_id),
        }

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

    @router.post("/api/v3/studio/projects/{project_id}/references/generate-missing")
    async def generate_missing_references(project_id: str) -> dict[str, Any]:
        try:
            return await service.generate_missing(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"生成一致性参考图失败：{type(exc).__name__}: {exc}") from exc

    @router.post("/api/v3/studio/projects/{project_id}/references/{entity_id}/generate")
    async def generate_reference(project_id: str, entity_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            body = payload or {}
            return await service.generate_candidate(
                project_id,
                entity_id,
                force=bool(body.get("force")),
                prompt_override=_clean(body.get("prompt_text") or body.get("prompt")),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"生成一致性参考图失败：{type(exc).__name__}: {exc}") from exc

    return router


__all__ = ["ReferenceAssetBootstrap", "create_reference_asset_router"]
