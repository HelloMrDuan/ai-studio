from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap
from app.v3.character_appearances import CharacterAppearanceService
from app.v3.production_authoring_assets import ProductionAuthoringAssetService


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.outputs = {
            "02": """
# 角色生成稿

## 角色资产：沈川
- 稳定身份：17岁男性，深蓝色束袖长袍，左眉旧伤，背旧剑匣。
- 形象版本：默认造型（N001）。
- 角色参考图生成要求：中性身份参考。

## 角色资产：苏瑶
- 稳定身份：16岁女性，红衣，高马尾，青玉坠。
- 形象版本：默认造型（N001）。
- 角色参考图生成要求：中性身份参考。
""",
            "03": "",
        }
        self.project = {
            "project_id": project_id,
            "title": "角色归属修复",
            "status": "active",
            "current_stage": "03",
            "completed_stages": ["01", "02"],
            "confirmed_outputs": {"01": {"handoff": "沈川与苏瑶的故事。", "production_asset_ids": []}},
            "stage_state": {"03": {"stage_ready": False, "skill_runtime": {}}},
        }
        self.production.ensure_project(project_id, "角色归属修复")

    def get_project(self, project_id: str):
        if project_id != self.project_id:
            raise FileNotFoundError(project_id)
        return dict(self.project)

    def list_projects(self):
        return [self.get_project(self.project_id)]

    def _latest_stage_output(self, project, stage: str):
        return self.outputs.get(stage, "")


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director

    def _wb_load_candidates(self, project_id: str):
        return []

    def _wb_sync_candidates(self, project_id: str):
        return []

    async def director_workbench_execute_candidate(self, project_id: str, payload: dict):
        return {"project_id": project_id, "payload": payload}


class CharacterOwnershipRepairTests(unittest.TestCase):
    def test_hidden_character_and_cross_bound_appearance_recover_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "d" * 24
            director = _Director(root, project_id)
            legacy = _Legacy(director)

            suyao = director.production.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="story:character:suyao",
                stage="01",
                metadata={"age": 16},
            )
            shenchuan = director.production.create_entity(
                project_id,
                entity_type="character",
                name="沈川",
                logical_key="story:character:shenchuan",
                stage="02",
                metadata={"authoring": {"source_stage": "02", "materialized_from_stage_output": True}},
            )

            # Reproduce the historical corruption visible in the browser: the
            # real 沈川 row is hidden under a cross-name alias to 苏瑶.
            graph = director.production.get_graph(project_id)
            shen_raw = graph["entities"][shenchuan["entity_id"]]
            shen_raw["entity_type"] = "retired_fragment"
            shen_raw.setdefault("metadata", {})["canonical_entity_id"] = suyao["entity_id"]
            shen_raw["metadata"]["merged_duplicate"] = True
            shen_raw["metadata"]["hidden_from_normal_lists"] = True
            director.production._save(graph)

            # Old appearance text still says 沈川, but the graph ownership points
            # at 苏瑶. The repair must trust the explicit character name, not the
            # broken entity_ids relation.
            wrong_payload = {
                "schema_version": "xiaoduan_character_appearance_v1",
                "appearance_id": "default",
                "character_entity_id": suyao["entity_id"],
                "character_name": "沈川",
                "name": "默认造型",
                "stable_design": "沈川：17岁男性，深蓝色束袖长袍，左眉旧伤。",
                "change_reason": "角色基础造型",
                "effective_story_node_ids": [],
                "inherits_identity": True,
            }
            director.production.create_text_asset(
                project_id,
                stage="02",
                skill="legacy",
                logical_key="legacy:wrong:shenchuan:default",
                asset_role="character_appearance",
                name="沈川 · 默认造型",
                content=json.dumps(wrong_payload, ensure_ascii=False),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=[suyao["entity_id"]],
                metadata={"appearance_id": "default", "character_name": "沈川"},
            )

            settings = SimpleNamespace(data_dir=root)
            state = ProductionAuthoringAssetService(settings, legacy).status(project_id)
            characters = [row for row in state["items"] if row["entity_type"] == "character"]
            self.assertEqual({row["name"] for row in characters}, {"沈川", "苏瑶"})
            self.assertEqual(len(characters), 2)
            self.assertEqual(state["model_calls_added"], 0)

            appearance_state = CharacterAppearanceService(legacy).list(project_id)
            appearances = appearance_state["appearances"]
            self.assertEqual({row["character_name"] for row in appearances}, {"沈川", "苏瑶"})
            self.assertEqual(len(appearances), 2)
            self.assertEqual({row["appearance_id"] for row in appearances}, {"default"})
            owner_by_name = {row["character_name"]: row["character_entity_id"] for row in appearances}
            self.assertNotEqual(owner_by_name["沈川"], owner_by_name["苏瑶"])

            references = CanonicalReferenceAssetBootstrap(legacy).status(project_id)
            character_refs = [row for row in references["items"] if row["entity_type"] == "character"]
            self.assertEqual({row["name"] for row in character_refs}, {"沈川", "苏瑶"})
            self.assertEqual(len(character_refs), 2)


if __name__ == "__main__":
    unittest.main()
