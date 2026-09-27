from __future__ import annotations

import re
from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def sanitize_character_stable_design(text: str) -> str:
    """Remove media/shot-only instructions from durable character identity.

    Stage② may contain a separate reference-image requirement. That requirement
    is useful to the reference generator, but it must never become part of the
    canonical cross-shot identity or the default appearance asset. In
    particular, scene backgrounds such as "苍梧山顶风雪" belong to Stage③/④.
    """
    rows: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            if rows and rows[-1] != "":
                rows.append("")
            continue
        normalized = re.sub(r"^[\s>*_`#-]+", "", line).strip()
        if "参考图生成要求" in normalized or "角色参考图生成要求" in normalized:
            continue
        if re.match(r"^背景\s*(?:为|[:：])", normalized):
            continue
        rows.append(raw.rstrip())

    while rows and not rows[-1].strip():
        rows.pop()
    result = "\n".join(rows).strip()
    return result or str(text or "").strip()


class CharacterIdentityCleanupService:
    """Reconcile already-materialized Stage② characters without another LLM call."""

    def __init__(self, legacy_runtime: Any) -> None:
        self.director = legacy_runtime.director
        self.production = self.director.production

    def reconcile_project(self, project_id: str) -> dict[str, Any]:
        graph = self.production.get_graph(project_id)
        changed_ids: list[str] = []

        for entity in (graph.get("entities") or {}).values():
            if not isinstance(entity, dict):
                continue
            if _clean(entity.get("entity_type")).lower() != "character":
                continue
            metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
            authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
            old_design = _clean(authoring.get("stable_design"))
            if not old_design:
                continue
            new_design = sanitize_character_stable_design(old_design)
            if new_design == old_design:
                continue

            authoring["stable_design"] = new_design
            authoring["identity_cleanup"] = "shot_background_removed"
            metadata["authoring"] = authoring

            continuity = metadata.get("continuity") if isinstance(metadata.get("continuity"), dict) else {}
            core = continuity.get("core_profile") if isinstance(continuity.get("core_profile"), dict) else {}
            if _clean(core.get("阶段正式设定")) == old_design:
                core["阶段正式设定"] = new_design
                continuity["core_profile"] = core
                metadata["continuity"] = continuity

            entity["metadata"] = metadata
            changed_ids.append(_clean(entity.get("entity_id")))

        if changed_ids:
            self.production._save(graph)

        return {
            "project_id": project_id,
            "changed": bool(changed_ids),
            "character_entity_ids": [item for item in changed_ids if item],
            "model_calls": 0,
        }


__all__ = ["sanitize_character_stable_design", "CharacterIdentityCleanupService"]
