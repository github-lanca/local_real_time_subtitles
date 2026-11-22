#!/opt/homebrew/bin/python3
"""Local bridge for IINA AI subtitle plugin.

Receives {video, start, duration, context}, extracts a gain-normalized audio
chunk with ffmpeg, transcribes it with the local whisper server, translates the
Japanese text to Chinese with the local llama server, and returns the result.

Runs on 127.0.0.1:18182.  Standard library only.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time as time_mod
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vad_silero

# Repo root: taken from this file's location so a clone anywhere works.
# SUBS_ROOT overrides it (models/, logs/, tools/ are resolved relative to it).
ROOT = os.environ.get("SUBS_ROOT") or os.path.dirname(os.path.abspath(__file__))
# Model is already cached, but keep mirror as default in case a lookup happens.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
WHISPER_URL = "http://127.0.0.1:18180/inference"
LLAMA_URL = "http://127.0.0.1:18181/v1/chat/completions"
# ffmpeg 路径：环境变量 > PATH > Homebrew 默认位置（换机器克隆时不用改代码）
FFMPEG = (os.environ.get("FFMPEG")
          or shutil.which("ffmpeg")
          or "/opt/homebrew/bin/ffmpeg")
PORT = 18182

# ASR backend (env ASR_BACKEND overrides):
#   "mlx"    = in-process mlx-whisper on Apple Metal (DEFAULT: most accurate,
#              ~2.2s/call steady-state, needs startup warmup for Metal kernels).
#   "http"   = whisper-server on 18180 (fallback, ~3s, weaker on hard words).
#   "faster" = in-process faster-whisper CT2 (accurate but ~6.5s fixed overhead
#              per call on CPU -> NOT realtime-capable).
# MLX notes: must pass a wav FILE (numpy + long window pads to 30s and
# hallucinates), default temperature ladder (forced 0 over-repeats short
# dialogue), NO beam_size (0.4.3 raises NotImplementedError).
ASR_BACKEND = os.environ.get("ASR_BACKEND", "mlx").lower()
# fp16 full precision: highest Japanese accuracy (correctly hears 即死 / すごい
# where whisper.cpp fails). Quantized variants were tried and rejected:
#   - 8bit / 4bit (safetensors) -> mlx-whisper 0.4.3 can't load them (npz only)
#   - q4 (npz)                 -> accuracy collapses (repeats / mishears words)
# Known issue: MLX Metal decode occasionally spikes to ~7s when a new audio
# pattern triggers shader recompilation (steady state ~1s). The plugin absorbs
# this with a larger LEAD budget. Override via env for experiments.
def _resolve_mlx_model():
    """Prefer the local HF snapshot directory over the repo id.

    Passing a repo id makes mlx-whisper call huggingface_hub on every
    transcribe() to resolve/download the snapshot. On this machine that request
    goes through a proxy that intermittently returns 502, which surfaced as a
    hard failure, and even when it succeeds it adds a network round trip to
    every subtitle chunk. A local directory skips all of it.
    """
    env = os.environ.get("MLX_MODEL")
    if env:
        return env
    hub = os.path.expanduser("~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots")
    if os.path.isdir(hub):
        snaps = [os.path.join(hub, d) for d in sorted(os.listdir(hub))
                 if os.path.isfile(os.path.join(hub, d, "weights.safetensors"))]
        if snaps:
            return snaps[-1]
    return "mlx-community/whisper-large-v3-turbo"


MLX_MODEL = _resolve_mlx_model()
FW_MODEL_PATH = os.path.join(ROOT, "models", "faster-whisper-large-v3-turbo")
_fw_model = None
_mlx_ready = threading.Event()
_mlx_error = None

SYS_PROMPT = (
    "你是中日字幕翻译。把输入的日语翻译成一行简体中文。\n"
    "规则：只输出译文本身，绝不输出日文假名，不加引号、不解释、不评价。\n"
    "输入来自语音识别，可能有错字、语气词、断句不完整、听不清的地方。\n"
    "遇到这种情况不要要求澄清、不要说无法翻译——尽最大努力给出最通顺的中文；\n"
    "实在没有实义就只输出语气词（如「啊～」「嗯～」「呃～」）。\n"
    "示例：\n"
    "输入：ちょっと待って\n输出：等一下\n"
    "输入：気持ちいい\n输出：好舒服\n"
    "输入：痛い\n输出：好痛\n"
    "输入：だめ\n输出：不行\n"
    "输入：あっつい\n输出：好热\n"
    "输入：割れちゃうから\n输出：要裂开了\n"
    "输入：んー、あのね、その\n输出：嗯…那个…\n"
    "输入：あ、あ、あ\n输出：啊～\n"
)

# Whisper hallucinates stock closing phrases on silence/music. These carry no
# speech and must never become subtitles. Matched against raw + translated text.
HALLUCINATION_PHRASES = [
    # Japanese stock phrases Whisper emits on silence
    "ご視聴ありがとうございました", "ご視聴ありがとう", "ご清聴ありがとうございました",
    "ご視聴いただきありがとうございました", "視聴ありがとうございました",
    "チャンネル登録", "高評価", "字幕提供", "字幕制作", "字幕 là",
    "おわり", "終わりました", "ありがとうございました",
    # English / Chinese hallucinations
    "thanks for watching", "thank you for watching", "subscribe", "like and subscribe",
    # English stock lines Whisper emits on non-speech audio. Seen live in this
    # film: "I'm not going to die", "Oh, my God" -- translated and displayed.
    "i'm not going to die", "i am not going to die", "oh, my god", "oh my god",
    "subtitles by", "amara.org", "please subscribe",
    "谢谢观看", "感谢收看", "感谢观看", "字幕组", "字幕提供", "请订阅", "点赞关注",
]
# Minimum seconds of real speech required in a chunk before we trust Whisper.
MIN_SPEECH_SEC = 0.35
# Silero VAD tuning: 0.5 is the reference threshold; 300ms of silence splits two
# utterances for subtitle purposes (the default 2000ms would merge whole scenes).
VAD_THRESHOLD = float(os.environ.get("VAD_THRESHOLD", "0.5"))
VAD_MIN_SILENCE_MS = int(os.environ.get("VAD_MIN_SILENCE_MS", "300"))
# Breathy / onomatopoeic audio: Silero max probability sits at 0.20-0.30 (music
# and true silence land lower). Above the floor we still run ASR, but only a
# vocalisation result is allowed through (see `weak` in process()).
VAD_WEAK_MIN_PROB = float(os.environ.get("VAD_WEAK_MIN_PROB", "0.18"))
VAD_WEAK_PROB = float(os.environ.get("VAD_WEAK_PROB", "0.15"))
# A breathy chunk with no words still deserves a "啊～" if the sound lasts this
# long; shorter blips are usually just noise.
MOAN_MIN_SEC = float(os.environ.get("MOAN_MIN_SEC", "0.8"))
# Minimum duration of a kept subtitle cue.
MIN_CUE_SEC = 0.3
# Extra audio past the chunk end, used as recognition context ONLY (cues are
# clipped to [start, start+duration]). Trailing context is what fixes words
# like "これ即死じゃん"; 2.5s is enough for mlx. Total ASR input is
# CHUNK 4.0 + 2.5 = 6.5s, processed in ~2.2s by mlx (well below CHUNK, so the
# prefetch pipeline keeps up with 1x playback).
LOOKAHEAD_SEC = 2.5

# Stage-by-stage timing. On-by-default because "ASR is slow" reports were
# repeatedly misattributed: the 10s+ request latency was ffmpeg + per-sentence
# llama round trips, not the ASR itself. Never optimize blind again.
TRACE = os.environ.get("BRIDGE_TRACE", "1") == "1"

# --- logging --------------------------------------------------------------
# Everything used to go to stdout only, which is fine while a tool call is
# watching the process but useless afterwards: the user could not see which
# chunks were dropped or why. Every trace line now also lands in logs/bridge.log
# so it can be tailed or reported on later.
LOG_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "bridge.log")
_log_lock = threading.Lock()


def _rotate_log(max_bytes=8 * 1024 * 1024):
    try:
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > max_bytes:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except Exception:
        pass


def logline(msg):
    line = time_mod.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with _log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def trace(msg):
    print(msg, flush=True)
    logline(msg)


DROPS_PATH = os.path.join(LOG_DIR, "drops.log")
FILM_DIR = os.path.join(LOG_DIR, "films")

# Plain-Chinese wording for every drop reason, so the film log reads like a
# sentence instead of a code.
REASON_CN = {
    "silence": "VAD 判定没有语音（静音/纯环境音）",
    "weak audio": "有声但无台词，且持续时间不够长（不出拟声）",
    "no valid speech": "识别出的句子全部被过滤（幻觉/无发声区间）",
    "empty transcription": "识别结果为空",
    "non-japanese hallucination": "强制日语却输出零日文（Whisper 幻觉）",
    "video not found": "影片文件不存在",
}


def _tfmt(sec):
    """Film time as HH:MM:SS.ss (T+01:00:00 = 影片第 1 小时)."""
    sec = max(0.0, float(sec))
    return "%02d:%02d:%05.2f" % (int(sec // 3600), int((sec % 3600) // 60), sec % 60)


def film_log(video, kind, vtime, text, extra=""):
    """Human-readable per-film log.

    One file per movie (logs/films/<影片名>.log), one line per event, always
    starting with wall-clock time and the film's own timeline, then a clearly
    labelled 识别 / 字幕 / 放弃 so the three stages can be told apart at a glance.
    """
    name = os.path.splitext(os.path.basename(video or "unknown"))[0] or "unknown"
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    path = os.path.join(FILM_DIR, name + ".log")
    line = "%s  T+%s  [%s] %s%s" % (time_mod.strftime("%Y-%m-%d %H:%M:%S"),
                                    _tfmt(vtime), kind, text, extra)
    try:
        with _log_lock:
            os.makedirs(FILM_DIR, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


UNTRANSLATED_PATH = os.path.join(LOG_DIR, "untranslated.log")


def untranslated_log(vtime, ja_text, model_said=""):
    """识别成功但没译出来的条目。

    单独一个文件，因为这是要人工看的东西：日语原文连着说的时间点，
    方便对照着纠正识别或者补一条译法。
    """
    line = ("%s  T+%s  %s%s"
            % (time_mod.strftime("%Y-%m-%d %H:%M:%S"), _tfmt(vtime),
               (ja_text or "").strip()[:120],
               ("   [模型]: " + (model_said or "").strip()[:60]) if model_said else ""))
    try:
        with _log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(UNTRANSLATED_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def drop_log(start, reason, ja, zh, vmax, speech, weak):
    """One line per discarded chunk, with the evidence used to decide.

    This is the file to read when tuning: it answers "what did it hear, how much
    speech did VAD see, and which rule threw it away".
    """
    line = ("%s  @%7.1f  %-27s vmax=%.2f sp=%4.2f%s  ja=%s"
            % (time_mod.strftime("%H:%M:%S"), start, reason, vmax, speech,
               " WEAK" if weak else "", (ja or "-")[:100]))
    try:
        with _log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(DROPS_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# whisper / llama servers are single-instance: serialize all inference requests.
_lock = threading.Lock()
_cache = {}


# --- 字幕文件侧车（看完一遍，下次直接复用）-----------------------------------
#
# 边看边把字幕累加进视频同目录的一个标准 SRT；下次打开同一部片，缓存里有的
# 区间直接秒回，没有的照常识别并补写回去 —— 也就是"看到哪、补到哪"。
#
#   <视频>.live-ai.srt      字幕本身（标准 SRT，随时可打开）
#   <视频>.live-ai.ranges   已处理过的时间区间（JSON）
#
# 用 .live-ai. 而不是 .zh. 命名，是为了避免 IINA 把它当成外挂字幕自动加载，
# 与 overlay 字幕叠在一起显示。
# 缓存命中后是否跳过的判据是"这一段处理过没有"（见 _Sidecar._covered），
# 不是覆盖率 —— 窗口尾部是静音时覆盖率永远到不了高位，而那一块其实早处理过了。

_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d+)")


def _srt_time(t):
    ms = int(round(max(0.0, float(t)) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _srt_parse_time(text):
    m = _SRT_TIME.match(text.strip())
    if not m:
        return None
    h, mi, se, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(frac.ljust(3, "0")[:3]) / 1000.0


def _norm_zh(text):
    """Compare cue texts ignoring punctuation/elongation marks."""
    return re.sub(r"[\s，。！？、,.!?…‥・\-~～ー—'\"]+", "", (text or "")).lower()


class _Sidecar:
    """一部影片的字幕缓存：边看边累加，随时可用。

    只要两个文件：
      <视频>.live-ai.srt      字幕本身（标准 SRT，随时能打开/交给别的播放器）
      <视频>.live-ai.ranges   已处理过的时间区间（JSON）

    ranges 是关键。光有 SRT 只能证明"有对白的地方处理过"，无法区分"这段是静音"
    和"这段还没看"；记下范围之后，第二遍播放才能对空白窗口也直接作答，而不必每次
    都跑一遍 ffmpeg 去确认没人说话。

    写入一律是"内存全量重写"：几千行亚毫秒级，换来去重/排序/编号在重启后天然正确，
    也不需要维护"写到第几条"这种状态。
    """

    def __init__(self, video):
        self.video = video
        base = os.path.splitext(video)[0]
        self.path = base + ".live-ai.srt"
        self.ranges_path = base + ".live-ai.ranges"
        # 旧版本产物：只读兼容，新内容一律写 self.path
        self.legacy = [base + ".zh.srt", base + ".zh.srt.part"]
        self.cues = []
        self.ranges = []          # [[a, b], ...] 有序且已合并
        self.lock = threading.RLock()
        self._load()

    # -- load / save ------------------------------------------------------
    def _load(self):
        src = self.path if os.path.isfile(self.path) else next(
            (f for f in self.legacy if os.path.isfile(f)), None)
        dropped = 0
        cleaned = 0
        dropped_spans = []
        if src:
            try:
                with open(src, encoding="utf-8", errors="ignore") as f:
                    blocks = f.read().replace("\r\n", "\n").split("\n\n")
                for blk in blocks:
                    lines = [l for l in blk.split("\n") if l.strip()]
                    if len(lines) < 2:
                        continue
                    i = 1 if lines[0].strip().isdigit() else 0
                    if i >= len(lines) or "-->" not in lines[i]:
                        continue
                    a, b = lines[i].split("-->", 1)
                    s, e = _srt_parse_time(a), _srt_parse_time(b)
                    raw_text = " ".join(l.strip() for l in lines[i + 1:]).strip()
                    # 旧文件里可能有 _strip_meta 上线前写进去的脏文本
                    text = _strip_meta(raw_text)
                    if text != raw_text:
                        cleaned += 1
                    if (s is None or e is None or not text or e <= s
                            or not _valid_zh(text)):
                        dropped += 1
                        if e is not None and s is not None and e > s:
                            dropped_spans.append([round(s, 2), round(e, 2)])
                        continue
                    self.cues.append({"start": round(s, 2), "end": round(e, 2),
                                      "zh": text})
                self.cues.sort(key=lambda c: c["start"])
                if self.cues:
                    trace("[srt] loaded %d cues from %s"
                          % (len(self.cues), os.path.basename(src)))
            except Exception as exc:
                trace("[srt] load failed: %s" % exc)

        try:
            if os.path.isfile(self.ranges_path):
                with open(self.ranges_path, encoding="utf-8") as f:
                    raw = json.load(f).get("ranges") or []
                self.ranges = sorted([[float(a), float(b)] for a, b in raw])
            elif self.cues:
                # 旧产物没有范围文件时，只能相信"有字幕的地方处理过"。
                # 绝不能假设"0 到最后一句话都处理过" —— 用户可能只看了中间一段，
                # 那样会把没处理过的空白区永久标成"已完成"，那段对白再也补不回来
                # （实测：片头 20-55 秒有完整对白，却因为这条假设被跳过了）。
                # 间隔 2 秒内合并：对白之间的停顿不值得重跑，长段静音才留空。
                spans = sorted([[c["start"], c["end"]] for c in self.cues])
                merged = []
                for a, b in spans:
                    if merged and a - merged[-1][1] <= 2.0:
                        merged[-1][1] = max(merged[-1][1], b)
                    else:
                        merged.append([a, b])
                self.ranges = [[round(a, 2), round(b, 2)] for a, b in merged]
                trace("[srt] derived %d range(s) from %d cue(s) (no ranges file)"
                      % (len(self.ranges), len(self.cues)))
        except Exception as exc:
            trace("[srt] ranges load failed: %s" % exc)

        # 脏条目被丢掉后，它占的那段时间也要从"已处理"里挖掉，否则下次
        # 会被当成"这段没对白"直接跳过，永远补不回来。
        for a, b in dropped_spans:
            self._subtract_range(a, b)

        # 两件事都靠一次重写完成：
        #   1) 旧缓存里可能存着修复前写进去的脏条目（模型的拒绝话术），丢掉后
        #      必须把文件一起改干净，否则它下次还会被读出来。
        #   2) 从旧产物迁移时要立刻写成新命名，否则 _sync_disk 会以为
        #      "缓存文件不存在"，把刚读进来的内容又清掉。
        if dropped or cleaned or ((self.cues or self.ranges) and not os.path.isfile(self.path)):
            if dropped or cleaned or self.cues or self.ranges:
                self._write()
                trace("[srt] rewrote %s (dropped %d, cleaned %d)"
                      % (os.path.basename(self.path), dropped, cleaned))

    def _srt_body(self):
        out = []
        for i, c in enumerate(self.cues, 1):
            out.append("%d\n%s --> %s\n%s\n"
                       % (i, _srt_time(c["start"]), _srt_time(c["end"]), c["zh"]))
        return "\n".join(out)

    def _write_atomic(self, path, text):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception as exc:
            trace("[srt] write failed (%s): %s" % (os.path.basename(path), exc))

    def _write(self):
        self._write_atomic(self.path, self._srt_body())
        self._write_atomic(self.ranges_path, json.dumps({
            "ranges": [[round(a, 2), round(b, 2)] for a, b in self.ranges]}))

    def _sync_disk(self):
        """文件被外部删掉时，内存状态要跟着清空。"""
        if self.cues and not os.path.isfile(self.path):
            trace("[srt] cache file gone, resetting %s"
                  % os.path.basename(self.video))
            self.cues = []
            self.ranges = []

    # -- content ----------------------------------------------------------
    def _subtract_range(self, a, b):
        """把 [a, b] 从已处理区间里挖掉（脏条目被丢弃时用）。"""
        out = []
        for x, y in self.ranges:
            if y <= a or x >= b:
                out.append([x, y])
                continue
            if x < a:
                out.append([x, a])
            if y > b:
                out.append([b, y])
        self.ranges = out

    def _merge_ranges(self, start, end):
        iv = sorted(self.ranges + [[float(start), float(end)]])
        out = []
        for a, b in iv:
            if out and a <= out[-1][1] + 0.05:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        self.ranges = out

    def _drop_in_window(self, start, end):
        """清掉"基本落在本窗口内"的旧条目。

        重新识别一段时（区间被挖掉过、或换了提示词重跑），旧结果必须让位，
        否则新旧两份并存：屏幕上显示旧的，文件里还多一条重复的。
        判定用重叠比例而不是完全包含——字幕尾部会被 CUE_TAIL_PAD 撑出窗口。
        """
        lo, hi = float(start), float(end)
        keep, removed = [], 0
        for c in self.cues:
            span = max(c["end"] - c["start"], 0.01)
            overlap = min(c["end"], hi) - max(c["start"], lo)
            if overlap > 0 and overlap / span >= 0.7:
                removed += 1
                continue
            keep.append(c)
        if removed:
            self.cues = keep
        return removed > 0

    def _merge_one(self, s, e, zh):
        """把一条字幕并进缓存；相邻窗口会重复识别同一句。"""
        key = _norm_zh(zh)
        for old in self.cues:
            overlap = min(e, old["end"]) - max(s, old["start"])
            if overlap <= 0:
                continue
            shorter = min(e - s, old["end"] - old["start"])
            if shorter <= 0:
                continue
            old_key = _norm_zh(old["zh"])
            same = key == old_key or key in old_key or old_key in key
            if same and overlap / shorter >= 0.6:
                old["start"] = round(min(old["start"], s), 2)
                old["end"] = round(max(old["end"], e), 2)
                if len(zh) > len(old["zh"]):
                    old["zh"] = zh
                return True
        self.cues.append({"start": s, "end": e, "zh": zh})
        return True

    def add(self, cues, start=None, duration=None):
        """并入这一块的识别结果，同时登记"这个窗口已经处理过"。

        即使这一块没有字幕（静音/音乐），也要记 ranges —— 否则第二遍播放还得重新
        跑一遍 ffmpeg 才能确认它确实没人说话。
        """
        with self.lock:
            self._sync_disk()
            marked = start is not None and duration
            changed = False
            if marked:
                self._merge_ranges(start, float(start) + float(duration))
                # 这一段要重新识别了：旧结果先让位，避免新旧并存
                if self._drop_in_window(start, float(start) + float(duration)):
                    changed = True
            for c in cues or []:
                zh = (c.get("zh") or "").strip()
                try:
                    s = round(float(c["start"]), 2)
                    e = round(float(c["end"]), 2)
                except Exception:
                    continue
                if not zh or e - s < 0.05:
                    continue
                if self._merge_one(s, e, zh):
                    changed = True
            if changed:
                self.cues.sort(key=lambda c: c["start"])
            if changed or marked:
                self._write()
            return changed

    def _covered(self, start, end):
        """[start, end] 是否整段落在"已处理"区间内。"""
        t = float(start) - 0.3
        limit = float(end) + 0.3
        for a, b in self.ranges:
            if a - 0.3 <= t <= b + 0.3:
                if b > t:
                    t = b
                if t + 0.3 >= limit:
                    return True
            elif a - 0.3 > t:
                break
        return t + 0.3 >= limit

    def lookup(self, start, duration):
        """这一段缓存里已经知道什么：(窗口内的 cue, 是否已处理过)。

        只看"cue 覆盖率"是不够的：窗口尾部是静音时覆盖率永远到不了高位，
        而这一块其实早就处理过了。
        """
        end = float(start) + float(duration)
        with self.lock:
            hit = [dict(c) for c in self.cues
                   if c["start"] < end and c["end"] > start]
            return hit, self._covered(start, end)

    def status(self, duration=0.0):
        with self.lock:
            self._sync_disk()
            covered = sum(b - a for a, b in self.ranges)
            cov = round(min(1.0, covered / duration), 3) if duration > 0 else None
            gaps = []
            if duration > 0:
                prev = 0.0
                for a, b in self.ranges:
                    if a - prev > 3.0:
                        gaps.append([round(prev, 1), round(a, 1)])
                    prev = max(prev, b)
                if duration - prev > 3.0:
                    gaps.append([round(prev, 1), round(float(duration), 1)])
            return {
                "path": self.path,
                "ranges_path": self.ranges_path,
                "exists": os.path.isfile(self.path),
                "cues": len(self.cues),
                "last": round(max((c["end"] for c in self.cues), default=0.0), 2),
                "covered_seconds": round(covered, 1),
                "coverage": cov,
                "gaps": gaps[:10],
            }

    def finalize(self, duration=0.0, force=False):
        """导出成标准命名的 .zh.srt（缓存本身一直在写，这个只是给手动加载用）。"""
        with self.lock:
            self._sync_disk()
            if not self.cues:
                return {"ok": False, "error": "no cues yet"}
            base = os.path.splitext(self.video)[0]
            dst = base + ".zh.srt"
            self._write_atomic(dst, self._srt_body())
            covered = sum(b - a for a, b in self.ranges)
            cov = round(min(1.0, covered / duration), 3) if duration > 0 else 1.0
            trace("[srt] exported %s (%d cues)" % (os.path.basename(dst), len(self.cues)))
            return {"ok": True, "path": dst, "cues": len(self.cues), "coverage": cov}


# 最近一次翻译被拒时模型的原话（只用于日志，不参与判断）
LAST_TRANSLATE_RAW = [""]

_sidecars = {}
_sidecars_lock = threading.Lock()


def get_sidecar(video):
    with _sidecars_lock:
        sc = _sidecars.get(video)
        if sc is None:
            sc = _Sidecar(video)
            _sidecars[video] = sc
        return sc


def health_check(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def extract_audio(video, start, duration, out_wav):
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", video, "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", "16000",
        # Dynamic normalization to a fixed true peak (~-2 dBFS). A fixed
        # volume=4.0 clipped normal-level dialogue (peak 0.84 -> 1.0, 6% dead)
        # while quiet dialogue stayed unintelligible. loudnorm handles both.
        "-af", "loudnorm=I=-18:TP=-2:LRA=7",
        "-c:a", "pcm_s16le", out_wav,
    ]
    subprocess.run(cmd, check=True, timeout=90)


def detect_voice_intervals(wav_path, duration, threshold=None):
    """Return [(start, end), ...] of real speech, clipped to `duration`.

    Silero (v6 ONNX, ~50ms for 6.5s on CPU) replaces ffmpeg silencedetect, which
    only measures energy: BGM, ambience and music stings all passed as "speech",
    so Whisper got burned on non-speech audio and spent 8-14s per chunk running
    its temperature ladder into stock hallucinations. Silero scores music < 0.1
    and dialogue > 0.9, and gives far tighter cue boundaries.

    Falls back to energy detection only if Silero itself errors (fail open).
    """
    thr = VAD_THRESHOLD if threshold is None else threshold
    try:
        audio = vad_silero.read_wav_float32(wav_path)
        probs = vad_silero.speech_probs(audio)
        max_prob = float(probs.max()) if probs.size else 0.0
        spans = vad_silero.speech_intervals(
            audio, threshold=thr,
            min_silence_ms=VAD_MIN_SILENCE_MS, pad_ms=80, min_speech_ms=200)
        return [(a, min(b, duration)) for a, b in spans if a < duration], max_prob
    except Exception as e:
        trace(f"[vad] silero failed ({e}); falling back to ffmpeg")
        return _detect_voice_ffmpeg(wav_path, duration), 0.0


def _detect_voice_ffmpeg(wav_path, duration):
    cmd = [
        FFMPEG, "-nostats", "-loglevel", "info", "-i", wav_path,
        "-af", "silencedetect=noise=-42dB:d=0.25", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        text = proc.stderr or ""
    except Exception:
        # Fail open: if VAD errors, don't block subtitles entirely.
        return [(0.0, duration)]

    silences = []
    s_start = None
    for line in text.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            s_start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and s_start is not None:
            silences.append((s_start, float(m.group(1))))
            s_start = None
    if s_start is not None:
        silences.append((s_start, duration))

    # Complement of silence = voice.
    voice = []
    cur = 0.0
    for a, b in silences:
        a = max(0.0, min(duration, a))
        b = max(0.0, min(duration, b))
        if a > cur + 0.1:
            voice.append((cur, a))
        cur = max(cur, b)
    if cur < duration - 0.1:
        voice.append((cur, duration))
    return voice


def is_hallucination(text):
    t = re.sub(r"\s+", "", text).lower()
    for phrase in HALLUCINATION_PHRASES:
        if re.sub(r"\s+", "", phrase).lower() in t:
            return True
    return False


def clip_to_voice(start_t, end_t, voice):
    """Clip a segment to the union of voice intervals; return kept [lo, hi] pairs."""
    kept = []
    for vs, ve in voice:
        lo, hi = max(start_t, vs), min(end_t, ve)
        if hi - lo >= MIN_CUE_SEC:
            kept.append((lo, hi))
    return kept


def whisper_http_transcribe(wav_path, context="", language="ja"):
    boundary = "----AISubsBoundary7d3f"
    with open(wav_path, "rb") as f:
        wav_data = f.read()

    body = bytearray()

    def add_field(name, value, filename=None, content_type=None):
        body.extend(f"--{boundary}\r\n".encode())
        if filename:
            body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
            body.extend(value)
            body.extend(b"\r\n")
        else:
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

    add_field("file", wav_data, "chunk.wav", "audio/wav")
    add_field("response_format", "verbose_json")
    if language:
        add_field("language", language)
    if context:
        add_field("prompt", context)
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        WHISPER_URL,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode("utf-8").strip()
    result = json.loads(raw)
    segments = []
    for seg in result.get("segments", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        # Skip likely hallucination on silence/noise.
        if seg.get("no_speech_prob", 0) > 0.6:
            continue
        start_t = float(seg.get("start", 0))
        end_t = float(seg.get("end", 0))
        # Refine boundaries with word-level timestamps so silence at the head/tail
        # is excluded: subtitles appear when speech actually starts, not before.
        words = [w for w in seg.get("words", []) if w.get("start") is not None and w.get("end") is not None]
        if words:
            ws = float(words[0]["start"])
            we = float(words[-1]["end"])
            if we > ws:
                start_t, end_t = ws, we
        segments.append({"text": text, "start": start_t, "end": end_t})
    full_text = result.get("text", "").strip()
    return full_text, segments, ""


def warmup_mlx():
    """Pre-load MLX model + compile Metal kernels once at startup.

    First mlx_whisper.transcribe on this Mac took ~18s (Metal shader compile);
    doing it here means the first real subtitle request isn't stalled. Safe to
    call repeatedly: mlx_whisper caches the model in-process after first use.
    """
    global _mlx_error
    if ASR_BACKEND != "mlx":
        _mlx_ready.set()
        return
    try:
        import wave
        # MLX keeps its own Metal buffer cache, which on a 16 GB Mac competes
        # with the llama translation server and pushes the system into swap
        # (that swap is what made ASR look "randomly 7-14s slow": the weights
        # get paged out and re-read from disk on every call).
        try:
            import mlx.core as mx
            mx.set_cache_limit(int(float(os.environ.get("MLX_CACHE_MB", "512"))) * 1024 * 1024)
        except Exception:
            pass
        fd, probe = tempfile.mkstemp(prefix="mlxwarm_", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(probe, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 8000)  # 0.5s silence
            mlx_whisper_transcribe(probe)
        finally:
            os.unlink(probe)
    except Exception as e:
        _mlx_error = f"{type(e).__name__}: {e}"
    finally:
        _mlx_ready.set()


DEGENERATE_CR = 4.0        # compression_ratio above this == repetition loop
DEGENERATE_RUN = 6         # same character repeated this many times in a row
# A loop does not have to repeat one character: "あらあらあら…", "はいはいはい…",
# "あーーーっあーーーっ…" all slide under a same-char rule, and their
# compression_ratio can stay low (2.6 measured) because Japanese compresses well
# anyway. What they share is a collapsing vocabulary: a 23-char line built from
# 5 distinct characters. Real dialogue sits above 0.5.
DEGENERATE_MIN_LEN = 12
DEGENERATE_DISTINCT = 0.35
# Escalating to the temperature ladder is worth it only when there is real
# dialogue to recover; below this much speech the chunk is ambience/music and a
# retry just burns 3-8s to reproduce noise.
RETRY_MIN_SPEECH_SEC = 1.2
# Cues shorter than this are unreadable in practice (0.3s flashes past). They get
# extended up to the next cue, never overlapped.
MIN_CUE_DISPLAY = float(os.environ.get("MIN_CUE_DISPLAY", "1.0"))
# VAD closes a moaned/breathy line before the audio actually stops (measured: the
# subtitle for 「ちょっと待って」ended at 1312.5 while the voice ran to 1313.5).
CUE_TAIL_PAD = float(os.environ.get("CUE_TAIL_PAD", "0.5"))


def _strip_punct(text):
    # Long-vowel marks stay: stripping "ー" turned "あーーーっあーーーっ…" into
    # "あっあっあっあっうん", which is short enough to dodge the vocabulary test.
    return re.sub(r"[\s、。,.!?！？…‥・\-]+", "", text)


def _looks_degenerate(text, segments):
    """Whisper's failure mode on short/silence-heavy chunks: a token or phrase
    repeated to the token limit ("あ、あ、あ…", "あらあらあら…", "セセセ…").

    Four cheap signals, all calibrated on real failures:
      - compression_ratio explodes (normal speech 0.5-1.0 -> loop 31-51);
      - one character repeats >= 6 times in a row;
      - a short unit (1-5 chars) is repeated >= 4 times back to back
        ("あらあらあら", "はいはいはい");
      - vocabulary collapses: >= 12 chars with < 35% distinct characters
        (the "あーーーっあーーーっ…うーん" case, cr only 2.65).
    """
    if not text:
        return True
    cr = max([s.get("compression_ratio") or 0 for s in segments] or [0])
    if cr > DEGENERATE_CR:
        return True

    core = _strip_punct(text)
    if not core:
        return True
    if re.search(r"(.)\1{%d,}" % (DEGENERATE_RUN - 1), core):
        return True

    n = len(core)
    if n >= DEGENERATE_MIN_LEN:
        for unit in range(1, 6):
            reps = n // unit
            if reps >= 4 and core[:unit * reps] == core[:unit] * reps:
                return True
        if len(set(core)) / n < DEGENERATE_DISTINCT:
            return True
    return False


def _clean_context(context):
    """Refuse to feed a repetition loop back in as the next chunk's prompt.

    The plugin sends the tail of the previous Japanese line as `initial_prompt`.
    If that line was itself a loop, every following chunk inherits it and starts
    looping too -- observed live: "@6010 あらあらあら..." followed by 3 chunks of
    "あらあらあらあら..." that no longer matched the audio at all.
    """
    if not context:
        return ""
    ctx = context.strip()
    if len(ctx) > 60:
        ctx = ctx[-60:]
    core = _strip_punct(ctx)
    if len(core) >= DEGENERATE_MIN_LEN:
        for unit in range(1, 6):
            reps = len(core) // unit
            if reps >= 3 and core[:unit * reps] == core[:unit] * reps:
                return ""
        if len(set(core)) / len(core) < DEGENERATE_DISTINCT:
            return ""
    return ctx


def _group_spans(spans, k):
    """Merge consecutive speech spans into k groups of roughly equal speech time.

    Assigning sentence i to span i breaks down in breathy scenes: VAD returns ten
    0.2s blips, so sentence 1 gets a blip at the head and the last sentence lands
    ~1s before its audio. Grouping keeps each sentence's window covering the
    stretch of audio it was actually decoded from.
    """
    if k <= 0 or not spans:
        return []
    if k >= len(spans):
        return spans[:k]
    total = sum(e - s for s, e in spans)
    target = total / k
    groups, cur, acc = [], [], 0.0
    for idx, sp in enumerate(spans):
        cur.append(sp)
        acc += sp[1] - sp[0]
        left_spans = len(spans) - idx - 1
        groups_left = k - len(groups)
        # close the bucket when it has its share, or when the remaining spans are
        # only just enough to give every remaining sentence a bucket
        if groups_left > 1 and (acc >= target or left_spans == groups_left - 1):
            groups.append((cur[0][0], cur[-1][1]))
            cur, acc = [], 0.0
    if cur:
        groups.append((cur[0][0], cur[-1][1]))
    return groups


def _pad_short_cues(cues, start, duration):
    """Give every cue a readable dwell time without overlapping the next one."""
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else start + duration + 0.5
        if c["end"] - c["start"] < MIN_CUE_DISPLAY:
            c["end"] = round(min(c["start"] + MIN_CUE_DISPLAY, max(nxt, c["end"])), 2)
        limit = nxt if nxt > c["start"] + 0.05 else c["end"]
        c["end"] = round(min(c["end"] + CUE_TAIL_PAD, max(limit, c["end"])), 2)
    return cues


# --- vocalisations ----------------------------------------------------------
# Breathy / onomatopoeic scenes produce strings like "あ、あ、あ…",
# "あらあらあら…", "あーーーっあーーーっ…". They carry no words, so showing the
# loop is wrong and dropping them leaves the scene silent. The viewer wants one
# line that conveys "a long sound": 啊～ / 嗯～ / 哎呀～.
VOCAL_TABLE = {
    "あ": "啊～", "ア": "啊～", "ぁ": "啊～", "ァ": "啊～",
    "い": "咿～", "イ": "咿～", "ぃ": "咿～", "ィ": "咿～",
    "う": "呜～", "ウ": "呜～", "ぅ": "呜～", "ゥ": "呜～",
    "え": "诶～", "エ": "诶～", "ぇ": "诶～", "ェ": "诶～",
    "お": "哦～", "オ": "哦～", "ぉ": "哦～", "ォ": "哦～",
    "ん": "嗯～", "ン": "嗯～",
    "は": "哈～", "ハ": "哈～", "ふ": "呼～", "フ": "呼～",
    "ひ": "咿～", "ヒ": "咿～", "へ": "嘿～", "ヘ": "嘿～",
}
KANA_ONLY = re.compile(r"[ぁ-ゖァ-ヺー]+")
# Any Japanese character (kana or kanji). With language forced to "ja", text that
# contains none of these is a Whisper stock hallucination, not speech: real
# examples from this film were "I'm not going to die" and "Oh, my God" emitted
# over silent/ambient chunks, and they were being shown as subtitles.
JA_CHAR = re.compile(r"[ぁ-ゖァ-ヺ一-鿿]")


# A one-word interjection followed by ellipsis ("啊…") reads like a stutter;
# the viewer asked for the elongation marker instead ("啊～").
INTERJECTION_RE = re.compile(
    r"^(啊|呀|嗯|哈|呜|哦|诶|哎呀|咿|嘿|呼|唔|呃|哎|唉)([….~～—\-!！?？]*)$")


def _normalize_interjection(zh):
    m = INTERJECTION_RE.match((zh or "").strip())
    return m.group(1) + "～" if m else zh


def _collapse_unit(core):
    """Smallest unit the string is an EXACT repetition of ("あらあら" -> "あら").

    Exact division matters: a prefix match turned "あーーーあーーーごめん" into a
    pure sound and silently dropped the word ごめん.
    """
    n = len(core)
    for unit in range(1, min(6, n) + 1):
        if n % unit == 0 and core == core[:unit] * (n // unit):
            return core[:unit]
    return core


def _vocal_zh(text):
    """Return a single Chinese line for a pure vocalisation, else None.

    Two shapes qualify, both kana-only:
      - a repetition loop ("あ、あ、あ", "あらあらあら", "んんんん");
      - one syllable held long ("あーーー", "はぁ").
    Anything carrying a word -- はい, あ、イク, あーーーあーーーごめん -- is left to
    the translator, which is why the unit body is required to be a single kana.
    """
    core = _strip_punct(text or "")
    if not core or KANA_ONLY.fullmatch(core) is None:
        return None

    unit = _collapse_unit(core)
    # Small kana and ー modify a vowel rather than adding a syllable: はぁ -> は.
    body = re.sub(r"[ーっッぁぃぅぇぉァィゥェォ]", "", unit)
    repeated = len(unit) < len(core)
    held = "ー" in core
    if not (repeated or held):
        return None
    # A sound, not a word: the repeated/held unit must be a single syllable
    # (あ, ん, はぁ, あーーー). "いやいやいや" / "あーーーあーーーごめん" carry words
    # and go to the translator.
    if len(body) > 1 and not unit.startswith("あら") and not unit.startswith("アラ"):
        return None

    if unit.startswith("あら") or unit.startswith("アラ"):
        return "哎呀～"
    head = body[:1] or core[:1]
    return VOCAL_TABLE.get(head, "啊～")


def _has_japanese(text):
    return JA_CHAR.search(text or "") is not None


def mlx_whisper_transcribe(wav_path, context="", language="ja", speech_sec=None, vad_conf=0.0):
    """In-process mlx-whisper (Apple Metal), most accurate of the local engines.

    Call contract (measured, not guessed):
      - wav FILE path; a numpy array pads to 30s and hallucinates on the tail.
      - no beam_size (mlx-whisper 0.4.3 raises NotImplementedError).
      - greedy first (temperature=0) => ~1.0s per 6.5s window, and byte-identical
        to the full ladder on every normal dialogue window tested (12 windows).
      - the ladder is only paid when greedy degenerates into a repetition loop
        (see _looks_degenerate), because that is the only case where a higher
        temperature produces the real sentence. Bounded to 0.2/0.4: the full
        ladder costs 13-15s, the bounded one 2.5-7.5s and recovers the same text.
      - if the loop happens with almost no speech in the chunk (BGM/ambience),
        return empty instead of paying for a retry.
    Segment timestamps are Whisper's fake evenly-spread values; cue boundaries
    come from Silero VAD instead.
    """
    import mlx_whisper

    def run(temps):
        kwargs = dict(path_or_hf_repo=MLX_MODEL, verbose=False,
                      condition_on_previous_text=False)
        if language:
            kwargs["language"] = language
        if context:
            kwargs["initial_prompt"] = context
        if temps:
            kwargs["temperature"] = temps
        return mlx_whisper.transcribe(wav_path, **kwargs)

    def to_segments(result):
        out = []
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            out.append({"text": text,
                        "start": float(seg.get("start", 0)),
                        "end": float(seg.get("end", 0)),
                        "compression_ratio": seg.get("compression_ratio") or 0})
        return out

    result = run((0.0,))
    full = (result.get("text") or "").strip()
    segments = to_segments(result)
    degenerate = _looks_degenerate(full, result.get("segments", []))

    # Pure vocalisation (breathy/onomatopoeic scene): one short line saying the
    # sound is long beats both alternatives -- a looping string the viewer can't
    # read, or nothing at all. Checked BEFORE the retry: retrying a scream only
    # buys 2.5-7.5s and the same scream.
    vocal = _vocal_zh(full)
    if vocal and (degenerate or len(_strip_punct(full)) <= 8):
        trace(f"[asr] vocalisation -> {vocal} (degenerate={degenerate})")
        return "", [], vocal

    # Forced to "ja" yet the output contains no Japanese at all -> it is a stock
    # hallucination ("Oh, my God", "I'm not going to die"). Paying for the
    # temperature ladder here just reproduces it: one chunk burned 13.8s on the
    # way to being dropped anyway.
    if not _has_japanese(full):
        # VAD is confident there is speech, yet greedy produced no Japanese at
        # all. That is usually a hallucination, but on real speech a higher
        # temperature sometimes lands on the words (one live case: vmax=0.83,
        # 0.74s speech, greedy output English). Retry only when VAD insists.
        if vad_conf >= 0.6 and (speech_sec or 0) >= 0.5:
            retry = run((0.2, 0.4))
            rt = (retry.get("text") or "").strip()
            if _has_japanese(rt) and not _looks_degenerate(rt, retry.get("segments", [])):
                trace("[asr] no-ja greedy -> ladder recovered japanese")
                return rt, to_segments(retry), ""
        trace("[asr] no japanese in forced-ja output -> dropped without retry")
        return "", [], ""

    if not degenerate:
        return full, segments, ""

    if speech_sec is not None and speech_sec < RETRY_MIN_SPEECH_SEC:
        # Repetition loop on a chunk with (almost) no speech: it's music. Emitting
        # nothing is correct; retrying would only produce more noise, slower.
        trace(f"[asr] degenerate on {speech_sec:.2f}s speech -> dropped")
        return "", [], ""

    retry = run((0.2, 0.4))
    retry_full = (retry.get("text") or "").strip()
    retry_segments = to_segments(retry)
    if retry_full and not _looks_degenerate(retry_full, retry.get("segments", [])):
        trace(f"[asr] ladder retry recovered {len(retry_full)} chars")
        return retry_full, retry_segments, ""
    vocal = _vocal_zh(retry_full)
    if vocal:
        trace(f"[asr] retry vocalisation -> {vocal}")
        return "", [], vocal
    # Both attempts looped on real words. Emitting the loop as a subtitle is
    # worse than showing nothing (and a 200-char loop costs ~6s of translation).
    trace("[asr] both attempts degenerate -> dropped")
    return "", [], ""


def _get_fw_model():
    """Lazily load the CT2 model once; it stays resident for all requests."""
    global _fw_model
    if _fw_model is None:
        from faster_whisper import WhisperModel
        _fw_model = WhisperModel(FW_MODEL_PATH, device="cpu", compute_type="int8")
    return _fw_model


def faster_whisper_transcribe(wav_path, context="", language="ja"):
    """In-process faster-whisper with real word-level timestamps and VAD."""
    model = _get_fw_model()
    kwargs = {
        "beam_size": 5,
        # Silero VAD inside faster-whisper misclassified real quiet dialogue
        # (e.g. "おはようございます" at ~20s) as silence and dropped it. We gate
        # silence ourselves with ffmpeg silencedetect in process(), so disable it.
        "vad_filter": False,
        "word_timestamps": True,
        # Don't let one chunk's hallucination feed the next chunk.
        "condition_on_previous_text": False,
    }
    if language:
        kwargs["language"] = language
    if context:
        kwargs["initial_prompt"] = context
    seg_iter, _ = model.transcribe(wav_path, **kwargs)
    texts, segments = [], []
    for seg in seg_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        texts.append(text)
        words = [w for w in (seg.words or []) if w.start is not None and w.end is not None]
        start_t, end_t = float(seg.start), float(seg.end)
        if words:
            ws, we = float(words[0].start), float(words[-1].end)
            if we > ws:
                start_t, end_t = ws, we
        segments.append({"text": text, "start": start_t, "end": end_t})
    return " ".join(texts), segments, ""


def whisper_transcribe(wav_path, context="", language="ja", speech_sec=None, vad_conf=0.0):
    """Returns (japanese_text, segments, vocal_zh). vocal_zh is a ready-made
    Chinese line for pure vocalisations (啊～/嗯～), in which case the text is empty
    and no translation is needed."""
    if ASR_BACKEND == "http":
        return whisper_http_transcribe(wav_path, context, language)
    if ASR_BACKEND == "mlx":
        _mlx_ready.wait(timeout=120)
        if _mlx_error:
            raise RuntimeError("mlx backend failed: " + _mlx_error)
        return mlx_whisper_transcribe(wav_path, context, language, speech_sec, vad_conf)
    return faster_whisper_transcribe(wav_path, context, language)


CJK_IDEOGRAPH = re.compile(r"[\u4e00-\u9fff]")
KANA_CHAR = re.compile(r"[ぁ-ゖァ-ヺ]")
# 纯拉丁字母/数字的短译文是合法的：日语里的外来语（バイバイ → byebye、OK）
# 本来就该原样保留，硬转成中文反而失真。
LATIN_ONLY = re.compile(r"^[A-Za-z0-9\s'’.,!?…&%/:;\-]+$")
EN_REFUSAL = re.compile(r"sorry|cannot|can't|unable|as an ai|i'?m not|please provide", re.I)
KANA_RUN = re.compile(r"[ぁ-ゖァ-ヺー]+")

# 译文里残留的假名多半是模型保留的**专名/昵称**（`果然我就是ダメくん` 其实译对了，
# 只是「ダメくん」没转）。整条丢掉是最差的选择：屏幕上什么都没有，观众还得猜。
# 这里先把常见词换掉；换不掉的短专名允许留着（见 _valid_zh）。
KANA_FIX = [
    ("ダメくん", "废柴君"), ("だめくん", "废柴君"),
    ("ダメ", "不行"), ("だめ", "不行"),
    ("ヤバい", "糟了"), ("やばい", "糟了"), ("ヤバ", "糟"),
    ("バカ", "笨蛋"), ("ばか", "笨蛋"),
    ("スケベ", "色鬼"), ("エロい", "色情"),
    ("ありがとう", "谢谢"), ("ごめん", "对不起"),
    ("すみません", "不好意思"), ("オッケー", "好的"),
    ("まあまあ", "算了算了"), ("まあ", "嘛"),
    ("えーと", "那个"), ("えっと", "那个"),
    ("ちゃん", "酱"), ("チャン", "酱"),
    ("くん", "君"), ("クン", "君"),
    ("さん", "桑"), ("サン", "桑"),
]


def _clean_kana(zh):
    """把译文里残留的假名词换成中文，换不掉的留着（专名）。"""
    out = zh or ""
    for jp, cn in KANA_FIX:
        out = out.replace(jp, cn)
    return out


# 小模型偶尔会对内容"讲道理"而不是翻译，把说明文字当成译文吐出来。实测原样
# 上屏过一整句："对不起，这句话包含了一些不合适的表达方式，我将重新翻译一句
# 更合适的句子：你生气了"。
META_ZH = re.compile(
    r"(重新翻译|不合适的表达|不合适的内容|不清晰的发音|发音和语气|"
    r"无法直接翻译|没办法翻译|无法翻译|不能翻译|"
    r"作为\s*AI|作为人工智能|语言模型|"
    r"我无法(直接)?(提供|回答|协助|判断|确定)|我不能(提供|回答|协助)|"
    r"抱歉[，,]\s*(我|这)|请注意[，,]|该内容|这段内容|"
    r"如果你有明确的|可以提供给我|很乐意帮你|请提供更多|需要更多上下文)")

# 台词里出现"翻译/译文"本身就极罕见，再叠加拒绝语气才算模型在讲道理。
_META_WORD = re.compile(r"翻译|译文|原文")


def _is_meta(zh):
    """模型在"讲怎么翻译"，而不是在翻译。

    "翻译"这两个字出现在中文字幕里几乎只可能是元话术（日语台词里极少说
    「翻訳」），所以只要出现就判掉——除非整句就是这个词本身。被误杀的原文
    仍然会写进 untranslated.log，不会静默丢失。
    """
    if not zh:
        return False
    if META_ZH.search(zh):
        return True
    if _META_WORD.search(zh):
        core = re.sub(r"[\s，。！？、,.!?…：:]+", "", zh)
        return len(core) > 2
    return False

# 这类字幕几乎不可能超过这个长度；超过说明模型在解释而不是翻译。
MAX_SUBTITLE_CHARS = 60


# 只有"我要重新译一遍"这类**更正**语里，冒号后面的才是真译文；
# "这个提议可以翻译为：X" 是模型在解释，X 是它编的，不能当字幕。
_RETRY_HINT = re.compile(r"重新翻译|更合适|更准确|更正|正确的是")


def _strip_meta(zh):
    """Drop a model's meta commentary, keeping the translation it appended."""
    if not zh or not _is_meta(zh):
        return zh
    if _RETRY_HINT.search(zh):
        tail = re.split(r"[：:]", zh)[-1].strip()
        if tail and not _is_meta(tail) and 2 <= len(tail) <= MAX_SUBTITLE_CHARS:
            return tail
    return ""


def _valid_zh(zh):
    """Reject translations that came back as Japanese (qwen echoes short lines)
    or as the model talking about the content instead of translating it.

    Seen live: 割れちゃうから came back as 裂けちゃうから and was displayed.
    """
    if not zh:
        return False
    if _is_meta(zh) or len(zh) > MAX_SUBTITLE_CHARS:
        return False
    # 假名：允许短的专名（ダメくん / ちゃん），但整句回吐日文（けちゃうから、
    # おはようございます）必须拒绝 —— 那是没翻译，不是保留专名。
    runs = KANA_RUN.findall(zh)
    if runs:
        if max(len(r) for r in runs) > 4:
            return False
        total = sum(len(r) for r in runs)
        if total / max(len(zh), 1) > 0.4:
            return False
    if CJK_IDEOGRAPH.search(zh):
        return True
    if LATIN_ONLY.match(zh) and len(zh) <= 24 and not EN_REFUSAL.search(zh):
        return True
    # pure interjections (啊～ / 嗯～) are fine without ideographs
    return re.fullmatch(r"[啊呀嗯哈呜哦诶哎呀咿嘿呼唔呃哎唉～~…\s!！?？]+", zh) is not None


def translate(ja_text):
    raw = _translate_once(ja_text)
    out = _clean_kana(_strip_meta(raw))
    if _valid_zh(out):
        return out
    if raw and raw != out:
        trace("[tr] model talked instead of translating: %s" % raw.strip()[:90])
    # One strict retry, then give up: a Japanese line under Chinese subtitles is
    # worse than a missing line.
    # 提高温度后结果有随机性（实测 4 次里 3 次能译出），所以给两个采样点。
    last_raw = raw
    for temp in (0.3, 0.6):
        retry_raw = _translate_once(ja_text, strict=True, temp=temp)
        last_raw = retry_raw or last_raw
        retry = _clean_kana(_strip_meta(retry_raw))
        if _valid_zh(retry):
            return retry
    LAST_TRANSLATE_RAW[0] = (last_raw or "").strip()
    return ""


def _translate_once(ja_text, strict=False, temp=None):
    payload = {
        "model": "qwen",
        "messages": [
            {"role": "system", "content": SYS_PROMPT + (
                "只输出简体中文，绝对不要输出日文假名，不要把原文照抄一遍。"
                if strict else "")},
            {"role": "user", "content": ja_text},
        ],
        # strict 重试要离开 temperature=0：3B 模型在 0 下遇到口语噪声会退化成
        # "复制输入"（实测输出 `啊～まあまあまあ、おもちゃの方が…` 原样照抄）。
        "temperature": temp if temp is not None else (0.3 if strict else 0),
        "max_tokens": 200,
        "stream": False,
    }
    req = urllib.request.Request(
        LLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def process(video, start, duration, context, language="ja"):
    context = _clean_context(context)
    _t0 = time_mod.time()
    _st = {"extract": 0.0, "vad": 0.0, "asr": 0.0, "tr": 0.0, "tr_n": 0}

    # 字幕缓存里已经有这一段：直接返回，连音频都不抽
    # （省掉 ffmpeg + VAD + ASR + 翻译）。缓存是边看边累加的，所以"看多少补多少"，
    # 没看过的区间自然回到下面的识别路径。
    try:
        reused, known = get_sidecar(video).lookup(start, duration)
    except Exception:
        reused, known = [], False

    if known:
        if reused:
            trace("[srt] cache hit @%7.1f %d cues %.3fs"
                  % (start, len(reused), time_mod.time() - _t0))
            film_log(video, "复用", start,
                     "字幕缓存已有：%s" % " / ".join(c["zh"] for c in reused)[:60])
            return {"ok": True, "ja": "", "zh": " ".join(c["zh"] for c in reused),
                    "cues": reused, "from_srt": True,
                    # 复用的内容不是这一块现听的，别让它变成下一块的 prompt。
                    "reset_context": True,
                    "_dbg": {"total": round(time_mod.time() - _t0, 3), "reuse": 1}}
        # 这一段处理过、而且没有字幕 -> 当时就判定没有对白，不必再听一遍。
        trace("[srt] cache hit @%7.1f empty (no dialogue here)" % start)
        film_log(video, "复用", start, "字幕缓存里这一段没有对白，跳过识别")
        return {"ok": False, "ja": "", "zh": "", "cues": [], "from_srt": True,
                "reset_context": True, "error": "silence",
                "_dbg": {"total": round(time_mod.time() - _t0, 3), "reuse": 1}}

    def tr(text):
        _t = time_mod.time()
        try:
            out = translate(text)
        finally:
            _st["tr"] += time_mod.time() - _t
            _st["tr_n"] += 1
        if not out and (text or "").strip():
            # 识别出来了但没译出来：原文写进 untranslated.log，供人工校对。
            untranslated_log(start, text, LAST_TRANSLATE_RAW[0])
            film_log(video, "未译", start, text)
        return out

    def done(obj):
        if TRACE:
            # ja= keeps the raw recognition (120 chars) even for dropped chunks,
            # so "what did it hear, and why was it thrown away" is answerable
            # straight from the log.
            line = ("[brk] start=%7.1f ext=%5.2f vad=%5.2f asr=%5.2f tr=%5.2f(n=%d) "
                    "tot=%5.2f vmax=%.2f sp=%4.2f%s %s%s"
                    % (start, _st["extract"], _st["vad"], _st["asr"], _st["tr"],
                       _st["tr_n"], time_mod.time() - _t0, _st.get("vmax", 0.0),
                       _st.get("sp", 0.0), " WEAK" if _st.get("weak") else "",
                       obj.get("error", ""),
                       (" ja=" + obj.get("ja", "")[:120]) if obj.get("ja") else ""))
            trace(line)
            # per-film readable log: what was heard, what was shown, what was dropped
            ja_text = obj.get("ja") or ""
            if ja_text:
                film_log(video, "识别", start, ja_text)
            elif obj.get("vocal"):
                film_log(video, "识别", start, "（气声/拟声，无台词）")
            for c in obj.get("cues") or []:
                film_log(video, "字幕", c["start"], c["zh"],
                         "   [%s → %s]" % (_tfmt(c["start"]), _tfmt(c["end"])))
            if obj.get("error"):
                film_log(video, "放弃", start, "%s — %s"
                         % (obj["error"], REASON_CN.get(obj["error"], "")),
                         "   (vmax=%.2f sp=%.2f%s)"
                         % (_st.get("vmax", 0.0), _st.get("sp", 0.0),
                            " WEAK" if _st.get("weak") else ""))
                drop_log(start, obj.get("error"), obj.get("ja", ""), obj.get("zh", ""),
                         _st.get("vmax", 0.0), _st.get("sp", 0.0), bool(_st.get("weak")))
            obj["_dbg"] = {k: round(v, 3) for k, v in _st.items()}
            obj["_dbg"]["total"] = round(time_mod.time() - _t0, 3)
        return obj

    with tempfile.TemporaryDirectory(prefix="aisubs_") as td:
        wav = os.path.join(td, "chunk.wav")
        # Capture a little past the chunk end so a sentence straddling the
        # boundary is transcribed whole instead of being cut mid-word.
        capture = duration + LOOKAHEAD_SEC
        _t = time_mod.time()
        extract_audio(video, start, capture, wav)
        _st["extract"] = time_mod.time() - _t

        # Gate 1: no real speech in this chunk -> don't even call Whisper.
        # Kills silence/music hallucinations (e.g. "谢谢观看" emitted with
        # no_speech_prob=0.000 on pure silence). Judge speech only within the
        # owned window [0, duration]; trailing overlap is context, not content.
        _t = time_mod.time()
        voice, vad_max_prob = detect_voice_intervals(wav, capture)
        _st["vad"] = time_mod.time() - _t
        _st["vmax"] = round(vad_max_prob, 3)
        owned_voice = [(s, min(e, duration)) for s, e in voice if s < duration]
        speech_sec = sum(max(0, e - s) for s, e in owned_voice)
        # "Weak audio": Silero hears no clear speech, but the chunk is not quiet
        # either -- breathy / onomatopoeic scenes score 0.2-0.3 while music and
        # silence score lower. We still run ASR on those, but accept ONLY a
        # vocalisation result (啊～), never invented words.
        weak = False
        if speech_sec < MIN_SPEECH_SEC:
            if vad_max_prob < VAD_WEAK_MIN_PROB:
                return done({"ok": False, "ja": "", "zh": "", "cues": [],
                             "error": "silence"})
            weak = True
            _st["weak"] = 1
            voice, _ = detect_voice_intervals(wav, capture, threshold=VAD_WEAK_PROB)
            owned_voice = [(s, min(e, duration)) for s, e in voice if s < duration]
            speech_sec = sum(max(0, e - s) for s, e in owned_voice)

        def emit_vocal(zh):
            """One cue over the widest speech span: the sound has no words."""
            spans = [(s, min(e, duration)) for s, e in voice if s < duration]
            spans = [(s, e) for s, e in spans if e - s >= MIN_CUE_SEC]
            if not spans:
                spans = [(0.0, duration)]
            lo, hi = max(spans, key=lambda p: p[1] - p[0])
            cues = [{"start": round(start + lo, 2), "end": round(start + hi, 2), "zh": zh}]
            _pad_short_cues(cues, start, duration)
            return done({"ok": True, "ja": "", "zh": zh, "cues": cues, "vocal": True})

        _st["sp"] = round(speech_sec, 2)
        _t = time_mod.time()
        ja, segments, vocal_zh = whisper_transcribe(wav, context, language, speech_sec,
                                                    vad_max_prob)
        _st["asr"] = time_mod.time() - _t

        # Vocalisation: no words to translate, just one line marking a long sound.
        if vocal_zh:
            return emit_vocal(vocal_zh)

        # Whisper, forced to "ja", sometimes answers in English on non-speech
        # audio ("I'm not going to die", "Oh, my God"). Those cannot be real
        # dialogue here, and they were being translated and shown.
        ja_ok = bool(ja) and _has_japanese(ja) and not _looks_degenerate(ja, segments)

        if weak:
            # A breathy chunk may still hold a short real line ("あ、イク"), which
            # is worth translating; if there are no usable words, mark the sound
            # itself -- but only when it lasts long enough to be a moan/breath.
            if not ja_ok:
                if speech_sec >= MOAN_MIN_SEC:
                    trace(f"[asr] weak audio {speech_sec:.2f}s -> 啊～")
                    return emit_vocal("啊～")
                return done({"ok": False, "ja": ja, "zh": "", "cues": [], "error": "weak audio"})
        elif not ja_ok:
            return done({"ok": False, "ja": ja, "zh": "", "cues": [],
                         "error": "non-japanese hallucination"})

        if not ja:
            return done({"ok": False, "ja": "", "zh": "", "cues": [], "error": "empty transcription"})

        # Gate 2: translate each sentence separately, clip its window to actual
        # speech, and drop hallucinated stock phrases. A sentence whose start
        # falls in the overlap (past `duration`) is left for the next chunk so
        # it never shows twice. No whole-chunk fallback: a filtered sentence
        # means "nothing to show", never "show it for the whole chunk".
        cues = []
        if ASR_BACKEND in ("http", "mlx"):
            # Both whisper.cpp and MLX use Whisper's "evenly spread" fake
            # timestamps (each sentence snapped to integer seconds), unusable
            # for subtitle boundaries. Real speech timing comes from ffmpeg
            # silencedetect (voice). Assign each recognized sentence, in order,
            # to a voice segment in the owned window; drop sentences with no
            # voice segment left.
            #
            # Owned voice segments clipped to [0, duration]. The trailing
            # lookahead is context only, never emitted here.
            owned = [(s, min(e, duration)) for s, e in voice if s < duration]
            owned = [(s, e) for s, e in owned if e - s >= MIN_CUE_SEC]
            usable = [s for s in segments if not is_hallucination(s["text"])]
            if not owned or not usable:
                pass  # -> "no valid speech" below
            elif len(owned) >= len(usable):
                # Enough real voice gaps: assign one sentence per gap, in order.
                # With many small gaps this groups them by speech time instead.
                groups = _group_spans(owned, len(usable))
                for i, seg in enumerate(usable):
                    zh = tr(seg["text"])
                    if zh and not is_hallucination(zh):
                        lo, hi = groups[i]
                        cues.append({"start": round(start + lo, 2),
                                     "end": round(start + hi, 2), "zh": zh})
            else:
                # Fewer voice gaps than sentences (continuous speech): split
                # the whole owned speech span evenly among all sentences so
                # every line gets a cue and a distinct time window.
                lo, hi = owned[0][0], max(e for _, e in owned)
                step = (hi - lo) / len(usable)
                for i, seg in enumerate(usable):
                    zh = tr(seg["text"])
                    if zh and not is_hallucination(zh):
                        cues.append({
                            "start": round(start + lo + i * step, 2),
                            "end": round(start + lo + (i + 1) * step, 2),
                            "zh": zh,
                        })
        else:
            # faster-whisper has genuine word-level timestamps: clip each
            # sentence to the real speech spans intersecting its window.
            for seg in segments:
                if is_hallucination(seg["text"]):
                    continue
                spans = clip_to_voice(seg["start"], seg["end"], voice)
                spans = [(lo, min(hi, duration)) for lo, hi in spans if lo < duration]
                spans = [(lo, hi) for lo, hi in spans if hi - lo >= MIN_CUE_SEC]
                if not spans:
                    continue
                zh = tr(seg["text"])
                if not zh or is_hallucination(zh):
                    continue
                for lo, hi in spans:
                    cues.append({
                        "start": round(start + lo, 2),
                        "end": round(start + hi, 2),
                        "zh": zh,
                    })
        if not cues:
            return done({"ok": False, "ja": ja, "zh": "", "cues": [], "error": "no valid speech"})
        for _c in cues:
            _c["zh"] = _normalize_interjection(_c["zh"])
        _pad_short_cues(cues, start, duration)
        return done({"ok": True, "ja": ja, "zh": " ".join(c["zh"] for c in cues), "cues": cues})


HARDSUB_TIMEOUT = 45
_hardsub_cache = {}


def check_hardsub(video, start):
    """画面里是否已经有烧进去的字幕（硬字幕）。

    只裁底部字幕带、交给 tools/hardsub_check.py（macOS Vision OCR）判定。
    每 5 分钟粒度缓存一次，避免同一部片反复检测。
    """
    key = (video, int(float(start) // 300))
    if key in _hardsub_cache:
        return _hardsub_cache[key]
    script = os.path.join(ROOT, "tools", "hardsub_check.py")
    data = {"hardsub": False, "samples": []}
    try:
        out = subprocess.run([sys.executable, script, video, str(float(start))],
                             capture_output=True, text=True, timeout=HARDSUB_TIMEOUT)
        tail = [l for l in (out.stdout or "").splitlines() if l.strip().startswith("{")]
        if tail:
            data = json.loads(tail[-1])
        elif out.stderr:
            data = {"hardsub": False, "samples": [], "error": out.stderr.strip()[:200]}
    except Exception as e:
        data = {"hardsub": False, "samples": [], "error": "%s: %s" % (type(e).__name__, e)}
    _hardsub_cache[key] = data
    trace("[hardsub] %s @%.1f -> %s %s" % (os.path.basename(video), float(start),
          data.get("hardsub"), data.get("error", "")))
    return data


def probe(video, start, duration, language="ja"):
    """Explain one window end-to-end: VAD at several thresholds, the raw greedy
    transcription, and which rule the current settings would apply.

    Runs inside the bridge so it reuses the resident model (no second copy) and
    exactly the same code path as production.
    """
    with tempfile.TemporaryDirectory(prefix="aisubs_probe_") as td:
        wav = os.path.join(td, "p.wav")
        capture = duration + LOOKAHEAD_SEC
        extract_audio(video, start, capture, wav)
        audio = vad_silero.read_wav_float32(wav)
        probs = vad_silero.speech_probs(audio)
        vmax = float(probs.max()) if probs.size else 0.0

        ths = []
        for thr in (0.5, 0.4, 0.3, 0.2, 0.15):
            iv = vad_silero.speech_intervals(
                audio, threshold=thr, min_silence_ms=VAD_MIN_SILENCE_MS,
                pad_ms=80, min_speech_ms=200)
            ths.append({"thr": thr,
                        "speech": round(vad_silero.speech_seconds(iv, limit=duration), 2),
                        "spans": [[round(a, 2), round(b, 2)] for a, b in iv if a < duration][:8]})

        import mlx_whisper
        r = mlx_whisper.transcribe(wav, path_or_hf_repo=MLX_MODEL, verbose=False,
                                   condition_on_previous_text=False,
                                   language=language, temperature=(0.0,))
        text = (r.get("text") or "").strip()
        segs = r.get("segments", [])
        cr = max([sg.get("compression_ratio") or 0 for sg in segs] or [0])
        degenerate = _looks_degenerate(text, segs)
        has_ja = _has_japanese(text)
        vocal = _vocal_zh(text)

        main_speech = ths[0]["speech"]
        weak = main_speech < MIN_SPEECH_SEC and vmax >= VAD_WEAK_MIN_PROB
        hit = next((ph for ph in HALLUCINATION_PHRASES
                    if re.sub(r"\s+", "", ph).lower() in re.sub(r"\s+", "", text).lower()), "")
        if main_speech < MIN_SPEECH_SEC and not weak:
            verdict = "silence：VAD 无语音（vmax %.2f < %.2f）" % (vmax, VAD_WEAK_MIN_PROB)
        elif hit:
            verdict = "丢弃：命中幻觉黑名单「%s」" % hit
        elif vocal:
            verdict = "拟声 → %s" % vocal
        elif not has_ja:
            verdict = "丢弃：强制日语却无日文（英文幻觉）"
        elif degenerate:
            verdict = "退化 → 有界重试（0.2/0.4），仍退化则丢弃"
        elif weak:
            verdict = "保留：弱语音但识别出日文，正常翻译"
        else:
            verdict = "正常出字幕"

        return {"ok": True, "start": start, "capture": round(capture, 2),
                "vad_max": round(vmax, 3), "thresholds": ths,
                "asr": {"text": text, "compression_ratio": round(cr, 2),
                        "degenerate": degenerate, "has_japanese": has_ja, "vocal": vocal},
                "settings": {"MIN_SPEECH_SEC": MIN_SPEECH_SEC,
                             "VAD_THRESHOLD": VAD_THRESHOLD,
                             "VAD_WEAK_MIN_PROB": VAD_WEAK_MIN_PROB,
                             "VAD_WEAK_PROB": VAD_WEAK_PROB,
                             "MOAN_MIN_SEC": MOAN_MIN_SEC},
                "verdict": verdict}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            if ASR_BACKEND == "http":
                asr_ok = health_check(18180)
            elif ASR_BACKEND == "mlx":
                asr_ok = _mlx_ready.is_set() and not _mlx_error
            else:
                asr_ok = os.path.isfile(os.path.join(FW_MODEL_PATH, "model.bin"))
            self._json({
                "ok": True,
                "backend": ASR_BACKEND,
                "whisper": asr_ok,
                "mlx_ready": _mlx_ready.is_set(),
                "mlx_error": _mlx_error,
                "llama": health_check(18181),
            })
        elif self.path.startswith("/hardsub"):
            try:
                from urllib.parse import urlparse, parse_qs, unquote
                q = parse_qs(urlparse(self.path).query)
                video = unquote((q.get("video") or [""])[0])
                start = float((q.get("start") or ["0"])[0])
                if not video or not os.path.isfile(video):
                    self._json({"ok": False, "hardsub": False, "error": "video not found"}, 400)
                    return
                data = check_hardsub(video, start)
                out = {"ok": True}
                out.update(data)
                self._json(out)
            except Exception as e:
                self._json({"ok": False, "hardsub": False, "error": str(e)}, 500)
        elif self.path.startswith("/subtitle"):
            try:
                from urllib.parse import urlparse, parse_qs, unquote
                q = parse_qs(urlparse(self.path).query)
                video = unquote((q.get("video") or [""])[0])
                duration = float((q.get("duration") or ["0"])[0])
                if not video or not os.path.isfile(video):
                    self._json({"ok": False, "error": "video not found"}, 400)
                    return
                out = {"ok": True}
                out.update(get_sidecar(video).status(duration))
                self._json(out)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/subtitle/finalize":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                video = req.get("video", "")
                if not video or not os.path.isfile(video):
                    self._json({"ok": False, "error": "video not found"}, 400)
                    return
                out = get_sidecar(video).finalize(float(req.get("duration", 0)),
                                                  bool(req.get("force", False)))
                self._json(out)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if self.path == "/probe":
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                video = req.get("video", "")
                if not video or not os.path.isfile(video):
                    self._json({"ok": False, "error": "video not found"}, 400)
                    return
                with _lock:
                    out = probe(video, float(req.get("start", 0)),
                                float(req.get("duration", 4.0)),
                                req.get("language", "ja"))
                self._json(out)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if self.path != "/transcribe":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            video = req.get("video", "")
            start = float(req.get("start", 0))
            duration = float(req.get("duration", 5))
            context = req.get("context", "")
            language = req.get("language", "ja")
            # Only player traffic persists subtitles: manual probes/tests must not
            # pollute the film's .srt.
            save = bool(req.get("save", False))
            if not video or not os.path.isfile(video):
                self._json({"ok": False, "error": "video not found"}, 400)
                return
            # Context affects recognition, so it must be part of the cache key.
            key = f"{video}|{start:.2f}|{duration:.2f}|{language}|{context}"
            t_req = time_mod.time()
            with _lock:
                t_lock = time_mod.time() - t_req
                cached = key in _cache
                if not cached:
                    _cache[key] = process(video, start, duration, context, language)
                    # Context makes almost every key unique, so the cache grows
                    # forever on a long film; keep it bounded (dict is ordered).
                    if len(_cache) > 600:
                        for old in list(_cache)[:200]:
                            del _cache[old]
                result = _cache[key]
                # Persist after the cache read so a cached chunk still lands in
                # the SRT (a restart replays keys that are still in memory).
                if save:
                    try:
                        # 没有字幕的窗口也要登记：否则第二遍还得重跑 ffmpeg
                        # 才能确认这一段确实没人说话。
                        get_sidecar(video).add(result.get("cues") or [],
                                               start, duration)
                    except Exception as exc:
                        trace("[srt] add failed: %s" % exc)
            # src= 直接告诉你这一块走的是哪条路：
            #   srt = 字幕文件里找到了，没用音频
            #   asr = 文件里没有，走的抽取+VAD+识别+翻译
            src = "srt" if result.get("from_srt") else "asr"
            trace(f"[req] start={start:7.1f} dur={duration:.1f} ctx={len(context)} "
                  f"src={src} cached={cached} lock={t_lock:.2f} "
                  f"{time_mod.time()-t_req:.2f}s ok={result.get('ok')} "
                  f"err={result.get('error','')} "
                  f"zh={(result.get('zh') or '')[:60]}")
            self._json(result)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


if __name__ == "__main__":
    # Warm up MLX in a background thread: Metal shader compile is ~18s on this
    # Mac, and doing it here means the first real request isn't stalled. The
    # global _lock doesn't cover startup, but _mlx_ready gates all requests.
    if ASR_BACKEND == "mlx":
        threading.Thread(target=warmup_mlx, daemon=True, name="mlx-warmup").start()
    _rotate_log()
    logline(f"=== bridge start (backend={ASR_BACKEND}, model={os.path.basename(MLX_MODEL)}) ===")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()
