#!/usr/bin/env python3
"""Real Stage04 acceptance verifier for V2.39.6.3.

The only mutating request is the explicitly confirmed Stage04 rebuild.  This
script never calls image, video, media-edit, FaceFusion or Stage06 endpoints.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
CONTRACT = "strict-shot-v2"
MODEL_ID = "qwen3-32b-abliterated"
MODEL_ALIAS = "qwen3-32b"
CONFIRMATION = "REBUILD_STAGE04_ONLY"
CALL_LIMITS = {"simple": 24, "normal": 60, "complex": 120}


class VerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url.rstrip("/") + path, data=data, headers=headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except Exception:
            value = {"raw": raw}
        return exc.code, value


def canonical_shot_payload(shot: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "shot_id", "scene_id", "episode_id", "global_order", "title", "summary",
        "duration_seconds", "composition", "shot_size", "camera", "camera_move",
        "action", "performance", "environment", "dialogue", "narration", "sound",
        "music", "continuity", "representative_state", "video_start_state",
        "video_end_state", "image_prompt", "video_start_prompt", "video_prompt",
        "covered_beat_orders", "source_provenance", "character_entity_ids",
        "prop_entity_ids", "batch_audit", "narrative_audit", "scene_global_audit",
        "forward_overlap_audit", "stage04_contract_version", "text_model_policy",
        "runtime_version",
    )
    return {field: shot.get(field) for field in fields}


def fingerprint(shot: dict[str, Any]) -> str:
    raw = json.dumps(canonical_shot_payload(shot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def media_ids(snapshot: dict[str, Any]) -> set[str]:
    return {
        str(asset.get("asset_id") or "")
        for asset in (snapshot.get("assets") or [])
        if str(asset.get("asset_type") or "").upper() in {"IMAGE", "VIDEO"}
    }


def _phase_count(scene: dict[str, Any], *needles: str) -> int:
    return sum(
        int(value or 0)
        for name, value in (scene.get("phase_calls") or {}).items()
        if any(needle.lower() in str(name).lower() for needle in needles)
    )


def _phase_count_all(scene: dict[str, Any], *needles: str) -> int:
    return sum(
        int(value or 0)
        for name, value in (scene.get("phase_calls") or {}).items()
        if all(needle.lower() in str(name).lower() for needle in needles)
    )


def validate_acceptance(
    health: dict[str, Any],
    task: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    reload_snapshot: dict[str, Any],
    *,
    scene_class: str,
) -> dict[str, Any]:
    require(health.get("platform") is True, "platform health is false")
    require(str(health.get("version") or "") == VERSION, "platform runtime version mismatch")

    runtime_contract = task.get("runtime_contract") or {}
    require(str(runtime_contract.get("selected_model_id") or "") == MODEL_ID, "selected Qwen model id mismatch")
    require(str(runtime_contract.get("resolved_model") or "") == MODEL_ALIAS, "resolved Qwen alias mismatch")
    require(runtime_contract.get("models") == [MODEL_ALIAS], "/v1/models identity mismatch")
    require(str(runtime_contract.get("response_model") or "") == MODEL_ALIAS, "response.model mismatch")
    require(str(task.get("status") or "") == "completed", "Stage04 rebuild did not complete")
    require(str(task.get("runtime_version") or "") == VERSION, "task runtime version mismatch")

    project = after.get("project") or {}
    stage = ((project.get("stage_state") or {}).get("04") or {}).get("studio_stage04_pipeline") or {}
    require(stage.get("ready") is True and stage.get("coverage_ok") is True, "Stage04 pipeline not ready")
    require(str(stage.get("runtime_version") or "") == VERSION, "pipeline runtime version mismatch")
    require(str(stage.get("stage04_contract_version") or "") == CONTRACT, "pipeline contract mismatch")
    require(str(stage.get("text_model_policy") or "") == MODEL_ALIAS, "pipeline model policy mismatch")

    shots = [row for row in ((after.get("continuity") or {}).get("shots") or []) if not row.get("provisional")]
    reloaded = [row for row in ((reload_snapshot.get("continuity") or {}).get("shots") or []) if not row.get("provisional")]
    require(bool(shots), "no formal Shots persisted")
    require(len(shots) == int(stage.get("formal_shot_count") or 0), "formal Shot count mismatch")
    require(
        [canonical_shot_payload(row) for row in shots]
        == [canonical_shot_payload(row) for row in reloaded],
        "persisted Shot contract changed after reload",
    )

    assets = {str(row.get("asset_id") or ""): row for row in (after.get("assets") or [])}
    storyboard = assets.get(str(stage.get("asset_id") or "")) or {}
    require(
        storyboard.get("active") is not False
        and str(storyboard.get("status") or "").lower() == "ready"
        and str(storyboard.get("dependency_state") or "").lower() != "stale",
        "Stage04 storyboard canonical is not current READY",
    )
    failed_candidates = [
        row for row in (after.get("candidates") or [])
        if str(row.get("status") or "").lower() in {"failed", "cancelled", "rejected", "interrupted"}
    ]
    require(all(not row.get("confirmed_asset_id") for row in failed_candidates), "failed candidate polluted canonical")

    entity_rows = {str(row.get("entity_id") or ""): row for row in (after.get("entities") or [])}
    relations = after.get("relations") or []
    orders: list[int] = []
    beat_bindings: list[tuple[str, int]] = []
    last_evidence_start_by_scene: dict[str, int] = {}
    for shot in sorted(shots, key=lambda row: int(row.get("global_order") or 0)):
        sid = str(shot.get("shot_id") or "")
        require(sid and str(shot.get("scene_id") or ""), "Shot identity missing")
        require(str(shot.get("runtime_version") or "") == VERSION, f"{sid}: runtime mismatch")
        require(str(shot.get("stage04_contract_version") or "") == CONTRACT, f"{sid}: contract mismatch")
        require(str(shot.get("text_model_policy") or "") == MODEL_ALIAS, f"{sid}: model policy mismatch")
        for field in ("representative_state", "video_start_state", "video_end_state"):
            require(bool(str(shot.get(field) or "").strip()), f"{sid}: empty {field}")
        start = str(shot.get("video_start_state") or "").strip()
        end = str(shot.get("video_end_state") or "").strip()
        require(str(shot.get("image_prompt") or "").strip() == str(shot.get("representative_state") or "").strip(), f"{sid}: image prompt drift")
        require(str(shot.get("video_start_prompt") or "").strip() == start, f"{sid}: video-start prompt drift")
        require(str(shot.get("video_prompt") or "").strip() == f"起始状态：{start}\n结束状态：{end}", f"{sid}: motion prompt drift")

        provenance = shot.get("source_provenance") or {}
        evidence = provenance.get("source_evidence") or []
        spans = provenance.get("source_evidence_spans") or []
        require(bool(evidence) and bool(spans), f"{sid}: evidence closure missing")
        span_pairs = [(int(row.get("start") or 0), int(row.get("end") or 0)) for row in spans]
        require(all(end_pos > start_pos >= 0 for start_pos, end_pos in span_pairs), f"{sid}: invalid evidence span")
        require(span_pairs == sorted(span_pairs), f"{sid}: evidence order is not source order")
        scene_id = str(shot.get("scene_id") or "")
        last_evidence_start = int(last_evidence_start_by_scene.get(scene_id, -1))
        require(span_pairs[0][0] >= last_evidence_start, f"{sid}: temporal source lineage reversed")
        last_evidence_start_by_scene[scene_id] = span_pairs[0][0]
        for audit_field in ("batch_audit", "narrative_audit", "scene_global_audit", "forward_overlap_audit"):
            require((shot.get(audit_field) or {}).get("valid") is True, f"{sid}: {audit_field} not PASS")

        covered = [int(value) for value in (shot.get("covered_beat_orders") or [])]
        require(bool(covered) and covered == sorted(set(covered)), f"{sid}: Beat binding invalid")
        beat_bindings.extend((str(shot.get("scene_id") or ""), value) for value in covered)
        orders.append(int(shot.get("global_order") or 0))

        expected_visible = set(shot.get("character_entity_ids") or []) | set(shot.get("prop_entity_ids") or [])
        require(expected_visible <= set(entity_rows), f"{sid}: visible entity is unknown")
        shot_entity = str(shot.get("entity_id") or "")
        related = {
            str(row.get("source_id") or "") for row in relations
            if str(row.get("target_id") or "") == shot_entity and str(row.get("relation_type") or "") == "appears_in"
        }
        require(related == expected_visible, f"{sid}: visible entity relation mismatch")

        mirror = ((entity_rows.get(shot_entity) or {}).get("metadata") or {}).get("continuity") or {}
        for field in (
            "representative_state", "video_start_state", "video_end_state",
            "image_prompt", "video_start_prompt", "video_prompt", "source_provenance",
        ):
            require(mirror.get(field) == shot.get(field), f"{sid}: Stage04→Stage05 mirror mismatch for {field}")

    require(orders == sorted(orders) and len(set(orders)) == len(orders), "Shot order is unstable or duplicate")
    require(len(set(beat_bindings)) == len(beat_bindings), "a Beat is bound to multiple Shots")
    require(media_ids(after) == media_ids(before), "image/video asset was created before Stage04 acceptance")

    performance = task.get("performance") or {}
    required_perf = ("workspace_start_seconds", "qwen_ready_wait_seconds", "total_seconds", "llm_calls", "input_tokens", "output_tokens", "phases", "categories", "repairs", "scenes")
    require(all(key in performance for key in required_perf), "performance counters are incomplete")
    scenes = performance.get("scenes") or []
    completed_indices = {int(row.get("scene_index") or 0) for row in scenes if row.get("status") == "completed"}
    require({1, 2} <= completed_indices, "Scene 1 and Scene 2 were not both completed")

    limit = CALL_LIMITS[scene_class]
    perf_rows = []
    performance_regression = False
    for scene in scenes:
        if scene.get("status") != "completed":
            continue
        calls = int(scene.get("llm_calls") or 0)
        repairs = int(scene.get("repair_calls") or 0)
        abnormal = calls > limit or (calls > 0 and repairs / calls > 0.5)
        performance_regression = performance_regression or abnormal
        perf_rows.append({
            "scene": int(scene.get("scene_index") or 0), "llm_calls": calls,
            "repair_calls": repairs,
            "schema_completion_calls": _phase_count(scene, "schema", "completion"),
            "anchor_repair_calls": _phase_count_all(scene, "anchor", "repair"),
            "membership_repair_calls": _phase_count(scene, "membership"),
            "shot_repair_calls": _phase_count_all(scene, "shot", "repair") + _phase_count(scene, "evidence_locked"),
            "input_tokens": int(scene.get("input_tokens") or 0),
            "output_tokens": int(scene.get("output_tokens") or 0),
            "phase_seconds": scene.get("total_seconds"), "abnormal": abnormal,
        })

    return {
        "semantic": "PASS", "performance_regression": performance_regression,
        "result": "SEMANTIC PASS / PERFORMANCE REGRESSION" if performance_regression else "FULL PASS",
        "runtime_version": VERSION, "shot_count": len(shots),
        "canonical_fingerprints": [fingerprint(row) for row in shots],
        "scene_performance": perf_rows,
        "media_safety_gate": "PASS - no image/video generation invoked",
        "stage05_readiness": "PASS", "stage06_real_e2e": "NOT RUN",
    }


def self_test_fixture() -> tuple[dict, dict, dict, dict, dict]:
    def shot(index: int, scene_id: str, start_pos: int) -> dict:
        start = f"subject {index} at A"
        end = f"subject {index} at B"
        return {
            "shot_id": f"shot_{index}", "entity_id": f"shot_entity_{index}", "scene_id": scene_id,
            "episode_id": "episode_1", "global_order": index, "title": "event", "summary": "event",
            "duration_seconds": 4.0, "composition": "balanced", "shot_size": "medium", "camera": "fixed",
            "camera_move": "none", "action": "change", "performance": "controlled", "environment": "exterior",
            "dialogue": "", "narration": "", "sound": "ambient", "music": "none", "continuity": "continuous",
            "representative_state": f"subject {index} midpoint", "video_start_state": start, "video_end_state": end,
            "image_prompt": f"subject {index} midpoint", "video_start_prompt": start,
            "video_prompt": f"起始状态：{start}\n结束状态：{end}", "covered_beat_orders": [1],
            "source_provenance": {"source_evidence": ["source"], "source_evidence_spans": [{"start": start_pos, "end": start_pos + 10, "text": "source"}]},
            "character_entity_ids": [f"character_{index}"], "prop_entity_ids": [],
            "batch_audit": {"valid": True}, "narrative_audit": {"valid": True},
            "scene_global_audit": {"valid": True}, "forward_overlap_audit": {"valid": True, "required": False},
            "stage04_contract_version": CONTRACT, "text_model_policy": MODEL_ALIAS, "runtime_version": VERSION,
            "provisional": False,
        }

    # Evidence offsets are Scene-relative, so Scene 2 legitimately restarts.
    shots = [shot(1, "scene_1", 10), shot(2, "scene_2", 2)]
    entities = []
    relations = []
    for row in shots:
        entities.extend([
            {"entity_id": row["entity_id"], "metadata": {"continuity": copy.deepcopy(row)}},
            {"entity_id": row["character_entity_ids"][0], "metadata": {}},
        ])
        relations.append({"source_id": row["character_entity_ids"][0], "target_id": row["entity_id"], "relation_type": "appears_in"})
    pipeline = {
        "ready": True, "coverage_ok": True, "runtime_version": VERSION,
        "stage04_contract_version": CONTRACT, "text_model_policy": MODEL_ALIAS,
        "formal_shot_count": 2, "asset_id": "storyboard_1",
    }
    snapshot = {
        "project": {"stage_state": {"04": {"studio_stage04_pipeline": pipeline}}},
        "continuity": {"shots": shots}, "entities": entities, "relations": relations,
        "assets": [{"asset_id": "storyboard_1", "asset_type": "TEXT", "active": True, "status": "ready", "dependency_state": "current"}],
        "candidates": [],
    }
    task = {
        "status": "completed", "runtime_version": VERSION,
        "runtime_contract": {"selected_model_id": MODEL_ID, "resolved_model": MODEL_ALIAS, "models": [MODEL_ALIAS], "response_model": MODEL_ALIAS},
        "performance": {
            "workspace_start_seconds": 1.0, "qwen_ready_wait_seconds": 2.0, "total_seconds": 30.0,
            "llm_calls": 20, "input_tokens": 1000, "output_tokens": 200, "phases": {}, "categories": {}, "repairs": {},
            "scenes": [
                {"scene_index": 1, "status": "completed", "llm_calls": 10, "repair_calls": 2, "input_tokens": 500, "output_tokens": 100, "phase_calls": {}, "total_seconds": 12.0},
                {"scene_index": 2, "status": "completed", "llm_calls": 10, "repair_calls": 2, "input_tokens": 500, "output_tokens": 100, "phase_calls": {}, "total_seconds": 14.0},
            ],
        },
    }
    health = {"platform": True, "version": VERSION}
    return health, task, {"assets": []}, snapshot, copy.deepcopy(snapshot)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:6008")
    parser.add_argument("--project-id")
    parser.add_argument("--confirm")
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--scene-class", choices=sorted(CALL_LIMITS), default="normal")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    try:
        if args.self_test:
            report = validate_acceptance(*self_test_fixture(), scene_class=args.scene_class)
            require(report["result"] == "FULL PASS", "verifier self-test did not fully pass")
            print("VERIFIER SELF-TEST PASS")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        require(args.project_id, "--project-id is required")
        require(args.confirm == CONFIRMATION, f"refusing mutation: pass --confirm {CONFIRMATION}")
        code, health = request_json(args.base_url, "/api/health")
        require(code == 200, f"health failed: HTTP {code}")
        code, before = request_json(args.base_url, f"/api/studio/projects/{args.project_id}")
        require(code == 200, f"project snapshot failed: HTTP {code}")
        code, submitted = request_json(
            args.base_url, f"/api/studio/projects/{args.project_id}/stage04/rebuild-production",
            method="POST", payload={}, timeout=180,
        )
        require(code == 200, f"Stage04 rebuild submission failed: HTTP {code} {submitted}")
        deadline = time.monotonic() + args.timeout
        task = submitted
        while time.monotonic() < deadline:
            code, task = request_json(args.base_url, f"/api/studio/projects/{args.project_id}/stage04/rebuild-production/status")
            require(code == 200, f"Stage04 status failed: HTTP {code}")
            if str(task.get("status") or "") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(5)
        require(str(task.get("status") or "") == "completed", f"Stage04 terminal state: {task}")
        code, after = request_json(args.base_url, f"/api/studio/projects/{args.project_id}")
        require(code == 200, "canonical readback failed")
        code, reload_snapshot = request_json(args.base_url, f"/api/studio/projects/{args.project_id}")
        require(code == 200, "canonical reload readback failed")
        report = validate_acceptance(health, task, before, after, reload_snapshot, scene_class=args.scene_class)
        if args.output:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report["performance_regression"] else 0
    except Exception as exc:
        print(f"VERIFY FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
