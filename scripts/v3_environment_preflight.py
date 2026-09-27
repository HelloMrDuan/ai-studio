#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(os.environ.get("XIAODUAN_ROOT", "/root/autodl-tmp/ai-studio/platform-v2")).resolve()
REPORT = Path(os.environ.get(
    "XIAODUAN_PREFLIGHT_REPORT",
    "/root/autodl-tmp/ai-studio/logs/v3_environment_preflight.json",
)).resolve()
EXPECTED_BRANCH = os.environ.get("XIAODUAN_EXPECTED_BRANCH", "refactor/xiaoduan-studio-v3")
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "127.0.0.1")
TEMPORAL_PORT = int(os.environ.get("TEMPORAL_PORT", "7233"))
MIN_FREE_GB = float(os.environ.get("XIAODUAN_MIN_FREE_GB", "30"))

results: list[dict[str, Any]] = []


def add(name: str, status: str, detail: str, *, required: bool = True, remediation: str = "", data: dict[str, Any] | None = None) -> None:
    results.append({
        "name": name,
        "status": status,
        "required": required,
        "detail": detail,
        "remediation": remediation,
        "data": data or {},
    })
    icon = {"PASS": "PASS", "MISSING": "MISS", "MISCONFIGURED": "WARN", "BLOCKED": "BLOCK"}.get(status, status)
    optional = " [optional]" if not required else ""
    print(f"[{icon:<5}] {name}{optional}: {detail}")


def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT if ROOT.is_dir() else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def http_json(url: str, timeout: float = 8.0) -> tuple[int, Any]:
    opener = build_opener(ProxyHandler({}))
    req = Request(url, headers={"User-Agent": "xiaoduan-v3-preflight/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8"))
        except Exception:
            return resp.status, raw.decode("utf-8", errors="replace")


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_file(root: Path, filename: str) -> Path | None:
    if not root.is_dir():
        return None
    try:
        for p in root.rglob(filename):
            if p.is_file():
                return p
    except Exception:
        pass
    return None


print("=" * 80)
print("xiaoduan映画 / Xiaoduan Studio V3 Environment Preflight")
print("READ ONLY: does not install, delete, modify models/workflows/services")
print("=" * 80)

# Repository / branch.
if not ROOT.is_dir():
    add("repository", "BLOCKED", f"repo not found: {ROOT}", remediation="check XIAODUAN_ROOT")
else:
    add("repository", "PASS", str(ROOT))
    git = shutil.which("git")
    if not git:
        add("git", "MISSING", "git not found in PATH")
    else:
        branch = run([git, "rev-parse", "--abbrev-ref", "HEAD"])
        head = run([git, "rev-parse", "HEAD"])
        dirty = run([git, "status", "--porcelain"])
        branch_name = branch.stdout.strip()
        head_sha = head.stdout.strip()
        if branch.returncode != 0:
            add("git branch", "BLOCKED", branch.stderr.strip() or "cannot read branch")
        elif branch_name != EXPECTED_BRANCH:
            add("V3 branch", "MISCONFIGURED", f"current={branch_name}, expected={EXPECTED_BRANCH}", remediation="switch to the V3 refactor branch after protecting local changes")
        else:
            add("V3 branch", "PASS", f"{branch_name} @ {head_sha[:12]}")
        if dirty.stdout.strip():
            add("working tree", "MISCONFIGURED", "uncommitted changes exist", required=False, data={"status": dirty.stdout.splitlines()[:30]})
        else:
            add("working tree", "PASS", "clean", required=False)

settings = None
provider_registry = None
Capability = None
try:
    sys.path.insert(0, str(ROOT))
    from app.config import get_settings
    from app.v3.contracts import Capability as _Capability
    from app.v3.provider_catalog import build_provider_registry

    Capability = _Capability
    settings = get_settings()
    provider_registry = build_provider_registry(settings)
    add("V3 Python imports", "PASS", f"python={sys.executable}")
except Exception as exc:
    add("V3 Python imports", "BLOCKED", f"{type(exc).__name__}: {exc}", remediation="use the ai-studio-platform-v2 Python and install requirements.txt")

for module in ("fastapi", "pydantic", "httpx", "temporalio"):
    try:
        __import__(module)
        add(f"python package:{module}", "PASS", "import ok")
    except Exception as exc:
        add(f"python package:{module}", "MISSING", f"{type(exc).__name__}: {exc}", remediation="install project requirements.txt")

# V3 API.
try:
    code, body = http_json("http://127.0.0.1:6008/api/v3/health")
    if code == 200 and isinstance(body, dict) and body.get("product") == "xiaoduan映画":
        add("V3 app health", "PASS", f"HTTP {code}, version={body.get('version')}")
    else:
        add("V3 app health", "MISCONFIGURED", f"HTTP {code}, body={str(body)[:300]}")
except Exception as exc:
    add("V3 app health", "MISSING", f"{type(exc).__name__}: {exc}", remediation="deploy V3 branch and start scripts/start.sh")

# Local Qwen/OpenAI-compatible service.
qwen_base = str(getattr(settings, "gemma_base_url", "http://127.0.0.1:6006/v1")).rstrip("/")
try:
    code, body = http_json(f"{qwen_base}/models", timeout=10)
    if code == 200:
        count = len(body.get("data", [])) if isinstance(body, dict) else 0
        add("local Qwen/OpenAI-compatible", "PASS", f"{qwen_base}/models, models={count}")
    else:
        add("local Qwen/OpenAI-compatible", "MISCONFIGURED", f"HTTP {code}")
except Exception as exc:
    add("local Qwen/OpenAI-compatible", "MISSING", f"{type(exc).__name__}: {exc}", remediation="start local Qwen and verify /v1/models")

# ComfyUI.
comfy_base = str(getattr(settings, "comfyui_base_url", "http://127.0.0.1:8188")).rstrip("/")
object_info: dict[str, Any] | None = None
try:
    code, _ = http_json(f"{comfy_base}/system_stats", timeout=10)
    if code == 200:
        add("ComfyUI health", "PASS", f"{comfy_base}/system_stats")
    else:
        add("ComfyUI health", "MISCONFIGURED", f"HTTP {code}")
except Exception as exc:
    add("ComfyUI health", "MISSING", f"{type(exc).__name__}: {exc}", remediation="start ComfyUI on 8188")

try:
    code, body = http_json(f"{comfy_base}/object_info", timeout=20)
    if code == 200 and isinstance(body, dict):
        object_info = body
        add("ComfyUI /object_info", "PASS", f"nodes={len(body)}")
    else:
        add("ComfyUI /object_info", "MISCONFIGURED", f"HTTP {code}")
except Exception as exc:
    add("ComfyUI /object_info", "MISSING", f"{type(exc).__name__}: {exc}")

if object_info is not None:
    keywords = ("ipadapter", "pulid", "instantid", "faceid", "controlnet")
    ref_nodes = sorted(k for k in object_info if any(w in k.lower() for w in keywords))
    if ref_nodes:
        add("Comfy reference nodes", "PASS", f"candidate nodes={len(ref_nodes)}", data={"nodes": ref_nodes[:100]})
    else:
        add("Comfy reference nodes", "MISSING", "no IPAdapter/PuLID/InstantID/FaceID/ControlNet-like nodes", remediation="install one real reference/identity stack compatible with the installed ComfyUI")
else:
    add("Comfy reference nodes", "BLOCKED", "cannot inspect without /object_info")

# Reference profile + workflow bindings.
if settings is not None:
    profile_path = Path(settings.data_dir) / "comfyui_reference_profile.v3.json"
    if not profile_path.is_file():
        add("reference profile", "MISSING", str(profile_path), remediation="build the profile only after verifying actual nodes/workflow")
    else:
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            wf_path = Path(str(profile.get("reference_workflow_path") or ""))
            bindings = profile.get("reference_bindings") or []
            problems: list[str] = []
            workflow = None
            if not wf_path.is_file():
                problems.append(f"workflow missing: {wf_path}")
            else:
                workflow = json.loads(wf_path.read_text(encoding="utf-8"))
            if not isinstance(bindings, list) or not bindings:
                problems.append("reference_bindings empty")
            if isinstance(workflow, dict) and isinstance(bindings, list):
                for b in bindings:
                    node_id = str(b.get("node_id") or "")
                    input_name = str(b.get("input_name") or "image")
                    node = workflow.get(node_id)
                    if not isinstance(node, dict):
                        problems.append(f"binding node missing: {node_id}")
                        continue
                    inputs = node.get("inputs")
                    if not isinstance(inputs, dict) or input_name not in inputs:
                        problems.append(f"binding input missing: node={node_id}, input={input_name}")
                    class_type = str(node.get("class_type") or "")
                    if object_info is not None and class_type and class_type not in object_info:
                        problems.append(f"Comfy node not installed: {class_type}")
                classes = [str(v.get("class_type") or "") for v in workflow.values() if isinstance(v, dict)]
                if profile.get("ip_adapter") and not any("ipadapter" in c.lower() for c in classes):
                    problems.append("profile says ip_adapter=true but workflow has no IPAdapter class")
                if profile.get("identity_reference") and not any(any(x in c.lower() for x in ("pulid", "instantid", "faceid", "ipadapter")) for c in classes):
                    problems.append("profile says identity_reference=true but workflow has no identity/reference class")
            if problems:
                add("reference profile", "MISCONFIGURED", "; ".join(problems[:12]))
            else:
                add("reference profile", "PASS", f"{profile_path}, bindings={len(bindings)}")
        except Exception as exc:
            add("reference profile", "MISCONFIGURED", f"{type(exc).__name__}: {exc}")

# Provider Registry.
if provider_registry is not None and Capability is not None:
    try:
        specs = provider_registry.list()
        data = [{
            "provider_id": x.provider_id,
            "model_id": x.model_id,
            "transport": x.transport.value,
            "capabilities": sorted(c.value for c in x.capabilities),
        } for x in specs]
        add("V3 Provider Registry", "PASS", f"providers={len(specs)}", data={"providers": data})
        ref_specs = [x for x in specs if Capability.image_reference in x.capabilities]
        tts_specs = [x for x in specs if Capability.tts in x.capabilities]
        if ref_specs:
            add("Provider:image_reference", "PASS", ", ".join(f"{x.provider_id}:{x.model_id}" for x in ref_specs))
        else:
            add("Provider:image_reference", "MISSING", "no image_reference provider", remediation="validate reference profile first; do not fake capability")
        if tts_specs:
            add("Provider:TTS", "PASS", ", ".join(f"{x.provider_id}:{x.model_id}" for x in tts_specs))
        else:
            add("Provider:TTS", "MISSING", "no tts capability in Provider Registry", remediation="register local or API TTS in data_dir/providers.v3.json")
    except Exception as exc:
        add("V3 Provider Registry", "MISCONFIGURED", f"{type(exc).__name__}: {exc}")

# H3 node graph requirements are derived from the actual V3 compiler.
if settings is not None:
    try:
        from app.v3.adapters.h3 import H3WorkflowCompiler, H3WorkflowConfig
        compiler = H3WorkflowCompiler(H3WorkflowConfig(
            fl2va_model=settings.h3_fl2va_model,
            ref2va_model=settings.h3_ref2va_model,
            text_encoder=settings.h3_text_encoder,
            video_vae=settings.h3_video_vae,
            audio_vae=settings.h3_audio_vae,
            sampler=settings.h3_sampler,
            scheduler=settings.h3_scheduler,
        ))
        wf1 = compiler.first_last_frame(prompt="preflight", first_name="dummy.png")
        wf2 = compiler.reference_to_video(prompt="preflight", reference_name="dummy.png")
        required_nodes = sorted({str(v["class_type"]) for v in [*wf1.values(), *wf2.values()] if isinstance(v, dict) and v.get("class_type")})
        if object_info is None:
            add("H3 ComfyUI nodes", "BLOCKED", "no /object_info")
        else:
            missing_nodes = [n for n in required_nodes if n not in object_info]
            if missing_nodes:
                add("H3 ComfyUI nodes", "MISSING", ", ".join(missing_nodes), remediation="install/fix H3 ComfyUI custom nodes", data={"required_nodes": required_nodes, "missing_nodes": missing_nodes})
            else:
                add("H3 ComfyUI nodes", "PASS", f"required={len(required_nodes)}, all present")
    except Exception as exc:
        add("H3 ComfyUI nodes", "MISCONFIGURED", f"{type(exc).__name__}: {exc}")

    models_root = Path(settings.h3_comfyui_dir) / "models"
    model_names = {
        "H3 FL2VA": settings.h3_fl2va_model,
        "H3 REF2VA": settings.h3_ref2va_model,
        "H3 text encoder": settings.h3_text_encoder,
        "H3 video VAE": settings.h3_video_vae,
        "H3 audio VAE": settings.h3_audio_vae,
    }
    for label, filename in model_names.items():
        found = find_file(models_root, str(filename))
        if found:
            add(label, "PASS", str(found))
        else:
            add(label, "MISSING", f"{filename} not found under {models_root}", remediation="add model or correct H3 config")

# FFmpeg / media.
ffmpeg = shutil.which("ffmpeg")
ffprobe = shutil.which("ffprobe")
add("ffmpeg", "PASS" if ffmpeg else "MISSING", ffmpeg or "ffmpeg not in PATH")
add("ffprobe", "PASS" if ffprobe else "MISSING", ffprobe or "ffprobe not in PATH")
if ffmpeg:
    enc = run([ffmpeg, "-hide_banner", "-encoders"], timeout=20)
    text = enc.stdout + "\n" + enc.stderr
    add("FFmpeg libx264", "PASS" if "libx264" in text else "MISSING", "available" if "libx264" in text else "libx264 encoder missing")
    add("FFmpeg AAC", "PASS" if "aac" in text.lower() else "MISSING", "available" if "aac" in text.lower() else "AAC encoder missing")
    add("FFmpeg NVENC", "PASS" if "h264_nvenc" in text else "MISSING", "h264_nvenc available" if "h264_nvenc" in text else "no h264_nvenc; libx264 fallback will be used", required=False)

for mod in ("app.v3.media.tts", "app.v3.media.subtitle", "app.v3.media.bgm", "app.v3.media.composition", "app.v3.media.pipeline"):
    try:
        __import__(mod)
        add(f"media module:{mod.rsplit('.', 1)[-1]}", "PASS", "import ok")
    except Exception as exc:
        add(f"media module:{mod.rsplit('.', 1)[-1]}", "BLOCKED", f"{type(exc).__name__}: {exc}")

# Temporal.
if tcp_open(TEMPORAL_HOST, TEMPORAL_PORT):
    add("Temporal server", "PASS", f"{TEMPORAL_HOST}:{TEMPORAL_PORT}")
else:
    add("Temporal server", "MISSING", f"{TEMPORAL_HOST}:{TEMPORAL_PORT} unreachable", remediation="start/deploy Temporal or set TEMPORAL_HOST/TEMPORAL_PORT")

pgrep = shutil.which("pgrep")
if pgrep:
    proc = run([pgrep, "-af", "xiaoduan|app.v3.workflow|temporal"], timeout=5)
    lines = [x for x in proc.stdout.splitlines() if "v3_environment_preflight" not in x]
    worker_lines = [x for x in lines if "worker" in x.lower() and ("xiaoduan" in x.lower() or "app.v3.workflow" in x.lower())]
    if worker_lines:
        add("Xiaoduan Temporal worker", "PASS", worker_lines[0][:300])
    else:
        add("Xiaoduan Temporal worker", "MISSING", "no V3 Temporal worker process", remediation="start the concrete V3 worker after domain-step wiring")
else:
    add("Xiaoduan Temporal worker", "BLOCKED", "pgrep unavailable", required=False)

# Disk / GPU.
try:
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / 1024**3
    total_gb = usage.total / 1024**3
    if free_gb >= MIN_FREE_GB:
        add("disk free", "PASS", f"{free_gb:.1f} GiB free / {total_gb:.1f} GiB")
    else:
        add("disk free", "MISSING", f"{free_gb:.1f} GiB < {MIN_FREE_GB:.1f} GiB", remediation="clean cache/model duplicates or expand disk")
except Exception as exc:
    add("disk free", "BLOCKED", f"{type(exc).__name__}: {exc}")

nvsmi = shutil.which("nvidia-smi")
if not nvsmi:
    add("GPU", "MISSING", "nvidia-smi not found")
else:
    gpu = run([nvsmi, "--query-gpu=index,name,memory.used,memory.free,memory.total", "--format=csv,noheader,nounits"], timeout=10)
    if gpu.returncode != 0:
        add("GPU", "BLOCKED", gpu.stderr.strip() or "nvidia-smi failed")
    else:
        rows = [x.strip() for x in gpu.stdout.splitlines() if x.strip()]
        min_free_mb = int(getattr(settings, "gpu_min_free_mb", 8000)) if settings is not None else 8000
        free_values = []
        for row in rows:
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 5:
                try:
                    free_values.append(int(parts[3]))
                except Exception:
                    pass
        if free_values and max(free_values) >= min_free_mb:
            add("GPU", "PASS", " | ".join(rows), data={"min_free_mb": min_free_mb})
        else:
            add("GPU", "MISCONFIGURED", f"free VRAM below {min_free_mb} MiB: {' | '.join(rows)}", remediation="release GPU memory or use the GPU orchestrator before H3 E2E")

blocking = [x for x in results if x["required"] and x["status"] in {"MISSING", "MISCONFIGURED", "BLOCKED"}]
summary = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "root": str(ROOT),
    "report": str(REPORT),
    "expected_branch": EXPECTED_BRANCH,
    "ready_for_full_e2e": not blocking,
    "counts": {status: sum(1 for x in results if x["status"] == status) for status in ("PASS", "MISSING", "MISCONFIGURED", "BLOCKED")},
    "blocking_count": len(blocking),
    "blocking_checks": [x["name"] for x in blocking],
    "results": results,
}
REPORT.parent.mkdir(parents=True, exist_ok=True)
REPORT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
for key, value in summary["counts"].items():
    print(f"{key:<13}: {value}")
print(f"BLOCKING      : {len(blocking)}")
print(f"FULL E2E READY: {'YES' if not blocking else 'NO'}")
print(f"REPORT        : {REPORT}")

if blocking:
    print("\nNeed to fix first:")
    for idx, item in enumerate(blocking, 1):
        print(f"{idx}. [{item['status']}] {item['name']} -> {item['detail']}")
        if item.get("remediation"):
            print(f"   remediation: {item['remediation']}")
    raise SystemExit(2)

print("\nEnvironment preflight passed; proceed to real 雪山寻剑 E2E.")
raise SystemExit(0)
