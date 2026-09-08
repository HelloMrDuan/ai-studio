#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

COMFY_BASE = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188").rstrip("/")
MODEL_ROOT = Path(os.environ.get("XIAODUAN_IMAGE_MODELS", "/root/autodl-tmp/models/image"))
COMFY_PY = Path(os.environ.get("XIAODUAN_COMFY_PY", "/root/autodl-tmp/envs/ai-studio-comfy/bin/python"))

FACEID_MODEL = "ip-adapter-faceid-plusv2_sdxl.bin"
FACEID_LORA = "ip-adapter-faceid-plusv2_sdxl_lora.safetensors"
CLIP_VISION = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"

required_files = {
    "FaceID Plus V2 SDXL": FACEID_MODEL,
    "FaceID Plus V2 SDXL LoRA": FACEID_LORA,
    "CLIP Vision ViT-H": CLIP_VISION,
}
required_nodes = {
    "IPAdapterUnifiedLoaderFaceID",
    "IPAdapterFaceID",
    "IPAdapterModelLoader",
    "CLIPVisionLoader",
    "LoadImage",
    "CheckpointLoaderSimple",
}


def find_file(name: str) -> Path | None:
    if not MODEL_ROOT.exists():
        return None
    for p in MODEL_ROOT.rglob(name):
        if p.is_file():
            return p
    return None


def input_choices(info: dict[str, Any], node_name: str, input_name: str) -> list[str]:
    """Return the live choices exposed by a ComfyUI node input.

    This is deliberately checked against the running ComfyUI server rather than
    only checking files on disk. Unified IPAdapter loaders resolve models through
    ComfyUI's registered folder_paths, so a file can exist physically while being
    invisible to the loader.
    """
    node = info.get(node_name)
    if not isinstance(node, dict):
        return []
    spec = (((node.get("input") or {}).get("required") or {}).get(input_name))
    if not isinstance(spec, list) or not spec:
        return []
    choices = spec[0]
    if not isinstance(choices, list):
        return []
    return [str(value) for value in choices]


def visible_by_basename(choices: list[str], filename: str) -> bool:
    return any(Path(value).name == filename for value in choices)


print("xiaoduan映画 V3 FaceID readiness")
print("=" * 72)

ok = True
for label, filename in required_files.items():
    found = find_file(filename)
    if found:
        print(f"[PASS] {label} on disk: {found}")
    else:
        ok = False
        print(f"[MISS] {label} on disk: {filename}")

try:
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{COMFY_BASE}/object_info", timeout=20) as r:
        info = json.load(r)

    missing_nodes = sorted(required_nodes - set(info))
    if missing_nodes:
        ok = False
        print(f"[MISS] Comfy nodes: {', '.join(missing_nodes)}")
    else:
        print("[PASS] Comfy FaceID nodes")

    ipadapter_choices = input_choices(info, "IPAdapterModelLoader", "ipadapter_file")
    if visible_by_basename(ipadapter_choices, FACEID_MODEL):
        print(f"[PASS] Comfy-visible FaceID model: {FACEID_MODEL}")
    else:
        ok = False
        print(f"[MISS] Comfy-visible FaceID model: {FACEID_MODEL}")
        print(f"       visible ipadapter files: {', '.join(ipadapter_choices) or '(none)'}")

    lora_choices = input_choices(info, "LoraLoader", "lora_name")
    if not lora_choices:
        lora_choices = input_choices(info, "LoraLoaderModelOnly", "lora_name")
    if visible_by_basename(lora_choices, FACEID_LORA):
        print(f"[PASS] Comfy-visible FaceID LoRA: {FACEID_LORA}")
    else:
        ok = False
        print(f"[MISS] Comfy-visible FaceID LoRA: {FACEID_LORA}")
        print(f"       visible loras: {', '.join(lora_choices[:30]) or '(none)'}")

    clip_choices = input_choices(info, "CLIPVisionLoader", "clip_name")
    if visible_by_basename(clip_choices, CLIP_VISION):
        print(f"[PASS] Comfy-visible CLIP Vision: {CLIP_VISION}")
    else:
        ok = False
        print(f"[MISS] Comfy-visible CLIP Vision: {CLIP_VISION}")
        print(f"       visible clip_vision files: {', '.join(clip_choices) or '(none)'}")
except Exception as exc:
    ok = False
    print(f"[MISS] Comfy /object_info: {type(exc).__name__}: {exc}")

if COMFY_PY.is_file():
    proc = subprocess.run(
        [str(COMFY_PY), "-c", "import insightface; print(insightface.__version__ if hasattr(insightface, '__version__') else 'ok')"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode == 0:
        print(f"[PASS] insightface: {proc.stdout.strip() or 'import ok'}")
    else:
        ok = False
        print(f"[MISS] insightface in Comfy env: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'import failed'}")
else:
    ok = False
    print(f"[MISS] Comfy Python: {COMFY_PY}")

print("=" * 72)
print("FACEID READY:", "YES" if ok else "NO")

if not ok:
    print("\nExpected files:")
    print(f"  /root/autodl-tmp/models/image/ipadapter/{FACEID_MODEL}")
    print(f"  /root/autodl-tmp/models/image/loras/{FACEID_LORA}")
    print(f"  /root/autodl-tmp/models/image/clip_vision/{CLIP_VISION}")
    print("\nIf files exist on disk but are not Comfy-visible, expose them through")
    print("ComfyUI models/ipadapter and models/loras (symlinks are sufficient),")
    print("then restart the actual ComfyUI process before rerunning this check.")
    sys.exit(1)
