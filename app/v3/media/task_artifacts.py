"""Atomic task-artifact persistence for Xiaoduan Studio V3.

Adapted from MoneyPrinterTurbo `app/services/task_artifacts.py`
(upstream commit 5ceffd02a267de2ede0bbdb0fab8d7d875ea9842), MIT License.
The Xiaoduan version removes MoneyPrinterTurbo-specific task-directory and
logging dependencies so it can be reused by Temporal Activities and media
services.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def atomic_write_json(target: Path | str, payload: Mapping[str, Any]) -> None:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                payload,
                temp_file,
                ensure_ascii=False,
                indent=2,
                default=lambda value: value.__dict__,
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def read_json(target: Path | str) -> dict[str, Any]:
    path = Path(target)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"artifact JSON must be an object: {path}")
    return payload


def patch_json(target: Path | str, **updates: Any) -> dict[str, Any]:
    payload = read_json(target)
    payload.update(updates)
    atomic_write_json(target, payload)
    return payload
