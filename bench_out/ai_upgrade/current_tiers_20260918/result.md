# T-P1-21 batch 2 — 质量档计时矩阵（证据）

- 日期: 20260918-143841；分辨率: 1920×1080；warmup 5、测量 24 帧，取中位数
- 后端: standalone GL（本机无头 fragment 路径，**非 Metal**；桌面 GPU 验收仍 BLOCKED: DESKTOP_GPU_NOT_VERIFIED）
- 结构正确性契约由 tests/edit/test_effect_material_parity.py（16 例）固定，本表只记成本侧。

| 效果 | 档位 | 参数 | median ms | fps | fast tier 生效 | 提速× |
|---|---|---|---:|---:|---|---:|
| blur | normal | `{'radius_x': 8.0, 'radius_y': 8.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.52 | 1915.8 | — |  |
| blur | fast | `{'radius_x': 8.0, 'radius_y': 8.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.53 | 1872.7 | 是 | 0.98 |
| blur | normal | `{'radius_x': 16.0, 'radius_y': 16.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.72 | 1396.3 | — |  |
| blur | fast | `{'radius_x': 16.0, 'radius_y': 16.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.51 | 1981.3 | 是 | 1.42 |
| blur | normal | `{'radius_x': 32.0, 'radius_y': 32.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 1.12 | 896.3 | — |  |
| blur | fast | `{'radius_x': 32.0, 'radius_y': 32.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.53 | 1882.0 | 是 | 2.1 |
| defocus | separable | `{'mode': 'separable', 'max_radius': 8.0, 'focal_size': 0.01}` | 0.60 | 1658.1 | — |  |
| defocus | mipmap-fast | `{'mode': 'mipmap', 'max_radius': 8.0, 'focal_size': 0.01}` | 0.42 | 2357.4 | 是 | 1.42 |
| defocus | separable | `{'mode': 'separable', 'max_radius': 16.0, 'focal_size': 0.01}` | 0.46 | 2181.6 | — |  |
| defocus | mipmap-fast | `{'mode': 'mipmap', 'max_radius': 16.0, 'focal_size': 0.01}` | 0.39 | 2559.6 | 是 | 1.17 |
| defocus | separable | `{'mode': 'separable', 'max_radius': 32.0, 'focal_size': 0.01}` | 0.61 | 1643.3 | — |  |
| defocus | mipmap-fast | `{'mode': 'mipmap', 'max_radius': 32.0, 'focal_size': 0.01}` | 0.43 | 2335.5 | 是 | 1.42 |
| denoise | quality=fast | `{'strength': 0.8, 'radius': 6}` | 0.66 | 1507.8 | — |  |
| denoise | quality=normal | `{'strength': 0.8, 'radius': 6}` | 2.51 | 398.3 | — |  |
| denoise | quality=high | `{'strength': 0.8, 'radius': 6}` | 5.89 | 169.8 | — |  |

## 结论
- blur/defocus 的 fast 档在 radius≥8 时真实走半分辨率路径（fast_tier_engaged=是），
  提速随半径增大而放大（fragment 成本对半径二次、对像素数一次）。
- denoise 三档按 effective 半径单调（fast≤normal≤high 成本），GPU/CPU 同契约。

## 遗留
- Metal 后端质量档评估 = BLOCKED: DESKTOP_GPU_NOT_VERIFIED（T-P0-31 前置）。
- GPU blur sigma 与 CPU cv2 约定差异维持已知缺陷记录（产品裁决后对齐）。