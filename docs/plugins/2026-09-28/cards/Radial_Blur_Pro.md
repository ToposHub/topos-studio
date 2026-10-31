# 效果卡片：Radial Blur Pro（径向模糊 Pro）

| | |
|---|---|
| plugin_id | `Radial_Blur_Pro`（plugins/Radial_Blur_Pro.tglsl，v1.0） |
| 分类 | 模糊 / 空间采样（变焦径向模糊） |
| 一句话 | 沿可调中心向像素方向做固定步长多点采样平均，产生从中心向外发散的速度感/冲击感模糊 |
| 色彩管线 | scene-linear in / scene-linear out；HDR 安全（内容值不 clamp） |
| 预览成本 | `@preview_cost medium`（播放时交由效果预览质量管理器降级） |
| i18n | `plugins.Radial_Blur_Pro.name` + `.params.<var>`（片段：`../i18n/Radial_Blur_Pro.json`，zh_CN + en_US） |

## 参数

| 参数 | 控件 | 默认 | 说明 |
|---|---|---|---|
| Sample Quality（采样质量） | select | 16 Samples | 8 / 16 / 24 / 32 四档；每档映射着色器内一个编译期常量循环上限（宏 `RADIAL_ACCUMULATE(N)` 字面量分支），非 uniform 上限循环 |
| Blur Strength（模糊强度） | slider 0–1 | 0.35 | 经 `pow(s, 1.5)` ease-in 映射为向中心的采样跨度；0.35 ≈ 21% 跨度 |
| Center X / Y（中心 X/Y） | slider 0–1 | 0.5 / 0.5 | 模糊中心（UV 空间），画面内任意位置 |

## 算法要点

- 采样公式：`sample_j = uv - (uv - center) * (pull * j / N)`，j = 0..N-1，
  等权平均；所有采样点严格落在中心→像素线段内（不出画）。
- UV 安全：显式半 texel 边界 clamp（clamp-to-edge 保险）+
  `0.5 / max(tex_size, vec2(1.0, 1.0))` 分母守护。
- 输出模式：delta 输出（`color + (blurred - original)`），基于 `u_input_tex`
  计算再叠加到已调色 `color`，与上游调色正确叠加。
- 首行早退出：`if (strength < 0.001) return color;`。
- 有意不乘 `u_aspect_ratio`：变焦条纹沿中心→像素射线，仿射 UV↔像素映射
  保持射线为射线；纵横比纠偏仅圆形核需要（决策已注释在着色器内）。

## 性能备注

- **纹理 tap 数 = N**（8/16/24/32，由 select 控制）：默认档 16 tap/像素；
  对比基准 bilateral_skin_smooth 为 32 tap × 两遍 refine。
- 4K（约 800 万像素）60FPS 预算下，32 Samples 档为重度配置（约 2.6 亿次
  纹理读取/帧），已声明 `@preview_cost medium`，播放时可被降级策略关闭。
- 昂贵函数：全函数仅 1 次 `pow`（强度曲线）；无 sin/cos/exp/log。
- 采样档间切换只改 int 分支，四个循环上限均为字面量常量，驱动可展开优化，
  无重新编译/伪循环开销。
- 分辨率无关：`textureSize(u_input_tex, 0)` 实时取分辨率，预览与导出
  （含 4K→8K）效果一致。

## 已验证

- headless 解析验证 37/37 通过（`../tools/validate_Radial_Blur_Pro.py`，
  正则镜像 plugin_manager 实态；7 类人为破坏负向对照全部命中）。
- ModernGL standalone GL 上下文实际编译通过；4 档 × 2 强度 × 离中心点
  数值与解析值最大偏差 ≤ 1.3e-5（float32 精度）；早退与 HDR 负值存活验证。
