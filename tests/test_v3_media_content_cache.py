from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.v3.workflow.production_cached_executor import MediaContentCache


class MediaContentCacheTests(unittest.TestCase):
    def test_exact_artifact_is_reused_and_tampering_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "frame.png"
            artifact.write_bytes(b"stable-frame-v1")
            cache = MediaContentCache(root)
            signature = "a" * 64
            cache.put(
                "project-001",
                signature,
                {
                    "kind": "image",
                    "provider_id": "local-comfyui-image",
                    "model_id": "configured-image-workflow",
                    "reference_ids": ["ref:1"],
                    "artifact_ref": "artifact://image/a",
                    "artifact_path": str(artifact),
                    "generation_task_id": "task-1",
                },
            )
            hit = cache.get("project-001", signature)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["artifact_ref"], "artifact://image/a")

            artifact.write_bytes(b"changed-frame")
            self.assertIsNone(cache.get("project-001", signature))

    def test_missing_artifact_never_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = MediaContentCache(temp)
            cache.put(
                "project-001",
                "b" * 64,
                {
                    "kind": "video",
                    "artifact_ref": "artifact://video/x",
                    "artifact_path": str(Path(temp) / "missing.mp4"),
                },
            )
            self.assertIsNone(cache.get("project-001", "b" * 64))


if __name__ == "__main__":
    unittest.main()
