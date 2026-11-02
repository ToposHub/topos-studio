# 三后端能力矩阵实测（B12.4）

- 时间：2026-11-02T22:13:08；git `8ce6cf1`
- 平台：macOS-15.3.1-x86_64-i386-64bit（x86_64），Python 3.12.14
- GL：OK（AMD Radeon RX 6800 OpenGL Engine）
- Metal：OK（AMD Radeon RX 6800）
- 颜色上下文：scene-linear HDR（RGBA16F 中转；F01 含 −0.25/16 锚点，F02 含透明边）；参考 = CPU 生产 oracle（N09）
- 门禁：SDR abs ≤ 3.92e-03；HDR rel ≤ 2e-03（计划 §7.1 / G2）

| fixture × 参数域 | CPU | GL | Metal |
|---|---|---|---|
| F01 neutral | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） | PASS（SDR 1.2e-04 / HDR rel 0.0e+00） | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） |
| F01 rich-basic | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） | PASS（SDR 4.9e-04 / HDR rel 8.3e-04） | PASS（SDR 2.4e-04 / HDR rel 3.8e-04） |
| F01 curves | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） | PASS（SDR 2.3e-03 / HDR rel 7.4e-04） | PASS（SDR 2.1e-04 / HDR rel 4.3e-04） |
| F01 serial A→B | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） | PASS（SDR 2.2e-03 / HDR rel 1.3e-03） | PASS（SDR 2.2e-04 / HDR rel 5.5e-04） |
| F02 rich-basic | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） | PASS（SDR 4.6e-04 / HDR rel 7.5e-04） | PASS（SDR 1.6e-04 / HDR rel 4.0e-04） |
| F02 curves | PASS（SDR 0.0e+00 / HDR rel 0.0e+00） | PASS（SDR 9.8e-04 / HDR rel 6.9e-04） | PASS（SDR 2.2e-04 / HDR rel 2.8e-04） |

## 未覆盖（NOT RUN）

- 其他平台（Windows/Linux、Apple Silicon、Intel GPU）未实测，不记 PASS；跨平台 golden 属 B16.3。
- Corrector 越域参数（插件/邻域/HSL 曲线/LUT3D/fx/mixer/warper/soft-clip 曲线/非 Normal 混合）三后端一致 fail-closed（tests/color/test_color_graph_backend_capabilities.py），不进入本表。
- qualifier GPU keyer 的图内生成（B10 债务）未并入，key 边仍为白 matte。

## 能力目录快照

```
op                       gl           cpu          metal       
---------------------------------------------------------------
alpha_output             exact        exact        exact       
combiner                 exact        exact        exact       
compound                 pending      unsupported  unsupported 
corrector                exact        fallback     fallback    
key_mixer                exact        exact        exact       
layer_mixer              exact        exact        exact       
output                   exact        exact        exact       
outside                  exact        fallback     fallback    
parallel_mixer           exact        exact        exact       
shared_corrector         exact        fallback     fallback    
source                   exact        exact        exact       
splitter                 exact        exact        exact       
corrector:param_domains  exact        fallback     fallback    
```
