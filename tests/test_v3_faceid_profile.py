from __future__ import annotations

import json
import unittest
from pathlib import Path


class FaceIDProfileTests(unittest.TestCase):
    def test_faceid_profile_and_workflow_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        profile = json.loads((root / "config/comfyui_reference_profile.v3.sdxl-faceid.json").read_text(encoding="utf-8"))
        workflow = json.loads((root / "workflows/xiaoduan_faceid_reference_api.json").read_text(encoding="utf-8"))

        self.assertTrue(profile["identity_reference"])
        self.assertTrue(profile["ip_adapter"])
        self.assertEqual(profile["max_references"], 1)
        self.assertEqual(profile["reference_bindings"][0]["node_id"], "10")
        self.assertEqual(workflow["10"]["class_type"], "LoadImage")
        self.assertEqual(workflow["12"]["class_type"], "IPAdapterUnifiedLoaderFaceID")
        self.assertEqual(workflow["12"]["inputs"]["preset"], "FACEID PLUS V2")
        self.assertEqual(workflow["13"]["class_type"], "IPAdapterFaceID")
        self.assertEqual(workflow["3"]["inputs"]["model"], ["13", 0])

        binding = profile["contract_bindings"][0]
        self.assertEqual(binding["contract_field"], "source_text")
        self.assertEqual(binding["node_id"], "6")
        self.assertEqual(binding["input_name"], "text")


if __name__ == "__main__":
    unittest.main()
