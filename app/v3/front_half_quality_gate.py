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
_PLACEHOLDER = re.compile(r"^(?:未指定|未知|待设计|待补充|not specified|unknown)\s*$", re.I)
_ASSET_HEADING = re.compile(r"(?m)^\s{0,3}##\s+(角色|地点|道具)资产\s*[：:]\s*(.+?)\s*$")
_VISUAL_BLOCK = re.compile(
    r"```visual-direction-json\s*(\{.*?\})\s*```",
    re.I | re.S,
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _contains(text: str, *tokens: str) -> bool:
    lowered = text.casefold()
    return any(token.casefold() in lowered for token in tokens)


def _heading_blocks(content: str, kind: str) -> list[tuple[str, str]]:
    matches = list(_ASSET_HEADING.finditer(content))
    rows: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        if match.group(1) != kind:
            continue
        start = match.end()
        end = len(content)
        for later in matches[index + 1 :]:
            end = later.start()
            break
        rows.append((_clean(match.group(2)), content[start:end].strip()))
    return rows


def parse_visual_direction_block(content: str) -> dict[str, Any]:
    match = _VISUAL_BLOCK.search(_clean(content))
    if not match:
        raise ValueError("缺少 ```visual-direction-json 机器视觉方向块")
    try:
        payload = json.loads(match.group(1))
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
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"visual-direction-json.{key} 必须是对象")
    negatives = payload.get("negative_constraints")
    if not isinstance(negatives, list) or not all(isinstance(item, str) and item.strip() for item in negatives):
        raise ValueError("visual-direction-json.negative_constraints 必须是非空字符串数组")
    if not negatives:
        raise ValueError("visual-direction-json.negative_constraints 至少要有一条项目级禁止漂移约束")
    return payload


def _validate_stage01(content: str) -> list[str]:
    text = _clean(content)
    issues: list[str] = []
    if len(text) < 500:
        issues.append("故事生产圣经过短，不能作为后续事实源")
    required = (
        ("故事定位",),
        ("不可篡改事实",),
        ("世界观", "时间线"),
        ("剧情节点",),
        ("角色实体表",),
        ("地点实体表",),
        ("道具实体表",),
        ("对白", "旁白"),
        ("连续性事件",),
        ("创作计划",),
    )
    for aliases in required:
        if not any(alias in text for alias in aliases):
            issues.append("故事生产圣经缺少：" + "/".join(aliases))
    if _contains(text, "最终图片 prompt", "最终图片提示词", "镜头焦段参数", "comfyui 参数"):
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
    anchor_rows = len(re.findall(r"(?m)^\s*[-*]\s*(?:锚点\s*\d+|固定视觉锚点\s*\d*)\s*[：:]", body))
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


def _extend_builtin_skill_contracts() -> None:
    stage01 = BUILTIN_PRODUCTION_SKILLS["xiaoduan-story-bible"]
    if "机器交付质量合同（强制）" not in stage01:
        BUILTIN_PRODUCTION_SKILLS["xiaoduan-story-bible"] = stage01 + r"""

## 机器交付质量合同（强制）
- 终态必须明确出现“故事定位、不可篡改事实、世界观与时间线、剧情节点、角色实体表、地点实体表、道具实体表、对白与旁白事实、连续性事件、创作计划”十类内容；没有实体也必须写“无”，不能省略整类。
- 这是后续资产的唯一事实源。宁可把未知内容写“待设计/未指定”，也不能补造事实。
"""

    stage02 = BUILTIN_PRODUCTION_SKILLS["xiaoduan-character-assets"]
    if "角色身份字段合同（强制）" not in stage02:
        BUILTIN_PRODUCTION_SKILLS["xiaoduan-character-assets"] = stage02 + r"""

## 角色身份字段合同（强制）
- 每个人类角色必须显式写 `性别呈现`，只能来自故事事实或明确设计结论；不得依据姓名、服装、发型猜性别。非人类角色明确写 `性别呈现：不适用（非人类）`。
- 每个角色块必须显式写年龄/年龄感、脸部/五官、发型、发色、肤色、体型、身高感、服装、鞋履、固定身份锚点、允许变化项、形象版本、`change_reason`、参考图生成要求、原文证据、设计补全来源。
- 发型未被故事指定时可以做设计补全，但必须作为明确设计结论固定下来；不能因为男性/女性使用性别刻板印象自动补长发或短发。
- 角色名称必须逐字沿用故事生产圣经中的稳定名称；本阶段禁止新增故事生产圣经不存在的角色实体。
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
- 该 JSON 是项目级 Visual Direction 的唯一机器事实源；不得从项目名、角色名或模型默认值推断其中字段。
- 地点/道具名称必须逐字沿用故事生产圣经中的稳定名称；本阶段禁止新增故事生产圣经不存在的地点或道具身份。
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
    """Install deterministic, fail-closed validation before front-half output is persisted.

    This follows the same boundary discipline as a strict output schema: Qwen is
    still the creative writer, but incomplete or structurally unsafe output is
    rejected before control entities/assets/handoffs are written. No mock or
    second semantic model judges the content.
    """
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
        check = validate_front_half_output(skill, content)
        if not check["valid"]:
            raise RuntimeError(
                "前半段生产结果未通过确定性交付质量门，未写入项目："
                + json.dumps(check, ensure_ascii=False)
            )
        if isinstance(result, dict):
            result = dict(result)
            result["front_half_quality_gate"] = check
        return result

    async def guarded_confirm(self: Any, project_id: str):
        project = self.get_project(project_id)
        stage = _clean(project.get("current_stage"))
        skill = next((name for name, sid in _FRONT_HALF_SKILLS.items() if sid == stage), "")
        if skill:
            content = _clean(self._latest_stage_output(project, stage))
            check = validate_front_half_output(skill, content)
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
    "parse_visual_direction_block",
    "validate_front_half_output",
]
