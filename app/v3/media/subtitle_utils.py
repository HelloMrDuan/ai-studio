"""Subtitle parsing/matching primitives.

The SRT parsing and Levenshtein helpers are adapted from MoneyPrinterTurbo
`app/services/subtitle.py` at upstream commit
5ceffd02a267de2ede0bbdb0fab8d7d875ea9842 (MIT License). They are kept free of
Whisper/config dependencies so Xiaoduan can use them with local or API TTS/ASR.
"""

from __future__ import annotations

import re
from pathlib import Path


_TIMESTAMP = re.compile(r"([0-9]+:[0-9]+:[0-9]+,[0-9]+)")


def file_to_subtitles(filename: str | Path) -> list[tuple[int, str, str]]:
    path = Path(filename)
    if not path.is_file():
        return []

    items: list[tuple[int, str, str]] = []
    current_times: str | None = None
    current_text = ""
    index = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            times = _TIMESTAMP.findall(line)
            if times:
                current_times = line.strip()
            elif line.strip() == "" and current_times:
                index += 1
                items.append((index, current_times, current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    if current_times:
        index += 1
        items.append((index, current_times, current_text.strip()))
    return items


def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for index, char1 in enumerate(s1):
        current_row = [index + 1]
        for column, char2 in enumerate(s2):
            insertions = previous_row[column + 1] + 1
            deletions = current_row[column] + 1
            substitutions = previous_row[column] + (char1 != char2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    max_length = max(len(a), len(b))
    if max_length == 0:
        return 1.0
    distance = levenshtein_distance(a.lower(), b.lower())
    return 1 - (distance / max_length)
