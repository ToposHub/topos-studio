# Clip Editor 与混音工作区实施进度（2026-11-19）

对照 [2026-11-18-clip-editor-and-mixer-roadmap.md](2026-11-18-clip-editor-and-mixer-roadmap.md)
的实施状态。每完成一批均独立提交；回归基线为本文件末尾的命令。

## 已落地

### P0-1 混音页 Media Pool、Effects（b06503db）
- `dockAudioMediaPool` / `dockAudioEffects` 两个 project-backed Mixer dock，
  创建后默认隐藏、进 Mixer 借用 Edit 活体面板、离场归还。
- Media Pool 音频作用域（All Audio 智能箱 + 搜索词）与 Effects Audio 标签
  均为可还原视图状态（`enter/leave_audio_workspace`）。
- 出厂种子：两者与 Viewport 同侧一个标签组；`mixer_media_docks_layout_version`
  一次性迁移修复旧 saveState。
- 音频时间线拖放语义：纯音频直接落音轨；含音频流的视频按仅音频落显式音轨
  （不建隐藏视频轨/链接）；无音频流素材与 Bus 轨 hover/提交一致拒绝并给状态反馈。
- 效果 MIME 可拖到混音通道条插入链（按受支持的音频插入类型校验）。

### P0-2 PCM、频谱与独立试听（fa238d04）
- `clip_pcm.py`：`ClipAudioRenderSpec`（绑定感知的半开 48k 采样范围）+
  可取消 worker + LRU/代际校验的 `ClipPcmProvider`；波形 min/max/RMS、
  对数频率行频谱、清洁度读数（peak/RMS/噪声底，可选 BS.1770）同源。
- `ClipAuditionSession`：单设备会话承载播放/停止/seek/循环/跳选区；
  设备时钟回填播放头；停止回到起听点；无设备优雅失败。
- 过零吸附（3 ms 容差，按声道符号变化 + 幅值量化 + 就近决胜）；
  画布新增程序化 `setSelection`。
- 试听开始自动暂停时间线播放；隐藏/重开/切工程即停并释放。

### P0-3 处理栈与渲染路由（d8e866c9）
- `audio_edit.py`：版本化 `ClipInstance.audio_edit`（range_gain/mute/
  normalize/channel_polarity，全保长），半开片段输出样本域，未知字段
  round-trip，未知 processor 逐条诊断并阻断应用/交付。
- 渲染顺序合同：源解码/裁剪/retime → 处理栈 → clip 增益/淡化 → Clip FX →
  轨链，在实时 mix_samples、成片导出、Stem 三处共用一个纯块函数；
  边缘过渡按绝对样本位置计算（任意分块 == 整段）。
- `UpdateClipAudioEditCommand`（整片段 before/after 事务 + 构造期校验 +
  undo/redo/宏序列化）；`edit_page` 时间线签名与音频图快照哈希均纳入
  enabled 步骤签名（旁路不触发重建；任意参数变化均扰动签名）。
- Clip Editor：静音/标准化选区入栈（标准化实测 PCM peak，零信号明示）；
  机架由模型驱动，旁路/删除/清空走命令；A/B 全局旁路为监听态。

### P1-4 片段音量包络（58da5b86 + ceca5538）
- `audio_edit["envelope"]`：`{id, local_sample, gain_db, interpolation}` 节点，
  dB 分段线性 + 端点延续的共用块求值器，与 clip gain/淡化相乘顺序不变；
  三条渲染路径与编辑器预览逐样本一致。验收：0→−12 dB@4 s 中点 −6 dB。
- 余项（ceca5538）：时间线音频片段叠加包络折线（0 dB 细线 + 节点 +
  选中读数），模型驱动、随布局刷新；节点编辑入口保持在 Clip Editor 画布
  （命令提交 → 时间线下一次布局更新折线）。

### P1-5 标记点与声道（71e28750）
- `audio_edit["markers"]`（稳定 ID/局部样本/名称/颜色）+ 画布装载与提交；
  复制/粘贴路径重新分配 marker ID；标记不进声音签名。
- `channel_polarity` 步骤承载工程级反极性（右键 Ø 提交命令）；
  静音/独奏保持监听态。

### P1-6 降噪与响度匹配（00999e66 + 7960a51b）
- `audio_denoise.py`：参考实现忠实移植（中位数噪声功率、软谱减掩码、
  频/时平滑、输入相位重建、5 ms 干湿边缘、可取消、SciPy 缺席显式降级）。
- `NoiseProfile` 携带输入图签名（调参/重连/换源即失效要求重采）。
- Clip Editor：采集噪声 → 降噪预览；响度匹配走 BS.1770 实测
  （g = min(ΔLUFS, ΔdBTP)）以 range_gain 提交，峰值受限/过短选区给诊断。
- 降噪入栈（7960a51b）：`spectral_denoise` 预渲染步骤；可编辑片段上
  降噪按钮直接提交（幂等替换、可撤销），机架显示、预览与缓存基底同语义。

### P2-7 变速与音高（81f5078c + 5f1d8faf）
- `audio_timepitch.py`：`output = round(N / (ratio_pct/100))` 长度合同；
  抗混叠有理重采样（不保调）、WSOLA 保调、纯音高与组合路径。
  验收：150% 保调主频 440 Hz、长度误差 ≤1 样本；±12 半音 880/440 Hz。
- 改长度事务（5f1d8faf）：`time_stretch` / `delete_range` 步骤携带
  输入/输出映射（域级 output_length），逐步骤推进坐标域校验；裁剪/
  删除选区入栈；gap/ripple 确认对话框（链接视频需显式独立编辑、转场
  阻断、扩长覆盖邻块检测、淡化钳制、inline 步骤/包络/标记重定位 +
  塌缩冲突可见）；`TimelineProjectTransactionCommand` 一次提交一次撤销。

### §3.3 预渲染 PCM 缓存（7960a51b）
- `pcm_render_cache.py`：规格哈希（指纹/速率/声道/精确范围/有序步骤
  签名/输出长度）索引的内存 LRU + 磁盘 npz；临时文件 + 回读校验 +
  原子替换；取消/失败清理临时件；同进程同 key 去重（同步 + 守护线程）；
  构建监听；protect 防回收；损坏条目按未命中清除。
- 三条渲染路径（mix_samples / 成片 / Stem）共用基底切块 + inline
  步骤；引擎签名含缓存就绪态（pending→ready 触发重建接入降噪基底）；
  交付预检阻断不可构建的预渲染栈（缺 SciPy/超长/倒放/原生变速）。

### §5 频谱修复笔（6c3b8853）
- `audio_repair.py`：时间×log-Hz 椭圆距离掩码（余弦软化、笔迹填隙、
  DC 行排除）；attenuate 软增益（0 dB 逐样本旁路）；heal 前后帧对数
  幅值插值 + 相邻帧相位推进（双侧置信度门控）；5 ms 干湿边缘。
- `spectral_repair` 预渲染步骤持久化笔迹 `{sample, hz, time_radius,
  bandwidth_oct}`；画布像素→数据域在提交前换算（重开/缩放可重现）。
- 验收：2 kHz×80 ms 干扰 −23.6 dB、440 Hz/1 kHz ≤0.06 dB、选区外
  逐样本相同；heal 为能量级恢复（无相位展开的保守下限，见下）。

### P2-8（P0 项）采样尺（f3472162）
- 音频工作区进入时时间尺切采样域（子帧位置显式）、返回 Edit 恢复帧域。

### §7 P1/P2 混音页余项（57e6d675 + f3a688be）
- `SetTrackAutomationModeCommand`：Automation Lane 模式按轨道入时间线
  历史（此前只改控制器全局态，Read 实际不回放）；引擎 per-track 模式
  即时下发；推子录制循环按轨道模型模式取值；Lane 打开时按模型回填。
- 通道条 Send 徽标（活跃/总数，点击开路由对话框）；录音准备弹窗
  （输入设备/声道/预卷/监听）入设置链（ADR 面板缺席时兜底、在场时
  回填同步）；修复 AutomationLane 一个潜在构造顺序缺陷。
- 交付预检补深度：逐 clip 处理栈诊断阻断、离线素材阻断（有工程时）、
  轨道推子 >0 dB 的增益结构降级提示；Meter 聚合（Peak/RMS/GR/hold/
  削波灯）已由既有 telemetry 链路覆盖。
- 可访问性：新增控件 accessibleName（sends/录音准备/输出路由/提交
  按钮）并有回归测试锁定。

## 已知边界（诚实声明）
- **原生变速/倒放片段**不支持预渲染步骤（域错位明确阻断）；时间线侧
  包络节点编辑（Alt+click/拖动/Delete）留给后续批次——当前编辑入口在
  Clip Editor 画布，时间线为只读叠加。
- **heal** 的相位推进不做展开（能量级恢复 ≥35% RMS；音色相干衰减兜底）。
- **改长度重提交**：同栈已含改长度步骤时再次提交被阻断（提示先撤销/
  清空）——重定位既有映射的重写事务未做。
- 录音的**监听**在 take 进行中生效（管理器 on_monitor_block → 输入监听
  sink）；弹窗勾选是下一次 take 的偏好。真实设备验收（48 kHz/预卷/掉线/
  连续 take）需带硬件实测。
- 全量 tests/edit 套件仍有 5 个与本路线图无关的既有失败（见下），
  未在本轮处理。

## 回归基线

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/mixer tests/edit/test_audio_graph_parity.py \
  tests/edit/test_audio_export_parity.py tests/edit/test_audio_export_preflight.py \
  tests/edit/test_sample_time_audio.py \
  tests/edit/test_fade_curve.py tests/edit/test_audio_automation.py \
  tests/edit/test_edit_effects_panel_drag_inject.py tests/edit/test_loudness_analyzer.py \
  tests/edit/test_clip_pcm.py tests/edit/test_audio_edit.py \
  tests/edit/test_audio_denoise.py tests/edit/test_audio_timepitch.py \
  tests/edit/test_pcm_render_cache.py tests/edit/test_audio_length_edit.py \
  tests/edit/test_audio_repair.py tests/edit/test_timeline_view_layout.py \
  tests/edit/test_clip_item_trim_preview.py tests/project
# 2026-11-19：968 passed
```

tests/edit 全量另有 5 个与本路线图无关的既有失败
（test_n03_grade_degrade_gate ×2、test_media_pool_grade_double_click、
test_media_pool_log_ui、test_media_pool_marquee_perf，改动前即失败）。
