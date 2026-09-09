from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.visual_direction import VisualDirection


def _direction() -> VisualDirection:
    return VisualDirection(
        world_style="xianxia",
        culture="chinese",
        era="ancient",
        art_style="cinematic realistic",
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
        entity_ids=("character-1",),
        character_appearances=({"character_id": "character-1", "appearance_version": "v1"},),
        identity_anchors=anchor,
    )


def test_explicit_seventeen_year_old_is_a_strong_visual_anchor():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="十七岁的少年，深蓝色古式长袍，黑色长发束起",
        visual_direction=_direction(),
        contract=_contract('{"年龄":"17岁","stable_design":"清秀少年，深蓝长袍"}'),
        reference=True,
    )

    assert "17-year-old" in result.positive_prompt
    assert "teenage adolescent" in result.positive_prompt
    assert "youthful facial proportions" in result.positive_prompt
    assert "middle-aged person" in result.negative_prompt
    assert "mature adult face" in result.negative_prompt
    assert "heavy mature jawline" in result.negative_prompt
    assert "full adult beard" in result.negative_prompt


def test_colloquial_chinese_age_range_is_preserved():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="十六七岁的少女，长发以玉簪挽起",
        visual_direction=_direction(),
        contract=_contract("少女，十六七岁，浅青色交领长裙"),
        reference=True,
    )

    assert "16-17-year-old" in result.positive_prompt
    assert "middle-aged person" in result.negative_prompt


def test_confirmed_facial_hair_is_not_negated_for_a_young_character():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="17岁少年，设定明确有淡胡须",
        visual_direction=_direction(),
        contract=_contract("年龄: 17；固定特征：淡胡须"),
        reference=True,
    )

    assert "17-year-old" in result.positive_prompt
    assert "full adult beard" not in result.negative_prompt


def test_middle_aged_character_is_not_forced_to_look_young():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="45岁男性角色",
        visual_direction=_direction(),
        contract=_contract("age: 45, weathered scholar"),
        reference=True,
    )

    assert "45-year-old" in result.positive_prompt
    assert "middle-aged adult" in result.positive_prompt
    assert "teenage appearance" in result.negative_prompt
    assert "middle-aged person" not in result.negative_prompt


def test_location_numbers_do_not_trigger_character_age_control():
    result = PromptCompiler().compile(
        asset_kind="location",
        asset_description="17级石阶通向古老道观",
        visual_direction=_direction(),
        contract=None,
        reference=True,
    )

    assert "17-year-old" not in result.positive_prompt
    assert "middle-aged person" not in result.negative_prompt
