from __future__ import annotations

from dataclasses import dataclass
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
    # Colloquial Chinese forms: 十六七岁 / 二十七八岁.
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
    # Character visual age outside this range is almost certainly another numeric field.
    if low < 1 or high > 120:
        return None
    return low, high


def _extract_visual_age(text: str) -> tuple[int, int] | None:
    source = str(text or "")
    # Prefer explicitly labelled age fields so asset versions, years and IDs are never mistaken for age.
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
        # A profile may use a life-stage word without a numeric age. Keep this weaker than an explicit age.
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


class PromptCompiler:
    """Unified media prompt compilation entry.

    All media providers should receive compiled prompts instead of raw asset
    descriptions. Project visual direction and asset identity constraints are
    merged before provider execution.
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
        template = get_reference_template(asset_kind) if reference else None
        if contract is not None:
            contract.validate()
            contract_context = "\n".join(filter(None, [
                contract_context, f"Asset {contract.asset_id} version {contract.asset_version}",
                f"身份引用: {contract.entity_ids}; 形象版本: {contract.character_appearances}",
                "固定视觉锚点: " + (contract.identity_anchors or "遵循已确认设定，不添加未确认外观"),
            ]))

        compiled = self.visual_compiler.compile(
            asset_description=asset_description,
            direction=visual_direction,
            contract_context=contract_context,
        )

        age_positive = ""
        age_negative = ""
        if str(asset_kind or "").strip().lower() == "character":
            age_positive, age_negative = _character_age_constraints(
                "\n".join(part for part in (asset_description, contract_context) if part)
            )

        positive = ", ".join(
            part
            for part in (
                template.positive if template else "shot production, preserve established identities and visual anchors",
                age_positive,
                compiled["positive_prompt"],
            )
            if part
        )

        negative = ", ".join(
            part
            for part in (
                template.negative if template else "identity change, inconsistent visual anchors",
                age_negative,
                compiled["negative_prompt"],
                provider_negative,
            )
            if part
        )

        return CompiledPrompt(
            positive_prompt=positive,
            negative_prompt=negative,
        )
