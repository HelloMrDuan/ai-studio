from __future__ import annotations

import unittest

from app.v3.production_legacy_bridge import ProductionReadyLegacyBridge


class ProductionBridgeQualityTests(unittest.TestCase):
    def test_legacy_image_defaults_do_not_override_smart_quality(self) -> None:
        bridge = object.__new__(ProductionReadyLegacyBridge)
        payload = {
            "params": {
                "aspect_ratio": "16:9",
                "model_key": "z_image_turbo",
                "steps": 9,
                "cfg": 1.0,
                "sampler": "euler",
                "scheduler": "simple",
            }
        }
        result = bridge._smart_payload(
            payload,
            formal={"quality_tier": "A", "title": "主角关键特写"},
            capability="image",
        )
        self.assertEqual(result["params"]["steps"], 24)
        self.assertEqual(result["params"]["final_refine_steps"], 34)
        self.assertEqual(result["params"]["quality_tier"], "A")
        self.assertNotIn("model_key", result["params"])

    def test_final_refine_keeps_explicit_higher_budget(self) -> None:
        bridge = object.__new__(ProductionReadyLegacyBridge)
        result = bridge._smart_payload(
            {"params": {"quality_stage": "final", "steps": 34, "seed": 77}},
            formal={"quality_tier": "A"},
            capability="image",
        )
        self.assertEqual(result["params"]["steps"], 34)
        self.assertEqual(result["params"]["seed"], 77)
        self.assertEqual(result["params"]["quality_stage"], "final")

    def test_aspect_ratio_maps_to_real_comfy_dimensions(self) -> None:
        self.assertEqual(ProductionReadyLegacyBridge._image_dimensions({"aspect_ratio": "16:9"}), (1024, 576))
        self.assertEqual(ProductionReadyLegacyBridge._image_dimensions({"aspect_ratio": "9:16"}), (576, 1024))
        self.assertEqual(ProductionReadyLegacyBridge._image_dimensions({"width": 832, "height": 1216}), (832, 1216))


if __name__ == "__main__":
    unittest.main()
