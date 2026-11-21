#!/usr/bin/env python3
"""Replay the plugin's exact request pattern against the bridge.

Mimics live-ai-subs/main.js: sequential (one in-flight) requests of CHUNK
seconds, each carrying the tail of the previous Japanese transcription as
context. Prints the per-stage breakdown the bridge returns in `_dbg`, so a
"slow" run immediately shows whether the cost is ffmpeg, VAD, ASR or llm.

Usage: python3 tools/bench_bridge.py <video> <start> [count] [chunk] [nocontext]
"""
import json
import sys
import time
import urllib.request

BRIDGE = "http://127.0.0.1:18182/transcribe"


def main():
    video = sys.argv[1]
    start = float(sys.argv[2])
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    chunk = float(sys.argv[4]) if len(sys.argv) > 4 else 4.0
    use_ctx = not (len(sys.argv) > 5 and sys.argv[5] == "nocontext")

    ctx = ""
    t_all = time.time()
    rows = []
    for i in range(count):
        s = start + i * chunk
        body = json.dumps({"video": video, "start": s, "duration": chunk,
                           "context": ctx}).encode()
        req = urllib.request.Request(BRIDGE, data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
        wall = time.time() - t0
        dbg = d.get("_dbg", {})
        rows.append(dbg.get("asr", 0.0))
        print("[%2d] start=%7.1f wall=%5.2f asr=%5.2f tr=%5.2f(n=%d) vad=%4.2f ext=%4.2f "
              "ok=%s ja=%s" % (i, s, wall, dbg.get("asr", 0), dbg.get("tr", 0),
                               dbg.get("tr_n", 0), dbg.get("vad", 0), dbg.get("extract", 0),
                               d.get("ok"), (d.get("ja") or "")[:40]), flush=True)
        if d.get("ok") and d.get("ja") and use_ctx:
            ctx = d["ja"][-24:]
    print("\n--- summary ---")
    print("total wall %.1fs for %d chunks (%.2fs/chunk)" % (time.time() - t_all, count,
                                                            (time.time() - t_all) / count))
    if rows:
        sr = sorted(rows)
        print("asr  min=%.2f med=%.2f max=%.2f" % (sr[0], sr[len(sr) // 2], sr[-1]))


if __name__ == "__main__":
    main()
