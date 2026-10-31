# Skin Even Matte · 哑光肤色平滑

> 肤色选择性频率分离插件卡片 · `plugins/Skin_Even_Matte.tglsl` · v1.0 · Topos Team

**一句话**：YCbCr 肤色窗口圈出皮肤，肤色门控低通吃掉斑块/泛红，毛孔纹理按比例回加——一步得到哑光均匀的肤色。

| | |
|---|---|
| 类别 | 人像/肤质 · Skin Retouch |
| 色彩空间 | scene-linear 输入/输出（HDR 安全，全程不 clamp 到 [0,1]） |
| 参数 | Radius（半径）· Skin Threshold（肤色检测阈值）· Texture Keep（纹理保留） |
| 性能 | 固定 17 纹理 tap · 零 pow · 零三角函数 · `@preview_cost medium` |
| 兼容 | OpenGL / glsl_fragment · float16 · deterministic |

## 与 bilateral_skin_smooth 的差异定位

两个都是"肤质"插件，算法路径完全不同，适合不同素材与叠加使用：

| | Skin Even Matte（本插件） | bilateral_skin_smooth |
|---|---|---|
| 选择机制 | **色度选择**：YCbCr 肤色窗口 matte，只动肤色，背景/衣物/发丝严格直通 | **无选择**：全画面统一处理，靠边缘权重保结构 |
| 平滑机制 | **频率分离**：肤色门控低通 + 高频差值按 Texture Keep 回加（ blotch 与毛孔分层处理） | **双边滤波**：边缘保持的联合滤波，32 tap 螺旋采样两遍 refine |
| 纹理控制 | 显式比例回加（0 = 全哑光，1 = 直通旁路） | Detail Protection（边缘阈值）+ Detail Recover 两级间接控制 |
| 强度控制 | 由肤色 matte 与 Texture Keep 隐式构成（无独立 strength 滑杆） | 独立 Strength 滑杆 + 感知曲线 |
| 性能特征 | 17 tap / 零 pow / 零 trig | 64 tap（32×2 遍）/ 每 tap pow 转换 |
| 典型场景 | 均匀肤色质感、压油光做哑光妆面、泛红/斑块淡化 | 通用磨皮、需要 strength 精细收放时 |

**叠加建议**：先用 Skin Even Matte 做肤色均匀底，再用低强度 bilateral_skin_smooth 补局部磨皮；两者 delta 输出模式保证与上游调色正确叠加。

## 参数速查

| 参数 | 默认 | 范围 | 效果 |
|---|---|---|---|
| Radius 半径 | 0.4 | 0.0–1.0 | 低通半径（图像宽度 0.3%–1.5%，4K 第 1 环约 12–58 px）；加大吃掉更大尺度的斑块 |
| Skin Threshold 肤色检测阈值 | 0.7 | 0.0–1.0 | 肤色窗口宽窄（经典窗口半宽的 0.4–1.0 倍）；偏亮/偏灰的油光肤色调高，误选木色/沙色调低 |
| Texture Keep 纹理保留 | 0.5 | 0.0–1.0 | 高频回加比例；0 = 全哑光（重度磨平），1 = 完全直通（近零开销旁路） |

## 使用建议

- **通用哑光肤**：默认组合（0.4 / 0.7 / 0.5）即"可见但不过火"；想要更重的哑光妆面把 Texture Keep 降到 0.25–0.35。
- **泛红/腮红不均**：Radius 0.5–0.7，Texture Keep 0.4；红肿色度仍落在 Cr 窗口内，会被低通均化。
- **油光/高光溢出肤色**（偏亮偏去饱和）：Skin Threshold 提到 0.85–1.0 把窗口放宽。
- **误伤木门/沙滩等暖色背景**：Skin Threshold 降到 0.3–0.5 收紧窗口。
- 非肤色区域（蓝天的 Cb、绿植与灰墙的 Cr 均在窗外）严格直通，无需额外遮罩。

## 技术要点

- 肤色检测在 BT.601 YCbCr 色度平面上用**软边窗口**（smoothstep 羽化约 5 个 8 位码值），无硬阈值抖动；经典公开肤色区间（Cb 77–127 / Cr 133–173，8 位）的近似。
- 低通是**肤色门控**的：每个采样按自身肤色隶属度加权，发丝/背景色度出局不污染均值——这是与双边滤波完全不同的边界保护思路。
- 固定 17 tap（中心 + 8×2 环），第 2 环用 const 22.5° 旋转矩阵错开相位，16 tap 得 16 个方向；循环上限为 `const int`（红线 #2），半径只改步长不改 tap 数。
- 全程在纹理原生编码域运算（HighPass_Retouch 频率分离同款惯例），**零 pow、零三角函数**；唯一的 clamp 是 ±0.45 的效果增量限幅（matte 边界防 halo），不触碰基色。
- 相关规格/验证：`docs/plugins/2026-09-28/specs/Skin_Even_Matte.md`。
