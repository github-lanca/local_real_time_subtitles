#!/usr/bin/env python3
"""Explain why one window got subtitles, a moan, or nothing.

Shows the VAD probability at several thresholds, the raw greedy transcription,
and which rule the current settings applied -- so a wrong-looking decision can be
traced to a threshold instead of guessed at.

Usage:
  python3 tools/probe.py <video> <start> [duration]
  python3 tools/probe.py /path/to/film.mp4 528
"""
import json
import sys
import urllib.request

BRIDGE = "http://127.0.0.1:18182/probe"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    video, start = sys.argv[1], float(sys.argv[2])
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    body = json.dumps({"video": video, "start": start, "duration": dur}).encode()
    req = urllib.request.Request(BRIDGE, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    if not d.get("ok"):
        print("失败:", d.get("error"))
        return 1

    print("窗口 %.1fs，抽取 %.2fs（含 %.1fs 前瞻）" % (d["start"], d["capture"],
                                                    d["capture"] - dur))
    print("\n--- Silero VAD（阈值 → 判定的人声时长）---")
    print("最大概率 %.3f" % d["vad_max"])
    for t in d["thresholds"]:
        print("  %.2f → %5.2fs  %s" % (t["thr"], t["speech"], t["spans"]))

    a = d["asr"]
    print("\n--- Whisper 贪心识别（temperature=0）---")
    print("文本:", a["text"] or "(空)")
    print("压缩比 %.2f | 退化=%s | 含日文=%s | 拟声=%s"
          % (a["compression_ratio"], a["degenerate"], a["has_japanese"],
             a["vocal"] or "—"))

    s = d["settings"]
    print("\n--- 当前判定 ---")
    print("  MIN_SPEECH_SEC=%.2f  VAD_THRESHOLD=%.2f  WEAK_MIN=%.2f  WEAK=%.2f  MOAN=%.2f"
          % (s["MIN_SPEECH_SEC"], s["VAD_THRESHOLD"], s["VAD_WEAK_MIN_PROB"],
             s["VAD_WEAK_PROB"], s["MOAN_MIN_SEC"]))
    print("  结论:", d["verdict"])

    # What-if: would a different weak threshold change the outcome?
    print("\n--- 如果调阈值 ---")
    for cand in (0.30, 0.24, 0.18, 0.12, 0.08):
        state = "会进入弱语音（跑 ASR，只收拟声/日文）" if d["vad_max"] >= cand \
            else "仍判静音（不跑 ASR）"
        mark = "← 当前" if abs(cand - s["VAD_WEAK_MIN_PROB"]) < 1e-9 else ""
        print("  WEAK_MIN=%.2f  %s %s" % (cand, state, mark))
    return 0


if __name__ == "__main__":
    sys.exit(main())
