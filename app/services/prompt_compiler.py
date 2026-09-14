from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection, VisualDirectionCompiler


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_ANCHOR_TECHNICAL_KEYS = {
    "schema_version", "asset_id", "entity_id", "character_id", "character_entity_id",
    "appearance_id", "appearance_version", "logical_key", "source_asset_id",
    "parent_asset_ids", "evidence", "source",
}
_ANCHOR_TRANSIENT_KEYS = {
    "action", "pose", "expression", "camera", "camera_direction", "shot", "shot_id",
    "scene_state", "momentary_state",
}


@dataclass(frozen=True)
class CompiledPrompt:
    positive_prompt: str
    negative_prompt: str

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


def _cn_number(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        if right == "":
            return tens * 10
        if len(right) == 1 and right in _CN_DIGITS:
            return tens * 10 + _CN_DIGITS[right]
        return None
    if all(char in _CN_DIGITS for char in text):
        number = 0
        for char in text:
            number = number * 10 + _CN_DIGITS[char]
        return number
    return None


def _cn_age_range(value: str) -> tuple[int, int] | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"([二三四五六七八九])?十([一二三四五六七八九])([一二三四五六七八九])", text)
    if match:
        tens = _CN_DIGITS.get(match.group(1), 1) if match.group(1) else 1
        first = tens * 10 + _CN_DIGITS[match.group(2)]
        second = tens * 10 + _CN_DIGITS[match.group(3)]
        if second >= first:
            return first, second
    number = _cn_number(text)
    if number is None:
        return None
    return number, number


def _valid_age_range(first: int, second: int | None = None) -> tuple[int, int] | None:
    low = int(first)
    high = int(second if second is not None else first)
    if low > high:
        low, high = high, low
    if low < 1 or high > 120:
        return None
    return low, high


def _extract_visual_age(text: str) -> tuple[int, int] | None:
    source = str(text or "")
    labelled = re.search(
        r"(?:visual_age(?:_range)?|age|年龄)\s*[\"']?\s*[:：=]\s*[\"']?\s*(\d{1,3})"
        r"(?:\s*[-~—–至到]\s*(\d{1,3}))?",
        source,
        flags=re.IGNORECASE,
    )
    if labelled:
        result = _valid_age_range(int(labelled.group(1)), int(labelled.group(2)) if labelled.group(2) else None)
        if result:
            return result

    english = re.search(
        r"(?<!\d)(\d{1,3})(?:\s*(?:-|–|—|~|to)\s*(\d{1,3}))?\s*[- ]?years?[- ]?old",
        source,
        flags=re.IGNORECASE,
    )
    if english:
        result = _valid_age_range(int(english.group(1)), int(english.group(2)) if english.group(2) else None)
        if result:
            return result

    arabic = re.search(r"(?<!\d)(\d{1,3})(?:\s*[-~—–至到]\s*(\d{1,3}))?\s*(?:周?岁)", source)
    if arabic:
        result = _valid_age_range(int(arabic.group(1)), int(arabic.group(2)) if arabic.group(2) else None)
        if result:
            return result

    chinese = re.search(r"([零〇一二两三四五六七八九十]{1,4})\s*岁", source)
    if chinese:
        result = _cn_age_range(chinese.group(1))
        if result:
            return _valid_age_range(*result)
    return None


def _has_explicit_facial_hair(text: str) -> bool:
    return bool(re.search(r"胡须|胡子|络腮胡|八字胡|beard|moustache|mustache|stubble", str(text or ""), re.IGNORECASE))


def _character_age_constraints(text: str) -> tuple[str, str]:
    age = _extract_visual_age(text)
    source = str(text or "")
    if age is None:
        if re.search(r"少年|少女|青少年|teenager|adolescent|teenage", source, re.IGNORECASE):
            positive = (
                "strict youthful age fidelity, teenage adolescent appearance, youthful facial proportions, "
                "smooth age-appropriate skin, age-appropriate youthful body proportions, clearly not middle-aged"
            )
            negative = (
                "age drift, aged-up appearance, middle-aged person, mature adult face, older-looking face, "
                "deep wrinkles, pronounced nasolabial folds, heavy mature jawline"
            )
            if not _has_explicit_facial_hair(source):
                negative += ", full adult beard, heavy moustache, heavy stubble"
            return positive, negative
        return "", ""

    low, high = age
    label = f"{low}-year-old" if low == high else f"{low}-{high}-year-old"
    strict = f"strict visual age anchor: {label}, visible age must remain within the confirmed character age"

    if high <= 17:
        positive = (
            f"{strict}, teenage adolescent, youthful facial proportions, smooth age-appropriate skin, "
            "age-appropriate youthful body proportions, clearly not an older adult"
        )
        negative = (
            "age drift, aged-up appearance, middle-aged person, mature adult face, older-looking face, "
            "deep wrinkles, pronounced nasolabial folds, heavy mature jawline"
        )
        if not _has_explicit_facial_hair(source):
            negative += ", full adult beard, heavy moustache, heavy stubble"
        return positive, negative
    if low >= 18 and high <= 24:
        return (
            f"{strict}, young adult, youthful adult facial proportions, smooth age-appropriate skin",
            "age drift, middle-aged appearance, elderly appearance, deep wrinkles, heavily aged facial features",
        )
    if low >= 25 and high <= 39:
        return (
            f"{strict}, adult with age-appropriate facial maturity and body proportions",
            "age drift, childlike face, teenage appearance, elderly appearance",
        )
    if low >= 40 and high <= 59:
        return (
            f"{strict}, middle-aged adult with age-appropriate facial maturity",
            "age drift, childlike face, teenage appearance, elderly appearance",
        )
    if low >= 60:
        return (
            f"{strict}, older adult with age-appropriate facial maturity and natural age detail",
            "age drift, childlike face, teenage appearance, unrealistically youthful face",
        )
    return strict, "age drift, visibly younger or older than the confirmed character age"


def _character_face_quality_constraints(direction: VisualDirection) -> tuple[str, str]:
    """Stable face anatomy rules independent of character identity.

    Anti-CGI/style negatives are enabled only for projects explicitly requesting
    a realistic/cinematic/photo visual language so stylized projects remain valid.
    """
    positive = (
        "natural human face, anatomically coherent facial structure, normal eye size and alignment, "
        "balanced facial proportions, anatomically plausible nose and mouth, natural jaw and cheeks, "
        "consistent facial identity, clean detailed eyes, natural skin texture"
    )
    negative = (
        "malformed face, distorted face, warped facial anatomy, asymmetrical eyes, cross-eyed, "
        "misaligned eyes, duplicated facial features, deformed mouth, broken jawline, melted face, "
        "uncanny face, oversized doll eyes"
    )
    style = str(direction.art_style or "").lower()
    if any(token in style for token in ("realistic", "photoreal", "cinematic", "photo", "写实", "电影")):
        positive += ", realistic human facial detail, subtle skin texture, natural non-plastic skin"
        negative += (
            ", cartoon face, chibi, disney-like face, pixar-like face, 3d toy, cgi doll, "
            "game-avatar face, plastic doll skin, wax figure"
        )
    return positive, negative


def _anchor_rows(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    rows: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or "").strip()
            lowered = key_text.lower()
            if lowered in _ANCHOR_TECHNICAL_KEYS or lowered in _ANCHOR_TRANSIENT_KEYS:
                continue
            next_prefix = key_text if not prefix else f"{prefix}.{key_text}"
            rows.extend(_anchor_rows(item, next_prefix, depth + 1))
        return rows
    if isinstance(value, list):
        scalar = [str(item).strip() for item in value if not isinstance(item, (dict, list)) and str(item).strip()]
        if scalar and prefix:
            rows.append(f"{prefix}: {' / '.join(scalar[:12])}")
        else:
            for item in value[:12]:
                rows.extend(_anchor_rows(item, prefix, depth + 1))
        return rows
    text = str(value or "").strip()
    if text:
        rows.append(f"{prefix}: {text}" if prefix else text)
    return rows


def naturalize_visual_anchor(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        rows = _anchor_rows(raw)
        return "; ".join(dict.fromkeys(rows))

    text = str(raw or "").strip()
    if not text:
        return ""

    parsed_rows: list[str] = []
    chunks = [line.strip() for line in text.splitlines() if line.strip()]
    all_json = bool(chunks)
    for chunk in chunks:
        try:
            parsed = json.loads(chunk)
        except (TypeError, ValueError, json.JSONDecodeError):
            all_json = False
            break
        parsed_rows.extend(_anchor_rows(parsed))
    if all_json and parsed_rows:
        return "; ".join(dict.fromkeys(parsed_rows))

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    rows = _anchor_rows(parsed)
    return "; ".join(dict.fromkeys(rows))


def _join_unique(parts: tuple[str, ...] | list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in parts:
        text = str(value or "").strip().strip(",")
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return ", ".join(result)


class PromptCompiler:
    """Unified media prompt compilation entry.

    Character identity, project direction, age and face anatomy always precede
    reference layout. This prevents a large turnaround layout prompt from
    crowding the actual person out of the provider's text context.
    """

    def __init__(self) -> None:
        self.visual_compiler = VisualDirectionCompiler()

    def compile(
        self,
        *,
        asset_kind: str,
        asset_description: str,
        visual_direction: VisualDirection,
        contract_context: str = "",
        provider_negative: str = "",
        contract: Any = None,
        reference: bool = True,
    ) -> CompiledPrompt:
        kind = str(asset_kind or "").strip().lower()
        template = get_reference_template(asset_kind) if reference else None

        anchor_text = ""
        if contract is not None:
            contract.validate()
            anchor_text = naturalize_visual_anchor(contract.identity_anchors)

        visual_context = _join_unique([
            visual_direction.compile_context(),
            anchor_text,
            contract_context,
        ])

        age_positive = ""
        age_negative = ""
        face_positive = ""
        face_negative = ""
        if kind == "character":
            age_positive, age_negative = _character_age_constraints(
                "\n".join(part for part in (asset_description, anchor_text, contract_context) if part)
            )
            face_positive, face_negative = _character_face_quality_constraints(visual_direction)

        if reference:
            positive = _join_unique([
                visual_context,
                age_positive,
                face_positive,
                asset_description,
                template.positive if template else "",
            ])
            negative = _join_unique([
                visual_direction.compile_negative_prompt(),
                age_negative,
                face_negative,
                provider_negative,
                template.negative if template else "",
            ])
        else:
            compiled = self.visual_compiler.compile(
                asset_description=asset_description,
                direction=visual_direction,
                contract_context=_join_unique([anchor_text, contract_context]),
            )
            positive = _join_unique([
                age_positive,
                face_positive,
                compiled["positive_prompt"],
                "shot production, preserve established identities and visual anchors",
            ])
            negative = _join_unique([
                age_negative,
                face_negative,
                compiled["negative_prompt"],
                provider_negative,
                "identity change, inconsistent visual anchors",
            ])

        return CompiledPrompt(positive_prompt=positive, negative_prompt=negative)
