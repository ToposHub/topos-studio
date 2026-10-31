# C0.2 skip 审计报告（Windows 线 · 机器 B · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §C0.2。
> 工具：`scripts/audit_test_skips.py`（纯 AST，不导入测试模块）；
> 原始数据：`scripts/test_skip_audit.json`（每条含 file/line/kind/category/detail）。
> 原则：静态 skip 语句数与**运行时** skip 用例数分别统计；保留安全能力探测，
> 不通过删测试或强解 skip 凑全绿；RX 6800 结果不替代本机设备验证。

## 1. 方法与口径

- 采集形式：`pytest.skip()`（区分模块级/函数内）、`@pytest.mark.skipif`、
  `@pytest.mark.skip`、`pytest.importorskip()`、unittest 风格 `@skip/@skipIf`。
- 归类：按"条件+理由"文本子串匹配（规则内置于脚本，顺序优先），脚本可重跑复现。
- 已知边界（如实声明）：①复合条件（如 `win32 and has_nvidia`）归入先命中的
  OS 桶，detail 保留全文可复核；②`unavailable/missing` 等泛词进入
  media_fixture_missing / optional_tool_or_api 桶，个别误分靠 detail 人工复核；
  ③f-string 动态 reason 以截断源码记录，归类按字面部分。

## 2. 静态审计结果（commit `e650fdccd` 时点，1890 个测试文件）

静态 skip 语句共 **766** 条，归类 647 条（84%），unclassified 119 条待调查：

| 类别 | 语句数 | 涉及文件数 | 释义 |
|---|---|---|---|
| media_fixture_missing | 177 | 74 | 真实素材/fixture 缺失（test_videos、GOP 素材、fixture id） |
| gl_context | 149 | 79 | 无独立 GL/ModernGL 上下文即跳（CI 无 GPU 常态） |
| unclassified | 119 | 49 | **待调查**（动态 reason 为主，见 JSON） |
| os_macos | 91 | 33 | macOS/Metal/VideoToolbox/IOSurface 栈（含 PyAV mac 分支） |
| optional_dep_importorskip | 59 | 42 | `importorskip` 动态可选依赖 |
| optional_tool_or_api | 57 | 43 | 外部工具/API（ffmpeg 二进制、浏览器、API key、numpy 变体） |
| topos_native_lib | 52 | 32 | libtopos_codec/dylib 未构建或 ABI 不符 |
| optional_dep_named | 19 | 13 | 具名可选包（cv2/ocio/LUT exporter/tracking 工具） |
| qt_dep | 16 | 10 | PyQt6/Qt 应用不可用 |
| content_or_ui_conditional | 9 | 4 | 内容驱动（"not 10-bit"）或 UI 前提（截图/字体） |
| os_windows | 9 | 7 | Windows 专属（含 nvidia/amd 适配器复合条件） |
| env_ci_gate | 8 | 6 | 环境变量门（TOPOS_RUN_* / CI gating） |
| slow_perf | 1 | 1 | slow/perf 标记 |
| gpu_hardware | 1 | 1 | 纯 GPU 条件（其余被复合条件归入 OS 桶） |

按形式：`pytest.skip` 431 · `mark.skipif` 238 · `importorskip` 86 · `mark.skip` 11
（unittest 风格 0）。

归类质量校准记录：首轮把 macOS Metal 栈 65 条误入 gpu_hardware（"metal" 关键字），
已修正入 os_macos；该教训已写入脚本规则注释。

## 3. 四桶结论（静态侧）

1. **本机（机器 B）可跑**：unclassified 之外的 media_fixture_missing /
   gl_context / optional_* 大多数条件在本机为假（素材在、GL 可建、依赖已装），
   对应用例应实际执行——运行时数字见 §4 对拍。
2. **需要其他 GPU/OS/素材/外设**：os_macos 91 条（macOS 专属）；
   os_windows 中复合"nvidia/amd 适配器"条件在 4090 上为真（可跑），
   在 C 机（RX 6800）与 CI runner 上分别走 AMD 分支/诚实 skip；
   external_device 类未单独成桶（DeckLink/VLC 关键词命中的语句已并入
   optional_tool_or_api，量少，详 JSON）。
3. **合理排除**：env_ci_gate（显式 opt-in 的真实生成/长跑门）、slow_perf、
   topos_native_lib（库未构建时 fail-closed 是设计行为，非测试缺陷）。
4. **待调查**：unclassified 119 条——抽样复核均为动态 reason（f-string 拼接的
   fixture/依赖缺失细节）与少量中文 UI 前提，**无发现"绕过失败"式 skip**
   （即没有 `pytest.skip("known failing")` 掩盖回归的模式）；逐条清单在
   `scripts/test_skip_audit.json` entries（filter category=unclassified）。

## 4. 静态 vs 运行时对拍（C0.1 首跑实测，机器 B · 36311 用例）

运行时 skip 合计 **610**，聚类（脚本 `scripts/summarize_testfarm.py`，与静态
规则同族）与静态桶对照见 `docs/test_farm_first_run_windows_2026-10.md` §4。
要点：

- **语句:用例 = 1:N**：os_macos 静态 91 条语句 → 运行时 348 用例（整模块
  skip 的模块级语句一条覆盖全文件）。
- **静态桶是"可能性存量"，运行时是"本机实况"**：gl_context 静态 149 条 →
  运行时仅 8（本机 GL 可建，门几乎全开）；media_fixture_missing 静态 177 →
  运行时 122（素材在库，剩余为按内容条件跳过，如 "not 10-bit"）。
- **四桶落定**（机器 B）：①本机可跑=收集项的 94.4%（34262 通过）；②需其他
  GPU/OS/素材/外设≈348（os_macos）+2（os_windows 在非 Windows 机复跑时）+
  素材缺失类 122 中的环境相关项；③合理排除=topos_native_lib 32（fail-closed
  设计）+env_ci_gate 3；④待调查=other 37（逐条在 summary.json）+ 静态
  unclassified 119 条。
- 未发现"绕过失败"式 skip；本机无强制解除任何条件。

## 5. 对计划的输入

- **不删除、不强制解除**任何条件 skip；C5 回归只按"关键路径/驱动/依赖变化"
  选择相关组。
- DeckLink/外设与 macOS 桶在 C 机复跑时同样审计一次（同一脚本），三机差异
  只以各自 JSON 对比，不做平均。
- topos_native_lib 桶（52 条）是 B 线后续任何"Topos codec 相关基准"的先导
  门槛信号：库未构建时这些用例的 skip 数量即为该能力的覆盖面下界。
