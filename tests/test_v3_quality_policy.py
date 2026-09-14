from __future__ import annotations

import unittest

from app.v3.quality_policy import apply_smart_candidate_params, infer_quality_tier, profile_for_shot


class QualityPolicyTests(unittest.TestCase):
    def test_explicit_tier_wins(self):
        self.assertEqual(infer_quality_tier({"quality_tier": "A", "title": "过渡"}), "A")

    def test_key_closeup_is_high_quality(self):
        self.assertEqual(infer_quality_tier({"shot_size": "特写", "summary": "主角情绪揭示"}), "A")
        profile = profile_for_shot({"shot_size": "特写"})
        self.assertTrue(profile.semantic_audit)
        self.assertGreater(profile.image_final_steps, profile.image_candidate_steps)

    def test_transition_shot_is_fast(self):
        self.assertEqual(infer_quality_tier({"summary": "雪山环境建立空镜"}), "C")
        params = apply_smart_candidate_params({}, shot={"summary": "过渡空镜"}, capability="video")
        self.assertEqual(params["quality_tier"], "C")
        self.assertEqual(params["candidate_count"], 1)
        self.assertLess(params["steps"], 20)

    def test_user_explicit_steps_are_not_overwritten(self):
        params = apply_smart_candidate_params(
            {"steps": 28},
            shot={"quality_tier": "C"},
            capability="video",
        )
        self.assertEqual(params["steps"], 28)
        self.assertEqual(params["quality_tier"], "C")


if __name__ == "__main__":
    unittest.main()
