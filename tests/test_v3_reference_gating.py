from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def test_only_profiled_assets_are_exposed_and_duplicate_names_collapse(self):
        with tempfile.TemporaryDirectory() as raw:
            project_id = "e" * 24
            director = _Director(Path(raw), project_id)
            p = director.production
            char = p.create_entity(project_id, entity_type="character", name="沈川")
            loc1 = p.create_entity(project_id, entity_type="location", name="苍梧山顶")
            loc2 = p.create_entity(project_id, entity_type="location", name="苍梧山顶")
            prop = p.create_entity(project_id, entity_type="prop", name="古剑")
            _profile(p, project_id, char, "character_profile")
            _profile(p, project_id, loc1, "location_profile")
            _profile(p, project_id, loc2, "location_profile")
            _profile(p, project_id, prop, "prop_profile")
            director.project["current_stage"] = "04"
            director.project["completed_stages"] = ["01", "02", "03"]

            service = CanonicalReferenceAssetBootstrap(_Legacy(director), submit_candidate=_inert_submit)
            state = service.status(project_id)

            self.assertEqual(state["required_count"], 3)
            self.assertEqual(
                [(item["entity_type"], item["name"]) for item in state["items"]],
                [("character", "沈川"), ("location", "苍梧山顶"), ("prop", "古剑")],
            )
            self.assertTrue(all(item["profile_asset_id"] for item in state["items"]))


if __name__ == "__main__":
    unittest.main()
