from __future__ import annotations

import base64
import hashlib
import pprint
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliverables/install_ai_studio_v2_39_6_3_stage04_full_pipeline_preflight.py"
BASELINE = {
    "app/main.py": "0c54cb0fc4c5cb09f1d3584b5eec1ee6ff86b208e0a323a6e08447241b957eb3",
    "app/stage04_v238_runtime.py": "17f805fe365fc1ab418ebf97f0461a180c5e583c62b8dca163398a670766947d",
    "app/core/task_store.py": "7d5ad3a4c4ba458dd9de80e5e249848c2951a02bd4453d6759d26c025c9276b8",
    "app/services/production_assets.py": "4e4ca6598e1f55a2802ddcbdae48ed5642a2274daf020cf5889e100019eec1c4",
    "app/services/story_continuity.py": "52b9a0feba2508c1a4aa8c4a04bf591fe37097be5313e1b5160da4fd2eec20cf",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


files = {}
for rel, baseline_sha in BASELINE.items():
    data = (ROOT / rel).read_bytes()
    files[rel] = {
        "baseline_sha256": baseline_sha,
        "target_sha256": sha(data),
        "target_payload": base64.b85encode(zlib.compress(data, 9)).decode("ascii"),
    }

header = '''#!/usr/bin/env python3
"""Transactional cumulative installer for AI Studio V2.39.6.3.

Rollback always reads the exact bytes captured from the live installation.
No baseline source payload is embedded and no business generation API is called.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASELINE_VERSION = "2.39.6.2-stage04-narrative-lineage-closure"
TARGET_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
BASE_URL = "http://127.0.0.1:6008"
ACTIVE = {"starting", "warming", "queued", "switching_gpu", "running", "repairing", "auditing", "persisting", "generating"}
FILES = __FILES__


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target(spec: dict[str, str]) -> bytes:
    data = zlib.decompress(base64.b85decode(spec["target_payload"]))
    require(sha(data) == spec["target_sha256"], "embedded target SHA256 mismatch")
    return data


def request_json(path: str, timeout: float = 20) -> tuple[int, Any]:
    request = urllib.request.Request(BASE_URL + path, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {"raw": exc.read().decode("utf-8", errors="replace")}


def run(command: list[str], timeout: float) -> None:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {command!r}\\n"
            f"stdout={result.stdout[-3000:]}\\nstderr={result.stderr[-3000:]}"
        )


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def parse_data_dir(root: Path) -> Path:
    env_path = root / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith("DATA_DIR="):
                value = raw.split("=", 1)[1].strip().strip('"').strip("'")
                path = Path(value)
                return path if path.is_absolute() else root / path
    return root / "data"


def iter_status_rows(value: Any):
    if isinstance(value, dict):
        if "status" in value:
            yield value
        for child in value.values():
            yield from iter_status_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_status_rows(child)


def check_active_tasks(root: Path) -> None:
    status, projects = request_json("/api/studio/projects")
    require(status == 200 and isinstance(projects, list), "cannot inspect Studio projects")
    for item in projects:
        project_id = str((item or {}).get("project_id") or (item or {}).get("id") or "")
        if not project_id:
            continue
        status, row = request_json(f"/api/studio/projects/{project_id}/stage04/rebuild-production/status")
        require(status == 200, f"cannot inspect Stage04 task: {project_id}")
        require(str((row or {}).get("status") or "").lower() not in ACTIVE, f"active Stage04 task: {project_id}")


def check_active_task_files(root: Path) -> None:
    data_dir = parse_data_dir(root)
    patterns = (
        "stage04_rebuild_tasks/*.json", "tasks/*/task.json", "studio_jobs/*.json",
        "studio_video_edit_jobs/*.json", "director_workbench_candidates/*.json",
    )
    for pattern in patterns:
        for path in data_dir.glob(pattern):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"cannot inspect task state {path}: {exc}") from exc
            for row in iter_status_rows(value):
                require(str(row.get("status") or "").lower() not in ACTIVE, f"active task in {path}")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    temp = path.with_name(path.name + ".v23963.tmp")
    temp.write_bytes(data)
    os.chmod(temp, mode)
    temp.replace(path)


def backup_live(root: Path, backup: Path) -> dict[str, Any]:
    backup.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "baseline_version": BASELINE_VERSION, "target_version": TARGET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(), "files": {},
    }
    for rel, spec in FILES.items():
        source = root / rel
        require(source.is_file(), f"baseline file missing: {rel}")
        data = source.read_bytes()
        require(sha(data) == spec["baseline_sha256"], f"baseline SHA256 mismatch: {rel}")
        mode = os.stat(source).st_mode & 0o777
        destination = backup / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        os.chmod(destination, mode)
        manifest["files"][rel] = {"before_sha256": sha(data), "target_sha256": spec["target_sha256"], "mode": mode}
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    return manifest


def restore_exact_backup(root: Path, backup: Path, manifest: dict[str, Any]) -> None:
    for rel, item in manifest["files"].items():
        data = (backup / rel).read_bytes()
        require(sha(data) == item["before_sha256"], f"backup corrupted: {rel}")
        atomic_write(root / rel, data, int(item["mode"]))
        require(sha((root / rel).read_bytes()) == item["before_sha256"], f"rollback hash mismatch: {rel}")


def stop_platform(root: Path) -> None:
    run(["bash", str(root / "scripts/stop.sh")], 60)
    deadline = time.monotonic() + 20
    while port_open(6008) and time.monotonic() < deadline:
        time.sleep(1)
    require(not port_open(6008), "port 6008 still listening after stop")


def start_and_verify(root: Path, expected_version: str) -> None:
    run(["bash", str(root / "scripts/start.sh")], 120)
    deadline = time.monotonic() + 120
    last = ""
    while time.monotonic() < deadline:
        try:
            status, health = request_json("/api/health", 30)
            if status == 200:
                require(health.get("version") == expected_version, "health runtime version mismatch")
                status, schema = request_json("/openapi.json", 20)
                require(status == 200 and schema.get("info", {}).get("version") == expected_version, "OpenAPI version mismatch")
                return
            last = f"HTTP {status}: {health}"
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"platform health timeout: {last}")


def self_test() -> int:
    for rel, spec in FILES.items():
        data = target(spec)
        if rel.endswith(".py"):
            ast.parse(data.decode("utf-8"), filename=rel)
            compile(data, rel, "exec")
    main_text = target(FILES["app/main.py"]).decode("utf-8")
    runtime_text = target(FILES["app/stage04_v238_runtime.py"]).decode("utf-8")
    for marker in (TARGET_VERSION, "_studio_v23963_persist_stage04_task", "runtime_contract", "shot_contract_fingerprint"):
        require(marker in main_text, f"main target marker missing: {marker}")
    for marker in (TARGET_VERSION, "recover_project_transaction", "audit = batch_audit", "narrative_audit"):
        require(marker in runtime_text, f"runtime target marker missing: {marker}")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        backup = Path(td) / "backup"
        root.mkdir()
        manifest = {"files": {}}
        for index, rel in enumerate(FILES):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            original = f"live-before-{index}".encode()
            path.write_bytes(original)
            destination = backup / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original)
            manifest["files"][rel] = {"before_sha256": sha(original), "mode": 0o644}
            atomic_write(path, b"changed", 0o644)
        restore_exact_backup(root, backup, manifest)
    print("INSTALLER SELF-TEST PASS")
    print("ROLLBACK SIMULATION PASS - exact live backup bytes restored")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/root/autodl-tmp/platform-v2"))
    parser.add_argument("--python", type=Path, default=Path("/root/miniconda3/envs/ai-studio/bin/python"))
    parser.add_argument("--backup-root", type=Path, default=Path("/root/autodl-tmp/ai-studio/backups"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    root = args.root.resolve()
    require(root.is_dir(), f"platform root missing: {root}")
    require(args.python.is_file(), f"platform Python missing: {args.python}")
    status, schema = request_json("/openapi.json")
    require(status == 200 and schema.get("info", {}).get("version") == BASELINE_VERSION, "baseline OpenAPI version mismatch")
    check_active_tasks(root)
    backup = args.backup_root / ("platform-v2-v23963-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    manifest = backup_live(root, backup)
    applied = False
    platform_stopped = False
    try:
        stop_platform(root)
        platform_stopped = True
        # Close the request/stop race: a task admitted after the online guard
        # leaves a durable active record and prevents any file write.
        check_active_task_files(root)
        applied = True
        for rel, spec in FILES.items():
            atomic_write(root / rel, target(spec), int(manifest["files"][rel]["mode"]))
        run([str(args.python), "-m", "py_compile", *[str(root / rel) for rel in FILES if rel.endswith(".py")]], 240)
        start_and_verify(root, TARGET_VERSION)
        for rel, spec in FILES.items():
            require(sha((root / rel).read_bytes()) == spec["target_sha256"], f"target hash readback mismatch: {rel}")
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["result"] = "INSTALLED"
        (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"BACKUP={backup}")
        print("INSTALL PASS; no Stage04 rebuild, image, video or composition task was executed")
        return 0
    except Exception:
        if applied:
            try:
                if port_open(6008):
                    stop_platform(root)
            except Exception as exc:
                print(f"ROLLBACK STOP WARNING: {exc}", file=sys.stderr)
            restore_exact_backup(root, backup, manifest)
        if platform_stopped:
            try:
                start_and_verify(root, BASELINE_VERSION)
            except Exception as exc:
                print(f"ROLLBACK RESTORED FILES BUT RESTART FAILED: {exc}", file=sys.stderr)
        if applied:
            print(f"ROLLBACK COMPLETE FROM EXACT LIVE BACKUP {backup}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INSTALL FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
'''

OUTPUT.write_text(header.replace("__FILES__", pprint.pformat(files, width=120, sort_dicts=True)), encoding="utf-8")
print(OUTPUT)
