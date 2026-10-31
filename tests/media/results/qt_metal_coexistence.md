# P1-03 Qt 表面共存 probe

> 2026-09-11T07:48:18.300995  git `10c1456ffa76`

| 检查项 | 结果 |
|---|---|
| GL 与 Metal 同窗各自呈现 | GL=green(GL) / Metal=red(Metal) → 是 |
| Z 序（Metal 压到 GL 区上方） | red(Metal) |
| resize 后 Metal 区域 | red(Metal) (host 宽 400.0) |
| present 节奏 | 62.0 fps @ 屏幕 60.00001525878906 Hz → vsync 锁定 |
| **K3 预判** | **PASS(共存可行)** |
