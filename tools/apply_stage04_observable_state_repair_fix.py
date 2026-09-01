from __future__ import annotations

from pathlib import Path

RUNTIME = Path("app/stage04_v238_runtime.py")
TEST = Path("tests/test_stage04_observable_state_repair.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = RUNTIME.read_text(encoding="utf-8")

    helper = r'''
async def _repair_observable_transition_state_consistency(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    """Repair only collapsed observable-transition state fields.

    Beat/evidence binding, temporal_mode, source fact, entities and timing are
    immutable.  A single evidence-grounded patch repairs the smallest failed
    transition; failure routes to evidence regroup instead of re-sending the
    same full Shot generation payload three times.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "observable_transition":
        return item

    evidence_ids = _id_list(item.get("source_evidence_ids"))
    covered_orders = _orders(item.get("covered_beat_orders"))
    if not evidence_ids or not covered_orders:
        return item

    # Normalize aliases first, then inspect the strict three-state invariant.
    item = _normalize_temporal_contract(
        item,
        evidence_ids=evidence_ids,
        raw_index=1,
    )
    try:
        _assert_temporal_state_distinction(
            item,
            context=f"{context} prevalidation",
        )
        return item
    except Stage04RepairInvariantError as exc:
        initial_error = str(exc)

    keys = {
        field: _semantic_text_key(item.get(field))
        for field in _SHOT_TEMPORAL_STATE_FIELDS
    }
    duplicate_pairs = [
        (left, right)
        for left, right in (
            ("video_start_state", "representative_state"),
            ("representative_state", "video_end_state"),
            ("video_start_state", "video_end_state"),
        )
        if keys.get(left) and keys.get(left) == keys.get(right)
    ]
    if not duplicate_pairs:
        raise

    # Prefer the smallest directional patch.  This exactly covers the current
    # real failure representative_state == video_end_state while preserving the
    # already valid start/core states.
    if duplicate_pairs == [("representative_state", "video_end_state")]:
        repair_fields = ("video_end_state",)
    elif duplicate_pairs == [("video_start_state", "representative_state")]:
        repair_fields = ("representative_state",)
    elif duplicate_pairs == [("video_start_state", "video_end_state")]:
        repair_fields = ("video_end_state",)
    else:
        repair_fields = _SHOT_TEMPORAL_STATE_FIELDS

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
        "failed_rule": "observable_transition_state_consistency",
        "failed_rules": ["state_order", "no_result_duplication"],
        "temporal_mode": "observable_transition",
        "evidence_ids": evidence_ids,
        "beat_id": covered_orders,
        "exact_selected_evidence": copy.deepcopy(exact_evidence),
        "locked_beats": copy.deepcopy(locked_beats),
        "pre_repair_states": _shot_state_snapshot(item),
        "duplicate_pairs": copy.deepcopy(duplicate_pairs),
        "repair_fields": list(repair_fields),
        "pre_repair_error": initial_error,
        "repair_context": context,
    }
    if (
        len(exact_evidence) != len(evidence_ids)
        or len(locked_beats) != len(covered_orders)
    ):
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态塌缩且无法完整锁定 Beat/evidence",
            metadata={
                **metadata_base,
                "repair_progress": "observable_state_repair_scope_incomplete",
                "evidence_sufficiency": "insufficient_for_observable_transition",
            },
        )

    system_prompt = (
        "你是 strict-shot-v2 observable_transition 字段级状态修复器。"
        "temporal_mode=observable_transition、Beat/source evidence、source_fact、summary/action、"
        "entity、duration 全部锁定。只修改 FAILED_STATE_FIELDS。"
        "三个状态必须由 EXACT_SELECTED_EVIDENCE 直接支持，属于同一因果链且严格时间前向。"
        "若只修 video_end_state，start 和 representative 已合法，必须保持不变，只给出证据支持的"
        "更晚 end；若只修 representative_state，则 start/end 锁定，给出二者之间可观察中间态。"
        "不得把下一 Beat 的结果提前写入，不得用抽象阶段词伪造时间推进。"
        "若证据不足以产生所需不同状态，对应 patch 字段返回空字符串；程序将进入 evidence regroup。"
        "只返回严格 JSON patch。"
    )
    current_payload = {
        "source_fact": item.get("source_fact"),
        "summary": item.get("summary"),
        "action": item.get("action"),
        "video_start_state": item.get("video_start_state"),
        "representative_state": item.get("representative_state"),
        "video_end_state": item.get("video_end_state"),
    }
    prompt = (
        "=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(exact_evidence, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(locked_beats, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CURRENT_OBSERVABLE_SHOT ===\n"
        + json.dumps(current_payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== FAILED_STATE_FIELDS ===\n"
        + json.dumps(list(repair_fields), ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== LOCAL_CONTRACT_ERROR ===\n"
        + initial_error
    )
    contract = json.dumps(
        {"patch": {field: "" for field in repair_fields}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        raw, parsed, _ = await _qwen(
            env,
            phase="studio_stage04_observable_state_consistency_repair_qwen32b",
            system_prompt=system_prompt,
            prompt=prompt,
            contract=contract,
            max_tokens=650,
            temperature=0.0,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态字段级修复调用失败：{exc}",
            metadata={
                **metadata_base,
                "repair_progress": "observable_state_repair_call_failed",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "repair_error": str(exc),
            },
        ) from exc

    obj = _parse_object(env, raw, parsed)
    patch = obj.get("patch") if isinstance(obj.get("patch"), dict) else obj
    patch = dict(patch) if isinstance(patch, dict) else {}
    patch = {
        field: str(patch.get(field) or "").strip()
        for field in repair_fields
    }
    if any(not patch[field] for field in repair_fields):
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 现有证据不足以修复状态链",
            metadata={
                **metadata_base,
                "repair_patch": copy.deepcopy(patch),
                "repair_progress": "needs_regrouping_or_evidence_selection",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "regroup_reason": "state repair returned empty evidence-grounded value",
            },
        )

    repaired = copy.deepcopy(item)
    narrative_alias = {
        "video_start_state": "narrative_start_state",
        "representative_state": "narrative_state",
        "video_end_state": "narrative_end_state",
    }
    for field, value in patch.items():
        repaired[field] = value
        repaired[narrative_alias[field]] = value

    changed = any(
        _semantic_text_key(item.get(field))
        != _semantic_text_key(repaired.get(field))
        for field in repair_fields
    )
    if not changed:
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态修复无语义进展",
            metadata={
                **metadata_base,
                "repair_patch": copy.deepcopy(patch),
                "post_repair_states": _shot_state_snapshot(repaired),
                "repair_progress": "rejected_no_progress",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "regroup_reason": "state repair fingerprint did not change",
            },
        )

    try:
        repaired = _normalize_temporal_contract(
            repaired,
            evidence_ids=evidence_ids,
            raw_index=1,
        )
        _assert_temporal_state_distinction(
            repaired,
            context=f"{context} post-repair",
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: observable_transition 状态修复后仍未形成前向时间链：{exc}",
            metadata={
                **metadata_base,
                "repair_patch": copy.deepcopy(patch),
                "post_repair_states": _shot_state_snapshot(repaired),
                "repair_progress": "needs_regrouping_or_evidence_selection",
                "evidence_sufficiency": "insufficient_for_observable_transition",
                "regroup_reason": str(exc),
            },
        ) from exc

    repaired["_observable_state_repair_diagnostics"] = {
        **metadata_base,
        "repair_patch": copy.deepcopy(patch),
        "post_repair_states": _shot_state_snapshot(repaired),
        "repair_progress": "observable_state_repaired",
        "evidence_sufficiency": "sufficient_for_observable_transition",
    }
    return repaired


'''

    marker = "async def _repair_static_outcome_payload_consistency(\n"
    if text.count(marker) != 1:
        raise SystemExit(f"observable helper insertion marker count={text.count(marker)}")
    text = text.replace(marker, helper + marker, 1)

    old = '''    item = await _repair_static_outcome_payload_consistency(\n        env,\n        row=item,\n        compact_beats=[beat],\n        anchors=allowed_anchor_rows,\n        context=f"Beat {target_order} targeted Shot",\n    )\n\n    item = _normalize_temporal_contract(\n'''
    new = '''    item = await _repair_static_outcome_payload_consistency(\n        env,\n        row=item,\n        compact_beats=[beat],\n        anchors=allowed_anchor_rows,\n        context=f"Beat {target_order} targeted Shot",\n    )\n\n    item = await _repair_observable_transition_state_consistency(\n        env,\n        row=item,\n        compact_beats=[beat],\n        anchors=allowed_anchor_rows,\n        context=f"Beat {target_order} targeted Shot",\n    )\n\n    item = _normalize_temporal_contract(\n'''
    text = replace_once(text, old, new, "targeted observable repair hook")

    old = '''            normalized = await _repair_static_outcome_payload_consistency(\n                env,\n                row=normalized,\n                compact_beats=compact_beats,\n                anchors=anchors,\n                context=f"initial Shot#{index}",\n            )\n            rows = validate_rows(\n'''
    new = '''            normalized = await _repair_static_outcome_payload_consistency(\n                env,\n                row=normalized,\n                compact_beats=compact_beats,\n                anchors=anchors,\n                context=f"initial Shot#{index}",\n            )\n            normalized = await _repair_observable_transition_state_consistency(\n                env,\n                row=normalized,\n                compact_beats=compact_beats,\n                anchors=anchors,\n                context=f"initial Shot#{index}",\n            )\n            rows = validate_rows(\n'''
    text = replace_once(text, old, new, "initial observable repair hook")

    old = '''        repaired = await _repair_static_outcome_payload_consistency(\n            env,\n            row=repaired,\n            compact_beats=[compact_beat],\n            anchors=allowed_anchors,\n            context=f"Beat {target_order} regroup regeneration",\n        )\n        repaired_candidates.append(repaired)\n'''
    new = '''        repaired = await _repair_static_outcome_payload_consistency(\n            env,\n            row=repaired,\n            compact_beats=[compact_beat],\n            anchors=allowed_anchors,\n            context=f"Beat {target_order} regroup regeneration",\n        )\n        repaired = await _repair_observable_transition_state_consistency(\n            env,\n            row=repaired,\n            compact_beats=[compact_beat],\n            anchors=allowed_anchors,\n            context=f"Beat {target_order} regroup regeneration",\n        )\n        repaired_candidates.append(repaired)\n'''
    text = replace_once(text, old, new, "regroup observable repair hook")

    old = '''        except Stage04ShotRepairError as exc:\n            if str(exc.metadata.get("evidence_sufficiency") or "") != (\n                "insufficient_visual_evidence"\n            ):\n                raise\n'''
    new = '''        except Stage04ShotRepairError as exc:\n            evidence_sufficiency = str(\n                exc.metadata.get("evidence_sufficiency") or ""\n            )\n            if evidence_sufficiency not in {\n                "insufficient_visual_evidence",\n                "insufficient_for_observable_transition",\n            }:\n                raise\n'''
    text = replace_once(text, old, new, "single beat regroup routing")

    RUNTIME.write_text(text, encoding="utf-8")

    if TEST.exists():
        raise SystemExit(f"{TEST} already exists")
    TEST.write_text(r'''from __future__ import annotations

import copy
import unittest
from unittest import mock

from app import stage04_v238_runtime as runtime


def anchor(text: str) -> dict:
    return {
        "id": "E001",
        "text": text,
        "source_start": 0,
        "source_end": len(text),
    }


def beat(text: str) -> dict:
    return {
        "order": 2,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
    }


def collapsed_dynamic(text: str) -> dict:
    return {
        "title": "向前推进",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": "observable_transition",
        "temporal_mode_reason": "evidence describes a visible forward transition",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": "守护者仍挡在通道前。",
        "narrative_state": "守护者开始侧身让开。",
        "narrative_end_state": "守护者开始侧身让开。",
        "video_start_state": "守护者仍挡在通道前。",
        "representative_state": "守护者开始侧身让开。",
        "video_end_state": "守护者开始侧身让开。",
        "covered_beat_orders": [2],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


class ObservableStateRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_representative_equals_end_repairs_end_only(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        row = collapsed_dynamic(text)
        before = copy.deepcopy(row)
        qwen = mock.AsyncMock(return_value=(
            {},
            {"patch": {"video_end_state": "守护者已经完全让开通道。"}},
            {},
        ))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_observable_transition_state_consistency(
                {}, row=row, compact_beats=[beat(text)], anchors=[anchor(text)],
                context="unit-observable-end",
            )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(repaired["video_start_state"], before["video_start_state"])
        self.assertEqual(repaired["representative_state"], before["representative_state"])
        self.assertEqual(repaired["video_end_state"], "守护者已经完全让开通道。")
        self.assertEqual(repaired["narrative_end_state"], repaired["video_end_state"])
        self.assertEqual(len({repaired[key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS}), 3)

    async def test_valid_observable_does_not_spend_repair_call(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        row = collapsed_dynamic(text)
        row["video_end_state"] = "守护者已经完全让开通道。"
        row["narrative_end_state"] = row["video_end_state"]
        qwen = mock.AsyncMock()
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_observable_transition_state_consistency(
                {}, row=row, compact_beats=[beat(text)], anchors=[anchor(text)],
                context="unit-observable-valid",
            )
        self.assertEqual(qwen.await_count, 0)
        self.assertEqual(repaired["temporal_mode"], "observable_transition")

    async def test_empty_state_patch_routes_to_regroup_without_loop(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        qwen = mock.AsyncMock(return_value=({}, {"patch": {"video_end_state": ""}}, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._repair_observable_transition_state_consistency(
                    {}, row=collapsed_dynamic(text), compact_beats=[beat(text)],
                    anchors=[anchor(text)], context="unit-observable-insufficient",
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(
            captured.exception.metadata["evidence_sufficiency"],
            "insufficient_for_observable_transition",
        )
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "needs_regrouping_or_evidence_selection",
        )

    async def test_missing_beat_real_path_repairs_collapse_instead_of_three_full_retries(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        generation = {"shots": [collapsed_dynamic(text)]}
        repair = {"patch": {"video_end_state": "守护者已经完全让开通道。"}}
        qwen = mock.AsyncMock(side_effect=[({}, generation, {}), ({}, repair, {})])
        with (
            mock.patch.object(runtime, "_qwen", qwen),
            mock.patch.object(
                runtime,
                "_select_targeted_evidence_ids",
                mock.AsyncMock(return_value=(["E001"], "test-evidence")),
            ),
            mock.patch.object(
                runtime,
                "_plan_targeted_duration",
                mock.AsyncMock(return_value=(3.0, "test-duration")),
            ),
        ):
            rows = await runtime._generate_missing_beat_shots(
                {},
                missing_orders=[2],
                compact_beats=[beat(text)],
                anchors=[anchor(text)],
                previous_shot=None,
                next_beat=None,
                allowed_chars=set(),
                allowed_props=set(),
                scene_id="scene-1",
                episode_id="episode-1",
            )
        self.assertEqual(qwen.await_count, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_end_state"], "守护者已经完全让开通道。")
        self.assertEqual(len({rows[0][key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS}), 3)


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")


if __name__ == "__main__":
    main()
