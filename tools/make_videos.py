"""Generate 30-second test videos with ground truth for the integrity analyser.

Every frame carries a *reference barcode* in its top strip: a start marker, a
16-bit source-frame counter and a parity bit. This is the "reference evidence"
the brief asks for: it lets the analyser prove (not guess) that a source frame
was dropped or duplicated, independently of container timestamps.

Usage:  python tools/make_videos.py [out_dir]
"""
import json
import os
import random
import sys
from fractions import Fraction

import av
import numpy as np

W, H = 640, 360
BAR_BITS = 16
BAR_H = 24
BLOCK_W = W // (BAR_BITS + 4)  # 2 marker bits + 16 counter bits + parity + spare


def barcode_bits(counter):
    bits = [1, 0]  # start marker
    bits += [(counter >> (BAR_BITS - 1 - i)) & 1 for i in range(BAR_BITS)]
    bits.append(sum(bits[2:]) % 2)  # even parity over counter bits
    return bits


def render_frame(counter, t):
    """Moving content + barcode. `t` drives motion so frozen frames are visible."""
    y, x = np.mgrid[0:H, 0:W]
    r = ((x + t * 120) % W) / W * 255
    g = ((y + t * 60) % H) / H * 255
    b = np.full((H, W), 90.0)
    img = np.stack([r, g, b], axis=-1)
    # sensor-like grain keeps the file a realistic size (~4-6 MB for 30 s)
    rng = np.random.default_rng(counter)
    img = np.clip(img + rng.normal(0, 6, (H, W, 1)), 0, 255).astype(np.uint8)
    # moving box
    bx = int((t * 200) % (W - 80))
    by = int(H / 2 + np.sin(t * 2) * 100) - 40
    img[by:by + 80, bx:bx + 80] = (255, 255, 255)
    # barcode strip
    img[:BAR_H] = 0
    for i, bit in enumerate(barcode_bits(counter)):
        if bit:
            img[2:BAR_H - 2, i * BLOCK_W + 3:(i + 1) * BLOCK_W - 3] = 255
    return img


def encode(path, frames, fps=30, faststart=True):
    """frames: list of (source_counter, pts_in_ticks_of_1/fps_or_timebase, motion_time)."""
    opts = {"movflags": "faststart"} if faststart else {}
    with av.open(path, "w", options=opts) as out:
        s = out.add_stream("libx264", rate=fps)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        s.time_base = Fraction(1, fps * 100)  # fine time base so we can place frames at arbitrary times
        s.options = {"preset": "veryfast", "crf": "23", "g": str(fps)}
        for counter, pts_sec, mt in frames:
            vf = av.VideoFrame.from_ndarray(render_frame(counter, mt), format="rgb24")
            vf.pts = int(round(pts_sec / s.time_base))
            vf.time_base = s.time_base
            for p in s.encode(vf):
                out.mux(p)
        for p in s.encode():
            out.mux(p)


def make_all(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    truth = {}

    # 1. Clean 30 FPS, 900 frames
    fr = [(i, i / 30, i / 30) for i in range(900)]
    encode(os.path.join(out_dir, "clean_30fps.mp4"), fr)
    truth["clean_30fps.mp4"] = {"expected": "no anomalies"}

    # 2. Capture drops: frames missing at source. Timestamps keep real capture time
    #    (gap appears) and the counter jumps.
    drops = set(range(300, 305)) | {600} | set(range(750, 760))
    fr = [(i, i / 30, i / 30) for i in range(900) if i not in drops]
    encode(os.path.join(out_dir, "dropped_frames.mp4"), fr)
    truth["dropped_frames.mp4"] = {"missing_source_frames": sorted(drops),
                                   "at_seconds": [10.0, 20.0, 25.0]}

    # 3. Duplicated frames: the encoder received the same picture again with a
    #    fresh timestamp (frozen capture). Timestamps are regular; counter repeats.
    fr = []
    for i in range(900):
        src = i
        if 450 <= i < 456:
            src = 449  # freeze for 6 frames at 15s
        if i == 810:
            src = 809
        fr.append((src, i / 30, src / 30))
    encode(os.path.join(out_dir, "duplicated_frames.mp4"), fr)
    truth["duplicated_frames.mp4"] = {"duplicated_slots": list(range(450, 456)) + [810]}

    # 4. 24 FPS source: converting to 30 FPS must duplicate 1 of every 4 frames.
    #    This is a *conversion* effect, not an anomaly.
    fr = [(i, i / 24, i / 24) for i in range(720)]
    encode(os.path.join(out_dir, "source_24fps.mp4"), fr, fps=24)
    truth["source_24fps.mp4"] = {"expected": "no source anomalies; ~180 conversion duplicates at 30 FPS"}

    # 5. Timestamp jitter / VFR with one 0.5 s capture stall (no frames lost by counter:
    #    camera simply stopped delivering, e.g. thermal throttling).
    fr = []
    t = 0.0
    rnd = random.Random(7)
    for i in range(880):
        fr.append((i, t, t))
        t += 1 / 30 + rnd.uniform(-0.004, 0.004)
        if i == 400:
            t += 0.5
    encode(os.path.join(out_dir, "capture_stall_vfr.mp4"), fr)
    truth["capture_stall_vfr.mp4"] = {"stall_after_frame": 400, "stall_seconds": 0.5}

    # 6-8. Damaged files derived from the clean one
    clean = open(os.path.join(out_dir, "clean_30fps.mp4"), "rb").read()
    n = len(clean)
    with open(os.path.join(out_dir, "truncated.mp4"), "wb") as f:
        f.write(clean[: int(n * 0.55)])
    truth["truncated.mp4"] = {"kept_bytes_fraction": 0.55}

    altered = bytearray(clean)
    rnd = random.Random(42)
    regions = [int(n * 0.30), int(n * 0.62)]
    for start in regions:
        for k in range(start, start + 4000, 7):
            altered[k] ^= rnd.randrange(1, 256)
    with open(os.path.join(out_dir, "altered.mp4"), "wb") as f:
        f.write(altered)
    truth["altered.mp4"] = {"bit_flipped_regions_at_byte_fraction": [0.30, 0.62]}

    with open(os.path.join(out_dir, "invalid.mp4"), "wb") as f:
        f.write(os.urandom(512 * 1024))
    truth["invalid.mp4"] = {"expected": "not a video"}

    with open(os.path.join(out_dir, "ground_truth.json"), "w") as f:
        json.dump(truth, f, indent=2)
    for name in sorted(os.listdir(out_dir)):
        print(f"{name:28s} {os.path.getsize(os.path.join(out_dir, name)):>10,d} bytes")


if __name__ == "__main__":
    make_all(sys.argv[1] if len(sys.argv) > 1 else "samples")
