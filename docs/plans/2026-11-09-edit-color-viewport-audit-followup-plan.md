# 剪辑与调色 Viewport 第二轮复查及后续计划

日期：2026-11-09。审查基线：`660dd206`，包含 Edit I01–I07（`9ebd8ba6` 起）和 Color CV-B01–B11（`382da75e` 起）。本文件记录本轮局部修复和需要单独实施的问题，不覆盖上一轮计划的历史表格。

## 1. 完成状态的重新核对

本轮已确认：Edit 播放协调器、入口门、时间域、capability 宿主、Source 拖出、工程释放和预算执行器均有生产接线，**不能再沿用上一轮“只有领域件、没有 installer 调用”的结论**。Color 也已有能力宿主、TC formatter、Power Window、监看静音、关闭守卫等真实接线。

但是，以下内容仍未达到端到端完成：失败事务的文件／Undo 一致性、预算与用户质量选择共存、真实 VFR 播放，以及 Color 比较网格等明确保留的能力。旧记录中“域件／宿主／媒体”分栏仍有效，测试数量不能替代这些验收。

审查参考：

- [Edit 实施与验证记录](../validation/viewport_followup_i01_i07_20261109.md)
- [Color 实施与验证记录](../validation/color_viewport_backend_cv_b01_b11_20261109.md)
- [上一轮复查计划](2026-11-09-viewport-integration-review-followup.md)
- [Color 原实施计划](2026-11-09-color-viewport-backend-plan.md)

## 2. 本轮直接修复的小问题

| 范围 | 已复现问题 | 局部修复／回归边界 |
|---|---|---|
| Edit 时间域 | 导入写 `fps_num/fps_den/timecode`，Source 只读取另一组别名；源起始码丢失 | 以导入字段优先，兼容旧别名；不探测／解码 |
| Edit 换源状态 | 仅设置 In 或仅设置 Out，A→B→A 后端点丢失 | 分别校验恢复单端点，双端仍统一校验 |
| Edit 时长 | duration 误用带源起始 TC 的“末帧时间码” | 用总帧数格式化相对时长；关闭源清除旧时间域 |
| Edit 起播 | Source／Timeline 已安装仲裁门抛异常，仍放行播放 | 异常时拒绝本次起播并记录；pause 不受影响 |
| Edit 方向反馈 | Source 倒放快照仍 forward；Timeline 用 Enum.name 产生大写方向 | Source 暴露只读实际方向／1x 速度；Timeline 反馈使用 Enum.value，统一 forward/backward |
| 页面模式 | Deliver→Color／Composite 归还控件时把仲裁模式误置 Edit | 归还只恢复控件；由实际目标页 enter hook 决定模式 |
| Source 拖放 | dragEnter 后当前项目同 ID 素材被替换／移除，drop 仍用缓存 asset 验证 | drop 从当前项目重新取 asset，重新校验身份／范围后才提交 |
| Color 静音 | 静音时起播被跳过音频桥，解除静音仍无声 | 只恢复明确被延后的、仍在 Color Source-GL 播放的音频；暂停／无上下文／TimelinePreview 不新起第二条音频 |
| Color 静音 | 用户原 Master 已 mute，解除临时 mute 时先 False 再 True；失败却清除持有状态 | 一次写入原 Master 目标；服务失败保留原状态，供 UI 如实回填／离页重试 |
| Color TC | 29.97 的 `01:00:00:00` 按 NDF 解析，却按 DF 显示成 `01:00:03;18` | 有效显式起始码的 parsed.tc_type 同时决定偏移与显示；无 TC 继续原 auto／flag 政策 |
| Color 关闭 | 关门只断开刷新，迟到的 mute 信号仍调用服务 | 断开 mute 信号并在 handler 加关闭守卫，覆盖已排队回调 |
| Color 状态 | controller 的 0 帧／0 时长被 `or` 当缺失，回退到上一条 transport 数值 | 只在 None 时回退 |
| Color 状态栏 | 窄窗折叠后换素材／输出空间，tooltip 保留旧色彩链路 | 数据刷新同步 tooltip，不依赖宽度变化 |
| Color 比较域件 | release 后 active 仍 True；无 clip provenance 的两张 Gallery still 被误去重 | 释放同步 inactive 且幂等；缓存身份区分 kind 和 Gallery item ID，保留同版本去重 |

这些修复均有针对触发条件的回归用例；没有把下列重大问题用 UI 补偿或临时 Undo 掩盖。

## 3. 需要独立实施的任务总表

| ID | 优先级 | 问题与结果 | 实施范围 | 依赖／估算 |
|---|---|---|---|---|
| R01 | P1，优先 | 静帧覆盖保存失败会删除旧 sidecar，保留的旧 .topos 变为不完整 | 多文件保存事务、备份／恢复／失败与播放收尾 | 独立，1–3 人日 |
| R02 | P1，优先 | 被拒绝的 Source drop 仍改变 revision、Redo 和既有历史 | 轨道＋片段＋配对音频的统一可回滚编辑命令 | 独立，2–4 人日 |
| R03 | P1 | 双视图预算覆盖用户质量、实际尺寸超 cap、resize 后政策不更新 | 用户偏好与有效预算分离、真实尺寸／队列执行与反馈 | 独立，2–4 人日 |
| R04 | P1 | VFR 只有索引／helper 协议，运行播放仍按 CFR；反查每次扫描全索引 | Edit／Color 的 PTS 时钟、异步索引身份、音频映射 | 统一时间域，3–5 人日 |
| R05 | P2 | Color 快照声明 clip-local，却混入源全域帧数；preview 路径仍用源 controller 判断可播 | 按实际 presentation path 聚合状态与能力 | R04 时间域契约，1–2 人日 |
| R06 | 验收门槛 | 真实 4K/HDR、听感、长会话与完整失败循环尚缺证据 | 可复现的整页媒体验收与诊断记录 | R01–R05，1–2 人日＋设备 |

总估算 10–20 人日，不包含下文已知未实施产品能力。优先级表示本批实施顺序；不要把“已有测试全绿”当作关闭这些任务的依据。

### R01：静帧覆盖的多文件保存事务

**代码位置**：`src/app/controllers/layer_controller.py:LayerOpsMixin.on_capture_still`，`src/features/color/core/preset_meta.py:write_still_images` 与现有 SaveRouter。

**已复现**：在临时目录预置同名 `.topos`、`.thumb.jpg`、`.still.jpg`、`.still.exr`，确认覆盖，令 SaveRouter 返回 False。结果旧 `.topos` 仍在，三个 sidecar 全部不存在。当前先覆盖 sidecar，再保存本体；失败分支直接删除固定目标路径，无法区分新文件与旧文件。新文件覆盖已成功但本体保存失败时，也不能靠“别删除”恢复旧内容。

**实施步骤**：

1. 为本次抓帧建立同目录 staging；先完成精确帧、grade、缩略图／全分辨率 JPEG／可选 HDR EXR 的一致快照，不修改现有目标。
2. 统一 SaveRouter 与 sidecar 的提交点；覆盖前保存可恢复备份，失败／取消恢复整个旧文件集合。多个 rename 并非整体原子，明确中途失败／进程退出的恢复策略和 manifest／事务标识。
3. 仅清理本事务拥有的临时文件，禁止按固定目标名删除已有资产；成功后才更新 Gallery。
4. 用 finally 收束“仅恢复抓帧前确实在播”的政策；关闭／换 clip／generation 已变时不能由延迟恢复回调重新起播旧源。

**验收**：新建与覆盖各测成功／SaveRouter False／抛异常／JPEG 或 EXR 写失败／取消／提交中断。失败后旧四文件逐字节不变，零新孤儿文件；成功时四文件属于同一帧与 grade。暂停状态保持暂停，原播放的恢复遵守上下文与关闭守卫。

### R02：Source drop 的工程与历史事务

**代码位置**：`src/features/edit/ui/timeline_view.py:_handle_source_clip_drop`、`AddTrackCommand`、既有 command invoker／编辑 planner。

**已复现**：需要新轨时先执行 AddTrack，后续插入 False 后只调 Undo 回滚。轨数虽然恢复，但 revision 从 0 变 2，Undo 为空、Redo 有 1 项；再 Redo 会复活被拒绝 drop 的空轨。AddTrack 的 execute 还会先清掉用户原有 Redo 历史，后续 Undo 无法恢复它。上一轮“失败零 Undo 垃圾”只检查了部分结果。

**实施步骤**：

1. 所有身份／源范围／流／目标／锁轨／模式前置验证完成后再执行；本轮已修当前项目 asset 的 drop 复验，继续复用。
2. 新轨、视频／音频片段、配对轨、ripple／overwrite 修改、selection 与项目 revision 合并成一个可提交、可回滚命令或既有事务。
3. 失败恢复模型和完整历史边界：revision、Undo／Redo 内容与游标、dirty、预览失效和选择；不要通过调用公开 Undo 模拟“从未执行”。
4. 成功单次 Undo／Redo 覆盖全部效果；避免半条音频和孤立轨道。

**验收**：av／video／audio × insert／overwrite × 新轨／现有轨，分别在有旧 Redo 历史时失败、抛异常、锁轨拒绝、成功。失败整个工程与历史快照相等且无脏标记；成功仅新增一项历史，Undo／Redo 完整可逆。

### R03：预算、用户质量与真实执行值

**代码位置**：`src/features/edit/ui/viewport_budget_executor.py`、`src/features/edit/core/viewport_playback_budget.py`、`TimelinePreviewRenderer.set_preview_quality/_effective_preview_quality_scale` 及 Source 请求执行端。

**已复现**：用户选择 Full（1.0）后一次 coordinator snapshot 将 renderer 质量改为 0.25；800×450 cap 在 8K Timeline 上映射出的 0.25 实际为 1920×1080，超过声明上限；pane resize 没有触发 refresh，诊断仍保留旧的 800×450，直到其他事件发生。

**实施步骤**：

1. 拆开持久化 user preference 与临时 effective budget，不调用用户质量 setter 代替预算 setter。预算解除／离页后恢复用户偏好，菜单显示真实选择与有效降级原因。
2. 使用真实 sequence／native 尺寸计算两轴约束，考虑 renderer 的自动档、最小尺寸与 aspect fit。既有离散档不能满足上限时，采用明确的新执行接口或诊断“无法满足”，不能记录为已满足。
3. 对 pane resize、splitter、show／hide、native size、素材／sequence、renderer 懒创建／替换和页面变化合并更新；只在有效限制变化时应用，避免反复废缓存。
4. Source 的 `request_size_cap`／priority 也必须接到真实请求或明确标记未执行。仅检查 `_gpu_pending_frame` 属性存在不证明 queue／size／priority 已生效；记录 actual applied 而不是结构推断。
5. hidden 控件真实可见性与 Deliver 借用后的可见性纳入输入；shutdown 停止更新，精确暂停 seek 仍可请求一次。

**验收**：1080p／4K／8K、横竖画幅与非整数缩放，记录真实输出像素不超 cap；连续 resize 后诊断及时更新；手选 Full／Half 不被覆盖或写入临时值；来回切页／借还／替换 renderer 后限制正确；统计申请、排队、取消与 slot ack，而非只对假 renderer 记录 setter 次数。

### R04：VFR 的实际播放路径与查询复杂度

**证据**：导入器已可保存 `metadata['pts_index']`，但 Source `_build_time_domain` 不消费它，`set_source_time_index` 在生产代码只有定义。Source tick 仍用 `elapsed * _fps`，音频位置仍按 float fps。Color `vfr_seconds_for_frame/vfr_frame_for_seconds` 同样没有生产调用方，后者先遍历全部 points 构造 times，再 bisect；10,000 点单次查询访问全部 10,000 个 timestamp，非声明的无扫描二分。

**实施步骤**：

1. 明确可复用导入 PTS 与异步分析产物的 schema、单位、起点／时间基、fingerprint、generation、取消与失效；迟到的 A 索引不能注入当前 B。
2. 将 Source 正放／倒放／loop／scrub 和 Color 源域映射接至同一 PTS 索引；缺失索引显式声明 CFR fallback，不伪称精确 VFR。
3. 音频启动／seek／loop 位置从同一秒域换算，不能视频按 PTS、音频仍按平均 fps。
4. 复用索引原生二分／预构建的只读时间列；每次查询 O(log n)，不在 UI tick 遍历全片，不重复探测。验证 NaN、重复／非单调 PTS、范围外和不完整索引的政策。
5. 避免新建第三套时钟或绕过现有异步解码队列；与 R05 的 path／time_domain 契约一致。

**验收**：真实非均匀 VFR，逐点比较正确帧／秒及音频位置，覆盖换源、换工程、倒放、单帧 loop 和迟到结果。100k 点压力测试记录查询访问次数／延迟，查询成本不随全片长度线性增长。

### R05：Color 快照的呈现路径与时间域一致

**代码位置**：`src/app/controllers/color_viewport_capability_host.py:_gather_state/_gather_context` 及 FileOps 的源／clip-local／timeline 映射。

**问题**：当前状态声明 source_gl 为 clip_local，却直接取 video_controller 的 source current_frame／total_frames；例如源 120 帧、局部 20 帧、clip 长 50、源长 1000，快照仍可报告 clip_local 的 120／1000。TimelinePreview 分支也继续按源 video_controller 判断可播；只有 Timeline controller 时会把 transport 禁用。这不等同于已证明活跃默认路径出现播放头错跳，但快照契约和备选路径不成立。

**实施步骤**：按实际 presentation_path 选唯一权威 controller 与域；有 clip 的 Source-GL 把位置／总长映射为 clip-local，独立源明确 source_frame；TimelinePreview 取共享 Timeline controller；Composite 保持自己的 adapter。零值不可当缺失（本轮已修）。上下文切换、播放状态、loop、媒体就绪和失败回填使用可清理连接，避免旧 controller 信号污染新 snapshot。关闭守卫继续生效。

**验收**：trim／2x／反向／冻结片段、不同源／Timeline fps、源第 0 帧／末帧、仅 Timeline renderer，无源 controller 时仍按真实能力回填。TC、slider、snapshot 的 time_domain 与 actual frame 一致，刷新不发 seek／toggle。

### R06：整页与真实媒体验收

复用既有真实素材 fixture 和诊断，不把 offscreen 的单素材 CPU smoke 标成全页通过。需要记录：4K60／HDR 的单／双视图呈现率、p95、队列与 10 分钟内存；Source／Timeline 音频互斥与取消静音听感；50 次切 clip／工程与 30 次 Edit↔Color↔Composite↔Deliver／浮动／全屏循环；pending decode、保存失败、关闭／取消。每个结果注明设备、素材、路径、命令、指标与未覆盖项。

## 4. 已知未实施的产品范围

以下是已有记录明确延期的范围，**不是本轮新发现的回归，也不因“完成 CV-B01–B11”而自动存在**：Color 多版本比较的 grid presenter／GPU 网格与预算、Proxy／Resolution／monitor-scoped Bypass 服务、Unmix、独立 Grade Freeze、Alpha 显示路径、viewer-only fullscreen、Color 视图外观偏好持久化；Edit 的 Source Tape／源 markers／倒放音频／Source 外部监看等。

保持禁用并说明真实原因。若要把原完整产品目标标为端到端完成，应另行排期并逐项提供 handler／执行端／成功失败回填证据；无需为本轮小修擅自启用。菜单门 `set_menu_open` 也仍只有定义，尚未找到生产 feed；本轮未复现原生菜单捕获按键被穿透，不将其记为已证实双播，后续 UI 验收补证。

## 5. 本轮验证记录

审查前重点套件 **196 passed**；新增回归先复现再修复。局部测试包括 Color mute／TC／音频桥 76 passed、Edit Source／拖放／播放 138 passed、页面／入口／Deliver／组合宿主 169 passed；这些集合存在重叠，不相加计算覆盖量。

最终合并回归：**67 个聚焦套件，1263 passed，38.93s，零 skip／fail**。包含已有真实素材的 Edit CPU 解码／seek／时钟／释放 smoke，以及 Color metadata／TC smoke。它们不等同于 R06 的全页媒体验收。

实际套件清单已保存，可复现运行：

```sh
xargs .venv/bin/python -m pytest -q --disable-warnings --maxfail=4 \
  < docs/validation/viewport_second_review_20261109_tests.txt
```

[67 套件清单](../validation/viewport_second_review_20261109_tests.txt)。局部数字与最终回归有重叠，不相加。

原生 macOS 启动检查退出码 **0**，隔离配置未覆盖用户配置：

```sh
TOPOS_CONFIG_PATH=/tmp/topos-viewport-second-review-startup/config.json \
TOPOS_TEST_MODE=1 .venv/bin/python -m src.main \
  --product studio --test-startup --no-startup-dialog
```

启动无新增 traceback／宿主安装失败；日志仍有缩略图 FFmpeg 路径未找到、可选 AI 模型缺失及懒创建 renderer 尚未挂接的预算提示。本轮不把这些提示当作播放／性能通过证据。

`git diff --check` 通过。真实 GPU／HDR／4K 性能未在本轮自动测试中宣称通过。
