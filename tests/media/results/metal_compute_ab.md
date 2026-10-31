# P4-05 Metal compute A/B：大半径效果 + Normalize 百分位

> 2026-09-11T12:37:37  GL `4.1 ATI-6.1.13`  Metal `AMD Radeon RX 6800`

## 大半径模糊（全链路 ms，p95）

| 用例 | Metal compute | GL fragment 基线 | 结论 |
|---|---:|---:|---|
| blur_r32 @1920x1080 | 87.261 | 0.554 | fragment 更快（CPU staging 为主要成本） |
| blur_r64 @1920x1080 | 101.744 | 0.6 | fragment 更快（CPU staging 为主要成本） |
| blur_r32 @3840x2160 | 425.314 | 0.742 | fragment 更快（CPU staging 为主要成本） |
| blur_r64 @3840x2160 | 438.376 | 0.944 | fragment 更快（CPU staging 为主要成本） |

## Normalize 百分位链路（Metal 路径 p95）

| 用例 | p95 ms | 旧路径 |
|---|---:|---|
| normalize_percentile @1920x1080 | 12.396 | silent no-op（返回 (0,1)，§0.4） |
| normalize_percentile @3840x2160 | 28.863 | silent no-op（返回 (0,1)，§0.4） |

## K5 门禁：未触发（全部落地）

- apply_blur_compute：Metal 路径解锁 ✅
- apply_erode_dilate_compute：Metal 路径解锁 ✅（与 blur 共用桥）
- Normalize 百分位：Metal 路径解锁 ✅（①非空直方图 ②CDF/百分位 GPU ③显式诊断）

Metal 路径含 CPU staging 全链路（GL 读回 + 上传 + kernel + 写回）；fragment 基线为最小可分离高斯近似（非 evaluator 完整管线），用于同机同 context 的成本结构对比。
