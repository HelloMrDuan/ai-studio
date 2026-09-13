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
            legacy = _Legacy(director)
            authoring = RefinedAuthoringAssetService(type("S", (), {"data_dir": root})(), legacy)
            authoring.status(project_id)

            service = CanonicalReferenceAssetBootstrap(legacy, submit_candidate=lambda *_: None)
            state = service.status(project_id)

            self.assertEqual({item["entity_type"] for item in state["items"]}, {"character", "location", "prop"})
            self.assertEqual(state["required_count"], 3)
            self.assertEqual(state["canonical_asset_kinds"], ["character", "location", "prop"])
            self.assertTrue(state["stable_profile_required"])
            self.assertFalse(state["stage01_story_entities_exposed"])

    def test_same_type_same_name_entities_merge_into_one_current_card_and_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director = _Director(root, project_id)
            p = director.production
            suyao_a = p.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="story:character:suyao",
                metadata={"continuity": {"core_profile": {"年龄": "16"}}},
            )
            suyao_b = p.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="design:character:suyao",
                metadata={"continuity": {"core_profile": {"外观": "红衣，高马尾，腰间青玉坠"}}},
            )
            p.create_entity(
                project_id,
                entity_type="location",
                name="苍梧山顶",
                logical_key="story:location:cangwu",
                metadata={"spatial_facts": "积雪、石阶"},
            )
            p.create_entity(
                project_id,
                entity_type="location",
                name="苍梧山顶",
                logical_key="design:location:cangwu",
                metadata={"fixed_anchor": "废弃山门"},
            )
            p.create_entity(
                project_id,
                entity_type="prop",
                name="古剑",
                logical_key="story:prop:sword",
                metadata={"type": "武器"},
            )
            p.create_entity(
                project_id,
                entity_type="prop",
                name="古剑",
                logical_key="design:prop:sword",
                metadata={"function": "剧情关键道具"},
            )

            service = RefinedAuthoringAssetService(type("S", (), {"data_dir": root})(), _Legacy(director))
            state = service.status(project_id)

            keys = [(item["entity_type"], item["name"]) for item in state["items"]]
            self.assertEqual(keys.count(("character", "苏瑶")), 1)
            self.assertEqual(keys.count(("location", "苍梧山顶")), 1)
            self.assertEqual(keys.count(("prop", "古剑")), 1)
            suyao = next(item for item in state["items"] if item["entity_type"] == "character")
            self.assertEqual(suyao["duplicate_source_count"], 2)
            self.assertEqual(set(suyao["source_entity_ids"]), {suyao_a["entity_id"], suyao_b["entity_id"]})
            self.assertIn("16", suyao["stable_design"])
            self.assertIn("高马尾", suyao["stable_design"])
            self.assertGreaterEqual(state["duplicate_groups_merged"], 3)

            profile_map = service._profile_map(project_id)
            self.assertEqual(
                profile_map[suyao_a["entity_id"]]["asset_id"],
                profile_map[suyao_b["entity_id"]]["asset_id"],
            )

    def test_editing_canonical_card_invalidates_dependents_bound_to_old_alias_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "e" * 24
            director = _Director(root, project_id)
            p = director.production
            old = p.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="story:character:suyao",
                metadata={"continuity": {"core_profile": {"年龄": "16"}}},
            )
            new = p.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="design:character:suyao",
                metadata={"continuity": {"core_profile": {"外观": "红衣，高马尾"}}},
            )
            shot = p.declare_asset(
                project_id,
                stage="04",
                skill="test",
                logical_key="shot:001",
                asset_type="STRUCTURED_DATA",
                asset_role="shot_contract",
                name="镜头001",
                status="ready",
                source={"type": "test"},
                parent_asset_ids=[],
                entity_ids=[old["entity_id"]],
                metadata={},
            )
            service = RefinedAuthoringAssetService(type("S", (), {"data_dir": root})(), _Legacy(director))
            state = service.status(project_id)
            card = next(item for item in state["items"] if item["entity_type"] == "character")

            result = service.update_profile(
                project_id,
                card["entity_id"],
                stable_design="16岁少女，红衣，高马尾，腰间固定青玉坠。",
                change_reason="补齐稳定身份",
            )

            self.assertEqual(result["duplicate_aliases_updated"], 1)
            self.assertIn(old["entity_id"], result["source_entity_ids"])
            self.assertIn(new["entity_id"], result["source_entity_ids"])
            self.assertEqual(p.get_asset(project_id, shot["asset_id"])["dependency_state"], "stale")

    def test_stage01_discovery_entities_do_not_show_as_stable_asset_cards(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "d" * 24
            director = _Director(root, project_id)
            director.project["current_stage"] = "01"
            director.project["completed_stages"] = []
            director.project["confirmed_outputs"] = {}
            director.production.create_entity(project_id, entity_type="character", name="苏瑶", metadata={"age": 16})
            director.production.create_entity(project_id, entity_type="location", name="苍梧山顶", metadata={"snow": True})
            director.production.create_entity(project_id, entity_type="prop", name="古剑", metadata={"type": "武器"})

            service = RefinedAuthoringAssetService(type("S", (), {"data_dir": root})(), _Legacy(director))
            state = service.status(project_id)

            self.assertEqual(state["items"], [])
            self.assertFalse(state["stage01_story_entities_exposed"])


if __name__ == "__main__":
    unittest.main()
