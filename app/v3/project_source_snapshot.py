from __future__ import annotations

import contextvars
import hashlib
import re
from types import MethodType
from typing import Any

from . import front_half_quality_gate as gate
from . import professional_output_registry as registry
from .professional_output_runtime import ProfessionalOutputStore


_CURRENT_SOURCE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "xiaoduan_project_source_snapshot", default=None
)
_CONTROL_ONLY = {
    "重新生成", "重试", "再试一次", "继续", "确认", "确认并继续", "下一步",
    "进入下一阶段", "开始", "生成", "重新执行", "retry", "regenerate", "continue",
}
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u200b-\u200d\ufeff]+", "", _clean(value)).casefold()


def _sha(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _is_control_only(value: str) -> bool:
    text = _clean(value)
    compact = re.sub(r"[\s，。！？!?；;,.]+", "", text).casefold()
    return compact in {_norm(item) for item in _CONTROL_ONLY}


def _source_candidate_from_project(project: dict[str, Any]) -> tuple[str, str]:
    """Legacy migration only: recover the earliest real Stage01 user source.

    New projects never need this path because the source is snapshotted before
    the first Director execution. Assistant history is deliberately ignored.
    """
    for item in project.get("history") or []:
        if not isinstance(item, dict):
            continue
        if _clean(item.get("role")).casefold() != "user":
            continue
        if _clean(item.get("stage")) not in {"", "01"}:
            continue
        text = _clean(item.get("content"))
        if len(text) >= 24 and not _is_control_only(text):
            return text, "legacy_user_history_migration"
    return "", ""


def _active_snapshot(production: Any, project_id: str) -> dict[str, Any] | None:
    rows = production.list_assets(
        project_id,
        asset_role="project_source_snapshot",
        active_only=True,
    )
    rows = [
        row for row in rows
        if _clean(row.get("status")).lower() == "ready"
        and _clean(row.get("dependency_state")).lower() != "stale"
    ]
    rows.sort(key=lambda row: int(row.get("version") or 0))
    return rows[-1] if rows else None


def _studio_source_payload(production: Any, project_id: str) -> dict[str, Any] | None:
    """Return the original source asset created by the Studio project boundary."""
    rows = production.list_assets(
        project_id,
        asset_role="source_full",
        active_only=True,
    )
    rows = [
        row for row in rows
        if _clean(row.get("status")).lower() == "ready"
        and _clean(row.get("dependency_state")).lower() != "stale"
    ]
    rows.sort(key=lambda row: (int(row.get("version") or 0), _clean(row.get("created_at"))))
    if not rows:
        return None
    asset = rows[-1]
    try:
        text = production.read_text_asset(
            project_id,
            _clean(asset.get("asset_id")),
            max_chars=2_000_000,
        )
    except Exception:
        return None
    if not _clean(text):
        return None
    return {"asset": asset, "text": text, "sha256": _sha(text)}


def _snapshot_payload(production: Any, project_id: str) -> dict[str, Any] | None:
    asset = _active_snapshot(production, project_id)
    if asset is None:
        return None
    try:
        text = production.read_text_asset(project_id, _clean(asset.get("asset_id")), max_chars=2_000_000)
    except Exception:
        return None
    metadata = dict(asset.get("metadata") or {})
    return {
        "source_id": _clean(metadata.get("source_id")) or f"src_{_sha(text)[:20]}",
        "source_version": int(metadata.get("source_version") or 1),
        "source_sha256": _clean(metadata.get("source_sha256")) or _sha(text),
        "asset_id": _clean(asset.get("asset_id")),
        "text": text,
        "origin": _clean((asset.get("source") or {}).get("type")) or "project_source_snapshot",
    }


def _ensure_snapshot(director: Any, project_id: str, user_text: str) -> dict[str, Any] | None:
    existing = _snapshot_payload(director.production, project_id)
    project = director.get_project(project_id)
    studio_source = _studio_source_payload(director.production, project_id)
    if studio_source is not None:
        text = _clean(studio_source["text"])
        digest = _clean(studio_source["sha256"])
        if existing is not None and _clean(existing.get("source_sha256")) == digest:
            return existing
        source_asset = studio_source["asset"]
        source_id = f"src_{digest[:20]}"
        asset = director.production.create_text_asset(
            project_id,
            stage="source",
            skill="project-source-ingest",
            logical_key="project:source:original",
            asset_role="project_source_snapshot",
            name="原始创作源文本",
            content=text,
            extension=".txt",
            source={
                "type": "studio_source_full",
                "source_asset_id": _clean(source_asset.get("asset_id")),
            },
            parent_asset_ids=[_clean(source_asset.get("asset_id"))],
            metadata={
                "immutable": True,
                "source_id": source_id,
                "source_version": int((existing or {}).get("source_version") or 0) + 1,
                "source_sha256": digest,
                "provenance_authority": True,
                "replaces_incorrect_snapshot_asset_id": _clean((existing or {}).get("asset_id")),
            },
        )
        return {
            "source_id": source_id,
            "source_version": int((asset.get("metadata") or {}).get("source_version") or 1),
            "source_sha256": digest,
            "asset_id": _clean(asset.get("asset_id")),
            "text": text,
            "origin": "studio_source_full",
        }

    if _clean(project.get("current_stage")) != "01":
        return existing
    if existing is not None:
        return existing

    text = _clean(user_text)
    origin = "initial_stage01_user_source"
    if len(text) < 24 or _is_control_only(text):
        text, origin = _source_candidate_from_project(project)
    if not text:
        return None

    digest = _sha(text)
    source_id = f"src_{digest[:20]}"
    asset = director.production.create_text_asset(
        project_id,
        stage="source",
        skill="project-source-ingest",
        logical_key="project:source:original",
        asset_role="project_source_snapshot",
        name="原始创作源文本",
        content=text,
        extension=".txt",
        source={"type": origin},
        metadata={
            "immutable": True,
            "source_id": source_id,
            "source_version": 1,
            "source_sha256": digest,
            "provenance_authority": True,
        },
    )
    return {
        "source_id": source_id,
        "source_version": 1,
        "source_sha256": digest,
        "asset_id": _clean(asset.get("asset_id")),
        "text": text,
        "origin": origin,
    }


def current_source_snapshot() -> dict[str, Any] | None:
    value = _CURRENT_SOURCE.get()
    return dict(value) if isinstance(value, dict) else None


def _exact_sentence(source_text: str, name: str) -> str:
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
    if sentence and sentence in source and len(sentence) <= 1200:
        return sentence
    start = max(0, pos - 220)
    end = min(len(source), pos + len(wanted) + 360)
    span = source[start:end].strip()
    return span if span and span in source else ""


def bind_server_owned_evidence(payload: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Evidence is provenance metadata owned by the server, never by the model."""
    kind = _clean(payload.get("output_kind"))
    groups = {
        "story_bible": ("characters", "locations", "props"),
        "character_assets": ("characters",),
        "visual_assets": ("locations", "props"),
    }.get(kind, ())
    for group in groups:
        rows = payload.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            exact = _exact_sentence(source_text, _clean(row.get("name")))
            if exact:
                row["source_evidence"] = exact
    return payload


def typed_entity_logical_key(entity_type: str, name: str) -> str:
    """Unicode-safe stable identity; never collapse Chinese names through ASCII slugging."""
    etype = _clean(entity_type).lower() or "generic"
    identity = _norm(name)
    return f"typed:{etype}:{_sha(identity)[:20]}"


def install_project_source_snapshot(settings: Any, director: Any) -> dict[str, Any]:
    if getattr(director, "_xiaoduan_project_source_snapshot_installed", False):
        return {"status": "already_installed", "policy": "immutable_project_source_v1"}

    original_message = director.message
    original_source_reader = gate._authoritative_message_source

    async def message(
        instance: Any,
        project_id: str,
        user_text: str,
        *,
        native_control_action: str = "",
    ):
        snapshot = _ensure_snapshot(instance, project_id, user_text)
        token = _CURRENT_SOURCE.set(snapshot)
        try:
            return await original_message(
                project_id,
                user_text,
                native_control_action=native_control_action,
            )
        finally:
            _CURRENT_SOURCE.reset(token)

    def authoritative_source(messages: list[dict[str, Any]]) -> str:
        snapshot = current_source_snapshot()
        if snapshot and _clean(snapshot.get("text")):
            return _clean(snapshot.get("text"))
        return original_source_reader(messages)

    director.message = MethodType(message, director)
    gate._authoritative_message_source = authoritative_source

    # The professional runtime imported the validator by value. Patch that
    # runtime boundary explicitly so all typed Stage01-03 validation binds to
    # the immutable source snapshot before checking lineage/completeness.
    import app.v3.professional_output_runtime as runtime

    previous_validate = runtime.validate_professional_output

    def validate_professional_output(
        payload: dict[str, Any],
        *,
        source_text: str,
        required_names: dict[str, list[str]] | None = None,
    ) -> list[str]:
        snapshot = current_source_snapshot()
        authoritative = _clean((snapshot or {}).get("text")) or source_text
        bind_server_owned_evidence(payload, authoritative)
        return previous_validate(
            payload,
            source_text=authoritative,
            required_names=required_names,
        )

    runtime.validate_professional_output = validate_professional_output
    registry.validate_professional_output = validate_professional_output

    # Typed professional entities are materialized from the strict object, not
    # from Markdown evidence echoes. This also fixes the ASCII slug collision
    # where distinct Chinese names could otherwise share `character:entity`.
    production = director.production
    sidecar = ProfessionalOutputStore(settings.data_dir)
    original_record = production.record_control_entities

    def record_control_entities(
        production_instance: Any,
        project_id: str,
        *,
        stage: str,
        skill: str,
        content: str,
        turn_id: str,
        raw_entities: list[Any] | None,
        turn_asset_id: str,
    ) -> list[dict[str, Any]]:
        payload = sidecar.get_by_document(content)
        if not isinstance(payload, dict):
            return original_record(
                project_id,
                stage=stage,
                skill=skill,
                content=content,
                turn_id=turn_id,
                raw_entities=raw_entities,
                turn_asset_id=turn_asset_id,
            )

        snapshot = _snapshot_payload(production_instance, project_id)
        source_text = _clean((snapshot or {}).get("text"))
        bind_server_owned_evidence(payload, source_text)
        kind = _clean(payload.get("output_kind"))
        if kind == "story_bible":
            from .typed_front_half_authority import normalize_story_bible_characters

            payload, _normalization = normalize_story_bible_characters(payload, source_text)
            sidecar.put(payload)
        groups = {
            "story_bible": (("character", "characters"), ("location", "locations"), ("prop", "props")),
            "character_assets": (("character", "characters"),),
            "visual_assets": (("location", "locations"), ("prop", "props")),
        }.get(kind, ())
        result: list[dict[str, Any]] = []
        for entity_type, group_name in groups:
            for row in payload.get(group_name) or []:
                if not isinstance(row, dict):
                    continue
                name = _clean(row.get("name"))
                if not name:
                    continue
                evidence_quote = _clean(row.get("source_evidence"))
                metadata: dict[str, Any] = {
                    "professional_output_kind": kind,
                    "typed_professional_entity": True,
                    "source_snapshot_id": _clean((snapshot or {}).get("source_id")),
                    "source_snapshot_version": int((snapshot or {}).get("source_version") or 1),
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
                    "logical_key": typed_entity_logical_key(entity_type, name),
                    "name": name,
                    # The base recorder verifies that this display evidence is
                    # present in the generated document. The exact source quote
                    # remains separate and is persisted as graph evidence.
                    "evidence_quote": name,
                    "source_evidence": evidence_quote,
                    "source_asset_id": _clean((snapshot or {}).get("asset_id")) or turn_asset_id,
                    "source_snapshot_sha256": _clean((snapshot or {}).get("source_sha256")),
                    "metadata": metadata,
                })
        return original_record(
            project_id,
            stage=stage,
            skill=skill,
            content=content,
            turn_id=turn_id,
            raw_entities=result,
            turn_asset_id=turn_asset_id,
        )

    production.record_control_entities = MethodType(record_control_entities, production)
    director._xiaoduan_project_source_snapshot_installed = True
    repaired_snapshots = 0
    backfilled_story_bibles = 0
    list_projects = getattr(director, "list_projects", None)
    if callable(list_projects):
        for project in list_projects():
            project_id = _clean((project or {}).get("project_id"))
            if not project_id:
                continue
            before = _snapshot_payload(production, project_id)
            try:
                after = _ensure_snapshot(director, project_id, "")
            except Exception:
                continue
            if _clean((after or {}).get("asset_id")) != _clean((before or {}).get("asset_id")):
                repaired_snapshots += 1
            formal = production.list_assets(
                project_id,
                asset_role="professional_story_bible",
                active_only=True,
            )
            if formal:
                continue
            turns = [
                item for item in production.list_assets(project_id, stage="01", active_only=True)
                if _clean(item.get("asset_role")) == "director_turn_output"
                and _clean(item.get("status")).lower() == "ready"
                and _clean(item.get("dependency_state")).lower() != "stale"
            ]
            turns.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("created_at"))))
            if not turns:
                continue
            turn_asset = turns[-1]
            try:
                content = production.read_text_asset(
                    project_id, _clean(turn_asset.get("asset_id")), max_chars=600_000,
                )
            except Exception:
                continue
            payload = sidecar.get_by_document(content)
            if not isinstance(payload, dict) or _clean(payload.get("output_kind")) != "story_bible":
                continue
            turn_source = turn_asset.get("source") if isinstance(turn_asset.get("source"), dict) else {}
            try:
                production.record_control_entities(
                    project_id,
                    stage="01",
                    skill=_clean(turn_asset.get("skill")) or "xiaoduan-story-bible",
                    content=content,
                    turn_id=_clean(turn_source.get("turn_id")),
                    raw_entities=None,
                    turn_asset_id=_clean(turn_asset.get("asset_id")),
                )
            except Exception:
                continue
            backfilled_story_bibles += 1
    return {
        "status": "installed",
        "policy": "immutable_project_source_v1",
        "normal_path_uses_chat_history": False,
        "model_owns_source_evidence": False,
        "typed_entity_identity": "unicode_sha256",
        "legacy_history": "one_time_snapshot_migration_only",
        "repaired_studio_source_snapshots": repaired_snapshots,
        "backfilled_professional_story_bibles": backfilled_story_bibles,
    }


__all__ = [
    "bind_server_owned_evidence",
    "current_source_snapshot",
    "install_project_source_snapshot",
    "typed_entity_logical_key",
]
