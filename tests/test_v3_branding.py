from __future__ import annotations

import unittest
from pathlib import Path


class XiaoduanV3BrandingTests(unittest.TestCase):
    def test_v3_runtime_and_ui_do_not_use_legacy_brand(self) -> None:
        root = Path("app/v3")
        files = [*root.rglob("*.py"), *root.rglob("*.html")]
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("船长AI视界", text, msg=str(path))
            self.assertNotIn("chuanzhang-ai-shijie-workflow", text, msg=str(path))

    def test_v3_ui_uses_xiaoduan_brand(self) -> None:
        text = Path("app/v3/static/index.html").read_text(encoding="utf-8")
        self.assertIn("xiaoduan映画", text)
        self.assertIn("Xiaoduan Studio V3", text)


if __name__ == "__main__":
    unittest.main()
