from __future__ import annotations

import ast
import base64
import copy
import hashlib
import unittest
import zlib
from pathlib import Path

from app import stage04_v238_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (
    ROOT
    / "deliverables"
    / "install_ai_studio_v2_39_6_1_stage04_shot_state_closure.py"
)


def _anchor(anchor_id: str = "C01E001") -> dict:
    return {
        "id": anchor_id,
        "text": "石卵迎风化作一只石猴。",
        "source_start": 10,
        "source_end": 24,
    }


def _beat(order: int = 1, anchor_id: str = "C01E001") -> dict:
    return {
        "order": order,
        "summary": "石猴诞生",
        "state_change": "石卵由静止转为破裂，石猴出现",
        "allowed_source_evidence_ids": [anchor_id],
        "source_evidence_ids": [anchor_id],
    }


def _raw_row(order: int = 1, anchor_id: str = "C01E001") -> dict:
    return {
        "title": "石猴诞生",
        "duration_seconds": 3.2,
        "summary": "石卵裂开，石猴诞生。",
        "action": "石卵裂开，石猴跃出。",
        "representative_state": "裂缝中的石猴正在向外跃出。",
        "video_start_state": "完整石卵静置于山巅。",
        "video_end_state": "石卵已裂，石猴站在山巅。",
        "covered_beat_orders": [order],
        "source_evidence_ids": [anchor_id],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _validate(row: dict) -> dict:
    return runtime.validate_rows(
        {},
        raw_rows=[row],
        compact_beats=[_beat()],
        allowed_chars=set(),
        allowed_props=set(),
        anchors=[_anchor()],
        scene_id="scene-1",
        episode_id="episode-1",
    )[0]


class ShotStateClosureTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_shot_three_states_survive_parse_normalize_and_validation(self):
        calls: list[dict] = []

        async def qwen_call(**kwargs):
            calls.append(kwargs)
            parsed = {"shots": [_raw_row()]}
            return parsed, parsed, {}

        rows = await runtime._generate_rows(
            {"_studio_v2371a_qwen_call": qwen_call},
            prompt="DIRECT",
            scene_index=1,
            scene_total=1,
            batch_index=0,
            batch_total=1,
        )
        normalized, _ = runtime._normalize_raw_shot_binding(
            rows[0],
            compact_beats=[_beat()],
            anchors=[_anchor()],
        )
        validated = _validate(normalized)

        self.assertEqual(
            [validated[field] for field in runtime._SHOT_STATE_FIELDS],
            [_raw_row()[field] for field in runtime._SHOT_STATE_FIELDS],
        )
        self.assertEqual(len(calls), 1)
        for field in runtime._SHOT_STATE_FIELDS:
            self.assertIn(f'"{field}"', calls[0]["contract"])

    async def test_missing_beat_completion_outputs_three_state_contract(self):
        calls: list[dict] = []

        async def qwen_call(**kwargs):
            calls.append(kwargs)
            parsed = {"shots": [_raw_row()]}
            return parsed, parsed, {}

        rows = await runtime._generate_missing_beat_shots(
            {"_studio_v2371a_qwen_call": qwen_call},
            missing_orders=[1],
            compact_beats=[_beat()],
            anchors=[_anchor()],
            previous_shot=None,
            next_beat=None,
            allowed_chars=set(),
            allowed_props=set(),
            scene_id="scene-1",
            episode_id="episode-1",
        )

        self.assertEqual(len(rows), 1)
        self.assertFalse(runtime._missing_shot_state_fields(rows[0]))
        self.assertEqual(len(calls), 1)
        for field in runtime._SHOT_STATE_FIELDS:
            self.assertIn(f'"{field}"', calls[0]["contract"])

    async def test_evidence_repair_preserves_existing_states_when_patch_is_empty(self):
        current = _validate(_raw_row())
        original_states = {
            field: current[field]
            for field in runtime._SHOT_STATE_FIELDS
        }

        async def qwen_call(**_kwargs):
            parsed = {
                "shot": {
                    "summary": "证据收紧后的摘要",
                    "action": "石卵裂开。",
                    "representative_state": "",
                    "video_start_state": None,
                    "video_end_state": "   ",
                    "character_entity_ids": [],
                    "prop_entity_ids": [],
                }
            }
            return parsed, parsed, {}

        repaired = await runtime._repair_batch(
            {"_studio_v2371a_qwen_call": qwen_call},
            current_rows=[current],
            audit={
                "valid": False,
                "violations": [{"shot_index": 1, "type": "evidence"}],
            },
            source_window="",
            anchors=[_anchor()],
            compact_beats=[_beat()],
            previous_shot=None,
            next_beat=None,
        )

        self.assertEqual(
            {field: repaired[0][field] for field in runtime._SHOT_STATE_FIELDS},
            original_states,
        )

    async def test_partial_field_repair_closes_only_missing_state(self):
        current = _raw_row()
        original_start = current["video_start_state"]
        original_representative = current["representative_state"]
        current["video_end_state"] = ""

        merged = runtime._merge_shot_repair_patch(
            current,
            {"video_end_state": "石猴已站稳在山巅。"},
            writable_fields=runtime._SHOT_STATE_FIELDS,
        )

        self.assertEqual(merged["video_start_state"], original_start)
        self.assertEqual(merged["representative_state"], original_representative)
        self.assertEqual(merged["video_end_state"], "石猴已站稳在山巅。")

    async def test_repaired_shot_passes_final_strict_validation(self):
        current = _validate(_raw_row())

        async def qwen_call(**_kwargs):
            parsed = {
                "shot": {
                    "summary": "石卵裂开，石猴跃出。",
                    "action": "石猴从裂开的石卵中跃出。",
                    "representative_state": "石猴正越过石卵裂口。",
                    "video_start_state": "石卵刚出现裂缝。",
                    "video_end_state": "石猴落在石卵旁。",
                    "character_entity_ids": [],
                    "prop_entity_ids": [],
                }
            }
            return parsed, parsed, {}

        repaired = await runtime._repair_batch(
            {"_studio_v2371a_qwen_call": qwen_call},
            current_rows=[current],
            audit={"valid": False, "violations": [{"shot_index": 1}]},
            source_window="",
            anchors=[_anchor()],
            compact_beats=[_beat()],
            previous_shot=None,
            next_beat=None,
        )
        final = _validate(repaired[0])

        self.assertFalse(runtime._missing_shot_state_fields(final))
        self.assertEqual(final["image_prompt"], final["representative_state"])
        self.assertEqual(final["video_start_prompt"], final["video_start_state"])

    async def test_repair_patch_cannot_replace_valid_states_with_empty_values(self):
        current = _raw_row()
        merged = runtime._merge_shot_repair_patch(
            current,
            {
                "representative_state": "",
                "video_start_state": None,
                "video_end_state": "\t",
            },
            writable_fields=runtime._SHOT_STATE_FIELDS,
        )

        for field in runtime._SHOT_STATE_FIELDS:
            self.assertEqual(merged[field], current[field])

    async def test_repair_call_count_is_scoped_to_audit_target(self):
        calls = 0
        first = _validate(_raw_row())
        second = copy.deepcopy(first)
        second["summary"] = "第二个已验收 Shot"

        async def qwen_call(**_kwargs):
            nonlocal calls
            calls += 1
            parsed = {
                "shot": {
                    "summary": "仅修复目标 Shot",
                    "action": "石卵裂开。",
                    "representative_state": "",
                    "video_start_state": "",
                    "video_end_state": "",
                    "character_entity_ids": [],
                    "prop_entity_ids": [],
                }
            }
            return parsed, parsed, {}

        repaired = await runtime._repair_batch(
            {"_studio_v2371a_qwen_call": qwen_call},
            current_rows=[first, second],
            audit={
                "valid": False,
                "violations": [{"shot_index": 1, "type": "evidence"}],
            },
            source_window="",
            anchors=[_anchor()],
            compact_beats=[_beat()],
            previous_shot=None,
            next_beat=None,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(repaired[1], second)
        self.assertFalse(runtime._missing_shot_state_fields(repaired[0]))


class ShotStateStaticRegressionTests(unittest.TestCase):
    def test_v2395_and_v2396_guards_and_new_version_remain_present(self):
        source = ROOT.joinpath(
            "app", "stage04_v238_runtime.py"
        ).read_text(encoding="utf-8")
        for marker in (
            "forward_with_replayed_prefix",
            "source_evidence_spans",
            "await _scene_global_audit",
            "evidence_locked_repair",
            "strict-shot-v2-state-derived",
            "2.39.6.1-stage04-shot-state-closure",
        ):
            self.assertIn(marker, source)


class ShotStateInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INSTALLER.read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.files = ast.literal_eval(next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "FILES"
                for target in node.targets
            )
        ))

    def test_installer_baseline_rollback_and_target_payloads_are_exact(self):
        expected_baselines = {
            "app/main.py": "c43c0778af97a443ba593e32ebb9b71c24a51bec3a226913c491773534976db7",
            "app/stage04_v238_runtime.py": "3d5ece6055f5e3341256818d6e76f480403c0aa946e94316b98d432c11bfa2e7",
        }
        expected_targets = {
            "app/main.py": "799818142338d46b779bd9386a1a3a2efdc2f16630f71a2378bdae07d7c617e2",
            "app/stage04_v238_runtime.py": "04e8a9bc20b2d1143d6d7046e30400fee9c023354588a03ca3c472cf4ca1eaac",
        }
        self.assertEqual(set(self.files), set(expected_baselines))

        for rel, spec in self.files.items():
            baseline = zlib.decompress(
                base64.b85decode(spec["baseline_payload"])
            )
            target = zlib.decompress(
                base64.b85decode(spec["target_payload"])
            )
            self.assertEqual(
                hashlib.sha256(baseline).hexdigest(),
                expected_baselines[rel],
            )
            self.assertEqual(spec["baseline_sha256"], expected_baselines[rel])
            self.assertEqual(
                hashlib.sha256(target).hexdigest(),
                expected_targets[rel],
            )
            self.assertEqual(spec["target_sha256"], expected_targets[rel])

    def test_installer_preserves_cumulative_guards_and_runs_no_business_task(self):
        runtime_target = zlib.decompress(base64.b85decode(
            self.files["app/stage04_v238_runtime.py"]["target_payload"]
        )).decode("utf-8")
        for marker in (
            "V2.39.10.7_SHOT_CONTEXT_COMPACTION",
            "forward_with_replayed_prefix",
            "await _scene_global_audit",
            "_merge_shot_repair_patch",
        ):
            self.assertIn(marker, runtime_target)
        self.assertNotIn('method="POST"', self.source)
        self.assertNotIn("generate-image", self.source)
        self.assertNotIn("generate-video", self.source)
        self.assertIn("baseline SHA256 mismatch", self.source)
        self.assertIn("ROLLBACK COMPLETE", self.source)


if __name__ == "__main__":
    unittest.main()
