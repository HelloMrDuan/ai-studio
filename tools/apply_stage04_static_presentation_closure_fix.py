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
def _ensure_static_presentation_frame_distinction(
    row: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically close static_outcome presentation-frame distinction.

    This helper never changes story facts, Beat/evidence binding, entities or timing.
    It only makes presentation-only frame descriptions distinguishable by shot
    grammar when a model returned empty or semantically identical frames.
    """
    item = copy.deepcopy(row)
    if _shot_temporal_mode(item) != "static_outcome":
        return item

    before = {
        field: str(item.get(field) or "").strip()
        for field in _STATIC_PRESENTATION_FIELDS
    }
    before_keys = {
        _semantic_text_key(before[field])
        for field in _STATIC_PRESENTATION_FIELDS
    }
    if "" not in before_keys and len(before_keys) == len(_STATIC_PRESENTATION_FIELDS):
        return item

    stable_visual = next((
        str(item.get(field) or "").strip()
        for field in (
            "visual_realization",
            "narrative_state",
            "source_fact",
            "summary",
        )
        if str(item.get(field) or "").strip()
    ), "同一已锁定叙事状态")

    prefixes = {
        "visual_start_frame": "远景建立构图",
        "representative_frame": "中景主体构图",
        "visual_end_frame": "较紧景别收束构图",
    }
    for field in _STATIC_PRESENTATION_FIELDS:
        original = before[field] or stable_visual
        item[field] = (
            f"{prefixes[field]}：{original}；"
            "仅改变景别、机位或构图，不改变叙事事实。"
        )

    item["realization_scope"] = "presentation_only"
    assumptions = [
        str(value).strip()
        for value in (item.get("realization_assumptions") or [])
        if str(value).strip()
    ]
    closure_assumption = "三个表现帧仅以景别/机位/构图区分，不代表剧情时间推进"
    if closure_assumption not in assumptions:
        assumptions.append(closure_assumption)
    item["realization_assumptions"] = assumptions
    if not str(item.get("visual_motion") or "").strip():
        item["visual_motion"] = (
            "镜头从远景建立构图过渡到中景主体构图并以较紧景别收束；"
            "全程仅为表现层变化。"
        )

    after_keys = {
        _semantic_text_key(item.get(field))
        for field in _STATIC_PRESENTATION_FIELDS
    }
    if "" in after_keys or len(after_keys) != len(_STATIC_PRESENTATION_FIELDS):
        raise Stage04RepairInvariantError(
            "strict-shot-v2 static_outcome deterministic presentation closure failed",
            metadata={
                "failed_rules": ["visual_realization"],
                "temporal_mode": "static_outcome",
                "before_frames": before,
            },
        )
    item["_static_presentation_closure_diagnostics"] = {
        "repair_progress": "deterministic_static_presentation_closed",
        "before_frames": before,
        "after_frames": {
            field: str(item.get(field) or "")
            for field in _STATIC_PRESENTATION_FIELDS
        },
        "fact_fields_mutated": [],
    }
    return item


'''
    marker = "async def _repair_static_outcome_payload_consistency(\n"
    if text.count(marker) != 1:
        raise SystemExit(f"static repair helper marker count={text.count(marker)}")
    text = text.replace(marker, helper + marker, 1)

    start = text.index("async def _repair_static_outcome_payload_consistency(\n")
    next_marker = "\n\nasync def _reconsider_edge_beat_temporal_mode(\n"
    end = text.index(next_marker, start)
    segment = text[start:end]

    old = '''    try:\n        repaired = _normalize_temporal_contract(\n            repaired,\n            evidence_ids=evidence_ids,\n            raw_index=1,\n        )\n    except Exception as exc:\n'''
    new = '''    repaired = _ensure_static_presentation_frame_distinction(repaired)\n    try:\n        repaired = _normalize_temporal_contract(\n            repaired,\n            evidence_ids=evidence_ids,\n            raw_index=1,\n        )\n    except Exception as exc:\n'''
    if segment.count(old) != 1:
        raise SystemExit(f"static post-repair normalization marker count={segment.count(old)}")
    segment = segment.replace(old, new, 1)
    text = text[:start] + segment + text[end:]
    RUNTIME.write_text(text, encoding="utf-8")

    tests = TEST.read_text(encoding="utf-8")
    helper_marker = '''def repair_patch(text: str) -> dict:\n    return {\n        "patch": {\n'''
    if helper_marker not in tests:
        raise SystemExit("repair_patch marker missing")

    insert_before_class = '''\n\nclass StaticOutcomePayloadRepairTests(unittest.IsolatedAsyncioTestCase):\n'''
    duplicate_patch = r'''


def duplicate_frame_repair_patch(text: str) -> dict:
    return {
        "patch": {
            "source_fact": text,
            "narrative_state": text,
            "visual_realization": "保持同一既成叙事状态，只允许表现层变化。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["不增加任何剧情事实"],
            "visual_start_frame": "同一画面保持不变。",
            "representative_frame": "同一画面保持不变。",
            "visual_end_frame": "同一画面保持不变。",
            "visual_motion": "",
        }
    }
'''
    if tests.count(insert_before_class) != 1:
        raise SystemExit(f"test class marker count={tests.count(insert_before_class)}")
    tests = tests.replace(insert_before_class, duplicate_patch + insert_before_class, 1)

    method_marker = '''    async def test_failed_static_repair_does_not_loop(self) -> None:\n'''
    new_test = r'''    async def test_regroup_duplicate_static_frames_close_deterministically_without_extra_qwen(self) -> None:
        text = "众人认可了他的决定。"
        generation = {"shots": [invalid_static(text)]}
        qwen = mock.AsyncMock(side_effect=[
            ({}, generation, {}),
            ({}, duplicate_frame_repair_patch(text), {}),
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
        self.assertEqual(rows[0]["source_fact"], text)
        self.assertEqual(
            {rows[0][key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )
        self.assertEqual(
            len({
                runtime._semantic_text_key(rows[0][key])
                for key in runtime._STATIC_PRESENTATION_FIELDS
            }),
            3,
        )
        self.assertEqual(
            rows[0]["_static_presentation_closure_diagnostics"]["fact_fields_mutated"],
            [],
        )

'''
    if tests.count(method_marker) != 1:
        raise SystemExit(f"test method marker count={tests.count(method_marker)}")
    tests = tests.replace(method_marker, new_test + method_marker, 1)
    TEST.write_text(tests, encoding="utf-8")


if __name__ == "__main__":
    main()
