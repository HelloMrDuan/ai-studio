from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import app.services.media_generation_pipeline as media_pipeline_module
from app.services.comfyui import ZIMAGE_TURBO_KEY
from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.visual_direction import VisualDirection
from app.v3.character_generation_policy import install_character_generation_policy
from app.v3.character_reference_hardening import install_character_reference_hardening
from app.v3.character_reference_package import CharacterReferencePackageBootstrap
from app.v3.runtime_model_contract import V3RuntimeModelContract


install_character_generation_policy()
install_character_reference_hardening()


def _direction() -> VisualDirection:
    return VisualDirection(
        world_style="东方古代奇幻",
        culture="古代中国文化语境",
        era="古代",
        art_style="电影级写实摄影",
        character_rules={"identity": "东亚人物身份稳定"},
        negative_constraints=["modern clothing"],
    )


def _lin_entity() -> dict:
    return {
        "name": "林昭",
        "metadata": {
            "stable_design": (
                "林昭，16岁女性，东亚少女面孔，黑色长发盘成低髻，用一支白玉簪固定，"
                "暗红色古代交领长裙，米白色短斗篷，脚穿黑色布靴，腰间挂着一枚圆形青铜铃"
            ),
            "stable_profile": {
                "性别呈现": "女性",
                "年龄感": "16岁少女",
                "脸部结构": "东亚少女面孔，杏眼，自然少年感面部比例",
                "发型": "黑色长发盘成低髻，用白玉簪固定",
                "服装": "暗红色古代交领长裙，米白色短斗篷",
                "固定配饰": "腰间圆形青铜铃",
            },
        },
    }


class CharacterReferenceHardeningTests(unittest.TestCase):
    def test_lin_face_prompt_keeps_face_hairpin_but_drops_costume_and_bell(self) -> None:
        service = CharacterReferencePackageBootstrap.__new__(CharacterReferencePackageBootstrap)
        prompt = service._face_prompt(_lin_entity())
        self.assertIn("性别：女性", prompt)
        self.assertIn("16岁", prompt)
        self.assertIn("低髻", prompt)
        self.assertIn("白玉簪", prompt)
        self.assertNotIn("暗红色古代交领长裙", prompt)
        self.assertNotIn("米白色短斗篷", prompt)
        self.assertNotIn("青铜铃", prompt)
        self.assertNotIn("绝不能生成男性", prompt)
        self.assertNotIn("持剑", prompt)

    def test_face_phase_anchor_projection_splits_mixed_identity_rows(self) -> None:
        raw = (
            "stable_design: 16岁女性、东亚少女面孔、黑色长发盘成低髻、白玉簪固定、"
            "暗红色古代交领长裙、米白色短斗篷、腰间青铜铃; "
            "固定身份锚点: 少女脸、青铜铃"
        )
        face = media_pipeline_module._phase_anchor_text(raw, "face_anchor")
        self.assertIn("16岁女性", face)
        self.assertIn("东亚少女面孔", face)
        self.assertIn("黑色长发盘成低髻", face)
        self.assertIn("白玉簪固定", face)
        self.assertNotIn("交领长裙", face)
        self.assertNotIn("短斗篷", face)
        self.assertNotIn("青铜铃", face)

    def test_face_provider_prompt_uses_period_clothing_and_negative_story_props(self) -> None:
        contract = GenerationContract(
            asset_id="face-anchor",
            asset_version="1",
            prompt="林昭身份锁脸",
            visual_direction={
                "world_style": "东方古代奇幻",
                "culture": "古代中国文化语境",
                "era": "古代",
                "art_style": "电影级写实摄影",
            },
            visual_context={"reference_phase": "face_anchor"},
            identity_anchors=(
                "性别：女性; 年龄：16岁; 脸部：东亚少女面孔; "
                "发型：黑色长发盘成低髻; 发饰：白玉簪"
            ),
        )
        result = PromptCompiler().compile(
            asset_kind="character",
            asset_description="角色林昭身份锁脸锚点，只生成正面头肩肖像。",
            visual_direction=_direction(),
            contract=contract,
            reference=False,
        )
        self.assertIn("FACE ANCHOR ISOLATION", result.positive_prompt)
        self.assertIn("ancient Chinese", result.positive_prompt)
        self.assertIn("female", result.positive_prompt)
        self.assertIn("modern fashion portrait", result.negative_prompt)
        self.assertIn("spaghetti straps", result.negative_prompt)
        self.assertIn("story prop", result.negative_prompt)
        self.assertNotIn("male character", result.positive_prompt)

    def test_runtime_contract_rebinds_public_dispatch_and_routes_face_to_zimage(self) -> None:
        async def original_llm(*args, **kwargs):
            return {}, "qwen3-32b"

        async def original_execute(project_id, payload):
            return {"project_id": project_id, "payload": payload}

        face_target = {
            "asset_role": "character_face_anchor",
            "metadata": {"reference_asset": True, "reference_phase": "face_anchor"},
        }
        llm = SimpleNamespace(_request_messages=original_llm)
        production = SimpleNamespace(get_asset=lambda project_id, target_id: face_target)
        director = SimpleNamespace(production=production, llm=llm)
        bootstrap = SimpleNamespace(submit_candidate=original_execute)
        bridge = SimpleNamespace(execute_candidate=original_execute, reference_bootstrap=bootstrap)
        legacy = SimpleNamespace(
            director=director,
            director_workbench_execute_candidate=original_execute,
            settings=SimpleNamespace(gemma_model="old", gemma_start_command="old"),
        )
        settings = SimpleNamespace(
            stage04_required_model_alias="qwen3-32b",
            gemma_model="old",
            gemma_start_command="old",
        )
        contract = V3RuntimeModelContract(settings, legacy, bridge)
        contract.install()

        self.assertIs(legacy.director_workbench_execute_candidate.__self__, contract)
        self.assertIs(bridge.execute_candidate.__self__, contract)
        self.assertIs(bootstrap.submit_candidate.__self__, contract)

        request = {
            "capability": "image",
            "mode": "txt2img",
            "target_asset_id": "face-asset",
            "params": {
                "reference_phase": "face_anchor",
                "model_key": "smart",
                "steps": 36,
                "cfg": 6.0,
                "sampler": "dpmpp_2m",
                "scheduler": "karras",
            },
        }
        result = asyncio.run(legacy.director_workbench_execute_candidate("p", request))
        routed = result["payload"]["params"]
        self.assertEqual(routed["model_key"], ZIMAGE_TURBO_KEY)
        self.assertEqual(routed["steps"], 9)
        self.assertEqual(routed["cfg"], 1.0)
        self.assertEqual(routed["sampler"], "euler")
        self.assertEqual(routed["scheduler"], "simple")
        self.assertEqual(routed["runtime_image_backend"], ZIMAGE_TURBO_KEY)


if __name__ == "__main__":
    unittest.main()
