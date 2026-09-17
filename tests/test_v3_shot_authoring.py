from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.shot_authoring import ShotAuthoringService


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return {"project_id": project_id}


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director


class ShotAuthoringTests(unittest.TestCase):
    def _setup(self, root: Path, project_id: str):
        director = _Director(root, project_id)
        p = director.production
        hero = p.create_entity(
            project_id,
            entity_type="character",
            name="少年",
            logical_key="continuity:character:hero",
        )
        master = p.create_entity(
            project_id,
            entity_type="character",
            name="师父",
            logical_key="continuity:character:master",
        )
        location = p.create_entity(
            project_id,
            entity_type="location",
            name="雪山古道",
            logical_key="continuity:location:snow-road",
        )
        shot1_entity = p.create_entity(
            project_id,
            entity_type="shot",
            name="镜头001",
            logical_key="continuity:shot:001",
        )
        shot2_entity = p.create_entity(
            project_id,
            entity_type="shot",
            name="镜头002",
            logical_key="continuity:shot:002",
        )
        continuity_root = root / "story_continuity"
        continuity_root.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": "story_continuity_v3_context_budget",
            "project_id": project_id,
            "scenes": [{"scene_id": "scene-1", "location_entity_id": location["entity_id"]}],
            "shots": [
                {
                    "shot_id": "shot-1",
                    "entity_id": shot1_entity["entity_id"],
                    "scene_id": "scene-1",
                    "title": "少年发现线索",
                    "summary": "少年看到雪中的玉佩",
                    "duration_seconds": 3.0,
                    "composition": "中景",
                    "shot_size": "中景",
                    "camera": "平视",
                    "camera_move": "缓慢推进",
                    "action": "少年弯腰看向雪地",
                    "performance": "警觉",
                    "environment": "雪山古道",
                    "dialogue": "",
                    "narration": "他发现了熟悉的玉佩。",
                    "sound": "风声",
                    "music": "紧张",
                    "representative_state": "少年站在雪山古道，目光落在雪地中的玉佩上",
                    "video_start_state": "少年站立并低头",
                    "video_end_state": "少年弯腰伸手",
                    "image_prompt": "少年在雪山古道看向雪中的玉佩，保持深蓝冬装",
                    "video_prompt": "少年缓慢弯腰并伸手拾取玉佩",
                    "character_entity_ids": [hero["entity_id"]],
                    "prop_entity_ids": [],
                    "source_provenance": {"source_evidence": ["雪中露出一枚熟悉的玉佩"]},
                },
                {
                    "shot_id": "shot-2",
                    "entity_id": shot2_entity["entity_id"],
                    "scene_id": "scene-1",
                    "title": "师父远去",
                    "summary": "师父身影出现在山脊",
                    "duration_seconds": 3.0,
                    "representative_state": "师父站在远处山脊",
                    "image_prompt": "师父站在远处雪山山脊",
                    "video_prompt": "师父转身向山顶走去",
                    "character_entity_ids": [master["entity_id"]],
                    "prop_entity_ids": [],
                },
            ],
            "updated_at": "",
        }
        (continuity_root / f"{project_id}.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return director, hero, master, location, shot1_entity, shot2_entity

    def _profile(self, p, project_id, entity):
        return p.create_text_asset(
            project_id,
            stage="02" if entity["entity_type"] == "character" else "03",
            skill="test",
            logical_key=f"studio:authoring:{entity['entity_id']}:profile",
            asset_role=f"{entity['entity_type']}_profile",
            name=entity["name"],
            content=json.dumps({"name": entity["name"]}, ensure_ascii=False),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            entity_ids=[entity["entity_id"]],
        )

    def test_edit_one_shot_versions_contract_and_only_invalidates_that_shot_chain(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director, hero, master, location, shot1_entity, shot2_entity = self._setup(root, project_id)
            p = director.production
            hero_profile = self._profile(p, project_id, hero)
            self._profile(p, project_id, master)
            self._profile(p, project_id, location)
            shot1_image = p.create_text_asset(
                project_id,
                stage="make",
                skill="test",
                logical_key="shot-1-image",
                asset_role="shot_keyframe",
                name="镜头1画面",
                content="image1",
                entity_ids=[shot1_entity["entity_id"], hero["entity_id"]],
                metadata={"shot_id": "shot-1"},
            )
            shot2_image = p.create_text_asset(
                project_id,
                stage="make",
                skill="test",
                logical_key="shot-2-image",
                asset_role="shot_keyframe",
                name="镜头2画面",
                content="image2",
                entity_ids=[shot2_entity["entity_id"], master["entity_id"]],
                metadata={"shot_id": "shot-2"},
            )
            final = p.create_text_asset(
                project_id,
                stage="final",
                skill="test",
                logical_key="final-cut",
                asset_role="final_cut",
                name="成片",
                content="final",
                parent_asset_ids=[shot1_image["asset_id"], shot2_image["asset_id"]],
            )
            service = ShotAuthoringService(SimpleNamespace(data_dir=root), _Legacy(director))

            result = service.update(
                project_id,
                "shot-1",
                {
                    "action": "少年蹲下，从雪中拾起玉佩并握在手中",
                    "representative_state": "少年蹲在雪山古道，右手靠近雪中的玉佩",
                    "video_start_state": "少年站立看向玉佩",
                    "video_end_state": "少年蹲下并将玉佩握在右手",
                    "image_prompt": "深蓝冬装少年蹲在雪山古道，右手靠近雪中的玉佩，人物身份与服装保持一致",
                    "video_prompt": "少年从站立姿态缓慢蹲下，右手拾起玉佩并握住",
                },
                reason="修正拾取玉佩动作",
            )

            self.assertTrue(result["local_invalidation"])
            self.assertEqual(result["contract_version"], 1)
            self.assertEqual(p.get_asset(project_id, shot1_image["asset_id"])["dependency_state"], "stale")
            self.assertEqual(p.get_asset(project_id, shot2_image["asset_id"])["dependency_state"], "current")
            self.assertEqual(p.get_asset(project_id, final["asset_id"])["dependency_state"], "stale")
            contract = p.get_asset(project_id, result["contract_asset_id"])
            self.assertIn(hero_profile["asset_id"], contract["parent_asset_ids"])
            saved = json.loads((root / "story_continuity" / f"{project_id}.json").read_text(encoding="utf-8"))
            shot = next(item for item in saved["shots"] if item["shot_id"] == "shot-1")
            self.assertIn("拾起玉佩", shot["action"])
            self.assertEqual(shot["manual_revision"]["revision_id"], result["revision_id"])

    def test_second_edit_creates_new_contract_version_without_touching_other_shot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director, hero, master, location, *_ = self._setup(root, project_id)
            p = director.production
            self._profile(p, project_id, hero)
            self._profile(p, project_id, master)
            self._profile(p, project_id, location)
            service = ShotAuthoringService(SimpleNamespace(data_dir=root), _Legacy(director))
            common = {
                "representative_state": "少年站在雪道上注视玉佩",
                "image_prompt": "深蓝冬装少年站在雪道上注视玉佩",
                "video_prompt": "少年缓慢向玉佩走近",
            }
            first = service.update(project_id, "shot-1", {**common, "action": "少年走近玉佩"})
            second = service.update(project_id, "shot-1", {**common, "action": "少年停下并低头观察玉佩"})
            self.assertEqual(first["contract_version"], 1)
            self.assertEqual(second["contract_version"], 2)
            self.assertNotEqual(first["contract_asset_id"], second["contract_asset_id"])


if __name__ == "__main__":
    unittest.main()
