from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .composition import CompositionReceipt, CompositionRequest, FFmpegCompositionService
from .subtitle import SubtitleCue, write_srt
from .tts import TTSReceipt, TTSRequest


@dataclass(frozen=True)
class MediaStageReceipt:
    stage: str
    output_path: Path
    metadata: dict


@dataclass(frozen=True)
class MediaPipelineResult:
    stages: tuple[MediaStageReceipt, ...]
    final_video: Path


TTSCallable = Callable[[TTSRequest, Path], Awaitable[TTSReceipt]]


class MediaProductionPipeline:
    """Independent post-production stages; image/video generation is never retried here."""

    def __init__(self, *, composition: FFmpegCompositionService) -> None:
        self.composition = composition

    async def run(
        self,
        *,
        work_dir: Path,
        video_clips: tuple[Path, ...],
        final_output: Path,
        tts_request: TTSRequest | None = None,
        tts_callable: TTSCallable | None = None,
        subtitle_cues: tuple[SubtitleCue, ...] = (),
        bgm_path: Path | None = None,
        bgm_volume: float = 0.18,
    ) -> MediaPipelineResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        stages: list[MediaStageReceipt] = []
        voice_path: Path | None = None
        if tts_request is not None:
            if tts_callable is None:
                raise ValueError("tts_callable is required when tts_request is provided")
            extension = "." + (tts_request.response_format.strip().lstrip(".") or "mp3")
            voice_path = work_dir / f"voice{extension}"
            receipt = await tts_callable(tts_request, voice_path)
            stages.append(
                MediaStageReceipt(
                    stage="tts",
                    output_path=receipt.output_path,
                    metadata={"provider_id": receipt.provider_id, "model": receipt.model, "voice": receipt.voice},
                )
            )

        subtitle_path: Path | None = None
        if subtitle_cues:
            subtitle_path = write_srt(subtitle_cues, work_dir / "subtitles.srt")
            stages.append(
                MediaStageReceipt(stage="subtitle", output_path=subtitle_path, metadata={"cue_count": len(subtitle_cues)})
            )

        if bgm_path is not None:
            stages.append(MediaStageReceipt(stage="bgm", output_path=bgm_path, metadata={"volume": bgm_volume}))

        composition_receipt: CompositionReceipt = self.composition.compose(
            CompositionRequest(
                video_clips=video_clips,
                output_path=final_output,
                voice_path=voice_path,
                bgm_path=bgm_path,
                subtitle_path=subtitle_path,
                bgm_volume=bgm_volume,
            )
        )
        stages.append(
            MediaStageReceipt(
                stage="composition",
                output_path=composition_receipt.output_path,
                metadata={
                    "video_codec": composition_receipt.video_codec,
                    "audio_codec": composition_receipt.audio_codec,
                    "used_voice": composition_receipt.used_voice,
                    "used_bgm": composition_receipt.used_bgm,
                    "burned_subtitles": composition_receipt.burned_subtitles,
                },
            )
        )
        return MediaPipelineResult(stages=tuple(stages), final_video=composition_receipt.output_path)
