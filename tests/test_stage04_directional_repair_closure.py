from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from unittest import mock

from app import stage04_v238_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/v23963_stage04_directional_repair_scene1_beat1.json"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def expanded_evidence() -> tuple[list[dict], list[dict]]:
    anchors = [
        {"id": "E001", "text": "一个少年在雪山寻找失落古剑，", "start": 0, "end": 14},
        {"id": "E002", "text": "途中遇到守护神兽，", "start": 15, "end": 24},
        {"id": "E003", "text": "最终获得认可。", "start": 25, "end": 32},
    ]
    beat = {
        "order": 1,
        "summary": "少年寻剑途中遇到神兽并最终获得认可。",
        "state_change": "少年从寻找古剑推进到与神兽相遇并获得认可。",
        "allowed_source_evidence_ids": ["E001", "E002", "E003"],
        "source_evidence_ids": ["E001", "E002", "E003"],
    }
    return anchors, [beat]


def valid_patch() -> dict:
    return {
        "summary": "少年在雪山寻剑，途中遇到守护神兽并获得认可。",
        "action": "少年寻找古剑、遇见守护神兽，神兽最终认可少年。",
        "video_start_state": "少年正在雪山中寻找失落古剑。",
        "representative_state": "寻剑少年在途中与守护神兽相遇。",
        "video_end_state": "守护神兽已经认可少年。",
    }


class DirectionalRepairRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_real_fixture_initial_three_states_are_rejected(self) -> None:
        row = fixture()["initial_shot"]
        with self.assertRaises(runtime.Stage04RepairInvariantError):
            runtime._assert_temporal_state_distinction(
                row,
                context="fixture",
            )

    def test_three_identical_prompts_are_rejected(self) -> None:
        row = {
            "image_prompt": "同一证据",
            "video_start_prompt": "同一证据",
            "video_prompt": "同一证据",
        }
        with self.assertRaises(runtime.Stage04RepairInvariantError):
            runtime._assert_prompt_projection_distinction(
                row,
                context="fixture",
            )

    def test_prompt_projection_has_three_distinct_purposes(self) -> None:
        row = valid_patch()
        compiled = runtime._compile_prompts_from_states(row)
        self.assertEqual(compiled["image_prompt"], row["representative_state"])
        self.assertEqual(compiled["video_start_prompt"], row["video_start_state"])
        self.assertIn(row["video_start_state"], compiled["video_prompt"])
        self.assertIn(row["video_end_state"], compiled["video_prompt"])
        self.assertNotEqual(compiled["image_prompt"], compiled["video_start_prompt"])
        self.assertNotIn("最终获得认可。", {
            compiled["image_prompt"],
            compiled["video_start_prompt"],
            compiled["video_prompt"],
        })

    def test_empty_or_null_patch_does_not_erase_valid_fields(self) -> None:
        current = {
            **fixture()["initial_shot"],
            **valid_patch(),
        }
        merged = runtime._merge_shot_repair_patch(
            current,
            {
                "summary": "",
                "action": None,
                "video_start_state": "  ",
                "representative_state": None,
                "video_end_state": "",
            },
            writable_fields=(
                "summary",
                "action",
                "video_start_state",
                "representative_state",
                "video_end_state",
            ),
        )
        for field in (
            "summary",
            "action",
            "video_start_state",
            "representative_state",
            "video_end_state",
        ):
            self.assertEqual(merged[field], current[field])

    async def test_real_insufficient_evidence_repair_is_rejected_once(self) -> None:
        data = fixture()
        current = data["initial_shot"]
        calls: list[dict] = []

        async def qwen_call(**kwargs):
            calls.append(kwargs)
            parsed = data["directional_repair_raw"]
            return parsed, parsed, {}

        audit = {
            "valid": False,
            "violations": [
                {"shot_index": 1, "code": code}
                for code in data["failed_rules"]
            ],
        }
        anchor = {
            "id": "E001", "text": "最终获得认可。", "start": 25, "end": 32,
        }
        beat = dict(data["original_beat"])

        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            await runtime._repair_batch(
                {"_studio_v2371a_qwen_call": qwen_call},
                current_rows=[current],
                audit=audit,
                source_window=data["source"],
                anchors=[anchor],
                compact_beats=[beat],
                previous_shot=None,
                next_beat=None,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "needs_regrouping_or_evidence_selection",
        )
        self.assertEqual(
            captured.exception.metadata["pre_repair_states"],
            captured.exception.metadata["post_repair_states"],
        )
        self.assertNotIn("image_prompt", calls[0]["contract"])
        self.assertNotIn("source_evidence_ids", calls[0]["contract"])

    async def test_scoped_repair_preserves_bindings_and_passes_validation(self) -> None:
        data = fixture()
        anchors, beats = expanded_evidence()
        current = dict(data["initial_shot"])
        current["source_evidence_ids"] = ["E001", "E002", "E003"]
        current["source_evidence"] = [row["text"] for row in anchors]
        current["source_evidence_spans"] = [dict(row) for row in anchors]
        current["character_entity_ids"] = ["char-visible"]
        current["prop_entity_ids"] = ["prop-visible"]
        calls: list[dict] = []

        async def qwen_call(**kwargs):
            calls.append(kwargs)
            parsed = {"patch": valid_patch()}
            return parsed, parsed, {}

        audit = {
            "valid": False,
            "violations": [
                {"shot_index": 1, "code": code}
                for code in data["failed_rules"]
            ],
        }
        repaired = await runtime._repair_batch(
            {"_studio_v2371a_qwen_call": qwen_call},
            current_rows=[current],
            audit=audit,
            source_window=data["source"],
            anchors=anchors,
            compact_beats=beats,
            previous_shot=None,
            next_beat=None,
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("=== CURRENT_SHOT ===", calls[0]["messages"][0]["content"])
        self.assertIn("=== FAILED_FIELDS ===", calls[0]["messages"][0]["content"])
        self.assertEqual(repaired[0]["source_evidence_ids"], ["E001", "E002", "E003"])
        self.assertEqual(repaired[0]["covered_beat_orders"], [1])
        self.assertEqual(repaired[0]["character_entity_ids"], ["char-visible"])
        self.assertEqual(repaired[0]["prop_entity_ids"], ["prop-visible"])

        validated = runtime.validate_rows(
            {},
            raw_rows=repaired,
            compact_beats=beats,
            allowed_chars={"char-visible"},
            allowed_props={"prop-visible"},
            anchors=anchors,
            scene_id=data["origin"]["scene_id"],
            episode_id="ep_cdc79344954c6de5",
        )[0]
        runtime._assert_temporal_state_distinction(validated, context="fixture replay")
        runtime._assert_prompt_projection_distinction(validated, context="fixture replay")
        self.assertEqual(validated["image_prompt"], validated["representative_state"])
        self.assertEqual(validated["video_start_prompt"], validated["video_start_state"])
        self.assertIn(validated["video_end_state"], validated["video_prompt"])

        evidence_text = "".join(validated["source_evidence"])
        replay_rules = {
            "evidence_entailment": all(
                fact in evidence_text
                for fact in ("寻找失落古剑", "遇到守护神兽", "获得认可")
            ),
            "no_result_duplication": len({
                runtime._semantic_text_key(validated[field])
                for field in runtime._SHOT_TEMPORAL_STATE_FIELDS
            }) == 3,
            "causal_order": [
                validated["video_start_state"],
                validated["representative_state"],
                validated["video_end_state"],
            ] == [
                valid_patch()["video_start_state"],
                valid_patch()["representative_state"],
                valid_patch()["video_end_state"],
            ],
            "redundant_representation": len({
                runtime._prompt_semantic_key(field, validated[field])
                for field in runtime._SHOT_PROMPT_FIELDS
            }) == 3,
            "representative_state": validated["representative_state"] not in {
                validated["video_start_state"],
                validated["video_end_state"],
            },
        }
        self.assertEqual(replay_rules, {code: True for code in data["failed_rules"]})

    async def test_repair_modifies_only_fields_authorized_by_failed_rules(self) -> None:
        anchors, beats = expanded_evidence()
        current = {
            **fixture()["initial_shot"],
            **valid_patch(),
            "source_evidence_ids": ["E001", "E002", "E003"],
            "character_entity_ids": ["char-visible"],
            "prop_entity_ids": ["prop-visible"],
        }

        async def qwen_call(**_kwargs):
            parsed = {
                "patch": {
                    "video_start_state": "少年刚开始在雪山寻找古剑。",
                    "representative_state": "少年寻剑途中遇到守护神兽。",
                    "video_end_state": "守护神兽最终认可少年。",
                    "summary": "不得覆盖合法摘要",
                    "character_entity_ids": [],
                }
            }
            return parsed, parsed, {}

        repaired = await runtime._repair_batch(
            {"_studio_v2371a_qwen_call": qwen_call},
            current_rows=[current],
            audit={
                "valid": False,
                "violations": [{"shot_index": 1, "code": "causal_order"}],
            },
            source_window=fixture()["source"],
            anchors=anchors,
            compact_beats=beats,
            previous_shot=None,
            next_beat=None,
        )
        self.assertEqual(repaired[0]["summary"], current["summary"])
        self.assertEqual(repaired[0]["character_entity_ids"], ["char-visible"])
        self.assertEqual(repaired[0]["prop_entity_ids"], ["prop-visible"])


class DirectionalRepairActiveMainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import (
            _configure_isolated_paths,
            _install_import_shims,
        )

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")

    def test_real_fixture_short_narrative_lines_remain_source_anchors(self) -> None:
        source = fixture()["source"]
        chunks = self.main._studio_v2372_source_chunks(source, max_chars=3000)
        anchors = self.main._studio_v2372_chunk_anchors(chunks[0])
        self.assertEqual([row["text"] for row in anchors], source.splitlines())
        self.assertEqual([(row["start"], row["end"]) for row in anchors], [
            (0, 14), (15, 24), (25, 32),
        ])

    def test_failed_task_progress_is_concise_but_keeps_raw_detail(self) -> None:
        task = {
            "status": "failed",
            "scene_total": 1,
            "scene_done": 0,
            "message": "raw strict audit JSON",
            "error": "RuntimeError: raw strict audit JSON",
            "failure_metadata": {
                "shot_id": "Shot 1",
                "failed_rules": fixture()["failed_rules"],
            },
        }
        project = {
            "project_id": "a" * 24,
            "status": "active",
            "current_stage": "04",
            "completed_stages": ["01", "02", "03"],
            "stage_state": {"04": {}},
        }
        with mock.patch.object(
            self.main,
            "_studio_v23963_current_stage04_task",
            return_value=task,
        ):
            row = self.main._studio_stage04_progress(project, None)
        self.assertEqual(row["current_item"], "分镜生成失败")
        self.assertIn("Shot 1 语义校验未通过", row["current_action"])
        self.assertEqual(row["error_detail"], task["error"])
        self.assertEqual(len(row["failure_summary"]), 3)

    def test_frontend_keeps_raw_error_behind_details(self) -> None:
        studio = (ROOT / "app/static/studio.html").read_text(encoding="utf-8")
        index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertEqual(index, studio)
        self.assertIn("分镜生成失败", studio)
        self.assertIn("查看详细错误", studio)
        self.assertIn("stageFailureDetails(row)", studio)


if __name__ == "__main__":
    unittest.main()
