from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.services.story_continuity import StoryContinuityService
from app.v3.continuity_entity_authority import repair_continuity_entity_aliases


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.project = {"project_id": project_id, "title": "alias repair"}
        self.production.ensure_project(project_id, "alias repair")

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return self.project


class ContinuityAliasRepairTests(unittest.TestCase):
    def test_historical_duplicate_ids_are_rewritten_to_canonical_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "a" * 24
            director = _Director(root, project_id)
            service = StoryContinuityService(SimpleNamespace(data_dir=root), director)

            canonical_id = "ent_" + "1" * 20
            duplicate_id = "ent_" + "2" * 20
            graph = director.production.ensure_project(project_id)
            graph["entities"][canonical_id] = {
                "entity_id": canonical_id,
                "entity_type": "prop",
                "name": "古剑",
                "metadata": {},
            }
            graph["entities"][duplicate_id] = {
                "entity_id": duplicate_id,
                "entity_type": "weapon",
                "name": "古剑",
                "metadata": {
                    "canonical_entity_id": canonical_id,
                    "merged_duplicate": True,
                    "hidden_from_normal_lists": True,
                },
            }
            graph["entity_aliases"] = {duplicate_id: canonical_id}
            director.production._save(graph)

            state = service._empty(project_id)
            state["scenes"] = [{
                "scene_id": "scn_test",
                "title": "青云山初雪",
                "location_entity_id": "",
                "character_entity_ids": [],
                "prop_entity_ids": [duplicate_id],
            }]
            state["events"] = [{
                "event_id": "evt_test",
                "entity_id": duplicate_id,
                "patch": {"holder_entity_id": duplicate_id},
            }]

            repaired, audit = repair_continuity_entity_aliases(
                service,
                project_id,
                state,
            )

            serialized = json.dumps(repaired, ensure_ascii=False)
            self.assertTrue(audit["repaired"])
            self.assertEqual(audit["aliases"][duplicate_id], canonical_id)
            self.assertNotIn(duplicate_id, serialized)
            self.assertIn(canonical_id, serialized)
            self.assertEqual(
                repaired["scenes"][0]["prop_entity_ids"],
                [canonical_id],
            )
            self.assertEqual(
                repaired["events"][0]["patch"]["holder_entity_id"],
                canonical_id,
            )
            self.assertGreaterEqual(audit["replacement_count"], 3)

    def test_alias_chain_collapses_to_live_canonical_entity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "b" * 24
            director = _Director(root, project_id)
            service = StoryContinuityService(SimpleNamespace(data_dir=root), director)

            canonical_id = "ent_" + "3" * 20
            middle_id = "ent_" + "4" * 20
            old_id = "ent_" + "5" * 20
            graph = director.production.ensure_project(project_id)
            graph["entities"][canonical_id] = {
                "entity_id": canonical_id,
                "entity_type": "location",
                "name": "青云山",
                "metadata": {},
            }
            graph["entities"][middle_id] = {
                "entity_id": middle_id,
                "entity_type": "location",
                "name": "青云山",
                "metadata": {"canonical_entity_id": canonical_id, "merged_duplicate": True},
            }
            graph["entities"][old_id] = {
                "entity_id": old_id,
                "entity_type": "location",
                "name": "青云山",
                "metadata": {"canonical_entity_id": middle_id, "merged_duplicate": True},
            }
            graph["entity_aliases"] = {
                old_id: middle_id,
                middle_id: canonical_id,
            }
            director.production._save(graph)

            state = service._empty(project_id)
            state["scenes"] = [{
                "scene_id": "scn_test",
                "title": "青云山初雪",
                "location_entity_id": old_id,
                "character_entity_ids": [],
                "prop_entity_ids": [],
            }]

            repaired, audit = repair_continuity_entity_aliases(
                service,
                project_id,
                state,
            )
            self.assertTrue(audit["repaired"])
            self.assertEqual(audit["aliases"][old_id], canonical_id)
            self.assertEqual(repaired["scenes"][0]["location_entity_id"], canonical_id)


if __name__ == "__main__":
    unittest.main()
