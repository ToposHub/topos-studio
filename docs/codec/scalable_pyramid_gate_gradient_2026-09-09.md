# RD4-08 scalable encode gate

Decision: **BLOCKED**

BLOCKED: V7-B writer is measurable, but final joint quality budgeting and the size/speed limits are not yet passed; keep default scalable rollout disabled.

The native runner compares the V2 full-frame encoder with the complete V7-B packet on identical deterministic input (writer=pyramid, content=gradient; rice = minor-2 base + per-pixel quantized residual, pyramid = ADR-C030 minor-4 shared pyramid with lossless detail segments). Both outputs are decoded to measure luma PSNR; multi-content quality budgeting uses the P0-03 frozen corpus.

## Final gate

| Gate | Limit | Status | Reason |
| --- | ---: | --- | --- |
| Total scalable file size median | ≤ +1% | MEASURED | final quality gate pending |
| Total scalable file size p95 | ≤ +3% | MEASURED | final quality gate pending |
| scalable-fast encode p50 | ≤ +5% | MEASURED | final quality gate pending |
| scalable-fast encode p95 | ≤ +8% | MEASURED | final quality gate pending |

## V7-B measurements

Table: **main**, writer: **pyramid**, content: **gradient** (ratio = source/preview target)

| Source | Base | Preview target | Ratio | V7-B bytes / V2 bytes | V2 / V7-B PSNR-Y | V2 p50 ms | V7-B p50 ms | Encode delta |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1920×1080 | 1920×1080 | 1920×1080 | 1/1 | 922,082 / 921,721 (0.039%) | 99.00 / 99.00 | 2.634 | 23.070 | 775.854% |
| 3840×2160 | 1920×1080 | 1920×1080 | 2/1 | 2,249,787 / 3,694,451 (-39.104%) | 99.00 / 99.00 | 37.452 | 267.566 | 614.424% |
| 5760×3240 | 1920×1080 | 1920×1080 | 3/1 | 1,680,637 / 6,962,564 (-75.862%) | 99.00 / 99.00 | 39.126 | 530.689 | 1256.359% |

Generated: 2026-09-09T15:29:53.455700+00:00
Commit: 5f9be63c981e8a58f9f7de0c21d71d2719d55f6a
