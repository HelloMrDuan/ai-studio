#!/usr/bin/env bash
# 第 5 步：安装 AI Studio 本体
set -euo pipefail
source "$(dirname "$0")/env.sh"
make_env studio
"$APP_PY" -m pip install -U pip
"$APP_PY" -m pip install -r "$APP_DIR/requirements.txt"
cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  log "已生成 .env（默认本机文字模型；要用外部 API 请修改 LLM_PROVIDER/LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）"
fi
mkdir -p "$BASE/data/studio"
log "AI Studio 安装完成"
