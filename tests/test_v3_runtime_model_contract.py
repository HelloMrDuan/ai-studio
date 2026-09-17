from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from app.config import Settings
from app.services.comfyui import (
    ZIMAGE_TURBO_CLIP,
    ZIMAGE_TURBO_KEY,
    ZIMAGE_TURBO_UNET,
    ZIMAGE_TURBO_VAE,
)
from app.v3.generation_executor import ReferenceAsset
from app.v3.runtime_model_contract import V3RuntimeModelContract
from app.v3.workflow.production_worker_executor import ProductionWorkerExecutor
from app.v3.workflow.unified_image_executor import UnifiedImageDomainExecutor
from app.v3.zimage_temporal_executor import (
    compile_zimage_controlled_master_workflow,
    compile_zimage_controlled_layout_workflow,
    compile_character_front_prompt,
    compile_zimage_turnaround_workflow,
)


ROOT = Path(__file__).resolve().parents[1]


class V3RuntimeModelContractTests(unittest.TestCase):
    def test_character_front_prompt_cannot_inherit_model_sheet_layout(self) -> None:
        prompt = compile_character_front_prompt(
            "专业角色身份母版；18岁少女；黑色高发髻；浅青色古式长裙；4-panel character turnaround sheet"
        )
        self.assertIn("exactly one character", prompt)
        self.assertIn("18岁少女", prompt)
        self.assertIn("黑色高发髻", prompt)
        self.assertIn("浅青色古式长裙", prompt)
        self.assertNotIn("专业角色身份母版", prompt)
        self.assertNotIn("4-panel character turnaround sheet", prompt)
        self.assertIn("inset portrait", prompt)

    def test_character_front_prompt_removes_reusable_held_props(self) -> None:
        prompt = compile_character_front_prompt(
            "18岁少女，手持一枚玉佩，身穿浅青色古式长裙；黑色高发髻"
        )
        self.assertNotIn("玉佩", prompt)
        self.assertIn("浅青色古式长裙", prompt)
        self.assertIn("both hands visibly empty", prompt)

    def test_default_text_runtime_is_qwen(self) -> None:
        registry = json.loads((ROOT / "config" / "llm_models.json").read_text(encoding="utf-8"))
        self.assertEqual(registry["default_model"], "qwen3-32b-abliterated")
        settings = Settings(_env_file=None)
        self.assertEqual(settings.gemma_model, "qwen3-32b")
        self.assertTrue(settings.gemma_start_command.endswith("/scripts/start_qwen_v3.sh"))

    def test_face_anchor_txt2img_is_forced_to_real_zimage_turbo_profile(self) -> None:
        target = {
            "asset_role": "character_face_anchor",
            "metadata": {"reference_asset": True, "reference_phase": "face_anchor"},
        }
        original = {
            "capability": "image",
            "mode": "txt2img",
            "target_asset_id": "ast_face",
            "params": {
                "model_key": "smart",
                "steps": 36,
                "cfg": 6.0,
                "sampler": "dpmpp_2m",
                "scheduler": "karras",
                "reference_phase": "face_anchor",
            },
        }
        routed = V3RuntimeModelContract.route_image_payload(target, original)
        self.assertEqual(original["params"]["model_key"], "smart")
        self.assertEqual(routed["params"]["model_key"], ZIMAGE_TURBO_KEY)
        self.assertEqual(routed["params"]["requested_model_key"], ZIMAGE_TURBO_KEY)
        self.assertEqual(routed["params"]["steps"], 9)
        self.assertEqual(routed["params"]["cfg"], 1.0)
        self.assertEqual(routed["params"]["sampler"], "euler")
        self.assertEqual(routed["params"]["scheduler"], "simple")
        self.assertEqual(routed["params"]["runtime_image_backend"], ZIMAGE_TURBO_KEY)

    def test_costume_reference_uses_zimage_primary_plus_facefusion_identity(self) -> None:
        target = {
            "asset_role": "character_costume_reference",
            "metadata": {"reference_asset": True, "reference_phase": "costume"},
        }
        original = {
            "capability": "image",
            "mode": "reference_img2img",
            "target_asset_id": "ast_costume",
            "params": {
                "model_key": "smart",
                "reference_phase": "costume",
                "reference_asset_ids": ["ast_face"],
            },
        }
        routed = V3RuntimeModelContract.route_image_payload(target, original)
        params = routed["params"]
        self.assertEqual(params["model_key"], ZIMAGE_TURBO_KEY)
        self.assertEqual(params["requested_model_key"], ZIMAGE_TURBO_KEY)
        self.assertEqual(params["steps"], 9)
        self.assertEqual(params["cfg"], 1.0)
        self.assertEqual(params["sampler"], "euler")
        self.assertEqual(params["scheduler"], "simple")
        self.assertEqual(params["runtime_image_backend"], "z_image_turbo_facefusion")
        self.assertEqual(params["runtime_identity_postprocess"], "facefusion")
        self.assertTrue(
            UnifiedImageDomainExecutor._uses_zimage_primary(
                {"metadata": {"reference_phase": "costume"}}, ["face-anchor:p:a"]
            )
        )

    def test_turnaround_with_two_lineage_refs_still_uses_zimage_primary(self) -> None:
        target = {
            "asset_role": "character_turnaround",
            "metadata": {"reference_asset": True, "reference_phase": "turnaround"},
        }
        original = {
            "capability": "image",
            "mode": "reference_img2img",
            "target_asset_id": "ast_turnaround",
            "params": {
                "reference_phase": "turnaround",
                "reference_asset_ids": ["ast_face", "ast_costume"],
            },
        }
        routed = V3RuntimeModelContract.route_image_payload(target, original)
        self.assertEqual(routed["params"]["runtime_image_backend"], "z_image_turbo_facefusion")
        self.assertEqual(routed["params"]["runtime_identity_postprocess"], "facefusion")
        self.assertTrue(
            UnifiedImageDomainExecutor._uses_zimage_primary(
                {"metadata": {"reference_phase": "turnaround"}},
                ["face-anchor:p:a", "face-anchor:p:b"],
            )
        )

    def test_turnaround_compiles_adopted_front_plus_generated_side_and_back(self) -> None:
        workflow = json.loads(
            (ROOT / "workflows" / "z_image_turbo_turnaround_api.json").read_text(encoding="utf-8")
        )
        compiled = compile_zimage_turnaround_workflow(
            workflow,
            adopted_costume_name="xiaoduan-v3/adopted-costume.png",
            adopted_face_name="xiaoduan-v3/adopted-face.png",
            side_pose_name="xiaoduan-v3/side-pose.png",
            back_pose_name="xiaoduan-v3/back-pose.png",
            positive_prompt=(
                "STRICT VISUAL AGE: 17岁, stable_profile.阶段正式设定: "
                "17岁的少年，身穿深蓝色古式长袍。; 4-panel character turnaround sheet"
            ),
            negative_prompt="costume drift",
            seed=101,
            filename_prefix="test/turnaround",
        )
        self.assertEqual(compiled["1"]["inputs"]["image"], "xiaoduan-v3/adopted-costume.png")
        self.assertEqual(compiled["2"]["inputs"]["image"], "xiaoduan-v3/adopted-face.png")
        self.assertEqual(compiled["3"]["inputs"]["image"], "xiaoduan-v3/side-pose.png")
        self.assertEqual(compiled["4"]["inputs"]["image"], "xiaoduan-v3/back-pose.png")
        self.assertEqual(compiled["5"]["inputs"]["base_model"], "z-image-turbo")
        self.assertTrue(compiled["5"]["inputs"]["load_controlnet"])
        self.assertEqual(compiled["7"]["inputs"]["image2"], ["2", 0])
        self.assertEqual(compiled["8"]["inputs"]["images"], ["7", 0])
        self.assertEqual(compiled["9"]["inputs"]["control_image"], ["3", 0])
        self.assertEqual(compiled["10"]["inputs"]["control_image"], ["4", 0])
        self.assertEqual(compiled["9"]["inputs"]["cfg_scale"], 1.0)
        self.assertEqual(compiled["9"]["inputs"]["num_inference_steps"], 12)
        self.assertIn("90-degree side profile", compiled["9"]["inputs"]["prompt"])
        self.assertIn("180-degree rear view", compiled["10"]["inputs"]["prompt"])
        self.assertNotIn("4-panel", compiled["9"]["inputs"]["prompt"])
        self.assertNotIn("turnaround sheet", compiled["10"]["inputs"]["prompt"])
        self.assertEqual(compiled["11"]["inputs"]["image1"], ["1", 0])
        self.assertEqual(compiled["13"]["inputs"]["images"], ["12", 0])
        self.assertNotIn("未明确描述", compiled["9"]["inputs"]["prompt"])

    def test_character_master_binds_identity_lora_to_controlled_layout(self) -> None:
        layout_workflow = json.loads(
            (ROOT / "workflows" / "z_image_turbo_controlled_layout_api.json").read_text(encoding="utf-8")
        )
        layout = compile_zimage_controlled_layout_workflow(
            layout_workflow,
            source_sheet_name="master/repeated-front.png",
            pose_sheet_name="master/front-side-back-poses.png",
            positive_prompt="18岁少女; 浅青色交领古式长裙; 黑发高髻; 正面全身",
            seed=88,
            filename_prefix="test/layout",
        )
        self.assertEqual(layout["1"]["inputs"]["image"], "master/front-side-back-poses.png")
        self.assertEqual(layout["2"]["inputs"]["image"], "master/repeated-front.png")
        self.assertIn("exactly three", layout["9"]["inputs"]["text"])
        self.assertEqual(layout["12"]["inputs"]["seed"], 88)

        master_workflow = json.loads(
            (ROOT / "workflows" / "z_image_turbo_controlled_master_api.json").read_text(encoding="utf-8")
        )
        master = compile_zimage_controlled_master_workflow(
            master_workflow,
            front_source_name="master/front.png",
            face_source_name="master/face.png",
            pose_sheet_name="master/layout.png",
            positive_prompt="18岁少女; 浅青色交领古式长裙; 黑发高髻",
            negative_prompt="hairstyle change, costume redesign",
            seed=88,
            filename_prefix="test/master",
        )
        self.assertEqual(master["1"]["inputs"]["image"], "master/front.png")
        self.assertEqual(master["2"]["inputs"]["image"], "master/face.png")
        self.assertEqual(master["7"]["inputs"]["image"], "master/layout.png")
        self.assertEqual(master["8"]["inputs"]["lora"], ["6", 0])
        self.assertEqual(master["8"]["inputs"]["control_scale"], 0.75)
        self.assertIn("same hairline", master["8"]["inputs"]["prompt"])
        self.assertEqual(master["8"]["inputs"]["seed"], 88)
        self.assertEqual(master["9"]["inputs"]["images"], ["8", 0])

    def test_identity_postprocess_selects_face_anchor_not_costume_reference(self) -> None:
        costume = ReferenceAsset(
            "costume:p:a", Path("costume.png"), "costume-sha", "image/png",
            "char-a", "character_costume_reference", "character",
        )
        face = ReferenceAsset(
            "face-anchor:p:a", Path("face.png"), "face-sha", "image/png",
            "char-a", "character_face_anchor", "character",
        )
        records = {costume.reference_id: costume, face.reference_id: face}
        worker = ProductionWorkerExecutor.__new__(ProductionWorkerExecutor)
        worker.visual = SimpleNamespace(
            base=SimpleNamespace(
                references=SimpleNamespace(resolve=lambda ref: records[ref])
            )
        )
        selected = worker._face_identity_reference(
            {"reference_ids": [costume.reference_id, face.reference_id]}
        )
        self.assertEqual(selected.reference_id, face.reference_id)
        self.assertEqual(selected.role, "character_face_anchor")

    def test_non_character_reference_domain_does_not_claim_zimage_reference_support(self) -> None:
        target = {
            "asset_role": "location_reference",
            "metadata": {"reference_asset": True, "reference_phase": "location"},
        }
        original = {
            "capability": "image",
            "mode": "reference_img2img",
            "target_asset_id": "ast_location",
            "params": {
                "reference_phase": "location",
                "reference_asset_ids": ["ast_location_ref"],
            },
        }
        routed = V3RuntimeModelContract.route_image_payload(target, original)
        self.assertEqual(routed["params"]["runtime_image_backend"], "sdxl_reference_ipadapter")
        self.assertEqual(
            routed["params"]["runtime_image_backend_reason"],
            "non_character_reference_required",
        )
        self.assertFalse(
            UnifiedImageDomainExecutor._uses_zimage_primary(
                {"metadata": {"reference_phase": "location"}}, ["location:p:a"]
            )
        )

    def test_zimage_workflow_uses_expected_real_model_components(self) -> None:
        workflow = json.loads((ROOT / "workflows" / "z_image_turbo_api.json").read_text(encoding="utf-8"))
        encoded = json.dumps(workflow, ensure_ascii=False)
        self.assertIn(ZIMAGE_TURBO_UNET, encoded)
        self.assertIn(ZIMAGE_TURBO_CLIP, encoded)
        self.assertIn(ZIMAGE_TURBO_VAE, encoded)
        sampler = workflow["3"]["inputs"]
        self.assertEqual(sampler["steps"], 9)
        self.assertEqual(sampler["cfg"], 1)
        self.assertEqual(sampler["sampler_name"], "euler")
        self.assertEqual(sampler["scheduler"], "simple")


if __name__ == "__main__":
    unittest.main()
