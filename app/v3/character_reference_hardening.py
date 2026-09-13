from __future__ import annotations

import json
import re
from typing import Any

import app.services.media_generation_pipeline as media_pipeline_module
from app.services.prompt_compiler import CompiledPrompt, PromptCompiler, naturalize_visual_anchor
from app.services.visual_direction import VisualDirection
from app.v3.character_generation_policy import infer_character_gender
from app.v3.character_reference_package import CharacterReferencePackageBootstrap, _atomic_facts


_PLACEHOLDER = re.compile(r"未明确|未知|待角色设计|待设计|待补充|未描述|not specified|unknown", re.IGNORECASE)
_FACE_SIGNAL = re.compile(
    r"性别|gender|年龄|岁|少年|少女|teen|adolescent|"
    r"脸|面部|脸型|五官|眉|眼|鼻|嘴|唇|下颌|轮廓|肤色|皮肤|skin|face|facial|eye|brow|nose|mouth|jaw|"
    r"发型|发色|头发|黑发|白发|银发|棕发|金发|长发|短发|束发|发髻|低髻|高髻|辫|盘发|披发|"
    r"簪|发钗|发冠|发带|hair|ponytail|bun|braid|hairpin|hair ornament|headband",
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
_HAIR_ORNAMENT = re.compile(r"簪|发钗|发冠|发带|hairpin|hair ornament|headband|hair ribbon", re.IGNORECASE)
_PROP_TERMS = (
    ("剑鞘", "scabbard"),
    ("长剑", "sword"),
    ("古剑", "sword"),
    ("剑", "sword"),
    ("刀", "blade"),
    ("枪", "weapon"),
    ("青铜铃", "bronze bell"),
    ("铃铛", "bell"),
    ("铃", "bell"),
    ("玉佩", "jade pendant"),
    ("玉坠", "jade pendant"),
    ("项链", "necklace"),
    ("sword", "sword"),
    ("scabbard", "scabbard"),
    ("bell", "bell"),
    ("pendant", "pendant"),
    ("weapon", "weapon"),
)
_MODERN_COSTUME_NEGATIVES = (
    "modern clothing",
    "contemporary clothing",
    "modern fashion",
    "streetwear",
    "sportswear",
    "tank top",
    "sleeveless tank top",
    "t-shirt",
    "modern button-down shirt",
    "dress shirt",
    "business shirt",
    "hoodie",
    "jeans",
    "modern sneakers",
    "graphic shirt",
    "logo print",
    "graphic print",
    "anchor logo",
    "fashion photoshoot",
    "clothing rack",
    "wardrobe rack",
    "camera equipment",
    "photography studio set",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _split_row(row: str) -> tuple[str, str]:
    text = _text(row)
    if "：" in text:
        return tuple(part.strip() for part in text.split("：", 1))  # type: ignore[return-value]
    if ":" in text:
        return tuple(part.strip() for part in text.split(":", 1))  # type: ignore[return-value]
    return "", text


def _atomic_segments(value: str) -> list[str]:
    return [
        part.strip(" -\t")
        for part in re.split(r"[\r\n,，;；。/／|、]+", _text(value))
        if part.strip(" -\t")
    ]


def _strict_face_facts(entity: dict[str, Any], limit: int = 14) -> list[str]:
    """Project only face/age/hair facts; never pass costume or story props.

    This mirrors waoowaoo's stableDescription boundary: a character identity
    reference contains reusable visible identity, while layout, scene state and
    independent props stay outside the character face asset.
    """
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    selected: list[str] = []
    for row in _atomic_facts(metadata):
        key, value = _split_row(row)
        if not value or _PLACEHOLDER.search(value):
            continue
        for segment in _atomic_segments(value):
            if not segment or _PLACEHOLDER.search(segment):
                continue
            noisy = bool(_FACE_NOISE.search(segment))
            hair_ornament = bool(_HAIR_ORNAMENT.search(segment))
            if noisy and not hair_ornament:
                continue
            if not (_FACE_SIGNAL.search(segment) or _FACE_SIGNAL.search(key) or hair_ornament):
                continue
            clean = segment.strip()
            if clean and clean not in selected:
                selected.append(clean)
    return selected[:limit]


def _identity_source(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    return "\n".join(_atomic_facts(metadata))


def _age_fact(source: str) -> str:
    match = re.search(r"(?<!\d)(\d{1,3})(?:\s*[-~—–至到]\s*(\d{1,3}))?\s*岁", source)
    if match:
        return match.group(1) + (f"-{match.group(2)}" if match.group(2) else "") + "岁"
    if re.search(r"少女|teenage girl|adolescent girl", source, re.IGNORECASE):
        return "少女/青少年年龄感"
    if re.search(r"少年|teenage boy|adolescent boy", source, re.IGNORECASE):
        return "少年/青少年年龄感"
    return ""


def _strict_face_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
    name = _text(entity.get("name")) or "角色"
    source = _identity_source(entity)
    gender = infer_character_gender(source)
    age = _age_fact(source)
    facts = _strict_face_facts(entity)

    identity: list[str] = []
    if gender == "female":
        identity.append("- 性别：女性")
    elif gender == "male":
        identity.append("- 性别：男性")
    if age:
        identity.append(f"- 视觉年龄：{age}")
    identity.extend(f"- {fact}" for fact in facts if fact not in {"女性", "男性"})
    if not identity:
        identity.append("- 仅使用上游已经确认的脸部、年龄与发型事实，不依据姓名补造身份。")

    gender_line = (
        "角色身份为女性，画面必须呈现与已确认年龄一致的女性面部身份。"
        if gender == "female"
        else "角色身份为男性，画面必须呈现与已确认年龄一致的男性面部身份。"
        if gender == "male"
        else "性别呈现只服从上游明确事实。"
    )

    return (
        f"角色「{name}」身份锁脸锚点。\n"
        f"{gender_line}\n"
        "本阶段只冻结角色本人稳定可见的脸部身份：年龄感、脸型、五官、肤色、发型、发色以及明确的发饰。\n"
        "已确认身份事实：\n" + "\n".join(identity) + "\n\n"
        "生成单人正面头肩肖像，视线自然朝向镜头，中性自然表情，脸部占画面主要区域，双眼、鼻、口、下颌比例自然，皮肤纹理清晰自然。"
        "只保留简洁、低存在感、符合项目时代的上身基础衣领；服装不承担本阶段设计职责。"
        "背景为干净中性浅灰或米白，光线均匀柔和，画面重点只有脸部身份与已确认发型。"
    )


def _strict_face_anchor_text(raw: str) -> str:
    selected: list[str] = []
    for row in re.split(r"[;；\n]+", _text(raw)):
        key, value = _split_row(row)
        for segment in _atomic_segments(value):
            if not segment or _PLACEHOLDER.search(segment):
                continue
            noisy = bool(_FACE_NOISE.search(segment))
            hair_ornament = bool(_HAIR_ORNAMENT.search(segment))
            if noisy and not hair_ornament:
                continue
            if not (_FACE_SIGNAL.search(segment) or _FACE_SIGNAL.search(key) or hair_ornament):
                continue
            if segment not in selected:
                selected.append(segment)
    return "; ".join(selected)


def _period_profile(direction: VisualDirection) -> tuple[bool, bool]:
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
    ancient = any(token in payload for token in ("古代", "古风", "ancient", "historical", "period"))
    chinese = any(token in payload for token in ("中国", "中华", "中式", "东亚", "chinese", "east asian", "xianxia", "仙侠"))
    return ancient, chinese


def _source_prop_negatives(source: str) -> list[str]:
    lowered = source.lower()
    result: list[str] = []
    for token, normalized in _PROP_TERMS:
        if token.lower() in lowered and normalized not in result:
            result.append(normalized)
    return result


def _confirmed_age_negative(source: str) -> list[str]:
    """Prevent both age-up and age-down drift around the confirmed age.

    The base compiler already guarded young characters against looking older but
    did not reject a 16/17-year-old collapsing into a child. Costume generation
    is especially vulnerable because the requested full-body framing reduces
    face pixels, so explicitly reject child/preteen drift here.
    """
    match = re.search(r"(?<!\d)(\d{1,3})(?:\s*[-~—–至到]\s*(\d{1,3}))?\s*岁", source)
    if match:
        low = int(match.group(1))
        high = int(match.group(2) or match.group(1))
        if high <= 17 and low >= 13:
            return [
                "visibly younger than confirmed age",
                "child",
                "preteen",
                "elementary-school-age appearance",
                "childlike face",
                "child body proportions",
            ]
    if re.search(r"少年|少女|teenager|teenage|adolescent", source, re.IGNORECASE):
        return [
            "visibly younger than confirmed teenage age",
            "child",
            "preteen",
            "elementary-school-age appearance",
            "childlike face",
            "child body proportions",
        ]
    return []


def _prepend(value: str, addition: str) -> str:
    value = _text(value).strip(",")
    addition = _text(addition).strip(",")
    if not addition:
        return value
    if addition in value:
        return value
    return f"{addition}, {value}" if value else addition


def install_character_reference_hardening() -> dict[str, Any]:
    """Harden face, costume and turnaround references before provider dispatch.

    ProductionAssetService, GenerationContract and the existing provider path
    remain authoritative. This policy only strengthens the phase contract that
    reaches the image model so an adopted face anchor cannot silently become a
    different age/person and an ancient wardrobe cannot drift into modern wear.
    """
    current_face = CharacterReferencePackageBootstrap._face_prompt
    if not getattr(current_face, "_xiaoduan_face_hardening_v3", False):
        setattr(_strict_face_prompt, "_xiaoduan_face_hardening_v3", True)
        CharacterReferencePackageBootstrap._face_prompt = _strict_face_prompt

    current_phase = media_pipeline_module._phase_anchor_text
    if not getattr(current_phase, "_xiaoduan_face_hardening_v3", False):
        original_phase = current_phase

        def guarded_phase(text: str, phase: str) -> str:
            if _text(phase).lower() == "face_anchor":
                return _strict_face_anchor_text(text)
            return original_phase(text, phase)

        setattr(guarded_phase, "_xiaoduan_face_hardening_v3", True)
        media_pipeline_module._phase_anchor_text = guarded_phase

    current_compile = PromptCompiler.compile
    if not getattr(current_compile, "_xiaoduan_reference_hardening_v3", False):
        original_compile = current_compile

        def guarded_compile(self: PromptCompiler, *args: Any, **kwargs: Any) -> CompiledPrompt:
            result = original_compile(self, *args, **kwargs)
            if _text(kwargs.get("asset_kind")).lower() != "character":
                return result
            contract = kwargs.get("contract")
            visual_context = getattr(contract, "visual_context", {}) if contract is not None else {}
            phase = _text(visual_context.get("reference_phase") if isinstance(visual_context, dict) else "").lower()
            if phase not in {"face_anchor", "costume", "turnaround"}:
                return result

            anchor = naturalize_visual_anchor(getattr(contract, "identity_anchors", "")) if contract is not None else ""
            source = "\n".join(
                part for part in (_text(kwargs.get("asset_description")), anchor) if part
            )
            direction = kwargs.get("visual_direction")
            if not isinstance(direction, VisualDirection):
                direction = VisualDirection()
            ancient, chinese = _period_profile(direction)

            if phase == "face_anchor":
                positive = (
                    "FACE ANCHOR ISOLATION: one head-and-shoulders identity portrait; face, confirmed age, confirmed gender and confirmed hairstyle are the only identity priorities; "
                    "visible clothing is minimal, visually subordinate and period-compatible"
                )
                if ancient and chinese:
                    positive += "; use only a simple conservative ancient Chinese inner upper garment/cross-collar as unobtrusive portrait clothing"
                elif ancient:
                    positive += "; use only a simple conservative period-compatible upper garment as unobtrusive portrait clothing"

                negative_parts = [
                    "story prop",
                    "handheld prop",
                    "waist-hanging prop",
                    "weapon",
                    "full costume showcase",
                    "full body",
                    "action pose",
                    "cinematic scene background",
                    "modern fashion portrait",
                ]
                if ancient:
                    negative_parts.extend([
                        "spaghetti straps",
                        "sleeveless modern dress",
                        "exposed-shoulder modern fashion",
                        "contemporary evening dress",
                        "modern studio fashion styling",
                    ])
                negative_parts.extend(_source_prop_negatives(source))
            elif phase == "costume":
                positive = (
                    "COSTUME FITTING IDENTITY LOCK: use the bound FaceID image as the authoritative person; exact same face, same confirmed age, same gender, same hairstyle and same facial proportions; "
                    "single person, full-body front view, neutral standing pose, head-to-feet visible; wardrobe text contract is authoritative for garment type, layers, color palette, footwear and fixed accessories"
                )
                if ancient and chinese:
                    positive += (
                        "; ANCIENT CHINESE WARDROBE ONLY: historically/period-compatible Chinese robe, hanfu or cross-collar silhouette exactly as confirmed by the character contract; traditional fabric layering and period footwear"
                    )
                elif ancient:
                    positive += "; PERIOD WARDROBE ONLY: use only the confirmed historical clothing silhouette and period-compatible footwear"

                negative_parts = [
                    "different person",
                    "identity drift",
                    "face redesign",
                    "hairstyle change",
                    "age drift",
                    "fashion photoshoot",
                    "cinematic scene background",
                    "other characters",
                ]
                negative_parts.extend(_confirmed_age_negative(source))
                if ancient:
                    negative_parts.extend(_MODERN_COSTUME_NEGATIVES)
            else:
                positive = (
                    "TURNAROUND PACKAGE LOCK: preserve the exact FaceID identity and the exact adopted costume reference; same confirmed age, same gender, same face, same hairstyle, same clothing layers, same color palette, same footwear and fixed accessories in every view"
                )
                if ancient and chinese:
                    positive += "; preserve the confirmed ancient Chinese wardrobe with zero modern-fashion substitution"
                elif ancient:
                    positive += "; preserve the confirmed period wardrobe with zero modern-fashion substitution"
                negative_parts = [
                    "different person between views",
                    "identity drift",
                    "face redesign",
                    "hairstyle change",
                    "age drift",
                    "costume redesign",
                    "clothing color drift",
                ]
                negative_parts.extend(_confirmed_age_negative(source))
                if ancient:
                    negative_parts.extend(_MODERN_COSTUME_NEGATIVES)

            return CompiledPrompt(
                positive_prompt=_prepend(result.positive_prompt, positive),
                negative_prompt=_prepend(result.negative_prompt, ", ".join(dict.fromkeys(negative_parts))),
            )

        setattr(guarded_compile, "_xiaoduan_reference_hardening_v3", True)
        PromptCompiler.compile = guarded_compile

    return {
        "installed": True,
        "face_positive_policy": "affirmative_identity_only",
        "face_anchor_projection": "face_age_hair_only",
        "story_prop_policy": "negative_only_excluded_from_positive",
        "period_clothing_policy": "strict_period_only_for_costume_and_turnaround",
        "costume_identity_policy": "bound_faceid_is_authoritative",
        "teen_age_policy": "reject_both_age_up_and_age_down_drift",
    }


__all__ = [
    "install_character_reference_hardening",
    "_strict_face_anchor_text",
    "_strict_face_facts",
]
