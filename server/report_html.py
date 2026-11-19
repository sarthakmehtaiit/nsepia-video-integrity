"""HTML rendering for the dashboard and integrity reports (no external assets)."""
import html
import json
import time

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e3e2dd;--ok:#1f7a4d;--warn:#b26b00;
--bad:#b3261e;--info:#2f5fb3;--chip:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#9a9992;--line:#33322f;
--ok:#4cc38a;--warn:#e5a33d;--bad:#ef6b62;--info:#7aa2f0;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.mute{color:var(--mute)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.kv{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.kv b{display:block;font-size:18px}
.kv span{color:var(--mute);font-size:12px}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;background:var(--chip)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.info{color:var(--info)}
.wrap{overflow-x:auto}code{font-size:12px;word-break:break-all}a{color:var(--info)}
.thumbs{display:flex;flex-wrap:wrap;gap:8px}.thumbs figure{margin:0;font-size:11px;color:var(--mute)}
.thumbs img{width:160px;border-radius:6px;border:1px solid var(--line);display:block}
ul{margin:6px 0;padding-left:20px}
"""

VCLS = {"VALID_NO_ANOMALIES": "ok", "VALID_WITH_MINOR_OBSERVATIONS": "info", "VALID_WITH_FRAME_ANOMALIES": "warn",
        "DAMAGED_VIDEO": "bad", "INVALID_VIDEO": "bad", "verified": "ok", "rejected": "bad", "uploading": "info"}
SCLS = {"critical": "bad", "high": "bad", "medium": "warn", "low": "info"}
LCOL = {"file": "var(--bad)", "source": "var(--warn)", "timing": "var(--info)", "content": "var(--mute)"}
e = html.escape


def page(title, body):
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def dashboard(sessions):
    rows = []
    for s in sessions:
        st = s.get("state_detail", "") if s["state"] == "uploading" else ""
        v = s.get("verdict") or ""
        link = f"<a href='/reports/{s['id']}'>report</a>" if v else ""
        rows.append(f"<tr><td><code>{s['id'][:8]}</code></td><td>{e(s['filename'])}</td>"
                    f"<td>{s['size']:,}</td><td>{s['progress'] * 100:.0f}%</td>"
                    f"<td><span class='tag {VCLS.get(s['state'], '')}'>{e(s['state'])}</span> "
                    f"<span class='mute'>{e(st)}</span></td>"
                    f"<td>{s['chunks_ok']} / <span class='{'bad' if s['chunks_rejected'] else 'mute'}'>"
                    f"{s['chunks_rejected']}</span></td>"
                    f"<td><span class='tag {VCLS.get(v, '')}'>{e(v)}</span></td><td>{link}</td></tr>")
    body = (
        "<h1>Video integrity backend</h1><p class=mute>Resumable uploads → SHA-256 verification → 30 FPS frame "
        "analysis. Auto-refreshes every 3 s.</p><div class='card wrap'><table><tr><th>ID</th><th>File</th>"
        "<th>Bytes</th><th>Recv</th><th>Transfer</th><th>Chunks ok / rejected</th><th>Verdict</th><th></th></tr>"
        + ("".join(rows) or "<tr><td colspan=8 class=mute>No uploads yet.</td></tr>") +
        "</table></div><script>setTimeout(()=>location.reload(),3000)</script>")
    return page("Video Integrity", body)


def timeline_svg(rep):
    d = (rep.get("stream") or {}).get("declared_duration_s") or (rep.get("decode") or {}).get("last_pts_s") or 30
    W, Hh = 1000, 70
    out = [f"<svg viewBox='0 0 {W} {Hh}' width='100%' role='img' aria-label='Anomaly timeline'>",
           f"<rect x=0 y=22 width={W} height=22 rx=4 style='fill:var(--chip)'/>"]
    last = (rep.get("decode") or {}).get("last_pts_s")
    if last is not None and last < d - 1.0:
        x = last / d * W
        out.append(f"<rect x={x:.1f} y=22 width={W - x:.1f} height=22 style='fill:var(--bad)' opacity=.25><title>no decodable frames</title></rect>")
    for a in rep.get("anomalies", []):
        if a.get("t_start_s") is None:
            continue
        x0 = a["t_start_s"] / d * W
        x1 = max(x0 + 3, (a.get("t_end_s") or a["t_start_s"]) / d * W)
        out.append(f"<rect x={x0:.1f} y=18 width={x1 - x0:.1f} height=30 style='fill:{LCOL.get(a['layer'], 'gray')}'>"
                   f"<title>{e(a['type'])} @ {a['t_start_s']}s</title></rect>")
    for s in range(0, int(d) + 1, 5):
        x = s / d * W
        out.append(f"<text x={x:.1f} y=64 font-size=11 style='fill:var(--mute)' text-anchor='middle'>{s}s</text>")
    out.append("</svg>")
    legend = " ".join(f"<span class=tag style='color:{c}'>■ {k}</span>" for k, c in LCOL.items())
    return "".join(out) + f"<div>{legend}</div>"


def render(rep, s):
    tr = rep.get("transfer", {})
    st = rep.get("stream", {})
    dc = rep.get("decode", {})
    f30 = rep.get("frames_30fps", {})
    ts = dc.get("timestamps", {})
    v = rep.get("verdict", "")
    kv = lambda k, val: f"<div class=kv><span>{e(k)}</span><b>{e(str(val))}</b></div>"  # noqa: E731
    meta = tr.get("client_meta", {})
    body = [f"<h1>Integrity report — {e(s['filename'])}</h1>",
            f"<p class=mute>Upload <code>{s['id']}</code> · original name {e(s['filename'])} · "
            f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}</p>",
            f"<p>Verdict: <span class='tag {VCLS.get(v, '')}' style='font-size:14px'>{e(v)}</span></p>",
            "<h2>1 · Transfer</h2><div class=grid>",
            kv("Status", tr.get("status", s["state"])), kv("Bytes", f"{tr.get('size', s['size']):,}"),
            kv("Upload time", f"{tr.get('upload_seconds', '–')} s"),
            kv("Chunks accepted", tr.get("chunks_ok", "–")), kv("Chunks rejected (hash/short)", tr.get("chunks_rejected", "–")),
            kv("Duplicate chunks (lost ACK)", tr.get("duplicate_chunks", "–")),
            kv("Client retries", meta.get("retries", "–")), kv("Path switches", meta.get("path_switches", "–")),
            "</div>",
            f"<div class=card><div>Client SHA-256 <code>{e(tr.get('client_sha256', ''))}</code></div>"
            f"<div>Server SHA-256 <code>{e(tr.get('server_sha256', ''))}</code></div>"
            f"<div class=ok>{'✔ byte-for-byte identical — any anomaly below was already in the file before upload' if tr.get('client_sha256') and tr.get('client_sha256') == tr.get('server_sha256') else ''}</div></div>",
            "<h2>2 · Validation & decode</h2><div class=grid>",
            kv("Container", (st.get("container_format") or "").split(",")[0] or ( (rep.get("validation") or {}).get("container_sniff") or "—")),
            kv("Codec", f"{st.get('codec', '—')} {st.get('width', '')}×{st.get('height', '')}"),
            kv("Nominal FPS", st.get("nominal_fps", "—")), kv("Measured FPS", ts.get("measured_fps", "—")),
            kv("Declared frames", st.get("declared_frames", "—")), kv("Decoded frames", dc.get("frames_decoded", "—")),
            kv("Declared duration", f"{st.get('declared_duration_s', '—')} s"), kv("Last frame PTS", f"{dc.get('last_pts_s', '—')} s"),
            kv("Decode errors", dc.get("decode_errors", "—")), kv("Timestamp jitter σ", f"{ts.get('jitter_std_ms', '—')} ms"),
            kv("Reference counter", "present" if (dc.get("reference_counter") or {}).get("present") else "absent"),
            kv("VFR", ts.get("is_vfr", "—")), "</div>"]
    errs = (rep.get("validation") or {}).get("errors") or []
    warns = (rep.get("validation") or {}).get("warnings") or []
    if errs or warns:
        body.append("<div class=card>" + "".join(f"<div class=bad>✖ {e(x)}</div>" for x in errs)
                    + "".join(f"<div class=warn>⚠ {e(x)}</div>" for x in warns) + "</div>")
    if f30:
        body += ["<h2>3 · 30 FPS extraction</h2><div class=grid>",
                 kv("30 FPS grid frames", f30.get("grid_frames")), kv("Source frames", f30.get("source_frames")),
                 kv("Grid slots repeating a frame", f30.get("grid_slots_repeating_previous_source_frame")),
                 kv("Source frames not sampled", f30.get("source_frames_not_sampled")), "</div>",
                 f"<p class=mute>{e(f30.get('note', ''))} Per-slot timeline (index, timestamp, source frame, "
                 f"source PTS, reference counter): <a href='/api/uploads/{s['id']}/timeline'>timeline.json</a></p>"]
    an = rep.get("anomalies", [])
    body.append(f"<h2>4 · Anomalies ({len(an)})</h2><div class=card>{timeline_svg(rep)}</div>")
    if an:
        rows = "".join(
            f"<tr><td><span class='tag {SCLS.get(a['severity'], '')}'>{a['severity']}</span></td>"
            f"<td>{e(a['layer'])}</td><td><b>{e(a['type'])}</b><br>{e(a['detail'])}</td>"
            f"<td>{'' if a['t_start_s'] is None else a['t_start_s']}{'' if a.get('t_end_s') in (None, a['t_start_s']) else ' – ' + str(a['t_end_s'])}</td>"
            f"<td>{'<br>'.join(e(x) for x in a['evidence'])}</td><td>{e(a['likely_origin'])}<br>"
            f"<span class=mute>{e(a['confidence'])}</span></td></tr>" for a in an)
        body.append("<div class='card wrap'><table><tr><th>Sev</th><th>Layer</th><th>What</th><th>Time (s)</th>"
                    f"<th>Evidence</th><th>Origin / confidence</th></tr>{rows}</table></div>")
    else:
        body.append("<p class=ok>No frame-level anomalies detected.</p>")
    th = rep.get("thumbnails") or {}
    if th:
        body.append("<h2>Frames at anomaly locations</h2><div class=thumbs>" + "".join(
            f"<figure><img src='/thumbs/{s['id']}/{fn}' loading=lazy><figcaption>frame {i}</figcaption></figure>"
            for i, fn in sorted(th.items(), key=lambda x: int(x[0]))) + "</div>")
    body.append("<h2>5 · How conclusions are drawn & limitations</h2><div class=card><ul>"
                "<li><b>Transfer issues</b> are decided only by size + SHA-256 (client vs. server). A verified "
                "transfer means the bytes are identical; it says nothing about the recording.</li>"
                "<li><b>file</b> layer: decoder/demuxer errors and declared-vs-decoded counts → damage or encoding fault "
                "in the file itself.</li><li><b>source</b> layer: reference counter burned into each picture → proves "
                "a capture frame is missing/duplicated.</li><li><b>timing</b> layer: PTS gaps/repeats → frames never "
                "written at capture/encode time.</li><li><b>content</b> layer: picture similarity → suggestive only.</li>"
                "</ul><ul>" + "".join(f"<li class=mute>{e(x)}</li>" for x in rep.get("limitations", [])) +
                "</ul></div>")
    body.append(f"<p class=mute>Raw JSON: <a href='/api/uploads/{s['id']}/report'>report.json</a> · "
                f"analysis took {rep.get('analysis_seconds', '–')} s · <a href='/'>all uploads</a></p>")
    return page(f"Report {s['filename']}", "".join(body))


if __name__ == "__main__":
    import sys
    rep = json.load(open(sys.argv[1]))
    print(render(rep, {"id": "0" * 32, "filename": rep.get("file", ""), "state": "verified", "size": rep.get("size_bytes", 0)}))
