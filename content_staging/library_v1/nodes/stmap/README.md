# C13 lib1_stmap —— 绝对 UV map 重映射节点（**多输入**）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C13 行——无绝对 UV 重映射（与 C12 相对位移区分：map
> 是**绝对采样坐标**），**新算法差量**；多输入子门与 C12 同批。
> 计划工作单 §5：image＋UV map；channel mapping、origin、invalid UV、
> filter；验收点：与相对位移区分绝对坐标；map 空间/精度/无效 UV。

## ⚠️ 多输入子门声明

**多输入子门未开，生产接线待集成者开子门（与 C12 同一多输入子门）。**
本组件为双输入节点（image / map），marker 使用 `_effect_input`（image）
+ `_effect_input_map`（map）私有键；接线前该 marker 属"未知 lib1_*"
显式拒绝——不发布。

## 交付物

- `lib1_stmap_node.py`：Node 类草案（channels/origin/invalid/filter）。
- `references/stmap_ref.py`：CPU oracle（双源签名
  `evaluate(image, uv_map, params)`，新算法无可复用核）。

## 算法 / 恒等语义

- **绝对坐标口径**（与 C12 区分）：uv=(0,0) → 原点角、uv=(1,1) →
  对角（增量位移请用 C12）；px = u·(W−1)、py 按 origin：
  BottomLeft → (1−v)·(H−1)（v=0 是底行）、TopLeft → v·(H−1)
  （v=0 是顶行）——像素中心 = 整数索引；
- channels：RG（u=ch0/v=ch1）或 BA（u=ch2/v=ch3）；
- invalid：**Clamp** = 域外 UV 由 clamp-half-texel 边缘钳制采样
  （索引钳 [0, n−1]）；**Passthrough** = 域外像素输出未位移原位内容
  （u,v ∈ [0,1] 外判定，域内不变）；
- filter：Nearest（floor(pos+0.5)，oracle 钉定）/ Linear（clamp 双线性）；
- **恒等锚**：map = 每像素自身归一化位置（u = x/(W−1)、v 按原点取
  反）→ 精确恒等（Nearest/Linear 均逐位，任意 origin——测试钉住）；
- 全通道（含 α）随 UV 重定位；内容色不 clamp（HDR 透传）；
- 防御回退：map 通道缺失（BA 缺 B/A）→ 缺失通道按 0.0（原点角）；
  分辨率不一致 → 同索引钳制读取（不重采样，接线期 GPU 声明）；
  map 端口缺失 → no-op 空输出（重映射未定义，不静默透传）。

## 参数命名（AUD-0832）

channels / origin / invalid / filter 均不在
`shader_compiler._GLSL_RESERVED_IDENTIFIERS` 黑名单（已查
`src/features/color/core/shader_compiler.py`，无同类 plugins/.tglsl
撞名；map 是端口名非 @param，且不在黑名单）。

## 验证现状

- marker 双输入协议/恒等锚（归一化位置 map）/常量 UV 手算/origin 翻转
  /invalid 两策略/BA 通道映射/防御回退/manifest 一致——已测
  （tests/content_library_v1/test_c13_stmap.py）。
  **未验**：L04 接线全家 + 多输入子门（集成者）。
