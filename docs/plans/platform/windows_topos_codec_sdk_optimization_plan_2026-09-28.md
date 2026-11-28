# TOPOS 编码 Windows 版与 SDK 优化计划（2026-09-28）

> 状态：**待 Windows 实机执行**。本文是执行清单，不是 Windows 验收报告。
> 范围：`native/topos_codec` 的编码、解码、`.toos` 图片、TRAW、MOV 容器、C/C++ 与 Python SDK。
> 边界：产品的 D3D11、NVDEC/NVENC、时间线 GPU 导出已有 [Windows GPU 计划](windows_optimization_plan_2026-09-11.md)与[全链路导出计划](windows_timeline_gpu_export_optimization_plan_2026-09-15.md)；本文只验证它们调用 TOPOS 库时的接口和数据正确性，不重复建设 GPU 后端。
> 执行环境：在 Windows 原生 x64 系统、MSVC 开发者命令行及干净测试机上运行；macOS 本地通过或 CI YAML 存在，均不能代替 Windows 结果。
> 状态记号：`准备`＝已有代码或脚本、仍需核实；`待做`＝需实现；`待验`＝实现已存在、缺 Windows 证据；`阻塞`＝缺硬件、素材或外部条件。只有证据归档后才能改成 `完成`。

> **执行状态（2026-09-28 实机）**：W01–W12、W15–W18、W20 已完成并有真机证据归档
> （`native/topos_codec/docs/windows/win_execution_status_2026-09-28.md` 及其指向的
> 报告/日志）；W13/W14/W19/W22 部分完成（遗留项见总结报告）；W21/W23 因缺证书与
> 发布机未做，发布定位维持 unsigned Preview；W24 未启动。本文件其余表格保留为
> 原始计划，不回写。

## 1. 当前基线与验收边界

| 领域 | 已有入口或事实 | Windows 当前缺口 | 本计划编号 |
| --- | --- | --- | --- |
| 原生库 | `native/topos_codec/CMakeLists.txt` 已有 MSVC `/utf-8`、C11 atomics、AVX2 独立编译单元、Win32 线程池、x64 ABI 测试 | `codec-matrix.yml` 将 Windows 设为必过，但仓库风险册 R-26/R-33 仍要求**真实 runner 首绿记录**；不能把工作流配置视为执行结果 | W01–W05 |
| Windows 内存指标 | `src/cli/quality.c::perf_peak_rss_kb` 的 Windows 分支当前返回 `0` | 性能报告会把“不可用”误写成 0 KB，内存优化无法验收 | W07 |
| 数值一致性 | golden、ABI、SIMD 分发、`/fp:precise` 已有测试与构建规则 | 需 MSVC Debug/Release 与 macOS/Linux 冻结向量对拍，包含无 AVX2 机器 | W03–W06 |
| 质量对照 | `benchmark_protocol.md`、`tools/bench_rd_vs_prores.py`、`tests/test_videos/` 已有口径与素材 | 现有报告不能外推为 Windows 同机 ProRes 对照；需原始输入、帧范围、位深一致 | W08–W13 |
| 预编译 SDK | `package_codec.py` 可放 DLL、`.lib`、头文件和 CMake config；`sdk_consumer_smoke.py` 可外部编译运行 | Windows 的干净机、目录搬迁、CRT 依赖、签名与用户文档仍无实机归档 | W16–W21 |
| Python SDK | `package_opensource.py` 可构建带平台标记的 wheel，`test_release_scripts.py` 有安装冒烟 | `.github/workflows/codec-matrix.yml` 的 `python-app` 只列 Linux/macOS；Windows wheel、独立 venv 与多 Python 版本需验 | W18–W20 |
| 发布状态 | `codec-release.yml` 生成 manifest 与 90 天 artifact | 尚非正式、可追溯的公开版本发布；签名、升级回滚、真实 Windows 下载后的验收缺证据 | W21–W23 |

**不预设所有格式都已支持。** 以 `native/topos_codec/docs/capability_manifest.json`、`docs/image/capability_manifest.json`、`tc_query_support` 和图片能力查询为真相源。当前 `.toos` 整数 GBR 图片没有 GBR16 档；RAW16 是 CFA 样本，Float16 是 HALF 浮点样本，不可互相冒充。视频 RAW 是 TOPOS 自己的 TRAW 路径；BRAW/R3D/ProRes RAW 的解码属于独立的上游输入适配器，不能把第三方 RAW SDK 直接算作 TOPOS codec 能力。

### 1.1 Windows 机器与证据目录

首轮至少一台 **Windows x64 + MSVC + 可关闭 AVX2 的对照环境**。性能基准固定一台可重复使用的 x64 机器，记录 CPU 型号、核心拓扑、内存、Windows build、Visual Studio/MSVC/Windows SDK/CMake/Python 版本、电源计划和后台负载；NVIDIA/AMD/Intel 机器仅在涉及产品集成时扩展。另用一台没有编译器、运行目标支持的 Windows x64 版本的干净机验证发布包。若当前无法取得非 AVX2 机器，先用测试钩子强制 scalar dispatch，再补真实 CPU 证据。

报告存入 `native/topos_codec/docs/windows/`，每次测试放同名 `.md` 与 `.json`：`win_codec_baseline_<机器>_<日期>`、`win_codec_quality_<机器>_<日期>`、`win_sdk_release_<机器>_<日期>`。JSON 记录命令、退出码、commit、DLL SHA-256、源码/母版 SHA-256、工具版本、完整样本结果；大体积素材和 ETW 原始 trace 放 `bench_out/`，报告保存相对路径与哈希。失败记录保留，不用下一次成功覆盖。

## 2. 优先级、依赖与任务表

### P0：先证明 Windows 编解码正确、稳定、可发布

| ID | 状态/执行地 | 工作项与具体改动 | 验收标准和证据 | 依赖 |
| --- | --- | --- | --- | --- |
| W01 | 待验／Windows | 固定 MSVC x64 构建环境；分别配置 Debug、Release，不把 Unix `-O2` 传给 `cl`。核对 `codec-matrix.yml` 和 `codec-release.yml` 的实际生成器、`--config`、DLL/`.lib` 路径。 | 两配置编译无错误；保存 `cmake --system-information`、`cl /Bv`、CMake cache 与完整日志；CI job URL/运行时间入报告。 | 无 |
| W02 | 待验／Windows | 执行 CTest 全集、`check_exports.py` PE 符号门、`dll_load_test.py`、CLI rawgen→encode→probe→decode，以及 `sdk_consumer_smoke.py`。 | 每项真实退出码 0；ctest 总数与跳过清单入 JSON；任一非预期跳过或 DLL 加载失败不得标完成。 | W01 |
| W03 | 待验／Windows | 核对 `topos_codec.h`/`topos_image.h` 的 x64 结构大小、对齐、`struct_size` 兼容、错误码、公共符号；用 C 与 C++ 外部消费者测试 Debug/Release DLL。 | ABI v2 与冻结向量一致；新旧头文件消费者在声明兼容域内均可加载；失败给明确状态码和 `tc_last_error()`。 | W01 |
| W04 | 待做／Windows | 对 Win32 线程池、`InitOnceExecuteOnce`、SRWLOCK/condition variable、取消和并发扩容做 1/2/4/8/逻辑核数线程压力；复现“先高线程、降线程扩容、再升线程”的槽位回归。 | 连续多轮无越界、死锁、内存增长；Windows ASan 运行故障注入与恶意包回放；失败可定位到样本和调用栈。MSVC ASan 支持范围按[微软文档](https://learn.microsoft.com/en-us/cpp/sanitizers/asan)核实。 | W02 |
| W05 | 待做／Windows | 检查 AVX2/标量运行时分发：CPUID+OSXSAVE 判定、非 AVX2 CPU、`/arch:AVX2` 单元未泄漏到通用代码；比较 `fastdiv.h` 的 MSVC 除法兜底。 | scalar 与 AVX2 的解码像素、编码位流在冻结域逐位一致；非 AVX2 机器无非法指令；差异先定位数值语义，再谈优化。 | W03 |
| W06 | 待验／Windows | 跨平台 golden：10/12/16-bit 视频、YUV422/444/GBR、Alpha 各模式、LP 微 GOP、TRAW12/16、图片整数/Float16、MOV 多音轨/时间码/元数据、损坏与截断输入。 | Windows 编码文件在 macOS/Linux 可解；反向可解；冻结格式的像素、metadata、帧数、音轨顺序、时间码和哈希符合规范；不支持格式有稳定拒绝码。不得因 `/fp:fast` 改变位流；浮点编译行为参考[微软 `/fp` 文档](https://learn.microsoft.com/en-us/cpp/build/reference/fp-specify-floating-point-behavior)。 | W02–W05 |
| W07 | 待做／Windows | 给 `quality.c::perf_peak_rss_kb` 实现 Windows 进程内存测量，并在报告中写明是工作集、私有字节还是提交量；同时记录峰值句柄数和线程数。 | 不再返回伪 0；与任务管理器/系统 API 交叉校验；1000 帧及长序列开关循环有峰值与收敛报告。 | W01 |
| W08 | 待做／Windows | 验证 `_fseeki64`/`_ftelli64`、4 GiB 以上 MOV、中文/空格/长路径、只读目录、磁盘满、短读短写、并发同目标、取消与失败回滚。CLI 路径入口优先验证 Unicode 宽字符语义。 | 文件完整或明确失败；无半成品覆盖、无越界、无句柄泄漏；路径限制按[微软文件路径文档](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)区分“系统支持”与“本程序已启用”。 | W02 |

### P1：画质与性能对照，再按证据优化

| ID | 状态/执行地 | 工作项与具体改动 | 验收标准和证据 | 依赖 |
| --- | --- | --- | --- | --- |
| W09 | 待做／Windows | 建立**同一原始母版**矩阵：`tests/test_videos` 指纹核对，预解码成固定平面输入；视频 2K/4K、10/12/16-bit、422/444/GBR、Alpha、HDR、运动/纹理/噪声；图片 `.toos` 整数、CFA12/16、HALF16 分开。 | manifest 包含母版哈希、像素格式、码值范围、颜色矩阵/transfer、帧区间；缺原生 12/16-bit 母版时标记缺口，8-bit 升位只能测兼容性，不能宣称高位深画质。 | W06 |
| W10 | 待做／Windows | ProRes 对位实验：TOPOS 422 Proxy/LT/422/HQ ↔ ProRes 同名档；TOPOS 4444/XQ ↔ ProRes 4444/XQ。422 LP 单列为帧间档，不混入帧内 RD 排名；TRAW 与 ProRes RAW 只在解码语义、传感器处理可对齐时另设实验。 | 同母版、帧数、位深、chroma、range、色彩 metadata、Alpha 条件；同时给同码率（误差≤5%）与同画质插值曲线，不只报预设名；无对应规格则 `N/A`，不能强配。 | W09 |
| W11 | 待做／Windows | 画质评分同时覆盖 Y/U/V 或 GBR 分通道 PSNR/SSIM、边缘/纹理/色偏、Alpha bit-exact 或误差界、1/3/5/10 代、多代漂移；HDR/Float16 另做线性光与码值域检查，按格式规范检验 NaN/Inf/负值的处理。 | 每条曲线有原始帧样本与误差图、指标算法版本和异常帧索引；与 macOS 参考报告对拍。评分工具先验证不把 12/16-bit 无声压成 10-bit。 | W09–W10 |
| W12 | 待做／Windows | 编码/解码性能拆分：预解码输入后分别计时 `encode`、`encode_sized`、decode、seek、mux、图片 encode/decode；记录 cold/warm、p50/p95/p99、FPS、峰值内存、线程数。 | 首先建 Windows 原生基线，**不套用 macOS 绝对毫秒阈值**；5 轮交替 A/B，保留每轮原始值和负载。瓶颈分析可用[Windows Performance Recorder/Analyzer](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/introduction-to-wpr) 的 CPU/线程 ETW。 | W07、W09 |
| W13 | 待做／Windows | 按 W12 热点实施小步优化：优先确认 MSVC `fastdiv` 兜底、AVX2 dispatch、线程池锁争用与任务颗粒、sized 搜索次数、逐帧分配和跨线程内存流量；每项独立开关及回退。 | 每项有前后同机 A/B：编码速度、解码速度、p95 尾延迟、码率、W11 画质、golden、RSS 同报；没有稳定收益或改变冻结输出则回退。 | W11–W12 |
| W14 | 待做／Windows | 跑 2K/4K 422 HQ、4444、XQ、RAW12/16、Float16 图片的长时间压力与真实产品入口（导出→重导入→seek）；验证 TOPOS 调用链没有额外 CPU↔GPU 拷贝或误报 GPU 加速。 | Native codec 与产品总耗时分开；产品层问题转交既有 Windows GPU 计划，本文记录接口与成本归因。 | W12 |

### P0/P1：Windows SDK 的可安装、可链接、可升级

| ID | 状态/执行地 | 工作项与具体改动 | 验收标准和证据 | 依赖 |
| --- | --- | --- | --- | --- |
| W15 | 待验／Windows | 从 Release 构建打包 `topos_codec.dll` + `topos_codec.lib`、公共头、LICENSE/NOTICE、规范、CMake config、示例、BUILDINFO/哈希清单；核对 PE x64 和导出清单。 | 包内 `.dll` 和 `.lib` 均存在，哈希全复算；`ToposCodec::topos_codec` 对外部 C/C++ 项目可链接。CMake Windows imported shared target 需要 import library，见[CMake 文档](https://cmake.org/cmake/help/latest/command/add_library.html)。 | W01–W03 |
| W16 | 待验／Windows | 在仓库外、含空格和中文的解压路径中运行 CMake `find_package(ToposCodec 0.1 CONFIG REQUIRED)`、C/C++ 编译、运行、目录搬迁后重跑；另测 Debug 消费者链接 Release SDK 的支持边界。 | 不使用主仓库头文件/库；不修改全局 PATH 也能按文档加载，或文档明确部署 DLL 的位置；版本不兼容时配置阶段报错。 | W15 |
| W17 | 待做／Windows | 明确 MSVC CRT 策略（`/MD` 与 `/MT`）和运行库最低要求，扫描 DLL 依赖；在没有 Visual Studio 的干净机运行。 | SDK 文档写明系统/VC runtime 要求；丢失依赖有可诊断信息；不得把 Debug CRT 放入发行版。CRT 选择与链接限制参考[微软运行库文档](https://learn.microsoft.com/en-us/cpp/build/reference/md-mt-ld-use-run-time-library)。 | W15–W16 |
| W18 | 待验／Windows | 生成 Windows x64 自包含 Python wheel，并用独立 venv 安装；验证 wheel 是平台包而非 `py3-none-any`，且不依赖源码树。增加 3.10/3.12/当前受支持版本矩阵，按实际支持范围收缩声明。 | `pip install` 后 `ToposCodec` 与 `ToposImageCodec` 加载、encode/decode、失败码、资源关闭成功；卸载后无残留；wheel tag 符合[Python 平台兼容标签规范](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/)。 | W15 |
| W19 | 待做／本机准备+Windows 验 | 把 Windows 加入 Python SDK/应用测试矩阵；优先运行不依赖 GPU 的 native binding、图片、MOV、export、序列、wheel 安装测试；GPU 用例仅在专用 runner 探测后启用。 | `python-app` Windows job 有真实绿灯与测试数量；缺素材显式 `BLOCKED/MISSING_ASSET`，不得把核心绑定整组 skip 算成功。 | W18 |
| W20 | 待做／Windows | 做 SDK API 上手审查：能力查询、错误码、内存所有权、回调寿命、线程安全、关闭/取消、半成品清理；提供 Windows PowerShell 快速开始、最小 C、C++、Python 实例。 | 新开发者仅凭发布包文档能在 30 分钟内完成 encode→MOV→decode 和 `.toos` roundtrip；示例均在干净机自动跑。高层封装不能绕过 C ABI 能力协商。 | W16、W18 |
| W21 | 待做／Windows 发布机 | 正式版对 DLL/CLI/安装器制定代码签名与时间戳流程；证书仅注入发布作业，不进入仓库。签后重算 manifest，再用 `signtool verify /pa` 验证。 | 正式发布产物签名有效、证书链与时间戳可验证；无证书的测试包显式标记 unsigned Preview，不能冒充正式签名。[微软 SignTool 文档](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool) | W15–W20 |
| W22 | 待做／Windows | 版本升级/回退：旧 DLL+新调用方、新 DLL+旧调用方、旧 `.mov/.toos` 新解码器、失败安装后恢复；维护 ABI 版本、语义版本与格式兼容矩阵。 | 每个公开版本保留上一版二进制与 golden 回归；ABI 不兼容必须在加载/配置时明确拒绝，绝不以结构体错位继续运行。 | W03、W15 |
| W23 | 待做／发布环境 | 把 CI artifact 提升为可下载、可校验、可复现的版本发布：x64 ZIP、Python wheel、SHA-256 manifest、changelog、许可证、已知限制、依赖清单；先 Preview，再考虑稳定版本。 | 至少一次在干净 Windows 机**从下载包**完成 CMake、Python、CLI 三路线；将下载地址、commit、哈希、签名/未签名状态和 CI run 绑定。 | W15–W22 |
| W24 | 可选／需求触发 | 在 C/C++、Python 稳定后再做 Rust FFI、.NET P/Invoke、FFmpeg/OIIO 等适配；每种适配单独声明平台、能力域与版本策略。 | 不把实验性补丁写成官方可安装插件；每个适配有独立使用示例、生命周期测试与 CI。 | W22–W23 |

## 3. Windows 首次切换的操作顺序

1. **保留现有工作区状态。** 切换前提交或备份正在进行的 SDK 修改；`bench_out/` 等未跟踪基准数据单独保存。Windows 与 macOS 使用同一 commit 和相同素材 SHA-256 对拍。
2. **准备工具。** 安装受支持的 Visual Studio C++ Build Tools、Windows SDK、CMake、Python、Git；在 VS x64 开发者命令行中记录 `cl /Bv`、`cmake --version`、`python --version`。不要先用 MinGW 的成功代替 MSVC 验收。
3. **建立 Debug 正确性基线。** 在项目根目录执行下列 PowerShell 命令；示例使用 Visual Studio 2022 生成器，若装的是其他版本，先替换为 `cmake --help` 列出的实际名称。Windows 使用独立的 `.venv-win`，不要复用 macOS 的 `.venv`。第一次只解决编译和测试失败，不做性能调参：

   ```powershell
   cmake -S native/topos_codec -B native/topos_codec/build/win-msvc -G "Visual Studio 17 2022" -A x64 -DTOPOS_ENABLE_TESTS=ON
   cmake --build native/topos_codec/build/win-msvc --config Debug --parallel
   ctest --test-dir native/topos_codec/build/win-msvc -C Debug --output-on-failure
   py -3 -m venv .venv-win
   .\.venv-win\Scripts\python.exe native/topos_codec/scripts/check_exports.py native/topos_codec/build/win-msvc/Debug/topos_codec.dll
   .\.venv-win\Scripts\python.exe native/topos_codec/scripts/dll_load_test.py native/topos_codec/build/win-msvc/Debug/topos_codec.dll
   ```

4. **冻结 Release 与 SDK 基线。** 使用 `--config Release` 重新构建；按 `codec-release.yml` 调用 `package_codec.py --lib .../Release/topos_codec.dll --import-lib .../Release/topos_codec.lib --dist ... --build-dir ...`；再运行 `sdk_consumer_smoke.py`。如果实际生成器路径不同，先在报告中记录，再修脚本，不手工改名掩盖问题。
5. **再跑质量与性能。** 先核对母版和 ProRes/TOPOS 对位输入（W09–W11），再建立原生编码、解码、产品导出三个独立计时层（W12–W14）。任何优化必须保留未修改版本作 A/B。
6. **最后做发布机验收。** 在没有源码、编译器和现有 venv 的机器上解压正式候选包，执行 W16–W23；签名、运行库与下载包哈希在这一步关闭。

## 4. 画质与性能实验的硬性约束

- **正确性先于速度。** Debug/Release、scalar/AVX2、Windows/macOS 之间的冻结格式必须先过像素和位流兼容门。若优化会改变有损输出，只能作为新实验档位，不能偷偷修改既有档。
- **ProRes 对比是同输入、同规格、双轴报告。** 同名档只表示预期用途；实际对比必须另报同码率画质、同画质码率、编码耗时、解码耗时、Alpha 和多代退化。不可用 ProRes 8/10-bit 素材升位后的分数宣称 TOPOS 原生 12/16-bit 优势。
- **Windows 性能阈值要实测后冻结。** 共享 CI 只拦结构性回退；专用基准机才用严格门。参考 `native/topos_codec/docs/benchmark_protocol.md` 的环境块、cold/warm 与交替 A/B 规则。报告同时写 p50/p95/p99、峰值内存、CPU/线程、码率和画质；只报平均 FPS 无效。
- **格式域分别验收。** 视频整数 10/12/16、图片整数、RAW CFA12/16、Float16 HALF、Alpha16、MOV 多音轨是不同语义；`tc_query_support` 返回不支持的组合必须记录为正常拒绝，不以强行转换冒充支持。
- **第三方 RAW 输入单独记账。** R3D/BRAW/ProRes RAW 的依赖许可、插件部署和输入转换在产品适配层管理；TOPOS SDK 发布物仅包含有权分发且实际使用的组件。

## 5. 完成定义与阶段门

| 阶段 | 必须完成 | 未满足时的发布结论 |
| --- | --- | --- |
| G0 Windows 可运行 | W01–W08；MSVC Debug/Release、CTest、golden、ABI、损坏/并发/大文件测试有真机记录 | 只能说“具备 Windows 构建代码”，不能说 Windows codec 已验收 |
| G1 Windows 品质/性能 | W09–W14；真实母版、ProRes 对照、Windows 同机基线与至少一轮 A/B | 不能宣称 Windows 更快或画质优于对应 ProRes 档 |
| G2 SDK Preview | W15–W20；干净机 C/C++、Python、图片和视频示例，真实 Windows CI | 可称 Windows SDK Preview；不得称完整正式发布 |
| G3 正式发布候选 | W21–W23；签名/未签名状态清楚、下载包哈希、升级回退、版本兼容 | 仍有缺口时维持 Preview；W24 是按需求扩展，不阻塞 C/Python 核心发布 |

**首个 Windows 工作日的优先级：W01 → W02 → W03 → W15 → W16 → W18。** 这条链先回答“能否构建、能否链接、能否在干净环境运行”；随后处理 W04–W14 的稳定性、画质与性能。任何步骤失败，保留日志并修复当前门，不跳到下一阶段。
