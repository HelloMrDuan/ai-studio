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
  sed -i "s#/root/autodl-tmp#$BASE#g" .env   # 换成本机实际的安装目录
  log "已生成 .env（默认本机文字模型；要用外部 API 请修改 LLM_PROVIDER/LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）"
fi
PROFILE=$(cat "$BASE/.studio_profile" 2>/dev/null || echo 32g)
if [ "$PROFILE" = 24g ]; then
  log "写入 24GB 显卡配置（H3 w6a8 + Qwen3.6 IQ4_XS）"
  sed -i "s#^VIDEO_I2V_UNET=.*#VIDEO_I2V_UNET=minimax_h3_fl2va_pruned_w6a8.safetensors#; s#^VIDEO_R2V_UNET=.*#VIDEO_R2V_UNET=minimax_h3_ref2va_pruned_w6a8.safetensors#; s#^LLM_GGUF=.*#LLM_GGUF=$BASE/models/llm/Qwen_Qwen3.6-35B-A3B-IQ4_XS.gguf#" .env
else
  sed -i "s#^VIDEO_I2V_UNET=.*#VIDEO_I2V_UNET=minimax_h3_fl2va_pruned_int8_convrot.safetensors#; s#^VIDEO_R2V_UNET=.*#VIDEO_R2V_UNET=minimax_h3_ref2va_pruned_int8_convrot.safetensors#; s#^LLM_GGUF=.*#LLM_GGUF=$BASE/models/llm/Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf#" .env
fi
grep -E "^(BASE_DIR|VIDEO_I2V_UNET|LLM_GGUF)=" .env
if [ -d /root/sj-tmp ] && ! grep -q "^PORT=8080" .env; then   # 算家云只对外开放 8080：网页用 8080，文字模型挪到 8081
  sed -i "s#^PORT=.*#PORT=8080#; s#^LLM_BASE_URL=http://127.0.0.1:8080/#LLM_BASE_URL=http://127.0.0.1:8081/#" .env
  log "算家云：网页端口改为 8080（对外开放端口），文字模型改用 8081"
fi
if ! grep -q "^ACCESS_PASSWORD=" .env; then   # 端口开放到公网后，没有密码任何人都能用你的显卡
  PW=$("$APP_PY" -c "import secrets; print(secrets.token_urlsafe(9))")
  printf '\n# 网页访问密码（留空 = 不需要密码）\nACCESS_PASSWORD=%s\n' "$PW" >> .env
  log "已生成网页访问密码：$PW（保存在 .env 的 ACCESS_PASSWORD）"
fi
mkdir -p "$BASE/data/studio"
log "AI Studio 安装完成"
