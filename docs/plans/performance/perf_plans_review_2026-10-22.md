# A/B 性能计划审查（2026-10-22）

审查对象：`perf_plan_A_mac_2026-10-22.md`、`perf_plan_B_windows_2026-10-22.md`，以及解释协作与指标口径所需的总纲。依据为当前工作树源码、已有基准原始 JSON、Python/PyAV/FFmpeg 官方资料。本次不执行计划中的 checkout、pull、环境重建、性能改造或长跑命令；未修改原计划和业务代码，也未重新测量 GPU 性能。

## 结论

方向有价值，源码定位也有实际依据；但还不能作为可以照着执行的函数级方案。A 的测量和兼容性步骤、B 的底层接口与验收口径需要先修正。后半段 native 调度器和新图形后端应作为有条件的候选项目，不能只因有 Token 预算就进入固定排期。

## 必须先修改的问题

### 1. [P1] A1.1 把完整 GC 放进每帧入口，可能直接制造卡顿

- 计划位置：A:64–68。
- `request_present()` 是每帧入口（`src/features/edit/core/realtime_playback/engine.py:704`），不是一次播放会话的入口；delegate 可能只是异步派发，函数返回并不表示该帧渲染完成。
- 无参数 `gc.collect()` 是完整回收；`gc.freeze()` 把当前受跟踪对象移到永久代，不等于“只暂停 gen2”。方案既没有定义有效的 gen2 控制方法，也没有覆盖进程级 GC 状态的恢复和内存增长。
- 修订：先用 GC 回调测停顿与掉帧关联；确有证据时，在播放状态切换处试验受控策略，并验证异常、停止、关闭及长播放内存。禁止每帧完整回收；不要用“gen2 次数为零”替代性能与内存验收。
- 官方依据：[Python 3.12 gc](https://docs.python.org/3.12/library/gc.html)。

### 2. [P1] A1.2/B4.3 将 DeckLink 占位接口误认成现成 GPU 直送

- 计划位置：A:74；B:155–156。
- Python 的 `submit_gpu_frame()` 只是转调扩展接口（`src/shared/video/monitor_output.py:1275`）。真正实现位于 `src/native/decklink_bridge/topos_decklink.cpp:648`：`submit_gpu_texture()` 直接返回 False；`:682` 的能力属性也固定返回 False。
- `src/app/controllers/file_controller.py:3905` 已根据该能力决定是否保留 CPU 回读。因此“把 PBO 改成优先直送”本身不能获得零拷贝，贸然跳过能力判断还会丢输出。
- 修订：先评估 SDK/驱动/平台是否支持可用的 GPU 输出路径，并实现原生后端；在此之前保留 CPU 回读。此项应重新估算，不能作为简单接线优化。

### 3. [P1] A0.1 的 ACK 不代表实际上屏，offscreen 不能验收显示延迟

- 计划位置：A:38、40、42。
- `ProgramViewer.set_present_frame()` 会先确认归还被替换的旧帧（`src/features/edit/ui/program_viewer.py:1754`）；`_ack_pending_present_without_display()`（`:1787`）明确允许未显示就 ACK。正常 ACK 也主要承担槽位归还职责。
- 因而 emit→ACK 可用于研究消费/归还延迟，不能全部记成“成功上屏时间”。offscreen 运行可以验证部分渲染管线，无法代替真实窗口和显示节奏验收。
- 修订：区分 submitted、superseded、copied、呈现事件和 slot_released；按 trace/generation/slot 身份关联。把离屏吞吐与有窗口播放测试分开。零上传的真实零拷贝路径允许 upload 为 0；未测量应显式标 unavailable，不能要求所有段都非零。

### 4. [P1] B1.1 的 D3D11 取帧描述不准确，B2.1 还缺真正互操作层

- 计划位置：B:72、90–92。
- 当前 `WindowsHwPlanarSource` 使用 `HWAccel(...)` 默认配置（`src/shared/media/windows_hw/hw_planar_source.py:185`）。PyAV 17.0.1 和 18.1.0 都已有 `is_hw_owned`；18.1.0 官方实现明确在其为 True 时保留硬件帧，否则会自动下载。因此只删调用端的 `frame.transfer()` 不足以实现驻留。
- FFmpeg 的 D3D11 帧通常通过 `AVFrame.data[0]` 保存纹理、`data[1]` 保存 array slice；`opaque` 不是通用的纹理指针。如何从 PyAV 安全取得原生资源、保持 AVFrame/COM 引用和同步，必须单独验证。
- `_wrap_external_gpu_planes()` 实际在 `src/features/edit/core/edit_gpu_preview_renderer.py:1829`，只包装已经存在的 OpenGL texture ID，并不导入 D3D11 纹理。原计划把该符号归在 TPR 也写错了。
- 修订：先验证 `is_hw_owned` 和受支持的资源桥接；NVIDIA 路线同时评估已有 CUDA/DLPack 能力。D3D11 路线明确纹理格式/平面/array slice、设备一致性、引用租约和线程同步，再谈生产包装。
- 官方依据：[PyAV 18.1.0 HWAccel](https://raw.githubusercontent.com/PyAV-Org/PyAV/v18.1.0/av/codec/hwaccel.py)、[解码下载实现](https://raw.githubusercontent.com/PyAV-Org/PyAV/v18.1.0/av/video/codeccontext.py)、[FFmpeg D3D11 帧描述](https://ffmpeg.org/doxygen/8.1/hwcontext__d3d11va_8h_source.html)。这些源码说明接口契约，不能替代 Windows 真机验证。

### 5. [P1] A2.2 不能为消灭拷贝而取消正确性回退

- 计划位置：A:106–107。
- `src/shared/video/yuv_gpu_upload.py:838` 已有 `_r16ui_auto_tested`，同一上传器只自检一次；失败时 float16 路径是保持功能的显式兼容性回退。
- 把失败分支改成“降级渲染提示”并不能证明内容仍正确。要求所有 DNxHR 都没有转换，也排除了这条合法回退。
- 修订：保留兼容路径；有测量证据后优化其分配/转换。分别验收快路径覆盖率和回退的正确性，不把硬件不支持判成优化失败。

### 6. [P2] B1.1 的 >700fps 与 B2.1 的“全部 H264/H265”都不能直接作门禁

- 计划位置：B:75、94。
- 486fps 来自 RX 6800 机器；4090 机器已有同素材生产 CPU planar 945.1fps、硬解 planar 162.7fps（`scripts/bench_hw_decode_results_nvidia_rtx4090.json:52` 起）。计划用前一机器的解码数据给后一机器的解码+着色设门槛，不能证明相对改善。
- 既有实测总结 `scripts/BENCH_HW_DECODE_WINDOWS.md:227` 还明确指出：单流预算足够，零拷贝立项要看多轨并发数据。
- B:93 正确写了 H264/H265 4:2:0 的边界，紧接着的“全部”验收却把不支持的 profile/色度格式也包括进去了。
- 修订：在同机、同素材、同输出尺寸/效果/缓存状态下比较现生产路径和 PoC；分别记录解码吞吐、端到端延迟、CPU 占用、多轨掉帧及资源水位。按 codec/profile/位深/色度/设备列支持矩阵，不支持的明确回退。

## 需要收紧的设计与排期

### 7. [P2] A0.1 的 GPU 结果需要按原始帧回填

`gpu_submit_ms` 在这里确实来自 GPU timer query，不是名称看起来像的 CPU submit 耗时。但 `src/shared/gpu/gl_pass_timer.py:53` 的 ring 深度是 3，`:111` 起延迟收集旧帧结果；`gpu_pass_timer_stats()`（`edit_gpu_preview_renderer.py:5102`）只返回最近值。直接写进当前 trace 会错配。应让 query 携带原始 trace/generation，并把 CPU 调度、队列等待、GPU 执行、呈现分别定义；流水线重叠时六段不能简单相加当作 wall time。

### 8. [P2] native 调度器、Metal/D3D 后端应设立项门槛

A3/A4、B3/B4 是架构项目。列出 OrderedDict、锁和临时对象，并不能证明 native 重写能改善真实播放；C 侧仍回调 Python 时还要计算边界和 GIL 成本。建议先依据归因数据选一个热点 PoC，证明收益后才扩大迁移。逐帧对拍应比较帧身份/时序/内容与释放行为，不能要求两个独立实现的 Python handle id 相同。

Metal/HLSL 的效果对拍框架值得保留，但还需单独覆盖生产效果能力协商、失败回退、色彩元数据、资源同步和关闭路径；“11 个 snippet 通过”不足以代表整个预览/导出管线可替换。已有测试原型不能量化成“只差四分之一”。

### 9. [P2] 环境、拷贝统计与工作量要减少伪精确

- 当前 `constraints.txt` 已锁 `av==18.1.0`。B0.1 倾向降到 17，至少需要和当前发布/CI 约束协调；统一 Python 也不能只靠 constraints。跨平台各自做前后比较，不应因版本未完全统一而停掉所有基线工作。
- `np.flipud()` 和切片通常是 view；`program_viewer.py:2164` 附近显式复制主要是 `ascontiguousarray` 与 `QImage.copy()`。不要把每一步数组操作都算成整帧拷贝，按实际分配/传输字节核算。
- 每任务数十万至数百万 Token、固定 14 周，以及“编排降 30%”目前没有估算或测量依据。可作暂定预算/期望，不能当作实施可信度的证明。以有证据的收益和完成门禁调度任务。

## 建议的执行顺序

1. 修订遥测契约，区分不可用、合法零值、延迟 GPU 结果、未显示归还和实际呈现；完善取消/seek/覆盖帧的关联规则。
2. Mac 和 Windows 各自建立单轨/多轨、播放/拖动、冷/热缓存的同机基线，记录运行时与图形/编解码依赖、素材和配置指纹。Windows 真实性能只能在对应真机上测量。
3. 根据排名选一个最小优化；Windows 先做保留硬件帧到渲染消费的有界 PoC，Mac 先修明确热点。保留正确性回退。
4. 只有收益和稳定性达标，才批准 native scheduler、新后端和 DeckLink 原生直送等较大项目。

ZCode 适合承担引用索引、测试/skip 原因聚类、支持矩阵整理和明确契约下的机械性修改；架构结论、真实性能口径和合并验收仍需独立复核。

本次确实尝试通过已安装的 ZCode CLI 派发只读证据核查，使用 plan 模式并移除 Bash/Edit/Write 工具。首轮遇到打包配置路径问题；通过进程级环境变量指定现有配置后重试，约四分钟未回传报告，随后停止本轮 CLI 及其子进程。以上审查结论由 Codex 独立核对，不计入未经取得和复核的 ZCode 结果；本次也未确认 CLI 实际选择的模型和额度归属。
