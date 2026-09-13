from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v3.legacy_authoring_retirement import retire_legacy_authoring_jobs


class LegacyAuthoringRetirementTests(unittest.TestCase):
    def test_only_active_multi_turn_authoring_jobs_are_retired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "studio_jobs"
            root.mkdir(parents=True)
            legacy = root / "legacy.json"
            legacy.write_text(json.dumps({
                "job_id": "legacy",
                "project_id": "p1",
                "status": "running",
                "turn_count": 13,
                "message": "正在执行当前创作阶段",
            }, ensure_ascii=False), encoding="utf-8")
            media = root / "media.json"
            media.write_text(json.dumps({
                "job_id": "media",
                "project_id": "p1",
                "status": "running",
                "capability": "image",
            }, ensure_ascii=False), encoding="utf-8")
            done = root / "done.json"
            done.write_text(json.dumps({
                "job_id": "done",
                "project_id": "p1",
                "status": "completed",
                "turn_count": 8,
            }, ensure_ascii=False), encoding="utf-8")

            result = retire_legacy_authoring_jobs(SimpleNamespace(data_dir=td))
            self.assertEqual(result["retired"], 1)
            retired = json.loads(legacy.read_text(encoding="utf-8"))
            self.assertEqual(retired["status"], "failed")
            self.assertEqual(retired["failure_kind"], "legacy_auto_advance_retired")
            self.assertEqual(retired["legacy_turn_count"], 13)
            self.assertIn("不会恢复或继续调用模型", retired["error"])
            self.assertEqual(json.loads(media.read_text(encoding="utf-8"))["status"], "running")
            self.assertEqual(json.loads(done.read_text(encoding="utf-8"))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
