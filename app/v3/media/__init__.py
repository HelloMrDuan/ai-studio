"""Reusable media production services for Xiaoduan Studio V3."""

from .bgm import BGMStore, should_use_bgm
from .composition import CompositionRequest, FFmpegCompositionService
from .pipeline import MediaProductionPipeline
from .subtitle import SubtitleCue, correct_against_script, read_srt, write_srt
from .subtitle_utils import file_to_subtitles, levenshtein_distance, similarity
from .task_artifacts import atomic_write_json, patch_json, read_json
from .tts import OpenAICompatibleTTS, OpenAICompatibleTTSConfig, TTSRequest, TTSReceipt

__all__ = [
    "BGMStore",
    "CompositionRequest",
    "FFmpegCompositionService",
    "MediaProductionPipeline",
    "OpenAICompatibleTTS",
    "OpenAICompatibleTTSConfig",
    "SubtitleCue",
    "TTSReceipt",
    "TTSRequest",
    "atomic_write_json",
    "correct_against_script",
    "file_to_subtitles",
    "levenshtein_distance",
    "patch_json",
    "read_json",
    "read_srt",
    "should_use_bgm",
    "similarity",
    "write_srt",
]
