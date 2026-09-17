from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v3.shot_continuity_linker import ShotContinuityLinker


class _Production:
    def __init__(self):
        self.created = []

    def create_text_asset(self, project_id, **kwargs):
        self.created.append((project_id, kwargs))
        return {"asset_id": "continuity-asset"}


class _Director:
    def __init__(self):
        self.production = _Production()

    def get_project(self, project_id):
        return {"project_id": project_id, "current_stage": "04"}


class _Legacy:
    def __init__(self):
        self.director = _Director()


class ShotContinuityLinkerTests(unittest.TestCase):
    def test_inherits_previous_end_state_and_breaks_on_scene_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "story_continuity"
            folder.mkdir(parents=True)
            project_id = "project-001"
            path = folder / f"{project_id}.json"
            path.write_text(
                json.dumps(
                    {
                        "shots": [
                            {
                                "shot_id": "s1",
                                "global_order": 1,
                                "scene_id": "scene-a",
                                "character_entity_ids": ["hero"],
                                "appearance_version_ids": ["hero-default"],
                                "video_end_state": "少年站到古道入口",
                            },
                            {
                                "shot_id": "s2",
                                "global_order": 2,
                                "scene_id": "scene-a",
                                "character_entity_ids": ["hero"],
                                "appearance_version_ids": ["hero-default"],
                                "video_start_state": "",
                                "video_end_state": "少年走到雪坡中段",
                            },
                            {
                                "shot_id": "s3",
                                "global_order": 3,
                                "scene_id": "scene-b",
                                "character_entity_ids": ["hero"],
                                "appearance_version_ids": ["hero-default"],
                                "video_start_state": "",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            legacy = _Legacy()
            linker = ShotContinuityLinker(SimpleNamespace(data_dir=root), legacy)
            result = linker.link(project_id)
            self.assertEqual(result["linked_count"], 1)
            self.assertEqual(result["break_count"], 1)
            self.assertEqual(result["inherited_start_state_count"], 1)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][1]["video_start_state"], "少年站到古道入口")
            self.assertEqual(saved["shots"][1]["continuity_link"]["mode"], "inherit")
            self.assertEqual(saved["shots"][2]["continuity_link"]["mode"], "break")
            self.assertTrue(legacy.director.production.created)


if __name__ == "__main__":
    unittest.main()
