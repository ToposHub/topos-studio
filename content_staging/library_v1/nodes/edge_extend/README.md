# C17 lib1_edge_extend —— alpha 边缘 RGB 延伸节点（试点，alpha 子门首个）

> 状态：**component_ready**（CPU oracle + 节点类草案 + shader 草案；dispatch 未接线）。
> 台账：L00 §3 C17 行——erode_dilate 的 "extend" 是采样钳制非 RGB 延伸。

## 算法（references/edge_extend_ref.py 同构）

- 前源 = alpha ≥ threshold（默认 0.5）；
- 迭代扩张 radius 轮（0–8 有界）：每轮把与已实像素 4-邻接的未实像素 RGB
  置为其已实邻居均值并入实——一轮 1px，radius 轮后覆盖曼哈顿距离 ≤ radius；
- 输出：color_only（默认，**保留原 alpha**，去黑边/白边前置步骤）或
  premultiplied（延伸 RGB × 原 alpha）；
- radius=0 / 全实 → 恒等；分层前景的多轮均值传播不可折叠为单 pass
  （GPU 生产形态=每轮一个乒乓 pass，见 edge_extend.glsl 草案）。

## 交付物

`lib1_edge_extend_node.py`（marker，tile_padding=radius）/ `edge_extend.glsl`
（每轮扩张 pass 草案 + output 组装 pass）/ `references/edge_extend_ref.py`
（oracle）/ `hand_vectors.json`（6 组：线性延伸/premult/钻石扩张/阈值/radius0/全实）/
`component_manifest.json`。

## 验证现状

- oracle ↔ 手算一致；alpha_fringe_hair 夹具延伸语义（实区到列 34、延伸 3px、
  原 alpha 保留）已测；marker 协议已测。
- **未验**：乒乓 FBO 调度、tile_padding 动态值分块、dispatch/registry/capability、
  真实渲染（L04 alpha 子门）。
