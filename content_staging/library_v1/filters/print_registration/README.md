# F13 print_registration —— CMY 套印错位

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F13 行——Halftone_Print 是单张网屏（4 档角度 select）、无 CMYK
> 分离/套印偏移/纸底。差量三项参数全缺，本项不重复造 dot_pitch（固定 8px）。

## 算法

```
三版 k ∈ {C, M, Y}，网屏角度 0°/15°/30°（角度差 15°，const mat2），pitch = 8px：
q_k  = 像素坐标 + off_k                # C 版 −off、M 版基准 0、Y 版 +off
                                            # off = (reg_offset_x, reg_offset_y) px
src_k = bilinear(源场_k, clamp(q_k))   # Luma：三版同源 Rec.709 luma
                                       # CMY approx：C←R、M←G、Y←B 互补通道
r_k   = sqrt(1 − clamp(src_k, 0, 1))   # 网点半径（clamp 仅几何参数域）
cov_k = 1 − smoothstep(r_k−0.06, r_k+0.06, |fract(scr_k)−0.5|)   # Halftone_Print 几何
paper = mix(color, vec3(1.0), paper_base·ink_amount)
out.R = paper.r·(1 − ink_amount·cov_C) # C 版吸收 R；M→G、Y→B 同理
```

- **工作单公式口径记录**："叠印 = paper_base + Σ版墨量×(1−cov)"按 Halftone_Print
  乘性油墨掩码惯例实现——每版墨量 `ink_amount×cov` 只作用于自己管辖的通道
  （C→R、M→G、Y→B）的乘性吸收，避免加性叠印把纸底加亮；`paper_base` 实现
  为"向白纸的混合量"（仅在 ink>0 时参与，保证 ink_amount=0 恒等不被纸底破坏）。
- `ink_amount=0` 精确恒等（首行早退，任意 paper_base）；平坦黑=全版满墨、
  平坦白=仅网屏胞心小网点（Halftone 几何连续性）；纯静态确定性。
- 3 版各 1 次邻域采样（共 3 tap），零循环（全展开）；UV 钳半纹素。
- **输出域声明**：clamp 仅作用于网点几何参数（超白→全露白、负值→满墨，
  Halftone_Print 同类惯例）；输出内容色不被钳制——HDR 输入按
  `(1−paper_base·ink_amount)` 线性衰减混入纸底（SDR 输入输出 ≤1，
  HDR 按 0.28 系数透传，测试钉住）；恢复原图走 ink_amount=0 旁路。

## 与相邻算法的区分（验收点）

- 非 Halftone_Print（单张网屏无分离/套印）：三版独立采样位 + 独立网屏相位，
  套印偏移改变 Y 版采样源 → 网点大小变化（手算向量 5 + 变体 1 钉住）；
- 非 Pixel_Blocks（均匀阶梯量化，无网屏几何）；
- Luma/CMY approx 两档在彩色输入上可区分（平坦红 R 通道 1.0 vs 0.2，向量 2/3）。

## 交付物

| 文件 | 内容 |
|---|---|
| `print_registration.tglsl` | 单 pass 源码草稿（3 版展开、const 旋转矩阵） |
| `component_manifest.json` | L01 契约 |
| `hand_vectors.json` | 5 主向量 + 3 变体（独立脚本 + 平坦场手算） |
| `references/print_registration_ref.py` | CPU oracle（float64 逐式同构） |

## 验证现状

- CPU oracle ↔ 独立脚本向量一致；真实 parser 解析通过；GLSL 红线静态检查通过
  （零循环；u_* 白名单）。
- **未验**：真实 GPU 编译/像素（L02 纹理邻域子门，ROI padding = |offset|+1 ≤ 5px）；
  高频走样/角度-像素比例（网屏频率 vs 输出分辨率的摩尔纹行为）；
  Metal/CPU 导出。LUT 不可表达（位置+邻域依赖）——@export_support opengl。
  不替代印刷 ICC 流程（近似 CMY 分离，显式声明）。
