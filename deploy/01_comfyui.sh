#!/usr/bin/env bash
# 第 1 步：安装 ComfyUI（官方最新稳定版，已用 v0.42.x 校验过全部工作流节点）
set -euo pipefail
source "$(dirname "$0")/env.sh"
COMFY_REF=${COMFY_REF:-v0.42.1}

turbo_on
if [ ! -d "$COMFY_DIR/.git" ]; then
  log "克隆 ComfyUI $COMFY_REF"
  gh_clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR" "$COMFY_REF" || die "克隆 ComfyUI 失败"
fi
cd "$COMFY_DIR"
turbo_off

# 继承镜像自带的 torch（省 3GB 下载）；版本不够新时再在环境里单独安装
make_env comfyui --system-site-packages
PIP="$COMFY_PY -m pip"
torch_ok() { $COMFY_PY - <<'PY'
import sys
try:
    import torch
except ImportError:
    sys.exit(1)
major, minor = (int(x) for x in torch.__version__.split("+")[0].split(".")[:2])
cuda = tuple(int(x) for x in (torch.version.cuda or "0.0").split(".")[:2])
import os
need_gpu = os.environ.get("HAS_GPU") == "1"
sys.exit(0 if (major, minor) >= (2, 8) and cuda >= (12, 8) and (torch.cuda.is_available() or not need_gpu) else 1)
PY
}
if torch_ok; then log "复用镜像自带的 PyTorch"; else
  log "安装 PyTorch 2.8+（CUDA 12.8，支持到 Blackwell）"
  $PIP install "torch>=2.8" torchvision torchaudio \
    || $PIP install "torch>=2.8" torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
  torch_ok || $PIP install --force-reinstall "torch>=2.8" torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
fi
log "安装 ComfyUI 依赖"
$PIP install -r requirements.txt
$COMFY_PY - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda, "可用:", torch.cuda.is_available())
if torch.cuda.is_available():
    cap = torch.cuda.get_device_capability()
    print("GPU", torch.cuda.get_device_name(), "算力", cap, "支持架构", torch.cuda.get_arch_list())
PY
mkdir -p models/diffusion_models models/text_encoders models/vae models/loras
log "ComfyUI 安装完成：$COMFY_DIR"
