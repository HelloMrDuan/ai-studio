from __future__ import annotations

import copy
import contextvars
import base64
import hashlib
import json
import math
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

# Cumulative lineage: 2.39.6.1-stage04-shot-state-closure
# Installer baseline: 2.39.6.2-stage04-narrative-lineage-closure
VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
CONTRACT_VERSION = "strict-shot-v2"
SHOT_BATCH_SIZE = 2

_SHOT_STATE_FIELDS = (
    "representative_state",
    "video_start_state",
    "video_end_state",
)

_SHOT_TEMPORAL_STATE_FIELDS = (
    "video_start_state",
    "representative_state",
    "video_end_state",
)

_SHOT_PROMPT_FIELDS = (
    "video_start_prompt",
    "image_prompt",
    "video_prompt",
)

_TEMPORAL_MODES = {
    "observable_transition",
    "static_outcome",
    "insufficient_visual_evidence",
}

_TEMPORAL_MODE_CONTRACT_MARKER = (
    "<enum:observable_transition|static_outcome|insufficient_visual_evidence>"
)

_STATIC_PRESENTATION_FIELDS = (
    "visual_start_frame",
    "representative_frame",
    "visual_end_frame",
)


class Stage04RepairInvariantError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.metadata = copy.deepcopy(metadata or {})


class Stage04ShotRepairError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.metadata = copy.deepcopy(metadata or {})

_PERF_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("stage04_perf", default=None)
)


def _perf_phase_category(phase: str) -> str:
    value = str(phase or "").lower()
    if "scene_global_audit" in value:
        return "scene_global_audit"
    if "repair" in value or "completion" in value:
        return "repair"
    if "audit" in value:
        return "batch_audit"
    if "anchor_classification" in value:
        return "anchor_classification"
    if "beat_grouping" in value or "membership" in value:
        return "beat_grouping"
    if (
        "adjacent_beat" in value
        or "same_unit" in value
        or "forward_overlap" in value
        or "boundary" in value
    ):
        return "adjacent_beat_reconcile"
    if "evidence" in value and "select" in value:
        return "evidence_selector"
    if "shot_generation" in value or "missing_beat" in value:
        return "shot_generation"
    return "other"


def _perf_number(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _perf_tokens(metrics: dict[str, Any]) -> tuple[int, int, str]:
    usage = metrics.get("usage") if isinstance(metrics, dict) else {}
    timings = metrics.get("timings") if isinstance(metrics, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    timings = timings if isinstance(timings, dict) else {}
    input_tokens = _perf_number(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
    )
    output_tokens = _perf_number(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
    )
    source = "usage" if input_tokens or output_tokens else "unavailable"
    if not input_tokens:
        input_tokens = _perf_number(
            timings.get("prompt_n")
            or timings.get("prompt_tokens")
        )
    if not output_tokens:
        output_tokens = _perf_number(
            timings.get("predicted_n")
            or timings.get("completion_tokens")
        )
    if source == "unavailable" and (input_tokens or output_tokens):
        source = "timings"
    return input_tokens, output_tokens, source


def _perf_server_seconds(metrics: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    timings = metrics.get("timings") if isinstance(metrics, dict) else {}
    timings = timings if isinstance(timings, dict) else {}
    try:
        if timings.get("total_ms") is not None:
            return max(0.0, float(timings["total_ms"]) / 1000.0), dict(timings)
        prompt_ms = float(timings.get("prompt_ms") or 0.0)
        predicted_ms = float(timings.get("predicted_ms") or 0.0)
        if prompt_ms or predicted_ms:
            return max(0.0, (prompt_ms + predicted_ms) / 1000.0), dict(timings)
    except Exception:
        pass
    return 0.0, dict(timings)


def _perf_counter(profile: dict[str, Any], section: str, key: str) -> dict[str, Any]:
    values = profile.setdefault(section, {})
    return values.setdefault(key, {
        "calls": 0,
        "logical_calls": 0,
        "retries": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_seconds": 0.0,
        "server_total_seconds": 0.0,
        "call_seconds": [],
        "server_timings": [],
        "token_sources": [],
    })


def _perf_record_llm(
    *,
    phase: str,
    seconds: float,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    profile = _PERF_CONTEXT.get()
    if not isinstance(profile, dict):
        return
    metrics: dict[str, Any] = {}
    if isinstance(result, dict):
        candidate = result.get("llm_metrics")
        if isinstance(candidate, dict):
            metrics = candidate
    elif isinstance(result, tuple) and result and isinstance(result[0], dict):
        candidate = result[0].get("llm_metrics")
        if isinstance(candidate, dict):
            metrics = candidate
    if error is not None:
        candidate = getattr(error, "llm_metrics", None)
        if isinstance(candidate, dict):
            metrics = candidate
    attempts = max(1, _perf_number(metrics.get("request_attempts")) or 1)
    retries = _perf_number(metrics.get("request_retries"))
    input_tokens, output_tokens, token_source = _perf_tokens(metrics)
    server_seconds, server_timings = _perf_server_seconds(metrics)
    category = _perf_phase_category(phase)
    for section, key in (("phases", str(phase)), ("categories", category)):
        counter = _perf_counter(profile, section, key)
        counter["calls"] += attempts
        counter["logical_calls"] += 1
        counter["retries"] += retries
        counter["input_tokens"] += input_tokens
        counter["output_tokens"] += output_tokens
        counter["total_seconds"] += max(0.0, float(seconds))
        counter["server_total_seconds"] += server_seconds
        counter["call_seconds"].append(max(0.0, float(seconds)))
        if server_timings:
            counter["server_timings"].append(server_timings)
        if token_source not in counter["token_sources"]:
            counter["token_sources"].append(token_source)
    if category == "repair":
        repair = _perf_counter(profile, "repairs", str(phase))
        repair["calls"] += attempts
        repair["logical_calls"] += 1
        repair["retries"] += retries
        repair["input_tokens"] += input_tokens
        repair["output_tokens"] += output_tokens
        repair["total_seconds"] += max(0.0, float(seconds))
        repair["server_total_seconds"] += server_seconds
        repair["call_seconds"].append(max(0.0, float(seconds)))
        if server_timings:
            repair["server_timings"].append(server_timings)
        if token_source not in repair["token_sources"]:
            repair["token_sources"].append(token_source)
    profile["llm_calls"] = int(profile.get("llm_calls") or 0) + attempts
    profile["llm_retries"] = int(profile.get("llm_retries") or 0) + retries
    profile["input_tokens"] = int(profile.get("input_tokens") or 0) + input_tokens
    profile["output_tokens"] = int(profile.get("output_tokens") or 0) + output_tokens
    scene_index = int(profile.get("_current_scene_index") or 0)
    if scene_index > 0:
        scene = profile.setdefault("scene_metrics", {}).setdefault(str(scene_index), {
            "llm_calls": 0, "llm_retries": 0, "repair_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "phase_calls": {},
        })
        scene["llm_calls"] += attempts
        scene["llm_retries"] += retries
        scene["input_tokens"] += input_tokens
        scene["output_tokens"] += output_tokens
        if category == "repair":
            scene["repair_calls"] += attempts
        phase_calls = scene.setdefault("phase_calls", {})
        phase_calls[str(phase)] = int(phase_calls.get(str(phase)) or 0) + attempts


def _perf_observe(event: str, seconds: float, **details: Any) -> None:
    profile = _PERF_CONTEXT.get()
    if not isinstance(profile, dict):
        return
    elapsed = max(0.0, float(seconds))
    if event == "scene":
        scene_index = int(details.get("scene_index") or 0)
        scene_metrics = copy.deepcopy(
            (profile.get("scene_metrics") or {}).get(str(scene_index)) or {}
        )
        profile.setdefault("scenes", []).append({
            **details,
            **scene_metrics,
            "total_seconds": elapsed,
        })
        return
    if event == "anchor_classification_batch":
        counter = _perf_counter(profile, "categories", "anchor_classification")
        counter["batch_count"] = int(counter.get("batch_count") or 0) + 1
        counter["batch_total_seconds"] = float(
            counter.get("batch_total_seconds") or 0.0
        ) + elapsed
        counter.setdefault("batch_seconds", []).append(elapsed)
        counter.setdefault("batch_details", []).append({**details, "seconds": elapsed})
        return
    if event == "shot_batch":
        counter = _perf_counter(profile, "categories", "shot_generation")
        counter["batch_count"] = int(counter.get("batch_count") or 0) + 1
        counter["batch_total_seconds"] = float(
            counter.get("batch_total_seconds") or 0.0
        ) + elapsed
        counter.setdefault("batch_seconds", []).append(elapsed)
        counter.setdefault("batch_details", []).append({**details, "seconds": elapsed})
        return
    counter = _perf_counter(profile, "categories", event)
    counter["calls"] += 1
    counter["logical_calls"] += 1
    counter["total_seconds"] += elapsed
    counter["call_seconds"].append(elapsed)
    counter.setdefault("details", []).append(dict(details))


def _perf_contract_cached() -> bool:
    profile = _PERF_CONTEXT.get()
    return bool(
        isinstance(profile, dict)
        and profile.get("qwen_contract_verified")
        and profile.get("_workspace_guard_active")
    )


def _perf_finalize(profile: dict[str, Any], total_seconds: float) -> dict[str, Any]:
    profile["total_seconds"] = max(0.0, float(total_seconds))
    for category in (
        "anchor_extraction",
        "anchor_classification",
        "beat_grouping",
        "adjacent_beat_reconcile",
        "evidence_selector",
        "shot_generation",
        "repair",
        "batch_audit",
        "scene_global_audit",
        "other",
    ):
        _perf_counter(profile, "categories", category)
    for section in ("phases", "categories", "repairs"):
        for counter in (profile.get(section) or {}).values():
            calls = int(counter.get("calls") or 0)
            counter["avg_seconds"] = (
                float(counter.get("total_seconds") or 0.0) / calls
                if calls else 0.0
            )
            counter["server_avg_seconds"] = (
                float(counter.get("server_total_seconds") or 0.0) / calls
                if calls else 0.0
            )
    anchor = (profile.get("categories") or {}).get("anchor_classification") or {}
    anchor["business_retries"] = max(
        0,
        int(anchor.get("logical_calls") or 0)
        - int(anchor.get("batch_count") or 0),
    )
    anchor["retry_count"] = (
        int(anchor.get("retries") or 0)
        + int(anchor.get("business_retries") or 0)
    )

    def rounded(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 6)
        if isinstance(value, dict):
            return {key: rounded(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rounded(item) for item in value]
        return value

    return rounded(copy.deepcopy(profile))


def _perf_print(profile: dict[str, Any]) -> None:
    categories = profile.get("categories") or {}
    scenes = profile.get("scenes") or []
    scene_fields = " ".join(
        f"scene_{index}={float(item.get('total_seconds') or 0.0):.3f}s"
        for index, item in enumerate(scenes, 1)
    )
    print("STAGE04_PERF " + json.dumps(profile, ensure_ascii=False, separators=(",", ":")), flush=True)
    print(
        "STAGE04_PERF_SUMMARY "
        f"workspace_start={float(profile.get('workspace_start_seconds') or 0.0):.3f}s "
        f"qwen_ready_wait={float(profile.get('qwen_ready_wait_seconds') or 0.0):.3f}s "
        f"{scene_fields} "
        f"llm_calls={int(profile.get('llm_calls') or 0)} "
        f"llm_retries={int(profile.get('llm_retries') or 0)} "
        f"anchor_extraction={float((categories.get('anchor_extraction') or {}).get('total_seconds') or 0.0):.3f}s "
        f"anchor_classification={float((categories.get('anchor_classification') or {}).get('batch_total_seconds') or (categories.get('anchor_classification') or {}).get('total_seconds') or 0.0):.3f}s "
        f"beat_grouping={float((categories.get('beat_grouping') or {}).get('total_seconds') or 0.0):.3f}s "
        f"adjacent_beat_reconcile={float((categories.get('adjacent_beat_reconcile') or {}).get('total_seconds') or 0.0):.3f}s "
        f"evidence_selector={float((categories.get('evidence_selector') or {}).get('total_seconds') or 0.0):.3f}s "
        f"shot_generation={float((categories.get('shot_generation') or {}).get('batch_total_seconds') or (categories.get('shot_generation') or {}).get('total_seconds') or 0.0):.3f}s "
        f"repair={float((categories.get('repair') or {}).get('total_seconds') or 0.0):.3f}s "
        f"batch_audit={float((categories.get('batch_audit') or {}).get('total_seconds') or 0.0):.3f}s "
        f"scene_global_audit={float((categories.get('scene_global_audit') or {}).get('total_seconds') or 0.0):.3f}s "
        f"total={float(profile.get('total_seconds') or 0.0):.3f}s",
        flush=True,
    )
    for phase, row in sorted((profile.get("phases") or {}).items()):
        print(
            "STAGE04_PERF_PHASE "
            + json.dumps({"phase": phase, **row}, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )


def _j(env: dict[str, Any]):
    return env.get("_studio_json") or json


def _cut(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _id_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        key = str(item or "").strip()
        if key and key not in out:
            out.append(key)
    return out


def _orders(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            n = int(item)
        except Exception:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def _parse_object(env: dict[str, Any], raw: Any, parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict):
        return parsed
    extractor = env.get("_studio_v2372_extract_object")
    if callable(extractor):
        try:
            obj = extractor(raw, parsed)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    texts: list[str] = []
    if isinstance(raw, str):
        texts.append(raw)
    elif isinstance(raw, dict):
        for key in ("content", "text", "output", "response", "raw"):
            value = raw.get(key)
            if isinstance(value, str):
                texts.append(value)
    for text in texts:
        clean = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
        clean = re.sub(r"\s*```$", "", clean)
        try:
            obj = json.loads(clean)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        start, end = clean.find("{"), clean.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(clean[start:end + 1])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
    return {}


_SHOT_SHAPE_KEYS = {
    "summary",
    "action",
    "representative_state",
    "video_start_state",
    "video_end_state",
    "duration_seconds",
    "covered_beat_orders",
    "source_evidence_ids",
    "character_entity_ids",
    "prop_entity_ids",
}


def _looks_like_shot_object(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {str(key) for key in value.keys()}
    hits = len(keys.intersection(_SHOT_SHAPE_KEYS))
    state_hits = sum(
        1
        for key in (
            "representative_state",
            "video_start_state",
            "video_end_state",
        )
        if key in keys
    )
    return hits >= 3 or (state_hits >= 1 and hits >= 2)


def _collect_shot_objects(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 7:
        return []
    result: list[dict[str, Any]] = []

    if isinstance(value, list):
        for item in value:
            if _looks_like_shot_object(item):
                result.append(copy.deepcopy(item))
            else:
                result.extend(_collect_shot_objects(item, depth=depth + 1))
        return result

    if not isinstance(value, dict):
        return []

    rows = value.get("shots")
    if isinstance(rows, list):
        for row in rows:
            if _looks_like_shot_object(row):
                result.append(copy.deepcopy(row))

    singular = value.get("shot")
    if _looks_like_shot_object(singular):
        result.append(copy.deepcopy(singular))

    if _looks_like_shot_object(value):
        result.append(copy.deepcopy(value))

    for key, child in value.items():
        if key in {"shots", "shot"}:
            continue
        if isinstance(child, (dict, list)):
            result.extend(_collect_shot_objects(child, depth=depth + 1))

    return result


def _collect_response_texts(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 7:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_collect_response_texts(child, depth=depth + 1))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_collect_response_texts(child, depth=depth + 1))
        return result
    return []


def _parse_json_text_candidate(text: str) -> Any:
    clean = str(text or "").strip()
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.S | re.I).strip()
    clean = re.sub(r"^\s*```(?:json)?\s*", "", clean, flags=re.I)
    clean = re.sub(r"\s*```\s*$", "", clean)
    variants = [clean, re.sub(r",\s*([}\]])", r"\1", clean)]
    for value in variants:
        try:
            return json.loads(value)
        except Exception:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = clean.find(opener)
        end = clean.rfind(closer)
        if start >= 0 and end > start:
            fragment = clean[start:end + 1]
            try:
                return json.loads(fragment)
            except Exception:
                pass
    return None


def _dedupe_shot_objects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            signature = json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            signature = repr(row)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(row)
    return result


def _extract_shots(
    env: dict[str, Any],
    raw: Any,
    parsed: Any,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    candidates.extend(_collect_shot_objects(parsed))
    candidates.extend(_collect_shot_objects(raw))

    extractor = env.get("_studio_v2371h_extract_shots_any")
    if callable(extractor):
        for value in (parsed, raw):
            try:
                rows = extractor(value)
                if isinstance(rows, list):
                    candidates.extend(
                        row
                        for row in rows
                        if _looks_like_shot_object(row)
                    )
            except Exception:
                pass

    text_extractor = env.get("_studio_v2371h_extract_shots_from_text")

    texts = []
    texts.extend(_collect_response_texts(raw))
    texts.extend(_collect_response_texts(parsed))

    collect = env.get("_studio_v2371a_collect_texts")
    if callable(collect):
        try:
            extra = collect(raw)
            if isinstance(extra, list):
                texts.extend(
                    str(value)
                    for value in extra
                    if isinstance(value, str) and value.strip()
                )
        except Exception:
            pass

    seen_texts: set[str] = set()

    for text in texts:
        if text in seen_texts:
            continue
        seen_texts.add(text)

        if callable(text_extractor):
            try:
                rows = text_extractor(text)
                if isinstance(rows, list):
                    candidates.extend(
                        row
                        for row in rows
                        if _looks_like_shot_object(row)
                    )
            except Exception:
                pass

        parsed_text = _parse_json_text_candidate(text)
        if parsed_text is not None:
            candidates.extend(_collect_shot_objects(parsed_text))

    obj = _parse_object(env, raw, parsed)
    candidates.extend(_collect_shot_objects(obj))

    return _dedupe_shot_objects(candidates)




def _contract_shape_value(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(child_key): _contract_shape_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        if not value:
            return []
        return [_contract_shape_value(value[0], key=key)]
    if key == "temporal_mode":
        return _TEMPORAL_MODE_CONTRACT_MARKER
    if key in {"source_evidence_ids", "evidence_ids", "source_ids"}:
        return "<allowed_evidence_id>"
    if key in {"covered_beat_orders", "beat_orders"}:
        return "<current_target_beat_integer>"
    if isinstance(value, bool):
        return "<boolean>"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "<number>"
    if value is None:
        return "<null>"
    return "<string>"


def _visible_output_contract(contract: str) -> str:
    raw = str(contract or "").strip()
    if not raw:
        return "{}"
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    return json.dumps(
        _contract_shape_value(parsed),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _structured_response_diagnostic(raw: Any, parsed: Any) -> str:
    parts: list[str] = []
    if isinstance(parsed, dict):
        parts.append(
            "parsed_keys="
            + repr(sorted(str(k) for k in parsed.keys())[:20])
        )
    elif isinstance(parsed, list):
        parts.append("parsed=list[" + str(len(parsed)) + "]")
    else:
        parts.append("parsed_type=" + type(parsed).__name__)

    texts: list[str] = []
    if isinstance(raw, str):
        texts.append(raw)
    elif isinstance(raw, dict):
        for key in ("content", "text", "output", "response", "raw"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
                break
    clean = re.sub(r"\s+", " ", (texts[0] if texts else "").strip())
    if clean:
        parts.append("raw_preview=" + repr(clean[:320]))
    return " ".join(parts)


async def _qwen(
    env: dict[str, Any],
    *,
    phase: str,
    system_prompt: str,
    prompt: str,
    contract: str,
    max_tokens: int,
    temperature: float = 0.0,
):
    call = env.get("_studio_v2371a_qwen_call")
    if not callable(call):
        raise RuntimeError("V2.39.5: Qwen3-32B structured call 不可用")

    visible_contract = _visible_output_contract(contract)
    strict_system = (
        str(system_prompt or "").rstrip()
        + "\n\n=== STRICT_OUTPUT_CONTRACT ===\n"
        + visible_contract
        + "\n=== END_STRICT_OUTPUT_CONTRACT ===\n"
        + "只返回一个满足上述结构的 JSON 值，不要 Markdown 代码块，不要解释文字。"
        + "<...> 是类型/作用域占位符，不是可复制的字面值。"
        + "其中 <enum:a|b> 表示该字段只能精确返回列出的枚举值之一，禁止自造同义标签。"
        + "所有 evidence ID、Beat order 和数值必须来自当前任务上下文或当前专用规划步骤，"
        + "不得复制合同中的示例值。"
    )

    return await call(
        phase=phase,
        messages=[{"role": "user", "content": prompt}],
        system_prompt=strict_system,
        temperature=temperature,
        max_tokens=max_tokens,
        contract=contract,
    )




def _beat_span(beat: dict[str, Any]) -> tuple[int, int]:
    spans = beat.get("source_evidence_spans") or []
    starts: list[int] = []
    ends: list[int] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        try:
            starts.append(int(span.get("start") or 0))
            ends.append(int(span.get("end") or 0))
        except Exception:
            continue
    return (min(starts) if starts else 0, max(ends) if ends else 0)


def _merge_two_beats(left: dict[str, Any], right: dict[str, Any], *, summary: str, state_change: str) -> dict[str, Any]:
    merged = copy.deepcopy(left)
    merged["summary"] = str(summary or left.get("summary") or right.get("summary") or "").strip()[:700]
    merged["state_change"] = str(state_change or left.get("state_change") or right.get("state_change") or "").strip()[:500]
    for field in ("character_entity_ids", "prop_entity_ids", "source_evidence_ids"):
        values: list[str] = []
        for source in (left, right):
            for value in source.get(field) or []:
                key = str(value or "").strip()
                if key and key not in values:
                    values.append(key)
        merged[field] = values
    for field in ("source_evidence", "source_evidence_spans"):
        values: list[Any] = []
        for source in (left, right):
            for value in source.get(field) or []:
                if value not in values:
                    values.append(copy.deepcopy(value))
        merged[field] = values
    merged["beat_source"] = "qwen3-32b-narrative-backbone+boundary-reconciled"
    return merged


_ADJACENT_BEAT_RELATIONS = {
    "distinct_forward",
    "forward_with_replayed_prefix",
    "right_replays_left",
    "same_unit_split",
}


def _compact_original_beat(
    beat: dict[str, Any],
    *,
    original_order: int,
) -> dict[str, Any]:
    return {
        "original_order":
            original_order,
        "summary":
            str(
                beat.get("summary")
                or ""
            )[:520],
        "state_change":
            str(
                beat.get(
                    "state_change"
                )
                or ""
            )[:520],
        "source_evidence_ids":
            list(
                beat.get(
                    "source_evidence_ids"
                )
                or []
            ),
        "source_evidence":
            [
                str(value)[:900]
                for value in (
                    beat.get(
                        "source_evidence"
                    )
                    or []
                )
            ],
        "source_evidence_spans":
            copy.deepcopy(
                beat.get(
                    "source_evidence_spans"
                )
                or []
            ),
    }


def _raw_text_candidates(
    raw: Any,
) -> list[str]:
    texts: list[str] = []

    if isinstance(
        raw,
        str,
    ):
        texts.append(
            raw
        )

    elif isinstance(
        raw,
        dict,
    ):
        for key in (
            "content",
            "text",
            "output",
            "response",
            "raw",
        ):
            value = raw.get(key)

            if (
                isinstance(
                    value,
                    str,
                )
                and value.strip()
            ):
                texts.append(
                    value
                )

    return texts


def _parse_adjacent_relation(
    env: dict[str, Any],
    raw: Any,
    parsed: Any,
) -> tuple[str | None, str]:
    obj = _parse_object(
        env,
        raw,
        parsed,
    )

    relation = str(
        obj.get("relation")
        or ""
    ).strip()

    reason = str(
        obj.get("reason")
        or ""
    ).strip()[:320]

    if relation in (
        _ADJACENT_BEAT_RELATIONS
    ):
        return (
            relation,
            reason,
        )

    # Safe salvage only for the authoritative enum field.
    for text in _raw_text_candidates(
        raw
    ):
        match = re.search(
            r'["\']relation["\']\s*:\s*'
            r'["\']('
            r'distinct_forward|'
            r'forward_with_replayed_prefix|'
            r'right_replays_left|'
            r'same_unit_split'
            r')["\']',
            text,
            flags=re.I,
        )

        if not match:
            continue

        relation = (
            match.group(1)
            .strip()
            .lower()
        )

        reason_match = re.search(
            r'["\']reason["\']\s*:\s*'
            r'["\']([^"\']{0,320})',
            text,
            flags=re.I,
        )

        return (
            relation,
            (
                reason_match.group(1)
                if reason_match
                else ""
            ),
        )

    return (
        None,
        "",
    )




async def _classify_original_adjacent_beats(
    env: dict[str, Any],
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    left_order: int,
    right_order: int,
) -> dict[str, Any]:
    """
    Classify ORIGINAL adjacent Beats only. No mutation and no merged text.
    """
    system_prompt = (
        "你是 Narrative Backbone 原始相邻 Beat 关系分类器。"
        "这里只做关系分类，绝对不要生成合并后的 Beat 文本。"
        "只能从四个 relation 中选择一个："
        "distinct_forward：RIGHT 从 LEFT 完成后的状态开始，且只描述新的独立状态变化；"
        "forward_with_replayed_prefix：RIGHT 既重复了 LEFT 已完成的起点/过程/结果，"
        "又在重复之后包含真正新的独立后续结果；"
        "right_replays_left：RIGHT 的主要结果已经由 LEFT 完成，RIGHT 没有新的独立结果；"
        "same_unit_split：LEFT/RIGHT 是同一个不可分割状态变化被错误拆成两段，"
        "两段都包含完成该单元所必需的不同事实。"
        "只依据两个 ORIGINAL Beat 自己的 summary/state_change/精确 source_evidence 判断。"
        "不得依据题材关键词、人物名词表或固定故事模板。"
        "如果 RIGHT 有新结果但它仍从 LEFT 已完成之前的状态重新叙述，"
        "必须判 forward_with_replayed_prefix，不能判 distinct_forward。"
        "reason 最多 120 个汉字。"
        "只返回 JSON：relation + reason。"
    )

    prompt = (
        "=== ORIGINAL_LEFT_BEAT ===\n"
        + json.dumps(
            _compact_original_beat(
                left,
                original_order=
                    left_order,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ORIGINAL_RIGHT_BEAT ===\n"
        + json.dumps(
            _compact_original_beat(
                right,
                original_order=
                    right_order,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "v2395_adjacent_beat_relation_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_RETRY："
                        "只返回 relation 和 reason；"
                        "尤其区分纯前向和“重复前缀+新后续”。"
                    )
                )
            ),
            contract=(
                '{"relation":"distinct_forward",'
                '"reason":""}'
            ),
            max_tokens=240,
            temperature=0.0,
        )

        relation, reason = (
            _parse_adjacent_relation(
                env,
                raw,
                parsed,
            )
        )

        if relation:
            return {
                "left_original_order":
                    left_order,
                "right_original_order":
                    right_order,
                "relation":
                    relation,
                "reason":
                    reason,
            }

        diagnostics.append(
            "attempt="
            + str(
                attempt + 1
            )
            + " "
            + _structured_response_diagnostic(
                raw,
                parsed,
            )
        )

    raise RuntimeError(
        "V2.39.5: 原始相邻 Beat 关系分类失败；"
        f"left={left_order} right={right_order} "
        + " | ".join(
            diagnostics
        )
    )




def _merge_group_lineage(
    beats: list[dict[str, Any]],
    *,
    summary: str,
    state_change: str,
    relation_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not beats:
        raise RuntimeError(
            "V2.39.5: 空 Beat merge group"
        )

    merged = copy.deepcopy(
        beats[0]
    )

    # Remove stale per-edge metadata from source Beats before writing the new
    # reconciliation record.
    merged.pop(
        "adjacent_forward_to_next",
        None,
    )
    merged.pop(
        "adjacent_reconcile",
        None,
    )

    for right in beats[1:]:
        merged = _merge_two_beats(
            merged,
            right,
            summary=summary,
            state_change=
                state_change,
        )

    merged["summary"] = str(
        summary
        or merged.get("summary")
        or ""
    ).strip()[:700]

    merged["state_change"] = str(
        state_change
        or merged.get(
            "state_change"
        )
        or ""
    ).strip()[:500]

    merged["beat_source"] = (
        "qwen3-32b-narrative-backbone"
        "+non-mutating-adjacent-reconciled"
    )

    merged["adjacent_reconcile"] = {
        "relation":
            "group_merge",
        "original_orders": [
            int(
                row.get("order")
                or 0
            )
            for row in beats
        ],
        "edge_relations":
            copy.deepcopy(
                relation_rows
            ),
        "runtime_version":
            VERSION,
    }

    return merged


async def _synthesize_same_unit_group(
    env: dict[str, Any],
    *,
    beats: list[dict[str, Any]],
    relation_rows: list[dict[str, Any]],
) -> tuple[str, str]:
    """
    Synthesis is a separate bounded call, only for same_unit_split groups.
    It never participates in relation classification.
    """
    original_rows = [
        _compact_original_beat(
            beat,
            original_order=
                int(
                    beat.get("order")
                    or index
                ),
        )
        for index, beat in enumerate(
            beats,
            1,
        )
    ]

    system_prompt = (
        "你是 Narrative Backbone 同一叙事单元合并器。"
        "RELATION_EDGES 已经确认这些 ORIGINAL_BEATS 中至少存在 same_unit_split。"
        "只根据 ORIGINAL_BEATS 的精确 source_evidence，"
        "生成一个简洁 merged_summary 和 merged_state_change。"
        "不得加入证据没有的解释、未来事件或更早历史。"
        "merged_summary 最多 260 个汉字；merged_state_change 最多 220 个汉字。"
        "不要复述整段故事历史。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== ORIGINAL_BEATS ===\n"
        + json.dumps(
            original_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== RELATION_EDGES ===\n"
        + json.dumps(
            relation_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "v2394_same_unit_synthesis_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_RETRY："
                        "summary<=260汉字，state_change<=220汉字，"
                        "只写当前 ORIGINAL_BEATS 的事实。"
                    )
                )
            ),
            contract=(
                '{"merged_summary":"",'
                '"merged_state_change":""}'
            ),
            max_tokens=420,
            temperature=0.0,
        )

        obj = _parse_object(
            env,
            raw,
            parsed,
        )

        summary = str(
            obj.get(
                "merged_summary"
            )
            or ""
        ).strip()

        state_change = str(
            obj.get(
                "merged_state_change"
            )
            or ""
        ).strip()

        if (
            summary
            and state_change
            and len(summary) <= 260
            and len(state_change) <= 220
        ):
            return (
                summary,
                state_change,
            )

        diagnostics.append(
            "attempt="
            + str(
                attempt + 1
            )
            + " summary_len="
            + str(
                len(summary)
            )
            + " state_len="
            + str(
                len(state_change)
            )
            + " "
            + _structured_response_diagnostic(
                raw,
                parsed,
            )
        )

    raise RuntimeError(
        "V2.39.5: same_unit Beat 合并文本连续越界/不完整；"
        + " | ".join(
            diagnostics
        )
    )


def _project_exact_novel_evidence(
    right: dict[str, Any],
    novel_evidence: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    texts = [
        str(value or "")
        for value in (
            right.get("source_evidence")
            or []
        )
    ]

    spans = [
        span
        for span in (
            right.get("source_evidence_spans")
            or []
        )
        if isinstance(span, dict)
    ]

    if not texts or len(texts) != len(spans):
        raise RuntimeError(
            "V2.39.5: forward-overlap projection "
            "缺少一一对应的 RIGHT evidence text/span"
        )

    projected_spans: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()

    for item in novel_evidence or []:
        if not isinstance(item, dict):
            continue

        try:
            evidence_item = int(
                item.get("evidence_item")
                or 0
            )
        except Exception:
            evidence_item = 0

        quote = str(
            item.get("quote")
            or ""
        )

        if not (
            1 <= evidence_item <= len(texts)
            and quote
        ):
            raise RuntimeError(
                "V2.39.5: forward-overlap projection "
                "novel_evidence 结构非法"
            )

        source_text = texts[
            evidence_item - 1
        ]

        source_span = spans[
            evidence_item - 1
        ]

        occurrences: list[int] = []
        cursor = 0

        while True:
            pos = source_text.find(
                quote,
                cursor,
            )

            if pos < 0:
                break

            occurrences.append(
                pos
            )

            cursor = (
                pos
                + max(
                    1,
                    len(quote),
                )
            )

        if len(occurrences) != 1:
            raise RuntimeError(
                "V2.39.5: forward-overlap projection "
                f"quote 必须在 RIGHT evidence#{evidence_item} 中唯一出现；"
                f"count={len(occurrences)} quote={quote[:120]!r}"
            )

        try:
            base_start = int(
                source_span.get("start")
            )
            base_end = int(
                source_span.get("end")
            )
        except Exception as exc:
            raise RuntimeError(
                "V2.39.5: forward-overlap projection "
                "RIGHT evidence span 缺少精确 offset"
            ) from exc

        if (
            base_start < 0
            or base_end <= base_start
        ):
            raise RuntimeError(
                "V2.39.5: forward-overlap projection "
                "RIGHT evidence span 非法"
            )

        start = (
            base_start
            + occurrences[0]
        )
        end = (
            start
            + len(quote)
        )

        if end > base_end:
            raise RuntimeError(
                "V2.39.5: forward-overlap projection "
                "quote 越出 RIGHT evidence span"
            )

        key = (
            start,
            end,
            quote,
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        projected_spans.append({
            "start":
                start,
            "end":
                end,
            "text":
                quote,
        })

    if not projected_spans:
        raise RuntimeError(
            "V2.39.5: forward-overlap projection "
            "没有可用 novel evidence"
        )

    projected_spans.sort(
        key=lambda row: (
            int(row.get("start") or 0),
            int(row.get("end") or 0),
        )
    )

    projected_texts = [
        str(
            row.get("text")
            or ""
        )
        for row in projected_spans
    ]

    return (
        projected_texts,
        projected_spans,
    )


async def _audit_forward_overlap_projection(
    env: dict[str, Any],
    *,
    left: dict[str, Any],
    projected: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = (
        "你是 Narrative Backbone forward-overlap 投影验收器，只审计不改写。"
        "PROJECTED_RIGHT 必须满足："
        "1) no_replayed_completed_state=true：不再重复 LEFT 已完成的起点/过程/结果；"
        "2) has_novel_forward_result=true：仍保留至少一个 LEFT 尚未完成的新后续结果；"
        "3) evidence_entailment_ok=true：summary/state_change 都由 PROJECTED_RIGHT "
        "自己的 exact evidence 直接支持。"
        "不得依据题材关键词。只返回三个 boolean + violations。"
    )

    prompt = (
        "=== LEFT_COMPLETED_BEAT ===\n"
        + json.dumps(
            _compact_original_beat(
                left,
                original_order=
                    int(
                        left.get("order")
                        or 0
                    ),
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PROJECTED_RIGHT ===\n"
        + json.dumps({
            "summary":
                projected.get("summary"),
            "state_change":
                projected.get(
                    "state_change"
                ),
            "source_evidence":
                projected.get(
                    "source_evidence"
                ),
            "source_evidence_spans":
                projected.get(
                    "source_evidence_spans"
                ),
        },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    required = (
        "no_replayed_completed_state",
        "has_novel_forward_result",
        "evidence_entailment_ok",
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "v2395_forward_overlap_projection_audit_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_RETRY："
                        "必须完整返回三个 boolean 和 violations。"
                    )
                )
            ),
            contract=(
                '{"no_replayed_completed_state":true,'
                '"has_novel_forward_result":true,'
                '"evidence_entailment_ok":true,'
                '"violations":[]}'
            ),
            max_tokens=500,
            temperature=0.0,
        )

        obj = _parse_object(
            env,
            raw,
            parsed,
        )

        if all(
            isinstance(
                obj.get(key),
                bool,
            )
            for key in required
        ):
            violations = (
                obj.get("violations")
                if isinstance(
                    obj.get("violations"),
                    list,
                )
                else []
            )

            return {
                **obj,
                "valid":
                    all(
                        obj.get(key)
                        is True
                        for key in required
                    )
                    and not violations,
                "violations":
                    violations,
            }

        diagnostics.append(
            "attempt="
            + str(
                attempt + 1
            )
            + " "
            + _structured_response_diagnostic(
                raw,
                parsed,
            )
        )

    return {
        "valid": False,
        "violations": [
            "projection audit schema incomplete: "
            + " | ".join(
                diagnostics
            )
        ],
    }


async def _project_forward_overlap_right(
    env: dict[str, Any],
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    left_order: int,
    right_order: int,
) -> dict[str, Any]:
    """V2.39.10.6_FORWARD_PROJECTION_EVIDENCE_ID"""
    spans = right.get("source_evidence_spans") or []
    right_texts = [str(x or "") for x in (right.get("source_evidence") or [])]

    if (
        not right_texts
        or len(right_texts) != len(spans)
        or not all(isinstance(span, dict) for span in spans)
    ):
        raise RuntimeError(
            f"V2.39.10.6: Beat {right_order} "
            "forward projection 缺少一一对应的 RIGHT evidence text/span"
        )

    projection_candidates: list[dict[str, Any]] = []
    candidate_map: dict[str, dict[str, Any]] = {}
    boundary_chars = set("，。！？；：!?;,")
    closing_chars = set("”’」』》〉\\\"'")

    def _append_candidate(
        evidence_item: int,
        source_text: str,
        source_span: dict[str, Any],
        local_start: int,
        local_end: int,
        sequence: int,
    ) -> int:
        if local_end <= local_start:
            return sequence

        text = source_text[local_start:local_end]
        if not text.strip():
            return sequence

        try:
            base_start = int(source_span.get("start"))
            base_end = int(source_span.get("end"))
        except Exception as exc:
            raise RuntimeError(
                "V2.39.10.6: RIGHT evidence span 缺少精确 offset"
            ) from exc

        absolute_start = base_start + local_start
        absolute_end = base_start + local_end

        if (
            base_start < 0
            or base_end <= base_start
            or absolute_start < base_start
            or absolute_end > base_end
        ):
            raise RuntimeError(
                "V2.39.10.6: candidate 越出 RIGHT evidence span；"
                f"item={evidence_item} "
                f"local=({local_start},{local_end}) "
                f"base=({base_start},{base_end})"
            )

        sequence += 1
        cid = f"E{evidence_item}S{sequence}"

        row = {
            "id": cid,
            "evidence_item": evidence_item,
            "text": text,
            "start": absolute_start,
            "end": absolute_end,
        }

        projection_candidates.append(row)
        candidate_map[cid] = row
        return sequence

    for evidence_item, (source_text, source_span) in enumerate(
        zip(right_texts, spans),
        1,
    ):
        sequence = 0
        segment_start = 0
        cursor = 0

        while cursor < len(source_text):
            if source_text[cursor] in boundary_chars:
                segment_end = cursor + 1

                while (
                    segment_end < len(source_text)
                    and source_text[segment_end] in closing_chars
                ):
                    segment_end += 1

                local = segment_start
                while segment_end - local > 180:
                    split_end = min(local + 160, segment_end)
                    sequence = _append_candidate(
                        evidence_item,
                        source_text,
                        source_span,
                        local,
                        split_end,
                        sequence,
                    )
                    local = split_end

                sequence = _append_candidate(
                    evidence_item,
                    source_text,
                    source_span,
                    local,
                    segment_end,
                    sequence,
                )

                segment_start = segment_end
                cursor = segment_end
                continue

            cursor += 1

        if segment_start < len(source_text):
            local = segment_start
            segment_end = len(source_text)

            while segment_end - local > 180:
                split_end = min(local + 160, segment_end)
                sequence = _append_candidate(
                    evidence_item,
                    source_text,
                    source_span,
                    local,
                    split_end,
                    sequence,
                )
                local = split_end

            sequence = _append_candidate(
                evidence_item,
                source_text,
                source_span,
                local,
                segment_end,
                sequence,
            )

    if not projection_candidates:
        raise RuntimeError(
            f"V2.39.10.6: Beat {right_order} 没有 exact evidence candidate"
        )

    candidate_payload = [
        {
            "id": row["id"],
            "evidence_item": row["evidence_item"],
            "text": row["text"],
        }
        for row in projection_candidates
    ]

    system_prompt = (
        "你是 Narrative Backbone forward-overlap 投影器。"
        "关系分类已经确定：RIGHT 同时包含 LEFT 已完成状态的重复前缀，"
        "以及一个或多个真正新的后续结果。"
        "当前任务不是合并 Beat，而是把 RIGHT 投影成新后续本身。"
        "projected_summary / projected_state_change "
        "只能描述 RIGHT 中尚未由 LEFT 完成的新事实和新状态变化。"
        "不得再次写 LEFT 已经完成的起点、过程或结果。"
        "证据已由 Python 从 RIGHT 原文确定性切成带 ID 的精确候选片段。"
        "你只能选择 novel_evidence_ids，禁止复制、改写或生成证据原文。"
        "只选择直接支持新后续的最小必要候选 ID；允许多个。"
        "不得使用题材关键词规则。只返回严格 JSON。"
    )

    prompt = (
        "=== LEFT_COMPLETED_BEAT ===\n"
        + json.dumps(
            _compact_original_beat(left, original_order=left_order),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== RIGHT_WITH_REPLAYED_PREFIX ===\n"
        + json.dumps(
            _compact_original_beat(right, original_order=right_order),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== EXACT_RIGHT_EVIDENCE_CANDIDATES ===\n"
        + json.dumps(
            candidate_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        projection_phase = (
            "studio_stage04_v2395_forward_overlap_projection_qwen32b"
            if attempt == 0
            else "studio_stage04_v239105_forward_overlap_projection_retry_qwen32b"
        )
        projection_max_tokens = 420 if attempt == 0 else 520

        print(
            "[V2.39.10.6][Stage04][ForwardProjectionID] "
            f"left={left_order} right={right_order} "
            f"attempt={attempt + 1}/2 "
            f"candidates={len(projection_candidates)} "
            f"requested_max_tokens={projection_max_tokens}",
            flush=True,
        )

        raw, parsed, _ = await _qwen(
            env,
            phase=projection_phase,
            system_prompt=system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_RETRY："
                        "上一轮未通过严格验证。"
                        "只返回完整闭合 JSON；"
                        "novel_evidence_ids 只能使用候选中已有的 ID；"
                        "禁止返回 quote/text/evidence 原文；"
                        "summary/state_change 必须简洁，只写 RIGHT 的新后续。"
                    )
                )
            ),
            contract=(
                '{"projected_summary":"",'
                '"projected_state_change":"",'
                '"novel_evidence_ids":["E1S1"]}'
            ),
            max_tokens=projection_max_tokens,
            temperature=0.0,
        )

        obj = _parse_object(env, raw, parsed)
        summary = str(obj.get("projected_summary") or "").strip()
        state_change = str(obj.get("projected_state_change") or "").strip()

        raw_ids = obj.get("novel_evidence_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []

        selected_ids: list[str] = []
        for value in raw_ids:
            key = str(value or "").strip()
            if key and key not in selected_ids:
                selected_ids.append(key)

        if not summary or not state_change or not selected_ids:
            diagnostics.append(
                "attempt="
                + str(attempt + 1)
                + " incomplete "
                + _structured_response_diagnostic(raw, parsed)
            )
            continue

        unknown_ids = [x for x in selected_ids if x not in candidate_map]
        if unknown_ids:
            diagnostics.append(
                "attempt="
                + str(attempt + 1)
                + " unknown_candidate_ids="
                + repr(unknown_ids[:20])
            )
            continue

        selected_rows = [candidate_map[x] for x in selected_ids]
        selected_rows.sort(
            key=lambda row: (
                int(row.get("start") or 0),
                int(row.get("end") or 0),
                str(row.get("id") or ""),
            )
        )

        projected_texts: list[str] = []
        projected_spans: list[dict[str, Any]] = []
        seen_spans: set[tuple[int, int]] = set()

        for row in selected_rows:
            start = int(row["start"])
            end = int(row["end"])
            text = str(row["text"])
            key = (start, end)

            if key in seen_spans:
                continue

            seen_spans.add(key)
            projected_texts.append(text)
            projected_spans.append(
                {
                    "start": start,
                    "end": end,
                    "text": text,
                }
            )

        if not projected_spans:
            diagnostics.append(
                "attempt="
                + str(attempt + 1)
                + " deterministic_materialization_empty"
            )
            continue

        projected = copy.deepcopy(right)
        projected["summary"] = summary[:700]
        projected["state_change"] = state_change[:500]
        projected["source_evidence_ids"] = []
        projected["source_evidence"] = projected_texts
        projected["source_evidence_spans"] = projected_spans
        projected["adjacent_projection"] = {
            "relation": "forward_with_replayed_prefix",
            "previous_original_order": left_order,
            "right_original_order": right_order,
            "continuity_rule": "do_not_replay_previous_completed_state",
            "evidence_selection": "deterministic_candidate_ids",
            "selected_candidate_ids": selected_ids,
            "runtime_patch": "V2.39.10.6",
            "runtime_version": VERSION,
        }

        audit = await _audit_forward_overlap_projection(
            env,
            left=left,
            projected=projected,
        )

        if audit.get("valid"):
            projected["adjacent_projection"]["audit"] = {
                "valid": True,
                "runtime_patch": "V2.39.10.6",
                "runtime_version": VERSION,
            }

            print(
                "[V2.39.10.6][Stage04][ForwardProjectionID] "
                f"left={left_order} right={right_order} "
                f"selected={len(selected_ids)} audit=PASS",
                flush=True,
            )
            return projected

        diagnostics.append(
            "attempt="
            + str(attempt + 1)
            + " audit="
            + json.dumps(
                audit.get("violations") or [],
                ensure_ascii=False,
            )[:700]
        )

    raise RuntimeError(
        "V2.39.10.6: forward-overlap novel suffix ID 投影失败；"
        f"left={left_order} right={right_order} "
        + " | ".join(diagnostics)
    )

# V2.39.9_STAGE04_ADJACENT_BATCH
async def _classify_original_adjacent_beats_batch(
    env: dict[str, Any],
    *,
    beats: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # V2.39.10.4_STAGE04_ADJACENT_MINIBATCH
    #
    # V2.39.9 sent every Scene edge in one request. With 10 edges the payload
    # exceeded the practical Qwen request/context envelope and returned HTTP
    # 400, after which all 10 edges fell back to serial calls.
    #
    # This implementation keeps the same ORIGINAL-Beat-only contract but uses
    # bounded mini-batches. A mini-batch may partially succeed; ONLY unresolved
    # edges fall back to the existing single-edge classifier.
    original = [
        copy.deepcopy(beat)
        for beat in (beats or [])
        if isinstance(beat, dict)
    ]

    if len(original) <= 1:
        return []

    edge_specs: list[dict[str, Any]] = []

    for index in range(
        len(original) - 1
    ):
        left = original[index]
        right = original[index + 1]

        left_order = int(
            left.get("order")
            or index + 1
        )
        right_order = int(
            right.get("order")
            or index + 2
        )

        edge_specs.append({
            "edge_index": index,
            "left_original_order":
                left_order,
            "right_original_order":
                right_order,
            "left":
                _compact_original_beat(
                    left,
                    original_order=
                        left_order,
                ),
            "right":
                _compact_original_beat(
                    right,
                    original_order=
                        right_order,
                ),
        })

    max_edges_per_batch = 3
    max_payload_chars = 6000

    mini_batches: list[
        list[dict[str, Any]]
    ] = []
    current: list[dict[str, Any]] = []

    for spec in edge_specs:
        trial = [
            *current,
            spec,
        ]

        trial_payload = {
            "edges": [
                {
                    key: value
                    for key, value in row.items()
                    if key != "edge_index"
                }
                for row in trial
            ]
        }

        trial_chars = len(
            json.dumps(
                trial_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        if (
            current
            and (
                len(trial)
                > max_edges_per_batch
                or trial_chars
                > max_payload_chars
            )
        ):
            mini_batches.append(
                current
            )
            current = [spec]
        else:
            current = trial

    if current:
        mini_batches.append(
            current
        )

    decisions: dict[
        int,
        dict[str, Any],
    ] = {}

    total_batch_calls = 0
    total_batch_valid = 0
    total_fallback = 0

    system_prompt = (
        "你是 Narrative Backbone 原始相邻 Beat 关系批量分类器。"
        "每个 EDGES 项都是一对 ORIGINAL Beat。"
        "这里只做关系分类，绝对不要生成合并后的 Beat 文本，也不要跨 edge 推断。"
        "每个 edge 只能从四个 relation 中选择一个："
        "distinct_forward：RIGHT 从 LEFT 完成后的状态开始，且只描述新的独立状态变化；"
        "forward_with_replayed_prefix：RIGHT 既重复 LEFT 已完成的起点/过程/结果，"
        "又在重复之后包含真正新的独立后续结果；"
        "right_replays_left：RIGHT 的主要结果已经由 LEFT 完成，RIGHT 没有新的独立结果；"
        "same_unit_split：LEFT/RIGHT 是同一个不可分割状态变化被错误拆成两段，"
        "两段都包含完成该单元所必需的不同事实。"
        "只依据各自 ORIGINAL Beat 的 summary/state_change/精确 source_evidence 判断。"
        "不得依据题材关键词、人物名词表或固定故事模板。"
        "如果 RIGHT 有新结果但仍从 LEFT 已完成之前的状态重新叙述，"
        "必须判 forward_with_replayed_prefix。"
        "按输入顺序返回 relations；每项必须带 left_original_order、"
        "right_original_order、relation、reason。reason 最多 120 个汉字。"
        "只返回严格 JSON。"
    )

    for mini_index, mini in enumerate(
        mini_batches,
        1,
    ):
        expected_by_pair = {
            (
                int(spec["left_original_order"]),
                int(spec["right_original_order"]),
            ): spec
            for spec in mini
        }

        valid_by_pair: dict[
            tuple[int, int],
            dict[str, Any],
        ] = {}
        conflicted: set[
            tuple[int, int]
        ] = set()
        batch_error = ""

        payload = {
            "edges": [
                {
                    key: value
                    for key, value in spec.items()
                    if key != "edge_index"
                }
                for spec in mini
            ]
        }

        payload_text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        # If one edge alone is already unusually large, do not make a doomed
        # mini-batch request; directly use the proven single-edge path.
        skip_batch = (
            len(mini) == 1
            and len(payload_text)
            > max_payload_chars
        )

        if not skip_batch:
            total_batch_calls += 1

            try:
                raw, parsed, _ = await _qwen(
                    env,
                    phase=(
                        "studio_stage04_"
                        "v239104_adjacent_beat_mini_batch_qwen32b"
                    ),
                    system_prompt=
                        system_prompt,
                    prompt=(
                        "=== ORIGINAL_ADJACENT_EDGES ===\n"
                        + payload_text
                    ),
                    contract=(
                        '{"relations":[{'
                        '"left_original_order":1,'
                        '"right_original_order":2,'
                        '"relation":"distinct_forward",'
                        '"reason":""'
                        '}]}'
                    ),
                    max_tokens=min(
                        420,
                        max(
                            220,
                            120
                            + len(mini) * 90,
                        ),
                    ),
                    temperature=0.0,
                )

                obj = _parse_object(
                    env,
                    raw,
                    parsed,
                )

                rows = obj.get(
                    "relations"
                )

                if not isinstance(
                    rows,
                    list,
                ):
                    rows = []

                for row in rows:
                    if not isinstance(
                        row,
                        dict,
                    ):
                        continue

                    try:
                        pair = (
                            int(
                                row.get(
                                    "left_original_order"
                                )
                                or 0
                            ),
                            int(
                                row.get(
                                    "right_original_order"
                                )
                                or 0
                            ),
                        )
                    except Exception:
                        continue

                    relation = str(
                        row.get("relation")
                        or ""
                    ).strip()

                    reason = str(
                        row.get("reason")
                        or ""
                    ).strip()[:320]

                    if (
                        pair not in expected_by_pair
                        or relation not in (
                            _ADJACENT_BEAT_RELATIONS
                        )
                    ):
                        continue

                    previous = valid_by_pair.get(
                        pair
                    )

                    if (
                        previous is not None
                        and previous.get("relation")
                        != relation
                    ):
                        conflicted.add(
                            pair
                        )
                        continue

                    valid_by_pair[pair] = {
                        "left_original_order":
                            pair[0],
                        "right_original_order":
                            pair[1],
                        "relation":
                            relation,
                        "reason":
                            reason,
                    }

                for pair in conflicted:
                    valid_by_pair.pop(
                        pair,
                        None,
                    )

            except Exception as exc:
                batch_error = (
                    type(exc).__name__
                    + ": "
                    + str(exc)[:500]
                )
        else:
            batch_error = (
                "payload_over_soft_limit"
            )

        mini_valid = 0
        mini_fallback = 0

        for spec in mini:
            pair = (
                int(spec["left_original_order"]),
                int(spec["right_original_order"]),
            )

            decision = valid_by_pair.get(
                pair
            )

            if decision is not None:
                decisions[
                    int(spec["edge_index"])
                ] = decision
                mini_valid += 1
                total_batch_valid += 1
                continue

            left = original[
                int(spec["edge_index"])
            ]
            right = original[
                int(spec["edge_index"]) + 1
            ]

            decision = (
                await _classify_original_adjacent_beats(
                    env,
                    left=left,
                    right=right,
                    left_order=
                        int(spec["left_original_order"]),
                    right_order=
                        int(spec["right_original_order"]),
                )
            )

            decisions[
                int(spec["edge_index"])
            ] = decision
            mini_fallback += 1
            total_fallback += 1

        print(
            "[V2.39.10.4][Stage04][AdjacentMiniBatch] "
            f"mini={mini_index}/{len(mini_batches)} "
            f"edges={len(mini)} "
            f"payload_chars={len(payload_text)} "
            f"batch_valid={mini_valid} "
            f"fallback={mini_fallback} "
            f"batch_error={batch_error}",
            flush=True,
        )

    result = [
        decisions[index]
        for index in range(
            len(edge_specs)
        )
        if index in decisions
    ]

    if len(result) != len(edge_specs):
        raise RuntimeError(
            "V2.39.10.4: Adjacent mini-batch classification coverage mismatch；"
            f"expected={len(edge_specs)} actual={len(result)}"
        )

    print(
        "[V2.39.10.4][Stage04][AdjacentMiniBatch] "
        f"edges={len(edge_specs)} "
        f"mini_batches={len(mini_batches)} "
        f"batch_calls={total_batch_calls} "
        f"batch_valid={total_batch_valid} "
        f"fallback={total_fallback}",
        flush=True,
    )

    return result



async def reconcile_beat_boundaries(
    env: dict[str, Any],
    *,
    source: str,
    beats: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    V2.39.5 non-mutating adjacent Beat reconciliation with hybrid-prefix
    projection.

    All relation decisions are made on ORIGINAL Beats first. If RIGHT contains
    both a replayed prefix and a genuinely new suffix, it remains a distinct
    Beat but its evidence/summary/state_change are projected to the novel suffix
    before Shot generation.
    """
    if not isinstance(
        beats,
        list,
    ):
        raise RuntimeError(
            "V2.39.5: Narrative Beat 列表类型异常"
        )

    original = [
        copy.deepcopy(
            beat
        )
        for beat in beats
        if isinstance(
            beat,
            dict,
        )
    ]

    if not original:
        return []

    for index, beat in enumerate(
        original,
        1,
    ):
        if int(
            beat.get("order")
            or 0
        ) <= 0:
            beat["order"] = index

    if len(original) == 1:
        original[0]["order"] = 1
        return original

    last_start = -1

    for index, beat in enumerate(
        original,
        1,
    ):
        start, end = _beat_span(
            beat
        )

        if (
            start > 0
            and start < last_start
        ):
            raise RuntimeError(
                "V2.39.5: 原始 Beat evidence 时间倒退；"
                f"beat={index}"
            )

        if (
            start > 0
            and end > 0
            and end <= start
        ):
            raise RuntimeError(
                "V2.39.5: 原始 Beat evidence span 非法；"
                f"beat={index} span=[{start},{end})"
            )

        if start > 0:
            last_start = start

    # Phase 1: classify every ORIGINAL edge.
    # V2.39.9 batches the semantic classification into one request.
    # Invalid/missing batch decisions fail closed to the historical
    # single-edge Qwen classifier.
    edge_relations = (
        await _classify_original_adjacent_beats_batch(
            env,
            beats=original,
        )
    )

    # Phase 2: project hybrid RIGHT Beats only after all original relation
    # decisions exist. No projected Beat is ever re-classified.
    materialized = [
        copy.deepcopy(
            beat
        )
        for beat in original
    ]

    for edge_index, decision in enumerate(
        edge_relations
    ):
        if (
            str(
                decision.get("relation")
                or ""
            )
            != "forward_with_replayed_prefix"
        ):
            continue

        left = original[
            edge_index
        ]

        right = original[
            edge_index + 1
        ]

        materialized[
            edge_index + 1
        ] = (
            await _project_forward_overlap_right(
                env,
                left=left,
                right=right,
                left_order=
                    int(
                        left.get("order")
                        or edge_index + 1
                    ),
                right_order=
                    int(
                        right.get("order")
                        or edge_index + 2
                    ),
            )
        )

        decision[
            "projection_applied"
        ] = True

    # Phase 3: convert edge decisions into merge groups. A hybrid-prefix edge is
    # now a distinct boundary because RIGHT has already been projected to its
    # novel suffix.
    groups: list[
        tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]
    ] = []

    group_start = 0
    group_edges: list[
        dict[str, Any]
    ] = []

    for edge_index, decision in enumerate(
        edge_relations
    ):
        relation = str(
            decision.get("relation")
            or ""
        )

        if relation in {
            "distinct_forward",
            "forward_with_replayed_prefix",
        }:
            groups.append((
                materialized[
                    group_start:
                    edge_index + 1
                ],
                copy.deepcopy(
                    group_edges
                ),
            ))

            group_start = (
                edge_index + 1
            )

            group_edges = []

            continue

        if relation not in (
            _ADJACENT_BEAT_RELATIONS
        ):
            raise RuntimeError(
                "V2.39.5: 未知 adjacent relation："
                + repr(
                    relation
                )
            )

        group_edges.append(
            copy.deepcopy(
                decision
            )
        )

    groups.append((
        materialized[
            group_start:
        ],
        copy.deepcopy(
            group_edges
        ),
    ))

    # Phase 4: materialize merge groups once.
    result: list[
        dict[str, Any]
    ] = []

    for group_beats, relation_rows in groups:
        if not group_beats:
            continue

        if len(group_beats) == 1:
            kept = copy.deepcopy(
                group_beats[0]
            )

            kept.pop(
                "adjacent_reconcile",
                None,
            )

            result.append(
                kept
            )

            continue

        relations = [
            str(
                row.get("relation")
                or ""
            )
            for row in relation_rows
        ]

        if (
            relation_rows
            and all(
                relation
                == "right_replays_left"
                for relation in relations
            )
        ):
            leftmost = group_beats[
                0
            ]

            merged = (
                _merge_group_lineage(
                    group_beats,
                    summary=
                        str(
                            leftmost.get(
                                "summary"
                            )
                            or ""
                        ),
                    state_change=
                        str(
                            leftmost.get(
                                "state_change"
                            )
                            or ""
                        ),
                    relation_rows=
                        relation_rows,
                )
            )

            merged[
                "adjacent_reconcile"
            ][
                "merge_mode"
            ] = (
                "replay_keep_left_semantics"
            )

            result.append(
                merged
            )

            continue

        summary, state_change = (
            await _synthesize_same_unit_group(
                env,
                beats=
                    group_beats,
                relation_rows=
                    relation_rows,
            )
        )

        merged = _merge_group_lineage(
            group_beats,
            summary=summary,
            state_change=
                state_change,
            relation_rows=
                relation_rows,
        )

        merged[
            "adjacent_reconcile"
        ][
            "merge_mode"
        ] = (
            "same_unit_single_bounded_synthesis"
        )

        result.append(
            merged
        )

    for order, beat in enumerate(
        result,
        1,
    ):
        beat["order"] = order

    last_start = -1

    for index, beat in enumerate(
        result,
        1,
    ):
        start, end = _beat_span(
            beat
        )

        if (
            start > 0
            and start < last_start
        ):
            raise RuntimeError(
                "V2.39.5: reconcile 后 Beat evidence 时间倒退；"
                f"beat={index}"
            )

        if (
            start > 0
            and end > 0
            and end <= start
        ):
            raise RuntimeError(
                "V2.39.5: reconcile 后 Beat evidence span 非法；"
                f"beat={index} span=[{start},{end})"
            )

        if start > 0:
            last_start = start

    return result










def _build_entity_context(env: dict[str, Any], project_id: str, allowed_chars: set[str], allowed_props: set[str]) -> list[dict[str, str]]:
    director = env["director"]
    entities = {
        str(row.get("entity_id") or ""): row
        for row in director.production.list_entities(project_id)
        if str(row.get("entity_id") or "")
    }
    rows: list[dict[str, str]] = []
    for entity_id in [*sorted(allowed_chars), *sorted(allowed_props)]:
        row = entities.get(entity_id)
        if not row:
            continue
        rows.append({
            "entity_id": entity_id,
            "entity_type": str(row.get("entity_type") or ""),
            "name": str(row.get("name") or ""),
        })
    return rows


def _compact_beats(
    batch: list[dict[str, Any]],
    beat_to_anchor_ids: dict[int, list[str]],
) -> list[dict[str, Any]]:
    rows: list[
        dict[str, Any]
    ] = []

    for beat in batch:
        order = int(
            beat.get("order")
            or 0
        )

        evidence = list(
            beat_to_anchor_ids.get(
                order
            )
            or []
        )

        if not evidence:
            raise RuntimeError(
                f"V2.39.5: Beat {order} 没有 Shot 可用证据锚点"
            )

        row = {
            "order":
                order,
            "summary":
                str(
                    beat.get("summary")
                    or ""
                )[:320],
            "state_change":
                str(
                    beat.get(
                        "state_change"
                    )
                    or ""
                )[:260],
            "allowed_source_evidence_ids":
                evidence,
            "source_evidence_ids":
                evidence,
            "source_evidence":
                list(
                    beat.get(
                        "source_evidence"
                    )
                    or []
                ),
            "source_evidence_spans":
                list(
                    beat.get(
                        "source_evidence_spans"
                    )
                    or []
                ),
            "character_entity_ids":
                list(
                    beat.get(
                        "character_entity_ids"
                    )
                    or []
                ),
            "prop_entity_ids":
                list(
                    beat.get(
                        "prop_entity_ids"
                    )
                    or []
                ),
        }

        projection = beat.get(
            "adjacent_projection"
        )

        if isinstance(
            projection,
            dict,
        ):
            row[
                "adjacent_projection"
            ] = copy.deepcopy(
                projection
            )

        rows.append(
            row
        )

    return rows




def _beat_evidence_map(
    compact_beats: list[dict[str, Any]],
) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for beat in compact_beats or []:
        if not isinstance(beat, dict):
            continue
        try:
            order = int(beat.get("order") or 0)
        except Exception:
            continue
        if order <= 0:
            continue
        result[order] = set(
            _id_list(
                beat.get("allowed_source_evidence_ids")
                or beat.get("source_evidence_ids")
                or []
            )
        )
    return result


def _covered_orders(
    rows: list[dict[str, Any]],
) -> set[int]:
    return {
        order
        for row in rows or []
        if isinstance(row, dict)
        for order in _orders(
            row.get("covered_beat_orders")
        )
    }


def _semantic_text_key(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(
        r"(?:起始|开始|结束|终止|代表|核心|画面|状态|帧|start|end|representative)\s*[:：]",
        "",
        text,
        flags=re.I,
    )
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _prompt_semantic_key(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if field != "video_prompt":
        return _semantic_text_key(text)

    parts = [
        _semantic_text_key(part)
        for part in re.split(r"\n+|(?:起始|开始|结束|终止)状态\s*[:：]", text)
    ]
    unique = [part for part in dict.fromkeys(parts) if part]
    return "|".join(unique)


def _shot_state_snapshot(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: str(row.get(field) or "").strip()
        for field in _SHOT_TEMPORAL_STATE_FIELDS
    }


def _shot_prompt_snapshot(row: dict[str, Any]) -> dict[str, str]:
    return {
        field: str(row.get(field) or "").strip()
        for field in _SHOT_PROMPT_FIELDS
    }


def _shot_semantic_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "temporal_mode": str(row.get("temporal_mode") or ""),
        "source_fact": _semantic_text_key(row.get("source_fact")),
        "summary": _semantic_text_key(row.get("summary")),
        "action": _semantic_text_key(row.get("action")),
        "states": [
            _semantic_text_key(row.get(field))
            for field in _SHOT_TEMPORAL_STATE_FIELDS
        ],
        "entities": {
            "characters": sorted(_id_list(row.get("character_entity_ids"))),
            "props": sorted(_id_list(row.get("prop_entity_ids"))),
        },
        "presentation": {
            field: _semantic_text_key(row.get(field))
            for field in (
                "narrative_state",
                "visual_realization",
                *_STATIC_PRESENTATION_FIELDS,
                "visual_motion",
            )
        },
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _shot_temporal_mode(row: dict[str, Any]) -> str:
    value = str(row.get("temporal_mode") or "").strip().lower()
    # Backward-compatible dynamic fixtures remain strict. Static/outcome mode
    # is never inferred from text or keywords; it must be explicit model output.
    return value or "observable_transition"


def _normalize_temporal_contract(
    row: dict[str, Any],
    *,
    evidence_ids: list[str],
    raw_index: int,
) -> dict[str, Any]:
    item = copy.deepcopy(row)
    explicit_mode = str(item.get("temporal_mode") or "").strip().lower()
    mode = _shot_temporal_mode(item)
    if mode not in _TEMPORAL_MODES:
        raise RuntimeError(
            f"strict-shot-v2 Shot#{raw_index} temporal_mode 非法：{mode!r}"
        )

    mode_evidence_ids = _id_list(
        item.get("temporal_mode_evidence_ids") or evidence_ids
    )
    if not mode_evidence_ids or not set(mode_evidence_ids).issubset(
        set(evidence_ids)
    ):
        raise RuntimeError(
            f"strict-shot-v2 Shot#{raw_index} temporal mode evidence 越权"
        )
    reason = str(item.get("temporal_mode_reason") or "").strip()
    if explicit_mode and not reason:
        raise RuntimeError(
            f"strict-shot-v2 Shot#{raw_index} temporal_mode 缺少 evidence-based reason"
        )

    item["temporal_mode"] = mode
    item["temporal_mode_evidence_ids"] = mode_evidence_ids
    item["temporal_mode_reason"] = reason or (
        "legacy dynamic contract with three explicit narrative states"
    )

    if mode == "insufficient_visual_evidence":
        raise Stage04ShotRepairError(
            f"Shot#{raw_index} 当前证据被分类为 insufficient_visual_evidence",
            metadata={
                "repair_progress": "needs_regrouping_or_evidence_selection",
                "evidence_sufficiency": "insufficient_visual_evidence",
                "failed_rules": ["visual_realization"],
                "evidence_ids": mode_evidence_ids,
                "temporal_mode": mode,
                "temporal_mode_reason": item["temporal_mode_reason"],
            },
        )

    if mode == "observable_transition":
        item["source_fact"] = str(
            item.get("source_fact") or item.get("summary") or ""
        ).strip()
        item["narrative_start_state"] = str(
            item.get("narrative_start_state")
            or item.get("video_start_state")
            or ""
        ).strip()
        item["narrative_state"] = str(
            item.get("narrative_state")
            or item.get("representative_state")
            or ""
        ).strip()
        item["narrative_end_state"] = str(
            item.get("narrative_end_state")
            or item.get("video_end_state")
            or ""
        ).strip()
        return item

    source_fact = str(item.get("source_fact") or "").strip()
    narrative_state = str(item.get("narrative_state") or "").strip()
    narrative_start = str(
        item.get("narrative_start_state") or narrative_state
    ).strip()
    narrative_end = str(
        item.get("narrative_end_state") or narrative_state
    ).strip()
    visual_realization = str(item.get("visual_realization") or "").strip()
    visual_motion = str(item.get("visual_motion") or "").strip()
    realization_scope = str(item.get("realization_scope") or "").strip()
    missing = [
        key
        for key, value in (
            ("source_fact", source_fact),
            ("narrative_state", narrative_state),
            ("visual_realization", visual_realization),
            ("visual_motion", visual_motion),
            ("realization_scope", realization_scope),
            *((field, str(item.get(field) or "").strip())
              for field in _STATIC_PRESENTATION_FIELDS),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"strict-shot-v2 Shot#{raw_index} static_outcome 合同不完整："
            + ", ".join(missing)
        )
    if realization_scope != "presentation_only":
        raise RuntimeError(
            f"strict-shot-v2 Shot#{raw_index} static_outcome realization_scope "
            "必须为 presentation_only"
        )
    narrative_keys = {
        _semantic_text_key(value)
        for value in (narrative_start, narrative_state, narrative_end)
    }
    if "" in narrative_keys or len(narrative_keys) != 1:
        raise Stage04RepairInvariantError(
            f"strict-shot-v2 Shot#{raw_index} static_outcome 不得伪造 narrative transition",
            metadata={
                "failed_rules": ["visual_realization"],
                "temporal_mode": mode,
            },
        )
    frame_keys = {
        _semantic_text_key(item.get(field))
        for field in _STATIC_PRESENTATION_FIELDS
    }
    if "" in frame_keys or len(frame_keys) != len(_STATIC_PRESENTATION_FIELDS):
        raise Stage04RepairInvariantError(
            f"strict-shot-v2 Shot#{raw_index} static_outcome 表现帧不可区分",
            metadata={
                "failed_rules": ["visual_realization"],
                "temporal_mode": mode,
            },
        )
    assumptions = item.get("realization_assumptions")
    if not isinstance(assumptions, list):
        raise RuntimeError(
            f"strict-shot-v2 Shot#{raw_index} realization_assumptions 必须为数组"
        )

    # For static outcomes, narrative fields are a locked stable fact. Any
    # composition/motion inference stays in presentation-only fields and can
    # never leak into summary/action or masquerade as source evidence.
    item.update({
        "source_fact": source_fact,
        "summary": source_fact,
        "action": "",
        "narrative_start_state": narrative_state,
        "narrative_state": narrative_state,
        "narrative_end_state": narrative_state,
        "video_start_state": narrative_state,
        "representative_state": narrative_state,
        "video_end_state": narrative_state,
        "visual_realization": visual_realization,
        "visual_motion": visual_motion,
        "realization_scope": "presentation_only",
        "realization_assumptions": [
            str(value).strip()
            for value in assumptions
            if str(value or "").strip()
        ],
    })
    return item


def _assert_temporal_state_distinction(
    row: dict[str, Any],
    *,
    context: str,
) -> None:
    mode = _shot_temporal_mode(row)
    values = {
        field: _semantic_text_key(row.get(field))
        for field in _SHOT_TEMPORAL_STATE_FIELDS
    }
    if not all(values.values()):
        return

    if mode == "static_outcome":
        if len(set(values.values())) != 1:
            raise Stage04RepairInvariantError(
                f"{context}: static_outcome narrative state 必须保持稳定",
                metadata={
                    "failed_rules": ["visual_realization"],
                    "post_repair_states": _shot_state_snapshot(row),
                    "repair_progress": "rejected_static_narrative_transition",
                },
            )
        return

    duplicates = [
        (left, right)
        for left, right in (
            ("video_start_state", "representative_state"),
            ("representative_state", "video_end_state"),
            ("video_start_state", "video_end_state"),
        )
        if values[left] == values[right]
    ]
    if duplicates:
        raise Stage04RepairInvariantError(
            f"{context}: strict-shot-v2 三状态没有形成可区分的前向时间链；"
            + ", ".join(f"{left}={right}" for left, right in duplicates),
            metadata={
                "failed_rules": [
                    "no_result_duplication",
                    "causal_order",
                    "representative_state",
                ],
                "post_repair_states": _shot_state_snapshot(row),
                "repair_progress": "rejected_state_collapse",
            },
        )


def _assert_prompt_projection_distinction(
    row: dict[str, Any],
    *,
    context: str,
) -> None:
    values = {
        field: _prompt_semantic_key(field, row.get(field))
        for field in _SHOT_PROMPT_FIELDS
    }
    if all(values.values()) and len(set(values.values())) == 1:
        raise Stage04RepairInvariantError(
            f"{context}: strict-shot-v2 三类 Prompt 语义塌缩为同一内容",
            metadata={
                "failed_rules": ["redundant_representation"],
                "post_repair_prompts": _shot_prompt_snapshot(row),
                "repair_progress": "rejected_prompt_collapse",
            },
        )


def _project_prompts_from_states(
    row: dict[str, Any],
) -> dict[str, Any]:
    """
    strict-shot-v2 deterministic, temporal-mode-aware prompt compiler.

    Observable transitions project from their three narrative states. Static
    outcomes project from a locked stable narrative state plus the separately
    audited presentation-only realization and frames.

    Existing model-authored prompt text is intentionally overwritten so it
    cannot omit fields or introduce unsupported future events / abstractions.
    """
    item = copy.deepcopy(
        row
    )

    if _shot_temporal_mode(item) == "static_outcome":
        narrative_state = str(item.get("narrative_state") or "").strip()
        visual_realization = str(item.get("visual_realization") or "").strip()
        visual_start = str(item.get("visual_start_frame") or "").strip()
        representative_frame = str(item.get("representative_frame") or "").strip()
        visual_end = str(item.get("visual_end_frame") or "").strip()
        visual_motion = str(item.get("visual_motion") or "").strip()
        if narrative_state and representative_frame:
            item["image_prompt"] = (
                "已锁定叙事状态：" + narrative_state
                + "\n表现层代表画面：" + representative_frame
                + ("\n视觉实现：" + visual_realization if visual_realization else "")
            )
        if narrative_state and visual_start:
            item["video_start_prompt"] = (
                "已锁定叙事状态：" + narrative_state
                + "\n表现层起始画面：" + visual_start
            )
        if narrative_state and visual_start and visual_end and visual_motion:
            item["video_prompt"] = (
                "叙事状态保持不变：" + narrative_state
                + "\n表现层起始画面：" + visual_start
                + "\n表现层结束画面：" + visual_end
                + "\n仅允许表现层运动：" + visual_motion
                + "\n禁止新增剧情事件、因果结果、角色或道具。"
            )
        item["prompt_compiler"] = "strict-shot-v2-static-presentation-derived"
        return item

    representative = str(
        item.get("representative_state")
        or ""
    ).strip()

    video_start = str(
        item.get("video_start_state")
        or ""
    ).strip()

    video_end = str(
        item.get("video_end_state")
        or ""
    ).strip()

    if representative:
        item["image_prompt"] = (
            representative
        )

    if video_start:
        item["video_start_prompt"] = (
            video_start
        )

    if video_start and video_end:
        item["video_prompt"] = (
            "起始状态："
            + video_start
            + "\n结束状态："
            + video_end
        )

    item["prompt_compiler"] = (
        "strict-shot-v2-state-derived"
    )

    return item


def compile_prompts_for_locked_shot(row: dict[str, Any]) -> dict[str, str]:
    """Return the deterministic Stage05 prompt projection for a locked Shot."""
    projected = _project_prompts_from_states(row)
    return {
        field: str(projected.get(field) or "").strip()
        for field in _SHOT_PROMPT_FIELDS
    }


def _compile_prompts_from_states(
    row: dict[str, Any],
) -> dict[str, Any]:
    return _project_prompts_from_states(row)


def validate_rows(
    env: dict[str, Any],
    *,
    raw_rows: list[dict[str, Any]],
    compact_beats: list[dict[str, Any]],
    allowed_chars: set[str],
    allowed_props: set[str],
    anchors: list[dict[str, Any]],
    scene_id: str,
    episode_id: str,
) -> list[dict[str, Any]]:
    """
    V2.39.2 strict local Shot contract.

    Invariants:
      - Beat binding must be explicit by this point.
      - source_evidence_ids are the factual authority.
      - source_evidence_spans are derived ONLY from the actually selected
        evidence anchors, never widened to the whole Beat.
      - duration_seconds is real model output and must be valid; there is no
        silent 3-second default.
      - image/video prompts are deterministic derivatives of temporal mode.
      - entity IDs are never inherited from the Beat/Scene and invalid IDs are
        rejected instead of silently erased.
    """
    expected = {
        int(row.get("order") or 0)
        for row in compact_beats or []
        if isinstance(row, dict)
        and int(row.get("order") or 0) > 0
    }

    beat_evidence = _beat_evidence_map(
        compact_beats
    )

    anchor_map = {
        str(row.get("id") or ""): row
        for row in anchors or []
        if isinstance(row, dict)
        and str(row.get("id") or "")
    }

    anchor_ids = set(anchor_map)

    cleaned: list[dict[str, Any]] = []

    for raw_index, original in enumerate(
        raw_rows or [],
        1,
    ):
        if not isinstance(original, dict):
            continue

        row = copy.deepcopy(original)

        beat_orders = _orders(
            row.get("covered_beat_orders")
        )

        if not beat_orders:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 未显式绑定 Beat"
            )

        illegal_orders = (
            set(beat_orders)
            - expected
        )

        if illegal_orders:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 引用当前批次以外 Beat："
                + repr(
                    sorted(
                        illegal_orders
                    )
                )
            )

        evidence_ids = _id_list(
            row.get("source_evidence_ids")
        )

        if not evidence_ids:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 未选择 source_evidence_ids"
            )

        unknown_evidence = [
            key
            for key in evidence_ids
            if key not in anchor_ids
        ]

        if unknown_evidence:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} evidence 不在当前批次："
                + repr(
                    unknown_evidence
                )
            )

        evidence_set = set(
            evidence_ids
        )

        allowed_union: set[str] = set()
        unsupported_orders: list[int] = []

        for order in beat_orders:
            allowed_for_beat = set(
                beat_evidence.get(order)
                or set()
            )

            if not allowed_for_beat:
                raise RuntimeError(
                    f"V2.39.5: Beat {order} 没有合法 evidence authority"
                )

            allowed_union.update(
                allowed_for_beat
            )

            if not (
                evidence_set
                & allowed_for_beat
            ):
                unsupported_orders.append(
                    order
                )

        if not evidence_set.issubset(
            allowed_union
        ):
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} evidence 越出 covered Beat；"
                f"selected={sorted(evidence_set)} "
                f"allowed={sorted(allowed_union)}"
            )

        if unsupported_orders:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 名义覆盖 Beat "
                f"{unsupported_orders}，但没有对应直接证据"
            )

        row = _normalize_temporal_contract(
            row,
            evidence_ids=evidence_ids,
            raw_index=raw_index,
        )

        # Three narrative states are the semantic production authority.
        state_fields = (
            "representative_state",
            "video_start_state",
            "video_end_state",
        )

        missing_states = [
            key
            for key in state_fields
            if not str(
                row.get(key)
                or ""
            ).strip()
        ]

        if missing_states:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 缺少三状态："
                + ", ".join(
                    missing_states
                )
            )

        _assert_temporal_state_distinction(
            row,
            context=f"Shot#{raw_index} temporal contract",
        )

        # No hidden duration default. H3 and final timing consume this value.
        if (
            "duration_seconds"
            not in row
            or row.get("duration_seconds")
            in (None, "")
        ):
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 缺少真实 duration_seconds"
            )

        try:
            duration = float(
                row.get(
                    "duration_seconds"
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} duration_seconds 非数字："
                + repr(
                    row.get(
                        "duration_seconds"
                    )
                )
            ) from exc

        if (
            not math.isfinite(
                duration
            )
            or duration < 0.8
            or duration > 20.0
        ):
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} duration_seconds 超出合同范围 "
                f"[0.8,20.0]：{duration}"
            )

        # Deterministic derived fields. Model-authored prompt drift is ignored.
        row = _compile_prompts_from_states(
            row
        )

        prompt_fields = (
            "image_prompt",
            "video_start_prompt",
            "video_prompt",
        )

        missing_prompts = [
            key
            for key in prompt_fields
            if not str(
                row.get(key)
                or ""
            ).strip()
        ]

        if missing_prompts:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} Prompt 编译失败："
                + ", ".join(
                    missing_prompts
                )
            )

        evidence_text: list[str] = []
        evidence_spans: list[dict[str, Any]] = []

        for evidence_id in evidence_ids:
            anchor = anchor_map[
                evidence_id
            ]

            text = str(
                anchor.get("text")
                or ""
            ).strip()

            if not text:
                raise RuntimeError(
                    f"V2.39.5: Shot#{raw_index} evidence {evidence_id} 文本为空"
                )

            if (
                "source_start"
                in anchor
            ):
                start_value = anchor.get(
                    "source_start"
                )
            else:
                start_value = anchor.get(
                    "start"
                )

            if (
                "source_end"
                in anchor
            ):
                end_value = anchor.get(
                    "source_end"
                )
            else:
                end_value = anchor.get(
                    "end"
                )

            try:
                start = int(
                    start_value
                )
                end = int(
                    end_value
                )
            except Exception as exc:
                raise RuntimeError(
                    f"V2.39.5: Shot#{raw_index} evidence {evidence_id} "
                    "缺少精确 source offset"
                ) from exc

            if (
                start < 0
                or end <= start
            ):
                raise RuntimeError(
                    f"V2.39.5: Shot#{raw_index} evidence {evidence_id} "
                    f"source offset 非法：[{start},{end})"
                )

            evidence_text.append(
                text
            )

            evidence_spans.append({
                "id": evidence_id,
                "start": start,
                "end": end,
                "text": text,
            })

        mode_evidence_set = set(_id_list(
            row.get("temporal_mode_evidence_ids")
        ))
        row["temporal_mode_source_spans"] = [
            copy.deepcopy(span)
            for span in evidence_spans
            if str(span.get("id") or "") in mode_evidence_set
        ]

        summary = str(
            row.get("summary")
            or ""
        ).strip()

        action = str(
            row.get("action")
            or ""
        ).strip()

        if not summary and action:
            summary = action

        # Safe fallback is the exact evidence selected by this Shot, not the
        # wider Beat summary (which may include facts from unselected evidence).
        if not summary:
            summary = "；".join(
                evidence_text
            )[:700]

        if (
            not summary
            or re.fullmatch(
                r"(?:C\d{2})?E\d{3}",
                summary,
                flags=re.I,
            )
            or summary in anchor_ids
        ):
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} summary 空/锚点泄漏"
            )

        raw_character_ids = _id_list(
            row.get(
                "character_entity_ids"
            )
        )

        illegal_chars = [
            key
            for key in raw_character_ids
            if key not in allowed_chars
        ]

        if illegal_chars:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 包含非法 character entity id："
                + repr(
                    illegal_chars
                )
            )

        raw_prop_ids = _id_list(
            row.get(
                "prop_entity_ids"
            )
        )

        illegal_props = [
            key
            for key in raw_prop_ids
            if key not in allowed_props
        ]

        if illegal_props:
            raise RuntimeError(
                f"V2.39.5: Shot#{raw_index} 包含非法 prop entity id："
                + repr(
                    illegal_props
                )
            )

        cleaned.append({
            "scene_id":
                scene_id,
            "episode_id":
                episode_id,
            "title":
                str(
                    row.get("title")
                    or ""
                ).strip(),
            "duration_seconds":
                duration,
            "duration_source":
                str(
                    row.get(
                        "duration_source"
                    )
                    or "shot-output"
                ),
            "evidence_binding_source":
                str(
                    row.get(
                        "evidence_binding_source"
                    )
                    or "shot-output"
                ),
            "summary":
                summary,
            "temporal_mode":
                str(row.get("temporal_mode") or ""),
            "temporal_mode_reason":
                str(row.get("temporal_mode_reason") or ""),
            "temporal_mode_evidence_ids":
                list(row.get("temporal_mode_evidence_ids") or []),
            "temporal_mode_source_spans":
                copy.deepcopy(row.get("temporal_mode_source_spans") or []),
            "source_fact":
                str(row.get("source_fact") or ""),
            "narrative_start_state":
                str(row.get("narrative_start_state") or ""),
            "narrative_state":
                str(row.get("narrative_state") or ""),
            "narrative_end_state":
                str(row.get("narrative_end_state") or ""),
            "visual_realization":
                str(row.get("visual_realization") or ""),
            "realization_scope":
                str(row.get("realization_scope") or ""),
            "realization_assumptions":
                list(row.get("realization_assumptions") or []),
            "visual_start_frame":
                str(row.get("visual_start_frame") or ""),
            "representative_frame":
                str(row.get("representative_frame") or ""),
            "visual_end_frame":
                str(row.get("visual_end_frame") or ""),
            "visual_motion":
                str(row.get("visual_motion") or ""),
            "composition":
                str(
                    row.get("composition")
                    or ""
                ).strip(),
            "shot_size":
                str(
                    row.get("shot_size")
                    or ""
                ).strip(),
            "camera":
                str(
                    row.get("camera")
                    or ""
                ).strip(),
            "camera_move":
                str(
                    row.get("camera_move")
                    or ""
                ).strip(),
            "action":
                action,
            "performance":
                str(
                    row.get("performance")
                    or ""
                ).strip(),
            "environment":
                str(
                    row.get("environment")
                    or ""
                ).strip(),
            "dialogue":
                str(
                    row.get("dialogue")
                    or ""
                ).strip(),
            "narration":
                str(
                    row.get("narration")
                    or ""
                ).strip(),
            "sound":
                str(
                    row.get("sound")
                    or ""
                ).strip(),
            "music":
                str(
                    row.get("music")
                    or ""
                ).strip(),
            "continuity":
                str(
                    row.get("continuity")
                    or ""
                ).strip(),
            "representative_state":
                str(
                    row.get(
                        "representative_state"
                    )
                    or ""
                ).strip(),
            "video_start_state":
                str(
                    row.get(
                        "video_start_state"
                    )
                    or ""
                ).strip(),
            "video_end_state":
                str(
                    row.get(
                        "video_end_state"
                    )
                    or ""
                ).strip(),
            "image_prompt":
                str(
                    row.get(
                        "image_prompt"
                    )
                    or ""
                ).strip(),
            "video_start_prompt":
                str(
                    row.get(
                        "video_start_prompt"
                    )
                    or ""
                ).strip(),
            "video_prompt":
                str(
                    row.get(
                        "video_prompt"
                    )
                    or ""
                ).strip(),
            "prompt_compiler":
                str(
                    row.get(
                        "prompt_compiler"
                    )
                    or ""
                ).strip(),
            "covered_beat_orders":
                beat_orders,
            "source_evidence_ids":
                evidence_ids,
            "source_evidence":
                evidence_text,
            "source_evidence_spans":
                evidence_spans,
            "character_entity_ids":
                raw_character_ids,
            "prop_entity_ids":
                raw_prop_ids,
            "stage04_contract_version":
                CONTRACT_VERSION,
            "text_model_policy":
                "qwen3-32b",
            "runtime_version":
                VERSION,
        })

    if not cleaned:
        raise RuntimeError(
            "V2.39.5: 当前批次没有可验收 Shot"
        )

    return cleaned






def _audit_ok(env: dict[str, Any], audit: dict[str, Any]) -> bool:
    checker = env.get("_studio_v2371_audit_ok")
    if callable(checker):
        try:
            return bool(checker(audit))
        except Exception:
            pass
    return bool(isinstance(audit, dict) and audit.get("valid") is True and not (audit.get("violations") or audit.get("issues")))


def _audit_issues(audit: dict[str, Any]) -> str:
    value = audit.get("violations") or audit.get("issues") or audit
    return json.dumps(value, ensure_ascii=False)[:2400]


def _batch_missing_orders(
    rows: list[dict[str, Any]],
    compact_beats: list[dict[str, Any]],
) -> list[int]:
    expected = {
        int(row.get("order") or 0)
        for row in compact_beats or []
        if isinstance(row, dict)
        and int(row.get("order") or 0) > 0
    }
    covered = _covered_orders(
        rows
    )
    return sorted(
        expected - covered
    )


def _candidate_semantic_core(
    row: dict[str, Any],
) -> dict[str, Any]:
    return {
        "summary":
            str(
                row.get("summary")
                or ""
            )[:700],
        "action":
            str(
                row.get("action")
                or ""
            )[:700],
        "representative_state":
            str(
                row.get(
                    "representative_state"
                )
                or ""
            )[:900],
        "video_start_state":
            str(
                row.get(
                    "video_start_state"
                )
                or ""
            )[:900],
        "video_end_state":
            str(
                row.get(
                    "video_end_state"
                )
                or ""
            )[:900],
        "dialogue":
            str(
                row.get("dialogue")
                or ""
            )[:600],
        "narration":
            str(
                row.get("narration")
                or ""
            )[:600],
    }


def _missing_shot_state_fields(
    row: dict[str, Any],
) -> list[str]:
    return [
        field
        for field in _SHOT_STATE_FIELDS
        if not str(
            row.get(field)
            or ""
        ).strip()
    ]


def _merge_shot_repair_patch(
    current: dict[str, Any],
    candidate: dict[str, Any],
    *,
    writable_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Merge a scoped model repair without deleting valid Shot states.

    Structured-output contracts can still yield present-but-empty string
    fields.  An empty repair value is not semantic authority and must never
    erase a previously validated state.  A non-empty candidate remains model
    authored and may replace a state that the semantic audit rejected.
    """
    merged = copy.deepcopy(current)

    for field in writable_fields:
        if field not in candidate:
            continue

        value = candidate[field]
        # A scoped repair has no authority to erase any already valid semantic
        # text.  Empty collections remain meaningful for entity-list repairs.
        if value is None or (isinstance(value, str) and not value.strip()):
            continue

        merged[field] = copy.deepcopy(value)

    missing = _missing_shot_state_fields(
        merged
    )
    if missing:
        raise RuntimeError(
            "V2.39.6.1: scoped Shot repair 未形成三状态闭包："
            + ", ".join(missing)
        )

    try:
        _assert_temporal_state_distinction(
            merged,
            context="Directional repair merge",
        )
        compiled = _compile_prompts_from_states(
            merged
        )
        _assert_prompt_projection_distinction(
            compiled,
            context="Directional repair prompt projection",
        )
    except Stage04RepairInvariantError as exc:
        metadata = copy.deepcopy(
            getattr(exc, "metadata", {})
        )
        metadata.update({
            "pre_repair_states":
                _shot_state_snapshot(current),
            "repair_patch":
                copy.deepcopy(candidate),
            "post_repair_states":
                _shot_state_snapshot(merged),
            "pre_repair_prompts":
                _shot_prompt_snapshot(current),
            "post_repair_prompts":
                _shot_prompt_snapshot(_project_prompts_from_states(merged)),
            "repair_changed_fields": [
                field
                for field in writable_fields
                if current.get(field) != merged.get(field)
            ],
        })
        raise Stage04RepairInvariantError(
            str(exc),
            metadata=metadata,
        ) from exc

    merged.update({
        key: compiled[key]
        for key in (
            "image_prompt",
            "video_start_prompt",
            "video_prompt",
            "prompt_compiler",
        )
        if key in compiled
    })

    return merged


def _valid_duration_value(
    value: Any,
) -> float | None:
    try:
        duration = float(
            value
        )
    except Exception:
        return None

    if (
        not math.isfinite(
            duration
        )
        or duration < 0.8
        or duration > 20.0
    ):
        return None

    return round(
        duration,
        2,
    )


def _duration_from_aliases(
    row: dict[str, Any],
) -> tuple[float | None, str]:
    for key in (
        "duration_seconds",
        "duration",
        "seconds",
        "shot_duration",
    ):
        if key not in row:
            continue

        duration = (
            _valid_duration_value(
                row.get(key)
            )
        )

        if duration is not None:
            return (
                duration,
                (
                    "shot-output"
                    if key
                    == "duration_seconds"
                    else (
                        "structural-alias:"
                        + key
                    )
                ),
            )

    return (
        None,
        "",
    )


async def _select_targeted_evidence_ids(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    target_order: int,
    beat: dict[str, Any],
    allowed_anchor_rows: list[dict[str, Any]],
) -> tuple[list[str], str]:
    allowed_ids = [
        str(
            anchor.get("id")
            or ""
        )
        for anchor in (
            allowed_anchor_rows
            or []
        )
        if isinstance(
            anchor,
            dict,
        )
        and str(
            anchor.get("id")
            or ""
        )
    ]

    allowed_set = set(
        allowed_ids
    )

    if not allowed_ids:
        raise RuntimeError(
            f"V2.39.5: Beat {target_order} 没有合法 evidence anchors"
        )

    # Structural aliases first.
    current = _id_list(
        row.get(
            "source_evidence_ids"
        )
    )

    if not current:
        for alias in (
            "evidence_ids",
            "source_ids",
            "evidence_anchor_ids",
        ):
            values = _id_list(
                row.get(alias)
            )

            if values:
                current = values
                break

    if (
        current
        and set(current).issubset(
            allowed_set
        )
    ):
        return (
            current,
            "shot-output",
        )

    # If the Beat has exactly one legal evidence anchor, assigning it is a
    # structural scope resolution, not a semantic guess. The strict semantic
    # audit still verifies that the Shot is actually entailed by that evidence.
    if len(allowed_ids) == 1:
        return (
            [allowed_ids[0]],
            "single-legal-anchor",
        )

    system_prompt = (
        "你是 strict-shot-v2 evidence selector。"
        "TARGET_SHOT 的语义内容已经生成，当前只补 source_evidence_ids。"
        "只能从 ALLOWED_EVIDENCE_ANCHORS 选择一个或多个 ID；"
        "每个被选择的证据都必须直接支持 TARGET_SHOT 至少一个事实命题。"
        "不得改写 Shot，不得创建新 evidence ID，不得依据题材关键词。"
        "如果多个 anchors 才能完整支持 Shot，可以选择多个。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== TARGET_BEAT ===\n"
        + json.dumps(
            {
                "order":
                    target_order,
                "summary":
                    beat.get("summary"),
                "state_change":
                    beat.get(
                        "state_change"
                    ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== TARGET_SHOT_SEMANTIC_CORE ===\n"
        + json.dumps(
            _candidate_semantic_core(
                row
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== ALLOWED_EVIDENCE_ANCHORS ===\n"
        + json.dumps(
            allowed_anchor_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "v2391_targeted_evidence_selection_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_RETRY："
                        "必须返回非空 source_evidence_ids 数组；"
                        "每个 ID 必须原样来自 ALLOWED_EVIDENCE_ANCHORS。"
                    )
                )
            ),
            contract=(
                '{"source_evidence_ids":'
                '["C01E001"]}'
            ),
            max_tokens=500,
            temperature=0.0,
        )

        obj = _parse_object(
            env,
            raw,
            parsed,
        )

        values = _id_list(
            obj.get(
                "source_evidence_ids"
            )
        )

        if not values:
            for alias in (
                "evidence_ids",
                "source_ids",
            ):
                values = _id_list(
                    obj.get(alias)
                )

                if values:
                    break

        if (
            values
            and set(values).issubset(
                allowed_set
            )
        ):
            return (
                values,
                "qwen-evidence-selector",
            )

        diagnostics.append(
            "attempt="
            + str(
                attempt + 1
            )
            + " values="
            + repr(
                values
            )
        )

    raise RuntimeError(
        f"V2.39.5: Beat {target_order} evidence 独立补全失败；"
        + " | ".join(
            diagnostics
        )
    )


async def _plan_targeted_duration(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    target_order: int,
) -> tuple[float, str]:
    existing, source = (
        _duration_from_aliases(
            row
        )
    )

    if existing is not None:
        return (
            existing,
            source,
        )

    core = _candidate_semantic_core(
        row
    )

    missing_states = [
        key
        for key in (
            "video_start_state",
            "representative_state",
            "video_end_state",
        )
        if not str(
            core.get(key)
            or ""
        ).strip()
    ]

    if missing_states:
        raise RuntimeError(
            f"V2.39.5: Beat {target_order} 无法规划时长，"
            "因为三状态不完整："
            + ", ".join(
                missing_states
            )
        )

    system_prompt = (
        "你是短视频单镜头 timing planner。"
        "当前只规划一个已经确定语义的 Shot 的 duration_seconds，"
        "不能改写剧情、状态或证据。"
        "根据可见动作的完成时间，以及 dialogue/narration 的实际表达长度，"
        "给出 0.8 到 20.0 秒之间的制作时长。"
        "这是制作参数，不是小说事实。"
        "只返回严格 JSON：duration_seconds(number)。"
    )

    prompt = (
        "=== TARGET_SHOT_SEMANTIC_CORE ===\n"
        + json.dumps(
            core,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "v2391_targeted_duration_planner_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_RETRY："
                        "只返回一个 0.8~20.0 的数字字段 duration_seconds。"
                    )
                )
            ),
            contract=(
                '{"duration_seconds":3.2}'
            ),
            max_tokens=180,
            temperature=0.0,
        )

        obj = _parse_object(
            env,
            raw,
            parsed,
        )

        value = None

        for key in (
            "duration_seconds",
            "duration",
            "seconds",
        ):
            if key in obj:
                value = (
                    _valid_duration_value(
                        obj.get(key)
                    )
                )

                if value is not None:
                    break

        if value is not None:
            return (
                value,
                "qwen3-32b-dedicated-timing",
            )

        diagnostics.append(
            "attempt="
            + str(
                attempt + 1
            )
            + " parsed="
            + repr(
                obj
            )[:350]
        )

    raise RuntimeError(
        f"V2.39.5: Beat {target_order} duration 独立规划失败；"
        + " | ".join(
            diagnostics
        )
    )



async def _repair_invalid_temporal_mode_classification(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    """Re-classify only an out-of-contract temporal_mode from locked evidence.

    This deliberately is not an alias mapper.  The replacement mode must come
    from a single evidence-grounded Qwen classification call.  Beat/evidence,
    entity, narrative, timing and presentation fields are immutable here.
    """
    item = copy.deepcopy(row)
    raw_mode = str(item.get("temporal_mode") or "").strip().lower()
    if not raw_mode or raw_mode in _TEMPORAL_MODES:
        return item

    evidence_ids = _id_list(item.get("source_evidence_ids"))
    covered_orders = _orders(item.get("covered_beat_orders"))
    anchor_map = {
        str(anchor.get("id") or ""): anchor
        for anchor in anchors or []
        if isinstance(anchor, dict) and str(anchor.get("id") or "")
    }
    beat_map = {
        int(beat.get("order") or 0): beat
        for beat in compact_beats or []
        if isinstance(beat, dict) and int(beat.get("order") or 0) > 0
    }
    exact_evidence = [
        copy.deepcopy(anchor_map[evidence_id])
        for evidence_id in evidence_ids
        if evidence_id in anchor_map
    ]
    locked_beats = [
        copy.deepcopy(beat_map[order])
        for order in covered_orders
        if order in beat_map
    ]

    repair_input = {
        "raw_temporal_mode": raw_mode,
        "allowed_temporal_modes": sorted(_TEMPORAL_MODES),
        "covered_beat_orders": covered_orders,
        "source_evidence_ids": evidence_ids,
        "locked_beats": locked_beats,
        "exact_selected_evidence": exact_evidence,
        "candidate_semantic_core": _candidate_semantic_core(item),
    }

    def metadata(
        *,
        output: dict[str, Any] | None = None,
        reason: str = "",
        progress: str,
        post: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repair_output = copy.deepcopy(output or {})
        return {
            "raw_temporal_mode": raw_mode,
            "allowed_temporal_modes": sorted(_TEMPORAL_MODES),
            "temporal_mode_repair_input": copy.deepcopy(repair_input),
            "temporal_mode_repair_output": repair_output,
            "temporal_mode_repair_reason": reason,
            "temporal_mode_evidence_ids": _id_list(
                repair_output.get("temporal_mode_evidence_ids")
            ),
            "pre_repair_candidate": copy.deepcopy(item),
            "post_repair_candidate": copy.deepcopy(post),
            "repair_input_fingerprint": hashlib.sha256(
                json.dumps(
                    repair_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "repair_output_fingerprint": hashlib.sha256(
                json.dumps(
                    repair_output,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "repair_progress": progress,
            "failed_rule": "temporal_mode_contract",
            "failed_rules": ["temporal_mode_contract"],
            "evidence_sufficiency": "temporal_mode_contract_invalid",
            "repair_context": context,
        }

    if (
        not evidence_ids
        or len(exact_evidence) != len(evidence_ids)
        or not covered_orders
        or len(locked_beats) != len(covered_orders)
    ):
        raise Stage04ShotRepairError(
            f"{context}: 非法 temporal_mode 且无法完整锁定 Beat/evidence 进行字段级重分类",
            metadata=metadata(
                progress="rejected_temporal_mode_repair_scope_incomplete"
            ),
        )

    system_prompt = (
        "你是 strict-shot-v2 temporal_mode 字段级重分类器。"
        "当前 Shot 的 Beat、source evidence、人物/道具绑定和全部叙事/表现字段已经锁定。"
        "只能根据 EXACT_SELECTED_EVIDENCE 重新判断 temporal_mode，绝对不能改写 Shot。"
        "temporal_mode 只能精确返回 observable_transition、static_outcome、"
        "insufficient_visual_evidence 三者之一："
        "observable_transition=证据明确支持可观察的前/中/后状态变化；"
        "static_outcome=证据只支持已经成立的状态/结果/关系，或只支持一个正在持续但没有证据支持内部前中后里程碑的稳定活动状态；"
        "insufficient_visual_evidence=不新增剧情事实就无法形成证据支持的视觉状态。"
        "必须返回 evidence-based reason 和 temporal_mode_evidence_ids；"
        "evidence ids 只能从当前 Shot 已锁定 source_evidence_ids 中选择。"
        "RAW_INVALID_TEMPORAL_MODE 仅用于诊断，不是候选枚举，禁止照抄。"
        "不得依据题材关键词或固定业务类别。只返回严格 JSON。"
    )
    prompt = (
        "=== RAW_INVALID_TEMPORAL_MODE ===\n"
        + raw_mode
        + "\n\n=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(locked_beats, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(exact_evidence, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== LOCKED_CANDIDATE_SEMANTIC_CORE ===\n"
        + json.dumps(
            {
                **_candidate_semantic_core(item),
                "source_fact": str(item.get("source_fact") or "")[:700],
                "narrative_start_state": str(item.get("narrative_start_state") or "")[:900],
                "narrative_state": str(item.get("narrative_state") or "")[:900],
                "narrative_end_state": str(item.get("narrative_end_state") or "")[:900],
                "visual_realization": str(item.get("visual_realization") or "")[:900],
                "visual_start_frame": str(item.get("visual_start_frame") or "")[:900],
                "representative_frame": str(item.get("representative_frame") or "")[:900],
                "visual_end_frame": str(item.get("visual_end_frame") or "")[:900],
                "visual_motion": str(item.get("visual_motion") or "")[:900],
                "character_entity_ids": _id_list(item.get("character_entity_ids")),
                "prop_entity_ids": _id_list(item.get("prop_entity_ids")),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    contract = json.dumps(
        {
            "temporal_mode": "observable_transition",
            "temporal_mode_reason": "",
            "temporal_mode_evidence_ids": [evidence_ids[0]],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        raw, parsed, _ = await _qwen(
            env,
            phase="studio_stage04_temporal_mode_classification_repair_qwen32b",
            system_prompt=system_prompt,
            prompt=prompt,
            contract=contract,
            max_tokens=420,
            temperature=0.0,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: temporal_mode 字段级重分类调用失败：{exc}",
            metadata=metadata(
                reason=str(exc),
                progress="temporal_mode_repair_call_failed",
            ),
        ) from exc

    obj = _parse_object(env, raw, parsed)
    repaired_mode = str(obj.get("temporal_mode") or "").strip().lower()
    reason = str(obj.get("temporal_mode_reason") or "").strip()
    repaired_evidence_ids = _id_list(obj.get("temporal_mode_evidence_ids"))
    repair_output = {
        "temporal_mode": repaired_mode,
        "temporal_mode_reason": reason,
        "temporal_mode_evidence_ids": repaired_evidence_ids,
    }

    valid_output = (
        repaired_mode in _TEMPORAL_MODES
        and bool(reason)
        and bool(repaired_evidence_ids)
        and set(repaired_evidence_ids).issubset(set(evidence_ids))
    )
    if not valid_output:
        progress = (
            "rejected_no_progress"
            if repaired_mode == raw_mode
            else "rejected_invalid_temporal_mode_repair_output"
        )
        raise Stage04ShotRepairError(
            f"{context}: temporal_mode 字段级重分类仍未形成闭合枚举；"
            f"raw={raw_mode!r} repaired={repaired_mode!r}",
            metadata=metadata(
                output=repair_output,
                reason=reason,
                progress=progress,
            ),
        )

    repaired = copy.deepcopy(item)
    repaired["temporal_mode"] = repaired_mode
    repaired["temporal_mode_reason"] = reason
    repaired["temporal_mode_evidence_ids"] = repaired_evidence_ids
    diagnostics = metadata(
        output=repair_output,
        reason=reason,
        progress="temporal_mode_reclassified",
        post=repaired,
    )
    diagnostics["failed_rule"] = ""
    diagnostics["failed_rules"] = []
    diagnostics["evidence_sufficiency"] = "classified"
    repaired["_temporal_mode_repair_diagnostics"] = diagnostics
    return repaired




async def _repair_observable_transition_state_consistency(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    """Repair only collapsed observable-transition state fields.

    Beat/evidence binding, temporal_mode, source fact, entities and timing are
    immutable.  A single evidence-grounded patch repairs the smallest failed
    transition; failure routes to evidence regroup instead of re-sending the
    same full Shot generation payload three times.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "observable_transition":
        return item

    evidence_ids = _id_list(item.get("source_evidence_ids"))
    covered_orders = _orders(item.get("covered_beat_orders"))
    if not evidence_ids or not covered_orders:
        return item

    # Normalize aliases first, then inspect the strict three-state invariant.
    item = _normalize_temporal_contract(
        item,
        evidence_ids=evidence_ids,
        raw_index=1,
    )
    try:
        _assert_temporal_state_distinction(
            item,
            context=f"{context} prevalidation",
        )
        return item
    except Stage04RepairInvariantError as exc:
        initial_error = str(exc)

    keys = {
        field: _semantic_text_key(item.get(field))
        for field in _SHOT_TEMPORAL_STATE_FIELDS
    }
    duplicate_pairs = [
        (left, right)
        for left, right in (
            ("video_start_state", "representative_state"),
            ("representative_state", "video_end_state"),
            ("video_start_state", "video_end_state"),
        )
        if keys.get(left) and keys.get(left) == keys.get(right)
    ]
    if not duplicate_pairs:
        raise

    # Prefer the smallest directional patch.  This exactly covers the current
    # real failure representative_state == video_end_state while preserving the
    # already valid start/core states.
    if duplicate_pairs == [("representative_state", "video_end_state")]:
        repair_fields = ("video_end_state",)
    elif duplicate_pairs == [("video_start_state", "representative_state")]:
        repair_fields = ("representative_state",)
    elif duplicate_pairs == [("video_start_state", "video_end_state")]:
        repair_fields = ("video_end_state",)
    else:
        repair_fields = _SHOT_TEMPORAL_STATE_FIELDS

    anchor_map = {
        str(anchor.get("id") or ""): anchor
        for anchor in anchors or []
        if isinstance(anchor, dict) and str(anchor.get("id") or "")
    }
    beat_map = {
        int(beat.get("order") or 0): beat
        for beat in compact_beats or []
        if isinstance(beat, dict) and int(beat.get("order") or 0) > 0
    }
    exact_evidence = [
        copy.deepcopy(anchor_map[evidence_id])
        for evidence_id in evidence_ids
        if evidence_id in anchor_map
    ]
    locked_beats = [
        copy.deepcopy(beat_map[order])
        for order in covered_orders
        if order in beat_map
    ]
    metadata_base = {
        "failed_rule": "observable_transition_state_consistency",
        "failed_rules": ["state_order", "no_result_duplication"],
        "temporal_mode": "observable_transition",
        "evidence_ids": evidence_ids,
        "beat_id": covered_orders,
        "exact_selected_evidence": copy.deepcopy(exact_evidence),
        "locked_beats": copy.deepcopy(locked_beats),
        "pre_repair_states": _shot_state_snapshot(item),
        "duplicate_pairs": copy.deepcopy(duplicate_pairs),
        "repair_fields": list(repair_fields),
        "pre_repair_error": initial_error,
        "repair_context": context,
    }
    if (
        len(exact_evidence) != len(evidence_ids)
        or len(locked_beats) != len(covered_orders)
    ):
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态塌缩且无法完整锁定 Beat/evidence",
            metadata={
                **metadata_base,
                "repair_progress": "observable_state_repair_scope_incomplete",
                "evidence_sufficiency": "insufficient_for_observable_transition",
            },
        )

    system_prompt = (
        "你是 strict-shot-v2 observable_transition 字段级状态修复器。"
        "temporal_mode=observable_transition、Beat/source evidence、source_fact、summary/action、"
        "entity、duration 全部锁定。只修改 FAILED_STATE_FIELDS。"
        "三个状态必须由 EXACT_SELECTED_EVIDENCE 直接支持，属于同一因果链且严格时间前向。"
        "若只修 video_end_state，start 和 representative 已合法，必须保持不变，只给出证据支持的"
        "更晚 end；若只修 representative_state，则 start/end 锁定，给出二者之间可观察中间态。"
        "不得把下一 Beat 的结果提前写入，不得用抽象阶段词伪造时间推进。"
        "若证据不足以产生所需不同状态，对应 patch 字段返回空字符串；程序将进入 evidence regroup。"
        "只返回严格 JSON patch。"
    )
    current_payload = {
        "source_fact": item.get("source_fact"),
        "summary": item.get("summary"),
        "action": item.get("action"),
        "video_start_state": item.get("video_start_state"),
        "representative_state": item.get("representative_state"),
        "video_end_state": item.get("video_end_state"),
    }
    prompt = (
        "=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(exact_evidence, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(locked_beats, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CURRENT_OBSERVABLE_SHOT ===\n"
        + json.dumps(current_payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== FAILED_STATE_FIELDS ===\n"
        + json.dumps(list(repair_fields), ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== LOCAL_CONTRACT_ERROR ===\n"
        + initial_error
    )
    contract = json.dumps(
        {"patch": {field: "" for field in repair_fields}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        raw, parsed, _ = await _qwen(
            env,
            phase="studio_stage04_observable_state_consistency_repair_qwen32b",
            system_prompt=system_prompt,
            prompt=prompt,
            contract=contract,
            max_tokens=650,
            temperature=0.0,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态字段级修复调用失败：{exc}",
            metadata={
                **metadata_base,
                "repair_progress": "observable_state_repair_call_failed",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "repair_error": str(exc),
            },
        ) from exc

    obj = _parse_object(env, raw, parsed)
    patch = obj.get("patch") if isinstance(obj.get("patch"), dict) else obj
    patch = dict(patch) if isinstance(patch, dict) else {}
    patch = {
        field: str(patch.get(field) or "").strip()
        for field in repair_fields
    }
    if any(not patch[field] for field in repair_fields):
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 现有证据不足以修复状态链",
            metadata={
                **metadata_base,
                "repair_patch": copy.deepcopy(patch),
                "repair_progress": "needs_regrouping_or_evidence_selection",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "regroup_reason": "state repair returned empty evidence-grounded value",
            },
        )

    repaired = copy.deepcopy(item)
    narrative_alias = {
        "video_start_state": "narrative_start_state",
        "representative_state": "narrative_state",
        "video_end_state": "narrative_end_state",
    }
    for field, value in patch.items():
        repaired[field] = value
        repaired[narrative_alias[field]] = value

    changed = any(
        _semantic_text_key(item.get(field))
        != _semantic_text_key(repaired.get(field))
        for field in repair_fields
    )
    if not changed:
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态修复无语义进展",
            metadata={
                **metadata_base,
                "repair_patch": copy.deepcopy(patch),
                "post_repair_states": _shot_state_snapshot(repaired),
                "repair_progress": "rejected_no_progress",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "regroup_reason": "state repair fingerprint did not change",
            },
        )

    try:
        repaired = _normalize_temporal_contract(
            repaired,
            evidence_ids=evidence_ids,
            raw_index=1,
        )
        _assert_temporal_state_distinction(
            repaired,
            context=f"{context} post-repair",
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态修复后仍未形成前向时间链：{exc}",
            metadata={
                **metadata_base,
                "repair_patch": copy.deepcopy(patch),
                "post_repair_states": _shot_state_snapshot(repaired),
                "repair_progress": "needs_regrouping_or_evidence_selection",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "regroup_reason": str(exc),
            },
        ) from exc

    repaired["_observable_state_repair_diagnostics"] = {
        **metadata_base,
        "repair_patch": copy.deepcopy(patch),
        "post_repair_states": _shot_state_snapshot(repaired),
        "repair_progress": "observable_state_repaired",
        "evidence_sufficiency": "sufficient_for_observable_transition",
    }
    return repaired



def _ensure_static_presentation_frame_distinction(
    row: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically close static_outcome presentation-frame distinction.

    This helper never changes story facts, Beat/evidence binding, entities or timing.
    It only makes presentation-only frame descriptions distinguishable by shot
    grammar when a model returned empty or semantically identical frames.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "static_outcome":
        return item

    before = {
        field: str(item.get(field) or "").strip()
        for field in _STATIC_PRESENTATION_FIELDS
    }
    before_keys = {
        _semantic_text_key(before[field])
        for field in _STATIC_PRESENTATION_FIELDS
    }
    if "" not in before_keys and len(before_keys) == len(_STATIC_PRESENTATION_FIELDS):
        return item

    stable_visual = next((
        str(item.get(field) or "").strip()
        for field in (
            "visual_realization",
            "narrative_state",
            "source_fact",
            "summary",
        )
        if str(item.get(field) or "").strip()
    ), "同一已锁定叙事状态")

    prefixes = {
        "visual_start_frame": "远景建立构图",
        "representative_frame": "中景主体构图",
        "visual_end_frame": "较紧景别收束构图",
    }
    for field in _STATIC_PRESENTATION_FIELDS:
        original = before[field] or stable_visual
        item[field] = (
            f"{prefixes[field]}：{original}；"
            "仅改变景别、机位或构图，不改变叙事事实。"
        )

    item["realization_scope"] = "presentation_only"
    assumptions = [
        str(value).strip()
        for value in (item.get("realization_assumptions") or [])
        if str(value).strip()
    ]
    closure_assumption = "三个表现帧仅以景别/机位/构图区分，不代表剧情时间推进"
    if closure_assumption not in assumptions:
        assumptions.append(closure_assumption)
    item["realization_assumptions"] = assumptions
    if not str(item.get("visual_motion") or "").strip():
        item["visual_motion"] = (
            "镜头从远景建立构图过渡到中景主体构图并以较紧景别收束；"
            "全程仅为表现层变化。"
        )

    after_keys = {
        _semantic_text_key(item.get(field))
        for field in _STATIC_PRESENTATION_FIELDS
    }
    if "" in after_keys or len(after_keys) != len(_STATIC_PRESENTATION_FIELDS):
        raise Stage04RepairInvariantError(
            "strict-shot-v2 static_outcome deterministic presentation closure failed",
            metadata={
                "failed_rules": ["visual_realization"],
                "temporal_mode": "static_outcome",
                "before_frames": before,
            },
        )
    item["_static_presentation_closure_diagnostics"] = {
        "repair_progress": "deterministic_static_presentation_closed",
        "before_frames": before,
        "after_frames": {
            field: str(item.get(field) or "")
            for field in _STATIC_PRESENTATION_FIELDS
        },
        "fact_fields_mutated": [],
    }
    return item


async def _repair_static_outcome_payload_consistency(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    """Repair a static_outcome payload that contradicts its own mode.

    temporal_mode, Beat/evidence bindings, entities and timing stay locked.
    Only the static narrative/presentation block may be regenerated from the
    exact selected evidence.  The repair is single-shot and fail-closed.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "static_outcome":
        return item

    evidence_ids = _id_list(item.get("source_evidence_ids"))
    mode_evidence_ids = _id_list(item.get("temporal_mode_evidence_ids"))
    reason = str(item.get("temporal_mode_reason") or "").strip()
    if (
        not evidence_ids
        or not reason
        or not mode_evidence_ids
        or not set(mode_evidence_ids).issubset(set(evidence_ids))
    ):
        # Classification itself is incomplete.  This helper only repairs the
        # payload/mode consistency and must not silently widen its authority.
        return item

    try:
        return _normalize_temporal_contract(
            item,
            evidence_ids=evidence_ids,
            raw_index=1,
        )
    except Stage04ShotRepairError:
        raise
    except Exception as exc:
        if "static_outcome" not in str(exc):
            raise
        initial_error = str(exc)

    covered_orders = _orders(item.get("covered_beat_orders"))
    anchor_map = {
        str(anchor.get("id") or ""): anchor
        for anchor in anchors or []
        if isinstance(anchor, dict) and str(anchor.get("id") or "")
    }
    beat_map = {
        int(beat.get("order") or 0): beat
        for beat in compact_beats or []
        if isinstance(beat, dict) and int(beat.get("order") or 0) > 0
    }
    exact_evidence = [
        copy.deepcopy(anchor_map[evidence_id])
        for evidence_id in evidence_ids
        if evidence_id in anchor_map
    ]
    locked_beats = [
        copy.deepcopy(beat_map[order])
        for order in covered_orders
        if order in beat_map
    ]

    metadata_base = {
        "failed_rule": "static_outcome_payload_consistency",
        "failed_rules": ["visual_realization"],
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": reason,
        "temporal_mode_evidence_ids": mode_evidence_ids,
        "evidence_ids": evidence_ids,
        "beat_id": covered_orders,
        "exact_selected_evidence": copy.deepcopy(exact_evidence),
        "locked_beats": copy.deepcopy(locked_beats),
        "pre_repair_candidate": copy.deepcopy(item),
        "pre_repair_error": initial_error,
        "repair_context": context,
    }

    if (
        len(exact_evidence) != len(evidence_ids)
        or not covered_orders
        or len(locked_beats) != len(covered_orders)
    ):
        raise Stage04ShotRepairError(
            f"{context}: static_outcome payload 冲突且无法完整锁定 Beat/evidence",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_scope_incomplete",
                "evidence_sufficiency": "undetermined",
            },
        )

    system_prompt = (
        "你是 strict-shot-v2 static_outcome 字段级一致性修复器。"
        "temporal_mode=static_outcome 已锁定，Beat/source evidence/entity/timing 全部不可修改。"
        "当前错误是 Shot 虽被分类为 static_outcome，却在 narrative 字段中伪造了前后变化。"
        "只能根据 EXACT_SELECTED_EVIDENCE 重建静态叙事事实和 presentation-only 表现层。"
        "source_fact 必须是证据直接支持的已成立事实；narrative_state 必须是同一稳定事实。"
        "程序会把 narrative_start_state=narrative_state=narrative_end_state，并把三个 video state "
        "投影为同一 narrative_state，所以你不得输出任何剧情前/中/后过程。"
        "visual_start_frame、representative_frame、visual_end_frame 必须画面可区分，"
        "但差异只能来自构图、机位、镜头运动、环境光影或不改变剧情的微小表现。"
        "visual_motion 只能描述表现层运动；realization_scope 必须精确为 presentation_only。"
        "不得新增角色、道具、动作、因果结果或下一 Beat 事实。只返回严格 JSON patch。"
    )
    prompt = (
        "=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(locked_beats, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(exact_evidence, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CURRENT_INVALID_STATIC_PAYLOAD ===\n"
        + json.dumps(
            {
                "source_fact": item.get("source_fact"),
                "summary": item.get("summary"),
                "action": item.get("action"),
                "narrative_start_state": item.get("narrative_start_state"),
                "narrative_state": item.get("narrative_state"),
                "narrative_end_state": item.get("narrative_end_state"),
                "visual_realization": item.get("visual_realization"),
                "visual_start_frame": item.get("visual_start_frame"),
                "representative_frame": item.get("representative_frame"),
                "visual_end_frame": item.get("visual_end_frame"),
                "visual_motion": item.get("visual_motion"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== INITIAL_LOCAL_CONTRACT_ERROR ===\n"
        + initial_error
    )
    contract = json.dumps(
        {
            "patch": {
                "source_fact": "",
                "narrative_state": "",
                "visual_realization": "",
                "realization_scope": "presentation_only",
                "realization_assumptions": [],
                "visual_start_frame": "",
                "representative_frame": "",
                "visual_end_frame": "",
                "visual_motion": "",
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        raw, parsed, _ = await _qwen(
            env,
            phase="studio_stage04_static_outcome_payload_repair_qwen32b",
            system_prompt=system_prompt,
            prompt=prompt,
            contract=contract,
            max_tokens=900,
            temperature=0.0,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: static_outcome 字段级一致性修复调用失败：{exc}",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_call_failed",
                "evidence_sufficiency": "undetermined",
                "repair_error": str(exc),
            },
        ) from exc

    obj = _parse_object(env, raw, parsed)
    patch = obj.get("patch") if isinstance(obj.get("patch"), dict) else obj
    patch = dict(patch) if isinstance(patch, dict) else {}
    required_text = (
        "source_fact",
        "narrative_state",
        "visual_realization",
        "realization_scope",
        "visual_start_frame",
        "representative_frame",
        "visual_end_frame",
        "visual_motion",
    )
    missing = [
        field
        for field in required_text
        if not str(patch.get(field) or "").strip()
    ]
    assumptions = patch.get("realization_assumptions")
    if not isinstance(assumptions, list):
        missing.append("realization_assumptions")
    if str(patch.get("realization_scope") or "").strip() != "presentation_only":
        missing.append("realization_scope=presentation_only")
    if missing:
        raise Stage04ShotRepairError(
            f"{context}: static_outcome 字段级修复输出不完整：{', '.join(missing)}",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_invalid_output",
                "evidence_sufficiency": "undetermined",
                "repair_output": copy.deepcopy(patch),
            },
        )

    repaired = copy.deepcopy(item)
    for field in required_text:
        repaired[field] = str(patch.get(field) or "").strip()
    repaired["realization_assumptions"] = [
        str(value).strip()
        for value in assumptions
        if str(value or "").strip()
    ]

    stable = repaired["narrative_state"]
    repaired.update({
        "summary": repaired["source_fact"],
        "action": "",
        "narrative_start_state": stable,
        "narrative_end_state": stable,
        "video_start_state": stable,
        "representative_state": stable,
        "video_end_state": stable,
    })

    repaired = _ensure_static_presentation_frame_distinction(repaired)
    try:
        repaired = _normalize_temporal_contract(
            repaired,
            evidence_ids=evidence_ids,
            raw_index=1,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: static_outcome 字段级修复后仍未闭合 strict contract：{exc}",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_rejected",
                "evidence_sufficiency": "undetermined",
                "repair_output": copy.deepcopy(patch),
                "post_repair_candidate": copy.deepcopy(repaired),
                "post_repair_error": str(exc),
            },
        ) from exc

    repaired["_static_outcome_repair_diagnostics"] = {
        **metadata_base,
        "repair_progress": "static_payload_repaired",
        "evidence_sufficiency": "sufficient_for_static_payload_repair",
        "repair_output": copy.deepcopy(patch),
        "post_repair_candidate": copy.deepcopy(repaired),
    }
    return repaired


async def _complete_targeted_shot_structure(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    target_order: int,
    beat: dict[str, Any],
    allowed_anchor_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Complete structural fields without regenerating the semantic Shot.

    Evidence and duration deliberately have different authorities:
      - evidence: exact Beat scope / constrained evidence selector;
      - duration: explicit dedicated production timing planner.
    """
    item = copy.deepcopy(
        row
    )

    item[
        "covered_beat_orders"
    ] = [
        target_order
    ]

    evidence_ids, evidence_source = (
        await _select_targeted_evidence_ids(
            env,
            row=item,
            target_order=
                target_order,
            beat=beat,
            allowed_anchor_rows=
                allowed_anchor_rows,
        )
    )

    item[
        "source_evidence_ids"
    ] = evidence_ids

    item[
        "evidence_binding_source"
    ] = evidence_source

    item = await _repair_invalid_temporal_mode_classification(
        env,
        row=item,
        compact_beats=[beat],
        anchors=allowed_anchor_rows,
        context=f"Beat {target_order} targeted Shot",
    )

    item = await _repair_static_outcome_payload_consistency(
        env,
        row=item,
        compact_beats=[beat],
        anchors=allowed_anchor_rows,
        context=f"Beat {target_order} targeted Shot",
    )

    item = await _repair_observable_transition_state_consistency(
        env,
        row=item,
        compact_beats=[beat],
        anchors=allowed_anchor_rows,
        context=f"Beat {target_order} targeted Shot",
    )

    item = _normalize_temporal_contract(
        item,
        evidence_ids=evidence_ids,
        raw_index=1,
    )

    duration, duration_source = (
        await _plan_targeted_duration(
            env,
            row=item,
            target_order=
                target_order,
        )
    )

    item[
        "duration_seconds"
    ] = duration

    item[
        "duration_source"
    ] = duration_source

    return item


async def _generate_missing_beat_shots(
    env: dict[str, Any],
    *,
    missing_orders: list[int],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    previous_shot: dict[str, Any] | None,
    next_beat: dict[str, Any] | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    scene_id: str,
    episode_id: str,
) -> list[dict[str, Any]]:
    anchor_map = {
        str(row.get("id") or ""): row
        for row in anchors or []
        if isinstance(row, dict)
        and str(row.get("id") or "")
    }
    beat_map = {
        int(row.get("order") or 0): row
        for row in compact_beats or []
        if isinstance(row, dict)
        and int(row.get("order") or 0) > 0
    }

    result: list[dict[str, Any]] = []
    local_previous = previous_shot

    for order in missing_orders:
        beat = beat_map.get(order)
        if not beat:
            raise RuntimeError(
                f"V2.39.5: missing Beat {order} 不存在"
            )

        allowed_evidence_ids = _id_list(
            beat.get("allowed_source_evidence_ids")
            or beat.get("source_evidence_ids")
        )
        allowed_anchor_rows = [
            anchor_map[key]
            for key in allowed_evidence_ids
            if key in anchor_map
        ]

        if not allowed_anchor_rows:
            raise RuntimeError(
                f"V2.39.5: missing Beat {order} 没有合法 evidence anchors"
            )

        system_prompt = (
            "你是 strict-shot-v2 缺失 Beat 定向补全器。"
            "只为 TARGET_BEAT 生成 Shot，不得消费其他 Beat。"
            "source_evidence_ids 必须从 ALLOWED_EVIDENCE_ANCHORS 中选择，"
            "至少一个，并直接支持该 Shot。"
            "必须从 evidence 输出 temporal_mode + reason + evidence ids。"
            "observable_transition 才要求三 narrative state 为前向因果链。"
            "static_outcome 也适用于证据只证明一个持续活动/稳定状态而不证明内部时间里程碑的情况；"
            "static_outcome 必须锁定同一 narrative_state，把不同画面和运动严格隔离到"
            " presentation_only visual realization 字段，不得虚构剧情转折。"
            "insufficient_visual_evidence 必须原样分类，不能造动作补足。"
            "不得提前消费 NEXT_BEAT_PREVIEW。"
            "如果 TARGET_BEAT 带 adjacent_projection 且 relation=forward_with_replayed_prefix，"
            "说明其证据已经裁剪为上一 Shot 完成之后的新后续；不得让 video_start_state "
            "回到 PREVIOUS_ACCEPTED_SHOT 已经完成之前的状态。"
            "人物/道具只填写当前 Shot 真实可见合法 entity id；不确定留空。"
            "必须输出该 temporal_mode 对应的完整合同；三个 Prompt 由程序生成。"
            "duration_seconds 若能规划则输出；遗漏时由独立 timing planner 补全。"
            "不得依赖固定业务词表或题材类别。只返回严格 JSON。"
        )

        prompt = (
            "=== PREVIOUS_ACCEPTED_SHOT ===\n"
            + json.dumps(
                local_previous or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== TARGET_BEAT ===\n"
            + json.dumps(
                beat,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== ALLOWED_EVIDENCE_ANCHORS ===\n"
            + json.dumps(
                allowed_anchor_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n"
            + json.dumps(
                next_beat or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        accepted: list[dict[str, Any]] = []
        diagnostics: list[str] = []

        for attempt in range(3):
            try:
                raw, parsed, _ = await _qwen(
                    env,
                    phase=(
                        "studio_stage04_"
                        "v2392_missing_beat_completion_qwen32b"
                    ),
                    system_prompt=system_prompt,
                    prompt=(
                        prompt
                        + (
                            ""
                            if attempt == 0
                            else (
                                "\n\nSTRICT_RETRY："
                                f"只制作 Beat {order}；"
                                "必须返回至少一个 Shot 和合法 source_evidence_ids。"
                            )
                        )
                    ),
                    contract=_shot_generation_contract(order),
                    max_tokens=1700,
                    temperature=0.0,
                )
            except Exception as exc:
                diagnostics.append(
                    f"attempt={attempt + 1} "
                    + type(exc).__name__
                    + ": "
                    + str(exc)[:450]
                )
                continue

            candidates = _extract_shots(
                env,
                raw,
                parsed,
            )
            if not candidates:
                diagnostics.append(
                    f"attempt={attempt + 1} shots_not_found "
                    + _structured_response_diagnostic(
                        raw,
                        parsed,
                    )
                )
                continue

            scoped: list[dict[str, Any]] = []

            for candidate_index, candidate in enumerate(
                candidates,
                1,
            ):
                if not isinstance(
                    candidate,
                    dict,
                ):
                    continue

                try:
                    item = (
                        await _complete_targeted_shot_structure(
                            env,
                            row=candidate,
                            target_order=order,
                            beat=beat,
                            allowed_anchor_rows=
                                allowed_anchor_rows,
                        )
                    )
                except Stage04ShotRepairError:
                    # Insufficient visual evidence is a semantic routing result,
                    # not malformed JSON. Do not resend the same prompt three times.
                    raise
                except Exception as exc:
                    diagnostics.append(
                        f"attempt={attempt + 1} "
                        f"candidate={candidate_index} "
                        "structural_completion="
                        + str(exc)[:700]
                    )
                    continue

                scoped.append(
                    item
                )

            if not scoped:
                diagnostics.append(
                    f"attempt={attempt + 1} "
                    "no_structurally_complete_candidate"
                )
                continue

            try:
                accepted = validate_rows(
                    env,
                    raw_rows=scoped,
                    compact_beats=[beat],
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    anchors=allowed_anchor_rows,
                    scene_id=scene_id,
                    episode_id=episode_id,
                )
            except Stage04ShotRepairError:
                raise
            except Exception as exc:
                diagnostics.append(
                    f"attempt={attempt + 1} validate="
                    + str(exc)[:650]
                )
                accepted = []
                continue

            if accepted:
                break

        if not accepted:
            raise RuntimeError(
                f"V2.39.5: Beat {order} 定向补全失败；"
                + " | ".join(diagnostics)
            )

        result.extend(accepted)
        local_previous = result[-1]

    return result


async def _ensure_batch_coverage(
    env: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    previous_shot: dict[str, Any] | None,
    next_beat: dict[str, Any] | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    scene_id: str,
    episode_id: str,
) -> list[dict[str, Any]]:
    missing = _batch_missing_orders(
        rows,
        compact_beats,
    )

    if missing:
        additions = (
            await _generate_missing_beat_shots(
                env,
                missing_orders=missing,
                compact_beats=compact_beats,
                anchors=anchors,
                previous_shot=previous_shot,
                next_beat=next_beat,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=scene_id,
                episode_id=episode_id,
            )
        )
        rows = [
            *rows,
            *additions,
        ]

    remaining = _batch_missing_orders(
        rows,
        compact_beats,
    )
    if remaining:
        raise RuntimeError(
            "V2.39.5: Beat 定向补全后仍覆盖不完整；missing="
            + repr(remaining)
        )

    return sorted(
        rows,
        key=lambda row: min(
            _orders(
                row.get("covered_beat_orders")
            )
            or [10**9]
        ),
    )


def _audit_target_shot_indices(
    audit: dict[str, Any],
    *,
    row_count: int,
) -> list[int]:
    issues = (
        audit.get("violations")
        or audit.get("issues")
        or []
    ) if isinstance(audit, dict) else []

    targets: list[int] = []

    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue

            try:
                shot_index = int(
                    issue.get("shot_index")
                    or 0
                )
            except Exception:
                shot_index = 0

            if (
                1 <= shot_index <= row_count
                and shot_index not in targets
            ):
                targets.append(
                    shot_index
                )

    if not targets:
        targets = list(
            range(1, row_count + 1)
        )

    return targets


def _issues_for_shot(
    audit: dict[str, Any],
    shot_index: int,
) -> list[dict[str, Any]]:
    issues = (
        audit.get("violations")
        or audit.get("issues")
        or []
    ) if isinstance(audit, dict) else []

    if not isinstance(issues, list):
        return []

    normalized: list[dict[str, Any]] = [
        copy.deepcopy(issue)
        for issue in issues
        if isinstance(issue, dict)
    ]

    # The ACTIVE Qwen audit may return human-readable violation strings while
    # its rule booleans remain the machine authority. Recover canonical codes
    # from explicit false flags so repair/recovery never sees an empty rule set.
    string_violations = [
        str(issue).strip()
        for issue in issues
        if isinstance(issue, str) and str(issue).strip()
    ]
    flag_codes = (
        ("evidence_entailment_ok", "evidence_entailment"),
        ("beat_coverage_ok", "beat_coverage"),
        ("temporal_order_ok", "state_order"),
        ("no_future_event_preconsumption", "future_preconsumption"),
        ("no_result_duplication", "no_result_duplication"),
        ("state_order_valid", "state_order"),
        ("entity_visibility_valid", "entity_visibility"),
        ("visual_realization_valid", "visual_realization"),
    )
    if string_violations:
        for flag, code in flag_codes:
            if audit.get(flag) is False:
                normalized.append({
                    "code": code,
                    "shot_index": shot_index,
                    "message": " | ".join(string_violations),
                    "source": f"audit_flag:{flag}",
                })
        if not normalized:
            normalized.append({
                "code": "unknown",
                "shot_index": shot_index,
                "message": " | ".join(string_violations),
                "source": "audit_string_violation",
            })

    exact: list[dict[str, Any]] = []

    for issue in normalized:

        try:
            value = int(
                issue.get("shot_index")
                or 0
            )
        except Exception:
            value = 0

        if value == shot_index:
            exact.append(
                copy.deepcopy(issue)
            )

    if exact:
        return exact

    return [
        copy.deepcopy(issue)
        for issue in normalized
        if isinstance(issue, dict)
    ]


def _audit_repair_signature(
    audit: dict[str, Any],
    *,
    row_count: int,
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    return tuple(
        (
            shot_index,
            tuple(sorted({
                _canonical_audit_code(issue)
                for issue in _issues_for_shot(audit, shot_index)
            })),
        )
        for shot_index in _audit_target_shot_indices(
            audit,
            row_count=row_count,
        )
    )


def _locked_evidence_rows(
    row: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    locked = set(
        _id_list(
            row.get("source_evidence_ids")
        )
    )

    return [
        copy.deepcopy(anchor)
        for anchor in anchors or []
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
        in locked
    ]


def _locked_covered_beats(
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    locked = set(
        _orders(
            row.get("covered_beat_orders")
        )
    )

    return [
        copy.deepcopy(beat)
        for beat in compact_beats or []
        if isinstance(beat, dict)
        and int(beat.get("order") or 0)
        in locked
    ]


_AUDIT_CODE_ALIASES: dict[str, str] = {
    "state_order_violation": "state_order",
    "state_order_valid": "state_order",
    "causal_order": "state_order",
    "evidence_entailment_violation": "evidence_entailment",
    "no_result_duplication_violation": "no_result_duplication",
    "redundant_representation_violation": "redundant_representation",
    "representative_state_violation": "representative_state",
    "entity_visibility_violation": "entity_visibility",
    "future_preconsumption": "future_preconsumption",
    "no_future_event_preconsumption": "future_preconsumption",
    "future_preconsumption_violation": "future_preconsumption",
    "beat_coverage_violation": "beat_coverage",
    "visual_realization_violation": "visual_realization",
}


def _canonical_audit_code(issue: dict[str, Any]) -> str:
    """Normalize ACTIVE audit variants without hiding a known rule as unknown."""
    if not isinstance(issue, dict):
        return "unknown"
    raw_values: list[str] = []
    for key in ("code", "type", "rule", "violation_type"):
        raw = str(issue.get(key) or "").strip().casefold()
        if raw:
            raw_values.append(
                re.sub(r"[\s\-]+", "_", raw).strip("_")
            )
    for raw in raw_values:
        canonical = _AUDIT_CODE_ALIASES.get(raw, raw)
        if canonical in _REPAIR_CODE_FIELDS:
            return canonical
        if raw.endswith("_violation"):
            canonical = raw[: -len("_violation")]
            if canonical in _REPAIR_CODE_FIELDS:
                return canonical
    return "unknown"


_REPAIR_CODE_FIELDS: dict[str, tuple[str, ...]] = {
    "evidence_entailment": (
        "summary",
        "action",
        *_SHOT_TEMPORAL_STATE_FIELDS,
    ),
    "no_result_duplication": _SHOT_TEMPORAL_STATE_FIELDS,
    "redundant_representation": _SHOT_TEMPORAL_STATE_FIELDS,
    "representative_state": ("representative_state",),
    # ACTIVE audit's state_order_violation means the accepted start/core
    # states stay locked and only the failed core -> end transition is repaired.
    "state_order": ("video_end_state",),
    "state_handoff": _SHOT_TEMPORAL_STATE_FIELDS,
    "future_preconsumption": (
        "summary",
        "action",
        *_SHOT_TEMPORAL_STATE_FIELDS,
    ),
    # Beat/evidence bindings are immutable in directional repair. A coverage
    # failure therefore routes to regroup/evidence selection without an LLM call.
    "beat_coverage": (),
    "visual_realization": (),
    "entity_visibility": (
        "character_entity_ids",
        "prop_entity_ids",
    ),
}


def _repair_fields_for_issues(
    issues: list[dict[str, Any]],
) -> tuple[str, ...]:
    result: list[str] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        code = _canonical_audit_code(issue)
        fields = _REPAIR_CODE_FIELDS.get(code)
        if fields is None:
            continue
        for field in fields:
            if field not in result:
                result.append(field)

    recognized = any(
        _canonical_audit_code(issue) != "unknown"
        for issue in issues or []
        if isinstance(issue, dict)
    )
    # A genuinely unstructured audit cannot authorize a broad rewrite. The
    # three state fields remain the smallest conservative legacy scope.
    if not result and not recognized:
        result.extend(_SHOT_TEMPORAL_STATE_FIELDS)
    return tuple(result)


def _repair_patch_contract(fields: tuple[str, ...]) -> str:
    values: dict[str, Any] = {}
    for field in fields:
        values[field] = [] if field.endswith("_entity_ids") else ""
    return json.dumps(
        {"patch": values},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _repair_failure_metadata(
    *,
    shot_index: int,
    current: dict[str, Any],
    issues: list[dict[str, Any]],
    candidate: dict[str, Any] | None,
    post: dict[str, Any] | None,
    progress: str,
    exact_evidence: list[dict[str, Any]] | None = None,
    covered_beats: list[dict[str, Any]] | None = None,
    evidence_sufficiency: str = "undetermined",
    regroup_reason: str = "",
) -> dict[str, Any]:
    candidate = candidate if isinstance(candidate, dict) else {}
    post = post if isinstance(post, dict) else current
    projected = _project_prompts_from_states(post)
    return {
        "shot_id": str(current.get("shot_id") or f"Shot {shot_index}"),
        "beat_id": list(_orders(current.get("covered_beat_orders"))),
        "failed_rules": list(dict.fromkeys(
            _canonical_audit_code(issue)
            for issue in issues
            if isinstance(issue, dict)
        )),
        "raw_violations": copy.deepcopy(issues),
        "source_evidence": copy.deepcopy(
            current.get("source_evidence")
            or [row.get("text") for row in (exact_evidence or [])]
        ),
        "covered_beat": copy.deepcopy(covered_beats or []),
        "evidence_ids": list(_id_list(current.get("source_evidence_ids"))),
        "source_spans": copy.deepcopy(
            current.get("source_evidence_spans")
            or exact_evidence
            or []
        ),
        "pre_repair_states": _shot_state_snapshot(current),
        "repair_patch": copy.deepcopy(candidate),
        "post_repair_states": _shot_state_snapshot(post),
        "pre_repair_prompts": _shot_prompt_snapshot(current),
        "post_repair_prompts": _shot_prompt_snapshot(projected),
        "repair_changed_fields": [
            field
            for field in candidate
            if current.get(field) != post.get(field)
        ],
        "repair_progress": progress,
        "evidence_sufficiency": evidence_sufficiency,
        "regroup_reason": regroup_reason,
    }


_SHOT_RECOVERY_BUDGET = {
    "scoped_repair": 1,
    "evidence_regroup": 1,
    "shot_regeneration": 1,
    "final_strict_audit": 1,
}


def _stage04_progress(
    env: dict[str, Any],
    phase_index: int,
    phase_name: str,
    message: str,
) -> None:
    callback = env.get("_studio_stage04_progress_update")
    if callable(callback):
        callback(
            phase_index=phase_index,
            phase_total=6,
            phase_name=phase_name,
            message=message,
        )


def _evidence_fingerprint(
    *,
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> str:
    beat_rows = []
    for beat in compact_beats or []:
        if not isinstance(beat, dict):
            continue
        beat_rows.append({
            "beat_id": int(beat.get("order") or 0),
            "lineage_beat_orders": list(_orders(
                beat.get("lineage_beat_orders") or [beat.get("order")]
            )),
            "evidence_ids": list(_id_list(
                beat.get("allowed_source_evidence_ids")
                or beat.get("source_evidence_ids")
            )),
            "source_spans": copy.deepcopy(
                beat.get("source_evidence_spans") or []
            ),
            "source_evidence_hash": hashlib.sha256(
                json.dumps(
                    beat.get("source_evidence") or [],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        })
    anchor_rows = [
        {
            "anchor_id": str(anchor.get("id") or ""),
            "beat_order": int(anchor.get("beat_order") or 0),
            "source_start": int(anchor.get("source_start") or 0),
            "source_end": int(anchor.get("source_end") or 0),
            "text_hash": hashlib.sha256(
                str(anchor.get("text") or "").encode("utf-8")
            ).hexdigest(),
        }
        for anchor in anchors or []
        if isinstance(anchor, dict)
    ]
    return hashlib.sha256(
        json.dumps(
            {"beats": beat_rows, "anchors": anchor_rows},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()



async def _reconsider_edge_beat_temporal_mode(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context: str,
) -> dict[str, Any] | None:
    """Reconsider a first/edge Beat after observable progression proved impossible.

    This is deliberately target-evidence-only.  It never borrows the next Beat
    as fact authority.  A stable ongoing activity may be represented through the
    existing static_outcome contract when the source proves the activity/state
    itself but does not prove internal before/middle/end milestones.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "observable_transition":
        return None

    evidence_ids = _id_list(item.get("source_evidence_ids"))
    covered_orders = _orders(item.get("covered_beat_orders"))
    anchor_map = {
        str(anchor.get("id") or ""): anchor
        for anchor in anchors or []
        if isinstance(anchor, dict) and str(anchor.get("id") or "")
    }
    beat_map = {
        int(beat.get("order") or 0): beat
        for beat in compact_beats or []
        if isinstance(beat, dict) and int(beat.get("order") or 0) > 0
    }
    exact_evidence = [
        copy.deepcopy(anchor_map[evidence_id])
        for evidence_id in evidence_ids
        if evidence_id in anchor_map
    ]
    locked_beats = [
        copy.deepcopy(beat_map[order])
        for order in covered_orders
        if order in beat_map
    ]
    metadata_base = {
        "failed_rule": "edge_temporal_mode_reconsideration",
        "failed_rules": ["state_order", "temporal_progression"],
        "pre_repair_candidate": copy.deepcopy(item),
        "pre_repair_states": _shot_state_snapshot(item),
        "evidence_ids": evidence_ids,
        "beat_id": covered_orders,
        "exact_selected_evidence": copy.deepcopy(exact_evidence),
        "locked_beats": copy.deepcopy(locked_beats),
        "repair_context": context,
        "recovery_scope": "target_evidence_only_no_future_borrowing",
    }
    if (
        not evidence_ids
        or not covered_orders
        or len(exact_evidence) != len(evidence_ids)
        or len(locked_beats) != len(covered_orders)
    ):
        raise Stage04ShotRepairError(
            f"{context}: edge Beat temporal_mode 重分类无法完整锁定当前 Beat/evidence",
            metadata={
                **metadata_base,
                "repair_progress": "edge_temporal_reconsideration_scope_incomplete",
                "evidence_sufficiency": "undetermined",
            },
        )

    system_prompt = (
        "你是 strict-shot-v2 edge Beat temporal_mode 重分类器。"
        "当前 Shot 原本被分类为 observable_transition，但严格审计已经证明现有 evidence "
        "无法支持可证明的 start→representative→end 前向时间链。"
        "只能使用 EXACT_SELECTED_EVIDENCE 和 LOCKED_BEAT 重新分类，绝对不能借用下一 Beat。"
        "observable_transition 仅适用于证据直接证明至少三个可区分前向状态的情况。"
        "static_outcome 不只包括已成立结果/关系，也包括证据明确证明一个正在持续的稳定活动/状态，"
        "但没有直接证明该活动内部的前/中/后里程碑；此时剧情事实保持稳定，变化只能放在"
        " presentation-only visual realization。"
        "insufficient_visual_evidence 表示连稳定可视觉化事实也无法从当前证据直接建立。"
        "只能返回 observable_transition、static_outcome、insufficient_visual_evidence 三者之一。"
        "必须给出 evidence-based reason 和当前 evidence ids；只返回严格 JSON。"
    )
    prompt = (
        "=== LOCKED_BEAT ===\n"
        + json.dumps(locked_beats, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(exact_evidence, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== FAILED_OBSERVABLE_SHOT ===\n"
        + json.dumps(
            {
                "source_fact": item.get("source_fact"),
                "summary": item.get("summary"),
                "action": item.get("action"),
                "video_start_state": item.get("video_start_state"),
                "representative_state": item.get("representative_state"),
                "video_end_state": item.get("video_end_state"),
                "narrative_start_state": item.get("narrative_start_state"),
                "narrative_state": item.get("narrative_state"),
                "narrative_end_state": item.get("narrative_end_state"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    contract = json.dumps(
        {
            "temporal_mode": "static_outcome",
            "temporal_mode_reason": "",
            "temporal_mode_evidence_ids": [evidence_ids[0]],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    raw, parsed, _ = await _qwen(
        env,
        phase="studio_stage04_edge_temporal_mode_reconsideration_qwen32b",
        system_prompt=system_prompt,
        prompt=prompt,
        contract=contract,
        max_tokens=520,
        temperature=0.0,
    )
    obj = _parse_object(env, raw, parsed)
    mode = str(obj.get("temporal_mode") or "").strip().lower()
    reason = str(obj.get("temporal_mode_reason") or "").strip()
    mode_evidence_ids = _id_list(obj.get("temporal_mode_evidence_ids"))
    output = {
        "temporal_mode": mode,
        "temporal_mode_reason": reason,
        "temporal_mode_evidence_ids": mode_evidence_ids,
    }
    if (
        mode not in _TEMPORAL_MODES
        or not reason
        or not mode_evidence_ids
        or not set(mode_evidence_ids).issubset(set(evidence_ids))
    ):
        raise Stage04ShotRepairError(
            f"{context}: edge Beat temporal_mode 重分类输出不合法",
            metadata={
                **metadata_base,
                "repair_output": output,
                "repair_progress": "edge_temporal_reconsideration_invalid_output",
                "evidence_sufficiency": "undetermined",
            },
        )

    if mode == "observable_transition":
        return None

    if mode == "insufficient_visual_evidence":
        raise Stage04ShotRepairError(
            f"{context}: 当前 Beat 证据不足以形成 grounded Shot",
            metadata={
                **metadata_base,
                "repair_output": output,
                "repair_progress": "edge_temporal_reconsideration_insufficient",
                "evidence_sufficiency": "insufficient_visual_evidence",
                "regroup_reason": "target-only evidence remains insufficient after temporal reconsideration",
            },
        )

    repaired = copy.deepcopy(item)
    repaired["temporal_mode"] = "static_outcome"
    repaired["temporal_mode_reason"] = reason
    repaired["temporal_mode_evidence_ids"] = mode_evidence_ids
    try:
        repaired = await _repair_static_outcome_payload_consistency(
            env,
            row=repaired,
            compact_beats=compact_beats,
            anchors=anchors,
            context=context + " static fallback",
        )
    except Exception as exc:
        metadata = copy.deepcopy(getattr(exc, "metadata", {}) or {})
        metadata.update({
            **metadata_base,
            "repair_output": output,
            "repair_progress": metadata.get("repair_progress") or "edge_static_payload_repair_failed",
            "evidence_sufficiency": metadata.get("evidence_sufficiency") or "undetermined",
        })
        raise Stage04ShotRepairError(
            f"{context}: edge Beat static_outcome payload 修复失败：{exc}",
            metadata=metadata,
        ) from exc

    repaired["_edge_temporal_reconsideration_diagnostics"] = {
        **metadata_base,
        "repair_output": output,
        "post_repair_candidate": copy.deepcopy(repaired),
        "post_repair_states": _shot_state_snapshot(repaired),
        "repair_progress": "edge_temporal_reclassified_static_outcome",
        "evidence_sufficiency": "sufficient_for_stable_state",
    }
    return repaired


def _reselect_adjacent_evidence(
    env: dict[str, Any],
    *,
    source: str,
    target_beat: dict[str, Any],
    all_beats: list[dict[str, Any]],
    current_compact_beats: list[dict[str, Any]],
    current_anchors: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    target_order = int(target_beat.get("order") or 0)
    previous_beat = next((
        beat for beat in all_beats
        if isinstance(beat, dict)
        and int(beat.get("order") or 0) == target_order - 1
    ), None)
    before = _evidence_fingerprint(
        compact_beats=current_compact_beats,
        anchors=current_anchors,
    )
    metadata = {
        "recovery_budget": copy.deepcopy(_SHOT_RECOVERY_BUDGET),
        "recovery_usage": {
            "scoped_repair": 1,
            "evidence_regroup": 1,
            "shot_regeneration": 0,
            "final_strict_audit": 0,
        },
        "evidence_fingerprint_before": before,
        "recovery_scope": {
            "target_beat_order": target_order,
            "adjacent_beat_order": target_order - 1,
            "mode": "previous_adjacent_evidence_expansion",
        },
    }
    if previous_beat is None:
        metadata.update({
            "repair_progress": "evidence_regroup_no_progress",
            "regroup_reason": "no previous adjacent Beat is available",
            "evidence_fingerprint_after": before,
        })
        raise Stage04ShotRepairError(
            f"Beat {target_order} evidence regroup 无可用前序相邻证据",
            metadata=metadata,
        )

    evidence_builder = env.get("_studio_v2371e_batch_evidence")
    if not callable(evidence_builder):
        raise RuntimeError("V2.39.5: Beat→Shot evidence builder 不可用")
    source_window, recovered_anchors, mapping = evidence_builder(
        source=source,
        batch=[previous_beat, target_beat],
        max_context_chars=1900,
    )
    expanded_ids = list(dict.fromkeys([
        *(mapping.get(target_order - 1) or []),
        *(mapping.get(target_order) or []),
    ]))
    target_compact = copy.deepcopy(next((
        beat for beat in current_compact_beats
        if int(beat.get("order") or 0) == target_order
    ), _compact_beats([target_beat], mapping)[0]))
    target_compact.update({
        "allowed_source_evidence_ids": expanded_ids,
        "source_evidence_ids": expanded_ids,
        "source_evidence": [
            str(anchor.get("text") or "")
            for anchor in recovered_anchors
            if str(anchor.get("id") or "") in expanded_ids
        ],
        "source_evidence_spans": [
            {
                "id": str(anchor.get("id") or ""),
                "start": int(anchor.get("source_start") or 0),
                "end": int(anchor.get("source_end") or 0),
                "text": str(anchor.get("text") or ""),
            }
            for anchor in recovered_anchors
            if str(anchor.get("id") or "") in expanded_ids
        ],
        "lineage_beat_orders": [target_order - 1, target_order],
        "evidence_recovery": "previous_adjacent_evidence_expansion",
    })
    recovered_compact = [target_compact]
    after = _evidence_fingerprint(
        compact_beats=recovered_compact,
        anchors=recovered_anchors,
    )
    metadata["evidence_fingerprint_after"] = after
    metadata["reselected_evidence_ids"] = expanded_ids
    old_ids = set(_id_list(
        current_compact_beats[0].get("allowed_source_evidence_ids")
        if current_compact_beats else []
    ))
    if after == before or not (set(expanded_ids) - old_ids):
        metadata.update({
            "repair_progress": "evidence_regroup_no_progress",
            "regroup_reason": "adjacent evidence fingerprint did not change",
        })
        raise Stage04ShotRepairError(
            f"Beat {target_order} evidence_regroup_no_progress",
            metadata=metadata,
        )
    return source_window, recovered_anchors, recovered_compact, metadata


async def _regenerate_shot_from_reselected_evidence(
    env: dict[str, Any],
    *,
    target_order: int,
    compact_beat: dict[str, Any],
    anchors: list[dict[str, Any]],
    previous_shot: dict[str, Any] | None,
    next_beat: dict[str, Any] | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    scene_id: str,
    episode_id: str,
) -> list[dict[str, Any]]:
    allowed_ids = set(_id_list(
        compact_beat.get("allowed_source_evidence_ids")
    ))
    allowed_anchors = [
        copy.deepcopy(anchor)
        for anchor in anchors
        if str(anchor.get("id") or "") in allowed_ids
    ]
    system_prompt = (
        "你是 strict-shot-v2 局部 evidence regroup 后的 Shot 重生器。"
        "必须基于 RESELECTED_EVIDENCE 重新生成一个全新 Shot，不能 patch OLD_SHOT。"
        "只覆盖 TARGET_BEAT；source_evidence_ids 只能从允许列表选择。"
        "必须直接根据 RESELECTED_EVIDENCE 分类 temporal_mode，并返回分类 reason 和 evidence ids；"
        "不得用题材关键词判断。observable_transition 才要求三 narrative state 形成证据支持的"
        "可见前向状态链。static_outcome 也适用于证据只证明持续活动/稳定状态但不证明内部前中后里程碑；"
        "static_outcome 必须保持 narrative state 稳定，并把构图、机位、光影、"
        "环境或镜头运动隔离在 presentation_only visual realization 字段；这些表现推断不得写入"
        " source_fact/summary/action。insufficient_visual_evidence 必须如实返回，不能虚构动作。"
        "不得重复 PREVIOUS_ACCEPTED_SHOT，不得消费 NEXT_BEAT。"
        "必须包含完整 strict-shot-v2 字段并只返回严格 JSON。"
    )
    prompt = (
        "=== TARGET_BEAT_WITH_RESELECTED_EVIDENCE ===\n"
        + json.dumps(compact_beat, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== RESELECTED_EVIDENCE ===\n"
        + json.dumps(allowed_anchors, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== PREVIOUS_ACCEPTED_SHOT ===\n"
        + json.dumps(previous_shot or {}, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n"
        + json.dumps(next_beat or {}, ensure_ascii=False, separators=(",", ":"))
    )
    raw, parsed, _ = await _qwen(
        env,
        phase="studio_stage04_regroup_shot_regeneration_qwen32b",
        system_prompt=system_prompt,
        prompt=prompt,
        contract=_shot_generation_contract(target_order),
        max_tokens=1700,
        temperature=0.0,
    )
    candidates = _extract_shots(env, raw, parsed)
    if not candidates:
        raise RuntimeError("regroup Shot regeneration 未返回 Shot")
    repaired_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        normalized, _origin = _normalize_raw_shot_binding(
            candidate,
            compact_beats=[compact_beat],
            anchors=allowed_anchors,
        )
        if normalized is None:
            continue
        repaired = await _repair_invalid_temporal_mode_classification(
            env,
            row=normalized,
            compact_beats=[compact_beat],
            anchors=allowed_anchors,
            context=f"Beat {target_order} regroup regeneration",
        )
        repaired = await _repair_static_outcome_payload_consistency(
            env,
            row=repaired,
            compact_beats=[compact_beat],
            anchors=allowed_anchors,
            context=f"Beat {target_order} regroup regeneration",
        )
        repaired = await _repair_observable_transition_state_consistency(
            env,
            row=repaired,
            compact_beats=[compact_beat],
            anchors=allowed_anchors,
            context=f"Beat {target_order} regroup regeneration",
        )
        repaired_candidates.append(repaired)
    if not repaired_candidates:
        raise RuntimeError("regroup Shot regeneration 没有可验证的 evidence-locked Shot")
    return validate_rows(
        env,
        raw_rows=repaired_candidates,
        compact_beats=[compact_beat],
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        anchors=allowed_anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )


async def _recover_single_beat_after_scoped_repair(
    env: dict[str, Any],
    *,
    source: str,
    target_beat: dict[str, Any],
    all_beats: list[dict[str, Any]],
    current_compact_beats: list[dict[str, Any]],
    current_anchors: list[dict[str, Any]],
    previous_shot: dict[str, Any] | None,
    next_beat: dict[str, Any] | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    scene_id: str,
    episode_id: str,
    audit_fn: Any,
    prior_metadata: dict[str, Any],
    current_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target_order = int(target_beat.get("order") or 0)

    previous_beat = next((
        beat for beat in all_beats
        if isinstance(beat, dict)
        and int(beat.get("order") or 0) == target_order - 1
    ), None)

    # A first/edge Beat has no earlier fact authority to borrow from.  Before
    # declaring regroup impossible, reconsider whether strict audit has shown
    # that the Beat is a stable state/ongoing activity rather than a provable
    # three-milestone transition.  This path is target-evidence-only and never
    # consumes NEXT_BEAT.
    if target_order == 1 and previous_beat is None and current_rows:
        edge_candidate = await _reconsider_edge_beat_temporal_mode(
            env,
            row=current_rows[0],
            compact_beats=current_compact_beats,
            anchors=current_anchors,
            context=f"Beat {target_order} edge recovery",
        )
        if edge_candidate is not None:
            try:
                edge_rows = validate_rows(
                    env,
                    raw_rows=[edge_candidate],
                    compact_beats=current_compact_beats,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    anchors=current_anchors,
                    scene_id=scene_id,
                    episode_id=episode_id,
                )
                edge_source_window = "\n".join(
                    str(anchor.get("text") or "")
                    for anchor in current_anchors
                    if isinstance(anchor, dict)
                )
                edge_audit = await audit_fn(
                    source_window=edge_source_window,
                    compact_beats=current_compact_beats,
                    shots=edge_rows,
                )
            except Exception as exc:
                prior_metadata["edge_temporal_reconsideration"] = {
                    "repair_progress": "edge_temporal_reconsideration_validation_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                if _audit_ok(env, edge_audit):
                    diagnostics = copy.deepcopy(
                        edge_candidate.get("_edge_temporal_reconsideration_diagnostics")
                        or {}
                    )
                    diagnostics.update({
                        "repair_progress": "edge_temporal_reconsideration_passed_strict_audit",
                        "final_audit": copy.deepcopy(edge_audit),
                        "prior_repair": copy.deepcopy(prior_metadata),
                        "recovery_usage": {
                            "scoped_repair": 1,
                            "edge_temporal_reconsideration": 1,
                            "evidence_regroup": 0,
                            "shot_regeneration": 0,
                            "final_strict_audit": 1,
                        },
                    })
                    for edge_row in edge_rows:
                        edge_row["_regroup_recovery_diagnostics"] = copy.deepcopy(diagnostics)
                    return edge_rows, edge_audit
                prior_metadata["edge_temporal_reconsideration"] = {
                    "repair_progress": "edge_temporal_reconsideration_failed_strict_audit",
                    "audit": copy.deepcopy(edge_audit),
                }

    # Beat 1 cannot expand evidence backward because no previous Beat exists.
    # If temporal reconsideration did not close the Shot, regenerate one fresh
    # Shot from the current Beat's locked evidence only.  This is not evidence
    # expansion: NEXT_BEAT is deliberately hidden and the evidence fingerprint
    # must remain unchanged.  The regenerated Shot still has to pass the normal
    # strict-shot-v2 validator and final semantic audit.
    if target_order == 1 and previous_beat is None:
        edge_compact = next((
            copy.deepcopy(beat)
            for beat in current_compact_beats
            if isinstance(beat, dict)
            and int(beat.get("order") or 0) == target_order
        ), None)
        if edge_compact is None:
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only regeneration 缺少锁定 Beat",
                metadata={
                    "repair_progress": "edge_target_only_regeneration_scope_incomplete",
                    "prior_repair": copy.deepcopy(prior_metadata),
                    "recovery_scope": "target_evidence_only_no_future_borrowing",
                },
            )

        edge_allowed_ids = set(_id_list(
            edge_compact.get("allowed_source_evidence_ids")
            or edge_compact.get("source_evidence_ids")
        ))
        edge_anchors = [
            copy.deepcopy(anchor)
            for anchor in current_anchors
            if isinstance(anchor, dict)
            and str(anchor.get("id") or "") in edge_allowed_ids
        ]
        if not edge_allowed_ids or len(edge_anchors) != len(edge_allowed_ids):
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only regeneration 无法完整锁定当前 evidence",
                metadata={
                    "repair_progress": "edge_target_only_regeneration_scope_incomplete",
                    "prior_repair": copy.deepcopy(prior_metadata),
                    "evidence_ids": sorted(edge_allowed_ids),
                    "recovery_scope": "target_evidence_only_no_future_borrowing",
                },
            )

        edge_source_window = "\n".join(
            str(anchor.get("text") or "")
            for anchor in edge_anchors
        )
        edge_fingerprint = _evidence_fingerprint(
            compact_beats=[edge_compact],
            anchors=edge_anchors,
        )
        edge_recovery = {
            "recovery_budget": copy.deepcopy(_SHOT_RECOVERY_BUDGET),
            "recovery_usage": {
                "scoped_repair": 1,
                "edge_temporal_reconsideration": 1 if current_rows else 0,
                "evidence_regroup": 0,
                "shot_regeneration": 1,
                "final_strict_audit": 0,
            },
            "repair_progress": "edge_target_only_regeneration_started",
            "prior_repair": copy.deepcopy(prior_metadata),
            "evidence_ids": sorted(edge_allowed_ids),
            "evidence_fingerprint_before": edge_fingerprint,
            "evidence_fingerprint_after": edge_fingerprint,
            "recovery_scope": {
                "target_beat_order": target_order,
                "mode": "target_only_shot_regeneration_no_future_borrowing",
            },
        }
        _stage04_progress(
            env, 5, "Edge recovery", "正在使用当前 Beat 锁定证据重生首镜头"
        )
        try:
            edge_regenerated = await _regenerate_shot_from_reselected_evidence(
                env,
                target_order=target_order,
                compact_beat=edge_compact,
                anchors=edge_anchors,
                previous_shot=None,
                next_beat=None,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=scene_id,
                episode_id=episode_id,
            )
        except Exception as exc:
            edge_recovery.update({
                "repair_progress": "edge_target_only_regeneration_failed",
                "regroup_reason": f"{type(exc).__name__}: {exc}",
            })
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only Shot 重生失败：{exc}",
                metadata=edge_recovery,
            ) from exc

        edge_recovery["recovery_usage"]["final_strict_audit"] = 1
        edge_final_audit = await audit_fn(
            source_window=edge_source_window,
            compact_beats=[edge_compact],
            shots=edge_regenerated,
        )
        edge_recovery["final_audit"] = copy.deepcopy(edge_final_audit)
        if not _audit_ok(env, edge_final_audit):
            edge_recovery.update({
                "repair_progress": "edge_target_only_regeneration_failed_strict_audit",
                "regroup_reason": "target-only regenerated Shot did not pass strict-shot-v2",
            })
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only Shot 仍未通过 strict-shot-v2："
                + _audit_issues(edge_final_audit),
                metadata=edge_recovery,
            )

        edge_recovery["repair_progress"] = (
            "edge_target_only_regeneration_passed_strict_audit"
        )
        for edge_row in edge_regenerated:
            edge_row["_regroup_recovery_diagnostics"] = copy.deepcopy(edge_recovery)
        return edge_regenerated, edge_final_audit

    _stage04_progress(
        env, 5, "Regroup recovery", "正在重新选择镜头证据"
    )
    try:
        source_window, anchors, compact_beats, recovery = (
            _reselect_adjacent_evidence(
                env,
                source=source,
                target_beat=target_beat,
                all_beats=all_beats,
                current_compact_beats=current_compact_beats,
                current_anchors=current_anchors,
            )
        )
    except Stage04ShotRepairError as exc:
        recovery = copy.deepcopy(exc.metadata)
        recovery["prior_repair"] = copy.deepcopy(prior_metadata)
        for key in (
            "shot_id",
            "beat_id",
            "failed_rules",
            "raw_violations",
            "source_evidence",
            "covered_beat",
            "evidence_ids",
            "source_spans",
        ):
            if key not in recovery and key in prior_metadata:
                recovery[key] = copy.deepcopy(prior_metadata[key])
        raise Stage04ShotRepairError(
            str(exc),
            metadata=recovery,
        ) from exc
    recovery["prior_repair"] = copy.deepcopy(prior_metadata)
    for key in (
        "shot_id",
        "beat_id",
        "failed_rules",
        "raw_violations",
    ):
        if key in prior_metadata:
            recovery[key] = copy.deepcopy(prior_metadata[key])
    recovery["recovery_usage"]["shot_regeneration"] = 1
    try:
        regenerated = await _regenerate_shot_from_reselected_evidence(
            env,
            target_order=target_order,
            compact_beat=compact_beats[0],
            anchors=anchors,
            previous_shot=previous_shot,
            next_beat=next_beat,
            allowed_chars=allowed_chars,
            allowed_props=allowed_props,
            scene_id=scene_id,
            episode_id=episode_id,
        )
    except Exception as exc:
        recovery.update({
            "repair_progress": "shot_regeneration_failed",
            "regroup_reason": f"{type(exc).__name__}: {exc}",
        })
        raise Stage04ShotRepairError(
            f"Beat {target_order} 使用新 evidence 重生 Shot 失败：{exc}",
            metadata=recovery,
        ) from exc
    recovery["recovery_usage"]["final_strict_audit"] = 1
    final_audit = await audit_fn(
        source_window=source_window,
        compact_beats=compact_beats,
        shots=regenerated,
    )
    recovery["final_audit"] = copy.deepcopy(final_audit)
    if not _audit_ok(env, final_audit):
        recovery.update({
            "repair_progress": "regenerated_shot_failed_strict_audit",
            "regroup_reason": "regenerated Shot did not pass strict-shot-v2",
        })
        raise Stage04ShotRepairError(
            f"Beat {target_order} 新 evidence Shot 仍未通过 strict-shot-v2："
            + _audit_issues(final_audit),
            metadata=recovery,
        )
    recovery["repair_progress"] = "regenerated_shot_passed_strict_audit"
    for row in regenerated:
        row["_regroup_recovery_diagnostics"] = copy.deepcopy(recovery)
    return regenerated, final_audit


async def _repair_batch(
    env: dict[str, Any],
    *,
    current_rows: list[dict[str, Any]],
    audit: dict[str, Any],
    source_window: str,
    anchors: list[dict[str, Any]],
    compact_beats: list[dict[str, Any]],
    previous_shot: dict[str, Any] | None,
    next_beat: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Field-scoped directional repair from the Shot's exact selected evidence.
    Already-valid fields, Beat bindings, evidence spans, and entity bindings
    remain immutable unless their specific audit rule failed.
    """
    if not current_rows:
        return []

    result = copy.deepcopy(
        current_rows
    )

    targets = _audit_target_shot_indices(
        audit,
        row_count=len(result),
    )

    for shot_index in targets:
        index = shot_index - 1
        current = result[index]

        locked_orders = list(
            _orders(
                current.get("covered_beat_orders")
            )
        )
        locked_evidence_ids = list(
            _id_list(
                current.get("source_evidence_ids")
            )
        )

        if not locked_orders:
            raise RuntimeError(
                f"V2.39.5: Shot#{shot_index} 没有可锁定 Beat"
            )

        if not locked_evidence_ids:
            raise RuntimeError(
                f"V2.39.5: Shot#{shot_index} 没有可锁定 evidence"
            )

        exact_evidence = _locked_evidence_rows(
            current,
            anchors,
        )

        covered_beats = _locked_covered_beats(
            current,
            compact_beats,
        )

        actual_evidence = {
            str(row.get("id") or "")
            for row in exact_evidence
        }

        if actual_evidence != set(
            locked_evidence_ids
        ):
            raise RuntimeError(
                f"V2.39.5: Shot#{shot_index} 锁定 evidence 解析不完整"
            )

        actual_orders = {
            int(row.get("order") or 0)
            for row in covered_beats
        }

        if actual_orders != set(
            locked_orders
        ):
            raise RuntimeError(
                f"V2.39.5: Shot#{shot_index} 锁定 Beat 解析不完整"
            )

        shot_issues = _issues_for_shot(
            audit,
            shot_index,
        )

        previous_context = (
            result[index - 1]
            if index > 0
            else previous_shot
        )

        next_current = (
            result[index + 1]
            if index + 1 < len(result)
            else None
        )

        repair_fields = _repair_fields_for_issues(
            shot_issues
        )
        routed_issues = [
            {
                **copy.deepcopy(issue),
                "repair_code": _canonical_audit_code(issue),
                "repair_fields": list(
                    _REPAIR_CODE_FIELDS.get(
                        _canonical_audit_code(issue),
                        _SHOT_TEMPORAL_STATE_FIELDS,
                    )
                ),
                **(
                    {
                        "failed_transition":
                            "representative_state_to_video_end_state"
                    }
                    if _canonical_audit_code(issue) == "state_order"
                    else {}
                ),
            }
            for issue in shot_issues
            if isinstance(issue, dict)
        ]

        if not repair_fields:
            metadata = _repair_failure_metadata(
                shot_index=shot_index,
                current=current,
                issues=shot_issues,
                candidate=None,
                post=None,
                progress="needs_regrouping_or_evidence_selection",
                exact_evidence=exact_evidence,
                covered_beats=covered_beats,
                evidence_sufficiency="insufficient_for_locked_directional_repair",
                regroup_reason=(
                    "audit rule requires Beat/evidence coverage changes that "
                    "directional repair is not allowed to make"
                ),
            )
            raise Stage04ShotRepairError(
                f"V2.39.5: Shot#{shot_index} 必须回到 grouping/evidence selection；"
                "锁定字段级 repair 无权修改 Beat/evidence binding",
                metadata=metadata,
            )

        current_shot = {
            field: copy.deepcopy(current.get(field))
            for field in (
                "summary",
                "action",
                *_SHOT_TEMPORAL_STATE_FIELDS,
                *_SHOT_PROMPT_FIELDS,
                "character_entity_ids",
                "prop_entity_ids",
                "covered_beat_orders",
                "source_evidence_ids",
            )
        }

        system_prompt = (
            "你是 strict-shot-v2 evidence-locked 修复器。"
            "当前 Shot 的 Beat 和 source evidence 已被系统锁定，不能修改。"
            "这是字段级 directional repair，不是重新生成整个 Shot。"
            "只修改 FAILED_FIELDS；CURRENT_SHOT 中其他字段已经合法，必须保持不变。"
            "对输出中的每个独立事实命题，都必须能由至少一个 "
            "EXACT_SELECTED_EVIDENCE 直接蕴含；不能直接蕴含的命题必须删除。"
            "LOCKED_COVERED_BEATS 规定需要表达的状态变化，但不能扩大证据事实边界。"
            "video_start_state→representative_state→video_end_state "
            "必须形成同一 Shot 的前向状态链。"
            "状态必须是从证据中可直接观察、可拍摄的 physical/visual state，"
            "不能用抽象动作阶段、未来意图或叙事概括伪造时间推进。"
            "若 FAILED_TRANSITION 仅为 representative_state_to_video_end_state，"
            "必须锁定 start 和 representative，只返回 evidence 支持的 end。"
            "不得提前消费 NEXT_BEAT_PREVIEW_DO_NOT_CONSUME，"
            "不得重复 PREVIOUS_ACCEPTED_SHOT 已完成的结果。"
            "若证据不足以形成三个不同的前向时间状态，不得复制证据或同一状态三次，"
            "应将对应 patch 字段留空，让程序回到 grouping/evidence selection。"
            "不要输出三个 Prompt；程序只从三状态确定性投影 Prompt。"
            "人物/道具字段只有出现在 FAILED_FIELDS 时才允许返回。"
            "只返回严格 JSON patch；不得返回 covered_beat_orders、"
            "source_evidence_ids、span、duration 或视觉制作字段。"
        )

        prompt = (
            "=== EXACT_SELECTED_EVIDENCE ===\n"
            + json.dumps(
                exact_evidence,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== LOCKED_COVERED_BEATS ===\n"
            + json.dumps(
                covered_beats,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== CURRENT_SHOT ===\n"
            + json.dumps(
                current_shot,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== FAILED_FIELDS ===\n"
            + json.dumps(
                list(repair_fields),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== FAILED_AUDIT_RULES ===\n"
            + json.dumps(
                routed_issues,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== PREVIOUS_ACCEPTED_SHOT ===\n"
            + json.dumps(
                previous_context or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== NEXT_CURRENT_SHOT_CONTEXT_ONLY ===\n"
            + json.dumps(
                next_current or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n"
            + json.dumps(
                next_beat or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        try:
            raw, parsed, _ = await _qwen(
                env,
                phase=(
                    "studio_stage04_"
                    "v2383_evidence_locked_repair_qwen32b"
                ),
                system_prompt=system_prompt,
                prompt=prompt,
                contract=_repair_patch_contract(
                    repair_fields
                ),
                max_tokens=1100,
                temperature=0.0,
            )
        except Exception as exc:
            metadata = _repair_failure_metadata(
                shot_index=shot_index,
                current=current,
                issues=shot_issues,
                candidate=None,
                post=None,
                progress="qwen_call_failed",
                exact_evidence=exact_evidence,
                covered_beats=covered_beats,
                regroup_reason="scoped repair Qwen call failed",
            )
            raise Stage04ShotRepairError(
                f"V2.39.5: Shot#{shot_index} evidence-locked repair 调用失败："
                f"{type(exc).__name__}: {exc}",
                metadata=metadata,
            ) from exc

        candidate: dict[str, Any] | None = None
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("patch"), dict)
        ):
            candidate = dict(parsed["patch"])
        elif (
            isinstance(parsed, dict)
            and isinstance(parsed.get("shot"), dict)
        ):
            candidate = dict(parsed["shot"])
        elif isinstance(parsed, dict):
            candidate = dict(parsed)

        candidate = {
            field: copy.deepcopy(candidate[field])
            for field in repair_fields
            if isinstance(candidate, dict)
            and field in candidate
        }

        usable_candidate = any(
            (
                isinstance(candidate.get(field), str)
                and bool(str(candidate.get(field) or "").strip())
            )
            or (
                field.endswith("_entity_ids")
                and isinstance(candidate.get(field), list)
            )
            for field in repair_fields
        ) if isinstance(candidate, dict) else False

        if not candidate or not usable_candidate:
            metadata = _repair_failure_metadata(
                shot_index=shot_index,
                current=current,
                issues=shot_issues,
                candidate=candidate,
                post=None,
                progress="needs_regrouping_or_evidence_selection",
                exact_evidence=exact_evidence,
                covered_beats=covered_beats,
                evidence_sufficiency="insufficient",
                regroup_reason=(
                    "scoped repair returned no evidence-supported value for "
                    + ", ".join(repair_fields)
                ),
            )
            raise Stage04ShotRepairError(
                f"V2.39.5: Shot#{shot_index} directional repair 没有可用字段；"
                "现有 evidence 不足时必须回到 grouping/evidence selection",
                metadata=metadata,
            )

        try:
            repaired = _merge_shot_repair_patch(
                current,
                candidate,
                writable_fields=repair_fields,
            )
        except Stage04RepairInvariantError as exc:
            metadata = _repair_failure_metadata(
                shot_index=shot_index,
                current=current,
                issues=shot_issues,
                candidate=candidate,
                post=(getattr(exc, "metadata", {}) or {}).get("post") or current,
                progress="needs_regrouping_or_evidence_selection",
                exact_evidence=exact_evidence,
                covered_beats=covered_beats,
                evidence_sufficiency="insufficient_or_invalid_projection",
                regroup_reason="scoped repair violated strict-shot-v2 invariants",
            )
            metadata.update(getattr(exc, "metadata", {}) or {})
            metadata["repair_progress"] = (
                "needs_regrouping_or_evidence_selection"
            )
            metadata["failed_rules"] = list(dict.fromkeys([
                *metadata.get("failed_rules", []),
                *[
                    _canonical_audit_code(issue)
                    for issue in shot_issues
                    if isinstance(issue, dict)
                ],
            ]))
            raise Stage04ShotRepairError(
                f"V2.39.5: Shot#{shot_index} directional repair 被严格不变量拒绝：{exc}",
                metadata=metadata,
            ) from exc

        repaired["covered_beat_orders"] = locked_orders
        repaired["source_evidence_ids"] = locked_evidence_ids

        semantic_changed = (
            _shot_semantic_fingerprint(repaired)
            != _shot_semantic_fingerprint(current)
        )
        if not semantic_changed:
            metadata = _repair_failure_metadata(
                shot_index=shot_index,
                current=current,
                issues=shot_issues,
                candidate=candidate,
                post=repaired,
                progress="needs_regrouping_or_evidence_selection",
                exact_evidence=exact_evidence,
                covered_beats=covered_beats,
                evidence_sufficiency="undetermined_after_scoped_repair",
                regroup_reason="scoped repair made no semantic progress",
            )
            raise Stage04ShotRepairError(
                f"V2.39.5: Shot#{shot_index} scoped repair 无语义进展；"
                "拒绝重复相同请求并回到 grouping/evidence selection",
                metadata=metadata,
            )

        repaired["_directional_repair_diagnostics"] = (
            _repair_failure_metadata(
                shot_index=shot_index,
                current=current,
                issues=shot_issues,
                candidate=candidate,
                post=repaired,
                progress="semantic_fields_changed",
                exact_evidence=exact_evidence,
                covered_beats=covered_beats,
                evidence_sufficiency="sufficient_for_scoped_repair",
            )
        )
        result[index] = repaired

    return result




async def _boundary_audit(
    env: dict[str, Any],
    *,
    previous_shot: dict[str, Any],
    current_shot: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = (
        "你是 strict-shot-v2 相邻镜头边界审计器。"
        "只判断前一 Shot 结束到后一 Shot 开始是否时间前向、"
        "状态可衔接、没有重复已经完成的结果。"
        "必须按 temporal_mode 判断：static_outcome 的 narrative state 稳定不是倒退；"
        "但它仍不得重复前一 Shot 结果、预消费未来或改变 source_fact。"
        "不得根据题材关键词判断。"
        "必须显式返回 temporal_forward_ok、state_handoff_ok、"
        "no_result_duplication 三个 boolean 和 violations。"
        "只输出严格 JSON。"
    )

    prompt = (
        "=== PREVIOUS_SHOT_END ===\n"
        + json.dumps({
            "summary":
                previous_shot.get(
                    "summary"
                ),
            "temporal_mode":
                previous_shot.get("temporal_mode"),
            "source_fact":
                previous_shot.get("source_fact"),
            "narrative_end_state":
                previous_shot.get("narrative_end_state"),
            "covered_beat_orders":
                previous_shot.get(
                    "covered_beat_orders"
                ),
            "representative_state":
                previous_shot.get(
                    "representative_state"
                ),
            "video_end_state":
                previous_shot.get(
                    "video_end_state"
                ),
        },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== CURRENT_SHOT_START ===\n"
        + json.dumps({
            "summary":
                current_shot.get(
                    "summary"
                ),
            "temporal_mode":
                current_shot.get("temporal_mode"),
            "source_fact":
                current_shot.get("source_fact"),
            "narrative_start_state":
                current_shot.get("narrative_start_state"),
            "covered_beat_orders":
                current_shot.get(
                    "covered_beat_orders"
                ),
            "video_start_state":
                current_shot.get(
                    "video_start_state"
                ),
            "representative_state":
                current_shot.get(
                    "representative_state"
                ),
        },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    required = (
        "temporal_forward_ok",
        "state_handoff_ok",
        "no_result_duplication",
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "cross_batch_boundary_audit_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_SCHEMA_RETRY："
                        "必须完整返回三个 boolean + violations。"
                    )
                )
            ),
            contract=(
                '{"valid":true,'
                '"temporal_forward_ok":true,'
                '"state_handoff_ok":true,'
                '"no_result_duplication":true,'
                '"violations":[]}'
            ),
            max_tokens=700,
            temperature=0.0,
        )

        audit = _parse_object(
            env,
            raw,
            parsed,
        )

        missing = [
            key
            for key in required
            if not isinstance(
                audit.get(key),
                bool,
            )
        ]

        if missing:
            diagnostics.append(
                "attempt="
                + str(
                    attempt + 1
                )
                + " missing="
                + repr(
                    missing
                )
            )
            continue

        violations = (
            audit.get(
                "violations"
            )
            if isinstance(
                audit.get(
                    "violations"
                ),
                list,
            )
            else []
        )

        valid = (
            all(
                audit.get(key)
                is True
                for key in required
            )
            and not violations
        )

        return {
            **audit,
            "valid": valid,
            "violations":
                violations,
        }

    return {
        "valid": False,
        "violations": [
            "V2.39.2 boundary audit "
            "连续返回不完整 schema；"
            + " | ".join(
                diagnostics
            )
        ],
    }




async def _repair_first_for_boundary(
    env: dict[str, Any],
    *,
    previous_shot: dict[str, Any],
    current_rows: list[dict[str, Any]],
    boundary_audit: dict[str, Any],
    source_window: str,
    anchors: list[dict[str, Any]],
    compact_beats: list[dict[str, Any]],
    next_beat: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Evidence-locked cross-batch repair.

    V2.38.x re-fed the old current Shot narrative and wide source context here.
    That made unsupported abstractions able to re-enter during boundary repair.
    V2.39.2 only provides:
      - previous accepted end-state context,
      - current Shot's exact selected evidence,
      - locked covered Beat summaries/state changes,
      - next Beat preview as a DO-NOT-CONSUME guard.
    """
    if not current_rows:
        return []

    current = copy.deepcopy(
        current_rows[0]
    )

    locked_orders = list(
        _orders(
            current.get(
                "covered_beat_orders"
            )
        )
    )

    locked_evidence_ids = list(
        _id_list(
            current.get(
                "source_evidence_ids"
            )
        )
    )

    if (
        not locked_orders
        or not locked_evidence_ids
    ):
        raise RuntimeError(
            "V2.39.5: boundary repair 缺少可锁定 Beat/evidence"
        )

    exact_evidence = (
        _locked_evidence_rows(
            current,
            anchors,
        )
    )

    covered_beats = (
        _locked_covered_beats(
            current,
            compact_beats,
        )
    )

    if {
        str(row.get("id") or "")
        for row in exact_evidence
    } != set(
        locked_evidence_ids
    ):
        raise RuntimeError(
            "V2.39.5: boundary repair 无法完整解析锁定 evidence"
        )

    if {
        int(row.get("order") or 0)
        for row in covered_beats
    } != set(
        locked_orders
    ):
        raise RuntimeError(
            "V2.39.5: boundary repair 无法完整解析锁定 Beat"
        )

    visual_metadata = {
        "title":
            current.get("title"),
        "duration_seconds":
            current.get(
                "duration_seconds"
            ),
        "composition":
            current.get(
                "composition"
            ),
        "shot_size":
            current.get(
                "shot_size"
            ),
        "camera":
            current.get(
                "camera"
            ),
        "camera_move":
            current.get(
                "camera_move"
            ),
        "performance":
            current.get(
                "performance"
            ),
        "environment":
            current.get(
                "environment"
            ),
        "dialogue":
            current.get(
                "dialogue"
            ),
        "narration":
            current.get(
                "narration"
            ),
        "sound":
            current.get(
                "sound"
            ),
        "music":
            current.get(
                "music"
            ),
    }

    previous_end = {
        "summary":
            previous_shot.get(
                "summary"
            ),
        "covered_beat_orders":
            previous_shot.get(
                "covered_beat_orders"
            ),
        "representative_state":
            previous_shot.get(
                "representative_state"
            ),
        "video_end_state":
            previous_shot.get(
                "video_end_state"
            ),
    }

    system_prompt = (
        "你是 strict-shot-v2 evidence-locked 跨镜头边界修复器。"
        "CURRENT Shot 的 covered Beat 和 source evidence 已锁定。"
        "只根据 EXACT_SELECTED_EVIDENCE 重写当前 Shot 的 summary、action、"
        "三状态和当前可见实体；不得扩大事实边界。"
        "PREVIOUS_ACCEPTED_END 只用于保证时间衔接，不是当前 Shot 的事实来源。"
        "若边界错误表明 CURRENT 从 PREVIOUS 已完成之前的状态重新开始，"
        "必须从 EXACT_SELECTED_EVIDENCE 中选择 PREVIOUS 完成之后最早的新状态作为 "
        "video_start_state；不得重放 PREVIOUS 已完成的起点/过程/结果。"
        "NEXT_BEAT_PREVIEW_DO_NOT_CONSUME 只用于禁止提前消费。"
        "不得复用旧 CURRENT Shot 的 summary/action/三状态/Prompt。"
        "不要输出三个 Prompt；它们由程序从三状态确定性编译。"
        "只输出严格 JSON。"
    )

    prompt = (
        "=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(
            exact_evidence,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(
            covered_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PREVIOUS_ACCEPTED_END ===\n"
        + json.dumps(
            previous_end,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== BOUNDARY_VIOLATIONS ===\n"
        + _audit_issues(
            boundary_audit
        )
        + "\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n"
        + json.dumps(
            next_beat or {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== CURRENT_VISUAL_METADATA_ONLY ===\n"
        + json.dumps(
            visual_metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    diagnostics: list[str] = []

    for attempt in range(2):
        raw, parsed, _ = await _qwen(
            env,
            phase=(
                "studio_stage04_"
                "v2390_boundary_evidence_locked_repair_qwen32b"
            ),
            system_prompt=
                system_prompt,
            prompt=(
                prompt
                + (
                    ""
                    if attempt == 0
                    else (
                        "\n\nSTRICT_EVIDENCE_RETRY："
                        "从零重写当前 Shot 语义字段，"
                        "每个事实必须由 EXACT_SELECTED_EVIDENCE 直接支持。"
                    )
                )
            ),
            contract=(
                '{"shot":{'
                '"summary":"",'
                '"action":"",'
                '"representative_state":"",'
                '"video_start_state":"",'
                '"video_end_state":"",'
                '"character_entity_ids":[],'
                '"prop_entity_ids":[]'
                '}}'
            ),
            max_tokens=1200,
            temperature=0.0,
        )

        candidate = None

        if (
            isinstance(
                parsed,
                dict,
            )
            and isinstance(
                parsed.get("shot"),
                dict,
            )
        ):
            candidate = dict(
                parsed["shot"]
            )
        elif isinstance(
            parsed,
            dict,
        ):
            if any(
                key in parsed
                for key in (
                    "summary",
                    "representative_state",
                    "video_start_state",
                    "video_end_state",
                )
            ):
                candidate = dict(
                    parsed
                )

        if candidate is None:
            extracted = (
                _extract_shots(
                    env,
                    raw,
                    parsed,
                )
            )

            if len(extracted) == 1:
                candidate = dict(
                    extracted[0]
                )

        if not candidate:
            diagnostics.append(
                "attempt="
                + str(
                    attempt + 1
                )
                + " semantic_shot_not_found"
            )
            continue

        try:
            merged = _merge_shot_repair_patch(
                current,
                candidate,
                writable_fields=(
                    "summary",
                    "action",
                    "representative_state",
                    "video_start_state",
                    "video_end_state",
                    "character_entity_ids",
                    "prop_entity_ids",
                ),
            )
        except RuntimeError as exc:
            diagnostics.append(
                "attempt="
                + str(attempt + 1)
                + " state_closure="
                + str(exc)[:500]
            )
            continue

        merged[
            "covered_beat_orders"
        ] = locked_orders

        merged[
            "source_evidence_ids"
        ] = locked_evidence_ids

        return [
            merged,
            *copy.deepcopy(
                current_rows[1:]
            ),
        ]

    raise RuntimeError(
        "V2.39.5: boundary evidence-locked 修复无可用输出；"
        + " | ".join(
            diagnostics
        )
    )






def _shot_generation_contract(order: int) -> str:
    return json.dumps({
        "shots": [{
            "title": "",
            "duration_seconds": 3,
            "summary": "",
            "action": "",
            "temporal_mode": "observable_transition",
            "temporal_mode_reason": "",
            "temporal_mode_evidence_ids": ["E001"],
            "source_fact": "",
            "narrative_start_state": "",
            "narrative_state": "",
            "narrative_end_state": "",
            "visual_realization": "",
            "realization_scope": "presentation_only",
            "realization_assumptions": [],
            "visual_start_frame": "",
            "representative_frame": "",
            "visual_end_frame": "",
            "visual_motion": "",
            "representative_state": "",
            "video_start_state": "",
            "video_end_state": "",
            "image_prompt": "",
            "video_start_prompt": "",
            "video_prompt": "",
            "covered_beat_orders": [order],
            "source_evidence_ids": ["E001"],
            "character_entity_ids": [],
            "prop_entity_ids": [],
        }],
    }, ensure_ascii=False, separators=(",", ":"))


def _system_prompt() -> str:
    return (
        "你是正式短视频分镜导演，运行 Qwen3-32B，输出 strict-shot-v2。"
        "小说精确正文证据和当前 Beats 是事实最高权威。"
        "每个 Shot 必须显式 covered_beat_orders + source_evidence_ids；"
        "证据只能来自被覆盖 Beat。"
        "必须直接根据所选 evidence 分类 temporal_mode，不得用题材关键词："
        "observable_transition=证据明确支持动作前/中/后；"
        "static_outcome=证据只支持已成立的状态/结果/关系，或一个正在持续但没有证据支持内部前中后里程碑的稳定活动状态；"
        "insufficient_visual_evidence=不新增剧情事实就无法视觉化。"
        "分类必须给 temporal_mode_reason、temporal_mode_evidence_ids。"
        "observable_transition 的 video_start_state、representative_state、video_end_state "
        "必须同一因果链、互不相同且只向前。"
        "static_outcome 的 source_fact/summary/narrative_state 只写证据直接支持的稳定事实，"
        "narrative_start_state=narrative_state=narrative_end_state，不得虚构剧情动作；"
        "构图、机位、光影、环境运动、镜头运动和不改变剧情的微小表现只能写入 "
        "visual_realization/visual_*_frame/visual_motion，并标记 realization_scope="
        "presentation_only、逐项记录 realization_assumptions。"
        "presentation inference 禁止进入 source_fact、summary、action 或 source evidence。"
        "insufficient_visual_evidence 不得生成 Shot，交由程序进行 evidence regroup。"
        "不得把 video_end_state 写成下一 Beat 的结果，不得重复前一 Shot 已完成的结果。"
        "如果 Beat 带 adjacent_projection 且 relation=forward_with_replayed_prefix，"
        "说明其证据已被系统裁剪为重复前缀之后的新后续；Shot 必须只制作这个新后续，"
        "video_start_state 不得回到上一 Shot 已经完成之前的状态。"
        "三个 Prompt 是程序按 temporal_mode 派生字段；"
        "模型不应把 Prompt 当成独立语义事实。"
        "人物/道具只填画面真实可见的 ALLOWED entity id；不确定必须留空，"
        "禁止 Beat/Scene 兜底。"
        "不得使用固定业务关键词、题材类别或预设剧情规则。只输出严格 JSON。"
    )




async def _generate_rows(
    env: dict[str, Any],
    *,
    prompt: str,
    scene_index: int,
    scene_total: int,
    batch_index: int,
    batch_total: int,
) -> list[dict[str, Any]]:
    """Direct Qwen generation; no historical Shot generator."""
    diagnostics: list[str] = []

    attempts = (
        (
            "json-primary",
            "",
            2600,
            0.04,
        ),
        (
            "json-strict",
            (
                "\n\nSTRICT_SCHEMA_RETRY："
                "只返回 {\"shots\":[...]}。"
                "每个 Shot 必须包含 temporal_mode 及其 evidence/reason、非空 summary、"
                "covered_beat_orders、source_evidence_ids、"
                "observable_transition 必须包含三个 distinct narrative state；"
                "static_outcome 必须包含 stable narrative_state、source_fact 和完整"
                " presentation-only visual realization/frames/motion。"
                "三个 Prompt 由程序按 mode 生成。"
            ),
            2600,
            0.0,
        ),
    )

    for (
        attempt_name,
        suffix,
        max_tokens,
        temperature,
    ) in attempts:
        try:
            raw, parsed, _ = await _qwen(
                env,
                phase=(
                    "studio_stage04_"
                    "v2392_direct_shot_generation_qwen32b"
                ),
                system_prompt=_system_prompt(),
                prompt=prompt + suffix,
                contract=_shot_generation_contract(1),
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            diagnostics.append(
                attempt_name
                + ": "
                + type(exc).__name__
                + ": "
                + str(exc)[:500]
            )
            continue

        rows = _extract_shots(
            env,
            raw,
            parsed,
        )
        if rows:
            return rows

        diagnostics.append(
            attempt_name
            + ": shots_not_found "
            + _structured_response_diagnostic(
                raw,
                parsed,
            )
        )

    raise RuntimeError(
        "V2.39.5: Qwen Shot 生成两轮均无可用输出；"
        + " | ".join(diagnostics)
    )




def _evidence_owner_orders(
    compact_beats: list[dict[str, Any]],
) -> dict[str, set[int]]:
    owners: dict[str, set[int]] = {}

    for beat in compact_beats or []:
        if not isinstance(beat, dict):
            continue

        try:
            order = int(
                beat.get("order")
                or 0
            )
        except Exception:
            order = 0

        if order <= 0:
            continue

        for evidence_id in _id_list(
            beat.get(
                "allowed_source_evidence_ids"
            )
            or beat.get(
                "source_evidence_ids"
            )
            or []
        ):
            owners.setdefault(
                evidence_id,
                set(),
            ).add(order)

    return owners


def _normalize_raw_shot_binding(
    row: dict[str, Any],
    *,
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """
    Recover only structural binding.

    Fact authority is source_evidence_ids:
      - if covered_beat_orders is missing/wrong but every selected evidence ID
        belongs to known current Beat(s), derive Beat orders from that evidence;
      - never invent source evidence;
      - if evidence is absent/unknown, do not guess a Beat.
    """
    if not isinstance(row, dict):
        return (
            None,
            "row_not_object",
        )

    item = copy.deepcopy(row)

    # Structural aliases only; no semantic guessing.
    if not _id_list(
        item.get("source_evidence_ids")
    ):
        for alias in (
            "evidence_ids",
            "source_ids",
            "evidence_anchor_ids",
        ):
            values = _id_list(
                item.get(alias)
            )
            if values:
                item[
                    "source_evidence_ids"
                ] = values
                break

    if not _orders(
        item.get("covered_beat_orders")
    ):
        for alias in (
            "beat_orders",
            "covered_beats",
            "beat_ids",
        ):
            values = _orders(
                item.get(alias)
            )
            if values:
                item[
                    "covered_beat_orders"
                ] = values
                break

    evidence_ids = _id_list(
        item.get("source_evidence_ids")
    )

    anchor_ids = {
        str(anchor.get("id") or "")
        for anchor in anchors or []
        if isinstance(anchor, dict)
        and str(anchor.get("id") or "")
    }

    if not evidence_ids:
        return (
            None,
            "source_evidence_ids_missing",
        )

    unknown = [
        key
        for key in evidence_ids
        if key not in anchor_ids
    ]

    if unknown:
        return (
            None,
            "source_evidence_ids_outside_batch="
            + repr(unknown),
        )

    owner_map = _evidence_owner_orders(
        compact_beats
    )

    derived_orders: set[int] = set()

    for evidence_id in evidence_ids:
        owners = owner_map.get(
            evidence_id
        ) or set()

        if not owners:
            return (
                None,
                "evidence_has_no_current_beat_owner="
                + evidence_id,
            )

        derived_orders.update(
            owners
        )

    if not derived_orders:
        return (
            None,
            "cannot_derive_beat_from_evidence",
        )

    # Evidence is the highest fact authority. If model omitted or mislabeled
    # the Beat binding, replace only the structural binding with evidence-owned
    # current Beat orders.
    item[
        "covered_beat_orders"
    ] = sorted(
        derived_orders
    )

    item[
        "source_evidence_ids"
    ] = evidence_ids

    return (
        item,
        "binding_from_exact_evidence",
    )


async def _validate_initial_rows_with_recovery(
    env: dict[str, Any],
    *,
    raw_rows: list[dict[str, Any]],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    previous_shot: dict[str, Any] | None,
    next_beat: dict[str, Any] | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    scene_id: str,
    episode_id: str,
) -> list[dict[str, Any]]:
    """
    Initial multi-Beat generation must not abort the whole batch because one
    model row omitted a structural field.

    Invalid rows are recorded, not silently accepted. Uncovered Beat(s) are
    regenerated through the existing scoped one-Beat generator.
    """
    accepted: list[dict[str, Any]] = []
    diagnostics: list[str] = []

    for index, raw_row in enumerate(
        raw_rows or [],
        1,
    ):
        normalized, origin = (
            _normalize_raw_shot_binding(
                raw_row,
                compact_beats=
                    compact_beats,
                anchors=anchors,
            )
        )

        if normalized is None:
            diagnostics.append(
                f"Shot#{index}: "
                + origin
            )
            continue

        try:
            normalized = await _repair_invalid_temporal_mode_classification(
                env,
                row=normalized,
                compact_beats=compact_beats,
                anchors=anchors,
                context=f"initial Shot#{index}",
            )
            normalized = await _repair_static_outcome_payload_consistency(
                env,
                row=normalized,
                compact_beats=compact_beats,
                anchors=anchors,
                context=f"initial Shot#{index}",
            )
            normalized = await _repair_observable_transition_state_consistency(
                env,
                row=normalized,
                compact_beats=compact_beats,
                anchors=anchors,
                context=f"initial Shot#{index}",
            )
            rows = validate_rows(
                env,
                raw_rows=[normalized],
                compact_beats=
                    compact_beats,
                allowed_chars=
                    allowed_chars,
                allowed_props=
                    allowed_props,
                anchors=anchors,
                scene_id=scene_id,
                episode_id=episode_id,
            )
        except Exception as exc:
            diagnostics.append(
                f"Shot#{index}: "
                + origin
                + " validate="
                + str(exc)[:700]
            )
            continue

        accepted.extend(
            rows
        )

    expected = {
        int(beat.get("order") or 0)
        for beat in compact_beats or []
        if isinstance(beat, dict)
        and int(beat.get("order") or 0)
        > 0
    }

    covered = _covered_orders(
        accepted
    )

    missing = sorted(
        expected - covered
    )

    if missing:
        additions = (
            await _generate_missing_beat_shots(
                env,
                missing_orders=missing,
                compact_beats=
                    compact_beats,
                anchors=anchors,
                previous_shot=
                    previous_shot,
                next_beat=next_beat,
                allowed_chars=
                    allowed_chars,
                allowed_props=
                    allowed_props,
                scene_id=scene_id,
                episode_id=episode_id,
            )
        )

        accepted.extend(
            additions
        )

    remaining = sorted(
        expected
        - _covered_orders(
            accepted
        )
    )

    if remaining:
        raise RuntimeError(
            "V2.39.5: 初次 Shot binding 恢复后仍有未覆盖 Beat；"
            "missing="
            + repr(remaining)
            + " diagnostics="
            + repr(diagnostics)
        )

    if not accepted:
        raise RuntimeError(
            "V2.39.5: 初次 Shot 全部无合法 evidence binding，"
            "且定向 Beat 重生未产生结果；diagnostics="
            + repr(diagnostics)
        )

    return sorted(
        accepted,
        key=lambda row: min(
            _orders(
                row.get(
                    "covered_beat_orders"
                )
            )
            or [10**9]
        ),
    )


async def _produce_batch(
    env: dict[str, Any],
    *,
    batch: list[dict[str, Any]],
    all_beats: list[dict[str, Any]],
    batch_index: int,
    batch_total: int,
    source: str,
    scene: dict[str, Any],
    scene_index: int,
    scene_total: int,
    previous_shot: dict[str, Any] | None,
    allowed_chars: set[str],
    allowed_props: set[str],
    entity_rows: list[dict[str, str]],
    resolved_text: str,
    character_anchor: str,
    visual_anchor: str,
    user_input: str,
) -> list[dict[str, Any]]:
    evidence_builder = env.get("_studio_v2371e_batch_evidence")
    if not callable(evidence_builder):
        raise RuntimeError("V2.39.5: Beat→Shot evidence builder 不可用")
    source_window, anchors, beat_to_anchor_ids = evidence_builder(source=source, batch=batch, max_context_chars=1900)
    compact_beats = _compact_beats(batch, beat_to_anchor_ids)
    next_order = int(batch[-1].get("order") or 0) + 1
    next_beat = next((row for row in all_beats if int(row.get("order") or 0) == next_order), None)

    # V2.39.10.7_SHOT_CONTEXT_COMPACTION
    #
    # Original generation prompt duplicated the same evidence several times:
    # source_window + anchors + Beat source_evidence/spans + full previous Shot.
    # With ctx=8192 this could leave only ~195-250 output tokens and truncate
    # {"shots":[...]} before the JSON closed.
    #
    # Keep all exact evidence and validators in Python, but feed Qwen only one
    # textual copy of each fact. Audit paths below still use source_window,
    # anchors and full compact_beats unchanged.
    previous_prompt = {}
    if isinstance(previous_shot, dict):
        previous_prompt = {
            "summary": str(previous_shot.get("summary") or "")[:260],
            "covered_beat_orders": list(previous_shot.get("covered_beat_orders") or []),
            "representative_state": str(previous_shot.get("representative_state") or "")[:320],
            "video_end_state": str(previous_shot.get("video_end_state") or "")[:360],
            "character_entity_ids": list(previous_shot.get("character_entity_ids") or []),
            "prop_entity_ids": list(previous_shot.get("prop_entity_ids") or []),
        }

    next_prompt = {}
    if isinstance(next_beat, dict):
        next_prompt = {
            "order": int(next_beat.get("order") or 0),
            "summary": str(next_beat.get("summary") or "")[:220],
            "state_change": str(next_beat.get("state_change") or "")[:200],
        }

    anchor_prompt = []
    for anchor in anchors or []:
        if not isinstance(anchor, dict):
            continue
        anchor_id = str(anchor.get("id") or "")
        if not anchor_id:
            continue
        anchor_prompt.append({
            "id": anchor_id,
            "text": str(
                anchor.get("text")
                or anchor.get("quote")
                or anchor.get("source_text")
                or ""
            ),
        })

    beat_prompt = []
    for beat in compact_beats or []:
        if not isinstance(beat, dict):
            continue
        beat_prompt.append({
            "order": int(beat.get("order") or 0),
            "summary": str(beat.get("summary") or "")[:280],
            "state_change": str(beat.get("state_change") or "")[:220],
            "allowed_source_evidence_ids": list(
                beat.get("allowed_source_evidence_ids")
                or beat.get("source_evidence_ids")
                or []
            ),
            "character_entity_ids": list(beat.get("character_entity_ids") or []),
            "prop_entity_ids": list(beat.get("prop_entity_ids") or []),
        })

    entity_prompt = []
    for row in entity_rows or []:
        if not isinstance(row, dict):
            continue
        entity_prompt.append({
            "entity_id": str(row.get("entity_id") or ""),
            "entity_type": str(row.get("entity_type") or ""),
            "name": str(row.get("name") or "")[:80],
        })

    continuity_prompt = _cut(resolved_text, 420)
    character_prompt = _cut(character_anchor, 360)
    visual_prompt = _cut(visual_anchor, 320)
    requirement_prompt = _cut(user_input, 180)

    base_prompt = (
        f"SCENE_PROGRESS={scene_index}/{scene_total}\n"
        f"BATCH_PROGRESS={batch_index + 1}/{batch_total}\n"
        + "TARGET_SHOTS≈"
        + str(max(1, len(batch)))
        + "；按真实状态变化决定，不得吞掉独立 Beat。\n"
        + "FACT_POLICY=SOURCE_EVIDENCE_ANCHORS_EXACT 是当前 Shot 唯一可引用正文证据；"
        + "BEATS_THIS_BATCH_COMPACT 提供语义边界；不得消费 NEXT_BEAT_PREVIEW。\n\n"
        + "=== PREVIOUS_ACCEPTED_SHOT_COMPACT ===\n"
        + json.dumps(previous_prompt, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== NEXT_BEAT_PREVIEW_DO_NOT_CONSUME ===\n"
        + json.dumps(next_prompt, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== SOURCE_EVIDENCE_ANCHORS_EXACT ===\n"
        + json.dumps(anchor_prompt, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== BEATS_THIS_BATCH_COMPACT ===\n"
        + json.dumps(beat_prompt, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CONTINUITY_COMPACT ===\n"
        + continuity_prompt
        + "\n\n=== ALLOWED_ENTITIES ===\n"
        + json.dumps(entity_prompt, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CHARACTER_ANCHOR_COMPACT ===\n"
        + (character_prompt or "<none>")
        + "\n\n=== VISUAL_ANCHOR_COMPACT ===\n"
        + (visual_prompt or "<none>")
        + "\n\n=== USER_REQUIREMENT ===\n"
        + requirement_prompt
    )

    print(
        "[V2.39.10.7][Stage04][ShotPrompt] "
        f"scene={scene_index}/{scene_total} "
        f"batch={batch_index + 1}/{batch_total} "
        f"beats={len(batch)} anchors={len(anchor_prompt)} "
        f"prompt_chars={len(base_prompt)} "
        f"previous_full_chars={len(json.dumps(previous_shot or {}, ensure_ascii=False))} "
        f"next_full_chars={len(json.dumps(next_beat or {}, ensure_ascii=False))}",
        flush=True,
    )

    # Fail closed before calling Qwen if compaction somehow regresses.
    # Do not silently drop exact evidence just to fit context.
    if len(base_prompt) > 6400:
        raise RuntimeError(
            "V2.39.10.7: Shot compact prompt 仍过大；"
            f"chars={len(base_prompt)} anchors={len(anchor_prompt)} "
            "拒绝让 Qwen 在不足输出空间下生成截断 JSON"
        )

    audit_fn = env.get("_studio_v2371_audit_batch")
    if not callable(audit_fn):
        raise RuntimeError("V2.39.5: V2.37.7 complete Shot audit 不可用")

    if len(batch) == 1:
        # V2.39.5: a single-Beat fallback is a structurally scoped task.
        # Do not let the generic batch generator copy NEXT_BEAT_PREVIEW into
        # covered_beat_orders. The target Beat order is locked by task scope,
        # while source_evidence_ids still must come from this Beat's legal
        # evidence and are validated by _generate_missing_beat_shots.
        target_order = int(
            batch[0].get("order")
            or 0
        )
        if target_order <= 0:
            raise RuntimeError(
                "V2.39.5: single-Beat scope 缺少合法 Beat order"
            )

        try:
            rows = await _generate_missing_beat_shots(
                env,
                missing_orders=[target_order],
                compact_beats=compact_beats,
                anchors=anchors,
                previous_shot=previous_shot,
                next_beat=next_beat,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=str(scene.get("scene_id") or ""),
                episode_id=str(scene.get("episode_id") or ""),
            )
        except Stage04ShotRepairError as exc:
            evidence_sufficiency = str(
                exc.metadata.get("evidence_sufficiency") or ""
            )
            if evidence_sufficiency not in {
                "insufficient_visual_evidence",
                "insufficient_for_observable_transition",
            }:
                raise
            # The model has produced a valid semantic routing decision. Reuse
            # the existing one-shot regroup/regenerate/final-audit recovery;
            # never retry the same insufficient evidence payload.
            rows, audit = await _recover_single_beat_after_scoped_repair(
                env,
                source=source,
                target_beat=batch[0],
                all_beats=all_beats,
                current_compact_beats=compact_beats,
                current_anchors=anchors,
                previous_shot=previous_shot,
                next_beat=next_beat,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=str(scene.get("scene_id") or ""),
                episode_id=str(scene.get("episode_id") or ""),
                audit_fn=audit_fn,
                prior_metadata=copy.deepcopy(exc.metadata),
                current_rows=[],
            )
            if previous_shot and rows:
                boundary = await _boundary_audit(
                    env,
                    previous_shot=previous_shot,
                    current_shot=rows[0],
                )
                if not boundary.get("valid"):
                    raise Stage04ShotRepairError(
                        "insufficient evidence regroup 后跨 Shot 边界审计失败："
                        + _audit_issues(boundary),
                        metadata={
                            **copy.deepcopy(exc.metadata),
                            "repair_progress": "regroup_boundary_audit_failed",
                            "boundary_audit": copy.deepcopy(boundary),
                        },
                    )
                rows[0]["forward_overlap_audit"] = copy.deepcopy(boundary)
            for row in rows:
                row["source_audit"] = copy.deepcopy(audit)
            return rows
    else:
        raw_rows = await _generate_rows(
            env,
            prompt=base_prompt,
            scene_index=scene_index,
            scene_total=scene_total,
            batch_index=batch_index,
            batch_total=batch_total,
        )
        try:
            rows = await _validate_initial_rows_with_recovery(
                env,
                raw_rows=raw_rows,
                compact_beats=compact_beats,
                anchors=anchors,
                previous_shot=previous_shot,
                next_beat=next_beat,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=str(scene.get("scene_id") or ""),
                episode_id=str(scene.get("episode_id") or ""),
            )
        except Stage04ShotRepairError as exc:
            evidence_sufficiency = str(
                exc.metadata.get("evidence_sufficiency") or ""
            )
            if evidence_sufficiency not in {
                "insufficient_visual_evidence",
                "insufficient_for_observable_transition",
            }:
                raise
            # Recover at one-Beat scope so each insufficient classification
            # gets exactly one adjacent-evidence regroup budget.
            sequential: list[dict[str, Any]] = []
            local_previous = previous_shot
            for single in batch:
                single_rows = await _produce_batch(
                    env,
                    batch=[single],
                    all_beats=all_beats,
                    batch_index=batch_index,
                    batch_total=batch_total,
                    source=source,
                    scene=scene,
                    scene_index=scene_index,
                    scene_total=scene_total,
                    previous_shot=local_previous,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                    resolved_text=resolved_text,
                    character_anchor=character_anchor,
                    visual_anchor=visual_anchor,
                    user_input=user_input,
                )
                sequential.extend(single_rows)
                local_previous = sequential[-1]
            return sequential
    rows = await _ensure_batch_coverage(
        env,
        rows=rows,
        compact_beats=compact_beats,
        anchors=anchors,
        previous_shot=previous_shot,
        next_beat=next_beat,
        allowed_chars=allowed_chars,
        allowed_props=allowed_props,
        scene_id=str(scene.get("scene_id") or ""),
        episode_id=str(scene.get("episode_id") or ""),
    )
    _stage04_progress(
        env, 4, "Strict audit / repair", "正在执行严格分镜审计与定向修复"
    )
    audit = await audit_fn(source_window=source_window, compact_beats=compact_beats, shots=rows)

    repair_failure: Stage04ShotRepairError | None = None
    last_repair_metadata: dict[str, Any] = {}
    attempted_repair_signatures: set[
        tuple[tuple[int, tuple[str, ...]], ...]
    ] = set()
    if not _audit_ok(env, audit):
        for _ in range(2):
            repair_signature = _audit_repair_signature(
                audit,
                row_count=len(rows),
            )
            if repair_signature in attempted_repair_signatures:
                last_repair_metadata.update({
                    "repair_progress":
                        "needs_regrouping_or_evidence_selection",
                    "evidence_sufficiency":
                        "undetermined_after_scoped_repair",
                    "regroup_reason":
                        "same audit violation remained after one scoped repair",
                })
                repair_failure = Stage04ShotRepairError(
                    "同一 audit violation 已执行一次 scoped repair；"
                    "拒绝重复相同路径并回到 grouping/evidence selection",
                    metadata=last_repair_metadata,
                )
                break
            attempted_repair_signatures.add(repair_signature)
            before_fingerprint = tuple(
                _shot_semantic_fingerprint(row)
                for row in rows
            )
            try:
                repaired_raw = await _repair_batch(
                    env,
                    current_rows=rows,
                    audit=audit,
                    source_window=source_window,
                    anchors=anchors,
                    compact_beats=compact_beats,
                    previous_shot=previous_shot,
                    next_beat=next_beat,
                )
            except Stage04ShotRepairError as exc:
                repair_failure = exc
                last_repair_metadata = copy.deepcopy(exc.metadata)
                break
            if not repaired_raw:
                break
            for repaired_item in repaired_raw:
                if isinstance(repaired_item, dict) and isinstance(
                    repaired_item.get("_directional_repair_diagnostics"),
                    dict,
                ):
                    last_repair_metadata = copy.deepcopy(
                        repaired_item["_directional_repair_diagnostics"]
                    )
            rows = validate_rows(
                env,
                raw_rows=repaired_raw,
                compact_beats=compact_beats,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                anchors=anchors,
                scene_id=str(scene.get("scene_id") or ""),
                episode_id=str(scene.get("episode_id") or ""),
            )
            after_fingerprint = tuple(
                _shot_semantic_fingerprint(row)
                for row in rows
            )
            if after_fingerprint == before_fingerprint:
                last_repair_metadata["repair_progress"] = "no_semantic_progress"
                repair_failure = Stage04ShotRepairError(
                    "Directional repair 无语义进展；拒绝重复发送相同上下文",
                    metadata=last_repair_metadata,
                )
                break
            rows = await _ensure_batch_coverage(
                env,
                rows=rows,
                compact_beats=compact_beats,
                anchors=anchors,
                previous_shot=previous_shot,
                next_beat=next_beat,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=str(scene.get("scene_id") or ""),
                episode_id=str(scene.get("episode_id") or ""),
            )
            audit = await audit_fn(source_window=source_window, compact_beats=compact_beats, shots=rows)
            if _audit_ok(env, audit):
                break

    if not _audit_ok(env, audit):
        if len(batch) > 1:
            # Fail closed but recover locally: regenerate each Beat sequentially, not the whole Scene.
            sequential: list[dict[str, Any]] = []
            local_previous = previous_shot
            for offset, single in enumerate(batch):
                single_rows = await _produce_batch(
                    env,
                    batch=[single],
                    all_beats=all_beats,
                    batch_index=batch_index,
                    batch_total=batch_total,
                    source=source,
                    scene=scene,
                    scene_index=scene_index,
                    scene_total=scene_total,
                    previous_shot=local_previous,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    entity_rows=entity_rows,
                    resolved_text=resolved_text,
                    character_anchor=character_anchor,
                    visual_anchor=visual_anchor,
                    user_input=user_input,
                )
                sequential.extend(single_rows)
                local_previous = sequential[-1]
            return sequential
        recovery_progress = str(
            last_repair_metadata.get("repair_progress") or ""
        )
        if repair_failure is not None and recovery_progress in {
            "needs_regrouping_or_evidence_selection",
            "no_semantic_progress",
        }:
            rows, audit = await _recover_single_beat_after_scoped_repair(
                env,
                source=source,
                target_beat=batch[0],
                all_beats=all_beats,
                current_compact_beats=compact_beats,
                current_anchors=anchors,
                previous_shot=previous_shot,
                next_beat=next_beat,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=str(scene.get("scene_id") or ""),
                episode_id=str(scene.get("episode_id") or ""),
                audit_fn=audit_fn,
                prior_metadata=last_repair_metadata,
                current_rows=rows,
            )
        if _audit_ok(env, audit):
            repair_failure = None
        else:
            issues = _issues_for_shot(audit, 1)
            metadata = copy.deepcopy(last_repair_metadata)
            if not metadata:
                metadata = _repair_failure_metadata(
                    shot_index=1,
                    current=rows[0],
                    issues=issues,
                    candidate=None,
                    post=None,
                    progress="audit_failed_without_valid_patch",
                    exact_evidence=_locked_evidence_rows(rows[0], anchors),
                    covered_beats=_locked_covered_beats(rows[0], compact_beats),
                    regroup_reason="strict audit failed without a valid scoped patch",
                )
            metadata["failed_rules"] = list(dict.fromkeys([
                *metadata.get("failed_rules", []),
                *[
                    _canonical_audit_code(issue)
                    for issue in issues
                    if isinstance(issue, dict)
                ],
            ]))
            metadata["audit"] = copy.deepcopy(audit)
            detail = (
                str(repair_failure)
                if repair_failure is not None
                else _audit_issues(audit)
            )
            raise Stage04ShotRepairError(
                f"场景 {scene_index}/{scene_total} Beat {int(batch[0].get('order') or 0)} "
                "定向修复及受控 evidence recovery 后仍未通过 strict-shot-v2 审计："
                + detail,
                metadata=metadata,
            )

    # Cross-batch boundary is mandatory and independently audited.
    if previous_shot and rows:
        boundary = await _boundary_audit(env, previous_shot=previous_shot, current_shot=rows[0])
        if not boundary.get("valid"):
            for _ in range(2):
                repaired_raw = await _repair_first_for_boundary(
                    env,
                    previous_shot=previous_shot,
                    current_rows=rows,
                    boundary_audit=boundary,
                    source_window=source_window,
                    anchors=anchors,
                    compact_beats=compact_beats,
                    next_beat=next_beat,
                )
                if not repaired_raw:
                    break
                rows = validate_rows(
                    env,
                    raw_rows=repaired_raw,
                    compact_beats=compact_beats,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    anchors=anchors,
                    scene_id=str(scene.get("scene_id") or ""),
                    episode_id=str(scene.get("episode_id") or ""),
                )
                rows = await _ensure_batch_coverage(
                    env,
                    rows=rows,
                    compact_beats=compact_beats,
                    anchors=anchors,
                    previous_shot=previous_shot,
                    next_beat=next_beat,
                    allowed_chars=allowed_chars,
                    allowed_props=allowed_props,
                    scene_id=str(scene.get("scene_id") or ""),
                    episode_id=str(scene.get("episode_id") or ""),
                )
                batch_audit = await audit_fn(source_window=source_window, compact_beats=compact_beats, shots=rows)
                if not _audit_ok(env, batch_audit):
                    boundary = {"valid": False, "violations": ["boundary repair broke batch audit", _audit_issues(batch_audit)]}
                    continue
                # The repaired rows are the persistence candidate.  Preserve
                # the audit that actually validated those rows, not the audit
                # from the pre-boundary object.
                audit = batch_audit
                boundary = await _boundary_audit(env, previous_shot=previous_shot, current_shot=rows[0])
                if boundary.get("valid"):
                    break
        if not boundary.get("valid"):
            if len(batch) > 1:
                sequential: list[dict[str, Any]] = []
                local_previous = previous_shot
                for single in batch:
                    single_rows = await _produce_batch(
                        env,
                        batch=[single], all_beats=all_beats, batch_index=batch_index, batch_total=batch_total,
                        source=source, scene=scene, scene_index=scene_index, scene_total=scene_total,
                        previous_shot=local_previous, allowed_chars=allowed_chars, allowed_props=allowed_props,
                        entity_rows=entity_rows, resolved_text=resolved_text, character_anchor=character_anchor,
                        visual_anchor=visual_anchor, user_input=user_input,
                    )
                    sequential.extend(single_rows)
                    local_previous = sequential[-1]
                return sequential
            raise RuntimeError(
                f"场景 {scene_index}/{scene_total} Beat {int(batch[0].get('order') or 0)} 跨批次状态边界修复失败：{_audit_issues(boundary)}"
            )

    for row in rows:
        row["source_audit"] = audit
    return rows

def _deterministic_scene_checks(all_rows: list[dict[str, Any]], beats: list[dict[str, Any]]) -> None:
    expected = {int(row.get("order") or 0) for row in beats if int(row.get("order") or 0) > 0}
    covered = {order for row in all_rows for order in _orders(row.get("covered_beat_orders"))}
    if covered != expected:
        raise RuntimeError(f"V2.39.3 Scene Beat 覆盖不完整：missing={sorted(expected-covered)} unexpected={sorted(covered-expected)}")

    last_max_order = 0
    last_span_start = -1
    seen_exact: set[str] = set()
    for index, row in enumerate(all_rows, 1):
        orders = _orders(row.get("covered_beat_orders"))
        if not orders:
            raise RuntimeError(f"V2.39.3 Scene Shot#{index} 无 Beat 绑定")
        if min(orders) < last_max_order:
            raise RuntimeError(f"V2.39.3 Scene Shot Beat 时间倒退：shot={index} orders={orders} previous_max={last_max_order}")
        last_max_order = max(last_max_order, max(orders))
        spans = row.get("source_evidence_spans") or []
        starts = [int(x.get("start") or 0) for x in spans if isinstance(x, dict)]
        if starts:
            current_start = min(starts)
            if current_start < last_span_start:
                raise RuntimeError(f"V2.39.3 Scene Shot 原文证据时间倒退：shot={index}")
            last_span_start = current_start
        fingerprint = re.sub(r"\s+", "", "|".join([
            str(row.get("representative_state") or ""),
            str(row.get("video_start_state") or ""),
            str(row.get("video_end_state") or ""),
        ]).casefold())
        if fingerprint and fingerprint in seen_exact:
            raise RuntimeError(f"V2.39.3 Scene 检测到完全重复三状态 Shot：shot={index}")
        if fingerprint:
            seen_exact.add(fingerprint)


def _scene_audit_compact_shot(
    row: dict[str, Any],
    index: int,
    *,
    include_evidence: bool,
) -> dict[str, Any]:
    result = {
        "shot_index":
            index,
        "covered_beat_orders":
            list(
                row.get(
                    "covered_beat_orders"
                )
                or []
            ),
        "summary":
            str(
                row.get("summary")
                or ""
            )[:420],
        "temporal_mode":
            str(row.get("temporal_mode") or ""),
        "source_fact":
            str(row.get("source_fact") or "")[:420],
        "narrative_start_state":
            str(row.get("narrative_start_state") or "")[:500],
        "narrative_state":
            str(row.get("narrative_state") or "")[:500],
        "narrative_end_state":
            str(row.get("narrative_end_state") or "")[:500],
        "visual_realization":
            str(row.get("visual_realization") or "")[:500],
        "realization_scope":
            str(row.get("realization_scope") or ""),
        "realization_assumptions":
            list(row.get("realization_assumptions") or []),
        "visual_start_frame":
            str(row.get("visual_start_frame") or "")[:500],
        "representative_frame":
            str(row.get("representative_frame") or "")[:500],
        "visual_end_frame":
            str(row.get("visual_end_frame") or "")[:500],
        "visual_motion":
            str(row.get("visual_motion") or "")[:500],
        "representative_state":
            str(
                row.get(
                    "representative_state"
                )
                or ""
            )[:500],
        "video_start_state":
            str(
                row.get(
                    "video_start_state"
                )
                or ""
            )[:500],
        "video_end_state":
            str(
                row.get(
                    "video_end_state"
                )
                or ""
            )[:500],
        "character_entity_ids":
            list(
                row.get(
                    "character_entity_ids"
                )
                or []
            ),
        "prop_entity_ids":
            list(
                row.get(
                    "prop_entity_ids"
                )
                or []
            ),
    }

    if include_evidence:
        result[
            "source_evidence_ids"
        ] = list(
            row.get(
                "source_evidence_ids"
            )
            or []
        )

        result[
            "source_evidence"
        ] = list(
            row.get(
                "source_evidence"
            )
            or []
        )

    return result


def _scene_audit_target_indices(
    audit: dict[str, Any],
    *,
    row_count: int,
) -> list[int]:
    violations = (
        audit.get("violations")
        if isinstance(
            audit,
            dict,
        )
        else []
    )

    if not isinstance(
        violations,
        list,
    ):
        return []

    result: list[int] = []

    for violation in violations:
        if not isinstance(
            violation,
            dict,
        ):
            continue

        try:
            index = int(
                violation.get(
                    "shot_index"
                )
                or 0
            )
        except Exception:
            index = 0

        if (
            1 <= index <= row_count
            and index not in result
        ):
            result.append(
                index
            )

    return result


async def _scene_global_audit(
    env: dict[str, Any],
    *,
    all_rows: list[dict[str, Any]],
    beats: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Bounded Scene-wide audit.

    Per-batch audit + adjacent boundary audit cannot detect a semantic result
    repeated many shots later. This pass uses windows of 6 current shots and a
    compact history of all previously accepted result states.
    """
    required = (
        "evidence_entailment_ok",
        "beat_coverage_ok",
        "temporal_monotonic",
        "no_future_event_preconsumption",
        "no_result_duplication",
        "state_order_valid",
        "entity_visibility_valid",
        "visual_realization_valid",
    )

    beat_map = {
        int(row.get("order") or 0):
            row
        for row in beats or []
        if isinstance(row, dict)
        and int(
            row.get("order")
            or 0
        ) > 0
    }

    violations: list[Any] = []

    window_size = 6

    for start in range(
        0,
        len(all_rows),
        window_size,
    ):
        window = all_rows[
            start:
            start + window_size
        ]

        current = [
            _scene_audit_compact_shot(
                row,
                start + offset + 1,
                include_evidence=True,
            )
            for offset, row in enumerate(
                window
            )
        ]

        history = [
            _scene_audit_compact_shot(
                row,
                index,
                include_evidence=False,
            )
            for index, row in enumerate(
                all_rows[:start],
                1,
            )
        ]

        current_orders = sorted({
            order
            for row in window
            for order in _orders(
                row.get(
                    "covered_beat_orders"
                )
            )
        })

        relevant_beats = [{
            "order":
                order,
            "summary":
                str(
                    beat_map.get(
                        order,
                        {},
                    ).get("summary")
                    or ""
                )[:420],
            "state_change":
                str(
                    beat_map.get(
                        order,
                        {},
                    ).get(
                        "state_change"
                    )
                    or ""
                )[:420],
        } for order in current_orders]

        system_prompt = (
            "你是 strict-shot-v2 Scene 最终一致性审计器，只审计不改写。"
            "CURRENT_WINDOW 中每个 Shot 的 source_evidence 是该 Shot 的事实最高权威。"
            "PRIOR_ACCEPTED_HISTORY 用于检查跨窗口时间连续和非相邻结果重复，"
            "不能作为 CURRENT_WINDOW 新事实来源。"
            "必须检查八个 boolean：evidence_entailment_ok、beat_coverage_ok、"
            "temporal_monotonic、no_future_event_preconsumption、"
            "no_result_duplication、state_order_valid、entity_visibility_valid、"
            "visual_realization_valid。observable_transition 保持动态三状态严格规则；"
            "static_outcome 必须保持 narrative state 稳定，并严格验证 presentation-only "
            "visual realization 不增加角色、道具、事件、结果、未来事件或关系变化。"
            "若失败，violations 每项必须带全 Scene 的 shot_index。"
            "不得依据题材关键词或固定业务类别。只输出严格 JSON。"
        )

        prompt = (
            "=== PRIOR_ACCEPTED_HISTORY ===\n"
            + json.dumps(
                history,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== RELEVANT_BEATS ===\n"
            + json.dumps(
                relevant_beats,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n=== CURRENT_WINDOW ===\n"
            + json.dumps(
                current,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        complete = False
        audit: dict[str, Any] = {}

        for attempt in range(2):
            raw, parsed, _ = await _qwen(
                env,
                phase=(
                    "studio_stage04_"
                    "v2390_scene_global_audit_qwen32b"
                ),
                system_prompt=
                    system_prompt,
                prompt=(
                    prompt
                    + (
                        ""
                        if attempt == 0
                        else (
                            "\n\nSTRICT_SCHEMA_RETRY："
                            "必须完整返回 valid + 八个 boolean + violations。"
                        )
                    )
                ),
                contract=(
                    '{"valid":true,'
                    '"evidence_entailment_ok":true,'
                    '"beat_coverage_ok":true,'
                    '"temporal_monotonic":true,'
                    '"no_future_event_preconsumption":true,'
                    '"no_result_duplication":true,'
                    '"state_order_valid":true,'
                    '"entity_visibility_valid":true,'
                    '"visual_realization_valid":true,'
                    '"violations":[]}'
                ),
                max_tokens=1300,
                temperature=0.0,
            )

            audit = _parse_object(
                env,
                raw,
                parsed,
            )

            missing = [
                key
                for key in required
                if not isinstance(
                    audit.get(key),
                    bool,
                )
            ]

            if not missing:
                complete = True
                break

        if not complete:
            return {
                "valid": False,
                "violations": [{
                    "type":
                        "scene_audit_schema_incomplete",
                    "shot_index":
                        start + 1,
                    "message":
                        "Scene 最终审计连续返回不完整 schema",
                }],
            }

        window_violations = (
            audit.get(
                "violations"
            )
            if isinstance(
                audit.get(
                    "violations"
                ),
                list,
            )
            else []
        )

        failed = [
            key
            for key in required
            if audit.get(key)
            is not True
        ]

        if failed and not window_violations:
            window_violations = [{
                "type":
                    "scene_audit_dimension_failed",
                "shot_index":
                    start + 1,
                "message":
                    "Scene audit failed dimensions: "
                    + ", ".join(
                        failed
                    ),
            }]

        if (
            failed
            or window_violations
            or audit.get("valid")
            is False
        ):
            violations.extend(
                window_violations
            )

    return {
        "valid":
            not violations,
        "violations":
            violations,
    }


def _row_exact_anchor_rows(
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    ids = list(
        row.get(
            "source_evidence_ids"
        )
        or []
    )

    texts = list(
        row.get(
            "source_evidence"
        )
        or []
    )

    spans = list(
        row.get(
            "source_evidence_spans"
        )
        or []
    )

    if not (
        len(ids)
        == len(texts)
        == len(spans)
    ):
        raise RuntimeError(
            "V2.39.5: Scene repair Shot evidence id/text/span 数量不一致"
        )

    result = []

    for evidence_id, text, span in zip(
        ids,
        texts,
        spans,
    ):
        if not isinstance(
            span,
            dict,
        ):
            raise RuntimeError(
                "V2.39.5: Scene repair Shot evidence span 非对象"
            )

        result.append({
            "id":
                str(
                    evidence_id
                    or ""
                ),
            "text":
                str(
                    text
                    or ""
                ),
            "source_start":
                int(
                    span.get(
                        "start"
                    )
                ),
            "source_end":
                int(
                    span.get(
                        "end"
                    )
                ),
        })

    return result


async def _repair_scene_shot_from_evidence(
    env: dict[str, Any],
    *,
    all_rows: list[dict[str, Any]],
    beats: list[dict[str, Any]],
    shot_index: int,
    scene_audit: dict[str, Any],
    allowed_chars: set[str],
    allowed_props: set[str],
    scene_id: str,
    episode_id: str,
) -> dict[str, Any]:
    row = copy.deepcopy(
        all_rows[
            shot_index - 1
        ]
    )

    anchors = _row_exact_anchor_rows(
        row
    )

    orders = list(
        _orders(
            row.get(
                "covered_beat_orders"
            )
        )
    )

    if not orders:
        raise RuntimeError(
            f"V2.39.5: Scene repair Shot#{shot_index} 无 Beat 绑定"
        )

    beat_map = {
        int(beat.get("order") or 0):
            beat
        for beat in beats or []
        if isinstance(beat, dict)
    }

    compact_beats = []

    for order in orders:
        beat = beat_map.get(
            order
        )

        if not beat:
            raise RuntimeError(
                f"V2.39.5: Scene repair Shot#{shot_index} 找不到 Beat {order}"
            )

        compact_beats.append({
            "order":
                order,
            "summary":
                str(
                    beat.get(
                        "summary"
                    )
                    or ""
                )[:420],
            "state_change":
                str(
                    beat.get(
                        "state_change"
                    )
                    or ""
                )[:420],
            "allowed_source_evidence_ids":
                list(
                    row.get(
                        "source_evidence_ids"
                    )
                    or []
                ),
            "source_evidence_ids":
                list(
                    row.get(
                        "source_evidence_ids"
                    )
                    or []
                ),
            "source_evidence":
                list(
                    row.get(
                        "source_evidence"
                    )
                    or []
                ),
            "source_evidence_spans":
                list(
                    row.get(
                        "source_evidence_spans"
                    )
                    or []
                ),
            "character_entity_ids":
                list(
                    beat.get(
                        "character_entity_ids"
                    )
                    or []
                ),
            "prop_entity_ids":
                list(
                    beat.get(
                        "prop_entity_ids"
                    )
                    or []
                ),
        })

    relevant_violations = []

    for violation in (
        scene_audit.get(
            "violations"
        )
        or []
    ):
        if not isinstance(
            violation,
            dict,
        ):
            continue

        try:
            index = int(
                violation.get(
                    "shot_index"
                )
                or 0
            )
        except Exception:
            index = 0

        if index == shot_index:
            relevant_violations.append(
                violation
            )

    prior_history = [
        _scene_audit_compact_shot(
            item,
            index,
            include_evidence=False,
        )
        for index, item in enumerate(
            all_rows[
                :shot_index - 1
            ],
            1,
        )
    ]

    next_context = (
        _scene_audit_compact_shot(
            all_rows[
                shot_index
            ],
            shot_index + 1,
            include_evidence=False,
        )
        if shot_index
        < len(all_rows)
        else {}
    )

    system_prompt = (
        "你是 strict-shot-v2 Scene 最终 evidence-locked 修复器。"
        "当前 Shot 的 source evidence 和 covered Beat 已锁定。"
        "必须保持 CURRENT_SHOT_LOCKED.temporal_mode。observable_transition 只允许根据"
        " EXACT_SELECTED_EVIDENCE 重写 summary、action、video_start_state、"
        "representative_state、video_end_state 和当前可见 entity IDs。"
        "static_outcome 的 source_fact/summary/narrative state 已锁定，不得重写；"
        "只允许修正 presentation-only visual_realization、realization_assumptions、"
        "visual_start_frame、representative_frame、visual_end_frame、visual_motion，"
        "且不得新增角色、道具、剧情事件、结果、未来事件或关系变化。"
        "PRIOR_HISTORY / NEXT_SHOT_CONTEXT 只用于消除时间倒退和结果重复，"
        "不能成为当前 Shot 新事实来源。"
        "不得修改 duration_seconds、Beat/evidence 绑定。"
        "不要输出三个 Prompt；程序会确定性编译。"
        "只返回严格 JSON。"
    )

    prompt = (
        "=== CURRENT_SHOT_LOCKED ===\n"
        + json.dumps(
            _scene_audit_compact_shot(row, shot_index, include_evidence=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(
            compact_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== SCENE_AUDIT_VIOLATIONS ===\n"
        + json.dumps(
            relevant_violations,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== PRIOR_HISTORY_CONTEXT_ONLY ===\n"
        + json.dumps(
            prior_history,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== NEXT_SHOT_CONTEXT_ONLY ===\n"
        + json.dumps(
            next_context,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    static_mode = _shot_temporal_mode(row) == "static_outcome"
    repair_contract = (
        '{"shot":{"visual_realization":"","realization_scope":"presentation_only",'
        '"realization_assumptions":[],"visual_start_frame":"",'
        '"representative_frame":"","visual_end_frame":"","visual_motion":""}}'
        if static_mode
        else (
            '{"shot":{"summary":"","action":"","representative_state":"",'
            '"video_start_state":"","video_end_state":"",'
            '"character_entity_ids":[],"prop_entity_ids":[]}}'
        )
    )

    raw, parsed, _ = await _qwen(
        env,
        phase=(
            "studio_stage04_"
            "v2390_scene_evidence_locked_repair_qwen32b"
        ),
        system_prompt=
            system_prompt,
        prompt=prompt,
        contract=repair_contract,
        max_tokens=1400,
        temperature=0.0,
    )

    candidate = None

    if (
        isinstance(
            parsed,
            dict,
        )
        and isinstance(
            parsed.get("shot"),
            dict,
        )
    ):
        candidate = dict(
            parsed["shot"]
        )
    elif isinstance(
        parsed,
        dict,
    ):
        candidate = dict(
            parsed
        )

    if not candidate:
        extracted = _extract_shots(
            env,
            raw,
            parsed,
        )

        if len(extracted) == 1:
            candidate = dict(
                extracted[0]
            )

    if not candidate:
        raise RuntimeError(
            f"V2.39.5: Scene repair Shot#{shot_index} 没有可恢复结构化输出"
        )

    writable_fields = (
        (
            "visual_realization",
            "realization_scope",
            "realization_assumptions",
            "visual_start_frame",
            "representative_frame",
            "visual_end_frame",
            "visual_motion",
        )
        if static_mode
        else (
            "summary",
            "action",
            "representative_state",
            "video_start_state",
            "video_end_state",
            "character_entity_ids",
            "prop_entity_ids",
        )
    )
    merged = _merge_shot_repair_patch(
        row,
        candidate,
        writable_fields=writable_fields,
    )

    merged[
        "covered_beat_orders"
    ] = orders

    merged[
        "source_evidence_ids"
    ] = list(
        row.get(
            "source_evidence_ids"
        )
        or []
    )

    validated = validate_rows(
        env,
        raw_rows=[merged],
        compact_beats=
            compact_beats,
        allowed_chars=
            allowed_chars,
        allowed_props=
            allowed_props,
        anchors=anchors,
        scene_id=scene_id,
        episode_id=episode_id,
    )

    if len(validated) != 1:
        raise RuntimeError(
            f"V2.39.5: Scene repair Shot#{shot_index} 验收数量异常"
        )

    return validated[0]


async def scene_shots(
    env: dict[str, Any],
    *,
    project_id: str,
    scene: dict[str, Any],
    state: dict[str, Any],
    source_text: str,
    upstream: dict[str, Any],
    user_input: str,
    scene_index: int,
    scene_total: int,
) -> list[dict[str, Any]]:
    scene_source = env.get(
        "_studio_stage04_scene_source"
    )

    allowed_fn = env.get(
        "_studio_stage04_allowed_ids"
    )

    ensure_beats = env.get(
        "_studio_v2371b_ensure_scene_beats"
    )

    if not (
        callable(scene_source)
        and callable(allowed_fn)
        and callable(ensure_beats)
    ):
        raise RuntimeError(
            "V2.39.5: Stage04 source/Beat 基础能力缺失"
        )

    scene_id = str(
        scene.get("scene_id")
        or ""
    )

    episode_id = str(
        scene.get("episode_id")
        or ""
    )

    resolved = (
        env["story_continuity"]
        .resolve_scene(
            project_id,
            scene_id,
        )
    )

    source = scene_source(
        scene,
        source_text,
    )

    allowed_chars, allowed_props = (
        allowed_fn(
            scene,
            resolved,
        )
    )

    beats, beat_source = (
        await ensure_beats(
            project_id=project_id,
            scene=scene,
            state=state,
            source=source,
            allowed_chars=
                allowed_chars,
            allowed_props=
                allowed_props,
        )
    )

    beats = (
        await reconcile_beat_boundaries(
            env,
            source=source,
            beats=beats,
        )
    )

    _stage04_progress(
        env, 2, "Beat / evidence", "Narrative Beat 已确认，正在绑定镜头证据"
    )

    if not beats:
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} "
            "没有可制作 Narrative Beat"
        )

    entity_rows = _build_entity_context(
        env,
        project_id,
        allowed_chars,
        allowed_props,
    )

    resolved_compact = {
        "location":
            resolved.get(
                "location"
            ),
        "characters":
            resolved.get(
                "characters"
            ),
        "props":
            resolved.get(
                "props"
            ),
        "scene_state":
            resolved.get(
                "scene_state"
            ),
    }

    resolved_text = _cut(
        json.dumps(
            resolved_compact,
            ensure_ascii=False,
        ),
        760,
    )

    character_anchor = _cut(
        upstream.get(
            "character_bible"
        ),
        1100,
    )

    visual_anchor = _cut(
        upstream.get(
            "visual_bible"
        ),
        900,
    )

    batches = [
        beats[
            i:
            i + SHOT_BATCH_SIZE
        ]
        for i in range(
            0,
            len(beats),
            SHOT_BATCH_SIZE,
        )
    ]

    all_rows: list[
        dict[str, Any]
    ] = []

    for batch_index, batch in enumerate(
        batches
    ):
        previous = (
            all_rows[-1]
            if all_rows
            else None
        )

        _stage04_progress(
            env,
            3,
            "Shot generation",
            f"正在生成详细分镜批次 {batch_index + 1}/{len(batches)}",
        )
        batch_started = time.perf_counter()
        try:
            rows = await _produce_batch(
                env,
                batch=batch,
                all_beats=beats,
                batch_index=
                    batch_index,
                batch_total=
                    len(batches),
                source=source,
                scene=scene,
                scene_index=
                    scene_index,
                scene_total=
                    scene_total,
                previous_shot=
                    previous,
                allowed_chars=
                    allowed_chars,
                allowed_props=
                    allowed_props,
                entity_rows=
                    entity_rows,
                resolved_text=
                    resolved_text,
                character_anchor=
                    character_anchor,
                visual_anchor=
                    visual_anchor,
                user_input=
                    user_input,
            )
        except Exception:
            _perf_observe(
                "shot_batch",
                time.perf_counter() - batch_started,
                scene_index=scene_index,
                batch_index=batch_index + 1,
                batch_total=len(batches),
                beat_count=len(batch),
                status="failed",
            )
            raise
        _perf_observe(
            "shot_batch",
            time.perf_counter() - batch_started,
            scene_index=scene_index,
            batch_index=batch_index + 1,
            batch_total=len(batches),
            beat_count=len(batch),
            shot_count=len(rows),
            status="completed",
        )

        for row in rows:
            row[
                "source_batch_index"
            ] = (
                batch_index + 1
            )

            row[
                "beat_source"
            ] = beat_source

            row[
                "evidence_lineage_version"
            ] = (
                "beat-to-shot-v2.39"
            )

            all_rows.append(
                row
            )

    _deterministic_scene_checks(
        all_rows,
        beats,
    )

    # Mandatory final Scene-wide semantic audit catches non-adjacent duplicate
    # results and cross-window temporal drift that pairwise checks cannot see.
    scene_audit = (
        await _scene_global_audit(
            env,
            all_rows=all_rows,
            beats=beats,
        )
    )

    if not scene_audit.get(
        "valid"
    ):
        targets = (
            _scene_audit_target_indices(
                scene_audit,
                row_count=
                    len(all_rows),
            )
        )

        if targets:
            for shot_index in targets:
                all_rows[
                    shot_index - 1
                ] = (
                    await _repair_scene_shot_from_evidence(
                        env,
                        all_rows=
                            all_rows,
                        beats=beats,
                        shot_index=
                            shot_index,
                        scene_audit=
                            scene_audit,
                        allowed_chars=
                            allowed_chars,
                        allowed_props=
                            allowed_props,
                        scene_id=
                            scene_id,
                        episode_id=
                            episode_id,
                    )
                )

            _deterministic_scene_checks(
                all_rows,
                beats,
            )

            scene_audit = (
                await _scene_global_audit(
                    env,
                    all_rows=
                        all_rows,
                    beats=
                        beats,
                )
            )

    if not scene_audit.get(
        "valid"
    ):
        raise RuntimeError(
            f"场景 {scene_index}/{scene_total} "
            "最终 Scene 语义审计未通过："
            + _audit_issues(
                scene_audit
            )
        )

    for index, row in enumerate(
        all_rows,
        1,
    ):
        row["local_order"] = (
            index
        )

        row[
            "scene_global_audit"
        ] = {
            "valid": True,
            "runtime_version":
                VERSION,
        }

        covered = set(_orders(row.get("covered_beat_orders")))
        covered_beats = [
            beat for beat in beats
            if int(beat.get("order") or 0) in covered
        ]
        narrative_ok = bool(covered_beats) and all(
            (
                beat.get("scene_narrative_audit")
                or beat.get("narrative_audit")
                or {}
            ).get("valid") is True
            for beat in covered_beats
        )
        if not narrative_ok:
            raise RuntimeError("Stage04 persisted Shot lacks narrative-audit lineage")
        row["narrative_audit"] = {
            "valid": True,
            "runtime_version": VERSION,
            "covered_beat_orders": sorted(covered),
        }
        projections = [
            beat.get("adjacent_projection") or {}
            for beat in covered_beats
            if isinstance(beat.get("adjacent_projection"), dict)
        ]
        projection_ok = all(
            (item.get("audit") or {}).get("valid") is True
            for item in projections
        )
        if not projection_ok:
            raise RuntimeError("Stage04 persisted Shot lacks forward-overlap audit closure")
        row["forward_overlap_audit"] = {
            "valid": True,
            "required": bool(projections),
            "runtime_version": VERSION,
        }

        if not str(
            row.get("title")
            or ""
        ).strip():
            row["title"] = (
                f"{scene.get('title') or '场景'}"
                f" · 镜头{index}"
            )

    return all_rows




def _transaction_journal_path(env: dict[str, Any], project_id: str) -> Path:
    return Path(env["director"].production._project_dir(project_id)) / ".stage04-rebuild-transaction.json"


def _encode_optional_bytes(value: bytes | None) -> str | None:
    return base64.b64encode(value).decode("ascii") if value is not None else None


def _decode_optional_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Stage04 transaction journal bytes are invalid")
    return base64.b64decode(value.encode("ascii"), validate=True)


def _write_transaction_journal(snapshot: dict[str, Any], project_id: str) -> None:
    journal_path: Path = snapshot["journal_path"]
    payload = {
        "schema_version": "stage04-transaction-v1",
        "runtime_version": VERSION,
        "project_id": project_id,
        "project_bytes": _encode_optional_bytes(snapshot["project_bytes"]),
        "continuity_bytes": _encode_optional_bytes(snapshot["continuity_bytes"]),
        "graph_bytes": _encode_optional_bytes(snapshot["graph_bytes"]),
        "files_before": sorted(snapshot["files_before"]),
    }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    temp = journal_path.with_suffix(journal_path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp.replace(journal_path)


def _project_transaction_snapshot(env: dict[str, Any], project_id: str) -> dict[str, Any]:
    director = env["director"]
    continuity = env["story_continuity"]
    production = director.production
    project_path = Path(director._project_path(project_id))
    continuity_path = Path(continuity._path(project_id))
    project_dir = Path(production._project_dir(project_id))
    graph_path = Path(production._graph_path(project_id))
    files_before = {str(path.relative_to(project_dir)) for path in project_dir.rglob("*") if path.is_file()}
    snapshot = {
        "project_path": project_path,
        "project_bytes": project_path.read_bytes() if project_path.is_file() else None,
        "continuity_path": continuity_path,
        "continuity_bytes": continuity_path.read_bytes() if continuity_path.is_file() else None,
        "project_dir": project_dir,
        "graph_path": graph_path,
        "graph_bytes": graph_path.read_bytes() if graph_path.is_file() else None,
        "files_before": files_before,
        "journal_path": _transaction_journal_path(env, project_id),
    }
    _write_transaction_journal(snapshot, project_id)
    return snapshot


def recover_project_transaction(env: dict[str, Any], project_id: str) -> bool:
    """Restore an interrupted canonical switch before any project read/write."""
    journal_path = _transaction_journal_path(env, project_id)
    if not journal_path.is_file():
        return False
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != "stage04-transaction-v1"
        or str(payload.get("project_id") or "") != str(project_id)
    ):
        raise RuntimeError("Stage04 transaction journal identity mismatch")
    director = env["director"]
    continuity = env["story_continuity"]
    production = director.production
    snapshot = {
        # Paths are recomputed from the requested project; journal content can
        # never redirect recovery outside that project's storage.
        "project_path": Path(director._project_path(project_id)),
        "project_bytes": _decode_optional_bytes(payload.get("project_bytes")),
        "continuity_path": Path(continuity._path(project_id)),
        "continuity_bytes": _decode_optional_bytes(payload.get("continuity_bytes")),
        "project_dir": Path(production._project_dir(project_id)),
        "graph_path": Path(production._graph_path(project_id)),
        "graph_bytes": _decode_optional_bytes(payload.get("graph_bytes")),
        "files_before": {str(value) for value in (payload.get("files_before") or [])},
        "journal_path": journal_path,
    }
    _restore_transaction(snapshot)
    return True


def _clear_transaction(snapshot: dict[str, Any]) -> None:
    journal_path = Path(snapshot["journal_path"])
    try:
        journal_path.unlink()
    except FileNotFoundError:
        pass


def _persist_rebuild_task(env: dict[str, Any], task: dict[str, Any]) -> None:
    callback = env.get("_studio_v23963_persist_stage04_task")
    if callable(callback):
        callback(task)


def _restore_transaction(snapshot: dict[str, Any]) -> None:
    project_dir: Path = snapshot["project_dir"]
    before: set[str] = set(snapshot["files_before"])
    if project_dir.exists():
        for path in sorted([p for p in project_dir.rglob("*") if p.is_file()], key=lambda p: len(p.parts), reverse=True):
            rel = str(path.relative_to(project_dir))
            if path == snapshot.get("journal_path"):
                continue
            if rel not in before:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    for key_path, key_bytes in (("project_path", "project_bytes"), ("continuity_path", "continuity_bytes"), ("graph_path", "graph_bytes")):
        path: Path = snapshot[key_path]
        data = snapshot[key_bytes]
        if data is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".v238rollback")
            temp.write_bytes(data)
            temp.replace(path)
    _clear_transaction(snapshot)


def _prune_stale_shot_entities(env: dict[str, Any], project_id: str, scenes: list[dict[str, Any]], new_entity_ids: set[str]) -> None:
    production = env["director"].production
    graph = production.ensure_project(project_id)
    prefixes = {
        f"continuity:shot:{int(scene.get('sequence') or 0):06d}:"
        for scene in scenes
    }
    remove_ids = {
        entity_id
        for entity_id, item in (graph.get("entities") or {}).items()
        if isinstance(item, dict)
        and str(item.get("entity_type") or "").lower() == "shot"
        and str(item.get("stage") or "") == "04"
        and any(str(item.get("logical_key") or "").startswith(prefix) for prefix in prefixes)
        and entity_id not in new_entity_ids
    }
    if not remove_ids:
        return
    for entity_id in remove_ids:
        graph.get("entities", {}).pop(entity_id, None)
    graph["relations"] = [
        relation for relation in (graph.get("relations") or [])
        if str(relation.get("source_id") or "") not in remove_ids
        and str(relation.get("target_id") or "") not in remove_ids
    ]
    production._save(graph)


def _commit_formal_shots(
    env: dict[str, Any],
    *,
    project_id: str,
    state: dict[str, Any],
    scenes: list[dict[str, Any]],
    all_shots: list[dict[str, Any]],
) -> int:
    director = env["director"]
    scope_ids = {str(scene.get("scene_id") or "") for scene in scenes}
    preserved = [row for row in (state.get("shots") or []) if str(row.get("scene_id") or "") not in scope_ids]
    scene_map = {str(scene.get("scene_id") or ""): scene for scene in scenes}
    formal: list[dict[str, Any]] = []
    local_counts: dict[str, int] = {}
    new_entity_ids: set[str] = set()

    for global_index, shot in enumerate(all_shots, 1):
        scene_id = str(shot.get("scene_id") or "")
        scene = scene_map.get(scene_id)
        if not scene:
            raise RuntimeError(f"V2.39.5: Shot 指向未知 scene_id={scene_id}")
        if str(shot.get("stage04_contract_version") or "") != CONTRACT_VERSION:
            raise RuntimeError("V2.39.5: 拒绝写入非 strict-shot-v2 Shot")
        if not list(shot.get("source_evidence") or []):
            raise RuntimeError("V2.39.5: 拒绝写入无 Shot 级原文证据")
        scene_entity_id = str(scene.get("entity_id") or "").strip()
        if not scene_entity_id:
            raise RuntimeError(f"V2.39.5: scene {scene_id} 缺少 production entity_id")

        local_order = local_counts.get(scene_id, 0) + 1
        local_counts[scene_id] = local_order
        scene_sequence = int(scene.get("sequence") or 0)
        logical_key = f"continuity:shot:{scene_sequence:06d}:{local_order:04d}"
        provenance = {
            "contract_version": CONTRACT_VERSION,
            "text_model_policy": "qwen3-32b",
            "runtime_version": VERSION,
            "scene_id": scene_id,
            "source_start": scene.get("source_start"),
            "source_end": scene.get("source_end"),
            "source_batch_index": int(shot.get("source_batch_index") or 0),
            "covered_beat_orders": list(shot.get("covered_beat_orders") or []),
            "source_evidence_ids": list(shot.get("source_evidence_ids") or []),
            "source_evidence": list(shot.get("source_evidence") or []),
            "source_evidence_spans": list(shot.get("source_evidence_spans") or []),
            "temporal_mode": str(shot.get("temporal_mode") or ""),
            "temporal_mode_reason": str(shot.get("temporal_mode_reason") or ""),
            "temporal_mode_evidence_ids": list(
                shot.get("temporal_mode_evidence_ids") or []
            ),
            "temporal_mode_source_spans": copy.deepcopy(
                shot.get("temporal_mode_source_spans") or []
            ),
            "source_fact": str(shot.get("source_fact") or ""),
            "realization_scope": str(shot.get("realization_scope") or ""),
            "realization_assumptions": list(
                shot.get("realization_assumptions") or []
            ),
        }
        continuity_meta = {
            "scene_id": scene_id,
            "order": local_order,
            "global_order": global_index,
            "duration_seconds": shot.get("duration_seconds"),
            "shot_size": shot.get("shot_size"),
            "camera": shot.get("camera"),
            "camera_move": shot.get("camera_move"),
            "action": shot.get("action"),
            "performance": shot.get("performance"),
            "dialogue": shot.get("dialogue"),
            "continuity": shot.get("continuity"),
            "representative_state": shot.get("representative_state"),
            "video_start_state": shot.get("video_start_state"),
            "video_end_state": shot.get("video_end_state"),
            "temporal_mode": shot.get("temporal_mode"),
            "source_fact": shot.get("source_fact"),
            "narrative_start_state": shot.get("narrative_start_state"),
            "narrative_state": shot.get("narrative_state"),
            "narrative_end_state": shot.get("narrative_end_state"),
            "visual_realization": shot.get("visual_realization"),
            "realization_scope": shot.get("realization_scope"),
            "realization_assumptions": list(
                shot.get("realization_assumptions") or []
            ),
            "visual_start_frame": shot.get("visual_start_frame"),
            "representative_frame": shot.get("representative_frame"),
            "visual_end_frame": shot.get("visual_end_frame"),
            "visual_motion": shot.get("visual_motion"),
            "image_prompt": shot.get("image_prompt"),
            "video_start_prompt": shot.get("video_start_prompt"),
            "video_prompt": shot.get("video_prompt"),
            "covered_beat_orders": list(shot.get("covered_beat_orders") or []),
            "source_provenance": provenance,
            "batch_audit": copy.deepcopy(shot.get("source_audit") or {}),
            "narrative_audit": copy.deepcopy(shot.get("narrative_audit") or {}),
            "scene_global_audit": copy.deepcopy(shot.get("scene_global_audit") or {}),
            "forward_overlap_audit": copy.deepcopy(shot.get("forward_overlap_audit") or {}),
            "stage04_contract_version": CONTRACT_VERSION,
            "runtime_version": VERSION,
        }
        entity = director.production.create_entity(
            project_id,
            entity_type="shot",
            name=f"镜头 {global_index:03d} · {shot.get('title') or scene.get('title') or ''}",
            logical_key=logical_key,
            stage="04",
            skill=env["_STUDIO_STAGE_SKILLS"]["04"],
            metadata={"continuity": continuity_meta},
        )
        entity_id = str(entity.get("entity_id") or "")
        if not entity_id:
            raise RuntimeError("V2.39.5: create_entity 未返回 entity_id")
        new_entity_ids.add(entity_id)

        # Mandatory relations: never swallow failures.
        director.production.add_relation(
            project_id,
            source_id=scene_entity_id,
            target_id=entity_id,
            relation_type="contains",
            metadata={"source": "studio_stage04_v2390"},
        )
        for related_id in [*(shot.get("character_entity_ids") or []), *(shot.get("prop_entity_ids") or [])]:
            related_id = str(related_id or "").strip()
            if not related_id:
                continue
            director.production.add_relation(
                project_id,
                source_id=related_id,
                target_id=entity_id,
                relation_type="appears_in",
                metadata={"source": "studio_stage04_v2390"},
            )

        formal.append({
            "shot_id": "shot_" + secrets.token_hex(8),
            "entity_id": entity_id,
            "scene_id": scene_id,
            "episode_id": str(scene.get("episode_id") or ""),
            "title": str(shot.get("title") or ""),
            "order": local_order,
            "global_order": global_index,
            "sequence": scene_sequence * 1000 + local_order,
            "duration_seconds": shot.get("duration_seconds"),
            "summary": str(shot.get("summary") or ""),
            "temporal_mode": str(shot.get("temporal_mode") or ""),
            "temporal_mode_reason": str(shot.get("temporal_mode_reason") or ""),
            "temporal_mode_evidence_ids": list(
                shot.get("temporal_mode_evidence_ids") or []
            ),
            "temporal_mode_source_spans": copy.deepcopy(
                shot.get("temporal_mode_source_spans") or []
            ),
            "source_fact": str(shot.get("source_fact") or ""),
            "narrative_start_state": str(shot.get("narrative_start_state") or ""),
            "narrative_state": str(shot.get("narrative_state") or ""),
            "narrative_end_state": str(shot.get("narrative_end_state") or ""),
            "visual_realization": str(shot.get("visual_realization") or ""),
            "realization_scope": str(shot.get("realization_scope") or ""),
            "realization_assumptions": list(
                shot.get("realization_assumptions") or []
            ),
            "visual_start_frame": str(shot.get("visual_start_frame") or ""),
            "representative_frame": str(shot.get("representative_frame") or ""),
            "visual_end_frame": str(shot.get("visual_end_frame") or ""),
            "visual_motion": str(shot.get("visual_motion") or ""),
            "composition": str(shot.get("composition") or ""),
            "shot_size": str(shot.get("shot_size") or ""),
            "camera": str(shot.get("camera") or ""),
            "camera_move": str(shot.get("camera_move") or ""),
            "action": str(shot.get("action") or ""),
            "performance": str(shot.get("performance") or ""),
            "environment": str(shot.get("environment") or ""),
            "dialogue": str(shot.get("dialogue") or ""),
            "narration": str(shot.get("narration") or ""),
            "sound": str(shot.get("sound") or ""),
            "music": str(shot.get("music") or ""),
            "continuity": str(shot.get("continuity") or ""),
            "representative_state": str(shot.get("representative_state") or ""),
            "video_start_state": str(shot.get("video_start_state") or ""),
            "video_end_state": str(shot.get("video_end_state") or ""),
            "image_prompt": str(shot.get("image_prompt") or ""),
            "video_start_prompt": str(shot.get("video_start_prompt") or ""),
            "video_prompt": str(shot.get("video_prompt") or ""),
            "covered_beat_orders": list(shot.get("covered_beat_orders") or []),
            "source_provenance": provenance,
            "batch_audit": copy.deepcopy(shot.get("source_audit") or {}),
            "narrative_audit": copy.deepcopy(shot.get("narrative_audit") or {}),
            "scene_global_audit": copy.deepcopy(shot.get("scene_global_audit") or {}),
            "forward_overlap_audit": copy.deepcopy(shot.get("forward_overlap_audit") or {}),
            "character_entity_ids": list(shot.get("character_entity_ids") or []),
            "prop_entity_ids": list(shot.get("prop_entity_ids") or []),
            "stage04_contract_version": CONTRACT_VERSION,
            "text_model_policy": "qwen3-32b",
            "runtime_version": VERSION,
            "provisional": False,
        })

    state["shots"] = preserved + formal
    _prune_stale_shot_entities(env, project_id, scenes, new_entity_ids)
    return len(formal)


async def rebuild(env: dict[str, Any], project_id: str, task_id: str) -> None:
    tasks = env["_STUDIO_V2371_REBUILD_TASKS"]
    task = tasks[project_id]
    preflight = dict(task.get("performance") or {})
    profile: dict[str, Any] = {
        "schema_version": "stage04-perf-v1",
        "workspace_start_seconds": float(
            preflight.get("workspace_start_seconds") or 0.0
        ),
        "qwen_ready_wait_seconds": float(
            preflight.get("qwen_ready_wait_seconds") or 0.0
        ),
        "qwen_contract_verified": bool(
            preflight.get("qwen_contract_verified")
        ),
        "scenes": [],
        "phases": {},
        "categories": {},
        "repairs": {},
        "llm_calls": 0,
        "llm_retries": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    task["performance"] = profile
    perf_token = _PERF_CONTEXT.set(profile)
    env["_studio_v2396_perf_observe"] = _perf_observe
    env["_studio_v2396_perf_record_llm"] = _perf_record_llm
    env["_studio_v2396_qwen_contract_cached"] = _perf_contract_cached
    rebuild_started = time.perf_counter()
    transaction = None
    persistence_started = False
    workspace_guard = None
    workspace_guard_entered = False
    try:
        workspace_guard = env["gpu"].use(env["GPUOwner"].gemma)
        await workspace_guard.__aenter__()
        workspace_guard_entered = True
        profile["_workspace_guard_active"] = True
        director = env["director"]
        continuity = env["story_continuity"]
        project = director.get_project(project_id)
        state = continuity.load(project_id)
        scenes, active_episode = env["_studio_stage04_scope"](state)
        if not scenes:
            raise RuntimeError("没有可用于重建正式分镜的 Scene")
        source_asset_id, source_text = env["_studio_stage04_full_source"](project_id)
        upstream = env["_studio_stage04_upstream"](project)
        all_shots: list[dict[str, Any]] = []
        scene_stats: list[dict[str, Any]] = []

        def progress_update(**values: Any) -> None:
            task.update({
                "status": "running",
                "phase_index": int(values.get("phase_index") or 1),
                "phase_total": int(values.get("phase_total") or 6),
                "phase_name": str(values.get("phase_name") or "Stage04"),
                "message": str(values.get("message") or "正在处理分镜"),
                "updated_at": env["_studio_now"](),
            })
            _persist_rebuild_task(env, task)

        env["_studio_stage04_progress_update"] = progress_update

        for index, scene in enumerate(scenes, 1):
            scene_started = time.perf_counter()
            profile["_current_scene_index"] = index
            task.update({
                "status": "running",
                "message": f"V2.39.2 正在重建严格分镜：场景 {index}/{len(scenes)}",
                "scene_done": index - 1,
                "scene_total": len(scenes),
                "shots_done": len(all_shots),
                "updated_at": env["_studio_now"](),
            })
            _persist_rebuild_task(env, task)
            _stage04_progress(
                env,
                1,
                "Narrative analysis",
                f"正在分析场景叙事 {index}/{len(scenes)}",
            )
            try:
                rows = await scene_shots(
                    env,
                    project_id=project_id,
                    scene=scene,
                    state=state,
                    source_text=source_text,
                    upstream=upstream,
                    user_input="按 strict-shot-v2 重建正式制作合同；逐 Beat 前向推进，不提前消费后续事件，不继承不可见实体。",
                    scene_index=index,
                    scene_total=len(scenes),
                )
            except Exception:
                _perf_observe(
                    "scene",
                    time.perf_counter() - scene_started,
                    scene_index=index,
                    scene_id=str(scene.get("scene_id") or ""),
                    title=str(scene.get("title") or ""),
                    status="failed",
                )
                raise
            scene_seconds = time.perf_counter() - scene_started
            _perf_observe(
                "scene",
                scene_seconds,
                scene_index=index,
                scene_id=str(scene.get("scene_id") or ""),
                title=str(scene.get("title") or ""),
                shot_count=len(rows),
                status="completed",
            )
            all_shots.extend(rows)
            scene_stats.append({
                "scene_id": str(scene.get("scene_id") or ""),
                "title": str(scene.get("title") or ""),
                "shot_count": len(rows),
                "performance_seconds": round(scene_seconds, 6),
            })
            profile.pop("_current_scene_index", None)

        final_text = env["_studio_stage04_markdown"](project, scenes, all_shots)
        if not str(final_text or "").strip():
            raise RuntimeError("V2.39.5: 严格详细分镜为空")

        # Nothing persistent has been mutated before this point.
        _stage04_progress(
            env, 6, "Persistence / finalize", "严格校验通过，正在原子写入正式分镜"
        )
        task.update({
            "status": "persisting",
            "message": "Stage04 validation complete; atomically switching canonical data",
            "updated_at": env["_studio_now"](),
        })
        _persist_rebuild_task(env, task)
        transaction = _project_transaction_snapshot(env, project_id)
        persistence_started = True

        entity_ids: list[str] = []
        for shot in all_shots:
            for entity_id in [*(shot.get("character_entity_ids") or []), *(shot.get("prop_entity_ids") or [])]:
                entity_id = str(entity_id or "").strip()
                if entity_id and entity_id not in entity_ids:
                    entity_ids.append(entity_id)

        asset = director.production.create_text_asset(
            project_id,
            stage="04",
            skill=env["_STUDIO_STAGE_SKILLS"]["04"],
            logical_key="studio:stage04:detailed-storyboard",
            asset_role="storyboard_master",
            name="完整详细分镜表 · strict-shot-v2 · V2.39.3",
            content=final_text,
            asset_type="TEXT",
            extension=".md",
            source={"type": "studio_stage04_v2395_forward_overlap_projection", "text_model_policy": "qwen3-32b"},
            parent_asset_ids=[source_asset_id] if source_asset_id else [],
            entity_ids=entity_ids,
            metadata={
                "studio_stage04_detailed": True,
                "stage04_contract_version": CONTRACT_VERSION,
                "runtime_version": VERSION,
                "text_model_policy": "qwen3-32b",
                "scene_count": len(scenes),
                "shot_count": len(all_shots),
                "active_episode_id": active_episode,
                "scene_stats": scene_stats,
            },
        )

        formal_count = _commit_formal_shots(
            env,
            project_id=project_id,
            state=state,
            scenes=scenes,
            all_shots=all_shots,
        )
        if formal_count != len(all_shots):
            raise RuntimeError(f"V2.39.5: 正式 Shot 写入数量不一致 generated={len(all_shots)} formal={formal_count}")

        final_sha = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        state["storyboard_source_sha256"] = final_sha
        continuity.save(project_id, state)

        project = director.get_project(project_id)
        stage_state = project.setdefault("stage_state", {}).setdefault("04", {})
        pipeline = {
            "schema_version": "studio_stage04_strict_v2390",
            "stage04_contract_version": CONTRACT_VERSION,
            "runtime_version": VERSION,
            "text_model_policy": "qwen3-32b",
            "ready": True,
            "coverage_ok": True,
            "confirmed": True,
            "asset_id": asset["asset_id"],
            "asset_sha256": final_sha,
            "scene_count": len(scenes),
            "covered_scene_count": len(scene_stats),
            "shot_count": len(all_shots),
            "formal_shot_count": formal_count,
            "active_episode_id": active_episode,
            "generated_at": env["_studio_now"](),
            "confirmed_at": env["_studio_now"](),
            "scene_stats": scene_stats,
        }
        handoff = final_text[:12000]
        completion = {
            "ready": True,
            "reason": "studio_stage04_v2390_complete",
            "missing_artifact_ids": [],
            "missing_requirement_ids": [],
            "required_artifact_ids": ["studio_stage04_detailed_storyboard"],
        }
        stage_state["studio_stage04_pipeline"] = pipeline
        stage_state["handoff"] = handoff
        stage_state["stage_ready"] = True
        stage_state.setdefault("skill_runtime", {})["completion"] = completion
        project.setdefault("confirmed_outputs", {})["04"] = {
            "skill": env["_STUDIO_STAGE_SKILLS"]["04"],
            "handoff": handoff,
            "handoff_audit": {
                "valid": True,
                "provenance_verified": True,
                "contract_version": CONTRACT_VERSION,
                "runtime_version": VERSION,
                "source": "studio_stage04_v2390_consolidated_hardening",
                "source_asset_id": asset["asset_id"],
                "source_sha256": final_sha,
                "scene_count": len(scenes),
                "shot_count": formal_count,
            },
            "completion": completion,
            "production_asset_ids": [asset["asset_id"]],
            "production_stage_status": director.production.stage_status(project_id, "04"),
            "studio_stage04_pipeline": pipeline,
            "confirmed_at": env["_studio_now"](),
        }
        project["updated_at"] = env["_studio_now"]()
        director._save_project(project)
        _clear_transaction(transaction)

        task.update({
            "status": "completed",
            "message": f"V2.39.2 严格分镜重建完成：{len(scenes)} 场 / {formal_count} 个正式镜头",
            "scene_done": len(scenes),
            "scene_total": len(scenes),
            "shots_done": formal_count,
            "formal_shots": formal_count,
            "asset_id": asset["asset_id"],
            "runtime_version": VERSION,
            "updated_at": env["_studio_now"](),
        })
        _persist_rebuild_task(env, task)
    except Exception as exc:
        rollback_error = ""
        if persistence_started and transaction is not None:
            try:
                _restore_transaction(transaction)
            except Exception as rollback_exc:
                rollback_error = f"; ROLLBACK_ERROR={type(rollback_exc).__name__}: {rollback_exc}"
        failure_metadata = getattr(exc, "metadata", None)
        task.update({
            "status": "failed",
            "message": str(exc) + rollback_error,
            "error": f"{type(exc).__name__}: {exc}" + rollback_error,
            "runtime_version": VERSION,
            "updated_at": env["_studio_now"](),
        })
        if isinstance(failure_metadata, dict) and failure_metadata:
            task["failure_metadata"] = copy.deepcopy(failure_metadata)
        _persist_rebuild_task(env, task)
    finally:
        try:
            if workspace_guard_entered and workspace_guard is not None:
                await workspace_guard.__aexit__(None, None, None)
        finally:
            env.pop("_studio_stage04_progress_update", None)
            profile.pop("_workspace_guard_active", None)
            profile.pop("_current_scene_index", None)
            final_profile = _perf_finalize(
                profile,
                float(profile.get("workspace_start_seconds") or 0.0)
                + float(profile.get("qwen_ready_wait_seconds") or 0.0)
                + (time.perf_counter() - rebuild_started),
            )
            task["performance"] = final_profile
            _persist_rebuild_task(env, task)
            _perf_print(final_profile)
            _PERF_CONTEXT.reset(perf_token)
