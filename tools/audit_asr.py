#!/usr/bin/env python3
"""Audit what the live bridge actually recognized against faster-whisper.

Reads `[brk] ... ja=` lines from the bridge log (the real pipeline output for the
film being watched), then re-transcribes those exact windows with
faster-whisper CT2 (beam 5, the most accurate CPU reference) and prints both
side by side with a character-overlap score.

Usage: python3 tools/audit_asr.py <video> <bridge-log> [n]
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGLINE = re.compile(r"\[brk\] start=\s*([\d.]+).*?ja=(.*)$")


def parse_windows(path):
    seen, out = set(), []
    with open(path, errors="ignore") as f:
        for line in f:
            m = LOGLINE.search(line.strip())
            if not m:
                continue
            start = float(m.group(1))
            ja = m.group(2).strip()
            if not ja or start in seen:
                continue
            seen.add(start)
            out.append((start, ja))
    return out


def extract(video, start, dur, out_wav):
    subprocess.run(["/opt/homebrew/bin/ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{start:.3f}", "-i", video, "-t", f"{dur:.3f}",
                    "-vn", "-ac", "1", "-ar", "16000",
                    "-af", "loudnorm=I=-18:TP=-2:LRA=7",
                    "-c:a", "pcm_s16le", out_wav], check=True)


def overlap(a, b):
    """Rough character-level agreement (order-insensitive), 0..1."""
    sa, sb = set(re.sub(r"[\s、。,.!?！？…ー]", "", a)), set(re.sub(r"[\s、。,.!?！？…ー]", "", b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def main():
    video, logpath = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    windows = parse_windows(logpath)
    if not windows:
        print("no ja= lines found in", logpath)
        return
    # Spread the sample over the whole log instead of taking a contiguous block.
    step = max(1, len(windows) // n)
    sample = windows[::step][:n]

    from faster_whisper import WhisperModel
    model = WhisperModel(os.path.join(ROOT, "models", "faster-whisper-large-v3-turbo"),
                         device="cpu", compute_type="int8")

    scores = []
    for start, ja in sample:
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "c.wav")
            extract(video, start, 6.5, wav)
            segs, _ = model.transcribe(wav, language="ja", beam_size=5, vad_filter=False,
                                       condition_on_previous_text=False)
            ref = "".join(s.text for s in segs).strip()
        ov = overlap(ja, ref)
        scores.append(ov)
        print("@" + f"{start:.1f}  overlap={ov:.2f}")
        print("   mlx : " + ja)
        print("   ct2 : " + ref)
    print("\nmean character overlap vs faster-whisper: %.2f over %d windows"
          % (sum(scores) / len(scores), len(scores)))


if __name__ == "__main__":
    main()
