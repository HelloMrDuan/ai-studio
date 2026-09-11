from __future__ import annotations

import re
import sys
from typing import Any

from . import front_half_quality_gate as gate
from . import professional_output_registry as registry


_SECTION_MARKER = re.compile(r"(?m)^===\s*(.+?)\s*===\s*$")
_AUTHORITATIVE_SECTIONS = {
    # Current Director full-pack contract.
    "AUTHORITATIVE PREVIOUS CONFIRMED STAGE TEXT",
    "AUTHORITATIVE CURRENT USER MESSAGE",
    # Soft/compact execution packs and historical compatibility.
    "CONFIRMED UPSTREAM FACTS",
    "ORIGINAL PROJECT REQUEST",
    "ORIGINAL PROJECT REQUEST (BOUNDED)",
    "CURRENT USER MESSAGE",
    "CURRENT USER INPUT",
}
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _compact(value: Any) -> str:
    return re.sub(
        r"[\s，。；：、“”‘’！？,.!?;:'\"（）()【】\[\]]+",
        "",
        str(value or ""),
    ).casefold()


def authoritative_message_source(messages: list[dict[str, Any]]) -> str:
    """Read only Director sections explicitly declared authoritative.

    The Director owns the prompt envelope. This reader follows its real section
    names instead of guessing from prose. Generated history, routing metadata,
    Skill text and runtime state are never accepted as source evidence.
    """
    rows: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = str(message.get("content") or "")
        matches = list(_SECTION_MARKER.finditer(text))
        for index, match in enumerate(matches):
            label = _clean(match.group(1)).upper()
            if label not in _AUTHORITATIVE_SECTIONS:
                continue
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            value = _clean(text[start:end])
            if not value or value in {"<none>", "<omitted>"} or value in seen:
                continue
            seen.add(value)
            rows.append(value)
    return "\n\n".join(rows)


def _exact_sentence_for_name(source_text: str, name: str) -> str:
    """Return an exact contiguous source span containing the canonical name."""
    source = str(source_text or "")
    wanted = _clean(name)
    if not source or not wanted:
        return ""
    pos = source.find(wanted)
    if pos < 0:
        return ""

    left = 0
    for match in _SENTENCE_BOUNDARY.finditer(source, 0, pos):
        left = match.end()

    right_match = _SENTENCE_BOUNDARY.search(source, pos + len(wanted))
    right = right_match.end() if right_match else len(source)
    sentence = source[left:right].strip()
    if sentence and sentence in source and len(sentence) <= 800:
        return sentence

    # Extremely long source line: keep a bounded exact window, aligned to the
    # real source string. This is provenance metadata, not creative rewriting.
    start = max(0, pos - 180)
    end = min(len(source), pos + len(wanted) + 260)
    span = source[start:end].strip()
    return span if span and span in source else wanted


def ground_source_evidence(payload: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Canonicalize entity evidence to exact server-owned source spans.

    The model owns creative fields. Provenance does not: when the model returns
    a paraphrased evidence sentence, the server deterministically binds the
    entity to an exact authoritative source span containing its canonical name.
    Missing identities still fail validation; nothing is invented here.
    """
    source = str(source_text or "")
    if not source or not isinstance(payload, dict):
        return payload

    output_kind = _clean(payload.get("output_kind"))
    if output_kind == "story_bible":
        groups = ("characters", "locations", "props")
    elif output_kind == "character_assets":
        groups = ("characters",)
    elif output_kind == "visual_assets":
        groups = ("locations", "props")
    else:
        groups = ()

    compact_source = _compact(source)
    for group in groups:
        rows = payload.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            evidence = _clean(row.get("source_evidence"))
            # Keep an already valid exact/normalized source quote unchanged.
            if evidence and evidence in source:
                continue
            if evidence and _compact(evidence) and _compact(evidence) in compact_source:
                # Normalized validation is valid, but persist an exact source
                # span so lineage remains inspectable without normalization.
                exact = _exact_sentence_for_name(source, _clean(row.get("name")))
                if exact:
                    row["source_evidence"] = exact
                continue
            exact = _exact_sentence_for_name(source, _clean(row.get("name")))
            if exact:
                row["source_evidence"] = exact
    return payload


def install_professional_source_grounding() -> dict[str, Any]:
    """Install the canonical source/provenance boundary before runtime import."""
    if getattr(gate, "_xiaoduan_professional_source_grounding_installed", False):
        return {"status": "already_installed", "policy": "professional_source_grounding_v1"}

    original_validate = registry.validate_professional_output

    def validate_professional_output(
        payload: dict[str, Any],
        *,
        source_text: str,
        required_names: dict[str, list[str]] | None = None,
    ) -> list[str]:
        ground_source_evidence(payload, source_text)
        return original_validate(
            payload,
            source_text=source_text,
            required_names=required_names,
        )

    gate._authoritative_message_source = authoritative_message_source
    registry.validate_professional_output = validate_professional_output

    # Be robust if a test/import loaded the runtime module before app.main.
    runtime = sys.modules.get("app.v3.professional_output_runtime")
    if runtime is not None:
        setattr(runtime, "validate_professional_output", validate_professional_output)

    gate._xiaoduan_professional_source_grounding_installed = True
    return {
        "status": "installed",
        "policy": "professional_source_grounding_v1",
        "authoritative_sections": sorted(_AUTHORITATIVE_SECTIONS),
        "server_owned_exact_evidence": True,
        "generated_history_is_source": False,
    }


__all__ = [
    "authoritative_message_source",
    "ground_source_evidence",
    "install_professional_source_grounding",
]
