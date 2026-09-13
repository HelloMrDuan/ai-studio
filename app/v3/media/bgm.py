"""Safe BGM ingestion/validation for Xiaoduan Studio V3.

Adapted from MoneyPrinterTurbo `app/services/bgm.py` at commit
5ceffd02a267de2ede0bbdb0fab8d7d875ea9842 (MIT). The Xiaoduan port keeps the
bounded upload, cross-platform filename checks, full FFmpeg audio decode probe,
and atomic persistence while removing Streamlit/global-config dependencies.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


MAX_BGM_UPLOAD_BYTES = 30 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_INTERNAL_UPLOAD_PREFIX = ".bgm-upload-"
_WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
SUPPORTED_BGM_EXTENSIONS = (
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"
)


class BGMUploadError(ValueError):
    pass


class BGMServiceError(RuntimeError):
    pass


def should_use_bgm(bgm_type: str | None, bgm_volume: float | None) -> bool:
    if not str(bgm_type or "").strip():
        return False
    try:
        volume = float(bgm_volume or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(volume) and volume > 0


def sanitize_upload_filename(filename: str) -> str:
    safe_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name) > 255
        or any(ord(character) < 32 for character in safe_name)
        or any(character in _WINDOWS_INVALID_FILENAME_CHARS for character in safe_name)
        or safe_name.lower().startswith(_INTERNAL_UPLOAD_PREFIX)
    ):
        raise BGMUploadError("invalid background music filename")
    windows_basename = safe_name.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        raise BGMUploadError("invalid background music filename")
    if Path(safe_name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
        raise BGMUploadError("unsupported background music format")
    return safe_name


def validate_audio_file(file_path: str | Path, *, ffmpeg_binary: str = "ffmpeg", timeout_seconds: int = 120) -> None:
    path = Path(file_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise BGMUploadError("background music file is empty or missing")
    try:
        decoded = subprocess.run(
            [
                ffmpeg_binary, "-nostdin", "-v", "error", "-xerror",
                "-i", str(path), "-map", "0:a:0", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BGMServiceError("FFmpeg background music validation timed out") from exc
    except OSError as exc:
        raise BGMServiceError("failed to run FFmpeg for background music validation") from exc
    if decoded.returncode != 0:
        raise BGMUploadError("background music must contain a fully decodable audio stream")


class BGMStore:
    def __init__(self, data_dir: Path | str, *, ffmpeg_binary: str = "ffmpeg") -> None:
        self.root = Path(data_dir) / "v3" / "bgm"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_binary = ffmpeg_binary

    def _stage(self, filename: str, source: BinaryIO) -> tuple[str, Path, int]:
        safe_name = sanitize_upload_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        descriptor, raw_path = tempfile.mkstemp(
            prefix=_INTERNAL_UPLOAD_PREFIX, suffix=suffix, dir=self.root
        )
        temp_path = Path(raw_path)
        total = 0
        try:
            try:
                source.seek(0)
            except (AttributeError, OSError) as exc:
                raise BGMUploadError("background music upload is not seekable") from exc
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = source.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise BGMUploadError("background music upload must be binary")
                    total += len(chunk)
                    if total > MAX_BGM_UPLOAD_BYTES:
                        raise BGMUploadError("background music file exceeds the 30 MB limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total <= 0:
                raise BGMUploadError("background music file is empty")
            return safe_name, temp_path, total
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        finally:
            try:
                source.seek(0)
            except (AttributeError, OSError):
                pass

    def save_upload(self, filename: str, source: BinaryIO) -> Path:
        safe_name, staged, _ = self._stage(filename, source)
        target = self.root / f"{uuid4().hex}{Path(safe_name).suffix.lower()}"
        try:
            validate_audio_file(staged, ffmpeg_binary=self.ffmpeg_binary, timeout_seconds=30)
            os.replace(staged, target)
            return target
        finally:
            staged.unlink(missing_ok=True)

    def list_files(self) -> tuple[Path, ...]:
        result = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.name.startswith(_INTERNAL_UPLOAD_PREFIX):
                continue
            if path.suffix.lower() in SUPPORTED_BGM_EXTENSIONS:
                result.append(path)
        return tuple(result)
