# Point Cloud 3D 点云

PLY/xyz 点云或跟踪数据点云渲染（对位检查参考）。MVP 为 CPU 正交拟合光栅（确定性）；Scene3D 的 GL_POINTS pass 为后续集成。

- **数据源**：`文件`（trimesh 读 PLY 含顶点色 / xyz 文本）/ `来自跟踪`（tracker 的 track_data，有效帧变换 → 平面点）
- **着色**：纯色 / 顶点色 / 深度渐变；点大小、不透明度可调
- 加载失败/空数据 → 可见错误态、无输出（不静默）
