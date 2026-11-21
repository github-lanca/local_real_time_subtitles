#!/usr/bin/env python3
"""Silero VAD (v6 ONNX) gate for the subtitle pipeline.

Why this exists: ffmpeg `silencedetect` only measures energy, so music, ambience
and BGM intros pass as "speech" and get sent to Whisper. Whisper then runs the
whole temperature ladder on non-speech audio and emits stock hallucinations
("ご視聴ありがとうございました") while burning 8-14s per fake chunk. That single
behaviour caused every "ASR randomly takes 10s+" report.

Silero is a small recurrent net trained on speech/non-speech: BGM scores < 0.1,
real dialogue > 0.9. Running it on CPU costs ~50ms for 6.5s of audio, and it
lets us skip Whisper entirely on music, plus use its intervals as true subtitle
boundaries instead of energy-derived ones.

Implemented standalone on purpose: `import faster_whisper` links a second
libomp into the process and aborts with OMP Error #15 once MLX has loaded.
"""

import os
import wave

import numpy as np
import onnxruntime as ort

ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(ROOT, "models", "vad", "silero_vad_v6.onnx")

SR = 16000
WINDOW = 512          # Silero v5/v6 hop size at 16 kHz
CONTEXT = 64          # samples of look-back context the model expects

_session = None


def _session_get():
    global _session
    if _session is None:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4
        _session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"],
                                        sess_options=opts)
    return _session


def read_wav_float32(path):
    """Read a 16 kHz mono PCM16 wav into float32 [-1, 1]."""
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected mono pcm_s16le wav")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def speech_probs(audio):
    """Per-512-sample speech probability, one value per 32 ms frame."""
    sess = _session_get()
    n = (len(audio) // WINDOW) * WINDOW
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = audio[:n].reshape(-1, WINDOW)

    h = np.zeros((1, 1, 128), dtype="float32")
    c = np.zeros((1, 1, 128), dtype="float32")
    # 64-sample context: each frame is prefixed with the tail of the previous
    # frame (first frame sees zeros), matching the reference implementation.
    ctx = frames[:, -CONTEXT:]
    ctx[-1] = 0.0
    ctx = np.roll(ctx, 1, 0)
    batch = np.concatenate([ctx, frames], axis=1)

    out, _, _ = sess.run(None, {"input": batch, "h": h, "c": c})
    return np.asarray(out).reshape(-1)


def speech_intervals(audio, threshold=0.5, min_silence_ms=300, pad_ms=80,
                     min_speech_ms=200):
    """[(start_s, end_s), ...] of actual speech, in seconds."""
    probs = speech_probs(audio)
    if probs.size == 0:
        return []
    frame = WINDOW / SR
    neg = max(threshold - 0.15, 0.01)
    min_sil = min_silence_ms / 1000.0

    spans = []
    start = None
    silent_run = 0.0
    for i, p in enumerate(probs):
        t = i * frame
        if p >= threshold:
            if start is None:
                start = t
            silent_run = 0.0
        elif start is not None:
            silent_run += frame
            if p < neg and silent_run >= min_sil:
                spans.append((start, t - silent_run + frame))
                start = None
                silent_run = 0.0
    if start is not None:
        spans.append((start, len(audio) / SR))

    pad = pad_ms / 1000.0
    total = len(audio) / SR
    out = []
    for a, b in spans:
        a = max(0.0, a - pad)
        b = min(total, b + pad)
        if b - a >= min_speech_ms / 1000.0:
            out.append((a, b))
    return out


def speech_seconds(intervals, limit=None):
    """Total speech seconds, optionally clipped to [0, limit]."""
    tot = 0.0
    for a, b in intervals:
        if limit is not None:
            b = min(b, limit)
        if b > a:
            tot += b - a
    return tot


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        audio = read_wav_float32(path)
        p = speech_probs(audio)
        iv = speech_intervals(audio)
        print("%s dur=%.2fs frames=%d maxprob=%.3f mean=%.3f speech=%.2fs segs=%s"
              % (os.path.basename(path), len(audio) / SR, p.size,
                 float(p.max()) if p.size else -1, float(p.mean()) if p.size else -1,
                 speech_seconds(iv),
                 [(round(a, 2), round(b, 2)) for a, b in iv]))
