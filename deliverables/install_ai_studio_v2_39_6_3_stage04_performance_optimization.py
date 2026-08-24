#!/usr/bin/env python3
"""Transactional V2.39.6.3 Stage04 performance optimization installer.

The installer transforms only an exact, hash-guarded baseline. Rollback uses
the exact bytes captured from the live installation. It never starts a
Stage04 rebuild or any image/video generation task.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BASELINE_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
TARGET_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
INSTALLER_VERSION = "V2.39.6.3-stage04-performance-optimization-installer"
BASE_URL = "http://127.0.0.1:6008"
ROOT_CANDIDATES = (
    Path("/root/autodl-tmp/ai-studio/platform-v2"),
    Path("/root/autodl-tmp/platform-v2"),
)
PYTHON_CANDIDATES = (
    Path("/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python"),
    Path("/root/miniconda3/envs/ai-studio/bin/python"),
)
REQUIRED_ROOT_FILES = (
    Path("app/main.py"),
    Path("app/stage04_v238_runtime.py"),
)
ACTIVE = {
    "starting", "warming", "queued", "switching_gpu", "running",
    "repairing", "auditing", "persisting", "generating",
}

# TARGET hashes are filled from the deterministic transformations below.
FILES: dict[str, dict[str, Any]] = {
    "app/main.py": {
        "baseline_sha256": "82c5ce06876ea3f17dba1853af2b4ffcbe2c2ca13f93ea78a7005d107a12c787",
        "target_sha256": "91685842a3178ec8c0b3c1a36eba6c87a6cc8d7946e69771d32063350e9c595e",
    },
    "app/services/gemma.py": {
        "baseline_sha256": "e50246b026bc65f4eb2b997af30004f8f9cd9f38109a06b78299a83c1ed5a4de",
        "target_sha256": "f84fe348213f88d82da87207cb473c05ce6133bdc5e30bbb21d2a98a2d9088d4",
    },
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    require(count == 1, f"patch anchor {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


def replace_last(text: str, old: str, new: str, label: str) -> str:
    index = text.rfind(old)
    require(index >= 0, f"patch anchor {label!r} not found")
    return text[:index] + new + text[index + len(old):]


def build_main(source: bytes) -> bytes:
    text = source.decode("utf-8")

    text = replace_once(
        text,
        '"studio_stage04_batched_anchor_classification_qwen32b": 420,',
        '"studio_stage04_batched_anchor_classification_qwen32b": 750,',
        "P0 classification output budget",
    )

    text = replace_once(
        text,
        """            except Exception:
                pass

        # V2.39.6_STAGE04_OUTPUT_BUDGET_CLAMP""",
        """            except RuntimeError:
                # V2.39.6.3_PERF_CONTEXT_GUARD: never dispatch a request the
                # active tokenizer has already proved cannot fit.
                raise
            except Exception:
                pass

        # V2.39.6_STAGE04_OUTPUT_BUDGET_CLAMP""",
        "P2 enforce existing token guard",
    )

    old_classification = """    plan = {}

    for attempt in range(2):
        raw, parsed, _ = (
            await _studio_v2371a_qwen_call(
                phase=(
                    "studio_stage04_"
                    "batched_anchor_classification_qwen32b"
                ),
                messages=[{
                    "role": "user",
                    "content": prompt + (
                        ""
                        if attempt == 0
                        else (
                            "\\n\\nSTRICT_RETRY："
                            "REQUESTED_ANCHORS 中每个 id "
                            "必须恰好出现一次；"
                            "beat_ids 与 support_evidence_ids 必须互斥。"
                        )
                    ),
                }],
                system_prompt=system_prompt,
                temperature=(
                    0.03
                    if attempt == 0
                    else 0.0
                ),
                max_tokens=750,
                contract=(
                    '{"beat_ids":["C01E001"],'
                    '"support_evidence_ids":["C01E002"]}'
                ),
            )
        )

        (
            candidate,
            _origin,
            _diagnostics,
        ) = (
            _studio_v2373_extract_classification_plan(
                raw=raw,
                parsed=parsed,
                anchors=batch_anchors,
            )
        )

        if candidate:
            plan = candidate
            break
"""
    new_classification = """    # V2.39.6.3_PERF_PARTIAL_CLASSIFICATION
    # One primary request only. The parser may return a partial, valid plan;
    # retain it and repair only missing/conflicting IDs below.
    raw, parsed, _ = (
        await _studio_v2371a_qwen_call(
            phase=(
                "studio_stage04_"
                "batched_anchor_classification_qwen32b"
            ),
            messages=[{
                "role": "user",
                "content": prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.03,
            max_tokens=750,
            contract=(
                '{"beat_ids":["C01E001"],'
                '"support_evidence_ids":["C01E002"]}'
            ),
        )
    )

    (
        candidate,
        _origin,
        _diagnostics,
    ) = (
        _studio_v2373_extract_classification_plan(
            raw=raw,
            parsed=parsed,
            anchors=batch_anchors,
        )
    )
    plan = candidate if isinstance(candidate, dict) else {}
"""
    text = replace_once(
        text,
        old_classification,
        new_classification,
        "P0 single primary classification",
    )

    partial_anchor = """\n\n\nasync def _studio_v2374_classify_batch("""
    partial_helper = """\n\n\ndef _studio_v23963_partial_classification_plan(
    *,
    raw: object,
    anchors: list[dict],
) -> dict:
    \"\"\"Recover only explicitly emitted requested IDs from truncated JSON.\"\"\"
    expected = [
        str(row.get("id") or "")
        for row in _studio_v2374_ordered_anchors(anchors)
        if str(row.get("id") or "")
    ]
    best = {"beat_ids": [], "support_evidence_ids": []}
    texts = sorted(
        _studio_v2372d_collect_texts(raw),
        key=len,
        reverse=True,
    )

    def field_segment(text: str, field: str, other: str) -> str:
        starts = [
            index
            for marker in (f'"{field}"', f"'{field}'", field)
            for index in [text.find(marker)]
            if index >= 0
        ]
        if not starts:
            return ""
        bracket = text.find("[", min(starts))
        if bracket < 0:
            return ""
        tail = text[bracket + 1:]
        boundaries = []
        closing = tail.find("]")
        if closing >= 0:
            boundaries.append(closing)
        for marker in (f'"{other}"', f"'{other}'", other):
            other_index = tail.find(marker)
            if other_index >= 0:
                boundaries.append(other_index)
        return tail[:min(boundaries)] if boundaries else tail

    for text in texts:
        value = str(text)
        candidate = {"beat_ids": [], "support_evidence_ids": []}
        for field, other in (
            ("beat_ids", "support_evidence_ids"),
            ("support_evidence_ids", "beat_ids"),
        ):
            segment = field_segment(value, field, other)
            candidate[field] = [
                evidence_id
                for evidence_id in expected
                if (
                    f'"{evidence_id}"' in segment
                    or f"'{evidence_id}'" in segment
                )
            ]
        if (
            len(candidate["beat_ids"])
            + len(candidate["support_evidence_ids"])
            > len(best["beat_ids"])
            + len(best["support_evidence_ids"])
        ):
            best = candidate
    return best


async def _studio_v2374_classify_batch("""
    text = replace_once(
        text,
        partial_anchor,
        partial_helper,
        "P0 truncated classification partial parser",
    )

    text = replace_once(
        text,
        """    plan = candidate if isinstance(candidate, dict) else {}

    (
        beat_ids,""",
        """    plan = candidate if isinstance(candidate, dict) else {}
    partial = _studio_v23963_partial_classification_plan(
        raw=raw,
        anchors=batch_anchors,
    )
    for field in ("beat_ids", "support_evidence_ids"):
        retained = list(plan.get(field) or [])
        for evidence_id in partial.get(field) or []:
            if evidence_id not in retained:
                retained.append(evidence_id)
        plan[field] = retained

    (
        beat_ids,""",
        "P0 merge parsed and truncated partial classifications",
    )

    text = replace_once(
        text,
        """    # V2.39.10_STAGE04_SCENE_BATCHED_NARRATIVE
    _studio_v2372_scene_range_guard(""",
        """    # Shared per-Scene MembershipRepair budget. Stage04 Scene work is
    # serialized by the existing rebuild/workspace guards.
    globals()["_studio_v23963_membership_repair_calls"] = 0

    # V2.39.10_STAGE04_SCENE_BATCHED_NARRATIVE
    _studio_v2372_scene_range_guard(""",
        "P1 reset shared membership budget per Scene",
    )

    text = replace_once(
        text,
        """    # Classification 只输出 ID partition，适合大 batch。
    # Grouping JSON 较重，因此控制在 20 anchors。""",
        """    # Carry only deterministic Scene identity as chunk metadata.
    # Existing classification/grouping prompts read chunk text explicitly and
    # are therefore byte-for-byte unaffected by this metadata.
    for selected_chunk in chunks:
        selected_chunk["scene_id"] = str(scene.get("scene_id") or "")

    # Classification 只输出 ID partition，适合大 batch。
    # Grouping JSON 较重，因此控制在 20 anchors。""",
        "schema completion Scene identity metadata",
    )

    text = replace_once(
        text,
        """    total_qwen_calls = 0
    repaired_total = 0
    repair_groups = _studio_v2374_chunks(
        ordered_requested,
        8,
    )""",
        """    total_qwen_calls = 0
    repaired_total = 0
    repair_call_budget = 5
    repair_budget_key = "_studio_v23963_membership_repair_calls"

    def consume_repair_call_budget() -> None:
        used = int(globals().get(repair_budget_key) or 0)
        if used >= repair_call_budget:
            raise RuntimeError(
                "V2.39.6.3: Scene MembershipRepair call budget exceeded; "
                f"budget={repair_call_budget}"
            )
        globals()[repair_budget_key] = used + 1

    # V2.39.6.3_PERF_MEMBERSHIP_LINE_ONLY: at most five groups/calls.
    repair_group_size = max(
        8,
        (len(ordered_requested) + repair_call_budget - 1)
        // repair_call_budget,
    )
    repair_groups = _studio_v2374_chunks(
        ordered_requested,
        repair_group_size,
    )""",
        "P1 membership repair grouping budget",
    )

    text = replace_once(
        text,
        """        round_modes = (
            ("json", 450),
            ("strict", 450),
            ("line", 360),
        )""",
        """        round_modes = (
            ("line", 360),
        )""",
        "P1 line-only membership group repair",
    )

    text = replace_once(
        text,
        """            diagnostics = []
            total_qwen_calls += 1

            try:
                candidate_rows, raw_preview = await ask_membership(""",
        """            diagnostics = []
            consume_repair_call_budget()
            total_qwen_calls += 1

            try:
                candidate_rows, raw_preview = await ask_membership(""",
        "P1 group call budget guard",
    )

    text = replace_once(
        text,
        'for singleton_attempt, mode in enumerate(("json", "strict"), 1):\n                total_qwen_calls += 1',
        'for singleton_attempt, mode in enumerate(("line",), 1):\n                if int(globals().get(repair_budget_key) or 0) >= repair_call_budget:\n                    break\n                consume_repair_call_budget()\n                total_qwen_calls += 1',
        "P1 line-only singleton fallback",
    )

    audit_helper_anchor = """    for index, row in enumerate(rows, 1):
        row["index"] = index
    return rows


async def _studio_v2372b_complete_audit_schema("""
    audit_helpers = """    for index, row in enumerate(rows, 1):
        row["index"] = index
    return rows


_STUDIO_V23963_AUDIT_PROMPT_TOKEN_LIMIT = 6500


def _studio_v23963_compact_audit_anchors(
    anchors: list[dict],
    support_ids: list[str],
) -> list[dict]:
    support = {str(value or "") for value in support_ids or []}
    rows = []
    for anchor in anchors or []:
        if not isinstance(anchor, dict):
            continue
        evidence_id = str(anchor.get("id") or "").strip()
        if not evidence_id:
            continue
        start = anchor.get("source_start")
        if start is None:
            start = anchor.get("start")
        end = anchor.get("source_end")
        if end is None:
            end = anchor.get("end")
        rows.append({
            "id": evidence_id,
            "start": int(start or 0),
            "end": int(end or 0),
            "classification": (
                "support" if evidence_id in support else "beat_evidence"
            ),
        })
    rows.sort(key=lambda row: (row["start"], row["end"], row["id"]))
    return rows


def _studio_v23963_compact_audit_beats(
    beats: list[dict],
) -> list[dict]:
    rows = []
    for row in _studio_v23962_audit_beats(beats):
        # Source text appears once in CORE_SOURCE_CHUNK. Evidence IDs/spans keep
        # exact binding and order, so repeated source_evidence text is transport
        # duplication rather than semantic authority.
        rows.append({
            "index": int(row.get("index") or 0),
            "state": row.get("summary"),
            "state_change": row.get("state_change"),
            "source_evidence_ids": list(
                row.get("source_evidence_ids") or []
            ),
            "source_evidence_spans": list(
                row.get("source_evidence_spans") or []
            ),
        })
    return rows


def _studio_v23963_render_audit_prompt(
    *,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    compact: bool,
    prior_audit: object = None,
    prior_missing: list[str] | None = None,
) -> str:
    if compact:
        anchor_label = "SOURCE_ANCHOR_BINDINGS_COMPACT"
        anchor_payload = _studio_v23963_compact_audit_anchors(
            anchors,
            support_ids,
        )
        beat_label = "PROPOSED_BEATS_COMPACT"
        beat_payload = _studio_v23963_compact_audit_beats(beats)
    else:
        anchor_label = "SOURCE_ANCHORS"
        anchor_payload = anchors
        beat_label = "PROPOSED_BEATS"
        beat_payload = _studio_v23962_audit_beats(beats)

    support_json = (
        _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if compact
        else _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
    )

    prompt = (
        "=== CORE_SOURCE_CHUNK ===\\n"
        + str(chunk.get("text") or "")
        + "\\n\\n=== " + anchor_label + " ===\\n"
        + _studio_json.dumps(
            anchor_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\\n\\n=== " + beat_label + " ===\\n"
        + _studio_json.dumps(
            beat_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\\n\\n=== SUPPORT_EVIDENCE_IDS ===\\n"
        + support_json
    )
    if prior_audit is not None or prior_missing is not None:
        prior_json = (
            _studio_json.dumps(
                prior_audit if isinstance(prior_audit, dict) else {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if compact
            else _studio_json.dumps(
                prior_audit if isinstance(prior_audit, dict) else {},
                ensure_ascii=False,
            )
        )
        missing_json = (
            _studio_json.dumps(
                prior_missing or [],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if compact
            else _studio_json.dumps(
                prior_missing or [],
                ensure_ascii=False,
            )
        )
        prompt += (
            "\\n\\n=== PRIOR_AUDIT_ONLY_FOR_SCHEMA_DIAGNOSTIC ===\\n"
            + prior_json
            + "\\nMISSING_FIELDS="
            + missing_json
        )
    return prompt


async def _studio_v23963_prepare_audit_prompt(
    *,
    phase: str,
    system_prompt: str,
    chunk: dict,
    anchors: list[dict],
    beats: list[dict],
    support_ids: list[str],
    prior_audit: object = None,
    prior_missing: list[str] | None = None,
) -> str:
    counter = getattr(director, "_count_prompt_tokens", None)
    if not callable(counter):
        raise RuntimeError(
            f"{phase}: active llama.cpp tokenizer is required for audit budget"
        )

    async def measured(prompt: str) -> tuple[int, str]:
        return await counter(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )

    full_prompt = _studio_v23963_render_audit_prompt(
        chunk=chunk,
        anchors=anchors,
        beats=beats,
        support_ids=support_ids,
        compact=False,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )
    full_tokens, estimator = await measured(full_prompt)
    if estimator != "llama_tokenize":
        raise RuntimeError(
            f"{phase}: real llama.cpp token budget unavailable; estimator={estimator}"
        )
    if full_tokens <= _STUDIO_V23963_AUDIT_PROMPT_TOKEN_LIMIT:
        print(
            "[V2.39.6.3][Stage04][AuditBudget] "
            f"phase={phase} tokens={full_tokens} compacted=false",
            flush=True,
        )
        return full_prompt

    compact_prompt = _studio_v23963_render_audit_prompt(
        chunk=chunk,
        anchors=anchors,
        beats=beats,
        support_ids=support_ids,
        compact=True,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )
    compact_tokens, compact_estimator = await measured(compact_prompt)
    if compact_estimator != "llama_tokenize":
        raise RuntimeError(
            f"{phase}: real llama.cpp compact token budget unavailable; "
            f"estimator={compact_estimator}"
        )
    if compact_tokens > _STUDIO_V23963_AUDIT_PROMPT_TOKEN_LIMIT:
        raise RuntimeError(
            f"{phase}: compact audit payload still exceeds token budget; "
            f"full_tokens={full_tokens} compact_tokens={compact_tokens} "
            f"limit={_STUDIO_V23963_AUDIT_PROMPT_TOKEN_LIMIT}"
        )
    print(
        "[V2.39.6.3][Stage04][AuditBudget] "
        f"phase={phase} full_tokens={full_tokens} "
        f"compact_tokens={compact_tokens} compacted=true",
        flush=True,
    )
    return compact_prompt


_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT = 6000


def _studio_v23963_schema_completion_payload(
    *,
    chunk: dict,
    beats: list[dict],
    support_ids: list[str],
    prior_audit: object,
    prior_missing: list[str],
) -> dict:
    scene_id = str(chunk.get("scene_id") or "").strip()
    if not scene_id:
        scene_id = "scene-chunk-" + str(chunk.get("index") or "unknown")
    audit_id = scene_id + ":narrative-beat:" + str(
        chunk.get("index") or "unknown"
    )

    evidence_ids = []
    for value in support_ids or []:
        key = str(value or "").strip()
        if key and key not in evidence_ids:
            evidence_ids.append(key)

    beat_binding = []
    temporal_fields = []
    for index, beat in enumerate(beats or [], 1):
        if not isinstance(beat, dict):
            continue
        ids = []
        for value in beat.get("source_evidence_ids") or []:
            key = str(value or "").strip()
            if key and key not in ids:
                ids.append(key)
            if key and key not in evidence_ids:
                evidence_ids.append(key)
        beat_binding.append({
            "beat_index": index,
            "evidence_ids": ids,
        })
        starts = []
        ends = []
        for span in beat.get("source_evidence_spans") or []:
            if not isinstance(span, dict):
                continue
            try:
                starts.append(int(span.get("start") or 0))
                ends.append(int(span.get("end") or 0))
            except Exception:
                continue
        temporal_fields.append({
            "beat_index": index,
            "source_start": min(starts) if starts else None,
            "source_end": max(ends) if ends else None,
        })

    missing_fields = []
    for value in prior_missing or []:
        key = str(value or "").strip()
        if key and key not in missing_fields:
            missing_fields.append(key)

    return {
        "scene_id": scene_id,
        "audit_id": audit_id,
        "missing_fields": missing_fields,
        "previous_audit_result": {
            "audit": dict(prior_audit) if isinstance(prior_audit, dict) else {},
            "evidence_ids": evidence_ids,
            "beat_binding": beat_binding,
            "temporal_fields": temporal_fields,
        },
        "required_schema": {
            "type": "object",
            "required": [
                "valid",
                "event_coverage_ok",
                "granularity_ok",
                "evidence_entailment_ok",
                "temporal_order_ok",
                "support_classification_ok",
                "violations",
            ],
            "boolean_fields": [
                "valid",
                "event_coverage_ok",
                "granularity_ok",
                "evidence_entailment_ok",
                "temporal_order_ok",
                "support_classification_ok",
            ],
            "violations_type": "array[string]",
            "invariants": [
                "preserve every already-present audit conclusion",
                "valid equals all five *_ok fields and empty violations",
                "do not alter evidence_ids, beat_binding, or temporal_fields",
            ],
        },
    }


async def _studio_v23963_prepare_schema_completion_prompt(
    *,
    phase: str,
    system_prompt: str,
    chunk: dict,
    beats: list[dict],
    support_ids: list[str],
    prior_audit: object,
    prior_missing: list[str],
) -> str:
    payload = _studio_v23963_schema_completion_payload(
        chunk=chunk,
        beats=beats,
        support_ids=support_ids,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )
    allowed = {
        "scene_id",
        "audit_id",
        "missing_fields",
        "previous_audit_result",
        "required_schema",
    }
    if set(payload) != allowed:
        raise RuntimeError(
            f"{phase}: schema completion payload fields mismatch; "
            f"fields={sorted(payload)}"
        )

    forbidden = {"source_text", "full_anchors", "full_beats"}

    def assert_no_forbidden(value: object) -> None:
        if isinstance(value, dict):
            overlap = forbidden.intersection(str(key) for key in value)
            if overlap:
                raise RuntimeError(
                    f"{phase}: forbidden schema completion fields={sorted(overlap)}"
                )
            for nested in value.values():
                assert_no_forbidden(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_forbidden(nested)

    assert_no_forbidden(payload)
    prompt = _studio_json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for marker in (
        "=== CORE_SOURCE_CHUNK ===",
        "=== SOURCE_ANCHORS ===",
        "=== PROPOSED_BEATS ===",
    ):
        if marker in prompt:
            raise RuntimeError(
                f"{phase}: forbidden full audit section in schema completion"
            )

    counter = getattr(director, "_count_prompt_tokens", None)
    if not callable(counter):
        raise RuntimeError(
            f"{phase}: active llama.cpp tokenizer is required for schema budget"
        )
    tokens, estimator = await counter(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    if estimator != "llama_tokenize":
        raise RuntimeError(
            f"{phase}: real llama.cpp schema token budget unavailable; "
            f"estimator={estimator}"
        )
    if tokens > _STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT:
        raise RuntimeError(
            f"{phase}: minimal schema completion payload exceeds token budget; "
            f"tokens={tokens} "
            f"limit={_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT}"
        )
    print(
        "[V2.39.6.3][Stage04][SchemaCompletionBudget] "
        f"phase={phase} tokens={tokens} "
        f"limit={_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT}",
        flush=True,
    )
    return prompt


async def _studio_v2372b_complete_audit_schema("""
    text = replace_once(
        text,
        audit_helper_anchor,
        audit_helpers,
        "P2 audit compaction helpers",
    )

    old_schema_prompt = """    prompt = (
        "=== CORE_SOURCE_CHUNK ===\\n"
        + str(chunk.get("text") or "")
        + "\\n\\n=== SOURCE_ANCHORS ===\\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\\n\\n=== PROPOSED_BEATS ===\\n"
        + _studio_json.dumps(
            audit_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\\n\\n=== SUPPORT_EVIDENCE_IDS ===\\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
        + "\\n\\n=== PRIOR_AUDIT_ONLY_FOR_SCHEMA_DIAGNOSTIC ===\\n"
        + _studio_json.dumps(
            prior_audit
            if isinstance(prior_audit, dict)
            else {},
            ensure_ascii=False,
        )
        + "\\nMISSING_FIELDS="
        + _studio_json.dumps(
            prior_missing,
            ensure_ascii=False,
        )
    )"""
    new_schema_prompt = """    prompt = await _studio_v23963_prepare_schema_completion_prompt(
        phase=(
            "studio_stage04_"
            "narrative_beat_audit_schema_completion_qwen32b"
        ),
        system_prompt=system_prompt,
        chunk=chunk,
        beats=beats,
        support_ids=support_ids,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )"""
    text = replace_once(
        text,
        old_schema_prompt,
        new_schema_prompt,
        "P2 compact schema-completion prompt",
    )

    old_schema_system = """    system_prompt = (
        "你是 Narrative Beat 审计结果结构补全器。"
        "你仍然必须独立审计正文和 Beat，不能沿用 prior_audit 的结论。"
        "分类只能基于当前 Scene 的最小有序叙事状态图和证据依赖，"
        "不得使用固定关键词、文本类别、题材类型或预设业务词表。"
        "必须逐项输出以下五个 boolean："
        "event_coverage_ok、granularity_ok、evidence_entailment_ok、"
        "temporal_order_ok、support_classification_ok。"
        "如果任何一项为 false，violations 必须至少写出一条具体原因；"
        "如果全部为 true，violations 必须为空数组。"
        "valid 必须等于上述五项全部为 true 且 violations 为空。"
        "禁止省略字段，禁止只返回 valid。只输出严格 JSON。"
    )"""
    new_schema_system = """    system_prompt = (
        "你是 Narrative Beat 审计结果 Schema 补全器，不重新执行语义审计。"
        "只根据 previous_audit_result 补齐 missing_fields，"
        "必须保留所有已经存在的 audit 结论。"
        "不得修改 evidence_ids、beat_binding 或 temporal_fields。"
        "输出必须满足 required_schema：显式返回 valid、五个 *_ok boolean "
        "以及 violations。valid 必须等于五个 *_ok 全部为 true 且 "
        "violations 为空。禁止引入正文、anchor 或 Beat 新事实。"
        "只输出补全后的严格 JSON audit 对象。"
    )"""
    text = replace_once(
        text,
        old_schema_system,
        new_schema_system,
        "minimal schema-completion system contract",
    )

    schema_start = text.index(
        "async def _studio_v2372b_complete_audit_schema("
    )
    schema_end = text.index(
        "async def _studio_v2372_audit_extraction(",
        schema_start,
    )
    schema_block = text[schema_start:schema_end]
    schema_block = replace_once(
        schema_block,
        """                messages=[{
                    "role": "user",
                    "content": prompt + (
                        ""
                        if attempt == 0
                        else (
                            "\\n\\nSTRICT_SCHEMA_RETRY："
                            "六个顶层字段 valid + 五个 *_ok "
                            "以及 violations 必须全部显式返回；"
                            "不得输出 reasons 代替这些字段。"
                        )
                    ),
                }],""",
        """                messages=[{
                    "role": "user",
                    "content": prompt,
                }],""",
        "schema completion JSON-only request body",
    )
    schema_block = replace_once(
        schema_block,
        """        decision, violations, missing = (
            _studio_v2372b_audit_violations(
                audit,
                required=required,
            )
        )""",
        """        audit = dict(audit) if isinstance(audit, dict) else {}
        if isinstance(prior_audit, dict):
            # Schema completion may fill absent fields but cannot rewrite any
            # conclusion already returned by the primary semantic audit.
            for field in ("valid", *required, "violations"):
                if field in prior_audit:
                    audit[field] = _studio_v2372_copy.deepcopy(
                        prior_audit[field]
                    )

        decision, violations, missing = (
            _studio_v2372b_audit_violations(
                audit,
                required=required,
            )
        )""",
        "schema completion preserves primary audit conclusions",
    )
    text = text[:schema_start] + schema_block + text[schema_end:]

    old_audit_prompt = """    prompt = (
        "=== CORE_SOURCE_CHUNK ===\\n"
        + str(chunk.get("text") or "")
        + "\\n\\n=== SOURCE_ANCHORS ===\\n"
        + _studio_json.dumps(
            anchors,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\\n\\n=== PROPOSED_BEATS ===\\n"
        + _studio_json.dumps(
            audit_beats,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\\n\\n=== SUPPORT_EVIDENCE_IDS ===\\n"
        + _studio_json.dumps(
            support_ids,
            ensure_ascii=False,
        )
    )"""
    new_audit_prompt = """    prompt = await _studio_v23963_prepare_audit_prompt(
        phase="studio_stage04_narrative_beat_audit_qwen32b",
        system_prompt=system_prompt,
        chunk=chunk,
        anchors=anchors,
        beats=beats,
        support_ids=support_ids,
    )"""
    # Earlier DEAD audit definitions contain the same transport block. Patch
    # only the final active definition selected by Python's name binding.
    text = replace_last(
        text,
        old_audit_prompt,
        new_audit_prompt,
        "P2 compact primary audit prompt",
    )

    return text.encode("utf-8")


def build_gemma(source: bytes) -> bytes:
    text = source.decode("utf-8")
    class_anchor = """def _specified_block(value: Any) -> dict[str, Any]:"""
    class_code = """class LLMContextOverflowError(RuntimeError):
    \"\"\"llama.cpp rejected a request because its rendered prompt cannot fit.\"\"\"


def _is_context_overflow_response(response: httpx.Response) -> bool:
    text = _text(response.text).casefold()
    code = ""
    message = ""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = _text(error.get("code")).casefold()
            message = _text(error.get("message")).casefold()
    except Exception:
        pass
    combined = " ".join((text, code, message))
    return any(marker in combined for marker in (
        "exceed_context_size_error",
        "exceeds the available context size",
        "context window exceeded",
        "maximum context length",
    ))


def _specified_block(value: Any) -> dict[str, Any]:"""
    text = replace_once(
        text,
        class_anchor,
        class_code,
        "P3 context overflow exception",
    )

    old_error_tail = """                    logger.warning(
                        "Qwen request rejected; model=%s message_count=%d "
                        "roles=%s content_lengths=%s response_body=%s",
                        model,
                        len(normalized_messages),
                        [message["role"] for message in normalized_messages],
                        [len(message["content"]) for message in normalized_messages],
                        exc.response.text,
                    )
                if attempt == 0:
                    await asyncio.sleep(1)"""
    new_error_tail = """                    logger.warning(
                        "Qwen request rejected; model=%s message_count=%d "
                        "roles=%s content_lengths=%s response_body=%s",
                        model,
                        len(normalized_messages),
                        [message["role"] for message in normalized_messages],
                        [len(message["content"]) for message in normalized_messages],
                        exc.response.text,
                    )
                    if _is_context_overflow_response(exc.response):
                        overflow = LLMContextOverflowError(
                            "Qwen context exceeded; caller must compact before retry: "
                            + exc.response.text[:1000]
                        )
                        overflow.llm_metrics = {
                            "usage": {},
                            "timings": {},
                            "request_attempts": attempt + 1,
                            "request_retries": attempt,
                        }
                        raise overflow from exc
                if attempt == 0:
                    await asyncio.sleep(1)"""
    text = replace_once(
        text,
        old_error_tail,
        new_error_tail,
        "P3 no identical context retry",
    )
    return text.encode("utf-8")


BUILDERS: dict[str, Callable[[bytes], bytes]] = {
    "app/main.py": build_main,
    "app/services/gemma.py": build_gemma,
}


def build_target(rel: str, baseline: bytes) -> bytes:
    require(sha(baseline) == FILES[rel]["baseline_sha256"], f"baseline SHA256 mismatch: {rel}")
    target = BUILDERS[rel](baseline)
    require(sha(target) == FILES[rel]["target_sha256"], f"target SHA256 construction mismatch: {rel}")
    return target


def root_valid(root: Path) -> bool:
    return root.is_dir() and all((root / rel).is_file() for rel in REQUIRED_ROOT_FILES)


def discover_platform_root(manual: Path | None = None) -> Path:
    checked = []
    candidates = ((manual,) if manual is not None else ROOT_CANDIDATES)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        checked.append(str(resolved))
        if root_valid(resolved):
            return resolved
    raise RuntimeError("platform root candidates checked:\n" + "\n".join(checked) + "\nnot found")


def _python_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            text=True,
        )
        return completed.returncode == 0
    except Exception:
        return False


def discover_platform_python(manual: Path | None = None) -> Path:
    candidates = [manual] if manual is not None else [*PYTHON_CANDIDATES, Path(sys.executable)]
    checked = []
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.expanduser().resolve()
        checked.append(str(resolved))
        if _python_usable(resolved):
            return resolved
    raise RuntimeError("platform Python candidates checked:\n" + "\n".join(checked) + "\nnot found")


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    print(completed.stdout, end="")
    require(completed.returncode == 0, f"command failed ({completed.returncode}): {command}")
    return completed


def request_json(path: str, timeout: int = 20) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(BASE_URL + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"body": body}
        return exc.code, payload


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def walk_status_rows(value: object):
    if isinstance(value, dict):
        if "status" in value:
            yield value
        for nested in value.values():
            yield from walk_status_rows(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_status_rows(nested)


def check_active_tasks(root: Path) -> None:
    for endpoint in ("/api/studio/stage04/rebuild/tasks", "/api/tasks"):
        try:
            status, payload = request_json(endpoint, 15)
        except Exception:
            continue
        if status != 200:
            continue
        for row in walk_status_rows(payload):
            require(str(row.get("status") or "").lower() not in ACTIVE, f"active task reported by {endpoint}")

    data_dir = root / "data"
    for pattern in ("stage04_rebuild_tasks/*.json", "tasks/*/task.json", "studio_jobs/*.json"):
        for path in data_dir.glob(pattern):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"cannot inspect task state {path}: {exc}") from exc
            for row in walk_status_rows(payload):
                require(str(row.get("status") or "").lower() not in ACTIVE, f"active task in {path}")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    temp = path.with_name(path.name + ".v23963-perf.tmp")
    temp.write_bytes(data)
    os.chmod(temp, mode)
    temp.replace(path)


def backup_live(root: Path, backup: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    backup.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "installer_version": INSTALLER_VERSION,
        "baseline_version": BASELINE_VERSION,
        "target_version": TARGET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {},
    }
    targets: dict[str, bytes] = {}
    for rel, spec in FILES.items():
        source = root / rel
        require(source.is_file(), f"baseline file missing: {rel}")
        data = source.read_bytes()
        target = build_target(rel, data)
        mode = os.stat(source).st_mode & 0o777
        destination = backup / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        os.chmod(destination, mode)
        manifest["files"][rel] = {
            "before_sha256": sha(data),
            "target_sha256": sha(target),
            "mode": mode,
        }
        targets[rel] = target
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest, targets


def restore_exact_backup(root: Path, backup: Path, manifest: dict[str, Any]) -> None:
    for rel, item in manifest["files"].items():
        data = (backup / rel).read_bytes()
        require(sha(data) == item["before_sha256"], f"backup corrupted: {rel}")
        atomic_write(root / rel, data, int(item["mode"]))
        require(sha((root / rel).read_bytes()) == item["before_sha256"], f"rollback hash mismatch: {rel}")


def validate_openapi(expected: str) -> None:
    status, schema = request_json("/openapi.json", 30)
    require(status == 200, f"OpenAPI HTTP status mismatch: {status}")
    require(schema.get("info", {}).get("version") == expected, "OpenAPI version mismatch")


def stop_platform(root: Path) -> None:
    run(["bash", str(root / "scripts/stop.sh")], 60)
    deadline = time.monotonic() + 20
    while port_open(6008) and time.monotonic() < deadline:
        time.sleep(1)
    require(not port_open(6008), "port 6008 still listening after stop")


def start_and_verify(root: Path, expected_version: str) -> None:
    run(["bash", str(root / "scripts/start.sh")], 120)
    deadline = time.monotonic() + 120
    last = ""
    while time.monotonic() < deadline:
        try:
            status, health = request_json("/api/health", 30)
            if status == 200:
                require(health.get("version") == expected_version, "health runtime version mismatch")
                validate_openapi(expected_version)
                return
            last = f"HTTP {status}: {health}"
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"platform health timeout: {last}")


def validate_target_source(rel: str, data: bytes) -> None:
    text = data.decode("utf-8")
    tree = ast.parse(text, filename=rel)
    compile(tree, rel, "exec")
    if rel == "app/main.py":
        for marker in (
            "V2.39.6.3_PERF_PARTIAL_CLASSIFICATION",
            "_studio_v23963_partial_classification_plan",
            "V2.39.6.3_PERF_MEMBERSHIP_LINE_ONLY",
            'globals()["_studio_v23963_membership_repair_calls"] = 0',
            "_STUDIO_V23963_AUDIT_PROMPT_TOKEN_LIMIT = 6500",
            "_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT = 6000",
            "_studio_v23963_prepare_schema_completion_prompt",
            'forbidden = {"source_text", "full_anchors", "full_beats"}',
            "real llama.cpp token budget unavailable",
            'round_modes = (\n            ("line", 360),',
        ):
            require(marker in text, f"main target marker missing: {marker}")
        classify_start = text.index("async def _studio_v2374_classify_batch(")
        classify_end = text.index("async def _studio_v2374_classify_all(", classify_start)
        classify_body = text[classify_start:classify_end]
        require("for attempt in range(2)" not in classify_body, "whole-batch classification retry still present")
        require("requested_ids=unresolved" in classify_body, "classification repair is not unresolved-only")
        membership_start = text.index("async def _studio_v2374_resolve_group_membership(")
        membership_end = text.index("async def _studio_v2374_group_batch(", membership_start)
        membership_body = text[membership_start:membership_end]
        require('(\"json\", 450)' not in membership_body, "membership JSON round still present")
        require('(\"strict\", 450)' not in membership_body, "membership strict round still present")
        require("repair_call_budget = 5" in membership_body, "membership repair budget missing")
        require("consume_repair_call_budget()" in membership_body, "shared membership repair budget is not consumed")
        schema_start = text.index("async def _studio_v2372b_complete_audit_schema(")
        schema_end = text.index("async def _studio_v2372_audit_extraction(", schema_start)
        schema_body = text[schema_start:schema_end]
        require("_studio_v23963_prepare_schema_completion_prompt" in schema_body, "minimal schema completion helper is not active")
        require('"content": prompt +' not in schema_body, "schema completion appends non-JSON context")
        for forbidden_section in (
            "=== CORE_SOURCE_CHUNK ===",
            "=== SOURCE_ANCHORS ===",
            "=== PROPOSED_BEATS ===",
        ):
            require(forbidden_section not in schema_body, f"schema completion carries full section: {forbidden_section}")
    else:
        require("class LLMContextOverflowError" in text, "context overflow exception missing")
        require("if _is_context_overflow_response(exc.response):" in text, "context overflow detector missing")
        require('"request_retries": attempt' in text, "overflow metrics missing")


def self_test(source_root: Path, python: Path) -> int:
    require(INSTALLER_VERSION == "V2.39.6.3-stage04-performance-optimization-installer", "installer marker mismatch")
    require(BASELINE_VERSION == TARGET_VERSION, "this patch must retain the V2.39.6.3 runtime version")
    targets: dict[str, bytes] = {}
    for rel in FILES:
        baseline = (source_root / rel).read_bytes()
        targets[rel] = build_target(rel, baseline)
        validate_target_source(rel, targets[rel])

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        root = temp / "platform-v2"
        backup = temp / "backup"
        manifest = {"files": {}}
        compiled = []
        for rel, target in targets.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            baseline = (source_root / rel).read_bytes()
            path.write_bytes(baseline)
            destination = backup / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(baseline)
            manifest["files"][rel] = {"before_sha256": sha(baseline), "mode": 0o644}
            atomic_write(path, target, 0o644)
            require(sha(path.read_bytes()) == FILES[rel]["target_sha256"], f"target readback failed: {rel}")
            compiled.append(str(path))
        run([str(python), "-m", "py_compile", *compiled], 120)
        restore_exact_backup(root, backup, manifest)
        for rel in FILES:
            require(sha((root / rel).read_bytes()) == FILES[rel]["baseline_sha256"], f"rollback self-test failed: {rel}")

    print("INSTALLER SELF-TEST PASS")
    print("P0 PARTIAL CLASSIFICATION STATIC CONTRACT PASS")
    print("P1 MEMBERSHIP LINE-ONLY BUDGET CONTRACT PASS")
    print("P2 REAL TOKEN GUARD/COMPACTION CONTRACT PASS")
    print("P3 CONTEXT OVERFLOW NO-RETRY CONTRACT PASS")
    print("ROLLBACK SIMULATION PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, help="platform root override")
    parser.add_argument("--python", type=Path, help="platform Python override")
    parser.add_argument("--backup-root", type=Path, default=Path("/root/autodl-tmp/ai-studio/backups"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--source-root", type=Path, help="baseline source root for local self-test")
    parser.add_argument("--print-target-hashes", action="store_true")
    args = parser.parse_args()

    if args.print_target_hashes:
        source_root = (args.source_root or Path(__file__).resolve().parent.parent).resolve()
        for rel in FILES:
            baseline = (source_root / rel).read_bytes()
            require(sha(baseline) == FILES[rel]["baseline_sha256"], f"baseline SHA256 mismatch: {rel}")
            print(f"{rel} {sha(BUILDERS[rel](baseline))}")
        return 0

    if args.self_test:
        source_root = (args.source_root or Path(__file__).resolve().parent.parent).resolve()
        platform_python = discover_platform_python(args.python)
        return self_test(source_root, platform_python)

    root = discover_platform_root(args.root)
    platform_python = discover_platform_python(args.python)
    print(f"INSTALLER_VERSION={INSTALLER_VERSION}")
    print(f"PLATFORM_ROOT={root}")
    print(f"PLATFORM_PYTHON={platform_python}")
    validate_openapi(BASELINE_VERSION)
    check_active_tasks(root)

    backup = args.backup_root / (
        "platform-v2-v23963-perf-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    manifest, targets = backup_live(root, backup)
    applied = False
    platform_stopped = False
    try:
        stop_platform(root)
        platform_stopped = True
        check_active_tasks(root)
        applied = True
        for rel, target in targets.items():
            atomic_write(root / rel, target, int(manifest["files"][rel]["mode"]))
            validate_target_source(rel, target)
        run([str(platform_python), "-m", "py_compile", *[str(root / rel) for rel in FILES]], 240)
        start_and_verify(root, TARGET_VERSION)
        for rel, spec in FILES.items():
            require(sha((root / rel).read_bytes()) == spec["target_sha256"], f"target hash readback mismatch: {rel}")
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["result"] = "INSTALLED"
        (backup / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"BACKUP={backup}")
        print("INSTALL PASS; no Stage04/image/video E2E was executed")
        print("LLAMA.CPP RECOMMENDATION ONLY: validate VRAM, then use --ctx-size 16384")
        return 0
    except Exception:
        if applied:
            try:
                if port_open(6008):
                    stop_platform(root)
            except Exception as exc:
                print(f"ROLLBACK STOP WARNING: {exc}", file=sys.stderr)
            restore_exact_backup(root, backup, manifest)
        if platform_stopped:
            try:
                start_and_verify(root, BASELINE_VERSION)
            except Exception as exc:
                print(f"ROLLBACK RESTORED FILES BUT RESTART FAILED: {exc}", file=sys.stderr)
        if applied:
            print(f"ROLLBACK COMPLETE FROM EXACT LIVE BACKUP {backup}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INSTALL FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
