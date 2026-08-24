#!/usr/bin/env bash
set -Eeuo pipefail
python3 - <<'PY'
import json
import time
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = 'http://127.0.0.1:6008'
request = urllib.request.Request(base + '/api/gpu/activate/gemma', data=b'', method='POST')
with opener.open(request, timeout=30) as response:
    print(json.dumps(json.load(response), ensure_ascii=False, indent=2))

deadline = time.time() + 900
last = None
while time.time() < deadline:
    with opener.open(base + '/api/gpu/status', timeout=10) as response:
        last = json.load(response)
    print(last.get('owner'), last.get('desired_owner'), last.get('phase'), last.get('message'))
    if last.get('owner') == 'gemma' and last.get('phase') == 'READY':
        break
    if last.get('phase') == 'FAILED':
        raise SystemExit(json.dumps(last, ensure_ascii=False))
    time.sleep(2)
else:
    raise SystemExit('Gemma GPU 工作区切换超时：' + json.dumps(last, ensure_ascii=False))

body = json.dumps({
    'text': '雨后的玻璃温室，柔和自然光',
    'mode': 'optimize',
    'width': 1024,
    'height': 1024,
}, ensure_ascii=False).encode('utf-8')
request = urllib.request.Request(
    base + '/api/gemma', data=body,
    headers={'Content-Type': 'application/json'}, method='POST'
)
with opener.open(request, timeout=360) as response:
    result = json.load(response)
print(json.dumps(result, ensure_ascii=False, indent=2))
if not str(result.get('positive_prompt', '')).strip():
    raise SystemExit('Gemma 实际推理没有返回正向提示词')
PY
