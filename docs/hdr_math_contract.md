# Topos Color - HDR 数学契约

日期：2026-05-24

本文档定义 Topos Color 调色工具链对 HDR 数据的处理规范，确保实时预览与 LUT Exporter 数值一致。

---

## 1. HDR 输入定义

Topos Color 支持以下 HDR 输入：

| 来源 | 数据类型 | 值域范围 |
|-----|---------|---------|
| RAW 文件 | uint16 → float32 | 0.0 - 4.0+ (取决于曝光) |
| Log 视频 (LogC, V-Log, S-Log) | uint8/10 → float32 | 0.0 - 1.0 (映射后可扩展到 1.5+) |
| EXR/HDR 图像 | float16/float32 | 无限制 (理论上支持 -∞ 到 +∞) |
| 普通 sRGB 图像 | uint8 → float32 | 0.0 - 1.0 (SDR) |

**关键约束**：
- 内部处理使用 **float32** (GPU f4 texture 或 CPU numpy float32)
- 不在 pipeline 中间环节 clamp 到 0-1（仅 Soft Clip 可选择性压缩）
- 显示输出时才应用 tone mapping 或 gamma

---

## 2. 工具 HDR 处理规范

### 2.1 Grading (Lift/Gamma/Gain/Exposure/Contrast)

**输入约束**：接受负值和 HDR (>1.0)

**输出约束**：不 clamp，保留负值和 HDR

**关键处理**：

1. **Exposure** (Line 28-30):
   - `color *= pow(2.0, exp_stops)` - 纯线性扩展
   - HDR (>1.0) → HDR (更亮)，负值 → 更负

2. **Contrast** (Line 39-45):
   - `color = sign(color) * pow(abs(color) / 0.18, vec3(c_factor)) * 0.18`
   - 使用 sign/abs 让 S 曲线延展到负数空间
   - pivot = 0.18 (mid-grey)，支持 HDR 和负值

3. **Gamma** (Line 51-68):
   - `[0, 1]` → 应用 pow gamma
   - `>1.0` → 线性外推 `gm[i] * (x - 1.0) + 1.0`
   - `<0` → 线性外推 `gm[i] * x`
   - 确保 HDR 和负值可逆

4. **Lift/Gain** (Line 48-49):
   - `color = color * gain * (1.0 - l) + l`
   - 可产生负值输出（当 lift > gain 且 color 小）

**暗部保护** (Line 70-91):
- 当 luma < 0 且通道极端负值 (<-0.5)，进行软性压缩
- 防止 Gamma 线性外推导致的 RGB 通道失衡

---

### 2.2 Soft Clip

**输入约束**：接受负值和 HDR (>1.0)

**输出约束**：可选择性压缩 HDR 和负值到目标范围

**关键处理**：

1. **Toe (暗部压缩)** (Line 859-873):
   - `c < low` → 压缩到 `[black_point, low]`
   - 负值被"拽"回黑场以上
   - 公式: `B + range * pow(1.0 + k * diff / range, -1.0 / k)`

2. **Shoulder (高光压缩)** (Line 891-908):
   - `max_c > high` → 比例缩放到 `[high, white_point]`
   - **关键**：使用 scale factor (`res *= scale`) 保持 RGB 比例，防止饱和度变化
   - 公式: `mapped_c = W - range * pow(1.0 + k * diff / range, -1.0 / k)`

**通道平衡保护** (Line 875-889):
- Toe 压缩后检查 RGB 通道差异
- `max - min > 0.2` 且 `min < low` → 向 luma 平衡

---

### 2.3 Curves (RGB Curves)

**输入约束**：接受 0.0 - 1.0 (SDR)

**输出约束**：输出 0.0 - 1.0 (SDR)

**关键处理** (Line 185-202):
- 使用 LUT texture 进行映射
- 输入 `>1.0` 或 `<0` 会被 clamp 到 LUT 边界
- **约束**：Curves 不支持 HDR 输入，需先用 Soft Clip 压缩到 SDR 范围

---

### 2.4 HSL Curves

**输入约束**：接受 0.0 - 1.0 (SDR)

**输出约束**：输出 0.0 - 1.0 (SDR)

**关键处理** (HSL_CURVE_SNIPPET):
- 先转换为 HSL/HSV
- Saturation 和 Luminance 乘法调整
- **约束**：HSL Curves 不支持 HDR 输入

---

### 2.5 Mixer (Hue/Sat/Value Shift)

**输入约束**：接受负值和 HDR (>1.0)

**输出约束**：保留 HDR 和负值

**关键处理** (Line 574-590):
- `sign_c = sign(color)` → 提取符号
- `hsv = rgb2hsv(abs(color))` → 对绝对值转换 HSV
- 处理 hue/sat/value
- `return sign_c * hsv2rgb(hsv)` → 恢复符号

**HDR 保护**：
- Mixer 在 HSV 空间工作，Value 可超过 1.0
- Saturation 调整可能影响 HDR 高光色彩平衡
- Hue shift 不改变 Value，保留 HDR 高光强度

---

### 2.6 LUT 3D

**输入约束**：接受负值和 HDR (>1.0)

**输出约束**：SDR 部分 (0-1) 经 LUT，HDR/负值保留

**关键处理** (Line 593-612):
- `sdr_part = clamp(color, 0.0, 1.0)` → 提取 SDR 部分
- `lut_result = texture(lut_tex, sdr_part)` → LUT 映射
- `blended_sdr = mix(sdr_part, lut_result, intensity)` → 混合

**HDR 保留** (Line 599-606):
- `max_orig > 1.0` → 比例缩放 `blended_sdr *= (max_orig / max_sdr)`
- **关键**：保持 HDR 高光的色彩比例，不丢失饱和度

**负值保留** (Line 608-610):
- `negative_excess = color - clamp(color, 0.0, 99999.0)`
- `return blended_sdr + negative_excess`

---

### 2.7 Qualifier (Keyer)

**输入约束**：接受 HDR (>1.0)

**输出约束**：Matte 值域 0.0 - 1.0

**关键处理** (keyer.glsl Line 31-53):
- `v = min(c_max, 1.0)` → HSV Value 被 clamp 到 1.0
- **约束**：HDR 高光 (>1.0) 的 Value 被视为 1.0，可被 `lum_high=1.0` 选择

**HDR 处理**：
- Hue 和 Saturation 正常处理（不受 HDR 影响）
- Value 超过 1.0 被视为 1.0（UI 参数范围 0-1）

---

## 3. 工具链顺序

Topos Color 的标准工具链顺序：

```
Source Image
  ↓
RAW Development (WB, Exposure, Decode)
  ↓
Blur/Sharpen
  ↓
Grading (Lift/Gamma/Gain/Contrast/Saturation)
  ↓
Curves (RGB)
  ↓
HSL Curves
  ↓
Soft Clip (Toe/Shoulder)
  ↓
Mixer (Hue/Sat/Value)
  ↓
Warper
  ↓
LUT 3D
  ↓
Plugins
  ↓
Output
```

**HDR 数据流**：
1. Source → RAW → Grading: **全 HDR** (0.0 - 4.0+ 或更宽)
2. Grading → Curves/HSL: **需要 Soft Clip 压缩**（如果 Curves/HSL 需要生效）
3. Soft Clip → Mixer → LUT: **HDR 可保留**（Soft Clip 可选择性压缩）
4. Output: **取决于显示或导出格式**

---

## 4. LUT Exporter 与实时 Shader 一致性

LUT Exporter 的 CPU 实现必须与 GLSL shader 保持数值一致。

### 4.1 关键差异

| 工具 | GPU Shader | CPU Exporter | 一致性 |
|-----|-----------|-------------|--------|
| Grading | shader_nodes.py | lut_exporter.py `_apply_grading_cpu` | 需验证 |
| Soft Clip | shader_nodes.py | lut_exporter.py `_apply_soft_clip_cpu` | 需验证 |
| Mixer | shader_nodes.py | lut_exporter.py `_apply_mixer_cpu` | 需验证 |
| Curves | LUT texture | lut_exporter.py `_apply_curves_cpu` | 需验证 |

### 4.2 验证方法

1. **生成测试输入**：
   - SDR ramp: `[0.0, 0.1, 0.2, ..., 1.0]`
   - HDR ramp: `[1.0, 1.5, 2.0, 3.0, 4.0]`
   - Negative ramp: `[-0.5, -0.2, -0.1]`
   - Hue wheel: `[hue=0..360, sat=0.5, val=0.5]`
   - Saturation grid: `[hue=0, sat=0..1, val=0..1]`

2. **对比 GPU 和 CPU 输出**：
   - 平均误差 < 1e-4
   - 最大误差 < 1e-3
   - HDR 边界误差 < 1e-2 (由于 float16 vs float32)

3. **黄金输出文件**：
   - `tests/fixtures/hdr_golden_outputs.json`
   - 包含每个工具在固定输入下的期望输出

---

## 5. 允许误差范围

| 数据类型 | GPU 内部精度 | CPU 精度 | 允许误差 |
|---------|------------|---------|---------|
| SDR (0-1) | float32 (f4) | float32 | < 1e-4 |
| HDR (>1) | float16/32 (f2/f4) | float32 | < 1e-2 |
| Negative | float32 | float32 | < 1e-3 |
| Matte | float16 (f2) | float32 | < 1e-2 |

**误差来源**：
- GPU texture interpolation (线性插值误差)
- float16 vs float32 精度差异
- pow/log/sqrt 等运算的 GPU/CPU 实现差异

---

## 6. 未来扩展

### 6.1 ACES 支持

当前 HDR 处理基于"scene-referred"线性空间。未来支持 ACES 需：
- 输入转换：IDT (Input Device Transform)
- 工作空间：ACEScg 或 ACEScct
- 输出转换：ODT (Output Device Transform)

### 6.2 HDR Display Output

当前显示输出假设 SDR monitor (gamma 2.2/2.4)。未来支持 HDR display 需：
- ST.2084 PQ curve
- Rec.2100 HLG curve
- HDR10 metadata

---

## 7. 版本历史

- 2026-05-24: Phase 2 后更新，添加 Qualifier matte 统一说明
- 2026-05-24: 初始版本，定义工具 HDR 处理规范