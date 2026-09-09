from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.asset_authoring_refined import RefinedAuthoringAssetService
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project = {
            "project_id": project_id,
            "status": "completed",
            "current_stage": "04",
            "completed_stages": ["01", "02", "03", "04"],
            "confirmed_outputs": {
                "01": {"handoff": "少年沿雪山古道寻找失落古剑。", "production_asset_ids": []},
                "02": {"handoff": "角色确认", "production_asset_ids": []},
                "03": {"handoff": "视觉确认", "production_asset_ids": []},
                "04": {"handoff": "分镜确认", "production_asset_ids": []},
            },
        }

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director

    def _wb_load_candidates(self, project_id: str):
        return []

    def _wb_sync_candidates(self, project_id: str):
        return []


class RefinedAssetModelTests(unittest.TestCase):
    def _setup(self, root: Path, project_id: str):
        director = _Director(root, project_id)
        p = director.production
        hero = p.create_entity(
            project_id,
            entity_type="character",
            name="少年",
            logical_key="continuity:character:hero",
            metadata={"continuity": {"core_profile": {"外观": "黑发，深蓝冬装，黑色长靴"}}},
        )
        location = p.create_entity(
            project_id,
            entity_type="location",
            name="雪山古道",
            logical_key="continuity:location:snow-road",
            metadata={"continuity": {"core_profile": {"结构": "狭窄山道，两侧积雪岩壁"}}},
        )
        prop = p.create_entity(
            project_id,
            entity_type="prop",
            name="失落古剑",
            logical_key="continuity:prop:sword",
            metadata={"continuity": {"core_profile": {"材质": "深色旧钢"}}},
        )
        scene = p.create_entity(
            project_id,
            entity_type="scene",
            name="雪山寻找",
            logical_key="continuity:scene:001",
            metadata={"continuity": {"summary": "少年在雪山寻找古剑"}},
        )
        return director, hero, location, prop, scene

    def test_status_refresh_is_idempotent_and_does_not_increase_versions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director, hero, location, prop, scene = self._setup(root, project_id)
            service = RefinedAuthoringAssetService(type("S", (), {"data_dir": root})(), _Legacy(director))

            first = service.status(project_id)
            first_ids = {item["entity_id"]: (item["profile_asset_id"], item["profile_version"]) for item in first["items"]}
            second = service.status(project_id)
            second_ids = {item["entity_id"]: (item["profile_asset_id"], item["profile_version"]) for item in second["items"]}

            self.assertEqual(first_ids, second_ids)
            self.assertEqual({item["entity_type"] for item in second["items"]}, {"character", "location", "prop"})
            self.assertNotIn(scene["entity_id"], second_ids)
            self.assertTrue(second["content_idempotent"])
            self.assertEqual(second["canonical_asset_kinds"], ["character", "location", "prop"])

    def test_reference_status_excludes_narrative_scene_duplicates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director, hero, location, prop, scene = self._setup(root, project_id)
            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=lambda *_: None)

            state = service.status(project_id)

            self.assertEqual({item["entity_type"] for item in state["items"]}, {"character", "location", "prop"})
            self.assertEqual(state["required_count"], 3)
            self.assertEqual(state["canonical_asset_kinds"], ["character", "location", "prop"])


if __name__ == "__main__":
    unittest.main()
