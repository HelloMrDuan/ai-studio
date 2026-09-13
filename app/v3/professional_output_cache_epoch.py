from __future__ import annotations

from types import MethodType
from typing import Any

from . import front_half_quality_gate as gate
from .professional_output_registry import output_kind_for_skill


_CACHE_EPOCH = "professional-output-registry-v1"


def install_professional_output_cache_epoch(director: Any) -> dict[str, str]:
    """Put the strict output-contract epoch inside the persistent LLM cache key.

    ProductionRuntimeOptimizer hashes the arguments presented to
    ``_tracked_llm_chat``. This wrapper is intentionally installed *after* that
    optimizer so old plain-Markdown front-half cache entries can never satisfy a
    new strict professional-output request.
    """
    if getattr(director, "_xiaoduan_professional_output_cache_epoch_installed", False):
        return {"status": "already_installed", "epoch": _CACHE_EPOCH}

    original = director._tracked_llm_chat

    async def tracked_llm_chat(
        instance: Any,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ):
        if phase == "director_orchestrator_content":
            skill = gate._detect_skill(system_prompt, messages)
            if output_kind_for_skill(skill):
                system_prompt = (
                    system_prompt
                    + "\n\nXIAODUAN_PROFESSIONAL_OUTPUT_CACHE_EPOCH="
                    + _CACHE_EPOCH
                )
        return await original(
            phase=phase,
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    director._tracked_llm_chat = MethodType(tracked_llm_chat, director)
    director._xiaoduan_professional_output_cache_epoch_installed = True
    return {"status": "installed", "epoch": _CACHE_EPOCH}


__all__ = ["install_professional_output_cache_epoch"]
