# C12 lib1_vector_displace —— 向量图驱动位移节点（**多输入**）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C12 行——无图像驱动位移（mesh_warp 是 UI 手柄网格；
> velocity map 只用于模糊），**新算法差量、首个多输入子门**；与 MeshWarp
> 区分：本节点为逐像素向量场位移，无网格/手柄/UI 几何。
> 计划工作单 §5：image＋RG/XY vector image＋可选 mask；scale、neutral、
> units；验收点：与 MeshWarp 区分、**输入角色不能串线**。

## ⚠️ 多输入子门声明

**多输入子门未开，生产接线待集成者开子门。** 本组件为三输入节点
（image / vector / mask），marker 使用多输入私有键
（`_effect_input` = image、`_effect_input_map` = vector、
`_effect_mask` = mask——base_node marker 协议允许 owning dispatcher
自有的私有键）；接线前该 marker 属"未知 lib1_*"显式拒绝——不发布。

## 交付物

- `lib1_vector_displace_node.py`：Node 类草案（scale/neutral/units；
  GENERIC_MASK_MIX=False——mask 是算法语义端口非通用后处理）。
- `references/vector_displace_ref.py`：CPU oracle（双源签名
  `evaluate(image, vector, params, mask=None)`，新算法无可复用核）。

## 算法 / 恒等语义

- dx = (vx − neutral)·kx、dy = (vy − neutral)·ky；Pixels：kx = ky =
  scale（像素）；UV：kx = scale·W、ky = scale·H（画幅归一化）；
- **mask 门控**：dx_eff = dx·m、dy_eff = dy·m——乘性衰减位移量
  （非 mix 混合）；m = mask α（4 通道）或通道 0；mask 域声明 [0,1]，
  oracle 不 clamp（越界由声明域负责，README 记录）；
- 采样位 (x + dx_eff, y + dy_eff)——**符号约定：正位移向采样位正向
  偏移**（内容向位移反方向移动；与 C13 stmap 绝对坐标采样同向口径），
  像素中心 = 整数索引，clamp-to-edge
  双线性（索引钳 [0, n−1]）；image 的 α 随 RGB 一起位移（整图重定位）；
- **恒等**：vector 全图 = neutral → 精确恒等（任意 scale/units/mask，
  含 HDR）；scale=0 同为精确恒等；
- **输入角色不可串线**（验收点）：image 是被位移内容、vector 是位移场、
  mask 是门控——oracle 双源签名按位置绑定，测试钉住 image/vector 互换
  必须改变输出且 marker 键指向正确纹理；
- 防御回退：vector 通道不足 2 → 缺失通道按 neutral（零位移）；vector
  /mask 与 image 分辨率不同 → 同索引读取（越界钳制），map 重采样到
  image 画幅属接线期 GPU 声明；vector 端口缺失 → no-op 空输出（位移
  未定义，不静默透传）。

## 参数命名（AUD-0832）

scale / neutral / units 均不在 `shader_compiler._GLSL_RESERVED_IDENTIFIERS`
黑名单（mix/length/mask/uv/color 等未撞名）——已查
`src/features/color/core/shader_compiler.py`，无同类 plugins/.tglsl 撞名。

## 验证现状

- marker 多输入协议/恒等（vector=neutral、scale=0）/Pixels 与 UV 手算
  位移/mask 乘性门控/角色串线互斥/vector 缺失 no-op/分辨率防御/
  manifest 一致——已测（tests/content_library_v1/test_c12_vector_displace.py）。
  **未验**：L04 接线全家 + 多输入子门（集成者）。
