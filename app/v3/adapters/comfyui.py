from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.v3.contracts import Capability, ProviderModelSpec, ProviderTransport


class ComfyUIAdapter:
    """Low-level ComfyUI transport adapter.

    Workflow compilation remains a domain/provider concern. The adapter only
    owns health, reference upload and workflow queue transport so business code
    never hard-codes ComfyUI HTTP endpoints.
    """

    def __init__(self, spec: ProviderModelSpec, *, timeout_seconds: float = 1200.0) -> None:
        if spec.transport != ProviderTransport.local_comfyui:
            raise ValueError("ComfyUI adapter requires local_comfyui transport")
        if not ({Capability.image_generation, Capability.video_generation} & spec.capabilities):
            raise ValueError("ComfyUI adapter requires image_generation or video_generation capability")
        if not spec.base_url:
            raise ValueError("ComfyUI base_url is required")
        self.spec = spec
        self.base_url = spec.base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                response = await client.get(f"{self.base_url}/system_stats")
                response.raise_for_status()
                body = response.json()
            return {"ready": True, "provider_id": self.spec.provider_id, "stats": body}
        except Exception as exc:
            return {
                "ready": False,
                "provider_id": self.spec.provider_id,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def upload_reference(
        self,
        *,
        filename: str,
        content: bytes,
        overwrite: bool = False,
        subfolder: str = "xiaoduan-v3",
    ) -> dict[str, Any]:
        if Capability.image_reference not in self.spec.capabilities:
            raise RuntimeError(f"provider {self.spec.identity} does not support image_reference")
        safe_name = Path(filename).name
        if not safe_name:
            raise ValueError("reference filename is required")
        files = {"image": (safe_name, content, "application/octet-stream")}
        data = {
            "type": "input",
            "subfolder": subfolder,
            "overwrite": "true" if overwrite else "false",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/upload/image", files=files, data=data)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or not body.get("name"):
            raise ValueError("ComfyUI upload response missing image name")
        return body

    async def queue_workflow(
        self,
        workflow: dict[str, Any],
        *,
        client_id: str | None = None,
        prompt_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("ComfyUI workflow is required")
        payload: dict[str, Any] = {"prompt": workflow}
        if client_id:
            payload["client_id"] = client_id
        if prompt_extra:
            payload.update(prompt_extra)
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/prompt", json=payload)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or not str(body.get("prompt_id") or "").strip():
            raise ValueError("ComfyUI queue response missing prompt_id")
        return body

    async def history(self, prompt_id: str) -> dict[str, Any]:
        pid = str(prompt_id or "").strip()
        if not pid:
            raise ValueError("prompt_id is required")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/history/{pid}")
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise ValueError("ComfyUI history response must be an object")
        return body
