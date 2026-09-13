#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "faceid": REPO_ROOT / "config" / "comfyui_reference_profile.v3.sdxl-faceid.json",
    "ipadapter": REPO_ROOT / "config" / "comfyui_reference_profile.v3.sdxl-ipadapter.json",
}


def _validate_profile(raw: dict, source: Path) -> Path:
    workflow_path = Path(str(raw.get("reference_workflow_path") or ""))
    if not workflow_path.is_file():
        raise SystemExit(f"reference workflow does not exist: {workflow_path}")
    bindings = raw.get("reference_bindings")
    if not isinstance(bindings, list):
        raise SystemExit("reference profile reference_bindings must be an array")
    mode = str(raw.get("multi_reference_mode") or "static").strip().lower()
    max_refs = int(raw.get("max_references") or len(bindings) or 1)
    if max_refs < 1:
        raise SystemExit("reference profile max_references must be positive")
    if mode == "role_aware_chain":
        if max_refs < 2:
            raise SystemExit("role-aware profile must support at least two references")
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        missing = [node for node in ("3", "4", "11", "12", "16") if node not in workflow]
        if missing:
            raise SystemExit(f"role-aware workflow missing required nodes: {missing}")
    elif not bindings:
        raise SystemExit("static reference profile requires reference_bindings")
    return workflow_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a Xiaoduan V3 Comfy reference profile")
    parser.add_argument("--data-dir", default="/root/autodl-tmp/ai-studio/data/platform-v2")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="faceid")
    args = parser.parse_args()

    source = PROFILES[args.profile]
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"invalid profile object: {source}")
    workflow_path = _validate_profile(raw, source)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "comfyui_reference_profile.v3.json"
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)

    print(target)
    print(f"profile={args.profile}")
    print(f"workflow={workflow_path}")
    print(f"max_references={raw.get('max_references')}")
    print(f"multi_reference_mode={raw.get('multi_reference_mode') or 'static'}")
    print(f"identity_reference={bool(raw.get('identity_reference'))}")
    print(f"ip_adapter={bool(raw.get('ip_adapter'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
