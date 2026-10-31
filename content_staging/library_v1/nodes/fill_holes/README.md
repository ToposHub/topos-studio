# C19 lib1_fill_holes —— matte 孔洞填充节点（**全帧声明**）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C19 行——matte_cleanup 仅 erode/dilate/blur/clamp/
> invert/levels、无孔洞填充，**新算法差量**；**全帧算法预算/取消**。
> 计划工作单 §5：matte 孔洞填充；connectivity、max area、threshold、
> output mode；验收点：连通区域/面积语义；**触边背景不误填**，
> 全帧算法预算/取消。

## ⚠️ 全帧声明子门

**全帧声明子门未开：本组件按 full-frame 声明——连通域标注需整帧
两遍扫描（BFS 标注 + 触边/面积判定），无法证明分块支持；生产接线待
集成者开全帧预算/取消子门，不宣称 tile-safe。**（`_tile_padding`
返回对角线上界 2204 @1920×1080 仅数值口径统一。）

## 交付物

- `lib1_fill_holes_node.py`：Node 类草案（connectivity/max_area/
  threshold/output_mode）。
- `references/fill_holes_ref.py`：CPU oracle（新算法，无可复用核；
  BFS 确定性标注，无 scipy 依赖）。

## 算法 / 恒等语义

- FG = alpha ≥ threshold（HDR α 合法）；BG = 其余；
- BG 连通域标注（connectivity=4/8，BFS 行主序确定性种子遍历）；
- **触边 BG 连通域 = 真背景，永不填**（验收点：触边背景不误填）；
- 未触边 BG 连通域 = 孔；area ≤ max_area·(W·H) → 填充（面积语义 =
  画幅面积比例；max_area=1.0 → 全部封闭孔填充）；
- output_mode：**Fill** → 孔像素 α 置 1.0（其余 α 原样——HDR α 保留）；
  **Holes** → 孔 mask 本身（1 孔 / 0 其余，供下游遮罩消费）；RGB 透传；
- **恒等锚**：max_area=0 → 不填，α 原样透传（任意输入）；
- 防御：缺 α 通道 → ValueError（显式，不静默猜测）；全 FG/全 BG →
  无孔，α 原样。

## 参数命名（AUD-0832）

connectivity / max_area / threshold / output_mode 均不在
`shader_compiler._GLSL_RESERVED_IDENTIFIERS` 黑名单（已查
`src/features/color/core/shader_compiler.py`；无同类 plugins/.tglsl 撞名）。

## 验证现状

- marker 协议/4×4 中心孔填充与 max_area 门控手算/触边背景不误填/
  对角连通差分（4-conn 填、8-conn 不填）/Holes 模式/恒等锚/HDR α/
  α 缺失显式报错/manifest 一致——已测
  （tests/content_library_v1/test_c19_fill_holes.py）。
  **未验**：L04 接线全家 + 全帧预算子门（集成者）。
