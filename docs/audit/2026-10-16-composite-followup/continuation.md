## 2026-10-16续验与新增计划

续验基线为`cdce6662cb7ab4468e84aeb2039792e38f1e9131`。原审计方案与证据已由`9e73315aa`归档。本次保留2026-10-14原始计数和性能数据，不把它们冒充今天全量重新测得的数据。

与原基线比较，`src/features/composite/`、`src/app/controllers/composite_controller.py`、`src/shared/project/save_router.py`、核心shared色彩/媒体/GPU/导出路径没有差异。MainWindow的变化是新增Edit批量参数信号连接，shared/keyframe新增轨道平移/缩放方法，没有修改原有合成求值实现。因此没有为了续写报告重跑整套7711个用例；改为重跑3个UI复现脚本和1个节点复现脚本。四个脚本均正常结束，保存字段丢失、跨图撤销、连线幽灵状态、表达式/时间语义错误仍可观察。今天记录见`docs/audit/2026-10-16-composite-followup/evidence/recheck_results.json`及逐脚本日志；结束码0表示复现脚本运行完成，不表示产品问题已修复。

原始GUI驱动曾因ScopeDemand计数器和直接SystemExit发生异常，已按QA-04作为基准问题处理。后续生产关闭入口验证与这些早期异常分开：2K HEVC关闭成功；4K HEVC在2026-10-14实际播放后窗口关闭但线程仍活着，驱动返回2；2026-10-16使用每个场景独立临时目录再测，4K HEVC仍出现相同状态。故补充R-24；不能再笼统把所有退出异常都归到测试脚本，也不能把未停止线程直接描述为“已证实用户一定崩溃”。

### R-24：4K播放后关闭窗口，渲染线程未退出却继续完成关窗

| 字段 | 详细计划 |
|---|---|
| 优先级 / 状态 | P1 / 已运行观察，待修复。证据A-APP+B源码。与早期未走closeEvent的测试脚本native崩溃区别记录。 |
| 复现环境 | 本机Qt6.10.0 / OpenGL4.1；独立临时config；导入`4K_H265.mov`，建立VideoFrame+Constant→Merge→Output，1920×1080合成、24fps，播放15秒。保存确认仅对临时合成明确返回Discard；真实`MainWindow.closeEvent`与既有cleanup完整执行。 |
| 实际结果 | 原始窗口测量224次frame_ready、14.9/s；今天复测210次、14.0/s。生产关闭后日志两次提示`Composite render thread did not stop within 3000 ms`，最终`composite_renderer_running=True window_visible=False`，驱动返回2。不是成功退出，也不是尚未接受保存确认。 |
| 影响 | 窗口树和共享GL宿主可能先于渲染线程销毁。仅保留QThread的Python引用不能证明context/surface与native frame lease仍有效；之后的异常释放或进程残留需要单独测，不能凭本次状态推断具体native崩溃根因。 |
| 定位 | `src/features/composite/render/composite_renderer.py:356` stop_thread返回停止结果；`src/features/composite/ui/composite_page.py:1951`超时仅保留引用和记录日志，cleanup没有向上返回失败；`src/app/main_window.py:2094`在所有owner停机完成前设置`_pipeline_shutdown_done=True`，closeEvent继续销毁窗口。原审计与当前版本该关闭逻辑相同。 |
| 已知根因与待定位点 | 关闭结果合同丢失已经有源码与运行证据。线程具体阻塞在解码join、GPU等待、scope还是pool/node释放，尚无足够现场栈，不能猜成某个GPU驱动bug。现有日志的重复3000ms等待不是可靠的资源完成确认。 |
| 修复步骤1：定位 | 在既有stop/cleanup各阶段加时间戳与owner线程ID；超时只抓取线程栈和未完成GPU lease/fence，不跨线程释放。先复现空窗口、2K、4K原片、代理片、播放中关窗和暂停后关窗，区分输入依赖。 |
| 修复步骤2：传播结果 | page cleanup返回明确stopped/timeout/failed及具体owner；MainWindow汇总结果，只有所有必须终止的线程完成后才设置stopped并允许最终销毁。不能先把整个管线永久标done而丢掉重试机会。 |
| 修复步骤3：有序停止 | 先停止timer/新提交与后台预取，发取消并完成/放弃在途请求；在owner线程回收pending decode/native frame、等待消费者lease/fence，释放node/cache/pool/output/scope/PBO，最后doneCurrent并销毁共享context/surface。所有入口幂等。 |
| 修复步骤4：超时处理 | 使用异步关闭状态机保持UI可响应及GL宿主存续；超时保留有效owner并明确停机未完成，可继续等待或重试。不得将强制terminate线程、吞异常、无界UI wait或单纯延长固定超时作为完整修复。 |
| 验收 | 2K/4K各至少5次真实关窗，标准进程退出0；窗口最终隐藏/销毁前renderer、decoder worker均停止且cleanup完成。注入超时后不销毁仍被使用的context，也不假报停止；取消关闭后原页面仍可用；重复cleanup不double-free；再执行V-06长测。 |
| 工时 / 依赖 | 1日抓取并定位阶段；2–4日修复与focused回归，依赖R-11资源生命周期及V-06。若涉及驱动原生等待，按现场栈另估。 |
| 证据 | `docs/audit/2026-10-14-composite/evidence/4K_H265.mov.real_app.log`与`docs/audit/2026-10-16-composite-followup/evidence/4K_H265.mov.real_app.log`；runner、逐案例临时config和结束码见两次gui结果JSON。 |

### P-05：真实窗口4K HEVC持续播放中后段退速

| 字段 | 详细计划 |
|---|---|
| 优先级 / 证据 | P1性能 / 两次真实frame_ready信号与播放头记录，短时15秒。不是物理显示器呈现测量，也不能用离线18ms结果宣称这个播放路径实时。 |
| 复现与症状 | 同R-24的简单合成。原始播放平均14.9/s，后半段每秒约7～8次回调；今天平均14.0/s，前段约23～24/s，7秒以后持续约7～8/s，播放头仍按24fps推进。只看“无超过1秒stall”会漏掉持续掉帧。 |
| 已有归因 | 今天drive日志每帧eval约3.4ms、render约1.7ms、等待约42.8ms、scope约5.1ms、retry共2458次。等待占比高，但没有完整decode/backend/cache事件链，尚不能把全部退速归于GPU效果、TOPOS代理或硬解。 |
| 解决方法 | 将请求、有效源帧、实际呈现帧号、VT/native ready、decoder切换、代理生成/启用、GPU lease/fence与scope需求时间串联；按秒记录distinct帧而非仅signal总数。做自动代理开/关、固定原片/已知代理、scope开/关、单一decoder后端对照，定位退速发生的首个事件。随后改有界等待/唤醒、取消过期请求、释放旧lease、控制代理切换，避免重试空转。沿已有VideoContext/service/渲染队列实现，不另起一条播放管线。 |
| 验收标准 | 相同素材及合成至少60秒并循环测试，实际不同帧按目标24fps稳定，持续一秒窗口不再降至7～8fps；呈现帧龄、连续丢帧、UI heartbeat和预览质量同时记录。平均FPS、无长stall和总signal数不能代替此验收；不靠丢画质或重复发同帧信号过关。 |
| 工时 / 依赖 | 1–2日事件链定位，2–4日优化；与P-02/P-04共享监测，依赖N-06/07保证源帧语义，R-24保证测试可靠结束。 |

### 续验结论的适用范围

今天TOPOS 4K来源的1080p合成短测为359次frame_ready、23.9/s，无超过1秒信号间隙，正常关闭后renderer与窗口均停止，进程退出0。这是本次受测路径的正面结果；不能把原先离线4K读回慢推广为TOPOS全部交互播放均不实时。HEVC的退速和关闭超时按R-24/P-05继续排期。原始基准未完成的最后一组窗口测试至此已有结果；V系列的长时、全交互与跨平台验收仍是后续修复验收任务。

### V-07补充证据：直接冷启动Composite时GL初始化等待

早期日志`4K_H265.mov.real_app.fifth.log`显示启动页为Composite，30秒内未建立renderer，停在等待`gl_initialized`；异常退出本身又触发测试生命周期问题。当前源码`composite_page.py:1226`只向父链查找`canvas.gl_viewport`，`:1293`成功连接它的就绪信号后等待，`:1547`的兜底重试不主动初始化一个尚未显示的donor viewport。这能解释“先打开Color可用，直接进入Composite可能等待”的风险，但仍需以正常应用入口排除harness启动时序。本项继续按V-07待验证任务记录，不计入确认bug数。

复验应分别从Color、保存过的Composite及独立Composite产品入口冷启动；要求共享Qt GL宿主在任何GPU页面首次进入时可完成初始化，不依赖用户先打开Color。若采用公共初始化服务，保持GL/ModernGL求值在既有渲染线程，不能由UI临时直接跑GPU操作。记录ready信号、context有效性与首帧时间，避免把“信号已连接”当“初始化已触发”。
