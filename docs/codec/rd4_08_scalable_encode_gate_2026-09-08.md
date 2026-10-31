# RD4-08 scalable encode gate

Decision: **BLOCKED**

BLOCKED: V7-B reference writer is measurable, but final joint quality budgeting and the size/speed limits are not yet passed; keep default scalable rollout disabled.

The native runner compares the V2 full-frame encoder with the complete V7-B reference packet (directory, ≤2K base, and enhancement residual) on identical deterministic input. The residual is lossless against the fixed-QP base prediction; final joint quality budgeting remains pending.

## Final gate

| Gate | Limit | Status | Reason |
| --- | ---: | --- | --- |
| Total scalable file size median | ≤ +1% | MEASURED | final quality gate pending |
| Total scalable file size p95 | ≤ +3% | MEASURED | final quality gate pending |
| scalable-fast encode p50 | ≤ +5% | MEASURED | final quality gate pending |
| scalable-fast encode p95 | ≤ +8% | MEASURED | final quality gate pending |

## V7-B reference measurements

| Source | Base | V7-B bytes / V2 bytes | V2 p50 ms | V7-B p50 ms | Encode delta |
| --- | --- | ---: | ---: | ---: | ---: |
| 1920×1080 | 1920×1080 | 922,082 / 921,721 (0.039%) | 2.415 | 27.655 | 1062.277% |
| 3840×2160 | 2048×1152 | 5,375,195 / 3,694,451 (45.494%) | 8.535 | 172.133 | 1916.790% |
| 6144×3456 | 2048×1152 | 6,695,159 / 7,541,871 (-11.227%) | 14.500 | 320.135 | 2088.738% |

Generated: 2026-09-08T15:58:05.258969+00:00
Commit: e9cd0f8f6258ba10a6f5a3e86becfb192d75583a
