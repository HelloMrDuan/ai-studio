from __future__ import annotations

import importlib.util
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "v23963-current-runtime.tar.gz"
INSTALLER = (
    ROOT
    / "deliverables"
    / "install_ai_studio_v2_39_6_3_stage_progress_v2_confirm_guard.py"
)


def load_installer():
    spec = importlib.util.spec_from_file_location("stage_progress_v2_installer", INSTALLER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load StageProgress v2 installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StageProgressV2ConfirmGuardInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_installer()
        cls.baseline: dict[str, bytes] = {}
        with tarfile.open(ARCHIVE, "r:gz") as archive:
            for rel in cls.installer.BASELINE_SHA_MANIFEST:
                member = archive.getmember(rel)
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"archive member is not a file: {rel}")
                cls.baseline[rel] = stream.read()
        cls.target = cls.installer.build_target(
            "app/main.py", cls.baseline["app/main.py"],
        )

    def test_real_autodl_baseline_manifest_matches_archive(self) -> None:
        for rel, expected in self.installer.BASELINE_SHA_MANIFEST.items():
            self.assertEqual(self.installer.sha(self.baseline[rel]), expected)

    def test_only_main_is_written_and_other_runtime_bytes_remain_unchanged(self) -> None:
        self.assertEqual(self.installer.WRITE_FILES, ("app/main.py",))
        self.assertEqual(
            self.installer.sha(self.baseline["app/services/gemma.py"]),
            "f84fe348213f88d82da87207cb473c05ce6133bdc5e30bbb21d2a98a2d9088d4",
        )
        self.assertEqual(
            self.installer.sha(self.baseline["app/stage04_v238_runtime.py"]),
            "e668321b8eccf9f8adaf02452ffd5c9a0c1f0b890db4ca53ff28bd718fbdf332",
        )

    def test_target_sha_and_generation_worker_stage04_finalize_are_unchanged(self) -> None:
        self.assertEqual(
            self.installer.sha(self.target),
            self.installer.TARGET_SHA_MANIFEST["app/main.py"],
        )
        self.installer.validate_target_source(self.baseline["app/main.py"], self.target)

    def test_stage02_and_stage03_confirm_never_call_qwen(self) -> None:
        self.installer.confirm_guard_self_test(self.target)

    def test_all_six_stages_have_complete_v2_progress_fields(self) -> None:
        self.installer.progress_self_test(self.target)

    def test_transactional_install_and_exact_rollback_simulation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage-progress-v2-test-") as td:
            root = Path(td) / "platform-v2"
            for rel, data in self.baseline.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            self.installer.self_test(root, Path(sys.executable))
            for rel, expected in self.installer.BASELINE_SHA_MANIFEST.items():
                self.assertEqual(self.installer.sha((root / rel).read_bytes()), expected)


if __name__ == "__main__":
    unittest.main()
