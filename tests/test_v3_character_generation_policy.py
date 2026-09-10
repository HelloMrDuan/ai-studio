from __future__ import annotations

import pytest

from app.services.comfyui import STYLE_PRESETS
from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.reference_templates import REFERENCE_TEMPLATES
from app.services.visual_direction import VisualDirection
from app.v3.character_generation_policy import (
    infer_character_gender,
    install_character_generation_policy,
)
from app.v3.character_reference_package import CharacterReferencePackageBootstrap


install_character_generation_policy()


def _direction() -> VisualDirection:
    return VisualDirection(
        world_style="xianxia",
        culture="chinese",
        era="ancient",
        art_style="cinematic realistic",
        character_rules={
            "face": "East Asian facial features",
            "clothing": "ancient Chinese clothing",
        },
        negative_constraints=["western face", "modern clothing"],
    )


def _contract(anchor: str) -> GenerationContract:
    return GenerationContract(
        asset_id="character-reference",
        asset_version="1",
        prompt="character identity reference",
        visual_direction={
            "world_style": "xianxia",
            "culture": "chinese",
            "era": "ancient",
            "art_style": "cinematic realistic",
        },
        identity_anchors=anchor,
    )


def test_explicit_female_identity_is_hard_constrained_before_provider():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="苏瑶，女性角色，少女，清秀面容",
        visual_direction=_direction(),
        contract=_contract("性别：女性；年龄感：少女；肤色偏白"),
        reference=False,
    )

    assert result.positive_prompt.startswith("STRICT CHARACTER IDENTITY: confirmed gender is female")
    assert "gender must remain female" in result.positive_prompt
    assert "male character" in result.negative_prompt
    assert "male face" in result.negative_prompt
    assert "man" in result.negative_prompt
    assert "boy" in result.negative_prompt


def test_explicit_male_identity_is_hard_constrained_before_provider():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="沈川，男性角色，17岁少年，黑发束起",
        visual_direction=_direction(),
        contract=_contract("性别：男性；年龄：17岁；发型：黑发束起"),
        reference=False,
    )

    assert result.positive_prompt.startswith("STRICT CHARACTER IDENTITY: confirmed gender is male")
    assert "17-year-old" in result.positive_prompt
    assert "STRICT HAIRSTYLE ANCHOR" in result.positive_prompt
    assert "黑发" in result.positive_prompt
    assert "female character" in result.negative_prompt
    assert "woman" in result.negative_prompt
    assert "girl" in result.negative_prompt


def test_gender_is_never_inferred_from_character_name():
    assert infer_character_gender("角色名称：苏瑶；脸型：清秀") == ""
    assert infer_character_gender("角色名称：沈川；脸型：清秀") == ""


def test_conflicting_labelled_gender_fails_closed():
    with pytest.raises(ValueError, match="角色性别设定冲突"):
        infer_character_gender("性别：女性\n性别：男性")


def test_face_prompt_uses_gender_but_rejects_misfiled_costume_and_prop_as_hair():
    entity = {
        "name": "苏瑶",
        "metadata": {
            "stable_profile": {
                "角色名称/稳定身份/年龄感": "苏瑶，女性角色，年龄未知",
                "脸部结构与辨识特征": "未明确描述，待角色设计",
                "发型、发色、肤色、体型、身高感": "浅青色长裙，手持玉佩",
            },
            "stable_design": "少女，浅青色长裙，手持玉佩",
        },
    }
    service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
    prompt = service._face_prompt(entity)

    assert "性别：女性" in prompt
    assert "绝不能生成男性" in prompt
    assert "浅青色长裙" not in prompt
    assert "玉佩" not in prompt
    assert "不根据姓名猜测" not in prompt


def test_unspecified_hair_does_not_become_a_gender_stereotype_anchor():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="苏瑶，女性角色，少女，清秀面容",
        visual_direction=_direction(),
        contract=_contract("性别：女性；年龄感：少女；发型未明确"),
        reference=False,
    )

    assert "HAIRSTYLE POLICY" in result.positive_prompt
    assert "STRICT HAIRSTYLE ANCHOR" not in result.positive_prompt


def test_confirmed_hair_is_preserved_without_gender_based_replacement():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="女性角色，短发，古代架空",
        visual_direction=_direction(),
        contract=_contract("性别：女性；发型：短发"),
        reference=False,
    )

    assert "STRICT HAIRSTYLE ANCHOR" in result.positive_prompt
    assert "短发" in result.positive_prompt
    assert "do not replace it with a gender stereotype" in result.positive_prompt


def test_portrait_style_no_longer_forces_every_person_to_be_adult():
    assert "adult subject" not in STYLE_PRESETS["portrait_photo"]["positive"]
    assert "single clearly visible person" in STYLE_PRESETS["portrait_photo"]["positive"]


def test_turnaround_template_rejects_gender_drift():
    template = REFERENCE_TEMPLATES["character"]
    assert "same confirmed gender identity" in template.positive
    assert "gender drift" in template.negative
