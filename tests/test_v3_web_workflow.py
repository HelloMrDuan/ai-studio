from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.v3.web_workflows import WebVisualWorkflowService


class WebVisualWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.service = WebVisualWorkflowService(self.settings)
        source = Path(self.temp.name) / "hero.png"
        source.write_bytes(b"fake-reference-image")
        self.service.references.import_file("hero-v1", source, entity_id="char_hero")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_builds_same_six_step_visual_temporal_chain(self) -> None:
        request, record = self.service.prepare(
            project_id="web-validation",
            reference_id="hero-v1",
            source_text="same canonical woman on a snowy mountain path",
            video_prompt="same woman walks forward; preserve identity and clothing",
        )
        self.assertEqual(record["workflow_id"], request.workflow_id)
        self.assertEqual(
            [step.step_id for step in request.steps],
            [
                "image-generate",
                "image-audit",
                "image-adopt",
                "h3-generate",
                "h3-audit",
                "h3-adopt",
            ],
        )
        self.assertEqual(request.steps[0].operation, "generation.image.generate_candidate")
        self.assertEqual(request.steps[3].operation, "generation.h3.generate_candidate")
        image_payload = self.service.payloads.resolve("web-validation", request.steps[0].payload_ref)
        video_payload = self.service.payloads.resolve("web-validation", request.steps[3].payload_ref)
        self.assertEqual(image_payload["reference_ids"], ["hero-v1"])
        self.assertEqual(video_payload["first_frame_logical_key"], record["image_logical_key"])

    def test_media_url_uses_private_v3_artifact_endpoint(self) -> None:
        media = self.service._media(
            {
                "resource_id": "res_001",
                "state": "adopted",
                "reference_ids": ["hero-v1"],
                "metadata": {"artifact_ref": "artifact://image/img_abc123", "artifact_path": "/tmp/a.png"},
            },
            "image",
        )
        self.assertIsNotNone(media)
        self.assertEqual(media["media_url"], "/api/v3/media/image/img_abc123")


if __name__ == "__main__":
    unittest.main()
