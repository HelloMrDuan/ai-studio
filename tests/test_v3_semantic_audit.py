from __future__ import annotations

import unittest

from app.v3.audit import AuditProtocolError, parse_semantic_audit, semantic_audit_passed


class XiaoduanV3SemanticAuditTests(unittest.TestCase):
    def _pass_payload(self) -> dict:
        return {
            "evidence_entailment_ok": True,
            "beat_coverage_ok": True,
            "temporal_monotonic": True,
            "no_future_event_preconsumption": True,
            "no_result_duplication": True,
            "state_order_valid": True,
            "entity_visibility_valid": True,
            "visual_realization_valid": True,
            "violations": [],
        }

    def test_complete_semantic_audit_can_pass(self) -> None:
        result = parse_semantic_audit(self._pass_payload())
        self.assertTrue(semantic_audit_passed(result))

    def test_valid_schema_false_is_semantic_failure_not_protocol_failure(self) -> None:
        payload = self._pass_payload()
        payload["entity_visibility_valid"] = False
        payload["violations"] = ["required protagonist is not visible"]
        result = parse_semantic_audit(payload)
        self.assertFalse(semantic_audit_passed(result))

    def test_error_shaped_payload_is_protocol_failure(self) -> None:
        with self.assertRaises(AuditProtocolError) as caught:
            parse_semantic_audit(
                {"code": "MODEL_ERROR", "message": "bad output", "source": "qwen"}
            )
        self.assertIn("schema incomplete", str(caught.exception))

    def test_missing_boolean_is_never_defaulted(self) -> None:
        payload = self._pass_payload()
        payload.pop("temporal_monotonic")
        with self.assertRaises(AuditProtocolError):
            parse_semantic_audit(payload)

    def test_extra_unknown_fields_fail_closed(self) -> None:
        payload = self._pass_payload()
        payload["invented_ok"] = True
        with self.assertRaises(AuditProtocolError):
            parse_semantic_audit(payload)

    def test_invalid_json_is_protocol_failure(self) -> None:
        with self.assertRaises(AuditProtocolError):
            parse_semantic_audit("not-json")


if __name__ == "__main__":
    unittest.main()
