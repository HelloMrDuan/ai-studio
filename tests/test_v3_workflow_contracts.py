from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from PIL import Image
from app.config import Settings
from app.v3.full_pipeline_workflows import FullMovieWorkflowService
from app.v3.workflow.full_pipeline_executor import FullPipelineArtifactStore
from concurrent.futures import ThreadPoolExecutor

from app.v3.workflow.contracts import (
    ProductionStep,
    ProductionWorkflowInput,
    StepActivityResult,
)


class XiaoduanV3WorkflowContractTests(unittest.TestCase):
    def test_parallel_activity_manifest_updates_are_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            store = FullPipelineArtifactStore(Path(raw))
            updates = {"image_path": "image.png", "voice_path": "voice.mp3", "bgm_path": "bgm.mp3"}
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(store.patch_manifest, "project", "workflow", **{key: value}) for key, value in updates.items()]
                for future in futures:
                    future.result()
            manifest = store.manifest("project", "workflow")
            self.assertEqual({key: manifest[key] for key in updates}, updates)

    def test_real_full_movie_plan_fans_out_after_storyboard(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = FullMovieWorkflowService(Settings(data_dir=root))
            image = root / "reference.png"
            Image.new("RGB", (8, 8), color="white").save(image)
            service.references.import_file("reference-1", image, entity_id="character-1")
            request, _ = service.prepare(project_id="e" * 24, story_text="A traveller enters a city.", reference_id="reference-1")
            completed = ["screenplay", "characters", "storyboard"]
            self.assertEqual({step.step_id for step in request.ready_steps(completed)}, {"image-generate", "tts", "bgm"})
            by_id = {step.step_id: step for step in request.steps}
            self.assertEqual(by_id["h3-generate"].depends_on, ("image-adopt",))
            self.assertEqual(by_id["subtitle"].depends_on, ("h3-adopt", "tts"))
            self.assertEqual(by_id["composition"].depends_on, ("subtitle", "bgm"))
            completed += ["image-generate", "image-audit", "image-adopt", "h3-generate", "h3-audit", "h3-adopt", "tts", "subtitle"]
            self.assertNotIn("composition", {s.step_id for s in request.ready_steps(completed)})
            completed.append("bgm")
            self.assertEqual([s.step_id for s in request.ready_steps(completed)], ["composition"])
            self.assertEqual(service._stage({"screenplay_path": "s", "characters_path": "c", "storyboard_path": "b", "bgm_path": "music"}, None, None), "image")

    def test_dag_rejects_missing_or_cyclic_dependencies(self):
        for dependencies in [("missing",), ("one",)]:
            with self.assertRaisesRegex(ValueError, "missing dependencies or a cycle"):
                ProductionWorkflowInput("workflow", "project", (
                    ProductionStep("one", "image", "image", "payload://one", "key-one", dependencies),
                ))

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
