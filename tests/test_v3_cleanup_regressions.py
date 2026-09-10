from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.authoring_execution_timing import AuthoringExecutionTimingFix
from app.v3.canonical_entity_reconciler import CanonicalEntityReconciler
from app.v3.reference_generation_optimization import ReferenceGenerationOptimizer
from app.v3.stage_progress import StageProgressTracker


class _GraphDirector:
    def __init__(self, root: Path) -> None:
        self.production = ProductionAssetService(root)


class _ProgressDirector:
    def __init__(self, project_id: str) -> None:
        self.project = {
            "project_id": project_id,
            "current_stage": "01",
            "completed_stages": [],
            "stage_state": {"01": {"stage_ready": False, "skill_runtime": {"completion": {"ready": False}}}},
        }

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return self.project

    async def message(self, project_id: str, user_text: str, *, native_control_action: str = ""):
        return {"ok": True}

    async def _tracked_llm_chat(self, **kwargs):
        return {"content": "ok"}


class CleanupRegressionTests(unittest.TestCase):
    def test_duplicate_visible_entities_collapse_and_asset_refs_follow_canonical_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director = _GraphDirector(root)
            p = director.production
            first = p.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="story:character:suyao",
                stage="01",
                metadata={"age": 16},
            )
            second = p.create_entity(
                project_id,
                entity_type="character",
                name="苏\u200b瑶",
                logical_key="character:design:suyao",
                stage="02",
                metadata={"appearance": "红衣，高马尾，青玉坠"},
            )
            asset = p.create_text_asset(
                project_id,
                stage="02",
                skill="test",
                logical_key="test:character-output",
                asset_role="character_profile",
                name="苏瑶设定",
                content="红衣，高马尾，青玉坠",
                entity_ids=[second["entity_id"]],
            )

            service = CanonicalEntityReconciler(SimpleNamespace(data_dir=root), director)
            service.install()
            rows = p.list_entities(project_id, "character")

            self.assertEqual(len(rows), 1)
            canonical_id = rows[0]["entity_id"]
            self.assertEqual(rows[0]["name"].replace("\u200b", ""), "苏瑶")
            self.assertEqual(p.get_asset(project_id, asset["asset_id"])["entity_ids"], [canonical_id])
            graph = p.get_graph(project_id)
            aliases = graph.get("entity_aliases") or {}
            self.assertEqual(len(aliases), 1)
            self.assertIn(canonical_id, aliases.values())
            hidden = [
                row for row in (graph.get("entities") or {}).values()
                if (row.get("metadata") or {}).get("merged_duplicate")
            ]
            self.assertEqual(len(hidden), 1)

    def test_completed_elapsed_time_excludes_idle_wait_and_freezes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director = _ProgressDirector(project_id)
            tracker = StageProgressTracker(SimpleNamespace(data_dir=root), director)
            AuthoringExecutionTimingFix(tracker).install()
            tracker.begin(project_id, "01", input_chars=466)

            record = tracker._read(project_id)
            record["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=18)).isoformat()
            record["active_started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=7)).isoformat()
            tracker._write(record)
            tracker.waiting(project_id, "01")

            waiting = tracker.snapshot(project_id)
            self.assertGreaterEqual(waiting["elapsed_seconds"], 6)
            self.assertLess(waiting["elapsed_seconds"], 20)

            tracker.complete(project_id, "01")
            first = tracker.snapshot(project_id)
            second = tracker.snapshot(project_id)
            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["elapsed_seconds"], second["elapsed_seconds"])
            self.assertLess(first["elapsed_seconds"], 20)
            self.assertEqual(first["timing_mode"], "active_execution_only")

    def test_original_workbench_injects_stage_state_overlay(self):
        text = (Path(__file__).resolve().parents[1] / "app" / "v3" / "original_workbench_overlay.py").read_text(encoding="utf-8")
        self.assertIn("stage-state-overlay.js", text)

    def test_frontend_progress_uses_one_stage_poller_and_stable_reference_updates(self):
        static = Path(__file__).resolve().parents[1] / "app" / "v3" / "static"
        progress = (static / "stage-progress-overlay.js").read_text(encoding="utf-8")
        state = (static / "stage-state-overlay.js").read_text(encoding="utf-8")
        refs = (static / "reference-generation-ux-overlay.js").read_text(encoding="utf-8")

        self.assertIn("v3:stage-progress", progress)
        self.assertIn("window.__v3StageProgressLast", progress)
        self.assertNotIn("setInterval(refresh", state)
        self.assertNotIn("/stage-progress`, {cache: 'no-store'}", state)
        self.assertIn("window.addEventListener('v3:stage-progress'", state)

        self.assertNotIn("setInterval(refreshUx", refs)
        self.assertIn("REFS_REFRESH_MS = 5000", refs)
        self.assertIn("syncTerminalCards", refs)
        self.assertIn("Full card rendering is expensive and moves the page", refs)
        self.assertNotIn("document.body, {childList:true, subtree:true}", refs)

    def test_reference_ratio_guard_normalizes_face_anchor_and_turnaround(self):
        face_payload = {
            "capability": "image",
            "params": {
                "reference_phase": "face_anchor",
                "aspect_ratio": "4:5",
                "width": 1024,
                "height": 1280,
                "steps": 36,
            },
        }
        face_target = {"metadata": {"reference_asset": True, "reference_phase": "face_anchor"}}
        normalized_face = ReferenceGenerationOptimizer._normalize_reference_payload(face_payload, face_target)
        self.assertEqual(normalized_face["params"]["aspect_ratio"], "1:1")
        self.assertEqual((normalized_face["params"]["width"], normalized_face["params"]["height"]), (1024, 1024))
        self.assertEqual(face_payload["params"]["aspect_ratio"], "4:5")
        self.assertEqual(face_payload["params"]["height"], 1280)

        turnaround_payload = {
            "capability": "image",
            "params": {
                "reference_phase": "turnaround",
                "aspect_ratio": "16:9",
                "width": 1280,
                "height": 720,
            },
        }
        turnaround_target = {"metadata": {"reference_asset": True, "reference_phase": "turnaround"}}
        normalized_turnaround = ReferenceGenerationOptimizer._normalize_reference_payload(
            turnaround_payload,
            turnaround_target,
        )
        self.assertEqual(normalized_turnaround["params"]["aspect_ratio"], "4:3")
        self.assertEqual(
            (normalized_turnaround["params"]["width"], normalized_turnaround["params"]["height"]),
            (1536, 1152),
        )

        location_payload = {
            "capability": "image",
            "params": {"aspect_ratio": "16:9", "width": 1280, "height": 720},
        }
        location_target = {"metadata": {"reference_asset": True, "reference_kind": "location"}}
        self.assertEqual(
            ReferenceGenerationOptimizer._normalize_reference_payload(location_payload, location_target),
            location_payload,
        )


if __name__ == "__main__":
    unittest.main()
