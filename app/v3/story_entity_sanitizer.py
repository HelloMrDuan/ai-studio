from __future__ import annotations

import re
from typing import Any

from . import story_source_coverage as coverage


# Field/schema labels may legitimately appear inside the Stage01 character
# section, but they are never reusable character identities.
_SCHEMA_LABELS = {
    "角色", "角色名称", "角色名", "人物", "人物名称", "姓名", "名称", "身份", "稳定身份",
    "人物关系", "角色关系", "关系", "涉及", "涉及剧情节点", "剧情节点", "关联剧情节点", "节点",
    "原文证据", "证据", "来源", "来源说明", "备注", "说明", "类型", "年龄", "年龄感", "性别",
    "性别呈现", "外观", "脸部", "面部", "发型", "发色", "肤色", "体型", "身高", "身高感",
    "服装", "鞋履", "配饰", "固定身份锚点", "允许变化项", "形象版本", "参考图生成要求",
}

_RETIRED_ASSET_ROLES = {
    "character_profile",
    "character_appearance",
    "character_reference",
    "character_turnaround",
    "character_consistency",
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
            "人物关系",
            "角色关系",
            "涉及剧情节点",
            "关联剧情节点",
            "原文证据",
            "参考图生成要求",
            "固定身份锚点",
            "允许变化项",
        )
    )


def _append_name(names: list[str], candidate: Any) -> None:
    raw = _clean(candidate)
    raw = re.sub(r"^#{1,6}\s*", "", raw)
    raw = re.sub(r"^\d+[.)、]\s*", "", raw)
    raw = re.sub(r"^[-*+]\s*", "", raw)
    raw = re.sub(
        r"\s*[（(](?:主角|男主|女主|配角|反派|角色)[^）)]*[）)]\s*$",
        "",
        raw,
    )
    if not raw or _is_schema_label(raw):
        return
    value = coverage._plausible_entity_name(raw)
    if not value or _is_schema_label(value) or coverage._looks_like_table_id(value):
        return
    if value not in names:
        names.append(value)


def extract_story_table_names_strict(text: str, label: str) -> list[str]:
    """Extract only entity identities, never field labels, from a Stage01 table.

    Prefer an explicit Markdown name column when present. For non-table layouts,
    support compact name lists and per-character headings, while ignoring schema
    rows such as ``人物关系：...`` or ``涉及：N01``.
    """
    body = coverage._section(text, label)
    if not body:
        return []

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    headers = {_norm(item) for item in coverage._TABLE_NAME_HEADERS.get(label, ("名称",))}
    table_name_index: int | None = None
    table_header_line = -1

    # First pass: if this is a Markdown table, locate the declared name column.
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

    # Non-table forms: headings, explicit `角色名称：X`, `X：身份...`, or
    # compact `沈璃；陆沉。`. Arbitrary field labels are ignored.
    for line in lines:
        if line.startswith("|"):
            continue
        if re.match(r"^#{1,6}\s+", line):
            _append_name(names, line)
            continue

        value = re.sub(r"^[-*+]\s*", "", line)
        value = re.sub(r"^\d+[.)、]\s*", "", value)
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

        # A plain single line can be a compact entity name, but never a schema
        # label. This preserves simple Stage01 outputs such as `陆沉。`.
        _append_name(names, first_sentence)

    return names


def _valid_stage01_character_names(content: str, source_text: str) -> list[str]:
    section = coverage._section(content, "角色实体表")
    names = extract_story_table_names_strict(content, "角色实体表")
    for name in coverage.infer_source_character_candidates(source_text):
        if coverage._contains_name(section, name) and name not in names:
            names.append(name)
    return names


def _retire_invalid_stage01_characters(
    director: Any,
    project_id: str,
    *,
    valid_names: list[str],
) -> list[str]:
    production = getattr(director, "production", None)
    if production is None:
        return []
    graph = production.get_graph(project_id)
    entities = graph.get("entities") or {}
    valid = {_norm(item) for item in valid_names if _norm(item)}
    retired_ids: set[str] = set()

    for entity_id, entity in entities.items():
        if not isinstance(entity, dict) or _clean(entity.get("entity_type")).lower() != "character":
            continue
        name = _clean(entity.get("name"))
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        owned_by_story_projection = bool(metadata.get("materialized_from_validated_story_bible")) or _clean(
            metadata.get("story_entity_contract")
        ).startswith("source_coverage")
        obviously_schema = _is_schema_label(name)
        invalid_projected = owned_by_story_projection and _norm(name) not in valid
        if not obviously_schema and not invalid_projected:
            continue

        entity["entity_type"] = "retired_fragment"
        entity["stage"] = "01"
        metadata["hidden_from_normal_lists"] = True
        metadata["retired_story_entity"] = True
        metadata["retired_reason"] = "Stage01 字段/模式标签曾被误识别为角色实体"
        entity["metadata"] = metadata
        retired_ids.add(str(entity_id))

    if not retired_ids:
        return []

    for asset in (graph.get("assets") or {}).values():
        if not isinstance(asset, dict) or asset.get("active") is False:
            continue
        ids = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
        if not (ids & retired_ids):
            continue
        if _clean(asset.get("asset_role")) not in _RETIRED_ASSET_ROLES:
            continue
        asset["active"] = False
        asset["status"] = "archived"
        asset["dependency_state"] = "stale"
        asset.setdefault("metadata", {})["retired_reason"] = "上游误识别角色已清理"

    production._save(graph)
    return sorted(retired_ids)


def install_story_entity_sanitizer(director: Any) -> dict[str, Any]:
    """Patch Stage01 extraction in-place and repair already-created bad roles."""
    if getattr(coverage, "_xiaoduan_story_entity_sanitizer_installed", False):
        return {"policy": "story_entity_sanitizer_v1", "status": "already_installed"}

    original_materialize = coverage._materialize_stage01_characters

    def materialize_with_cleanup(
        patched_director: Any,
        project_id: str,
        content: str,
        source_text: str,
    ) -> list[str]:
        created = original_materialize(patched_director, project_id, content, source_text)
        valid_names = _valid_stage01_character_names(content, source_text)
        _retire_invalid_stage01_characters(
            patched_director,
            project_id,
            valid_names=valid_names,
        )
        # Return only currently active character ids. Historical bad ids may have
        # been created by an older run and are deliberately excluded.
        active_ids = {
            _clean(item.get("entity_id"))
            for item in patched_director.production.list_entities(project_id, "character")
        }
        return [item for item in created if item in active_ids]

    coverage.extract_story_table_names = extract_story_table_names_strict
    coverage._materialize_stage01_characters = materialize_with_cleanup
    coverage._xiaoduan_story_entity_sanitizer_installed = True

    return {
        "policy": "story_entity_sanitizer_v1",
        "status": "installed",
        "schema_labels_blocked": sorted(_SCHEMA_LABELS),
    }


__all__ = [
    "extract_story_table_names_strict",
    "install_story_entity_sanitizer",
]
