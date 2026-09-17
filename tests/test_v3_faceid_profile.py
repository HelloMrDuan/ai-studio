from __future__ import annotations

import json
import unittest
from pathlib import Path


class FaceIDProfileTests(unittest.TestCase):
    def test_faceid_profile_and_workflow_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        profile = json.loads((root / "config/comfyui_reference_profile.v3.sdxl-faceid.json").read_text(encoding="utf-8"))
        workflow = json.loads((root / "workflows/xiaoduan_multi_reference_faceid_api.json").read_text(encoding="utf-8"))

        self.assertTrue(profile["identity_reference"])
        self.assertTrue(profile["ip_adapter"])
        self.assertEqual(profile["multi_reference_mode"], "role_aware_chain")
        self.assertEqual(profile["max_references"], 4)
        self.assertEqual(profile["reference_bindings"], [])
        self.assertEqual(workflow["12"]["class_type"], "IPAdapterUnifiedLoaderFaceID")
        self.assertEqual(workflow["12"]["inputs"]["preset"], "FACEID PLUS V2")
        self.assertEqual(workflow["12"]["inputs"]["lora_strength"], 0.8)
        self.assertEqual(workflow["11"]["class_type"], "CLIPVisionLoader")
        self.assertEqual(workflow["16"]["class_type"], "IPAdapterModelLoader")
        self.assertEqual(workflow["3"]["inputs"]["model"], ["4", 0])

        binding = profile["contract_bindings"][0]
        self.assertEqual(binding["contract_field"], "source_text")
        self.assertEqual(binding["node_id"], "6")
        self.assertEqual(binding["input_name"], "text")


if __name__ == "__main__":
    unittest.main()
