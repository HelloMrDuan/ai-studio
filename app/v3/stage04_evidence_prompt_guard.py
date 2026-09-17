from __future__ import annotations

import copy
import json
import sys
from typing import Any, Callable


_MISSING_BEAT_PHASE = "studio_stage04_v2392_missing_beat_completion_qwen32b"
_DIRECTIONAL_REPAIR_PHASE = "studio_stage04_v2383_evidence_locked_repair_qwen32b"
_PROMPT_FIELDS = ("video_start_prompt", "image_prompt", "video_prompt")
_SCOPE_KEYS = (
    "order",
    "allowed_source_evidence_ids",
    "source_evidence_ids",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _section_bounds(text: str, name: str) -> tuple[int, int, int] | None:
    marker = f"=== {name} ==="
    start = text.find(marker)
    if start < 0:
        return None
    content_start = start + len(marker)
    next_marker = text.find("=== ", content_start)
    content_end = len(text) if next_marker < 0 else next_marker
    return start, content_start, content_end


def _section_json(text: str, name: str) -> Any:
    bounds = _section_bounds(text, name)
    if bounds is None:
        return None
    _start, content_start, content_end = bounds
    raw = text[content_start:content_end].strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _replace_section(text: str, name: str, value: Any) -> str:
    bounds = _section_bounds(text, name)
    if bounds is None:
        return text
    _start, content_start, content_end = bounds
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text[:content_start] + "\n" + rendered + "\n\n" + text[content_end:]


def _minimal_scope(value: Any) -> Any:
    if isinstance(value, list):
        return [_minimal_scope(item) for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return {}
    return {
        key: copy.deepcopy(value.get(key))
        for key in _SCOPE_KEYS
        if value.get(key) not in (None, "", [], {})
    }


def _minimize_scope_section(text: str, name: str) -> str:
    value = _section_json(text, name)
    return _replace_section(text, name, _minimal_scope(value))


def _blank_failed_current_shot(text: str) -> str:
    current = _section_json(text, "CURRENT_SHOT")
    failed = _section_json(text, "FAILED_FIELDS")
    if not isinstance(current, dict):
        return text
    failed_fields = {
        _clean(field)
        for field in (failed if isinstance(failed, list) else [])
        if _clean(field)
    }
    guarded = copy.deepcopy(current)
    for field in failed_fields:
        if field.endswith("_entity_ids"):
            guarded[field] = []
        else:
            guarded[field] = ""
    # Prompt text is derived from the accepted states by the runtime.  Feeding
    # a previously rejected prompt back into semantic repair only gives Qwen a
    # second route to copy unsupported adjacent-beat facts.
    for field in _PROMPT_FIELDS:
        if field in guarded:
            guarded[field] = ""
    return _replace_section(text, "CURRENT_SHOT", guarded)


def _guard_missing_beat_prompt(prompt: str) -> str:
    guarded = str(prompt or "")
    # TARGET_BEAT supplies structural scope only.  Its summary/state_change is
    # not fact authority and can contain an adjacent projection wider than the
    # evidence selected for this one Shot.
    guarded = _minimize_scope_section(guarded, "TARGET_BEAT")
    # Previous/next narrative is useful to a boundary auditor but must never be
    # visible to target-only semantic generation.  The independent boundary
    # audit still runs after generation with the real neighbouring Shot.
    guarded = _replace_section(guarded, "PREVIOUS_ACCEPTED_SHOT", {})
    guarded = _replace_section(guarded, "NEXT_BEAT_PREVIEW_DO_NOT_CONSUME", {})
    return guarded


def _guard_directional_repair_prompt(prompt: str) -> str:
    guarded = str(prompt or "")
    # Locked Beat rows are reduced to IDs/order; the exact selected evidence is
    # the sole source of semantic facts during repair.
    guarded = _minimize_scope_section(guarded, "LOCKED_COVERED_BEATS")
    guarded = _blank_failed_current_shot(guarded)
    guarded = _replace_section(guarded, "PREVIOUS_ACCEPTED_SHOT", {})
    guarded = _replace_section(guarded, "NEXT_CURRENT_SHOT_CONTEXT_ONLY", {})
    guarded = _replace_section(guarded, "NEXT_BEAT_PREVIEW_DO_NOT_CONSUME", {})
    return guarded


def guard_stage04_prompt(phase: str, system_prompt: str, prompt: str) -> tuple[str, str]:
    phase_name = _clean(phase)
    guarded_system = str(system_prompt or "")
    guarded_prompt = str(prompt or "")
    if phase_name == _MISSING_BEAT_PHASE:
        guarded_prompt = _guard_missing_beat_prompt(guarded_prompt)
    elif phase_name == _DIRECTIONAL_REPAIR_PHASE:
        guarded_prompt = _guard_directional_repair_prompt(guarded_prompt)
    else:
        return guarded_system, guarded_prompt

    guarded_system += (
        " RUNTIME_EVIDENCE_GUARD：本次语义生成/修复只能使用当前 Shot 的"
        " EXACT/ALLOWED evidence；运行时已移除相邻 Shot、相邻 Beat 和 Beat 摘要中的"
        "叙事事实。不得猜测或恢复被移除的相邻上下文。"
    )
    return guarded_system, guarded_prompt


def install_stage04_evidence_prompt_guard(runtime: Any | None = None) -> dict[str, Any]:
    runtime = runtime or sys.modules.get("app.stage04_v238_runtime")
    if runtime is None:
        return {"status": "runtime_unavailable"}
    if getattr(runtime, "_xiaoduan_evidence_prompt_guard_installed", False):
        return {"status": "already_installed", "policy": "locked_evidence_only_v1"}

    original_qwen: Callable[..., Any] | None = getattr(runtime, "_qwen", None)
    if not callable(original_qwen):
        return {"status": "qwen_hook_unavailable"}

    async def guarded_qwen(*args: Any, **kwargs: Any) -> Any:
        phase = _clean(kwargs.get("phase"))
        system_prompt = str(kwargs.get("system_prompt") or "")
        prompt = str(kwargs.get("prompt") or "")
        if phase in {_MISSING_BEAT_PHASE, _DIRECTIONAL_REPAIR_PHASE}:
            system_prompt, prompt = guard_stage04_prompt(
                phase,
                system_prompt,
                prompt,
            )
            kwargs["system_prompt"] = system_prompt
            kwargs["prompt"] = prompt
        return await original_qwen(*args, **kwargs)

    runtime._qwen = guarded_qwen
    runtime._xiaoduan_evidence_prompt_guard_installed = True
    runtime._xiaoduan_evidence_prompt_guard_original_qwen = original_qwen
    return {
        "status": "installed",
        "policy": "locked_evidence_only_v1",
        "guarded_phases": [_MISSING_BEAT_PHASE, _DIRECTIONAL_REPAIR_PHASE],
    }


__all__ = [
    "guard_stage04_prompt",
    "install_stage04_evidence_prompt_guard",
]
