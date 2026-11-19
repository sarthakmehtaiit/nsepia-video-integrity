"""TCP network-impairment proxy: sits between client and backend and degrades the link.

Network *strength* s(t) in [0.2, 1.0] drives everything:
  bandwidth   = max_bw * s
  latency     = base_latency + (1 - s) * 600 ms
  stall prob  ∝ (1 - s)   (connection silently stops forwarding -> client timeout)
  reset prob  ∝ (1 - s)   (TCP RST mid-transfer)
plus scheduled outages (blackhole or reset), optional in-flight byte corruption
(to prove application-level hashes catch what TCP would not), and a 'dead' window
used to simulate one network path (e.g. Wi-Fi) disappearing during connection switching.

Run standalone:
  python netsim/proxy.py --listen 9001 --upstream 8000 --profile variable
"""
import math
import random
import socket
import struct
import threading
import time

PROFILES = {
    "stable":    dict(strength="stable", max_bw=1_500_000, stall=0.0, reset=0.0, corrupt=0.0, outages=[]),
    "variable":  dict(strength="walk", max_bw=1_500_000, stall=0.004, reset=0.004, corrupt=0.0, outages=[]),
    "interrupt": dict(strength="walk", max_bw=1_500_000, stall=0.004, reset=0.004, corrupt=0.0,
                      outages=[(2.5, 6.0, "blackhole"), (9.0, 11.0, "reset"), (14.0, 18.0, "blackhole")]),
    "hostile":   dict(strength="walk", max_bw=1_200_000, stall=0.006, reset=0.008, corrupt=0.01,
                      outages=[(4.0, 6.0, "reset")]),
    "wifi_dies": dict(strength="stable", max_bw=2_000_000, stall=0.0, reset=0.0, corrupt=0.0,
                      outages=[(2.0, 1e9, "blackhole")]),
    "cellular":  dict(strength="walk", max_bw=700_000, stall=0.003, reset=0.003, corrupt=0.0, outages=[]),
}


class Strength:
    def __init__(self, kind, seed):
        self.kind, self.rnd, self.t0 = kind, random.Random(seed), time.time()
        self.cur, self.last = 0.6, 0.0
        self.lock = threading.Lock()

    def __call__(self):
        t = time.time() - self.t0
        if self.kind == "stable":
            return 1.0
        if self.kind == "sine":
            return 0.6 + 0.4 * math.sin(t * 0.8)
        with self.lock:  # bounded random walk, re-drawn every 0.5 s, can jump to extremes
            while self.last + 0.5 <= t:
                self.last += 0.5
                if self.rnd.random() < 0.12:
                    self.cur = self.rnd.choice([0.2, 1.0])
                else:
                    self.cur = min(1.0, max(0.2, self.cur + self.rnd.uniform(-0.25, 0.25)))
            return self.cur


class NetSim:
    def __init__(self, listen_port, upstream_port, profile="variable", name=None, seed=1,
                 upstream_host="127.0.0.1", **overrides):
        cfg = dict(PROFILES[profile])
        cfg.update(overrides)
        self.cfg, self.name = cfg, name or profile
        self.listen_port, self.upstream = listen_port, (upstream_host, upstream_port)
        self.rnd = random.Random(seed)
        self.strength = Strength(cfg["strength"], seed)
        self.t0 = time.time()
        self.conns = set()
        self.lock = threading.Lock()
        self.stats = dict(connections=0, bytes_up=0, stalls=0, resets=0, corrupted=0, outage_kills=0,
                          refused_in_outage=0)
        self.samples = []  # (t, strength) for evidence
        self.running = True

    # -- helpers
    def now(self):
        return time.time() - self.t0

    def outage(self):
        t = self.now()
        for a, b, kind in self.cfg["outages"]:
            if a <= t < b:
                return kind
        return None

    @staticmethod
    def rst(sock):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def kill(self, pair):
        for s in pair:
            self.rst(s)
        with self.lock:
            self.conns.discard(pair)

    # -- lifecycle
    def start(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.listen_port))
        self.srv.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()
        return self

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
        for p in list(self.conns):
            self.kill(p)

    def _monitor(self):
        last_kind = None
        while self.running:
            kind = self.outage()
            self.samples.append((round(self.now(), 2), round(self.strength(), 3), kind or ""))
            if kind and kind != last_kind and kind == "reset":
                for p in list(self.conns):
                    self.stats["outage_kills"] += 1
                    self.kill(p)
            last_kind = kind
            time.sleep(0.25)

    def _accept(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            kind = self.outage()
            if kind == "reset":
                self.stats["refused_in_outage"] += 1
                self.rst(c)
                continue
            try:
                u = socket.create_connection(self.upstream, timeout=5)
            except OSError:
                self.rst(c)
                continue
            pair = (c, u)
            with self.lock:
                self.conns.add(pair)
            self.stats["connections"] += 1
            threading.Thread(target=self._up, args=(pair,), daemon=True).start()
            threading.Thread(target=self._down, args=(pair,), daemon=True).start()

    def _up(self, pair):
        """client -> server: throttled, delayed, possibly stalled / reset / corrupted."""
        c, u = pair
        c.settimeout(1.0)
        first = True
        try:
            while self.running:
                if self.outage() == "blackhole":  # path is dead: nothing moves
                    time.sleep(0.2)
                    continue
                try:
                    data = c.recv(16384)
                except socket.timeout:
                    first = True
                    continue
                if not data:
                    break
                s = self.strength()
                if first:  # one-way latency at the start of each burst
                    time.sleep((20 + (1 - s) * 600) / 1000)
                    first = False
                while self.outage() == "blackhole":
                    time.sleep(0.2)
                if self.rnd.random() < self.cfg["stall"] * (1.25 - s) * 4:
                    self.stats["stalls"] += 1
                    time.sleep(25)  # longer than the client's timeout
                if self.rnd.random() < self.cfg["reset"] * (1.25 - s) * 4:
                    self.stats["resets"] += 1
                    return self.kill(pair)
                if self.cfg["corrupt"] and len(data) > 1024 and self.rnd.random() < self.cfg["corrupt"] * 4:
                    b = bytearray(data)
                    k = self.rnd.randrange(512, len(b))
                    b[k] ^= 0x5A
                    data = bytes(b)
                    self.stats["corrupted"] += 1
                time.sleep(len(data) / (self.cfg["max_bw"] * s))
                u.sendall(data)
                self.stats["bytes_up"] += len(data)
        except OSError:
            pass
        self.kill(pair)

    def _down(self, pair):
        c, u = pair
        try:
            while self.running:
                data = u.recv(16384)
                if not data:
                    break
                while self.outage() == "blackhole":
                    time.sleep(0.2)
                time.sleep((20 + (1 - self.strength()) * 600) / 1000)
                c.sendall(data)
        except OSError:
            pass
        self.kill(pair)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=9001)
    ap.add_argument("--upstream", type=int, default=8000)
    ap.add_argument("--profile", default="variable", choices=sorted(PROFILES))
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    n = NetSim(a.listen, a.upstream, a.profile, seed=a.seed).start()
    print(f"netsim[{a.profile}] 127.0.0.1:{a.listen} -> :{a.upstream}")
    try:
        while True:
            time.sleep(2)
            print(f"t={n.now():6.1f}s strength={n.strength() * 100:5.1f}% outage={n.outage() or '-':9s} {n.stats}")
    except KeyboardInterrupt:
        n.stop()
