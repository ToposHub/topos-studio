# C0.1 测试农场首跑报告（Windows 线 · 机器 B · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §C0.1。
> 驱动脚本：`testfarm/2026-10-24_machineB/run_groups.sh`；汇总器：
> `scripts/summarize_testfarm.py`；结构化产物：同目录 `G*.xml`（junit xunit2）、
> `G*.log`、`G*.rc`、`summary.json`。
> 环境：机器 B（见 `docs/perf_env_matrix_windows_2026-10.md`，commit `23405ac16`
> 时点树，src 无改动）。首跑前按项目 constraints 补装了缺失的 `pytest-qt==4.5.0`
> （登记为环境修复，非测试改动）。

## 1. 采集与分组

`pytest --collect-only`：**36081** 项收集（86.6s）。按域分 8 组顺序执行
（G1=windows-logic CI 对齐 13 文件；G2=media；G3=shared+export；
G4=edit+color；G5=composite+render+plugins；G6=app/integration/杂域；
G7=tests 根散置；G8=ui+video+visual+validation+vault+test_videos——G8 为
首跑时发现的目录遗漏补跑，如实记录）。全程 31 分钟。

## 2. 总量（8 组合计，junit 汇总）

| 指标 | 数值 |
|---|---|
| 执行用例 | 36311（含 G8；与单独收集数差 ~230 为分组重入与参数化口径差） |
| 通过 | **34262（94.4%）** |
| 失败 | 416 |
| 错误 | 1023 |
| 跳过 | 610（运行时聚类见 §4 与 C0.2 报告） |

分组的完整 rc/数字见 `G*.rc` 与 `summary.json`（每组含失败/错误/skip 的
用例名与 message 截断，junit 含全文）。

## 3. 失败/错误分类（C0.1 核心：既有失败 / 环境缺项 / 新增回归）

**新回归：0**（本批未改任何 src 代码；G1 逻辑组与 CI 同套件 228 全绿）。

单一主根因（**环境缺项**，占错误 100% / 失败 50%）：

- `libtopos_codec` 构建产物落后于绑定层：**1023 个 error + 208 个失败**全部
  指向缺失符号 `tc_mux_add_audio`。源码已含该函数
  （`native/topos_codec/src/mov/mov.c:985`），但实际加载的
  `native/topos_codec/build/win-release-zig8/topos_codec.dll` 为旧构建
  （`candidate_library_paths()` 实查）。修复=在有 zig/CMake 工具链的机器重建
  （`build_release_repo.sh` 为 macOS 路线；本机无 zig/cmake/ninja，不改环境，
  留作独立环境修复项）。修复后预计回收 ~1200 红项。

其余 **208 个失败**（~182 个非 topos-lib）分类抽样：

| 类别 | 数量（约） | 例 | 处置 |
|---|---|---|---|
| POSIX 路径假设 | 8+ | `test_fix_wd15g79_aud`（断言 `/opt/Topos`，Windows 得 `C:\opt\...`） | 既有失败：测试缺 win32 适配，登记待修（测试侧） |
| macOS 专属逻辑在 Windows 执行 | 7+ | `test_macos_vt_hw_capability`、`test_frame_provider_vt_zero_copy`、`test_cv_metal_bridge` | 既有失败：应 skipif win32（对应 C0.2 静态桶 os_macos 的运行时漏网） |
| 疑似依赖本机用户配置 | 2 | `test_backend_selector_config`（断言全新 config，得到含 `vulkan` 的既有段） | 待调查：可能读到本机历史配置文件 |
| 其余分散失败 | ~165 | edit 导出 9、ai_editing ~15、composite paint 4、decode 路由 4… | 既有失败：逐项清单在 `summary.json`，topos-lib 修复后需复跑再归因（部分当前失败可能同为陈旧 DLL 连锁） |

## 4. 运行时 skip 聚类（与 C0.2 静态审计对拍）

| 聚类 | 运行时 skip | 静态语句桶（C0.2）参考 |
|---|---|---|
| os_macos | **348** | os_macos 语句 91 条（1:N —— macOS 模块常为整模块 skip） |
| media_fixture_missing | 122 | 177 条 |
| optional_missing | 42 | importorskip 59 + tool_or_api 57 |
| other | 37 | unclassified 119 |
| topos_native_lib | 32 | 52 条 |
| qt_dep | 10 | 16 条 |
| gl_context | 8 | 149 条（本机 GL 可建，绝大多数实际执行——即静态 149 ≠ 运行时 8，符合"本机可跑"预期） |
| gpu_hardware | 6 | 1 条（复合条件归 OS 桶） |
| env_ci_gate | 3 | 8 条 |
| os_windows | 2 | 9 条（本机为 Windows，实跑为主） |

**对拍结论**：静态语句数是"跳过可能性"的存量，运行时数是"本机实际跳过"——
两者比值本身就是能力面证据（如 gl_context 静态 149 → 运行时 8，说明本机
GPU/GL 环境几乎全消化了这批门）。"本机可跑 / 需要其他 GPU、OS、素材或外设 /
合理排除 / 待调查"四桶结论见 C0.2 报告 §3–§4。

## 5. 交接

- C 机（RX 6800）复跑同一 `run_groups.sh` + `summarize_testfarm.py`，只比各自
  summary.json；macOS 桶预期同样大，gpu 桶行为按 AMD 分支差异记录。
- 修复项清单（按优先级）：① 在有工具链的机器重建 `topos_codec.dll`（回收
  ~1200 红项）→ 复跑 G2/G3 复归因；② macOS 系测试补 `skipif(win32)`；
  ③ POSIX 路径假设测试补平台分支；④ backend_selector_config 是否误读本机
  配置待查。
- 本报告不改任何测试/不解除任何 skip（计划 C0.1 红线）。
