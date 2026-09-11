from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3 import front_half_quality_gate as gate
from app.v3.project_source_snapshot import (
    _ensure_snapshot,
    bind_server_owned_evidence,
    install_project_source_snapshot,
    typed_entity_logical_key,
)


SOURCE = """《青云山的第一场雪》
青云山迎来入冬后的第一场大雪。
17岁的少年沈川独自沿着覆雪的山间石阶前行。他身穿深蓝色古式长袍，黑发束起，背负一柄古剑。
走到悬崖附近时，沈川发现少女苏瑶独自站在雪中。苏瑶身穿浅青色古式长裙，手中握着一枚玉佩。
""".strip()


class FakeDirector:
    def __init__(self, root: Path, project: dict) -> None:
        self.production = ProductionAssetService(root)
        self.project = project
        self.seen_sources: list[str] = []

    def get_project(self, project_id: str) -> dict:
        assert project_id == self.project["project_id"]
        return self.project

    async def message(self, project_id: str, user_text: str, *, native_control_action: str = ""):
        packed = [{"role": "user", "content": "=== AUTHORITATIVE CURRENT USER MESSAGE ===\n" + user_text}]
        self.seen_sources.append(gate._authoritative_message_source(packed))
        return {"ok": True}


class ProjectSourceSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_source_is_immutable_across_regenerate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_id = "1" * 24
            project = {
                "project_id": project_id,
                "current_stage": "01",
                "history": [],
            }
            director = FakeDirector(Path(tmp), project)
            director.production.ensure_project(project_id, "test")
            install_project_source_snapshot(SimpleNamespace(data_dir=Path(tmp)), director)

            await director.message(project_id, SOURCE)
            project["history"].append({"role": "user", "stage": "01", "content": SOURCE})
            await director.message(project_id, "重新生成")

            self.assertEqual(director.seen_sources, [SOURCE, SOURCE])
            rows = director.production.list_assets(
                project_id, asset_role="project_source_snapshot", active_only=True
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["version"], 1)
            self.assertTrue(rows[0]["metadata"]["immutable"])
            self.assertEqual(
                director.production.read_text_asset(project_id, rows[0]["asset_id"]),
                SOURCE,
            )

    async def test_legacy_regenerate_migrates_only_user_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_id = "2" * 24
            project = {
                "project_id": project_id,
                "current_stage": "01",
                "history": [
                    {"role": "user", "stage": "01", "content": SOURCE},
                    {"role": "assistant", "stage": "01", "content": "沈川其实有一枚不存在的神秘戒指"},
                ],
            }
            director = FakeDirector(Path(tmp), project)
            director.production.ensure_project(project_id, "legacy")
            install_project_source_snapshot(SimpleNamespace(data_dir=Path(tmp)), director)

            await director.message(project_id, "重新生成")
            self.assertEqual(director.seen_sources[-1], SOURCE)
            self.assertNotIn("神秘戒指", director.seen_sources[-1])
            rows = director.production.list_assets(
                project_id, asset_role="project_source_snapshot", active_only=True
            )
            self.assertEqual(rows[0]["source"]["type"], "legacy_user_history_migration")

    def test_server_overwrites_model_evidence_with_exact_source(self) -> None:
        payload = {
            "output_kind": "story_bible",
            "characters": [
                {"name": "沈川", "source_evidence": "模型自己改写的证据"},
                {"name": "苏瑶", "source_evidence": "另一个改写"},
            ],
            "locations": [{"name": "青云山", "source_evidence": "不是原文"}],
            "props": [
                {"name": "古剑", "source_evidence": "不是原文"},
                {"name": "玉佩", "source_evidence": "不是原文"},
            ],
        }
        bind_server_owned_evidence(payload, SOURCE)
        for group in ("characters", "locations", "props"):
            for row in payload[group]:
                self.assertIn(row["source_evidence"], SOURCE)
                self.assertIn(row["name"], row["source_evidence"])

    def test_chinese_entity_keys_do_not_collapse_to_ascii_slug(self) -> None:
        a = typed_entity_logical_key("character", "沈川")
        b = typed_entity_logical_key("character", "苏瑶")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("typed:character:"))
        self.assertTrue(b.startswith("typed:character:"))


if __name__ == "__main__":
    unittest.main()
