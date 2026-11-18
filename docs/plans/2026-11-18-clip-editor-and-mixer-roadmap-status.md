# Clip Editor 与混音工作区实施进度（2026-11-18）

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
  enabled 步骤签名（旁路不触发重建）。
- Clip Editor：静音/标准化选区入栈（标准化实测 PCM peak，零信号明示）；
  机架由模型驱动，旁路/删除/清空走命令；A/B 全局旁路为监听态。

### P1-4 片段音量包络（58da5b86）
- `audio_edit["envelope"]`：`{id, local_sample, gain_db, interpolation}` 节点，
  dB 分段线性 + 端点延续的共用块求值器，与 clip gain/淡化相乘顺序不变；
  三条渲染路径与编辑器预览逐样本一致；画布包络工具随可编辑片段启用，
  拖拽结束/增删各提交一条命令。验收：0→−12 dB@4 s 中点 −6 dB、分块一致。

### P1-5 标记点与声道（71e28750）
- `audio_edit["markers"]`（稳定 ID/局部样本/名称/颜色）+ 画布装载与提交；
  复制/粘贴路径重新分配 marker ID；标记不进声音签名。
- `channel_polarity` 步骤承载工程级反极性（右键 Ø 提交命令）；
  静音/独奏保持监听态。

### P1-6 降噪与响度匹配（00999e66）
- `audio_denoise.py`：参考实现忠实移植（中位数噪声功率、软谱减掩码、
  频/时平滑、输入相位重建、5 ms 干湿边缘、可取消、SciPy 缺席显式降级）。
- `NoiseProfile` 携带输入图签名（调参/重连/换源即失效要求重采）。
- Clip Editor：采集噪声 → 降噪预览（基底：原始 → 降噪 → 处理栈）；
  响度匹配走 BS.1770 实测（g = min(ΔLUFS, ΔdBTP)）以 range_gain 提交，
  峰值受限/过短选区给出可达响度诊断。降噪入栈待预渲染缓存（见下）。

### P2-7 变速与音高（81f5078c）
- `audio_timepitch.py`：`output = round(N / (ratio_pct/100))` 长度合同；
  抗混叠有理重采样（不保调）、WSOLA 保调、纯音高（重采样 ×2^(st/12) 后
  以 1/p 保调拉回时长）与组合路径。Clip Editor 以预览态应用（选区上下文
  原样拼接），提交待长度事务。验收：150% 保调主频 440 Hz、长度误差 ≤1 样本；
  ±12 半音落在 880/440 Hz 且时长保持。

### P2-8（P0 项）采样尺
- 音频工作区进入时时间尺切采样域（子帧位置显式）、返回 Edit 恢复帧域。

## 待办（下一批次的入口）
- **预渲染 PCM 缓存（§3 第 3 条）**：按规格哈希的可重建缓存目录 + 原子替换 +
  取消清理；这是降噪/频谱修复入栈并保持播放/导出一致性的前提。
- **改长度事务**：处理栈 step 的输入/输出映射、ripple 确认、链接视频约束
  （§3/§6 的裁剪、删除区间、变速提交）。
- **频谱修复笔/瞬态修复（§5 画笔）**：可编辑频谱 + attenuate/heal DSP。
- **时间线包络 lane 可视化**（P1-4 余项）与轨道自动化 Read/Touch 接线扩展、
  通道条 Bus/Send 声明的 UI 补全、Meter 聚合与交付预检（P2-8 表）。
- **录音准备/监听**（P1）与工作区可访问性（P2）。

## 回归基线

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/mixer tests/edit/test_audio_graph_parity.py \
  tests/edit/test_audio_export_parity.py tests/edit/test_sample_time_audio.py \
  tests/edit/test_fade_curve.py tests/edit/test_audio_automation.py \
  tests/edit/test_edit_effects_panel_drag_inject.py tests/edit/test_loudness_analyzer.py \
  tests/edit/test_clip_pcm.py tests/edit/test_audio_edit.py \
  tests/edit/test_audio_denoise.py tests/edit/test_audio_timepitch.py
# 2026-11-18：271 passed
```

tests/edit 全量另有 5 个与本路线图无关的既有失败
（test_n03_grade_degrade_gate ×2、test_media_pool_grade_double_click、
test_media_pool_log_ui、test_media_pool_marquee_perf，改动前即失败）。
