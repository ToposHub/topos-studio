# M16.1b/M16.3b · 真实窗口三相位矩阵 + 冷启动实机基线（复开执行）

> HEAD：`5e54134c5`；日期：2026-10-25；宿主：Darwin x86_64 + AMD Radeon
> RX 6900 XT（GL 4.1 ATI-6.1.13）。
> 命令：`.venv/bin/python scripts/bench_m16_real_window.py --runs 5 --frames 72 --warmup 8 --target-fps 24 --cold-start-samples 5`
> 素材：tests/test_videos 真实素材五件（4K H264/H265/ProRes422 + 2K DNxHR
> 444 10bit + 2K Topos 422——**harness 补 Topos source 验收项自此落地**，
> 原生库身份登记：`native/topos_codec/build/release/topos_codec.dylib`
> sha256 前 16 位 `7159caffdae983f7`）。
> 完整数据：`bench_m16_real_window.json`（25 组原始 runs + 聚合 + 环境指纹）。

## 口径（预登记，不冒充）

- **present** = frameSwapped（swap 级可观测呈现，无物理扫描反馈）；
- **冷首帧** = 素材 open 前 → 首次 swap（含 open/decode/import/composite）；
- **seek** = seek 原语前 → 该帧 swap（定位+解码计入）；10 个固定分位目标
  （0.50/0.92/0.08/0.70/0.30/0.85/0.12/0.60/0.40/0.95 × 总帧数）；
- **播放** = QTimer 24fps 节拍（非音钟）warmup 8 + 72 帧；
- **隔离** = 每素材独立子进程（验收"独立进程复现"口径；同时隔离同进程
  顺序窗口的桥/GL 名字空间跨上下文污染——首轮单进程实测第 3 个零拷贝
  窗口触发环槽检疫后 FBO invalid operation，子进程化后 25/25 稳定）；
- **交错** = 5 组 × 5 素材，素材顺序逐组轮转去相关；
- 环境：Agent session（非独占安静机，基准期间无并行测试负载）。

## 结果（p50，跨组噪声带 = max−min）

| 素材 | 路由 | ok | 冷首帧 ms | seek p50 ms | 播放 decode p50/p95 | 播放帧 p95 ms | 掉帧 |
|---|---|---|---|---|---|---|---|
| 4K_H264 | macos_vt_zero_copy | 5/5 | 677（带 70） | 166（带 18） | 16.9 / 26.8 | 49.6（带 4.3） | 0.0 |
| 4K_H265 | macos_vt_zero_copy | 5/5 | 649（带 189） | 157（带 8） | 22.5 / **99.1** | **132.8**（带 8.1） | 0.0 |
| 4K_prores422 | macos_vt_zero_copy | 5/5 | 719（带 38） | 31（带 4） | 16.4 / — | 50.4（带 4.0） | 0.0 |
| 2K_DNxHR 444 10bit | ffmpeg_planar（VT 回退原因 `unsupported_codec: dnxhd` 如实记录） | 5/5 | 531（带 31） | 31（带 2） | 3.2 / — | 44.9（带 1.0） | 0.0 |
| 2K_topos_422 | topos_native（dylib sha256 登记） | 5/5 | 193（带 20） | 31（带 1） | 4.5 / — | 46.7（带 1.2） | 0.0 |

**冷启动实机基线**（`--test-startup --no-startup-dialog`，口径=进程启动→
MainWindow 构造+注册表就绪，未进 exec/首绘）：5 采样
[9049.7, 7615.9, 7505.8, 7533.9, 7599.1] ms，p50 7599，min 7506
（首采样 9049 为磁盘缓存冷态，如实分列）。

## 解读（不冒充结论）

- **25/25 组零错误零掉帧**：真实窗口生产路由（零拷贝/planar/Topos 三路）
  在 24fps 节拍下全程稳定呈现，无 decode_none/bind 失败/掉帧。
- **播放帧 p95**：ProRes/DNxHR/Topos/H264 ≈ 45–52ms（24fps 预算 41.7ms，
  p50 均在预算内，p95 小幅超带＝节拍相位+尾帧）。
- **唯一热点候选：4K H265 decode 尾部**——p50 22.5ms 在预算内，但
  p95 99.1ms（重排/尾帧解码尾部，与 M16.1 离屏登记的 VT 重排尾帧警告
  同族）把播放帧 p95 推到 132.8ms。**按 M16 纪律（一次只试一项、只在
  证据显示缺口时改代码）登记为后续单 item 优化候选，本轮测量门不改
  代码**——p50 在预算内且零掉帧，不构成当前缺口。
- 历史 38.2ms 基线为旧管线等待口径，与本矩阵 frameSwapped 口径不可直
  接对比（M16.1 已声明），不做跨口径宣称。
- RSS：每子素材进程 104→193/196 MiB（源打开+工作集；子进程隔离下无
  跨素材累积）。

## M16 门裁决

真实窗口冷首帧/seek/连续播放矩阵 + 冷启动实机基线**已执行完毕**（本
manifest + JSON 为证）。结果：无管线阻断性回归；热点候选（H265 4K
decode p95）在案。M16 剩余开放项仅：H265 尾部优化的单 item 轮（按
"证据显示缺口才改代码"纪律暂不动）、2h/10h 长压扩档（W08 Windows 侧
首档 600s+1800s 已在案，Mac 侧扩档随发布节奏）。

## 回滚

纯测量+文档，无产品代码改动（本轮唯一代码改动是基准脚本自身的子进程
隔离修复，见 `5e54134c5` 提交说明）。
