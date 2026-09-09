from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v3.bgm_prefetch import BGMPrefetchService


class _Director:
    def get_project(self, project_id):
        return {"project_id": project_id}


class _Legacy:
    def __init__(self):
        self.director = _Director()


class BGMPrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_exposes_candidates_without_auto_adopting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            continuity = root / "story_continuity"
            continuity.mkdir(parents=True)
            project_id = "project-001"
            (continuity / f"{project_id}.json").write_text(
                json.dumps({"shots": [{"shot_id": "s1", "music": "冷峻、克制、逐步增强"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            bgm_root = root / "v3" / "bgm"
            bgm_root.mkdir(parents=True)
            # Preparation only indexes the already-owned library. Decode
            # validation is intentionally deferred to adoption.
            (bgm_root / "snow-theme.mp3").write_bytes(b"library-placeholder")

            service = BGMPrefetchService(SimpleNamespace(data_dir=root), _Legacy())
            state = await service.prepare(project_id)
            self.assertEqual(state["bgm_prefetch_status"], "ready")
            self.assertIn("冷峻", state["bgm_intent"])
            self.assertEqual(len(state["bgm_candidates"]), 1)
            self.assertFalse(bool(state.get("bgm_path")))
            public = service.public_state(project_id)
            self.assertTrue(public["manual_adoption_required"])
            self.assertTrue(public["candidates"][0]["url"].startswith("/files/"))

    async def test_no_library_is_explicit_not_fake_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = BGMPrefetchService(SimpleNamespace(data_dir=Path(temp)), _Legacy())
            state = await service.prepare("project-001")
            self.assertEqual(state["bgm_prefetch_status"], "needs_source")
            self.assertEqual(state["bgm_candidates"], [])
            self.assertIn("不会随机乱配", state["bgm_prefetch_message"])


if __name__ == "__main__":
    unittest.main()
