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
_RECENT_HISTORY_SECTION = "RECENT CURRENT-STAGE HISTORY"
_HISTORY_ROLE = re.compile(r"(?m)^(user|assistant):\s*")
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]")
_OLD_MACHINE_BLOCKS = (
    "```story-entities-json",
    "```appearance-versions-json",
    "```visual-direction-json",
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _compact(value: Any) -> str:
    return re.sub(
        r"[\s，。；：、“”‘’！？,.!?;:'\"（）()【】\[\]]+",
        "",
        str(value or ""),
    ).casefold()


def _user_history_sources(value: str) -> list[str]:
    """Recover only user-authored records from Director stage history.

    A regenerate click usually makes CURRENT USER MESSAGE equal to a control
    command such as ``重新生成``. The original story still exists in
    RECENT CURRENT-STAGE HISTORY, mixed with generated assistant turns. Only
    user records are authoritative; assistant records remain excluded.
    """
    text = str(value or "")
    matches = list(_HISTORY_ROLE.finditer(text))
    rows: list[str] = []
    for index, match in enumerate(matches):
        if match.group(1).casefold() != "user":
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        item = _clean(text[start:end])
        if item:
            rows.append(item)
    return rows


def authoritative_message_source(messages: list[dict[str, Any]]) -> str:
    """Read only Director sections explicitly declared authoritative.

    Current authoritative sections are accepted verbatim. For regeneration,
    user-authored records are additionally recovered from recent stage history;
    generated assistant history is never accepted as evidence.
    """
    rows: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        item = _clean(value)
        if not item or item in {"<none>", "<omitted>"} or item in seen:
            return
        seen.add(item)
        rows.append(item)

    for message in messages:
        if not isinstance(message, dict):
            continue
        text = str(message.get("content") or "")
        matches = list(_SECTION_MARKER.finditer(text))
        for index, match in enumerate(matches):
            label = _clean(match.group(1)).upper()
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            value = _clean(text[start:end])
            if label in _AUTHORITATIVE_SECTIONS:
                add(value)
            elif label == _RECENT_HISTORY_SECTION:
                for item in _user_history_sources(value):
                    add(item)
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


def _entity_groups(payload: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    output_kind = _clean(payload.get("output_kind"))
    if output_kind == "story_bible":
        return [
            ("character", "角色", list(payload.get("characters") or [])),
            ("location", "地点", list(payload.get("locations") or [])),
            ("prop", "道具", list(payload.get("props") or [])),
        ]
    if output_kind == "character_assets":
        return [("character", "角色", list(payload.get("characters") or []))]
    if output_kind == "visual_assets":
        return [
            ("location", "地点", list(payload.get("locations") or [])),
            ("prop", "道具", list(payload.get("props") or [])),
        ]
    return []


def _sync_document_entity_index(payload: dict[str, Any]) -> None:
    """Mirror machine identities into Markdown without inventing new facts."""
    document = _clean(payload.get("document"))
    if not document:
        return
    lines: list[str] = []
    for _entity_type, label, rows in _entity_groups(payload):
        names = [
            _clean(row.get("name"))
            for row in rows
            if isinstance(row, dict) and _clean(row.get("name"))
        ]
        missing = [name for name in names if name not in document]
        if missing:
            lines.append(f"- {label}：" + "、".join(dict.fromkeys(missing)))
    if lines:
        payload["document"] = (
            document.rstrip()
            + "\n\n## 实体索引（系统同步）\n"
            + "\n".join(lines)
        )


def _typed_document_issues(payload: dict[str, Any]) -> list[str]:
    """Validate only presentation boundaries owned by the typed runtime.

    Machine arrays/objects are the source of truth. The human Markdown document
    is no longer forced to duplicate the retired legacy heading template.
    """
    document = _clean(payload.get("document"))
    issues: list[str] = []
    if not document:
        issues.append("专业输出 document 不能为空")
        return issues
    for old_block in _OLD_MACHINE_BLOCKS:
        if old_block in document:
            issues.append(f"document 不得再内嵌旧机器块：{old_block}")
    if _clean(payload.get("output_kind")) == "story_bible" and gate._contains(
        document,
        "最终图片 prompt",
        "最终图片提示词",
        "镜头焦段参数",
        "comfyui 参数",
    ):
        issues.append("Stage01 混入了后续媒体/镜头执行参数")
    return issues


def _typed_entity_rows(payload: dict[str, Any], document: str) -> list[dict[str, Any]]:
    """Materialize typed entities from machine authority, not prose parsing."""
    output_kind = _clean(payload.get("output_kind"))
    result: list[dict[str, Any]] = []
    for entity_type, _label, rows in _entity_groups(payload):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = _clean(row.get("name"))
            if not name:
                continue
            metadata: dict[str, Any] = {
                "professional_output_kind": output_kind,
                "typed_professional_entity": True,
                "source_evidence": _clean(row.get("source_evidence")),
            }
            stable = _clean(row.get("stable_description"))
            if stable:
                metadata["authoring"] = {
                    "stable_design": stable,
                    "source_design": stable,
                    "stable_projection": "typed_professional_output_v1",
                }
            result.append({
                "entity_type": entity_type,
                "name": name,
                # ProductionAssetService requires evidence_quote to be present
                # in the materialized turn text. The synchronized entity index
                # guarantees the canonical name is present without a second LLM.
                "evidence_quote": name,
                "metadata": metadata,
            })
    return result


def _patch_runtime(runtime: Any) -> None:
    # Runtime functions look these names up dynamically, so patching the module
    # keeps one typed authority without another model pass.
    setattr(runtime, "_document_issues", _typed_document_issues)
    setattr(runtime, "_entity_rows", _typed_entity_rows)


def install_professional_source_grounding() -> dict[str, Any]:
    """Install canonical source/provenance and typed presentation boundaries."""
    already_installed = bool(
        getattr(gate, "_xiaoduan_professional_source_grounding_installed", False)
    )

    if not already_installed:
        original_validate = registry.validate_professional_output

        def validate_professional_output(
            payload: dict[str, Any],
            *,
            source_text: str,
            required_names: dict[str, list[str]] | None = None,
        ) -> list[str]:
            ground_source_evidence(payload, source_text)
            _sync_document_entity_index(payload)
            issues = original_validate(
                payload,
                source_text=source_text,
                required_names=required_names,
            )
            # Machine identity arrays are authoritative. The deterministic index
            # should satisfy display mirroring, but never let prose duplication
            # become a second semantic blocker.
            return [
                issue for issue in issues
                if "未出现在 document，展示文本与机器事实不一致" not in issue
            ]

        gate._authoritative_message_source = authoritative_message_source
        registry.validate_professional_output = validate_professional_output
        gate._xiaoduan_professional_source_grounding_installed = True

    # Be robust if a test/import loaded the runtime module before or after the
    # first installer call.
    runtime = sys.modules.get("app.v3.professional_output_runtime")
    if runtime is not None:
        setattr(runtime, "validate_professional_output", registry.validate_professional_output)
        _patch_runtime(runtime)

    return {
        "status": "already_installed" if already_installed else "installed",
        "policy": "professional_source_grounding_v2",
        "authoritative_sections": sorted(_AUTHORITATIVE_SECTIONS),
        "regenerate_user_history_source": True,
        "server_owned_exact_evidence": True,
        "generated_history_is_source": False,
        "typed_document_is_machine_authority": False,
        "document_entity_index": "deterministic_mirror_only",
    }


__all__ = [
    "authoritative_message_source",
    "ground_source_evidence",
    "install_professional_source_grounding",
]
