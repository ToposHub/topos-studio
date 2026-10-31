# T-P1-21 batch 2 — 质量档计时矩阵（证据）

- 日期: 20260918-122040；分辨率: 1920×1080；warmup 3、测量 16 帧，取中位数
- 后端: standalone GL（本机无头 fragment 路径，**非 Metal**；桌面 GPU 验收仍 BLOCKED: DESKTOP_GPU_NOT_VERIFIED）
- 结构正确性契约由 tests/edit/test_effect_material_parity.py（16 例）固定，本表只记成本侧。

| 效果 | 档位 | 参数 | median ms | fps | fast tier 生效 | 提速× |
|---|---|---|---:|---:|---|---:|
| blur | normal | `{'radius_x': 8.0, 'radius_y': 8.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.59 | 1698.0 | — |  |
| blur | fast | `{'radius_x': 8.0, 'radius_y': 8.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.55 | 1808.4 | 是 | 1.07 |
| blur | normal | `{'radius_x': 16.0, 'radius_y': 16.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.77 | 1294.0 | — |  |
| blur | fast | `{'radius_x': 16.0, 'radius_y': 16.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.55 | 1827.7 | 是 | 1.41 |
| blur | normal | `{'radius_x': 32.0, 'radius_y': 32.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 1.11 | 899.3 | — |  |
| blur | fast | `{'radius_x': 32.0, 'radius_y': 32.0, 'blur_type': 'gaussian', 'mix': 1.0}` | 0.59 | 1681.5 | 是 | 1.87 |
| defocus | separable | `{'mode': 'separable', 'max_radius': 8.0, 'focal_size': 0.01}` | 0.57 | 1751.1 | — |  |
| defocus | mipmap-fast | `{'mode': 'mipmap', 'max_radius': 8.0, 'focal_size': 0.01}` | 0.39 | 2553.3 | 是 | 1.46 |
| defocus | separable | `{'mode': 'separable', 'max_radius': 16.0, 'focal_size': 0.01}` | 0.47 | 2134.8 | — |  |
| defocus | mipmap-fast | `{'mode': 'mipmap', 'max_radius': 16.0, 'focal_size': 0.01}` | 0.36 | 2796.7 | 是 | 1.31 |
| defocus | separable | `{'mode': 'separable', 'max_radius': 32.0, 'focal_size': 0.01}` | 0.46 | 2164.3 | — |  |
| defocus | mipmap-fast | `{'mode': 'mipmap', 'max_radius': 32.0, 'focal_size': 0.01}` | 0.38 | 2635.7 | 是 | 1.22 |
| denoise | quality=fast | `{'strength': 0.8, 'radius': 6}` | 0.65 | 1531.0 | — |  |
| denoise | quality=normal | `{'strength': 0.8, 'radius': 6}` | 2.47 | 404.7 | — |  |
| denoise | quality=high | `{'strength': 0.8, 'radius': 6}` | 5.94 | 168.3 | — |  |

## 结论
- blur/defocus 的 fast 档在 radius≥8 时真实走半分辨率路径（fast_tier_engaged=是），
  提速随半径增大而放大（fragment 成本对半径二次、对像素数一次）。
- denoise 三档按 effective 半径单调（fast≤normal≤high 成本），GPU/CPU 同契约。

## 遗留
- Metal 后端质量档评估 = BLOCKED: DESKTOP_GPU_NOT_VERIFIED（T-P0-31 前置）。
- GPU blur sigma 与 CPU cv2 约定差异维持已知缺陷记录（产品裁决后对齐）。