from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection


def test_xianxia_visual_direction_keeps_eastern_constraints():
    direction = VisualDirection(
        world_style="xianxia",
        culture="chinese",
        era="ancient",
        art_style="cinematic realistic",
        character_rules={"face": "east asian", "clothing": "hanfu"},
        negative_constraints=["western face", "european features"],
    )
    contract = GenerationContract(
        asset_id="shen-chuan",
        asset_version="v1",
        prompt="young immortal swordsman",
        visual_direction={
            "world_style": direction.world_style,
            "culture": direction.culture,
            "era": direction.era,
            "art_style": direction.art_style,
            "negative_constraints": direction.negative_constraints,
        },
    )

    assert "chinese" in contract.context()
    assert "ancient" in contract.context()
    assert "xianxia" in contract.context()
    assert "western face" in contract.negative_prompt()


def test_reference_templates_are_asset_specific():
    assert get_reference_template("character").role == "character_identity_reference"
    assert get_reference_template("location").role == "location_identity_reference"
    assert get_reference_template("prop").role == "prop_identity_reference"


def test_prompt_compiler_keeps_negative_constraints():
    compiler = PromptCompiler()
    result = compiler.compile(
        asset_kind="character",
        asset_description="ancient swordsman",
        visual_direction=VisualDirection(
            world_style="xianxia",
            culture="chinese",
            era="ancient",
            negative_constraints=["modern hairstyle"],
        ),
        contract_context="identity version v1",
    )

    assert "xianxia" in result["positive_prompt"]
    assert "modern hairstyle" in result["negative_prompt"]
