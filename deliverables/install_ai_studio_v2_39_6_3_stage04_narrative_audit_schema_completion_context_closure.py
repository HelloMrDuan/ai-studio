#!/usr/bin/env python3
"""Transactional V2.39.6.3 narrative audit schema-completion context closure."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASELINE_VERSION = "2.39.6.3-stage04-full-pipeline-preflight"
TARGET_VERSION = BASELINE_VERSION
INSTALLER_VERSION = (
    "V2.39.6.3-stage04-narrative-audit-schema-completion-context-closure"
)
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
    "app/main.py": "0e4fa0e14fdd3015e83fae1f55c54b12caee60d4240f1f71f4d2279ebc1742f9",
}
WRITE_FILES = ("app/main.py",)
REQUIRED_ROOT_FILES = tuple(Path(rel) for rel in BASELINE_SHA_MANIFEST)
ACTIVE = {
    "starting", "warming", "queued", "switching_gpu", "running",
    "repairing", "auditing", "persisting", "generating",
}


SCENE_METADATA_OLD = """    # Classification 只输出 ID partition，适合大 batch。
    # Grouping JSON 较重，因此控制在 20 anchors。"""
SCENE_METADATA_NEW = """    # Schema completion transports deterministic identity only.
    # Classification/grouping prompts continue reading chunk text explicitly.
    for selected_chunk in chunks:
        selected_chunk["scene_id"] = str(scene.get("scene_id") or "")

    # Classification 只输出 ID partition，适合大 batch。
    # Grouping JSON 较重，因此控制在 20 anchors。"""


HELPER_OLD = """    return compact_prompt


async def _studio_v2372b_complete_audit_schema("""
HELPER_NEW = """    return compact_prompt


_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT = 6000


def _studio_v23963_schema_completion_payload(
    *,
    chunk: dict,
    beats: list[dict],
    support_ids: list[str],
    prior_audit: object,
    prior_missing: list[str],
) -> dict:
    scene_id = str(chunk.get("scene_id") or "").strip()
    if not scene_id:
        scene_id = "scene-chunk-" + str(chunk.get("index") or "unknown")
    audit_id = scene_id + ":narrative-beat:" + str(
        chunk.get("index") or "unknown"
    )

    evidence_ids = []
    for value in support_ids or []:
        key = str(value or "").strip()
        if key and key not in evidence_ids:
            evidence_ids.append(key)

    beat_binding = []
    temporal_fields = []
    for index, beat in enumerate(beats or [], 1):
        if not isinstance(beat, dict):
            continue
        ids = []
        for value in beat.get("source_evidence_ids") or []:
            key = str(value or "").strip()
            if key and key not in ids:
                ids.append(key)
            if key and key not in evidence_ids:
                evidence_ids.append(key)
        beat_binding.append({
            "beat_index": index,
            "evidence_ids": ids,
        })
        starts = []
        ends = []
        for span in beat.get("source_evidence_spans") or []:
            if not isinstance(span, dict):
                continue
            try:
                starts.append(int(span.get("start") or 0))
                ends.append(int(span.get("end") or 0))
            except Exception:
                continue
        temporal_fields.append({
            "beat_index": index,
            "source_start": min(starts) if starts else None,
            "source_end": max(ends) if ends else None,
        })

    missing_fields = []
    for value in prior_missing or []:
        key = str(value or "").strip()
        if key and key not in missing_fields:
            missing_fields.append(key)

    audit_conclusion = {}
    if isinstance(prior_audit, dict):
        for field in (
            "valid",
            "event_coverage_ok",
            "granularity_ok",
            "evidence_entailment_ok",
            "temporal_order_ok",
            "support_classification_ok",
            "violations",
        ):
            if field in prior_audit:
                audit_conclusion[field] = _studio_v2372_copy.deepcopy(
                    prior_audit[field]
                )

    return {
        "scene_id": scene_id,
        "audit_id": audit_id,
        "missing_fields": missing_fields,
        "previous_audit_result": {
            "audit": audit_conclusion,
            "evidence_ids": evidence_ids,
            "beat_binding": beat_binding,
            "temporal_fields": temporal_fields,
        },
        "required_schema": {
            "type": "object",
            "required": [
                "valid",
                "event_coverage_ok",
                "granularity_ok",
                "evidence_entailment_ok",
                "temporal_order_ok",
                "support_classification_ok",
                "violations",
            ],
            "boolean_fields": [
                "valid",
                "event_coverage_ok",
                "granularity_ok",
                "evidence_entailment_ok",
                "temporal_order_ok",
                "support_classification_ok",
            ],
            "violations_type": "array[string]",
            "invariants": [
                "preserve every already-present audit conclusion",
                "valid equals all five *_ok fields and empty violations",
                "do not alter evidence_ids, beat_binding, or temporal_fields",
            ],
        },
    }


async def _studio_v23963_prepare_schema_completion_prompt(
    *,
    phase: str,
    system_prompt: str,
    chunk: dict,
    beats: list[dict],
    support_ids: list[str],
    prior_audit: object,
    prior_missing: list[str],
) -> str:
    payload = _studio_v23963_schema_completion_payload(
        chunk=chunk,
        beats=beats,
        support_ids=support_ids,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )
    allowed = {
        "scene_id",
        "audit_id",
        "missing_fields",
        "previous_audit_result",
        "required_schema",
    }
    if set(payload) != allowed:
        raise RuntimeError(
            f"{phase}: schema completion payload fields mismatch; "
            f"fields={sorted(payload)}"
        )

    forbidden = {"source_text", "full_anchors", "full_beats"}

    def assert_no_forbidden(value: object) -> None:
        if isinstance(value, dict):
            overlap = forbidden.intersection(str(key) for key in value)
            if overlap:
                raise RuntimeError(
                    f"{phase}: forbidden schema completion fields={sorted(overlap)}"
                )
            for nested in value.values():
                assert_no_forbidden(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_forbidden(nested)

    assert_no_forbidden(payload)
    prompt = _studio_json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for marker in (
        "=== CORE_SOURCE_CHUNK ===",
        "=== SOURCE_ANCHORS ===",
        "=== PROPOSED_BEATS ===",
    ):
        if marker in prompt:
            raise RuntimeError(
                f"{phase}: forbidden full audit section in schema completion"
            )

    counter = getattr(director, "_count_prompt_tokens", None)
    if not callable(counter):
        raise RuntimeError(
            f"{phase}: active llama.cpp tokenizer is required for schema budget"
        )
    tokens, estimator = await counter(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    if estimator != "llama_tokenize":
        raise RuntimeError(
            f"{phase}: real llama.cpp schema token budget unavailable; "
            f"estimator={estimator}"
        )
    if tokens > _STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT:
        raise RuntimeError(
            f"{phase}: minimal schema completion payload exceeds token budget; "
            f"tokens={tokens} "
            f"limit={_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT}"
        )
    print(
        "[V2.39.6.3][Stage04][SchemaCompletionBudget] "
        f"phase={phase} tokens={tokens} "
        f"limit={_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT}",
        flush=True,
    )
    return prompt


async def _studio_v2372b_complete_audit_schema("""


SCHEMA_SYSTEM_OLD = """    system_prompt = (
        "你是 Narrative Beat 审计结果结构补全器。"
        "你仍然必须独立审计正文和 Beat，不能沿用 prior_audit 的结论。"
        "分类只能基于当前 Scene 的最小有序叙事状态图和证据依赖，"
        "不得使用固定关键词、文本类别、题材类型或预设业务词表。"
        "必须逐项输出以下五个 boolean："
        "event_coverage_ok、granularity_ok、evidence_entailment_ok、"
        "temporal_order_ok、support_classification_ok。"
        "如果任何一项为 false，violations 必须至少写出一条具体原因；"
        "如果全部为 true，violations 必须为空数组。"
        "valid 必须等于上述五项全部为 true 且 violations 为空。"
        "禁止省略字段，禁止只返回 valid。只输出严格 JSON。"
    )"""
SCHEMA_SYSTEM_NEW = """    system_prompt = (
        "你是 Narrative Beat 审计结果 Schema 补全器，不重新执行语义审计。"
        "只根据 previous_audit_result 补齐 missing_fields，"
        "必须保留所有已经存在的 audit 结论。"
        "不得修改 evidence_ids、beat_binding 或 temporal_fields。"
        "输出必须满足 required_schema：显式返回 valid、五个 *_ok boolean "
        "以及 violations。valid 必须等于五个 *_ok 全部为 true 且 "
        "violations 为空。禁止引入正文、anchor 或 Beat 新事实。"
        "只输出补全后的严格 JSON audit 对象。"
    )"""


SCHEMA_PROMPT_OLD = """    prompt = await _studio_v23963_prepare_audit_prompt(
        phase=(
            "studio_stage04_"
            "narrative_beat_audit_schema_completion_qwen32b"
        ),
        system_prompt=system_prompt,
        chunk=chunk,
        anchors=anchors,
        beats=beats,
        support_ids=support_ids,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )"""
SCHEMA_PROMPT_NEW = """    prompt = await _studio_v23963_prepare_schema_completion_prompt(
        phase=(
            "studio_stage04_"
            "narrative_beat_audit_schema_completion_qwen32b"
        ),
        system_prompt=system_prompt,
        chunk=chunk,
        beats=beats,
        support_ids=support_ids,
        prior_audit=prior_audit,
        prior_missing=prior_missing,
    )"""


SCHEMA_MESSAGE_OLD = """                messages=[{
                    "role": "user",
                    "content": prompt + (
                        ""
                        if attempt == 0
                        else (
                            "\\n\\nSTRICT_SCHEMA_RETRY："
                            "六个顶层字段 valid + 五个 *_ok "
                            "以及 violations 必须全部显式返回；"
                            "不得输出 reasons 代替这些字段。"
                        )
                    ),
                }],"""
SCHEMA_MESSAGE_NEW = """                messages=[{
                    "role": "user",
                    "content": prompt,
                }],"""


SCHEMA_MERGE_OLD = """        decision, violations, missing = (
            _studio_v2372b_audit_violations(
                audit,
                required=required,
            )
        )"""
SCHEMA_MERGE_NEW = """        audit = dict(audit) if isinstance(audit, dict) else {}
        if isinstance(prior_audit, dict):
            # Schema completion fills absent fields; it cannot rewrite an
            # existing conclusion from the primary semantic audit.
            for field in ("valid", *required, "violations"):
                if field in prior_audit:
                    audit[field] = _studio_v2372_copy.deepcopy(
                        prior_audit[field]
                    )

        decision, violations, missing = (
            _studio_v2372b_audit_violations(
                audit,
                required=required,
            )
        )"""


GLOBAL_PATCHES = (
    ("scene identity metadata", SCENE_METADATA_OLD, SCENE_METADATA_NEW),
    ("schema completion helpers", HELPER_OLD, HELPER_NEW),
)
SCHEMA_PATCHES = (
    ("schema system contract", SCHEMA_SYSTEM_OLD, SCHEMA_SYSTEM_NEW),
    ("five-field schema prompt", SCHEMA_PROMPT_OLD, SCHEMA_PROMPT_NEW),
    ("JSON-only retry body", SCHEMA_MESSAGE_OLD, SCHEMA_MESSAGE_NEW),
    ("preserve prior audit conclusions", SCHEMA_MERGE_OLD, SCHEMA_MERGE_NEW),
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    require(count == 1, f"patch anchor {label!r} expected once, found {count}")
    return text.replace(old, new, 1)


def patch_schema_function(
    text: str,
    patches: tuple[tuple[str, str, str], ...],
) -> str:
    start = text.index("async def _studio_v2372b_complete_audit_schema(")
    end = text.index("async def _studio_v2372_audit_extraction(", start)
    block = text[start:end]
    for label, old, new in patches:
        block = replace_once(block, old, new, label)
    return text[:start] + block + text[end:]


def transform_main(baseline: bytes) -> bytes:
    require(
        sha(baseline) == BASELINE_SHA_MANIFEST["app/main.py"],
        "baseline SHA256 mismatch: app/main.py",
    )
    text = baseline.decode("utf-8")
    for label, old, new in GLOBAL_PATCHES:
        text = replace_once(text, old, new, label)
    text = patch_schema_function(text, SCHEMA_PATCHES)
    return text.encode("utf-8")


def build_target(baseline: bytes) -> bytes:
    target = transform_main(baseline)
    require(
        sha(target) == TARGET_SHA_MANIFEST["app/main.py"],
        "target SHA256 construction mismatch: app/main.py",
    )
    return target


def reverse_target(target: bytes) -> bytes:
    require(
        sha(target) == TARGET_SHA_MANIFEST["app/main.py"],
        "target fixture SHA256 mismatch: app/main.py",
    )
    text = target.decode("utf-8")
    reverse_schema = tuple(
        (label, new, old) for label, old, new in reversed(SCHEMA_PATCHES)
    )
    text = patch_schema_function(text, reverse_schema)
    for label, old, new in reversed(GLOBAL_PATCHES):
        text = replace_once(text, new, old, "reverse " + label)
    baseline = text.encode("utf-8")
    require(
        sha(baseline) == BASELINE_SHA_MANIFEST["app/main.py"],
        "rollback payload does not restore archive baseline",
    )
    return baseline


def root_valid(root: Path) -> bool:
    return root.is_dir() and all((root / rel).is_file() for rel in REQUIRED_ROOT_FILES)


def discover_platform_root(manual: Path | None = None) -> Path:
    checked = []
    candidates = ((manual,) if manual is not None else ROOT_CANDIDATES)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        checked.append(str(resolved))
        if root_valid(resolved):
            return resolved
    raise RuntimeError(
        "platform root candidates checked:\n" + "\n".join(checked) + "\nnot found"
    )


def python_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            text=True,
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
    raise RuntimeError(
        "platform Python candidates checked:\n" + "\n".join(checked) + "\nnot found"
    )


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
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
                require(
                    str(row.get("status") or "").lower() not in ACTIVE,
                    f"active task reported by {endpoint}",
                )
    data_dir = root / "data"
    for pattern in ("stage04_rebuild_tasks/*.json", "tasks/*/task.json", "studio_jobs/*.json"):
        for path in data_dir.glob(pattern):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for row in walk_status_rows(payload):
                require(
                    str(row.get("status") or "").lower() not in ACTIVE,
                    f"active task in {path}",
                )


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
    temp = path.with_name(path.name + ".v23963-schema-closure.tmp")
    temp.write_bytes(data)
    os.chmod(temp, mode)
    temp.replace(path)


def backup_live(
    root: Path,
    backup: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    live = validate_baseline(root)
    target = build_target(live["app/main.py"])
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
            "before_sha256": sha(data),
            "target_sha256": (
                TARGET_SHA_MANIFEST[rel] if rel in TARGET_SHA_MANIFEST else sha(data)
            ),
            "mode": mode,
            "written": rel in WRITE_FILES,
        }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest, {"app/main.py": target}


def restore_exact_backup(root: Path, backup: Path, manifest: dict[str, Any]) -> None:
    for rel in WRITE_FILES:
        item = manifest["files"][rel]
        data = (backup / rel).read_bytes()
        require(sha(data) == item["before_sha256"], f"backup corrupted: {rel}")
        atomic_write(root / rel, data, int(item["mode"]))
    for rel, item in manifest["files"].items():
        require(
            sha((root / rel).read_bytes()) == item["before_sha256"],
            f"rollback hash mismatch: {rel}",
        )


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


def validate_target_source(data: bytes) -> None:
    text = data.decode("utf-8")
    tree = ast.parse(text, filename="app/main.py")
    compile(tree, "app/main.py", "exec")
    for marker in (
        "_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT = 6000",
        "_studio_v23963_prepare_schema_completion_prompt",
        'forbidden = {"source_text", "full_anchors", "full_beats"}',
        "active llama.cpp tokenizer is required for schema budget",
        "real llama.cpp schema token budget unavailable",
        "V2.39.6.3_PERF_PARTIAL_CLASSIFICATION",
        "V2.39.6.3_PERF_MEMBERSHIP_LINE_ONLY",
        "strict-shot-v2",
    ):
        require(marker in text, f"target marker missing: {marker}")
    start = text.index("async def _studio_v2372b_complete_audit_schema(")
    end = text.index("async def _studio_v2372_audit_extraction(", start)
    body = text[start:end]
    require("_studio_v23963_prepare_schema_completion_prompt" in body, "minimal prompt inactive")
    require('"content": prompt +' not in body, "retry appends non-JSON content")
    for marker in (
        "=== CORE_SOURCE_CHUNK ===",
        "=== SOURCE_ANCHORS ===",
        "=== PROPOSED_BEATS ===",
    ):
        require(marker not in body, f"schema completion carries full section: {marker}")


def schema_payload_self_test(target: bytes) -> int:
    tree = ast.parse(target.decode("utf-8"))
    names = {
        "_studio_v23963_schema_completion_payload",
        "_studio_v23963_prepare_schema_completion_prompt",
    }
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    namespace: dict[str, Any] = {
        "_studio_json": json,
        "_studio_v2372_copy": __import__("copy"),
        "_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT": 6000,
    }
    recorded: dict[str, Any] = {}

    class Tokenizer:
        async def _count_prompt_tokens(self, *, system_prompt, messages):
            prompt = messages[0]["content"]
            recorded["prompt"] = prompt
            recorded["tokens"] = len((system_prompt + prompt).encode("utf-8")) // 3 + 128
            return recorded["tokens"], "llama_tokenize"

    namespace["director"] = Tokenizer()
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), "schema-helper-test", "exec"),
        namespace,
    )
    huge_source = "FULL_SOURCE_TEXT " * 8000
    huge_evidence = "NARRATIVE_EVIDENCE_TEXT " * 8000
    prompt = asyncio.run(namespace["_studio_v23963_prepare_schema_completion_prompt"](
        phase="studio_stage04_narrative_beat_audit_schema_completion_qwen32b",
        system_prompt="schema-only",
        chunk={"scene_id": "scene-1", "index": 2, "text": huge_source},
        beats=[{
            "summary": "DUPLICATE_NARRATIVE",
            "source_evidence_ids": ["A1", "A2"],
            "source_evidence": [huge_evidence],
            "source_evidence_spans": [
                {"start": 10, "end": 20},
                {"start": 10, "end": 20},
            ],
        }],
        support_ids=["A3"],
        prior_audit={"valid": True, "event_coverage_ok": True, "violations": []},
        prior_missing=["granularity_ok", "temporal_order_ok"],
    ))
    payload = json.loads(prompt)
    require(
        set(payload) == {
            "scene_id", "audit_id", "missing_fields",
            "previous_audit_result", "required_schema",
        },
        "schema payload top-level contract mismatch",
    )
    require(recorded["tokens"] <= 6000, "8000-token fixture exceeds 6000 after closure")
    for forbidden in (
        huge_source, huge_evidence, "DUPLICATE_NARRATIVE",
        "source_text", "full_anchors", "full_beats",
    ):
        require(forbidden not in prompt, f"forbidden schema payload content: {forbidden[:32]}")
    return int(recorded["tokens"])


def self_test(source_root: Path, python: Path) -> int:
    require(WRITE_FILES == ("app/main.py",), "write scope is not main-only")
    baseline = validate_baseline(source_root)
    target = build_target(baseline["app/main.py"])
    validate_target_source(target)
    require(reverse_target(target) == baseline["app/main.py"], "forward/reverse bytes mismatch")
    token_count = schema_payload_self_test(target)
    with tempfile.TemporaryDirectory(
        prefix="v23963-schema-closure-selftest-",
        dir=source_root.parent,
    ) as td:
        temp = Path(td)
        root = temp / "platform-v2"
        backup = temp / "backup"
        for rel, data in baseline.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest, targets = backup_live(root, backup)
        atomic_write(root / "app/main.py", targets["app/main.py"], 0o644)
        require(
            sha((root / "app/main.py").read_bytes()) == TARGET_SHA_MANIFEST["app/main.py"],
            "self-test target readback failed",
        )
        run([str(python), "-m", "py_compile", str(root / "app/main.py")], 120)
        restore_exact_backup(root, backup, manifest)
        validate_baseline(root)
    print("INSTALLER SELF-TEST PASS")
    print("ARCHIVE BASELINE SHA MANIFEST PASS")
    print("MAIN-ONLY WRITE SCOPE PASS")
    print(f"SCHEMA COMPLETION 8000-TOKEN FIXTURE PASS tokens={token_count} limit=6000")
    print("ROLLBACK ARCHIVE-BYTE RESTORE PASS")
    print("TARGET SHA PASS " + TARGET_SHA_MANIFEST["app/main.py"])
    return 0


def install(root: Path, python: Path, backup_root: Path) -> int:
    print(f"INSTALLER_VERSION={INSTALLER_VERSION}")
    print(f"PLATFORM_ROOT={root}")
    print(f"PLATFORM_PYTHON={python}")
    validate_baseline(root)
    validate_openapi(BASELINE_VERSION)
    check_active_tasks(root)
    backup = backup_root / (
        "platform-v2-v23963-schema-closure-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    manifest, targets = backup_live(root, backup)
    applied = False
    platform_stopped = False
    try:
        stop_platform(root)
        platform_stopped = True
        check_active_tasks(root)
        applied = True
        atomic_write(
            root / "app/main.py",
            targets["app/main.py"],
            int(manifest["files"]["app/main.py"]["mode"]),
        )
        validate_target_source(targets["app/main.py"])
        run([str(python), "-m", "py_compile", str(root / "app/main.py")], 240)
        start_and_verify(root, TARGET_VERSION)
        require(
            sha((root / "app/main.py").read_bytes()) == TARGET_SHA_MANIFEST["app/main.py"],
            "target hash readback mismatch: app/main.py",
        )
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["result"] = "INSTALLED"
        (backup / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(f"BACKUP={backup}")
        print("INSTALL PASS; no Stage04/image/video E2E was executed")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, help="platform root override")
    parser.add_argument("--python", type=Path, help="platform Python override")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/root/autodl-tmp/ai-studio/backups"),
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--source-root", type=Path, help="archive baseline root for self-test")
    parser.add_argument("--print-target-hashes", action="store_true")
    args = parser.parse_args()
    python = discover_platform_python(args.python)
    if args.self_test or args.print_target_hashes:
        source_root = (args.source_root or Path(__file__).resolve().parent.parent).resolve()
        baseline = validate_baseline(source_root)
        if args.print_target_hashes:
            print("app/main.py " + sha(transform_main(baseline["app/main.py"])))
            return 0
        return self_test(source_root, python)
    root = discover_platform_root(args.root)
    return install(root, python, args.backup_root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"INSTALL FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
