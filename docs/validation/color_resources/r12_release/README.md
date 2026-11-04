# R12 — 发布就绪矩阵（2026-11-04，计划全部 R/N 批次收口）

## 批次完成总览（34+13 提交，工作树干净）

### R 系（前会话，21 提交）
R00 基线审计 / R01 资源目录 / R02 LUT 注册 / R03 资源命令主干 /
R04 事务手势 / R05 节点 LUT 语义（含 HDR 恒等缺陷修正+golden 重归档）
/ R06 能力与输出一致 / R07 LUT 缩略图 / R08 逐节点缩略图 / R09 资源
闭包 / R10 旧入口路由+D1 / R11 真实状态投影。

### N 系（跨两会话，全部交付；v1 边界如实标注）
- **N01** 出口语义：full-res f4 / 播放附帧 / headless+Composite+Edit
  预览复杂图显式拒绝。**flag 默认仍关**（见下）。
- **N02** key 边图域化：key_out 产物通道、key_in×invert 组合、
  fail-closed 移除常白/图像顶替、GL↔CPU 对拍。
- **N03** typed handle：qualifier/window/tracker 域图命令 + D2 旧控件
  删除。
- **N04/N08/N11/N12/N13/N17**（前会话）。
- **N05** Node Color Space/Gamma：IDT/ODT 包裹（grading/LUT 在变换内
  侧）、PQ/HLG/DaVinci Intermediate + 六 gamut 矩阵（GLSL/numpy 同
  源）；Metal/pointwise 导出显式拒绝。菜单已启用。
- **N06** Node Cache：cone 键（剔策略位+节点级资源指纹）、Auto=分叉
  /On/Off、字节预算 LRU+世代老化、命中事件→徽标（区别于策略蓝条）。
  菜单已启用。
- **N07** Timeline 栈：timeline_grade_stacks 登记+命令+IO+懒建绑定；
  链序 pre→main→trim→post→timeline（5 段，4 消费端一致）；Composite
  跨组归属歧义告警。
- **N09** 压力录制：30 分钟原生实跑（五预算过四）+ 行为窗分段定位
  （float 1647/page 490 MB/10min 主犯）+ scope 池单活动池修复（float
  1647→1230）；残余 Qt GL 上下文重建路径登记待治理。
- **N10 v1** 节点 grade 域关键帧：三原子 op、params→payload 合并
  （命令/播放/导出同域 evaluate_grade_keyframes）、帧感知 provider、
  动画节点缓存绕过、LUT 导出阻断、徽标权威域。mask/qualifier 轨道、
  剪贴板/定义级扇出 v2 登记。
- **N14 v1** CPU LUT3D（trilinear+R05 混合+缓存；falsy-zero 强度 bug
  修复两处）；Metal/其余域保持 fail-closed 逐域推进。
- **N15** 窗口组合：add_window_combo 原子 op（CPW/LPW/PPW/PCW 一键
  Serial+Window）；实机人工对照待办（Resolve 脚本 API 不覆盖窗口创建，
  本机 Resolve 20.2.1 已连接验证）。
- **N16**：golden corpus 复绿（grading_extreme/checker 经 R05 ADR 重归
  档）；逐文件扫测本会话全部批次（含批次间回归修复——帧随参
  `__dict__` 直取）；tests/shared 存量失败（AI provider 类，clean HEAD
  复现）与单进程原生崩溃债务如实保留登记。

## R12 回归矩阵（本轮实跑）

| 套件 | 结果 |
|---|---|
| color 核心（service/compiler/keys/key_channel/mixers/cpu_parity/能力/缓存/关键帧/色彩空间/LUT3D/timeline 栈/窗口组合/UI 契约/迁移/持久化/闭包/粘贴/golden/scope 多视图） | **257 passed** |
| app 层（工具路由×3/全出口/生产开关/宿主/clip 切换/反馈/覆盖率/资源路由/控制器/手势/目录） | **219 passed** |
| 关键帧 legacy + M05 链序 + 组 | **54 + 26 passed** |
| 原生 workflow（TOPOS_COLOR_GRAPH_RENDER=1） | **passed**（project_history/grade_commit/save_reload/disconnect_reconnect 全过） |

## 平台与开关（如实）

- 本机 macOS（cocoa + ATI GL 4.1）原生验证通过；Linux/Windows/
  Apple Silicon 实机矩阵 **NOT RUN**（无设备，随实机批次补）。
- `TOPOS_COLOR_GRAPH_RENDER` 默认关闭：N01 出口语义+N09 矩阵已具备，
  残余 RSS 债务（float/page Qt 上下文重建）未归零——按计划「性能/资
  源矩阵通过才默认开启」维持关闭，不虚报。
- N16 债务保留：单进程全量套件原生崩溃（clean HEAD 复现）、
  tests/shared AI provider 存量失败、QThread/timer 逐所有者治理。

## 结论

R00–R11 与多数 N 项的已声明实现版本已有回归和本机原生证据。
**R12 完整发布验收尚未通过**：30 分钟 RSS 预算失败，其他平台未运行，
计划要求的 4K/HDR/32 节点及多出口同帧矩阵未逐项留证。
N15 的 Resolve 实机交互对照、N16 的全量套件债务与 R06 离线
AI/bake 执行 worker/结果生命周期也仍待完成；默认图渲染开关保持关闭。

## 复查补记（2026-11-04 第二轮整体复核）

全量重跑 + 语义深扫发现并修复 **2 个真实缺陷**：

1. **N06 预算淘汰纹理别名（真实渲染 bug）**：执行中预算淘汰把仍在
   当前 `node_outputs` 工作集的纹理归还池，后续 `_alloc_texture` 会
   取出当渲染目标——与下游采样同纹理 = GL 反馈环/串写（默认 256MB
   预算下 4 个 4K f2 缓存节点即可触发）。修复：工作集内淘汰延迟到
   执行末归还（`evicted_deferred` 事件可解释）；2 个回归测试（单元
   不变式 + 端到端位级一致性，旧代码上均失败验证非空洞）。
2. **菜单 parity 测试遗漏**：N05/N06 启用 Color Space/Gamma/Node
   Cache 菜单后测试仍断言旧「显式禁用」契约——更新为「启用 + 子项
   即真实命令值」新契约（color 全量由 3566+1 失败 → 3569 全绿）。

另修复 **12 个基线既有 tests/app 失败**（经 R00 基线 worktree 定证
全部先于本计划存在，与本批 48 提交无关）：8 个测试假宿主漂移
（`_report_package_compatibility`/`_on_mixer_routing_preview`×2/
`get_tracked_geometry`/`engine` 等产品新增入口未跟上）+ gap002 视图
桩升级到事务桥语义（`_Signal` 分发 + committed 链，含 ref 回写）+
wd1g6 撤销语义更新（M07 自动锁后为逐命令撤销）+ dock×2（350a037
Effects 升为一等可见面板的过期断言）。

复查无新问题区域：N05 TF 双向全路径输入防护（负值 clamp/log 下界）、
N07 dangling 栈优雅退化 4 段、N02 fail-closed 一致性；顺手清理
color_node_color_space.py 重复 raise 死代码。tests/app 由 12 failed
→ **全绿**；color+edit 复跑全绿。

### tests/edit 全量补扫（本轮一并定证）

tests/edit 全量 9652+ 单跑：27 失败，三类定证（R00 基线 worktree
逐文件对照）：

- **8 个已修**：n03_grade_degrade_gate/n03_layers_mask_preflight
  假宿主漂移（bd8b15f 稳定化重构后 `_apply_color_grade_unstabilized`
  /`_clip_stabilization_state` 未跟上——委托真实方法后 15 全绿）。
- **11 个基线文件级既有**（基线同样失败，与本批无关）：metal export
  ×2（渲染均值阈值）、m06 qualifier ×1、wd12g52 ×3（含 soundfile/
  qwen_asr 缺依赖——与本机 ssl 损坏同族环境债务）、transition/
  ef01/properties_layout/drag_inject ×4。
- **~8 个顺序依赖污染**（基线与当前 HEAD **文件级均通过**，全套件
  单进程跑才失败——前置测试状态泄漏）：wd12g65×3、round4×2、
  media_pool/wd11g53/ui_aud3/edit_mode_toggle×4。与已登记的「单进程
  全量原生崩溃」债务同族（非崩溃变体），治理归 N16 后续批次
  （逐所有者隔离污染源）。
