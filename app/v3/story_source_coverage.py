from __future__ import annotations

import hashlib
import re
from types import MethodType
from typing import Any

from . import front_half_quality_gate as gate


_ENTITY_STOP_WORDS = {
    "角色", "角色名称", "角色名", "人物", "人物名称", "名称", "姓名", "身份", "类型",
    "地点", "地点名称", "场景", "场景名称", "道具", "道具名称", "原文", "原文证据",
    "剧情节点", "节点", "说明", "备注", "无", "未指定", "未知", "待设计", "待补充",
}
_SECTION_LABELS = (
    "故事定位",
    "不可篡改事实",
    "世界观与时间线",
    "世界观",
    "时间线",
    "剧情节点",
    "角色实体表",
    "地点实体表",
    "道具实体表",
    "对白与旁白事实",
    "对白",
    "旁白",
    "连续性事件",
    "创作计划",
)
_TABLE_NAME_HEADERS = {
    "角色实体表": ("稳定唯一名称", "角色名称", "人物名称", "姓名", "名称", "角色", "人物"),
    "地点实体表": ("稳定唯一名称", "地点名称", "场景名称", "名称", "地点", "场景"),
    "道具实体表": ("稳定唯一名称", "道具名称", "关键物件", "物件名称", "名称", "道具", "物件"),
}

# Natural-prose coverage is only a safety net for obvious character omissions.
# It must be conservative: a false positive blocks Stage01 entirely. Therefore
# candidates are accepted only when they are explicit dialogue speakers or are
# observed as named subjects in at least two distinct narrative units.
_CN_SUBJECT = re.compile(
    r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4}?)"
    r"(?=(?:独自|站|坐|走|跑|来到|进入|离开|抬手|抬头|提着|提灯|握住|握紧|握|"
    r"看向|看了|看|望向|望|问|回答|答|说道|说|喊|笑|哭|转身|回头|勒马|翻身|"
    r"下马|拔剑|拔|按住|跟上|跟|穿过|听见|听到|没有|已经))"
)
_CN_SPEAKER = re.compile(
    r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4}?)"
    r"(?:问|回答|答道|说道|说|喊道|喊|低声道|轻声道|沉声道|冷声道|笑道)"
)
_EN_SUBJECT = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b"
    r"(?=\s+(?:stood|sat|walked|ran|entered|left|looked|asked|answered|replied|said|"
    r"shouted|turned|drew|held|followed|heard)\b)"
)
_CN_CAPTURED_MODIFIERS = (
    "没有", "已经", "正在", "仍然", "依然", "忽然", "突然",
    "独自", "轻声", "低声", "沉声", "冷声", "笑着",
)
_CN_NON_NAME_MARKERS = ("的", "从", "把", "被")
_CN_NON_NAME_PREFIXES = ("他", "她", "它", "只")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", _clean(value)).casefold()


def _trim_cn_candidate(value: str) -> str:
    """Remove compact narrative modifiers accidentally absorbed into a name."""
    text = _clean(value)
    changed = True
    while changed:
        changed = False
        for suffix in sorted(_CN_CAPTURED_MODIFIERS, key=len, reverse=True):
            if text.endswith(suffix) and len(text) - len(suffix) >= 2:
                text = text[:-len(suffix)]
                changed = True
                break
    return text


def _narrative_cn_name(value: str) -> str:
    """Return a conservative Chinese proper-name candidate or empty string."""
    text = _plausible_entity_name(_trim_cn_candidate(value))
    if not text:
        return ""
    if any(marker in text for marker in _CN_NON_NAME_MARKERS):
        return ""
    if text.startswith(_CN_NON_NAME_PREFIXES):
        return ""
    return text


def _unique_narrative_units(source_text: str) -> list[str]:
    """Deduplicate story sentences before counting subject evidence."""
    rows: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[。！？!?；;\n]+", _clean(source_text)):
        unit = raw.strip(" \t\r\n\"'“”‘’|#>*_`-")
        key = _norm(unit)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(unit)
    return rows


def _section(text: str, label: str) -> str:
    source = _clean(text)
    start = source.find(label)
    if start < 0:
        return ""
    start += len(label)
    ends = []
    for other in _SECTION_LABELS:
        if other == label:
            continue
        pos = source.find(other, start)
        if pos >= 0:
            ends.append(pos)
    end = min(ends) if ends else len(source)
    return source[start:end].strip()


def _plausible_entity_name(value: str) -> str:
    text = _clean(value).strip("|`*_#> -\t：:，,。；;（）()[]【】")
    text = re.sub(r"^(?:角色|人物|地点|场景|道具)(?:名称|名)?\s*[：:]\s*", "", text)
    text = text.strip("|`*_#> -\t：:，,。；;")
    if not text or text in _ENTITY_STOP_WORDS:
        return ""
    if len(text) > 40:
        return ""
    if any(token in text for token in ("实体表", "证据", "说明", "备注", "关联", "节点", "稳定唯一名称")):
        return ""
    if re.fullmatch(r"[-:|\s]+", text):
        return ""
    return text


def _looks_like_table_id(value: str) -> bool:
    text = _clean(value)
    return bool(
        re.fullmatch(r"(?:[A-Za-z]{0,8}[-_.]?)?\d{1,6}", text)
        or re.fullmatch(r"[A-Za-z]{1,8}[-_.]?\d{1,6}", text)
        or re.fullmatch(r"N\d+(?:[-_.]\d+)*", text, flags=re.I)
    )


def extract_story_table_names(text: str, label: str) -> list[str]:
    """Read stable names from one Stage01 entity-table section.

    Supports markdown tables (including an ID column before the name), bullet
    lists and compact `甲；乙。` prose. Schema/header cells and row IDs are never
    treated as identities.
    """
    body = _section(text, label)
    if not body:
        return []
    names: list[str] = []
    table_name_index: int | None = None
    headers = {_norm(item) for item in _TABLE_NAME_HEADERS.get(label, ("名称",))}

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidates: list[str] = []
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not cells or all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell or "") for cell in cells):
                continue
            if table_name_index is None:
                normalized = [_norm(cell) for cell in cells]
                for index, cell in enumerate(normalized):
                    if cell in headers:
                        table_name_index = index
                        break
                if table_name_index is not None:
                    # This row is the table header itself.
                    continue
            if table_name_index is not None and table_name_index < len(cells):
                candidates.append(cells[table_name_index])
            else:
                # Legacy tables without a recognizable header: select the first
                # plausible non-ID cell rather than blindly taking column zero.
                for cell in cells:
                    value = _plausible_entity_name(cell)
                    if value and not _looks_like_table_id(value):
                        candidates.append(value)
                        break
        else:
            line = re.sub(r"^[-*+]\s*", "", line)
            first_sentence = re.split(r"[。！？!?]", line, maxsplit=1)[0]
            pieces = re.split(r"[；;、]", first_sentence)
            if len(pieces) > 1:
                candidates.extend(pieces)
            else:
                separator = "：" if "：" in first_sentence else ":" if ":" in first_sentence else ""
                if separator:
                    left, right = first_sentence.split(separator, 1)
                    if _norm(left) in headers:
                        candidates = [right]
                    else:
                        candidates = [left]
                else:
                    candidates = [first_sentence]
        for candidate in candidates:
            name = _plausible_entity_name(candidate)
            if name and not _looks_like_table_id(name) and name not in names:
                names.append(name)
    return names


def infer_source_character_candidates(source_text: str) -> list[str]:
    """Detect only high-confidence named characters from natural prose.

    The gate intentionally prefers a false negative over a false positive:
    Stage01 already performs semantic extraction, while this function exists to
    stop obvious silent omissions. Duplicate prompt copies never increase the
    evidence count.
    """
    units = _unique_narrative_units(source_text)
    if not units:
        return []

    strong: list[str] = []
    subject_units: dict[str, set[str]] = {}
    english_units: dict[str, set[str]] = {}

    for unit in units:
        unit_key = _norm(unit)
        for match in _CN_SPEAKER.finditer(unit):
            name = _narrative_cn_name(match.group(1))
            if name and name not in strong:
                strong.append(name)
        for match in _CN_SUBJECT.finditer(unit):
            name = _narrative_cn_name(match.group(1))
            if name:
                subject_units.setdefault(name, set()).add(unit_key)
        for match in _EN_SUBJECT.finditer(unit):
            name = _plausible_entity_name(match.group(1))
            if name:
                english_units.setdefault(name, set()).add(unit_key)

    result: list[str] = []
    for name in strong:
        if name not in result:
            result.append(name)
    for bucket in (subject_units, english_units):
        for name, evidence in bucket.items():
            if len(evidence) >= 2 and name not in result:
                result.append(name)
    return result


def _contains_name(text: str, name: str) -> bool:
    wanted = _norm(name)
    return bool(wanted and wanted in _norm(text))


def source_coverage_issues(skill_name: str, content: str, source_text: str) -> list[str]:
    """Bidirectional coverage checks missing from the original source guard."""
    skill = _clean(skill_name)
    issues: list[str] = []

    if skill == "xiaoduan-story-bible":
        character_section = _section(content, "角色实体表")
        for name in infer_source_character_candidates(source_text):
            if not _contains_name(character_section, name):
                issues.append(f"角色实体表遗漏真实故事角色「{name}」；必须从原始故事补回，禁止静默丢角色")
        return issues

    if skill == "xiaoduan-character-assets":
        required = extract_story_table_names(source_text, "角色实体表")
        for name in infer_source_character_candidates(source_text):
            if name not in required:
                required.append(name)
        actual = [name for name, _body in gate._heading_blocks(content, "角色")]
        for name in required:
            if not any(_norm(name) == _norm(item) for item in actual):
                issues.append(f"Stage02 遗漏已确认角色「{name}」；每个故事角色必须有且只有一个角色资产块")
        return issues

    if skill == "xiaoduan-visual-assets":
        for table_label, kind in (("地点实体表", "地点"), ("道具实体表", "道具")):
            required = extract_story_table_names(source_text, table_label)
            actual = [name for name, _body in gate._heading_blocks(content, kind)]
            for name in required:
                if not any(_norm(name) == _norm(item) for item in actual):
                    issues.append(f"Stage03 遗漏已确认{kind}「{name}」；不得静默丢失故事资产")
        return issues

    return issues


def _materialize_stage01_characters(
    director: Any,
    project_id: str,
    content: str,
    source_text: str,
) -> list[str]:
    """Project validated Stage01 character rows into the shared Entity graph."""
    production = getattr(director, "production", None)
    if production is None:
        return []
    section = _section(content, "角色实体表")
    required = extract_story_table_names(content, "角色实体表")
    for name in infer_source_character_candidates(source_text):
        if _contains_name(section, name) and name not in required:
            required.append(name)

    existing = production.list_entities(project_id)
    created_ids: list[str] = []
    for name in required:
        # Stage01 can contain design labels, so do not create a character unless
        # the canonical name is actually present in the authoritative source.
        if not _contains_name(source_text, name):
            continue
        current = next(
            (
                item for item in existing
                if _clean(item.get("entity_type")).lower() == "character"
                and _norm(item.get("name")) == _norm(name)
            ),
            None,
        )
        if current is not None:
            created_ids.append(_clean(current.get("entity_id")))
            continue
        digest = hashlib.sha256(_norm(name).encode("utf-8")).hexdigest()[:16]
        created = production.create_entity(
            project_id,
            entity_type="character",
            name=name,
            logical_key=f"stage01:character:{digest}",
            stage="01",
            skill="xiaoduan-story-bible",
            metadata={
                "story_entity_contract": "source_coverage_v2",
                "source_stage": "01",
                "source_table": "角色实体表",
                "materialized_from_validated_story_bible": True,
            },
            evidence={
                "type": "validated_stage01_entity_table",
                "name": name,
            },
        )
        existing.append(created)
        created_ids.append(_clean(created.get("entity_id")))
    return [item for item in created_ids if item]


def reconcile_stage01_story_characters(
    director: Any,
    project_id: str,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Synchronize validated Stage01 roles into the graph before Stage02.

    Single-pass authoring reaches ``stage_ready`` before the user presses the
    manual confirm button. The story-elements panel reads the shared Entity
    graph at that point, so confirmation-only projection is too late. This
    reconciler is idempotent and is safe from both the ready-state finalizer and
    authoring-asset status/startup reconciliation paths.
    """
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
            "character_count": 0,
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
    if not content or not _section(content, "角色实体表"):
        return {
            "project_id": project_id,
            "reconciled": False,
            "reason": "stage01_character_table_unavailable",
            "character_entity_ids": [],
            "character_count": 0,
        }

    source_text = gate._project_authoritative_source(director, project, "01")
    ids = _materialize_stage01_characters(director, project_id, content, source_text)
    names = [
        _clean(item.get("name"))
        for item in director.production.list_entities(project_id, "character")
        if _clean(item.get("name"))
    ]
    required = [
        name for name in infer_source_character_candidates(source_text)
        if _contains_name(_section(content, "角色实体表"), name)
    ]
    missing = [name for name in required if not any(_norm(name) == _norm(item) for item in names)]
    if missing:
        raise RuntimeError("Stage01 角色实体图同步失败，仍缺少：" + "、".join(missing))
    return {
        "project_id": project_id,
        "reconciled": True,
        "policy": "source_coverage_v2",
        "character_entity_ids": ids,
        "character_count": len(ids),
        "character_names": names,
        "required_character_names": required,
    }


def install_story_source_coverage(director: Any) -> dict[str, str]:
    """Harden front-half validation and materialize validated Stage01 roles."""
    if getattr(director, "_xiaoduan_story_source_coverage_installed", False):
        return {"policy": "source_coverage_v2", "status": "already_installed"}

    original_validate = gate._validate_with_source
    original_confirm = director.confirm_stage

    def enhanced_validate(skill: str, content: str, source_text: str) -> dict[str, Any]:
        check = original_validate(skill, content, source_text)
        extra = source_coverage_issues(skill, content, source_text)
        if extra:
            check = dict(check)
            check["issues"] = list(check.get("issues") or []) + [
                item for item in extra if item not in (check.get("issues") or [])
            ]
            check["valid"] = False
        return check

    async def confirm_with_story_entities(self: Any, project_id: str):
        project = self.get_project(project_id)
        stage = _clean(project.get("current_stage"))
        content = _clean(self._latest_stage_output(project, stage)) if stage == "01" else ""
        source_text = gate._project_authoritative_source(self, project, stage) if stage == "01" else ""
        result = await original_confirm(project_id)
        if stage == "01" and content:
            materialized = _materialize_stage01_characters(self, project_id, content, source_text)
            if isinstance(result, dict):
                result = dict(result)
                result["stage01_source_coverage"] = {
                    "policy": "source_coverage_v2",
                    "character_entity_ids": materialized,
                    "character_count": len(materialized),
                }
        return result

    gate._validate_with_source = enhanced_validate
    director.confirm_stage = MethodType(confirm_with_story_entities, director)
    director._xiaoduan_story_source_coverage_installed = True
    return {"policy": "source_coverage_v2", "status": "installed"}


__all__ = [
    "extract_story_table_names",
    "infer_source_character_candidates",
    "install_story_source_coverage",
    "reconcile_stage01_story_characters",
    "source_coverage_issues",
]
