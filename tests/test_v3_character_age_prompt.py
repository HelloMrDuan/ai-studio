from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler, naturalize_visual_anchor
from app.services.visual_direction import VisualDirection


def _direction() -> VisualDirection:
    return VisualDirection(
        world_style="xianxia",
        culture="chinese",
        era="ancient",
        art_style="cinematic realistic",
        character_rules={
            "face": "East Asian facial features",
            "clothing": "ancient Chinese hanfu silhouette",
            "hair": "traditional ancient Chinese hairstyle",
        },
        negative_constraints=["western fantasy", "knight armor", "modern clothing"],
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
    assert "East Asian facial features" in result.positive_prompt
    assert "ancient Chinese hanfu silhouette" in result.positive_prompt
    assert "natural human face" in result.positive_prompt
    assert "anatomically coherent facial structure" in result.positive_prompt
    assert "realistic human facial detail" in result.positive_prompt
    assert "middle-aged person" in result.negative_prompt
    assert "mature adult face" in result.negative_prompt
    assert "heavy mature jawline" in result.negative_prompt
    assert "full adult beard" in result.negative_prompt
    assert "western face" in result.negative_prompt
    assert "knight armor" in result.negative_prompt
    assert "malformed face" in result.negative_prompt
    assert "cgi doll" in result.negative_prompt
    assert "cartoon face" in result.negative_prompt
    assert "modern high heels" in result.negative_prompt


def test_character_identity_precedes_turnaround_layout_in_provider_prompt():
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="17岁少年，深蓝长袍，黑色古风束发",
        visual_direction=_direction(),
        contract=_contract('{"stable_profile":{"年龄":"17岁","脸型":"清秀少年脸"},"stable_design":"深蓝长袍"}'),
        reference=True,
    )

    prompt = result.positive_prompt
    assert prompt.index("东方仙侠") < prompt.index("4-panel character turnaround sheet")
    assert prompt.index("East Asian facial features") < prompt.index("4-panel character turnaround sheet")
    assert prompt.index("17-year-old") < prompt.index("4-panel character turnaround sheet")
    assert prompt.index("natural human face") < prompt.index("4-panel character turnaround sheet")
    assert prompt.index("深蓝长袍") < prompt.index("4-panel character turnaround sheet")


def test_raw_identity_json_is_naturalized_before_provider_prompt():
    raw = '{"entity_id":"ent_123","stable_profile":{"年龄":"17岁","脸型":"清秀少年脸"},"stable_design":"深蓝长袍，黑色束发"}'
    natural = naturalize_visual_anchor(raw)
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="角色参考图",
        visual_direction=_direction(),
        contract=_contract(raw),
        reference=True,
    )

    assert "年龄: 17岁" in natural
    assert "清秀少年脸" in result.positive_prompt
    assert "深蓝长袍" in result.positive_prompt
    assert "entity_id" not in result.positive_prompt
    assert "ent_123" not in result.positive_prompt
    assert '{"' not in result.positive_prompt
    assert "{'" not in result.positive_prompt


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


def test_western_stylized_project_is_not_contaminated_by_chinese_or_realistic_style_rules():
    direction = VisualDirection(
        world_style="western fantasy",
        culture="european",
        era="medieval",
        art_style="stylized illustration",
    )
    result = PromptCompiler().compile(
        asset_kind="character",
        asset_description="young knight apprentice",
        visual_direction=direction,
        contract=None,
        reference=True,
    )

    lowered = result.positive_prompt.lower()
    assert "xianxia" not in lowered
    assert "chinese" not in lowered
    assert "east asian" not in lowered
    assert "cgi doll" not in result.negative_prompt
    assert "cartoon face" not in result.negative_prompt
