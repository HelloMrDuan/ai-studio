from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceEntity(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    source_evidence: str = Field(min_length=1, max_length=2000)


class StoryBibleOutput(StrictModel):
    schema_version: Literal[1]
    output_kind: Literal["story_bible"]
    document: str = Field(min_length=500, max_length=600000)
    characters: list[SourceEntity]
    locations: list[SourceEntity]
    props: list[SourceEntity]
    assumptions: list[str] = Field(default_factory=list, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=64)


class AppearanceVersion(StrictModel):
    appearance_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    name: str = Field(min_length=1, max_length=300)
    stable_design: str = Field(min_length=8, max_length=16000)
    change_reason: str = Field(min_length=1, max_length=2000)
    effective_story_node_ids: list[str] = Field(default_factory=list, max_length=512)


class CharacterAsset(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    stable_description: str = Field(min_length=8, max_length=16000)
    gender_presentation: str = Field(min_length=1, max_length=300)
    age: str = Field(min_length=1, max_length=300)
    face: str = Field(min_length=1, max_length=4000)
    hair_style: str = Field(min_length=1, max_length=4000)
    hair_color: str = Field(min_length=1, max_length=1000)
    skin_tone: str = Field(min_length=1, max_length=1000)
    body_type: str = Field(min_length=1, max_length=2000)
    height_impression: str = Field(min_length=1, max_length=1000)
    clothing: str = Field(min_length=1, max_length=5000)
    footwear: str = Field(min_length=1, max_length=2000)
    fixed_identity_anchors: list[str] = Field(min_length=1, max_length=64)
    allowed_variations: list[str] = Field(default_factory=list, max_length=64)
    appearances: list[AppearanceVersion] = Field(min_length=1, max_length=64)
    reference_requirements: str = Field(min_length=1, max_length=4000)
    source_evidence: str = Field(min_length=1, max_length=4000)
    design_basis: str = Field(min_length=1, max_length=4000)


class CharacterAssetsOutput(StrictModel):
    schema_version: Literal[1]
    output_kind: Literal["character_assets"]
    document: str = Field(min_length=300, max_length=600000)
    characters: list[CharacterAsset]
    assumptions: list[str] = Field(default_factory=list, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=64)


class VisualDirection(StrictModel):
    world_style: str = Field(min_length=1, max_length=4000)
    culture: str = Field(min_length=1, max_length=4000)
    era: str = Field(min_length=1, max_length=4000)
    art_style: str = Field(min_length=1, max_length=4000)
    character_rules: dict[str, str]
    environment_rules: dict[str, str]
    prop_rules: dict[str, str]
    negative_constraints: list[str] = Field(min_length=1, max_length=128)


class LocationAsset(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    stable_description: str = Field(min_length=8, max_length=16000)
    space_boundary: str = Field(min_length=1, max_length=4000)
    layout: str = Field(min_length=1, max_length=4000)
    materials: str = Field(min_length=1, max_length=4000)
    foreground: str = Field(min_length=1, max_length=3000)
    midground: str = Field(min_length=1, max_length=3000)
    background: str = Field(min_length=1, max_length=3000)
    fixed_visual_anchors: list[str] = Field(min_length=3, max_length=32)
    variable_states: list[str] = Field(default_factory=list, max_length=64)
    reference_requirements: str = Field(min_length=1, max_length=4000)
    source_evidence: str = Field(min_length=1, max_length=4000)


class PropAsset(StrictModel):
    name: str = Field(min_length=1, max_length=300)
    stable_description: str = Field(min_length=8, max_length=16000)
    silhouette: str = Field(min_length=1, max_length=3000)
    structure: str = Field(min_length=1, max_length=3000)
    materials: str = Field(min_length=1, max_length=3000)
    colors_patterns: str = Field(min_length=1, max_length=3000)
    fixed_wear: str = Field(min_length=1, max_length=2000)
    scale: str = Field(min_length=1, max_length=2000)
    reference_requirements: str = Field(min_length=1, max_length=4000)
    source_evidence: str = Field(min_length=1, max_length=4000)


class VisualAssetsOutput(StrictModel):
    schema_version: Literal[1]
    output_kind: Literal["visual_assets"]
    document: str = Field(min_length=500, max_length=600000)
    visual_direction: VisualDirection
    locations: list[LocationAsset]
    props: list[PropAsset]
    assumptions: list[str] = Field(default_factory=list, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=64)


_OUTPUT_MODELS: dict[str, type[StrictModel]] = {
    "story_bible": StoryBibleOutput,
    "character_assets": CharacterAssetsOutput,
    "visual_assets": VisualAssetsOutput,
}

_SKILL_OUTPUT_KIND = {
    "xiaoduan-story-bible": "story_bible",
    "xiaoduan-character-assets": "character_assets",
    "xiaoduan-visual-assets": "visual_assets",
}

_STAGE_OUTPUT_KIND = {"01": "story_bible", "02": "character_assets", "03": "visual_assets"}

_OUTPUT_ASSET_ROLE = {
    "story_bible": "professional_story_bible",
    "character_assets": "professional_character_assets",
    "visual_assets": "professional_visual_assets",
}

_TRANSIENT_STABLE_TOKENS = (
    "参考图", "背景", "镜头", "构图", "景别", "机位", "动作", "表情", "姿势",
    "水印", "字幕", "标注", "箭头", "model sheet", "turnaround", "white background",
)


def output_kind_for_skill(skill_name: str) -> str:
    return _SKILL_OUTPUT_KIND.get(str(skill_name or "").strip(), "")


def output_kind_for_stage(stage: str) -> str:
    return _STAGE_OUTPUT_KIND.get(str(stage or "").strip(), "")


def asset_role_for_output_kind(output_kind: str) -> str:
    return _OUTPUT_ASSET_ROLE.get(str(output_kind or "").strip(), "")


def professional_output_json_schema(output_kind: str) -> dict[str, Any]:
    model = _OUTPUT_MODELS.get(str(output_kind or "").strip())
    if model is None:
        raise ValueError(f"未知专业输出类型：{output_kind}")
    return model.model_json_schema()


def parse_professional_output(value: Any, *, expected_output_kind: str = "") -> dict[str, Any]:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"专业输出不是合法 JSON：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("专业输出必须是 JSON 对象")
    output_kind = str(value.get("output_kind") or "").strip()
    if expected_output_kind and output_kind != expected_output_kind:
        raise ValueError(
            f"专业输出 output_kind 不匹配：expected={expected_output_kind}, actual={output_kind or '<empty>'}"
        )
    model = _OUTPUT_MODELS.get(output_kind)
    if model is None:
        raise ValueError(f"未知专业输出类型：{output_kind or '<empty>'}")
    try:
        parsed = model.model_validate(value)
    except ValidationError as exc:
        raise ValueError("专业输出 Schema 校验失败：" + exc.json()) from exc
    return parsed.model_dump(mode="json")


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", str(value or "")).casefold()


def _compact_evidence(value: Any) -> str:
    return re.sub(
        r"[\s，。；：、“”‘’！？,.!?;:'\"（）()【】\[\]]+",
        "",
        str(value or ""),
    ).casefold()


def _duplicate_names(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicate: list[str] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        key = _norm(name)
        if key in seen and name not in duplicate:
            duplicate.append(name)
        seen.add(key)
    return duplicate


def _stable_pollution(text: str) -> list[str]:
    lowered = str(text or "").casefold()
    return [token for token in _TRANSIENT_STABLE_TOKENS if token.casefold() in lowered]


def validate_professional_output(
    payload: dict[str, Any],
    *,
    source_text: str,
    required_names: dict[str, list[str]] | None = None,
) -> list[str]:
    """Semantic checks around the strict schema; never infer new identities.

    The JSON Schema owns fields and shape. This function only verifies source
    lineage, identity completeness and stable-description boundaries that need
    the current project context.
    """
    kind = str(payload.get("output_kind") or "")
    issues: list[str] = []
    document = str(payload.get("document") or "")
    source_compact = _compact_evidence(source_text)
    required_names = required_names or {}

    entity_groups: list[tuple[str, list[dict[str, Any]]]] = []
    if kind == "story_bible":
        entity_groups = [
            ("characters", list(payload.get("characters") or [])),
            ("locations", list(payload.get("locations") or [])),
            ("props", list(payload.get("props") or [])),
        ]
    elif kind == "character_assets":
        entity_groups = [("characters", list(payload.get("characters") or []))]
    elif kind == "visual_assets":
        entity_groups = [
            ("locations", list(payload.get("locations") or [])),
            ("props", list(payload.get("props") or [])),
        ]

    for group_name, rows in entity_groups:
        for duplicate in _duplicate_names(rows):
            issues.append(f"{group_name} 实体重复：{duplicate}")
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name and _norm(name) not in _norm(document):
                issues.append(f"{group_name} 实体「{name}」未出现在 document，展示文本与机器事实不一致")
            evidence = str(row.get("source_evidence") or "").strip()
            if evidence and source_compact and _compact_evidence(evidence) not in source_compact:
                issues.append(f"{group_name} 实体「{name}」的 source_evidence 不是权威上游中的逐字事实")
            stable = str(row.get("stable_description") or "").strip()
            if stable:
                polluted = _stable_pollution(stable)
                if polluted:
                    issues.append(
                        f"{group_name} 实体「{name}」stable_description 混入瞬时/生成信息："
                        + ", ".join(polluted[:6])
                    )

    for group_name, required in required_names.items():
        actual_rows = dict(entity_groups).get(group_name, [])
        actual = {_norm(row.get("name")) for row in actual_rows}
        for name in required:
            if _norm(name) and _norm(name) not in actual:
                issues.append(f"{group_name} 遗漏已确认实体「{name}」")

    if kind == "character_assets":
        for row in payload.get("characters") or []:
            appearances = list(row.get("appearances") or [])
            ids = [str(item.get("appearance_id") or "") for item in appearances]
            if "default" not in ids:
                issues.append(f"角色「{row.get('name')}」缺少 appearance_id=default")
            if len(ids) != len(set(ids)):
                issues.append(f"角色「{row.get('name')}」appearance_id 重复")
            for item in appearances:
                polluted = _stable_pollution(str(item.get("stable_design") or ""))
                if polluted:
                    issues.append(
                        f"角色「{row.get('name')}」形象版本 {item.get('appearance_id')} 混入瞬时/生成信息："
                        + ", ".join(polluted[:6])
                    )

    if kind == "visual_assets":
        direction = payload.get("visual_direction") or {}
        for rule_key in ("character_rules", "environment_rules", "prop_rules"):
            rules = direction.get(rule_key)
            if not isinstance(rules, dict) or not any(str(value or "").strip() for value in rules.values()):
                issues.append(f"visual_direction.{rule_key} 至少需要一条明确规则")

    return list(dict.fromkeys(issues))


PROFESSIONAL_OUTPUT_REGISTRY = {
    skill: {
        "output_kind": output_kind,
        "asset_role": _OUTPUT_ASSET_ROLE[output_kind],
        "schema": _OUTPUT_MODELS[output_kind],
    }
    for skill, output_kind in _SKILL_OUTPUT_KIND.items()
}


__all__ = [
    "PROFESSIONAL_OUTPUT_REGISTRY",
    "asset_role_for_output_kind",
    "output_kind_for_skill",
    "output_kind_for_stage",
    "parse_professional_output",
    "professional_output_json_schema",
    "validate_professional_output",
]
