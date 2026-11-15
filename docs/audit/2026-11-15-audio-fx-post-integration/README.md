# 七款音频效果器接入后复审证据

2026-11-15；输入提交 `22e6e1c8`，开始时工作区干净。对应 [新修复计划](../../plans/2026-11-15-audio-fx-post-integration-review.md)。本目录包含修前、修后和仍待修复的测量，不把所有JSON都当作通过结果。

## 最终验证

- 修改前：96个相关文件，**1719 passed、1 skipped**。
- 最终：102个相关文件，**1796 passed、1 skipped，101.23s**；新增六个回归文件共 **77项通过**。
- 唯一跳过：`test_audio_waveform_perf_and_fx.py::TestClipEffectWaveformWorker::test_compressor_lowers_peaks`，原因是旧测试的WAV assets不可用。
- 未运行全仓测试。合成验证使用真实DSP／Mixer／renderer／TimelineExporter／Stem；编码输出或确定性音源边界可使用替身，具体见脚本。新的Qt回归包含真实processor→实例tap→打开窗口，不只直接调用窗口函数。
- 两款新FX的真实MainWindow测量：约8秒墙钟、43条实例遥测、暂停归零，但音频时钟只有约0.460x实时；无新FX的顺序对照约1.001x。ring无underrun不能替代时钟／墙钟验证。

## 文件索引

| 文件 | 用途／边界 |
|---|---|
| [baseline.json](baseline.json)、[environment.json](environment.json) | revision、测试环境与范围 |
| [baseline-regression.log](baseline-regression.log)／[baseline-regression.xml](baseline-regression.xml) | 修前现有回归1719项通过 |
| [final-regression.log](final-regression.log)／[final-regression.xml](final-regression.xml) | 修后完整本轮范围的最终结果 |
| [test-files.txt](test-files.txt)、[final-test-files.txt](final-test-files.txt) | 确切96／102文件路径 |
| [voice-before.json](voice-before.json)／[voice-after.json](voice-after.json) | Voice独立数值：局部修复与仍存在的WOLA／Pitch／F0问题 |
| [mangler-before.json](mangler-before.json)／[mangler-after.json](mangler-after.json) | Ring／reset小修及Grain／Dist／MiniLimiter剩余缺陷 |
| [state-export-baseline.json](state-export-baseline.json)／[state-export-after.json](state-export-after.json) | 真实导出、Stem、选区、尾音、变化自动化和真历史v2；大问题未改 |
| [mangler-master-limiter.json](mangler-master-limiter.json) | Master修前：同PCM pre-Master差0、post差0.08230；未来峰导致整块变小 |
| [master-guard-after.json](master-guard-after.json) | 修后首样本保留0.2，整块／1920块相同，峰受保护 |
| [real-app-result.json](real-app-result.json)／[real-app.log](real-app.log) | 有两款新FX、两个真实编辑窗口、实际AudioSink测量 |
| [real-app-none-result.json](real-app-none-result.json)／[real-app-none.log](real-app-none.log) | 同视频和提取音频、无FX对照；新FX收集器没有实例所以emissions=0，不代表没有播放音频 |
| [parameter-validation.json](parameter-validation.json) | NaN gain／bad seed现象，不是通过的功能声明 |
| [delay-feedback-hotspot.json](delay-feedback-hotspot.json) | 带反馈近零Delay仍超预算，非隔离计时 |

`*-before*.log`是各focused回归在旧源码上的失败证据。它们并不都使用完整baseline worktree：Voice／Mangler以私有模块加载原`22e6e1c8`源，metadata／automation回归替换相应旧方法；具体方式由各日志／探针记录。不能将“新focused共77通过”解读成77项全部在旧版本必失败。

`master-guard-before.log`还保留了一条早期浮点严格相等检查失败；最终已改为1e−12的数值容差，**这条浮点差不是旧实现的独立缺陷**。真正的两个失败行为是未来峰非因果压低和块分割结果不同。旧Spectral E2E在Ring修正后暴露Master问题的中间失败日志也保留；最后修了Master保护，未放宽原E2E断言。

## 重复验证

在仓库根目录，使用已有`.venv`。下列独立探针只打印数值／生成临时合成WAV，不修改用户工程、预设或生产配置：

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-fx-post-integration/voice-probe.py
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-fx-post-integration/mangler-probe.py
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-fx-post-integration/state-export-probe.py
```

Voice／Mangler探针支持按历史源比较（查看脚本参数）；state-export探针读取本地`302b88b6`的真实v2源，没有checkout或覆盖生产模块。

按最终清单重跑测试：

```sh
OPENBLAS_NUM_THREADS=1 QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
from pathlib import Path
import subprocess
import sys

paths = Path('docs/audit/2026-11-15-audio-fx-post-integration/final-test-files.txt').read_text().splitlines()
raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', *paths, '-q']))
PY
```

真实窗口测试会打开独立MainWindow与AudioSink，使用视频、临时提取WAV和临时配置；不会连接到用户原先已开的窗口。手动按需执行，**不要与性能压测／全量pytest同时运行**：

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-fx-post-integration/real-app-probe.py
TOPOS_POST_FX_PROBE_MODE=none OPENBLAS_NUM_THREADS=1 .venv/bin/python docs/audit/2026-11-15-audio-fx-post-integration/real-app-probe.py
```

脚本复用项目已有benchmark主窗驱动，只在自己的进程挂临时测量和两个新FX，不修改benchmark源码。会覆盖本目录对应`real-app*-result.json`；要冻结新一轮结果先另存。benchmark末尾强制进程退出，因此本结果不验收自然退出／录音清理。运行产生的临时目录路径记录在JSON中；它不是工程持久路径。

## 解释数值的限制

- 5s ring不足、2.575GiB／小时Pitch历史等内存结论中，前者有具体read-age数值，后者是按float64 stereo采样数外推，未分配一小时数组。
- `expected_max_block_delta`是项目一致性判据；当前JSON若actual仍大于它，就是待修复反例，不是放宽容差的理由。
- Voice F0被送达窗口不代表F0算法已经正确；880／1760Hz和右声道独有输入仍失败。wave／grain缓存能接收不代表paint消费已实现。
- 控制台没有报错、registry健康为绿、`n进n出`、固定一秒同块长三路径一致，都不能证明任意选区、声音时延或实际实时速度已经通过。
