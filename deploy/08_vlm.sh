#!/usr/bin/env bash
# 第 8 步（可选）：视觉审片（P2-6）。给本地文字模型 Qwen3.6 加上视觉投影（mmproj），审片和写剧本共用一个模型、不额外占显存；
# 再装 faster-whisper 做台词音画核对。可在无卡模式下运行（只下载），自检需要 GPU 模式。
#   bash deploy/08_vlm.sh            # mmproj + 语音识别
#   bash deploy/08_vlm.sh --no-asr   # 只装视觉
# 用外部视觉接口（如百炼 qwen-vl、OpenAI 兼容服务）则不用跑本脚本，在 .env 设置 VLM_BASE_URL / VLM_API_KEY / VLM_MODEL 即可。
set -euo pipefail
source "$(dirname "$0")/env.sh"
NO_ASR=0; for a in "$@"; do [ "$a" = "--no-asr" ] && NO_ASR=1; done
cd "$APP_DIR"; [ -f .env ] || die "请先运行 deploy/05_app.sh"
setenv() { if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi; }

# ---------- 1. 视觉投影 mmproj（和 LLM_GGUF 同一个仓库） ----------
REPO=${VLM_REPO:-bartowski/Qwen_Qwen3.6-35B-A3B-GGUF}
log "查找 $REPO 中的 mmproj 文件"
NAME=$(curl -fsSL "$HF_ENDPOINT/api/models/$REPO" | "$APP_PY" -c "
import json,sys
fs=[s['rfilename'] for s in json.load(sys.stdin).get('siblings',[]) if 'mmproj' in s['rfilename'].lower()]
fs.sort(key=lambda f:(0 if 'f16' in f.lower() and 'bf16' not in f.lower() else 1 if 'bf16' in f.lower() else 2, f))
print(fs[0] if fs else '')")
[ -n "$NAME" ] || die "仓库里没有 mmproj 文件：该模型可能不带视觉，改用外部视觉接口（VLM_BASE_URL）"
DEST=$LLM_DIR/$(basename "$NAME")
if [ ! -s "$DEST" ] || [ -f "$DEST.aria2" ]; then
  log "下载 $NAME"
  aria2c -c -x 16 -s 16 -k 20M --file-allocation=none --console-log-level=warn --summary-interval=30 \
    -d "$LLM_DIR" -o "$(basename "$NAME")" "https://www.modelscope.cn/models/$REPO/resolve/master/$NAME" \
  || aria2c -c -x 16 -s 16 -k 20M --file-allocation=none --console-log-level=warn -d "$LLM_DIR" -o "$(basename "$NAME")" \
       "$HF_ENDPOINT/$REPO/resolve/main/$NAME"
fi
setenv LLM_MMPROJ "$DEST"
setenv VLM_PROVIDER auto
log "✓ 视觉投影：$DEST（llama-server 下次启动时自动加载 --mmproj）"

# ---------- 2. 音画核对：faster-whisper ----------
if [ "$NO_ASR" = 0 ]; then
  ASR_PY=$ENVS/asr/bin/python
  if [ ! -x "$ASR_PY" ]; then
    log "创建语音识别环境"
    "$BASE_PY" -m venv "$ENVS/asr"
    "$ASR_PY" -m pip install -q -U pip
  fi
  "$ASR_PY" -c "import faster_whisper" 2>/dev/null || "$ASR_PY" -m pip install -q faster-whisper
  log "预下载 whisper small 模型"
  WD=$BASE/models/faster-whisper-small
  if [ -s "$WD/model.bin" ]; then setenv ASR_MODEL "$WD"
  elif HF_ENDPOINT=$HF_ENDPOINT "$ASR_PY" -c "from faster_whisper import download_model; print(download_model('small'))"; then :
  else
    log "HF 镜像下载失败，改从魔搭社区下载 Systran/faster-whisper-small"
    mkdir -p "$WD"; ok=1
    for f in config.json model.bin tokenizer.json vocabulary.txt preprocessor_config.json; do
      aria2c -c -x 8 -s 8 --file-allocation=none --console-log-level=warn -d "$WD" -o "$f" \
        "https://www.modelscope.cn/models/Systran/faster-whisper-small/resolve/master/$f" || { [ "$f" = preprocessor_config.json ] || ok=0; }
    done
    if [ $ok = 1 ]; then setenv ASR_MODEL "$WD"; else log "⚠ 模型预下载失败，首次使用时会再试"; fi
  fi
  setenv ASR_PYTHON "$ASR_PY"
  log "✓ 语音识别：$ASR_PY"
fi

# ---------- 3. 自检（需要 GPU 模式） ----------
if [ "$HAS_GPU" = 1 ]; then
  log "重启服务并自检视觉模型"
  bash deploy/stop.sh >/dev/null 2>&1 || true; bash deploy/start.sh
  "$APP_PY" scripts/vlm_selftest.py || log "⚠ 自检未通过，查看 $LOG_DIR/llama-server.log"
else
  log "无卡模式：已完成下载，开卡后运行 $APP_PY scripts/vlm_selftest.py 自检"
fi
