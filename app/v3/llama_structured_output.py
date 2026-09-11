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

# llama.cpp accepts JSON Schema only through the subset its JSON-Schema -> GBNF
# converter can compile. Pydantic is still the canonical business schema owner;
# this module projects that schema to a transport-safe grammar schema only.
_STRUCTURED_CALL: ContextVar[dict[str, Any] | None] = ContextVar(
    "xiaoduan_llama_structured_call", default=None
)

_LLAMA_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "additionalProperties",
    "anyOf",
    "oneOf",
    "allOf",
}
_LLAMA_SCHEMA_METADATA = {
    "$schema",
    "$id",
    "$anchor",
    "title",
    "description",
    "default",
    "examples",
}


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
    """Recover only the canonical schema supplied by the professional runtime."""
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


def _resolve_local_ref(root: dict[str, Any], ref: str) -> Any:
    """Resolve one local JSON pointer without fetching or inventing schema."""
    if not ref.startswith("#/"):
        raise ValueError(f"llama transport only supports local schema refs: {ref}")
    node: Any = root
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise ValueError(f"unresolved local schema ref: {ref}")
        node = node[token]
    return node


def llama_transport_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project canonical Pydantic JSON Schema to llama.cpp's grammar subset.

    llama.cpp's converter documents only a subset of JSON Schema and its C++
    converter has known trouble with nested refs. Pydantic emits `$defs/$ref`
    heavily for nested models, which is exactly what caused the production
    `failed to parse grammar` error. We therefore inline local refs and keep only
    structural generation constraints. The original schema is never mutated and
    remains the authority for Pydantic validation after generation.
    """
    root = copy.deepcopy(schema)

    def project(node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [project(item, stack) for item in node]
        if not isinstance(node, dict):
            return copy.deepcopy(node)

        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in stack:
                raise ValueError(f"circular local schema ref: {ref}")
            target = _resolve_local_ref(root, ref)
            if not isinstance(target, dict):
                raise ValueError(f"schema ref does not target an object: {ref}")
            merged = copy.deepcopy(target)
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = copy.deepcopy(value)
            return project(merged, stack + (ref,))

        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in {"$defs", "definitions"} or key in _LLAMA_SCHEMA_METADATA:
                continue
            if key == "const":
                # Single-value enum is supported by older llama.cpp converters
                # more consistently than JSON-Schema const.
                result["enum"] = [copy.deepcopy(value)]
                continue
            if key == "properties":
                if not isinstance(value, dict):
                    raise ValueError("schema properties must be an object")
                result[key] = {
                    str(name): project(child, stack)
                    for name, child in value.items()
                }
                continue
            if key not in _LLAMA_SCHEMA_KEYS:
                # Length/pattern/title/etc. remain canonical Pydantic rules. They
                # are deliberately not grammar rules because the deployed local
                # llama.cpp build is older and rejected the richer projection.
                continue
            result[key] = project(value, stack)
        return result

    projected = project(root)
    if not isinstance(projected, dict):
        raise ValueError("llama transport schema root must be an object")

    serialized = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    if '"$ref"' in serialized or '"$defs"' in serialized or '"definitions"' in serialized:
        raise ValueError("llama transport schema still contains refs/definitions")
    return projected


def llama_response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Build llama.cpp response_format from the transport-safe schema projection."""
    return {
        "type": "json_object",
        "schema": llama_transport_schema(schema),
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
                "structured_output_transport": "llama.cpp-response_format-flat-structural-schema-v2",
                "output_kind": output_kind,
            }
            if finish_reason == "length":
                if not aggressive:
                    continue
                error = RuntimeError(
                    "Qwen 严格结构输出连续两次达到 token 上限；"
                    "已拒绝把截断 JSON 交给业务层"
                )
                error.llm_metrics = metrics
                raise error

            content = service._extract_content(body)
            return content, response_model, metrics
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response is not None and exc.response.status_code in {400, 422}:
                error = RuntimeError(
                    "本机 llama.cpp 已收到 response_format，但仍拒绝平台投影后的"
                    "扁平结构 Schema；这是本地 grammar 转换兼容错误，不再归因于"
                    "业务 Schema。"
                    f" server={exc.response.text[:1200]}"
                )
                error.llm_metrics = {
                    "usage": {},
                    "timings": {},
                    "request_attempts": total_attempts,
                    "request_retries": total_attempts - 1,
                    "structured_output": True,
                    "structured_output_transport": "llama.cpp-response_format-flat-structural-schema-v2",
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

    error = RuntimeError(f"Qwen 结构化请求失败：{last_error}")
    error.llm_metrics = {
        "usage": {},
        "timings": {},
        "request_attempts": total_attempts,
        "request_retries": max(0, total_attempts - 1),
        "structured_output": True,
        "structured_output_transport": "llama.cpp-response_format-flat-structural-schema-v2",
    }
    raise error


def install_llama_structured_output(director: DirectorService) -> dict[str, Any]:
    """Install provider-adapted schema-constrained decoding.

    ProfessionalOutputRegistry/Pydantic owns canonical semantics. llama.cpp only
    receives a flattened structural projection that its GBNF converter can parse.
    """
    if getattr(DirectorService, "_xiaoduan_llama_structured_output_installed", False):
        return {
            "status": "already_installed",
            "policy": "llama_cpp_json_schema_v2",
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
        "policy": "llama_cpp_json_schema_v2",
        "transport": "response_format.flat_structural_schema",
        "canonical_schema_owner": "professional_output_registry+pydantic",
        "unconstrained_fallback": False,
        "truncation_retry": True,
        "repair_reuses_raw_output": False,
    }


__all__ = [
    "build_structured_payload",
    "install_llama_structured_output",
    "llama_response_format",
    "llama_transport_schema",
    "output_kind_from_schema",
    "response_schema_from_call",
]
