from __future__ import annotations

import unittest
from unittest.mock import patch

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
        with patch("app.v3.production_legacy_bridge.secrets.randbelow", return_value=122):
            result = bridge._smart_payload(
                payload,
                formal={"quality_tier": "A", "title": "主角关键特写"},
                capability="image",
            )
        self.assertEqual(result["params"]["steps"], 24)
        self.assertEqual(result["params"]["final_refine_steps"], 34)
        self.assertEqual(result["params"]["quality_tier"], "A")
        self.assertEqual(result["params"]["seed"], 123)
        self.assertNotIn("model_key", result["params"])

    def test_each_preview_freezes_a_new_seed_when_caller_did_not_supply_one(self) -> None:
        bridge = object.__new__(ProductionReadyLegacyBridge)
        with patch(
            "app.v3.production_legacy_bridge.secrets.randbelow",
            side_effect=[10, 20],
        ):
            first = bridge._smart_payload(
                {"params": {"aspect_ratio": "16:9", "seed": -1}},
                formal={"quality_tier": "B"},
                capability="image",
            )
            second = bridge._smart_payload(
                {"params": {"aspect_ratio": "16:9", "seed": -1}},
                formal={"quality_tier": "B"},
                capability="image",
            )
        self.assertEqual(first["params"]["seed"], 11)
        self.assertEqual(second["params"]["seed"], 21)
        self.assertNotEqual(first["params"]["seed"], second["params"]["seed"])

    def test_final_refine_keeps_explicit_higher_budget_and_preview_seed(self) -> None:
        bridge = object.__new__(ProductionReadyLegacyBridge)
        with patch("app.v3.production_legacy_bridge.secrets.randbelow") as random_seed:
            result = bridge._smart_payload(
                {"params": {"quality_stage": "final", "steps": 34, "seed": 77}},
                formal={"quality_tier": "A"},
                capability="image",
            )
        random_seed.assert_not_called()
        self.assertEqual(result["params"]["steps"], 34)
        self.assertEqual(result["params"]["seed"], 77)
        self.assertEqual(result["params"]["quality_stage"], "final")

    def test_aspect_ratio_maps_to_real_comfy_dimensions(self) -> None:
        self.assertEqual(ProductionReadyLegacyBridge._image_dimensions({"aspect_ratio": "16:9"}), (1024, 576))
        self.assertEqual(ProductionReadyLegacyBridge._image_dimensions({"aspect_ratio": "9:16"}), (576, 1024))
        self.assertEqual(ProductionReadyLegacyBridge._image_dimensions({"width": 832, "height": 1216}), (832, 1216))


if __name__ == "__main__":
    unittest.main()
