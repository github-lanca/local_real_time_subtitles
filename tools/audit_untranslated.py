#!/usr/bin/env python3
"""清理 logs/untranslated.log：把里面的原文重译一遍，能译出来的就移出文件。

用途：改过翻译规则（提示词 / 过滤 / 假名清洗）之后，用它验证"历史遗留的未译条目
现在能不能译"，并把已解决的从日志里清掉，剩下的就是真正要人工处理的。

用法：
  python3 tools/audit_untranslated.py           # 只报告，不改文件
  python3 tools/audit_untranslated.py --apply   # 移出已能翻译的条目

需要 llama-server(:18181) 在跑（不用启动 bridge 本体）。
"""
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "untranslated.log")
# 已解决的条目归档在这里：留着才能看出"哪次改规则修好了什么"。
RESOLVED = os.path.join(ROOT, "logs", "untranslated.resolved.log")

# 时间  T+时:分:秒  日语原文   [模型]: 模型原话
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(T\+\S+)\s+(.*?)"
                  r"(?:\s+\[模型\]:\s*(.*))?$")


def load_bridge():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("bs", os.path.join(ROOT, "bridge_server.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bs"] = mod
    spec.loader.exec_module(mod)
    return mod


def diagnose(m, ja, model_said):
    """没译出来时给一句人话解释，方便决定改哪条规则。"""
    if not model_said:
        return "模型没返回内容（超时/服务异常？）"
    raw = model_said.strip()
    if m._is_meta(raw):
        return "模型在解释自己（元话术）"
    cleaned = m._clean_kana(m._strip_meta(raw))
    runs = m.KANA_RUN.findall(cleaned)
    if runs:
        longest = max(len(r) for r in runs)
        ratio = sum(len(r) for r in runs) / max(len(cleaned), 1)
        if longest > 4:
            return "整句回吐日文（最长假名串 %d 字）—— 模型没翻译" % longest
        if ratio > 0.4:
            return "假名占比 %.0f%% 过高 —— 模型没翻译" % (ratio * 100)
    if m.EN_REFUSAL.search(cleaned):
        return "英文拒绝话术"
    return "其他（%r）" % cleaned[:30]


def main():
    apply = "--apply" in sys.argv
    if not os.path.isfile(LOG):
        print("没有 %s" % os.path.relpath(LOG, ROOT))
        return 0
    rows = [l.rstrip("\n") for l in open(LOG, encoding="utf-8") if l.strip()]
    if not rows:
        print("untranslated.log 是空的，没有待处理的条目")
        return 0

    m = load_bridge()
    keep, fixed, still = [], [], []
    for line in rows:
        mo = LINE.match(line)
        if not mo:
            keep.append(line)
            still.append(("?", line[:40], "格式无法解析"))
            continue
        when, vtime, ja, model_said = mo.groups()
        out = m.translate(ja)
        if out:
            fixed.append((vtime, ja, out))
        else:
            still.append((vtime, ja, diagnose(m, ja, model_said)))
            keep.append(line)

    print("\n=== 已能翻译（%d 条）===" % len(fixed))
    for vtime, ja, out in fixed:
        print("  %-14s %s\n                 -> %s" % (vtime, ja[:34], out[:40]))

    if still:
        print("\n=== 仍译不出来（%d 条）===" % len(still))
        for vtime, ja, why in still:
            print("  %-14s %-34s %s" % (vtime, ja[:32], why))
    else:
        print("\n=== 没有剩余条目 ===")

    if apply and fixed:
        import time as _t
        stamp = _t.strftime("%Y-%m-%d %H:%M:%S")
        with open(RESOLVED, "a", encoding="utf-8") as f:
            for vtime, ja, out in fixed:
                f.write("%s  %s  ->  %s   (resolved %s)\n" % (vtime, ja, out, stamp))
        with open(LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
        print("\n已移除 %d 条（归档到 untranslated.resolved.log），保留 %d 条"
              % (len(fixed), len(keep)))
    elif fixed:
        print("\n（加 --apply 才会写回文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
