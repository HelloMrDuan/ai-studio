from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.reference_assets import ReferenceAssetBootstrap


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
        self.rows = []

    def _wb_load_candidates(self, project_id: str):
        return list(self.rows)

    def _wb_sync_candidates(self, project_id: str):
        return list(self.rows)


async def _inert_submit(project, payload):
    raise AssertionError("status-only test must not submit generation")


class ReferenceAssetWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_only_exposes_reusable_asset_types_and_upload_is_optional(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director = _Director(root, project_id)
            p = director.production
            p.create_entity(
                project_id,
                entity_type="character",
                name="少年",
                metadata={"appearance": "黑发，深蓝冬装，黑色长靴"},
            )
            p.create_entity(
                project_id,
                entity_type="location",
                name="雪山古道",
                metadata={"structure": "狭窄山道，两侧积雪岩壁"},
            )
            p.create_entity(
                project_id,
                entity_type="prop",
                name="失落古剑",
                metadata={"material": "深色旧钢，剑格有磨损"},
            )
            p.create_entity(project_id, entity_type="shot", name="镜头001")

            state = ReferenceAssetBootstrap(_Legacy(director), submit_candidate=_inert_submit).status(project_id)

            self.assertFalse(state["upload_required"])
            self.assertTrue(state["manual_adoption_required"])
            self.assertEqual({item["entity_type"] for item in state["items"]}, {"character", "location", "prop"})
            character = next(item for item in state["items"] if item["entity_type"] == "character")
            self.assertIn("4:3横向角色身份参考图", character["prompt_text"])
            self.assertIn("左侧是同一角色清晰面部近景", character["prompt_text"])
            self.assertIn("右侧是同一角色无遮挡全身", character["prompt_text"])
            self.assertIn("不表现本镜头动作", character["prompt_text"])

    async def test_user_can_edit_reference_prompt_before_regeneration(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director = _Director(root, project_id)
            entity = director.production.create_entity(
                project_id,
                entity_type="character",
                name="林彻",
                metadata={"appearance": "短黑发，白色西装，黑色皮鞋"},
            )
            captured = {}

            async def submit(project, payload):
                captured["project"] = project
                captured["payload"] = payload
                return {"candidate": {"candidate_id": "candidate-1", "status": "queued"}}

            service = ReferenceAssetBootstrap(_Legacy(director), submit_candidate=submit)
            override = "4:3横向角色参考图。保持短黑发、白色西装和黑色皮鞋；左脸部近景，右全身，中性站姿。"
            result = await service.generate_candidate(
                project_id,
                entity["entity_id"],
                force=True,
                prompt_override=override,
            )

            self.assertTrue(result["submitted"])
            self.assertEqual(captured["project"], project_id)
            self.assertEqual(captured["payload"]["params"]["aspect_ratio"], "4:3")
            prompt_asset_id = captured["payload"]["prompt_asset_id"]
            self.assertEqual(director.production.read_text_asset(project_id, prompt_asset_id), override)
            target = director.production.get_asset(project_id, captured["payload"]["target_asset_id"])
            self.assertEqual(target["asset_role"], "character_reference")
            self.assertTrue(target["metadata"]["manual_adoption_required"])

    async def test_generate_missing_submits_candidates_but_never_auto_adopts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director = _Director(root, project_id)
            for kind, name in (("character", "少年"), ("location", "雪山")):
                director.production.create_entity(project_id, entity_type=kind, name=name, metadata={"description": name})
            calls = []

            async def submit(project, payload):
                calls.append(payload)
                return {"candidate": {"candidate_id": f"cand-{len(calls)}", "status": "queued"}}

            service = ReferenceAssetBootstrap(_Legacy(director), submit_candidate=submit)
            result = await service.generate_missing(project_id)

            self.assertEqual(len(result["submitted_entity_ids"]), 2)
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(call["params"]["semantic_compile"] == "auto" for call in calls))
            state = service.status(project_id)
            self.assertEqual(state["ready_count"], 0)


if __name__ == "__main__":
    unittest.main()
