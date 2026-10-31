# C21 lib1_frequency_separation —— low/high 双彩色输出节点（**多输出**）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C21 行——无双彩色输出节点（mask_merge 的 result+matte
> 是彩色+遮罩先例，非 low/high 双彩色）；HighPass_Retouch.tglsl 是单输入
> 高通对照（无重建语义、无双输出）；**首个多输出子门**。
> 计划工作单 §5：low/high 两输出；radius、filter、reconstruct mode；
> 验收点：low＋high 在声明域重建输入、**high 允许负值**、不是 Sharpen
> 预设。

## ⚠️ 多输出子门声明

**多输出子门未开，生产接线待集成者开子门（首个双彩色输出）。**
节点 outputs = [low, high] 两个 image 端口；marker 占位如实返回**两个**
TextureResult 占位（texture_id=-1）+ `_effect_type`/`_effect_input`/
`_effect_params`；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。

## 交付物

- `lib1_frequency_separation_node.py`：Node 类草案（radius/filter/mode；
  双输出端口 low/high）。
- `references/frequency_separation_ref.py`：CPU oracle（新算法，无可
  复用核；**双输出签名扩展**：`evaluate(image, params) →
  {"low": ndarray, "high": ndarray}`——convention #7 单返回的显式双
  输出扩展，已在本 README 与 manifest verification 记录）。

## 算法 / 恒等语义

- low = RGB 可分离低通（Box：窗半宽 k=round(radius)、权重 1/(2k+1)；
  Gaussian：σ=max(radius/3, 1e-3)、半宽 k=ceil(3σ)、全窗归一化）；
  索引钳 [0, n−1]（边缘纹素重复计入分母——clamp 口径）；
- **high = RGB − low，带符号允许负值、不 clamp**（验收点）；
- **重建（验收点）**：low+high = 输入（float32 声明域 ~1e-6 相对；
  测试钉住）——这是与 Sharpen 预设的语义分界：本节点无损分解，
  Sharpen 是破坏性卷积；
- **mode**：LowHigh → low/high 端口各带频带；Reconstruct → low 端口
  输出 low+high（声明域=输入，round-trip 验证模式）、high 端口仍输出
  high（无损，可再消费）；
- α 透传（随 low 输出）；**high 的 α = 0**（频率分离语义：retouch
  消费者只改 low/high RGB，不携带 matte）；
- **恒等锚**：radius=0 → low=RGB、high=0 精确（任意 filter/mode，含
  HDR）；常数图 → high ≈ 0（float64 两趟求和噪声 ~1e-16）；clamp 边界
  下 mean(high) 不保证为 0（边缘纹素重复计入的 DC 偏置，如实记录）；
- tile_padding = ceil(radius)+1（核半宽上界有限——本项无全帧问题）。

## 参数命名（AUD-0832）

radius / filter / mode 均不在
`shader_compiler._GLSL_RESERVED_IDENTIFIERS` 黑名单（已查
`src/features/color/core/shader_compiler.py`；无同类 plugins/.tglsl
撞名——HighPass_Retouch 的参数为另一种口径）。

## 验证现状

- marker 双输出占位协议/恒等锚（radius=0、常数图）/Box 手算（1×5 ramp
  逐像素分数）/Gaussian 独立权重重推/high 负值不 clamp/重建精确/α 透传
  与 high α=0/Reconstruct 模式/manifest 一致——已测
  （tests/content_library_v1/test_c21_frequency_separation.py）。
  **未验**：L04 接线全家 + 多输出子门（集成者）。
