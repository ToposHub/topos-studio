# E 阶段第二批（E3，FUP-07 核心）：播放完成语义——只有实际呈现完成才发布 EOF

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/playback` 三引擎的
EOF 判定从"producer 耗尽"改为"末帧实际呈现 + 音频排空"。

## 缺口（FUP-07 已确认）

哨兵分支（解码线程想读 `frame_count` 号帧）一到这里就 `_playing=False`
并触发 EOF：2 帧假会话在 `shown_index=-1`、队列仍 `[0,1]` 时宣布播完；
Topos 同时暂停音频。StopIteration（截断）与自然 EOF 复用同一信号；
UltimatePlayer 里自然播完与用户停止是同一个 `StoppedState`。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `base_player.py` | 哨兵只置 `_decode_exhausted`（解码侧事实）并泊车，不停播不发 EOF；新增 `_maybe_publish_eof`（GUI 线程，`_poll_ring` 每 tick 调）：producer 耗尽 + 末帧已呈现（`_shown_index == frame_count-1`，0 帧会话 `-1==-1` 直接收尾）+ ring/pending 已清 + `_audio_drained()` 全成立才 `_on_playback_finished`（恰好一次，锁内防重入）；seek/新会话/stop 重置两层状态；泊车条件含耗尽（seek 唤醒重播） |
| `libav_player.py` | `_eof_sig` 更名 `_decode_stopped_sig`——只承载截断/时长高估的 GUI 收尾，不再与自然 EOF 混用；`_on_playback_finished`：音频暂停 + 图标复位；`_audio_drained` 无声恒真、有声走管线；`_AudioPipeline.drained()`：音源读完 + 待写清空 + sink 缓冲放完（`_source_eof/_pend_len/_sink_backlog` 三态，音频线程写、GUI 读、seek 重置）；`_poll_ring` 两条返回路径都接 `_maybe_publish_eof` |
| `topos_player.py` | 解码线程 `_on_eof`/`_eof_signal` 删除；`_on_playback_finished`（GUI）：音频停泵 + `_at_eof` + 图标；`_audio_drained` 走 `ToposAudioPipeline.drained()`（音频主时钟引擎的排空 = 收尾前提）；`_poll_ring` 音频时钟路径与严格按序路径都接发布判定 |
| `topos_audio.py` | `ToposAudioPipeline.drained()` + 同构三态跟踪（`_source_done/_pending_len/_sink_backlog`，seek 重置） |
| `player_qt.py` | UltimatePlayer：`EndOfMedia` 才是"实际呈现完成"权威信号 → `_at_eof`；片尾按播放 = `setPosition(0)` 从头重播（与 topos/libav 对齐），用户中途停止后再按 = 原地续播；进度条 seek / 换源清除片尾状态 |

## 语义矩阵（E3 的"分离"落点）

| 事件 | 旧行为 | 新行为 |
| --- | --- | --- |
| producer 耗尽（哨兵） | 立即 `_playing=False` + EOF + Topos 暂停音频 | 只置 `_decode_exhausted`，解码线程泊车等 seek/停机 |
| 末帧呈现 | 与耗尽混同 | `_maybe_publish_eof` 的必要条件之一 |
| 音频排空 | 从不发布；EOF 时直接 pause（截尾） | `drained()` = 音源读完+待写清空+sink 放完；有声素材收尾等排空 |
| 用户暂停 | 与 EOF 无区分 | 不触发发布（除非暂停点恰为呈现完成点——此时确属完成） |
| seek | 清 `_at_eof` | 另清 `_decode_exhausted/_eof_published`（重播/回跳重新获得发布资格） |
| 截断 StopIteration | 复用 EOF 信号 | 独立 `_decode_stopped_sig` + toast，与自然收尾分流 |
| Qt 引擎自然播完/用户停止 | 同一 `StoppedState` 不可区分 | `EndOfMedia` 置片尾标记：结尾重播 vs 中途续播语义分离 |

## 验证

- `tests/ui/test_playback_semantics_regressions.py`：FR-18 段重写为
  新契约（0/1/2/24 帧：每帧恰好读一次 + 耗尽不停播 + producer 侧不发
  EOF），新增呈现完成判定矩阵（末帧未呈现/队列未清/音频未排空/全
  成立恰好一次/0 帧会话/seek 重置）——22 项全绿。
- `tests/ui/test_playback_eof_e3.py`（新，9 项）：libav 无声路径发布、
  队列未清保持、topos 音频主时钟等排空→发布、无声路径真实收尾钩子
  （`_at_eof`）、片尾重播、ToposAudioPipeline 真实线程循环排空
  （fake sink：闸住音源→未排空→放行→三态同时成立）、libav 管线三态
  判定、UltimatePlayer EndOfMedia/用户停止/进度 seek 分离。
- 回归：`test_topos_audio`（8）、`test_gpu_yuv_frame`（5+3 真媒体跳
  过）、`test_native_player_host`（1）、`test_topos_audio_container`、
  `test_export_outcome` 全绿——共 46+23 通过。

## 诚实边界

- **真机矩阵未跑**：FUP-07 验收的 0/1/2/长片、有声/无声、片尾暂停/
  seek/重播、双窗口的真实媒体/真实音频设备验证仍待实机（本批全部
  假会话 + fake sink，与既有 FR-18~21 回归同口径）。
- FR-21 的 GPU 所属线程未动（E5 范围）。
- `drained()` 的"音源读完"在持续解码失败时同样置位（EOF 与异常在
  `_pull` 层同为 `(None, None)`）——排空判定自纠（后续有数据则待写
  量>0），不悬挂收尾。

## 隐私

合成帧/假会话/fake sink；无真实媒体文件、无隐私路径。
