"""Video validation, 30 FPS frame extraction and anomaly detection.

Evidence sources, from strongest to weakest:
  1. Reference barcode (frame counter burned into the picture by the capture app)
     -> proves which *source* frames are missing / duplicated / out of order.
  2. Container presentation timestamps (PTS)
     -> gaps / repeats / non-monotonic times show capture or muxing timing faults.
  3. Picture content (frame-to-frame difference, flat frames)
     -> frozen or blank pictures; ambiguous on its own (a static scene also looks frozen).
  4. Decoder / demuxer errors and declared-vs-decoded frame counts
     -> file damage or encoding faults.

Transfer integrity is decided *before* this module runs (SHA-256 of the
assembled upload == SHA-256 computed by the client from the original file), so
any anomaly found here existed in the file before it was uploaded.
"""
import hashlib
import json
import math
import os
import time

import av
import numpy as np

TARGET_FPS = 30
BAR_BITS = 16
BAR_BLOCKS = BAR_BITS + 4
THUMB = (36, 64)


# --------------------------------------------------------------------------- helpers
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sniff_container(path):
    with open(path, "rb") as f:
        head = f.read(64)
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "iso-bmff (mp4/mov)"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "matroska/webm"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    return None


def read_barcode(gray):
    """Decode the reference counter from the top strip; None if absent/unreadable."""
    h, w = gray.shape
    bw = w // BAR_BLOCKS
    row = gray[12 if h > 24 else h // 2]
    bits = []
    for i in range(BAR_BITS + 3):
        seg = row[i * bw + bw // 3:(i + 1) * bw - bw // 3]
        if seg.size == 0:
            return None
        bits.append(1 if seg.mean() > 128 else 0)
    if bits[0] != 1 or bits[1] != 0:
        return None
    data = bits[2:2 + BAR_BITS]
    if sum(data) % 2 != bits[-1]:
        return None
    v = 0
    for b in data:
        v = (v << 1) | b
    return v


def thumb(gray):
    h, w = gray.shape
    th, tw = THUMB
    g = gray[: h - h % th, : w - w % tw].astype(np.float32)
    return g.reshape(th, g.shape[0] // th, tw, g.shape[1] // tw).mean(axis=(1, 3))


def runs(indices):
    """[3,4,5,9] -> [(3,5),(9,9)]"""
    out = []
    for i in sorted(indices):
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [tuple(r) for r in out]


# --------------------------------------------------------------------------- main
def analyze(path, transfer=None, thumbs_dir=None):
    t0 = time.time()
    report = {
        "file": os.path.basename(path),
        "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "sha256": sha256_file(path) if os.path.exists(path) else None,
        "transfer": transfer or {},
        "validation": {"container_sniff": None, "opened": False, "errors": [], "warnings": []},
        "stream": {},
        "decode": {},
        "frames_30fps": {},
        "anomalies": [],
        "verdict": None,
        "limitations": LIMITATIONS,
    }
    v = report["validation"]

    # ---- 1. readiness gate: transfer must be verified first
    if transfer is not None and transfer.get("status") != "verified":
        report["verdict"] = "NOT_PROCESSED_TRANSFER_UNVERIFIED"
        return report

    # ---- 2. validate & open
    v["container_sniff"] = sniff_container(path)
    if v["container_sniff"] is None:
        v["errors"].append("File signature is not a known video container (no 'ftyp'/EBML/RIFF header).")
    try:
        container = av.open(path, options={"err_detect": "careful"})
    except Exception as e:  # noqa: BLE001
        v["errors"].append(f"Demuxer could not open file: {type(e).__name__}: {e}")
        report["verdict"] = "INVALID_VIDEO"
        report["anomalies"].append(anom("file", "unparseable_container", "critical", None, None,
                                        "Container could not be parsed; no frames can be extracted.",
                                        ["demuxer error"], "file damage / not a video"))
        return finish(report, t0)
    v["opened"] = True
    vs = next((s for s in container.streams if s.type == "video"), None)
    if vs is None:
        v["errors"].append("No video stream found.")
        report["verdict"] = "INVALID_VIDEO"
        return finish(report, t0)

    tb = vs.time_base
    nominal = float(vs.average_rate or vs.guessed_rate or 0) or None
    st = report["stream"] = {
        "codec": vs.codec_context.name,
        "width": vs.codec_context.width,
        "height": vs.codec_context.height,
        "pix_fmt": vs.codec_context.pix_fmt,
        "time_base": str(tb),
        "nominal_fps": round(nominal, 3) if nominal else None,
        "declared_frames": vs.frames or None,
        "declared_duration_s": round(float(vs.duration * tb), 3) if vs.duration else
        (round(container.duration / 1e6, 3) if container.duration else None),
        "container_format": container.format.name,
        "bit_rate": container.bit_rate,
    }

    # ---- 3. demux + decode frame-by-frame, keeping going after errors
    frames = []          # dicts: idx, pts, t, key, counter, mad, mean, std
    decode_errors = []
    corrupt_packets = 0
    packets = 0
    prev_th = None
    vs.thread_type = "NONE"  # single-threaded so decoder log lines map to the packet being decoded
    av.logging.set_level(av.logging.VERBOSE)
    with av.logging.Capture(local=False) as logs:
        try:
            for pkt in container.demux(vs):
                if pkt.size == 0:
                    continue
                packets += 1
                if pkt.is_corrupt:
                    corrupt_packets += 1
                t = float(pkt.pts * tb) if pkt.pts is not None else None
                mark = len(logs)
                try:
                    decoded = pkt.decode()
                except Exception as e:  # noqa: BLE001
                    decode_errors.append({"packet": packets - 1, "time_s": rnd(t), "error": str(e)[:160]})
                    continue
                errs = [m.strip() for lvl, _n, m in logs[mark:] if lvl <= 24]  # ERROR/WARNING
                if errs:
                    decode_errors.append({"packet": packets - 1, "time_s": rnd(t), "error": "; ".join(errs)[:200],
                                          "concealed": True})
                for fr in decoded:
                    add_frame(frames, fr, tb, prev_th)
                    prev_th = frames[-1].pop("_th")
        except Exception as e:  # noqa: BLE001 - demux error mid-file (e.g. truncation)
            decode_errors.append({"packet": packets, "time_s": None, "error": f"demux stopped: {str(e)[:160]}"})
        try:
            for fr in vs.codec_context.decode(None):
                add_frame(frames, fr, tb, prev_th)
                prev_th = frames[-1].pop("_th")
        except Exception:  # noqa: BLE001
            pass
        demux_msgs = [m.strip() for lvl, n, m in logs if lvl <= 16 and n and "h264" not in n]
    av.logging.set_level(None)
    container.close()
    if demux_msgs:
        v["warnings"].extend(sorted(set(demux_msgs))[:10])

    # PTS order (B-frames are already returned in presentation order, but be safe)
    frames.sort(key=lambda f: (f["t"] if f["t"] is not None else 1e18))
    for i, f in enumerate(frames):
        f["idx"] = i

    report["decode"] = {
        "packets_read": packets,
        "corrupt_packets_flagged": corrupt_packets,
        "frames_decoded": len(frames),
        "decode_errors": len(decode_errors),
        "decode_error_samples": decode_errors[:20],
        "first_pts_s": rnd(frames[0]["t"]) if frames else None,
        "last_pts_s": rnd(frames[-1]["t"]) if frames else None,
    }
    if not frames:
        v["errors"].append("Zero frames could be decoded.")
        report["verdict"] = "INVALID_VIDEO"
        report["anomalies"].append(anom("file", "undecodable", "critical", None, None,
                                        "No decodable frames.", ["decoder"], "file damage / encoding"))
        return finish(report, t0)

    A = report["anomalies"]

    # ---- 4. file / encoding level
    if decode_errors:
        times = [d["time_s"] for d in decode_errors if d["time_s"] is not None]
        for d in decode_errors[:50]:
            A.append(anom("file", "decode_error", "high", d["time_s"], d["time_s"],
                          f"Packet {d['packet']}: {'decoded with error concealment' if d.get('concealed') else 'failed'}"
                          f" — {d['error']}",
                          ["decoder error log" if d.get("concealed") else "decoder exception"],
                          "bitstream damage present in the file before upload (transfer verified by SHA-256)"))
    if corrupt_packets:
        A.append(anom("file", "corrupt_packets", "high", None, None,
                      f"{corrupt_packets} packet(s) flagged corrupt by demuxer.", ["demuxer flags"],
                      "file damage"))
    declared = st["declared_frames"]
    if declared and abs(declared - len(frames)) > 0:
        sev = "high" if len(frames) < declared * 0.98 else "low"
        A.append(anom("file", "frame_count_mismatch", sev, rnd(frames[-1]["t"]), st["declared_duration_s"],
                      f"Container declares {declared} frames but {len(frames)} decoded "
                      f"({declared - len(frames)} missing).",
                      ["container index (stts/stsz)", "decoder output"],
                      "truncated / damaged file" if sev == "high" else "encoder delay / edit list"))
    dur = st["declared_duration_s"]
    if dur and frames[-1]["t"] is not None and frames[-1]["t"] < dur - 1.0:
        A.append(anom("file", "early_end", "high", rnd(frames[-1]["t"]), dur,
                      f"Decoded frames stop at {frames[-1]['t']:.2f}s but container declares {dur:.2f}s.",
                      ["container duration", "last decoded PTS"], "truncated / damaged file"))

    # ---- 5. timestamp analysis (source timing)
    ts = [f["t"] for f in frames if f["t"] is not None]
    deltas = np.diff(ts) if len(ts) > 1 else np.array([])
    # Expected interval: median spacing is robust to the very gaps we are looking for
    # (average_rate is frames/duration and is biased by drops).
    exp = float(np.median(deltas)) if deltas.size else (1.0 / nominal if nominal else 1 / TARGET_FPS)
    ts_info = {"expected_interval_ms": round(exp * 1000, 3)}
    if deltas.size:
        ts_info.update({
            "median_interval_ms": round(float(np.median(deltas)) * 1000, 3),
            "jitter_std_ms": round(float(np.std(deltas)) * 1000, 3),
            "min_interval_ms": round(float(deltas.min()) * 1000, 3),
            "max_interval_ms": round(float(deltas.max()) * 1000, 3),
            "measured_fps": round(1 / float(np.median(deltas)), 3) if np.median(deltas) > 0 else None,
            "is_vfr": bool(np.std(deltas) > exp * 0.05),
        })
    report["decode"]["timestamps"] = ts_info
    for i, d in enumerate(deltas):
        a, b = frames[i], frames[i + 1]
        if d <= 0:
            A.append(anom("timing", "duplicate_or_backward_pts", "medium", rnd(a["t"]), rnd(b["t"]),
                          f"PTS {'repeats' if d == 0 else 'goes backwards'} between frames {a['idx']} and {b['idx']}.",
                          ["container PTS"], "muxing / encoding fault", frames=[a["idx"], b["idx"]]))
        elif d > exp * 1.5:
            missing = int(round(d / exp)) - 1
            A.append(anom("timing", "timestamp_gap", "medium", rnd(a["t"]), rnd(b["t"]),
                          f"{d * 1000:.1f} ms gap (expected {exp * 1000:.1f} ms) ≈ {missing} frame slot(s) "
                          f"with no picture after frame {a['idx']}.",
                          ["container PTS"], "capture / encoding (frames never written)",
                          frames=[a["idx"], b["idx"]], missing_frames=missing))

    # ---- 6. reference counter (strongest evidence)
    counters = [f["counter"] for f in frames]
    readable = sum(c is not None for c in counters)
    has_ref = readable >= 0.9 * len(frames)
    report["decode"]["reference_counter"] = {"present": has_ref, "readable_frames": readable}
    if has_ref:
        prev = None
        for f in frames:
            c = f["counter"]
            if c is None:
                A.append(anom("content", "unreadable_reference", "medium", rnd(f["t"]), rnd(f["t"]),
                              f"Reference counter unreadable on frame {f['idx']} (picture damaged).",
                              ["reference barcode"], "file damage / decode artefact", frames=[f["idx"]]))
                continue
            if prev is not None:
                pc, pf = prev
                dc = c - pc
                if dc == 0:
                    last = A[-1] if A else None
                    if last and last["type"] == "duplicated_source_frame" and last["frames"][-1] == pf["idx"]:
                        last["frames"][-1] = f["idx"]
                        last["t_end_s"] = rnd(f["t"])
                        last["repeats"] += 1
                        last["detail"] = (f"Frames {last['frames'][0] + 1}–{f['idx']} repeat source frame #{c} "
                                          f"({last['repeats']} duplicate(s)).")
                    else:
                        A.append(anom("source", "duplicated_source_frame", "medium", rnd(pf["t"]), rnd(f["t"]),
                                      f"Frame {f['idx']} repeats source frame #{c}.",
                                      ["reference counter repeats", "PTS advances normally"] +
                                      (["picture identical"] if f["mad"] is not None and f["mad"] < 1.0 else []),
                                      "capture / encoding (frame re-used)", frames=[pf["idx"], f["idx"]], repeats=1))
                elif dc > 1:
                    gap_ts = (f["t"] - pf["t"]) > exp * 1.5
                    A.append(anom("source", "missing_source_frames", "high", rnd(pf["t"]), rnd(f["t"]),
                                  f"Source frames #{pc + 1}–#{c - 1} ({dc - 1}) are absent between "
                                  f"frames {pf['idx']} and {f['idx']}.",
                                  ["reference counter jump"] + (["PTS gap agrees"] if gap_ts else ["PTS regular (frames dropped before encode, timeline re-packed)"]),
                                  "capture / encoding (never in file)", frames=[pf["idx"], f["idx"]],
                                  missing_frames=dc - 1))
                elif dc < 0:
                    A.append(anom("source", "out_of_order_frame", "high", rnd(pf["t"]), rnd(f["t"]),
                                  f"Counter goes backwards #{pc} → #{c}.", ["reference counter"],
                                  "encoding / muxing fault", frames=[pf["idx"], f["idx"]]))
            prev = (c, f)

    # ---- 7. content analysis
    mads = np.array([f["mad"] for f in frames[1:] if f["mad"] is not None])
    base = float(np.median(mads)) if mads.size else 0.0
    spread = float(np.median(np.abs(mads - base))) if mads.size else 0.0
    report["decode"]["content"] = {"median_frame_diff": round(base, 3), "diff_mad": round(spread, 3)}
    frozen = [f["idx"] for f in frames[1:] if f["mad"] is not None and f["mad"] < max(0.35, base * 0.05)]
    for s, e in runs(frozen):
        if has_ref and all(frames[i]["counter"] is not None and frames[i]["counter"] == frames[i - 1]["counter"]
                           for i in range(s, e + 1)):
            continue  # already reported (with stronger evidence) by the counter check
        A.append(anom("content", "frozen_picture", "low", rnd(frames[s - 1]["t"]), rnd(frames[e]["t"]),
                      f"Frames {s - 1}–{e} are visually identical ({e - s + 1} repeat(s)).",
                      ["frame difference ≈ 0"],
                      "capture freeze OR genuinely static scene (ambiguous without reference)",
                      frames=[s - 1, e]))
    if spread > 0:
        thr = base + 12 * spread + 10
        for f in frames[1:]:
            if f["mad"] is not None and f["mad"] > thr:
                A.append(anom("content", "abrupt_change", "low", rnd(f["t"]), rnd(f["t"]),
                              f"Frame {f['idx']} differs sharply from its predecessor (Δ={f['mad']:.1f}, typical {base:.1f}).",
                              ["frame difference spike"], "scene cut, skipped frames or corruption",
                              frames=[f["idx"] - 1, f["idx"]]))
    for f in frames:
        if f["std"] < 2.0:
            A.append(anom("content", "flat_frame", "medium", rnd(f["t"]), rnd(f["t"]),
                          f"Frame {f['idx']} is a uniform picture (mean luma {f['mean']:.0f}).",
                          ["luma variance ≈ 0"], "capture blank / decoder concealment", frames=[f["idx"]]))

    # ---- 8. 30 FPS extraction grid (what a 30 FPS consumer sees)
    report["frames_30fps"], timeline = resample_30(frames)

    if thumbs_dir and A:
        report["thumbnails"] = save_thumbs(path, A, thumbs_dir)

    # ---- verdict
    sev = {a["severity"] for a in A}
    if "critical" in sev or v["errors"]:
        report["verdict"] = "INVALID_VIDEO"
    elif any(a["layer"] == "file" and a["severity"] == "high" for a in A):
        report["verdict"] = "DAMAGED_VIDEO"
    elif any(a["layer"] in ("source", "timing") for a in A) or "high" in sev or "medium" in sev:
        report["verdict"] = "VALID_WITH_FRAME_ANOMALIES"
    else:
        report["verdict"] = "VALID_NO_ANOMALIES" if not A else "VALID_WITH_MINOR_OBSERVATIONS"
    report["_timeline"] = timeline
    return finish(report, t0)


def add_frame(frames, fr, tb, prev_th):
    gray = fr.to_ndarray(format="gray")
    th = thumb(gray)
    t = float(fr.pts * tb) if fr.pts is not None else None
    frames.append({
        "pts": fr.pts, "t": t, "key": bool(fr.key_frame),
        "counter": read_barcode(gray),
        "mad": float(np.abs(th - prev_th).mean()) if prev_th is not None else None,
        "mean": float(gray.mean()), "std": float(gray.std()),
        "_th": th,
    })


def resample_30(frames):
    ts = [f["t"] for f in frames]
    start, end = ts[0], ts[-1]
    n = int(math.floor((end - start) * TARGET_FPS + 1e-6)) + 1
    tl = []
    used = set()
    j = 0
    repeats = 0
    for k in range(n):
        t = start + k / TARGET_FPS
        while j + 1 < len(frames) and frames[j + 1]["t"] <= t + 1e-4:
            j += 1
        f = frames[j]
        status = "new" if f["idx"] not in used else "repeat"
        repeats += status == "repeat"
        used.add(f["idx"])
        tl.append({"i": k, "t": round(t, 4), "src_idx": f["idx"], "src_pts_s": round(f["t"], 4),
                   "src_counter": f["counter"], "status": status})
    skipped = [f["idx"] for f in frames if f["idx"] not in used]
    summary = {
        "target_fps": TARGET_FPS,
        "grid_frames": n,
        "source_frames": len(frames),
        "grid_slots_repeating_previous_source_frame": repeats,
        "source_frames_not_sampled": len(skipped),
        "note": ("Repeats/omissions here are caused by converting the source timeline to a 30 FPS grid "
                 "and are NOT anomalies by themselves. A repeat counts as an anomaly only if the source "
                 "timeline also shows a gap (see timing/source anomalies)."),
    }
    return summary, tl


def save_thumbs(path, anomalies, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    want = {}
    for a in anomalies:
        for fi in (a.get("frames") or [])[:2]:
            if len(want) < 24:
                want[fi] = True
    saved = {}
    try:
        with av.open(path) as c:
            for i, fr in enumerate(c.decode(video=0)):
                if i in want:
                    p = os.path.join(out_dir, f"frame_{i:05d}.jpg")
                    fr.to_image().resize((320, 180)).save(p, quality=80)
                    saved[i] = os.path.basename(p)
                if len(saved) == len(want):
                    break
    except Exception:  # noqa: BLE001
        pass
    return saved


def anom(layer, kind, severity, t_start, t_end, detail, evidence, attribution, **extra):
    a = {"layer": layer, "type": kind, "severity": severity, "t_start_s": t_start, "t_end_s": t_end,
         "detail": detail, "evidence": evidence, "likely_origin": attribution,
         "confidence": confidence(layer, evidence)}
    a.update(extra)
    return a


def confidence(layer, evidence):
    if layer == "source" and any("agrees" in e for e in evidence):
        return "confirmed (reference counter + timestamps)"
    if layer == "source":
        return "confirmed (reference counter)"
    if layer in ("file",):
        return "confirmed (decoder/container)"
    if layer == "timing":
        return "likely (timestamps only)"
    return "possible (picture content only)"


def rnd(x):
    return None if x is None else round(float(x), 4)


def finish(report, t0):
    report["analysis_seconds"] = round(time.time() - t0, 2)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    report["anomalies"].sort(key=lambda a: (order[a["severity"]], a["t_start_s"] if a["t_start_s"] is not None else -1))
    report["anomaly_counts"] = {}
    for a in report["anomalies"]:
        report["anomaly_counts"][a["type"]] = report["anomaly_counts"].get(a["type"], 0) + 1
    return report


LIMITATIONS = [
    "A byte-perfect transfer (matching SHA-256) proves only that the backend holds exactly the bytes the client "
    "sent. It cannot prove that the recording itself has no dropped frames.",
    "Without a reference counter, missing frames are inferred from timestamp gaps. If a capture pipeline drops "
    "frames and then re-stamps the remaining frames at a regular rate, the drop is invisible to timestamps and "
    "can only be suspected from abrupt picture changes.",
    "Frozen (identical) consecutive pictures may be a capture freeze or a genuinely static scene; content "
    "analysis alone cannot tell them apart.",
    "Resampling to 30 FPS intentionally repeats frames (sources < 30 FPS) or omits them (sources > 30 FPS); "
    "those are reported as conversion effects, not anomalies.",
    "Decoder error concealment can hide small corruptions: a damaged slice may decode without an error but "
    "with visual artefacts that no metric here flags.",
    "Abrupt-change detection uses a statistical threshold; scene cuts in real footage produce false positives.",
]


if __name__ == "__main__":
    import sys
    r = analyze(sys.argv[1])
    r.pop("_timeline", None)
    print(json.dumps(r, indent=2))
