from __future__ import annotations

from pathlib import Path

RUNTIME = Path("app/stage04_v238_runtime.py")
TEST = Path("tests/test_stage04_first_beat_temporal_reconsideration.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = RUNTIME.read_text(encoding="utf-8")

    helper = r'''
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


'''
    marker = "def _reselect_adjacent_evidence(\n"
    if text.count(marker) != 1:
        raise SystemExit(f"edge temporal helper insertion marker count={text.count(marker)}")
    text = text.replace(marker, helper + marker, 1)

    old = '"static_outcome=证据只支持已成立的状态/结果/关系，未描述其发生过程；"'
    new = (
        '"static_outcome=证据只支持已成立的状态/结果/关系，或一个正在持续但没有证据支持内部前中后里程碑的稳定活动状态；"'
    )
    text = replace_once(text, old, new, "main temporal mode semantics")

    old = (
        '            "static_outcome=证据只支持已经成立的状态/结果/关系而未描述发生过程；"\n'
    )
    new = (
        '            "static_outcome=证据只支持已经成立的状态/结果/关系，或只支持一个正在持续但没有证据支持内部前中后里程碑的稳定活动状态；"\n'
    )
    text = replace_once(text, old, new, "classification repair semantics")

    old = (
        '            "static_outcome 必须锁定同一 narrative_state，把不同画面和运动严格隔离到"\n'
    )
    new = (
        '            "static_outcome 也适用于证据只证明一个持续活动/稳定状态而不证明内部时间里程碑的情况；"\n'
        '            "static_outcome 必须锁定同一 narrative_state，把不同画面和运动严格隔离到"\n'
    )
    text = replace_once(text, old, new, "missing beat static semantics")

    old = (
        '        "可见前向状态链。static_outcome 必须保持 narrative state 稳定，并把构图、机位、光影、"\n'
    )
    new = (
        '        "可见前向状态链。static_outcome 也适用于证据只证明持续活动/稳定状态但不证明内部前中后里程碑；"\n'
        '        "static_outcome 必须保持 narrative state 稳定，并把构图、机位、光影、"\n'
    )
    text = replace_once(text, old, new, "regroup static semantics")

    old = '''    prior_metadata: dict[str, Any],\n) -> tuple[list[dict[str, Any]], dict[str, Any]]:\n    target_order = int(target_beat.get("order") or 0)\n    _stage04_progress(\n'''
    new = '''    prior_metadata: dict[str, Any],\n    current_rows: list[dict[str, Any]] | None = None,\n) -> tuple[list[dict[str, Any]], dict[str, Any]]:\n    target_order = int(target_beat.get("order") or 0)\n\n    previous_beat = next((\n        beat for beat in all_beats\n        if isinstance(beat, dict)\n        and int(beat.get("order") or 0) == target_order - 1\n    ), None)\n\n    # A first/edge Beat has no earlier fact authority to borrow from.  Before\n    # declaring regroup impossible, reconsider whether strict audit has shown\n    # that the Beat is a stable state/ongoing activity rather than a provable\n    # three-milestone transition.  This path is target-evidence-only and never\n    # consumes NEXT_BEAT.\n    if previous_beat is None and current_rows:\n        edge_candidate = await _reconsider_edge_beat_temporal_mode(\n            env,\n            row=current_rows[0],\n            compact_beats=current_compact_beats,\n            anchors=current_anchors,\n            context=f"Beat {target_order} edge recovery",\n        )\n        if edge_candidate is not None:\n            try:\n                edge_rows = validate_rows(\n                    env,\n                    raw_rows=[edge_candidate],\n                    compact_beats=current_compact_beats,\n                    allowed_chars=allowed_chars,\n                    allowed_props=allowed_props,\n                    anchors=current_anchors,\n                    scene_id=scene_id,\n                    episode_id=episode_id,\n                )\n                edge_source_window = "\\n".join(\n                    str(anchor.get("text") or "")\n                    for anchor in current_anchors\n                    if isinstance(anchor, dict)\n                )\n                edge_audit = await audit_fn(\n                    source_window=edge_source_window,\n                    compact_beats=current_compact_beats,\n                    shots=edge_rows,\n                )\n            except Exception as exc:\n                prior_metadata["edge_temporal_reconsideration"] = {\n                    "repair_progress": "edge_temporal_reconsideration_validation_failed",\n                    "error": f"{type(exc).__name__}: {exc}",\n                }\n            else:\n                if _audit_ok(env, edge_audit):\n                    diagnostics = copy.deepcopy(\n                        edge_candidate.get("_edge_temporal_reconsideration_diagnostics")\n                        or {}\n                    )\n                    diagnostics.update({\n                        "repair_progress": "edge_temporal_reconsideration_passed_strict_audit",\n                        "final_audit": copy.deepcopy(edge_audit),\n                        "prior_repair": copy.deepcopy(prior_metadata),\n                        "recovery_usage": {\n                            "scoped_repair": 1,\n                            "edge_temporal_reconsideration": 1,\n                            "evidence_regroup": 0,\n                            "shot_regeneration": 0,\n                            "final_strict_audit": 1,\n                        },\n                    })\n                    for edge_row in edge_rows:\n                        edge_row["_regroup_recovery_diagnostics"] = copy.deepcopy(diagnostics)\n                    return edge_rows, edge_audit\n                prior_metadata["edge_temporal_reconsideration"] = {\n                    "repair_progress": "edge_temporal_reconsideration_failed_strict_audit",\n                    "audit": copy.deepcopy(edge_audit),\n                }\n\n    _stage04_progress(\n'''
    text = replace_once(text, old, new, "edge recovery insertion")

    old = '''                audit_fn=audit_fn,\n                prior_metadata=copy.deepcopy(exc.metadata),\n            )\n'''
    new = '''                audit_fn=audit_fn,\n                prior_metadata=copy.deepcopy(exc.metadata),\n                current_rows=[],\n            )\n'''
    text = replace_once(text, old, new, "initial insufficient recovery call")

    old = '''                audit_fn=audit_fn,\n                prior_metadata=last_repair_metadata,\n            )\n'''
    new = '''                audit_fn=audit_fn,\n                prior_metadata=last_repair_metadata,\n                current_rows=rows,\n            )\n'''
    text = replace_once(text, old, new, "post-audit recovery call")

    old = '            f"Beat {target_order} evidence regroup 无可用前向相邻证据",\n'
    new = '            f"Beat {target_order} evidence regroup 无可用前序相邻证据",\n'
    text = replace_once(text, old, new, "edge regroup error wording")

    RUNTIME.write_text(text, encoding="utf-8")

    if TEST.exists():
        raise SystemExit(f"{TEST} already exists")
    TEST.write_text(r'''from __future__ import annotations

import copy
import unittest
from unittest import mock

from app import stage04_v238_runtime as runtime


def _anchor(text: str) -> dict:
    return {
        "id": "E001",
        "text": text,
        "beat_order": 1,
        "source_start": 0,
        "source_end": len(text),
    }


def _beat(text: str) -> dict:
    return {
        "order": 1,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": "E001", "start": 0, "end": len(text), "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _next_beat() -> dict:
    text = "途中遇到守护神兽。"
    return {
        "order": 2,
        "summary": text,
        "state_change": "少年从寻找推进到遇到守护神兽。",
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": "C02", "start": 13, "end": 22, "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _observable_row(text: str) -> dict:
    return {
        "title": "雪山寻剑",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": "observable_transition",
        "temporal_mode_reason": "the source contains an observable activity",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": "少年在雪山寻找失落古剑。",
        "narrative_state": "少年仍在雪山寻找失落古剑。",
        "narrative_end_state": "少年继续在雪山寻找失落古剑。",
        "visual_realization": "",
        "realization_scope": "presentation_only",
        "realization_assumptions": [],
        "visual_start_frame": "",
        "representative_frame": "",
        "visual_end_frame": "",
        "visual_motion": "",
        "video_start_state": "少年在雪山寻找失落古剑。",
        "representative_state": "少年仍在雪山寻找失落古剑。",
        "video_end_state": "少年继续在雪山寻找失落古剑。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": "E001", "start": 0, "end": len(text), "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _static_patch(text: str) -> dict:
    return {
        "patch": {
            "source_fact": text,
            "narrative_state": text,
            "visual_realization": "用构图、景别和镜头运动呈现正在持续的寻剑状态，剧情事实不变。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["只改变构图与镜头运动，不新增寻剑结果"],
            "visual_start_frame": "雪山环境中景，少年处于寻剑状态。",
            "representative_frame": "镜头轻推，少年仍处于同一寻剑状态。",
            "visual_end_frame": "较紧景别停住，寻剑状态持续。",
            "visual_motion": "镜头缓慢推近后停住。",
        }
    }


class FirstBeatTemporalReconsiderationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_beat_stable_ongoing_activity_reclassifies_without_future_evidence(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        row = _observable_row(text)
        classification = {
            "temporal_mode": "static_outcome",
            "temporal_mode_reason": "evidence proves an ongoing search state but no internal temporal milestones",
            "temporal_mode_evidence_ids": ["E001"],
        }
        qwen = mock.AsyncMock(side_effect=[
            ({}, classification, {}),
            ({}, _static_patch(text), {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._reconsider_edge_beat_temporal_mode(
                {},
                row=row,
                compact_beats=[_beat(text)],
                anchors=[_anchor(text)],
                context="unit-first-beat",
            )
        self.assertEqual(qwen.await_count, 2)
        self.assertEqual(repaired["temporal_mode"], "static_outcome")
        self.assertEqual(repaired["source_evidence_ids"], ["E001"])
        self.assertEqual(repaired["summary"], text)
        self.assertEqual(repaired["action"], "")
        self.assertEqual(
            {
                repaired["video_start_state"],
                repaired["representative_state"],
                repaired["video_end_state"],
            },
            {text},
        )

    async def test_first_beat_recovery_passes_without_borrowing_next_beat(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        row = _observable_row(text)
        classification = {
            "temporal_mode": "static_outcome",
            "temporal_mode_reason": "the target evidence supports a stable ongoing activity only",
            "temporal_mode_evidence_ids": ["E001"],
        }
        qwen = mock.AsyncMock(side_effect=[
            ({}, classification, {}),
            ({}, _static_patch(text), {}),
        ])

        def forbidden_builder(**_kwargs):
            raise AssertionError("first-beat edge recovery must not borrow adjacent/future evidence")

        async def audit_fn(**kwargs):
            self.assertEqual(kwargs["shots"][0]["source_evidence_ids"], ["E001"])
            self.assertNotIn("守护神兽", kwargs["shots"][0]["summary"])
            return {"valid": True, "violations": []}

        env = {"_studio_v2371e_batch_evidence": forbidden_builder}
        with mock.patch.object(runtime, "_qwen", qwen):
            rows, audit = await runtime._recover_single_beat_after_scoped_repair(
                env,
                source=text + "\n途中遇到守护神兽。",
                target_beat=_beat(text),
                all_beats=[_beat(text), _next_beat()],
                current_compact_beats=[_beat(text)],
                current_anchors=[_anchor(text)],
                previous_shot=None,
                next_beat=_next_beat(),
                allowed_chars=set(),
                allowed_props=set(),
                scene_id="scene-1",
                episode_id="episode-1",
                audit_fn=audit_fn,
                prior_metadata={"repair_progress": "needs_regrouping_or_evidence_selection"},
                current_rows=[copy.deepcopy(row)],
            )
        self.assertTrue(audit["valid"])
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["source_evidence_ids"], ["E001"])
        self.assertEqual(
            rows[0]["_regroup_recovery_diagnostics"]["repair_progress"],
            "edge_temporal_reconsideration_passed_strict_audit",
        )
        self.assertEqual(
            rows[0]["_regroup_recovery_diagnostics"]["recovery_usage"]["evidence_regroup"],
            0,
        )

    async def test_first_beat_observable_no_progress_stays_fail_closed_without_future_borrowing(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        classification = {
            "temporal_mode": "observable_transition",
            "temporal_mode_reason": "classifier still believes the evidence proves a transition",
            "temporal_mode_evidence_ids": ["E001"],
        }
        qwen = mock.AsyncMock(return_value=({}, classification, {}))

        def forbidden_builder(**_kwargs):
            raise AssertionError("no previous Beat means recovery must fail before borrowing future evidence")

        env = {"_studio_v2371e_batch_evidence": forbidden_builder}
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._recover_single_beat_after_scoped_repair(
                    env,
                    source=text + "\n途中遇到守护神兽。",
                    target_beat=_beat(text),
                    all_beats=[_beat(text), _next_beat()],
                    current_compact_beats=[_beat(text)],
                    current_anchors=[_anchor(text)],
                    previous_shot=None,
                    next_beat=_next_beat(),
                    allowed_chars=set(),
                    allowed_props=set(),
                    scene_id="scene-1",
                    episode_id="episode-1",
                    audit_fn=mock.AsyncMock(),
                    prior_metadata={"repair_progress": "needs_regrouping_or_evidence_selection"},
                    current_rows=[_observable_row(text)],
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertIn("无可用前序相邻证据", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")


if __name__ == "__main__":
    main()
