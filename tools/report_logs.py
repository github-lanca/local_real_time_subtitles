#!/usr/bin/env python3
"""Readable report of what the subtitle pipeline recognized, showed and dropped.

Sources:
  logs/bridge.log        per-chunk breakdown + drop reason + raw recognition
  logs/plugin.log        what the player actually rendered (CUE / RENDER)
  /tmp/liveai_debug.log  same, from before the log path moved (older sessions)

Usage:
  python3 tools/report_logs.py            # last 40 chunks
  python3 tools/report_logs.py 120        # last 120 chunks
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE_LOG = os.path.join(ROOT, "logs", "bridge.log")
PLUGIN_LOGS = [os.path.join(ROOT, "logs", "plugin.log"), "/tmp/liveai_debug.log"]

BRK = re.compile(r"\[brk\] start=\s*([\d.]+).*?asr=\s*([\d.]+).*?tot=\s*([\d.]+)\s*(.*)$")
REQ = re.compile(r"\[req\] start=\s*([\d.]+).*?ok=(\w+)\s+err=(.*)$")
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})")

# Human wording for each drop reason the bridge emits.
REASONS = {
    "silence": "静音/环境音（VAD 判无语音）",
    "weak audio": "弱语音但没有可用台词（声音不够长，不出拟声）",
    "no valid speech": "有人声但没有有效字幕（句子被过滤光）",
    "empty transcription": "识别为空",
    "non-japanese hallucination": "Whisper 幻觉（输出里没有日文）",
    "video not found": "文件不存在",
}


def parse_bridge(path, limit):
    chunks = []
    if not os.path.isfile(path):
        return chunks
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    for line in lines[-limit * 6:]:
        m = BRK.search(line)
        if not m:
            continue
        start, asr, tot, rest = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4)
        # Format is "tot= X <error> ja=<text>"; the error may be empty, in which
        # case the string starts straight with "ja=" (the regex ate the space).
        ja = ""
        if rest.startswith("ja="):
            ja, reason = rest[3:], ""
        elif " ja=" in rest:
            rest, ja = rest.split(" ja=", 1)
            reason = rest.strip()
        else:
            reason = rest.strip()
        ts = ""
        t = TS.match(line)
        if t:
            ts = t.group(2)
        chunks.append({"ts": ts, "start": float(start), "asr": asr, "tot": tot,
                       "reason": reason, "ja": ja.strip()})
    return chunks


def parse_plugin_cues():
    out = []
    for path in PLUGIN_LOGS:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "CUE @" in line and " zh=" in line:
                    m = re.search(r"(\d{2}:\d{2}:\d{2}).*span=([\d.]+)-([\d.]+)\s+zh=(.*)$", line)
                    if m:
                        out.append((m.group(1), float(m.group(2)), float(m.group(3)), m.group(4).strip()))
                elif "RENDER @" in line:
                    pass
    return out


def latest_film_log():
    """Newest per-film readable log (logs/films/<影片名>.log)."""
    d = os.path.join(ROOT, "logs", "films")
    if not os.path.isdir(d):
        return None
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".log")]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def main():
    args = [a for a in sys.argv[1:] if a != "-f"]
    limit = int(args[0]) if args else 40

    film = latest_film_log()
    if film and "-f" in sys.argv:
        print("=== %s（最后 30 行） ===" % os.path.basename(film))
        with open(film, encoding="utf-8", errors="ignore") as f:
            for line in f.readlines()[-30:]:
                print(line.rstrip())
        return

    chunks = parse_bridge(BRIDGE_LOG, limit)
    if not chunks:
        print("还没有 %s —— bridge 需要先用带文件日志的版本重启过一遍。" % BRIDGE_LOG)
        return

    shown = [c for c in chunks if not c["reason"]]
    dropped = [c for c in chunks if c["reason"]]
    print("=== 概览（最近 %d 个识别块） ===" % len(chunks))
    print("出字幕: %d    丢弃: %d" % (len(shown), len(dropped)))
    counts = {}
    for c in dropped:
        counts[c["reason"]] = counts.get(c["reason"], 0) + 1
    for r, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("  %-28s %3d  %s" % (r, n, REASONS.get(r, "")))
    if chunks:
        arr = sorted(c["asr"] for c in chunks)
        print("ASR 耗时: 中位 %.2fs  最大 %.2fs" % (arr[len(arr) // 2], arr[-1]))

    print("\n=== 被丢弃的块（最近 20 个，含原始识别文本） ===")
    if not dropped:
        print("（无）")
    for c in dropped[-20:]:
        ja = c["ja"] if c["ja"] else "—"
        print("%s  @%7.1f  [%s]  听到: %s" % (c["ts"] or "--:--:--", c["start"],
                                              c["reason"], ja[:60]))

    print("\n=== 已出字幕（最近 15 个识别块） ===")
    for c in shown[-15:]:
        print("%s  @%7.1f  %.2fs  %s" % (c["ts"] or "--:--:--", c["start"], c["asr"],
                                         c["ja"][:50] if c["ja"] else ""))

    # Drops worth a second look: they produced Japanese text (so something was
    # said) or VAD was fairly confident there was speech.
    suspicious = []
    if os.path.isfile(os.path.join(ROOT, "logs", "drops.log")):
        with open(os.path.join(ROOT, "logs", "drops.log"), encoding="utf-8", errors="ignore") as f:
            for line in f:
                ja = ""
                m = re.search(r"ja=(.*)$", line)
                if m:
                    ja = m.group(1).strip()
                vmax = re.search(r"vmax=([\d.]+)", line)
                v = float(vmax.group(1)) if vmax else 0.0
                if (ja and ja != "-") or v >= 0.5:
                    suspicious.append(line.rstrip())
    if suspicious:
        print("\n=== 可疑丢弃（识别出日文，或 VAD 很确信有人声）===")
        for line in suspicious[-12:]:
            print(line)

    cues = parse_plugin_cues()
    if cues:
        print("\n=== 播放器实际渲染（最近 12 条） ===")
        for ts, a, b, zh in cues[-12:]:
            print("%s  %7.2f-%7.2f  %s" % (ts, a, b, zh[:50]))


if __name__ == "__main__":
    main()
