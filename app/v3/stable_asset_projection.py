from __future__ import annotations

import re
from typing import Any


_DROP_BY_KIND = {
    "character": (
        "允许变化项", "可变化项", "形象版本", "change_reason", "参考图生成要求", "角色参考图生成要求",
        "原文证据", "设计补全来源", "补全来源", "剧情节点", "生效剧情节点", "appearance-versions-json",
    ),
    "location": (
        "可变化状态", "允许变化项", "参考图生成要求", "原文证据", "设计补全来源", "补全来源",
        "剧情动作", "人物位置", "change_reason",
    ),
    "prop": (
        "剧情功能", "用途", "可变化状态", "允许变化项", "版本变化", "change_reason", "参考图生成要求",
        "原文证据", "设计补全来源", "补全来源", "人物动作", "握持动作",
    ),
}
_FENCE = re.compile(r"^\s*```(?:appearance-versions-json|visual-direction-json)?\s*$", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*$")
_LABEL = re.compile(r"^[\s>*_-]*(?:\*\*)?([^：:]{1,40})(?:\*\*)?\s*[：:]")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _drop_label(kind: str, label: str) -> bool:
    lowered = _clean(label).casefold()
    return any(token.casefold() in lowered for token in _DROP_BY_KIND.get(kind, ()))


def project_stable_design(kind: str, text: str) -> str:
    """Project a stage design block to stable, reusable visible identity facts.

    The original Stage02/03 block remains source evidence elsewhere. This
    projection intentionally removes generation layout, provenance, transient
    state and version-routing metadata so downstream prompts cannot mistake
    them for visual identity anchors.
    """
    asset_kind = _clean(kind).lower()
    if asset_kind not in _DROP_BY_KIND:
        return _clean(text)
    rows: list[str] = []
    skipping_fence = False
    skip_section_level = 0

    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if rows and rows[-1] != "":
                rows.append("")
            continue

        if stripped.startswith("```appearance-versions-json") or stripped.startswith("```visual-direction-json"):
            skipping_fence = True
            continue
        if skipping_fence:
            if stripped == "```":
                skipping_fence = False
            continue

        heading = _HEADING.match(line)
        if heading:
            hashes = len(line) - len(line.lstrip("#"))
            title = _clean(heading.group(1))
            if _drop_label(asset_kind, title):
                skip_section_level = max(1, hashes)
                continue
            if skip_section_level and hashes <= skip_section_level:
                skip_section_level = 0
            if skip_section_level:
                continue
            rows.append(line)
            continue

        if skip_section_level:
            continue

        label_match = _LABEL.search(stripped)
        if label_match and _drop_label(asset_kind, label_match.group(1)):
            continue
        if any(token.casefold() in stripped.casefold() for token in _DROP_BY_KIND[asset_kind] if token in {"appearance-versions-json"}):
            continue
        rows.append(line)

    while rows and not rows[-1].strip():
        rows.pop()
    result = "\n".join(rows).strip()
    return result or _clean(text)


__all__ = ["project_stable_design"]
