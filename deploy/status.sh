#!/usr/bin/env bash
source "$(dirname "$0")/env.sh"
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader
for p in "$(studio_port):AI-Studio" 8188:ComfyUI "$(llm_port):llama-server"; do
  port=${p%%:*}; name=${p#*:}
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$port/"; then echo "✓ $name ($port)"; else echo "✗ $name ($port)"; fi
done
df -h "$BASE" | tail -1
