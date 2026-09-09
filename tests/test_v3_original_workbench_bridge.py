from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OriginalWorkbenchBridgeContractTests(unittest.TestCase):
    def test_original_workbench_remains_source_page(self) -> None:
        source = (ROOT / "app" / "v3" / "original_workbench_overlay.py").read_text(encoding="utf-8")
        original = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('Path(__file__).resolve().parents[1] / "static" / "index.html"', source)
        self.assertIn('id="app-shortvideo"', original)
        self.assertIn('id="view-make"', original)
        self.assertIn('id="view-final"', original)
        self.assertIn("authoring-continuity-overlay.js", source)
        self.assertIn("workbench-status-localization.js", source)

    def test_shot_generation_is_bridged_to_v3_but_manual_adoption_remains(self) -> None:
        source = (ROOT / "app" / "v3" / "legacy_candidate_bridge.py").read_text(encoding="utf-8")
        self.assertIn('operation = "generation.image.generate_candidate"', source)
        self.assertIn('operation = "generation.h3.generate_candidate"', source)
        self.assertIn('"manual_adoption_required": True', source)
        self.assertIn('self.resources.set_audit_result', source)
        self.assertIn('self.resources.adopt', source)
        self.assertIn('self.legacy._studio_publish_confirmed_shot_candidate = self.publish_confirmed_shot_candidate', source)

    def test_front_half_reference_panel_is_editable_and_requires_adoption(self) -> None:
        frontend = (ROOT / "app" / "v3" / "static" / "authoring-continuity-overlay.js").read_text(encoding="utf-8")
        backend = (ROOT / "app" / "v3" / "reference_assets.py").read_text(encoding="utf-8")
        self.assertIn("一致性参考资产", frontend)
        self.assertIn("自动生成缺失参考图", frontend)
        self.assertIn("生成要求（可修改后重新生成）", frontend)
        self.assertIn("采用候选", frontend)
        self.assertIn("丢弃", frontend)
        self.assertIn("upload_required\": False", backend)
        self.assertIn("manual_adoption_required\": True", backend)
        self.assertIn('"aspect_ratio": "4:3"', backend)

    def test_completed_authoring_stages_can_be_reopened_without_deleting_history(self) -> None:
        frontend = (ROOT / "app" / "v3" / "static" / "authoring-continuity-overlay.js").read_text(encoding="utf-8")
        backend = (ROOT / "app" / "v3" / "stage_revision.py").read_text(encoding="utf-8")
        self.assertIn("修改${name}", frontend)
        self.assertIn("old_asset_versions_preserved", backend)
        self.assertIn('item["dependency_state"] = "stale"', backend)
        self.assertIn("上游创作阶段已重新打开修改", backend)
        self.assertNotIn("unlink(", backend)

    def test_dynamic_candidate_status_is_localized(self) -> None:
        source = (ROOT / "app" / "v3" / "static" / "workbench-status-localization.js").read_text(encoding="utf-8")
        self.assertIn("completed: '已完成 · 待采用'", source)
        self.assertIn("running: '生成中'", source)
        self.assertIn("failed: '失败'", source)

    def test_postproduction_has_editable_voice_subtitle_bgm_and_composition(self) -> None:
        backend = (ROOT / "app" / "v3" / "legacy_postproduction.py").read_text(encoding="utf-8")
        frontend = (ROOT / "app" / "v3" / "static" / "original-workbench-overlay.js").read_text(encoding="utf-8")
        for route in (
            "/postproduction/tts",
            "/postproduction/subtitle/generate",
            "/postproduction/subtitle",
            "/postproduction/bgm",
            "/postproduction/compose",
        ):
            self.assertIn(route, backend)
        for text in (
            "生成 / 重新生成配音",
            "保存手工修改字幕",
            "上传或替换背景音乐",
            "生成最终成片",
        ):
            self.assertIn(text, frontend)

    def test_original_root_not_replaced_by_v3_dashboard(self) -> None:
        entry = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("load_original_workbench_runtime", entry)
        self.assertIn("front_half_skill_overlay.install()", entry)
        self.assertIn("legacy_v3_bridge.install()", entry)
        self.assertIn("original_workbench_router", entry)
        self.assertNotIn("from app.v3.main import app\n", entry)


if __name__ == "__main__":
    unittest.main()
