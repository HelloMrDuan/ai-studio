from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError


class AuditProtocolError(RuntimeError):
    """The auditor failed its machine protocol; this is not a shot semantic failure."""


class SemanticAuditResult(BaseModel):
    evidence_entailment_ok: bool
    beat_coverage_ok: bool
    temporal_monotonic: bool
    no_future_event_preconsumption: bool
    no_result_duplication: bool
    state_order_valid: bool
    entity_visibility_valid: bool
    visual_realization_valid: bool
    violations: list[str]

    model_config = {"extra": "forbid"}


_REQUIRED_FIELDS = frozenset(SemanticAuditResult.model_fields)


def parse_semantic_audit(payload: str | dict[str, Any]) -> SemanticAuditResult:
    """Parse the canonical semantic audit schema without inventing booleans.

    Error-shaped model output such as `{code,message,source}` is a protocol
    failure. Missing booleans are never defaulted to True/False and therefore
    cannot accidentally enter semantic recovery.
    """

    if isinstance(payload, str):
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AuditProtocolError(f"semantic auditor returned invalid JSON: {exc}") from exc
    elif isinstance(payload, dict):
        raw = payload
    else:
        raise AuditProtocolError("semantic auditor payload must be JSON object or JSON string")

    if not isinstance(raw, dict):
        raise AuditProtocolError("semantic auditor JSON root must be an object")
    missing = sorted(_REQUIRED_FIELDS - set(raw))
    if missing:
        keys = sorted(str(key) for key in raw)
        raise AuditProtocolError(
            f"semantic audit schema incomplete: missing={missing} keys={keys}"
        )
    try:
        return SemanticAuditResult.model_validate(raw)
    except ValidationError as exc:
        raise AuditProtocolError(f"semantic audit schema invalid: {exc}") from exc


def semantic_audit_passed(result: SemanticAuditResult) -> bool:
    boolean_fields = (
        result.evidence_entailment_ok,
        result.beat_coverage_ok,
        result.temporal_monotonic,
        result.no_future_event_preconsumption,
        result.no_result_duplication,
        result.state_order_valid,
        result.entity_visibility_valid,
        result.visual_realization_valid,
    )
    return all(boolean_fields) and not result.violations
