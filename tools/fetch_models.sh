#!/bin/bash
# 一键下载本项目所需的全部模型（全部走国内镜像，官方源太慢）。
#
#   bash tools/fetch_models.sh            # 下全部
#   bash tools/fetch_models.sh --no-ct2   # 不下可选的 CT2（离线校验用）
#
# 下载完成后执行 bash start_services.sh 即可。
set -euo pipefail

ROOT="${SUBS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODELS="$ROOT/models"
mkdir -p "$MODELS" "$ROOT/tools" "$ROOT/logs"

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
GH_PROXY="${GH_PROXY:-https://gh-proxy.com/}"
WITH_CT2=1
[ "${1:-}" = "--no-ct2" ] && WITH_CT2=0

PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=/usr/bin/python3

if command -v hf >/dev/null 2>&1; then HFCLI=hf
elif command -v huggingface-cli >/dev/null 2>&1; then HFCLI=huggingface-cli
else
  echo "缺少 huggingface_hub CLI：$PY -m pip install -U huggingface_hub --user --break-system-packages" >&2
  exit 1
fi

echo "== 1/4 实时 ASR：mlx-community/whisper-large-v3-turbo（fp16，约 1.6G）"
echo "   下载到 ~/.cache/huggingface/hub，bridge 会自动发现（也可用 MLX_MODEL 指定目录）"
$HFCLI download mlx-community/whisper-large-v3-turbo

echo "== 2/4 翻译模型：Qwen2.5-3B-Instruct q4_k_m（约 2.0G）"
$HFCLI download Qwen/Qwen2.5-3B-Instruct-GGUF qwen2.5-3b-instruct-q4_k_m.gguf \
  --local-dir "$MODELS"

echo "== 3/4 翻译引擎：llama.cpp 预编译 b12602（macOS arm64，约 16M）"
if [ ! -x "$ROOT/tools/llama/llama-b12602/llama-server" ]; then
  mkdir -p "$ROOT/tools/llama"
  curl -L --retry 3 -C - \
    "${GH_PROXY}https://github.com/ggml-org/llama.cpp/releases/download/b12602/llama-b12602-bin-macos-arm64.tar.gz" \
    -o "$ROOT/tools/llama/llama-b12602.tar.gz"
  tar -xzf "$ROOT/tools/llama/llama-b12602.tar.gz" -C "$ROOT/tools/llama"
  rm -f "$ROOT/tools/llama/llama-b12602.tar.gz"
fi

echo "== 4/4 语音门禁：Silero VAD v6（仓库内已带 models/vad/silero_vad_v6.onnx）"
[ -f "$MODELS/vad/silero_vad_v6.onnx" ] || echo "   ⚠️ 缺少 models/vad/silero_vad_v6.onnx（应从仓库检出）"

if [ "$WITH_CT2" = "1" ]; then
  echo "== 可选：faster-whisper CT2（离线校验用，约 1.5G，不参与实时）"
  $HFCLI download deepdml/faster-whisper-large-v3-turbo-ct2 \
    --local-dir "$MODELS/faster-whisper-large-v3-turbo" || \
    echo "   （跳过，不影响实时字幕）"
fi

echo
echo "全部完成。下一步：bash $ROOT/start_services.sh"
