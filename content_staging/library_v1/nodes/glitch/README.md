# C09 lib1_glitch —— 条带位移/RGB 分离节点（参照 Glitch_Blocks）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C09 行——复用 `plugins/Glitch_Blocks.tglsl`（静态确定性）；
> **动态逐帧模式过 frame/seed 子门后才开**（本批不含）。

## 交付物

- `lib1_glitch_node.py`：Node 类草案（band_height/shift_amount/rgb_split/
  corruption/seed）。
- `references/glitch_ref.py`：CPU oracle（与 Glitch_Blocks.tglsl 逐式同构 +
  seed 进 hash 扩展）。

## 算法 / 恒等语义

- 条带 band = floor((y+0.5)/max(band_height,1))；Dave Hoskins hash12；
  **seed 进 hash**：n1/n2 的条带坐标平移 seed·97（静态确定性，GPU 侧同式
  uniform）；
- hot = 1 当 n1 ≥ 1−corruption；x_shift = (n2−0.5)·shift_amount·0.25·hot
  （UV 域 ±12.5% 画幅宽内）；split_px = rgb_split·12·(0.25+0.75·hot)；
- r/g/b 分别采 x_shift ± split_px 处；采样位钳半纹素 [0.5, W−0.5]，oracle
  钉 NEAREST（LINEAR 差异 L02 声明）。
- **恒等**：corruption=0 且 rgb_split=0 → 精确恒等（任意 band_height/
  shift_amount/seed：n1<1 恒成立 → hot=0 → 位移 0、三通道采样位重合）。
- tile_padding：12px（split 上界）+ 0.125·1920·shift_amount（位移上界，
  width-aware 换算属接线期）。

## 验证现状

- marker 协议/恒等（含 corruption=0+split=0 的滑杆非零组合）/split-only
  手算位移/条带一致性/seed 确定性与差异性——已测
  （tests/content_library_v1/test_c09_glitch.py）。**未验**：L04 接线全家。
