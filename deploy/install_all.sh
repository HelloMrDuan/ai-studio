#!/usr/bin/env bash
# 一键安装：bash deploy/install_all.sh [--r2v] [--no-llm] [--voice]
# 每一步都可以单独重跑；中途失败修好问题后重新执行即可（已完成的部分会跳过）。
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
source "$D/env.sh"
NO_LLM=0; VOICE=0; for a in "$@"; do [ "$a" = "--no-llm" ] && NO_LLM=1; [ "$a" = "--voice" ] && VOICE=1; done
bash "$D/00_system.sh"
bash "$D/01_comfyui.sh"
bash "$D/download_models.sh" "$@"
[ $NO_LLM = 1 ] || bash "$D/03_llama.sh"
bash "$D/04_facefusion.sh"
bash "$D/05_app.sh"
[ $VOICE = 1 ] && bash "$D/06_voice.sh"
if [ "$HAS_GPU" != 1 ]; then
  log "无卡模式安装完成 ✅  环境和模型都已就绪。"
  echo "下一步：用 GPU 模式开机，然后执行："
  echo "  bash deploy/install_all.sh   # 会自动跳过已完成的部分，只补编译 llama.cpp 并启动服务"
  exit 0
fi
bash "$D/start.sh"
sleep 5
"$APP_PY" "$APP_DIR/scripts/preflight.py" || true
