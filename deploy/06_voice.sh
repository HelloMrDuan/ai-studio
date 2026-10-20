#!/usr/bin/env bash
# 第 6 步（可选）：自动质检 OCR + 固定音色配音（IndexTTS2 + Demucs）+ 对口型（LatentSync 1.6）
#   bash deploy/06_voice.sh               # 全部安装（约 25GB 磁盘：环境 ~12GB + 模型 ~13GB）
#   bash deploy/06_voice.sh --no-lipsync  # 不装对口型
# 可在无卡模式下运行（装环境、下模型）；最后的自检需要 GPU 模式。每一部分都可单独重跑，已完成的会跳过。
set -euo pipefail
source "$(dirname "$0")/env.sh"
NO_LIPSYNC=0; for a in "$@"; do [ "$a" = "--no-lipsync" ] && NO_LIPSYNC=1; done
TTS_DIR=$BASE/index-tts; TTS_PY=$ENVS/tts/bin/python
LS_DIR=$BASE/LatentSync; LS_PY=$ENVS/lipsync/bin/python
export HF_HOME=$BASE/.cache/huggingface TORCH_HOME=$BASE/.cache/torch
export UV_CACHE_DIR=$BASE/.cache/uv UV_PYTHON_INSTALL_DIR=$BASE/.uv-python UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300
export UV_INDEX_URL=$PIP_INDEX_URL UV_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL
[ -n "$GH_PROXY" ] && export UV_PYTHON_INSTALL_MIRROR=${GH_PROXY}https://github.com/astral-sh/python-build-standalone/releases/download
[ -x "$APP_PY" ] || die "请先运行 deploy/05_app.sh"
[ -x "$ENVS/studio/bin/uv" ] || "$APP_PY" -m pip install -q uv
UV=$ENVS/studio/bin/uv
hf_get() {  # hf_get <python> <repo> <local_dir|-> [patterns...]：经 hf-mirror 下载，local_dir 为 - 时放进 HF 缓存
  local py=$1 repo=$2 dir=$3; shift 3
  "$py" - "$repo" "$dir" "$@" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, d, pats = sys.argv[1], sys.argv[2], sys.argv[3:] or None
for i in range(3):
    try:
        print(snapshot_download(repo, local_dir=None if d == "-" else d, allow_patterns=pats, max_workers=8)); break
    except Exception as e:
        print("重试", repo, e)
else:
    sys.exit(f"下载失败：{repo}")
PY
}

# ---------- 1. 自动质检用的 OCR（装进 FaceFusion 环境，复用它的 onnxruntime-gpu，所以 --no-deps） ----------
if [ -x "$FF_PY" ]; then
  if ! "$FF_PY" -c "import rapidocr" 2>/dev/null; then
    log "安装 RapidOCR（质检：检测视频里冒出的乱码字幕）"
    "$FF_PY" -m pip install -q --no-deps "rapidocr>=3"
    "$FF_PY" -m pip install -q pyclipper shapely pyyaml six omegaconf colorlog tqdm requests pillow
  fi
  log "预下载 OCR 模型"
  "$FF_PY" - <<'PY' || echo "⚠️ OCR 自检失败，质检会跳过文字检查"
import numpy as np
from rapidocr import RapidOCR
img = np.full((120, 400, 3), 255, np.uint8)
import cv2
cv2.putText(img, "TEST 123", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
print("OCR 自检：", RapidOCR()(img).txts)
PY
else
  echo "⚠️ 未安装 FaceFusion，自动质检不可用（先运行 deploy/04_facefusion.sh）"
fi

# ---------- 2. IndexTTS2（Python 3.11，torch 2.8） ----------
if [ ! -x "$TTS_PY" ]; then
  log "创建配音环境（Python 3.11）"
  "$UV" python install 3.11 || die "下载 Python 3.11 失败（可 export GH_PROXY= 换直连重试）"
  "$UV" venv -p 3.11 "$ENVS/tts"
fi
[ -d "$TTS_DIR/.git" ] || gh_clone https://github.com/index-tts/index-tts.git "$TTS_DIR" || die "克隆 index-tts 失败"
cd "$TTS_DIR"
if ! "$TTS_PY" -c "import indextts.infer_v2, demucs" 2>/dev/null; then
  log "安装 IndexTTS2 依赖（torch 2.8，约 8GB，十几分钟）"
  VIRTUAL_ENV=$ENVS/tts "$UV" pip install -p "$TTS_PY" --no-sources --index-strategy unsafe-best-match -e . demucs \
    || die "IndexTTS2 依赖安装失败"
fi
if [ ! -f checkpoints/gpt.pth ]; then
  log "下载 IndexTTS2 模型（ModelScope，约 5GB）"
  "$ENVS/tts/bin/modelscope" download --model IndexTeam/IndexTTS-2 --local_dir checkpoints || die "下载 IndexTTS-2 失败"
fi
log "预下载 IndexTTS2 依赖的小模型（hf-mirror）"
hf_get "$TTS_PY" facebook/w2v-bert-2.0 - "*.json" "model.safetensors" "*.txt"
hf_get "$TTS_PY" amphion/MaskGCT - "semantic_codec/model.safetensors"
hf_get "$TTS_PY" funasr/campplus - "campplus_cn_common.bin"
hf_get "$TTS_PY" nvidia/bigvgan_v2_22khz_80band_256x - "*.json" "*generator*" "*.py"

# ---------- 3. Demucs（去掉视频原声里的人声，保留环境音） ----------
if [ ! -f "$TTS_DIR/.demucs_ok" ]; then
  log "预下载 Demucs 模型并自检"
  T=$(mktemp -d)
  ffmpeg -nostdin -v error -f lavfi -i "sine=frequency=440:duration=2" -ac 2 -ar 44100 "$T/t.wav"
  "$TTS_PY" -m demucs --two-stems vocals -n htdemucs -d cpu -o "$T" "$T/t.wav" >/dev/null 2>&1 \
    && ls "$T"/htdemucs/t/no_vocals.* >/dev/null && touch "$TTS_DIR/.demucs_ok" && log "Demucs 就绪" \
    || echo "⚠️ Demucs 自检失败（模型来自 dl.fbaipublicfiles.com，可稍后重跑本脚本）；配音时会改为压低原声"
  rm -rf "$T"
fi

# ---------- 4. LatentSync 1.6 对口型（Python 3.10，torch 2.5.1） ----------
if [ $NO_LIPSYNC = 0 ]; then
  if [ ! -x "$LS_PY" ]; then
    log "创建对口型环境（Python 3.10）"
    "$UV" python install 3.10 || die "下载 Python 3.10 失败"
    "$UV" venv -p 3.10 "$ENVS/lipsync"
  fi
  [ -d "$LS_DIR/.git" ] || gh_clone https://github.com/bytedance/LatentSync.git "$LS_DIR" || die "克隆 LatentSync 失败"
  cd "$LS_DIR"
  if ! "$LS_PY" -c "import diffusers, decord, insightface" 2>/dev/null; then
    log "安装 LatentSync 依赖（约 5GB）"
    grep -v "^--extra-index-url" requirements.txt > /tmp/ls_req.txt
    VIRTUAL_ENV=$ENVS/lipsync "$UV" pip install -p "$LS_PY" --index-strategy unsafe-best-match -r /tmp/ls_req.txt \
      || die "LatentSync 依赖安装失败"
  fi
  log "下载 LatentSync 1.6 模型"
  [ -f checkpoints/latentsync_unet.pt ] || hf_get "$LS_PY" ByteDance/LatentSync-1.6 checkpoints "latentsync_unet.pt" "whisper/tiny.pt"
  hf_get "$LS_PY" stabilityai/sd-vae-ft-mse - "*.json" "diffusion_pytorch_model.safetensors"
  if [ ! -f checkpoints/auxiliary/models/buffalo_l/det_10g.onnx ]; then
    mkdir -p checkpoints/auxiliary/models
    curl -fL --retry 3 -o /tmp/buffalo_l.zip "${GH_PROXY}https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip" \
      && unzip -oq /tmp/buffalo_l.zip -d checkpoints/auxiliary/models/buffalo_l && rm -f /tmp/buffalo_l.zip \
      || echo "⚠️ buffalo_l 下载失败，首次对口型时会自动下载"
  fi
  grep -q "enable_deepcache" scripts/inference.py && touch .deepcache_ok || rm -f .deepcache_ok
fi

# ---------- 5. 自检（需要 GPU） ----------
if [ "$HAS_GPU" = 1 ]; then
  log "配音自检：合成一句话"
  T=$(mktemp -d)
  ffmpeg -nostdin -v error -f lavfi -i "sine=frequency=220:duration=3" -ar 24000 -ac 1 "$T/ref.wav"
  printf '{"tts_dir": "%s", "items": [{"text": "你好，这是一句配音测试。", "voice": "%s", "emo": "", "out": "%s"}]}' \
    "$TTS_DIR" "$T/ref.wav" "$T/out.wav" > "$T/job.json"
  (cd "$TTS_DIR" && env -u CONDA_PREFIX "$TTS_PY" "$APP_DIR/scripts/tts_run.py" "$T/job.json" 2>&1 | tail -3) \
    && [ -f "$T/out.wav" ] && log "IndexTTS2 就绪" || echo "⚠️ IndexTTS2 自检失败，请查看上面的输出"
  rm -rf "$T"
else
  log "无卡模式：跳过自检。GPU 模式开机后可重跑本脚本做自检"
fi
log "完成。重启服务生效：bash deploy/stop.sh; bash deploy/start.sh"
