from __future__ import annotations

import hashlib
import sys
import tarfile
import types
from pathlib import Path
from types import ModuleType


_ARCHIVE_SHA256 = "b333b86467682dd766f57c5ea5fb04f271e1710f9314192e4a4cb3cb00483786"
_MAIN_SHA256 = "35591e0373c37efa62f2b3606c81bff68a34f28bf665dbb7968e80697d085ff1"
_STAGE04_SHA256 = "e668321b8eccf9f8adaf02452ffd5c9a0c1f0b890db4ca53ff28bd718fbdf332"
_RUNTIME_MODULE = "app._archived_v23963_main"
_STAGE04_MODULE = "app.stage04_v238_runtime"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_path() -> Path:
    return Path(__file__).resolve().parents[1] / "v23963-current-runtime.tar.gz"


def _read_member(archive: tarfile.TarFile, name: str, expected_sha256: str) -> bytes:
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise RuntimeError(f"原工作台运行时快照缺少文件：{name}") from exc
    if not member.isfile():
        raise RuntimeError(f"原工作台运行时快照成员不是普通文件：{name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise RuntimeError(f"无法读取原工作台运行时快照：{name}")
    data = handle.read()
    actual = _sha256(data)
    if actual != expected_sha256:
        raise RuntimeError(
            f"原工作台运行时快照校验失败：{name}，expected={expected_sha256} actual={actual}"
        )
    return data


def _exec_module(name: str, source: bytes, *, file_path: Path) -> ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(file_path)
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    try:
        code = compile(source.decode("utf-8"), str(file_path), "exec")
        exec(code, module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_original_workbench_runtime() -> ModuleType:
    """Load the repository-pinned V2.39.6.3 workbench without rewriting it.

    V3 intentionally keeps the original workbench UI and its mature editable
    stage/candidate/adoption interactions. The archived runtime is hash-locked
    by deliverables/v23963_current_runtime_baseline_sha_manifest.json. V3 APIs
    are layered on top by app.main rather than replacing the original shell.
    """
    existing = sys.modules.get(_RUNTIME_MODULE)
    if existing is not None:
        return existing

    archive_path = _archive_path()
    if not archive_path.is_file():
        raise RuntimeError(f"缺少原工作台运行时快照：{archive_path}")
    archive_bytes = archive_path.read_bytes()
    actual_archive_sha = _sha256(archive_bytes)
    if actual_archive_sha != _ARCHIVE_SHA256:
        raise RuntimeError(
            "原工作台运行时压缩包校验失败："
            f"expected={_ARCHIVE_SHA256} actual={actual_archive_sha}"
        )

    app_dir = Path(__file__).resolve().parent
    with tarfile.open(archive_path, "r:gz") as archive:
        # The historical main expects the Stage04 runtime module to exist under
        # the original import name. Load the pinned copy into sys.modules first
        # when the active V3 tree intentionally no longer carries that file.
        if _STAGE04_MODULE not in sys.modules:
            stage04_source = _read_member(
                archive,
                "app/stage04_v238_runtime.py",
                _STAGE04_SHA256,
            )
            _exec_module(
                _STAGE04_MODULE,
                stage04_source,
                file_path=app_dir / "stage04_v238_runtime.py",
            )

        main_source = _read_member(archive, "app/main.py", _MAIN_SHA256)

    return _exec_module(
        _RUNTIME_MODULE,
        main_source,
        file_path=app_dir / "main.py",
    )


__all__ = ["load_original_workbench_runtime"]
