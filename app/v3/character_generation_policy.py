from __future__ import annotations

import logging
import re
from typing import Any

from app.services.comfyui import STYLE_PRESETS
from app.services.prompt_compiler import (
    CompiledPrompt,
    PromptCompiler,
    _extract_visual_age,
    naturalize_visual_anchor,
)
from app.services.reference_templates import REFERENCE_TEMPLATES, ReferenceTemplate
from app.v3.character_reference_package import CharacterReferencePackageBootstrap, _atomic_facts


logger = logging.getLogger(__name__)

_EXPLICIT_GENDER_PATTERNS = (
    ("female", re.compile(r"(?:角色性别|性别呈现|性别|gender)\s*[：:=]\s*(?:女性|女|female|girl|woman)", re.IGNORECASE)),
    ("male", re.compile(r"(?:角色性别|性别呈现|性别|gender)\s*[：:=]\s*(?:男性|男|male|boy|man)", re.IGNORECASE)),
)
_FEMALE_PATTERNS = (
    re.compile(r"女性角色|女性人物|少女|女孩|女孩子|姑娘"),
    re.compile(r"\b(?:female character|female human|girl|woman)\b", re.IGNORECASE),
)
_MALE_PATTERNS = (
    re.compile(r"男性角色|男性人物|少年|男孩|男子"),
    re.compile(r"\b(?:male character|male human|boy|man)\b", re.IGNORECASE),
)
_NEGATED_GENDER = re.compile(
    r"(?:禁止|不要|不得|不能|不是|并非|非)\s*(?:生成|出现|变成|成为)?\s*"
    r"(?:男性角色|男性人物|男性|男人|男孩|男子|女性角色|女性人物|女性|女人|女孩|少女)"
    r"|\b(?:not|no|never)\s+(?:a\s+)?(?:male|man|boy|female|woman|girl)\b",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"未明确|未知|待角色设计|待设计|待补充|未描述|not specified|unknown", re.IGNORECASE)
_FACE_VALUE = re.compile(
    r"脸|面部|脸型|五官|眉|眼|鼻|嘴|唇|下颌|轮廓|肤|皮肤|"
    r"发型|发色|黑发|白发|银发|棕发|金发|长发|短发|束发|发髻|低髻|高髻|辫|盘发|披发|"
    r"face|facial|eye|brow|nose|mouth|jaw|skin|hair",
    re.IGNORECASE,
)
_HAIR_KEY = re.compile(r"发型|发色|头发|hair", re.IGNORECASE)
_HAIR_VALUE = re.compile(
    r"发|束|髻|辫|盘发|披发|卷发|直发|hair|ponytail|bun|braid|tied",
    re.IGNORECASE,
)
_FACE_NOISE = re.compile(
    r"长裙|短裙|裙|衣|袍|斗篷|披风|外套|鞋|靴|剑|剑鞘|刀|枪|铃|铃铛|青铜铃|玉佩|玉坠|项链|耳坠|"
    r"背景|场景|山|雪|建筑|道观|房间|桥|钟楼|手持|拿着|持有|腰间|背着|"
    r"dress|robe|clothes|clothing|cape|coat|shoe|boot|sword|scabbard|weapon|bell|pendant|background|mountain|temple|holding",
    re.IGNORECASE,
)
_HAIR_CLAUSE = re.compile(
    r"(?:黑发|白发|银发|棕发|金发|长发|短发|束发|发髻|低髻|高髻|辫|盘发|披发|卷发|直发|头发[^，,；;。、]{0,30}|hair[^,;.]{0,30})",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def infer_character_gender(text: str) -> str:
    """Resolve explicit character gender semantics; never infer from a name.

    Labelled gender wins. Contradictory labelled values fail closed before an
    expensive image generation task is launched. ``性别呈现`` is the canonical
    Stage02 field and therefore has the same authority as legacy ``性别``.
    """
    source = _text(text)
    labelled: set[str] = set()
    for value, pattern in _EXPLICIT_GENDER_PATTERNS:
        if pattern.search(source):
            labelled.add(value)
    if len(labelled) > 1:
        raise ValueError("角色性别设定冲突：同时存在男性和女性的显式性别字段")
    if labelled:
        return next(iter(labelled))

    probe = _NEGATED_GENDER.sub(" ", source)
    female = any(pattern.search(probe) for pattern in _FEMALE_PATTERNS)
    male = any(pattern.search(probe) for pattern in _MALE_PATTERNS)
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return ""


def _gender_constraints(gender: str) -> tuple[str, str]:
    if gender == "female":
        return (
            "STRICT CHARACTER IDENTITY: confirmed gender is female; render one female human character; "
            "female facial identity and female presentation appropriate to the confirmed age; gender must remain female; "
            "do not infer hairstyle from gender",
            "gender drift, wrong gender, male character, male face, man, boy, beard, moustache, masculine male identity",
        )
    if gender == "male":
        return (
            "STRICT CHARACTER IDENTITY: confirmed gender is male; render one male human character; "
            "male facial identity appropriate to the confirmed age; gender must remain male; "
            "do not infer hairstyle from gender",
            "gender drift, wrong gender, female character, female face, woman, girl, feminine female identity",
        )
    return "", ""


def _identity_source(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    return "\n".join(_atomic_facts(metadata))


def _age_label(source: str) -> str:
    age = _extract_visual_age(source)
    if age:
        low, high = age
        return f"{low}岁" if low == high else f"{low}-{high}岁"
    if re.search(r"少女|青少年女孩|teenage girl|adolescent girl", source, re.IGNORECASE):
        return "少女/青少年年龄感"
    if re.search(r"少年|青少年男孩|teenage boy|adolescent boy", source, re.IGNORECASE):
        return "少年/青少年年龄感"
    return ""


def _safe_face_facts(entity: dict[str, Any], limit: int = 12) -> list[str]:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    selected: list[str] = []
    for row in _atomic_facts(metadata):
        row = _text(row)
        if not row:
            continue
        if "：" in row:
            key, value = row.split("：", 1)
        elif ":" in row:
            key, value = row.split(":", 1)
        else:
            key, value = "", row
        value = _text(value)
        if not value or _PLACEHOLDER.search(value):
            continue

        # Upstream authoring can occasionally place clothing/prop content under
        # a hair heading. Consume semantic values, not the heading name alone.
        segments = [_text(part) for part in re.split(r"[，,；;。、]+", value) if _text(part)]
        for segment in segments:
            if _FACE_NOISE.search(segment) and not _FACE_VALUE.search(segment):
                continue
            semantic_face = bool(_FACE_VALUE.search(segment))
            semantic_hair = bool(_HAIR_KEY.search(key) and _HAIR_VALUE.search(segment))
            if not semantic_face and not semantic_hair:
                continue
            if segment not in selected:
                selected.append(segment)
    return selected[:limit]


def _explicit_hair_anchor(source: str) -> str:
    candidates: list[str] = []
    for clause in re.split(r"[\r\n；;。]+", source):
        clause = _text(clause)
        if not clause or _PLACEHOLDER.search(clause):
            continue
        if _FACE_NOISE.search(clause) and not _HAIR_CLAUSE.search(clause):
            continue
        match = _HAIR_CLAUSE.search(clause)
        if match:
            value = _text(match.group(0))
            if value and value not in candidates:
                candidates.append(value)
    return "；".join(candidates[:3])


def _prepend(value: str, addition: str) -> str:
    addition = _text(addition)
    value = _text(value)
    if not addition:
        return value
    if addition in value:
        return value
    return f"{addition}, {value}" if value else addition


def _face_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
    name = _text(entity.get("name")) or "角色"
    source = _identity_source(entity)
    gender = infer_character_gender(source)
    age = _age_label(source)
    facts = _safe_face_facts(entity)
    gender_cn = "女性" if gender == "female" else "男性" if gender == "male" else "未明确"
    identity_lines = [f"- 性别：{gender_cn}"]
    if age:
        identity_lines.append(f"- 视觉年龄：{age}")
    identity_lines.extend(f"- {fact}" for fact in facts)
    if len(identity_lines) == 1 and gender_cn == "未明确":
        identity_lines.append("- 只使用已确认的脸部、年龄与发型事实，不根据姓名猜测身份属性。")

    hard = ""
    if gender == "female":
        hard = "性别硬约束：角色是女性，必须生成女性人物，绝不能生成男性、男孩或男性面孔。"
    elif gender == "male":
        hard = "性别硬约束：角色是男性，必须生成男性人物，绝不能生成女性、女孩或女性面孔。"

    return (
        f"角色「{name}」身份锁脸锚点。\n"
        f"{hard}\n"
        "只锁定角色本人稳定可见的身份：性别、年龄感、脸型、五官、肤色和已经明确的发型。"
        "发型只能服从已确认事实；未确认时不得因为性别擅自添加长发、短发、卷发或现代发型。\n"
        "已确认身份事实：\n" + "\n".join(identity_lines) + "\n\n"
        "只生成一个角色的正面头肩/胸像身份肖像，正视镜头，中性自然表情，脸部占画面主要区域。"
        "正常真实人类面部解剖，双眼位置自然，鼻口下颌比例自然，皮肤纹理自然，五官清晰。"
        "纯净中性浅灰或米白背景，均匀柔和光线。"
        "本阶段禁止全身、剧情动作、持剑或其他道具、服装展示、山景、建筑、多人、三视图、文字、标签和水印。"
        "输出只作为后续服装与三视图的身份锚点。"
    )


def _install_face_prompt_guard() -> None:
    current = CharacterReferencePackageBootstrap._face_prompt
    if getattr(current, "_xiaoduan_identity_policy", False):
        return
    setattr(_face_prompt, "_xiaoduan_identity_policy", True)
    CharacterReferencePackageBootstrap._face_prompt = _face_prompt


def _install_costume_guard() -> None:
    current = CharacterReferencePackageBootstrap._costume_prompt
    if getattr(current, "_xiaoduan_identity_policy", False):
        return
    original = current

    def guarded(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
        source = _identity_source(entity)
        gender = infer_character_gender(source)
        positive, _ = _gender_constraints(gender)
        hair = _explicit_hair_anchor(source)
        prefix = positive
        if hair:
            prefix += f"; STRICT HAIRSTYLE ANCHOR: {hair}; hairstyle and hair length must not change"
        base = original(self, entity)
        return f"{prefix}.\n{base}" if prefix else base

    setattr(guarded, "_xiaoduan_identity_policy", True)
    CharacterReferencePackageBootstrap._costume_prompt = guarded


def _install_prompt_compiler_guard() -> None:
    current = PromptCompiler.compile
    if getattr(current, "_xiaoduan_identity_policy", False):
        return
    original = current

    def guarded(self: PromptCompiler, *args: Any, **kwargs: Any) -> CompiledPrompt:
        result = original(self, *args, **kwargs)
        kind = _text(kwargs.get("asset_kind")).lower()
        if kind != "character":
            return result

        contract = kwargs.get("contract")
        anchor = ""
        if contract is not None:
            anchor = naturalize_visual_anchor(getattr(contract, "identity_anchors", ""))
        source = "\n".join(
            part for part in (
                _text(kwargs.get("asset_description")),
                anchor,
                _text(kwargs.get("contract_context")),
            )
            if part
        )
        gender = infer_character_gender(source)
        gender_positive, gender_negative = _gender_constraints(gender)
        hair = _explicit_hair_anchor(source)
        hair_positive = (
            f"STRICT HAIRSTYLE ANCHOR: {hair}; preserve the confirmed hair length, color and hairstyle exactly; "
            "do not replace it with a gender stereotype"
            if hair else
            "HAIRSTYLE POLICY: no confirmed hairstyle means do not invent a conspicuously long, short or modern hairstyle from gender stereotypes"
        )
        positive = _prepend(result.positive_prompt, hair_positive)
        positive = _prepend(positive, gender_positive)
        negative = _prepend(result.negative_prompt, gender_negative)
        if gender:
            logger.info(
                "V3_CHARACTER_IDENTITY_CONTRACT gender=%s hair_anchor=%s",
                gender,
                hair or "<unspecified>",
            )
        return CompiledPrompt(positive_prompt=positive, negative_prompt=negative)

    setattr(guarded, "_xiaoduan_identity_policy", True)
    PromptCompiler.compile = guarded


def _remove_adult_style_bias() -> None:
    portrait = STYLE_PRESETS.get("portrait_photo")
    if not isinstance(portrait, dict):
        return
    positive = _text(portrait.get("positive"))
    portrait["positive"] = positive.replace(
        "single clearly visible adult subject when a person is requested",
        "single clearly visible person when a person is requested",
    )


def _strengthen_character_turnaround_template() -> None:
    current = REFERENCE_TEMPLATES.get("character")
    if current is None or "same confirmed gender identity" in current.positive:
        return
    REFERENCE_TEMPLATES["character"] = ReferenceTemplate(
        asset_type=current.asset_type,
        role=current.role,
        positive=(
            "same confirmed gender identity in every panel, gender identity must not change, "
            + current.positive
        ),
        negative=(
            "gender drift, gender swap, wrong gender presentation, "
            + current.negative
        ),
    )


def install_character_generation_policy() -> dict[str, Any]:
    """Install one strict identity boundary for every V3 character render.

    The policy follows the useful discipline from waoowaoo: stable identity facts
    remain separate from layout/scene instructions and the final provider prompt
    is complete. It also follows MoneyPrinterTurbo's explicit task/provider
    contract discipline: contradictory identity input fails before generation
    instead of silently falling back.
    """
    _remove_adult_style_bias()
    _strengthen_character_turnaround_template()
    _install_face_prompt_guard()
    _install_costume_guard()
    _install_prompt_compiler_guard()
    return {
        "installed": True,
        "gender_policy": "explicit_semantics_fail_closed",
        "hair_policy": "confirmed_only_no_gender_stereotype",
        "face_prompt_policy": "identity_only_no_scene_costume_prop",
        "portrait_age_bias_removed": True,
    }


__all__ = [
    "infer_character_gender",
    "install_character_generation_policy",
]
