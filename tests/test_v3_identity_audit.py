from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.v3.audit.identity import (
    IdentityAuditDecision,
    IdentityAuditPolicy,
    evaluate_identity_similarity,
)


class IdentityAuditTests(unittest.TestCase):
    def setUp(self):
        self.policy = IdentityAuditPolicy(
            policy_id="test-calibration-v1",
            recognition_model="buffalo_l/w600k_r50",
            pass_threshold=0.75,
            fail_threshold=0.45,
        )

    def test_pass_review_fail_bands_are_deterministic(self):
        self.assertEqual(
            evaluate_identity_similarity(0.80, policy=self.policy).decision,
            IdentityAuditDecision.pass_,
        )
        self.assertEqual(
            evaluate_identity_similarity(0.60, policy=self.policy).decision,
            IdentityAuditDecision.review_required,
        )
        self.assertEqual(
            evaluate_identity_similarity(0.40, policy=self.policy).decision,
            IdentityAuditDecision.fail,
        )

    def test_threshold_boundaries_are_inclusive(self):
        self.assertEqual(
            evaluate_identity_similarity(0.75, policy=self.policy).decision,
            IdentityAuditDecision.pass_,
        )
        self.assertEqual(
            evaluate_identity_similarity(0.45, policy=self.policy).decision,
            IdentityAuditDecision.fail,
        )

    def test_policy_rejects_inverted_thresholds(self):
        with self.assertRaises(ValidationError):
            IdentityAuditPolicy(
                policy_id="bad",
                recognition_model="test",
                pass_threshold=0.5,
                fail_threshold=0.5,
            )

    def test_score_outside_cosine_range_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_identity_similarity(1.01, policy=self.policy)


if __name__ == "__main__":
    unittest.main()
