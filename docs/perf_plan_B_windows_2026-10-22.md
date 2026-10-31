# 计划表 B · Windows 线（v3：复用现有契约，真机验证后接入）

> 目标机器 B：i9-13900K / RTX 4090 / Windows；历史记录为 Python 3.13.12、PyAV 18.1.0，开工重新采集。
> 机器 C：RX 6800 / Windows，负责在其硬件范围内运行矩阵、回归和长跑，不承担业务实现。
> 建议独立分支：`perf/b-zerocopy`；配套：[Mac 计划 A](perf_plan_A_mac_2026-10-22.md)；依据：[审查报告](perf_plans_review_2026-10-22.md)。
> 本文件为后续计划，未宣称 Windows 真机验证已完成。旧总纲中的 700fps、固定周数、Token 配额及“D3D11 从零建设”不作为本线依据；共同测量契约见 A v3 §2。

## 实施状态（2026-10-24 · 机器 B 首批）

| 任务 | 状态 | 产物/锚点 |
|---|---|---|
| B0.1 环境兼容矩阵 | ✅ 完成（含同日驱动 610.88→621.01 漂移登记） | `docs/perf_env_matrix_windows_2026-10.md`、`scripts/probe_env_matrix_windows.py`、JSON ×2 |
| B0.2 基准核对与重锚 | ✅ 完成（口径修复 + 4090 四轮；顺带修复 d3d11va 枚举查找与 DNxHR 素材名两处既有缺陷） | `scripts/BENCH_HW_DECODE_WINDOWS.md`、结果 JSON ×4；关键：H265 10bit 硬解反超 CPU，与 AMD 旧结论分歧 |
| B0.3 资产/拷贝/能力台账 | ✅ 完成 | `docs/perf_frame_budget_report_windows_2026-10.md`（22 模块逐项 + 字节流 + H1–H4 瓶颈假设） |
| B1.1 驻留 PoC | ✅ 完成（L2：cuda 保留帧 0 CPU 字节 + md5 逐帧对拍一致 + 消费接口实查；d3d11va 驻留本机证伪） | `scripts/poc_nvdec_zerocopy.py`、`docs/poc_zerocopy_b1_windows_2026-10.md` |
| C0.1 农场首跑 | ✅ 完成（36311 用例 / 94.4% 通过 / 新回归 0 / 主根因=陈旧 topos_codec.dll） | `docs/test_farm_first_run_windows_2026-10.md`、`testfarm/2026-10-24_machineB/` |
| C0.2 skip 审计 | ✅ 完成（静态 766 语句 + 运行时 610 用例对拍） | `docs/test_skip_audit_windows_2026-10.md`、`scripts/audit_test_skips.py` |
| B1.2 互操作与路线选择 | ✅ 完成（L2：cuda→GL 私有上下文 + cuda-bindings 全链路 md5/FBO 采样/交织正确；零拷贝 vs 下载 2.36×/3.81×；-129 毒化机制修正版与 ctypes 否决实录；路线选择=cuda→GL，D3D11 不立项） | `scripts/poc_cuda_gl_interop.py`、`docs/poc_interop_b12_windows_2026-10.md`、JSON ×1 |
| B2.1 预览消费与回退 | ✅ 完成（opt-in 默认关；契约/源/会话 + 四处管线接线 + L1×35/L2×2 单测 + **L3×3 真实渲染线程端到端**（单轨/反向 seek 源级确定性/双轨合成，present 画面 md5 一致）+ 运行中降级门闩（连续 3 次导入失败停新源路由）；快照回读路径非确定性缺陷已定界登记（显示不受影响），窗口交互浸泡与单源级降级为后续） | `src/shared/media/windows_hw/cuda_resident.py`、`docs/b21_cuda_resident_wiring_2026-10.md`、`tests/media/test_windows_cuda_resident.py`、`tests/edit/test_cuda_resident_playback_l3.py` |
| B2.2 素材 × 三机能力矩阵 | ✅ 机器 B 侧完成（32 素材发现式矩阵：驻留在全部 H264/H265 上反超下载 1.67–3.08×，0 CPU 字节，内容对拍全过；A/C 部分待各自机器运行同一脚本） | `scripts/bench_decode_matrix.py`、`docs/b22_decode_matrix_machineB_2026-10.md`、JSON ×1 |
| B2.3 AMD/Intel 扩展决策 | ✅ 决策完成：**暂缓（硬件门未满足，非拒绝）**——五步能力门验收清单已冻结供 C/Intel 机器执行；cuda 路线在非 NVIDIA 机自动 fail-closed | `docs/b23_b24_b3_b4_gate_decisions_2026-10.md` §B2.3 |
| B2.4 Topos GPU 解码 ADR | ✅ 决策完成：**不启动（触发条件未满足）**——瓶颈假设与 B2.2 矩阵均不支持立项；重开条件已登记 | 同上 §B2.4 |
| B3.x / B4.x 条件立项 | ✅ 决策完成：**未触发**（cuda→GL 已满足预览需求；导出侧无"不足"证据；B3.1 需 A 会签不在本线范围；B4 依赖 B3.2 连锁未触发；D3D11 导出 fail-closed 分支保持不动） | 同上 §B3/§B4 |
| C3.1 短时压力 | ✅ 完成（91s×3960 帧 0 错误 + 33/33 内容 md5 抽查 + 1127 随机 seek 0 失败槽位无泄漏 + 8/8 重开；10h 档与 TDR 人工项按计划登记为后续执行安排） | `docs/c31_stress_cuda_resident_2026-10.md`、`scripts/stress_cuda_resident.py` |
| Phase 5 回归矩阵 | ✅ 机器 B 受影响面（tests/media + tests/edit 全量）重跑完成，新回归 0（详见下行状态表更新提交） | `testfarm/2026-10-24_machineB/phase5_*.xml` |


## 0. 目标、已知资产与证据等级

**目标**：在支持的格式/设备上减少不必要的 GPU→CPU→GPU 往返，改善多轨播放或导出的实际瓶颈，并保留正确性、可解释的回退和 CPU 路径。零拷贝是候选手段，不要求硬件路径在所有场景替代 CPU。

当前源码与历史数据支持以下事实：

| 事实/资产 | 对计划的影响 |
|---|---|
| 现生产 `WindowsHwPlanarSource` 使用默认 HWAccel 配置，得到下载后的 NV12/P010 家族软件平面；解码管理器默认仅在 NVIDIA 或显式强制时尝试该分支 | 区分生产行为和 PyAV 能力。不能把源码注释中的“固定下载”当成库永远不支持保留设备帧 |
| 4090 历史 4K H264 同素材生产 CPU planar 945.1fps、硬解 planar 162.7fps；RX 6800 机器对应约 486/46.9fps | 这些是旧环境的解码基线，不能跨机拼接，也不能当成解码+着色/显示的验收线 |
| PyAV 17.0.1/18.1.0 有 `is_hw_owned`；18.1.0 实现会在其开启时保留硬件帧 | 先验证保留帧及消费接口，不从删除调用端 transfer 开始；D3D11 原生资源访问仍需验证 |
| `windows_hw/` 已有 D3D11 帧、设备、租约池、同步、decoder、GL bridge、render graph、编码 surface、native bridge 协议 | 先盘点实现/占位/测试替身，再补缺；不平行新建一套帧和租约 ABI |
| `GraphicsBackendKind` 已有 `D3D11/D3D12`；`timeline_export.py` 已有 D3D11 selector/probe，但 composite 集成仍显式拒绝 | 不重复扩枚举，也不能因 selector 存在就认为生产合成已接通 |
| `d3d11_shaders.py` 已有 shader ID 与资源声明，原生桥接由可选模块承担 | 注册表键或资源声明不等于完整可编译 kernel，更不等于实际 GPU 执行 |
| DeckLink 原生 `submit_gpu_texture()` 返回 False、能力属性关闭 | 保留 CPU 回读；GPU 直送另行做可行性和原生实现 |

报告统一标记证据等级：**L0** 静态/协议；**L1** mock 逻辑测试；**L2** 对应设备上的真实编译/执行；**L3** 生产播放/导出端到端。L0/L1 通过不能升级为真机性能结论，文件存在也不能证明扩展已安装可用。

## 1. 开工、协作与验收口径

环境自检仅采集，不自动切分支、pull、重建或降级环境。以下在 Windows CMD 的项目目录运行；解释器路径与实际 venv 不同时先修正路径。

```bat
git status --short
git branch --show-current
git rev-parse HEAD
.venv\Scripts\python.exe --version
.venv\Scripts\python.exe -c "import av, moderngl, PyQt6; print('av', av.__version__); print('ffmpeg', av.library_versions)"
```

所有权遵循 A v3 §1：B 主责 `src/shared/media/windows_hw/`、Windows 探针与原生实现；telemetry/帧契约/backend/uniform 等共享文件由一个任务串行修改；新增 `src/shared/gpu/d3d/` 或 Color/HLSL 文件须先说明现有模块无法承载的边界。tests/docs 可按文件分工，不能因“共享只读”而不补测试。

- 复用 A v3 §2 的帧身份、validity、原始帧 GPU query 回填、离屏/窗口分组和同机成对比较。B0 的环境与解码基线可先做，不必等待 A0.1；生产端到端报告依赖该契约。
- 指标至少包括首帧/seek、decode-only 吞吐、解码到渲染完成、窗口可观测呈现、CPU、RSS/可取得的显存、下载/上传字节及多轨掉帧。帧预算取实际项目 fps，缺测项标 unavailable。
- 每配置至少 5 组交错/成对采样；稳态预热、样本长度、缓存/代理/效果/输出分辨率一致。冷启动另测；按 A0/B0 基线噪声冻结主要指标、退化限值和停止条件。无基线不承诺 1.5×，无显著提升不因“零拷贝”标签自动切默认。
- 普通播放、导出、监看分别验收；GPU 内部复制、CPU 下载、CPU 上传和帧数据 view 分开计。编码后压缩包到主机不等于原始像素回读；不把纯 D3D11 吞吐与带显示的 GL 路线直接比较。

## Phase 0 · 优先完成：环境、资产与同机重锚

### B0.1 环境兼容矩阵

**实施**：记录 Python/PyAV/FFmpeg/Qt、GPU LUID/驱动、设备 API、扩展模块来源/版本/ABI 和实际加载库。`constraints.txt` 当前是 `av==18.1.0`；先对照发布/CI 选择受支持组合，不默认降至 17。Python 版本由环境配置/CI 指定，不能靠 pip constraints 锁解释器。

**验收**：每台机有可复现环境记录与依赖检查；版本变更单独提案/验证。允许在现环境先做同机基线，跨版本结果分层归档，不混进同一提升比例。

### B0.2 RTX 4090 重锚与基准脚本核对

**复用**：`scripts/bench_hw_decode_windows.py`、`scripts/probe_hw_decode_windows.py`。先确认产物归档与参数，再跑代表素材和扩展矩阵，使用唯一 tag 保留旧 JSON。快速复跑示例（tag 每次更换）：

```bat
.venv\Scripts\python.exe scripts\bench_hw_decode_windows.py --only 4K_H264.mov --hw-backend nvdec --tag B0_20261022_run01
```

先修/确认测量口径：当前脚本 `setup_ms` 的外层计时包含整个 bench 调用，不能当成真正初始化耗时；CPU/HW 路径的线程数、帧格式和缩放也须记录。新增真实 open/setup、首帧、warmup、steady 分段时保持历史字段可解释。

基准错误不能只看进程返回码：逐项检查 JSON error、实际测量帧数、EOF、缺失素材和能力回退。若改脚本，给结构化不完整/失败状态与相应退出语义。

**验收**：四种旧路径在支持项上可复现，重建同机噪声范围；旧 JSON 仅作历史参考。先补/复用公共运行 manifest，Topos native codec 相关项保留加载库强校验，纯 H264 基准不强绑无关 codec 库；实际 FFmpeg、桥接和驱动依赖仍需指纹。

### B0.3 资产、拷贝与能力台账（可并行只读）

逐模块核实 `D3D11Decoder`、`D3D11GLBridge`、`D3D11TexturePool`、`D3D11TimelineRenderer`、`D3D11EncodeSurfaceProvider`、`load_windows_native_bridge()` 的真实调用方与可选扩展。默认加载目标 `topos_hwdecode_windows` 必须在 B/C 上实查，不能由测试 FakeBridge 推断可用。

**产物**：`docs/perf_frame_budget_report_windows_2026-10.md`，列 L0–L3、缺失实现、显式 fallback reason、每路径实际字节与待验证项；附 profile/位深/色度/尺寸/设备支持矩阵。确认现有瓶颈是否来自多轨 CPU、下载、渲染或显示，而非只看解码器独立 fps。

### C0.1 测试农场首跑

先采集本次 commit/环境、collect 结果和测试选择；根据实际收集项及设备能力划分逻辑、Qt/GPU、真素材和外设套件，不沿用“3.6 万”作固定验收数。主机不能跑的模块单列，避免一个收集错误遮住全部逻辑结果。

按组保存完整 stdout/stderr、pytest 返回码、通过/失败/skip 数与原因；可用 `--junitxml` 输出结构化报告。分类既有失败、环境缺项和新增回归，不通过删除测试或强制解除 skip 获得全绿。

### C0.2 skip 审计

先按 AST/条件/理由自动归类，再复核边界；静态 skip 语句数与运行时 skip 用例数分别统计。报告“本机可跑 / 需要其他 GPU、OS、素材或外设 / 合理排除 / 待调查”，保留安全能力探测。用 RX 6800 的结果不能替代 4090 或 Mac 的设备验证。

## Phase 1 · 有界 PoC：保留硬件帧并证明渲染消费

### B1.1 设备驻留与资源访问 PoC

**进入条件**：B0.3 给出瓶颈假设与一个主要指标；先选择一种格式、一个适配器和一条消费链。不能同时无界开发多个完整解码器。

候选按验证成本递进：

1. 优先核实已部署的 native bridge 与已有 D3D11Decoder 能否提供真实 surface；可复用就补最小缺口，协议缺项通过有版本的兼容扩展处理。
2. 原生能力缺失时，验证当前 PyAV 的 `HWAccel(..., is_hw_owned=True)`，区分 CUDA 帧和 D3D11 帧。NVIDIA 路线评估该版本提供的 CUDA/DLPack 消费；与现有 GL 渲染接通的成本一起比较，不能只证明设备帧返回。
3. D3D11 原生访问通过受支持绑定/窄桥接层管理 AVFrame、COM 引用、array slice、adapter LUID 和完成 fence。FFmpeg 的 D3D11 帧通常用 `data[0]` 放纹理、`data[1]` 放 slice；不把 `opaque` 当通用纹理入口，不猜测 Cython/Python 对象内存布局。
4. 只有前述路线不满足需求且成本评估通过，才立项 PyAV demux + 自管解码或 NVDEC DLL 路线；不能同时声称“PyAV 只 demux”又依赖其负责解码会话。

**产物**：可独立运行的最小 PoC（建议 `scripts/poc_nvdec_zerocopy.py`，实现后登记真实参数），记录真实 surface 格式、数据驻留、生命周期、显式回退与处理字节。至少覆盖 NV12；声明支持 10bit 时加 P010 和 HDR metadata。

**验收**：L2 真机证明 decoder→渲染消费的原始帧不经过 CPU；若需 GPU 内部复制则如实列出。输出与同一解码器下载参考验证，跨解码器差异须预定义容差；性能阶段与 correctness 下载阶段分开。EOF/seek/错误/取消后资源可归还；未过门禁不进入 B2。

### B1.2 互操作与路线选择

比较现生产 CPU planar、HW download planar 和候选驻留路径；输出尺寸、效果、缓存和计时终点必须一致。只对 B1.1 可行候选继续评估：现有 GL 路线所需的 D3D11→GL 或 CUDA→GL，以及已有 D3D11 合成路线的最小缺口。

- `D3D11GLBridge` 默认无 interop factory 时不能视为可用；`edit_gpu_preview_renderer.py` 的 `_wrap_external_gpu_planes()` 只包装 GL texture ID，必须先产生合法 GL 资源。
- WGL/CUDA API 是否支持 NV12/P010、array texture、共享标志、设备匹配和同步必须真机探测；“可注册纹理”不等于两平面可被正确采样。
- 测 device removal/失效：先可控故障注入，再按设备/系统允许的方式做真机故障恢复；不把某个删除设备 API 当成可移植的驱动重启模拟。

**验收**：有一致终点的同机矩阵、资源上限和失败行为。明确选择一条先接生产；如无端到端收益，可保留 CPU/下载路径并结束 PoC。纯 D3D11 的 shader/生产链未就绪时标依赖 B3/B4，不能提前记为路线已跑通。

## Phase 2 · PoC 通过后接生产

### B2.1 预览消费与回退

**依赖**：B1.1/B1.2；与 A 约定帧身份、租约、telemetry 和 source-kind 契约。生产当前复用的 `vt_planar` 分类不应直接套给 GPU 驻留帧；如需调整内部类型，审查所有消费者及兼容路径。

- 扩展现有 windows_hw 帧/桥接协议，明确内存驻留、像素格式、颜色/PTS、array slice、LUID、完成同步和幂等释放；由既有渲染线程/队列消费，不在 UI/脚本线程直接导入 GPU 资源。
- 路由先复用现有选择器与 `TOPOS_DISABLE_WIN_HW_DECODE` / `TOPOS_FORCE_WIN_HW_DECODE` 的诊断体系。新增驻留 opt-in 开关仅在需要时提出并默认关闭；定义与强制 CPU/禁用硬解/能力拒绝的优先级，保留既有值的兼容性。
- 能力矩阵同时验证 codec/profile/位深/色度/尺寸/设备。unsupported 和 bridge 缺失明确回退；运行中失败不能交付半帧或伪造后端标记，重建/回退须维护 generation、帧序和所有权。

**验收**：仅矩阵内支持项声明驻留；其余正确回退。按 §1 同机主要指标与回归限值验收，覆盖多轨/seek/最小化/尺寸变化/关闭，资源释放恰好一次。先 opt-in；默认启用另需 L3 真机覆盖和稳定性证据。

### B2.2 素材 × 三机能力矩阵

复用可用 harness，必要时新增 `scripts/bench_decode_matrix.py`；实际发现素材、生成内容指纹，不硬编码 72 个。每格记期望/实际后端、格式、硬件 session、首帧、吞吐/延迟、上传/下载字节、资源水位、证据等级和 fallback reason。

C 负责汇总并运行 RX 6800 项；B 和 A 分别产出本机原始数据，C 不代跑其他硬件。未知/失败/不支持分别编码，三机独立列；正式锚点仅收录可复现且满足完整性校验的结果。

### B2.3 AMD/Intel 扩展决策

设备创建成功只是必要条件。RX 6800 须通过实际格式解码、surface 消费、同步/引用寿命、同机 CPU 对比及失败恢复，才考虑从 NVIDIA 门扩展为能力门。Intel 需要自己的硬件证据；不要从 AMD/NVIDIA 结果推断。跨适配器复制或 LUID 不匹配显式拒绝/回退，不伪装零拷贝。

### B2.4 Topos GPU 解码 ADR

与 A2.4 共用接口草案；仅对应素材解码仍是瓶颈时启动。区分 rANS、重建、色彩/alpha、D3D11 compute 与纹理驻留，不把 Metal kernel 的存在等同 HLSL 可直接平移。成本/收益不成立可延后。

## Phase 3 · 条件立项：D3D11 合成与共享后端边界

### B3.1 复用与缺口 ADR

**依赖**：B0.3、B1.2 证据；与 A 会签。先复核已有 backend selector、D3D11 render graph、shader ID、native bridge 和测试，列出复用/补齐/不可用项。ADR 路径先检查现有编号，不预占 `0005`。

明确三项：①预览 GL interop 与纯 D3D11 导出的不同消费边界；②GLSL 权威语义、HLSL emitter、现有 shader registry 和效果 Catalog 的身份/参数映射；③CPU/HLSL constant-buffer 的真实 packing、寄存器槽、数组/矩阵/float3/bool ABI，用编译反射及字节对拍验证，不只写“16 字节对齐”。

是否需要新 `src/shared/gpu/d3d/` 由实际共享职责决定，不能照搬 Metal 包目录或命名即称 RHI。若为正确性必须扩容 native bridge 契约，先有版本与兼容策略。

### B3.2 单效果真实执行 PoC

从 grading 起，先完整可编译入口、resource binding、线程组/边界检查和常量布局，再做 GL/D3D11 同输入真机对拍。声明字符串或 mock `d3d11_renderer=True` 只算 L0/L1。

**验收**：L2；SDR 参考现有 `tests/color/test_metal_composition_grading.py` 的适用口径，HDR、噪声与邻域另定对应门槛。测试可检测实际 native 执行，不能回退 CPU 后仍标 D3D11 通过；无设备诚实 skip。完成吞吐、资源回收与无效 shader/设备丢失测试后，才扩 B4。

### C3.1 native/后端稳定性长跑

仅相关实现与短测通过后运行。A3 scheduler 对拍必须先有 Windows 构建/ABI 验证；D3D 路径长跑依 B2/B4 各自门禁，不互相冒充完成。

先短时压力，再按风险延至 10h。记录帧身份/内容差异、错误、进程和 GPU 资源趋势、停止后回落及环境；比较可重放事件与帧身份，不比较跨实现对象 id。定期长跑是后续执行安排，本计划不创建自动任务。

## Phase 4 · 条件扩展：效果和导出

### B4.1 HLSL 语义与效果能力

**依赖**：B3.2 通过。以运行时 Effect Catalog/Registry 和 GLSL 权威文本生成能力清单，与既有 `d3d11_effects.py`/`d3d11_shaders.py` 映射；先复用再补完整实现，不把固定“11 项”或 registry ID 齐全作为最终覆盖。

参数 schema、版本、纹理/采样槽、uniform packing、组合顺序、alpha、HDR 和噪声/随机种子分别验证；新增 HLSL emitter/dtype 变体须和 A 串行修改共享 manifest。复用 Metal 两层测试方法，但不把 MSL 当语义权威，也不默认复制所有数值阈值。生产遇到不支持的效果/插件必须完整回退或显式拒绝，不静默丢效果。

### B4.2 接通现有导出链，再评估预览推广

当前 `timeline_export.py` 已识别 d3d11，但合成入口仍有 `not_yet_implemented:d3d11_timeline_composite_integration`。本任务是接通并验证这条链，不是简单加枚举或移除拒绝分支。复用 `D3D11TimelineRenderer`、`D3D11EncodeSurfaceProvider` 和编码完成 fence，避免重复新建平行 exporter。

先验证最小支持素材/效果组合的 decode→render→YUV surface→NVENC（AMD 单独验证 AMF）与封装结果；编码器接受 texture 不代表像素格式、metadata、适配器和同步都正确。保持严格模式/自动模式可解释的失败与回退，不用 GL/CPU 结果冒充 D3D11。

**验收**：L3 真机导出文件可解码，帧数/PTS/音频/色彩 metadata、图像/效果对拍正确；取消/drain/磁盘错误/关闭无泄漏；相同编码设置下同机比较端到端和资源。预览推广另外验证窗口呈现/seek/多轨，不以导出通过替代。

### B4.3 DeckLink 原生 GPU 输出预研

与 A Phase 5 共用 ADR。只有 SDK/驱动/格式/适配器可行性成立、真实提交完成和失败语义实现后，才能启用 GPU 能力。当前占位返回 False 时不得关闭 CPU 回读；提交拒绝也不能被 Python 门面误判成成功。真机验收帧内容、色彩、同步、坏帧和资源水位，未具备条件可持续使用 CPU 路径。

## Phase 5 · 回归矩阵与长期维护

按实际支持能力覆盖 NVENC/AMF/QSV、H264/HEVC/其他可用格式的 profile/位深/帧结构；unsupported 显式记录，不把所有编码器都列成必须支持全部组合。覆盖窗口失焦/最小化、多屏 DPI、恢复、内存压力与长播放。

静态搜索只辅助发现 `transfer`/`to_ndarray`；运行时下载/上传字节与设备事件才是驻留证据，PyAV 内部自动下载也须计入。合法回退/监看回读单列，不通过关键词禁令删除必要兼容路径。

## 2. 现有资产与回归索引

| 范围 | 先复用的路径 |
|---|---|
| 解码/能力/诊断 | `src/shared/media/windows_hw/hw_planar_source.py`、`d3d11_decoder.py`、`native_bridge.py`、`capability.py`、`reasons.py`、`vendor_policy.py` |
| 帧/设备/租约/同步 | 同目录 `d3d11_frame.py`、`d3d11_device.py`、`d3d11_texture_pool.py`、`d3d11_sync.py` |
| 渲染/导出/interop | 同目录 `d3d11_gl_bridge.py`、`d3d11_render_graph.py`、`d3d11_shaders.py`、`d3d11_effects.py`、`gpu_encode_surface.py`、`d3d11_export_capability.py` |
| 后端/生产接线 | `src/shared/gpu/backend_types.py`、`backend_selector.py`；`src/features/edit/core/edit_gpu_decode_manager.py`、`edit_gpu_preview_renderer.py`、`timeline_export.py` |
| 逻辑门禁 | `.github/workflows/windows-logic.yml` 当前选择的套件；`tests/media/test_windows_hw_native_surface_contract.py`、`test_windows_hw_t0_chain.py`、`test_windows_hw_d3d11_device_pool.py` 等按改动补充 |
| 生产行为门禁 | A0 的 telemetry/present/seek 契约；设备路径须额外补真机成功、失败、取消与资源测试，不能只跑无 GPU CI |

库契约参考：[PyAV 18.1.0 HWAccel](https://raw.githubusercontent.com/PyAV-Org/PyAV/v18.1.0/av/codec/hwaccel.py)、[保留设备帧/下载分支](https://raw.githubusercontent.com/PyAV-Org/PyAV/v18.1.0/av/video/codeccontext.py)、[FFmpeg D3D11 描述](https://ffmpeg.org/doxygen/8.1/hwcontext__d3d11va_8h_source.html)。实现时对应实际安装版本核验 ABI，不把参考源码代替本机证据。

## 3. C 农场与工作包交接

| 任务 | 触发与交付 |
|---|---|
| C0.1/C0.2 | 环境可用即做分组首跑与 skip 审计；交日志、返回码、实收用例与缺项清单 |
| B2.2 | B/A/C 分别跑自己的硬件；C 汇总素材×设备×路径矩阵，不平均、不代替其他真机 |
| C3.1 | 对应实现短测/构建通过后长跑；交失败种子、帧差异、资源趋势、停止回落 |
| C5 | 关键路径/驱动/依赖变化后选择相关回归；不因一次测试通过强制解除所有条件 skip |

B→A：能力台账、帧/租约字段变更、主要指标、原始结果及失败路径；A→B：共同遥测 schema、呈现状态与 GPU query 关联规则。共享文件由一个任务修改并让另一线审查，按小变更合并，不默认执行 rebase 或长期积压到 Phase 末。

ZCode 适合资产引用索引、日志/skip 聚类、矩阵整理和明确契约下的局部实现；给定绝对项目/文件路径、PyQt6+ModernGL+GLSL 背景、`.clinerules`、允许修改范围和验收物。CLI/模型配置与结果回收先用小任务验证；独立复核后才扩大委派，不按未用 Token 反推工作量。

## 4. 立即可做与推进门禁

1. **现在可做**：B0.1 环境实录、B0.3 只读盘点；C0 分组收集。B0.2 检查脚本归档/计时后跑当前生产基线。
2. **PoC 门禁**：瓶颈假设、支持矩阵和主要指标先冻结；完成 B1 的设备帧与真实渲染消费验证后，才批准 B2 接线。
3. **架构门禁**：B3/B4 只在既有路线不足且有成本/收益证据时立项；桥接、shader/效果、生产链、真机性能分层验收。
4. **默认启用门禁**：L3 端到端正确性、主要指标/回归限值、失败恢复与稳定性均通过；否则保持 opt-in 或现有回退。DeckLink 未实现可独立延后。

## 修订记录

- 2026-10-22 v3：修正跨机 700fps、PyAV 设备帧访问、格式边界与 DeckLink 误判；补查现有 D3D11 契约/枚举/导出拒绝分支，改为复用资产和补齐生产能力；新增证据等级、同机指标及条件立项。
- 2026-10-22 v2：原函数级测绘版；v1：自总纲拆出的初版。
