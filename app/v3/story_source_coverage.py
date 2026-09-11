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

# Strong Chinese narrative subject signals. These are intentionally narrow:
# they are used only to detect obvious named characters that the Stage01 entity
# table must not silently drop. Location/prop completeness is read from the
# already-confirmed Stage01 entity tables instead of guessed from prose.
# The name group is lazy so `陆沉没有回答` resolves to `陆沉` + `没有`, not
# `陆沉没有` + `回答`.
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


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", _clean(value)).casefold()


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


def extract_story_table_names(text: str, label: str) -> list[str]:
    """Read stable names from one Stage01 entity-table section.

    Supports markdown tables, bullet lists and compact `甲；乙。` prose while
    rejecting schema/header words. This parser never invents identities.
    """
    body = _section(text, label)
    if not body:
        return []
    names: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidates: list[str] = []
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|") if cell.strip()]
            if cells:
                candidates.append(cells[0])
        else:
            line = re.sub(r"^[-*+]\s*", "", line)
            # Split only the compact identity-list part; explanatory prose after
            # the first sentence is not treated as additional identities.
            first_sentence = re.split(r"[。！？!?]", line, maxsplit=1)[0]
            candidates.extend(re.split(r"[；;、]", first_sentence))
            if len(candidates) == 1 and "：" in first_sentence:
                candidates = [first_sentence.split("：", 1)[0]]
            elif len(candidates) == 1 and ":" in first_sentence:
                candidates = [first_sentence.split(":", 1)[0]]
        for candidate in candidates:
            name = _plausible_entity_name(candidate)
            if name and name not in names:
                names.append(name)
    return names


def infer_source_character_candidates(source_text: str) -> list[str]:
    """Detect only obvious named narrative characters from natural prose.

    Repeated named subjects and explicit dialogue speakers are strong enough to
    enforce coverage. We deliberately do not run a broad NER guesser here: the
    goal is to catch omissions such as `沈璃`/`陆沉`, not turn scenery nouns into
    people.
    """
    source = _clean(source_text)
    if not source:
        return []
    repeated: list[str] = []
    strong: list[str] = []
    for pattern, sink in ((_CN_SUBJECT, repeated), (_EN_SUBJECT, repeated), (_CN_SPEAKER, strong)):
        for match in pattern.finditer(source):
            name = _plausible_entity_name(match.group(1))
            if name and name not in sink:
                sink.append(name)
    result: list[str] = []
    for name in strong:
        if name not in result:
            result.append(name)
    for name in repeated:
        # Repetition makes the subject signal robust against a one-off common
        # noun occurring at sentence start.
        if source.count(name) >= 2 and name not in result:
            result.append(name)
    return result


def _contains_name(text: str, name: str) -> bool:
    wanted = _norm(name)
    return bool(wanted and wanted in _norm(text))


def source_coverage_issues(skill_name: str, content: str, source_text: str) -> list[str]:
    """Bidirectional coverage checks missing from the original source guard.

    The existing guard prevents *invented* downstream identities. This function
    adds the opposite invariant: confirmed upstream identities may not silently
    disappear from Stage01/02/03 deliverables.
    """
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
        # Natural source detection is a second safety net for legacy Story Bible
        # text that was created before this completeness contract existed.
        for name in infer_source_character_candidates(source_text):
            if name not in required:
                required.append(name)
        actual = [name for name, _body in gate._heading_blocks(content, "角色")]
        for name in required:
            if not any(_norm(name) == _norm(item) for item in actual):
                issues.append(f"Stage02 遗漏已确认角色「{name}」；每个故事角色必须有且只有一个角色资产块")
        return issues

    if skill == "xiaoduan-visual-assets":
        for label, kind in (("地点实体表", "地点"), ("道具实体表", "道具")):
            required = extract_story_table_names(source_text, label)
            actual = [name for name, _body in gate._heading_blocks(content, kind)]
            for name in required:
                if not any(_norm(name) == _norm(item) for item in actual):
                    issues.append(f"Stage03 遗漏已确认{kind}「{name}」；不得静默丢失故事资产")
        return issues

    return issues


def _materialize_stage01_characters(director: Any, project_id: str, content: str, source_text: str) -> list[str]:
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
                "story_entity_contract": "source_coverage_v1",
                "source_stage": "01",
                "source_table": "角色实体表",
            },
            evidence={
                "type": "confirmed_stage01_entity_table",
                "name": name,
            },
        )
        existing.append(created)
        created_ids.append(_clean(created.get("entity_id")))
    return [item for item in created_ids if item]


def install_story_source_coverage(director: Any) -> dict[str, str]:
    """Harden the front-half gate and Stage01 entity materialization.

    Install after ``install_front_half_quality_gate``. The existing Qwen repair
    path then automatically receives the new missing-identity issues, while the
    confirmation wrapper guarantees that every confirmed character-table row is
    represented by a real Stage01 ProductionAsset entity.
    """
    if getattr(director, "_xiaoduan_story_source_coverage_installed", False):
        return {"policy": "source_coverage_v1", "status": "already_installed"}

    original_validate = gate._validate_with_source
    original_confirm = director.confirm_stage

    def enhanced_validate(skill: str, content: str, source_text: str) -> dict[str, Any]:
        check = original_validate(skill, content, source_text)
        extra = source_coverage_issues(skill, content, source_text)
        if extra:
            check = dict(check)
            check["issues"] = list(check.get("issues") or []) + [item for item in extra if item not in (check.get("issues") or [])]
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
                    "policy": "source_coverage_v1",
                    "character_entity_ids": materialized,
                    "character_count": len(materialized),
                }
        return result

    gate._validate_with_source = enhanced_validate
    director.confirm_stage = MethodType(confirm_with_story_entities, director)
    director._xiaoduan_story_source_coverage_installed = True
    return {"policy": "source_coverage_v1", "status": "installed"}


__all__ = [
    "extract_story_table_names",
    "infer_source_character_candidates",
    "install_story_source_coverage",
    "source_coverage_issues",
]
