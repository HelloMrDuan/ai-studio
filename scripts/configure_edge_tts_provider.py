#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Register Xiaoduan Edge TTS in providers.v3.json")
    parser.add_argument(
        "--data-dir",
        default="/root/autodl-tmp/ai-studio/data/platform-v2",
    )
    parser.add_argument("--port", type=int, default=6011)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "providers.v3.json"

    data: dict[str, object]
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = raw if isinstance(raw, dict) else {}
    else:
        data = {}

    models = data.get("models")
    if not isinstance(models, list):
        models = []

    provider = {
        "provider_id": "edge-tts-gateway",
        "model_id": "edge-tts",
        "transport": "local_openai_compatible",
        "base_url": f"http://127.0.0.1:{args.port}/v1",
        "capabilities": ["tts"],
        "priority": 10,
        "metadata": {
            "backend": "Microsoft Edge speech service via local gateway",
            "local_gateway": True,
            "no_silent_fallback": True,
        },
    }

    kept = [
        item for item in models
        if not (
            isinstance(item, dict)
            and item.get("provider_id") == provider["provider_id"]
            and item.get("model_id") == provider["model_id"]
        )
    ]
    kept.append(provider)
    data["models"] = kept

    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
