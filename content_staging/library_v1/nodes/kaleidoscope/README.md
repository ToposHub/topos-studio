# C10 lib1_kaleidoscope —— 扇区数万花筒节点

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C10 行——Kaleido_Mirror 仅 2/4/8 重折叠镜像，**无扇区数
> 旋转万花筒**（判定比计划口径更严：部分等价非可复用）——本组件为差量
> 新语义。

## 交付物

- `lib1_kaleidoscope_node.py`：Node 类草案（sectors/rotation/center/amount）。
- `references/kaleidoscope_ref.py`：CPU oracle（扇区折叠镜像采样）。

## 算法 / 恒等语义

- 中心：center 为 frame-normalized (u,v)，映射 cx = u·(W−1)、cy = v·(H−1)；
- θ = atan2(dy, dx) + radians(rotation)；扇区角 A = 2π/N；
  a = θ mod A（正模 [0,A)）；**镜像回基准扇区**：f = a（a ≤ A/2）否则
  A − a → f ∈ [0, A/2]；
- 半径 r 保持，采样位 = center + r·(cos f, sin f)；双线性 + clamp-to-edge；
- out = rgb + (sampled − rgb)·amount；**恒等**：amount=0 精确恒等（含 HDR）；
- **参数名修正（F16/F19 先例）**：mix 是 GLSL 保留字（AUD-0832），改 amount；
- 基准扇区（θ ∈ [0, A/2]）内采样位即原位（浮点往返 ~1e-12 → 输出=输入）；
- **连续性**：扇区边界 a=A/2（镜像 C0 对接）与接缝 a=0/A（两侧 f→0）
  均连续——N 不整除 360（如 5/7 扇区）时同样成立（已测）。
- tile_padding：折叠位移 ≤ 2·r_max ≤ 画幅对角线（2048² 上界 2897 声明，
  width-aware 换算属接线期）。

## 验证现状

- marker 协议/恒等/90°→0° 折叠手算/基准扇区自映射/边界与接缝连续性
  （含 N=5 不整除 360）——已测（tests/content_library_v1/test_c10_kaleidoscope.py）。
  **未验**：L04 接线全家。
