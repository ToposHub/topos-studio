# 效果卡片：Prism Aberration（棱镜色散）

| | |
|---|---|
| plugin_id | `Prism_Aberration`（plugins/Prism_Aberration.tglsl，v1.0） |
| 分类 | 空间采样（径向 RGB 色散，分离轴可旋转） |
| 一句话 | R/B 通道沿"径向 + 可旋转"轴对称偏移重采样、G 通道原位参考，偏移量随离中心距离与边缘衰减曲线增长 |
| 色彩管线 | scene-linear in / scene-linear out；HDR 安全（内容值不 clamp） |
| 预览成本 | `@preview_cost low`（3 tap/像素，最轻的空间采样类插件） |
| i18n | `plugins.Prism_Aberration.name` + `.params.<var>`（片段：`../i18n/Prism_Aberration.json`，zh_CN + en_US） |

## 参数

| 参数 | 控件 | 默认 | 说明 |
|---|---|---|---|
| Dispersion Strength（色散强度） | slider 0–1 | 0.35 | 经 `pow(s, 1.5)` ease-in 映射为通道偏移上限（较长边的 1.5% 封顶）；默认 ≈ 远角 12px @4K；0 时插件旁通 |
| Rotation (Degrees)（旋转角度） | slider -180–180 | 0 | 分离轴旋转：0° 经典径向色散、90° 切向扭转、180° 红/蓝镶边极性互换 |
| Edge Falloff（边缘衰减） | slider 0–1 | 0.5 | 偏移随离中心距离的增长曲线 `dist01^mix(0,4,falloff)`：0 全画面均匀、1 集中于最外角、默认 0.5 ≈ 距离平方 |

## 算法要点

- 采样结构：`dispersed = (tex(uv+off).r, tex(uv).g, tex(uv-off).b)`——G 原位
  参考，R/B 沿轴对称分离；中心固定画面中心（参数面按规格保持 3 项）。
- 纵横比纠偏：径向距离、旋转、偏移量全部在 `uv.x * u_aspect_ratio` 的像素
  比例空间计算（1 单位 = 画面高度），距离为真圆、角度为真几何角，采样前
  除回 UV 单位。
- 距离归一：`dist01 = dist / length(半对角线)`，构造性 ≤ 1.0（无需 clamp）；
  `pow` 基数 `max(dist01, 0.0001)` 守护防 `pow(0, 0)`（falloff=0 时指数为 0）。
- UV 安全：显式半 texel 边界 clamp（clamp-to-edge 保险）+
  `0.5 / max(tex_size, vec2(1.0, 1.0))` 分母守护；中心退化
  `radial / max(dist, 0.0001)`。
- 输出模式：delta 输出（`color + (dispersed - original)`），基于 `u_input_tex`
  计算再叠加到已调色 `color`，与上游调色正确叠加。
- 首行早退出：`if (separation < 0.001) return color;`。

## 性能备注

- **纹理 tap 数 = 3**（参考 + R + B）/像素；对比基准：Radial_Blur_Pro 默认
  16 tap、bilateral_skin_smooth 32 tap × 两遍 refine。
- 每像素超越函数精确配额：sin×1、cos×1（旋转必需）、pow×2（衰减曲线 +
  强度 ease-in）、exp/log×0；无循环，无分支发散（除早退）。
- 4K（约 800 万像素）60FPS 预算下余量充足，声明 `@preview_cost low`。
- 分辨率无关：`textureSize(u_input_tex, 0)` 实时取分辨率，偏移按较长边比例
  （1.5% 封顶），预览与导出（含 4K→8K）效果一致。

## 已验证

- headless 解析验证 67/67 通过（`../tools/validate_Prism_Aberration.py`，
  经 `PluginManager._parse_plugin_file` 真实解析路径；8 类人为破坏负向对照
  全部命中：裸整数、内容 clamp(0,1)、未声明 `u_*`、uniform 循环上限、
  Label 去引号、缺包裹行、移除 UV clamp、第 4 个纹理 tap）。
- ModernGL standalone GL 上下文实际编译通过；24 组参数（2 强度 × 4 角度 ×
  3 衰减）× 6 像素中心数值对照：NEAREST 控制 5.76e-07（纯 float32 舍入，
  逻辑精确一致），LINEAR 最大 1.55e-04（≈1.3e-05 相对，GPU float32 双线性
  权重硬件精度上限）；早退（separation=0）输出与调色原值一致（9.28e-07），
  HDR 负值调色经 delta 输出无 clamp 存活（最小 -2.778）。
