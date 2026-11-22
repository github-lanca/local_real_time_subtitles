#!/usr/bin/env python3
"""字幕文件质量审计：覆盖率、大段缺失、可疑漏识别、译文质量。

用法：
  python3 tools/audit_quality.py <video> [--gap 10]

只读，不改任何文件。
"""
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
CJK = re.compile(r"[\u4e00-\u9fff]")
META = re.compile(r"翻译|译文|无法|不清晰|上下文|语言模型|作为\s*AI")
INTERJ = re.compile(r"^[啊呀嗯哈呜哦诶哎呀咿嘿呼唔呃哎唉～~…\s!！?？]+$")


def srt_sec(t):
    m = re.match(r"(\d+):(\d\d):(\d\d)[,.](\d+)", t.strip())
    if not m:
        return None
    h, mi, se, f = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(f.ljust(3, "0")[:3]) / 1000


def load_bridge():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("bs", os.path.join(ROOT, "bridge_server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bs"] = mod
    spec.loader.exec_module(mod)
    return mod


def read_srt(path):
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for blk in f.read().replace("\r\n", "\n").split("\n\n"):
            lines = [l for l in blk.split("\n") if l.strip()]
            if len(lines) < 3 or "-->" not in lines[1]:
                continue
            a, b = lines[1].split("-->", 1)
            s, e = srt_sec(a), srt_sec(b)
            text = " ".join(l.strip() for l in lines[2:]).strip()
            if s is None or e is None or not text:
                continue
            out.append({"start": s, "end": e, "zh": text})
    return out


def merge(iv, gap=0.5):
    out = []
    for a, b in sorted(iv):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def fmt(t):
    return "%d:%02d:%05.2f" % (int(t // 3600), int((t % 3600) // 60), t % 60)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    video = os.path.abspath(args[0])
    gap_limit = 10.0
    if "--gap" in sys.argv:
        gap_limit = float(sys.argv[sys.argv.index("--gap") + 1])

    base = os.path.splitext(video)[0]
    srt_path = base + ".live-ai.srt"
    rng_path = base + ".live-ai.ranges"
    if not os.path.isfile(srt_path):
        print("找不到", srt_path)
        return 1

    dur = 0.0
    try:
        import subprocess
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", video], capture_output=True, text=True)
        dur = float(out.stdout.strip())
    except Exception:
        pass

    cues = read_srt(srt_path)
    ranges = []
    if os.path.isfile(rng_path):
        with open(rng_path, encoding="utf-8") as f:
            ranges = [[float(a), float(b)] for a, b in json.load(f).get("ranges", [])]

    print("=" * 62)
    print("字幕 %d 条 | 片长 %s | 字幕覆盖 %.1f 分钟"
          % (len(cues), fmt(dur) if dur else "?", sum(c["end"] - c["start"] for c in cues) / 60))
    if ranges:
        cov = sum(b - a for a, b in ranges)
        print("已处理 %d 段 / %.1f 分钟（%.0f%% 片长）"
              % (len(ranges), cov / 60, cov / max(dur, 1) * 100))
    print("=" * 62)

    # ---- 1. 大段无字幕 ----
    print("\n【1】时长 >%.0f 秒的无字幕区间" % gap_limit)
    spans = merge([[c["start"], c["end"]] for c in cues])
    holes = []
    prev = 0.0
    for a, b in spans:
        if a - prev > gap_limit:
            holes.append((prev, a))
        prev = max(prev, b)
    if dur and dur - prev > gap_limit:
        holes.append((prev, dur))

    in_range = 0
    for a, b in holes:
        overlap = sum(min(b, y) - max(a, x) for x, y in ranges if y > a and x < b)
        tag = "已处理" if overlap > (b - a) * 0.5 else "未观看"
        if tag == "已处理":
            in_range += 1
        print("  %10s - %10s  %5.0fs  %s" % (fmt(a), fmt(b), b - a, tag))
    if not holes:
        print("  无")
    print("  → 其中「已处理却没有字幕」的有 %d 段（最值得查）" % in_range)

    # ---- 2. 译文质量 ----
    print("\n【2】疑似有问题的条目")
    bad_kana = [c for c in cues if KANA.search(c["zh"])]
    no_cjk = [c for c in cues if not CJK.search(c["zh"]) and not INTERJ.match(c["zh"])]
    long_zh = [c for c in cues if len(c["zh"]) > 40]
    meta = [c for c in cues if META.search(c["zh"])]
    short = [c for c in cues if c["end"] - c["start"] < 0.35]

    def show(name, items, limit=5):
        print("\n  %s：%d 条" % (name, len(items)))
        for c in items[:limit]:
            print("     %s  %.2fs  %s" % (fmt(c["start"]), c["end"] - c["start"], c["zh"][:46]))

    show("含日文假名", bad_kana)
    show("无中文（非拟声/非英文）", no_cjk)
    show("超长（>40 字，可能是解释而非台词）", long_zh)
    show("含元话术关键词", meta)
    show("过短（<0.35s，会闪）", short)

    # ---- 3. 重复与重叠 ----
    print("\n【3】重复 / 时间重叠")
    dup = 0
    for i in range(1, len(cues)):
        a, b = cues[i - 1], cues[i]
        if a["zh"] == b["zh"] and abs(a["start"] - b["start"]) < 2.0:
            dup += 1
            if dup <= 4:
                print("  重复 %s  &  %s  %s" % (fmt(a["start"]), fmt(b["start"]), a["zh"][:30]))
    ov = 0
    for i in range(1, len(cues)):
        a, b = cues[i - 1], cues[i]
        overlap = a["end"] - b["start"]
        # 0.5s 的尾部补偿（CUE_TAIL_PAD）不算问题：显示时取第一条匹配，不会重叠
        if overlap > 0.6 and a["zh"] != b["zh"]:
            ov += 1
            if ov <= 4:
                print("  重叠 %.2fs  %s | %s" % (overlap, a["zh"][:22], b["zh"][:22]))
    print("  重复 %d 处，非同一句重叠 %d 处" % (dup, ov))

    # ---- 4. 抽样 ----
    print("\n【4】随机抽样 10 条（对照原文自行判断）")
    step = max(1, len(cues) // 10)
    for c in cues[::step][:10]:
        print("  %s  %s" % (fmt(c["start"]), c["zh"][:50]))

    # ---- 5. 区间内无字幕的密集区 ----
    if ranges:
        print("\n【5】已处理区间里，连续 20 秒以上没有字幕的地方")
        merged_r = merge(ranges, 0.1)
        cnt = 0
        for a, b in merged_r:
            seg = [c for c in cues if c["end"] > a and c["start"] < b]
            seg_spans = merge([[max(c["start"], a), min(c["end"], b)] for c in seg])
            cur = a
            for x, y in seg_spans:
                if x - cur > 20:
                    print("  %10s - %10s  %5.0fs" % (fmt(cur), fmt(x), x - cur))
                    cnt += 1
                cur = max(cur, y)
            if b - cur > 20:
                print("  %10s - %10s  %5.0fs" % (fmt(cur), fmt(b), b - cur))
                cnt += 1
        if not cnt:
            print("  无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
