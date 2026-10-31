# Topos 项目整体审计与优化计划表（2026-09-20）

> 审计分支：`chore/edit`  
> 审计范围：`src/`、`apps/`、`tests/`、`plugins/`、`native/`、`scripts/`、`.github/workflows/`、依赖与项目文档  
> 技术基线：PyQt6 + ModernGL + GLSL，HDR float16/32，`products → features → shared → app`  
> 文档性质：当前代码基线的可执行整改台账；只记录本轮已获得代码、测试或仓库数据证据的问题。

## 0. 整改进度（2026-09-20 第七轮更新：全分辨率线程迁移）

前几轮已提交、且本轮已落地并完成复验的项（提交序列 2e395b9d → 8924ac53 → 当前工作区）：

| ID | 状态 | 说明 |
|---|---|---|
| P0-QA-01 | ✅ 完成 | test_cache_fix.py 重写为子进程隔离（实测本机 opencv 的 OpenEXR 编码器存在随机 SIGILL，详见新测试文件 docstring）；新增 tests/color/conftest.py 的 git-clean 门禁；test_cache_dir_not_under_src 的 parents 索引错误修正（原断言恒真）。**附带结论**：tests/color 合并运行的 Segfault 是「先建 standalone Metal/GL 上下文、后建 Qt 控件」的 macOS 平台竞态（最小组合无法复现，CI 用 ubuntu+offscreen 规避）；本机 OpenEXR 编码器应升级 opencv 轮子（P3 级环境项）。 |
| P0-COL-01 | ✅ 完成 | warm_tint.tglsl 改为裸参数名引用；shader_compiler 在命名空间改写后校验残留 `u_*` 并写入渲染诊断缓冲；新增 5 个内置 .tglsl 的 GL 3.3 真实编译门禁测试；合成循环对编译失败层从静默 continue 改为记录 layer_id/插件清单的 warning（P1-OBS-01 最小版同步落地）。 |
| P0-COL-02 | ✅ 线程迁移、请求控制与异步 readback 完成 | 新增不可合并的 `RENDER_FULL_RESOLUTION` 命令：源图上传、完整调色管线、同步与 GPU→CPU readback 全部在 `AsyncRenderer` 线程执行；`ImageEngine.render_full_resolution()` 通过 Qt event loop 等待结果，保持既有同步 API；单图 Apply、Layer UI、脚本和批量导出自动复用该路径。批量导出的 Composite pass 也优先投递到现有 `CompositeRenderer` 线程，只有 CLI/测试无 renderer 时才保留 standalone fallback。2026-09-20 新增 `request_full_resolution()`、request registry、按请求取消、queued/prepare/render/readback/done 进度信号和 timeout 主动取消；批量导出取消按钮已转发到活动 full-res request，阶段进度已接入全局批量进度条和状态栏。当前 full-res 优先使用 render-thread PBO/fence 读回，尺寸切换会先安全排空在途 DMA；PBO 初始化/入队/轮询失败自动回退到既有同步读回。失败清理旧结果、shader fail-closed、re-entrant guard 和 request/layer 诊断均保留。 |
| P0-COL-03 | ✅ 完成 | 通道契约定为「磁盘 BGR / GPU RGB」并修复两条上传路径；发现并修复 AI 完成/移除槽调用不存在的 `request_render()`（AttributeError 使预览不刷新、UI 进度条卡死）；预览与全分辨率路径均消费 AI 纹理。preview/full/export 像素 parity E2E 待 P2-TEST-01 的 GL 分层 harness，现有 seam 级契约测试先守卫边界。 |
| P0-COL-04 | ✅ 完成（方案 A） | bake 携带完整栈前缀逐层合成（含嵌套 baked 直通与 AI 消费）；新增前缀内容摘要台账 + 渲染入口校验自动解冻（覆盖全部下层变更路径）；摘要规范化目标层自身 is_baked 防止建库即过期。 |
| P0-COL-05 | ✅ 完成 | DIST_LABEL_PIXEL label→零像素序号 LUT 解码 + 向量化查表（同时替换 O(像素×采样) 循环）；手算 oracle 测试锁定（锚点选在无距离歧义的曲线像素与端部）。 |
| P0-CI-01 | ✅ 完成 | 新增 .github/workflows/pr-fast-gate.yml：core-gates（compile/pip-check/project/deliver/composite/color + 工作区洁净）required 候选；known-regressions（arch/app/edit/render/ui）watch + 翻正计划；nightly 全量 junit。分支保护需将 core-gates 设为 required。**复检补漏**：Qt 测试 job 补装 libegl1/libxcb 系统库（与 product-matrix 对齐，此前裸 runner 上 Qt 测试会 import 失败）。 |
| P1-OBS-01 | 🟨 关联链路已补 | 编译失败/层跳过均写入诊断缓冲与日志；AI 帮助、澄清、技能推荐、结果卡、任务工作区、技能表单装配失败现在会记录日志并插入可见 error card。新增 path-free 的 `request_id`、`layer_id`、`plugin_ids`，并补充稳定 `code`（如 `SHADER_UNDECLARED_UNIFORM`），从渲染请求贯通到 composition/effect-pass 编译诊断；导出 preflight 仍待做。 |
| P1-APP-01 | ✅ 完成 | 4 项 fixture 更新（绑定真实 _build_render_request）；In/Out 规格裁决为「单设入点出点默认 clip 末帧」，测试注明。 |
| P1-EDIT-01 | ✅ 完成 | 拖拽策略裁决为 BoundingRect 并同步测试。mcp_local_ipc 组合运行 ModuleNotFound：本轮三种场景复现均通过（tests/edit 全套 8328 passed / 0 failed、ai_editing 子域 2004 passed、architecture 隔离测试+victim 同进程配对），判定在当前代码状态（含此前 P0/P1 修复）下已不再复现——原审计失败疑为当时代码状态下的顺序污染；残留风险由 P2-TEST-01 的随机顺序/重复运行 CI 兜底，若再现按 sys.modules/meta_path 污染路径排查。 |
| P1-RENDER-01 | ✅ 完成 | 三项裁决落地：结构化错误文案、page_id 默认页迁移规则（空 → page_1）、导出/渲染背景 scaffolding 仅在定制时注入。附带修复 s0 真后端两例（同一根因）。 |
| P1-UI-01 | ✅ 完成 | HOST_OWNED_DOM_IDS 唯一清单建立，Apply 清理由清单生成（修复 background-style/controller 遗漏）。**复检补漏**：清理清单提为模块级 DEVTOOLS_STRIP_SELECTORS，契约测试改导入常量断言（此前测试正则抠 JS 模板源码，占位符化后失效）。DOM provenance 校验与已有文档迁移去重仍待做。 |
| P1-UI-02 | ✅ 完成（macOS 1 项显式跳过） | UI 套件 25 项失败全部裁决并清零：① AI 结果卡 16 项——消息优先排序保留，测试改按类型查找；② Keyframes 播放联动 4 项——保留 `_composition_active` 空态门禁并激活页面级 fixture；③ snap 99 帧语义与 i18n 文案固化。最近一次完整 macOS 套件为 `1960 passed, 1 skipped`；本轮又新增 1 个可观测性回归测试并单独通过。跳过项是 Qt 6.10 在大 UI 组合中切换 tabified dock style 的平台级 segfault，Linux PR Qt shard 保留真实验证。 |
| P1-THREAD-01 | 🟨 full-res/RAW/AI 协作取消已完成，生命周期收口待做 | stop() 返回值已消费：超时推迟销毁（进程级引用）+ 显式上报；AsyncRenderer 新增 stop event，并在预览层循环、视频帧入口、bake 前缀和 full-res 命令入口检查取消。2026-09-20 full-res 增加线程安全 request registry、`cancel_full_resolution()`、进度回传与 timeout 主动取消；RAW worker 增加取消事件，AI Mask worker 增加 generation token，关闭时先取消再 quit。视频解码统一 token、native provider 的更细粒度中断和关闭期强杀清理仍待 S2/S4。 |
| P1-DEP-01 | ✅ 完成 | pyproject 核心依赖下限与 requirements 对齐；新增 constraints.txt（验证环境全量传递闭包，82 项锁定）；pr-fast-gate 新增 clean-install-parity job：两个入口（`-e .` / `-r requirements.txt`）同一 constraints 下安装 + pip check + 核心依赖版本一致性断言 + 二进制依赖 import 冒烟。 |
| P1-PROD-01 | ✅ 完成 | 3 项失败清零：MenuBuilder 自 3f1c38f3 起 callback 解析失败跳过整个 action（防空壳菜单项）是较新的有意行为，测试 stub resolver 同步；新增空壳跳过行为规格锁定测试。physical-missing 21/21。 |
| P2-BENCH-01 | ✅ 完成 | 7 个 benchmark 脚本的个人绝对路径默认值改为必填参数/环境变量；脱敏测试向量保留；tests/media/results/*.json 历史产物归 P2-REPO-01。 |
| P3-DOC-01/02 | ✅ 完成 | README 目录树失效路径修正（session/resource_pool/cache_manager/video_decoder/exporter）；Python 版本口径统一（支持 ≥3.10，CI 3.10/3.12，macOS 26 建议 3.13+）。 |
| P3-QA-01 | ✅ 完成 | test_matrix 数据类改名消除收集告警（**复检发现 71a272e8 声称的改名实际被此前 checkout 回滚丢失、未随提交落地——本轮重做并核实**）；26 处 class-scoped 实例方法 fixture 转 @classmethod，PytestRemovedIn10Warning 清零；全仓收集告警为 0。 |
| P1-ARCH-01 | ⬜ 未开始 | 按原计划属 S4 阶段（1–3 周分批），本轮未动。复检附带发现：test_module_isolation 的 test_color_core_still_works 在隐藏 composite 目录时因 color→shared.effects→composite 依赖链失败，属同一 ARCH 域存量。 |
| P2-* 其余 / P1-OBS-01 完整版 | 🟨 部分完成 | P1-OBS 已补 UI 可见错误卡、日志、稳定 Diagnostic code 和 request/layer/plugin 关联；导出 preflight，以及 P2 静态工具、测试分层和仓库治理仍未开始。 |

### 复检结论（2026-09-20 第三轮）

1. **发现并修复历史提交失真**：71a272e8 提交信息声称的 test_matrix 改名实际未随提交落地（被更早的 `git checkout -- tests/` 回滚连带冲掉）。所有提交声明已逐项与 `git show --stat` 核对，无其他失真。
2. **pr-fast-gate 系统库缺口**：Qt 测试 job 缺 libegl1/libxcb 系列（product-matrix 有装），首次真实 CI 运行会全量失败——已补。
3. **mcp_local_ipc 组合运行失败**：三种场景复现均不失败（tests/edit 全套 8328 passed / 0 failed / 2 xfailed；ai_editing 子域 2004 passed；architecture 隔离测试+victim 同进程配对通过）。判定：在当前代码状态（含此前 P0/P1 修复）下已不复现，P1-EDIT-01 关闭；若在随机顺序 CI（P2-TEST-01）中再现，按 sys.modules 篡改 / meta_path / 目录物理重命名（tests/conftest_module_isolation.hide_modules）三类路径排查。
4. **P1 域收口状态**：P1-APP-01 / P1-RENDER-01 / P1-EDIT-01 / P1-PROD-01 / P1-UI-01 / P1-UI-02 / P1-DEP-01 全部 ✅；P1-ARCH-01（1–3 周分批）与 P1-OBS-01 完整版 / P1-THREAD-01 完整版按 S2/S4 阶段推进。

### 本轮实现与验证（2026-09-20 第四轮）

1. **UI/架构规格回归收口**：`timeline_shortcuts` 对轻量 ShortcutManager 做兼容注册，移除与 EditModule 菜单动作重复的 `edit.timeline.add_marker`；Preferences guardrail 纳入新增 `edit.ai_editing` 页面。`tests/architecture` 现为 `409 passed, 2 failed, 1 xfailed`，仅剩 P1-ARCH-01 记录的 app/shared 深层导入边界债务。
2. **P1-OBS-01 增强**：AI 面板可选卡片装配不再静默 `pass`；失败会记录组件名/异常并显示不含敏感路径的可见错误卡，保留对话流可用。对应面板/结果卡 focused 套件 `54 passed`。
3. **App/Render 组合复验**：最新一次 `tests/app tests/render` 为 `737 passed, 8 skipped`（约 94 秒）；此前并发跑 architecture 的文件隐藏竞态已确认不是产品导入缺失。
4. **Color P0 定向复验**：内置插件编译、AI 通道、bake 前缀、曲线羽化、缓存隔离共 `21 passed`；`compileall` 与 `.venv/bin/python -m pip check` 均通过。
5. **测试隔离补强**：macOS 上 tabified dock style 的 Qt 组合测试改为显式跳过并注明 Linux CI 验证，避免整套 UI 在本机 segfault；UI 上一轮全量实测 `1960 passed, 1 skipped`，耗时约 126 秒；本轮新增的可观测性测试另行通过。
6. **仍未完成**：P1-ARCH-01 边界拆分、P1-THREAD-01 在 RAW/AI/视频解码与关闭期的完整生命周期收口、P1-OBS-01 导出 preflight，以及 P2 静态工具、测试分层和仓库瘦身；full-res PBO/fence 已落地，真实硬件基线与 ROI/dirty-layer 等进一步性能项仍待补齐。

7. **本轮新增优化**：渲染请求现在生成无路径的本地 `render-XXXXXXXX` 关联 ID；composition shader 编译诊断同时记录 `request_id`、`layer_id` 和去重后的 `plugin_ids`，并通过工具 facade 输出。新增诊断字段规范化、序列化和真实插件编译失败关联测试。

8. **本轮补强**：编译诊断增加稳定的 `code` 字段，区分未声明 uniform、composition 驱动编译失败和 effect-pass 编译失败，工具层可以按代码消费而不依赖错误文本。

9. **本轮补强**：同步 full-res 渲染为每次请求生成关联 ID，并把它传到逐层 composition 与全局 grain shader 编译；导出失败不再出现“有诊断但无法定位到本次请求/图层”的断链。

10. **本轮补强**：full-res 渲染增加可恢复的 re-entrant guard，防止 UI 回调或取消检查触发嵌套 GL pass。

11. **本轮补强**：AsyncRenderer stop 增加 cooperative stop event；长渲染阶段在安全边界主动退出，避免只能等待整段 GPU/CPU 工作结束。

12. **本轮补强**：full-res composition shader 构建失败改为 fail-closed，立即终止本次输出并保留诊断，不再把缺少某个图层效果的图片当作成功导出。

13. **本轮复验**：App/Render 宽回归保持 `737 passed, 8 skipped`；依赖检查无 broken requirements，工作区保持 clean。架构门禁仍只剩历史 app/shared 深层导入债务的 2 项失败，未通过改 baseline 掩盖。

14. **本轮迁移**：新增 `RenderCommand.RENDER_FULL_RESOLUTION` 关键命令及结果/失败信号；`AsyncRenderer` 在线程上下文创建 full-res 源纹理、复用统一 composition pipeline 并 readback；`ImageEngine` 通过带超时的 Qt event loop 保持 API 兼容；批量导出不再直接进入 `_do_render_full_res` 或主线程 OpenGL。Composite 新增 `RENDER_EXPORT` 命令，将独立 export evaluator、CPU 源图上传和 readback 投递到 `CompositeRenderer` 线程。

15. **本轮复验**：Color/Composite 定向测试 `106 passed`；App/Render `737 passed, 8 skipped`；`compileall`、`git diff --check` 通过。真实 GPU full-res/batch parity 仍需在硬件门禁中补充。

16. **本轮补强**：full-res 优先使用 render-thread PBO/fence 非阻塞读回；批量项目切换分辨率时先排空在途 DMA，渲染线程关闭前也会最终排空或发出 `renderer_stopped` 终态，避免 UI 等待超时。

17. **本轮补强**：Composite live export 改为 CPU-only `GraphSnapshot`，由 `CompositeRenderer` 线程完成源图上传、求值与 readback；无活动 render thread 时才走 standalone fallback，UI 线程不再创建导出 context。

## 1. 结论摘要

项目已有很强的功能覆盖和大量回归测试，项目文档/序列化、Composite、Shared/Deliver 等主域总体稳定；当前最大风险不是功能数量不足，而是快速扩展后出现了三类断层：

1. **Color 渲染正确性存在发布阻断项**：内置插件可编译失败后静默丢失，AI 插件像素未进入最终合成，非底层 Bake 语义错误，全分辨率/导出路径与预览路径分叉且运行在 UI 线程。
2. **测试数量大，但默认发布门禁不可信**：共收集 25,495 项测试，但架构/UI/Render/App/Edit 已存在失败；Color 全套测试会进程级崩溃；部分测试会改写已跟踪文件。CI 又没有覆盖默认全局 Python 门禁，因此这些回归可以长期留在主线。
3. **架构与维护成本已超过安全阈值**：`app/shared` 深度依赖 feature internals，39 个 Python 文件超过 3,000 行，宽泛异常处理和静默 `pass` 大量存在，依赖与文档基线漂移，仓库/工作区产物体积过大。

当前建议：**先停止新增横向功能 1 个整改周期，按 P0 正确性 → P1 门禁/边界 → P2 可维护性/性能的顺序收敛。**

## 2. 当前健康度快照

| 领域 | 状态 | 审计结论 |
|---|---|---|
| 项目模型、保存与迁移 | 绿 | `tests/project` 424/424 通过；schema、未知 payload 保留、离线重连等基础较稳。 |
| Composite 核心 | 绿/黄 | 5,378 通过、10 跳过；核心覆盖强，但有测试告警及 App/UI 联动回归。 |
| Shared / Deliver | 绿 | 855 通过、4 跳过；本轮未发现阻断性核心回归。 |
| Edit 核心 | 绿（整改后） | 8,328 通过、24 跳过、2 xfail、0 失败（2026-09-20 复检：拖拽策略与 mcp_local_ipc 两项审计失败均已关闭）。 |
| Color 核心 | 绿（整改后） | 1,532 通过、4 跳过（排除 macOS 平台竞态崩溃路径后）；P0 像素正确性缺陷（插件编译/AI 通道/bake/羽化）已全部修复并有 oracle/契约测试守卫。 |
| UI / App 集成 | 绿（整改后） | tests/ui 当前 macOS 实测 1,960 通过、1 个平台级 Qt style 测试显式跳过；tests/app + tests/render 合并 737 通过、8 跳过。 |
| 架构边界 | 红（已隔离存量债务） | `tests/architecture` 当前 409 通过、2 失败、1 xfail；失败仅为 `app` 130 / `shared` 47 个深层导入边界债务，归入 P1-ARCH-01。 |
| CI / 发布门禁 | 黄（整改后） | pr-fast-gate 已建（core-gates + clean-install-parity + known-regressions + nightly）；分支保护 required 设置与 P1-ARCH-01 翻正仍待做。 |
| 性能架构 | 黄/红 | 有较多性能预算与专项基准，但图片全分辨率/批量导出仍在主线程同步 GL/readback，预览与导出路径未统一。 |
| 文档与依赖 | 绿（整改后） | README/pyproject/requirements 对齐；constraints.txt 已建并进入 CI clean-install 验证。 |

## 3. 审计证据与基线

| 检查项 | 结果 |
|---|---|
| Python 规模 | `src + apps + tests` 共 3,056 个 Python/GLSL 类文件，约 1,286,710 行 Python。 |
| 超大文件 | 155 个 Python 文件超过 1,000 行；39 个超过 3,000 行。最大文件包括 `commands.py` 16,082 行、`timeline_view.py` 15,708 行、`composite_viewport.py` 11,512 行。 |
| 测试收集 | 25,495 项，1 项收集期跳过；收集期另有 2 个 `PytestCollectionWarning`。 |
| 架构测试 | 400 通过、10 失败、1 xfail。 |
| 项目/序列化 | 424 通过。 |
| Edit | 8,309 通过、2 失败、24 跳过、17 deselected、2 xfail。 |
| Composite | 5,378 通过、10 跳过、193 deselected。 |
| Shared + Deliver | 855 通过、4 跳过。 |
| Color | 排除 `test_cache_fix.py` 和 `test_plugin_params.py` 的组合污染后 1,501 通过、4 跳过；完整套件发生进程崩溃。`test_plugin_params.py` 单独运行 10/10 通过。 |
| Render | 393 通过、3 失败、7 跳过、23 deselected。 |
| UI | 在 67% 处累计 20 失败、1,334 通过并因 `--maxfail=20` 停止。 |
| 编译/依赖 | `compileall` 通过，`pip check` 通过。 |
| 产品源配置 | 四个产品的 `verify_product_bundle.py --source` 均通过，但 physical-missing 行为测试仍有 3 个失败。 |
| 静态质量工具 | 当前环境和项目配置均未启用 Ruff、mypy/pyright、coverage、pip-audit、Bandit。 |
| 仓库资产 | 已跟踪 `bench_out` 837 个 blob、约 98.29 MiB；已跟踪媒体/二进制 374 个、约 171.66 MiB。 |
| 本地工作区 | `tests/` 约 30 GiB、`bench_out/` 约 54 GiB；Git 对象目录另有约 2.78 GiB 临时垃圾。此项是当前工作区数据，不等同于仓库克隆体积。 |

## 4. P0：发布阻断问题与立即优化项

| ID | 类型 | 问题与证据 | 影响 | 优化动作 | 完成标准 | 预计 |
|---|---|---|---|---|---|---|
| P0-COL-01 | 已确认产品缺陷 | 内置 [`plugins/warm_tint.tglsl`](../plugins/warm_tint.tglsl) 第 14 行使用 `u_strength`，而 [`shader_compiler.py`](../src/features/color/core/shader_compiler.py) 279-294 只按参数名替换 `strength`，把局部变量改名后留下未声明的 `u_strength`。真实 ModernGL 编译报 `undeclared identifier 'u_strength'`；[`async_renderer.py`](../src/features/color/core/async_renderer.py) 2209-2216 对失败程序直接 `continue`。 | 用户启用内置 Warm Tint 后整层可被静默跳过，预览/导出结果错误且无明确提示。 | 修正插件 ABI/命名空间规则；禁止插件源码同时声明局部同名变量和 `u_*`；扫描全部内置 `.tglsl` 做真实 GL 编译；编译失败必须生成可见诊断并阻止“成功导出”。 | 全部内置插件在 GL 3.3 编译通过；新增 warm_tint 像素 golden；任一插件故障不会静默丢层。 | 1–2 天 |
| P0-COL-02 | 主线程迁移、请求控制与 PBO/fence readback 已完成第一阶段 | 旧路径在 `image_engine.py` 中同步创建 GL 资源、渲染并 readback；现已新增 `RENDER_FULL_RESOLUTION` 关键命令，`layer_controller.py`、`deliver_controller.py`、Apply 与脚本通过 `ImageEngine.render_full_resolution()` 提交到 `AsyncRenderer`。兼容 API 仍同步等待结果，因此 UI 调度仍使用 Qt event loop。2026-09-20 已补 `request_full_resolution()`、请求注册/取消、阶段进度信号、超时取消以及 render-thread PBO/fence 读回；尺寸变化先排空在途 DMA，PBO 异常自动回退同步读回。 | 主线程不再创建 full-res GL 资源或执行 readback；真实硬件吞吐与跨尺寸批量导出仍需基线。 | 保持统一 `RenderRequest/RenderResult` 语义；在真实 GPU 上补 parity/吞吐基线，再评估 ROI/dirty-layer。 | 4K 单帧导出期间 UI 保持响应；取消有明确 `cancelled` 结果且不重入；进度覆盖 queued/prepare/render/readback/done；失败不返回旧帧；静态检查保证 UI 线程不直接调用 ModernGL；PBO 异常不改变失败/取消语义。 | 主迁移、请求控制和 PBO/fence 已完成；硬件基线 1–2 天 |
| P0-COL-03 | 已确认产品缺陷 | [`async_renderer.py`](../src/features/color/core/async_renderer.py) 2139-2151 只把 AI 结果纹理用于 matte 生成，真正合成在 2221 仍绑定 `current_source_tex`；[`image_engine.py`](../src/features/color/core/image_engine.py) 1430-1445 又把 RGB 结果转为 BGR，而上传函数 [`async_renderer.py`](../src/features/color/core/async_renderer.py) 5897-5960 声明输入为 RGB 并原样上传。全分辨率旧路径也未消费 AI 纹理。 | AI 超分/修复等插件显示“完成”但可见像素不变或通道错色；预览、保存、导出不一致。 | 定义 AI 插件输出的权威色彩/通道/alpha/尺寸契约；将 AI 输出作为该层真正 source；明确多 AI 插件串联顺序；统一 CPU 缓存和 GPU 上传；补预览/全分辨率/导出三路 parity 测试。 | 纯红/纯蓝探针无通道互换；AI 插件开关产生预期像素差；preview/full/export 在阈值内一致。 | 3–5 天 |
| P0-COL-04 | 已确认产品缺陷 | [`bake_layer`](../src/features/color/core/image_engine.py) 3999-4044 只把“原图 + 所选单层”交给 bake；[`async_renderer.py`](../src/features/color/core/async_renderer.py) 5672-5836 也只渲染该层。但普通渲染 2160-2189 将 baked 纹理视为“到该层为止的完整栈前缀”并替换当前下层结果。 | Bake 非底层图层时，下方图层效果被丢失；解冻前后画面不等价。 | 明确选择其一：A. bake 生成完整栈前缀；B. bake 只缓存该层算子并继续与当前 source 合成。建议 A，且 bake key 包含下层 revision、源图 hash、色彩上下文和插件版本。 | 3 层栈逐层 bake/unbake 前后 golden 一致；修改任一下层会使上层 bake 失效；重开项目仍一致。 | 3–5 天 |
| P0-COL-05 | 已确认算法缺陷 | [`image_engine.py`](../src/features/color/core/image_engine.py) 2049-2072 将 `cv2.DIST_LABEL_PIXEL` 的 label 当成 `y*w+x` 解码；OpenCV 返回的是零像素标签序号，不是原图线性坐标。 | 曲线逐点内外羽化大部分像素无法匹配采样点，局部 feather 参数近似失效。 | 建立 label→零像素坐标映射，或改用显式 Voronoi/最近采样点计算；对闭合曲线、重合采样、HDR 大图做向量化；补小尺寸手算 oracle。 | 不同控制点羽化宽度在像素图上可测；与 CPU oracle 误差受控；4K 性能预算达标。 | 1–3 天 |
| P0-QA-01 | 已确认门禁缺陷 | `tests/color/test_cache_fix.py` 直接在仓库根写 `test_cache_image.exr`，删除 `FileIO.CACHE_DIR`，失败时多处 `return` 而非断言；运行时在 `cv2.imwrite` 触发 `Illegal instruction`。Color 组合运行还在 Qt teardown 触发 `Segmentation fault`，但相关测试单跑通过。 | 默认测试可改写开发者数据/已跟踪文件并让整个进程崩溃；全量结果不可判定，发布门禁失真。 | 立即隔离该测试：只使用 `tmp_path` 与测试专用 cache env，禁止删除生产 cache；用 fixture 恢复全局状态；OpenEXR capability 不满足时显式 skip；Qt/GL 测试按进程分片；增加“测试后 git 必须干净”门禁。 | Color 全套重复 3 次无 crash；测试前后 `git status --porcelain` 一致；不得访问真实用户 cache。 | 1–2 天 |
| P0-CI-01 | 已确认流程缺陷 | `.github/workflows/product-matrix.yml` 仍是手动 template；现有 workflow 均为路径限定专项门禁。当前架构、UI、Render、App 回归没有一个统一的 PR required job。 | 已失败的门禁可以继续合并；大量测试数量不能转化为发布可信度。 | 新增快速必跑 CI：compile/pip check、architecture、project、Color safe shard、App/UI focused、Render、产品 physical-missing；夜间跑全量与 GPU/真实媒体；失败测试不得用更新 fingerprint 掩盖。 | 任一 P0/P1 回归会阻断 PR；主分支连续 7 天绿；CI 输出 junit、耗时和 skip 原因。 | 2–4 天 |

## 5. P1：高优先级架构、线程与可见功能问题

| ID | 类型 | 问题与证据 | 优化动作 | 完成标准 | 预计 |
|---|---|---|---|---|---|
| P1-ARCH-01 | 已确认架构回归 | `tests/architecture/test_boundary_guardrails.py` 记录的旧基线为 app 115/shared 46；当前实际为 app 130/shared 47。`app` 直接导入 Edit/Composite core/ui，`shared` 反向依赖 Edit core。 | 不更新 baseline 糊过去；先把新增 16 个 offender 清零，再按 facade/port 分批清偿旧基线。优先抽 `AudioModel/ChannelLayout/ExportFrameProvider/CompositeRenderPort` 到合法 shared contract。 | 新增 offender=0；随后每个迭代至少下降 10%；最终 app/shared 禁止深层 feature import。 | 1–3 周，分批 |
| P1-PROD-01 | 已确认门禁回归 | `tests/architecture/test_product_physical_missing.py` 3 项失败：Color/Composite/Edit 裁剪模式下动作表为空；源码配置验证虽通过，但行为验证失败。 | 修复模块发现与菜单贡献注册的 physical-missing 路径；将 source config、实际裁剪、冷启动、菜单/快捷键/偏好页注册组成同一个矩阵。 | 四产品真实裁剪包启动；只出现允许功能；关键 action 各至少一个 smoke 断言。 | 2–4 天 |
| P1-UI-01 | 已确认数据所有权缺陷 | [`html_page_deck.py`](../src/features/teaching_animation/core/html_page_deck.py) 注入 `topos-page-background-style/controller`，但 [`html_browser_preview.py`](../src/features/teaching_animation/ui/html_browser_preview.py) 402-412 的 DevTools Apply 清理列表未移除它们；对应 ownership contract 失败。 | 建立唯一 `HOST_OWNED_DOM_IDS` 清单，由注入与清理双方生成/引用；Apply 前做 DOM provenance 校验；已有文档迁移去重。 | DevTools Apply 永不把 host scaffolding 写回 authored HTML；重复 Apply byte-stable。 | 1–2 天 |
| P1-UI-02 | 确认回归，需先定规格 | UI 套件到 67% 已有 20 个失败：Keyframes 播放联动 4、snap 1、AI 结果卡/表单/动作一组、生成素材按钮文案 1。多数 AI 卡失败来自测试仍假定“卡片位于 stretch 前最后一项”，而 `_insert_message_flow_widget` 已改为消息优先排序；并非 20 个独立产品 bug。 | 对每个失败先做产品规格裁决：保留新消息排序则改测试为按类型/ID 查找；需要旧交互则修实现。对 Keyframes `_composition_active` gate、snap 到 99 帧语义、生成素材动作文案分别固化契约。 | `tests/ui` 全绿；测试不依赖布局下标；结果卡的 retry/adjust/start 用真实 signal E2E 验证。 | 2–4 天 |
| P1-APP-01 | 确认回归，需先定规格 | `tests/app/test_composite_entry_source_selection.py` 5 项失败：4 项 fixture 未补 `_build_render_request`，1 项 In/Out 期望 `(8,None)` 实际 `(8,99)`。 | 更新 fake page builder 或恢复兼容 API；明确 clip-local out point 是否默认 clip 末帧；不要仅放宽断言。 | App 套件全绿；timeline-backed Composite 的 request id、project、frame range 有集成测试。 | 1–2 天 |
| P1-RENDER-01 | 确认回归/规格漂移 | Render 有 3 项失败：HTML patch 错误文案、导出新增 page background scaffolding、v3 roundtrip 给空 `page_id` 填 `page_1`。其中后两项涉及持久化/导出语义。 | 为 HTML patch 错误码使用结构化 code；区分 authored HTML 与 runtime-injected HTML；为 `page_id` 默认迁移写 schema 版本规则。 | Render 套件全绿；旧 v2/v3 fixture roundtrip 行为在文档中明确。 | 1–3 天 |
| P1-EDIT-01 | 测试隔离/行为回归 | Edit 全套有 2 项失败：`mcp_local_ipc` 仅组合运行时出现 ModuleNotFound、时间线拖拽实现改为 `BoundingRectViewportUpdate` 而测试仍要求 `FullViewportUpdate`。 | 定位 sys.modules/sys.path/全局 import 污染；为拖拽 repaint 用视觉残影+帧耗时验收，选择正确策略后同步测试。 | Edit 分片与随机顺序运行均一致；拖拽无残影且 P95 帧耗时满足预算。 | 2–3 天 |
| P1-THREAD-01 | 请求级协作取消已完成，生命周期收口待做 | [`async_renderer.stop`](../src/features/color/core/async_renderer.py) 超时返回 `False`，但 [`file_controller.py`](../src/app/controllers/file_controller.py) 仍需统一消费；RAW/AI worker 在 [`image_engine.py`] 超时后仍有强杀路径。2026-09-20 已为 full-res 增加线程安全请求注册表、`cancel_full_resolution()`、`full_resolution_progress` 阶段信号和 timeout 主动取消；取消只影响目标 request，不设置 renderer 全局 stop。 | 将同一 cooperative cancellation contract 扩展到 RAW/AI/视频解码；关闭时若 renderer 未停，延迟销毁 GL owner 并给用户/日志明确状态；禁止强杀持有原生资源的线程。 | full-res 取消有 `cancelled` 终态、状态清理和进度结束事件；连续打开/关闭、推理中退出、导出中退出 100 次无 abort、无悬挂线程、无资源泄漏。 | 3–7 天 |
| P1-OBS-01 | 已确认可观测性缺陷 | shader 构建失败返回 `None`，调用方多处 `continue`；AI/UI 装配在 [`ai_edit_assistant_panel.py`](../src/features/edit/ui/ai_edit_assistant_panel.py) 3597-3598、3618-3620、3754-3755 等处静默 `pass`。 | 建立结构化 `Diagnostic(code, severity, scope, user_message, cause)`；只允许在明确可降级边界捕获；UI 卡片装配失败要显示 error card，渲染能力不匹配要进入导出 preflight。 | 关键链路不再出现“成功但没效果”；错误可从日志关联到 request/layer/plugin/run id。 | 3–5 天 |
| P1-DEP-01 | 已确认依赖基线漂移 | README 声明 PyQt6 ≥6.10；`requirements.txt` 有版本下限；但 `pyproject.toml` 多数核心依赖无下限，且注释又声称其为单一事实源。无 lock/constraints。 | 选择单一依赖源；生成按 Python/平台的 constraints 或 lock；核心/可选/系统依赖分组；CI 用 clean install 验证最低版本与锁定版本。 | `pip install -e .` 与 `-r requirements.txt` 得到兼容集合；四平台安装可复现；依赖漂移由 bot/审计 PR 管理。 | 2–4 天 |

## 6. P2：可维护性、性能与工程治理

| ID | 类型 | 问题与证据 | 优化动作 | 完成标准 | 预计 |
|---|---|---|---|---|---|
| P2-MOD-01 | 系统性技术债 | 39 个 Python 文件超过 3,000 行；`commands.py`、`timeline_view.py`、`composite_viewport.py`、`timeline_preview_renderer.py`、`edit_controller.py` 等承担过多职责。 | 按领域和数据流拆分，不做纯机械搬移：Command families、timeline interaction/render、decode/render scheduling、Composite 2D/3D/input、controller application services；旧入口保留薄 facade。 | 单文件建议上限 1,500 行；新增文件职责单一；拆分前后 API/像素/undo golden 全绿。 | 4–8 周，持续 |
| P2-ERR-01 | 系统性技术债 | `src/ + apps/` 约 3,536 个宽泛 `except Exception/BaseException`，另有 38 个 bare `except`；大量清理、装配、文件和 GPU 路径静默吞错。 | 先治理渲染/保存/导出/线程/AI 五个高风险域；捕获具体异常；使用 context manager/deferred garbage；为允许降级的捕获加 code、metric 和测试。 | 高风险域 bare except=0；静默 pass=0；宽泛捕获数量每迭代下降并有 allowlist。 | 2–6 周 |
| P2-STATIC-01 | 工程能力缺口 | 未配置 Ruff、mypy/pyright、coverage、Bandit/pip-audit。 | 先以新增/修改文件为范围启用 Ruff + 类型检查；逐步扩大；新增依赖审计和 changed-lines coverage，不追求一次性全仓清零。 | PR 必跑；新增代码无 lint/type error；高风险模块 changed-lines coverage ≥80%。 | 3–5 天起步 |
| P2-PERF-01 | 性能优化（PBO/fence 已完成第一阶段） | Preview/Full-res/Export/Bake 有多套实现，shader/纹理/色彩语义容易漂移；同步 readback 是 4K 热点。当前 full-res 已在 render thread 统一执行，优先使用 PBO/fence 非阻塞读回，并在能力不足时保留同步回退。 | 统一 render graph 与 capability manifest；预览只改变 quality policy，不改变效果语义；复用纹理池/FBO；补真实硬件 throughput/VRAM 基线；按 ROI/dirty layer 缩小重算；将 pass count、upload/readback bytes 纳入预算。 | 同一输入三路像素 parity；4K preview P95、导出 fps、VRAM 峰值均有固定机基线和回归阈值；PBO/fence 不改变取消/失败语义；跨尺寸批量导出无悬挂请求。 | 1–2 周（基线与下一阶段） |
| P2-TEST-01 | 测试设计债 | 测试数量极大但存在顺序依赖、真实缓存访问、写仓库、返回 bool 而不 assert、class fixture deprecation、旧路径 pyc 诊断噪声。 | 建立 `unit / qt / gl / real_media / benchmark` 分层；每层独立进程和临时目录；随机顺序/重复运行；禁止写 workspace；修 pytest 10 deprecation 与 return-not-none。 | 默认 suite 可并行、可重复；3 次结果一致；无 collection/return/deprecation warning。 | 1–2 周 |
| P2-CI-02 | CI 性能与覆盖 | 25k 测试若每次全跑成本高，当前做法转为多个孤立专项 workflow，缺少风险驱动选择。 | 建 manifest：目录/能力→必跑 suite；PR 快速门禁控制在 15 分钟；合并后全量 CPU；夜间 GPU/真实媒体/性能；缓存只缓存依赖，不缓存运行时用户数据。 | PR 15 分钟内给出高置信结果；夜间全量失败自动关联责任域。 | 1 周 |
| P2-REPO-01 | 仓库/工作区治理 | 已跟踪 bench 结果和媒体较多；本地 `tests + bench_out` 约 84 GiB；`.gitignore` 新规则无法移除已跟踪历史文件。 | 分类：小型 deterministic golden 保留；大媒体进 Git LFS/对象存储；bench 仅提交 manifest/汇总/可复现脚本；提供 dry-run 清理工具和保留策略；不要自动删除用户素材。 | 普通 clone 体积显著下降；CI 按 manifest 拉取所需 fixture；本地清理可预览、可选择、默认不破坏。 | 3–7 天 + 历史迁移 |
| P2-BENCH-01 | 可移植性 | 多个 `tests/benchmark/drive_*` 和 benchmark 脚本包含 `/Users/testuser/...` 默认路径。 | 改为必填 CLI 参数、fixture manifest 或环境变量；报告中只存脱敏相对标识和素材 hash。 | 全仓生产/脚本默认值无个人绝对路径；无参数时给可操作错误。 | 1–2 天 |
| P2-SEC-01 | 供应链与插件治理 | 已有插件 sandbox/凭据测试，但缺少持续依赖漏洞审计；插件编译/导出能力失败可静默降级。 | CI 加 pip-audit/OSV；插件 manifest 签名/hash/权限/后端能力进入 preflight；外部进程命令保留参数数组和 timeout；对可选 provider 明确联网边界。 | 高危依赖阻断发布；插件权限和导出能力可在 UI/manifest 查看；失败有稳定错误码。 | 3–5 天 |

## 7. P3：文档、告警与低风险清理

| ID | 问题 | 优化动作 | 完成标准 |
|---|---|---|---|
| P3-DOC-01 | README 目录树列出多个已不存在路径，如 `src/features/color/session`、`src/shared/gpu/resource_pool.py`、`src/shared/render/cache_manager.py`、`src/shared/video/video_decoder.py`、`src/features/deliver/exporter`。 | 从实际模块 registry 自动生成架构目录或增加文档链接检查。 | README 路径检查进入 CI，所有列出的源码路径存在。 |
| P3-DOC-02 | README 一处建议 Python 3.13+，项目元数据支持 ≥3.10，CI 主要验证 3.10/3.12，口径不一致。 | 明确“支持版本”和“macOS 推荐版本”，给出每个平台验证矩阵。 | README、pyproject、CI 三处版本一致。 |
| P3-QA-01 | `tests/media/test_matrix.py` 的 `TestCase/TestMatrix` 因构造器不能收集；Composite AI 测试返回 bool；多处 pytest 10 fixture deprecation。 | 重命名数据类、将 return 改 assert、更新 class fixture。 | pytest 收集/运行 warnings 为 0（第三方不可控告警单独 filter 并说明）。 |
| P3-GIT-01 | 当前本地 `.git/objects/pack` 有约 2.78 GiB 临时垃圾。 | 在确认没有并行 Git 操作后由开发者手动执行仓库维护；不纳入应用代码。 | `git count-objects -vH` 无 garbage；维护步骤写入开发文档。 |

## 8. 分阶段执行路线

| 阶段 | 时间盒 | 目标 | 必做项 | 退出条件 |
|---|---:|---|---|---|
| S0 稳定基线 | 1–2 天 | 让测试结果可信 | P0-QA-01、P0-CI-01 的最小版本；隔离 crash 测试；保存当前失败清单 | 默认快速门禁可重复，不改仓库文件，不进程崩溃 |
| S1 Color 正确性 | 3–7 天 | 消除错误像素和静默丢效果 | P0-COL-01/03/04/05；插件真实编译 corpus；像素 golden | 预览、bake、full-res、export 关键样例一致 |
| S2 渲染线程统一 | 1–2 周 | 消除 UI 同步 GL 和旧结果误导出 | P0-COL-02、P1-THREAD-01、P1-OBS-01 | 4K 导出 UI 响应；取消/失败/关闭可诊断且无泄漏 |
| S3 产品与 UI 收口 | 1 周 | 恢复架构/产品/交互门禁 | P1-PROD-01、P1-UI-01/02、P1-APP-01、P1-RENDER-01、P1-EDIT-01 | architecture/app/ui/render/edit focused 全绿 |
| S4 边界与工程化 | 2–4 周 | 降低变更风险 | P1-ARCH-01、P1-DEP-01、P2-STATIC-01、P2-TEST-01/CI-02 | 新代码边界零新增，PR 有静态与依赖门禁 |
| S5 模块化与性能 | 持续 | 控制复杂度并提升 4K 性能 | P2-MOD-01、P2-PERF-01、P2-ERR-01、P2-REPO-01 | 指标持续下降且不改变 preview/export 语义 |

## 9. 建议的首批任务拆分

| 顺序 | 任务包 | 包含 ID | 建议交付物 |
|---:|---|---|---|
| 1 | 测试隔离急救 | P0-QA-01 | `tmp_path` cache、OpenEXR capability gate、Qt/GL shard、git-clean test |
| 2 | 插件编译与诊断 | P0-COL-01、P1-OBS-01 | ABI 修复、全插件 compile test、UI/preflight error code |
| 3 | AI/Bake/Feather 像素正确性 | P0-COL-03/04/05 | 小型 deterministic pixel goldens + preview/export parity |
| 4 | 全分辨率异步化设计与实现 | P0-COL-02、P1-THREAD-01 | RenderRequest/Result 协议、取消、失败、PBO/readback 基准 |
| 5 | 恢复必跑 CI | P0-CI-01、P2-CI-02 | PR fast gate + nightly full/GPU matrix |
| 6 | 规格裁决周 | P1-UI-02、P1-APP-01、P1-RENDER-01、P1-EDIT-01 | 每个失败标记“实现错/测试旧”，逐项清零 |
| 7 | 产品边界与 physical missing | P1-ARCH-01、P1-PROD-01 | facade/port、菜单/快捷键/偏好注册矩阵 |
| 8 | 工程治理 | P1-DEP-01、P2-STATIC-01、P2-ERR-01 | lock/constraints、lint/type/security、异常 allowlist |
| 9 | 模块化与仓库瘦身 | P2-MOD-01、P2-REPO-01、P2-BENCH-01 | 分批重构 ADR、LFS/fixture manifest、清理工具 |

## 10. 统一验收门槛

### 正确性

- 所有内置插件在真实 GL 3.3 context 编译；不允许 `None + continue` 静默降级。
- Color 的 preview、full-res、batch export、bake/unbake 使用同一效果语义；HDR 值不被无理由 clamp。
- AI 插件和普通插件都有通道、alpha、色彩空间、尺寸和版本契约。
- 项目保存/加载、未知 payload、schema migration 继续保持现有全绿基线。

### 线程与资源

- ModernGL/OpenGL 只在所有者渲染线程执行；UI 不运行同步 readback/编码/探测。
- 取消、失败、切片、切项目、窗口关闭路径均释放 texture/FBO/buffer/worker。
- 不再使用 `QThread.terminate()` 作为正常兜底；超时必须保留 owner 生命周期并上报。

### 测试与 CI

- 默认测试运行不会写工作区、用户 cache、真实项目或固定绝对路径。
- PR 快速门禁必跑并 required；夜间全量覆盖 CPU、GPU、真实媒体和性能。
- 失败、skip、xfail 都必须有稳定编号和 owner；禁止只更新 fingerprint 掩盖架构债。
- 测试随机顺序和重复运行结果一致；运行后 `git status --porcelain` 为空。

### 性能

- 固定机记录 4K 25/30/60fps 的 preview P50/P95、drop rate、upload/readback bytes、VRAM 峰值、导出 fps。
- 优化必须在像素/音频/时间基准 parity 通过后才计为完成。
- 任何 preview 降级都通过明确 quality policy，不允许静默改变效果或导出语义。

## 11. 本轮未直接验证的事项

- 未在 Windows 真机、NVIDIA/AMD GPU、DeckLink、真实 HDR 显示器上做硬件验收。
- 未执行全部 25,495 项到自然结束，因为 Color 测试存在进程级崩溃，UI 又在 20 失败后按门槛停止。
- 未做第三方依赖在线 CVE 查询；本轮只确认“缺少持续依赖安全审计与锁定机制”。
- 未在真实导出 preflight、全分辨率异步渲染和硬件环境中验证新关联字段的端到端覆盖；这些仍属于后续 S2 工作。
