# F3.4 · 路由、自动化、响度、Stem 与录音 — 验证台账

- 日期：2026-10-24
- 提交：片1 `1bbadd9e3`（响度公式/stem 键/静音缺陷）＋ 片2 `834bb9fcb`（sends 路由/BUS 干声）＋ 片3（sidecar/录音设备/收口）
- 计划条目：docs/feature_plan_F_davinci_2026-10-22.md §F3.4
- 调研：Explore 全量盘点（六验收项差距，2026-10-24，结论驱动本台账）

## 1. 缺陷修复（本轮实测发现）

| # | 缺陷 | 修复 | 测试 |
|---|---|---|---|
| 1 | **BS.1770-4 响度公式**：`loudness_meter` 声道能量 `/ sum(weights)` 归一化（加权平均），标准为求和不归一化——dual-mono 立体声与同电平 mono 同读数（标准应 +3.01 LU）、5.1 整体低 7.66 LU；立体声交付按 -23 目标归一后在外部合规表系统性偏响 ~3 LU | 求和语义 + 校准测试改回标准参考（dual-mono 每声道峰值 0.0708；mono 0.1084） | 声道语义向量 4 项：mono vs dual-mono +3.01 / 反相不求和相消 / 不相关 +3.01 / 静音声道退化 mono 无 -inf 传播；校准 5 项重锚 |
| 2 | **Stem sends 整段丢失**：stem 落位循环无 send 分支（成片经 route_track_output 处理）——带 send 工程 Master stem ≠ 成片 | send 并行 tap × level 进 stem 集合内 bus（与 route_track_output 同语义） | MASTER stem 含 send 贡献 0.5（旧 0.4）/ BUS stem t1+send=0.3（旧 0.4+） |
| 3 | **BUS stem 干声泄漏**：路由到别处/直达 Master 的轨干声直入 BUS stem 缓冲 | BUS stem = 该 Bus 输出语义：非本 bus 路由的轨只经 send 贡献 | 同上 BUS stem 数值断言 |
| 4 | **成片源补丁访问崩溃**：duck-typed 工程无 `source_patch_overrides` → AttributeError 被外层吞 → 整 clip 静音（双角色 stems 全静且相等）| getattr 防御 | `test_exporter_dual_role_masters_match_container_tracks` 复活（经 F2.5 前提交二分确认 pre-existing） |
| 5 | **Stem 缺 F3.3 采样域键**：使用采样域淡化/子帧偏移时 Master stem ≠ 成片 | stem clip_fade 补 fade_in/out_samples/audio_sample_offset | stem 套件回归 |
| 6 | **录音设备 id 类型**：PyQt6 `d.id()` 返回 QByteArray，str device_id 匹配不上 → 指定设备静默回退默认设备；UI 下拉拿到非 str | 枚举/两处匹配三站点统一 str 化 | 设备枚举契约 2 项 |

## 2. 核验已有（不重复建设，声明为主）

- **FX 关键帧真实 PCM 求值**：实时（mix_samples R08 采样器）/成片（timeline_export 逐帧 set_params）/Stem（_clip_fx_for）三路一致逐帧求值，pre-fader 位置一致——无缺口。
- **响度**：true peak 4x 过采样（FE-B15）、两阶段 gating、静音 floor（-70 → normalize gain None → 导出跳过缩放，isfinite 守卫）、K-weighting 48k/44.1k 双系数——存量能力核验通过（公式缺陷已修，见 #1）。
- **Master Stem 与成片同边界**：同一 AudioGraphRenderer/精确样本边界 D06/同 master 链；M-B8 容器路径 `_render_stem_masters` 直接复用 `_render_audio_master(only_track_ids)`。本轮补齐 sends/采样域键两处偏差后一致成立（限幅器非线性与响度归一只作用成片为声明语义，见 sidecar）。
- **录音**：设备枚举/选择、_ChunkedPcmBuffer 长录制溢写磁盘、取消不落盘、磁盘耗尽安全保留、设备拔出 save_partial——存量测试 44 项核验通过；设备热切换不支持为**声明**（config 构造期固定，切换=停录重建，契约测试固化）。

## 3. 本轮新增交付

- **路由 sidecar**（分轨重组的明确路由声明）：`build_routing_manifest` 纯函数 + `export_all_stems` 写 `routing.json`（schema topos.stem_routing/1：每轨 output_bus_id/sends（电平+post-fader）/master 链（限幅器非线性、归一只作用成片）/重组声明——有路由时"简单相加不等于混音"显式化，不再只散落代码注释）。
- 测试：stem 32（含 sends 路由 2 + sidecar 3）、响度 19（含向量 4）、录音 46（含设备 2）等。

## 4. 验收 → 证据映射

| 计划验收 | 证据 |
|---|---|
| mono/stereo/反相/不相关及声明支持的多声道向量 | TestChannelSemanticsVectors 4 项 + 校准 5 项重锚（#1） |
| Master Stem 与成片在相同边界一致 | 双角色 e2e 对拍复活（#4）+ sends/采样域键补齐（#2/#5）+ 三方对拍 49 绿 |
| 分轨重组按明确路由重建混音 | routing.json sidecar（§3）+ master==Σ(stems) 容器对拍（平面工程）|
| 监听/录音设备切换、长录制、取消与异常及时关闭资源 | 设备枚举/切换契约 2 项（#6）+ 存量 44 项（取消/磁盘/溢写/长录制 profile）|
| FX 关键帧真实 PCM 求值、平滑、pre/post-fader | 核验声明（§2，三路一致无缺口）|
| 响度保留声道语义、静音保护与 true peak | 公式修复（#1）+ 静音 floor/true peak 存量 13 项 |

## 5. 登记的后续片（不阻塞验收）

1. GROUP/TRACK stem 的 bus 链保留语义统一（M-B8 容器路径已对齐，
   render_stem_pcm 路径 GROUP stem 仍绕过 bus——语义分歧已在 sidecar
   声明外，待统一为"被排除轨贡献恒零"）。
2. 全静音+target 的导出端到端（analyzer+delivery 层守卫已覆盖；e2e 待补）。
3. 录音设备热切换 API（当前为声明：停录重建）与监听输出设备选择。
4. 实时 LUFS 表 scipy 缺失的 UI 降级提示（is_backend_available 消费）。
5. surround/auto-duck：实际消费者核验后按需接入（计划原文允许）。

## 6. 诚实边界

- "分轨相加=混音"仅在无路由+限幅器未激活+无归一时逐位成立；有路由时
  以 routing.json 声明重建路径（sidecar 明示）。
- 响度公式修复改变了读数语义：旧工程用旧读数归一过的成片，重新分析会
  得到不同（正确）数值——校准测试已重锚到标准参考。
