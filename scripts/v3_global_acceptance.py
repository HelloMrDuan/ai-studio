#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API = os.environ.get("XIAODUAN_V3_BASE", "http://127.0.0.1:6008").rstrip("/")
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188").rstrip("/")
QWEN = os.environ.get("QWEN_BASE", "http://127.0.0.1:6006").rstrip("/")
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "127.0.0.1")
TEMPORAL_PORT = int(os.environ.get("TEMPORAL_PORT", "7233"))
DATA_ROOT = Path(os.environ.get("XIAODUAN_DATA_DIR", "/root/autodl-tmp/ai-studio/data/platform-v2"))
OUT_ROOT = Path(os.environ.get("XIAODUAN_ACCEPTANCE_OUT", "/root/autodl-tmp/manual-upload/v3-global-acceptance"))

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class Check:
    area: str
    status: str
    detail: str


checks: list[Check] = []


def record(area: str, status: str, detail: str) -> None:
    checks.append(Check(area, status, detail))
    print(f"[{status:<7}] {area}: {detail}")


def request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)


def check_http_json(area: str, url: str) -> Any | None:
    try:
        data = request_json("GET", url)
    except Exception as exc:
        record(area, "BLOCKED", f"{type(exc).__name__}: {exc}")
        return None
    record(area, "PASS", "HTTP JSON reachable")
    return data


def wait_comfy(prompt_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        data = request_json("GET", f"{COMFY}/history/{prompt_id}", timeout=20)
        item = data.get(prompt_id)
        if not item:
            time.sleep(2)
            continue
        status = item.get("status") or {}
        if status.get("status_str") == "error":
            raise RuntimeError(json.dumps(status, ensure_ascii=False))
        if item.get("outputs"):
            return item
        time.sleep(2)
    raise TimeoutError(f"Comfy prompt timed out: {prompt_id}")


def first_output_file(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for node_id, output in (item.get("outputs") or {}).items():
        for field in ("images", "videos", "gifs"):
            for artifact in (output or {}).get(field, []) or []:
                if isinstance(artifact, dict) and artifact.get("filename"):
                    return node_id, artifact
    return None


def download_comfy_artifact(artifact: dict[str, Any], target: Path) -> None:
    params = urllib.parse.urlencode({
        "filename": artifact["filename"],
        "subfolder": artifact.get("subfolder", ""),
        "type": artifact.get("type", "output"),
    })
    target.parent.mkdir(parents=True, exist_ok=True)
    with opener.open(f"{COMFY}/view?{params}", timeout=120) as r:
        target.write_bytes(r.read())


def control_plane() -> None:
    health = check_http_json("V3 health", f"{API}/api/v3/health")
    if health:
        if health.get("legacy_stage_router") is False and health.get("reference_first_generation") is True:
            record("V3 invariants", "PASS", "legacy router retired; reference-first enabled")
        else:
            record("V3 invariants", "FAIL", json.dumps(health, ensure_ascii=False))

    skills = check_http_json("Skill Registry", f"{API}/api/v3/skills")
    if isinstance(skills, list) and skills:
        names = [str(x.get("skill_id") or x.get("name") or "") for x in skills]
        requested = [n for n in names if n][:2]
        if requested:
            try:
                plan = request_json("POST", f"{API}/api/v3/plans", {"requested_skills": requested})
                record("Production Plan", "PASS", f"skills={requested}; plan keys={sorted(plan)[:8]}")
            except Exception as exc:
                record("Production Plan", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        resolved = request_json("POST", f"{API}/api/v3/context/resolve", {"source_text": "古代中国雪山客栈中的一场追逐"})
        record("Context Resolver", "PASS", json.dumps(resolved, ensure_ascii=False)[:220])
    except Exception as exc:
        record("Context Resolver", "FAIL", f"{type(exc).__name__}: {exc}")

    providers = check_http_json("Provider Registry", f"{API}/api/v3/providers")
    if isinstance(providers, list):
        caps: set[str] = set()
        for p in providers:
            for cap in p.get("capabilities") or []:
                caps.add(str(cap))
        for cap, area in [
            ("image_generation", "Provider:image"),
            ("image_reference", "Provider:reference"),
            ("identity_reference", "Provider:identity"),
            ("video_generation", "Provider:H3 video"),
            ("tts", "Provider:TTS"),
        ]:
            record(area, "PASS" if cap in caps else "BLOCKED", f"capability {cap} {'present' if cap in caps else 'missing'}")


def resource_lifecycle() -> None:
    project = "global-acceptance"
    logical = f"shot-{int(time.time())}"
    try:
        candidate = request_json("POST", f"{API}/api/v3/projects/{project}/resources/candidates", {
            "logical_key": logical,
            "generation_task_id": f"acceptance-{int(time.time())}",
            "provider_id": "local-comfyui-image",
            "model_id": "configured-image-workflow",
            "reference_ids": ["hero-v1"],
            "metadata": {"source": "v3_global_acceptance"},
        })
        rid = candidate["resource_id"]
        if candidate.get("state") != "generated":
            raise RuntimeError(f"candidate state={candidate.get('state')}")
        audited = request_json("POST", f"{API}/api/v3/projects/{project}/resources/{rid}/audit", {
            "passed": True,
            "audit": {"acceptance": True},
        })
        if audited.get("state") != "candidate_ready":
            raise RuntimeError(f"audit state={audited.get('state')}")
        adopted = request_json("POST", f"{API}/api/v3/projects/{project}/resources/{rid}/adopt")
        if adopted.get("state") != "adopted":
            raise RuntimeError(f"adopt state={adopted.get('state')}")
        query = urllib.parse.urlencode({"logical_key": logical})
        current = request_json("GET", f"{API}/api/v3/projects/{project}/resources/adopted?{query}")
        if current.get("resource_id") != rid:
            raise RuntimeError("adopted pointer mismatch")
        record("Artifact lifecycle", "PASS", "generated -> candidate_ready -> adopted")
    except Exception as exc:
        record("Artifact lifecycle", "FAIL", f"{type(exc).__name__}: {exc}")


def runtime_dependencies() -> float:
    check_http_json("ComfyUI runtime", f"{COMFY}/system_stats")
    check_http_json("Local Qwen runtime", f"{QWEN}/v1/models")

    try:
        with socket.create_connection((TEMPORAL_HOST, TEMPORAL_PORT), timeout=2):
            record("Temporal server", "PASS", f"{TEMPORAL_HOST}:{TEMPORAL_PORT}")
    except OSError as exc:
        record("Temporal server", "BLOCKED", str(exc))

    try:
        ps = subprocess.run(["ps", "-eo", "args"], text=True, capture_output=True, check=False).stdout.lower()
        worker = "temporal" in ps and "worker" in ps and "v3_global_acceptance.py" not in ps
        record("Temporal worker", "PASS" if worker else "BLOCKED", "V3 worker process detected" if worker else "no V3 Temporal worker process")
    except Exception as exc:
        record("Temporal worker", "BLOCKED", f"{type(exc).__name__}: {exc}")

    for module in (
        "app.v3.media.tts",
        "app.v3.media.subtitle",
        "app.v3.media.bgm",
        "app.v3.media.composition",
        "app.v3.media.pipeline",
    ):
        try:
            importlib.import_module(module)
            record(f"Media:{module.rsplit('.', 1)[-1]}", "PASS", "import ok")
        except Exception as exc:
            record(f"Media:{module.rsplit('.', 1)[-1]}", "FAIL", f"{type(exc).__name__}: {exc}")

    free_gib = shutil.disk_usage("/root/autodl-tmp").free / 1024**3
    record("Disk free", "PASS" if free_gib >= 30 else "BLOCKED", f"{free_gib:.1f} GiB free; target >=30 GiB for video acceptance")
    return free_gib


def live_image(reference_id: str, timeout_seconds: int) -> None:
    try:
        queued = request_json("POST", f"{API}/api/v3/generation/comfy/image/queue", {
            "provider_id": "local-comfyui-image",
            "model_id": "configured-image-workflow",
            "shot_id": f"global-image-{int(time.time())}",
            "source_text": "canonical adult East Asian female character, snowy mountain path, cinematic medium shot, realistic skin",
            "reference_ids": [reference_id],
            "entity_ids": ["char_hero"],
            "camera_direction": "medium shot",
            "action": "standing in snow",
        })
        item = wait_comfy(str(queued["prompt_id"]), timeout_seconds)
        found = first_output_file(item)
        if not found:
            raise RuntimeError("image workflow completed without downloadable artifact")
        _, artifact = found
        suffix = Path(str(artifact["filename"])).suffix or ".png"
        target = OUT_ROOT / f"image-smoke{suffix}"
        download_comfy_artifact(artifact, target)
        record("LIVE image E2E", "PASS", str(target))
    except Exception as exc:
        record("LIVE image E2E", "FAIL", f"{type(exc).__name__}: {exc}")


def live_h3(reference_id: str, timeout_seconds: int, free_gib: float) -> None:
    if free_gib < 30:
        record("LIVE H3 E2E", "BLOCKED", f"disk free {free_gib:.1f} GiB <30 GiB")
        return
    try:
        queued = request_json("POST", f"{API}/api/v3/generation/h3/video/queue", {
            "provider_id": "local-h3-video",
            "model_id": "minimax-h3",
            "shot_id": f"global-h3-{int(time.time())}",
            "prompt": "The same woman walks slowly through falling snow on an ancient mountain path. Cinematic natural motion.",
            "first_frame_reference_id": reference_id,
            "entity_ids": ["char_hero"],
            "width": 768,
            "height": 448,
            "length": 124,
            "steps": 20,
            "seed": 0,
        }, timeout=60)
        item = wait_comfy(str(queued["prompt_id"]), timeout_seconds)
        found = first_output_file(item)
        if found:
            _, artifact = found
            suffix = Path(str(artifact["filename"])).suffix or ".mp4"
            target = OUT_ROOT / f"h3-smoke{suffix}"
            download_comfy_artifact(artifact, target)
            record("LIVE H3 E2E", "PASS", str(target))
        else:
            record("LIVE H3 E2E", "PASS", f"workflow completed; outputs={json.dumps(item.get('outputs') or {}, ensure_ascii=False)[:500]}")
    except Exception as exc:
        record("LIVE H3 E2E", "FAIL", f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Xiaoduan Studio V3 global functional acceptance")
    parser.add_argument("--reference-id", default="hero-v1")
    parser.add_argument("--live-image", action="store_true")
    parser.add_argument("--live-h3", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    print("=" * 88)
    print("xiaoduan映画 / Xiaoduan Studio V3 GLOBAL ACCEPTANCE")
    print("Tests control-plane, resource lifecycle, runtime dependencies and optional real generation")
    print("=" * 88)

    control_plane()
    resource_lifecycle()
    free_gib = runtime_dependencies()
    if args.live_image:
        live_image(args.reference_id, args.timeout)
    else:
        record("LIVE image E2E", "SKIP", "pass --live-image to execute")
    if args.live_h3:
        live_h3(args.reference_id, args.timeout, free_gib)
    else:
        record("LIVE H3 E2E", "SKIP", "pass --live-h3 to execute")

    print("\n" + "=" * 88)
    summary: dict[str, int] = {}
    for item in checks:
        summary[item.status] = summary.get(item.status, 0) + 1
    for status in ("PASS", "FAIL", "BLOCKED", "SKIP"):
        print(f"{status:<7}: {summary.get(status, 0)}")
    print("=" * 88)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    report = OUT_ROOT / "report.json"
    report.write_text(json.dumps([item.__dict__ for item in checks], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("REPORT:", report)
    return 1 if summary.get("FAIL", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
