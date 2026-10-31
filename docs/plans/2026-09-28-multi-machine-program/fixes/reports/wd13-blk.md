# 修复报告 wd13-blk — 阻塞项/登记项重派批（8 条）

- 日期：2026-10-01
- 计划表：docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md（条目行
  193 / 616 / 619 / 620 / 631 / 722 / 761 / 1009；本报告不改动计划表）
- 前置复核：上轮额度中断已回退，本批在干净基线上重做。逐条以当前代码复核：
  6 条白名单阻塞项（AUD-17108 / AUD-16906 / AUD-16911 / AUD-26910 /
  AUD-20203 服务侧 / AUD-19500 接线）与 1 条登记项（AUD-16900 CPU/GPU 同步）
  仍存在并完成根因修复；1 条登记项（AUD-R2-003）已被后续修复取代，判 INVALID
  （附证据）。
- 改动文件（全部在本批白名单内）：
  - `src/features/edit/core/commands.py`（AUD-17108）
  - `src/features/edit/core/mixer_preset.py`（AUD-16906）
  - `src/features/edit/core/nested_sequence.py`（AUD-16911）
  - `src/features/teaching_animation/ai/html_llm_generator.py`（AUD-26910）
  - `src/shared/media/disk_proxy_service.py`（AUD-20203 服务侧）
  - `src/features/edit/ui/media_pool_panel.py`（AUD-20203 面板替换直写）
  - `src/features/teaching_animation/module.py`（AUD-19500 接线）
  - `src/features/edit/core/transition_mask.py`（AUD-16900 CPU/GPU 同步）
- 新增测试：`tests/edit/test_fix_wd13blk_aud.py`（51 项；无 GL 上下文、无真实媒体）
- 未修改任何既有测试（全部锁定行为与修复兼容，见"验证记录"）。

## 逐条结论

| 编号 | 结论 |
|------|------|
| AUD-17108 | **FIXED**（命令层注册/反注册对称；project.py 依赖倒置遗留见"遗留"） |
| AUD-16906 | **FIXED** |
| AUD-16911 | **FIXED**（分层放置 + 用户语义报错） |
| AUD-26910 | **FIXED** |
| AUD-20203 服务侧半 | **FIXED**（mark_ready 公共 API + 面板改走公共接口） |
| AUD-19500 接线 | **FIXED**（module.py 订阅 motion_graphic_template_requested） |
| AUD-16900 CPU/GPU 同步 | **FIXED**（CPU iris 对齐 GPU 自适应公式） |
| AUD-R2-003 | **INVALID**（端口数启发式已被 AUD-10602 的测量式方案移除，附证据） |

---

## 修复明细

### AUD-17108 — duplicate_asset 的 scene store 注册副作用撤销不可回收【FIXED】

**位置**：`commands.py` DuplicateAssetCommand（project.py:2075 的 register 调用
不在本批白名单，按 wd11-g58 的"命令层授权"建议实施）。

**修复**（注册/反注册对称，.clinerules 命令 apply/undo 约束）：

1. 新增命令字段 `created_scene_id: str | None = None`（to_dict/from_dict 随
   `created_asset_id` 一并往返，持久化命令的 undo 同样可回收）。
2. `apply` 后经模块级帮助函数 `_registered_scene_id_for_asset` 记录副作用：
   仅当新素材为 motion_graphic、metadata 带 scene_id 且**共享 store 中确实
   存在**该场景时记录（空场景回退分支与普通素材记录 None，undo 对其零动作）。
3. `undo` 经 `_unregister_duplicated_scene` 调 `get_shared_scene_store()
   .remove(scene_id)` 反注册——**素材已被其它路径移除的早退路径同样回收**
   （scene_id 由 apply 独占生成，不会误删其它素材的场景）。redo 重新走
   duplicate_asset 生成全新 scene_id 并覆盖记录，循环对称。
4. teaching_animation import 保持函数内惰性（与 project.py 现风格一致），
   不在 commands.py 模块加载期建立反向依赖。

**测试**：`TestDuplicateAssetCommandSceneRollback`（5 项：注册记录+undo 反注册、
素材已失时仍反注册、普通素材零记录、空场景回退分支零记录、dict 往返）。

**遗留**：project.py duplicate_asset 内仍直接 import scene_store 并注册
（跨 feature 反向 import 的依赖倒置需 project.py 归属批次授权"store 由调用方
注入"式改造）；本条的主害——撤销不可回收的副作用——已消除。

### AUD-16906 — mixer 预设库单条损坏即永久清空【FIXED】

**修复**（mixer_preset.py）：

1. `_parse_preset_entry`：逐条解析，损坏条目（非 dict / float() 抛 TypeError·
   ValueError 等）跳过并 `logger.warning`——单条坏记录不再拖垮整个列表。
2. `_load_presets_for_write`：顶层 JSON 损坏 / schema 畸形（非对象、presets
   非列表）时返回 **None**（区别于空列表）；`load_presets` 对外仍回空列表。
3. `add_preset` / `remove_preset`：载入得 None 时**拒绝整文件落盘覆写**（error
   日志，仅返回内存视图）——盘上损坏库保留人工修复机会，不再被单条新预设
   清空（返回值形状与旧行为一致）。
4. `save_presets`：临时文件 + `os.replace` 原子替换；except 收窄为
   `(OSError, TypeError, ValueError)`（磁盘/权限/不可序列化），失败清理临时
   文件——不再留下截断的半成品文件。
5. 全部宽泛 `except Exception` 移除，模块新增 logging。

**测试**：`TestMixerPresetLibraryResilience`（5 项：坏条目跳过、坏库 add 不
覆写、坏库 remove 不覆写、序列化失败原子性+无 .tmp 残留、add/load 往返）。

### AUD-16911 — build_nested_from_selection 宿主轨重叠校验误伤合法选择【FIXED】

**修复**（nested_sequence.py，分层放置 + 显式前置检测双管齐下）：

1. 候选宿主轨道 = 有选中片段的视频轨（按最早选中片段排序；纯音频选择回退
   首条选中轨道，与旧行为一致）；嵌套片段占整个选择跨度
   `[min_start, min_start+span)`。
2. 逐候选显式检测跨度内是否残留未选片段：脏候选**顺延到下一候选**（分层
   放置）——多轨/非连续选择中存在干净视频轨时构建成功而非崩溃。首选候选
   干净时宿主选择与旧代码逐字节一致（成功路径零行为变化）。
3. 所有候选都脏时抛带用户语义的 ValueError（含帧号区间，指导用户把跨度内
   同轨片段纳入选择或移出），**不再泄漏内部 UUID / "Clip overlaps with"**
   （旧错误经状态栏直接展示给用户）。
4. docstring 声明前置条件与放置策略（Raises 段补全）。

**测试**：`TestNestedSelectionHostTrackPlacement`（3 项：脏 v1 顺延干净 v2
并验证父/子轨道结果、全脏抛用户语义错误（无 UUID）、连续选择行为不变）。

### AUD-26910 — strip_markdown_fence 无条件截断腰斩正文【FIXED】

**修复**（html_llm_generator.py）：`rfind("```")` 无条件截断改为
`re.search(r"```\s*$")`——仅当残留围栏位于文档**末尾**（其后仅空白）才剥离。
正文含代码围栏的教学页面（如 `<pre>` 内示例代码）不再被静默腰斩；正文围栏 +
外层围栏共存时只剥外层；行 230 的历史 `_FENCE_RE` 兼容通道本就以 `$` 锚定
末尾，保持不变。

**测试**：`TestStripMarkdownFenceBodyFences`（4 项：正文围栏不截断、前导
preamble+尾围栏正常剥离、正文围栏在外层剥离后存活、纯文档/空值安全）。

### AUD-20203（服务侧半）— DiskProxyService 公共 cache 写入 API【FIXED】

**修复**：

1. `disk_proxy_service.py` 新增公共 `mark_ready(source_path, info)`：服务自有
   `_lock` 内写 `_info_cache`，随后与内部生成完成路径一致地分发状态回调
   （订阅方如 AssetProxyManager 据此同步 manifest）；不越权触碰
   VideoFrameProviderService（面板已自行经其公共接口更新）。
2. `media_pool_panel.py` `_update_disk_proxy_services` 的带锁直写块
   （`with disk_proxy_service._lock: ... _info_cache[...] = ...`）替换为
   `get_disk_proxy_service().mark_ready(source_path, proxy_info)` 一行——
   面板不再触碰该服务的私有锁与私有缓存（wd12-g66 留下的半侧注释指引兑现）。

**测试**：`TestDiskProxyMarkReadyApi`（2 项：mark_ready 发布 cache + 回调
分发 + get_proxy_status/READY；面板 fake 服务只暴露 mark_ready（无
`_lock`/`_info_cache` 属性）断言公共调用恰好一次且 FrameProviderService 半侧
不回归）。wd12-g66 的既有锁定测试（fake disk 服务无 mark_ready）只断言
frame-service 侧，面板 except 路径吸收 AttributeError，实测仍绿。

### AUD-19500（接线）— 宿主订阅 motion_graphic_template_requested【FIXED】

**修复**（teaching_animation/module.py，按面板侧 wd11-g64 声明的契约
"宿主应连接本信号并在确认前把 plan["use_template"] 置 True"）：

1. `attach_main_window` 的面板接线块（confirm/cancel/proposal 同块、同一
   `_teaching_motion_approval_attached` 一次性守卫）追加订阅
   `motion_graphic_template_requested` → `_request_motion_graphic_template`。
2. 新增处理方法：pending plan 置 `use_template = True` 后走与"确认执行"相同
   的 `_confirm_motion_graphic_plan` 分派——MG-05 守卫（LLM 未产出 scene 时
   拒绝裸确认）据此放行内置模板场景；plan 未知/已完结时直接按普通确认分派。
3. 接线后面板 `receivers(...)` 检测到订阅者，专用信号路径真正可达（未连接
   的旧宿主仍走面板侧回退，不回归）。

**测试**：`TestMotionGraphicTemplateWiring`（3 项：attach 后 receivers==1、
信号发射置 use_template 并转确认链、未知 id 仍安全委托）。

### AUD-16900（CPU/GPU 同步）— transition_mask.py iris 1.55 对齐 GPU 公式【FIXED】

**修复**：CPU iris 分支改用与 `gpu_transition_shaders.py:procedural_mask`
（fc3436c6，wd11-g57）完全相同的自适应公式（两侧 x 均已乘 aspect）：

- 圆（iris_circle）：`radius = progress * sqrt(aspect*aspect + 1.0)`（对角距）；
- 椭圆（iris_ellipse）：保持 0.62 纵横比，
  `ellipse_scale = progress * sqrt(aspect*aspect + 1.0/(0.62*0.62))`，
  y 侧分母 `ellipse_scale * 0.62`——与 GPU 侧 0.62 尺度补偿逐项一致。

宽幅画幅（16:9/2.39）下 iris 终段 CPU 导出与 GPU 预览不再有可预期差异，
终点全帧跳变的根因（盖不住左右边缘中点/四角）两侧同步消除。polygon 顶点
常数（58/60 行等）**有意不动**：GPU 侧 `transition_mask_gpu.py` 本就消费本
文件的 `_shape_vertices`，天然一致。

**测试**：`TestIrisCpuGpuAspectAlignment`（11 项：GPU 公式 numpy 镜像在
4 种画幅 × 3 个进度 × 圆/椭圆上与 CPU 掩码 `array_equal`；16:9 边缘中点在
progress<1 时已被覆盖（记录旧 1.55 公式未覆盖的缺陷事实）；椭圆上下边缘中点
0.62 补偿后覆盖；0/1 端点钳制不变）。wd11-g57 的 GPU 侧测试
（test_fix_wd11g57_aud.py）实测仍绿。

### AUD-R2-003 — _calculate_reconnection_links 端口数启发式漏多 image 输入口【INVALID】

**证据（当前代码复核）**：

1. `_calculate_reconnection_links` 在 `src/` 全库已不存在
   （`grep -rn "_calculate_reconnection_links" src` 零命中；唯一命中是
   `tests/composite/test_fix_wd4g25_aud.py:833` 断言其**不存在**）。
2. `DeleteNodeCommand.redo`（node_graph_commands.py:89-144，AUD-10602，
   wave-4）已改为**测量式**：删除前快照全图连接集，`remove_node_and_reconnect`
   后差分——引擎真实创建的桥接链路被精确记录（含 Blur velocity/Grade mask
   等第二 image 输入口场景），undo 经 `graph.disconnect` 显式拆除，不再依赖
   "connect 单输入替换碰巧正确"。
3. w1-g8 报告发现该问题时所担心的精确场景已由
   `test_fix_wd4g25_aud.py::test_aud_10602_delete_command_measures_reconnected_links`
   （双 image 输入口 Blend）锁定覆盖。

**本批守护测试**：`TestReconnectHeuristicSupersededGuard`（2 项：启发式
不存在断言；Blur 节点（image+velocity 双 image 输入口，仅主口连线）删除→
桥接链路被记录→undo 精确还原拓扑）。

---

## 验证记录

py_compile（.venv Python 3.12，全部通过）：

```
$ .venv/bin/python -m py_compile src/features/edit/core/commands.py \
    src/features/edit/core/mixer_preset.py \
    src/features/edit/core/nested_sequence.py \
    src/features/teaching_animation/ai/html_llm_generator.py \
    src/shared/media/disk_proxy_service.py \
    src/features/edit/ui/media_pool_panel.py \
    src/features/teaching_animation/module.py \
    src/features/edit/core/transition_mask.py
PY_COMPILE OK (8 files)
$ .venv/bin/python -m py_compile tests/edit/test_fix_wd13blk_aud.py
PY_COMPILE OK
```

新建测试 + 邻接（.venv/bin/python -m pytest，逐组输出）：

```
tests/edit/test_fix_wd13blk_aud.py                                        51 passed
tests/edit/test_mixer_preset_completeness.py
+ tests/edit/test_fix_wd11g64_aud.py（AUD-19500 面板侧锁定）
+ tests/ai/test_html_llm_generator.py（AUD-26910 邻接）
+ tests/edit/test_nested_sequence_production.py（AUD-16911 锁定行为）
+ tests/edit/test_media_pool_finder_phase2.py（DuplicateAssetCommand 锁定）
+ tests/edit/test_fix_wd12g66_aud.py（AUD-20203 面板侧锁定）
+ tests/composite/test_fix_wd4g25_aud.py（AUD-10602 锁定）         135 passed, 1 skipped
转场域（test_transition_mask_gpu / test_gpu_transition_preview /
  test_transition_effects / test_custom_transitions / test_native_transitions /
  test_transition_preview_mode / test_audio_transitions）            121 passed
tests/composite/test_node_graph_commands.py
+ tests/composite/test_composite_undo.py
+ tests/composite/test_fix_ui_crashes_aud.py
+ tests/edit/test_motion_graphic_creation_entrypoints.py              130 passed
tests/edit/test_edit_project.py（duplicate_asset/remove_asset 锁定行为）
+ tests/shared/test_fix_audio_mixer_aud4.py
+ tests/media/test_p1_1_disk_proxy_priority.py                         98 passed
tests/teaching_animation/（module.py 全域）                            189 passed
tests/ai/（html_llm_generator + motion graphic 全域）            625 passed, 10 skipped
tests/media/（disk proxy 域全量）                      3322 passed, 63 skipped, 4 failed*
tests/edit/test_fix_wd11g57_aud.py（AUD-16900 GPU 侧锁定）
+ tests/edit/test_media_pool_panel_refresh.py                           55 passed
tests/composite/（node graph 全量）                      7044 passed, 13 skipped, 1 xfailed
tests/edit/test_fix_wd11g58_aud.py（project.py duplicate 域锁定）
+ tests/feature_availability/test_teaching_animation_phase0.py
+ tests/ui/test_motion_graphic_manual_controls.py                       86 passed
```

\* tests/media/ 的 4 个失败为**既有环境失败，与本批无关**（单文件复跑即失败，
不经过本批任何改动文件；按锁定旧行为协议不做处理、不改任何既有测试）：

- `test_macos_vt_compression.py` 3 项（`[True-h264]` / `[True-hevc]` /
  `multitrack_surface[True]`）：libx265 HEVC 10-bit 软件编码环境守卫拒绝 +
  VideoToolbox bridge 能力探测缺失——w4-g6 报告已用修复前代码复现并登记为
  既有失败（fixes/reports/w4-g6.md "失败处置"段）。
- `test_bench_timeline_export_windows.py::test_declared_sources_exist`：
  断言 gitignored 本地夹具 `tests/test_videos/2K_topos_proxy.mov` 存在，
  本机缺该文件（与 REMEDIATION_PLAN AUD-R2-005 记录的 gitignored fixture
  环境问题同类）。

无既有测试因本批修改失败；无锁定旧行为更新；无 REMEDIATION_PLAN.md 改动；
无 git 分支/commit/push 操作。

## 遗留 / 交接

1. **AUD-17108 依赖倒置残余**：project.py duplicate_asset 仍直接 import
   teaching_animation scene_store 并在数据层注册（project.py 不在本批白名单）。
   撤销可回收性已由命令层消除；建议 project.py 归属批次实施"store 协议由
   调用方注入"以断开反向 import。
2. **AUD-16911 全脏拒绝**：所有候选宿主轨跨度内都有未选片段时仍拒绝（分层
   放置仅覆盖存在干净候选的场景），报错已带用户语义；如需支持全脏场景需
   引入自动新建轨道策略（行为变化更大，需产品决策）。
3. **AUD-20203 回调面扩展**：mark_ready 会分发状态回调（AssetProxyManager
   同步 manifest），面板路径由"仅写 cache"变为"写 cache + 订阅方通知"——
   与服务内部生成完成路径对齐，属预期增强；如有调用方不希望通知，可在
   mark_ready 上加 notify 开关。
