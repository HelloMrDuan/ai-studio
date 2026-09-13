from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.character_appearances import CharacterAppearanceService


class _Director:
    def __init__(self, production, project_id):
        self.production = production
        self.project_id = project_id

    def get_project(self, project_id):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return {"project_id": project_id}


class _Legacy:
    def __init__(self, production, project_id):
        self.director = _Director(production, project_id)


class CharacterAppearanceTests(unittest.TestCase):
    def test_default_and_story_variant_are_versioned_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project_id = "0123456789abcdef01234567"
            production = ProductionAssetService(Path(temp))
            production.ensure_project(project_id, "测试")
            entity = production.create_entity(
                project_id,
                entity_type="character",
                name="少年",
                logical_key="character:hero",
                stage="02",
                skill="test",
            )
            profile = production.create_text_asset(
                project_id,
                stage="02",
                skill="test",
                logical_key="studio:profile:hero",
                asset_role="character_profile",
                name="少年稳定设定",
                content=json.dumps({"stable_design": "黑发少年，深蓝冬装，黑色长靴，固定银色护腕"}, ensure_ascii=False),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=[entity["entity_id"]],
            )
            service = CharacterAppearanceService(_Legacy(production, project_id))
            default = service.ensure_default(project_id, entity["entity_id"])
            self.assertIsNotNone(default)
            self.assertEqual(default["asset_role"], "character_appearance")
            result = service.save(
                project_id,
                entity["entity_id"],
                appearance_id="snow_battle",
                name="雪山战斗造型",
                stable_design="保持同一张脸和发型，深蓝冬装外加破损披风，银色护腕保留",
                change_reason="进入雪山决战后披风破损",
                effective_story_node_ids=["beat-9"],
            )
            self.assertEqual(result["appearance_id"], "snow_battle")
            rows = service.list(project_id)["appearances"]
            ids = {row["appearance_id"] for row in rows}
            self.assertEqual(ids, {"default", "snow_battle"})
            snow = next(row for row in rows if row["appearance_id"] == "snow_battle")
            self.assertEqual(snow["effective_story_node_ids"], ["beat-9"])
            self.assertIn("披风破损", snow["change_reason"])
            self.assertEqual(default["parent_asset_ids"], [profile["asset_id"]])


if __name__ == "__main__":
    unittest.main()
