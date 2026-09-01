from __future__ import annotations

from pathlib import Path


RUNTIME = Path("app/stage04_v238_runtime.py")
TEST = Path("tests/test_stage04_static_regroup_repair.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    text = RUNTIME.read_text(encoding="utf-8")

    helper = r'''
async def _repair_static_outcome_payload_consistency(
    env: dict[str, Any],
    *,
    row: dict[str, Any],
    compact_beats: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    context: str,
) -> dict[str, Any]:
    """Repair a static_outcome payload that contradicts its own mode.

    temporal_mode, Beat/evidence bindings, entities and timing stay locked.
    Only the static narrative/presentation block may be regenerated from the
    exact selected evidence.  The repair is single-shot and fail-closed.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "static_outcome":
        return item

    evidence_ids = _id_list(item.get("source_evidence_ids"))
    mode_evidence_ids = _id_list(item.get("temporal_mode_evidence_ids"))
    reason = str(item.get("temporal_mode_reason") or "").strip()
    if (
        not evidence_ids
        or not reason
        or not mode_evidence_ids
        or not set(mode_evidence_ids).issubset(set(evidence_ids))
    ):
        # Classification itself is incomplete.  This helper only repairs the
        # payload/mode consistency and must not silently widen its authority.
        return item

    try:
        return _normalize_temporal_contract(
            item,
            evidence_ids=evidence_ids,
            raw_index=1,
        )
    except Stage04ShotRepairError:
        raise
    except Exception as exc:
        if "static_outcome" not in str(exc):
            raise
        initial_error = str(exc)

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
        "failed_rule": "static_outcome_payload_consistency",
        "failed_rules": ["visual_realization"],
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": reason,
        "temporal_mode_evidence_ids": mode_evidence_ids,
        "evidence_ids": evidence_ids,
        "beat_id": covered_orders,
        "exact_selected_evidence": copy.deepcopy(exact_evidence),
        "locked_beats": copy.deepcopy(locked_beats),
        "pre_repair_candidate": copy.deepcopy(item),
        "pre_repair_error": initial_error,
        "repair_context": context,
    }

    if (
        len(exact_evidence) != len(evidence_ids)
        or not covered_orders
        or len(locked_beats) != len(covered_orders)
    ):
        raise Stage04ShotRepairError(
            f"{context}: static_outcome payload 冲突且无法完整锁定 Beat/evidence",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_scope_incomplete",
                "evidence_sufficiency": "undetermined",
            },
        )

    system_prompt = (
        "你是 strict-shot-v2 static_outcome 字段级一致性修复器。"
        "temporal_mode=static_outcome 已锁定，Beat/source evidence/entity/timing 全部不可修改。"
        "当前错误是 Shot 虽被分类为 static_outcome，却在 narrative 字段中伪造了前后变化。"
        "只能根据 EXACT_SELECTED_EVIDENCE 重建静态叙事事实和 presentation-only 表现层。"
        "source_fact 必须是证据直接支持的已成立事实；narrative_state 必须是同一稳定事实。"
        "程序会把 narrative_start_state=narrative_state=narrative_end_state，并把三个 video state "
        "投影为同一 narrative_state，所以你不得输出任何剧情前/中/后过程。"
        "visual_start_frame、representative_frame、visual_end_frame 必须画面可区分，"
        "但差异只能来自构图、机位、镜头运动、环境光影或不改变剧情的微小表现。"
        "visual_motion 只能描述表现层运动；realization_scope 必须精确为 presentation_only。"
        "不得新增角色、道具、动作、因果结果或下一 Beat 事实。只返回严格 JSON patch。"
    )
    prompt = (
        "=== LOCKED_COVERED_BEATS ===\n"
        + json.dumps(locked_beats, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== EXACT_SELECTED_EVIDENCE ===\n"
        + json.dumps(exact_evidence, ensure_ascii=False, separators=(",", ":"))
        + "\n\n=== CURRENT_INVALID_STATIC_PAYLOAD ===\n"
        + json.dumps(
            {
                "source_fact": item.get("source_fact"),
                "summary": item.get("summary"),
                "action": item.get("action"),
                "narrative_start_state": item.get("narrative_start_state"),
                "narrative_state": item.get("narrative_state"),
                "narrative_end_state": item.get("narrative_end_state"),
                "visual_realization": item.get("visual_realization"),
                "visual_start_frame": item.get("visual_start_frame"),
                "representative_frame": item.get("representative_frame"),
                "visual_end_frame": item.get("visual_end_frame"),
                "visual_motion": item.get("visual_motion"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n=== INITIAL_LOCAL_CONTRACT_ERROR ===\n"
        + initial_error
    )
    contract = json.dumps(
        {
            "patch": {
                "source_fact": "",
                "narrative_state": "",
                "visual_realization": "",
                "realization_scope": "presentation_only",
                "realization_assumptions": [],
                "visual_start_frame": "",
                "representative_frame": "",
                "visual_end_frame": "",
                "visual_motion": "",
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        raw, parsed, _ = await _qwen(
            env,
            phase="studio_stage04_static_outcome_payload_repair_qwen32b",
            system_prompt=system_prompt,
            prompt=prompt,
            contract=contract,
            max_tokens=900,
            temperature=0.0,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: static_outcome 字段级一致性修复调用失败：{exc}",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_call_failed",
                "evidence_sufficiency": "undetermined",
                "repair_error": str(exc),
            },
        ) from exc

    obj = _parse_object(env, raw, parsed)
    patch = obj.get("patch") if isinstance(obj.get("patch"), dict) else obj
    patch = dict(patch) if isinstance(patch, dict) else {}
    required_text = (
        "source_fact",
        "narrative_state",
        "visual_realization",
        "realization_scope",
        "visual_start_frame",
        "representative_frame",
        "visual_end_frame",
        "visual_motion",
    )
    missing = [
        field
        for field in required_text
        if not str(patch.get(field) or "").strip()
    ]
    assumptions = patch.get("realization_assumptions")
    if not isinstance(assumptions, list):
        missing.append("realization_assumptions")
    if str(patch.get("realization_scope") or "").strip() != "presentation_only":
        missing.append("realization_scope=presentation_only")
    if missing:
        raise Stage04ShotRepairError(
            f"{context}: static_outcome 字段级修复输出不完整：{', '.join(missing)}",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_invalid_output",
                "evidence_sufficiency": "undetermined",
                "repair_output": copy.deepcopy(patch),
            },
        )

    repaired = copy.deepcopy(item)
    for field in required_text:
        repaired[field] = str(patch.get(field) or "").strip()
    repaired["realization_assumptions"] = [
        str(value).strip()
        for value in assumptions
        if str(value or "").strip()
    ]

    stable = repaired["narrative_state"]
    repaired.update({
        "summary": repaired["source_fact"],
        "action": "",
        "narrative_start_state": stable,
        "narrative_end_state": stable,
        "video_start_state": stable,
        "representative_state": stable,
        "video_end_state": stable,
    })

    try:
        repaired = _normalize_temporal_contract(
            repaired,
            evidence_ids=evidence_ids,
            raw_index=1,
        )
    except Exception as exc:
        raise Stage04ShotRepairError(
            f"{context}: static_outcome 字段级修复后仍未闭合 strict contract：{exc}",
            metadata={
                **metadata_base,
                "repair_progress": "static_payload_repair_rejected",
                "evidence_sufficiency": "undetermined",
                "repair_output": copy.deepcopy(patch),
                "post_repair_candidate": copy.deepcopy(repaired),
                "post_repair_error": str(exc),
            },
        ) from exc

    repaired["_static_outcome_repair_diagnostics"] = {
        **metadata_base,
        "repair_progress": "static_payload_repaired",
        "evidence_sufficiency": "sufficient_for_static_payload_repair",
        "repair_output": copy.deepcopy(patch),
        "post_repair_candidate": copy.deepcopy(repaired),
    }
    return repaired


'''

    marker = "async def _complete_targeted_shot_structure(\n"
    if text.count(marker) != 1:
        raise SystemExit(f"helper insertion marker count={text.count(marker)}")
    text = text.replace(marker, helper + marker, 1)

    old = '''    item = await _repair_invalid_temporal_mode_classification(\n        env,\n        row=item,\n        compact_beats=[beat],\n        anchors=allowed_anchor_rows,\n        context=f"Beat {target_order} targeted Shot",\n    )\n\n    item = _normalize_temporal_contract(\n'''
    new = '''    item = await _repair_invalid_temporal_mode_classification(\n        env,\n        row=item,\n        compact_beats=[beat],\n        anchors=allowed_anchor_rows,\n        context=f"Beat {target_order} targeted Shot",\n    )\n\n    item = await _repair_static_outcome_payload_consistency(\n        env,\n        row=item,\n        compact_beats=[beat],\n        anchors=allowed_anchor_rows,\n        context=f"Beat {target_order} targeted Shot",\n    )\n\n    item = _normalize_temporal_contract(\n'''
    text = replace_once(text, old, new, "targeted static payload repair hook")

    old = '''            normalized = await _repair_invalid_temporal_mode_classification(\n                env,\n                row=normalized,\n                compact_beats=compact_beats,\n                anchors=anchors,\n                context=f"initial Shot#{index}",\n            )\n            rows = validate_rows(\n'''
    new = '''            normalized = await _repair_invalid_temporal_mode_classification(\n                env,\n                row=normalized,\n                compact_beats=compact_beats,\n                anchors=anchors,\n                context=f"initial Shot#{index}",\n            )\n            normalized = await _repair_static_outcome_payload_consistency(\n                env,\n                row=normalized,\n                compact_beats=compact_beats,\n                anchors=anchors,\n                context=f"initial Shot#{index}",\n            )\n            rows = validate_rows(\n'''
    text = replace_once(text, old, new, "initial static payload repair hook")

    old = '''        repaired_candidates.append(\n            await _repair_invalid_temporal_mode_classification(\n                env,\n                row=normalized,\n                compact_beats=[compact_beat],\n                anchors=allowed_anchors,\n                context=f"Beat {target_order} regroup regeneration",\n            )\n        )\n'''
    new = '''        repaired = await _repair_invalid_temporal_mode_classification(\n            env,\n            row=normalized,\n            compact_beats=[compact_beat],\n            anchors=allowed_anchors,\n            context=f"Beat {target_order} regroup regeneration",\n        )\n        repaired = await _repair_static_outcome_payload_consistency(\n            env,\n            row=repaired,\n            compact_beats=[compact_beat],\n            anchors=allowed_anchors,\n            context=f"Beat {target_order} regroup regeneration",\n        )\n        repaired_candidates.append(repaired)\n'''
    text = replace_once(text, old, new, "regroup static payload repair hook")

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
        "order": 1,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
    }


def invalid_static(text: str) -> dict:
    return {
        "title": "既成结果",
        "duration_seconds": 3,
        "summary": text,
        "action": "错误地伪造了动作过程",
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": "evidence states an established result without its transition process",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": "众人尚未认可他的决定。",
        "narrative_state": text,
        "narrative_end_state": "众人最终认可了他的决定。",
        "visual_realization": "中景表现既成关系。",
        "realization_scope": "presentation_only",
        "realization_assumptions": ["使用中景表现稳定结果"],
        "visual_start_frame": "中景静止。",
        "representative_frame": "中景轻微推近。",
        "visual_end_frame": "较紧中景停住。",
        "visual_motion": "镜头轻微推近。",
        "video_start_state": "众人尚未认可他的决定。",
        "representative_state": text,
        "video_end_state": "众人最终认可了他的决定。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def repair_patch(text: str) -> dict:
    return {
        "patch": {
            "source_fact": text,
            "narrative_state": text,
            "visual_realization": "中景锁定既成认可关系，叙事事实保持不变。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["只使用构图与镜头运动呈现既成状态"],
            "visual_start_frame": "中景固定构图，认可关系已经成立。",
            "representative_frame": "镜头缓慢推近，认可关系保持不变。",
            "visual_end_frame": "较紧中景停住，认可关系仍保持不变。",
            "visual_motion": "镜头缓慢推近后停住。",
        }
    }


class StaticOutcomePayloadRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_transition_payload_repairs_only_static_block(self) -> None:
        text = "众人认可了他的决定。"
        row = invalid_static(text)
        before = copy.deepcopy(row)
        qwen = mock.AsyncMock(return_value=({}, repair_patch(text), {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_static_outcome_payload_consistency(
                {},
                row=row,
                compact_beats=[beat(text)],
                anchors=[anchor(text)],
                context="unit-static",
            )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(repaired["temporal_mode"], "static_outcome")
        self.assertEqual(repaired["source_evidence_ids"], before["source_evidence_ids"])
        self.assertEqual(repaired["covered_beat_orders"], before["covered_beat_orders"])
        self.assertEqual(repaired["duration_seconds"], before["duration_seconds"])
        self.assertEqual(repaired["summary"], text)
        self.assertEqual(repaired["action"], "")
        self.assertEqual(
            {repaired[key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )
        self.assertEqual(
            {
                repaired["narrative_start_state"],
                repaired["narrative_state"],
                repaired["narrative_end_state"],
            },
            {text},
        )
        self.assertEqual(
            len({repaired[key] for key in runtime._STATIC_PRESENTATION_FIELDS}),
            3,
        )

    async def test_valid_static_payload_does_not_spend_repair_call(self) -> None:
        text = "众人认可了他的决定。"
        row = invalid_static(text)
        patch = repair_patch(text)["patch"]
        row.update(patch)
        row.update({
            "summary": text,
            "action": "",
            "narrative_start_state": text,
            "narrative_state": text,
            "narrative_end_state": text,
            "video_start_state": text,
            "representative_state": text,
            "video_end_state": text,
        })
        qwen = mock.AsyncMock()
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_static_outcome_payload_consistency(
                {},
                row=row,
                compact_beats=[beat(text)],
                anchors=[anchor(text)],
                context="unit-valid-static",
            )
        self.assertEqual(qwen.await_count, 0)
        self.assertEqual(repaired["temporal_mode"], "static_outcome")

    async def test_regroup_regeneration_repairs_static_transition_before_validate(self) -> None:
        text = "众人认可了他的决定。"
        generation = {"shots": [invalid_static(text)]}
        qwen = mock.AsyncMock(side_effect=[
            ({}, generation, {}),
            ({}, repair_patch(text), {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            rows = await runtime._regenerate_shot_from_reselected_evidence(
                {},
                target_order=1,
                compact_beat=beat(text),
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
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["summary"], text)
        self.assertEqual(
            {rows[0][key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )

    async def test_failed_static_repair_does_not_loop(self) -> None:
        text = "众人认可了他的决定。"
        qwen = mock.AsyncMock(return_value=({}, {"patch": {"source_fact": text}}, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._repair_static_outcome_payload_consistency(
                    {},
                    row=invalid_static(text),
                    compact_beats=[beat(text)],
                    anchors=[anchor(text)],
                    context="unit-fail-static",
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "static_payload_repair_invalid_output",
        )


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")


if __name__ == "__main__":
    main()
