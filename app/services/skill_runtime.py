from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def empty_runtime_state() -> dict[str, Any]:
    return {
        "schema_version": "skill_runtime_state_v2",
        "contract_source_sha256": "",
        "selected_output_group_ids": [],
        "active_requirement_ids": [],
        "artifact_registry": {},
        "requirement_registry": {},
        "completion": {
            "ready": False,
            "mode": "uninitialized",
            "missing_artifact_ids": [],
            "missing_requirement_ids": [],
            "reason": "Skill Runtime 尚未初始化",
        },
        "updated_at": "",
    }


def _exact_quote_ok(source: str, quote: str) -> bool:
    q = _clean(quote)
    return bool(q and q in source)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(minimum, min(number, maximum))


def normalize_contract(
    *,
    skill_name: str,
    skill_md: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Normalize a model-compiled contract and ground every rule to SKILL.md.

    This function never infers business meaning. Anything that cannot be tied
    to an exact contiguous SKILL.md quote is dropped rather than guessed.
    """
    source_hash = source_sha256(skill_md)
    completion_mode = _clean(raw.get("completion_mode"))
    if completion_mode not in {"native_only", "artifact_gate"}:
        completion_mode = "native_only"

    groups: list[dict[str, Any]] = []
    seen_artifacts: set[str] = set()
    raw_groups = raw.get("output_groups")
    if isinstance(raw_groups, list):
        for gi, group in enumerate(raw_groups, 1):
            if not isinstance(group, dict):
                continue
            group_quote = _clean(group.get("source_quote"))
            if group_quote and not _exact_quote_ok(skill_md, group_quote):
                continue
            artifacts: list[dict[str, Any]] = []
            raw_artifacts = group.get("artifacts")
            if isinstance(raw_artifacts, list):
                for item in raw_artifacts:
                    if not isinstance(item, dict):
                        continue
                    src_quote = _clean(item.get("source_quote"))
                    if not _exact_quote_ok(skill_md, src_quote):
                        continue
                    marker = _clean(item.get("literal_marker"))
                    if marker and marker not in skill_md:
                        marker = ""
                    aid = f"A{len(seen_artifacts) + 1:03d}"
                    seen_artifacts.add(aid)
                    artifacts.append({
                        "artifact_id": aid,
                        "name": _clean(item.get("name")) or aid,
                        "required": bool(item.get("required", True)),
                        "source_quote": src_quote,
                        "literal_marker": marker,
                        "evidence_mode": (
                            "literal_marker" if marker else "grounded_receipt"
                        ),
                        "asset_type": (
                            _clean(item.get("asset_type")).upper()
                            if _clean(item.get("asset_type")).upper()
                            in {"TEXT","STRUCTURED_DATA","IMAGE","VIDEO","AUDIO","FILE","ENTITY","COLLECTION"}
                            else "TEXT"
                        ),
                        "asset_role": _clean(item.get("asset_role")) or "skill_artifact",
                        "materialization": (
                            _clean(item.get("materialization")).lower()
                            if _clean(item.get("materialization")).lower()
                            in {"text","structured","task_output","external_file","entity","collection"}
                            else "text"
                        ),
                        "producer_capability": (
                            _clean(item.get("producer_capability")).lower()
                            if _clean(item.get("producer_capability")).lower()
                            in {"director","image","video","facefusion","external","none"}
                            else "director"
                        ),
                        "cardinality_min": _bounded_int(item.get("cardinality_min", 1), 1, 0, 1000),
                        "cardinality_max": (
                            _bounded_int(item.get("cardinality_max"), 1, 1, 1000)
                            if str(item.get("cardinality_max") or "").isdigit()
                            else None
                        ),
                        "file_extension": (
                            _clean(item.get("file_extension"))
                            if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", _clean(item.get("file_extension")))
                            else ".md"
                        ),
                    })
            if not artifacts:
                continue
            groups.append({
                "group_id": f"G{len(groups) + 1:03d}",
                "name": _clean(group.get("name")) or f"output-{gi}",
                "source_quote": group_quote,
                "artifacts": artifacts,
            })

    requirements: list[dict[str, Any]] = []
    raw_requirements = raw.get("conditional_requirements")
    if isinstance(raw_requirements, list):
        for item in raw_requirements:
            if not isinstance(item, dict):
                continue
            src_quote = _clean(item.get("source_quote"))
            if not _exact_quote_ok(skill_md, src_quote):
                continue
            requirements.append({
                "requirement_id": f"R{len(requirements) + 1:03d}",
                "name": _clean(item.get("name")) or f"requirement-{len(requirements)+1}",
                "source_quote": src_quote,
                "activation_description": _clean(item.get("activation_description")),
                "required_behavior": _clean(item.get("required_behavior")),
            })

    # An artifact gate without any grounded artifact would be a self-created
    # blocker, so degrade to native_only instead of inventing requirements.
    if completion_mode == "artifact_gate" and not groups:
        completion_mode = "native_only"

    return {
        "schema_version": "skill_contract_v2",
        "skill_name": skill_name,
        "source_sha256": source_hash,
        "completion_mode": completion_mode,
        "output_groups": groups,
        "conditional_requirements": requirements,
        "compiler_reason": _clean(raw.get("reason")),
    }


def contract_index(contract: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    groups: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    requirements: dict[str, dict[str, Any]] = {}
    for group in contract.get("output_groups") or []:
        if not isinstance(group, dict):
            continue
        gid = _clean(group.get("group_id"))
        if gid:
            groups[gid] = group
        for item in group.get("artifacts") or []:
            if isinstance(item, dict):
                aid = _clean(item.get("artifact_id"))
                if aid:
                    artifacts[aid] = item
    for item in contract.get("conditional_requirements") or []:
        if isinstance(item, dict):
            rid = _clean(item.get("requirement_id"))
            if rid:
                requirements[rid] = item
    return groups, artifacts, requirements


def _verified_receipt(
    *,
    item_id: str,
    evidence_quote: str,
    content: str,
    content_sha256: str,
    turn_id: str,
    source_kind: str,
) -> dict[str, Any] | None:
    quote = _clean(evidence_quote)
    if not quote or quote not in content:
        return None
    return {
        "id": item_id,
        "evidence_quote": quote,
        "evidence_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "content_sha256": content_sha256,
        "turn_id": turn_id,
        "source_kind": source_kind,
        "verified": True,
        "recorded_at": _utcnow(),
    }


def update_runtime_state(
    *,
    contract: dict[str, Any],
    previous: dict[str, Any] | None,
    content: str,
    control_runtime: dict[str, Any] | None,
    native_target: dict[str, Any],
    native_plan: dict[str, Any],
    turn_id: str,
) -> dict[str, Any]:
    state = dict(previous or empty_runtime_state())
    state["schema_version"] = "skill_runtime_state_v2"
    state["contract_source_sha256"] = _clean(contract.get("source_sha256"))
    groups, artifacts, requirements = contract_index(contract)
    payload = control_runtime if isinstance(control_runtime, dict) else {}

    selected = []
    for gid in payload.get("selected_output_group_ids") or []:
        gid = _clean(gid)
        if gid in groups and gid not in selected:
            selected.append(gid)
    # One declared terminal output shape needs no model choice. Multiple
    # shapes keep the previously selected route unless the current control
    # plane explicitly selects another valid one.
    if not selected and len(groups) == 1:
        selected = [next(iter(groups))]
    prior_selected = [
        _clean(x) for x in state.get("selected_output_group_ids") or []
        if _clean(x) in groups
    ]
    if not selected:
        selected = prior_selected
    state["selected_output_group_ids"] = selected

    # Conditional requirements reflect the CURRENT real input state. Do not
    # accumulate stale conditions forever (for example, an input may be added
    # on a later turn). Receipts themselves remain persisted if still active.
    active_requirements: list[str] = []
    for rid in payload.get("active_requirement_ids") or []:
        rid = _clean(rid)
        if rid in requirements and rid not in active_requirements:
            active_requirements.append(rid)
    state["active_requirement_ids"] = active_requirements

    artifact_registry = dict(state.get("artifact_registry") or {})
    requirement_registry = dict(state.get("requirement_registry") or {})
    content = _clean(content)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Literal markers come directly from SKILL.md. A marker alone is not
    # enough: bind it to a contiguous slice that also contains substantive
    # produced body text. This keeps short headings usable without treating
    # an empty heading as a completed artifact.
    for aid, spec in artifacts.items():
        marker = _clean(spec.get("literal_marker"))
        if marker and marker in content:
            pos = content.find(marker)
            tail = content[pos : pos + 360]
            lines = tail.splitlines()
            evidence_lines: list[str] = []
            visible_after_marker = 0
            marker_seen = False
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    if marker_seen and visible_after_marker:
                        break
                    continue
                if marker_seen and stripped.startswith(("# ", "## ", "### ", "#### ")):
                    break
                evidence_lines.append(line)
                if marker in line:
                    marker_seen = True
                    after = line.split(marker, 1)[1]
                    visible_after_marker += len(re.sub(r"\s+", "", after))
                elif marker_seen:
                    visible_after_marker += len(re.sub(r"\s+", "", stripped))
                if marker_seen and visible_after_marker >= 16:
                    break
            literal_evidence = "\n".join(evidence_lines).strip()
            if marker_seen and visible_after_marker >= 16:
                receipt = _verified_receipt(
                    item_id=aid,
                    evidence_quote=literal_evidence,
                    content=content,
                    content_sha256=content_hash,
                    turn_id=turn_id,
                    source_kind="skill_literal_marker",
                )
                if receipt:
                    artifact_registry[aid] = receipt

    # For output shapes without a literal marker, the existing control-plane
    # call may register an exact quote from the produced content. Runtime only
    # accepts IDs from the grounded contract and verbatim quotes from content.
    for item in payload.get("artifact_receipts") or []:
        if not isinstance(item, dict):
            continue
        aid = _clean(item.get("artifact_id"))
        if aid not in artifacts:
            continue
        receipt = _verified_receipt(
            item_id=aid,
            evidence_quote=_clean(item.get("evidence_quote")),
            content=content,
            content_sha256=content_hash,
            turn_id=turn_id,
            source_kind="grounded_artifact_receipt",
        )
        if receipt:
            artifact_registry[aid] = receipt

    for item in payload.get("requirement_receipts") or []:
        if not isinstance(item, dict):
            continue
        rid = _clean(item.get("requirement_id"))
        if rid not in requirements:
            continue
        receipt = _verified_receipt(
            item_id=rid,
            evidence_quote=_clean(item.get("evidence_quote")),
            content=content,
            content_sha256=content_hash,
            turn_id=turn_id,
            source_kind="grounded_requirement_receipt",
        )
        if receipt:
            requirement_registry[rid] = receipt

    state["artifact_registry"] = artifact_registry
    state["requirement_registry"] = requirement_registry
    state["updated_at"] = _utcnow()
    state["completion"] = completion_status(
        contract=contract,
        runtime_state=state,
        control_runtime=payload,
        native_target=native_target,
        native_plan=native_plan,
    )
    return state


def completion_status(
    *,
    contract: dict[str, Any],
    runtime_state: dict[str, Any],
    control_runtime: dict[str, Any] | None,
    native_target: dict[str, Any],
    native_plan: dict[str, Any],
    asset_readiness: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    mode = _clean(contract.get("completion_mode")) or "native_only"
    groups, artifacts, requirements = contract_index(contract)
    selected = [
        _clean(x) for x in runtime_state.get("selected_output_group_ids") or []
        if _clean(x) in groups
    ]
    active_requirements = [
        _clean(x) for x in runtime_state.get("active_requirement_ids") or []
        if _clean(x) in requirements
    ]
    artifact_registry = runtime_state.get("artifact_registry") or {}
    requirement_registry = runtime_state.get("requirement_registry") or {}

    required_artifacts: list[str] = []
    if mode == "artifact_gate":
        for gid in selected:
            group = groups.get(gid) or {}
            for item in group.get("artifacts") or []:
                if bool(item.get("required", True)):
                    aid = _clean(item.get("artifact_id"))
                    if aid and aid not in required_artifacts:
                        required_artifacts.append(aid)

    readiness = asset_readiness if isinstance(asset_readiness, dict) else {}
    missing_artifacts: list[str] = []
    materialized_asset_ids: dict[str, list[str]] = {}
    for aid in required_artifacts:
        spec = artifacts.get(aid) or {}
        receipt = artifact_registry.get(aid) or {}
        ready_ids = [
            _clean(x) for x in readiness.get(aid) or [] if _clean(x)
        ]
        materialized_asset_ids[aid] = ready_ids
        materialization = _clean(spec.get("materialization")).lower() or "text"
        asset_type = _clean(spec.get("asset_type")).upper() or "TEXT"
        requires_real_asset = (
            materialization in {"task_output", "external_file"}
            or asset_type in {"IMAGE", "VIDEO", "AUDIO"}
        )
        minimum = _bounded_int(spec.get("cardinality_min", 1), 1, 0, 1000)
        if asset_readiness is not None:
            if len(ready_ids) < minimum:
                missing_artifacts.append(aid)
        elif requires_real_asset:
            if len(ready_ids) < minimum:
                missing_artifacts.append(aid)
        elif not bool(receipt.get("verified")):
            missing_artifacts.append(aid)
    missing_requirements = [
        rid for rid in active_requirements
        if not bool((requirement_registry.get(rid) or {}).get("verified"))
    ]

    target_kind = _clean(native_target.get("kind"))
    plan_mode = _clean(native_plan.get("mode"))
    payload = control_runtime if isinstance(control_runtime, dict) else {}

    if plan_mode == "sequential":
        native_terminal = target_kind == "complete_stage"
    else:
        native_terminal = bool(payload.get("stage_complete_claim"))

    native_steps = [
        _clean(value) for value in native_plan.get("steps") or []
        if _clean(value)
    ]
    try:
        current_index = int(native_plan.get("current_index", -1))
    except Exception:
        current_index = -1
    try:
        target_index = int(native_target.get("index", -1))
    except Exception:
        target_index = -1
    observed_index = max(
        current_index,
        target_index if target_kind == "step" else -1,
    )
    completed_native_steps = max(
        0, min(len(native_steps), observed_index + 1),
    )
    next_native_step = ""
    awaiting_native_approval = False
    native_issue = ""
    if not native_terminal:
        if plan_mode == "sequential" and native_steps:
            if completed_native_steps < len(native_steps):
                next_native_step = native_steps[completed_native_steps]
                native_issue = f"native_step:{next_native_step}"
            else:
                awaiting_native_approval = True
                native_issue = f"native_approval:{native_steps[-1]}"
        else:
            native_issue = (
                "native_completion_claim:false;"
                f"target={target_kind or 'missing'};"
                f"plan_mode={plan_mode or 'missing'}"
            )
    native_progress = {
        "plan_mode": plan_mode or "missing",
        "total_steps": len(native_steps),
        "completed_steps": completed_native_steps,
        "current_index": current_index,
        "target_kind": target_kind or "missing",
        "target_index": target_index,
        "next_step": next_native_step,
        "awaiting_approval": awaiting_native_approval,
        "issue": native_issue,
    }

    if mode == "artifact_gate" and groups and not selected:
        ready = False
        reason = "Skill 定义了多个输出形态，但当前尚未选择适用输出形态"
    elif missing_artifacts:
        ready = False
        reason = "当前 Skill 仍缺少必需产物"
    elif missing_requirements:
        ready = False
        reason = "当前 Skill 仍有已激活规则尚未在用户可见输出中落实"
    elif not native_terminal:
        ready = False
        if next_native_step:
            reason = (
                "Skill内部步骤未完成："
                f"{completed_native_steps}/{len(native_steps)}；"
                f"尚未完成：{next_native_step}"
            )
        elif awaiting_native_approval:
            reason = (
                "Skill生产步骤已完成，尚缺原生完成批准："
                f"{native_steps[-1]}"
            )
        else:
            reason = "Skill动态完成声明未产生：" + native_issue
    else:
        ready = True
        reason = "Skill 原生流程与已声明产物均已满足"

    return {
        "ready": ready,
        "mode": mode,
        "selected_output_group_ids": selected,
        "required_artifact_ids": required_artifacts,
        "missing_artifact_ids": missing_artifacts,
        "materialized_asset_ids": materialized_asset_ids,
        "active_requirement_ids": active_requirements,
        "missing_requirement_ids": missing_requirements,
        "native_terminal": native_terminal,
        "native_progress": native_progress,
        "reason": reason,
    }


def apply_asset_completion(
    *,
    contract: dict[str, Any],
    runtime_state: dict[str, Any],
    control_runtime: dict[str, Any] | None,
    native_target: dict[str, Any],
    native_plan: dict[str, Any],
    asset_readiness: dict[str, list[str]],
) -> dict[str, Any]:
    runtime_state["completion"] = completion_status(
        contract=contract,
        runtime_state=runtime_state,
        control_runtime=control_runtime,
        native_target=native_target,
        native_plan=native_plan,
        asset_readiness=asset_readiness,
    )
    runtime_state["updated_at"] = _utcnow()
    return runtime_state


def compact_contract(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _clean(contract.get("schema_version")),
        "skill_name": _clean(contract.get("skill_name")),
        "source_sha256": _clean(contract.get("source_sha256")),
        "completion_mode": _clean(contract.get("completion_mode")),
        "output_groups": contract.get("output_groups") or [],
        "conditional_requirements": contract.get("conditional_requirements") or [],
    }
