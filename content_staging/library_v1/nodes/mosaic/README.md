# C05 lib1_mosaic —— 像素块/量化节点（参照 Pixel_Blocks）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C05 行——无 mosaic 节点；复用 `plugins/Pixel_Blocks.tglsl`
> 块中心采样；差量：grid origin/mask/border/非整块边缘未纳入（本批最小口径）。

## 交付物

- `lib1_mosaic_node.py`：Node 类草案（block_px/levels/dither）。
- `references/mosaic_ref.py`：CPU oracle（与 Pixel_Blocks.tglsl 逐式同构）。

## 算法 / 恒等语义

- 马赛克：bid = floor(px/block)（px 为像素中心坐标 x+0.5），采样位=块几何
  中心 bid·block + block/2；oracle 钉 **NEAREST**（texel = floor(采样位)）；
  GPU 输入若 LINEAR 滤波，偶数块取相邻 texel 均值——对拍差异 L02 声明。
- 量化：q = (floor(c·L + d) + 0.5)/L（scene-linear 均匀阶梯，单调无界，
  HDR 外推不 clamp）；d = Bayer 4x4 相位 (b+0.5)/16 − 0.5。
- **恒等（近似，显式声明）**：量化阶宽 1/L——block_px=1 + dither=off +
  levels=32 时 |out−in| ≤ 1/64；**levels→∞ 才精确恒等**，参数域 levels
  ≤32（对齐 .tglsl 上限），故 neutral 声明为 levels=32 近似恒等。
- tile_padding = ceil(block/2)（块中心采样距块内像素最大距离上界）。

## 验证现状

- marker 协议/近似恒等界/块一致性/量化锚点/Bayer 相位/HDR 外推——已测
  （tests/content_library_v1/test_c05_mosaic.py）。**未验**：L04 接线全家。
