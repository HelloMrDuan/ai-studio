from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.v3.shot_refinement import ShotRefinementService


class _Production:
    def get_asset(self, project_id, asset_id):
        return {"asset_id": asset_id, "metadata": {"shot_id": "shot-1"}}


class _Director:
    def __init__(self):
        self.production = _Production()


class _Legacy:
    def __init__(self):
        self.director = _Director()
        self.submitted = None

    def _wb_load_candidates(self, project_id):
        return [
            {
                "candidate_id": "cand-preview",
                "capability": "image",
                "status": "completed",
                "target_asset_id": "target-1",
                "prompt_asset_id": "prompt-1",
                "mode": "txt2img",
                "confirmed_asset_id": "",
                "params": {"quality_stage": "preview", "quality_tier": "A", "seed": 42, "aspect_ratio": "16:9"},
            }
        ]

    def _studio_formal_shot(self, project_id, shot_id):
        return {"shot_id": shot_id, "quality_tier": "A", "title": "关键特写"}

    async def director_workbench_execute_candidate(self, project_id, payload):
        self.submitted = payload
        return {"candidate": {"candidate_id": "cand-final"}}


class ShotRefinementTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_becomes_separate_same_seed_final_candidate(self) -> None:
        legacy = _Legacy()
        service = ShotRefinementService(legacy)
        result = await service.refine("project-1", "cand-preview")
        self.assertTrue(result["manual_adoption_required"])
        self.assertEqual(result["quality_tier"], "A")
        self.assertEqual(result["final_steps"], 34)
        params = legacy.submitted["params"]
        self.assertEqual(params["quality_stage"], "final")
        self.assertEqual(params["steps"], 34)
        self.assertEqual(params["seed"], 42)
        self.assertEqual(params["preview_candidate_id"], "cand-preview")
        self.assertNotIn("confirmed_asset_id", legacy.submitted)


if __name__ == "__main__":
    unittest.main()
