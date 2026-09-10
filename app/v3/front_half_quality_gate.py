from __future__ import annotations

import json
import re
from types import MethodType
from typing import Any

from app.services.production_skills import BUILTIN_PRODUCTION_SKILLS


_FRONT_HALF_SKILLS = {
    "xiaoduan-story-bible": "01",
    "xiaoduan-character-assets": "02",
    "xiaoduan-visual-assets": "03",
}
_PLACEHOLDER = re.compile(
    r"^(?:未指定|未知|待设计|待补充|未描述|not specified|unknown)\s*$",
    re.I,
)
_ASSET_HEADING = re.compile(
    r"(?m)^\s{0,3}##\s+(角色|地点|道具)资产\s*[：:]\s*(.+?)\s*$"
)
_VISUAL_BLOCK = re.compile(
    r"```visual-direction-json\s*(\{.*?\})\s*```",
    re.I | re.S,
)
_APPEARANCE_BLOCK = re.compile(
    r"```appearance-versions-json\s*(\{.*?\})\s*```",
    re.I | re.S,
)
_SAFE_APPEARANCE_ID = re.compile(r"[A-Za-z0-9._-]{1,80}$")
_SECTION_MARKER = re.compile(r"(?m)^===\s*(.+?)\s*===\s*$")
_AUTHORITATIVE_PROMPT_SECTIONS = {
    "AUTHORITATIVE PREVIOUS CONFIRMED STAGE TEXT",
    "CONFIRMED UPSTREAM FACTS",
    "ORIGINAL PROJECT REQUEST",
    "ORIGINAL PROJECT REQUEST (BOUNDED)",
    "CURRENT USER MESSAGE",
    "CURRENT USER INPUT",
}
_TRANSIENT_APPEARANCE_TOKENS = (
    "参考图", "背景", "镜头", "构图", "景别", "机位", "动作", "表情", "姿势",
    "水印", "字幕", "标注", "箭头", "model sheet", "turnaround", "white background",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _contains(text: str, *tokens: str) -> bool:
    lowered = text.casefold()
    return any(token.casefold() in lowered for token in tokens)


def _normalized_identity(value: str) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", _clean(value)).casefold()


def _heading_blocks(content: str, kind: str) -> list[tuple[str, str]]:
    matches = list(_ASSET_HEADING.finditer(content))
    rows: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        if match.group(1) != kind:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        rows.append((_clean(match.group(2)), content[start:end].strip()))
    return rows


def _meaningful_string_leaves(value: Any) -> list[str]:
    rows: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            rows.extend(_meaningful_string_leaves(item))
    elif isinstance(value, list):
        for item in value:
            rows.extend(_meaningful_string_leaves(item))
    elif isinstance(value, str):
        text = _clean(value)
        if text and not _PLACEHOLDER.fullmatch(text):
            rows.append(text)
    return rows


def parse_visual_direction_block(content: str) -> dict[str, Any]:
    blocks = _VISUAL_BLOCK.findall(_clean(content))
    if not blocks:
        raise ValueError("缺少 ```visual-direction-json 机器视觉方向块")
    if len(blocks) != 1:
        raise ValueError("项目视觉圣经必须且只能包含一份 ```visual-direction-json 机器视觉方向块")
    try:
        payload = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"visual-direction-json 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("visual-direction-json 必须是 JSON 对象")

    expected = {
        "world_style",
        "culture",
        "era",
        "art_style",
        "character_rules",
        "environment_rules",
        "prop_rules",
        "negative_constraints",
    }
    missing = sorted(expected - set(payload))
    extra = sorted(set(payload) - expected)
    if missing:
        raise ValueError("visual-direction-json 缺少字段：" + ", ".join(missing))
    if extra:
        raise ValueError("visual-direction-json 包含未定义字段：" + ", ".join(extra))

    for key in ("world_style", "culture", "era", "art_style"):
        value = _clean(payload.get(key))
        if not value or _PLACEHOLDER.fullmatch(value):
            raise ValueError(f"visual-direction-json.{key} 必须是明确的项目视觉规则")
        payload[key] = value

    for key in ("character_rules", "environment_rules", "prop_rules"):
        value = payload.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"visual-direction-json.{key} 必须是对象")
        if not _meaningful_string_leaves(value):
            raise ValueError(f"visual-direction-json.{key} 至少需要一条明确、非占位的视觉规则")

    negatives = payload.get("negative_constraints")
    if not isinstance(negatives, list) or not all(
        isinstance(item, str) and item.strip() and not _PLACEHOLDER.fullmatch(item.strip())
        for item in negatives
    ):
        raise ValueError("visual-direction-json.negative_constraints 必须是非空明确字符串数组")
    normalized_negatives = list(dict.fromkeys(item.strip() for item in negatives))
    if not normalized_negatives:
        raise ValueError("visual-direction-json.negative_constraints 至少要有一条项目级禁止漂移约束")
    payload["negative_constraints"] = normalized_negatives
    return payload


def parse_character_appearance_versions(content: str) -> dict[str, list[dict[str, Any]]]:
    """Parse exact Stage02 appearance contracts by canonical character name."""
    result: dict[str, list[dict[str, Any]]] = {}
    for name, body in _heading_blocks(content, "角色"):
        blocks = _APPEARANCE_BLOCK.findall(body)
        if not blocks:
            raise ValueError(f"角色「{name}」缺少 ```appearance-versions-json 机器形象版本块")
        if len(blocks) != 1:
            raise ValueError(f"角色「{name}」必须且只能包含一份 ```appearance-versions-json 机器形象版本块")
        try:
            payload = json.loads(blocks[0])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"角色「{name}」appearance-versions-json 不是合法 JSON：{exc.msg}"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"versions"}:
            raise ValueError(f"角色「{name}」appearance-versions-json 只能包含 versions")
        versions = payload.get("versions")
        if not isinstance(versions, list) or not versions:
            raise ValueError(f"角色「{name}」至少需要一个形象版本")

        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(versions):
            if not isinstance(raw, dict):
                raise ValueError(f"角色「{name}」形象版本 #{index + 1} 必须是对象")
            expected = {
                "appearance_id",
                "name",
                "stable_design",
                "change_reason",
                "effective_story_node_ids",
            }
            missing = expected - set(raw)
            extra = set(raw) - expected
            if missing or extra:
                detail = []
                if missing:
                    detail.append("缺少 " + ", ".join(sorted(missing)))
                if extra:
                    detail.append("多余 " + ", ".join(sorted(extra)))
                raise ValueError(
                    f"角色「{name}」形象版本字段不合法：{'；'.join(detail)}"
                )

            aid = _clean(raw.get("appearance_id"))
            if not _SAFE_APPEARANCE_ID.fullmatch(aid):
                raise ValueError(
                    f"角色「{name}」appearance_id 必须为 1-80 位 ASCII 字母/数字/._-：{aid}"
                )
            if aid in seen:
                raise ValueError(f"角色「{name}」appearance_id 重复：{aid}")
            seen.add(aid)

            label = _clean(raw.get("name"))
            design = _clean(raw.get("stable_design"))
            reason = _clean(raw.get("change_reason"))
            nodes = raw.get("effective_story_node_ids")
            if not label or len(design) < 8 or not reason:
                raise ValueError(
                    f"角色「{name}」形象版本 {aid} 的 name/stable_design/change_reason 不完整"
                )
            polluted = [
                token for token in _TRANSIENT_APPEARANCE_TOKENS
                if token.casefold() in design.casefold()
            ]
            if polluted:
                raise ValueError(
                    f"角色「{name}」形象版本 {aid}.stable_design 混入瞬时/生成执行信息："
                    + ", ".join(polluted[:6])
                )
            if not isinstance(nodes, list) or not all(
                isinstance(item, str) and item.strip() for item in nodes
            ):
                raise ValueError(
                    f"角色「{name}」形象版本 {aid}.effective_story_node_ids 必须是字符串数组"
                )
            normalized.append({
                "appearance_id": aid,
                "name": label,
                "stable_design": design,
                "change_reason": reason,
                "effective_story_node_ids": [item.strip() for item in nodes],
            })
        if "default" not in seen:
            raise ValueError(
                f"角色「{name}」必须包含 appearance_id=default 的默认形象版本"
            )
        result[name] = normalized
    return result


def _validate_stage01(content: str) -> list[str]:
    text = _clean(content)
    issues: list[str] = []
    if len(text) < 500:
        issues.append("故事生产圣经过短，不能作为后续事实源")

    for label in (
        "故事定位",
        "不可篡改事实",
        "剧情节点",
        "角色实体表",
        "地点实体表",
        "道具实体表",
        "连续性事件",
        "创作计划",
    ):
        if label not in text:
            issues.append("故事生产圣经缺少：" + label)
    if "世界观" not in text or "时间线" not in text:
        issues.append("故事生产圣经必须同时覆盖：世界观 + 时间线")
    if "对白" not in text or "旁白" not in text:
        issues.append("故事生产圣经必须同时覆盖：对白 + 旁白（不存在时也要明确写无）")

    node_pattern = re.compile(
        r"(?:\b[A-Za-z]{1,8}[-_]?\d{1,4}\b|(?:剧情)?节点\s*(?:ID\s*[：:]?\s*)?[A-Za-z0-9][A-Za-z0-9._-]{1,20})",
        re.I,
    )
    if "剧情节点" in text and not node_pattern.search(text):
        issues.append("剧情节点缺少可稳定引用的节点 ID")

    if _contains(
        text,
        "最终图片 prompt",
        "最终图片提示词",
        "镜头焦段参数",
        "comfyui 参数",
    ):
        issues.append("Stage01 混入了后续媒体/镜头执行参数")
    return issues


def _validate_character_block(name: str, body: str) -> list[str]:
    issues: list[str] = []
    required = (
        ("性别呈现", "性别：", "性别:"),
        ("年龄", "岁"),
        ("脸部", "面部", "五官", "非人类形态"),
        ("发型", "无毛发", "非人类形态"),
        ("发色", "无毛发", "非人类形态"),
        ("肤色", "表面", "非人类形态"),
        ("体型", "身体轮廓"),
        ("身高感", "尺度感"),
        ("服装", "不适用（非人类）", "不适用(非人类)"),
        ("鞋履", "不适用（非人类）", "不适用(非人类)"),
        ("固定身份锚点",),
        ("允许变化项",),
        ("形象版本",),
        ("change_reason",),
        ("参考图生成要求",),
        ("原文证据",),
        ("设计补全", "补全来源"),
    )
    for aliases in required:
        if not any(alias in body for alias in aliases):
            issues.append(f"角色「{name}」缺少：{'/'.join(aliases)}")
    return issues


def _validate_stage02(content: str) -> list[str]:
    text = _clean(content)
    blocks = _heading_blocks(text, "角色")
    if not blocks:
        if "## 无角色资产" in text:
            return []
        return ["Stage02 没有任何 `## 角色资产：<稳定唯一名称>` 正式资产块"]
    issues: list[str] = []
    names: set[str] = set()
    for name, body in blocks:
        key = name.casefold()
        if not name:
            issues.append("角色资产标题缺少稳定名称")
            continue
        if key in names:
            issues.append(f"角色资产重复：{name}")
        names.add(key)
        issues.extend(_validate_character_block(name, body))
    try:
        parse_character_appearance_versions(text)
    except ValueError as exc:
        issues.append(str(exc))
    return issues


def _validate_location_block(name: str, body: str) -> list[str]:
    issues: list[str] = []
    required = (
        ("空间边界",),
        ("布局",),
        ("材质",),
        ("前景",),
        ("中景",),
        ("后景",),
        ("固定视觉锚点", "视觉锚点"),
        ("可变化状态",),
        ("参考图生成要求",),
    )
    for aliases in required:
        if not any(alias in body for alias in aliases):
            issues.append(f"地点「{name}」缺少：{'/'.join(aliases)}")
    anchor_rows = len(
        re.findall(
            r"(?m)^\s*[-*]\s*(?:锚点\s*\d+|固定视觉锚点\s*\d*)\s*[：:]",
            body,
        )
    )
    if "至少三个" not in body and anchor_rows and anchor_rows < 3:
        issues.append(f"地点「{name}」固定视觉锚点少于 3 个")
    return issues


def _validate_prop_block(name: str, body: str) -> list[str]:
    issues: list[str] = []
    required = (
        ("轮廓",),
        ("比例",),
        ("结构",),
        ("材质",),
        ("颜色",),
        ("尺度",),
        ("剧情功能",),
        ("change_reason",),
        ("参考图生成要求",),
    )
    for aliases in required:
        if not any(alias in body for alias in aliases):
            issues.append(f"道具「{name}」缺少：{'/'.join(aliases)}")
    return issues


def _validate_stage03(content: str) -> list[str]:
    text = _clean(content)
    issues: list[str] = []
    if "## 项目视觉圣经" not in text:
        issues.append("Stage03 缺少 `## 项目视觉圣经`")
    for aliases in (
        ("媒介与技法", "媒介/技法"),
        ("边缘与线条", "边缘/线条", "线条策略"),
        ("完成度与细节密度", "完成度/细节密度", "细节密度"),
        ("禁止漂移", "禁止项"),
    ):
        if not any(alias in text for alias in aliases):
            issues.append("项目视觉圣经缺少：" + "/".join(aliases))
    colors = set(re.findall(r"#[0-9A-Fa-f]{6}\b", text))
    if len(colors) < 3 or len(colors) > 6:
        issues.append("项目视觉圣经必须给出 3-6 个明确 HEX 色值及其用途")
    try:
        parse_visual_direction_block(text)
    except ValueError as exc:
        issues.append(str(exc))
    for name, body in _heading_blocks(text, "地点"):
        issues.extend(_validate_location_block(name, body))
    for name, body in _heading_blocks(text, "道具"):
        issues.extend(_validate_prop_block(name, body))
    return issues


def validate_front_half_output(skill_name: str, content: str) -> dict[str, Any]:
    skill = _clean(skill_name)
    if skill == "xiaoduan-story-bible":
        issues = _validate_stage01(content)
    elif skill == "xiaoduan-character-assets":
        issues = _validate_stage02(content)
    elif skill == "xiaoduan-visual-assets":
        issues = _validate_stage03(content)
    else:
        issues = []
    return {
        "valid": not issues,
        "skill": skill,
        "stage": _FRONT_HALF_SKILLS.get(skill, ""),
        "issues": issues,
    }


def _source_anchor_issues(skill_name: str, content: str, source_text: str) -> list[str]:
    """Ensure Stage02/03 identity names already exist in authoritative source text."""
    kind = "角色" if skill_name == "xiaoduan-character-assets" else None
    kinds = (
        [kind]
        if kind
        else ["地点", "道具"]
        if skill_name == "xiaoduan-visual-assets"
        else []
    )
    compact_source = _normalized_identity(source_text)
    issues: list[str] = []
    for item_kind in kinds:
        for name, _body in _heading_blocks(content, item_kind):
            wanted = _normalized_identity(name)
            if wanted and wanted not in compact_source:
                issues.append(
                    f"{item_kind}资产「{name}」未出现在真实上游故事/用户上下文，禁止创建新身份"
                )
    return issues


def _authoritative_sections(text: str) -> list[str]:
    matches = list(_SECTION_MARKER.finditer(str(text or "")))
    rows: list[str] = []
    for index, match in enumerate(matches):
        label = _clean(match.group(1)).upper()
        if label not in _AUTHORITATIVE_PROMPT_SECTIONS:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = _clean(text[start:end])
        if value and value not in {"<none>", "<omitted>"}:
            rows.append(value)
    return rows


def _authoritative_message_source(messages: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        rows.extend(_authoritative_sections(_clean(item.get("content"))))
    return "\n\n".join(rows)


def _project_authoritative_source(
    director: Any,
    project: dict[str, Any],
    stage: str,
) -> str:
    rows: list[str] = []
    confirmed = (project.get("confirmed_outputs") or {}).get("01") or {}
    if _clean(confirmed.get("handoff")):
        rows.append(_clean(confirmed.get("handoff")))
    for item in project.get("history") or []:
        if not isinstance(item, dict) or _clean(item.get("role")) != "user":
            continue
        if _clean(item.get("stage")) in {"01", stage} and _clean(item.get("content")):
            rows.append(_clean(item.get("content")))
    production = getattr(director, "production", None)
    if production is not None:
        try:
            for entity in production.list_entities(_clean(project.get("project_id"))):
                if _clean(entity.get("stage")) == "01" and _clean(entity.get("name")):
                    rows.append(_clean(entity.get("name")))
        except Exception:
            pass
    return "\n\n".join(rows)


def _validate_with_source(skill: str, content: str, source_text: str) -> dict[str, Any]:
    check = validate_front_half_output(skill, content)
    source_issues = _source_anchor_issues(skill, content, source_text)
    if source_issues:
        check["issues"].extend(source_issues)
        check["valid"] = False
    return check


def _extend_builtin_skill_contracts() -> None:
    stage01 = BUILTIN_PRODUCTION_SKILLS["xiaoduan-story-bible"]
    if "机器交付质量合同（强制）" not in stage01:
        BUILTIN_PRODUCTION_SKILLS["xiaoduan-story-bible"] = stage01 + r"""

## 机器交付质量合同（强制）
- 终态必须明确出现“故事定位、不可篡改事实、世界观与时间线、剧情节点、角色实体表、地点实体表、道具实体表、对白与旁白事实、连续性事件、创作计划”十类内容；没有实体也必须写“无”，不能省略整类。
- 剧情节点必须具有可稳定引用的节点 ID；世界观和时间线都要覆盖，对白和旁白都要覆盖，不存在时明确写“无”。
- 这是后续资产的唯一事实源。宁可把未知内容写“待设计/未指定”，也不能补造事实。
"""

    stage02 = BUILTIN_PRODUCTION_SKILLS["xiaoduan-character-assets"]
    if "角色身份字段合同（强制）" not in stage02:
        BUILTIN_PRODUCTION_SKILLS["xiaoduan-character-assets"] = stage02 + r"""

## 角色身份字段合同（强制）
- 每个人类角色必须显式写 `性别呈现`，只能来自故事事实或明确设计结论；不得依据姓名、服装、发型猜性别。非人类角色明确写 `性别呈现：不适用（非人类）`。
- 每个角色块必须显式写年龄/年龄感、脸部/五官、发型、发色、肤色、体型、身高感、服装、鞋履、固定身份锚点、允许变化项、形象版本、`change_reason`、参考图生成要求、原文证据、设计补全来源。
- 发型未被故事指定时可以做设计补全，但必须作为明确设计结论固定下来；不能因为男性/女性使用性别刻板印象自动补长发或短发。
- 角色名称必须逐字沿用故事生产圣经中的稳定名称；本阶段禁止新增故事生产圣经不存在、且用户本轮没有明确新增的角色实体。
- 稳定身份描述只写可见、可复用的身份/外观事实；允许变化项、剧情状态、证据说明、参考图版式不得混入 stable_design。
- 每个 `## 角色资产：名称` 块内必须且只放一份以下机器形象版本块。`appearance_id` 只能使用 ASCII 字母、数字、`.`、`_`、`-`，默认造型固定为 `default`：
```appearance-versions-json
{
  "versions": [
    {
      "appearance_id": "default",
      "name": "默认造型",
      "stable_design": "只写这个形象版本稳定可见的服装、发型、鞋履、配饰、颜色和体型差异",
      "change_reason": "角色基础造型",
      "effective_story_node_ids": []
    }
  ]
}
```
- `stable_design` 禁止混入背景、镜头、构图、动作、表情、姿势、字幕、水印或参考图版式。
- 剧情存在持续换装、伤势或阶段性造型时，必须在 versions 中增加独立版本；不能覆盖 default，也不能只写在说明文字里。
"""

    stage03 = BUILTIN_PRODUCTION_SKILLS["xiaoduan-visual-assets"]
    if "结构化视觉方向合同（强制）" not in stage03:
        BUILTIN_PRODUCTION_SKILLS["xiaoduan-visual-assets"] = stage03 + r"""

## 结构化视觉方向合同（强制）
项目视觉圣经除原有内容外，必须明确写出四类稳定视觉锚点：
1. `媒介与技法`：例如写实摄影、二维动画、水墨、3D CG，以及对应渲染/笔触方法。
2. `项目色板`：3-6 个明确 `#RRGGBB` 色值，每个色值写明主色/辅色/强调色/背景等用途。
3. `边缘与线条`：轮廓边缘、线稿、软硬边和局部锐度的统一原则。
4. `完成度与细节密度`：材质精度、纹理密度、背景细节和最终完成度原则。

并在 `## 项目视觉圣经` 内输出且只输出一份以下机器块；字段不能省略，键名不能改：
```visual-direction-json
{
  "world_style": "项目世界视觉类型",
  "culture": "文化/地域视觉来源",
  "era": "时代视觉约束",
  "art_style": "媒介、技法与最终画风",
  "character_rules": {"identity": "角色共同身份/造型规则"},
  "environment_rules": {"space": "地点共同空间/材质规则"},
  "prop_rules": {"design": "道具共同结构/材质规则"},
  "negative_constraints": ["至少一条项目级禁止漂移约束"]
}
```
- `character_rules`、`environment_rules`、`prop_rules` 都必须至少包含一条明确规则，不能使用空对象或占位值。
- 该 JSON 是项目级 Visual Direction 的唯一机器事实源；不得从项目名、角色名或模型默认值推断其中字段。
- 地点/道具名称必须逐字沿用故事生产圣经中的稳定名称；本阶段禁止新增故事生产圣经不存在、且用户本轮没有明确新增的地点或道具身份。
- 地点/道具 stable_design 只保存稳定空间/结构/材质事实；天气、人物位置、剧情功能、版本原因、参考图版式属于状态或执行元数据，不得污染稳定身份。
"""


def _detect_skill(system_prompt: str, messages: list[dict[str, Any]]) -> str:
    haystack = _clean(system_prompt) + "\n" + "\n".join(
        _clean(item.get("content")) for item in messages if isinstance(item, dict)
    )
    for skill in _FRONT_HALF_SKILLS:
        if skill in haystack:
            return skill
    return ""


def install_front_half_quality_gate(director: Any) -> None:
    """Validate and once-repair front-half Qwen output before project writes."""
    _extend_builtin_skill_contracts()
    if getattr(director, "_xiaoduan_front_half_quality_gate_installed", False):
        return
    original_chat = director._tracked_llm_chat
    original_confirm = director.confirm_stage

    async def guarded_chat(self: Any, *args: Any, **kwargs: Any):
        result = await original_chat(*args, **kwargs)
        phase = _clean(kwargs.get("phase"))
        if phase != "director_orchestrator_content":
            return result
        messages = kwargs.get("messages") if isinstance(kwargs.get("messages"), list) else []
        skill = _detect_skill(_clean(kwargs.get("system_prompt")), messages)
        if not skill:
            return result

        content = _clean((result or {}).get("content")) if isinstance(result, dict) else ""
        source_text = _authoritative_message_source(messages)
        check = _validate_with_source(skill, content, source_text)
        if check["valid"]:
            if isinstance(result, dict):
                result = dict(result)
                result["front_half_quality_gate"] = check
                result["front_half_contract_repair"] = {"attempted": False}
            return result

        # Formatting/contract omissions are production defects, not a reason to
        # make the user press Generate again. Perform exactly one bounded repair
        # from the same model transport, then re-run the deterministic gate.
        repair_system = """你是前半段生产合同修复器。你的任务不是重新创作，而是把一个已有生产结果修成可直接交付的完整终态。

规则：
1. 只输出修复后的完整最终 Markdown，不要解释、不要 JSON 外壳、不要列修复日志。
2. 保留 DRAFT 中已经正确的故事事实与设计结论；只修复 VALIDATION_ISSUES 指出的缺失、冲突和结构问题。
3. STORY/USER SOURCE 是唯一身份与故事事实来源。DRAFT 中出现但 SOURCE 不支持的人物、地点、道具身份必须删除，不得合理化。
4. 故事事实缺失时写“未指定/无”，不得补造；角色/视觉阶段允许对未指定外观做明确设计补全，但必须标明设计补全来源。
5. 必须严格满足 CURRENT_SKILL 的机器合同，包括机器代码块；不得把背景、镜头、动作、表情、版式写进 stable_design。
6. 不增加新的剧情事件、人物关系或结局。
"""
        repair_prompt = f"""CURRENT_SKILL={skill}

=== CURRENT SKILL ===
{BUILTIN_PRODUCTION_SKILLS.get(skill, '')}

=== VALIDATION_ISSUES ===
{json.dumps(check['issues'], ensure_ascii=False)}

=== AUTHORITATIVE STORY/USER SOURCE ===
{source_text or '<none>'}

=== DRAFT TO REPAIR ===
{content}

Return the complete repaired production result only.
"""
        repaired = await original_chat(
            phase="front_half_contract_repair",
            messages=[{"role": "user", "content": repair_prompt}],
            system_prompt=repair_system,
            temperature=0.2,
            max_tokens=max(800, int(kwargs.get("max_tokens") or 3200)),
        )
        repaired_content = (
            _clean(repaired.get("content")) if isinstance(repaired, dict) else ""
        )
        repaired_check = _validate_with_source(skill, repaired_content, source_text)
        if not repaired_check["valid"]:
            raise RuntimeError(
                "前半段生产结果自动修复后仍未通过确定性交付质量门，未写入项目："
                + json.dumps(
                    {
                        "initial_issues": check["issues"],
                        "repair_issues": repaired_check["issues"],
                    },
                    ensure_ascii=False,
                )
            )
        repaired = dict(repaired)
        repaired["front_half_quality_gate"] = repaired_check
        repaired["front_half_contract_repair"] = {
            "attempted": True,
            "initial_issue_count": len(check["issues"]),
            "resolved": True,
        }
        return repaired

    async def guarded_confirm(self: Any, project_id: str):
        project = self.get_project(project_id)
        stage = _clean(project.get("current_stage"))
        skill = next(
            (name for name, sid in _FRONT_HALF_SKILLS.items() if sid == stage),
            "",
        )
        if skill:
            content = _clean(self._latest_stage_output(project, stage))
            source_text = _project_authoritative_source(self, project, stage)
            check = _validate_with_source(skill, content, source_text)
            if not check["valid"]:
                raise RuntimeError(
                    f"阶段 {stage} 交付质量未闭合，禁止确认："
                    + json.dumps(check["issues"], ensure_ascii=False)
                )
        return await original_confirm(project_id)

    director._tracked_llm_chat = MethodType(guarded_chat, director)
    director.confirm_stage = MethodType(guarded_confirm, director)
    director._xiaoduan_front_half_quality_gate_installed = True


__all__ = [
    "install_front_half_quality_gate",
    "parse_character_appearance_versions",
    "parse_visual_direction_block",
    "validate_front_half_output",
]
