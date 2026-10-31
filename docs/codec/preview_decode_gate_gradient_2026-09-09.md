# Plan §6.3 codec-only 2K preview decode gate

Decision: **FAIL** (content: gradient)

Reference (normal 2K decode, V2 1920×1080): p50 2.248 ms / p95 2.611 ms.

| Source | Target | p50 ms | p95 ms | p50/2K | p95/2K | Gate p50 ≤1.25× | Gate p95 ≤1.50× | Base read | Skipped |
| --- | --- | ---: | ---: | ---: | ---: | :-: | :-: | ---: | ---: |
| 1920×1080 | 1920×1080 | 2.649 | 2.993 | 1.18× | 1.15× | PASS | PASS | 900 KB | 0 KB |
| 3840×2160 | 1920×1080 | 3.774 | 4.014 | 1.68× | 1.54× | FAIL | FAIL | 1457 KB | 740 KB |
| 5760×3240 | 1920×1080 | 2.619 | 2.895 | 1.16× | 1.11× | PASS | PASS | 934 KB | 707 KB |
| 7680×4320 | 1920×1080 | 3.827 | 4.094 | 1.70× | 1.57× | FAIL | FAIL | 1503 KB | 3364 KB |

Generated: 2026-09-09T15:27:50.627715+00:00
Commit: 5f9be63c981e8a58f9f7de0c21d71d2719d55f6a
