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
PROFILE=$(cat "$BASE/.studio_profile" 2>/dev/null || echo 32g)
if [ "$PROFILE" = 24g ]; then
  log "写入 24GB 显卡配置（H3 w6a8 + Qwen3.6 IQ4_XS）"
  sed -i 's#^VIDEO_I2V_UNET=.*#VIDEO_I2V_UNET=minimax_h3_fl2va_pruned_w6a8.safetensors#; s#^VIDEO_R2V_UNET=.*#VIDEO_R2V_UNET=minimax_h3_ref2va_pruned_w6a8.safetensors#; s#^LLM_GGUF=.*#LLM_GGUF=/root/autodl-tmp/models/llm/Qwen_Qwen3.6-35B-A3B-IQ4_XS.gguf#' .env
else
  sed -i 's#^VIDEO_I2V_UNET=.*#VIDEO_I2V_UNET=minimax_h3_fl2va_pruned_int8_convrot.safetensors#; s#^VIDEO_R2V_UNET=.*#VIDEO_R2V_UNET=minimax_h3_ref2va_pruned_int8_convrot.safetensors#; s#^LLM_GGUF=.*#LLM_GGUF=/root/autodl-tmp/models/llm/Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf#' .env
fi
grep -E "^(VIDEO_I2V_UNET|LLM_GGUF)=" .env
mkdir -p "$BASE/data/studio"
log "AI Studio 安装完成"
