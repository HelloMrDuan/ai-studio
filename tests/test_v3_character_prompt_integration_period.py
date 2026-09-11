from __future__ import annotations

import unittest

from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.visual_direction import VisualDirection
from app.v3.character_identity_contract import build_face_anchor_prompt
from app.v3.character_prompt_integration import install_character_prompt_integration
from app.v3.character_reference_package import CharacterReferencePackageBootstrap
from app.v3.typed_reference_profile_authority import (
    enrich_reference_entity,
    resolve_reference_contract,
)


install_character_prompt_integration()


def _entity() -> dict:
    return {
        "name": "沈川",
        "metadata": {
            "authoring": {
                "stable_design": "17岁少年，古风武侠世界中的初遇角色。",
                "typed_character_contract": {
                    "性别呈现": "男性",
                    "年龄": "17岁",
                    "脸部": "东亚少年面孔，轮廓清秀，五官自然",
                    "发型": "黑色长发束起",
                    "发色": "黑色",
                    "服装": "深蓝色古式长袍",
                    "鞋履": "黑色布靴",
                },
            },
            "continuity": {
                "core_profile": {
                    "阶段正式设定": "17岁少年，古风武侠世界中的初遇角色。",
                    "专业角色合同": {
                        "性别呈现": "男性",
                        "年龄": "17岁",
                        "脸部": "东亚少年面孔，轮廓清秀，五官自然",
                        "发型": "黑色长发束起",
                        "发色": "黑色",
                        "服装": "深蓝色古式长袍",
                        "鞋履": "黑色布靴",
                    },
                },
                "default_state": {},
            },
        },
    }


def _production_profile_entity() -> dict:
    return {
        "name": "沈川",
        "metadata": {
            "stable_profile": {
                "阶段正式设定": "17岁少年，古风武侠世界中的初遇角色。",
                "专业角色合同": {
                    "性别呈现": "男",
                    "年龄": "17岁",
                    "脸部": "黑发束起，深蓝色古式长袍。",
                    "发型": "束发",
                    "发色": "黑色",
                    "肤色": "未指定",
                    "体型": "未指定",
                    "身高感": "未指定",
                    "服装": "深蓝色古式长袍",
                    "鞋履": "未指定",
                    "固定身份锚点": ["古风武侠世界中的少年", "古剑持有者"],
                },
            },
            "default_state": {},
            "stable_design": "17岁少年，古风武侠世界中的初遇角色。",
        },
    }


class _NoLiveStage02Production:
    def list_assets(self, project_id: str, active_only: bool = True):
        return []


class CharacterPromptIntegrationPeriodTests(unittest.TestCase):
    def test_face_anchor_uses_exact_confirmed_garment_only_as_visible_crop_policy(self) -> None:
        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(_entity())
        self.assertIn("17岁", prompt)
        self.assertIn("黑色长发束起", prompt)
        self.assertIn("东亚少年面孔", prompt)
        self.assertIn("人物明确穿着已确认服装", prompt)
        self.assertIn("深蓝色古式长袍", prompt)
        self.assertIn("领口、双肩与上胸", prompt)
        self.assertIn("只展示头肩", prompt)
        self.assertNotIn("黑色布靴", prompt)

    def test_face_anchor_period_clothing_is_affirmative_for_zimage_cfg1(self) -> None:
        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(_entity())
        contract = GenerationContract(
            asset_id="face-anchor",
            asset_version="1",
            prompt=prompt,
            visual_direction={"world_style": "neutral"},
            visual_context={"reference_phase": "face_anchor"},
            identity_anchors="性别：男性; 年龄：17岁; 脸部：东亚少年面孔; 发型：黑色长发束起",
        )
        compiled = PromptCompiler().compile(
            asset_kind="character",
            asset_description=prompt,
            visual_direction=VisualDirection(world_style="neutral"),
            contract=contract,
            reference=False,
        )
        self.assertIn("FACE ANCHOR CLOTHING COVERAGE", compiled.positive_prompt)
        self.assertIn("FACE ANCHOR VISIBLE GARMENT CONTRACT", compiled.positive_prompt)
        self.assertIn("深蓝色古式长袍", compiled.positive_prompt)
        self.assertIn("17岁", compiled.positive_prompt)
        self.assertIn("黑色长发束起", compiled.positive_prompt)
        self.assertIn("white T-shirt", compiled.negative_prompt)
        self.assertIn("crew-neck T-shirt", compiled.negative_prompt)
        self.assertIn("hoodie", compiled.negative_prompt)

    def test_face_anchor_without_period_contract_is_still_explicitly_clothed(self) -> None:
        entity = {
            "name": "测试角色",
            "metadata": {
                "stable_profile": {"年龄": "17岁"},
                "stable_design": "17岁少年",
            },
        }
        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(entity)
        self.assertIn("人物必须穿着完整基础上衣", prompt)
        self.assertIn("领口、双肩与上胸", prompt)
        compiled = PromptCompiler().compile(
            asset_kind="character",
            asset_description=prompt,
            visual_direction=VisualDirection(world_style="neutral"),
            contract=GenerationContract(
                asset_id="face-anchor-generic-clothed",
                asset_version="1",
                prompt=prompt,
                visual_direction={"world_style": "neutral"},
                visual_context={"reference_phase": "face_anchor"},
                identity_anchors="17岁少年",
            ),
            reference=False,
        )
        self.assertIn("FACE ANCHOR CLOTHING COVERAGE", compiled.positive_prompt)

    def test_production_profile_shape_preserves_hair_and_period_boundary(self) -> None:
        entity = _production_profile_entity()
        base_prompt = build_face_anchor_prompt(entity)
        self.assertIn("17岁", base_prompt)
        self.assertIn("黑发束起", base_prompt)
        self.assertIn("束发", base_prompt)
        self.assertIn("黑色", base_prompt)
        self.assertIn("古代东亚传统上衣领口", base_prompt)
        self.assertNotIn("黑色布靴", base_prompt)

        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(entity)
        self.assertIn("深蓝色古式长袍", prompt)
        self.assertIn("领口、双肩与上胸", prompt)

        contract = GenerationContract(
            asset_id="face-anchor-production-shape",
            asset_version="1",
            prompt=prompt,
            visual_direction={"world_style": "neutral"},
            visual_context={"reference_phase": "face_anchor"},
            identity_anchors="17岁少年; 黑发束起; 束发; 黑色",
        )
        compiled = PromptCompiler().compile(
            asset_kind="character",
            asset_description=prompt,
            visual_direction=VisualDirection(world_style="neutral"),
            contract=contract,
            reference=False,
        )
        self.assertIn("FACE ANCHOR CLOTHING COVERAGE", compiled.positive_prompt)
        self.assertIn("FACE ANCHOR VISIBLE GARMENT CONTRACT", compiled.positive_prompt)
        self.assertIn("深蓝色古式长袍", compiled.positive_prompt)
        self.assertIn("modern T-shirt", compiled.negative_prompt)
        self.assertIn("white T-shirt", compiled.negative_prompt)
        self.assertIn("crew-neck T-shirt", compiled.negative_prompt)

    def test_stale_formal_profile_is_enriched_from_typed_stage02_contract(self) -> None:
        stale = {
            "name": "沈川",
            "entity_type": "character",
            "metadata": {
                "stable_profile": {"阶段正式设定": "17岁少年，古风武侠世界中的初遇角色。"},
                "stable_design": "17岁少年，古风武侠世界中的初遇角色。",
            },
        }
        contract = {
            "性别呈现": "男",
            "年龄": "17岁",
            "脸部": "东亚少年面孔",
            "发型": "黑发束起",
            "发色": "黑色",
            "服装": "深蓝色古式长袍",
        }
        enriched = enrich_reference_entity(stale, contract)
        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(enriched)
        self.assertIn("黑发束起", prompt)
        self.assertIn("深蓝色古式长袍", prompt)
        compiled = PromptCompiler().compile(
            asset_kind="character",
            asset_description=prompt,
            visual_direction=VisualDirection(world_style="neutral"),
            contract=GenerationContract(
                asset_id="face-anchor-stale-profile",
                asset_version="1",
                prompt=prompt,
                visual_direction={"world_style": "neutral"},
                visual_context={"reference_phase": "face_anchor"},
                identity_anchors="",
            ),
            reference=False,
        )
        self.assertIn("FACE ANCHOR CLOTHING COVERAGE", compiled.positive_prompt)
        self.assertIn("FACE ANCHOR VISIBLE GARMENT CONTRACT", compiled.positive_prompt)
        self.assertIn("深蓝色古式长袍", compiled.positive_prompt)
        self.assertIn("modern T-shirt", compiled.negative_prompt)

    def test_reference_contract_falls_back_to_persisted_profile_projection(self) -> None:
        entity = _production_profile_entity()
        contract, source = resolve_reference_contract(
            _NoLiveStage02Production(),
            "project-with-stale-stage02",
            entity,
        )
        self.assertEqual(source, "canonical_profile_projection")
        self.assertEqual(contract["服装"], "深蓝色古式长袍")
        self.assertEqual(contract["发型"], "束发")


if __name__ == "__main__":
    unittest.main()
