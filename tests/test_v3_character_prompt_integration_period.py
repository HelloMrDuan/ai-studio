from __future__ import annotations

import unittest

from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.visual_direction import VisualDirection
from app.v3.character_prompt_integration import install_character_prompt_integration
from app.v3.character_reference_package import CharacterReferencePackageBootstrap


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


class CharacterPromptIntegrationPeriodTests(unittest.TestCase):
    def test_face_anchor_keeps_period_boundary_without_full_costume(self) -> None:
        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(_entity())
        self.assertIn("17岁", prompt)
        self.assertIn("黑色长发束起", prompt)
        self.assertIn("东亚少年面孔", prompt)
        self.assertIn("古代东亚传统上衣领口", prompt)
        self.assertNotIn("深蓝色古式长袍", prompt)
        self.assertNotIn("黑色布靴", prompt)

    def test_face_anchor_blocks_modern_tshirt_without_relying_on_visual_direction(self) -> None:
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
        self.assertIn("white T-shirt", compiled.negative_prompt)
        self.assertIn("crew-neck T-shirt", compiled.negative_prompt)
        self.assertIn("hoodie", compiled.negative_prompt)
        self.assertIn("17岁", compiled.positive_prompt)
        self.assertIn("黑色长发束起", compiled.positive_prompt)


if __name__ == "__main__":
    unittest.main()
