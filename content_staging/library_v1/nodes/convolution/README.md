# C16 lib1_convolution —— 3×3 自定义卷积节点

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C16 行——无自定义核节点（edge_detect/sharpen 专用核不算）。

## 交付物

- `lib1_convolution_node.py`：Node 类草案（k00..k22 + normalize + bias；
  tile_padding=1）。
- `references/convolution_ref.py`：CPU oracle。

## 算法 / 恒等语义

- 核布局 k00..k22 行主序（k00=左上、**k11=中心**、k22=右下）；
  out = Σ K[i,j]·src[y+i−1, x+j−1]（edge 边界钳制）；
- normalize=True → 除以 D = Σ|K|（绝对值和，负权重核能量归一）；
  **防御回退**：D < 1e-6（全零核）时 D=1（不除零不发散）；
- bias 在归一化之后加：out = conv/D + bias；
- **恒等**：中心核 k11=1（其余 0）+ bias 0 → 精确恒等（normalize 任意，
  含 HDR）。注意恒等核是中心 tap k11——**k00=1 是 (−1,−1) 平移核非恒等**
  （任务口径按行列网格语义修正，测试钉住该区分）；
- 线性卷积无 clamp；alpha 透传。

## 验证现状

- marker 协议/恒等/box 归一化/平移核/Laplacian 手算/全零核回退——已测
  （tests/content_library_v1/test_c16_convolution.py）。**未验**：L04 接线全家。
