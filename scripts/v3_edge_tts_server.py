#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

try:
    import edge_tts
except ImportError as exc:  # pragma: no cover - runtime dependency guard
    raise SystemExit(
        "edge-tts is not installed. Run: python -m pip install edge-tts"
    ) from exc


app = FastAPI(title="Xiaoduan Edge TTS Gateway", version="1.0")


class SpeechRequest(BaseModel):
    model: str = Field(min_length=1)
    input: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    response_format: str = "mp3"
    speed: float = Field(default=1.0, gt=0.0, le=4.0)
    instructions: str = ""


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "provider": "edge-tts", "openai_compatible": True}


@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest) -> Response:
    fmt = request.response_format.strip().lower().lstrip(".")
    if fmt != "mp3":
        raise HTTPException(status_code=400, detail="edge-tts gateway currently supports mp3 only")

    # Edge TTS expresses speaking rate as a signed percentage.
    rate_percent = int(round((request.speed - 1.0) * 100))
    rate = f"{rate_percent:+d}%"

    with tempfile.NamedTemporaryFile(prefix="xiaoduan-edge-", suffix=".mp3", delete=False) as tmp:
        target = Path(tmp.name)
    try:
        communicator = edge_tts.Communicate(
            text=request.input.strip(),
            voice=request.voice.strip(),
            rate=rate,
        )
        await communicator.save(str(target))
        content = target.read_bytes()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"edge-tts synthesis failed: {exc}") from exc
    finally:
        target.unlink(missing_ok=True)

    if not content:
        raise HTTPException(status_code=502, detail="edge-tts returned empty audio")
    return Response(content=content, media_type="audio/mpeg")
