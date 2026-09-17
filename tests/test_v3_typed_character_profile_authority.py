from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.authoring_assets import AuthoringAssetService
from app.v3.character_identity_contract import (
    build_costume_reference_prompt,
    build_face_anchor_prompt,
)
from app.v3.typed_character_profile_authority import reconcile_typed_character_profiles


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.production.ensure_project(project_id, "青云山")

    def get_project(self, project_id: str):
        assert project_id == self.project_id
        return {
            "project_id": project_id,
            "current_stage": "03",
            "completed_stages": ["01", "02"],
        }


class TypedCharacterProfileAuthorityTests(unittest.TestCase):
    def test_generic_stable_description_does_not_drop_typed_hair_and_costume_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "f" * 24
            director = _Director(root, project_id)
            entity = director.production.create_entity(
                project_id,
                entity_type="character",
                name="沈川",
                logical_key="typed:character:shenchuan",
                stage="02",
                skill="xiaoduan-character-assets",
                metadata={
                    "authoring": {
                        # This intentionally reproduces the production weakness:
                        # the visible stable_description is generic even though
                        # the typed CharacterAsset contains much richer facts.
                        "stable_design": "17岁少年，古风武侠世界中的初遇角色。",
                    },
                    "continuity": {
                        "core_profile": {
                            "阶段正式设定": "17岁少年，古风武侠世界中的初遇角色。",
                        },
                        "default_state": {},
                    },
                },
            )

            payload = {
                "schema_version": 1,
                "output_kind": "character_assets",
                "document": "## 角色资产：沈川\n沈川为17岁少年，黑色长发束起，身穿深蓝色古式长袍与黑色布靴。",
                "characters": [{
                    "name": "沈川",
                    "stable_description": "17岁少年，古风武侠世界中的初遇角色。",
                    "gender_presentation": "男性",
                    "age": "17岁",
                    "face": "东亚少年面孔，轮廓清秀，五官自然",
                    "hair_style": "黑色长发束起",
                    "hair_color": "黑色",
                    "skin_tone": "自然偏白肤色",
                    "body_type": "少年清瘦体型",
                    "height_impression": "同龄人中等身高感",
                    "clothing": "深蓝色古式长袍",
                    "footwear": "黑色布靴",
                    "fixed_identity_anchors": ["17岁少年", "黑色长发束起", "东亚少年面孔"],
                    "allowed_variations": ["表情可随剧情变化"],
                    "appearances": [{
                        "appearance_id": "default",
                        "name": "默认造型",
                        "stable_design": "深蓝色古式长袍，黑色布靴",
                        "change_reason": "角色基础造型",
                        "effective_story_node_ids": [],
                    }],
                    "reference_requirements": "锁脸阶段保持17岁、东亚面孔、黑色长发束起；服装阶段保持古式长袍。",
                    "source_evidence": "17岁的少年沈川独自沿着覆雪的山间石阶前行。",
                    "design_basis": "依据原文已确认年龄、发型、服装和古风世界观进行稳定资产设计。",
                }],
                "assumptions": [],
                "warnings": [],
            }
            director.production.create_text_asset(
                project_id,
                stage="02",
                skill="xiaoduan-character-assets",
                logical_key="studio:professional-output:character_assets",
                asset_role="professional_character_assets",
                name="角色资产 · 专业结构化结果",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=[entity["entity_id"]],
                metadata={"source_of_truth": True, "output_kind": "character_assets"},
            )

            result = reconcile_typed_character_profiles(director.production, project_id)
            self.assertTrue(result["reconciled"])
            self.assertEqual(result["updated_count"], 1)

            updated = next(
                row
                for row in director.production.list_entities(project_id, "character")
                if row["entity_id"] == entity["entity_id"]
            )
            contract = updated["metadata"]["continuity"]["core_profile"]["专业角色合同"]
            self.assertEqual(contract["年龄"], "17岁")
            self.assertEqual(contract["发型"], "黑色长发束起")
            self.assertEqual(contract["服装"], "深蓝色古式长袍")
            self.assertEqual(contract["鞋履"], "黑色布靴")

            face_prompt = build_face_anchor_prompt(updated)
            self.assertIn("17岁", face_prompt)
            self.assertIn("黑色长发束起", face_prompt)
            self.assertIn("东亚少年面孔", face_prompt)

            costume_prompt = build_costume_reference_prompt(updated)
            self.assertIn("深蓝色古式长袍", costume_prompt)
            self.assertIn("黑色布靴", costume_prompt)

            legacy = SimpleNamespace(director=director)
            authoring = AuthoringAssetService(SimpleNamespace(data_dir=root), legacy)
            profile = authoring._profile_payload(updated)
            self.assertEqual(
                profile["stable_profile"]["专业角色合同"]["发型"],
                "黑色长发束起",
            )
            self.assertEqual(
                profile["stable_profile"]["专业角色合同"]["服装"],
                "深蓝色古式长袍",
            )


if __name__ == "__main__":
    unittest.main()
