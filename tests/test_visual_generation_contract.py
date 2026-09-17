from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection
from app.services.production_assets import ProductionAssetService
from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.generation_contract import GenerationContract as ProviderContract, visual_contract_fields
from app.v3.generation_executor import bind_comfy_contract, ComfyContractBinding
from app.v3.contracts import Capability
import json


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
        asset_id="asset-identity-1",
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


def create_reference(production, project_id, kind, name, key):
    entity = production.create_entity(project_id, entity_type=kind, name=name, logical_key=key)
    profile = production.create_text_asset(
        project_id, stage="02", skill="test", logical_key=f"{key}:profile", asset_role=f"{kind}_profile",
        name=name, content=json.dumps({"entity_id": entity["entity_id"], "name": name,
                                     "stable_design": "银色纹样，深蓝材质，固定轮廓"}, ensure_ascii=False),
        entity_ids=[entity["entity_id"]],
    )
    target = production.declare_asset(
        project_id, stage="03", skill="test", logical_key=f"{key}:reference", asset_type="IMAGE",
        asset_role=f"{kind}_reference", name=name, parent_asset_ids=[profile["asset_id"]], entity_ids=[entity["entity_id"]],
    )
    prompt = production.create_text_asset(
        project_id, stage="03", skill="test", logical_key=f"{key}:prompt", asset_role="reference_prompt",
        name=name, content="保持已确认身份与外观", parent_asset_ids=[profile["asset_id"]],
    )
    return entity, target, {"target_asset_id": target["asset_id"], "prompt_asset_id": prompt["asset_id"]}


def test_real_asset_to_compiled_contract_to_comfy_nodes(tmp_path):
    production = ProductionAssetService(tmp_path)
    project = "a" * 24
    direction = production.set_visual_direction(project, {"world_style": "xianxia", "culture": "chinese", "era": "ancient"})
    for index, name in enumerate(["沈川", "苏瑶"]):
        entity, target, payload = create_reference(production, project, "character", name, f"char-{index}")
        prepared = MediaGenerationPipeline().prepare_candidate(production, project, payload)
        asset_count = len(production.list_assets(project))
        assert MediaGenerationPipeline().prepare_candidate(production, project, prepared) == prepared
        assert len(production.list_assets(project)) == asset_count
        positive, negative = prepared["params"]["positive_prompt"], prepared["params"]["negative_prompt"]
        assert all(word in positive for word in ["东方", "古代", "仙侠", "银色纹样", "fixed face shape"])
        assert all(word in negative for word in ["western face", "european features", "modern hairstyle"])
        assert prepared["visual_context"] == {"visual_direction_id": direction["asset_id"],
                                              "asset_identity_type": "character", "appearance_version": "v1"}
        assert prepared["character_appearances"] == [{"character_id": entity["entity_id"], "appearance_version": "v1"}]
        stored = production.get_asset(project, prepared["generation_contract_id"])
        assert direction["asset_id"] in stored["parent_asset_ids"]
        assert production.get_asset(project, target["asset_id"])["contract_artifact_id"] == stored["asset_id"]
        assert json.loads(production.read_text_asset(project, stored["asset_id"]))["negative_prompt"] == negative
        contract = ProviderContract(
            shot_id="shot", source_text="raw must not reach render", entity_ids=(entity["entity_id"],),
            reference_ids=(), provider_reference_ids=(), provider_id="comfy", model_id="image",
            required_capabilities=frozenset({Capability.image_generation}),
            **visual_contract_fields({**prepared, **prepared["params"]}),
        )
        workflow = {"pos": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                    "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality"}},
                    "sample": {"class_type": "KSampler", "inputs": {"positive": ["pos", 0], "negative": ["neg", 0]}}}
        result = bind_comfy_contract(workflow, contract, [ComfyContractBinding("source_text", "pos", "text")])
        assert result["pos"]["inputs"]["text"] == positive
        assert negative in result["neg"]["inputs"]["text"]
        assert "low quality" in result["neg"]["inputs"]["text"]


def test_project_isolation_and_all_three_real_templates(tmp_path):
    production = ProductionAssetService(tmp_path)
    production.set_visual_direction("a" * 24, {"world_style": "xianxia", "culture": "chinese", "era": "ancient"})
    project = "b" * 24
    production.set_visual_direction(project, {"world_style": "western fantasy", "culture": "european", "era": "medieval"})
    for kind, phrase in [("character", "fixed face shape"), ("location", "spatial layout"), ("prop", "craftsmanship")]:
        _, _, payload = create_reference(production, project, kind, "任意名称", kind)
        result = MediaGenerationPipeline().prepare_candidate(production, project, payload)["params"]
        assert phrase in result["positive_prompt"]
        assert f"{kind} identity reference" in result["positive_prompt"]
        assert all(f"{other} identity reference" not in result["positive_prompt"] for other in {"character", "location", "prop"} - {kind})
        assert all(word not in result["positive_prompt"] for word in ["仙侠", "东方", "chinese", "hanfu"])
        assert "western face" not in result["negative_prompt"]


def test_appearance_versions_survive_rename_and_reference_adoption(tmp_path):
    production = ProductionAssetService(tmp_path)
    project = "c" * 24
    entity = production.create_entity(project, entity_type="character", name="初始名称", logical_key="character-1")
    owner = entity["entity_id"]
    appearances = []
    refs = []
    for version in ["v1", "v2"]:
        appearance = production.create_text_asset(
            project, stage="02", skill="test", logical_key=f"{owner}:appearance:{version}",
            asset_role="character_appearance", name=version, content=version,
            entity_ids=[owner], metadata={"appearance_id": version},
        )
        appearances.append(appearance)
        reference = production.declare_asset(
            project, stage="03", skill="test", logical_key=f"{owner}:reference:{version}",
            asset_type="IMAGE", asset_role="character_reference", name=version, entity_ids=[owner],
            parent_asset_ids=[appearance["asset_id"]],
        )
        assert reference["metadata"]["visual_context"]["appearance_version"] == version
        production.bind_task(project, reference["asset_id"], {"task_id": version, "status": "completed"})
        refs.append(reference)
    production.update_entity(project, owner, {"name": "改名后"})
    reloaded = ProductionAssetService(tmp_path).get_graph(project)["entities"][owner]
    assert reloaded["character_id"] == owner
    assert reloaded["appearance_versions"] == [
        {"version": v, "asset_id": a["asset_id"], "reference_assets": [r["asset_id"]]}
        for v, a, r in zip(["v1", "v2"], appearances, refs)
    ]
    production.archive_asset(project, refs[0]["asset_id"])
    assert production.get_graph(project)["entities"][owner]["appearance_versions"][0]["reference_assets"] == []


def test_direction_change_versions_same_text_and_preserves_old_context(tmp_path):
    production = ProductionAssetService(tmp_path)
    project = "d" * 24
    old = production.set_visual_direction(project, {"world_style": "xianxia", "culture": "chinese"})
    arguments = dict(stage="02", skill="test", logical_key="profile", asset_role="character_profile", name="actor", content="same")
    first = production.create_text_asset(project, **arguments)
    current = production.set_visual_direction(project, {"world_style": "western fantasy", "culture": "european"})
    second = production.create_text_asset(project, **arguments)
    assert first["asset_id"] != second["asset_id"]
    assert first["metadata"]["visual_context"]["visual_direction_id"] == old["asset_id"]
    assert second["metadata"]["visual_context"]["visual_direction_id"] == current["asset_id"]
    fork = production.fork_asset_version(project, first["asset_id"])
    assert fork["metadata"]["visual_context"] == first["metadata"]["visual_context"]


def test_selected_appearance_compiles_only_that_outfit(tmp_path):
    production = ProductionAssetService(tmp_path)
    project = "f" * 24
    entity, _, payload = create_reference(production, project, "character", "可改名角色", "actor")
    appearances = []
    for version, outfit in [("v1", "white robe"), ("v2", "red armor")]:
        appearances.append(production.create_text_asset(
            project, stage="02", skill="test", logical_key=f"actor:appearance:{version}",
            asset_role="character_appearance", name=version,
            content=json.dumps({"stable_design": outfit}), entity_ids=[entity["entity_id"]],
            metadata={"appearance_id": version},
        ))
    target = production.declare_asset(
        project, stage="03", skill="test", logical_key="actor:reference:v2", asset_type="IMAGE",
        asset_role="character_reference", name="形象参考", entity_ids=[entity["entity_id"]],
        parent_asset_ids=[appearances[1]["asset_id"]],
    )
    result = MediaGenerationPipeline().prepare_candidate(production, project, {**payload, "target_asset_id": target["asset_id"]})
    assert "red armor" in result["params"]["positive_prompt"]
    assert "white robe" not in result["params"]["positive_prompt"]
    assert result["character_appearances"] == [{"character_id": entity["entity_id"], "appearance_version": "v2"}]
