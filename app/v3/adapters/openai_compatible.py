from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from app.v3.contracts import Capability, ProviderModelSpec, ProviderTransport


class OpenAICompatibleAdapter:
    """OpenAI-compatible chat adapter for both local and remote endpoints.

    Retry belongs to the workflow/activity layer. This adapter performs one
    provider attempt and returns provider-native errors to the caller.
    """

    def __init__(
        self,
        spec: ProviderModelSpec,
        *,
        secret_resolver: Callable[[str], str | None] | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        if spec.transport not in {
            ProviderTransport.remote_api,
            ProviderTransport.local_http,
            ProviderTransport.local_openai_compatible,
        }:
            raise ValueError(f"unsupported transport for OpenAI-compatible adapter: {spec.transport}")
        if Capability.text not in spec.capabilities:
            raise ValueError("OpenAI-compatible chat adapter requires text capability")
        if not spec.base_url:
            raise ValueError("provider base_url is required")
        self.spec = spec
        self.base_url = spec.base_url.rstrip("/")
        self.secret_resolver = secret_resolver
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.spec.secret_ref:
            if self.secret_resolver is None:
                raise RuntimeError(f"secret resolver required for {self.spec.secret_ref}")
            secret = self.secret_resolver(self.spec.secret_ref)
            if not secret:
                raise RuntimeError(f"secret not found: {self.spec.secret_ref}")
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
                response.raise_for_status()
                body = response.json()
            models = [
                str(item.get("id") or "")
                for item in (body.get("data") or [])
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]
            return {
                "ready": True,
                "provider_id": self.spec.provider_id,
                "model_id": self.spec.model_id,
                "models": models,
            }
        except Exception as exc:
            return {
                "ready": False,
                "provider_id": self.spec.provider_id,
                "model_id": self.spec.model_id,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        require_structured_output: bool = False,
    ) -> dict[str, Any]:
        if require_structured_output and Capability.structured_output not in self.spec.capabilities:
            raise RuntimeError(f"provider {self.spec.identity} does not support structured_output")
        payload: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if require_structured_output:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("provider response missing choices[0]")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("provider response missing choices[0].message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("provider response contains no final content")
        return {
            "content": content,
            "model": str(body.get("model") or self.spec.model_id),
            "usage": body.get("usage") if isinstance(body.get("usage"), dict) else {},
            "provider_id": self.spec.provider_id,
        }
