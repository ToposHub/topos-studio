# Cryptomatte Select 抠选（ID 抠像）

从 Cryptomatte / OBJECT_ID AOV 中按物体 ID 抠出 matte——点选物体即可做局部调色、换背景，不必画 roto。

## 输入

`image`：ID 纹理。Scene3D 节点的 `object_id` 输出直连，或 EXR 读回的 ID 通道。

## 用法

1. 连接 ID 纹理后，在属性面板把场景 manifest 交给节点（拾取路径自动完成）。
2. 在查看器中点击像素拾取物体（拾取 API：`pick_pixel(rgba)` 读回 ID → `toggle_selection(id, name)` 加入/移除选中集）。
3. `输出方式`：
   - **Matte** — 选中物体为白色、其余为黑色的 alpha matte（可接 matte_cleanup / erode_dilate 收边，或作为任意节点的 mask 输入）。
   - **隔离显示** — 原图 × matte（选中区域保留原像素）。

## ID 编码

- **RGBA24 分配器 ID**（默认）：本项目 Scene3D 的 OBJECT_ID 编码（24 位整数拆到 RGB/255），整数比较。
- **Float R（标准 Cryptomatte）**：R 通道直接携带 float32 位保持 ID（murmurhash3_32 → float），1e-6 比较。选中的物体名会在 manifest 变更时按名称重映射 ID；无法重映射的名称保留原 ID 并标记离线。

## 限制

- 选中集上限 32 个物体；标准 float-pair 编码的外部 Cryptomatte EXR（converter）为后续版本。
- 与标准 Cryptomatte 的差异：本项目纹理端使用 24 位分配器整数 ID，manifest 仍为 MurmurHash3_32 hex→name，二者在 Float R 模式下打通。
