from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.services.production_assets import ProductionAssetService
from app.v3.shot_authoring import (ShotAuthoringService, _normalize_back_prop_geometry,
                                   _reconcile_prop_holders, _story_prop_holders)
from app.v3.storyboard.contracts import ShotSupportRegion, ShotVisualPlan, snap_shot_support, validate_shot_support
from app.v3.workflow.unified_image_executor import build_shot_scene_plate_prompt


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
    def test_story_prop_holders_rebind_swapped_model_identity(self):
        state = {"events": [
            {"target_type": "prop", "target_id": "prop-back", "scene_id": "scene-1",
             "source_kind": "story", "scope": "persistent",
             "patch": {"holder_entity_id": "character-a"}},
            {"target_type": "prop", "target_id": "prop-hand", "scene_id": "scene-1",
             "source_kind": "story", "scope": "persistent",
             "patch": {"holder_entity_id": "character-b"}},
        ]}
        holders = _story_prop_holders(state, {"scene_id": "scene-1"},
                                      ["prop-back", "prop-hand"],
                                      ["character-a", "character-b"])
        raw = {"prop_placements": [
            {"entity_id": "prop-hand", "holder_entity_id": "character-a",
             "attachment": "back", "screen_position": "upper back"},
            {"entity_id": "prop-back", "holder_entity_id": "character-b",
             "attachment": "hand", "screen_position": "right hand"},
        ]}
        corrected, changes = _reconcile_prop_holders(raw, holders)
        self.assertEqual([(row["entity_id"], row["holder_entity_id"], row["attachment"])
                          for row in corrected["prop_placements"]], [
            ("prop-hand", "character-b", "hand"),
            ("prop-back", "character-a", "back"),
        ])
        self.assertEqual(len(changes), 2)
        self.assertEqual(raw["prop_placements"][0]["entity_id"], "prop-hand")

    def test_prop_holder_without_story_authority_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no source-grounded holder"):
            _reconcile_prop_holders({"prop_placements": [
                {"entity_id": "prop-x", "holder_entity_id": "character-x", "attachment": "hand"},
            ]}, {})

    def test_hand_prop_requires_anatomical_side(self):
        frame = {
            "shot_id": "shot-x", "location_entity_id": "location-x",
            "frame_description": "One character holds a small object beside a stone wall.",
            "composition": "A full-body person standing in a medium-wide frame",
            "standing_surface": "Flat stone ground",
            "subject_positions": [{
                "entity_id": "character-x", "screen_position": "center",
                "pose_and_gaze": "standing", "body_view": "front",
                "screen_box": [0.2, 0.1, 0.6, 0.9],
            }],
            "prop_placements": [{
                "entity_id": "prop-x", "holder_entity_id": "character-x",
                "screen_position": "at the right hand", "visible_state": "held",
                "screen_box": [0.25, 0.55, 0.35, 0.65],
                "layer": "front_of_subject", "attachment": "hand",
                "attachment_anchor": [0.3, 0.6],
            }],
            "visual_exclusions": [],
        }
        with self.assertRaisesRegex(ValueError, "anatomical hand_side"):
            ShotVisualPlan.model_validate(frame)
        frame["prop_placements"][0]["hand_side"] = "right"
        self.assertEqual(ShotVisualPlan.model_validate(frame).prop_placements[0].hand_side, "right")
    def test_walkable_plate_rejects_unsupported_feet_before_render(self):
        base = {
            "shot_id": "shot-1", "location_entity_id": "location-1",
            "frame_description": "Two travelers stand together on a narrow snowy rocky ledge.",
            "composition": "Two full-length characters on the same ledge",
            "standing_surface": "Snow over granite",
            "subject_positions": [
                {"entity_id": "character-a", "screen_position": "left", "pose_and_gaze": "standing",
                 "body_view": "back", "screen_box": [0.2, 0.2, 0.4, 0.85]},
                {"entity_id": "character-b", "screen_position": "right", "pose_and_gaze": "standing",
                 "body_view": "front", "screen_box": [0.46, 0.28, 0.58, 0.83]},
            ],
            "prop_placements": [], "visual_exclusions": [],
        }
        region = ShotSupportRegion(polygon=[(0.15, 1), (0.18, 0.68), (0.36, 0.48),
                                            (0.50, 0.50), (0.68, 0.72), (0.78, 1)])
        validate_shot_support(ShotVisualPlan.model_validate(base), [region])
        unsupported = copy.deepcopy(base)
        unsupported["subject_positions"][1]["screen_box"] = [0.72, 0.28, 0.84, 0.83]
        with self.assertRaisesRegex(ValueError, "unsupported foot position"):
            validate_shot_support(ShotVisualPlan.model_validate(unsupported), [region])
        unsupported["prop_placements"] = [{
            "entity_id": "prop-b", "holder_entity_id": "character-b",
            "screen_position": "right hand", "visible_state": "held",
            "screen_box": [0.78, 0.5, 0.82, 0.6], "layer": "front_of_subject",
            "attachment": "hand", "attachment_anchor": [0.8, 0.55], "hand_side": "right",
        }]
        shifted, changes = snap_shot_support(ShotVisualPlan.model_validate(unsupported), [region])
        validate_shot_support(shifted, [region])
        self.assertLess(shifted.subject_positions[1].screen_box[0], 0.72)
        self.assertEqual(len(changes), 1)
        self.assertAlmostEqual(
            shifted.prop_placements[0].screen_box[0] - shifted.subject_positions[1].screen_box[0],
            0.06, places=3,
        )

    def test_scene_plate_approval_is_versioned_and_invalidated_by_formal_edit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director, _hero, _master, location, *_ = self._setup(root, project_id)
            media = root / "v3" / "media" / "image"
            media.mkdir(parents=True)
            Image.new("RGB", (640, 640), "white").save(media / "location.png")
            Image.new("RGB", (640, 640), "gray").save(media / "plate.png")
            source = director.production.register_existing_file(
                project_id, stage="03", skill="test", logical_key="location-ref",
                asset_type="IMAGE", asset_role="location_reference", name="地点参考",
                url="/files/v3/media/image/location.png", entity_ids=[location["entity_id"]],
            )
            service = ShotAuthoringService(SimpleNamespace(data_dir=root), _Legacy(director))
            approved = service.approve_scene_plate(project_id, "shot-1", {
                "source_location_asset_id": source["asset_id"],
                "image_url": "/files/v3/media/image/plate.png",
                "support_regions": [{"polygon": [[0.1, 0.9], [0.1, 0.4], [0.9, 0.4], [0.9, 0.9]]}],
            })
            plate = director.production.get_asset(project_id, approved["asset_id"])
            self.assertEqual(plate["parent_asset_ids"], [source["asset_id"]])
            self.assertEqual(plate["metadata"]["sha256"], approved["sha256"])
            service.update(project_id, "shot-1", {"composition": "近景，人物走在雪道上"})
            self.assertEqual(director.production.get_asset(project_id, approved["asset_id"])["dependency_state"], "stale")

    def test_back_geometry_is_derived_from_typed_view_not_model_layer(self):
        raw = {
            "shot_id": "s", "location_entity_id": "l",
            "frame_description": "A traveler is viewed from behind on a rocky snowy ledge.",
            "composition": "Full-body back view with visible upper back",
            "standing_surface": "Snow and stone",
            "subject_positions": [{
                "entity_id": "c", "screen_position": "left", "pose_and_gaze": "facing away",
                "body_view": "back", "screen_box": [0.12, 0.3, 0.42, 0.92],
            }],
            "prop_placements": [{
                "entity_id": "p", "holder_entity_id": "c",
                "screen_position": "upper back", "visible_state": "visible",
                "screen_box": [0.25, 0.25, 0.45, 0.4],
                "layer": "behind_subject", "attachment": "back",
                "attachment_anchor": [0.35, 0.3], "hand_side": None,
            }],
            "visual_exclusions": [],
        }
        normalized, changes = _normalize_back_prop_geometry(raw)
        self.assertEqual(raw["prop_placements"][0]["layer"], "behind_subject")
        self.assertEqual(normalized["prop_placements"][0]["layer"], "front_of_subject")
        self.assertEqual(len(changes), 1)
        sword_box = normalized["prop_placements"][0]["screen_box"]
        self.assertAlmostEqual((sword_box[0] + sword_box[2]) / 2, 0.27, places=3)
        ShotVisualPlan.model_validate(normalized)
    def test_back_carried_prop_is_visible_in_back_view(self):
        frame = {
            "shot_id": "shot-back", "location_entity_id": "location-x",
            "frame_description": "A traveler is seen from behind with a long object across the upper back.",
            "composition": "Rear full-body view beside a mountain trail",
            "standing_surface": "Stone trail",
            "subject_positions": [{
                "entity_id": "character-x", "screen_position": "center",
                "pose_and_gaze": "standing away", "body_view": "back",
                "screen_box": [0.2, 0.1, 0.5, 0.9],
            }],
            "prop_placements": [{
                "entity_id": "prop-x", "holder_entity_id": "character-x",
                "screen_position": "upper back", "visible_state": "visible and attached",
                "screen_box": [0.27, 0.2, 0.4, 0.55],
                "layer": "front_of_subject", "attachment": "back",
                "attachment_anchor": [0.32, 0.3],
            }],
            "visual_exclusions": [],
        }
        ShotVisualPlan.model_validate(frame)
        hidden = copy.deepcopy(frame)
        hidden["prop_placements"][0]["layer"] = "behind_subject"
        with self.assertRaisesRegex(ValueError, "depth layer conflicts"):
            ShotVisualPlan.model_validate(hidden)
        tiny = copy.deepcopy(frame)
        tiny["prop_placements"][0]["screen_box"] = [0.27, 0.2, 0.4, 0.3]
        with self.assertRaisesRegex(ValueError, "too small"):
            ShotVisualPlan.model_validate(tiny)
    def test_back_prop_requires_upper_back_anchor_and_side_view_is_typed(self):
        frame = {
            "shot_id": "shot-1", "location_entity_id": "location-1",
            "frame_description": "A traveler stands on an empty path with one sword on the back.",
            "composition": "Side view, full body, grounded feet",
            "standing_surface": "A narrow snow-covered stone path beside the mountain cliff",
            "subject_positions": [{"entity_id": "character-1", "screen_position": "left",
                                   "pose_and_gaze": "standing and looking right",
                                   "body_view": "right_profile", "screen_box": [0.1, 0.1, 0.4, 0.9]}],
            "prop_placements": [{"entity_id": "prop-1", "holder_entity_id": "character-1",
                                 "screen_position": "on upper back", "visible_state": "sheathed",
                                 "screen_box": [0.23, 0.22, 0.35, 0.65],
                                 "layer": "behind_subject", "attachment": "back",
                                 "attachment_anchor": [0.3, 0.35]}],
            "visual_exclusions": [],
        }
        self.assertEqual(ShotVisualPlan.model_validate(frame).subject_positions[0].body_view,
                         "right_profile")
        bad = copy.deepcopy(frame)
        bad["prop_placements"][0]["screen_box"] = [0.23, 0.62, 0.35, 0.72]
        bad["prop_placements"][0]["attachment_anchor"] = [0.3, 0.67]
        with self.assertRaisesRegex(ValueError, "outside the holder body region"):
            ShotVisualPlan.model_validate(bad)
        low_back = copy.deepcopy(frame)
        low_back["prop_placements"][0]["screen_box"] = [0.23, 0.4, 0.35, 0.6]
        low_back["prop_placements"][0]["attachment_anchor"] = [0.3, 0.42]
        with self.assertRaisesRegex(ValueError, "starts below"):
            ShotVisualPlan.model_validate(low_back)

        prompt = build_shot_scene_plate_prompt(
            ShotVisualPlan.model_validate(frame), "The mountain path and cliff are covered in snow."
        )
        self.assertIn("narrow snow-covered stone path", prompt)
        self.assertIn("mountain path and cliff", prompt)
        self.assertIn("Do not replace a narrow path", prompt)
        self.assertNotIn("broad, continuous, level walkable floor", prompt)

    def test_render_visual_plan_keeps_frozen_camera_and_shot_size(self):
        with tempfile.TemporaryDirectory() as raw:
            project_id = "f" * 24
            director, hero, _master, location, _shot1, _shot2 = self._setup(Path(raw), project_id)
            service = ShotAuthoringService(SimpleNamespace(data_dir=raw), _Legacy(director))
            rendered = service.render_visual_plan(project_id, {
                "frame_description": "人物站在雪山古道上，目光望向前方",
                "composition": "人物在画面左侧",
                "location_entity_id": location["entity_id"],
                "subject_positions": [{"entity_id": hero["entity_id"],
                                       "screen_position": "左侧", "pose_and_gaze": "向前看"}],
                "prop_placements": [], "visual_exclusions": ["其他角色"],
            }, formal={"shot_size": "中景", "camera": "侧面平视"})
            self.assertIn("正式景别：中景", rendered)
            self.assertIn("正式机位：侧面平视", rendered)
            self.assertIn("雪山古道", rendered)

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
