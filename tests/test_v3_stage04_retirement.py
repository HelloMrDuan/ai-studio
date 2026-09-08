from __future__ import annotations

import pathlib
import unittest


class Stage04RetirementTests(unittest.TestCase):
    def test_legacy_stage04_runtime_is_physically_removed(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        self.assertFalse((root / "app" / "stage04_v238_runtime.py").exists())

    def test_default_asgi_entrypoint_is_v3_only(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        text = (root / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("from app.v3.main import app", text)
        self.assertNotIn("stage04_v238_runtime", text)
        self.assertNotIn("DirectorService", text)
        self.assertNotIn("StageProgress", text)

    def test_default_start_and_check_use_v3_health(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        start = (root / "scripts" / "start.sh").read_text(encoding="utf-8")
        check = (root / "scripts" / "check.sh").read_text(encoding="utf-8")
        for text in (start, check):
            self.assertIn("/api/v3/health", text)
            self.assertNotIn("/api/gpu/status", text)
        self.assertIn("xiaoduan-studio-v3.log", start)

    def test_v3_source_has_no_legacy_stage04_dependency(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "v3"
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "stage04_v238_runtime" in text or "chuanzhang-ai-shijie-workflow" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
