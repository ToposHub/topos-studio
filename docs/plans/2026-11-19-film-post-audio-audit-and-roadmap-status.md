# Topos Studio 影视声音后期复核与下一阶段计划 · 完成报告

基线 `51dbe7a1`（fix-ui）。本报告对应 [计划](2026-11-19-film-post-audio-audit-and-roadmap.md) 的执行进度；每完成一批均独立提交（`7ac6bf6e` → `53faf668`，共 19 个功能提交）。

## 总览

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| 第一阶段 · 交付正确性（6 项 P0） | ✅ 全部落地 | 坐标域、独立源、机架序、声道域、预览数据、PDC/Stem 求和 |
| 第二阶段 · 对白编辑与混音（P1 ×7） | ✅ 核心层全部落地 | UI 命令链 + 纯函数算法；真机监听/AAF 实文件往返属现场补测项 |
| P2 · Reconform / 多声道 | ✅ 核心算法落地 | 指纹/LCS/音频 handles；5.1/7.1 解码与交付矩阵在 P0-4 完成 |
| 第三阶段 · 效果器接入 | ✅ 1-7 批次落地 | #8（exciter/subharmonic/doubler/pitch-correction）按计划"剧情性需求明确时再做"延后 |

新增 185 项定向验收测试全数通过；定向回归 620 项通过（audio edit/cache/export/stem/mixer 全域）。全量 Edit 套件另有约 35 项与本轮无关的既有 UI 失败（基线即存在：timeline 手势/快捷键、n03 调色门、media pool 等，见下"已知边界"）。

## 第一阶段 · 交付正确性（P0）

### P0-1 统一 48 kHz 编辑坐标与消费采样率（`7ac6bf6e`）
- `sample_time` 新增 `reference_position_to_rate` / `rate_position_to_reference` / `reference_fraction_at_rate` 有理换算（唯一换算层）。
- `apply_clip_audio_edit` 把 step 区间与包络节点从 48 kHz 参考域映射到消费率域；预渲染基底切块跨率走有理窗口 + 绝对位置线性重采样。
- 验收：44.1/48/96 kHz 下 `mute[48000,96000)` 落点一致、边界 ≤1 输出样本；斜坡/脉冲质心/忙闲块/分块一致共 17 项。

### P0-2 预渲染 PCM 作为独立源（`870e7e16`）
- 新增 `clip_block_from_pre_render`：实时/成片/Stem 三端共用的块供给（先查渲染状态再读成品）；源 EOF 后的块由成品直接供出。
- 离线 WSOLA 尾部提前停机修复（输入补 `frame_len+hop+delta` 静音余量，能量覆盖 96.9%→99.4%）。
- 验收：1 秒 WAV @50% 生成的 2 秒缓存，实时第二秒/成片/Stem 最后一帧有应有能量（真实 WAV 端到端）。

### P0-3 机架顺序与交付保真（`ca6d2dc9`）
- 缓存前缀 = 截至最后一个 enabled 预渲染步骤的全部 enabled 步骤（`render_prefix_steps`）；`render_pre_render_steps` 按机架序处理前缀 inline 步骤（`invert_position_through_step` 有理逆映射折域）。
- 基底在场时块路径只应用后缀（`suffix_audio_edit`）——修复 range_gain 双重应用。
- `PreRenderState`（pending/ready/failed/cancelled）+ `PreRenderUntrustedError`（片段/步骤/原因）；成片与 Stem 非 READY 中止交付（成片 clip 循环的解码 except 对该异常穿透）。
- SCHEMA_VERSION 2→3。验收 14 项（Gain→Denoise 非通勤、状态机四态、构建失败中止等）。

### P0-4 声道与源 patch 先后顺序（`bb05c272`）
- `decode_audio_range` 保留源布局解码（>2ch 逐声道 mono 重采样——无名布局下 PyAV 重采样器会段错误）；Nch 域源 patch 选道 → BS.775 矩阵折到 ≤2ch（LFE 显式排除）。
- `PreRenderSpec` 增 `channel_layout` / `source_channel_map` / `domain_before|after` 并参与键；计划携带工程 patch 烘进基底。
- 验收 6 项：双单声道零串漏、5.1/7.1 one-hot ITU 系数、poly WAV 选道、patch 进 spec/基底、键敏感性。

### P0-5 重开/切换预览同一已处理数据（`3825e308`）
- `ProjectClipEditor._apply_pcm` 不再以源 PCM 当栈处理结果；新增 `ClipPreviewWorker`（`(spec, steps, view, fft, generation)` 键后台计算，同 key 合并/可取消/失败留痕/可结束）。
- 预览沿用预渲染缓存优先；计量基于处理后结果且移出 UI 回调；A/B 旁路往返、FFT 切换均基于处理后数据。
- 验收 7 项 + workspace 2 项改异步等待。冷启静音栈首次试听即正确版本。

### P0-6 Master/Stem/预卷/PDC（`83f55e16` + `202860b0` 补强）
- `EffectChain.latency_samples()` 聚合；`AudioGraphRenderer` 图级 PDC（轨链/Bus 链两级"最迟路径对齐"输出延迟线；mute 块也推进延迟线）；成片/Stem 尾音预算计入 `pdc_total_samples()`。
- 线性图 pre-Master 分类 stems 求和 == 合成（atol 1e-5）；MasterLimiter 触发时求和预期不等（另测）。
- 验收 6 项：干/延迟轨脉冲同一样本落点、块长无关（733 vs 4001 逐位）、静音块推进、求和语义。
- 已知边界：实时监听路径暂未接 PDC（零延迟经典 DSP 不受影响）；Stem 选区预卷复用成片预算逻辑待后续接线（成片选区预卷既有）。

## 第二阶段 · 对白编辑与混音（P1）

### P1-1 时间线包络编辑（`75c6f3ac`）
ClipItem 节点命中/Alt 增删/拖动/选择（局部采样时间 + dB，0 dB 磁吸、相邻钳制）；拖动只动临时 overlay，松手一次 `envelope_edit_finished` → 视图折算 48 kHz 局部样本（id 邻近沿用）→ 单命令提交；Esc/Delete 独立编辑模式；锁轨不可编辑；1200 节点命中 <250ms/200 次。12 项验收。

### P1-2 选区增益/监听/计量/标记（`3b1a2d1f`）
Normalize/LUFS 对实际插入阶段（处理后预览）与目标声道测量；允许实际衰减（−10→−23 ≈−13 dB）；升益受 dBTP 钳制；写入后复测回报；`upsert_step` 同约束替换不叠加；`ClipAuditionSession.set_monitor`（L/R Mute/Solo 设备 buffer 域监听矩阵——录音/导出零副作用）；标记 id 画布↔模型往返稳定。11 项验收。

### P1-3 改时长反向编辑事务（`8a846fc7`）
`unmap_audio_edit_for_removed_length_steps`（旁路/删除/替换/清空的坐标逆映射，塌缩项点名）；`plan_clip_length_change` 增 `removed_length_steps` + `base_output_samples`（raw 域步进）+ ripple 重算后续自动化/序列标记/Sync Lock 轨（含视频轨）下游片段；Clip Editor 长度失配栈变更不再阻断（统一走事务，单条历史）。6 项验收 + workspace 语义更新。

### P1-4 对白剪辑工具核心（`c2bfac5d`）
`dialogue_tools`：Room Tone 填补（循环铺满 + 接缝淡化 + 边缘交叉淡化 + 能量归一）；J/L cut 分割计划；批量交叉淡化容差配对；ADR 互相关对齐（FFT + 抛物线子帧）。11 项验收。take 轮换/场次镜次复用既有 `take_sheet`/`audition`；麦克风 source patch 在 P0-4 落地。实施方法列的"切空不动同步"与"同场对话编辑组"未实现（见已知边界 6）。

### P1-5 VCA（`167b9578`）
`VcaGroup`（成员/增益/静音/快照，序列化）+ 序列 `vca_factor_for`（多组相乘、任一静音=0）；渲染器音量级应用（automation ramp 相乘、pre-fader tap 不受影响）；`AudioMixer.set_vca_overrides` 实时同源。9 项验收。Track/Bus/Master 的 Read/Touch/Latch/Write/Off 为既有能力（`automation_recorder`）；实施方法列的"时间线参数 lane、Trim Automation、写保护、自动化预览捕获"未实现（见已知边界 6）。

### P1-6 监控与录音分离（`784456f5`）
`MonitorSection`（音量/Mono/DIM −20/Mute/PFL/AFL/solo-safe/参考校准，状态往返）；只作用于监听转发（take 录监听前信号）；`RecordingManager` Arm/InputMonitor/Record 三态分离（录音中禁撤备录；`require_arm_to_record` 兼容开关）。6 项 + 既有 45 项回归。

### P1-7 交付预设与 QC（`6f03ce97`）
`DeliveryPreset`/`LoudnessTarget` 按客户规范分版本（广播 −23/流媒体 −14/影院 −27+5.1 声道序）；标准 stem 清单（Full Mix/DX/ADR/Foley/FX/MX/M&E）；清单携带起始 TC/帧率 DF/声道顺序/SHA-256/实测；`evaluate_delivery`：缺 stem/响度超差/dBTP/峰值越界 BLOCKING、过量静音 WARNING。9 项验收。

## 第三阶段 · 效果器接入（生产 EffectChain 注册）

| # | 效果器 | 提交 | 要点 |
| --- | --- | --- | --- |
| 1 | De-Esser（`de_esser`） | `97160162` | band-pass 检测 + 流式 RMS 包络 + attack/release；oracle 增益控制；Wide/Split（减法互补）；listen 隔离；分块逐位一致 |
| 2 | De-Click/De-Hum | `f36cb528` | 50/60 谐波窄 notch（零相位、频漂跟随、自动基频检测）；高通残差 + 滑窗 MAD 点击检测 + 宽带瞬态 veto（保护鼓点）；短窗插值/长缺失转频谱修复 |
| 3 | True Peak Limiter（`true_peak_limiter`） | `202860b0` | 4× 多相 FIR 过采样（跨块上下文）+ lookahead + 平滑 release；链接/独立通道；延迟声明接 PDC；dBTP 独立复核路径验收 |
| 4 | Dynamic EQ（`dynamic_eq`） | `5dee3d63` | 复用 StripEQ RBJ Bell（关闭动态 == 同版本 EQ 逐样本一致）；段内 detector + 子块系数刷新（zi 保留） |
| 5 | Bus/Multiband Comp（`bus_comp`/`multiband_comp`） | `be3d70d1` | CompressorEffect 核心绑定 + DX/MX 胶合模板 + SC 高通；减法互补分频（重构精确）+ 每段独立 envelope |
| 6 | Convolution Reverb（`convolution_reverb`） | `4ce271ba` | IR 登记（SHA-256/重采样/裁切/规范化/加窗）；overlap-save 分区 FFT（脉冲复现 <1.5e-6）；mono→stereo/立体声/true stereo 四路径 |
| 7 | 瞬态塑形 + 调制类（`transient_designer`/`tremolo_auto_pan`/`chorus_flanger`/`phaser`） | `1b4d4489` | dB 域瞬态量（3dB 膝）；等功率 auto-pan；反馈尾音预算；相位连续 LFO（参数变化/自动化后连续） |
| 8 | exciter 等 | 按计划延后 | "剧情性设计需求明确时再做" |

## P2 · Reconform（`53faf668`）
`shot_fingerprint`（4 时间块亮度统计，抗噪/敏感）；`match_shots`（LCS → keep/insert/delete/move 候选）；`plan_audio_reconform`（音频 handles 保留）。6 项验收。

## 后续批次（2026-11-20 · 已知边界收口）

原"已知边界与后续批次"清单中全部可离线完成项已按批次落地并独立提交：

| 批次 | 内容 | 提交 |
| --- | --- | --- |
| WIP 收口 | ADR 台词/录制工作流接入 MainWindow（mixin 挂载、`_start_recording` 扩展 anchor/scene/shared_input、ADR dock 左槽迁移修复、混音器录音走带接线、语言包补齐） | `0424b982` |
| 边界 1 | 实时监听 PDC：`AudioMixer` 两级最迟路径对齐（共享 `apply_pdc_delay_line`；mute/solo/无输入块喂零推进；seek 清线）；新增 10 项对齐验收 | `73eb0b21` |
| 边界 2 | Stem 选区预卷：`_iter_stem_pcm_chunks`/`render_stem_pcm`/`export_all_stems` 增 `frame_range`，复用成片 H02 预卷预算（不含 PDC），预卷段裁剪交付；新增 5 项 | `a74abe48` |
| 边界 4 | 十款 rack FX 生产 UI：timeline_v2 参数载体（`effect_type_support` 修复为 supported）+ `FxParamDialog` 表驱动对话框 + 机架菜单/拖拽/默认参数 + `effect_registry` 注册 + 卷积混响 `ir_path` 装载；新增 19 项 | `e0741a5a` |
| 边界 6 | P1-4 子项：切空不动同步（`plan_gap_splits` + 静音间隙降序宏切分）与同场对话编辑组（`dialogue_group_id` + `SetDialogueGroupCommand`）；P1-5 子项：Trim Automation（`automation_trim` + 引擎 ramp 同语义）、写保护（录制/切写模式守卫）、预览捕获（`AutomationPreviewCapture` + 引擎预览层优先）、时间线参数 lane（`AutomationLanePanel` 停靠条带）；新增 12+12 项 | `a476f008`、`9a42a36e` |
| 边界 7 | 环绕 Bus 路由核心：`upmix_matrix`/`routing_matrix` + `bus_routing` 能力域 + `EditTrack.bus_layout` + 离线图/实时混音器全边路由矩阵（Bus→Master BS.775 降混按实际 master 宽度）；stereo Bus 逐位零回归；新增 14 项 | `ec860a90` |
| 边界 5 | 既有套件失败清理：n03 调色门×8（测试桩补 `_clip_color_graph`）、channel_layout_import×2（探测 str() 防御）、export_outcome×2（preflight 桩）、stopgaps×2（mock 声明 `_audio_only`）、media pool×3（sip 属性/事件泵）、plain-D（委托绑定）、retime 菜单/涟漪宏/降级日志/color wheel None 事件防护；连带回归 5 项同批修复（Bus→Master 宽度泛化、嵌套展开全量、注册表契约） | test(edit) + fix(edit) |

延后项不变：#8 效果器批次（"剧情性设计需求明确时再做"）、真机项（边界 3：真机监听/多声道路由监看/第三方 AAF 实文件往返，计划明示现场补测）。十款新 FX 的 Clip FX 插槽适配（clip 级对话框）与 ADR/混音录音的整段实机联调留待后续需求。

## 后续批次 · 第二轮（2026-11-20 · Clip FX 插槽收口）

按用户指示"尤其是片段编辑，新添加的效果器后端实现必须完美完成"，原"留待后续需求"的十款新 FX Clip FX 插槽适配全部落地并独立提交：

| 批次 | 内容 | 提交 |
| --- | --- | --- |
| Clip FX 对话框 | 十款新 rack FX（de_esser/true_peak_limiter/dynamic_eq/bus_comp/multiband_comp/convolution_reverb/transient_designer/tremolo_auto_pan/chorus_flanger/phaser）接入 `ClipAudioEffectDialogManager`：`FxParamDialog` 通用分支（ParamsClass `from_dict→to_dict` 归一化、损坏降级 AUD-19720 同语义、multiband crossovers ↔ 扁平键往返、committed/preview dict 双兼容）；控制器预览直送引擎（不落模型/不入撤销栈）+ 关窗回滚；修复「打开面板」悬空入口；连带修复 `35a4451d` 引入的 `_TelemetryStrip` 陈旧桩 | `303650e2` |
| Clip FX PDC | 片段级效果器链延迟声明接入最迟路径对齐（轨/Bus 两级在 clip 级的延伸）：实时 recompute 轨节点延迟 = 轨链 + clip 最大、同轨相对延迟线（无链/旁路按 0 须补满）、五条跳过路径喂零推进、H03 drain 预算 = max(尾音,链延迟)+持留量；离线 `note_clip_chain_latency` + `render_track_block` 相对线；成片/Stem 导出块前预构建 clip 链（最大延迟先知）；**true_peak_limiter 声明修正**——`resample_poly` 零相位对齐无净 FIR 群延迟（单位脉冲实测），修前多声明 3 样本 | `1d024567` |
| 注册表清理 | `effect_registry.py` 整段重复定义清零（WIP 恢复遗留 ×2 块约 450 行死代码；死副本的惰性 import 缺十款新 Params 类——单独调用会 NameError），唯一注释归位，行为逐项复核不变 | `5ae1e546` |
| 目录与套件修复 | **effect catalog 重生成**：十款新 FX 注册后未重生成入库目录（目录/注册表分叉，契约 3 项失败）——`effect_catalog_v1.json` 重生成 + digest 重冻结（214ca242…→86cab967…）；mx01 遥测 `_Strip` 陈旧桩修复；**H03/R04 drain 汇入修复**——批次 2 编辑把 drain 汇入块嵌进 `if _crel > 0:` 分支，零延迟链尾音回声全丢（stem 0.344→0.0），两处回归循环体层级；ef17 扫描正例更新到 G01 gallery 查询契约（库内 User 分区 + 合法 topos bundle，库根密闭替换） | `75c20d24`、`bdc1a23d` |

新增验收 **32 项**（`test_clip_fx_new_effects` 21：注册表互斥全覆盖/十款逐一弹窗/归一化/缓存/信号回路/控制器预览·回滚·MacroCommand/引擎后端十款真实块处理 + dBTP 上限；`test_clip_fx_pdc` 11：跨轨/同轨脉冲重合、零延迟零回归、mute 推进、结束 clip 持留 flush、离线 note/相对线/跨轨对齐、Stem 真实 limiter 对齐 + 干轨对照）；既有 clip 适配器排除契约翻转为联合覆盖。

## 已知边界（收口后余项）

1. ~~实时监听 PDC~~ → 已落地（批次"边界 1"）。
2. ~~Stem 选区预卷~~ → 已落地（批次"边界 2"）。
3. **真机项**（计划明示现场补测）：真机监听（macOS/Windows 双设备）、多声道路由监看、第三方 AAF 实文件往返。
4. ~~效果器 UI 面板~~ → 已落地（批次"边界 4"；~~十款新 FX 的 clip 级插槽适配~~ → 已落地（第二轮批次"Clip FX 对话框"+“Clip FX PDC"））。
5. **全量套件余项**：timeline 手势/指针 chrome 8 项为 Qt 离屏加载序波动（单独运行全过）；本轮修复后已无既有持久失败（`_TelemetryStrip` 陈旧桩已随第二轮修复）。
6. ~~P1-4/P1-5 方法列子项~~ → 已全部落地（批次"边界 6"）。
7. ~~环绕 Bus 路由（核心）~~ → 已落地（批次"边界 7"；"多声道路由监看"真机验收仍属边界 3）。
8. **#8 效果器批次**（exciter/subharmonic/doubler_harmonizer/pitch_correction）：按计划明示"剧情性设计需求明确时再做"延后——设计输入（分频/带限谐波映射、音高校正的音符/调性追踪范围）未明确前不接生产 schema；原型 UI 面板已在仓库（`src/features/mixer/effects/panels/`）。

## 回归基线

- 上一轮（2026-11-19）：新增 185 项；定向回归 620 项。
- 本轮（2026-11-20 后续批次）新增验收：**66 项**（ADR 工作流 6、PDC 对齐 10、Stem 预卷 5、FX UI 19、对白剪辑/编辑组 16、自动化 P1-5 12、环绕路由 14）；ADR 录音工作流域另有 59 项（recording/arm/adr 全域）。
- 第二轮（2026-11-20 Clip FX 插槽收口）新增验收：**32 项**（Clip FX 对话框 21 + Clip FX PDC 11）；连带回归：playback/mixer/graph 970、导出 parity 74、stem 98、录音/自动化 288、registry/effect 域 995、H03/R04 尾音 60 全过。
- 全量套件终态（本轮扩大范围至 `tests/edit + tests/shared` 全量，29:21）：**16925 通过 / 25 失败 / 79 skipped / 92 errors**。25 项失败中 15 项 + 92 errors 全部为 `tests/shared/tracking` 域——**环境编码器缺口**（OpenCV 5.0 headless 的 VideoWriter 无法打开 mp4v，fixture 视频无法按需生成；属调色/追踪域既有问题，不在影视声音计划范围，历史音频基线亦未收录该目录）。余 10 项 = 9 项既有 Qt 离屏加载序波动（`test_timeline_clip_affordance_gestures`×8 + `test_timeline_pointer_and_chrome`×1，单独运行 103/103 全过）+ 1 项同类的加载序波动（`test_fix_core_aud4::test_descriptor_from_config_with_real_manager`，单独运行全过）。**音频域持久失败清零**；历史上的 Edit+Mixer 口径（14145/9/65）中 9 项 flaky 与本轮完全重合。
- 修复的连带回归（第一轮）：本轮引入并当批修复——Bus→Master 单声道 stem 宽度、嵌套展开顺序依赖、注册表契约测试、表头 arm 状态 busy 重算与工作流退出复位。
- 修复的连带回归（第二轮）：本轮引入并当批修复——Clip FX PDC 编辑把 H03/R04 尾音 drain 汇入块嵌进 `if _crel > 0:` 分支（零延迟链尾音回声丢失，stem 0.344→0.0）→ 两处回归循环体层级；effect catalog 与注册表分叉（十款新 FX 未入库）→ 目录重生成 + digest 重冻结；true_peak_limiter 延迟多声明 3 样本 → 声明 = lookahead（实测对齐）；effect_registry 整段重复定义 → 清零；`_TelemetryStrip`/mx01 `_Strip` 陈旧桩 ×2 → 补 `display_meter_mode`；ef17 扫描正例陈旧 → G01 查询契约更新。

