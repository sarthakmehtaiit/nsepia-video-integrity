# Reliable Video Upload & Frame Integrity Detection

nSEPIA technical challenge (Confluentia 2026, IIT BHU). This project sends a 30-second video from a client to a backend over a network whose strength changes between 20% and 100%. The backend accepts the file only after it proves the bytes arrived complete and unchanged. It then decodes the video, samples it at 30 FPS and reports frame-level anomalies, along with the evidence for each finding.

**Live demo: https://nsepia-video-integrity.onrender.com**. Browse the integrity reports from the automated network-fault tests, or upload your own video and get its report. The site runs on Render's free plan, so the first visit after it has been idle takes about 30 seconds to wake up.

```
 ┌──────────────┐  PATCH chunks + SHA-256   ┌──────────────────┐  TCP   ┌───────────────────────────────┐
 │ client/      │ ────────────────────────► │ netsim/proxy.py  │ ─────► │ server/server.py              │
 │ uploader.py  │ ◄──── offset ACKs ─────── │  Wi-Fi path      │        │  resumable offset protocol    │
 │              │                           ├──────────────────┤        │  chunk hash check + fsync     │
 │ • resumable  │  switch path after N      │ netsim/proxy.py  │        │  whole-file SHA-256 gate      │
 │ • AIMD chunk │  consecutive failures     │  cellular path   │        │            │ verified only  │
 │ • timeouts   │ ────────────────────────► │ 20–100% strength │        │            ▼                  │
 │ • back-off   │                           │ latency, stalls, │        │ server/analyzer.py (PyAV)     │
 │ • multipath  │                           │ resets, outages, │        │  validate → decode → 30 FPS   │
 └──────────────┘                           │ byte corruption  │        │  grid → anomalies → report    │
                                            └──────────────────┘        └───────────────────────────────┘
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python tools/make_videos.py samples
```

```bash
.venv/bin/python tests/run_scenarios.py
```

The last command runs all 13 scenarios (about 10 minutes; on a Mac, prefix it with `caffeinate -i` so sleep doesn't freeze the run). It writes `evidence/RESULTS.md`, the per-scenario logs in `evidence/logs/` and the integrity reports in `evidence/reports/` (JSON and HTML).

### Deploy (Render, free tier)

The repo includes a [render.yaml](render.yaml) blueprint. On render.com, click **New → Blueprint** and pick this repo. Render installs `requirements.txt` and starts `python server/server.py`. The server binds to Render's `PORT` and loads the committed test-run reports as demo rows (`SEED_DEMO=1`). Visitors can also upload their own video from the dashboard: the browser sends it in SHA-256-checked chunks that resume after network drops.

The free instance sleeps when idle, so the first visit takes about 30 seconds. Uploaded files are lost whenever it restarts. The network-impairment proxy and test suite are meant to run locally.

### Manual demo (three terminals)

```bash
.venv/bin/python server/server.py --port 8000
```

```bash
.venv/bin/python netsim/proxy.py --listen 9001 --upstream 8000 --profile interrupt
```

```bash
.venv/bin/python client/uploader.py samples/dropped_frames.mp4 --endpoint http://127.0.0.1:9001 --endpoint http://127.0.0.1:8000
```

Open http://127.0.0.1:8000 for the live dashboard. Every upload links to its HTML integrity report.

You can also run the analyser on its own: `.venv/bin/python server/analyzer.py some.mp4`

## 1 · Reliable transfer

| Problem in the brief | How it is handled |
|---|---|
| Low bandwidth, variable speed | Chunk size adapts by AIMD: it grows by 128 KiB when a chunk lands in under 1 s and halves after any failure (32 KiB to 2 MiB). |
| High latency, request timeouts | Each request's timeout is sized from the measured throughput (EWMA), so a stalled request is abandoned instead of hanging. |
| Packet loss, temporary disconnections, upload interruptions | Retries use exponential back-off with jitter. After each failure the client sends `HEAD` to learn how many bytes the server has durably stored, then continues from that offset. |
| Connection switching | The client takes several endpoints (network paths). After 3 consecutive failures it switches path, and the upload continues at the same server offset. |
| Lost ACK (server stored the chunk, client never heard) | The server sees `offset+n ≤ stored offset` and answers `200 duplicate`, so nothing is written twice. |
| Corruption in flight | Every chunk carries `Chunk-SHA256`. The server reads the whole body and checks the hash before it touches state, then answers `422` and the client resends. |
| Partial or short request body | The server needs exactly `Content-Length` bytes and has a 15 s read timeout. A short body is discarded, so a half-written chunk is never committed. |
| Client crash or app restart | `POST /api/uploads` with the same `size+sha256` returns the in-progress session, so the upload resumes from the stored offset. |
| Stalled or failed transfer | The client reports `STALLED` when no bytes are confirmed for `--stall-timeout` seconds, and `FAILED` when the deadline passes. The dashboard flags server sessions with no progress for 20 s. |
| Declaring success | The client declares `SUCCESS` only when `/complete` returns `state=verified` **and** `server_sha256 == client_sha256`. The server refuses `/complete` with `409` while bytes are missing, and rejects the upload with `422` if the whole-file hash differs. |

Durability: each chunk is written at its offset and `fsync`'d. The offset is then advanced in `session.json`, which is written to a temp file, `fsync`'d and replaced atomically. If the server crashes it never claims bytes it does not have.

### Network simulator (`netsim/proxy.py`)

This is a real TCP proxy, so the client and server run their normal network code. The strength `s(t) ∈ [0.2, 1.0]` follows a bounded random walk that jumps to the extremes from time to time, and it drives these settings:

- bandwidth `= max_bw·s`
- one-way latency `= 20 + (1−s)·600 ms`
- stall probability (25 s of silence, which forces a client timeout) and TCP-reset probability, both higher when `s` is low

Scheduled outages either blackhole the path or reset its connections. The proxy can also flip bytes in flight, which shows that the application-level hashes catch damage TCP would not.

Profiles: `stable`, `variable`, `interrupt`, `hostile`, `wifi_dies`, `cellular`.

## 2 · Validation, 30 FPS extraction and anomaly detection

The analyser runs **only after** the transfer is verified. Its steps:

1. **Validate.** Sniff the container signature, open the file with the demuxer and record its stream metadata (codec, size, nominal FPS, declared frames and duration).
2. **Decode.** Decode every packet on a single thread and capture FFmpeg's own error log per packet. This catches damage the decoder *conceals* without raising an exception, and records which packet and time it hit.
3. **Extract at 30 FPS.** Build a 30 FPS grid. For each grid slot, record the index, the timestamp, the source frame index, the source PTS and the reference counter (`/api/uploads/<id>/timeline`). Repeats and omissions caused by the grid itself are reported as *conversion effects*, not anomalies. A 24 FPS source shows 180 repeats and is still `VALID_NO_ANOMALIES`.
4. **Detect anomalies.** Each anomaly is placed in one of four layers, with its evidence and a confidence level:

| Layer | Evidence | Typical conclusion | Confidence |
|---|---|---|---|
| `file` | decoder/demuxer errors, declared vs decoded frame count, last PTS vs declared duration | truncation, bitstream damage, encoder fault | confirmed |
| `source` | **reference counter** burned into each frame | source frames missing, duplicated or out of order | confirmed (plus "PTS agrees" when timestamps also show it) |
| `timing` | PTS gaps, repeats, backwards steps; jitter; VFR | frames never written during capture or encoding | likely |
| `content` | frame-difference (frozen), spikes (abrupt change), flat frames | freeze or blank, or a scene cut | possible |

### Transfer issues vs capture and encoding issues

The brief notes that a byte-perfect transfer cannot prove the recording has no dropped frames. This project keeps the two questions apart:

- **Transfer issues** are decided only by size and SHA-256. A transfer that fails that check is *rejected* and never analysed. So every anomaly in a report on a verified upload was **already in the file before upload**, and the report says so.
- **Capture and encoding issues** need evidence inside the file. Timestamp gaps show that time passed with no picture. Only a **reference counter** (a frame number the capture app embeds in each frame; the test videos carry a 16-bit barcode with parity) proves which source frames are missing. That includes a pipeline that drops frames and then re-stamps the rest at a regular rate, which timestamps alone would never show.

### Sample results (from `samples/`)

| Video | Planted fault | Verdict | What the report finds |
|---|---|---|---|
| clean_30fps | none | VALID_NO_ANOMALIES | nothing |
| dropped_frames | 16 frames removed at 10 s, 20 s and 25 s | VALID_WITH_FRAME_ANOMALIES | 3 × missing_source_frames (5, 1, 10), confirmed by counter + PTS |
| duplicated_frames | freeze of 6 frames at 15 s, 1 at 27 s | VALID_WITH_FRAME_ANOMALIES | duplicated_source_frame runs at 14.97 s and 26.97 s |
| capture_stall_vfr | 0.5 s capture stall, jittery timing | VALID_WITH_FRAME_ANOMALIES | timestamp_gap of 530 ms at 13.27 s (≈15 slots); counter continuous, so it is a stall rather than lost frames |
| source_24fps | none | VALID_NO_ANOMALIES | 180 conversion repeats, correctly not counted as anomalies |
| truncated | file cut at 55% | DAMAGED_VIDEO | 492 of 900 frames decoded, early end at 16.4 s, partial-file error |
| altered | bit flips at 30% and 62% | DAMAGED_VIDEO | decode errors concealed at 8.87 s and 18.53 s |
| invalid | random bytes | INVALID_VIDEO | unparseable container |

## 3 · Tests (`tests/run_scenarios.py`)

| # | Scenario | Pass condition |
|---|---|---|
| 1 | Stable connection | SUCCESS and VALID_NO_ANOMALIES |
| 2 | Variable signal 20–100% | SUCCESS and all 3 drops located |
| 3 | Interruptions, timeouts, reconnection (blackholes and resets) | SUCCESS after retries; duplicates found |
| 4 | Wi-Fi dies, switch to cellular | SUCCESS with ≥ 1 path switch; stall found |
| 5 | Hostile: resets plus in-flight corruption | SUCCESS (corrupt chunks rejected and resent); clean 24 FPS verdict |
| 6 | Client crash at 40%, then restart | Same upload ID resumed from offset > 0, SUCCESS |
| 7 | Network dead, no alternative path | Client reports STALLED and never claims success |
| 8–10 | Altered, truncated or invalid file over a variable link | Transfer SUCCESS; verdict DAMAGED or INVALID |
| 11 | `/complete` with bytes missing | 409, refused |
| 12 | Whole-file SHA-256 mismatch | 422, upload rejected |
| 13 | Chunk with a bad hash | 422, server offset unchanged |

## Limitations

- SHA-256 equality proves the backend holds exactly the client's bytes. It does not prove the recording is free of dropped frames.
- Without a reference counter, missing frames can only be inferred from timestamp gaps. A pipeline that re-stamps frames hides its drops; then only abrupt-change hints remain, and those also fire on scene cuts.
- Frozen pictures can't be told apart from a static scene using content alone.
- Decoder concealment can hide small damage. Errors are caught through FFmpeg's log, but artefacts with no logged error go undetected.
- The proxy simulates network conditions at TCP level on localhost. Radio effects such as real packet reordering are folded into stalls and resets.
- The upload stream is sequential (one connection). Parallel chunk streams would use high-bandwidth links better, but they add reordering logic and gain little at 20% strength.
- The backend is single-node and file-based, and has no auth. In production it would need TLS, auth, session expiry and garbage collection, plus object storage with multipart upload.

## Layout

```
client/uploader.py     resilient resumable client (CLI + library)
server/server.py       upload backend + dashboard (stdlib http.server)
server/analyzer.py     validation, decode, 30 FPS grid, anomaly detection
server/report_html.py  HTML dashboard and integrity report
netsim/proxy.py        TCP network-impairment proxy (20–100% strength)
tools/make_videos.py   test videos with ground truth + reference barcode
tests/run_scenarios.py end-to-end scenarios -> evidence/
```
