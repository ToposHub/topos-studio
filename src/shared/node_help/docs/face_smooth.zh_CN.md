# Face Smooth 磨皮

皮肤区域内双边磨皮：`mask` 输入（来自 build_skin_mask 或任意 matte）限定作用区，**mask 外逐位不变**；内部复用已验证的双边 denoise shader（边保持，可仅亮度模式保色度）。`强度=0` 为精确直通。

- **强度** 0-1 可动画；**半径** 双边采样半径（也是 tile padding）；**仅亮度** 默认开。
- 缺 mask 输入时直通并给出可见提示。
