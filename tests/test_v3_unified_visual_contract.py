from __future__ import annotations

import json
from pathlib import Path

from app.services.comfyui import ZIMAGE_TURBO_CLIP, ZIMAGE_TURBO_UNET, ZIMAGE_TURBO_VAE
from app.v3.character_identity_contract import (
    build_costume_reference_prompt,
    project_character_anchor,
    strict_costume_facts,
)
from app.v3.character_package_integrity import install_character_package_integrity
from app.v3.character_reference_package import CharacterReferencePackageBootstrap
from app.v3.unified_production_bridge import UnifiedProductionBridge
from app.v3.production_legacy_bridge import ProductionReadyLegacyBridge
from app.v3.zimage_temporal_executor import ZImageWorkflowError, compile_zimage_workflow


def _lin_entity() -> dict:
    return {
        "name": "林昭",
        "metadata": {
            "stable_profile": {
                "性别呈现": "女性",
                "年龄感": "16岁少女",
                "脸部结构": "东亚少女面孔",
                "发型": "黑色长发盘成低髻，用白玉簪固定",
                "体型": "纤细少年体型",
                "服装": "暗红色古代交领长裙，米白色短斗篷",
                "鞋履": "黑色布靴",
                "固定配饰": "腰间圆形青铜铃",
                "剧情道具": "手持青铜铃",
            },
            "stable_design": (
                "16岁女性，东亚少女面孔，黑色长发盘成低髻，白玉簪；"
                "暗红色古代交领长裙，米白色短斗篷，黑色布靴；腰间青铜铃"
            ),
        },
    }


def test_costume_contract_keeps_wardrobe_but_excludes_story_props() -> None:
    entity = _lin_entity()
    facts = strict_costume_facts(entity["metadata"])
    joined = "；".join(facts)
    assert "交领长裙" in joined
    assert "短斗篷" in joined
    assert "布靴" in joined
    assert "青铜铃" not in joined
    assert "白玉簪" not in joined

    prompt = build_costume_reference_prompt(entity)
    assert "交领长裙" in prompt
    assert "短斗篷" in prompt
    assert "青铜铃" not in prompt
    assert "手持" not in prompt


def test_costume_anchor_projection_cannot_reimport_weapon_or_bell() -> None:
    raw = (
        "性别呈现：女性; 年龄感：16岁; 服装：暗红色交领长裙; 鞋履：黑色布靴; "
        "固定配饰：腰间青铜铃; 剧情道具：长剑; 场景：雪夜石桥"
    )
    projected = project_character_anchor(raw, "costume")
    assert "交领长裙" in projected
    assert "黑色布靴" in projected
    assert "青铜铃" not in projected
    assert "长剑" not in projected
    assert "雪夜石桥" not in projected


def test_character_package_is_not_published_before_turnaround_adoption() -> None:
    install_character_package_integrity()
    service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
    result = service._reference_package(
        "a" * 24,
        {"entity_id": "character-1", "name": "测试角色"},
        face={"status": "ready", "dependency_state": "fresh"},
        costume={"status": "ready", "dependency_state": "fresh"},
        turnaround={"status": "planned", "dependency_state": "fresh"},
    )
    assert result == {}


def test_zimage_workflow_freezes_real_model_and_provider_prompt() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = json.loads((root / "workflows" / "z_image_turbo_api.json").read_text(encoding="utf-8"))
    compiled = compile_zimage_workflow(
        workflow,
        positive_prompt="female, 16岁, 东亚少女面孔, 黑色长发盘低髻",
        negative_prompt="male face, story prop, modern fashion",
        width=1024,
        height=1024,
        seed=123456,
    )
    assert compiled["16"]["inputs"]["unet_name"] == ZIMAGE_TURBO_UNET
    assert compiled["17"]["inputs"]["vae_name"] == ZIMAGE_TURBO_VAE
    assert compiled["18"]["inputs"]["clip_name"] == ZIMAGE_TURBO_CLIP
    assert compiled["6"]["inputs"]["text"].startswith("female")
    assert "story prop" in compiled["7"]["inputs"]["text"]
    assert compiled["3"]["inputs"]["steps"] == 9
    assert compiled["3"]["inputs"]["cfg"] == 1.0
    assert compiled["3"]["inputs"]["sampler_name"] == "euler"
    assert compiled["3"]["inputs"]["scheduler"] == "simple"


def test_invalid_zimage_profile_is_semantic_not_retryable_runtime_noise() -> None:
    assert issubclass(ZImageWorkflowError, ValueError)
    root = Path(__file__).resolve().parents[1]
    workflow = json.loads((root / "workflows" / "z_image_turbo_api.json").read_text(encoding="utf-8"))
    try:
        compile_zimage_workflow(
            workflow,
            positive_prompt="portrait",
            negative_prompt="",
            width=1024,
            height=1024,
            seed=1,
            steps=36,
            cfg=6.0,
        )
    except ZImageWorkflowError:
        pass
    else:
        raise AssertionError("invalid SDXL-like params must be rejected for Z-Image")


def test_workbench_bridge_has_one_temporal_image_entry_for_no_ref_and_ref_modes() -> None:
    assert issubclass(UnifiedProductionBridge, ProductionReadyLegacyBridge)
    assert hasattr(UnifiedProductionBridge, "_execute_zimage_temporal_target")
    assert hasattr(ProductionReadyLegacyBridge, "_execute_reference_first_target")
