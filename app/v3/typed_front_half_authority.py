from __future__ import annotations

import copy
import hashlib
import json
import re
from types import MethodType
from typing import Any

from app.services.director import DirectorService

from . import professional_output_registry as registry
from . import professional_output_runtime as runtime
from .professional_output_runtime import ProfessionalOutputStore
from .project_source_snapshot import (
    _snapshot_payload,
    bind_server_owned_evidence,
    typed_entity_logical_key,
)


_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_ROLE = (
    "少年|少女|青年|老者|老人|男子|女子|男人|女人|男孩|女孩|姑娘|小伙|"
    "先生|女士|公子|小姐|师父|师兄|师姐|师弟|师妹|将军|王子|公主|"
    "皇帝|皇后|太子|医生|护士|警察|老师|学生|父亲|母亲|哥哥|姐姐|弟弟|妹妹"
)
_PREDICATE = (
    "独自|迅速|缓缓|突然|忽然|已经|没有|正在|仍然|站|坐|走|跑|来到|进入|离开|"
    "发现|看向|看了|看|望向|望|问|回答|答道|答|说道|说|喊道|喊|笑|哭|转身|"
    "回头|勒马|翻身|下马|拔剑|拔|按住|跟上|跟|穿过|听见|听到|抬手|抬头|"
    "提着|提灯|握住|握紧|握|身穿|穿着|穿|拿着|拿|背着|背负|背"
)
_ROLE_NAME = re.compile(
    rf"(?:{_ROLE})\s*([{_CJK}]{{2,4}}?)(?=(?:{_PREDICATE})|[，。！？!?；;、\s])"
)
_SPEAKER = re.compile(
    rf"(?:^|[，。！？!?；;：:\n\s“”‘’\"'])"
    rf"([{_CJK}]{{2,4}}?)(?=(?:问|回答|答道|说道|说|喊道|喊|低声道|轻声道|沉声道|冷声道|笑道))"
)
_CLAUSE_SUBJECT = re.compile(
    rf"(?:^|[，。！？!?；;：:\n\s“”‘’\"'])"
    rf"([{_CJK}]{{2,4}}?)(?=(?:{_PREDICATE}))"
)
_UNIT_SPLIT = re.compile(r"[。！？!?；;\n]+")

_RETIRED_ASSET_ROLES = {
    "character_profile", "character_appearance", "character_reference",
    "character_turnaround", "character_consistency", "location_profile",
    "location_reference", "scene_reference", "prop_profile", "prop_reference",
    "item_reference",
}
_OLD_MACHINE_BLOCKS = (
    "```story-entities-json",
    "```appearance-versions-json",
    "```visual-direction-json",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", _clean(value)).casefold()


def _sha(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _units(source_text: str) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for raw in _UNIT_SPLIT.split(_clean(source_text)):
        unit = raw.strip(" \t\r\n\"'“”‘’|#>*_`-")
        key = _norm(unit)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(unit)
    return rows


def infer_canonical_characters(source_text: str) -> list[str]:
    """Return stable, independently nameable characters from exact source text.

    This intentionally follows the reusable-identity boundary used by wao's
    asset-development skill instead of treating arbitrary source substrings as
    entities. A Chinese name is accepted when the source introduces it with a
    human-role cue, uses it as a dialogue speaker, or uses the same clause-start
    subject in at least two distinct narrative units.
    """
    source = _clean(source_text)
    if not source:
        return []

    found: list[tuple[int, str]] = []
    repeated: dict[str, set[str]] = {}
    first_pos: dict[str, int] = {}

    for match in _ROLE_NAME.finditer(source):
        name = _clean(match.group(1))
        if name:
            found.append((match.start(1), name))
    for match in _SPEAKER.finditer(source):
        name = _clean(match.group(1))
        if name:
            found.append((match.start(1), name))

    offset = 0
    for unit in _units(source):
        unit_pos = source.find(unit, offset)
        if unit_pos < 0:
            unit_pos = source.find(unit)
        if unit_pos >= 0:
            offset = unit_pos + len(unit)
        unit_key = _norm(unit)
        for match in _CLAUSE_SUBJECT.finditer(unit):
            name = _clean(match.group(1))
            if not name:
                continue
            repeated.setdefault(name, set()).add(unit_key)
            first_pos.setdefault(name, max(0, unit_pos) + match.start(1))

    for name, evidence in repeated.items():
        if len(evidence) >= 2:
            found.append((first_pos.get(name, source.find(name)), name))

    found.sort(key=lambda item: (item[0] if item[0] >= 0 else 10**9, len(item[1])))
    result: list[str] = []
    seen: set[str] = set()
    for _pos, name in found:
        key = _norm(name)
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _exact_sentence(source_text: str, name: str) -> str:
    source = _clean(source_text)
    wanted = _clean(name)
    if not source or not wanted:
        return ""
    pos = source.find(wanted)
    if pos < 0:
        return ""
    left = max(
        source.rfind("。", 0, pos), source.rfind("！", 0, pos), source.rfind("？", 0, pos),
        source.rfind(";", 0, pos), source.rfind("；", 0, pos), source.rfind("\n", 0, pos),
    ) + 1
    candidates = [
        value for value in (
            source.find("。", pos + len(wanted)), source.find("！", pos + len(wanted)),
            source.find("？", pos + len(wanted)), source.find(";", pos + len(wanted)),
            source.find("；", pos + len(wanted)), source.find("\n", pos + len(wanted)),
        ) if value >= 0
    ]
    right = min(candidates) + 1 if candidates else len(source)
    return source[left:right].strip()


def normalize_story_bible_characters(
    payload: dict[str, Any],
    source_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Make Stage01 canonical character identity server-owned.

    The model writes the professional object, but it cannot promote a grammar
    fragment such as ``手中`` into a reusable character. Missing exact-source
    canonical characters are restored before persistence.
    """
    value = copy.deepcopy(payload)
    if _clean(value.get("output_kind")) != "story_bible":
        return value, {"changed": False, "removed": [], "added": []}

    canonical = infer_canonical_characters(source_text)
    canonical_map = {_norm(name): name for name in canonical}
    existing_rows = [row for row in value.get("characters") or [] if isinstance(row, dict)]
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    seen: set[str] = set()

    for row in existing_rows:
        name = _clean(row.get("name"))
        key = _norm(name)
        if not key or key not in canonical_map:
            if name:
                removed.append(name)
            continue
        if key in seen:
            continue
        seen.add(key)
        exact_name = canonical_map[key]
        item = dict(row)
        item["name"] = exact_name
        item["source_evidence"] = _exact_sentence(source_text, exact_name) or exact_name
        kept.append(item)

    added: list[str] = []
    for name in canonical:
        key = _norm(name)
        if key in seen:
            continue
        kept.append({
            "name": name,
            "source_evidence": _exact_sentence(source_text, name) or name,
        })
        seen.add(key)
        added.append(name)

    value["characters"] = kept
    bind_server_owned_evidence(value, source_text)
    return value, {
        "changed": value != payload,
        "removed": removed,
        "added": added,
        "canonical": canonical,
    }


def _typed_document_issues(payload: dict[str, Any]) -> list[str]:
    """Presentation checks for typed results; never revive legacy Markdown gates."""
    document = _clean(payload.get("document"))
    issues: list[str] = []
    if not document:
        return ["专业输出 document 不能为空"]
    for marker in _OLD_MACHINE_BLOCKS:
        if marker in document:
            issues.append(f"document 不得再内嵌旧机器块：{marker}")
    if _clean(payload.get("output_kind")) == "story_bible":
        lowered = document.casefold()
        for token in ("最终图片 prompt", "最终图片提示词", "镜头焦段参数", "comfyui 参数"):
            if token.casefold() in lowered:
                issues.append("Stage01 混入了后续媒体/镜头执行参数")
                break
    return issues


def _retire_noncanonical_entities(
    production: Any,
    project_id: str,
    valid: dict[str, set[str]],
) -> list[str]:
    graph = production.get_graph(project_id)
    retired: list[str] = []
    for entity_id, entity in (graph.get("entities") or {}).items():
        if not isinstance(entity, dict):
            continue
        kind = _clean(entity.get("entity_type")).lower()
        if kind not in valid:
            continue
        name = _clean(entity.get("name"))
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        managed = (
            bool(metadata.get("typed_professional_entity"))
            or bool(metadata.get("materialized_from_validated_story_bible"))
            or _clean(metadata.get("source_stage")) == "01"
            or _clean(entity.get("stage")) == "01"
        )
        if not managed or _norm(name) in valid[kind]:
            continue
        entity["entity_type"] = "retired_fragment"
        entity["stage"] = "01"
        metadata["hidden_from_normal_lists"] = True
        metadata["retired_story_entity"] = True
        metadata["retired_reason"] = "不属于不可变源文本中的规范可复用身份"
        entity["metadata"] = metadata
        retired.append(_clean(entity_id))

    retired_set = set(retired)
    if retired_set:
        for asset in (graph.get("assets") or {}).values():
            if not isinstance(asset, dict) or asset.get("active") is False:
                continue
            if not (retired_set & {_clean(x) for x in asset.get("entity_ids") or []}):
                continue
            if _clean(asset.get("asset_role")) not in _RETIRED_ASSET_ROLES:
                continue
            asset["active"] = False
            asset["status"] = "archived"
            asset["dependency_state"] = "stale"
            asset.setdefault("metadata", {})["retired_reason"] = "上游非规范实体已退休"
        production._save(graph)
    return retired


def _upsert_typed_entities(
    director: Any,
    project_id: str,
    payload: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> tuple[list[str], dict[str, set[str]]]:
    production = director.production
    source_text = _clean((snapshot or {}).get("text"))
    bind_server_owned_evidence(payload, source_text)
    groups = (
        ("character", "characters"),
        ("location", "locations"),
        ("prop", "props"),
    )
    valid: dict[str, set[str]] = {kind: set() for kind, _ in groups}
    entity_ids: list[str] = []

    for kind, group_name in groups:
        for row in payload.get(group_name) or []:
            if not isinstance(row, dict):
                continue
            name = _clean(row.get("name"))
            if not name:
                continue
            valid[kind].add(_norm(name))
            logical_key = typed_entity_logical_key(kind, name)
            existing = next(
                (
                    item for item in production.list_entities(project_id)
                    if _clean(item.get("entity_type")).lower() == kind
                    and _norm(item.get("name")) == _norm(name)
                ),
                None,
            )
            if existing is None:
                existing = production.create_entity(
                    project_id,
                    entity_type=kind,
                    logical_key=logical_key,
                    name=name,
                    stage="01",
                    skill="xiaoduan-story-bible",
                    metadata={
                        "typed_professional_entity": True,
                        "professional_output_kind": "story_bible",
                        "source_snapshot_id": _clean((snapshot or {}).get("source_id")),
                        "source_snapshot_version": int((snapshot or {}).get("source_version") or 1),
                    },
                    evidence={
                        "type": "immutable_source_snapshot",
                        "evidence_quote": _clean(row.get("source_evidence")),
                        "source_asset_id": _clean((snapshot or {}).get("asset_id")),
                        "source_snapshot_sha256": _clean((snapshot or {}).get("source_sha256")),
                    },
                )
            else:
                graph = production.get_graph(project_id)
                current = (graph.get("entities") or {}).get(_clean(existing.get("entity_id")))
                if isinstance(current, dict):
                    current["name"] = name
                    current["logical_key"] = logical_key
                    current["stage"] = "01"
                    current["skill"] = "xiaoduan-story-bible"
                    meta = current.setdefault("metadata", {})
                    meta.update({
                        "typed_professional_entity": True,
                        "professional_output_kind": "story_bible",
                        "source_snapshot_id": _clean((snapshot or {}).get("source_id")),
                        "source_snapshot_version": int((snapshot or {}).get("source_version") or 1),
                    })
                    evidence = {
                        "type": "immutable_source_snapshot",
                        "evidence_quote": _clean(row.get("source_evidence")),
                        "source_asset_id": _clean((snapshot or {}).get("asset_id")),
                        "source_snapshot_sha256": _clean((snapshot or {}).get("source_sha256")),
                    }
                    if evidence not in current.setdefault("evidence", []):
                        current["evidence"].append(evidence)
                    production._save(graph)
            entity_id = _clean(existing.get("entity_id"))
            if entity_id:
                entity_ids.append(entity_id)
    return list(dict.fromkeys(entity_ids)), valid


def _persist_normalized_story_output(
    settings: Any,
    director: Any,
    project_id: str,
    payload: dict[str, Any],
    current_asset: dict[str, Any] | None,
    entity_ids: list[str],
) -> dict[str, Any] | None:
    if current_asset is None:
        return None
    current_text = director.production.read_text_asset(
        project_id, _clean(current_asset.get("asset_id")), max_chars=700000,
    )
    normalized_text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        current_payload = json.loads(current_text)
    except Exception:
        current_payload = None
    ProfessionalOutputStore(settings.data_dir).put(payload)
    if current_payload == payload:
        return current_asset

    return director.production.create_text_asset(
        project_id,
        stage="01",
        skill="xiaoduan-story-bible",
        logical_key="studio:professional-output:story_bible",
        asset_role="professional_story_bible",
        name="故事生产圣经 · 专业结构化结果",
        content=normalized_text,
        asset_type="STRUCTURED_DATA",
        extension=".json",
        source={
            "type": "typed_front_half_authority",
            "previous_asset_id": _clean(current_asset.get("asset_id")),
            "normalization": "immutable_source_canonical_identity",
        },
        parent_asset_ids=list(current_asset.get("parent_asset_ids") or []),
        entity_ids=entity_ids,
        metadata={
            "source_of_truth": True,
            "single_writer": True,
            "schema_version": int(payload.get("schema_version") or 1),
            "output_kind": "story_bible",
            "document_sha256": _sha(payload.get("document")),
            "server_rewrites_creative_content": False,
            "server_canonicalizes_identity_facts": True,
        },
    )


def reconcile_typed_stage01_entities(
    settings: Any,
    director: Any,
    project_id: str,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    project = director.get_project(project_id)
    state = ((project.get("stage_state") or {}).get("01") or {})
    completion = ((state.get("skill_runtime") or {}).get("completion") or {})
    ready = (
        "01" in {_clean(x) for x in project.get("completed_stages") or []}
        or bool(state.get("stage_ready"))
        or bool(completion.get("ready"))
        or _clean(project.get("current_stage")) not in {"", "01"}
    )
    if require_ready and not ready:
        return {"project_id": project_id, "reconciled": False, "reason": "stage01_not_ready"}

    payload, asset = runtime._latest_professional_output(director.production, project_id, "01")
    if not isinstance(payload, dict):
        return {"project_id": project_id, "reconciled": False, "reason": "typed_story_output_unavailable"}

    snapshot = _snapshot_payload(director.production, project_id)
    source_text = _clean((snapshot or {}).get("text"))
    normalized, normalization = normalize_story_bible_characters(payload, source_text)
    entity_ids, valid = _upsert_typed_entities(director, project_id, normalized, snapshot)
    retired = _retire_noncanonical_entities(director.production, project_id, valid)
    persisted = _persist_normalized_story_output(
        settings, director, project_id, normalized, asset, entity_ids,
    )

    names = {
        kind: [
            _clean(item.get("name"))
            for item in director.production.list_entities(project_id, kind)
            if _clean(item.get("name"))
        ]
        for kind in ("character", "location", "prop")
    }
    return {
        "project_id": project_id,
        "reconciled": True,
        "policy": "typed_immutable_source_authority_v1",
        "character_names": names["character"],
        "location_names": names["location"],
        "prop_names": names["prop"],
        "retired_entity_ids": retired,
        "normalization": normalization,
        "professional_asset_id": _clean((persisted or {}).get("asset_id")),
        "model_calls": 0,
    }


def install_typed_front_half_authority(settings: Any, director: Any) -> dict[str, Any]:
    """Retire legacy Markdown gates whenever a strict typed result exists."""
    if getattr(director, "_xiaoduan_typed_front_half_authority_installed", False):
        return {"status": "already_installed", "policy": "typed_front_half_authority_v1"}

    original_schema_prompt = runtime._runtime_schema_prompt

    def schema_prompt(output_kind: str) -> str:
        base = original_schema_prompt(output_kind)
        if output_kind != "story_bible":
            return base
        return base + """

=== CANONICAL IDENTITY SCOPE ===
The exact source text is the fact boundary. `characters` contains only stable,
independently nameable character identities. A body-part phrase, locative phrase,
action fragment, pronoun/group phrase, field label or other sentence fragment is
never a character even when the same characters occur in the source text.
Use the shortest canonical identity name that can refer to the same character
across multiple sentences. Do not promote incidental visible nouns into assets.
This follows the reusable-identity boundary used by asset-development: identity
must survive reuse; transient sentence state does not.
=== END CANONICAL IDENTITY SCOPE ===
""".strip()

    runtime._runtime_schema_prompt = schema_prompt
    # Make typed presentation checks self-contained; installer order can no
    # longer resurrect the retired Markdown template gate.
    runtime._document_issues = _typed_document_issues

    previous_validate = runtime.validate_professional_output

    def validate_professional_output(
        payload: dict[str, Any],
        *,
        source_text: str,
        required_names: dict[str, list[str]] | None = None,
    ) -> list[str]:
        if _clean(payload.get("output_kind")) == "story_bible":
            normalized, _meta = normalize_story_bible_characters(payload, source_text)
            payload.clear()
            payload.update(normalized)
        return previous_validate(
            payload,
            source_text=source_text,
            required_names=required_names,
        )

    runtime.validate_professional_output = validate_professional_output
    registry.validate_professional_output = validate_professional_output

    previous_required_names = runtime._required_names

    def required_names(output_kind: str, source_text: str) -> dict[str, list[str]]:
        if output_kind in {"story_bible", "character_assets"}:
            return {"characters": infer_canonical_characters(source_text)}
        return previous_required_names(output_kind, source_text)

    runtime._required_names = required_names

    legacy_confirm = director.confirm_stage
    base_confirm = DirectorService.confirm_stage.__get__(director, type(director))

    async def confirm_stage(instance: Any, project_id: str):
        project = instance.get_project(project_id)
        stage = _clean(project.get("current_stage"))
        payload, _asset = runtime._latest_professional_output(instance.production, project_id, stage)
        if stage not in {"01", "02", "03"} or not isinstance(payload, dict):
            return await legacy_confirm(project_id)

        reconciliation: dict[str, Any] | None = None
        if stage == "01":
            reconciliation = reconcile_typed_stage01_entities(
                settings, instance, project_id, require_ready=False,
            )
            payload, _asset = runtime._latest_professional_output(instance.production, project_id, stage)

        snapshot = _snapshot_payload(instance.production, project_id)
        source_text = _clean((snapshot or {}).get("text"))
        issues = runtime._validate_payload(payload or {}, source_text=source_text)
        if issues:
            raise RuntimeError(
                f"阶段 {stage} 严格专业结果未通过，禁止确认："
                + json.dumps(issues, ensure_ascii=False)
            )

        result = await base_confirm(project_id)
        if isinstance(result, dict):
            result = dict(result)
            result["typed_front_half_authority"] = {
                "policy": "typed_front_half_authority_v1",
                "legacy_markdown_confirm_gate": False,
                "stage01_reconciliation": reconciliation or {},
            }
        return result

    director.confirm_stage = MethodType(confirm_stage, director)

    try:
        from . import production_authoring_assets as authoring_assets

        legacy_reconcile = authoring_assets.reconcile_stage01_story_characters

        def reconcile_stage01_story_characters(
            patched_director: Any,
            project_id: str,
            *,
            require_ready: bool = True,
        ) -> dict[str, Any]:
            payload, _asset = runtime._latest_professional_output(
                patched_director.production, project_id, "01",
            )
            if isinstance(payload, dict):
                return reconcile_typed_stage01_entities(
                    settings,
                    patched_director,
                    project_id,
                    require_ready=require_ready,
                )
            return legacy_reconcile(
                patched_director,
                project_id,
                require_ready=require_ready,
            )

        authoring_assets.reconcile_stage01_story_characters = reconcile_stage01_story_characters
    except Exception:
        pass

    director._xiaoduan_typed_front_half_authority_installed = True
    return {
        "status": "installed",
        "policy": "typed_front_half_authority_v1",
        "legacy_markdown_confirm_gate_for_typed_outputs": False,
        "ready_state_legacy_entity_reconcile_for_typed_outputs": False,
        "server_owned_character_identity": True,
        "wao_reusable_identity_boundary": True,
    }


__all__ = [
    "infer_canonical_characters",
    "install_typed_front_half_authority",
    "normalize_story_bible_characters",
    "reconcile_typed_stage01_entities",
]
