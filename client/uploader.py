"""Fault-tolerant video uploader.

* Whole-file SHA-256 computed up front; every chunk carries its own SHA-256.
* Offset-based resumable protocol: after ANY failure the client asks the server
  how many bytes are durably stored (HEAD) and continues from there.
* Adaptive chunk size (AIMD): grows on fast successes, halves on failures.
* Per-request timeouts sized from measured throughput -> stalled requests are
  detected and abandoned instead of hanging.
* Exponential back-off with jitter; multiple network paths (e.g. Wi-Fi + cellular)
  with automatic switching after repeated failures on one path.
* Explicit failure: if no progress for --stall-timeout or the overall deadline
  passes, the upload is reported FAILED/STALLED — never silently "done".
* Success is declared ONLY when the server answers /complete with state=verified
  and its SHA-256 equals ours.

Usage:
  python client/uploader.py video.mp4 --endpoint http://127.0.0.1:9001 --endpoint http://127.0.0.1:9002
"""
import hashlib
import http.client
import json
import os
import random
import sys
import time
from urllib.parse import urlparse

MIN_CHUNK, MAX_CHUNK = 32 * 1024, 2 * 1024 * 1024


class Abort(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Uploader:
    def __init__(self, path, endpoints, chunk=256 * 1024, stall_timeout=60, deadline=600,
                 switch_after=3, log=print, abort_after_bytes=None, seed=None):
        self.path, self.endpoints = path, [e.rstrip("/") for e in endpoints]
        self.ep = 0
        self.chunk = chunk
        self.stall_timeout, self.deadline, self.switch_after = stall_timeout, deadline, switch_after
        self.log = log
        self.abort_after = abort_after_bytes
        self.rnd = random.Random(seed)
        self.size = os.path.getsize(path)
        self.thr = 200_000.0  # bytes/s estimate (EWMA)
        self.m = dict(retries=0, timeouts=0, conn_errors=0, hash_rejects=0, offset_resyncs=0,
                      path_switches=0, chunks_sent=0, bytes_sent=0, duplicate_acks=0)
        self.attempts = []  # evidence log

    # ------------------------------------------------------------ http
    def req(self, method, path, body=None, headers=None, timeout=10):
        u = urlparse(self.endpoints[self.ep])
        conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        c = conn(u.hostname, u.port, timeout=timeout)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            data = r.read()
            return r.status, dict(r.getheaders()), data
        finally:
            c.close()

    def note(self, kind, **kw):
        kw.update(t=round(time.time() - self.t0, 3), kind=kind, ep=self.ep)
        self.attempts.append(kw)

    def fail(self, err):
        self.m["retries"] += 1
        self.consec += 1
        if isinstance(err, TimeoutError) or "timed out" in str(err):
            self.m["timeouts"] += 1
        else:
            self.m["conn_errors"] += 1
        self.chunk = max(MIN_CHUNK, self.chunk // 2)
        if len(self.endpoints) > 1 and self.consec % self.switch_after == 0:
            old = self.ep
            self.ep = (self.ep + 1) % len(self.endpoints)
            self.m["path_switches"] += 1
            self.log(f"  ↪ switching path {self.endpoints[old]} -> {self.endpoints[self.ep]}")
            self.note("path_switch", frm=old, to=self.ep)
        back = min(8.0, 0.25 * 2 ** min(self.consec, 6)) * (0.5 + self.rnd.random())
        time.sleep(back)

    def check_budget(self):
        now = time.time()
        if now - self.last_progress > self.stall_timeout:
            raise Abort(f"STALLED: no progress for {int(now - self.last_progress)}s at "
                        f"{self.offset}/{self.size} bytes")
        if now - self.t0 > self.deadline:
            raise Abort(f"FAILED: deadline {self.deadline}s exceeded at {self.offset}/{self.size} bytes")

    # ------------------------------------------------------------ steps
    def create(self):
        body = json.dumps({"filename": os.path.basename(self.path), "size": self.size,
                           "sha256": self.sha}).encode()
        while True:
            self.check_budget()
            try:
                st, _, data = self.req("POST", "/api/uploads", body, {"Content-Type": "application/json"})
                if st in (200, 201):
                    s = json.loads(data)
                    self.consec = 0
                    return s["id"], s["offset"]
                raise IOError(f"HTTP {st}: {data[:200]!r}")
            except Exception as e:  # noqa: BLE001
                self.log(f"  create failed: {e}")
                self.note("create_fail", err=str(e)[:120])
                self.fail(e)

    def resync(self):
        while True:
            self.check_budget()
            try:
                st, h, _ = self.req("HEAD", f"/api/uploads/{self.uid}", timeout=8)
                if st == 200:
                    off = int(h.get("Upload-Offset") or h.get("upload-offset"))
                    if off != self.offset:
                        self.m["offset_resyncs"] += 1
                        self.log(f"  resync: server has {off:,} bytes (we were at {self.offset:,})")
                    self.offset = off
                    return
                raise IOError(f"HEAD {st}")
            except Exception as e:  # noqa: BLE001
                self.note("resync_fail", err=str(e)[:120])
                self.fail(e)

    def send_chunks(self):
        with open(self.path, "rb") as f:
            while self.offset < self.size:
                self.check_budget()
                if self.abort_after and self.offset >= self.abort_after:
                    raise Abort(f"SIMULATED CLIENT CRASH at {self.offset:,} bytes")
                n = min(self.chunk, self.size - self.offset)
                f.seek(self.offset)
                data = f.read(n)
                digest = hashlib.sha256(data).hexdigest()
                timeout = max(4.0, min(30.0, 2.5 * n / max(self.thr, 20_000) + 2))
                t = time.time()
                try:
                    st, h, body = self.req("PATCH", f"/api/uploads/{self.uid}", data, {
                        "Upload-Offset": str(self.offset), "Chunk-SHA256": digest,
                        "Content-Type": "application/offset+octet-stream"}, timeout=timeout)
                except Exception as e:  # noqa: BLE001  (timeout, reset, refused, ...)
                    self.note("chunk_fail", off=self.offset, n=n, timeout=round(timeout, 1), err=str(e)[:100])
                    self.log(f"  chunk @{self.offset:,} ({n // 1024} KiB) failed: {type(e).__name__}: {str(e)[:60]}")
                    self.fail(e)
                    self.resync()
                    continue
                dt = time.time() - t
                if st == 200:
                    new = int(json.loads(body)["offset"])
                    if json.loads(body).get("duplicate"):
                        self.m["duplicate_acks"] += 1
                    self.m["chunks_sent"] += 1
                    self.m["bytes_sent"] += n
                    self.thr = 0.7 * self.thr + 0.3 * (n / max(dt, 1e-3))
                    self.note("chunk_ok", off=self.offset, n=n, dt=round(dt, 3), bps=int(n / max(dt, 1e-3)))
                    self.offset = new
                    self.last_progress = time.time()
                    self.consec = 0
                    if dt < 1.0:  # additive increase while the link is good
                        self.chunk = min(MAX_CHUNK, self.chunk + 128 * 1024)
                    pct = self.offset / self.size * 100
                    self.log(f"  {pct:5.1f}%  {self.offset:>10,}/{self.size:,}  chunk={n // 1024}KiB "
                             f"{n / max(dt, 1e-3) / 1024:7.0f} KiB/s")
                elif st == 422:  # corrupted in transit, server refused it
                    self.m["hash_rejects"] += 1
                    self.note("chunk_hash_reject", off=self.offset, n=n)
                    self.log(f"  chunk @{self.offset:,} rejected by server (hash mismatch) – resending")
                    self.fail(IOError("hash mismatch"))
                    self.resync()
                elif st == 409:
                    self.note("offset_conflict", off=self.offset)
                    self.resync()
                else:
                    self.note("chunk_http_error", off=self.offset, status=st)
                    self.fail(IOError(f"HTTP {st}"))
                    self.resync()

    def complete(self):
        while True:
            self.check_budget()
            try:
                meta = dict(self.m, client_seconds=round(time.time() - self.t0, 2))
                st, _, body = self.req("POST", f"/api/uploads/{self.uid}/complete",
                                       json.dumps({"client_meta": meta}).encode(),
                                       {"Content-Type": "application/json"}, timeout=120)
                s = json.loads(body)
                if st == 409:  # server says bytes are missing: go back and send them
                    self.log(f"  server refused completion: {s.get('error')}")
                    self.resync()
                    self.send_chunks()
                    continue
                return st, s
            except Exception as e:  # noqa: BLE001
                self.note("complete_fail", err=str(e)[:120])
                self.log(f"  complete failed: {e}")
                self.fail(e)

    # ------------------------------------------------------------ main
    def run(self):
        self.t0 = self.last_progress = time.time()
        self.consec, self.offset, self.uid = 0, 0, None
        self.sha = sha256_file(self.path)
        self.log(f"uploading {self.path} ({self.size:,} B) sha256={self.sha[:16]}…")
        result = {"file": self.path, "size": self.size, "sha256": self.sha}
        try:
            self.uid, self.offset = self.create()
            if self.offset:
                self.log(f"  resuming existing upload {self.uid[:8]} at {self.offset:,} bytes")
            result["upload_id"] = self.uid
            result["resumed_from"] = self.offset
            self.send_chunks()
            st, s = self.complete()
            ok = st == 200 and s.get("state") == "verified" and s.get("server_sha256") == self.sha
            result.update(status="SUCCESS" if ok else "REJECTED", server_state=s.get("state"),
                          server_detail=s.get("state_detail"), server_sha256=s.get("server_sha256"),
                          verdict=s.get("verdict"), anomaly_counts=s.get("anomaly_counts"),
                          report_url=self.endpoints[self.ep] + s.get("report_url", ""))
        except Abort as e:
            result.update(status="STALLED" if "STALLED" in str(e) else
                          ("CRASHED" if "CRASH" in str(e) else "FAILED"), error=str(e), bytes_confirmed=self.offset)
        result.update(seconds=round(time.time() - self.t0, 2), metrics=self.m, final_endpoint=self.endpoints[self.ep])
        self.log(f"RESULT {result['status']} in {result['seconds']}s  {json.dumps(self.m)}")
        return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--endpoint", action="append", help="repeat for multiple network paths")
    ap.add_argument("--stall-timeout", type=float, default=60)
    ap.add_argument("--deadline", type=float, default=900)
    ap.add_argument("--abort-after-bytes", type=int)
    ap.add_argument("--json-out")
    a = ap.parse_args()
    up = Uploader(a.file, a.endpoint or ["http://127.0.0.1:8000"], stall_timeout=a.stall_timeout,
                  deadline=a.deadline, abort_after_bytes=a.abort_after_bytes)
    res = up.run()
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump({"result": res, "attempts": up.attempts}, f, indent=2)
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["status"] == "SUCCESS" else 2)
