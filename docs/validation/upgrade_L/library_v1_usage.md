# 内容库使用说明（升级计划 L · library_v1）

> 适用版本：2026-10-25。本说明只声明**已通过验收的能力**（发布门
> L20/L21/L22 证据见 `docs/validation/upgrade_L/L20_L21_L22_gates_report.md`）；
> 暂存未接线的能力一律不宣称可用。

## 一、转场（Edit 页 · 18 家族）

全部以 `lib1_*` 前缀注册于转场目录，时间线两 clip 间添加即可。

**几何/揭示 mask 类**（预览走 GPU 分支）：
- Clock Wipe（时钟极角）、Venetian Blinds（周期百叶，count 2–32 全带同时
  扫掠）、Checker Reveal（棋盘奇偶两波）、Luma Reveal（按 A/B 图亮度排序
  揭示——首个内容驱动 mask）、Noise Dissolve（Value/Cell 两 pattern 的
  rank 场）、Halftone Reveal（网点扩张 Dot/Square）、Voronoi Reveal
  （晶胞推进）。

**双源重采样/复合类**：True Push（真双源平移）、Staggered Tiles（块错
峰）、Hinged Doors（铰链门）、Cube Faces（双面立方）、Page Curl（卷页，
back_color/shadow）、Pixelate Bridge（块采样桥）、Displacement Bridge
（噪声位移桥）、Ripple Refraction（径向涟漪）、Scanline Slices（条带错
位）、Kaleido Bridge（万花筒折叠）、Burn Reveal（rank 揭示+前沿发热）。

方向/参数语义与默认值见各家族标签；用户自定义转场 JSON 仅白名单
base（当前 lib1_clock_wipe / lib1_true_push）可参数化——其余家族的
具名配方走内容库预设（见下）。

**已知限制**：Metal 导出对全部 lib1 转场显式回退 CPU（MSL 未实现）；
部分家族 CPU 导出档 1080p 单帧 >100ms（分档数据见门报告）。

## 二、节点（Composite 页 · 21 入口）

`lib1_*` 节点经节点图正常连线使用。按子门分类：

- **单像素/单输入**（可融合）：RGB Matrix、Despill、Solarize、Mosaic、
  CRT、Glitch、Kaleidoscope、Halftone、Hatching、Median、Convolution、
  Halation、Polar Coordinates、Tile Array。
- **多输入**：Vector Displace（image+vector+mask 端口，角色不可串线）、
  STMAP（image+uv_map，绝对 UV）。
- **多输出**：Frequency Separation（low/high 双输出；low+high 在声明域
  精确重建输入）。
- **全帧 CPU**（不参与融合、不宣称 tile-safe）：Distance Matte（有符号
  距离场）、Fill Holes（触边背景永不填）。

未知 `lib1_*` 类型精确报错不透传；HDR 内容全程无 clamp（各自 README
声明的除外——如 halftone 网点几何域）。

## 三、配方（预设 · 80 项）

P01–P80（`content_staging/library_v1/presets/`，随内容库分发流程进入
宿主预设面板——白名单接入归 L05 既有机制）：

- P01–P32 调色/质感（生产插件双载荷或暂存组件参数载荷）；
- P33–P56 转场配方（白名单 base 另有用户转场 JSON；白名单外不产）；
- P57–P80 合成宏（slots/steps/connections 模板载荷）。

全部 `status=component_ready`：参数域已对真实组件校验，宿主图一键
导入与宏事务在后续批次接线（不宣称一键可用）。

## 四、验证口径

- 每组件：oracle（float64 独立参考）↔ 独立手算双轨对拍 + manifest 契约；
- GPU：真实 GL 对拍（Mac/RX 6800/GL 4.1），hash 类逐位一致；
- 发布门：每发布 ID 真实消费者证据（L20 能力矩阵 23 条）、性能分档
  （L21）、暂存隔离红线与 wheel 内容（L22）。
