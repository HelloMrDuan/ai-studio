from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.typed_entity_graph_authority import TypedEntityGraphAuthority


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
            "stage_state": {"01": {"stage_ready": True}},
        }
        self.production.ensure_project(project_id, "青云山")

    def get_project(self, project_id: str):
        assert project_id == self.project_id
        return self.project

    def list_projects(self):
        return [self.project]


def _seed_typed_story(director: _Director, project_id: str) -> None:
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
    # Reproduce the real failed model output: the typed result itself once
    # contained the grammar fragment “手中”. The graph authority must normalize
    # character identity from immutable source before deciding visibility.
    payload = {
        "schema_version": 1,
        "output_kind": "story_bible",
        "document": "青云山故事生产结果。沈川与苏瑶在雪中相遇。",
        "characters": [
            {"name": "手中", "source_evidence": "手中握着一枚玉佩"},
            {"name": "沈川", "source_evidence": "沈川迅速拔剑挡在苏瑶身前"},
            {"name": "苏瑶", "source_evidence": "苏瑶身穿浅青色古式长裙"},
        ],
        "locations": [
            {"name": "青云山", "source_evidence": "青云山迎来入冬后的第一场大雪"},
        ],
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


class TypedEntityGraphAuthorityTests(unittest.TestCase):
    def test_untagged_legacy_hand_fragment_is_retired_from_visible_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "b" * 24
            director = _Director(root, project_id)
            _seed_typed_story(director, project_id)

            # This is what the previous regression missed: the production ghost
            # came from a legacy writer and had no typed/stage ownership metadata.
            ghost = director.production.create_entity(
                project_id,
                entity_type="character",
                logical_key="legacy:control:hand-fragment",
                name="手中",
                stage="",
                skill="",
                metadata={},
            )
            director.production.create_entity(
                project_id,
                entity_type="character",
                logical_key="legacy:character:shenchuan",
                name="沈川",
                stage="",
                skill="",
                metadata={},
            )
            director.production.create_entity(
                project_id,
                entity_type="character",
                logical_key="legacy:character:suyao",
                name="苏瑶",
                stage="",
                skill="",
                metadata={},
            )
            director.production.create_entity(
                project_id,
                entity_type="location",
                logical_key="legacy:location:qingyunshan",
                name="青云山",
                stage="",
                skill="",
                metadata={},
            )
            director.production.create_entity(
                project_id,
                entity_type="prop",
                logical_key="legacy:prop:sword",
                name="古剑",
                stage="",
                skill="",
                metadata={},
            )
            director.production.create_entity(
                project_id,
                entity_type="prop",
                logical_key="legacy:prop:jade",
                name="玉佩",
                stage="",
                skill="",
                metadata={},
            )

            authority = TypedEntityGraphAuthority(SimpleNamespace(data_dir=root), director)
            install = authority.install()
            self.assertEqual(install["policy"], "typed_story_graph_single_authority_v1")

            characters = director.production.list_entities(project_id, "character")
            self.assertEqual({row["name"] for row in characters}, {"沈川", "苏瑶"})

            raw_graph = director.production.get_graph(project_id)
            raw_ghost = raw_graph["entities"][ghost["entity_id"]]
            self.assertEqual(raw_ghost["entity_type"], "retired_fragment")
            self.assertTrue(raw_ghost["metadata"]["hidden_from_normal_lists"])
            self.assertEqual(
                raw_ghost["metadata"]["retired_by_policy"],
                "typed_story_graph_single_authority_v1",
            )

    def test_startup_reconcile_yields_exact_two_one_two_story_elements(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "c" * 24
            director = _Director(root, project_id)
            _seed_typed_story(director, project_id)

            for kind, name in (
                ("character", "手中"),
                ("character", "沈川"),
                ("character", "苏瑶"),
                ("location", "青云山"),
                ("prop", "古剑"),
                ("prop", "玉佩"),
            ):
                director.production.create_entity(
                    project_id,
                    entity_type=kind,
                    logical_key=f"legacy:{kind}:{hashlib.sha256(name.encode()).hexdigest()[:8]}",
                    name=name,
                    stage="",
                    skill="",
                    metadata={},
                )

            authority = TypedEntityGraphAuthority(SimpleNamespace(data_dir=root), director)
            result = authority.install()
            self.assertEqual(result["startup_reconciliation"]["scanned"], 1)
            self.assertEqual(
                {row["name"] for row in director.production.list_entities(project_id, "character")},
                {"沈川", "苏瑶"},
            )
            self.assertEqual(
                {row["name"] for row in director.production.list_entities(project_id, "location")},
                {"青云山"},
            )
            self.assertEqual(
                {row["name"] for row in director.production.list_entities(project_id, "prop")},
                {"古剑", "玉佩"},
            )


if __name__ == "__main__":
    unittest.main()
