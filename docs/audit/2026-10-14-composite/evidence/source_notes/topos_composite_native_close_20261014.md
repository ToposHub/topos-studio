> 原始阶段审查记录：正文部分副本行号/退出结论对应早期驱动。最终复测与归类以主计划的真实应用表、QA-04、V-06/07为准。2K最终生产closeEvent路径已退出0，未确认正常关窗产品崩溃。

# QA-05 / V-退出-01：真实应用驱动结束时 native SIGSEGV，关闭入口尚未覆盖（2026-10-14）

| 唯一编号 | 当前分类 | 优先级 | 标题 | 证据级别 | 估计工时/依赖 |
|---|---|---|---|---|---|
| QA-05 | **验证缺口 + 审计 harness 生命周期错误；暂不计入已确认正常关窗的产品 bug** | P1 验证优先级（若正式 closeEvent 复现则升已确认崩溃） | 2K HEVC 播放后 driver 直接 Python 退出，未调用生产关闭链，触发 native SIGSEGV | A：进程退出 -11 + 日志；B：driver 未关窗的源码；真实生产 closeEvent 尚待复验 | 0.5 天修审计驱动并重复验证；若产品链复现，再估 2–5 天，依赖 native .ips / faulthandler 栈和退出线程清单 |

## 目前可得出的结论

真实应用已持续播放 15 秒，第二轮收到 358 次 viewer emission（23.8 次/秒），未见超过 1 秒 viewer gap 或 playhead stall；随后进程 native SIGSEGV(-11)。第三轮 356 次 emission、23.7/s，日志末尾 `Fatal Python error: Segmentation fault`，未给 Python frame。播放测量成立，但这不证明“点击关闭窗口必崩”——当前 driver 没有走正常关闭入口。

审计副本 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/real_app_driver_audit_copy.py:824` 仅 `page.comp_pause()`，末尾 `:1008` `return 0`，`:1012` `raise SystemExit(main())`。全文没有 `mw.close()`、`app.quit()`、`_shutdown_playback_pipeline()`、`page.cleanup()`、`renderer.stop_thread()` 或 `app.exec()`。

所以当前退出的实际链为：main局部作用域返回 → Python/Qt/native对象终结 → QApplication/QWidget/OpenGL相关对象销毁。图渲染线程仍可能活着、共享context/surface和viewer仍有引用；`comp_pause()`只停合成时钟，并不join GPU thread。没有证据表明harness手动shutdown导致重复释放，当前harness根本没有执行shutdown。

首轮driver的 `ScopeDemand`被当dict调用`.get()`是审计计数器错误；第二轮已经只改计数器与ROOT。这个首轮-6不得算产品bug，末尾render_submits=1而viewer358也不能解释为35800%完成率——生产走带内部驱动不按单次submit计数。

日志路径：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/2K_H265.mov.real_app.second.log`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/audit/2026-10-14-composite/evidence/2K_H265.mov.real_app.log`

native .ips 由根任务查读。未经 .ips 指向具体释放栈，不把 QtWebEngine、VT、ModernGL、GPU decoder 中任何一方认定为确定根因。

## 生产关闭链核对

1. **窗口关闭确认后才停机**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/main_window.py:1975` `MainWindow.closeEvent` 先调用 `_confirm_save_before_close`，取消则 ignore；确认后分别关闭Tangent/tracking/benchmark/updatechecker/script/denoise/files，再调用`:2078` `_shutdown_playback_pipeline`。
2. **播放管线关闭**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/main_window.py:2093` 使用`_pipeline_shutdown_done`幂等锁；先pause走带、停音频和Color video controller，再shutdown timeline preview，最后`:2134`调用CompositePage.cleanup。
3. **合成页**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py:1897` 先stop debounce/prefetch/retry/nav timers、is_active=False、comp_pause、tracker/AI取消、Keyframes.dispose，再`:1956`调用renderer.stop_thread。成功则 `_renderer=None`；超时保留renderer引用并记录错误。
4. **渲染线程**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_renderer.py:356` 设`is_running=False`，CLEANUP队列唤醒，wait(3000)，返回bool；`:519` run.finally在渲染线程 `_cleanup()`，然后shared_context.doneCurrent。
5. **GPU清理幂等**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_renderer.py:2009` `_cleanup_lock/_cleanup_started`防重复清理，依次取消AI/image loader、PBO、scope、output/VBO/VAO、cache/pool、render nodes、mesh pool、shader/compute和deferred释放，finally设cleanup_complete。即使CLEANUP命令和run.finally各到一次，也有幂等锁，不能凭双入口宣称double-free。
6. **mixin后半段**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/app/controllers/file_controller.py:6967` 再调用audio/Composite runtime cleanup；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py:266`再委托page.cleanup。这是可见的重复调用，但page正常第一次成功把_renderer置None；renderer cleanup本身幂等，重复入口本身没有确认错误。
7. **媒体资源**：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/video_frame_node.py:2829`明确先join preload，再close video、释放GPUtexture、清帧缓存。驱动直接SystemExit不保证走到这段。节点注释说明GPU不能依赖GC释放。

## 生产路径仍需重点验证的风险（证据B，不是当前-11已证原因）

- `_pipeline_shutdown_done=True`在join前设置（main_window:2097）；若Composite stop_thread超时，窗口关闭仍继续。仅持有live QThread Python引用不能保证其shared Qt OpenGL surface/context或其他子对象在QWidget层级销毁后仍存在，且最终进程退出仍会清理。
- CompositePage tracker worker `wait(2000)`结果未检查就`deleteLater()`（composite_page:1932）；若取消不及时，有QThread still running析构风险。当前driver没有tracker节点，**不能把本次崩溃归于tracker**。
- `_stop_gpu_renderer_deferred`把超时Color/Source renderer放process-global `_LEAKED_GPU_OBJECTS`（file_controller:253）；保护仅到进程退出，native解释器终结阶段仍不应当有活着的GPU thread。与合成当前无正常关窗复现分开。
- viewport原生OpenGL资源释放需保证widget自己的context current；`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py:3719`有cleanup，但此审计未证明所有销毁路径调用前makeCurrent/结束doneCurrent，也没有确认它引起-11。
- 正式main采用`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/main.py:321` `app.exec()`，driver用processEvents自行泵；两种生命周期差别须在复验中消除。

## 可执行解决/验证计划

### 步骤1：修审计驱动的资源生命周期，产品源码不动

在**新的审计副本**的main外层try/finally集中处理退出，所有分支（含MODE timeline等中的raise SystemExit）都必须经过finally。先记录close前renderer和timer状态；在UI线程调用 `mw.close()`，让真实MainWindow.closeEvent负责shutdown。不能仅用 `app.quit()`代替关窗，也不能直接逐一cleanup所有拥有者绕过真实入口。

临时工程无需保留，保存确认应在明确审计上下文下选择丢弃；日志记录该选择。自动reject所有QDialog会把保存确认视为“取消关闭”，所以用正常关闭route时必须停止/改写dialog-killer，并确认closeEvent已accepted。不要把未accepted的mw.close()当已关闭。

在closeEvent/renderer.stop_thread周围增加**审计副本级**轻量日志，不改产品：`[CLOSE ENTER]`、返回accepted/visible状态、stop_thread bool、cleanup_complete、各关键QThread.isRunning。随后pump Qt events直到相关finished/deleteLater完成（有界）；保留mw/app强引用直到停机确认。只有所有线程结束后返回main。若任何线程仍running，输出验证失败与具体拥有者，避免给退出成功报告。

### 步骤2：确认正常关窗，做3组对照

1. 启动不播放→正常关窗；2. 2K_H265播放15秒→正常关窗；3. 4K_HEVC播放15秒→正常关窗。每组重复至少5次，先短负载排除无关变量。所有测试独立临时config，不覆盖用户工程/布局。

用PYTHONFAULTHANDLER=1记录stderr，并保留每次native .ips、exitcode。记录close时有无GPU/decoder/loader线程、GPU lease数量、Qt context current所属线程。再追加source/timeline双页、连续切页、播放中直接关闭、正在导出时取消后关闭。不能将driver直接SystemExit组的失败混入正常close组通过率。

### 步骤3：若正式close入口仍崩溃，按native栈定位最小修复

- QObject/QThread销毁栈且thread仍运行：把“关闭允许”与全部owner线程退出绑定；超时不继续销毁窗口树，保持有效surface/context与owner引用，提示停止失败，并允许重试。关闭状态设为stopping/stopped，不能stop失败时永久done。
- GL/ModernGL/nativeGPUlease销毁栈：先停提交、等待GPU fence/consumer lease，owner-thread清理node/provider/cache/output/PBO，再doneCurrent，最后销毁Qt context/surface。明确wrapped/owned context分离；不能补跨线程glDelete。
- VT/media session栈：先cancel/decode join；回收pending/leased帧；最后invalidate session；异步回调不得再访问被清理node。对session cancel/close/late callbacks加focused regression。
- 重复清理栈：明确首次owner并对每个资源句柄置None/幂等，保留frame lease存续合同；不要依赖宽泛except吞掉native状态。

### 步骤4：生产关闭长期回归门禁

加真实生命周期integration test：建共享GL/视频源，播放、关窗、确认QThread停机和GPU清理后结束应用；标准进程exit0、无SIGSEGV/SIGABRT、无`QThread: Destroyed while thread is still running`。超时注入、Tracker取消和late queued signal单独focused test，测试只验证关闭合同，不复制实现。

## 验收标准

- 正常产品closeEvent路径2K/4K每组5次exit0；无native崩溃；日志明确close entered→stop success→cleanup complete→context released。
- 用户取消保存确认时保持原窗口/播放/渲染可继续，未开始shutdown。
- 用户确认关窗后不产生新渲染请求，所有page/debounce/drive/prefetch/timer停机。
- 有界join超时不得释放still-running线程依赖的context/surface，不将半关闭状态标记done；能安全等待、取消或重试。
- 第一次正常close后第二次cleanup安全且不重复GL释放；close路径不在UI线程执行GPU调用。
- 性能测量记录与退出验证分开：23.8/s只代表viewer信号率，不能称显示器实际帧率或全部稳定性已通过。

本项采用QA-05唯一编号供根计划作为“必须补验”安排，**目前不建议写成“产品退出必崩”的已确认缺陷**。如后续正常closeEvent复现且获得native栈，再更新证据/根因，避免把审计driver生命周期错误写进产品优化计划的确定bug计数。

根任务后续进展：第四轮已在审计副本加入真实`mw.close()`并泵events；保存确认显式Discard，禁止直接renderer.stop绕过关闭链；末尾验证renderer.isRunning/window_visible，未完成退出则返回2。该轮结果仍待写入，第三轮直接finalize的-11不得移作正式关窗证据。
