from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.v3.workflow.contracts import ProductionStep, StepActivityInput
from app.v3.workflow.domain_executor import DomainStepExecutor
from app.v3.workflow.materialized_generation import GenerationJobStore, MaterializedDomainExecutor


class MaterializedGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.base = DomainStepExecutor(self.settings)
        self.executor = MaterializedDomainExecutor(self.settings, base=self.base)
        self.project_id = "visual-test-project"
        self.workflow_id = "visual-test-workflow"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _input(self, key: str = "visual-test-key") -> StepActivityInput:
        return StepActivityInput(
            workflow_id=self.workflow_id,
            project_id=self.project_id,
            step=ProductionStep(
                step_id="image-generate",
                skill_id="image_direction",
                operation="generation.image.generate_candidate",
                payload_ref="payload://visual-test",
                idempotency_key=key,
            ),
        )

    def test_generation_job_store_round_trip(self) -> None:
        store = GenerationJobStore(self.settings.data_dir)
        written = store.put(
            self.project_id,
            "job-key-001",
            {
                "kind": "image",
                "state": "queued",
                "prompt_id": "prompt-001",
                "provider_id": "provider-a",
                "model_id": "model-a",
            },
        )
        loaded = store.get(self.project_id, "job-key-001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["prompt_id"], "prompt-001")
        self.assertEqual(loaded["state"], "queued")
        self.assertEqual(written["schema_version"], store.schema_version)

    def test_candidate_creation_is_idempotent_by_generation_task(self) -> None:
        artifact = Path(self.temp.name) / "candidate.png"
        artifact.write_bytes(b"fake-png-content")
        input = self._input("candidate-once-key")
        payload = {
            "logical_key": "shot-visual-001",
            "metadata": {"source": "unit-test"},
        }
        first = self.executor._candidate_once(
            input,
            payload,
            provider_id="provider-a",
            model_id="model-a",
            reference_ids=["hero-v1"],
            artifact_ref="artifact://image/img_test",
            artifact_path=artifact,
            prompt_id="prompt-001",
            kind="image",
        )
        second = self.executor._candidate_once(
            input,
            payload,
            provider_id="provider-a",
            model_id="model-a",
            reference_ids=["hero-v1"],
            artifact_ref="artifact://image/img_test",
            artifact_path=artifact,
            prompt_id="prompt-001",
            kind="image",
        )
        self.assertEqual(first["resource_id"], second["resource_id"])
        versions = self.base.resources.list_versions(self.project_id, "shot-visual-001")
        self.assertEqual(len(versions), 1)

    def test_adopted_image_resource_becomes_h3_reference(self) -> None:
        artifact = Path(self.temp.name) / "adopted-frame.png"
        artifact.write_bytes(b"fake-adopted-frame")
        candidate = self.base.resources.create_candidate(
            self.project_id,
            logical_key="image-canonical-001",
            generation_task_id="image-task-001",
            provider_id="provider-a",
            model_id="model-a",
            reference_ids=["hero-v1"],
            metadata={"artifact_path": str(artifact), "artifact_ref": "artifact://image/img_a"},
        )
        self.base.resources.set_audit_result(
            self.project_id,
            candidate["resource_id"],
            passed=True,
            audit={"unit_test": True},
        )
        adopted = self.base.resources.adopt(self.project_id, candidate["resource_id"])

        reference_id, resolved_resource = self.executor._adopted_reference(
            self.project_id,
            "image-canonical-001",
            entity_id="char_hero",
        )
        self.assertEqual(resolved_resource["resource_id"], adopted["resource_id"])
        self.assertEqual(reference_id, f"resource:{adopted['resource_id']}")
        reference = self.base.references.resolve(reference_id)
        self.assertTrue(reference.path.is_file())
        self.assertEqual(reference.entity_id, "char_hero")

    def test_first_artifact_supports_video_outputs(self) -> None:
        artifact = self.executor._first_artifact(
            {
                "outputs": {
                    "18": {
                        "videos": [
                            {
                                "filename": "xiaoduan.mp4",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                }
            }
        )
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["filename"], "xiaoduan.mp4")


if __name__ == "__main__":
    unittest.main()
