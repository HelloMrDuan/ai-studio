"""Subtitle timeline primitives and script correction.

Adapted from MoneyPrinterTurbo `app/services/subtitle.py` at commit
5ceffd02a267de2ede0bbdb0fab8d7d875ea9842 (MIT). This V3 port is independent
of Whisper: ASR can be local or API and only has to return timed segments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .subtitle_utils import file_to_subtitles, similarity


@dataclass(frozen=True)
class SubtitleCue:
    start_seconds: float
    end_seconds: float
    text: str

    def __post_init__(self) -> None:
        if self.start_seconds < 0 or self.end_seconds <= self.start_seconds:
            raise ValueError("subtitle cue requires 0 <= start < end")
        if not self.text.strip():
            raise ValueError("subtitle cue text is required")


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(cues: list[SubtitleCue] | tuple[SubtitleCue, ...], output_path: str | Path) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for index, cue in enumerate(cues, 1):
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start_seconds)} --> {_srt_timestamp(cue.end_seconds)}\n{cue.text.strip()}"
        )
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    temp.replace(target)
    return target


def _parse_timestamp(value: str) -> float:
    hours, minutes, tail = value.strip().split(":")
    seconds, millis = tail.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def read_srt(path: str | Path) -> tuple[SubtitleCue, ...]:
    cues = []
    for _, times, text in file_to_subtitles(path):
        start, end = [item.strip() for item in times.split("-->", 1)]
        cues.append(SubtitleCue(_parse_timestamp(start), _parse_timestamp(end), text))
    return tuple(cues)


def correct_against_script(
    cues: list[SubtitleCue] | tuple[SubtitleCue, ...],
    script_lines: list[str] | tuple[str, ...],
    *,
    similarity_threshold: float = 0.8,
) -> tuple[SubtitleCue, ...]:
    """Merge adjacent ASR cues when doing so better matches one script line.

    This preserves the upstream correction strategy but accepts already
    segmented script lines, keeping punctuation/language segmentation out of
    this low-level module.
    """
    script = [str(item).strip() for item in script_lines if str(item).strip()]
    source = list(cues)
    corrected: list[SubtitleCue] = []
    s_idx = 0
    c_idx = 0
    while s_idx < len(script) and c_idx < len(source):
        expected = script[s_idx]
        current = source[c_idx]
        combined = current.text.strip()
        end = current.end_seconds
        next_idx = c_idx + 1
        while next_idx < len(source):
            candidate = combined + " " + source[next_idx].text.strip()
            if similarity(expected, candidate) > similarity(expected, combined):
                combined = candidate
                end = source[next_idx].end_seconds
                next_idx += 1
            else:
                break
        if similarity(expected, combined) >= similarity_threshold:
            corrected.append(SubtitleCue(current.start_seconds, end, expected))
            c_idx = next_idx
        else:
            corrected.append(current)
            c_idx += 1
        s_idx += 1
    corrected.extend(source[c_idx:])
    return tuple(corrected)


def proportional_cues(texts: list[str] | tuple[str, ...], duration_seconds: float) -> tuple[SubtitleCue, ...]:
    """Fallback timeline when TTS/ASR exposes no word timestamps.

    Duration is allocated by visible character count. This is deterministic and
    intentionally marked as a fallback; provider-native or ASR timings should
    be preferred when available.
    """
    items = [str(item).strip() for item in texts if str(item).strip()]
    if not items:
        return ()
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    weights = [max(1, len(item.replace(" ", ""))) for item in items]
    total = sum(weights)
    cursor = 0.0
    result = []
    for index, (text, weight) in enumerate(zip(items, weights)):
        end = duration_seconds if index == len(items) - 1 else cursor + duration_seconds * weight / total
        result.append(SubtitleCue(cursor, end, text))
        cursor = end
    return tuple(result)
