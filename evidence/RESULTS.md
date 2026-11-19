# Transfer reliability & integrity test evidence

Run at 2026-11-19 22:53:35 · 13/13 passed

| # | Scenario | Result | Time (s) | Transfer | Retries | Timeouts | Hash rejects | Path switches | Resyncs | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Stable connection (100%) | ✅ PASS | 11.3 | SUCCESS | 0 | 0 | 0 | 0 | 0 | VALID_NO_ANOMALIES |
| 2 | Variable signal 20–100% | ✅ PASS | 82.2 | SUCCESS | 11 | 6 | 0 | 0 | 0 | VALID_WITH_FRAME_ANOMALIES |
| 3 | Temporary interruptions, timeouts, reconnection | ✅ PASS | 63.0 | SUCCESS | 10 | 4 | 0 | 0 | 0 | VALID_WITH_FRAME_ANOMALIES |
| 4 | Connection switching Wi-Fi → cellular | ✅ PASS | 71.7 | SUCCESS | 10 | 3 | 0 | 1 | 0 | VALID_WITH_FRAME_ANOMALIES |
| 5 | Fluctuating speed + resets + in-flight corruption | ✅ PASS | 267.7 | SUCCESS | 88 | 16 | 51 | 0 | 0 | VALID_NO_ANOMALIES |
| 6 | Client crash at 40% then resume | ✅ PASS | 120.4 | SUCCESS | 6 | 2 | 0 | 0 | 0 | VALID_NO_ANOMALIES |
| 7 | Dead network: stall must be reported, not success | ✅ PASS | 19.2 | STALLED | 3 | 1 | 0 | 0 | 0 |  |
| 8 | Altered (bit-flipped) video | ✅ PASS | 91.4 | SUCCESS | 18 | 6 | 0 | 0 | 0 | DAMAGED_VIDEO |
| 9 | Truncated video | ✅ PASS | 57.6 | SUCCESS | 11 | 2 | 0 | 0 | 0 | DAMAGED_VIDEO |
| 10 | Invalid (non-video) file | ✅ PASS | 12.2 | SUCCESS | 3 | 0 | 0 | 0 | 0 | INVALID_VIDEO |
| 11 | Server refuses to complete a partial upload | ✅ PASS | 0.0 | 409 |  |  |  |  |  |  |
| 12 | Server rejects whole-file SHA-256 mismatch | ✅ PASS | 0.0 | 422 |  |  |  |  |  |  |
| 13 | Server rejects corrupted chunk | ✅ PASS | 0.0 | 422 |  |  |  |  |  |  |

## Network conditions observed by the impairment proxy

| Scenario | Path | Profile | Strength min–max | Stalls | Resets | Corrupted blocks | Outage kills |
|---|---|---|---|---|---|---|---|
| stable | wired | stable | 100%–100% | 0 | 0 | 0 | 0 |
| variable | mobile | variable | 20%–100% | 8 | 3 | 0 | 0 |
| interrupt | flaky | interrupt | 20%–100% | 5 | 3 | 0 | 3 |
| switching | wifi | wifi_dies | 100%–100% | 0 | 0 | 0 | 0 |
| switching | cellular | cellular | 20%–100% | 4 | 3 | 0 | 0 |
| hostile | hostile | hostile | 20%–100% | 17 | 19 | 74 | 1 |
| crash_resume | mobile | variable | 20%–100% | 3 | 3 | 0 | 0 |
| stall_detect | dead | wifi_dies | 100%–100% | 0 | 0 | 0 | 0 |
| altered | mobile | variable | 20%–100% | 8 | 10 | 0 | 0 |
| truncated | mobile | variable | 20%–100% | 4 | 7 | 0 | 0 |
| invalid | mobile | variable | 20%–100% | 1 | 2 | 0 | 0 |

## Anomalies reported per uploaded video

- **stable** → `VALID_NO_ANOMALIES` {}
- **variable** → `VALID_WITH_FRAME_ANOMALIES` {"missing_source_frames": 3, "timestamp_gap": 3, "abrupt_change": 2}
- **interrupt** → `VALID_WITH_FRAME_ANOMALIES` {"missing_source_frames": 2, "duplicated_source_frame": 2, "abrupt_change": 1}
- **switching** → `VALID_WITH_FRAME_ANOMALIES` {"timestamp_gap": 1, "abrupt_change": 1}
- **hostile** → `VALID_NO_ANOMALIES` {}
- **crash_resume** → `VALID_NO_ANOMALIES` {}
- **altered** → `DAMAGED_VIDEO` {"decode_error": 2}
- **truncated** → `DAMAGED_VIDEO` {"corrupt_packets": 1, "frame_count_mismatch": 1, "early_end": 1, "decode_error": 1}
- **invalid** → `INVALID_VIDEO` {"unparseable_container": 1}

Per-scenario client attempt logs and strength traces: `evidence/logs/*.json`. Integrity reports: `evidence/reports/*.json|html`.
