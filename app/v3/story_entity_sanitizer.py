from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.services.production_skills import BUILTIN_PRODUCTION_SKILLS

from . import front_half_quality_gate as gate
from . import story_source_coverage as coverage


# Stage01 prose remains human-readable, but downstream code must never infer the
# canonical Entity graph from arbitrary Markdown labels. The fenced machine
# block below is the authoritative identity contract for new results.
_STORY_ENTITIES_BLOCK = re.compile(
    r"```story-entities-json\s*(.*?)\s*```",
    re.I | re.S,
)
_MACHINE_KEYS = ("characters", "locations", "props")
_KIND_BY_KEY = {
    "characters": "character",
    "locations": "location",
    "props": "prop",
}
_LABEL_BY_KEY = {
    "characters": "角色实体表",
    "locations": "地点实体表",
    "props": "道具实体表",
}

# Field/schema labels may legitimately appear inside entity sections, but they
# are never reusable identities. This list deliberately contains the exact bad
# values already observed in production: 人物关系 / 涉及.
_SCHEMA_LABELS = {
    "角色", "角色名称", "角色名", "人物", "人物名称", "姓名", "名称", "身份", "稳定身份",
    "人物关系", "角色关系", "关系", "涉及", "涉及剧情节点", "剧情节点", "关联剧情节点", "节点",
    "原文证据", "证据", "来源", "来源说明", "备注", "说明", "类型", "年龄", "年龄感", "性别",
    "性别呈现", "外观", "脸部", "面部", "发型", "发色", "肤色", "体型", "身高", "身高感",
    "服装", "鞋履", "配饰", "固定身份锚点", "允许变化项", "形象版本", "参考图生成要求",
    "空间事实", "空间边界", "布局", "材质", "视觉锚点", "剧情功能", "首次出现", "状态变化",
}
_RETIRED_ASSET_ROLES = {
    "character": {
        "character_profile", "character_appearance", "character_reference",
        "character_turnaround", "character_consistency",
    },
    "location": {"location_profile", "scene_reference", "location_reference"},
    "prop": {"prop_profile", "prop_reference", "item_reference"},
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return coverage._norm(value)


def _is_schema_label(value: Any) -> bool:
    text = _norm(value)
    if not text:
        return True
    if text in {_norm(item) for item in _SCHEMA_LABELS}:
        return True
    return any(
        token in text
        for token in (
            "人物关系", "角色关系", "涉及剧情节点", "关联剧情节点",
            "原文证据", "参考图生成要求", "固定身份锚点", "允许变化项",
        )
    )


def _append_name(names: list[str], candidate: Any) -> None:
    raw = _clean(candidate)
    raw = re.sub(r"^#{1,6}\s*", "", raw)
    raw = re.sub(r"^\d+[.)、）]\s*", "", raw)
    raw = re.sub(r"^[-*+]\s*", "", raw)
    raw = re.sub(
        r"\s*[（(](?:主角|男主|女主|配角|反派|角色)[^）)]*[）)]\s*$",
        "",
        raw,
    )
    # Common per-entity headings such as `角色1：沈璃` are identities, while
    # field labels such as `人物关系：...` are not.
    match = re.match(r"^(?:角色|人物|地点|场景|道具)\s*\d+\s*[：:]\s*(.+)$", raw)
    if match:
        raw = match.group(1).strip()
    if not raw or _is_schema_label(raw):
        return
    value = coverage._plausible_entity_name(raw)
    if not value or _is_schema_label(value) or coverage._looks_like_table_id(value):
        return
    if value not in names:
        names.append(value)


def extract_story_table_names_strict(text: str, label: str) -> list[str]:
    """Extract identities from a Stage01 human-readable entity section.

    This exists only for old results that predate the machine block. New results
    are materialized from ``story-entities-json`` and do not depend on prose
    shape, heading order or field labels.
    """
    body = coverage._section(text, label)
    if not body:
        return []

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    headers = {_norm(item) for item in coverage._TABLE_NAME_HEADERS.get(label, ("名称",))}
    table_name_index: int | None = None
    table_header_line = -1

    for line_index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        normalized = [_norm(cell) for cell in cells]
        for index, cell in enumerate(normalized):
            if cell in headers:
                table_name_index = index
                table_header_line = line_index
                break
        if table_name_index is not None:
            break

    names: list[str] = []
    if table_name_index is not None:
        for line_index, line in enumerate(lines):
            if line_index <= table_header_line or not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not cells or all(
                re.fullmatch(r"\s*:?-{3,}:?\s*", cell or "")
                for cell in cells
            ):
                continue
            if table_name_index < len(cells):
                _append_name(names, cells[table_name_index])
        return names

    for line in lines:
        if line.startswith("|"):
            continue
        if re.match(r"^#{1,6}\s+", line):
            _append_name(names, line)
            continue

        value = re.sub(r"^[-*+]\s*", "", line)
        value = re.sub(r"^\d+[.)、）]\s*", "", value)
        first_sentence = re.split(r"[。！？!?]", value, maxsplit=1)[0].strip()
        if not first_sentence:
            continue

        pieces = [item.strip() for item in re.split(r"[；;]", first_sentence) if item.strip()]
        if len(pieces) > 1:
            for piece in pieces:
                separator = "：" if "：" in piece else ":" if ":" in piece else ""
                if separator:
                    left, right = [part.strip() for part in piece.split(separator, 1)]
                    if _norm(left) in headers:
                        _append_name(names, right)
                    elif not _is_schema_label(left):
                        _append_name(names, left)
                else:
                    _append_name(names, piece)
            continue

        separator = "：" if "：" in first_sentence else ":" if ":" in first_sentence else ""
        if separator:
            left, right = [part.strip() for part in first_sentence.split(separator, 1)]
            if _norm(left) in headers:
                _append_name(names, right)
            elif not _is_schema_label(left):
                _append_name(names, left)
            continue

        _append_name(names, first_sentence)

    return names


def parse_story_entities_block(content: str) -> dict[str, list[dict[str, str]]]:
    blocks = _STORY_ENTITIES_BLOCK.findall(_clean(content))
    if not blocks:
        raise ValueError("缺少 ```story-entities-json 故事实体机器合同")
    if len(blocks) != 1:
        raise ValueError("故事生产圣经必须且只能包含一份 ```story-entities-json 机器合同")
    try:
        payload = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"story-entities-json 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict) or set(payload) != set(_MACHINE_KEYS):
        raise ValueError("story-entities-json 只能包含 characters / locations / props")

    result: dict[str, list[dict[str, str]]] = {}
    for key in _MACHINE_KEYS:
        rows = payload.get(key)
        if not isinstance(rows, list):
            raise ValueError(f"story-entities-json.{key} 必须是数组")
        seen: set[str] = set()
        normalized: list[dict[str, str]] = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict) or set(raw) != {"name", "source_evidence"}:
                raise ValueError(
                    f"story-entities-json.{key}[{index}] 只能包含 name/source_evidence"
                )
            name = _clean(raw.get("name"))
            evidence = _clean(raw.get("source_evidence"))
            if not name or _is_schema_label(name) or coverage._looks_like_table_id(name):
                raise ValueError(f"story-entities-json.{key}[{index}].name 不是合法实体名称：{name}")
            if not evidence:
                raise ValueError(f"story-entities-json.{key}[{index}].source_evidence 不能为空")
            identity = _norm(name)
            if identity in seen:
                raise ValueError(f"story-entities-json.{key} 实体重复：{name}")
            seen.add(identity)
            normalized.append({"name": name, "source_evidence": evidence})
        result[key] = normalized
    return result


def _compact_evidence(value: str) -> str:
    return re.sub(
        r"[\s，。；：、“”‘’！？,.!?;:'\"（）()【】\[\]]+",
        "",
        _clean(value),
    ).casefold()


def story_entity_contract_issues(content: str, source_text: str) -> list[str]:
    try:
        payload = parse_story_entities_block(content)
    except ValueError as exc:
        return [str(exc)]

    issues: list[str] = []
    compact_source = _compact_evidence(source_text)
    for key in _MACHINE_KEYS:
        for row in payload[key]:
            evidence = _compact_evidence(row["source_evidence"])
            if not evidence or evidence not in compact_source:
                issues.append(
                    f"{key} 实体「{row['name']}」的 source_evidence 必须是原始故事中的真实片段"
                )

    machine_characters = {_norm(row["name"]) for row in payload["characters"]}
    for name in coverage.infer_source_character_candidates(source_text):
        if _norm(name) not in machine_characters:
            issues.append(f"story-entities-json.characters 遗漏真实故事角色「{name}」")

    # Human-readable tables and the machine contract must agree. Markdown is for
    # editors; JSON is for code. If the two disagree, repair the output rather
    # than guessing which version downstream should trust.
    for key in _MACHINE_KEYS:
        label = _LABEL_BY_KEY[key]
        table_names = extract_story_table_names_strict(content, label)
        machine_names = [row["name"] for row in payload[key]]
        table_set = {_norm(item) for item in table_names}
        machine_set = {_norm(item) for item in machine_names}
        if table_set:
            for name in table_names:
                if _norm(name) not in machine_set:
                    issues.append(f"{label} 中的「{name}」未写入 story-entities-json.{key}")
            for name in machine_names:
                if _norm(name) not in table_set:
                    issues.append(f"story-entities-json.{key} 的「{name}」未出现在{label}")
    return list(dict.fromkeys(issues))


def _legacy_payload(content: str, source_text: str) -> dict[str, list[dict[str, str]]]:
    """One-way compatibility for projects created before the machine contract."""
    payload = {key: [] for key in _MACHINE_KEYS}
    character_names: list[str] = []
    for name in coverage.infer_source_character_candidates(source_text):
        if name not in character_names:
            character_names.append(name)
    for name in extract_story_table_names_strict(content, "角色实体表"):
        if coverage._contains_name(source_text, name) and name not in character_names:
            character_names.append(name)
    payload["characters"] = [
        {"name": name, "source_evidence": name} for name in character_names
    ]
    for key in ("locations", "props"):
        label = _LABEL_BY_KEY[key]
        names = extract_story_table_names_strict(content, label)
        payload[key] = [{"name": name, "source_evidence": name} for name in names]
    return payload


def _contract_payload(content: str, source_text: str) -> tuple[dict[str, list[dict[str, str]]], str]:
    try:
        return parse_story_entities_block(content), "story_entities_json_v1"
    except ValueError:
        return _legacy_payload(content, source_text), "legacy_stage01_projection"


def _create_or_reuse_entity(
    director: Any,
    project_id: str,
    *,
    kind: str,
    name: str,
    evidence: str,
    policy: str,
) -> str:
    production = director.production
    existing = production.list_entities(project_id)
    current = next(
        (
            item for item in existing
            if _clean(item.get("entity_type")).lower() == kind
            and _norm(item.get("name")) == _norm(name)
        ),
        None,
    )
    if current is not None:
        return _clean(current.get("entity_id"))

    digest = hashlib.sha256(f"{kind}:{_norm(name)}".encode("utf-8")).hexdigest()[:16]
    created = production.create_entity(
        project_id,
        entity_type=kind,
        name=name,
        logical_key=f"stage01:{kind}:{digest}",
        stage="01",
        skill="xiaoduan-story-bible",
        metadata={
            "story_entity_contract": policy,
            "source_stage": "01",
            "materialized_from_validated_story_bible": True,
            "canonical_story_entity": True,
        },
        evidence={
            "type": "validated_stage01_entity_contract",
            "name": name,
            "source_evidence": evidence,
        },
    )
    return _clean(created.get("entity_id"))


def _retire_bad_entities(
    director: Any,
    project_id: str,
    payload: dict[str, list[dict[str, str]]],
) -> list[str]:
    production = director.production
    graph = production.get_graph(project_id)
    entities = graph.get("entities") or {}
    valid = {
        _KIND_BY_KEY[key]: {_norm(row["name"]) for row in payload[key]}
        for key in _MACHINE_KEYS
    }
    retired: set[str] = set()

    for entity_id, entity in entities.items():
        if not isinstance(entity, dict):
            continue
        kind = _clean(entity.get("entity_type")).lower()
        if kind not in {"character", "location", "prop"}:
            continue
        name = _clean(entity.get("name"))
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        contract = _clean(metadata.get("story_entity_contract"))
        projected = bool(metadata.get("materialized_from_validated_story_bible")) or contract.startswith(
            ("source_coverage", "story_entities_json", "legacy_stage01_projection")
        )
        invalid_projected = projected and _norm(name) not in valid[kind]
        if not _is_schema_label(name) and not invalid_projected:
            continue
        entity["entity_type"] = "retired_fragment"
        entity["stage"] = "01"
        metadata["hidden_from_normal_lists"] = True
        metadata["retired_story_entity"] = True
        metadata["retired_reason"] = "Stage01 字段标签/旧投影不属于当前故事实体合同"
        entity["metadata"] = metadata
        retired.add(str(entity_id))

    if not retired:
        return []

    for asset in (graph.get("assets") or {}).values():
        if not isinstance(asset, dict) or asset.get("active") is False:
            continue
        ids = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
        hits = ids & retired
        if not hits:
            continue
        roles = set().union(*_RETIRED_ASSET_ROLES.values())
        if _clean(asset.get("asset_role")) not in roles:
            continue
        asset["active"] = False
        asset["status"] = "archived"
        asset["dependency_state"] = "stale"
        asset.setdefault("metadata", {})["retired_reason"] = "上游错误故事实体已清理"

    production._save(graph)
    return sorted(retired)


def materialize_stage01_story_entities(
    director: Any,
    project_id: str,
    content: str,
    source_text: str,
) -> dict[str, Any]:
    payload, policy = _contract_payload(content, source_text)
    ids: dict[str, list[str]] = {key: [] for key in _MACHINE_KEYS}
    for key in _MACHINE_KEYS:
        kind = _KIND_BY_KEY[key]
        for row in payload[key]:
            entity_id = _create_or_reuse_entity(
                director,
                project_id,
                kind=kind,
                name=row["name"],
                evidence=row["source_evidence"],
                policy=policy,
            )
            if entity_id:
                ids[key].append(entity_id)

    retired = _retire_bad_entities(director, project_id, payload)
    active = director.production.list_entities(project_id)
    names_by_kind = {
        kind: sorted({
            _clean(item.get("name"))
            for item in active
            if _clean(item.get("entity_type")).lower() == kind and _clean(item.get("name"))
        })
        for kind in ("character", "location", "prop")
    }
    return {
        "project_id": project_id,
        "reconciled": True,
        "policy": policy,
        "character_entity_ids": ids["characters"],
        "location_entity_ids": ids["locations"],
        "prop_entity_ids": ids["props"],
        "character_count": len(ids["characters"]),
        "location_count": len(ids["locations"]),
        "prop_count": len(ids["props"]),
        "character_names": names_by_kind["character"],
        "location_names": names_by_kind["location"],
        "prop_names": names_by_kind["prop"],
        "retired_entity_ids": retired,
    }


def reconcile_stage01_story_entities(
    director: Any,
    project_id: str,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    project = director.get_project(project_id)
    completed = {_clean(item) for item in project.get("completed_stages") or []}
    state = ((project.get("stage_state") or {}).get("01") or {})
    runtime = state.get("skill_runtime") if isinstance(state.get("skill_runtime"), dict) else {}
    completion = runtime.get("completion") if isinstance(runtime.get("completion"), dict) else {}
    ready = (
        "01" in completed
        or bool(state.get("stage_ready"))
        or bool(completion.get("ready"))
        or _clean(project.get("current_stage")) not in {"", "01"}
    )
    if require_ready and not ready:
        return {
            "project_id": project_id,
            "reconciled": False,
            "reason": "stage01_not_ready",
            "character_entity_ids": [],
            "location_entity_ids": [],
            "prop_entity_ids": [],
            "character_count": 0,
            "location_count": 0,
            "prop_count": 0,
        }

    content = ""
    getter = getattr(director, "_latest_stage_output", None)
    if callable(getter):
        try:
            content = _clean(getter(project, "01"))
        except Exception:
            content = ""
    if not content:
        content = _clean(((project.get("confirmed_outputs") or {}).get("01") or {}).get("handoff"))
    if not content:
        return {
            "project_id": project_id,
            "reconciled": False,
            "reason": "stage01_output_unavailable",
            "character_entity_ids": [],
            "location_entity_ids": [],
            "prop_entity_ids": [],
            "character_count": 0,
            "location_count": 0,
            "prop_count": 0,
        }

    source_text = gate._project_authoritative_source(director, project, "01")
    return materialize_stage01_story_entities(director, project_id, content, source_text)


def install_story_entity_sanitizer(director: Any) -> dict[str, Any]:
    """Install a typed Stage01 entity contract and repair old heuristic output."""
    if getattr(coverage, "_xiaoduan_story_entity_sanitizer_installed", False):
        return {"policy": "story_entities_json_v1", "status": "already_installed"}

    # Strengthen the Stage01 writer contract before ProductionSkillRegistry is
    # installed. The existing bounded repair call will add/fix the block without
    # requiring another user interaction.
    stage01 = BUILTIN_PRODUCTION_SKILLS["xiaoduan-story-bible"]
    marker = "story-entities-json"
    if marker not in stage01:
        BUILTIN_PRODUCTION_SKILLS["xiaoduan-story-bible"] = stage01 + r"""

## 故事实体机器合同（强制）
人类可读的角色/地点/道具表之后，必须且只能输出一份以下机器块。它是后续 Entity Graph 的唯一身份清单；代码不得再从任意 Markdown 字段名猜实体：
```story-entities-json
{
  "characters": [
    {"name": "原文中的稳定角色名", "source_evidence": "原始故事中的短句原文"}
  ],
  "locations": [
    {"name": "稳定地点名", "source_evidence": "原始故事中的短句原文"}
  ],
  "props": [
    {"name": "稳定关键物件名", "source_evidence": "原始故事中的短句原文"}
  ]
}
```
- characters / locations / props 三个键必须全部存在，没有对应实体时使用空数组。
- 每项只能包含 name 与 source_evidence；source_evidence 必须是原始故事中的真实短句，不得改写或补造。
- `人物关系`、`涉及`、`剧情节点`、`原文证据` 等字段名绝不是实体。
- 机器块与上方三张实体表必须一一对应；任一真实角色不得遗漏。
"""

    original_validate = gate._validate_with_source

    def validate_with_typed_entities(skill: str, content: str, source_text: str) -> dict[str, Any]:
        check = original_validate(skill, content, source_text)
        if skill != "xiaoduan-story-bible":
            return check
        extra = story_entity_contract_issues(content, source_text)
        if not extra:
            return check
        patched = dict(check)
        current = list(patched.get("issues") or [])
        patched["issues"] = current + [item for item in extra if item not in current]
        patched["valid"] = False
        return patched

    def materialize_characters_compat(
        patched_director: Any,
        project_id: str,
        content: str,
        source_text: str,
    ) -> list[str]:
        result = materialize_stage01_story_entities(
            patched_director, project_id, content, source_text,
        )
        return list(result.get("character_entity_ids") or [])

    def reconcile_compat(
        patched_director: Any,
        project_id: str,
        *,
        require_ready: bool = True,
    ) -> dict[str, Any]:
        return reconcile_stage01_story_entities(
            patched_director,
            project_id,
            require_ready=require_ready,
        )

    gate._validate_with_source = validate_with_typed_entities
    coverage.extract_story_table_names = extract_story_table_names_strict
    coverage._materialize_stage01_characters = materialize_characters_compat
    coverage.reconcile_stage01_story_characters = reconcile_compat

    # production_authoring_assets imported the compatibility function before
    # this installer runs. Replace that module binding too so startup/status
    # reconciliation uses the typed contract immediately for existing projects.
    try:
        from . import production_authoring_assets as authoring_assets

        authoring_assets.reconcile_stage01_story_characters = reconcile_compat
    except Exception:
        pass

    coverage._xiaoduan_story_entity_sanitizer_installed = True
    return {
        "policy": "story_entities_json_v1",
        "status": "installed",
        "schema_labels_blocked": sorted(_SCHEMA_LABELS),
        "machine_contract": True,
        "legacy_projection": "read_only_compatibility",
    }


__all__ = [
    "extract_story_table_names_strict",
    "install_story_entity_sanitizer",
    "materialize_stage01_story_entities",
    "parse_story_entities_block",
    "reconcile_stage01_story_entities",
    "story_entity_contract_issues",
]
