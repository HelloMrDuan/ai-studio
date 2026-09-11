from __future__ import annotations

import asyncio
import copy
import json
import logging
from contextvars import ContextVar
from typing import Any

import httpx

from app.services.director import DirectorService
from app.services.gemma import (
    GemmaService,
    _normalize_request_messages,
    _normalize_runtime_model,
    _text,
)


logger = logging.getLogger(__name__)

# llama.cpp's official OpenAI-compatible example constrains generation with:
#   response_format={"type":"json_object","schema": <JSON Schema>}
# Keep that transport contract here instead of asking the model to "please emit
# valid JSON" and trying to repair malformed text afterwards.
_STRUCTURED_CALL: ContextVar[dict[str, Any] | None] = ContextVar(
    "xiaoduan_llama_structured_call", default=None
)


def _json_after_marker(text: str, marker: str) -> dict[str, Any] | None:
    source = str(text or "")
    pos = source.find(marker)
    if pos < 0:
        return None
    tail = source[pos + len(marker):].lstrip()
    if not tail:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(tail)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def response_schema_from_call(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Recover only the schema explicitly supplied by the professional runtime."""
    schema = _json_after_marker(system_prompt, "AUTHORITATIVE_JSON_SCHEMA=")
    if schema is not None:
        return schema
    for message in messages:
        if not isinstance(message, dict):
            continue
        schema = _json_after_marker(
            str(message.get("content") or ""),
            "AUTHORITATIVE_SCHEMA=",
        )
        if schema is not None:
            return schema
    return None


def output_kind_from_schema(schema: dict[str, Any]) -> str:
    try:
        node = (schema.get("properties") or {}).get("output_kind") or {}
        value = node.get("const")
        if isinstance(value, str):
            return value
        enum = node.get("enum")
        if isinstance(enum, list) and len(enum) == 1 and isinstance(enum[0], str):
            return enum[0]
    except Exception:
        pass
    return ""


def llama_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Exact llama.cpp JSON-schema response_format shape used by its examples."""
    return {
        "type": "json_object",
        "schema": copy.deepcopy(schema),
    }


def build_structured_payload(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": llama_response_format(schema),
    }


def _drop_invalid_raw_output(text: str) -> str:
    """A schema retry regenerates from source; never feed truncated JSON back in."""
    value = str(text or "")
    marker = "\nRAW_OUTPUT="
    pos = value.find(marker)
    if pos < 0:
        return value
    return (
        value[:pos]
        + "\nPREVIOUS_OUTPUT_STATUS=invalid_or_truncated; regenerate the complete object from SOURCE."
    )


def _budget_instruction(output_kind: str, *, aggressive: bool) -> str:
    if aggressive:
        detail = (
            "The previous structured result reached the output boundary. Regenerate from the "
            "authoritative source and finish the whole object early. Keep document concise "
            "(normally 500-900 CJK characters for a short story input), use 1-2 concise "
            "sentences per descriptive field, and use the shortest exact source sentence for "
            "source_evidence. Do not repeat the schema or duplicate facts."
        )
    else:
        detail = (
            "Structured decoding is active. Finish the complete object before the token limit. "
            "Keep human-readable document and descriptive fields concise; for a short input, "
            "prefer roughly 600-1400 CJK characters for document, 1-3 sentences per descriptive "
            "field, and one short exact source sentence for source_evidence. Do not repeat the "
            "schema or duplicate the same facts across prose."
        )
    return f"\n\n=== STRUCTURED OUTPUT BUDGET ({output_kind or 'professional'}) ===\n{detail}"


def _prepare_messages(
    messages: list[dict[str, Any]],
    *,
    output_kind: str,
    phase: str,
    aggressive: bool,
) -> list[dict[str, str]]:
    normalized = _normalize_request_messages(messages)
    if phase == "professional_output_schema_repair":
        normalized = [
            {
                "role": item["role"],
                "content": _drop_invalid_raw_output(item["content"]),
            }
            for item in normalized
        ]
    instruction = _budget_instruction(output_kind, aggressive=aggressive)
    if normalized and normalized[0]["role"] == "system":
        normalized[0] = {
            "role": "system",
            "content": normalized[0]["content"] + instruction,
        }
    else:
        normalized.insert(0, {"role": "system", "content": instruction.strip()})
    return normalized


def _finish_reason(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return _text(choices[0].get("finish_reason"))
    return ""


async def _request_messages_structured(
    service: GemmaService,
    *,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    verified_model: str,
    schema: dict[str, Any],
    phase: str,
) -> tuple[str, str, dict[str, Any]]:
    model = _normalize_runtime_model(verified_model)
    if not model:
        status = await service.status()
        if not status.get("ready"):
            raise RuntimeError(_text(status.get("message")) or "Qwen 服务不可用")
        model = _normalize_runtime_model(status.get("resolved_model"))
    if not model:
        raise RuntimeError("Qwen 服务未返回可用 model")

    output_kind = output_kind_from_schema(schema)
    last_error: Exception | None = None
    total_attempts = 0

    # Attempt 1 is concise by default. If the model still consumes the output
    # boundary, attempt 2 regenerates from the same source under the same schema
    # with a tighter budget instruction. No unconstrained JSON fallback exists.
    for aggressive in (False, True):
        total_attempts += 1
        prepared = _prepare_messages(
            messages,
            output_kind=output_kind,
            phase=phase,
            aggressive=aggressive,
        )
        payload = build_structured_payload(
            model=model,
            messages=prepared,
            temperature=min(float(temperature), 0.35),
            max_tokens=max_tokens,
            schema=schema,
        )
        try:
            async with httpx.AsyncClient(
                timeout=service.settings.gemma_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{service.base_url}/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()

            response_model = _text(body.get("model"))
            if not response_model:
                raise ValueError("Qwen 响应缺少实际 model 字段")
            finish_reason = _finish_reason(body)
            metrics = {
                "usage": (
                    dict(body.get("usage"))
                    if isinstance(body.get("usage"), dict)
                    else {}
                ),
                "timings": (
                    dict(body.get("timings"))
                    if isinstance(body.get("timings"), dict)
                    else {}
                ),
                "request_attempts": total_attempts,
                "request_retries": total_attempts - 1,
                "finish_reason": finish_reason,
                "structured_output": True,
                "structured_output_transport": "llama.cpp-response_format-json-schema",
                "output_kind": output_kind,
            }
            if finish_reason == "length":
                if not aggressive:
                    continue
                error = RuntimeError(
                    "Qwen 严格 JSON Schema 输出连续两次达到 token 上限；"
                    "已拒绝把截断 JSON 交给业务层"
                )
                error.llm_metrics = metrics
                raise error

            content = service._extract_content(body)
            return content, response_model, metrics
        except httpx.HTTPStatusError as exc:
            last_error = exc
            # Do not silently fall back to prompt-only JSON. That is the exact
            # failure mode this layer exists to remove.
            if exc.response is not None and exc.response.status_code in {400, 422}:
                error = RuntimeError(
                    "当前 llama.cpp 不接受 JSON Schema response_format；"
                    "请升级/更换支持 schema grammar 的 llama-server。"
                    f" server={exc.response.text[:1200]}"
                )
                error.llm_metrics = {
                    "usage": {},
                    "timings": {},
                    "request_attempts": total_attempts,
                    "request_retries": total_attempts - 1,
                    "structured_output": True,
                }
                raise error from exc
            if not aggressive:
                await asyncio.sleep(0.5)
                continue
            raise
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            if not aggressive:
                await asyncio.sleep(0.5)
                continue
            break

    error = RuntimeError(f"Qwen JSON Schema 请求失败：{last_error}")
    error.llm_metrics = {
        "usage": {},
        "timings": {},
        "request_attempts": total_attempts,
        "request_retries": max(0, total_attempts - 1),
        "structured_output": True,
    }
    raise error


def install_llama_structured_output(director: DirectorService) -> dict[str, Any]:
    """Install schema-constrained decoding before ProfessionalOutputRuntime.

    This deliberately follows llama.cpp's own response_format+JSON-Schema
    contract. The existing Director remains the owner of prompt/context/telemetry;
    only the transport for strict professional objects is constrained.
    """
    if getattr(DirectorService, "_xiaoduan_llama_structured_output_installed", False):
        return {
            "status": "already_installed",
            "policy": "llama_cpp_json_schema_v1",
        }

    original_tracked = DirectorService._tracked_llm_chat
    original_request = GemmaService._request_messages

    async def tracked_llm_chat(
        self: DirectorService,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        schema = response_schema_from_call(
            system_prompt=system_prompt,
            messages=messages,
        )
        if schema is None:
            return await original_tracked(
                self,
                phase=phase,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        token = _STRUCTURED_CALL.set({"schema": schema, "phase": phase})
        try:
            return await original_tracked(
                self,
                phase=phase,
                messages=messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        finally:
            _STRUCTURED_CALL.reset(token)

    async def request_messages(
        self: GemmaService,
        *,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int = 2048,
        verified_model: str = "",
    ) -> tuple[str, str, dict[str, Any]]:
        active = _STRUCTURED_CALL.get()
        if not active:
            return await original_request(
                self,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                verified_model=verified_model,
            )
        return await _request_messages_structured(
            self,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            verified_model=verified_model,
            schema=dict(active["schema"]),
            phase=str(active.get("phase") or ""),
        )

    DirectorService._tracked_llm_chat = tracked_llm_chat
    GemmaService._request_messages = request_messages
    DirectorService._xiaoduan_llama_structured_output_installed = True
    director._xiaoduan_llama_structured_output = True
    return {
        "status": "installed",
        "policy": "llama_cpp_json_schema_v1",
        "transport": "response_format.json_object.schema",
        "unconstrained_fallback": False,
        "truncation_retry": True,
        "repair_reuses_raw_output": False,
    }


__all__ = [
    "build_structured_payload",
    "install_llama_structured_output",
    "llama_response_format",
    "output_kind_from_schema",
    "response_schema_from_call",
]
