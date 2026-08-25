#!/usr/bin/env python3
"""Transactional StageProgress v2 and Stage02/03 confirm-guard patch.

This installer is intentionally bound to the exported real AutoDL runtime
archive.  Only app/main.py is written.  Gemma, Stage04 runtime, Skills and all
generation functions remain byte-identical to the exported baseline.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict


BASELINE_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
TARGET_VERSION = BASELINE_VERSION
INSTALLER_VERSION = "V2.39.6.3-stage-progress-v2-confirm-guard"
BASE_URL = "http://127.0.0.1:6008"
ROOT_CANDIDATES = (
    Path("/root/autodl-tmp/ai-studio/platform-v2"),
    Path("/root/autodl-tmp/platform-v2"),
)
PYTHON_CANDIDATES = (
    Path("/root/autodl-tmp/envs/ai-studio-platform-v2/bin/python"),
    Path("/root/miniconda3/envs/ai-studio/bin/python"),
)
BASELINE_SHA_MANIFEST = {
    "app/main.py": "35591e0373c37efa62f2b3606c81bff68a34f28bf665dbb7968e80697d085ff1",
    "app/services/gemma.py": "f84fe348213f88d82da87207cb473c05ce6133bdc5e30bbb21d2a98a2d9088d4",
    "app/stage04_v238_runtime.py": "e668321b8eccf9f8adaf02452ffd5c9a0c1f0b890db4ca53ff28bd718fbdf332",
}
TARGET_SHA_MANIFEST = {
    "app/main.py": "eb624465adfaba7e6edfcd8d50308e0b3c529151c08332d18ac38c75e1da675c",
}
WRITE_FILES = ("app/main.py",)
REQUIRED_ROOT_FILES = tuple(Path(rel) for rel in BASELINE_SHA_MANIFEST)
ACTIVE = {
    "starting", "warming", "queued", "switching_gpu", "running",
    "repairing", "auditing", "persisting", "generating",
}
V2_REQUIRED_FIELDS = {
    "stage", "stage_name", "status", "total_steps", "completed_steps",
    "current_step", "current_step_name", "percent", "current_action",
    "eta", "waiting_confirm",
}


# The additive display-only block is compressed to keep the installer compact.
# Its decoded source is validated by AST, functional tests and target SHA.
PROGRESS_BLOCK_ZLIB_BASE64 = (
    "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"
)


IMPORT_OLD = "from pathlib import Path\n\nfrom fastapi"
IMPORT_NEW = "from pathlib import Path\nfrom typing import TypedDict\n\nfrom fastapi"
PROGRESS_ANCHOR = '@app.get("/api/studio/projects")'

SNAPSHOT_PATCHES = (
    (
        "snapshot asset reuse",
        """        graph = director.production.ensure_project(project_id, str(project.get("title") or ""))
        candidates = _wb_sync_candidates(project_id)
        try:
            face_caps = await facefusion.capabilities()""",
        """        graph = director.production.ensure_project(project_id, str(project.get("title") or ""))
        candidates = _wb_sync_candidates(project_id)
        assets_snapshot = director.production.list_assets(project_id)
        try:
            face_caps = await facefusion.capabilities()""",
    ),
    (
        "snapshot progress mapping",
        """        if (
            current_job
            and project.get("status") == "active"
            and str(current_job.get("stage") or "") != stage
        ):
            current_job = None
        try:
            _studio_schedule_continuity(project_id)""",
        """        if (
            current_job
            and project.get("status") == "active"
            and str(current_job.get("stage") or "") != stage
        ):
            current_job = None
        try:
            stage_progress = _studio_stage_progress_snapshot(
                project, current_job, assets_snapshot, candidates,
            )
        except Exception:
            logger.exception("StageProgress v2 display mapping failed: %s", project_id)
            stage_progress = _studio_stage_progress_fallback(project)
        try:
            _studio_schedule_continuity(project_id)""",
    ),
    (
        "snapshot asset response",
        '        return {\n            "project": project,\n            "assets": director.production.list_assets(project_id),\n            "entities":',
        '        return {\n            "project": project,\n            "assets": assets_snapshot,\n            "entities":',
    ),
    (
        "snapshot progress response",
        '            "active_job": current_job,\n            "video_edit_job":',
        '            "active_job": current_job,\n            "stage_progress": stage_progress,\n            "video_edit_job":',
    ),
)

RUN_OLD = r"""        if str(project.get("current_stage") or "") == "04":
            raw = str(payload.get("input") or "").strip()
            normalized = _studio_re.sub(
                r"[\s，。！？!?、；;：:（）()【】\[\]<>《》“”‘’]+", "", raw
            ).lower()
            if normalized not in {
                "通过", "确认", "继续", "下一步", "确认通过",
                "通过继续", "继续下一步", "ok", "okay",
            }:
                raise RuntimeError("Stage04 generation is available only through /stage04/rebuild-production")"""

RUN_NEW = r"""        stage = str(project.get("current_stage") or "")
        raw = str(payload.get("input") or "").strip()
        normalized = _studio_re.sub(
            r"[\s，。！？!?、；;：:（）()【】\[\]<>《》“”‘’]+", "", raw
        ).lower()
        explicit_approval = normalized in {
            "通过", "确认", "继续", "下一步", "确认通过",
            "通过继续", "继续下一步", "ok", "okay",
        }
        if stage in {"02", "03"} and explicit_approval:
            return await studio_confirm_stage(project_id)
        if stage == "04":
            if not explicit_approval:
                raise RuntimeError("Stage04 generation is available only through /stage04/rebuild-production")"""

CONFIRM_OLD = """        if completion.get("ready") is not True:
            raise RuntimeError("当前阶段还没有完成，请先生成本阶段或补齐真实媒体资产：" + str(completion.get("reason") or ""))
        if not str(state.get("handoff") or "").strip():"""
CONFIRM_NEW = """        if completion.get("ready") is not True:
            raise RuntimeError("当前阶段还没有完成，请先生成本阶段或补齐真实媒体资产：" + str(completion.get("reason") or ""))
        if stage not in {"02", "03"} and not str(state.get("handoff") or "").strip():"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    require(count == 1, f"patch anchor {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


def progress_block() -> str:
    block = zlib.decompress(base64.b64decode(PROGRESS_BLOCK_ZLIB_BASE64)).decode("utf-8")
    old = """    if waiting_confirm:
        # Generation is complete; confirmation is a workflow state, not a
        # partially-complete model step.
        completed = total
        step = total
        percent_value = 100
"""
    require(block.count(old) == 1, "StageProgress waiting-confirm patch anchor mismatch")
    return block.replace(old, "", 1)


def transform_main(baseline: bytes) -> bytes:
    require(
        sha(baseline) == BASELINE_SHA_MANIFEST["app/main.py"],
        "baseline SHA256 mismatch: app/main.py",
    )
    text = baseline.decode("utf-8")
    text = replace_once(text, IMPORT_OLD, IMPORT_NEW, "TypedDict import")
    require("class StageProgress(TypedDict):" not in text, "StageProgress already present")
    text = replace_once(
        text, PROGRESS_ANCHOR, progress_block() + PROGRESS_ANCHOR,
        "StageProgress v2 block",
    )
    for label, old, new in SNAPSHOT_PATCHES:
        text = replace_once(text, old, new, label)
    text = replace_once(text, RUN_OLD, RUN_NEW, "Stage02/03 run-stage confirm guard")
    text = replace_once(text, CONFIRM_OLD, CONFIRM_NEW, "Stage02/03 no-Qwen confirm")
    target = text.encode("utf-8")
    require(
        sha(target) == TARGET_SHA_MANIFEST["app/main.py"],
        "target SHA256 construction mismatch: app/main.py",
    )
    return target


def build_target(rel: str, baseline: bytes) -> bytes:
    require(rel == "app/main.py", f"unsupported write target: {rel}")
    return transform_main(baseline)


def function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise RuntimeError(f"function/class not found: {name}")


def validate_target_source(baseline: bytes, target: bytes) -> None:
    require(sha(target) == TARGET_SHA_MANIFEST["app/main.py"], "target SHA mismatch")
    baseline_text = baseline.decode("utf-8")
    target_text = target.decode("utf-8")
    tree = ast.parse(target_text, filename="app/main.py")
    compile(tree, "app/main.py", "exec")
    for marker in (
        "class StageProgress(TypedDict):",
        '"schema_version": "stage-progress-v2"',
        'if stage in {"02", "03"} and explicit_approval:',
        'if stage not in {"02", "03"} and not str(state.get("handoff") or "").strip():',
        '"completed_steps": completed,',
        '"waiting_confirm": bool(waiting_confirm),',
        '"stage_progress": stage_progress,',
    ):
        require(marker in target_text, f"target marker missing: {marker}")
    require(
        function_source(baseline_text, "_studio_stage04_finalize")
        == function_source(target_text, "_studio_stage04_finalize"),
        "Stage04 finalize changed unexpectedly",
    )
    require(
        function_source(baseline_text, "_studio_run_stage_job")
        == function_source(target_text, "_studio_run_stage_job"),
        "generation worker changed unexpectedly",
    )


def selected_namespace(source: str, names: set[str], namespace: dict[str, Any]) -> dict[str, Any]:
    tree = ast.parse(source)
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), "target-fragment", "exec"), namespace)
    return namespace


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def confirm_guard_self_test(target: bytes) -> None:
    source = target.decode("utf-8")
    names = {"studio_confirm_stage", "studio_run_stage"}

    for stage, next_stage in (("02", "03"), ("03", "04")):
        calls = {"message": 0, "confirm": 0, "save": 0}

        class Director:
            def __init__(self):
                self.project = {
                    "project_id": "p" * 24,
                    "status": "active",
                    "current_stage": stage,
                    "stage_state": {
                        stage: {
                            "handoff": "",
                            "skill_runtime": {"completion": {"ready": True}},
                        }
                    },
                }

            def refresh_production_completion(self, project_id: str) -> None:
                return None

            def get_project(self, project_id: str) -> dict:
                return self.project

            async def message(self, *args, **kwargs):
                calls["message"] += 1
                raise AssertionError("Qwen path must not be reached")

            async def confirm_stage(self, project_id: str) -> dict:
                calls["confirm"] += 1
                return {**self.project, "current_stage": next_stage}

        class FailingGPU:
            def use(self, owner):
                raise AssertionError("GPU/Qwen context must not be entered")

        namespace: dict[str, Any] = {
            "director": Director(),
            "gpu": FailingGPU(),
            "GPUOwner": object(),
            "HTTPException": _HTTPException,
            "_studio_re": re,
            "_studio_active_job": lambda project_id: None,
            "_studio_save_job": lambda job: calls.__setitem__("save", calls["save"] + 1),
        }
        selected_namespace(source, names, namespace)
        result = asyncio.run(namespace["studio_run_stage"]("p" * 24, {"input": "通过"}))
        require(result["confirmed_stage"] == stage, f"Stage{stage} confirm result mismatch")
        require(calls == {"message": 0, "confirm": 1, "save": 0}, f"Stage{stage} called generation path: {calls}")


def progress_self_test(target: bytes) -> None:
    source = target.decode("utf-8")
    names = {
        "StageProgress", "_studio_progress_eta", "_studio_progress_row",
        "_studio_core_stage_progress", "_studio_stage04_progress",
        "_studio_stage05_progress", "_studio_stage06_progress",
        "_studio_stage_progress_snapshot", "_studio_stage_progress_fallback",
        "_studio_asset_is_current",
    }
    task_holder: dict[str, Any] = {"value": {}}
    continuity_holder: dict[str, Any] = {"value": {"shots": []}}

    class Continuity:
        @staticmethod
        def load(project_id: str) -> dict:
            return continuity_holder["value"]

    namespace: dict[str, Any] = {
        "TypedDict": TypedDict,
        "_STUDIO_STAGE_LABELS": {
            "01": "剧本", "02": "角色", "03": "视觉",
            "04": "分镜", "05": "制作", "06": "成片",
        },
        "_STUDIO_PROGRESS_PHASES": {
            "01": ("准备剧本事实", "执行剧本 Skill", "校验剧本成果"),
            "02": ("准备角色输入", "生成角色设定", "校验角色成果"),
            "03": ("准备视觉输入", "生成视觉设计", "校验视觉成果"),
        },
        "_studio_datetime": datetime,
        "_studio_timezone": timezone,
        "_studio_v23963_current_stage04_task": lambda *args, **kwargs: task_holder["value"],
        "story_continuity": Continuity(),
    }
    selected_namespace(source, names, namespace)
    project = {
        "project_id": "p" * 24,
        "status": "active",
        "current_stage": "02",
        "completed_stages": ["01"],
        "stage_state": {
            "01": {"skill_runtime": {"completion": {"ready": True}}},
            "02": {"skill_runtime": {"completion": {"ready": False}}},
            "03": {"skill_runtime": {"completion": {"ready": False}}},
            "04": {},
        },
    }
    job = {
        "stage": "02", "status": "running", "turn_count": 1,
        "message": "正在校验角色成果", "created_at": "",
    }
    result = namespace["_studio_stage_progress_snapshot"](project, job, [], [])
    require(result["schema_version"] == "stage-progress-v2", "schema version mismatch")
    require([row["stage"] for row in result["stages"]] == ["01", "02", "03", "04", "05", "06"], "six-stage order mismatch")
    for row in result["stages"]:
        require(V2_REQUIRED_FIELDS.issubset(row), f"Stage{row.get('stage')} fields incomplete")
    stage02 = result["stages"][1]
    require(stage02["completed_steps"] == 2, "Stage02 completed_steps mismatch")
    require(stage02["current_step"] == 3 and stage02["total_steps"] == 3, "Stage02 ordinal mismatch")
    require(stage02["percent"] < 100, "running Stage02 cannot be complete")

    project["current_stage"] = "04"
    task_holder["value"] = {
        "status": "completed", "scene_done": 4, "scene_total": 4,
        "message": "严格分镜重建完成",
    }
    result = namespace["_studio_stage_progress_snapshot"](project, None, [], [])
    stage04 = result["stages"][3]
    require(stage04["status"] == "waiting_confirm", "Stage04 display state mismatch")
    require(stage04["percent"] == 100 and stage04["waiting_confirm"] is True, "Stage04 completion display mismatch")

    continuity_holder["value"] = {
        "shots": [
            {"shot_id": "shot-1", "global_order": 1},
            {"shot_id": "shot-2", "global_order": 2},
        ]
    }
    assets = [{
        "asset_id": "target-1", "active": True, "status": "planned",
        "dependency_state": "current", "metadata": {"shot_id": "shot-1"},
    }]
    candidates = [{
        "target_asset_id": "target-1", "status": "completed",
        "confirmed_asset_id": "",
    }]
    result = namespace["_studio_stage_progress_snapshot"](project, None, assets, candidates)
    stage05 = result["stages"][4]
    require(stage05["waiting_confirm"] is True, "Stage05 candidate confirmation not exposed")
    require(stage05["percent"] < 100, "one candidate cannot complete the entire Stage05")


def root_valid(root: Path) -> bool:
    return root.is_dir() and all((root / rel).is_file() for rel in REQUIRED_ROOT_FILES)


def discover_platform_root(manual: Path | None = None) -> Path:
    checked = []
    candidates = (manual,) if manual is not None else ROOT_CANDIDATES
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        checked.append(str(resolved))
        if root_valid(resolved):
            return resolved
    raise RuntimeError("platform root candidates checked:\n" + "\n".join(checked) + "\nnot found")


def python_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(path), "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=15, check=False,
        )
        return completed.returncode == 0
    except Exception:
        return False


def discover_platform_python(manual: Path | None = None) -> Path:
    candidates = [manual] if manual is not None else [*PYTHON_CANDIDATES, Path(sys.executable)]
    checked = []
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.expanduser().resolve()
        checked.append(str(resolved))
        if python_usable(resolved):
            return resolved
    raise RuntimeError("platform Python candidates checked:\n" + "\n".join(checked) + "\nnot found")


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout, check=False,
    )
    print(completed.stdout, end="")
    require(completed.returncode == 0, f"command failed ({completed.returncode}): {command}")
    return completed


def request_json(path: str, timeout: int = 20) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(BASE_URL + path, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"body": body}
        return exc.code, payload


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def walk_status_rows(value: object):
    if isinstance(value, dict):
        if "status" in value:
            yield value
        for nested in value.values():
            yield from walk_status_rows(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_status_rows(nested)


def check_active_tasks(root: Path) -> None:
    for endpoint in ("/api/studio/stage04/rebuild/tasks", "/api/tasks"):
        try:
            status, payload = request_json(endpoint, 15)
        except Exception:
            continue
        if status == 200:
            for row in walk_status_rows(payload):
                require(str(row.get("status") or "").lower() not in ACTIVE, f"active task reported by {endpoint}")
    data_dir = root / "data"
    for pattern in ("stage04_rebuild_tasks/*.json", "tasks/*/task.json", "studio_jobs/*.json"):
        for path in data_dir.glob(pattern):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for row in walk_status_rows(payload):
                require(str(row.get("status") or "").lower() not in ACTIVE, f"active task in {path}")


def validate_baseline(root: Path) -> dict[str, bytes]:
    values = {}
    for rel, expected in BASELINE_SHA_MANIFEST.items():
        path = root / rel
        require(path.is_file(), f"baseline file missing: {rel}")
        data = path.read_bytes()
        require(sha(data) == expected, f"baseline SHA256 mismatch: {rel}")
        values[rel] = data
    return values


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    temp = path.with_name(path.name + ".stage-progress-v2.tmp")
    temp.write_bytes(data)
    os.chmod(temp, mode)
    temp.replace(path)


def create_backup(root: Path, backup: Path, live: dict[str, bytes]) -> dict[str, Any]:
    backup.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "installer_version": INSTALLER_VERSION,
        "baseline_version": BASELINE_VERSION,
        "target_version": TARGET_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "write_files": list(WRITE_FILES),
        "files": {},
    }
    for rel, data in live.items():
        source = root / rel
        mode = os.stat(source).st_mode & 0o777
        destination = backup / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        os.chmod(destination, mode)
        manifest["files"][rel] = {
            "before_sha256": sha(data), "mode": mode,
            "written": rel in WRITE_FILES,
            "target_sha256": TARGET_SHA_MANIFEST.get(rel, sha(data)),
        }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return manifest


def restore_exact_backup(root: Path, backup: Path, manifest: dict[str, Any]) -> None:
    for rel in WRITE_FILES:
        item = manifest["files"][rel]
        data = (backup / rel).read_bytes()
        require(sha(data) == item["before_sha256"], f"backup corrupted: {rel}")
        atomic_write(root / rel, data, int(item["mode"]))
    for rel, item in manifest["files"].items():
        require(sha((root / rel).read_bytes()) == item["before_sha256"], f"rollback hash mismatch: {rel}")


def validate_openapi(expected: str) -> None:
    status, schema = request_json("/openapi.json", 30)
    require(status == 200, f"OpenAPI HTTP status mismatch: {status}")
    require(schema.get("info", {}).get("version") == expected, "OpenAPI version mismatch")


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
                validate_openapi(expected_version)
                return
            last = f"HTTP {status}: {health}"
        except Exception as exc:
            last = str(exc)
        time.sleep(2)
    raise RuntimeError(f"platform health timeout: {last}")


def self_test(source_root: Path, python: Path) -> None:
    require(WRITE_FILES == ("app/main.py",), "write scope is not main-only")
    baseline = validate_baseline(source_root)
    target = transform_main(baseline["app/main.py"])
    validate_target_source(baseline["app/main.py"], target)
    confirm_guard_self_test(target)
    progress_self_test(target)
    with tempfile.TemporaryDirectory(prefix="stage-progress-v2-selftest-") as td:
        root = Path(td) / "platform-v2"
        backup = Path(td) / "backup"
        for rel, data in baseline.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        live = validate_baseline(root)
        manifest = create_backup(root, backup, live)
        mode = os.stat(root / "app/main.py").st_mode & 0o777
        atomic_write(root / "app/main.py", target, mode)
        run([str(python), "-m", "py_compile", str(root / "app/main.py")], 120)
        require(sha((root / "app/main.py").read_bytes()) == TARGET_SHA_MANIFEST["app/main.py"], "self-test target readback mismatch")
        require(sha((root / "app/services/gemma.py").read_bytes()) == BASELINE_SHA_MANIFEST["app/services/gemma.py"], "self-test modified gemma.py")
        require(sha((root / "app/stage04_v238_runtime.py").read_bytes()) == BASELINE_SHA_MANIFEST["app/stage04_v238_runtime.py"], "self-test modified Stage04 runtime")
        restore_exact_backup(root, backup, manifest)
        validate_baseline(root)
    print("SELF-TEST PASS")


def install(root: Path, python: Path, backup: Path, *, skip_restart: bool) -> None:
    require(WRITE_FILES == ("app/main.py",), "write scope is not main-only")
    live = validate_baseline(root)
    target = transform_main(live["app/main.py"])
    validate_target_source(live["app/main.py"], target)
    confirm_guard_self_test(target)
    progress_self_test(target)
    if not skip_restart:
        status, health = request_json("/api/health", 30)
        require(status == 200, f"baseline health HTTP status mismatch: {status}")
        require(health.get("version") == BASELINE_VERSION, "baseline health version mismatch")
        validate_openapi(BASELINE_VERSION)
    check_active_tasks(root)
    manifest = create_backup(root, backup, live)
    stopped = False
    try:
        if not skip_restart:
            stop_platform(root)
            stopped = True
        mode = int(manifest["files"]["app/main.py"]["mode"])
        atomic_write(root / "app/main.py", target, mode)
        run([str(python), "-m", "py_compile", str(root / "app/main.py")], 120)
        validate_target_source(live["app/main.py"], (root / "app/main.py").read_bytes())
        require(sha((root / "app/services/gemma.py").read_bytes()) == BASELINE_SHA_MANIFEST["app/services/gemma.py"], "gemma.py changed")
        require(sha((root / "app/stage04_v238_runtime.py").read_bytes()) == BASELINE_SHA_MANIFEST["app/stage04_v238_runtime.py"], "Stage04 runtime changed")
        if not skip_restart:
            start_and_verify(root, TARGET_VERSION)
        print("INSTALL PASS")
        print("backup=" + str(backup))
        print("target app/main.py=" + TARGET_SHA_MANIFEST["app/main.py"])
    except Exception:
        restore_exact_backup(root, backup, manifest)
        if not skip_restart and stopped:
            start_and_verify(root, BASELINE_VERSION)
        print("ROLLBACK PASS", file=sys.stderr)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--skip-restart", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = discover_platform_root(args.root)
    python = discover_platform_python(args.python)
    if args.self_test:
        self_test(root, python)
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = args.backup_dir or root.parent / "platform-v2-backups" / (
        "v23963-stage-progress-v2-" + stamp
    )
    install(root, python, backup.resolve(), skip_restart=args.skip_restart)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INSTALL FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
