from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.services.story_continuity import StoryContinuityService
from app.v3.continuity_entity_authority import (
    install_continuity_entity_authority,
    sanitize_continuity_chunk,
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
            "stage_state": {"01": {"stage_ready": True}},
        }
        self.production.ensure_project(project_id, "青云山")

    def get_project(self, project_id: str):
        assert project_id == self.project_id
        return self.project


def _seed(director: _Director, project_id: str) -> None:
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
        "document": "沈川与苏瑶在青云山雪中相遇，古剑与玉佩产生呼应。",
        "characters": [
            {"name": "沈川", "source_evidence": "17岁的少年沈川独自沿着覆雪的山间石阶前行"},
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
    for kind, name in (
        ("character", "沈川"),
        ("character", "苏瑶"),
        ("location", "青云山"),
        ("prop", "古剑"),
        ("prop", "玉佩"),
    ):
        director.production.create_entity(
            project_id,
            entity_type=kind,
            logical_key=f"typed:{kind}:{hashlib.sha1(name.encode()).hexdigest()[:10]}",
            name=name,
            stage="01",
            skill="xiaoduan-story-bible",
            metadata={"typed_professional_entity": True},
        )


def _bad_continuity_chunk() -> dict:
    return {
        "episode": {"title": "第一场雪", "summary": ""},
        "scenes": [{
            "title": "雪中相遇",
            "summary": "沈川发现苏瑶",
            "source_excerpt": "苏瑶身穿浅青色古式长裙，手中握着一枚玉佩。",
            "location": {"name": "青云山", "aliases": [], "core_profile": {}},
            "characters": [
                {"name": "沈川", "aliases": [], "core_profile": {}, "state_patch": {}, "performance": {}},
                {"name": "手中", "aliases": [], "core_profile": {}, "state_patch": {}, "performance": {}},
                {"name": "苏瑶", "aliases": [], "core_profile": {}, "state_patch": {}, "performance": {}},
            ],
            "props": [
                {"name": "古剑", "aliases": [], "core_profile": {}, "state_patch": {}, "holder_name": "沈川"},
                {"name": "玉佩", "aliases": [], "core_profile": {}, "state_patch": {}, "holder_name": "苏瑶"},
            ],
            "beats": [],
        }],
        "carry_forward": {"episode_title": "第一场雪", "scene_title": "雪中相遇"},
    }


class ContinuityEntityAuthorityTests(unittest.TestCase):
    def test_sanitizer_drops_hand_before_continuity_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "d" * 24
            director = _Director(root, project_id)
            _seed(director, project_id)
            service = StoryContinuityService(SimpleNamespace(data_dir=root), director)

            sanitized, audit = sanitize_continuity_chunk(
                service, project_id, _bad_continuity_chunk()
            )
            names = [x["name"] for x in sanitized["scenes"][0]["characters"]]
            self.assertEqual(names, ["沈川", "苏瑶"])
            self.assertIn(
                {"entity_type": "character", "name": "手中"},
                audit["dropped"],
            )

    def test_installed_guard_keeps_graph_at_two_characters_after_analysis_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_id = "e" * 24
            director = _Director(root, project_id)
            _seed(director, project_id)
            service = StoryContinuityService(SimpleNamespace(data_dir=root), director)
            state = service._empty(project_id)

            original = StoryContinuityService._merge_chunk
            old_flag = getattr(
                StoryContinuityService,
                "_xiaoduan_typed_entity_authority_installed",
                False,
            )
            if old_flag:
                # The full suite may import app.main before this test. In that
                # case the production guard is already active and should be used.
                restore = False
            else:
                install_continuity_entity_authority()
                restore = True
            try:
                service._merge_chunk(
                    project_id,
                    state,
                    _bad_continuity_chunk(),
                    chunk_start=0,
                    chunk_end=len(_SOURCE),
                    chunk_index=0,
                )
                characters = director.production.list_entities(project_id, "character")
                self.assertEqual({x["name"] for x in characters}, {"沈川", "苏瑶"})
                self.assertEqual(len(state["scenes"]), 1)
                self.assertEqual(len(state["scenes"][0]["character_entity_ids"]), 2)
                dropped = (
                    state.get("analysis", {})
                    .get("typed_entity_authority", {})
                    .get("dropped_noncanonical", [])
                )
                self.assertIn(
                    {"entity_type": "character", "name": "手中"},
                    dropped,
                )
            finally:
                if restore:
                    StoryContinuityService._merge_chunk = original
                    try:
                        delattr(
                            StoryContinuityService,
                            "_xiaoduan_typed_entity_authority_installed",
                        )
                    except AttributeError:
                        pass


if __name__ == "__main__":
    unittest.main()
