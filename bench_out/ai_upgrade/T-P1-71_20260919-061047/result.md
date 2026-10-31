# T-P1-71 批次二：H265 内存增长定性 + Edit 套件段错误根因修复（2026-09-19）

## 一、Edit 套件 SIGSEGV——最小复现 → 原生栈 → 已修复

**最小复现（单测试对）**：

```
pytest "tests/edit/test_audio_routing_gate.py::test_empty_timeline_clears_gate_errors" \
       tests/edit/test_audio_waveform.py     # → SIGSEGV(139)
```

（delta debugging 从 35 个前置文件收敛到 1 个毒化测试；崩溃点
test_request_new_waveform_starts_worker。）

**根因（macOS .ips 崩溃报告原生栈）**：

```
pyqtBoundSignal_emit → PyQtSlotProxy::unislot → PyQtSlot::invoke
→ cfunction_call → SEGV
```

`EditPage.__init__` 曾把**全局长命单例** WaveformExtractor 的
`status_changed` 连接到 `self.waveform_status_message.emit`——
signal→`.emit` 的 C 函数槽**不带接收者上下文**；测试中 `page` 局部变量
被 GC 后连接悬空，下一次单例 emit（"波形正在生成中…"）直接调用已删除
页面的 bound signal → 段错误。单跑不崩是因为没有先创建再销毁 EditPage。

**修复**：改为 signal→signal 连接（`connect(self.waveform_status_message)`），
PyQt 注册接收者上下文，页面析构时 Qt 自动断开。

**验证**：
- 最小配对：27 passed（修复前必崩）；
- 回归测试 tests/edit/test_edit_page_waveform_signal_lifecycle.py（2 例，
  若回归则以 SIGSEGV 形式可见——该缺陷类唯一可靠失败信号）；
- 全套件复跑见同目录 log（修复前每次全套件 139 at test_audio_waveform）。

## 二、"H265 导出 native 内存增长"——定性修正

测量脚本：scripts/bench_h265_memory_growth.py（--codec hevc/h264 对照）。

| 探针 | 结果 |
|---|---|
| HEVC 导出 10 轮×48f（topos 源） | +276MB，前段 25-33MB/轮 |
| H.264 导出 10 轮（对照） | +241MB——**与 codec 无关** |
| H.265 素材→HEVC 导出 6 轮 | +357MB（更陡，FFmpeg 解码路径更长） |
| tracemalloc（4 轮导出差分） | **Python 堆仅 +0.2MB → 增长全在原生侧** |
| topos 源纯解码循环 10 轮 | +120MB 后**平台化**（缓存填充） |
| **裸 PyAV h265 解码 60 轮** | **+204MB 后完全平台化（末 20 轮 +0.26MB/轮）** |
| HEVC 导出 40 轮长跑 | 末 15 轮降至 ~4MB/轮（多次零增长），主饱和期已过 |

**修正后的结论**：此前记录的"线性增长"主体是 FFmpeg/PyAV 首次使用后的
**有界高水位保留**（线程池/查找表/arena，h265 路径 ~200-400MB 封顶），
不是无界泄漏。Python 侧无泄漏；导出 40 轮后残余 ~4MB/轮的慢增速需要
200+ 轮长跑进一步定性（可能是 manifest 后台线程/QC probe 的次生保留）。

**剩余行动**（下一步）：200 轮导出长跑判定残余项；若确认残余为真泄漏，
用 malloc stack logging 定位原生分配点。

证据：T-P1-71_20260919-021654/（hevc 10 轮）、T-P1-71_20260919-060326/
（h264 10 轮）、T-P1-71_20260919-061047/（hevc 40 轮 + 本报告）。

## 补充：全套件修复后首跑结果与预存失败甄别（2026-09-19）

段错误修复后 tests/edit 全套件首次完整跑通（此前每次都在
test_audio_waveform 处 139 终止，后续文件的失败从未暴露）：

- 最终：8181+ passed，29 failed / 28+ failed 全部为**预存失败**
  （基线对照：stash 未提交改动后在同一文件集复现同数失败；
  4 个可疑文件另在会话前基点 211dceac worktree 验证）。
- 其中 `test_timeline_export_effects::test_unknown_effect_passthrough`
  为本会话 a744030f 引入（`_effect_export_skips` 属性在 `__new__`
  直构的 exporter 上缺失 → AttributeError）——已修（setdefault 防御）。
- `test_subtitle_font_preflight::test_export_blocks_on_missing_font_when_burning`
  在 211dceac 即失败：T-P0-20 时代 recorder 创建前移到 font preflight
  之前，测试桩 `_Cfg` 缺 `output_path` → AttributeError（桩保真度问题，
  非产品缺陷；真实 TimelineExportConfig 恒有 output_path）。留待
  预存失败清理批次。
- 其余 27 项（probe matrix×10、glfinish×4、workspace_presets×4 等）
  与本会话改动无关，属预存。

回归测试：tests/edit/test_edit_page_waveform_signal_lifecycle.py
（修复形态最终采用**绑定方法中继** `EditPage._on_waveform_status_from_singleton`
——signal→.emit 是段错误、signal→signal 直连在删除后仍残留代理并抛
AttributeError，只有绑定方法让 PyQt 注册接收者上下文、析构自动断开）。

- 偶发记录：test_timeline_decode_scheduler::test_async_prefetch_is_latest_only
  在全套件/文件级运行中间歇失败（单跑必过；本会话未触及该模块）——
  预存 flaky，与段错误修复无关。
