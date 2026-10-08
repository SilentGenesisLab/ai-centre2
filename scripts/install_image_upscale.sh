#!/usr/bin/env bash
# 图片超分（本地 SeedVR2）的安装脚本。
#
# 与 install_video_depth.sh 有两处**故意不同**，都是这台机器的实际情况决定的：
#
# 1. 源码不从 GitHub 拉。服务器连不上 huggingface.co（实测 000），GitHub 的
#    raw 能通但要挑版本；更重要的是 SeedVR2 那份代码是 ComfyUI 自定义节点仓库，
#    按 git 拉会带一大堆用不上的东西。改成从本机 scp 过去的一份 vendored 副本，
#    版本就是我们测过的那一份。
# 2. 权重也从本机 scp。服务器从 hf-mirror 拉实测只有 ~2MB/s，本机 scp 到服务器
#    **26MB/s**（实测 286MB / 10s），差一个数量级。7B 有 8.2G，这个差值就是
#    一小时。
#
# 所以这个脚本只负责「装 venv + 装单元」，源码与权重由调用方先放好，脚本负责校验。
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/donxu/ai-centre}"
SERVICE_ROOT="${SEEDVR2_SOURCE_DIR:-/home/donxu/services/seedvr2}"
WEIGHTS="${SEEDVR2_WEIGHTS_DIR:-$SERVICE_ROOT/weights}"
VENV="${SEEDVR2_VENV:-$PROJECT_ROOT/.venv-seedvr2}"
PYTHON_BIN="${SEEDVR2_PYTHON_BIN:-$PROJECT_ROOT/.venv-control/bin/python}"

# 3b 是默认模型；7b-sharp 体积大一倍，装不装由调用方决定（两者都缺才算失败）。
REQUIRED=(
  "ema_vae_fp16.safetensors"
  "seedvr2_ema_3b_fp8_e4m3fn.safetensors"
)

missing=0
if [[ ! -f "$SERVICE_ROOT/inference_cli.py" ]]; then
  echo "缺少 SeedVR2 源码：$SERVICE_ROOT/inference_cli.py" >&2
  echo "  从本机传：scp -P 2222 -r <本机>/seedvr2/code/ComfyUI-SeedVR2_VideoUpscaler-main/* donxu@121.15.184.231:$SERVICE_ROOT/" >&2
  missing=1
fi
for name in "${REQUIRED[@]}"; do
  if [[ ! -s "$WEIGHTS/$name" ]]; then
    echo "缺少权重：$WEIGHTS/$name" >&2
    missing=1
  fi
done
if [[ $missing -ne 0 ]]; then
  echo "先补齐上面的文件再跑这个脚本。" >&2
  exit 1
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip wheel
# cu128：服务器是 RTX 5090（sm_120），cu118/cu121 的轮子跑不了。
"$VENV/bin/python" -m pip install \
  --index-url https://download.pytorch.org/whl/cu128 \
  torch==2.8.0 torchvision==0.23.0
"$VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements-seedvr2.txt"

# 装完立刻验一次：torch 看不到卡的话，worker 起来也只会在第一张图上炸，
# 那时候已经排队等了很久，不如这里直接失败。
"$VENV/bin/python" - <<'PY'
import sys
import torch
if not torch.cuda.is_available():
    sys.exit("torch 看不到 CUDA —— 多半是装错了 cu 版本的轮子（要 cu128）")
print("cuda ok:", torch.cuda.get_device_name(0))
PY

mkdir -p /home/donxu/.config/systemd/user "$PROJECT_ROOT/runtime/image-upscale"
install -m 0644 \
  "$PROJECT_ROOT/deploy/systemd-user/ai-centre-image-upscale-worker.service" \
  /home/donxu/.config/systemd/user/ai-centre-image-upscale-worker.service
systemctl --user daemon-reload
systemctl --user enable --now ai-centre-image-upscale-worker.service
echo "已安装。状态：systemctl --user status ai-centre-image-upscale-worker.service"
