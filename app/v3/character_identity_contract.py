from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.visual_direction import VisualDirection


_PLACEHOLDER = re.compile(
    r"未明确|未知|待角色设计|待设计|待补充|未描述|not specified|unknown",
    re.IGNORECASE,
)
_GENDER_LABEL = re.compile(
    r"(?:角色性别|性别呈现|性别|gender)\s*[：:=]\s*"
    r"(女性|女|female|girl|woman|男性|男|male|boy|man)",
    re.IGNORECASE,
)
_FEMALE = re.compile(r"女性角色|女性人物|少女|女孩|姑娘|\b(?:female|girl|woman)\b", re.IGNORECASE)
_MALE = re.compile(r"男性角色|男性人物|少年|男孩|男子|\b(?:male|boy|man)\b", re.IGNORECASE)
_NEGATED_GENDER = re.compile(
    r"(?:禁止|不要|不得|不能|不是|并非|非)\s*(?:生成|出现|变成|成为)?\s*"
    r"(?:男性角色|男性人物|男性|男人|男孩|男子|女性角色|女性人物|女性|女人|女孩|少女)"
    r"|\b(?:not|no|never)\s+(?:a\s+)?(?:male|man|boy|female|woman|girl)\b",
    re.IGNORECASE,
)
_FACE_SIGNAL = re.compile(
    r"性别|gender|年龄|岁|少年|少女|teen|adolescent|"
    r"脸|面部|脸型|五官|眉|眼|鼻|嘴|唇|下颌|轮廓|肤色|皮肤|skin|face|facial|eye|brow|nose|mouth|jaw|"
    r"发型|发色|头发|黑发|白发|银发|棕发|金发|长发|短发|束发|发髻|低髻|高髻|辫|盘发|披发|"
    r"簪|发钗|发冠|发带|hair|ponytail|bun|braid|hairpin|hair ornament|headband",
    re.IGNORECASE,
)
_COSTUME_SIGNAL = re.compile(
    r"性别|gender|年龄|岁|身高|体型|肩|腰|body|build|height|"
    r"服装|上衣|下装|衣|袍|裙|斗篷|披风|外套|鞋|靴|腰带|配饰|饰品|首饰|配色|颜色|材质|纹样|刺绣|"
    r"clothes|clothing|robe|dress|cape|coat|shoe|boot|belt|accessory|jewelry|outfit|garment|material|color|pattern|embroidery",
    re.IGNORECASE,
)
_FACE_NOISE = re.compile(
    r"服装|衣|袍|裙|斗篷|披风|外套|鞋|靴|腰带|"
    r"剑|剑鞘|刀|枪|铃|铃铛|玉佩|玉坠|项链|手持|拿着|持有|腰间|背着|"
    r"背景|场景|山|雪|建筑|房间|桥|钟楼|动作|姿势|镜头|构图|"
    r"clothes|clothing|robe|dress|cape|coat|shoe|boot|belt|sword|scabbard|weapon|bell|pendant|necklace|"
    r"background|scene|mountain|building|holding|full[- ]?body",
    re.IGNORECASE,
)
_COSTUME_NOISE = re.compile(
    r"手持|拿着|挥|战斗|剧情|背景|场景|山|雪|建筑|房间|桥|钟楼|镜头|构图|"
    r"holding|action|battle|background|scene|mountain|building|camera|composition",
    re.IGNORECASE,
)
_STORY_PROP = re.compile(
    r"剑|剑鞘|刀|枪|铃|铃铛|玉佩|玉坠|手持|拿着|背着|"
    r"sword|scabbard|weapon|blade|bell|pendant|holding|carrying",
    re.IGNORECASE,
)
_HAIR_ORNAMENT = re.compile(r"簪|发钗|发冠|发带|hairpin|hair ornament|headband|hair ribbon", re.IGNORECASE)
_HAIR = re.compile(
    r"(?:黑色?|白色?|银色?|棕色?|金色?)?\s*(?:长发|短发|束发|发髻|低髻|高髻|辫|盘发|披发|卷发|直发)"
    r"|(?:头发|发型|发色)[^,，;；。\n]{0,48}|hair[^,;.\n]{0,48}",
    re.IGNORECASE,
)
_AGE = re.compile(r"(?<!\d)(\d{1,3})(?:\s*[-~—–至到]\s*(\d{1,3}))?\s*岁")
_PERIOD_SIGNAL = re.compile(
    r"古代|古风|古式|古装|汉服|长袍|交领|襦裙|武侠|仙侠|"
    r"ancient|historical|period|traditional\s+(?:robe|garment)|\brobe\b",
    re.IGNORECASE,
)
_EAST_ASIAN_SIGNAL = re.compile(
    r"中国|中华|中式|东亚|武侠|仙侠|chinese|east\s*asian|xianxia|wuxia",
    re.IGNORECASE,
)
_PROP_TERMS = (
    ("剑鞘", "scabbard"), ("长剑", "sword"), ("古剑", "sword"), ("剑", "sword"),
    ("刀", "blade"), ("枪", "weapon"), ("青铜铃", "bronze bell"), ("铃铛", "bell"),
    ("铃", "bell"), ("玉佩", "jade pendant"), ("玉坠", "jade pendant"),
    ("sword", "sword"), ("scabbard", "scabbard"), ("bell", "bell"),
    ("pendant", "pendant"), ("weapon", "weapon"),
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _atomic_rows(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    rows: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_atomic_rows(item, next_prefix, depth + 1))
        return rows
    if isinstance(value, list):
        for item in value[:24]:
            rows.extend(_atomic_rows(item, prefix, depth + 1))
        return rows
    text = _text(value)
    if not text:
        return rows
    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"^[\s\-*#>]+", "", line).strip()
        if line:
            rows.append(f"{prefix}：{line}" if prefix and len(text.splitlines()) <= 1 else line)
    return rows


def _split_row(row: str) -> tuple[str, str]:
    text = _text(row)
    if "：" in text:
        key, value = text.split("：", 1)
        return key.strip(), value.strip()
    if ":" in text:
        key, value = text.split(":", 1)
        return key.strip(), value.strip()
    return "", text


def _segments(value: str) -> list[str]:
    return [
        part.strip(" -\t")
        for part in re.split(r"[\r\n,，;；。/／|、]+", _text(value))
        if part.strip(" -\t")
    ]


def _find_character_contract(value: Any, depth: int = 0) -> dict[str, Any]:
    """Find the typed Stage02 character contract inside the formal profile payload.

    Production profiles nest it under stable_profile/专业角色合同. Do not rely on
    flattened prose when a typed contract is already available.
    """
    if depth > 5 or not isinstance(value, dict):
        return {}
    for key in ("专业角色合同", "typed_character_contract", "character_contract"):
        item = value.get(key)
        if isinstance(item, dict):
            return item
    for item in value.values():
        found = _find_character_contract(item, depth + 1)
        if found:
            return found
    return {}


def _structured_face_facts(metadata: dict[str, Any]) -> list[str]:
    contract = _find_character_contract(metadata)
    if not contract:
        return []
    selected: list[str] = []
    for key in ("脸部", "发型", "发色", "肤色"):
        value = _text(contract.get(key))
        if not value or _PLACEHOLDER.search(value):
            continue
        for segment in _segments(value):
            if not segment or _PLACEHOLDER.search(segment):
                continue
            if key == "脸部" and _FACE_NOISE.search(segment) and not _HAIR_ORNAMENT.search(segment):
                continue
            if segment not in selected:
                selected.append(segment)
    return selected


def _period_cue(metadata: dict[str, Any]) -> str:
    source = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    if not _PERIOD_SIGNAL.search(source):
        return "只保留低存在感且符合项目时代的基础上衣领。"
    if _EAST_ASIAN_SIGNAL.search(source):
        return "可见服装只保留低存在感的古代东亚传统上衣领口，与已确认古式服装处于同一时代文化体系。"
    return "可见服装只保留低存在感、与已确认历史服装同一时代体系的传统上衣领口。"


def infer_character_gender(text: str) -> str:
    source = _text(text)
    labelled: set[str] = set()
    for match in _GENDER_LABEL.finditer(source):
        value = match.group(1).lower()
        labelled.add("female" if value in {"女性", "女", "female", "girl", "woman"} else "male")
    if len(labelled) > 1:
        raise ValueError("角色性别设定冲突：同时存在男性和女性的显式性别字段")
    if labelled:
        return next(iter(labelled))
    probe = _NEGATED_GENDER.sub(" ", source)
    female = bool(_FEMALE.search(probe))
    male = bool(_MALE.search(probe))
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return ""


def extract_age_label(text: str) -> str:
    match = _AGE.search(_text(text))
    if match:
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else low
        if 1 <= low <= 120 and 1 <= high <= 120:
            return f"{low}岁" if low == high else f"{min(low, high)}-{max(low, high)}岁"
    source = _text(text)
    if re.search(r"少女|teenage girl|adolescent girl", source, re.IGNORECASE):
        return "少女/青少年年龄感"
    if re.search(r"少年|teenage boy|adolescent boy", source, re.IGNORECASE):
        return "少年/青少年年龄感"
    return ""


def strict_face_facts(metadata: dict[str, Any], limit: int = 14) -> list[str]:
    selected: list[str] = list(_structured_face_facts(metadata))
    for row in _atomic_rows(metadata):
        key, value = _split_row(row)
        if not value or _PLACEHOLDER.search(value):
            continue
        for segment in _segments(value):
            if not segment or _PLACEHOLDER.search(segment):
                continue
            hair_ornament = bool(_HAIR_ORNAMENT.search(segment))
            if _FACE_NOISE.search(segment) and not hair_ornament:
                continue
            if not (_FACE_SIGNAL.search(segment) or _FACE_SIGNAL.search(key) or hair_ornament):
                continue
            if segment not in selected:
                selected.append(segment)
    return selected[:limit]


def strict_costume_facts(metadata: dict[str, Any], limit: int = 18) -> list[str]:
    """Project only body/clothing/shoes/wearable design, never independent props."""
    selected: list[str] = []
    for row in _atomic_rows(metadata):
        key, value = _split_row(row)
        if not value or _PLACEHOLDER.search(value):
            continue
        for segment in _segments(value):
            if not segment or _PLACEHOLDER.search(segment):
                continue
            if _STORY_PROP.search(segment):
                continue
            if _COSTUME_NOISE.search(segment):
                continue
            if not (_COSTUME_SIGNAL.search(segment) or _COSTUME_SIGNAL.search(key)):
                continue
            if segment not in selected:
                selected.append(segment)
    return selected[:limit]


def project_character_anchor(raw: str, phase: str) -> str:
    source = _text(raw)
    current = _text(phase).lower()
    if not source or current not in {"face_anchor", "costume"}:
        return source
    selected: list[str] = []
    signal = _FACE_SIGNAL if current == "face_anchor" else _COSTUME_SIGNAL
    noise = _FACE_NOISE if current == "face_anchor" else _COSTUME_NOISE
    for row in re.split(r"[;；\n]+", source):
        key, value = _split_row(row)
        for segment in _segments(value):
            if not segment or _PLACEHOLDER.search(segment):
                continue
            hair_ornament = current == "face_anchor" and bool(_HAIR_ORNAMENT.search(segment))
            if current == "costume" and _STORY_PROP.search(segment):
                continue
            if noise.search(segment) and not hair_ornament:
                continue
            if not (signal.search(segment) or signal.search(key) or hair_ornament):
                continue
            if segment not in selected:
                selected.append(segment)
    return "; ".join(selected)


def build_face_anchor_prompt(entity: dict[str, Any]) -> str:
    name = _text(entity.get("name")) or "角色"
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    source = "\n".join(_atomic_rows(metadata))
    gender = infer_character_gender(source)
    age = extract_age_label(source)
    identity: list[str] = []
    if gender:
        identity.append(f"性别：{'女性' if gender == 'female' else '男性'}")
    if age:
        identity.append(f"视觉年龄：{age}")
    identity.extend(strict_face_facts(metadata))
    identity = list(dict.fromkeys(item for item in identity if item))
    if not identity:
        identity.append("仅使用上游已确认的脸部、年龄与发型事实，不依据姓名补造身份")
    collar = _period_cue(metadata)
    return (
        f"角色「{name}」身份锚点。\n"
        "冻结范围仅包括稳定可见身份：性别呈现、年龄感、脸型、五官、肤色、发型、发色和明确发饰。\n"
        "已确认身份事实：\n- " + "\n- ".join(identity) + "\n\n"
        "生成单人正面头肩身份肖像，视线自然朝向镜头，中性自然表情，脸部占画面主要区域。"
        "双眼、鼻、口、下颌比例自然，皮肤纹理清晰自然。"
        f"{collar}背景为干净浅灰或米白，画面只服务于身份锁定。"
    )


def build_costume_reference_prompt(entity: dict[str, Any]) -> str:
    name = _text(entity.get("name")) or "角色"
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    facts = strict_costume_facts(metadata)
    fact_text = "\n- ".join(facts) if facts else "只使用上游已确认的体型、服装、鞋履与可穿戴配饰"
    return (
        f"角色「{name}」服装定装参考。\n"
        "脸部身份和发型只继承已采用的 Face Anchor，本阶段不重新设计脸。\n"
        "稳定服装事实：\n- " + fact_text + "\n\n"
        "生成同一角色的单人全身正面中性站姿，从头到脚完整可见。"
        "只设计体型轮廓、服装层次、材质、配色、鞋履和真正属于角色穿戴系统的配饰。"
        "背景保持干净中性，人物身份与服装结构清晰可复用。"
    )


def _period_flags(direction: VisualDirection, source: str = "") -> tuple[bool, bool]:
    payload = json.dumps(
        {
            "world_style": direction.world_style,
            "culture": direction.culture,
            "era": direction.era,
            "art_style": direction.art_style,
            "character_rules": direction.character_rules,
        },
        ensure_ascii=False,
    ).lower()
    combined = payload + "\n" + _text(source).lower()
    ancient = bool(_PERIOD_SIGNAL.search(combined))
    chinese = bool(_EAST_ASIAN_SIGNAL.search(combined))
    return ancient, chinese


def _hair_anchor(source: str) -> str:
    matches: list[str] = []
    for clause in re.split(r"[\r\n;；。]+", _text(source)):
        match = _HAIR.search(clause)
        if match:
            value = _text(match.group(0))
            if value and value not in matches:
                matches.append(value)
    return "；".join(matches[:3])


def _story_prop_negatives(source: str) -> list[str]:
    lowered = source.lower()
    values: list[str] = []
    for token, normalized in _PROP_TERMS:
        if token.lower() in lowered and normalized not in values:
            values.append(normalized)
    return values


@dataclass(frozen=True)
class CharacterPromptConstraints:
    positive: tuple[str, ...]
    negative: tuple[str, ...]


def compile_character_constraints(
    *,
    asset_description: str,
    identity_anchors: str,
    contract_context: str,
    visual_direction: VisualDirection,
    reference_phase: str = "",
) -> CharacterPromptConstraints:
    source = "\n".join(
        part for part in (_text(asset_description), _text(identity_anchors), _text(contract_context)) if part
    )
    gender = infer_character_gender(source)
    age = extract_age_label(source)
    hair = _hair_anchor(source)
    phase = _text(reference_phase).lower()
    positive: list[str] = []
    negative: list[str] = []

    if gender == "female":
        positive.append("STRICT CHARACTER IDENTITY: confirmed gender is female; gender must remain female")
        negative.append("gender drift, wrong gender, male character, male face, man, boy, beard, moustache")
    elif gender == "male":
        positive.append("STRICT CHARACTER IDENTITY: confirmed gender is male; gender must remain male")
        negative.append("gender drift, wrong gender, female character, female face, woman, girl")
    if age:
        positive.append(f"STRICT VISUAL AGE: {age}; preserve the confirmed apparent age")
        negative.append("age drift, visibly older or younger than the confirmed age")
    if hair:
        positive.append(f"STRICT HAIRSTYLE ANCHOR: {hair}; preserve confirmed hair length, color, style and ornaments")
    else:
        positive.append("HAIRSTYLE POLICY: no confirmed hairstyle means do not invent one from gender stereotypes")

    ancient, chinese = _period_flags(visual_direction, source)
    if phase == "face_anchor":
        positive.append(
            "FACE ANCHOR ISOLATION: one head-and-shoulders identity portrait; face, confirmed age, confirmed gender and confirmed hairstyle are the only identity priorities"
        )
        if ancient and chinese:
            positive.append("visible neckline is a simple conservative ancient East Asian traditional robe collar")
        elif ancient:
            positive.append("visible neckline is a simple conservative historical period-compatible upper garment collar")
        negative.extend([
            "story prop", "handheld prop", "waist-hanging prop", "weapon", "full costume showcase",
            "full body", "action pose", "cinematic scene background", "modern fashion portrait",
        ])
        if ancient:
            negative.extend([
                "modern T-shirt", "white T-shirt", "crew-neck T-shirt", "hoodie", "sweatshirt",
                "modern casual shirt", "western suit", "contemporary sportswear", "modern fashion collar",
                "spaghetti straps", "sleeveless modern dress", "exposed-shoulder modern fashion",
                "contemporary evening dress", "modern studio fashion styling",
            ])
        negative.extend(_story_prop_negatives(source))
    elif phase == "costume":
        positive.extend([
            "COSTUME REFERENCE CONTRACT: preserve the adopted face identity and confirmed hairstyle exactly",
            "single character, neutral full-body front view, clothing structure, material, color, footwear and wearable accessories are the only design priorities",
        ])
        negative.extend([
            "face redesign", "identity drift", "different hairstyle", "action pose", "battle pose",
            "cinematic scene background", "extra character", "handheld story prop", "weapon", "story artifact",
        ])
        negative.extend(_story_prop_negatives(source))
    elif phase == "turnaround":
        positive.extend([
            "TURNAROUND CONTRACT: preserve the same adopted face identity, hairstyle, body proportions and adopted costume in every panel",
            "one character model sheet with front view, exact 90-degree side view and back view; same person and same outfit in every view",
        ])
        negative.extend([
            "identity drift", "gender drift", "mixed outfits", "different face between panels",
            "different hairstyle between panels", "action pose", "scene background", "story prop",
        ])
        negative.extend(_story_prop_negatives(source))

    return CharacterPromptConstraints(tuple(dict.fromkeys(positive)), tuple(dict.fromkeys(negative)))


__all__ = [
    "CharacterPromptConstraints",
    "build_costume_reference_prompt",
    "build_face_anchor_prompt",
    "compile_character_constraints",
    "extract_age_label",
    "infer_character_gender",
    "project_character_anchor",
    "strict_costume_facts",
    "strict_face_facts",
]
