from __future__ import annotations

import json
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
