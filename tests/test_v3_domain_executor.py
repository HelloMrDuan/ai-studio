from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.v3.workflow.contracts import ProductionStep, StepActivityInput
from app.v3.workflow.domain_executor import DomainStepExecutor


class DomainStepExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.executor = DomainStepExecutor(self.settings)
        self.project_id = "test-project"
        self.workflow_id = "test-workflow"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, step: ProductionStep):
        return asyncio.run(
            self.executor(
                StepActivityInput(
                    workflow_id=self.workflow_id,
                    project_id=self.project_id,
                    step=step,
                )
            )
        )

    def _step(self, step_id: str, operation: str, payload_ref: str, key: str) -> ProductionStep:
        return ProductionStep(
            step_id=step_id,
            skill_id="test-skill",
            operation=operation,
            payload_ref=payload_ref,
            idempotency_key=key,
        )

    def test_resource_lifecycle_is_real_and_idempotent(self) -> None:
        logical_key = "shot-001"
        candidate_ref = self.executor.payloads.put(
            self.project_id,
            "candidate-001",
            {
                "logical_key": logical_key,
                "generation_task_id": "task-001",
                "provider_id": "test-provider",
                "model_id": "test-model",
                "reference_ids": ["hero-v1"],
                "metadata": {"source": "unit-test"},
            },
        )
        candidate_step = self._step(
            "candidate",
            "resource.candidate.create",
            candidate_ref,
            "candidate-idempotency",
        )
        first = self._run(candidate_step)
        second = self._run(candidate_step)
        self.assertEqual(first.kind, "completed")
        self.assertEqual(first.output_ref, second.output_ref)
        versions = self.executor.resources.list_versions(self.project_id, logical_key)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["state"], "generated")

        audit_ref = self.executor.payloads.put(
            self.project_id,
            "audit-001",
            {"logical_key": logical_key, "passed": True, "audit": {"score": 1.0}},
        )
        audited = self._run(
            self._step("audit", "resource.audit_latest", audit_ref, "audit-idempotency")
        )
        self.assertEqual(audited.kind, "completed")
        self.assertEqual(
            self.executor.resources.list_versions(self.project_id, logical_key)[0]["state"],
            "candidate_ready",
        )

        adopt_ref = self.executor.payloads.put(
            self.project_id,
            "adopt-001",
            {"logical_key": logical_key},
        )
        adopted = self._run(
            self._step("adopt", "resource.adopt_latest", adopt_ref, "adopt-idempotency")
        )
        self.assertEqual(adopted.kind, "completed")
        canonical = self.executor.resources.adopted(self.project_id, logical_key)
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical["state"], "adopted")
        self.assertEqual(adopted.output_ref, f"resource://{canonical['resource_id']}")

    def test_missing_payload_is_typed_semantic_failure(self) -> None:
        result = self._run(
            self._step(
                "missing",
                "resource.candidate.create",
                "payload://does-not-exist",
                "missing-payload-key",
            )
        )
        self.assertEqual(result.kind, "semantic_failure")
        self.assertEqual(result.error_code, "REFERENCE_OR_PAYLOAD_UNAVAILABLE")

    def test_unsupported_operation_fails_closed(self) -> None:
        payload_ref = self.executor.payloads.put(self.project_id, "noop-001", {})
        result = self._run(
            self._step("unsupported", "legacy.stage04.magic", payload_ref, "unsupported-key")
        )
        self.assertEqual(result.kind, "semantic_failure")
        self.assertEqual(result.error_code, "UNSUPPORTED_OPERATION")


if __name__ == "__main__":
    unittest.main()
