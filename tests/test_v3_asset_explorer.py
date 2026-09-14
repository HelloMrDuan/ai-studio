from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AssetExplorerContractTests(unittest.TestCase):
    def test_backend_exposes_formal_candidates_and_v3_resources(self) -> None:
        source = (ROOT / "app" / "v3" / "asset_explorer.py").read_text(encoding="utf-8")
        self.assertIn("list_assets(project_id, active_only=False)", source)
        self.assertIn("_wb_load_candidates", source)
        self.assertIn("self.resources.list_all(project_id)", source)
        self.assertIn("used_by", source)
        self.assertIn("/api/v3/studio/projects/{project_id}/asset-explorer", source)

    def test_original_page_injects_global_asset_explorer(self) -> None:
        overlay = (ROOT / "app" / "v3" / "original_workbench_overlay.py").read_text(encoding="utf-8")
        frontend = (ROOT / "app" / "v3" / "static" / "asset-explorer-overlay.js").read_text(encoding="utf-8")
        self.assertIn("asset-explorer-overlay.js", overlay)
        self.assertIn("作品资产", frontend)
        self.assertIn("跨阶段查看正式资产、生成候选和新版资源", frontend)
        self.assertIn("点击放大查看", frontend)
        self.assertIn("<video controls autoplay", frontend)
        self.assertIn("<pre>", frontend)
        self.assertIn("下游引用", frontend)

    def test_main_mounts_asset_explorer_router(self) -> None:
        entry = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("create_asset_explorer_router", entry)
        self.assertIn("app.include_router(create_asset_explorer_router(settings, legacy_runtime))", entry)


if __name__ == "__main__":
    unittest.main()
