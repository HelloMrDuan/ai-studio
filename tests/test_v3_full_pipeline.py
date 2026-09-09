from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.v3.full_pipeline_workflows import FullMovieWorkflowService
from app.v3.generation_executor import ReferenceAssetStore
from app.v3.workflow.full_pipeline_executor import FullPipelineArtifactStore, FullPipelineExecutor


class FullPipelineContractTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        return Settings(data_dir=root / "data")

    def test_full_movie_prepare_contains_entire_story_to_final_mp4_chain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = self._settings(root)
            source = root / "hero.png"
            source.write_bytes(b"fake-image-for-contract-test")
            ReferenceAssetStore(settings.data_dir).import_file("hero-v1", source, entity_id="char_hero")

            request, record = FullMovieWorkflowService(settings).prepare(
                project_id="full-movie-validation",
                story_text="一个人在雪山上寻找失踪的师父。",
                reference_id="hero-v1",
            )

            operations = [step.operation for step in request.steps]
            self.assertEqual(
                operations,
                [
                    "full.llm.screenplay",
                    "full.llm.characters",
                    "full.llm.storyboard",
                    "full.image.generate_from_storyboard",
                    "resource.audit_latest",
                    "resource.adopt_latest",
                    "full.h3.generate_from_storyboard",
                    "resource.audit_latest",
                    "resource.adopt_latest",
                    "full.tts.generate_from_screenplay",
                    "full.subtitle.generate",
                    "full.bgm.select",
                    "full.composition.render",
                ],
            )
            self.assertEqual(request.workflow_id, record["workflow_id"])
            self.assertEqual(request.project_id, "full-movie-validation")
            self.assertEqual(len({step.idempotency_key for step in request.steps}), len(request.steps))

    def test_full_pipeline_manifest_is_atomic_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = FullPipelineArtifactStore(Path(raw))
            first = store.patch_manifest("project-a", "workflow-a", status="running", screenplay_path="/tmp/a.json")
            self.assertEqual(first["status"], "running")
            second = store.patch_manifest("project-a", "workflow-a", status="completed", final_path="/tmp/final.mp4")
            self.assertEqual(second["screenplay_path"], "/tmp/a.json")
            self.assertEqual(second["final_path"], "/tmp/final.mp4")
            loaded = json.loads(store.manifest_path("project-a", "workflow-a").read_text(encoding="utf-8"))
            self.assertEqual(loaded["status"], "completed")

    def test_llm_json_parser_accepts_json_and_code_fence(self) -> None:
        plain = FullPipelineExecutor._json_content('{"shots":[{"shot_id":"s1"}]}')
        fenced = FullPipelineExecutor._json_content('```json\n{"characters":[{"name":"A"}]}\n```')
        self.assertEqual(plain["shots"][0]["shot_id"], "s1")
        self.assertEqual(fenced["characters"][0]["name"], "A")


if __name__ == "__main__":
    unittest.main()
