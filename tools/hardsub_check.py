#!/usr/bin/env python3
"""检查影片画面里是否已经烧了字幕（硬字幕）。

只取底部字幕带做 macOS Vision OCR，判断标准：多个采样点都能读出中文
（≥4 个汉字），且这些字来自画面而不是纯噪点。

独立成进程的原因：Vision 会加载 OCR 模型并起线程，和 ffmpeg 子进程混在同一
进程里在内存吃紧的机器上会被系统杀掉（实测 SIGKILL）；拆成短命令进程最稳。

用法：
    python3 tools/hardsub_check.py <video> <start_sec> [samples]
输出（stdout，单行 JSON）：
    {"hardsub": true, "samples": [{"t": 123.0, "cjk": 14, "text": "..."}]}
"""
import json
import os
import re
import subprocess
import sys
import tempfile

FFMPEG = "/opt/homebrew/bin/ffmpeg"
CJK = re.compile(r"[\u4e00-\u9fff]")
# 底部字幕带：画面下方 18%（0.82 ~ 1.00），只裁这一条，不整帧识别
CROP_Y = 0.82
CROP_H = 0.18
BAND_WIDTH = 1280
MIN_CJK = 4          # 一条字幕至少这么多个汉字
NEED_HITS = 2        # 3 个采样点里至少 2 个命中


def ocr_image(path):
    import Quartz
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return ""
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        return ""
    out = []

    def handler(req, err):
        for obs in (req.results() or []):
            cands = obs.topCandidates_(1)
            if cands and len(cands):
                out.append(str(cands[0].string()))

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    req.setRecognitionLevel_(0)          # 0 = accurate（1=fast 会漏字）
    req.setUsesLanguageCorrection_(False)
    req.setRecognitionLanguages_(["zh-Hans", "ja-JP", "en-US"])
    Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None) \
        .performRequests_error_([req], None)
    return " ".join(out)


def band_png(video, t):
    fd, out = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t, "-i", video,
                    "-frames:v", "1",
                    "-vf", "crop=iw:ih*%.3f:0:ih*%.3f,scale=%d:-1"
                           % (CROP_H, CROP_Y, BAND_WIDTH),
                    out], capture_output=True, timeout=30)
    return out


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    video, start = sys.argv[1], float(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    # 采样点分散开，避免同一句字幕被重复采样
    offsets = [3.0, 9.0, 15.0][:n]
    samples, hits = [], 0
    for off in offsets:
        t = start + off
        tmp = band_png(video, t)
        try:
            text = ocr_image(tmp)
        finally:
            os.unlink(tmp)
        cjk = len(CJK.findall(text))
        got = cjk >= MIN_CJK
        hits += 1 if got else 0
        samples.append({"t": round(t, 1), "cjk": cjk, "text": text.strip()[:60]})

    print(json.dumps({"hardsub": hits >= NEED_HITS, "samples": samples},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
