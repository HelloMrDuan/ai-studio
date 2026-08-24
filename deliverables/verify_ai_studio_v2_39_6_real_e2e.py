#!/usr/bin/env python3
"""Destructive-by-confirmation real AutoDL verifier for AI Studio V2.39.6.

This script cold-starts ports 6008/6006 and rebuilds Stage04 for one existing
project. It never calls image, video, ComfyUI, H3 or FaceFusion APIs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "2.39.6-stage04-qwen-runtime-contract"
REQUIRED_MODEL_ID = "qwen3-32b-abliterated"
REQUIRED_ALIAS = "qwen3-32b"
CONFIRMATION = "COLD_START_AND_REBUILD_STAGE04"


class VerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def run(command: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"command failed ({result.returncode}): {shlex.join(command)}\n"
            f"stdout:\n{result.stdout[-3000:]}\nstderr:\n{result.stderr[-3000:]}"
        )
    return result


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        return exc.code, body


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_http(base_url: str, path: str, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body = request_json(base_url, path, timeout=5)
            if status == 200:
                return body
            last = f"HTTP {status}: {body}"
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise VerificationError(f"wait HTTP timeout: {path}; last={last}")


def project_ids(base_url: str) -> list[str]:
    status, body = request_json(base_url, "/api/director/projects")
    require(status == 200 and isinstance(body, list), "cannot list projects")
    values: list[str] = []
    for item in body:
        if isinstance(item, dict):
            value = str(item.get("project_id") or item.get("id") or "").strip()
            if value:
                values.append(value)
    return values


def assert_no_running_stage04(base_url: str) -> None:
    for project_id in project_ids(base_url):
        status, body = request_json(
            base_url,
            f"/api/studio/projects/{project_id}/stage04/rebuild-production/status",
        )
        require(status == 200, f"cannot inspect Stage04 task: {project_id}")
        task_status = str((body or {}).get("status") or "")
        require(
            task_status not in {"queued", "running"},
            f"Stage04 task is active for {project_id}: {task_status}",
        )


async def verify_failed_retry_component(root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(root))
    from app.config import Settings
    from app.core.gpu_orchestrator import GPUOrchestrator
    from app.models import GPUOwner, SwitchPhase

    settings = Settings(_env_file=None)
    orchestrator = GPUOrchestrator(settings)

    async def fail_switch(_target):
        raise RuntimeError("v2396-isolated-failure")

    orchestrator._switch = fail_switch
    failed = False
    try:
        await orchestrator.ensure_ready(GPUOwner.gemma, timeout=2)
    except RuntimeError as exc:
        failed = "v2396-isolated-failure" in str(exc)
    require(failed, "isolated orchestrator did not enter FAILED")
    state = await orchestrator.snapshot()
    require(state.phase == SwitchPhase.failed, "orchestrator FAILED state missing")
    await asyncio.sleep(0)

    async def successful_switch(target):
        async with orchestrator._state_lock:
            orchestrator.state.owner = target
            orchestrator.state.desired_owner = target
        await orchestrator._set_state(
            phase=SwitchPhase.ready,
            message="v2396-isolated-retry-ready",
            error=None,
        )

    orchestrator._switch = successful_switch
    ready = await orchestrator.ensure_ready(GPUOwner.gemma, timeout=2)
    require(ready.phase == SwitchPhase.ready, "FAILED -> retry did not reach READY")
    return {"failed_observed": True, "retry_ready": True}


def read_source_text(snapshot: dict[str, Any], data_dir: Path) -> str:
    assets = [
        item
        for item in (snapshot.get("assets") or [])
        if isinstance(item, dict)
        and str(item.get("asset_role") or "") == "source_full"
        and bool(item.get("active", True))
    ]
    require(bool(assets), "active source_full asset not found")
    assets.sort(key=lambda item: int(item.get("version") or 0), reverse=True)
    storage = assets[0].get("storage") or {}
    path_value = str(storage.get("path") or "").strip()
    path = Path(path_value) if path_value else None
    if path is None or not path.is_file():
        url = str(storage.get("url") or "")
        if url.startswith("/files/"):
            path = (data_dir / url[len("/files/"):]).resolve()
    require(path is not None and path.is_file(), "source_full storage file not found")
    return path.read_text(encoding="utf-8", errors="replace")


def semantic_assertions(
    *,
    root: Path,
    data_dir: Path,
    project_id: str,
    task: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    state_path = data_dir / "story_continuity" / f"{project_id}.json"
    require(state_path.is_file(), f"Stage04 state file not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assets = [
        item
        for item in (snapshot.get("assets") or [])
        if isinstance(item, dict)
        and str(item.get("asset_role") or "") == "storyboard_master"
        and bool(item.get("active", True))
    ]
    require(bool(assets), "active storyboard_master asset missing")
    task_asset_id = str(task.get("asset_id") or "")
    matching_assets = [
        item for item in assets
        if str(item.get("asset_id") or "") == task_asset_id
    ]
    storyboard_asset = matching_assets[0] if matching_assets else assets[-1]
    metadata = storyboard_asset.get("metadata") or {}
    scope_scene_ids = {
        str(item.get("scene_id") or "")
        for item in (metadata.get("scene_stats") or [])
        if isinstance(item, dict) and str(item.get("scene_id") or "")
    }
    require(bool(scope_scene_ids), "storyboard asset has no scene scope")
    shots = [
        item
        for item in (state.get("shots") or [])
        if isinstance(item, dict)
        and item.get("runtime_version") == VERSION
        and str(item.get("scene_id") or "") in scope_scene_ids
    ]
    require(bool(shots), "no V2.39.6 formal shots found")
    formal_count = int(task.get("formal_shots") or 0)
    require(formal_count == len(shots), f"formal shot count mismatch: {formal_count} != {len(shots)}")
    source_text = read_source_text(snapshot, data_dir)

    global_orders: list[int] = []
    scene_orders: dict[str, list[int]] = {}
    beat_orders: dict[str, list[int]] = {}
    evidence_count = 0
    for index, shot in enumerate(shots, 1):
        prefix = f"shot#{index}"
        require(not bool(shot.get("provisional")), f"{prefix} is provisional")
        require(shot.get("stage04_contract_version") == "strict-shot-v2", f"{prefix} contract mismatch")
        require(shot.get("text_model_policy") == REQUIRED_ALIAS, f"{prefix} model policy mismatch")
        for field in ("representative_state", "video_start_state", "video_end_state"):
            require(bool(str(shot.get(field) or "").strip()), f"{prefix} missing {field}")
        representative = str(shot["representative_state"]).strip()
        start = str(shot["video_start_state"]).strip()
        end = str(shot["video_end_state"]).strip()
        require(str(shot.get("image_prompt") or "").strip() == representative, f"{prefix} image prompt drift")
        require(str(shot.get("video_start_prompt") or "").strip() == start, f"{prefix} start prompt drift")
        require(
            str(shot.get("video_prompt") or "").strip() == f"起始状态：{start}\n结束状态：{end}",
            f"{prefix} video prompt drift",
        )
        duration = float(shot.get("duration_seconds"))
        require(math.isfinite(duration) and 0.8 <= duration <= 20.0, f"{prefix} invalid duration")

        scene_id = str(shot.get("scene_id") or "")
        require(bool(scene_id), f"{prefix} missing scene_id")
        global_orders.append(int(shot.get("global_order") or 0))
        scene_orders.setdefault(scene_id, []).append(int(shot.get("order") or 0))
        beats = [int(value) for value in (shot.get("covered_beat_orders") or [])]
        require(bool(beats), f"{prefix} has no covered beats")
        beat_orders.setdefault(scene_id, []).extend(beats)

        provenance = shot.get("source_provenance") or {}
        require(provenance.get("contract_version") == "strict-shot-v2", f"{prefix} provenance contract mismatch")
        require(provenance.get("text_model_policy") == REQUIRED_ALIAS, f"{prefix} provenance model mismatch")
        require(provenance.get("runtime_version") == VERSION, f"{prefix} provenance runtime mismatch")
        evidence = list(provenance.get("source_evidence") or [])
        spans = list(provenance.get("source_evidence_spans") or [])
        ids = list(provenance.get("source_evidence_ids") or [])
        require(evidence and len(evidence) == len(spans) == len(ids), f"{prefix} evidence binding mismatch")
        for expected_id, expected_text, span in zip(ids, evidence, spans):
            require(isinstance(span, dict), f"{prefix} invalid evidence span")
            start_offset = int(span.get("start"))
            end_offset = int(span.get("end"))
            require(str(span.get("id") or "") == str(expected_id), f"{prefix} evidence id mismatch")
            require(str(span.get("text") or "") == str(expected_text), f"{prefix} evidence text mismatch")
            require(0 <= start_offset < end_offset <= len(source_text), f"{prefix} evidence offset out of range")
            require(source_text[start_offset:end_offset] == str(expected_text), f"{prefix} evidence is not exact verbatim source")
            evidence_count += 1

    require(global_orders == list(range(1, len(shots) + 1)), "global shot order is not exact")
    for scene_id, values in scene_orders.items():
        require(values == list(range(1, len(values) + 1)), f"scene shot order is not exact: {scene_id}")
    for scene_id, values in beat_orders.items():
        require(values == sorted(values), f"shot Beat coverage moves backward: {scene_id}")
        ordered = sorted(set(values))
        require(ordered == list(range(ordered[0], ordered[-1] + 1)), f"Beat coverage has gaps: {scene_id}")

    require(metadata.get("runtime_version") == VERSION, "storyboard asset runtime mismatch")
    require(metadata.get("stage04_contract_version") == "strict-shot-v2", "storyboard asset contract mismatch")
    require(metadata.get("text_model_policy") == REQUIRED_ALIAS, "storyboard asset model mismatch")

    runtime_source = (root / "app" / "stage04_v238_runtime.py").read_text(encoding="utf-8")
    for marker in (
        "forward_with_replayed_prefix",
        "source_evidence_spans",
        "scene_global_audit",
        "evidence_locked_repair",
        "strict-shot-v2-state-derived",
    ):
        require(marker in runtime_source, f"live Stage04 protection missing: {marker}")
    return {
        "formal_shots": len(shots),
        "scenes": len(scene_orders),
        "evidence_spans_verified": evidence_count,
        "exact_beat_coverage": True,
        "deterministic_prompts": True,
        "semantic_contract_pass": True,
    }


def validate_performance(performance: Any, task: dict[str, Any]) -> dict[str, Any]:
    require(isinstance(performance, dict), "Stage04 performance profile missing")
    require(performance.get("schema_version") == "stage04-perf-v1", "performance schema mismatch")
    require(performance.get("qwen_contract_verified") is True, "Qwen performance preflight marker missing")
    require(float(performance.get("workspace_start_seconds") or 0.0) >= 0.0, "workspace timing invalid")
    require(float(performance.get("qwen_ready_wait_seconds") or 0.0) >= 0.0, "Qwen READY timing invalid")
    require(float(performance.get("total_seconds") or 0.0) > 0.0, "Stage04 total timing missing")
    scenes = performance.get("scenes") or []
    require(len(scenes) == int(task.get("scene_total") or 0), "Scene timing count mismatch")
    require(all(float(item.get("total_seconds") or 0.0) > 0.0 for item in scenes), "Scene timing invalid")

    categories = performance.get("categories") or {}
    required_categories = (
        "anchor_extraction",
        "anchor_classification",
        "beat_grouping",
        "adjacent_beat_reconcile",
        "evidence_selector",
        "shot_generation",
        "repair",
        "batch_audit",
        "scene_global_audit",
    )
    for category in required_categories:
        require(category in categories, f"performance category missing: {category}")
    require(int(categories["anchor_extraction"].get("calls") or 0) > 0, "anchor extraction timing missing")
    require(int(categories["anchor_classification"].get("batch_count") or 0) > 0, "anchor classification batch timing missing")
    require(bool(categories["anchor_classification"].get("batch_seconds")), "anchor batch durations missing")
    require(int(categories["shot_generation"].get("batch_count") or 0) > 0, "shot batch timing missing")
    require(bool(categories["shot_generation"].get("batch_seconds")), "shot batch durations missing")
    require(int(categories["scene_global_audit"].get("calls") or 0) > 0, "scene-global audit timing missing")

    phases = performance.get("phases") or {}
    require(bool(phases), "per-phase timing table is empty")
    for phase, row in phases.items():
        require(str(phase).strip() != "", "empty performance phase")
        for field in ("calls", "input_tokens", "output_tokens", "total_seconds", "avg_seconds"):
            require(field in row, f"phase metric missing: {phase}.{field}")
        require(int(row.get("calls") or 0) > 0, f"phase calls invalid: {phase}")
        require(float(row.get("total_seconds") or 0.0) >= 0.0, f"phase seconds invalid: {phase}")
    return {
        "workspace_start_seconds": performance.get("workspace_start_seconds"),
        "qwen_ready_wait_seconds": performance.get("qwen_ready_wait_seconds"),
        "scene_count": len(scenes),
        "llm_calls": int(performance.get("llm_calls") or 0),
        "llm_retries": int(performance.get("llm_retries") or 0),
        "input_tokens": int(performance.get("input_tokens") or 0),
        "output_tokens": int(performance.get("output_tokens") or 0),
        "phase_count": len(phases),
        "total_seconds": performance.get("total_seconds"),
        "profile": performance,
    }


def performance_recommendations(performance: dict[str, Any]) -> str:
    categories = performance.get("categories") or {}
    specs = {
        "workspace_start": (
            float(performance.get("workspace_start_seconds") or 0.0),
            0.35,
            "在同一次 rebuild 内维持现有 Qwen workspace lease，并合并重复的只读 READY/身份探测；仍须在 rebuild 边界执行完整身份合同。",
            "低",
        ),
        "anchor_classification": (
            float((categories.get("anchor_classification") or {}).get("batch_total_seconds") or (categories.get("anchor_classification") or {}).get("total_seconds") or 0.0),
            0.12,
            "复用 llama.cpp prompt cache 中不变的模型合同与 Scene 前缀；只对完全相同的请求哈希做同一 rebuild 内去重。",
            "低",
        ),
        "beat_grouping": (
            float((categories.get("beat_grouping") or {}).get("total_seconds") or 0.0),
            0.10,
            "缓存确定性的 anchor 序列化和实体上下文，保持每个 grouping 语义调用及输出合同不变。",
            "低",
        ),
        "adjacent_beat_reconcile": (
            float((categories.get("adjacent_beat_reconcile") or {}).get("total_seconds") or 0.0),
            0.08,
            "对完全相同的 Beat pair、证据和合同使用请求级结果缓存；任何输入差异都必须重新调用 Qwen。",
            "中",
        ),
        "evidence_selector": (
            float((categories.get("evidence_selector") or {}).get("total_seconds") or 0.0),
            0.10,
            "预构建并复用确定性的 evidence ID→offset 索引；不减少 selector 调用和原文验证。",
            "低",
        ),
        "shot_generation": (
            float((categories.get("shot_generation") or {}).get("batch_total_seconds") or (categories.get("shot_generation") or {}).get("total_seconds") or 0.0),
            0.10,
            "保持 Qwen 常驻并提高稳定 prompt 前缀的 llama.cpp cache 命中率；不改变 batch 边界、max_tokens 或 Shot 合同。",
            "中",
        ),
        "repair": (
            float((categories.get("repair") or {}).get("total_seconds") or 0.0),
            0.08,
            "继续只传 unresolved/违规项，并复用已验证 evidence packet；不得跳过 repair 或扩大自动接受范围。",
            "低",
        ),
        "audit": (
            sum(float((categories.get(key) or {}).get("total_seconds") or 0.0) for key in ("batch_audit", "scene_global_audit")),
            0.06,
            "只优化审计输入的确定性序列化与稳定前缀缓存；保留 batch audit、projection audit 和 scene-global audit 全部调用。",
            "中",
        ),
    }
    risk_order = {"低": 0, "中": 1, "高": 2}
    rows = []
    for name, (observed, ratio, action, risk) in specs.items():
        estimated = observed * ratio
        rows.append((estimated, risk_order[risk], name, observed, action, risk))
    rows.sort(key=lambda item: (-item[0], item[1], item[2]))

    lines = [
        "# 《V2.39.7 Stage04 性能优化建议》",
        "",
        "本建议由 V2.39.6 真实 E2E timing 数据生成。预计收益是基于观测耗时的保守工程估算，不是已验证加速结果。",
        "所有建议均要求保持现有 audit、repair、evidence、coverage、Qwen3-32B 和输出合同。",
        "",
        f"- Stage04 实测总耗时：{float(performance.get('total_seconds') or 0.0):.3f}s",
        f"- LLM 实际请求次数：{int(performance.get('llm_calls') or 0)}",
        f"- LLM retry：{int(performance.get('llm_retries') or 0)}",
        f"- 实际 input/output tokens：{int(performance.get('input_tokens') or 0)} / {int(performance.get('output_tokens') or 0)}",
        "",
        "| 排名 | 观测阶段 | 实测耗时 | 预计收益 | 风险 | 是否改变语义 | 建议 |",
        "|---:|---|---:|---:|---|---|---|",
    ]
    for index, (estimated, _risk_rank, name, observed, action, risk) in enumerate(rows, 1):
        lines.append(
            f"| {index} | {name} | {observed:.3f}s | ≤{estimated:.3f}s/次 rebuild | {risk} | 否 | {action} |"
        )
    lines.extend([
        "",
        "## 禁止项",
        "",
        "不得删除或合并语义不同的调用，不得减少 evidence 验证，不得删除任何 audit/repair，"
        "不得用关键词规则或小模型替代 Qwen3-32B，不得降低输出合同。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/root/autodl-tmp/ai-studio/platform-v2")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--confirm", required=True, help=f"must equal {CONFIRMATION}")
    parser.add_argument("--task-timeout-seconds", type=int, default=21600)
    args = parser.parse_args()
    require(args.confirm == CONFIRMATION, "explicit cold-start/rebuild confirmation is missing")

    root = Path(args.root).resolve()
    require(root.is_dir(), f"root not found: {root}")
    env = read_env(root / ".env")
    base_url = "http://127.0.0.1:6008"
    llm_base = env.get("GEMMA_BASE_URL", "http://127.0.0.1:6006/v1").rstrip("/")
    data_dir = Path(env.get("DATA_DIR", "/root/autodl-tmp/ai-studio/data/platform-v2"))
    report: dict[str, Any] = {
        "version": VERSION,
        "project_id": args.project_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "layers": {},
    }

    gpu_info = run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,driver_version,memory.total,memory.free,compute_cap",
            "--format=csv,noheader,nounits",
        ],
        timeout=30,
    ).stdout.strip().splitlines()
    require(bool(gpu_info), "nvidia-smi returned no GPU")
    report["layers"]["gpu"] = {"rows": gpu_info}

    openapi = wait_http(base_url, "/openapi.json", 30)
    require(openapi.get("info", {}).get("version") == VERSION, "platform version mismatch")
    assert_no_running_stage04(base_url)
    status, models = request_json(base_url, "/api/llm/models")
    require(status == 200, "cannot read selected model")
    require(models.get("selected_model") == REQUIRED_MODEL_ID, "selected model is not required Qwen")
    selected_items = [item for item in (models.get("models") or []) if item.get("selected")]
    require(len(selected_items) == 1, "selected model record is not unique")
    selected = selected_items[0]
    require(selected.get("alias") == REQUIRED_ALIAS, "selected alias mismatch")
    require(Path(str(selected.get("path") or "")).is_file(), "selected GGUF does not exist")

    platform_pid_file = Path("/root/autodl-tmp/ai-studio/logs/platform-v2.pid")
    pid = int(platform_pid_file.read_text().strip())
    require(process_alive(pid), "6008 PID is not alive")
    cwd = os.readlink(f"/proc/{pid}/cwd")
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    require(Path(cwd).resolve() == root, f"6008 cwd mismatch: {cwd}")
    require("uvicorn app.main:app" in cmdline, f"unexpected 6008 command: {cmdline}")
    report["layers"]["platform_before_cold_start"] = {"pid": pid, "cwd": cwd, "cmdline": cmdline}

    report["layers"]["orchestrator_failed_retry"] = asyncio.run(
        verify_failed_retry_component(root)
    )

    run(["bash", str(root / "scripts" / "stop.sh")], timeout=60)
    run(["bash", str(root / "scripts" / "stop_llm.sh")], timeout=180)
    deadline = time.monotonic() + 60
    while port_open(6006) and time.monotonic() < deadline:
        time.sleep(1)
    require(not port_open(6006), "port 6006 did not stop for cold start")

    run(["bash", str(root / "scripts" / "start.sh")], timeout=120)
    wait_http(base_url, "/openapi.json", 90)
    start_timeout = int(env.get("GEMMA_START_TIMEOUT_SECONDS", "600"))
    margin = int(env.get("LLM_STARTUP_TIMEOUT_MARGIN_SECONDS", "60"))
    rebuild_started = time.monotonic()
    rebuild_status, rebuild = request_json(
        base_url,
        f"/api/studio/projects/{args.project_id}/stage04/rebuild-production",
        method="POST",
        payload={},
        timeout=start_timeout + margin + 120,
    )
    require(rebuild_status == 200, f"cold-start Stage04 preflight/rebuild rejected: {rebuild}")
    require(rebuild.get("status") == "queued", f"unexpected rebuild response: {rebuild}")
    report["layers"]["startup_race"] = {
        "rebuild_request_seconds": round(time.monotonic() - rebuild_started, 3),
        "preflight_completed_before_queue": True,
    }

    llm_pid_file = Path("/root/autodl-tmp/ai-studio/logs/gemma-llama-server.pid")
    llm_pid = int(llm_pid_file.read_text().strip())
    require(process_alive(llm_pid), "llama-server PID is not alive")
    llm_cmdline = Path(f"/proc/{llm_pid}/cmdline").read_bytes().split(b"\0")
    llm_args = [item.decode(errors="replace") for item in llm_cmdline if item]
    require(bool(llm_args) and Path(llm_args[0]).is_absolute(), "llama binary is not absolute")
    require("--model" in llm_args and "--alias" in llm_args, "llama command lacks model/alias")
    def arg_value(flag: str) -> str:
        require(flag in llm_args, f"llama command lacks {flag}")
        index = llm_args.index(flag)
        require(index + 1 < len(llm_args), f"llama command has no value for {flag}")
        return llm_args[index + 1]
    model_path = arg_value("--model")
    require(Path(model_path).is_absolute() and Path(model_path).is_file(), "GGUF path invalid")
    require(Path(model_path).resolve() == Path(str(selected["path"])).resolve(), "running GGUF differs from selected model")
    require(arg_value("--alias") == REQUIRED_ALIAS, "running alias mismatch")
    require(arg_value("--ctx-size") == "8192", "ctx-size mismatch")
    require(arg_value("--parallel") == "1", "parallel mismatch")
    require(int(arg_value("--n-gpu-layers")) > 0, "GPU layers is not positive")

    status, model_body = request_json(llm_base, "/models")
    require(status == 200, "/v1/models failed")
    aliases = [str(item.get("id") or "") for item in (model_body.get("data") or [])]
    require(aliases == [REQUIRED_ALIAS], f"/v1/models exact alias mismatch: {aliases}")
    status, chat = request_json(
        llm_base,
        "/chat/completions",
        method="POST",
        payload={
            "model": REQUIRED_ALIAS,
            "messages": [{"role": "user", "content": "Reply exactly QWEN_OK"}],
            "temperature": 0,
            "max_tokens": 16,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=180,
    )
    require(status == 200, f"QWEN_OK chat failed: {chat}")
    content = str((((chat.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
    require(content == "QWEN_OK", f"QWEN_OK content mismatch: {content!r}")
    require(str(chat.get("model") or "") == REQUIRED_ALIAS, "chat response.model mismatch")
    status, gpu_state = request_json(base_url, "/api/gpu/status")
    require(status == 200, "GPU status failed")
    require(gpu_state.get("owner") == "gemma" and gpu_state.get("phase") == "READY", f"GPU orchestrator not READY: {gpu_state}")
    report["layers"]["autodl_runtime"] = {
        "llama_pid": llm_pid,
        "binary": llm_args[0],
        "gguf": model_path,
        "alias": REQUIRED_ALIAS,
        "ctx_size": arg_value("--ctx-size"),
        "parallel": arg_value("--parallel"),
        "gpu_layers": arg_value("--n-gpu-layers"),
        "models": aliases,
        "qwen_ok": True,
        "response_model": chat.get("model"),
        "orchestrator_ready": True,
    }

    active_protection = False
    task_deadline = time.monotonic() + args.task_timeout_seconds
    last_task: dict[str, Any] = {}
    while time.monotonic() < task_deadline:
        status, last_task = request_json(
            base_url,
            f"/api/studio/projects/{args.project_id}/stage04/rebuild-production/status",
            timeout=30,
        )
        require(status == 200, "Stage04 status request failed")
        task_status = str(last_task.get("status") or "")
        if not active_protection and task_status in {"queued", "running"}:
            _, live_gpu = request_json(base_url, "/api/gpu/status")
            active = live_gpu.get("active_tasks") or {}
            if int(active.get("gemma") or 0) > 0:
                select_status, _ = request_json(
                    base_url,
                    f"/api/llm/select/{REQUIRED_MODEL_ID}",
                    method="POST",
                    payload={},
                )
                require(select_status == 409, "LLM active-task protection did not return 409")
                active_protection = True
        if task_status in {"completed", "failed"}:
            break
        time.sleep(5)
    require(str(last_task.get("status") or "") == "completed", f"Stage04 did not complete: {last_task}")
    require(active_protection, "active-task protection was not observed during real Stage04")
    performance = validate_performance(last_task.get("performance"), last_task)

    status, snapshot = request_json(base_url, f"/api/studio/projects/{args.project_id}", timeout=120)
    require(status == 200, "cannot read final Stage04 project JSON")
    semantic = semantic_assertions(
        root=root,
        data_dir=data_dir,
        project_id=args.project_id,
        task=last_task,
        snapshot=snapshot,
    )
    report["layers"]["stage04_e2e"] = {
        "task": last_task,
        "active_task_protection": True,
        "final_json_read": True,
    }
    report["layers"]["semantic_acceptance"] = semantic
    report["layers"]["stage04_performance"] = performance
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["result"] = "SEMANTIC_ACCEPTANCE_PASS"

    output_dir = root / "deliverables"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"v2396-real-e2e-{int(time.time())}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    recommendations = output_dir / f"v2397-stage04-performance-recommendations-{int(time.time())}.md"
    recommendations.write_text(
        performance_recommendations(performance["profile"]),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"REPORT={output}")
    print(f"PERFORMANCE_RECOMMENDATIONS={recommendations}")
    print("AUTO DL RUNTIME PASS")
    print("STAGE04 E2E PASS")
    print("SEMANTIC ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"REAL E2E FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
