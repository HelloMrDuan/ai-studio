"""FFmpeg-based final composition for Xiaoduan Studio V3.

The codec/fallback, audio bitrate and duration-safety ideas are adapted from
MoneyPrinterTurbo `app/services/video.py` at commit
5ceffd02a267de2ede0bbdb0fab8d7d875ea9842 (MIT). Xiaoduan uses a smaller,
provider-neutral FFmpeg service so composition can run as an independent
Temporal Activity instead of a monolithic video task.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"
SUPPORTED_VIDEO_CODECS = frozenset(
    {"libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_mf", "h264_videotoolbox"}
)


class CompositionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompositionRequest:
    video_clips: tuple[Path, ...]
    output_path: Path
    voice_path: Path | None = None
    bgm_path: Path | None = None
    subtitle_path: Path | None = None
    voice_volume: float = 1.0
    bgm_volume: float = 0.18
    preferred_video_codec: str = DEFAULT_VIDEO_CODEC


@dataclass(frozen=True)
class CompositionReceipt:
    output_path: Path
    video_codec: str
    audio_codec: str
    used_voice: bool
    used_bgm: bool
    burned_subtitles: bool
    command_count: int


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"{label} missing or empty: {path}")
    return path


def _ffmpeg_filter_escape(path: Path) -> str:
    # FFmpeg subtitle filter has its own escaping layer. Backslashes become
    # forward slashes and ':'/'\'' are escaped for filter syntax.
    value = str(path.resolve()).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "\\'")


class FFmpegCompositionService:
    def __init__(self, *, ffmpeg_binary: str = "ffmpeg", timeout_seconds: int = 7200) -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = timeout_seconds
        self._runtime_disabled_codecs: set[str] = set()

    def _encoder_available(self, codec: str) -> bool:
        if codec == DEFAULT_VIDEO_CODEC:
            return True
        try:
            result = subprocess.run(
                [self.ffmpeg_binary, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and codec in result.stdout

    def resolve_video_codec(self, preferred: str) -> str:
        codec = str(preferred or DEFAULT_VIDEO_CODEC).strip()
        if codec not in SUPPORTED_VIDEO_CODECS:
            return DEFAULT_VIDEO_CODEC
        if codec in self._runtime_disabled_codecs:
            return DEFAULT_VIDEO_CODEC
        return codec if self._encoder_available(codec) else DEFAULT_VIDEO_CODEC

    @staticmethod
    def _write_concat_file(clips: tuple[Path, ...], directory: Path) -> Path:
        descriptor, raw = tempfile.mkstemp(prefix=".xiaoduan-concat-", suffix=".txt", dir=directory)
        path = Path(raw)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for clip in clips:
                # concat demuxer escaping: single quote is represented by '\''.
                escaped = str(clip.resolve()).replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def build_command(
        self,
        request: CompositionRequest,
        *,
        concat_file: Path,
        output_path: Path,
        video_codec: str,
    ) -> list[str]:
        command = [
            self.ffmpeg_binary, "-y", "-nostdin", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
        ]
        voice_index = None
        bgm_index = None
        next_index = 1
        if request.voice_path:
            voice_index = next_index
            next_index += 1
            command += ["-i", str(request.voice_path)]
        if request.bgm_path:
            bgm_index = next_index
            next_index += 1
            # Loop BGM so short music does not truncate the final video.
            command += ["-stream_loop", "-1", "-i", str(request.bgm_path)]

        video_filters = []
        if request.subtitle_path:
            video_filters.append(f"subtitles='{_ffmpeg_filter_escape(request.subtitle_path)}'")
        if video_filters:
            command += ["-vf", ",".join(video_filters)]

        if voice_index is not None and bgm_index is not None:
            filter_complex = (
                f"[{voice_index}:a]volume={float(request.voice_volume):.4f}[voice];"
                f"[{bgm_index}:a]volume={float(request.bgm_volume):.4f}[bgm];"
                "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            command += ["-filter_complex", filter_complex, "-map", "0:v:0", "-map", "[aout]"]
        elif voice_index is not None:
            command += ["-map", "0:v:0", "-map", f"{voice_index}:a:0"]
        elif bgm_index is not None:
            command += ["-map", "0:v:0", "-map", f"{bgm_index}:a:0"]
        else:
            command += ["-map", "0:v:0", "-map", "0:a?",]

        command += [
            "-c:v", video_codec,
            "-c:a", AUDIO_CODEC,
            "-b:a", AUDIO_BITRATE,
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-shortest",
            str(output_path),
        ]
        return command

    def _run(self, command: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CompositionError("FFmpeg composition timed out") from exc
        except OSError as exc:
            raise CompositionError("failed to execute FFmpeg") from exc

    def compose(self, request: CompositionRequest) -> CompositionReceipt:
        if not request.video_clips:
            raise ValueError("composition requires at least one video clip")
        clips = tuple(_require_file(Path(path), "video clip") for path in request.video_clips)
        if request.voice_path:
            _require_file(request.voice_path, "voice audio")
        if request.bgm_path:
            _require_file(request.bgm_path, "background music")
        if request.subtitle_path:
            _require_file(request.subtitle_path, "subtitle")
        if request.voice_volume < 0 or request.bgm_volume < 0:
            raise ValueError("audio volumes cannot be negative")

        target = Path(request.output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = target.suffix or ".mp4"
        temp_output = target.with_name(f".{target.stem}.rendering{suffix}")
        concat_file = self._write_concat_file(clips, target.parent)
        preferred = self.resolve_video_codec(request.preferred_video_codec)
        command_count = 0
        try:
            command = self.build_command(
                request, concat_file=concat_file, output_path=temp_output, video_codec=preferred
            )
            result = self._run(command)
            command_count += 1
            effective = preferred
            if result.returncode != 0 and preferred != DEFAULT_VIDEO_CODEC:
                self._runtime_disabled_codecs.add(preferred)
                temp_output.unlink(missing_ok=True)
                effective = DEFAULT_VIDEO_CODEC
                command = self.build_command(
                    request, concat_file=concat_file, output_path=temp_output, video_codec=effective
                )
                result = self._run(command)
                command_count += 1
            if result.returncode != 0:
                raise CompositionError(
                    f"FFmpeg composition failed: {(result.stderr or result.stdout or '')[-2000:]}"
                )
            if not temp_output.is_file() or temp_output.stat().st_size <= 0:
                raise CompositionError("FFmpeg returned success but output is missing or empty")
            os.replace(temp_output, target)
            return CompositionReceipt(
                output_path=target,
                video_codec=effective,
                audio_codec=AUDIO_CODEC,
                used_voice=request.voice_path is not None,
                used_bgm=request.bgm_path is not None,
                burned_subtitles=request.subtitle_path is not None,
                command_count=command_count,
            )
        finally:
            concat_file.unlink(missing_ok=True)
            temp_output.unlink(missing_ok=True)
