"""Provider-neutral TTS services for Xiaoduan Studio V3.

Provider dispatch and local/self-hosted support patterns are adapted from
MoneyPrinterTurbo `app/services/voice.py` at commit
5ceffd02a267de2ede0bbdb0fab8d7d875ea9842 (MIT). Xiaoduan V3 removes the
upstream global config/dispatcher and exposes a narrow capability adapter that
works identically with remote or local OpenAI-compatible speech endpoints.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class TTSRequest:
    text: str
    voice: str
    model: str
    response_format: str = "mp3"
    speed: float = 1.0
    instructions: str = ""


@dataclass(frozen=True)
class TTSReceipt:
    output_path: Path
    provider_id: str
    model: str
    voice: str
    bytes_written: int
    response_format: str


@dataclass(frozen=True)
class OpenAICompatibleTTSConfig:
    provider_id: str
    base_url: str
    secret_ref: str | None = None
    timeout_seconds: float = 300.0

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/audio/speech"


class OpenAICompatibleTTS:
    """Same adapter for cloud APIs and local OpenAI-compatible TTS servers."""

    def __init__(self, config: OpenAICompatibleTTSConfig) -> None:
        if not config.provider_id.strip():
            raise ValueError("provider_id is required")
        if not config.base_url.strip():
            raise ValueError("base_url is required")
        self.config = config

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.secret_ref:
            secret = os.environ.get(self.config.secret_ref, "").strip()
            if not secret:
                raise TTSError(f"TTS secret environment variable is not set: {self.config.secret_ref}")
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    async def synthesize(self, request: TTSRequest, output_path: Path | str) -> TTSReceipt:
        text = str(request.text or "").strip()
        if not text:
            raise ValueError("TTS text is required")
        if not request.voice.strip() or not request.model.strip():
            raise ValueError("TTS voice and model are required")
        if request.speed <= 0 or request.speed > 4:
            raise ValueError("TTS speed must be within (0, 4]")
        payload: dict[str, Any] = {
            "model": request.model,
            "input": text,
            "voice": request.voice,
            "response_format": request.response_format,
            "speed": request.speed,
        }
        if request.instructions.strip():
            payload["instructions"] = request.instructions.strip()
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=False) as client:
            response = await client.post(self.config.endpoint(), headers=self._headers(), json=payload)
            if response.status_code >= 400:
                detail = response.text[-1200:]
                raise TTSError(f"TTS provider rejected request: HTTP {response.status_code}: {detail}")
            content = bytes(response.content)
        if not content:
            raise TTSError("TTS provider returned empty audio")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_bytes(content)
        temp.replace(target)
        return TTSReceipt(
            output_path=target,
            provider_id=self.config.provider_id,
            model=request.model,
            voice=request.voice,
            bytes_written=len(content),
            response_format=request.response_format,
        )
