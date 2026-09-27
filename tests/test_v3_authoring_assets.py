from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.authoring_assets import AuthoringAssetService


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project = {
            "project_id": project_id,
            "status": "completed",
            "current_stage": "04",
            "completed_stages": ["01", "02", "03", "04"],
            "confirmed_outputs": {
                "01": {
                    "handoff": "少年在雪山寻找失落古剑。人物、地点和古剑为持续出现的故事元素。",
                    "production_asset_ids": [],
                },
                "02": {"handoff": "少年角色身份已确认", "production_asset_ids": []},
                "03": {"handoff": "雪山与古剑视觉设定已确认", "production_asset_ids": []},
                "04": {"handoff": "分镜已确认", "production_asset_ids": []},
            },
            "stage_state": {},
        }
        self.confirm_calls = 0

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)

    async def confirm_stage(self, project_id: str):
        self.confirm_calls += 1
        return {"ok": True}


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director

    def _wb_load_candidates(self, project_id: str):
        return []


class AuthoringAssetTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, root: Path, project_id: str):
        director = _Director(root, project_id)
        p = director.production
        a = p.create_entity(
            project_id,
            entity_type="character",
            name="少年",
            logical_key="continuity:character:young-hero",
            metadata={
                "continuity": {
                    "aliases": ["少年侠客"],
                    "core_profile": {"外观": "黑发，深蓝冬装，黑色长靴"},
                    "default_state": {"身份": "寻找师父的少年"},
                }
            },
        )
        b = p.create_entity(
            project_id,
            entity_type="character",
            name="师父",
            logical_key="continuity:character:master",
            metadata={
                "continuity": {
                    "core_profile": {"外观": "灰发，深色长袍"},
                    "default_state": {},
                }
            },
        )
        location = p.create_entity(
            project_id,
            entity_type="location",
            name="雪山古道",
            logical_key="continuity:location:snow-road",
            metadata={"continuity": {"core_profile": {"结构": "狭窄山道，两侧积雪岩壁"}}},
        )
        return director, a, b, location

    async def test_sync_materializes_story_and_reusable_entity_profiles(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director, a, b, location = self._setup(root, project_id)
            service = AuthoringAssetService(SimpleNamespace(data_dir=root), _Legacy(director))

            result = service.sync(project_id)
            state = service.status(project_id)

            self.assertTrue(result["story_asset_id"])
            self.assertEqual(len(state["items"]), 3)
            self.assertTrue(state["local_invalidation"])
            for item in state["items"]:
                self.assertTrue(item["profile_asset_id"])
                self.assertGreaterEqual(item["profile_version"], 1)
                self.assertTrue(item["stable_design"])

            profile = next(item for item in state["items"] if item["entity_id"] == a["entity_id"])
            self.assertIn("黑发", profile["stable_design"])
            self.assertIn("深蓝冬装", profile["stable_design"])

    async def test_editing_one_character_only_stales_assets_that_reference_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director, a, b, _ = self._setup(root, project_id)
            service = AuthoringAssetService(SimpleNamespace(data_dir=root), _Legacy(director))
            service.sync(project_id)
            p = director.production

            profiles = {
                item["entity_id"]: item["profile_asset_id"]
                for item in service.status(project_id)["items"]
            }
            ref_a = p.declare_asset(
                project_id,
                stage="03",
                skill="test",
                logical_key="ref-a",
                asset_type="IMAGE",
                asset_role="character_reference",
                name="少年参考图",
                status="ready",
                parent_asset_ids=[profiles[a["entity_id"]]],
                entity_ids=[a["entity_id"]],
            )
            ref_b = p.declare_asset(
                project_id,
                stage="03",
                skill="test",
                logical_key="ref-b",
                asset_type="IMAGE",
                asset_role="character_reference",
                name="师父参考图",
                status="ready",
                parent_asset_ids=[profiles[b["entity_id"]]],
                entity_ids=[b["entity_id"]],
            )
            shot_a = p.create_text_asset(
                project_id,
                stage="04",
                skill="test",
                logical_key="shot-a",
                asset_role="shot_contract",
                name="少年镜头",
                content="少年沿雪山古道前进",
                parent_asset_ids=[profiles[a["entity_id"]]],
                entity_ids=[a["entity_id"]],
            )
            shot_b = p.create_text_asset(
                project_id,
                stage="04",
                skill="test",
                logical_key="shot-b",
                asset_role="shot_contract",
                name="师父镜头",
                content="师父站在山顶",
                parent_asset_ids=[profiles[b["entity_id"]]],
                entity_ids=[b["entity_id"]],
            )
            image_a = p.create_text_asset(
                project_id,
                stage="make",
                skill="test",
                logical_key="image-a",
                asset_role="shot_keyframe",
                name="少年画面",
                content="image-a",
                parent_asset_ids=[shot_a["asset_id"], ref_a["asset_id"]],
                entity_ids=[a["entity_id"]],
            )
            image_b = p.create_text_asset(
                project_id,
                stage="make",
                skill="test",
                logical_key="image-b",
                asset_role="shot_keyframe",
                name="师父画面",
                content="image-b",
                parent_asset_ids=[shot_b["asset_id"], ref_b["asset_id"]],
                entity_ids=[b["entity_id"]],
            )
            final = p.create_text_asset(
                project_id,
                stage="final",
                skill="test",
                logical_key="final",
                asset_role="final_cut",
                name="最终成片",
                content="final",
                parent_asset_ids=[image_a["asset_id"], image_b["asset_id"]],
            )

            result = service.update_profile(
                project_id,
                a["entity_id"],
                stable_design="黑色短发，深蓝色冬装外套，黑色长靴，固定银色护腕，整体配色保持深蓝与黑色。",
                change_reason="锁定少年服装与鞋履",
            )

            self.assertTrue(result["local_invalidation"])
            self.assertGreater(result["profile_version"], 1)
            for asset in (ref_a, shot_a, image_a, final):
                self.assertEqual(p.get_asset(project_id, asset["asset_id"])["dependency_state"], "stale")
            for asset in (ref_b, shot_b, image_b):
                self.assertEqual(p.get_asset(project_id, asset["asset_id"])["dependency_state"], "current")

    async def test_confirmation_hook_keeps_original_stage_flow_and_syncs_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director, *_ = self._setup(root, project_id)
            service = AuthoringAssetService(SimpleNamespace(data_dir=root), _Legacy(director))
            service.install_confirmation_hook()

            result = await director.confirm_stage(project_id)

            self.assertEqual(result, {"ok": True})
            self.assertEqual(director.confirm_calls, 1)
            self.assertTrue(service.status(project_id)["items"])


if __name__ == "__main__":
    unittest.main()
