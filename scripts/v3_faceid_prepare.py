#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

COMFY_BASE = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188").rstrip("/")
MODEL_ROOT = Path(os.environ.get("XIAODUAN_IMAGE_MODELS", "/root/autodl-tmp/models/image"))
COMFY_PY = Path(os.environ.get("XIAODUAN_COMFY_PY", "/root/autodl-tmp/envs/ai-studio-comfy/bin/python"))

required_files = {
    "FaceID Plus V2 SDXL": "ip-adapter-faceid-plusv2_sdxl.bin",
    "FaceID Plus V2 SDXL LoRA": "ip-adapter-faceid-plusv2_sdxl_lora.safetensors",
    "CLIP Vision ViT-H": "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
}
required_nodes = {"IPAdapterUnifiedLoaderFaceID", "IPAdapterFaceID", "LoadImage", "CheckpointLoaderSimple"}


def find_file(name: str) -> Path | None:
    if not MODEL_ROOT.exists():
        return None
    for p in MODEL_ROOT.rglob(name):
        if p.is_file():
            return p
    return None


print("xiaoduan映画 V3 FaceID readiness")
print("=" * 72)

ok = True
for label, filename in required_files.items():
    found = find_file(filename)
    if found:
        print(f"[PASS] {label}: {found}")
    else:
        ok = False
        print(f"[MISS] {label}: {filename}")

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
except Exception as exc:
    ok = False
    print(f"[MISS] Comfy /object_info: {type(exc).__name__}: {exc}")

if COMFY_PY.is_file():
    import subprocess
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
    print("  /root/autodl-tmp/models/image/ipadapter/ip-adapter-faceid-plusv2_sdxl.bin")
    print("  /root/autodl-tmp/models/image/loras/ip-adapter-faceid-plusv2_sdxl_lora.safetensors")
    print("  /root/autodl-tmp/models/image/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors")
    sys.exit(1)
