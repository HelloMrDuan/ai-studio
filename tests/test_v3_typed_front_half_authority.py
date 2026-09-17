from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.typed_front_half_authority import (
    infer_canonical_characters,
    normalize_story_bible_characters,
    reconcile_typed_stage01_entities,
)


_SOURCE = """《青云山的第一场雪》

青云山迎来入冬后的第一场大雪。

17岁的少年沈川独自沿着覆雪的山间石阶前行。他身穿深蓝色古式长袍，黑发束起，背负一柄古剑。古剑使用乌木剑鞘，剑鞘上有暗银色纹路。

走到悬崖附近时，沈川发现少女苏瑶独自站在雪中。苏瑶身穿浅青色古式长裙，手中握着一枚玉佩。

突然，上方岩壁松动，一块巨石向苏瑶坠落。

沈川迅速拔剑挡在苏瑶身前。

古剑出鞘的一瞬间，剑身上的暗银纹路开始发光。

与此同时，苏瑶手中的玉佩也亮起了完全相同的纹路。

两人同时愣住。

风雪越来越大。""".strip()


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.project = {
            "project_id": project_id,
            "title": "青云山",
            "status": "active",
            "current_stage": "01",
            "completed_stages": [],
            "stage_state": {
                "01": {
                    "stage_ready": True,
                    "skill_runtime": {"completion": {"ready": True}},
                }
            },
        }
        self.production.ensure_project(project_id, "青云山")

    def get_project(self, project_id: str):
        assert project_id == self.project_id
        return self.project


class TypedFrontHalfAuthorityTests(unittest.TestCase):
    def test_exact_source_character_scope_excludes_sentence_fragment(self) -> None:
        names = infer_canonical_characters(_SOURCE)
        self.assertEqual(names, ["沈川", "苏瑶"])
        self.assertNotIn("手中", names)
        self.assertNotIn("身前", names)

    def test_story_bible_normalization_removes_false_character_and_restores_real_names(self) -> None:
        payload = {
            "schema_version": 1,
            "output_kind": "story_bible",
            "document": "简洁故事生产结果。",
            "characters": [
                {"name": "手中", "source_evidence": "手中握着一枚玉佩"},
                {"name": "沈川", "source_evidence": "沈川迅速拔剑"},
            ],
            "locations": [{"name": "青云山", "source_evidence": "青云山迎来入冬后的第一场大雪"}],
            "props": [
                {"name": "古剑", "source_evidence": "背负一柄古剑"},
                {"name": "玉佩", "source_evidence": "手中握着一枚玉佩"},
            ],
            "assumptions": [],
            "warnings": [],
        }
        normalized, meta = normalize_story_bible_characters(payload, _SOURCE)
        self.assertEqual([row["name"] for row in normalized["characters"]], ["沈川", "苏瑶"])
        self.assertEqual(meta["removed"], ["手中"])
        self.assertEqual(meta["added"], ["苏瑶"])
        for row in normalized["characters"]:
            self.assertIn(row["source_evidence"], _SOURCE)

    def test_ready_state_reconcile_retires_existing_false_character_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "a" * 24
            director = _Director(root, project_id)
            settings = SimpleNamespace(data_dir=root)
            source_sha = hashlib.sha256(_SOURCE.encode("utf-8")).hexdigest()

            source_asset = director.production.create_text_asset(
                project_id,
                stage="source",
                skill="project-source-ingest",
                logical_key="project:source:original",
                asset_role="project_source_snapshot",
                name="原始创作源文本",
                content=_SOURCE,
                extension=".txt",
                source={"type": "test"},
                metadata={
                    "immutable": True,
                    "source_id": f"src_{source_sha[:20]}",
                    "source_version": 1,
                    "source_sha256": source_sha,
                },
            )

            payload = {
                "schema_version": 1,
                "output_kind": "story_bible",
                "document": "青云山故事生产结果。沈川与苏瑶在雪中相遇。",
                "characters": [
                    {"name": "手中", "source_evidence": "手中握着一枚玉佩"},
                    {"name": "沈川", "source_evidence": "沈川迅速拔剑挡在苏瑶身前"},
                    {"name": "苏瑶", "source_evidence": "苏瑶身穿浅青色古式长裙"},
                ],
                "locations": [{"name": "青云山", "source_evidence": "青云山迎来入冬后的第一场大雪"}],
                "props": [
                    {"name": "古剑", "source_evidence": "背负一柄古剑"},
                    {"name": "玉佩", "source_evidence": "手中握着一枚玉佩"},
                ],
                "assumptions": [],
                "warnings": [],
            }
            director.production.create_text_asset(
                project_id,
                stage="01",
                skill="xiaoduan-story-bible",
                logical_key="studio:professional-output:story_bible",
                asset_role="professional_story_bible",
                name="故事生产圣经 · 专业结构化结果",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                source={"type": "test"},
                parent_asset_ids=[source_asset["asset_id"]],
                metadata={"source_of_truth": True, "output_kind": "story_bible"},
            )
            director.production.create_entity(
                project_id,
                entity_type="character",
                logical_key="typed:character:false-hand",
                name="手中",
                stage="01",
                skill="xiaoduan-story-bible",
                metadata={"typed_professional_entity": True},
            )

            result = reconcile_typed_stage01_entities(
                settings, director, project_id, require_ready=True,
            )
            self.assertTrue(result["reconciled"])
            self.assertEqual(set(result["character_names"]), {"沈川", "苏瑶"})
            self.assertEqual(result["model_calls"], 0)
            all_entities = director.production.list_entities(project_id)
            retired = [row for row in all_entities if row.get("name") == "手中"]
            self.assertEqual(len(retired), 1)
            self.assertEqual(retired[0]["entity_type"], "retired_fragment")


if __name__ == "__main__":
    unittest.main()
