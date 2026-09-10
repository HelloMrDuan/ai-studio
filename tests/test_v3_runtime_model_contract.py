from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.config import Settings
from app.services.comfyui import (
    ZIMAGE_TURBO_CLIP,
    ZIMAGE_TURBO_KEY,
    ZIMAGE_TURBO_UNET,
    ZIMAGE_TURBO_VAE,
)
from app.v3.runtime_model_contract import V3RuntimeModelContract


ROOT = Path(__file__).resolve().parents[1]


class V3RuntimeModelContractTests(unittest.TestCase):
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

    def test_reference_conditioned_generation_is_not_falsely_labelled_zimage(self) -> None:
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
        self.assertEqual(routed["params"]["model_key"], "smart")
        self.assertEqual(routed["params"]["runtime_image_backend"], "sdxl_reference_faceid")
        self.assertEqual(routed["params"]["runtime_image_backend_reason"], "identity_reference_required")

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
