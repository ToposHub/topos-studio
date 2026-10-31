# P1-05 render-only A/B：Metal vs OpenGL

> 2026-09-11T07:42:56.266518  git `890ae5102848`  macOS-15.3.1-x86_64-i386-64bit

| 尺寸 | 格式 | OpenGL ms/f | Metal ms/f | Metal 加速 | K1 (≥15%) |
|---|---|---:|---:|---:|---|
| 1920x1080 | bgra8 | 0.2561 | 0.0436 | +83.0% | PASS |
| 1920x1080 | rgba16f | 0.3120 | 0.0480 | +84.6% | PASS |
| 3840x2160 | bgra8 | 0.9550 | 0.1766 | +81.5% | PASS |
| 3840x2160 | rgba16f | 1.3045 | 0.1964 | +84.9% | PASS |

归因：render-only（GPU-resident 输入，无上传/解码），Metal 计时取 commandBuffer GPU 时间，OpenGL 取 timer query。K1 中止条件（计划 §1.4）以 render-only 加速 <15% 为未达门槛。
