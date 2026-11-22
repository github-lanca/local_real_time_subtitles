#!/bin/bash
# Start the local AI subtitle stack: llama (18181), bridge (18182).
# whisper-server (18180) is only needed when ASR_BACKEND=http (fallback).
# Idempotent: skips services that are already listening.
#
# Services are launched through tools/detach.py (setsid), NOT plain `nohup &`:
# this script is called by IINA's utils.exec, which returns as soon as the script
# exits. With nohup the daemons stayed in the caller's process group and were
# torn down with it -- "services ready" followed by silence a few seconds later.

ROOT="${SUBS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"

# IINA 插件通过 utils.exec 调用本脚本时，子进程环境里**没有 HOME**（2026-11-22 实测
# env 只剩 LC_ALL）。mlx / huggingface 的缓存路径、python 的 ~ 展开都依赖家目录，
# 缺失时会走 passwd 回退（大概率没事，但行为和终端里不一致，出问题极难排查），
# 这里显式补回来；$(echo ~) 在 HOME 缺失时由 bash 查 passwd 展开。
export HOME="${HOME:-$(echo ~)}"

# Default ASR is mlx (in-process, Apple Metal, most accurate + fastest).
export ASR_BACKEND="${ASR_BACKEND:-mlx}"

# 端口探测必须用绝对路径：IINA 插件 exec 出来的进程 PATH 是
# /usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.（实测，**不含 /usr/sbin**），
# 直接调 lsof 会 command not found -> 判成"端口没被监听" -> 明明服务在跑还重复
# 启动一份，bridge.log 里刷 "OSError: [Errno 48] Address already in use"。
LSOF="/usr/sbin/lsof"
[ -x "$LSOF" ] || LSOF="$(command -v lsof)"
listening() {
  "$LSOF" -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN
}

py="/opt/homebrew/bin/python3"
[ -x "$py" ] || py="/usr/bin/python3"
detach="$py $ROOT/tools/detach.py"

# 1) whisper-server fallback (only for ASR_BACKEND=http).
if [ "$ASR_BACKEND" = "http" ] && ! listening 18180; then
  $detach "$LOGDIR/whisper.log" \
    whisper-server -m "$ROOT/models/ggml-large-v3-turbo-q5_0.bin" \
    -l ja --host 127.0.0.1 --port 18180 --convert -t 8 >/dev/null
fi

# 2) llama translation server.
# -c 2048 (not 4096): on a 16GB Mac every MB of KV cache competes with the 1.6GB
# MLX weights; swapping those weights out is what makes ASR look "randomly slow".
if ! listening 18181; then
  if [ -n "$DYLD_LIBRARY_PATH" ]; then
    LLAMA_ENV=(env "DYLD_LIBRARY_PATH=$ROOT/tools/llama/llama-b12602:$DYLD_LIBRARY_PATH")
  else
    LLAMA_ENV=(env "DYLD_LIBRARY_PATH=$ROOT/tools/llama/llama-b12602")
  fi
  $detach "$LOGDIR/llama.log" "${LLAMA_ENV[@]}" \
    "$ROOT/tools/llama/llama-b12602/llama-server" \
    -m "$ROOT/models/qwen2.5-3b-instruct-q4_k_m.gguf" \
    --host 127.0.0.1 --port 18181 -c 2048 -ngl 99 --temp 0 >/dev/null
fi

# 3) bridge (in-process mlx ASR; warms the model up in a background thread).
if ! listening 18182; then
  ASR_BACKEND="$ASR_BACKEND" $detach "$LOGDIR/bridge.log" \
    "$py" "$ROOT/bridge_server.py" >/dev/null
fi

# Wait until the bridge reports ASR + translation healthy (cold model load ~20s).
for i in $(seq 1 160); do
  h=$(curl -s --max-time 1 http://127.0.0.1:18182/health 2>/dev/null)
  if echo "$h" | grep -q '"whisper": *true' && echo "$h" | grep -q '"llama": *true'; then
    echo "AI subtitle services ready ($h)"
    exit 0
  fi
  sleep 0.5
done

echo "AI subtitle services failed to become ready; check $LOGDIR" >&2
exit 1
