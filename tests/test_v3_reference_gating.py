from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from app.services.production_assets import ProductionAssetService
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.project = {
            "project_id": project_id,
            "current_stage": "01",
            "completed_stages": [],
        }

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return dict(self.project)


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director
        self.rows = []

    def _wb_load_candidates(self, project_id: str):
        return list(self.rows)

    def _wb_sync_candidates(self, project_id: str):
        return list(self.rows)


async def _inert_submit(project, payload):
    raise AssertionError("reference gating test must not submit generation")


def _profile(p: ProductionAssetService, project_id: str, entity: dict, role: str) -> None:
    entity_id = entity["entity_id"]
    p.create_text_asset(
        project_id,
        stage="02" if role == "character_profile" else "03",
        skill="xiaoduan-authoring-assets",
        logical_key=f"studio:authoring:{entity_id}:profile",
        asset_role=role,
        name=f"{entity['name']}稳定设定",
        content="已确认稳定设定",
        asset_type="STRUCTURED_DATA",
        extension=".json",
        source={"type": "canonical_reusable_asset_profile"},
        parent_asset_ids=[],
        entity_ids=[entity_id],
        metadata={"content_idempotent": True},
    )


class ReferenceGatingTests(unittest.TestCase):
    def test_stage01_rough_entities_are_not_reference_assets(self):
        with tempfile.TemporaryDirectory() as raw:
            project_id = "d" * 24
            director = _Director(Path(raw), project_id)
            p = director.production
            p.create_entity(project_id, entity_type="character", name="沈川")
            p.create_entity(project_id, entity_type="location", name="苍梧山顶")
            p.create_entity(project_id, entity_type="location", name="苍梧山顶")
            p.create_entity(project_id, entity_type="prop", name="古剑")
            p.create_entity(project_id, entity_type="prop", name="古剑")

            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=_inert_submit)
            state = service.status(project_id)

            self.assertEqual(state["items"], [])
            self.assertTrue(state["waiting_for_stable_assets"])
            self.assertFalse(state["stage01_story_entities_exposed"])

    def test_distinct_formal_profiles_are_not_collapsed_by_name(self):
        with tempfile.TemporaryDirectory() as raw:
            project_id = "e" * 24
            director = _Director(Path(raw), project_id)
            p = director.production
            char = p.create_entity(project_id, entity_type="character", name="沈川")
            loc1 = p.create_entity(project_id, entity_type="location", name="苍梧山顶", logical_key="location:first")
            loc2 = p.create_entity(project_id, entity_type="location", name="苍梧山顶", logical_key="location:second")
            prop = p.create_entity(project_id, entity_type="prop", name="古剑")
            _profile(p, project_id, char, "character_profile")
            _profile(p, project_id, loc1, "location_profile")
            _profile(p, project_id, loc2, "location_profile")
            _profile(p, project_id, prop, "prop_profile")
            director.project["current_stage"] = "04"
            director.project["completed_stages"] = ["01", "02", "03"]

            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=_inert_submit)
            state = service.status(project_id)

            self.assertEqual(state["required_count"], 4)
            self.assertEqual(
                [(item["entity_type"], item["name"]) for item in state["items"]],
                [("character", "沈川"), ("location", "苍梧山顶"), ("location", "苍梧山顶"), ("prop", "古剑")],
            )
            self.assertTrue(all(item["profile_asset_id"] for item in state["items"]))

    def _five_profiles(self, director):
        for index, (role, name) in enumerate([
            ("character_profile", "沈川"), ("character_profile", "苏瑶"),
            ("location_profile", "苍梧山顶"), ("prop_profile", "古剑"), ("prop_profile", "青玉坠"),
        ]):
            director.production.create_text_asset(
                director.project_id, stage="02" if index < 2 else "03", skill="test",
                logical_key=f"formal:{index}", asset_role=role, name=f"「{name}」稳定设定",
                content=json.dumps({"entity_id": f"owner-{index}", "name": name,
                                    "stable_design": f"{name}的正式外观"}, ensure_ascii=False),
                asset_type="STRUCTURED_DATA", extension=".json",
                # Reproduce cross-bound and missing legacy associations.
                entity_ids=["wrong-shared-owner"] if index < 2 else [],
            )

    def test_five_profiles_without_story_entities_or_stage_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            director = _Director(Path(raw), "f" * 24)
            self._five_profiles(director)
            submit = AsyncMock(return_value={})
            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=submit)
            with patch.object(director.production, "list_entities", side_effect=AssertionError("story entity dependency")):
                state = service.status(director.project_id)
                self.assertEqual(state["reference_candidate_count"], 5)
                self.assertEqual(state["stable_profile_count"], 5)
                self.assertEqual({row["name"] for row in state["items"]}, {"沈川", "苏瑶", "苍梧山顶", "古剑", "青玉坠"})
                self.assertTrue(all(row["name"] + "的正式外观" in row["prompt_text"] for row in state["items"]))
                result = asyncio.run(service.generate_missing(director.project_id))
                self.assertEqual(len(result["submitted_entity_ids"]), 5)
                self.assertEqual(submit.await_count, 5)

    def test_missing_candidate_blocks_all_generation_entrypoints(self):
        with tempfile.TemporaryDirectory() as raw:
            director = _Director(Path(raw), "f" * 24)
            self._five_profiles(director)
            submit = AsyncMock()
            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=submit)
            build = service._build_reference_candidates
            with patch.object(service, "_build_reference_candidates", side_effect=lambda pid, profiles: [
                row for row in build(pid, profiles) if row[0]["entity_id"] != "owner-0"
            ]):
                for operation in [
                    lambda: service.status(director.project_id),
                    lambda: asyncio.run(service.generate_candidate(director.project_id, "owner-1")),
                    lambda: asyncio.run(service.generate_missing(director.project_id)),
                    lambda: asyncio.run(service.generate_first_missing_for_entities(director.project_id, ["owner-1"])),
                ]:
                    with self.assertRaisesRegex(ValueError, "沈川.*stable_profile_count=5, reference_candidate_count=4"):
                        operation()
            submit.assert_not_awaited()

    def test_profile_owner_conflict_is_not_silently_collapsed(self):
        with tempfile.TemporaryDirectory() as raw:
            director = _Director(Path(raw), "f" * 24)
            self._five_profiles(director)
            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=_inert_submit)
            build = service._build_reference_candidates
            def conflicting(pid, profiles):
                rows = build(pid, profiles)
                rows[1][0]["entity_id"] = rows[0][0]["entity_id"]
                return rows
            with patch.object(service, "_build_reference_candidates", side_effect=conflicting):
                with self.assertRaisesRegex(ValueError, "归属 ID 冲突"):
                    service.status(director.project_id)

    def test_equal_counts_cannot_hide_a_missing_profile(self):
        profiles = [{"asset_id": "a", "name": "first"}, {"asset_id": "b", "name": "second"}]
        rows = [({"entity_id": "one"}, profiles[0]), ({"entity_id": "two"}, profiles[0])]
        with self.assertRaisesRegex(ValueError, "second.*stable_profile_count=2, reference_candidate_count=2"):
            CanonicalReferenceAssetBootstrap._validate_reference_candidates(profiles, rows)

    def test_merged_aliases_and_old_versions_produce_one_candidate(self):
        with tempfile.TemporaryDirectory() as raw:
            director = _Director(Path(raw), "f" * 24)
            for name in ["旧名称", "新名称"]:
                director.production.create_text_asset(
                    director.project_id, stage="02", skill="test",
                    logical_key="studio:authoring:owner:profile", asset_role="character_profile",
                    name=f"角色「{name}」稳定设定", content=name,
                    entity_ids=["alias-a", "alias-b"],
                )
            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=_inert_submit)
            state = service.status(director.project_id)
            self.assertEqual(state["reference_candidate_count"], 1)
            self.assertEqual(state["items"][0]["name"], "新名称")
            self.assertEqual(state["items"][0]["entity_id"], "owner")


if __name__ == "__main__":
    unittest.main()
