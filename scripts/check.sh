#!/usr/bin/env bash
set -u

echo "===== 平台进程 ====="
ps -ef | grep '[u]vicorn app.main:app' || true

echo
echo "===== 服务端口 ====="
python3 - <<'PY'
import urllib.request

checks = [
    ("统一平台", "http://127.0.0.1:6008/api/health"),
    ("Gemma", "http://127.0.0.1:6006/v1/models"),
    ("ComfyUI", "http://127.0.0.1:8188/system_stats"),
]
for name, url in checks:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            print(f"{name}: HTTP {response.status}")
    except Exception as exc:
        print(f"{name}: 不可用或未激活：{exc}")
PY

echo
echo "===== GPU ====="
nvidia-smi --query-gpu=name,memory.used,memory.free,memory.total \
  --format=csv,noheader 2>/dev/null || true

echo
echo "===== 平台日志 ====="
tail -n 80 /root/autodl-tmp/ai-studio/logs/platform-v2.log 2>/dev/null || true
