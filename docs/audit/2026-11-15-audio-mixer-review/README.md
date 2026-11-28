# Audio Mixer 与五款效果器复审证据

> 文中提及的部分旧计划或测试附件已清理，正文保留当时的记录。

日期：2026-11-15。输入提交 `06d670a9`；最终源码小修在本轮工作区。任务与实施步骤见 [新复审计划](../../plans/2026-11-15-audio-mixer-fx-review-followup.md)。

## 最终验证结果

- **82 个相关测试文件，1595 passed、1 skipped，74.15s。** 包括播放、DSP、成片／Stem、Master 自动化／限幅、预设／快照、录音以及 Qt 窗口／遥测。没有运行全仓约 1.6 万项测试，不能据此断言全仓零失败。
- 本轮四个新回归文件共 **57 项通过**；另外收紧原分数延迟／负反馈／FFT 测试。
- 唯一跳过：`test_audio_waveform_perf_and_fx.py::TestClipEffectWaveformWorker::test_compressor_lowers_peaks`，原因是该旧测试所需 WAV assets 不可用；其余压缩器／波形相关测试按日志执行。
- 完整主窗口视频实播：12 秒、203 条遥测，暂停归零、恢复新增 25 条；ring 无 underrun／overflow。观察的是真实引擎／窗口状态和 PCM 链路，没有作新的 Resolve 盲听或视觉逐项对照。
- 已确认的大问题仍存在，保留独立脚本输出，未把它们伪装成通过的 pytest。

| 文件 | 用途 |
|---|---|
| regression.log／regression.xml | 最终 pytest 文本／JUnit 证据 |
| [test-files.txt](test-files.txt) | 确切测试范围，82 个路径 |
| [environment.json](environment.json) | 输入 revision、Python／NumPy／SciPy／Qt 环境 |
| [real-playback.json](real-playback.json) | 本轮完整 MainWindow 实播探针的数值记录 |
| [remaining-issues.json](remaining-issues.json) | 时间回退、Bus FX 自动化、Stem pre-send、空预设／快照／未知 FX、SC cache 数值 |
| probe.stderr.log | 剩余问题探针日志；含“丢弃损坏 Master chain”这条真实降级警告 |
| [delay-measurements.json](delay-measurements.json) | Delay 参数跳变、近零时间处理成本；机器／负载相关，非 CI 硬阈值 |
| [legacy-eq-comparison.json](legacy-eq-comparison.json) | 相同 HPF 参数在旧 df749759 与当前 DSP 的 24.12dB 声音差异 |
| regression-before-fixture-update.log | 首次宽回归发现的测试替身接口不完整，1506 passed／1 failed／1 skipped；已修并完整复跑 |

首次宽回归失败原因：`test_fix_audio_mixer_aud4.py` 的 FakeDlg 只派生 QObject，缺少新增窗口释放逻辑使用的 QDialog.setAttribute／finished 接口。已将替身改为 QDialog，仍然真实发射 preview／committed 并验证命令接线；没有在产品代码中绕过窗口生命周期以迁就替身。

最后补查还修复了 EQ／FX 徽章忽略实例级旁路的问题，新增四项回归；`test_fix_wd17g99_aud.py` 的最小实例桩同步补上生产模型已有的 enabled 字段，后端不可用／探测失败的三条原断言保持不变。上述最终日志已包含全部更新。

## 重复验证

在项目根目录执行，使用已有 `.venv`。脚本不修改工程、用户预设或生产配置；合成 WAV 仅在临时目录，完成即清理。以下命令会打印新的数值，不会自动覆盖已冻结的 JSON：

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-mixer-review/reproduce_remaining_issues.py
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-mixer-review/measure_delay.py
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-mixer-review/compare_legacy_eq.py
```

`compare_legacy_eq.py` 需要本地有 df749759 Git 历史，只读取该版本的 audio_dsp.py，在私有 module 中运行，不 checkout、不覆盖生产模块。

按确切清单重跑测试，以下 Python 代码通过参数列表调用 pytest，不依赖 shell 的空格分割：

```sh
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from pathlib import Path
import subprocess
import sys

manifest = Path('docs/audit/2026-11-15-audio-mixer-review/test-files.txt')
raise SystemExit(subprocess.call([
    sys.executable, '-m', 'pytest', *manifest.read_text().splitlines(), '-q',
]))
PY
```

## 数值如何解释

- 200ms Delay 在 120ms 到达是导出调度 bug；`render_frames` 的重复 0／1 是独立的时序证据。不能只把预期改成 120ms。
- Bus 0.2 应衰减为 0.0502377；Stem 的 pre-fader Send 在推子 0 时应仍为 0.2。探针同时保留 actual／expected。
- `empty_preset_keeps_*` 为 true、future version 从 777 变 3、未知 FX 抛错／整链丢弃，均为待修现象。
- SC cache 字节是导出结束时仍持有的 NumPy 数据大小，不是 peak RSS；小时级内存是按时长的结构性外推，不是实测分配。
- Delay near-zero 耗时应在空闲机器重测。当前结果足以表明热点与延迟长度相关，不能由一次共享机器计时宣称已经达到发布预算。
- legacy EQ 比较用于证明需要版本兼容，不表示应撤回新的数学修正。
- real-playback.json 来自本轮临时完整主窗探针控制台输出记录；测试进程最后强制退出，不能把它当成自然退出／录音清理的验收。项目既有 `tests/benchmark/drive_real_app_audio_tracks.py` 可作为重新搭建实际播放测试的入口；本轮人工增加了五窗电平采集、暂停／恢复和 mix_samples 时长统计。
