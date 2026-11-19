"""End-to-end reliability scenarios -> evidence/ (JSON + Markdown summary).

Starts the backend and network-impairment proxies in-process, uploads sample
videos through them with the real client, and checks the outcome of each test.

  python tests/run_scenarios.py              # all scenarios
  python tests/run_scenarios.py stable hostile
"""
import hashlib
import http.client
import json
import os
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, d) for d in ("server", "client", "netsim")]
EVID = os.path.join(ROOT, "evidence")
os.environ["VI_DATA"] = os.path.join(ROOT, "data")

import server as backend  # noqa: E402
from proxy import NetSim  # noqa: E402
from uploader import Uploader  # noqa: E402

BACKEND = 8765
S = os.path.join(ROOT, "samples")


def sample(n):
    return os.path.join(S, n)


def run_upload(name, video, proxies, **kw):
    lines = []
    logf = lambda m: (lines.append(m), print(f"[{name}] {m}", flush=True))  # noqa: E731
    sims = [NetSim(port, BACKEND, prof, name=label, seed=seed).start() for label, port, prof, seed in proxies]
    try:
        up = Uploader(video, [f"http://127.0.0.1:{p[1]}" for p in proxies], log=logf, seed=3, **kw)
        res = up.run()
    finally:
        for s in sims:
            s.stop()
    res["netsim"] = {s.name: {"profile": p[2], "stats": s.stats,
                              "strength_min": min((x[1] for x in s.samples), default=None),
                              "strength_max": max((x[1] for x in s.samples), default=None)}
                     for s, p in zip(sims, proxies)}
    os.makedirs(os.path.join(EVID, "logs"), exist_ok=True)
    with open(os.path.join(EVID, "logs", f"{name}.json"), "w") as f:
        json.dump({"result": res, "attempts": up.attempts,
                   "strength_trace": {s.name: s.samples for s in sims}, "client_log": lines}, f, indent=1)
    return res


def raw(method, path, body=b"", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", BACKEND, timeout=60)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


# ------------------------------------------------------------------ scenarios
def sc_stable():
    r = run_upload("stable", sample("clean_30fps.mp4"), [("wired", 9101, "stable", 1)])
    return r, r["status"] == "SUCCESS" and r["verdict"] == "VALID_NO_ANOMALIES"


def sc_variable():
    r = run_upload("variable", sample("dropped_frames.mp4"), [("mobile", 9102, "variable", 2)])
    return r, r["status"] == "SUCCESS" and r["anomaly_counts"].get("missing_source_frames") == 3


def sc_interrupt():
    r = run_upload("interrupt", sample("duplicated_frames.mp4"), [("flaky", 9103, "interrupt", 3)])
    return r, r["status"] == "SUCCESS" and r["metrics"]["retries"] > 0 and "duplicated_source_frame" in r["anomaly_counts"]


def sc_switching():
    r = run_upload("switching", sample("capture_stall_vfr.mp4"),
                   [("wifi", 9104, "wifi_dies", 4), ("cellular", 9105, "cellular", 5)])
    return r, r["status"] == "SUCCESS" and r["metrics"]["path_switches"] >= 1 and "timestamp_gap" in r["anomaly_counts"]


def sc_hostile():
    r = run_upload("hostile", sample("source_24fps.mp4"), [("hostile", 9106, "hostile", 6)])
    return r, r["status"] == "SUCCESS" and r["verdict"] == "VALID_NO_ANOMALIES"


def sc_crash_resume():
    v = sample("clean_30fps.mp4")
    size = os.path.getsize(v)
    r1 = run_upload("crash_part1", v, [("mobile", 9107, "variable", 7)], abort_after_bytes=int(size * 0.4))
    r2 = run_upload("crash_part2_resume", v, [("mobile", 9107, "variable", 8)])
    r2["first_attempt"] = {k: r1.get(k) for k in ("status", "error", "bytes_confirmed", "upload_id")}
    ok = (r1["status"] == "CRASHED" and r2["status"] == "SUCCESS" and r2["upload_id"] == r1["upload_id"]
          and r2["resumed_from"] > 0)
    return r2, ok


def sc_stall_detect():
    """Path dead for the whole run and no alternative: client must report STALLED, never success."""
    r = run_upload("stall_detect", sample("clean_30fps.mp4"), [("dead", 9108, "wifi_dies", 9)], stall_timeout=12)
    return r, r["status"] in ("STALLED", "FAILED") and r.get("bytes_confirmed", 0) < r["size"]


def sc_damaged(fname, expect):
    def f():
        r = run_upload(f"file_{fname}", sample(fname), [("mobile", 9110, "variable", 10)])
        return r, r["status"] == "SUCCESS" and r["verdict"] == expect
    return f


def sc_reject_incomplete():
    """Server must refuse /complete while bytes are missing."""
    data = os.urandom(300_000)
    st, s = raw("POST", "/api/uploads", json.dumps({"filename": "x.mp4", "size": len(data),
                                                   "sha256": hashlib.sha256(data).hexdigest()}).encode())
    part = data[:100_000]
    raw("PATCH", f"/api/uploads/{s['id']}", part, {"Upload-Offset": "0",
                                                    "Chunk-SHA256": hashlib.sha256(part).hexdigest()})
    st2, body = raw("POST", f"/api/uploads/{s['id']}/complete")
    return {"complete_status": st2, "response": body}, st2 == 409 and body.get("missing_bytes") == 200_000


def sc_reject_hash_mismatch():
    """Client declares one SHA-256 but different bytes arrive (all chunk hashes valid): must be rejected."""
    real = os.urandom(200_000)
    claimed = hashlib.sha256(b"something else").hexdigest()
    st, s = raw("POST", "/api/uploads", json.dumps({"filename": "y.mp4", "size": len(real),
                                                   "sha256": claimed}).encode())
    raw("PATCH", f"/api/uploads/{s['id']}", real, {"Upload-Offset": "0",
                                                    "Chunk-SHA256": hashlib.sha256(real).hexdigest()})
    st2, body = raw("POST", f"/api/uploads/{s['id']}/complete")
    return {"complete_status": st2, "state": body.get("state"), "detail": body.get("state_detail")}, \
        st2 == 422 and body.get("state") == "rejected"


def sc_reject_bad_chunk():
    data = os.urandom(50_000)
    st, s = raw("POST", "/api/uploads", json.dumps({"filename": "z.mp4", "size": len(data),
                                                   "sha256": hashlib.sha256(data).hexdigest()}).encode())
    st2, b = raw("PATCH", f"/api/uploads/{s['id']}", data, {"Upload-Offset": "0", "Chunk-SHA256": "0" * 64})
    st3, b3 = raw("GET", f"/api/uploads/{s['id']}")
    return {"patch_status": st2, "server_offset_after": b3["offset"]}, st2 == 422 and b3["offset"] == 0


SCENARIOS = {
    "stable": ("Stable connection (100%)", sc_stable),
    "variable": ("Variable signal 20–100%", sc_variable),
    "interrupt": ("Temporary interruptions, timeouts, reconnection", sc_interrupt),
    "switching": ("Connection switching Wi-Fi → cellular", sc_switching),
    "hostile": ("Fluctuating speed + resets + in-flight corruption", sc_hostile),
    "crash_resume": ("Client crash at 40% then resume", sc_crash_resume),
    "stall_detect": ("Dead network: stall must be reported, not success", sc_stall_detect),
    "altered": ("Altered (bit-flipped) video", sc_damaged("altered.mp4", "DAMAGED_VIDEO")),
    "truncated": ("Truncated video", sc_damaged("truncated.mp4", "DAMAGED_VIDEO")),
    "invalid": ("Invalid (non-video) file", sc_damaged("invalid.mp4", "INVALID_VIDEO")),
    "reject_incomplete": ("Server refuses to complete a partial upload", sc_reject_incomplete),
    "reject_hash": ("Server rejects whole-file SHA-256 mismatch", sc_reject_hash_mismatch),
    "reject_chunk": ("Server rejects corrupted chunk", sc_reject_bad_chunk),
}


def main(names):
    prev = []
    if names and os.path.exists(os.path.join(EVID, "results.json")):  # re-running a subset: keep the rest
        prev = json.load(open(os.path.join(EVID, "results.json")))
    elif os.path.isdir(os.environ["VI_DATA"]):
        shutil.rmtree(os.environ["VI_DATA"])
    srv = backend.serve(port=BACKEND)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.makedirs(EVID, exist_ok=True)
    results = []
    for key in names or SCENARIOS:
        title, fn = SCENARIOS[key]
        print(f"\n===== {key}: {title}", flush=True)
        t = time.time()
        try:
            res, ok = fn()
        except Exception as e:  # noqa: BLE001
            res, ok = {"exception": repr(e)}, False
        results.append({"key": key, "title": title, "pass": bool(ok), "seconds": round(time.time() - t, 1),
                        "result": res})
        print(f"===== {key}: {'PASS' if ok else 'FAIL'}", flush=True)
        if res.get("upload_id"):  # keep a copy of each integrity report
            src = os.path.join(os.environ["VI_DATA"], "uploads", res["upload_id"], "report.json")
            if os.path.exists(src):
                os.makedirs(os.path.join(EVID, "reports"), exist_ok=True)
                shutil.copy(src, os.path.join(EVID, "reports", f"{key}.json"))
                c = http.client.HTTPConnection("127.0.0.1", BACKEND)
                c.request("GET", f"/reports/{res['upload_id']}")
                html = c.getresponse().read().decode()
                html = html.replace("src='/thumbs/", f"src='../../data/uploads/").replace(
                    f"{res['upload_id']}/frame_", f"{res['upload_id']}/thumbs/frame_")
                with open(os.path.join(EVID, "reports", f"{key}.html"), "w") as f:
                    f.write(html)
    if prev:
        new = {r["key"]: r for r in results}
        order = list(SCENARIOS)
        results = sorted([new.get(r["key"], r) for r in prev] + [r for k, r in new.items()
                         if k not in {p["key"] for p in prev}], key=lambda r: order.index(r["key"]))
    with open(os.path.join(EVID, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    write_md(results)
    srv.shutdown()
    print("\n" + open(os.path.join(EVID, "RESULTS.md")).read())
    return all(r["pass"] for r in results)


def write_md(results):
    L = ["# Transfer reliability & integrity test evidence", "",
         f"Run at {time.strftime('%Y-%m-%d %H:%M:%S')} · {sum(r['pass'] for r in results)}/{len(results)} passed", "",
         "| # | Scenario | Result | Time (s) | Transfer | Retries | Timeouts | Hash rejects | Path switches | Resyncs | Verdict |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        x = r["result"]
        m = x.get("metrics", {})
        L.append(f"| {i} | {r['title']} | {'✅ PASS' if r['pass'] else '❌ FAIL'} | {r['seconds']} | "
                 f"{x.get('status', x.get('complete_status', x.get('patch_status', '')))} | {m.get('retries', '')} | "
                 f"{m.get('timeouts', '')} | {m.get('hash_rejects', '')} | {m.get('path_switches', '')} | "
                 f"{m.get('offset_resyncs', '')} | {x.get('verdict', '') or ''} |")
    L += ["", "## Network conditions observed by the impairment proxy", "",
          "| Scenario | Path | Profile | Strength min–max | Stalls | Resets | Corrupted blocks | Outage kills |",
          "|---|---|---|---|---|---|---|---|"]
    for r in results:
        for path, n in (r["result"].get("netsim") or {}).items():
            st = n["stats"]
            smin, smax = n["strength_min"], n["strength_max"]
            rng = f"{smin * 100:.0f}%–{smax * 100:.0f}%" if smin is not None else "–"
            L.append(f"| {r['key']} | {path} | {n['profile']} | {rng} | {st['stalls']} | {st['resets']} | "
                     f"{st['corrupted']} | {st['outage_kills'] + st['refused_in_outage']} |")
    L += ["", "## Anomalies reported per uploaded video", ""]
    for r in results:
        if r["result"].get("anomaly_counts") is not None:
            L.append(f"- **{r['key']}** → `{r['result'].get('verdict')}` {json.dumps(r['result']['anomaly_counts'])}")
    L += ["", "Per-scenario client attempt logs and strength traces: `evidence/logs/*.json`. "
          "Integrity reports: `evidence/reports/*.json|html`."]
    with open(os.path.join(EVID, "RESULTS.md"), "w") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) else 1)
