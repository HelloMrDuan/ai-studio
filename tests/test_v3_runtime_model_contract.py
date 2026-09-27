from __future__ import annotations

import json
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from PIL import Image, ImageDraw

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
    normalize_controlled_layout,
    compile_zimage_turnaround_workflow,
    compile_qwen_edit_turnaround_workflow,
)


ROOT = Path(__file__).resolve().parents[1]


class V3RuntimeModelContractTests(unittest.TestCase):
    def test_four_figure_layout_is_normalized_before_identity_diffusion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = Image.new("RGB", (2304, 1024), (220, 222, 224))
            draw = ImageDraw.Draw(source)
            colors = ((18, 42, 82), (22, 70, 105), (25, 55, 90), (30, 80, 115))
            for center, color in zip((280, 820, 1360, 1940), colors):
                draw.ellipse((center - 55, 60, center + 55, 190), fill=(35, 28, 25))
                draw.rectangle((center - 125, 190, center + 125, 930), fill=color)
            source_path = root / "four.png"
            output_path = root / "three.png"
            source.save(source_path)
            normalize_controlled_layout(source_path, output_path)
            with Image.open(output_path) as normalized:
                self.assertEqual(normalized.size, (2304, 1024))
                pixels = normalized.convert("RGB")
                for index in range(3):
                    panel = pixels.crop((index * 768, 0, (index + 1) * 768, 1024))
                    self.assertLess(min(value[2] for value in panel.getdata()), 150)

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

    def test_turnaround_compiles_reference_bound_qwen_camera_edits(self) -> None:
        workflow = json.loads(
            (ROOT / "workflows" / "qwen_image_edit_turnaround_api.json").read_text(encoding="utf-8")
        )
        compiled = compile_qwen_edit_turnaround_workflow(
            workflow,
            adopted_costume_name="xiaoduan-v3/adopted-costume.png",
            seed=101,
            filename_prefix="test/turnaround",
        )
        self.assertEqual(compiled["1"]["inputs"]["image"], "xiaoduan-v3/adopted-costume.png")
        self.assertEqual(compiled["2"]["inputs"]["unet_name"], "qwen_image_edit_2511_int8_convrot.safetensors")
        self.assertEqual(compiled["3"]["inputs"]["lora_name"], "qwen-image-edit-2511-multiple-angles-lora.safetensors")
        self.assertEqual(compiled["6"]["inputs"]["clip_name"], "qwen_2.5_vl_7b_nvfp4.safetensors")
        self.assertEqual(compiled["10"]["inputs"]["prompt"], "")
        self.assertTrue(compiled["12"]["inputs"]["prompt"].startswith("<sks> right side view"))
        self.assertTrue(compiled["15"]["inputs"]["prompt"].startswith("<sks> back view"))
        self.assertIn("absence of bangs", compiled["12"]["inputs"]["prompt"])
        self.assertIn("Do not add or remove", compiled["15"]["inputs"]["prompt"])
        self.assertEqual(compiled["14"]["inputs"]["seed"], 101)
        self.assertEqual(compiled["17"]["inputs"]["seed"], 101)
        self.assertEqual(compiled["14"]["inputs"]["steps"], 40)
        self.assertEqual(compiled["17"]["inputs"]["cfg"], 4.0)
        self.assertEqual(compiled["20"]["inputs"]["image1"], ["1", 0])
        self.assertEqual(compiled["22"]["inputs"]["images"], ["21", 0])

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
