# C14 lib1_tile_array —— 瓦片阵列节点

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C14 行——crop 的 outside=repeat 只是裁剪外填充、非阵列
> 平铺，**新算法差量**（不是 Crop 边界 repeat 换名）。
> 计划工作单 §5：repeat/array count、spacing、offset、layout bounds；
> 验收点：明确输出 bbox 和空隙 alpha。

## 交付物

- `lib1_tile_array_node.py`：Node 类草案（count_x/count_y/spacing/
  offset_x/offset_y）。
- `references/tile_array_ref.py`：CPU oracle（新算法，无可复用核）。

## 算法 / 恒等语义

- 格胞：tw = W/count_x、th = H/count_y；格胞 (i,j) = floor(x/tw)
  （钳 [0, count−1]）；内容盒 cw = tw·(1−spacing)、格胞内四边对称
  边距 tw·spacing/2；
- 内容盒内 u_c ∈ [0,1) → 源采样 u_src = frac(u_c + offset_x)（环绕
  repeat 正模；offset 负值同样取正模）→ **texel-block 最近邻**索引
  clip(floor(u_src·W), 0, W−1)（纹素 i 覆盖 [i/W, (i+1)/W)）；
- **空隙（验收点）**：u_c/v_c ∉ [0,1) → RGB=0、**alpha=0**（straight；
  HDR 内容仅存在于瓦片内）——空隙不是黑边而是透明；
- **layout bounds（验收点）**：阵列布局在输出画幅内完成，输出 bbox =
  输入画幅（compute_output_bbox 透传；接线期与 BBox 契约由集成者
  细化）；
- **恒等**：count 1×1 + spacing=0 + offset=0 → 精确恒等（任意 HDR/
  alpha；texel-block 口径下 u_c=x/W → floor(x)=x）；spacing=0 时内容
  盒=格胞（无空隙、无缝拼接）；
- tile_padding = 0：环绕采样永在输入画幅内（同画幅重采样，非邻域
  越界）。

## 参数命名（AUD-0832）

count_x / count_y / spacing / offset_x / offset_y 均不在
`shader_compiler._GLSL_RESERVED_IDENTIFIERS` 黑名单（已查
`src/features/color/core/shader_compiler.py`；无同类 plugins/.tglsl 撞名）。

## 验证现状

- marker 协议/恒等（1×1+0+0）/2×1 阵列手算/spacing 空隙 alpha=0 手算/
  offset 环绕相位/count 钳制防御/参数有效性/manifest 一致——已测
  （tests/content_library_v1/test_c14_tile_array.py）。
  **未验**：L04 接线全家（无多输入/多输出/全帧子门涉及）。
