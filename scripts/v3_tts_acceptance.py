#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


API = os.environ.get("XIAODUAN_V3_BASE", "http://127.0.0.1:6008").rstrip("/")
OUT = Path(
    os.environ.get(
        "XIAODUAN_TTS_ACCEPTANCE_OUT",
        "/root/autodl-tmp/manual-upload/v3-global-acceptance/tts-smoke.mp3",
    )
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main() -> None:
    payload = json.dumps(
        {
            "provider_id": "edge-tts-gateway",
            "model_id": "edge-tts",
            "text": "小段映画语音链路验收。雪山古道上，旅人缓缓向前走去。",
            "voice": "zh-CN-XiaoxiaoNeural",
            "response_format": "mp3",
            "speed": 1.0,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API}/api/v3/generation/tts",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=180) as response:
        result = json.load(response)

    download_path = str(result["download_path"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with opener.open(f"{API}{download_path}", timeout=60) as response:
        content = response.read()
    if len(content) < 1024:
        raise SystemExit(f"TTS ACCEPTANCE FAILED: audio too small ({len(content)} bytes)")
    OUT.write_bytes(content)
    print("TTS ACCEPTANCE: PASS")
    print("provider_id:", result.get("provider_id"))
    print("model_id:", result.get("model_id"))
    print("bytes:", len(content))
    print("output:", OUT)


if __name__ == "__main__":
    main()
