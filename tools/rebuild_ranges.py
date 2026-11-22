#!/usr/bin/env python3
"""按字幕文件的实际条目重建 `.live-ai.ranges`。

什么时候用：
  - 换过片源、或区间文件与字幕对不上（比如字幕里某段有内容，区间却标了"没处理过"）
  - 从旧版本迁移过来，区间是"0 到结尾都处理过"这种粗略推断
  - 手动删过字幕条目

原理：`.ranges` 记的是"处理过的时间范围"，本来应该由处理过程写入。当它丢了或错了，
就用字幕条目本身反推 —— **有字幕的地方一定处理过**，中间的空隙按 `--gap` 合并，
避免把每个标点都变成待重跑的窗口。

用法：
  python3 tools/rebuild_ranges.py <video> [--gap 2.0] [--dry]
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_bridge():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("bs", os.path.join(ROOT, "bridge_server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bs"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    video = os.path.abspath(args[0])
    gap = 2.0
    if "--gap" in sys.argv:
        gap = float(sys.argv[sys.argv.index("--gap") + 1])
    dry = "--dry" in sys.argv

    if not os.path.isfile(video):
        print("找不到影片:", video)
        return 2

    m = load_bridge()
    sc = m._Sidecar(video)
    if not sc.cues:
        print("字幕文件里没有条目，无从推导")
        return 1

    spans = sorted([[c["start"], c["end"]] for c in sc.cues])
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    new = [[round(a, 2), round(b, 2)] for a, b in merged]

    old = sc.ranges
    old_cov = sum(b - a for a, b in old)
    new_cov = sum(b - a for a, b in new)
    print("字幕 %d 条" % len(sc.cues))
    print("区间: %d 段 / 覆盖 %.1fs  ->  %d 段 / 覆盖 %.1fs（gap≤%.1fs 合并）"
          % (len(old), old_cov, len(new), new_cov, gap))

    # 旧区间声称处理过、新区间却没有的地方 —— 这些就是之前被漏掉的内容
    holes = []
    for a, b in old:
        cur = a
        for x, y in new:
            if y <= cur or x >= b:
                continue
            if x - cur > 5.0:
                holes.append([round(cur, 1), round(x, 1)])
            cur = max(cur, y)
        if b - cur > 5.0:
            holes.append([round(cur, 1), round(b, 1)])
    if holes:
        print("\n旧区间标了'处理过'、但字幕里没有内容的片段（>5s）：")
        for h in holes[:12]:
            print("  %8.1f - %8.1f  (%.0fs)" % (h[0], h[1], h[1] - h[0]))
        print("  …这些段落下次播放会重新识别")
    else:
        print("\n没有发现明显空洞")

    if dry:
        print("\n(--dry：未写入)")
        return 0
    sc.ranges = new
    sc._write()
    print("\n已写入", os.path.basename(sc.ranges_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
