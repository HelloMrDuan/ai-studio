from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import MethodType
from typing import Any

from app.services.director import DirectorService

from . import front_half_quality_gate as gate
from . import story_source_coverage as coverage
from .professional_output_registry import (
    asset_role_for_output_kind,
    output_kind_for_skill,
    output_kind_for_stage,
    parse_professional_output,
    professional_output_json_schema,
    validate_professional_output,
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", _clean(value)).casefold()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class ProfessionalOutputStore:
    """Content-addressed bridge between the one writer and deterministic runtime.

    The LLM writes one strict professional object. Existing Director UI still
    displays that object's ``document`` field, so this sidecar lets later local
    runtime steps recover the exact object from the displayed document without
    asking another model to interpret it.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "professional-output-by-document"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, document: str) -> Path:
        return self.root / f"{_sha(document)}.json"

    def put(self, payload: dict[str, Any]) -> None:
        document = _clean(payload.get("document"))
        if not document:
            raise ValueError("专业输出缺少 document")
        path = self._path(document)
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if path.is_file():
            try:
                if json.loads(path.read_text(encoding="utf-8")) == payload:
                    return
            except Exception:
                pass
        temp = path.with_suffix(".tmp")
        temp.write_text(body, encoding="utf-8")
        temp.replace(path)

    def get_by_document(self, document: str) -> dict[str, Any] | None:
        path = self._path(document)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None


def _extract_execution_content(messages: list[dict[str, str]]) -> str:
    text = "\n".join(_clean(item.get("content")) for item in messages if isinstance(item, dict))
    marker = "=== EXECUTION_CONTENT ==="
    pos = text.rfind(marker)
    if pos < 0:
        return ""
    return text[pos + len(marker):].strip()


def _required_names(output_kind: str, source_text: str) -> dict[str, list[str]]:
    if output_kind == "story_bible":
        return {"characters": coverage.infer_source_character_candidates(source_text)}
    if output_kind == "character_assets":
        names = coverage.extract_story_table_names(source_text, "角色实体表")
        for name in coverage.infer_source_character_candidates(source_text):
            if name not in names:
                names.append(name)
        return {"characters": names}
    if output_kind == "visual_assets":
        return {
            "locations": coverage.extract_story_table_names(source_text, "地点实体表"),
            "props": coverage.extract_story_table_names(source_text, "道具实体表"),
        }
    return {}


def _document_issues(payload: dict[str, Any]) -> list[str]:
    """Human-readable document quality, separate from machine identity shape."""
    output_kind = _clean(payload.get("output_kind"))
    document = _clean(payload.get("document"))
    issues: list[str] = []
    if output_kind == "story_bible":
        issues.extend(gate._validate_stage01(document))
    elif output_kind == "character_assets":
        if "角色" not in document:
            issues.append("角色专业结果 document 缺少角色设计正文")
    elif output_kind == "visual_assets":
        for token in ("视觉", "场景", "道具"):
            if token not in document:
                issues.append(f"视觉专业结果 document 缺少：{token}")
    # New output never duplicates a second machine template inside Markdown.
    for old_block in (
        "```story-entities-json",
        "```appearance-versions-json",
        "```visual-direction-json",
    ):
        if old_block in document:
            issues.append(f"document 不得再内嵌旧机器块：{old_block}")
    return issues


def _validate_payload(payload: dict[str, Any], *, source_text: str) -> list[str]:
    output_kind = _clean(payload.get("output_kind"))
    issues = validate_professional_output(
        payload,
        source_text=source_text,
        required_names=_required_names(output_kind, source_text),
    )
    issues.extend(_document_issues(payload))
    return list(dict.fromkeys(issues))


def _runtime_schema_prompt(output_kind: str) -> str:
    schema = json.dumps(
        professional_output_json_schema(output_kind),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""

=== XIAODUAN PROFESSIONAL OUTPUT CONTRACT ===
You are the sole writer of this professional result.
Return exactly ONE JSON object and nothing else. The object below is the only
machine authority for this turn. Do not create a second machine template in
Markdown and do not rely on a later model to extract entities from prose.

Rules:
1. The object must satisfy the JSON Schema exactly; unknown fields are forbidden.
2. `document` is only the human-readable Markdown presentation. It must NOT
   contain story-entities-json / appearance-versions-json / visual-direction-json
   fenced blocks. Those older embedded templates are superseded by this runtime
   schema for new results.
3. Machine identity arrays are authoritative. Every entity name must also appear
   naturally in `document`, but field labels such as 人物关系/涉及/身份 are never entities.
4. `source_evidence` must copy a real contiguous fact fragment from the supplied
   user/source context. Do not invent evidence.
5. `stable_description` and appearance `stable_design` contain only reusable,
   visible identity/structure facts, never shot, action, expression, background,
   layout, watermark, subtitle or generation instructions.
6. Produce the complete object in this single writer turn. The server validates,
   freezes and materializes it; no second semantic writer will repair or reinterpret it.

AUTHORITATIVE_JSON_SCHEMA={schema}
=== END PROFESSIONAL OUTPUT CONTRACT ===
""".strip()


def _parse_and_validate(raw: str, *, output_kind: str, source_text: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = parse_professional_output(raw, expected_output_kind=output_kind)
    except ValueError as exc:
        return None, [_clean(exc)]
    return payload, _validate_payload(payload, source_text=source_text)


def _entity_rows(payload: dict[str, Any], document: str) -> list[dict[str, Any]]:
    output_kind = _clean(payload.get("output_kind"))
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    if output_kind == "story_bible":
        groups = [
            ("character", list(payload.get("characters") or [])),
            ("location", list(payload.get("locations") or [])),
            ("prop", list(payload.get("props") or [])),
        ]
    elif output_kind == "character_assets":
        groups = [("character", list(payload.get("characters") or []))]
    elif output_kind == "visual_assets":
        groups = [
            ("location", list(payload.get("locations") or [])),
            ("prop", list(payload.get("props") or [])),
        ]

    result: list[dict[str, Any]] = []
    for entity_type, rows in groups:
        for row in rows:
            name = _clean(row.get("name"))
            if not name or name not in document:
                continue
            metadata: dict[str, Any] = {
                "professional_output_kind": output_kind,
                "typed_professional_entity": True,
            }
            stable = _clean(row.get("stable_description"))
            if stable:
                metadata["authoring"] = {
                    "stable_design": stable,
                    "source_design": stable,
                    "stable_projection": "typed_professional_output_v1",
                }
            result.append({
                "entity_type": entity_type,
                "name": name,
                "evidence_quote": name,
                "metadata": metadata,
            })
    return result


def _deterministic_control(payload: dict[str, Any], document: str) -> dict[str, Any]:
    output_kind = _clean(payload.get("output_kind"))
    return {
        "internal_step": "",
        "stage_memory": json.dumps({
            "professional_output_kind": output_kind,
            "document_sha256": _sha(document),
        }, ensure_ascii=False, separators=(",", ":")),
        "handoff": "",
        "next_expected_action": "确认本阶段并进入下一阶段",
        "skill_runtime": {
            "selected_output_group_ids": [],
            "active_requirement_ids": [],
            "artifact_receipts": [],
            "requirement_receipts": [],
            "stage_complete_claim": False,
        },
        "production_entities": _entity_rows(payload, document),
    }


def _latest_professional_output(production: Any, project_id: str, stage: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    output_kind = output_kind_for_stage(stage)
    role = asset_role_for_output_kind(output_kind)
    if not role:
        return None, None
    rows = [
        asset for asset in production.list_assets(project_id, active_only=True)
        if _clean(asset.get("asset_role")) == role
        and _clean(asset.get("status")).lower() == "ready"
        and _clean(asset.get("dependency_state")).lower() != "stale"
    ]
    rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
    if not rows:
        return None, None
    asset = rows[-1]
    try:
        raw = production.read_text_asset(project_id, _clean(asset.get("asset_id")), max_chars=700000)
        payload = parse_professional_output(raw, expected_output_kind=output_kind)
        return payload, asset
    except Exception:
        return None, asset


def _install_materializer_consumers() -> None:
    """New results consume typed outputs; prose parsing remains legacy-only."""
    from .stage_asset_materialization import StageOutputAssetMaterializer

    if getattr(StageOutputAssetMaterializer, "_xiaoduan_professional_output_installed", False):
        return

    original_extract = StageOutputAssetMaterializer._extract
    original_materialize = StageOutputAssetMaterializer.materialize

    def extract(self: Any, project_id: str, stage: str, text: str):
        payload, _asset = _latest_professional_output(self.production, project_id, stage)
        if payload is None:
            return original_extract(self, project_id, stage, text)
        rows: list[dict[str, str]] = []
        if stage == "02":
            for item in payload.get("characters") or []:
                rows.append({
                    "kind": "character",
                    "name": _clean(item.get("name")),
                    "design": _clean(item.get("stable_description")),
                })
        elif stage == "03":
            for key, kind in (("locations", "location"), ("props", "prop")):
                for item in payload.get(key) or []:
                    rows.append({
                        "kind": kind,
                        "name": _clean(item.get("name")),
                        "design": _clean(item.get("stable_description")),
                    })
        return [row for row in rows if row["name"] and row["design"]]

    def materialize(self: Any, project_id: str):
        result = original_materialize(self, project_id)
        payload, _asset = _latest_professional_output(self.production, project_id, "03")
        if payload is not None:
            direction = dict(payload.get("visual_direction") or {})
            if direction:
                asset = self.production.set_visual_direction(project_id, direction)
                result["visual_direction_asset_id"] = _clean(asset.get("asset_id"))
                result.pop("visual_direction_materialization_error", None)
                result["visual_direction_source"] = "professional_output_registry"
        result["professional_output_primary"] = True
        return result

    StageOutputAssetMaterializer._extract = extract
    StageOutputAssetMaterializer.materialize = materialize
    StageOutputAssetMaterializer._xiaoduan_professional_output_installed = True

    from .character_appearance_materialization import CharacterAppearanceMaterializer

    original_appearance_materialize = CharacterAppearanceMaterializer.materialize

    def appearance_materialize(self: Any, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        if not self.stage_materializer._stage_ready(project, "02"):
            return original_appearance_materialize(self, project_id)
        payload, _asset = _latest_professional_output(self.production, project_id, "02")
        if payload is None:
            return original_appearance_materialize(self, project_id)

        created: list[str] = []
        by_character: dict[str, list[str]] = {}
        for package in payload.get("characters") or []:
            character_name = _clean(package.get("name"))
            character = self._character(project_id, character_name)
            if character is None:
                raise ValueError(f"Stage02 专业输出找不到正式角色实体：{character_name}")
            entity_id = _clean(character.get("entity_id"))
            profile = self._profile(project_id, entity_id)
            if profile is None:
                raise ValueError(f"Stage02 专业输出找不到角色稳定资产：{character_name}")
            for row in package.get("appearances") or []:
                appearance_id = _clean(row.get("appearance_id"))
                appearance_version = "v1" if appearance_id == "default" else appearance_id
                content = {
                    "schema_version": "xiaoduan_character_appearance_v2",
                    "appearance_id": appearance_id,
                    "character_entity_id": entity_id,
                    "character_id": entity_id,
                    "appearance_version": appearance_version,
                    "character_name": character_name,
                    "name": _clean(row.get("name")),
                    "stable_design": _clean(row.get("stable_design")),
                    "change_reason": _clean(row.get("change_reason")),
                    "effective_story_node_ids": list(row.get("effective_story_node_ids") or []),
                    "inherits_identity": True,
                }
                asset = self.production.create_text_asset(
                    project_id,
                    stage="02",
                    skill="xiaoduan-character-appearances",
                    logical_key=f"studio:character:{entity_id}:appearance:{appearance_id}",
                    asset_role="character_appearance",
                    name=f"{character_name} · {content['name']}",
                    content=json.dumps(content, ensure_ascii=False, indent=2),
                    asset_type="STRUCTURED_DATA",
                    extension=".json",
                    source={
                        "type": "professional_character_assets",
                        "output_kind": "character_assets",
                        "character_name": character_name,
                    },
                    parent_asset_ids=[_clean(profile.get("asset_id"))],
                    entity_ids=[entity_id],
                    metadata={
                        "appearance_id": appearance_id,
                        "character_name": character_name,
                        "change_reason": content["change_reason"],
                        "effective_story_node_ids": content["effective_story_node_ids"],
                        "inherits_identity": True,
                        "materialized_from_stage02": True,
                        "professional_output_primary": True,
                    },
                )
                aid = _clean(asset.get("asset_id"))
                created.append(aid)
                by_character.setdefault(entity_id, []).append(aid)
        return {
            "project_id": project_id,
            "materialized": bool(created),
            "asset_ids": list(dict.fromkeys(created)),
            "character_asset_ids": by_character,
            "source": "professional_character_assets",
            "professional_output_primary": True,
            "model_calls": 0,
        }

    CharacterAppearanceMaterializer.materialize = appearance_materialize


def _install_context_resource_preference() -> None:
    """Downstream consumes the exact typed Stage01 Resource when available."""
    try:
        from .production_runtime_optimization import ProjectProductionContext
    except Exception:
        return
    if getattr(ProjectProductionContext, "_xiaoduan_professional_story_preference", False):
        return
    original = ProjectProductionContext._active_text_asset

    def active_text_asset(self: Any, project_id: str, role: str):
        if role == "story_bible":
            rows = [
                item for item in self.production.list_assets(project_id, active_only=True)
                if _clean(item.get("asset_role")) == "professional_story_bible"
                and _clean(item.get("status")).lower() == "ready"
                and _clean(item.get("dependency_state")).lower() != "stale"
            ]
            rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
            if rows:
                return rows[-1]
        return original(self, project_id, role)

    ProjectProductionContext._active_text_asset = active_text_asset
    ProjectProductionContext._xiaoduan_professional_story_preference = True


def install_professional_output_runtime(settings: Any, director: Any) -> dict[str, Any]:
    """Wao-style one-writer, one-object front-half execution boundary.

    Stage01-03 content is generated as one strict schema object. The displayed
    Markdown is only its ``document`` field. Entity materialization and control
    metadata are deterministic code; the old second Qwen semantic-extraction
    pass is bypassed for new typed results.
    """
    if getattr(director, "_xiaoduan_professional_output_runtime_installed", False):
        return {"status": "already_installed", "policy": "professional_output_v1"}

    store = ProfessionalOutputStore(settings.data_dir)
    original_tracked = director._tracked_llm_chat
    original_structured = director._structured_json_call
    production = director.production
    original_record_entities = production.record_control_entities

    # Use the class implementation only for the single professional writer call.
    # It preserves Director telemetry while deliberately bypassing the old
    # front_half_quality_gate writer/repair wrapper for this phase.
    base_tracked = DirectorService._tracked_llm_chat.__get__(director, type(director))

    async def tracked_llm_chat(
        instance: Any,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        if phase != "director_orchestrator_content":
            return await original_tracked(
                phase=phase,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        skill = gate._detect_skill(system_prompt, messages)
        output_kind = output_kind_for_skill(skill)
        if not output_kind:
            return await original_tracked(
                phase=phase,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        source_text = gate._authoritative_message_source(messages)
        strict_system = system_prompt + "\n\n" + _runtime_schema_prompt(output_kind)
        result = await base_tracked(
            phase=phase,
            messages=messages,
            system_prompt=strict_system,
            temperature=min(float(temperature), 0.35),
            max_tokens=max_tokens,
        )
        raw = _clean(result.get("content"))
        payload, issues = _parse_and_validate(
            raw,
            output_kind=output_kind,
            source_text=source_text,
        )

        if payload is None or issues:
            repair_system = """You repair one strict professional output object.
Return exactly one JSON object matching the supplied schema. You are not a
second writer: preserve all valid decisions from RAW_OUTPUT and only correct
schema, source-lineage, completeness or stability-boundary errors. Never add a
new story identity or fact. `source_evidence` must be copied from SOURCE.
No Markdown outside the JSON object."""
            repair_prompt = f"""OUTPUT_KIND={output_kind}

VALIDATION_ERRORS={json.dumps(issues, ensure_ascii=False)}

AUTHORITATIVE_SCHEMA={json.dumps(professional_output_json_schema(output_kind), ensure_ascii=False)}

SOURCE={source_text or '<none>'}

RAW_OUTPUT={raw}
"""
            repaired = await base_tracked(
                phase="professional_output_schema_repair",
                messages=[{"role": "user", "content": repair_prompt}],
                system_prompt=repair_system,
                temperature=0.0,
                max_tokens=max_tokens,
            )
            raw = _clean(repaired.get("content"))
            payload, issues = _parse_and_validate(
                raw,
                output_kind=output_kind,
                source_text=source_text,
            )
            if payload is None or issues:
                raise RuntimeError(
                    "专业输出严格 Schema 修复后仍未通过，未写入项目："
                    + json.dumps(issues, ensure_ascii=False)
                )
            result = dict(repaired)
            result["professional_output_repaired"] = True

        store.put(payload)
        value = dict(result)
        value["content"] = _clean(payload.get("document"))
        value["professional_output"] = payload
        value["professional_output_kind"] = output_kind
        value["professional_output_schema_version"] = int(payload.get("schema_version") or 1)
        value["professional_output_document_sha256"] = _sha(value["content"])
        value["front_half_quality_gate"] = {
            "valid": True,
            "issues": [],
            "mode": "strict_professional_output_registry",
        }
        return value

    async def structured_json_call(
        instance: Any,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        contract: str,
    ):
        if phase == "director_orchestrator_control":
            document = _extract_execution_content(messages)
            payload = store.get_by_document(document) if document else None
            if payload is not None and _clean(payload.get("output_kind")) in {
                "story_bible", "character_assets", "visual_assets",
            }:
                control = _deterministic_control(payload, document)
                return (
                    {
                        "content": json.dumps(control, ensure_ascii=False),
                        "model": "deterministic-professional-output-control",
                        "llm_metrics": {
                            "usage": {}, "timings": {},
                            "request_attempts": 0, "request_retries": 0,
                        },
                        "professional_output_control": True,
                    },
                    control,
                    False,
                )
        return await original_structured(
            phase=phase,
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            contract=contract,
        )

    def record_control_entities(
        production_instance: Any,
        project_id: str,
        *,
        stage: str,
        skill: str,
        content: str,
        turn_id: str,
        raw_entities: list[Any] | None,
        turn_asset_id: str,
    ):
        payload = store.get_by_document(content)
        entities = original_record_entities(
            project_id,
            stage=stage,
            skill=skill,
            content=content,
            turn_id=turn_id,
            raw_entities=(
                _entity_rows(payload, content)
                if payload is not None
                else raw_entities
            ),
            turn_asset_id=turn_asset_id,
        )
        if payload is not None:
            output_kind = _clean(payload.get("output_kind"))
            role = asset_role_for_output_kind(output_kind)
            if role:
                production_instance.create_text_asset(
                    project_id,
                    stage=stage,
                    skill=skill,
                    logical_key=f"studio:professional-output:{output_kind}",
                    asset_role=role,
                    name={
                        "story_bible": "故事生产圣经 · 专业结构化结果",
                        "character_assets": "角色资产 · 专业结构化结果",
                        "visual_assets": "视觉资产 · 专业结构化结果",
                    }.get(output_kind, output_kind),
                    content=json.dumps(payload, ensure_ascii=False, indent=2),
                    asset_type="STRUCTURED_DATA",
                    extension=".json",
                    source={
                        "type": "professional_output_registry",
                        "turn_id": turn_id,
                        "output_kind": output_kind,
                    },
                    parent_asset_ids=[turn_asset_id],
                    entity_ids=[_clean(item.get("entity_id")) for item in entities if _clean(item.get("entity_id"))],
                    metadata={
                        "source_of_truth": True,
                        "single_writer": True,
                        "schema_version": int(payload.get("schema_version") or 1),
                        "output_kind": output_kind,
                        "document_sha256": _sha(content),
                        "server_rewrites_creative_content": False,
                    },
                )
        return entities

    director._tracked_llm_chat = MethodType(tracked_llm_chat, director)
    director._structured_json_call = MethodType(structured_json_call, director)
    production.record_control_entities = MethodType(record_control_entities, production)
    director._xiaoduan_professional_output_runtime_installed = True
    director._xiaoduan_professional_output_store = store

    _install_materializer_consumers()
    _install_context_resource_preference()

    return {
        "status": "installed",
        "policy": "professional_output_v1",
        "one_writer": True,
        "strict_output_registry": True,
        "second_semantic_control_llm": False,
        "professional_asset_roles": [
            "professional_story_bible",
            "professional_character_assets",
            "professional_visual_assets",
        ],
    }


__all__ = [
    "ProfessionalOutputStore",
    "install_professional_output_runtime",
]
