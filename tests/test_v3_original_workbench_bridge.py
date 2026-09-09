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

    def test_shot_generation_is_bridged_to_v3_but_manual_adoption_remains(self) -> None:
        source = (ROOT / "app" / "v3" / "legacy_candidate_bridge.py").read_text(encoding="utf-8")
        self.assertIn('operation = "generation.image.generate_candidate"', source)
        self.assertIn('operation = "generation.h3.generate_candidate"', source)
        self.assertIn('"manual_adoption_required": True', source)
        self.assertIn('self.resources.set_audit_result', source)
        self.assertIn('self.resources.adopt', source)
        self.assertIn('self.legacy._studio_publish_confirmed_shot_candidate = self.publish_confirmed_shot_candidate', source)

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
        self.assertIn("legacy_v3_bridge.install()", entry)
        self.assertIn("original_workbench_router", entry)
        self.assertNotIn("from app.v3.main import app\n", entry)


if __name__ == "__main__":
    unittest.main()
