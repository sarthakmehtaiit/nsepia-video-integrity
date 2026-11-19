"""Resumable, integrity-checked video upload backend (stdlib only + analyzer).

Protocol (offset based, tus-like, every step idempotent):

  POST /api/uploads              {"filename","size","sha256"} -> session (re-used if the same
                                 sha256+size is already in progress => survives client restarts)
  HEAD /api/uploads/<id>         -> Upload-Offset header (bytes durably stored)
  GET  /api/uploads/<id>         -> session JSON
  PATCH /api/uploads/<id>        headers: Upload-Offset, Chunk-SHA256, Content-Length; body = chunk
                                 * body must be read completely and hash must match, else rejected
                                 * written at offset, fsync'd, then offset advances (atomic commit)
                                 * offset mismatch -> 409 with the real offset (client re-syncs)
  POST /api/uploads/<id>/complete
                                 -> verifies size + whole-file SHA-256, then runs the analyser.
                                 Only then is the upload "verified". Mismatch -> "rejected".
  GET  /api/uploads/<id>/report  -> integrity report JSON;  /reports/<id>  -> HTML report
  GET  /                         -> dashboard
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyzer  # noqa: E402
import report_html  # noqa: E402

DATA = os.environ.get("VI_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
MAX_CHUNK = 8 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024 * 1024
STALL_SECONDS = 20
READ_TIMEOUT = 15  # socket read timeout: a stalled body is abandoned, never half-committed

LOCK = threading.Lock()
SESSION_LOCKS = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ persistence
def sess_dir(uid):
    return os.path.join(DATA, "uploads", uid)


def load(uid):
    if not re.fullmatch(r"[0-9a-f]{32}", uid or ""):
        return None
    p = os.path.join(sess_dir(uid), "session.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def save(s):
    p = os.path.join(sess_dir(s["id"]), "session.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def all_sessions():
    root = os.path.join(DATA, "uploads")
    out = []
    if os.path.isdir(root):
        for uid in os.listdir(root):
            s = load(uid)
            if s:
                out.append(public(s))
    return sorted(out, key=lambda s: s["created"], reverse=True)


def public(s):
    s = dict(s)
    now = time.time()
    if s["state"] == "uploading" and now - s["last_activity"] > STALL_SECONDS:
        s["state_detail"] = f"stalled: no progress for {int(now - s['last_activity'])}s"
    s["progress"] = round(s["offset"] / s["size"], 4) if s["size"] else 0
    return s


def slock(uid):
    with LOCK:
        return SESSION_LOCKS.setdefault(uid, threading.Lock())


# ------------------------------------------------------------------ handler
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VideoIntegrity/1.0"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    # -- utils
    def send_json(self, code, obj, headers=None):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, n):
        """Read exactly n bytes or raise (short read = incomplete chunk)."""
        self.connection.settimeout(READ_TIMEOUT)
        buf = bytearray()
        while len(buf) < n:
            part = self.rfile.read(min(65536, n - len(buf)))
            if not part:
                raise IOError(f"connection closed after {len(buf)}/{n} bytes")
            buf += part
        return bytes(buf)

    def route(self):
        p = self.path.split("?")[0].rstrip("/")
        m = re.fullmatch(r"/api/uploads/([0-9a-f]{32})(/(complete|report|timeline))?", p)
        return p, (m.group(1) if m else None), (m.group(3) if m else None)

    # -- verbs
    def do_HEAD(self):
        _, uid, _ = self.route()
        s = load(uid) if uid else None
        if not s:
            return self.send_json(404, {"error": "unknown upload"})
        self.send_json(200, {}, {"Upload-Offset": s["offset"], "Upload-Length": s["size"],
                                 "Upload-State": s["state"]})

    def do_GET(self):
        p, uid, sub = self.route()
        if p in ("", "/"):
            return self.send_bytes(200, report_html.dashboard(all_sessions()).encode(), "text/html; charset=utf-8")
        if p == "/api/uploads":
            return self.send_json(200, all_sessions())
        if p == "/api/health":
            return self.send_json(200, {"ok": True})
        m = re.fullmatch(r"/reports/([0-9a-f]{32})", p)
        if m:
            s = load(m.group(1))
            rp = os.path.join(sess_dir(m.group(1)), "report.json") if s else None
            if not rp or not os.path.exists(rp):
                return self.send_bytes(404, b"no report", "text/plain")
            with open(rp) as f:
                return self.send_bytes(200, report_html.render(json.load(f), s).encode(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/thumbs/([0-9a-f]{32})/(frame_\d{5}\.jpg)", p)
        if m:
            fp = os.path.join(sess_dir(m.group(1)), "thumbs", m.group(2))
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self.send_bytes(200, f.read(), "image/jpeg")
            return self.send_bytes(404, b"", "text/plain")
        s = load(uid) if uid else None
        if not s:
            return self.send_json(404, {"error": "unknown upload"})
        if sub in ("report", "timeline"):
            rp = os.path.join(sess_dir(uid), f"{sub}.json")
            if not os.path.exists(rp):
                return self.send_json(404, {"error": f"no {sub} yet", "state": s["state"]})
            with open(rp) as f:
                return self.send_bytes(200, f.read().encode(), "application/json")
        return self.send_json(200, public(s))

    def do_POST(self):
        p, uid, sub = self.route()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.read_body(n) if n else b"{}"
        except Exception as e:  # noqa: BLE001
            return self.send_json(400, {"error": f"incomplete request body: {e}"})
        if p == "/api/uploads":
            return self.create(raw)
        if uid and sub == "complete":
            try:
                meta = json.loads(raw).get("client_meta") or {}
            except Exception:  # noqa: BLE001
                meta = {}
            return self.complete(uid, meta)
        self.send_json(404, {"error": "not found"})

    def create(self, raw):
        try:
            req = json.loads(raw)
            size, sha = int(req["size"]), str(req["sha256"]).lower()
            assert 0 < size <= MAX_FILE and re.fullmatch(r"[0-9a-f]{64}", sha)
        except Exception:  # noqa: BLE001
            return self.send_json(400, {"error": "need filename, size (bytes) and sha256 (hex)"})
        with LOCK:
            for s in all_sessions():  # resume an in-progress upload of the same file
                if s["sha256"] == sha and s["size"] == size and s["state"] == "uploading":
                    log(f"[{s['id'][:8]}] resume requested; offset={s['offset']}")
                    return self.send_json(200, s)
            uid = uuid.uuid4().hex
            os.makedirs(sess_dir(uid))
            open(os.path.join(sess_dir(uid), "data.part"), "wb").close()
            now = time.time()
            s = {"id": uid, "filename": os.path.basename(str(req.get("filename", "video")))[:120],
                 "size": size, "sha256": sha, "offset": 0, "state": "uploading", "created": now,
                 "last_activity": now, "chunks_ok": 0, "chunks_rejected": 0, "duplicate_chunks": 0,
                 "offset_conflicts": 0, "client_meta": req.get("client_meta", {}), "events": []}
            save(s)
        log(f"[{uid[:8]}] new upload {s['filename']} {size:,} B")
        self.send_json(201, public(s))

    def do_PATCH(self):
        _, uid, sub = self.route()
        s = load(uid) if uid and not sub else None
        if not s:
            return self.send_json(404, {"error": "unknown upload"})
        try:
            off = int(self.headers["Upload-Offset"])
            n = int(self.headers["Content-Length"])
            want = self.headers["Chunk-SHA256"].lower()
            assert 0 < n <= MAX_CHUNK
        except Exception:  # noqa: BLE001
            return self.send_json(400, {"error": "need Upload-Offset, Content-Length (<=8MiB), Chunk-SHA256"})
        # read the full body BEFORE touching state: a dropped connection leaves nothing behind
        try:
            body = self.read_body(n)
        except Exception as e:  # noqa: BLE001
            self.close_connection = True
            with slock(uid):
                s = load(uid)
                s["chunks_rejected"] += 1
                s["events"] = (s["events"] + [[time.time(), "incomplete_chunk", off]])[-200:]
                save(s)
            log(f"[{uid[:8]}] incomplete chunk @{off}: {e}")
            return  # connection is dead; nothing to reply to
        if hashlib.sha256(body).hexdigest() != want:
            with slock(uid):
                s = load(uid)
                s["chunks_rejected"] += 1
                s["events"] = (s["events"] + [[time.time(), "chunk_hash_mismatch", off]])[-200:]
                save(s)
            log(f"[{uid[:8]}] chunk @{off} hash mismatch -> rejected")
            return self.send_json(422, {"error": "chunk SHA-256 mismatch; resend", "offset": s["offset"]},
                                  {"Upload-Offset": s["offset"]})
        with slock(uid):
            s = load(uid)
            if s["state"] != "uploading":
                return self.send_json(409, {"error": f"upload is {s['state']}", "offset": s["offset"]},
                                      {"Upload-Offset": s["offset"]})
            if off + n <= s["offset"]:  # retry of a chunk we already committed (lost ACK)
                s["duplicate_chunks"] += 1
                save(s)
                return self.send_json(200, {"offset": s["offset"], "duplicate": True},
                                      {"Upload-Offset": s["offset"]})
            if off != s["offset"]:
                s["offset_conflicts"] += 1
                save(s)
                return self.send_json(409, {"error": "offset mismatch", "offset": s["offset"]},
                                      {"Upload-Offset": s["offset"]})
            if off + n > s["size"]:
                return self.send_json(400, {"error": "chunk exceeds declared size"})
            with open(os.path.join(sess_dir(uid), "data.part"), "r+b") as f:
                f.seek(off)
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            s["offset"] = off + n
            s["chunks_ok"] += 1
            s["last_activity"] = time.time()
            save(s)
        self.send_json(200, {"offset": s["offset"]}, {"Upload-Offset": s["offset"]})

    def complete(self, uid, meta=None):
        with slock(uid):
            s = load(uid)
            if not s:
                return self.send_json(404, {"error": "unknown upload"})
            if meta:
                s["client_meta"] = {k: v for k, v in meta.items() if isinstance(v, (int, float, str))}
            if s["state"] in ("verified", "rejected"):
                return self.send_json(200, public(s))
            part = os.path.join(sess_dir(uid), "data.part")
            actual = os.path.getsize(part)
            if s["offset"] != s["size"] or actual < s["size"]:
                return self.send_json(409, {"error": "upload incomplete — refusing to accept",
                                            "offset": s["offset"], "size": s["size"],
                                            "missing_bytes": s["size"] - s["offset"]})
            digest = analyzer.sha256_file(part)
            s["server_sha256"] = digest
            s["completed_at"] = time.time()
            if digest != s["sha256"]:
                s["state"] = "rejected"
                s["state_detail"] = "whole-file SHA-256 mismatch: stored bytes differ from the client's file"
                save(s)
                log(f"[{uid[:8]}] REJECTED sha mismatch")
                return self.send_json(422, public(s))
            ext = os.path.splitext(s["filename"])[1].lower() or ".bin"
            final = os.path.join(sess_dir(uid), "video" + re.sub(r"[^.a-z0-9]", "", ext)[:6])
            os.replace(part, final)
            s["state"], s["path"] = "verified", os.path.basename(final)
            s["state_detail"] = "byte-for-byte match (size + SHA-256)"
            s["duration_s"] = round(s["completed_at"] - s["created"], 2)
            save(s)
            log(f"[{uid[:8]}] VERIFIED {s['size']:,} B in {s['duration_s']}s; analysing…")

        transfer = {"status": "verified", "client_sha256": s["sha256"], "server_sha256": digest,
                    "size": s["size"], "chunks_ok": s["chunks_ok"], "chunks_rejected": s["chunks_rejected"],
                    "duplicate_chunks": s["duplicate_chunks"], "offset_conflicts": s["offset_conflicts"],
                    "upload_seconds": s["duration_s"], "client_meta": s.get("client_meta", {})}
        try:
            rep = analyzer.analyze(final, transfer, thumbs_dir=os.path.join(sess_dir(uid), "thumbs"))
        except Exception as e:  # noqa: BLE001
            rep = {"verdict": "ANALYSIS_ERROR", "error": repr(e), "transfer": transfer}
        timeline = rep.pop("_timeline", [])
        with open(os.path.join(sess_dir(uid), "report.json"), "w") as f:
            json.dump(rep, f, indent=2)
        with open(os.path.join(sess_dir(uid), "timeline.json"), "w") as f:
            json.dump(timeline, f)
        with slock(uid):
            s = load(uid)
            s["verdict"] = rep["verdict"]
            s["anomaly_counts"] = rep.get("anomaly_counts", {})
            save(s)
        log(f"[{uid[:8]}] report: {rep['verdict']} {rep.get('anomaly_counts')}")
        out = public(s)
        out["report_url"] = f"/reports/{uid}"
        self.send_json(200, out)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # network faults are expected; log one line
        e = sys.exc_info()[1]
        log(f"connection error from {client_address[0]}:{client_address[1]}: {type(e).__name__}")


def serve(host="127.0.0.1", port=8000):
    os.makedirs(os.path.join(DATA, "uploads"), exist_ok=True)
    srv = Server((host, port), H)
    srv.daemon_threads = True
    log(f"backend listening on http://{host}:{port}  data={os.path.abspath(DATA)}")
    return srv


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    serve(a.host, a.port).serve_forever()
