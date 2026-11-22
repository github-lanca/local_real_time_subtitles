#!/usr/bin/env python3
"""字幕缓存自检：累加、区间登记、断点续传、复用分支。

跑法：python3 tools/test_sidecar.py
不依赖 bridge 服务，也不碰真实影片。
"""
import importlib.util
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_bridge():
    # bridge imports vad_silero from the repo root.
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("bs", os.path.join(ROOT, "bridge_server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bs"] = mod
    spec.loader.exec_module(mod)
    return mod


def fake_video(tag):
    td = tempfile.mkdtemp(prefix="sidecar_%s_" % tag)
    path = os.path.join(td, "film.mp4")
    open(path, "wb").close()
    return path


def test_accumulate(m):
    print("=== 累加与去重 ===")
    video = fake_video("acc")
    sc = m._Sidecar(video)

    # 三个窗口；第 2 个与第 1 个在边界重复识别同一句；第 3 个是静音
    sc.add([{"start": 20.0, "end": 22.5, "zh": "早上好"},
            {"start": 23.0, "end": 25.0, "zh": "今天天气不错"}], 20, 4)
    sc.add([{"start": 23.0, "end": 25.1, "zh": "今天天气不错。"},
            {"start": 25.2, "end": 27.0, "zh": "要不要出去走走"}], 24, 4)
    sc.add([], 28, 4)          # 静音窗口：没有字幕，但也要登记已处理

    print("  cues  =", [(c["start"], c["end"], c["zh"]) for c in sc.cues])
    print("  ranges=", sc.ranges)
    assert len(sc.cues) == 3, "重复句应合并"
    assert sc.ranges == [[20.0, 32.0]], "相邻窗口应合并成一个区间"

    print("\n=== 文件产物 ===")
    print("  srt    =", os.path.basename(sc.path), os.path.isfile(sc.path))
    print("  ranges =", os.path.basename(sc.ranges_path), os.path.isfile(sc.ranges_path))
    print(open(sc.path, encoding="utf-8").read())
    print("  ranges 内容:", open(sc.ranges_path, encoding="utf-8").read().strip())
    return video


def test_lookup(m, video):
    print("\n=== 复用判定 ===")
    sc = m.get_sidecar(video)

    for start, dur, want_known, want_cues in [
            (20.0, 4.0, True, 1),    # 处理过，有字幕
            (28.0, 4.0, True, 0),    # 处理过，但当时没有对白
            (40.0, 4.0, False, 0),   # 没处理过
            (26.0, 4.0, True, 1),    # 跨窗口
    ]:
        hit, known = sc.lookup(start, dur)
        print("  lookup(%5.1f, %.0f) -> known=%-5s cues=%d"
              % (start, dur, known, len(hit)))
        assert known is want_known, "known 判定错误 @%.1f" % start
        if want_known:
            assert len(hit) >= want_cues

    print("\n=== process()：命中缓存不碰音频 ===")
    r = m.process(video, 20.0, 4.0, "")
    print("  有对白窗口: from_srt=%s cues=%s 耗时=%.3fs"
          % (r.get("from_srt"), [c["zh"] for c in r.get("cues") or []],
             r.get("_dbg", {}).get("total", -1)))
    assert r.get("from_srt") and r["cues"] and r["_dbg"]["total"] < 0.2

    r2 = m.process(video, 28.0, 4.0, "")
    print("  无对白窗口: from_srt=%s err=%s 耗时=%.3fs"
          % (r2.get("from_srt"), r2.get("error"), r2.get("_dbg", {}).get("total", -1)))
    assert r2.get("from_srt") and r2.get("error") == "silence"
    assert r2["_dbg"]["total"] < 0.2, "空白窗口也不该跑 ffmpeg"


def test_resume(m):
    print("\n=== 断点续传：只看中段、关掉、下次接着用 ===")
    video = fake_video("resume")

    # 第一遍：只看了 30:00 - 30:20
    sc = m._Sidecar(video)
    sc.add([{"start": 1800.5, "end": 1802.0, "zh": "这段我只看过一次"}], 1800, 4)
    sc.add([{"start": 1804.0, "end": 1806.0, "zh": "后面补上"}], 1804, 4)
    sc.add([], 1808, 4)
    print("  第一遍后: cues=%d ranges=%s" % (len(sc.cues), sc.ranges))

    # 关掉 -> 重开（新实例从磁盘读）
    sc2 = m._Sidecar(video)
    print("  重开后:   cues=%d ranges=%s" % (len(sc2.cues), sc2.ranges))
    assert len(sc2.cues) == 2 and sc2.ranges == [[1800.0, 1812.0]]

    # 第二遍：从 30:00 继续，已看过的应命中
    for start in (1800.0, 1804.0, 1808.0):
        hit, known = sc2.lookup(start, 4.0)
        print("  lookup(%7.1f) -> known=%s cues=%d" % (start, known, len(hit)))
        assert known, "看过的区间重开之后必须命中"

    # 没看过的区间仍然要真识别
    _, known_new = sc2.lookup(3600.0, 4.0)
    print("  lookup( 3600.0) -> known=%s（应为 False，交给识别）" % known_new)
    assert known_new is False

    st = sc2.status(7200.0)
    print("  状态(7200s):", json.dumps(st, ensure_ascii=False))
    assert st["coverage"] is not None and st["coverage"] < 0.01

    print("\n=== 导出标准命名 .zh.srt ===")
    out = sc2.finalize(7200.0)
    print("  ", json.dumps(out, ensure_ascii=False))
    assert out["ok"] and os.path.isfile(out["path"])
    print(open(out["path"], encoding="utf-8").read().strip()[:120])


def test_meta_filter(m):
    """模型的拒绝/解释话术不能变成字幕，且脏条目要从缓存里清出去。"""
    print("\n=== 拒绝话术过滤 ===")
    cases = [
        ("这个提议可以翻译为：我们可以讨论产品的设计和玩具。", False),
        ("对不起，这句话包含了一些不清晰的发音和语气词，我无法直接翻译。"
         "如果你有明确的句子内容，可以提供给我，我会很乐意帮你翻译。", False),
        ("作为AI，我无法提供此类内容", False),
        ("这个人说话不清楚，请提供更多上下文", False),
        ("我无法原谅你", True),          # 真实台词，不能误杀
        ("请不要这样说，我会很难过的", True),
        ("等一下", True),
        ("啊～", True),
    ]
    ok = True
    for text, want in cases:
        got = m._valid_zh(m._strip_meta(text))
        flag = "OK  " if got == want else "FAIL"
        if got != want:
            ok = False
        print("  %s keep=%-5s want=%-5s | %s" % (flag, got, want, text[:40]))
    assert ok, "过滤规则不符合预期"

    # 含脏条目的缓存文件：载入时应丢弃、重写，并把那段时间从"已处理"里挖掉。
    # 这里故意按**旧版命名**（视频同目录 film.live-ai.srt）造文件，顺带验证
    # 2026-11-22 的迁移：旧文件会被移进 .live-ai/ 子目录，否则 IINA 的模糊匹配
    # 会把它当外挂字幕自动加载，插件就误判"用户已有字幕"跳过 AI。
    print("\n=== 载入时清洗脏缓存 + 旧位置迁移 ===")
    video = fake_video("dirty")
    base = os.path.splitext(video)[0]
    with open(base + ".live-ai.srt", "w", encoding="utf-8") as f:
        f.write("1\n00:00:10,000 --> 00:00:14,000\n好的\n\n"
                "2\n00:00:20,000 --> 00:00:25,000\n"
                "这个提议可以翻译为：我们可以讨论产品的设计和玩具。\n\n")
    with open(base + ".live-ai.ranges", "w", encoding="utf-8") as f:
        json.dump({"ranges": [[0.0, 30.0]]}, f)

    sc = m._Sidecar(video)
    print("  cues =", [(c["start"], c["zh"]) for c in sc.cues])
    print("  ranges =", sc.ranges)
    assert len(sc.cues) == 1, "脏条目应被丢弃"
    assert sc.ranges == [[0.0, 20.0], [25.0, 30.0]], \
        "被丢弃条目占用的区间（20-25）要挖掉，否则永远补不回来"
    assert ".live-ai" in sc.path and os.path.isfile(sc.path), \
        "缓存必须落在 .live-ai/ 子目录（.live-ai/ 前缀的路径）"
    assert not os.path.exists(base + ".live-ai.srt"), \
        "旧位置的缓存必须被移走，否则 IINA 还会模糊匹配加载它"
    on_disk = open(sc.path, encoding="utf-8").read()
    assert "可以翻译为" not in on_disk, "磁盘上的文件也要清洗掉"
    print("  磁盘已重写，脏条目消失")
    _, known = sc.lookup(20.0, 4.0)
    print("  lookup(20,4) known =", known, "（应为 False，交给重新识别）")
    assert known is False


def main():
    m = load_bridge()
    test_meta_filter(m)
    video = test_accumulate(m)
    test_lookup(m, video)
    test_resume(m)
    print("\nOK")


if __name__ == "__main__":
    main()
