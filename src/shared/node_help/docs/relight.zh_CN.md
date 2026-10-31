# Relight 重光照（AOV 重打光）

Relight 用同一次渲染输出的 **NORMAL（法线）** 和 **POSITION（世界位置）** AOV，在 2D 图像上重新打光——不用回 3D 软件改灯，直接在合成里移动光源。

## 输入

| 端口 | 说明 |
|---|---|
| image | beauty / albedo 图像 |
| normal | 世界空间法线（本项目 AOV 编码为 [0,1]，必连） |
| position | 世界空间位置（HDR float，点光源模式建议连接） |

Scene3D 节点的 `normal` / `position` 输出可直接连接；Blender 等外部 DCC 导出的多层 EXR 也可以经 Shuffle 接入。

## 参数

- **光源类型**：`point`（点光源，按 light_position 与逐像素 POSITION 的夹角打光）/ `directional`（平行光，方向 = light_direction 取反）。
- **光源位置 / 光照方向**：世界坐标单位，全部可动画、可表达式。
- **强度 / 光源颜色 / 环境光**：输出 = image × (max(dot(N,L),0) × 强度 × 颜色 + 环境光)，全部在 linear 域计算。
- **法线解码**：`encoded`（本项目 AOV 的 [0,1] 编码，默认）/ `signed`（外部 DCC 直接存 -1~1 的 float 法线）。
- **翻转法线 Y**：Blender 导出的法线若上下颠倒，打开此开关。

## 注意

- MVP 只做 Lambert 漫反射（镜面高光为后续版本）；点光源模式下若未连接 position 输入，光源位置参数会被当作相对世界原点的方向使用。
- 纯逐像素运算，无空间采样，交互开销可忽略。
