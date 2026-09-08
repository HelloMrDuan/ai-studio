from __future__ import annotations

import unittest

from app.v3.workflow.contracts import (
    ProductionStep,
    ProductionWorkflowInput,
    StepActivityResult,
)


class XiaoduanV3WorkflowContractTests(unittest.TestCase):
    def test_workflow_requires_unique_step_ids_and_idempotency_keys(self) -> None:
        first = ProductionStep(
            step_id="shot-001-image",
            skill_id="image_direction",
            operation="generate_image",
            payload_ref="resource://shot/001/contract",
            idempotency_key="project:shot001:image:v1",
        )
        duplicate = ProductionStep(
            step_id="shot-001-image",
            skill_id="quality_audit",
            operation="audit_image",
            payload_ref="resource://shot/001/image",
            idempotency_key="project:shot001:audit:v1",
        )
        with self.assertRaises(ValueError):
            ProductionWorkflowInput(
                workflow_id="wf_001",
                project_id="project-demo",
                steps=(first, duplicate),
            )

    def test_semantic_failure_is_typed_not_an_exception_retry_hint(self) -> None:
        result = StepActivityResult(
            kind="semantic_failure",
            error_code="CONTINUITY_FAILURE",
            message="guardian creature identity mismatch",
        )
        self.assertEqual(result.kind, "semantic_failure")
        self.assertEqual(result.error_code, "CONTINUITY_FAILURE")

    def test_semantic_failure_requires_error_code(self) -> None:
        with self.assertRaises(ValueError):
            StepActivityResult(kind="semantic_failure")


if __name__ == "__main__":
    unittest.main()
