from __future__ import annotations

import copy
import ast
import asyncio
import importlib
import json
import tempfile
import unittest
import base64
import hashlib
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import stage04_v238_runtime as runtime
from app.services.production_assets import ProductionAssetService


ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"


def formal_shot() -> dict:
    return {
        "shot_id": "shot_generic_001", "scene_id": "scene_generic_001",
        "episode_id": "episode_generic_001", "global_order": 1,
        "title": "event", "summary": "observable event", "duration_seconds": 4.0,
        "composition": "balanced", "shot_size": "medium", "camera": "fixed",
        "camera_move": "none", "action": "subject changes position",
        "performance": "controlled", "environment": "exterior",
        "dialogue": "", "narration": "", "sound": "ambient", "music": "none",
        "continuity": "continuous",
        "representative_state": "subject at the midpoint",
        "video_start_state": "subject at position A",
        "video_end_state": "subject at position B",
        "image_prompt": "subject at the midpoint",
        "video_start_prompt": "subject at position A",
        "video_prompt": "起始状态：subject at position A\n结束状态：subject at position B",
        "covered_beat_orders": [1],
        "source_provenance": {
            "source_evidence_ids": ["anchor_001"],
            "source_evidence": [{"anchor_id": "anchor_001", "source_start": 10, "source_end": 30}],
        },
        "character_entity_ids": ["character_001"], "prop_entity_ids": [],
        "stage04_contract_version": "strict-shot-v2", "text_model_policy": "qwen3-32b",
        "runtime_version": TARGET_VERSION,
        "batch_audit": {"valid": True}, "narrative_audit": {"valid": True},
        "scene_global_audit": {"valid": True}, "forward_overlap_audit": {"valid": True, "required": False},
    }


class FakeProduction:
    def __init__(self, root: Path) -> None:
        self.root = root / "production"

    def _project_dir(self, project_id: str) -> Path:
        path = self.root / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _graph_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "graph.json"


class FakeDirector:
    def __init__(self, root: Path, production: FakeProduction) -> None:
        self.root = root / "projects"
        self.production = production

    def _project_path(self, project_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{project_id}.json"


class FakeContinuity:
    def __init__(self, root: Path) -> None:
        self.root = root / "continuity"

    def _path(self, project_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{project_id}.json"


class FullPipelinePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import _configure_isolated_paths, _install_import_shims

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")

    def test_effective_runtime_and_route_binding(self) -> None:
        self.assertEqual(runtime.VERSION, TARGET_VERSION)
        endpoint = next(
            route.endpoint for route in self.main.app.routes
            if getattr(route, "path", "") == "/api/studio/projects/{project_id}/stage04/rebuild-production"
        )
        self.assertIs(endpoint, self.main.studio_rebuild_stage04_production)
        self.assertIs(self.main._studio_v2371_rebuild_stage04, self.main.__dict__["_studio_v2371_rebuild_stage04"])
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        definitions = [
            node.lineno for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_studio_v2371_rebuild_stage04"
        ]
        self.assertEqual(self.main._studio_v2371_rebuild_stage04.__code__.co_firstlineno, definitions[-1])

    def test_legacy_stage04_generation_is_api_blocked(self) -> None:
        import inspect

        worker = inspect.getsource(self.main._studio_run_stage_job)
        self.assertNotIn("await _studio_stage04_generate_detailed", worker)
        self.assertIn("/stage04/rebuild-production", worker)

    def test_generic_stage04_api_handler_fails_before_task_creation(self) -> None:
        with mock.patch.object(
            self.main.director, "get_project",
            return_value={"status": "active", "current_stage": "04"},
        ), mock.patch.object(self.main, "_studio_active_job", return_value=None):
            with self.assertRaises(self.main.HTTPException) as caught:
                asyncio.run(self.main.studio_run_stage("0123456789abcdef01234567", {"input": "regenerate"}))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("/stage04/rebuild-production", str(caught.exception.detail))

    def test_fingerprint_covers_every_semantic_contract_group(self) -> None:
        base = formal_shot()
        original = self.main._studio_shot_contract_fingerprint(base)
        mutations = {
            "representative_state": "changed representative",
            "video_start_state": "changed start",
            "video_end_state": "changed end",
            "image_prompt": "changed image prompt",
            "video_start_prompt": "changed start prompt",
            "video_prompt": "changed motion prompt",
            "source_provenance": {"source_evidence_ids": ["anchor_002"], "source_evidence": []},
            "runtime_version": "older-runtime",
            "stage04_contract_version": "older-contract",
            "covered_beat_orders": [2],
        }
        for key, value in mutations.items():
            changed = copy.deepcopy(base)
            changed[key] = value
            self.assertNotEqual(original, self.main._studio_shot_contract_fingerprint(changed), key)

    def test_repair_null_and_blank_cannot_erase_valid_semantics(self) -> None:
        current = formal_shot()
        candidate = {
            "summary": None, "action": "   ", "representative_state": "",
            "video_start_state": None, "video_end_state": "updated end",
        }
        merged = runtime._merge_shot_repair_patch(
            current, candidate,
            writable_fields=("summary", "action", "representative_state", "video_start_state", "video_end_state"),
        )
        self.assertEqual(merged["summary"], current["summary"])
        self.assertEqual(merged["action"], current["action"])
        self.assertEqual(merged["representative_state"], current["representative_state"])
        self.assertEqual(merged["video_start_state"], current["video_start_state"])
        self.assertEqual(merged["video_end_state"], "updated end")

    def test_partial_state_repair_recompiles_prompt_closure(self) -> None:
        repaired = runtime._merge_shot_repair_patch(
            formal_shot(), {"video_end_state": "subject at position C"},
            writable_fields=("video_end_state",),
        )
        compiled = runtime._compile_prompts_from_states(repaired)
        self.assertEqual(compiled["image_prompt"], compiled["representative_state"])
        self.assertEqual(compiled["video_start_prompt"], compiled["video_start_state"])
        self.assertEqual(
            compiled["video_prompt"],
            f"起始状态：{compiled['video_start_state']}\n结束状态：{compiled['video_end_state']}",
        )

    def test_stage05_rejects_independently_drifting_prompts(self) -> None:
        row = formal_shot()
        self.main._studio_v2371_require_strict_shot(row)
        for field in ("image_prompt", "video_start_prompt", "video_prompt"):
            broken = copy.deepcopy(row)
            broken[field] = "independent semantic rewrite"
            with self.assertRaises(ValueError):
                self.main._studio_v2371_require_strict_shot(broken)

    def test_old_candidate_cannot_be_confirmed_after_contract_change(self) -> None:
        shot = formal_shot()
        old = copy.deepcopy(shot)
        old["video_end_state"] = "old end"
        target = {
            "asset_role": "shot_keyframe",
            "metadata": {"shot_id": shot["shot_id"], "shot_contract_fingerprint": self.main._studio_shot_contract_fingerprint(old)},
        }
        with mock.patch.object(self.main, "_studio_formal_shot", return_value=shot):
            with self.assertRaisesRegex(ValueError, "旧分镜合同"):
                self.main._studio_publish_confirmed_shot_candidate(
                    project_id="project_generic", candidate_id="candidate_generic", rows=[], row={},
                    target=target, task={}, selected="/files/output.bin",
                )

    def test_stage06_selector_rejects_old_shot_fingerprint(self) -> None:
        shot = formal_shot()
        stale_contract_asset = {
            "asset_id": "asset_old", "active": True, "status": "ready",
            "dependency_state": "current", "asset_role": "shot_clip",
            "metadata": {"shot_id": shot["shot_id"], "video_contract_version": "h3-start-frame-lineage-v2", "shot_contract_fingerprint": "old"},
        }
        with mock.patch.object(self.main, "_studio_formal_shot", return_value=shot), \
             mock.patch.object(self.main, "_studio_current_video_start", return_value={"asset_id": "start"}), \
             mock.patch.object(self.main, "_studio_current_role_asset", return_value=stale_contract_asset):
            self.assertIsNone(self.main._studio_latest_shot_asset("project_generic", shot["shot_id"], "VIDEO"))

    def test_stale_asset_is_excluded_from_context_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = ProductionAssetService(Path(td))
            project_id = "0123456789abcdef01234567"
            graph = service.ensure_project(project_id)
            graph["assets"] = {
                "current": {"asset_id": "current", "active": True, "status": "ready", "dependency_state": "current", "stage": "04"},
                "stale": {"asset_id": "stale", "active": True, "status": "ready", "dependency_state": "stale", "stage": "04"},
            }
            service._save(graph)
            manifest = service.context_manifest(project_id)
            self.assertIn('"asset_id":"current"', manifest)
            self.assertNotIn('"asset_id":"stale"', manifest)

    def test_transaction_journal_recovers_partial_persistence_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            production = FakeProduction(root)
            director = FakeDirector(root, production)
            continuity = FakeContinuity(root)
            env = {"director": director, "story_continuity": continuity}
            project_id = "project_generic"
            paths = [director._project_path(project_id), continuity._path(project_id), production._graph_path(project_id)]
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"before-{index}".encode())
            existing = production._project_dir(project_id) / "existing.bin"
            existing.write_bytes(b"existing")

            runtime._project_transaction_snapshot(env, project_id)
            for path in paths:
                path.write_bytes(b"partial-new")
            (production._project_dir(project_id) / "new-candidate.bin").write_bytes(b"candidate")

            self.assertTrue(runtime.recover_project_transaction(env, project_id))
            for index, path in enumerate(paths):
                self.assertEqual(path.read_bytes(), f"before-{index}".encode())
            self.assertFalse((production._project_dir(project_id) / "new-candidate.bin").exists())
            self.assertFalse(runtime._transaction_journal_path(env, project_id).exists())

    def test_stage04_contract_survives_persist_reload(self) -> None:
        before = formal_shot()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "continuity.json"
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps({"shots": [before]}, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
            after = json.loads(path.read_text(encoding="utf-8"))["shots"][0]
        fields = (
            "shot_id", "scene_id", "covered_beat_orders", "source_provenance",
            "representative_state", "video_start_state", "video_end_state",
            "image_prompt", "video_start_prompt", "video_prompt", "duration_seconds",
            "continuity", "runtime_version", "stage04_contract_version",
        )
        self.assertEqual({k: before[k] for k in fields}, {k: after[k] for k in fields})

    def test_task_store_and_stage04_journals_use_atomic_replace(self) -> None:
        task_source = (ROOT / "app/core/task_store.py").read_text(encoding="utf-8")
        main_source = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn('temp.replace(path)', task_source)
        self.assertIn('temp.replace(path)', main_source)

    def test_post_boundary_audit_attached_to_persisted_rows(self) -> None:
        source = (ROOT / "app/stage04_v238_runtime.py").read_text(encoding="utf-8")
        assigned = source.index("audit = batch_audit")
        attached = source.index('row["source_audit"] = audit', assigned)
        self.assertLess(assigned, attached)

    def test_effective_scene_beat_audit_field_is_consumed(self) -> None:
        main_source = (ROOT / "app/main.py").read_text(encoding="utf-8")
        runtime_source = (ROOT / "app/stage04_v238_runtime.py").read_text(encoding="utf-8")
        self.assertIn('"scene_narrative_audit"', main_source)
        self.assertIn('beat.get("scene_narrative_audit")', runtime_source)

    def test_no_business_specific_rules_in_v23963_production_patch(self) -> None:
        forbidden = ("宴别", "渡海", "猴王", "众猴")
        for path in (ROOT / "app/main.py", ROOT / "app/stage04_v238_runtime.py"):
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, text)

    def test_call_budget_abnormal_guards(self) -> None:
        def abnormal(total: int, repair: int, maximum: int) -> bool:
            return total > maximum or (total > 0 and repair / total > 0.5)

        self.assertTrue(abnormal(77, 31, 60))
        self.assertTrue(abnormal(42, 25, 60))
        self.assertFalse(abnormal(36, 8, 60))

    def test_compact_snapshot_exposes_complete_stage04_contract(self) -> None:
        source = (ROOT / "app/services/story_continuity.py").read_text(encoding="utf-8")
        for field in (
            "representative_state", "video_start_state", "video_end_state",
            "video_start_prompt", "source_provenance", "batch_audit",
            "narrative_audit", "scene_global_audit", "forward_overlap_audit",
            "stage04_contract_version", "runtime_version",
        ):
            self.assertIn(f'"{field}"', source)

    def test_installer_baseline_guard_and_target_payloads(self) -> None:
        installer = ROOT / "deliverables/install_ai_studio_v2_39_6_3_stage04_full_pipeline_preflight.py"
        source = installer.read_text(encoding="utf-8")
        tree = ast.parse(source)
        files = ast.literal_eval(next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "FILES" for target in node.targets)
        ))
        expected_baseline = {
            "app/main.py": "0c54cb0fc4c5cb09f1d3584b5eec1ee6ff86b208e0a323a6e08447241b957eb3",
            "app/stage04_v238_runtime.py": "17f805fe365fc1ab418ebf97f0461a180c5e583c62b8dca163398a670766947d",
            "app/core/task_store.py": "7d5ad3a4c4ba458dd9de80e5e249848c2951a02bd4453d6759d26c025c9276b8",
            "app/services/production_assets.py": "4e4ca6598e1f55a2802ddcbdae48ed5642a2274daf020cf5889e100019eec1c4",
            "app/services/story_continuity.py": "52b9a0feba2508c1a4aa8c4a04bf591fe37097be5313e1b5160da4fd2eec20cf",
        }
        self.assertEqual({rel: spec["baseline_sha256"] for rel, spec in files.items()}, expected_baseline)
        self.assertNotIn("baseline_payload", source)
        for rel, spec in files.items():
            payload = zlib.decompress(base64.b85decode(spec["target_payload"]))
            self.assertEqual(payload, (ROOT / rel).read_bytes())
            self.assertEqual(hashlib.sha256(payload).hexdigest(), spec["target_sha256"])
        self.assertIn("restore_exact_backup", source)

    def test_verifier_has_stage04_media_safety_gate(self) -> None:
        source = (ROOT / "deliverables/verify_ai_studio_v2_39_6_3_real_e2e.py").read_text(encoding="utf-8")
        self.assertIn('CONFIRMATION = "REBUILD_STAGE04_ONLY"', source)
        self.assertNotIn('generate-image"', source)
        self.assertNotIn('generate-video"', source)
        self.assertNotIn('/assemble"', source)
        for marker in ("response_model", "narrative_audit", "scene_global_audit", "performance_regression"):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
