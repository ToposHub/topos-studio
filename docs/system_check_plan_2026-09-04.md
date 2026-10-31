# 全项目系统检查计划（第二轮审计）

> **✅ 执行状态（2026-09-06 更新）**：R0 全部、R1 全部、R2 全部（R2-3 随 R0-5 合并完成）、R4 全部、R3-1（渲染核心三文件）与 R3-5 已完成，commits `94cd639b..`（chore/color 分支）。**R5 三个专项全部完成**：ARCH-1（main.py 1,995 行 → 组合根 117 行 + webapp/db/bridge/thumbs/routes/window/__main__ 七模块，路由表逐条一致，双视图单例共享，waitress 真实 HTTP 实测）；ARCH-2（BasePlayer + DecoderBackend 统一 topos/libav 骨架，真实夹具播放+精确 seek 实测）；ARCH-13+FUNC-16（escapeHtml 全量铺开 + index.html 93 处 onclick 清零改 data-action 委托 + script.js 拆 state/render/actions 三模块 IIFE，浏览器 GUI 全链路实测）。R3-1 其余文件与 R3-2/3/4/6 按特性开发渐进。
> 遗留发现：① tests/ui / tests/app 存在 28 个存量失败（AI result card / keyframes / composite entry source 区域），经 worktree 基线验证早于本轮工作，需另立批次；② boundary guardrails 基线已按协议更新至现状（app 74→115、shared 内容漂移），清偿属 R3/架构批次；③ edit_controller 顶层 edit 导入破坏物理 color-only 构建，已标记 xfail；④ hdri/INDREF-023.jpg (8.8MB) 转 LFS 需基础设施决策，暂缓。

> 审计基线：commit `5c7024cc`（2026-09-04，M5 架构批次完成后）。行号基于该时点，后续改动会漂移，以符号名为准。
> 审计范围：全仓库（上一轮 docs/file_explorer_upgrade_plan.md 只覆盖 file_explorer 与集成层，本轮扩展到 src 主应用外壳、src/features 功能层、工程卫生）。
> 审计方式：四路并行调研（file_explorer 残留与新回归 / 主应用外壳 / src/features 普查 / 工程卫生）。**标注〔已核实〕的条目均经过人工逐条代码核实**，其余为调研结论、动手前先复核。
> 工作量标记：S ≤0.5 天｜M 1~2 天｜L ≥3 天。旧计划 ID（SEC/CRASH/FUNC/UX/ARCH）继续沿用，本轮新发现用新前缀。

---

## 0. 总体结论

| 维度 | 评价 |
|---|---|
| file_explorer | M1~M5 后安全/稳定性大幅改善，但本轮发现 **1 个 P0 + 2 个 P1 回归**（均为近期改动引入）：缩略图冷启动永不生成、四个 GUI 插件嵌入模式静默失效、一个插件 JS 全废 |
| 主应用外壳 | 功能完整，但存在**关键失真项**：版本号硬编码 1.0.0（实际 3.0.0）、ai_workflow 默认开启却无注册路径、两处 UI 线程同步阻塞直接影响启动/打开文件体感 |
| src/features | 核心健康：57.7 万行、22,362 个测试可收集、TODO 密度极低。风险集中在巨型文件（最大 15,649 行）、渲染热路径 385 处吞异常、edit↔teaching_animation/ai_harness 双向依赖环 |
| 工程卫生 | 中。运行时产物（metadata.db/config.json/.cache）仍在版本库、pyproject 缺 4 个关键依赖（含 waitress）、.git 有 2.78GB 中断 repack 残留、一次性脚本与历史计划文档堆积 |

---

## 1. R0 — 已核实的回归与关键失真（第一批，建议立即做）

| ID | 严重性 | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|---|
| R0-1 | **P0**〔已核实〕| **缩略图冷启动永不生成**（M3 FUNC-5/11 改动引入的缩进回归）：`/api/thumb` 的入队块 `with thumbnail_lock:` 被缩进到 `if os.path.exists(target):` 内部——目标缓存不存在时整块跳过、直接返回占位图，**永不入队**。只有"缓存已存在但 sidecar 判定 stale"和手动 `/api/thumb/retry` 会生成。新目录/冷启动视频大图全部停留在占位图 | file_explorer/main.py:1244-1249（挂在 main.py:1232 的 if 块内）| L1244-1249 反缩进回函数级；补一条"请求不存在的 target 会入队"的回归测试 | S |
| R0-2 | **P1** | **四个 GUI 插件在嵌入模式启动失效**（ARCH-6 引入）：`ensure_runtime()` 从宿主 GUI 线程移进了 FlaskWorker（QThread，阻塞在 waitress、无 Qt 事件循环），`plugin_manager.load_plugins` 由此在 worker 线程执行各插件 `setup()`；GuiLauncher 的 QObject 线程亲和落在 FlaskWorker 线程，Flask 请求线程 `emit launch_signal` 队列投递到无事件循环的线程 → **槽永不执行**。受影响：decode_benchmark、format_factory、image_factory、shutdown_timer（嵌入模式点启动按钮静默无效；独立模式正常）| adapter.py:148-166（FlaskWorker.run）；四个插件的 GuiLauncher | 两个方案择一：① adapter 启动 worker 前先在 GUI 线程调一次 ensure_runtime()（worker 里调用变幂等空转）；② 各桥对象 `moveToThread(QThread.currentThread()主线程)`。方案①改动最小 | S |
| R0-3 | **P1**〔已核实〕| **ai_library_browser 前端 JS 全废**（SEC-5 改造连带回归）：插件注册的 `/static_plugin/ai_library_browser/main.js` 404——SEC-5 后该路由只服务 `plugins/<name>/static/`，而 main.js 还留在顶层 `static_plugin/ai_library_browser/`（插件目录内无 static/ 子目录）。前端搜索框注入成功但 `window.aiLibrarySearch is not defined`，功能整体不可用 | file_explorer/main.py:1527-1542；static_plugin/ai_library_browser/main.js | main.js 迁入 `plugins/ai_library_browser/static/`，插件注册路径不变即可命中 | S |
| R0-4 | **P1**〔已核实〕| **版本基线错误**：`CURRENT_VERSION = "1.0.0"` 硬编码，与 pyproject.toml `version = "3.0.0"` 严重不符——欢迎页/Project Manager 显示 v1.0.0，远端版本比较基线错误 | src/app/update_checker.py:12 | 从打包元数据或 pyproject 单一来源注入；顺带把未连接的 `error`/`no_update` 信号接上（main_window.py:729 只连了 update_available，检查失败完全不可见）| S |
| R0-5 | **P1**〔已核实〕| **ai_workflow 从未注册**：`src/shared/feature_flags.py:39` 默认 `ai_workflow: True`，但 main.py 的内联注册（真实生产路径）没有 ai_workflow 分支——只有休眠的 module_loader.py 里有。该功能在真实启动中永不加载 | src/main.py:231-294；src/app/module_loader.py:78-83（DORMANT）；src/products/*.py 的 register() 也是死代码 | 与 R2-3 合并处理：注册逻辑收敛为单一数据驱动实现，补 ai_workflow 分支 | S~M |
| R0-6 | **P1**〔已核实〕| **Windows QWebEngine 导入时序错误**："临时完全禁用 QWebEngine 导入来测试 OpenGL 冲突"的诊断分支已固化为主路径，但实际**无效**——files_panel.py:11 顶层导入 QWebEngineView，真实导入发生在 main_window.py:1150（QApplication 创建之后），恰好违反 main.py:88 自己注释的约束，QtWebEngine 晚初始化与 ModernGL 的冲突风险并未消除，只被软件渲染 flags 掩盖 | src/main.py:91-98；src/app/ui/files_panel.py:11；src/app/main_window.py:1150 | 删除误导性"临时禁用"分支；恢复 QApplication 前统一导入，或 Files 面板 QWebEngineView 惰性创建并移除顶层导入 | M |
| R0-7 | **P1** | **UI 线程同步阻塞热点**：① 文件加载路径同步调 exiftool（subprocess timeout=2s），exiftool 迟缓时每次打开文件冻结最长 2 秒；② Project Manager 启动/刷新在 UI 线程同步 `scan_video_projects`（逐个 .topos zip 读 manifest），项目多时启动卡顿；③ welcome_dialog._classify_project 同样在 UI 线程逐个开 zip | src/app/controllers/file_controller.py:654；src/shared/io/file_io.py:498；src/app/controllers/menu_toolbar_controller.py:927-946；src/app/ui/welcome_dialog.py:234-253 | 元数据获取挪 worker + 缓存；项目扫描改先渲染骨架列表、meta/缩略图异步填充 | M |

**验收**：冷启动新目录缩略图自动出现；嵌入模式四个插件 GUI 可拉起；ai_library_browser 搜索可用；关于页显示 v3.0.0；ai_workflow 按 flag 出现/隐藏；Windows 启动无 QWebEngine 时序告警；打开大 RAW 文件 UI 不冻结。

---

## 2. R1 — file_explorer 其余新发现（第二批，P2/P3）

### 2.1 数据一致性收尾（ARCH-10 后续）

| ID | 严重性 | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|---|
| R1-1 | P2〔已核实〕| `_volume_available()` 逻辑反了：循环第一次迭代就用**完整文件路径**命中 `/Volumes/` 条件直接 return（调用前提恰是该文件不存在）→ 恒 False，**外接卷孤儿 meta 永不清理**（保守方向失败，不丢数据但 ARCH-10 目标对 /Volumes 无效）| file_explorer/main.py:1545-1556 | 先把路径归约到 `/Volumes/<卷>` 根再 exists | S |
| R1-2 | P2 | NFD 自愈每次启动全表扫描：无条件 `SELECT path FROM file_meta` 全量 fetchall + 逐行 normalize，大库（10 万行级）启动内存尖峰+变慢 | file_explorer/main.py:399-407 | SQL 预筛（path 与 NFC 不等的行）或持久化"已清扫"标志 | S |
| R1-3 | P2 | NFD 孪生行删除时丢数据：发现 NFC 孪生直接 DELETE NFD 行，不合并其 rating/tags/comments——宿主写过 NFD 键即静默丢评分 | file_explorer/main.py:368-369, 403-404, 1603-1604 | 删除前 COALESCE 非空字段进 NFC 行 | S |
| R1-4 | P2 | **宿主侧 file_meta 不做 NFC 归一**：宿主 `set_file_rating`/`get_file_rating` 用原始路径写共享 topos.db，ARCH-10 的 `_meta_key` 只覆盖了 file_explorer 一侧——宿主以 NFD 路径写 → fe 侧 NFC 查询 miss，叠加 R1-3 会被当孪生行删掉 | src/shared/db/database.py:102,111 | 宿主两端同样 `unicodedata.normalize('NFC', ...)` | S |
| R1-5 | P3 | 旧表 `file_metadata` 迁移后从不 DROP → 迁移块（含一份冗余 NFD 自愈）每次启动全量重跑 | file_explorer/main.py:330-377 | 迁移成功后 DROP TABLE | S |
| R1-6 | P3 | 嵌入模式端口探测 TOCTOU：ensure_runtime 自行从 DEFAULT_PORT 起探测并写端口文件，而实际绑定的是 adapter 传入端口，短暂漂移后被纠正 | file_explorer/main.py:607-609；adapter.py:157-165 | `ensure_runtime(port=None)` 接受外部端口 | S |
| R1-7 | P3 | 独立模式 waitress bind 失败不中止：探活 50 次失败后照常起 Qt → 白屏窗口无提示 | file_explorer/main.py:1920-1936 | 探活超限弹窗退出 | S |
| R1-8 | P3 | `_process_meta_queue` 异常路径 sqlite 连接泄漏（except: pass 跳过 conn.close()）| file_explorer/main.py:186-195 | try/finally 关闭 | S |

### 2.2 插件深审发现（8 个此前只审到 __init__ 级别的插件）

鉴权结论：所有 Blueprint 注册在 `/api/plugin/<name>` 前缀下，全部被 `_guard_api` token 覆盖，无插件自起端口 ✓。除 R0-2 涉及的四个外：

| ID | 严重性 | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|---|
| R1-9 | P2 | format_factory：MonitorThread `while True` 无退出条件，窗口关闭后仍每秒 psutil 轮询直到进程退出 | converter_gui.py:239-245 | 加 stop 标志 + closeEvent 通知 | S |
| R1-10 | P2 | format_factory：`del_orig` 时**先 move_to_trash 源文件再 rename**，rename 失败（同名冲突）时源已进废纸篓——数据丢失窗口（Transcode/Remux 两处）| converter_gui.py:431-433, 617-621 | rename 成功后再 trash | S |
| R1-11 | P2 | image_factory：`Image.MAX_IMAGE_PIXELS = None` 在插件 import 时执行，**进程级关闭 PIL 解压炸弹保护**，影响主应用所有 PIL 调用点 | image_gui.py:32 | 移入任务执行时局部设置 | S |
| R1-12 | P2 | image_factory：ProcessorThread `future.result()` 无 timeout 无取消，处理中关窗有 "QThread: Destroyed while running" 崩溃风险 | image_gui.py:280-287 | closeEvent 守卫 + 可取消 | S |
| R1-13 | P2 | shutdown_timer：取消关机在 GUI 线程同步执行 `osascript with administrator privileges`（无 timeout），授权弹窗期间整窗冻结 | timer_gui.py:701 | 与 start 一致放 daemon 线程 | S |
| R1-14 | P3 | 插件杂项：format_factory ffmpeg 裸名/无 timeout subprocess/os.setsid Windows 不可用 + 1 处 print 漏网（ARCH-4）；ai_library_browser main.js 两处 innerHTML 未转义 + limit 未校验；image_editor /read 无大小上限；shutdown_timer L420 一处必炸死代码（当前被短路屏蔽）| 各插件 | 顺批清理 | S |
| R1-15 | P2 | disabled_plugins 与宿主偏好设置未打通：plugin_system 读的是 file_explorer 自己的 config.json；宿主偏好页写的是用户目录另一份，且其插件列表扫的是仓库根 plugins/（DCTL/.tai），与 FE/plugins 不相交 → **UI 无法停用 file_explorer 插件** | plugin_system.py:70-76；src/app/ui/preferences_dialog.py:1927, 1507-1563 | 偏好页插件列表改读 plugin_manager.plugins、写入 FE config | S |
| R1-16 | P3 | ARCH-5 魔法数残留：小图直读阈值 500KB、max-age 300/3600、Semaphore(2)、队列上限 500、分块 500、探活 50 次等（均有注释，建议入 const.py）| file_explorer/main.py 多处 | 顺批入 const | S |

已检查无问题：ensure_runtime 双检锁、_FFMPEG_AVAILABLE 懒缓存、/api/health、/api/cache/invalidate、adapter 两个 wrapper、files_panel 健康探测/_run_js/退避、code_editor、audio_pro、decode_benchmark 主体（benchmark.py 质量好）。

---

## 3. R2 — 主应用外壳其余问题（第三批，P2/P3）

| ID | 严重性 | 问题 | 位置 | 修复方案 | 工作量 |
|---|---|---|---|---|---|
| R2-1 | P2 | `sys.path.insert(0, external/file_explorer)` 模块遮蔽：该目录含 main.py/const.py/utils.py 等通用名模块，任何 `import main` 都会命中 file_explorer 的——低频但难排查的隐形地雷 | src/app/main_window.py:82-83 | 改 append 或包化导入 | S |
| R2-2 | P2 | 对话框累积泄漏：WelcomeDialog/ProjectManagerDialog 每次新建、parent=self、无 WA_DeleteOnClose → C++ 对象永久累积；且 PM 每次创建向单例 ThumbnailProvider 重复 connect → 历史对话框全被缩略图信号刷新 | menu_toolbar_controller.py:913-946；project_manager_dialog.py:163-164 | `setAttribute(WA_DeleteOnClose)` 或关闭时 disconnect，一并解决两条 | S |
| R2-3 | P1 | 注册逻辑三份平行实现漂移（R0-5 的根因）：main.py 内联（活）、module_loader.py（DORMANT 死代码）、products/*.py register()（死代码，四文件 85-90% 同构）。已发生实际漂移（ai_workflow 丢失）| src/main.py:231-294；module_loader.py；src/products/ | 收敛为单一数据驱动注册（PRODUCT_PRESETS 已在 products/__init__.py），删两份死实现 | M |
| R2-4 | P2 | feature_registry.register_pages/register_actions 无 try 隔离：单个模块注册抛异常中断整个链 → 应用启动 FATAL（i18n/ai_providers 有隔离，页面/动作没有）| src/app/feature_registry.py:65-83 | 与 i18n 同样加隔离 try + warning | S |
| R2-5 | P2 | 日志/缓存目录绕过平台规范：log_manager 固定写 ~/.topos/logs、model_manager 用 ~/.topos_cache、thumbnail_provider 兜底缓存写**源码树内** .assetdeck/（打包后可能落只读安装目录静默失败）——与 ConfigManager 的平台目录策略三处不一致 | src/shared/logging/log_manager.py:81,116；model_manager.py:247；thumbnail_provider.py:603-611 | 统一走平台规范目录 | S |
| R2-6 | P2 | 三种重启路径并存：welcome/project_manager/preferences 用 `os.execv` 原地重启（不做任何清理，Flask/后台线程瞬间蒸发），menu_toolbar 用 detached subprocess + 正常退出 | welcome_dialog.py:414；project_manager_dialog.py；preferences_dialog.py:2140；menu_toolbar_controller.py:712-750 | 统一为 subprocess 方案 | S |
| R2-7 | P2 | 欢迎页开关 auto_save=False：`show_welcome_on_startup` 依赖退出时才落盘，崩溃即回滚 | welcome_dialog.py:417 | auto_save=True | S |
| R2-8 | P3 | 模块加载失败无 UI 提示：ImportError 仅 warning 跳过，用户不知道功能被禁用及原因 | src/main.py:249-294 | 启动横幅/帮助页列出禁用模块 | S |
| R2-9 | P3 | 诊断残留：Windows 每次启动创建一次性 QOpenGLContext 只为打版本日志；COMPOSITE-STARTUP 调试 print 绕过 logging；main.py:98 诊断日志用 debug 级永不可见 | src/main.py:193-202, 98；main_window.py:671-693 | 顺批清理 | S |
| R2-10 | P3 | Project Manager 交互改进：无 `--project <path>` / `--no-startup-dialog` 命令行参数（自动化测试每次都要手工关弹窗的根源）；无"继续上次项目"一键入口 | src/main.py:139-151；main_window.py:722-725 | 加参数 + 记住上次项目 | S |

已检查无问题：update_checker 网络在线程中执行、有超时、SSL 正常；关闭清理链 QThreadPool+waitForDone+os._exit 有意设计（注意 3 秒硬杀有截断写入尾部风险）；ai_edit_controller 等 worker 线程生命周期规范；无循环 import。

---

## 4. R3 — src/features 功能层健康度（第四批，按需渐进）

规模：57.7 万行 / 1,037 文件。edit 24.7 万（43%）＞composite 15.7 万＞teaching_animation 6.2 万＞color 5.3 万＞ai_harness 2.5 万＞ai_workflow 1.9 万＞deliver 0.9 万。测试：根 tests/ 共 **22,362 个测试可收集**（edit 457 文件、composite 218、color 87），薄弱处为 scripting（1 个）与 features/shared（无专属测试）。

| ID | 严重性 | 主题 | 数据与方向 | 工作量 |
|---|---|---|---|---|
| R3-1 | P2 | 渲染热路径吞异常 | `except Exception: pass` 385 处（composite_page 48、edit_gpu_preview_renderer 31、async_renderer 24、timeline_preview_renderer 24……），裸 `except:` 24 处（async_renderer 占 17）。症状是"画面不更新但无日志"。方向：渲染核心三文件（async_renderer/image_engine/color canvas）先给吞错点补 debug 级日志，不必改控制流 | M |
| R3-2 | P2 | 巨型文件 | commands.py 15,649、timeline_view.py 15,164、timeline_items.py 10,745、media_pool_panel.py 10,428、composite_viewport.py 10,230（>4,300 行共 15 个）。方向：不做全面重构，仅随特性开发渐进拆分；新增代码禁止再进这些文件 | 长期 |
| R3-3 | P2 | 双向依赖环 | edit→teaching_animation 52 条 vs 反向 10 条；ai_harness↔edit 32/23 双向环；edit 是全仓依赖中心（被 6 模块依赖 + 出边 118）。方向：先断最小的环（teaching_animation→edit 的 10 条），大环接受并文档化 | M~L |
| R3-4 | P3 | 分层违例 | features/shared → composite 17 条（effect_catalog.py 占 16）——"shared" 反向依赖具体功能层 | M |
| R3-5 | P3 | 未完成角落 | features/models/ 空壳目录（0 文件）；scripting 2,193 行从未注册、仅 1 测试；module_loader.py 自注"半年内无接线计划建议删除"；feature_flags 里 `library: True` 但 features 下无此目录（误导）| S |
| R3-6 | P3 | 线程约定靠注释 | async_renderer/image_engine 的"Must be called from render thread only"全靠注释约定，无运行时断言；Windows WGL 主线程约束、SAM 推理槽不可中断是两处自认高危区 | 随特性 |

成熟度评估：color 生产级（渲染核心是全仓最脆点）；edit 生产级但超载；composite 可用偏生产；teaching_animation/deliver 可用；ai_workflow flag 门控可用；ai_harness 库层可用；scripting 实验性；models 死目录。

---

## 5. R4 — 工程卫生（第五批，低风险高确定性）

| ID | 严重性 | 问题 | 处置 | 工作量 |
|---|---|---|---|---|
| R4-1 | P2 | 运行时产物被 git 跟踪：根 metadata.db、external/file_explorer/metadata.db、根 config.json（运行时实际读用户目录，根目录这份是滞留快照）、.cache/ 4 张 preview 缩略图 | `git rm --cached` 四项；config.json 如需模板改名 config.example.json | S |
| R4-2 | P2 | AI IDE 状态与测试产物入库：.qoder/ 148 文件（含 1MB repowiki）、.qwen/ 13 文件、tests/video_denoise/output/ 5 张 2.2MB PNG（11MB）、tests/benchmark/results/ 53 个 trace | untrack + .gitignore 补 `.qoder/ .qwen/ tests/video_denoise/output/ tests/benchmark/results/` | S |
| R4-3 | P1 | **pyproject 缺关键依赖**：核心依赖缺 waitress/trimesh/pyassimp/PyOpenGL（PyOpenGL 只在 macos extra——Linux/Windows `pip install -e .` 会装出 Edit/Composite 页面整体禁用的坏环境）；macos extra 多出 `pyobjc-framework-CoreVideo`（README:411 明言 PyPI 无此包、已从 requirements 移除——两处矛盾）| 以 pyproject 为唯一事实源补齐/删除；requirements.txt 改为锁定产物或声明派生关系 | S |
| R4-4 | P3 | pytest 配置双份：pytest.ini 与 pyproject [tool.pytest.ini_options] 内容重复，pytest.ini 优先，双处维护易漂移 | 只留一处 | S |
| R4-5 | P3 | .git 内 2.78GB garbage：两个中断 repack 的 tmp_pack | `git gc --prune=now` 或手动删 tmp_pack（只影响本地磁盘，不影响 clone）| S |
| R4-6 | P3 | 一次性脚本堆积：scripts/ 49 个中约 10 个 debug_*/fix_*/v2_* 嫌疑、tools/ 78 个中 40+ 个 bench_m1xx/dbg/diag/repro 一次性脚本 | 移 tools/archive/ 或删除，保留 e2e_*/verify_*/bootstrap_*/build_* | S |
| R4-7 | P3 | docs 顶层历史计划堆积：2026-05~08 的带日期计划/审计单体 3~10 万字节 ×多份，mtime 大多停在 08-28 批量迁移 | 归档到 docs/plans/archive/，顶层只留活文档；新计划表（本文档）列入顶层 | S |
| R4-8 | P3 | 两套插件系统无文档区分：PLUGIN_DEV_GUIDE.md 写的是主程序 plugins/（.tglsl/.tai），file_explorer/plugin_system.py 是另一套（plugin.json/register_js/启停配置），指南未提 | PLUGIN_DEV_GUIDE 补一节对比说明 | S |
| R4-9 | P3 | 杂项：playback_fps_report*.txt 一次性报告入库；run_claude.command/run_opencode.command 是个人 AI CLI 启动器（含个人模型映射），不该在仓库根；bench_out/ 2.1MB hash 目录可精简；hdri/INDREF-023.jpg 8.8MB 考虑 LFS | 各自处置 | S |

---

## 6. R5 — 旧计划遗留大项（专项会话，保持原排期判断）

| ID | 现状数据（本轮实测） | 内容 | 工作量 |
|---|---|---|---|
| ARCH-1 | ✅ **已完成（2026-09-06）**：main.py 拆为 webapp（app+SEC-1+端口）/db（SQLite+config）/bridge（AppBridge+CopyWorker+ensure_runtime）/thumbs（后台 worker）/routes（41 条路由+智能文件夹）/window（MainWindow）+ __main__ 入口；main.py 仅剩 117 行组合根+兼容外观。循环依赖解法：webapp 零内部依赖、ensure_runtime 对 thumbs 调用期导入。实测：URL map 41 条与拆前逐条一致、平铺/包双视图共享单例、waitress 真实 HTTP 探活/鉴权/列表全过 | 机械拆分无行为变化 | L |
| ARCH-2 | ✅ **已完成（2026-09-06）**：新增 base_player.py（BasePlayer UI 骨架/解码线程模板/自动隐藏/快照封面 + DecoderBackend ABC）；ToposMovieSession 与 _LibavSession 实现该接口；topos/libav 差异经钩子注入（_poll_ring 帧消费策略、_on_play_state_changed/_on_seek_requested 音频时钟、_build_extra_ui 片段面板）。topos 807→360 行、libav 856→506 行。player_qt（QMediaPlayer 后端）刻意不并入：UI 面貌不同（音量条/meta 徽标/比例进度条），三引擎路由互斥仍在 player_host。实测：2K_topos.mov 真实播放+精确 seek、ProRes 时钟门控出帧、playback_matrix 11 文件 seek=√ 零错误（4K 实时项为 offscreen 环境性能，fps 区间与 HEAD 一致） | 抽 BasePlayer（UI/进度/自动隐藏/快照）+ DecoderBackend 接口 | L |
| ARCH-13+FUNC-16 | ✅ **已完成（2026-09-06，三个提交）**：① FUNC-16 escapeHtml 铺开——计划点名 7 处 + 顺带审计出的同风险点（音频扩展名/分辨率角标/meta 文本/插件管理器 name·version·description·author·error/深度元数据 EXIF/重命名预览）全部转义；createTreeNodeHtml P2 文件名直插修复 + data-path 属性转义 + 7 处 CSS 选择器改 cssPathSel；附带修复 openWithApp 读取从未赋值的 contextMenuIndex（右键"打开方式"必抛 TypeError）。② 事件委托——index.html 93 处静态 onclick 清零（data-action + JSON data-args + $el 哨兵 + data-stop/prevent 语义，捕获相位复刻 inline 时序）；script.js 动态 onclick 全部数据化（树/智能文件夹/固定库/卡片重试/规则行/批量面板走容器委托，updateSortMenuState 高亮改读 data-args）。③ 三模块 IIFE——script.js 1,325 行拆 fe-state（226）/fe-render（434）/fe-actions（708），window.__FE 共享命名空间 + 晚期绑定，window.* 公开 API 不变。实测：真实服务 + 浏览器 GUI 14 项全链路（XSS 注入/委托传参/树导航/灯箱/智能文件夹/批量面板/插件管理器）零 JS 错误 + 截图视觉确认 | 事件委托 + escapeHtml 铺开 + state/render/actions 三模块 IIFE 拆分 | M~L |

---

## 7. 建议排期

| 批次 | 内容 | 工作量 | 说明 |
|---|---|---|---|
| **第一批（立即）** | R0-1~7 | 2~3 天 | 全部是回归/失真修复，改动小收益大；R0-1 缩略图 P0 建议当天修 |
| **第二批** | R1-1~16 | 1~2 天 | file_explorer 收尾，多为 S 级；R1-4（宿主 NFC）与 R1-3 联动必做 |
| **第三批** | R4-1~9 | 0.5~1 天 | 工程卫生，零行为风险，R4-3（依赖缺失）其实影响新环境安装，可提前 |
| **第四批** | R2-1~10 | 2~3 天 | 主应用稳定性；R2-3 注册收敛含 R0-5 |
| **第五批** | R5 三个专项 | 各 L | 每个开专门会话，实现→断言→实测→提交完整周期 |
| **持续** | R3-1（吞错补日志）、R3-5（半成品清理）| S×2 | 可搭任意批次顺风车；R3 其余按特性开发渐进 |

**快速胜利清单**（半天内、收益直接）：R0-1（一行 dedent 恢复缩略图）、R0-3（挪一个文件恢复整个插件）、R0-4（版本号一行）、R1-1（volume 判断一行）、R1-8/ R1-9/ R1-11、R4-3/R4-5。

---

## 8. 审计覆盖与核实状态

| 区域 | 覆盖方式 | 核实状态 |
|---|---|---|
| file_explorer | 残留项复查 + M1~M5 新代码审查 + 8 插件全文通读 | R0-1/R0-3/R1-1 及 static_plugin 路由已人工核实；R0-2 机制分析依据 adapter.py/插件代码，修复前先写最小复现 |
| 主应用外壳（src/main、src/app、src/shared、src/products） | 十类专项扫描 + 热点文件精读 | R0-4/R0-5/R0-6 已人工核实；其余有行号证据，动手前复核 |
| src/features | 规模/测试/吞错/耦合/死代码五维普查（非逐行审计） | 数据类结论（行数/测试数/依赖边数）来自脚本统计，可信；单点问题未逐条核实 |
| 工程卫生 | git ls-files/du/count-objects 实测 + 文档抽查 | git/依赖结论来自命令输出，可直接采信；文档矛盾点（CoreVideo）已对照两文件 |

**上一轮计划（docs/file_explorer_upgrade_plan.md）完成状态**：M1~M4 全部完成；M5 完成 10/14（ARCH-12/5/7/8/9/14/6/3/4/10/11），剩余 ARCH-1/2/13 移入本文档 R5。上一轮"已确认无虞"清单全部维持。
