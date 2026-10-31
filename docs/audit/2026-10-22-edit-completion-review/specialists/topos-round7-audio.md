# 第七轮音频修复复核（2026-10-22）

基线 `d11615bb8`；只检查/运行诊断，未修改产品或已有测试。项目 PyQt6 + ModernGL + GLSL，遵循 `.clinerules`。本报告的源码行号均是此基线。音频图测试与 PCM 诊断不等于声卡耳听或真实 GUI 全流程。

## 执行与通过点

- `tests/edit/test_fix_round6_phase1b_audio.py` 独立进程：21 passed，3.96s。
- 扩展音频/Stem/保调/嵌套相关 **45 文件：937 passed、1 skipped、0 failed，22.32s**。包含上述21，不可相加。独立 subprocess 设置120s硬超时，实际正常结束。唯一样本跳过为 `test_audio_waveform_perf_and_fx.py:220` 缺既定 WAV 资产。日志 `/tmp/topos-round7-audio-suite.log`，JUnit `/tmp/topos-round7-audio-suite.xml`，启动器 `/tmp/topos-round7-audio-test-runner.py`。
- D02 实际 WAV→生产 AudioDecoder→真实响度分析：同相和反相 stereo 的 gain 都 `-8.8489215574dB`，静音 gain=None，旧反相抵消问题已修。证据 `/tmp/topos-round7-audio-normalize-real.{py,json,log}`。
- 新增测试锁定了源入点0的2x/0.5x基本变调、部分倒放内部帧、冻结静音、Limiter动画离线求值、普通FX和2x的Master stem等价、NTSC常量无额外零洞、取消后decoder显式关闭。这些通过点不覆盖以下失败组合。

## 确认问题（7项；父报告可合并 A01/A05 为 retime 边界工作包）

### A01 / P1：非零源入点的变速音频读错缓存窗口，可能整段成为DC常量（D01/D05修复不完整，新引入）

- 位置：`src/features/edit/core/audio_retime.py:228-250`、`:260-261`。
- 根因：窗口 first/last 为源相对帧，但直接传给 decoder 当绝对源帧。读样本时却加 `source_start`，窗口尾越界被 `resample_segment` 钳成最后一个样本。
- **M-real**：`tests/test_videos/2K_h264.mp4`，source_start=100、source_duration=100、timeline_duration=50、speed=2、48kHz。真实 `_render_audio_master` 的 2×96000 样本几乎全常量，peak=0.0271786824、尾1秒std=3.725e-9；同入点1x正常peak=0.3613522、std=.0230769。
- **M-real(合成媒体)**：10秒线性坡度WAV，入点100+100源帧2x应从约.166→.322，实际首末都.165985，尾std0。入点25只后半被截断，入点0正常。
- 重放：`/tmp/topos-round7-audio-probe.py`、`/tmp/topos-round7-audio-real-probe.py`；JSON `/tmp/topos-round7-audio-data/results.json`、`/tmp/topos-round7-audio-real-results.json`。
- 建议：解码窗口、base_sample、采样位置统一绝对源样本坐标；源fps/精确采样边界同时统一，不能仅在读取点加offset。
- 验收：入点0/25/100/大入点 × 0.5/2x/reverse/速度点，PCM对齐真实源窗口且末段不保持常量；成片和Stem均对外部参考音频正确，而非只相互等价。

### A02 / P1：AAC短解码块导致广播异常，整段片段音频静音，导出仍可成功（D06引入回归）

- 位置：`timeline_export.py:4321`、`:4413-4426`。
- 根因：旧 `out_end=min(out_start+samples.shape[-1],total_samples)` 被替换为精确视频帧样本边界。`actual`识别了短块，但累加目的slice仍完整帧长；异常包住整个clip，后续帧全跳过。
- **M-real**：原始 `2K_h264.mp4`，入点50、源范围100、输出50帧、普通1x、44100Hz。第一块要1764，生产AAC decoder实际1748。日志 `operands could not be broadcast together with shapes (2,1764) (2,1748) (2,1764)`；最终2×88200 PCM peak=0、nonzero_samples=0。后续直接解码帧51起均可1764，证明不是源没声音。
- 父agent真实MainWindow Custom导出同样观察到该异常且任务done；此处是独立领域路径重现，不单列第二个bug。
- 证据：`/tmp/topos-round7-audio-real-probe.py`、`/tmp/topos-round7-audio-real.log`、`/tmp/topos-round7-audio-real-results.json`。
- 建议：短块显式补齐/精确续读或收窄目标slice，保持时间定位；解码失败不能整段无声后伪装成功，明确错误/降级。
- 验收：AAC非零入点、44100/48000、完整/Custom、短尾、Bus/Send均无广播异常；音频从正确时刻连续输出，短块不丢后续内容。

### A03 / P1：离线Preserve Pitch实际退化为变调；Stem还保留有意降级分支（历史遗漏，D01完成声明过宽）

- 位置：`timeline_export.py:3354-3364` 使用 `np`，模块唯一全局导入位于`:89-90 TYPE_CHECKING`；其他np导入为函数局部，不能使该helper可用。`:4241-4254` 捕获None再回退。
- **M-real(合成媒体)**：440Hz真实WAV、2x、preserve_pitch=True，真实Master日志 `preserve-pitch stretch failed for clip c: name 'np' is not defined` 后回退，FFT=880Hz；应保留440Hz。主成片/Stem虽然maxdiff0但一起错误，不能当作保调一致验收。
- `stem_export.py:861-862` 明确仍有“preserve-pitch回退变调”；`:927-941` 不检查preserve_pitch而直接读变调reader。修复np后还需使Stem共享离线保调图。此处实际主成片已经先失败，不能声称当前两者音高不同。
- 证据：`/tmp/topos-round7-audio-probe.py`、`/tmp/topos-round7-audio-probe.log` 及results.json preserve_pitch_parity。
- 建议/验收：实际贯通WSOLA、canonical反向/曲线/混帧率映射及Stem；440Hz在0.5/2x保持440±合理容差，语音时长和源位置正确；不支持组合必须preflight明示，不能静默改变用户勾选含义。实时保调G06仍是下一阶段能力，不因本项修复就算实现。

### A04 / P1：混合源fps下音频源帧被用序列fps解释，声音时域与视频不同（历史遗漏，非G17专属）

- 位置：`timeline_export.py:4314-4316`、`stem_export.py:945-949`；retime reader也只接序列fps/spf（`audio_retime.py:211-212,249-250`）。
- **M-real(合成媒体+显式生产元数据)**：真实坡度WAV，ClipInstance源fps50、序列25、timeline50、source100、speed1，模型canonical第25帧→source50（正确），但decoder `decode_samples_for_frame(50,25,...)` 读2秒；时间线1秒处PCM=.0880127，应源1秒=.0490001。片尾实际约.16443，应2秒=.0879994。
- 复现用真实ClipInstance字段 `_source_fps_num=50/_timeline_fps_num=25`，不是简化模型或假decoder。未跑同组合完整GUI，不夸大为已鼠标复现。
- 建议：区分源帧和序列帧到秒/样本的换算；普通与retime统一源样本时钟，采样级连续读，不能把正确视频源帧号直接送序列时钟。
- 验收：24/25/30/50/59.94不同源→不同序列，已知打板/脉冲和连续音频时刻一致；1x声音不意外倍速，成片/Stem同正确参考。

### A05 / P2：倒放最后一视频帧音频成为常量块（D01新端点错误）

- 位置：`audio_retime.py:86-87,100-102,259-267`。
- 根因：最后帧lo=0、外推hi=-1先被clamp成0，使x1==x0，读帧的reverse移位不触发，所有样本取同一点。
- **M-real(合成媒体)**：4秒/100帧完整倒放坡度WAV，最后1920样本std0且首末都.0100097656，前一帧std.00045047；应按首帧1920样本倒序，不能整帧保持单一样本。
- 证据同主probe results.json reverse_tail。现有新增reverse测试主动只测1/10/50/98，避开首尾，因此绿灯未发现。
- 验收：完整首尾逐样本reverse对比，单帧clip、0.5/2x和带源入点的倒放端点没有平直块/多丢一帧。

### A06 / P2：实时音频FX从无效值进入动画后永远不采样（D04新接线不完整）

- 位置：`audio_playback.py:2340-2353`：先判断静态链is_active，再调用动画sampler；初始效果inactive时没有机会激活，运行中变inactive后也无法再激活。
- **M-fixture**：生产AudioMixer+EffectChain、DC decoder替身（非真实音频文件），saturation static drive=0，animated_sampler(frame10)返回drive100。实际sampler调用0次、PCMpeak.1000000；生产EffectChain在正确drive100时peak.8500000。没有修改产品，仅对mixer提供公开API输入。
- 证据 `/tmp/topos-round7-audio-qt-probe.py`、`/tmp/topos-round7-audio-qt-results.json`。
- 验收：0→非0→0→非0的saturation、旁路开关、EQ中性→非中性，预览和成片同帧生效；采样必须先于active优化判断。

### A07 / P2：归一化取消信号排入忙worker线程，工作结束后才设置cancel；退出进度框还会wait阻塞UI（D03仍未达到验收）

- 位置：`timeline_view.py:11748,11754,11768-11769`，worker.cancel只置布尔值。
- **M-fixture/真实Qt线程调度**：生产 `_NormalizeGainWorker` moveToThread；与QProgressDialog.canceled同形的主线程signal连接worker.cancel；用可控1秒analysis循环每10ms轮询cancel。0.1008秒发送cancel后worker._cancelled=False，直到run结束才True，worker循环从未看到取消。真实Qt queued连接问题，非声称真实长素材已鼠标复现。
- `progress.exec()`被取消返回后主线程还调用`thread.wait(5000)`，无法满足原<200ms响应标准；worker完成回调/quit还依赖主线程处理队列。若5秒仍运行，其后对话框生命周期风险需修复时实测，**本轮未证明崩溃，不计额外崩溃bug**。
- 证据同Qt probe/log/results。
- 建议：线程安全cancel token可从GUI直接设置，非排队到正在run的QObject；取消后异步清理，不在GUI wait，过时/取消结果禁止更新spin；明确线程/worker销毁序列。
- 验收：真实长素材在解码、DSP期间取消均<200ms响应且零增益应用/零提交；worker及时结束，不留线程；关闭父窗口和连续取消重试稳定。

## 下一阶段/明确未完成

- G06实时保调仍未实现，现有UI“export only”披露没有消除A03离线错误。
- G17嵌套父retime/mixedfps/子转场仍在进度表剩余，不能因为U02 resolver贯通就标整个嵌套音频专业可用；45文件中普通嵌套回归通过，只说明其支持子集稳定。
- G07采样级剪辑、G08 AAF、专业音频插件宿主并无本轮新实现；应沿用上一计划作为能力建设，并先修以上输出正确性。
- 本轮无声卡监听、主观保调音质、5.1/7.1实物接线、小时级内存/实时性能、外部DAW导入验收。

## 证据索引

`/tmp/topos-round7-audio-probe.py`（WAV/主输出诊断）、`/tmp/topos-round7-audio-probe.log`、`/tmp/topos-round7-audio-data/results.json`；`/tmp/topos-round7-audio-real-probe.py`、`/tmp/topos-round7-audio-real.log`、`/tmp/topos-round7-audio-real-results.json`；`/tmp/topos-round7-audio-qt-probe.py`、`/tmp/topos-round7-audio-qt.log`、`/tmp/topos-round7-audio-qt-results.json`；归一化real三文件；suite.log/xml/test-runner及单独tests.log。均由父agent选择复制到新审计证据目录，不改历史材料。
