# Plan §6.3 codec-only 2K preview decode gate

Decision: **FAIL** (content: grain)

Reference (normal 2K decode, V2 1920×1080): p50 10.555 ms / p95 11.684 ms.

| Source | Target | p50 ms | p95 ms | p50/2K | p95/2K | Gate p50 ≤1.25× | Gate p95 ≤1.50× | Base read | Skipped |
| --- | --- | ---: | ---: | ---: | ---: | :-: | :-: | ---: | ---: |
| 1920×1080 | 1920×1080 | 13.405 | 13.929 | 1.27× | 1.19× | FAIL | PASS | 6489 KB | 0 KB |
| 3840×2160 | 1920×1080 | 13.876 | 14.568 | 1.31× | 1.25× | FAIL | PASS | 6888 KB | 7599 KB |
| 5760×3240 | 1920×1080 | 13.683 | 15.134 | 1.30× | 1.30× | FAIL | PASS | 5764 KB | 19708 KB |
| 7680×4320 | 1920×1080 | 14.537 | 15.370 | 1.38× | 1.32× | FAIL | PASS | 7836 KB | 37983 KB |

Generated: 2026-09-09T15:28:01.093837+00:00
Commit: 5f9be63c981e8a58f9f7de0c21d71d2719d55f6a
