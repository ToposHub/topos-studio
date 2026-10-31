# EF-03 复查报告（GLM5.3）— src/features/edit/ui

- 复查日期：2026-10-01
- 批次：EF-03（e_flash_batches.json，39 条，白名单 `src/features/edit/ui`）
- 执行提交：`bff172ec6`（bff172ec652f790c9a70c809f0eda6ec3a2031bd，`[fix-E ef-03] … (39)`）
- 执行报告：`fixes/reports/ef-03-part1.md`（20 条）、`fixes/reports/ef-03-part2.md`（19 条）
- 复查方式：只读复核（git show/log 只读 + 当前 HEAD 源码独立核实 + py_compile 抽检；未运行 pytest、未勾选、未改任何源码/计划文件）

## §6 八项检查逐项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | `git show --stat bff172ec6` 共 32 个文件：27 个源码全部在 `src/features/edit/ui/`；4 个测试在 `tests/ui/`、`tests/edit/`；2 个报告在 `fixes/reports/`。REMEDIATION_PLAN.md / e_flash_batches.json / FIXLOG.md 零改动（定向 diff 为空）。无白名单外文件 |
| 2 | 真实修复（逐条） | **PASS** | 39/39 在当前 HEAD 独立核实成立（38 FIXED + 1 INVALID）。AUD-20605 的 INVALID 判定独立验证通过：`git show 8307d1f9c` 证实该提交确在 `scene_cut_shortcuts.py` 别名表加入 `"Delete": (Key_Delete, Key_Backspace)`，本批补回归测试是正确处置。逐 ID 证据见下表 |
| 3 | GLSL/HDR 红线 | **PASS** | 新增 GLSL 仅 source_viewer 顶点着色器（`u_display_rect`/`u_flip_y`），字面量 `0.5`/`1.0` 均带小数点，无循环。无 HDR 数值钳制、无 601/709 矩阵改动（diff 中 601/709 命中仅为报告文本行号） |
| 4 | 并发 | **PASS** | UI 线程无新增 OpenGL/moderngl 调用（program_viewer/source_viewer 的 gl 调用位于 QOpenGLWidget 自有上下文路径，为既有模式；本批反而消除了每帧 glGetUniformLocation/glBufferData）。后台化均走 QThreadPool worker + 队列信号回 UI（19511/20210/20502）；拖放探测用有界 ThreadPoolExecutor（≤4，21412）；provider 关闭延迟到 worker finished 且带"无运行中 worker"守卫（20604）；状态锁大量补 try/finally（20702/20717/21304 等） |
| 5 | 资源 | **PASS** | `_pending_requests` finally 清理（20714）；multicam `_pending_by_key` 超时剪枝防泄漏（20301）；scene_cut 旧 provider 超时移入 pending-close 列表安全释放（20604）；未发现异常路径资源泄漏 |
| 6 | 异常 | **PASS** | 新增源码行无裸 `except:`、无 `except Exception: pass`。新增的 8 处 `except Exception` 全部 `logger.debug`/`logger.warning` 留痕并带 noqa 理由注释（media_pool_panel.py:340/372/400/426/471、quick_project_dialog.py:71/246/284、scene_cut_dialog.py 延迟关闭路径）。media_pool 822/835/1946 与 scene_cut_dialog 217 的静默 except 为**既有代码**，非本批引入（diff 未触及） |
| 7 | 测试质量 | **PASS** | 新增 121 项（part1 51 + part2 70，与提交信息一致）；每条 ID 至少一个正例；part1 多数 ID 另有 `test_reverted_*` 负控用例（模拟回退行为验证测试会失败）；锁治理用异常注入验证 finally 释放；GL 热路径用 AST 结构断言避免测试环境 GL 上下文依赖。2 个既有测试随异步语义最小适配且已在报告"行为变化说明"披露：`test_ai_assistant_panel_p0_integration.py`（等待 settle + 附件按多重集断言，适配 19511 后台编码）、`test_adr_take_sheet_ui.py`（_FakeManager 补 `list_input_devices` 接口对齐，无断言弱化）。弱断言标注见"测试质量附注" |
| 8 | 回退验证建议 | 已给出 | 见下文（AUD-20316、AUD-21204，含精确原文与 pytest 节点 id；flash 执行期已对 20316/19412/21304/21204 做过临时回退验证且失败，本复查另核对了节点 id 存在性） |

## 逐 ID 核实表（39/39，证据为当前 HEAD 的 file:line）

| ID | 计划指控摘要 | 报告结论 | 复查判定 | 当前 HEAD 证据 |
|----|--------------|----------|----------|----------------|
| AUD-19403 | 设备枚举 UI 线程同步且构造期触发 | FIXED | CONFIRMED | adr_take_sheet_panel.py:404（`QTimer.singleShot(0, self.reload_devices)` 延迟补扫）；:461-483（枚举失败捕获 AttributeError/RuntimeError/OSError → 降级 System Default + logger.warning） |
| AUD-19407 | 菜单每次打开同步重扫插件目录 | FIXED | CONFIRMED | ai_assistant_composer.py:662（`_refresh_plugins_if_changed()` 门控）；:686（`_plugins_directory_signature` mtime 轻量签名，无 JSON 解析） |
| AUD-19412 | 锁定/拒绝按钮不按 decision 门控 | FIXED | CONFIRMED | ai_decision_inspector.py:71-79（`lock_btn.setEnabled(card.state != "locked")`、reject 同理；替换按钮保留） |
| AUD-19511 | 图片附件解码/编码 UI 线程同步 | FIXED | CONFIRMED | ai_edit_assistant_panel.py:91（`_ImageAttachmentWorker(QRunnable)`）；:2613-2619（QThreadPool.globalInstance().start + 队列信号回填）；:507（signals 对象接线） |
| AUD-19513 | 无 Run Drawer 时 shimmer 定时器空转 | FIXED | CONFIRMED | ai_edit_assistant_panel.py:1761-1777（`set_live_status` 存在才 start；否则 stop；目标消失自愈） |
| AUD-19620 | 高风险置顶文档与实现不符 | FIXED | CONFIRMED | ai_timeline_diff.py:3-8（docstring 明确置顶构成 = removed + 预检 warnings，替换为 removed+added 对） |
| AUD-19700 | Find 命中后 cue_selected 被锁抑制 | FIXED | CONFIRMED | caption_workspace.py:500-507（Find 路径显式补发 `cue_selected.emit`；控制器驱动路径仍被 `_building_ui` 抑制，无回环） |
| AUD-19730 | docstring 声称归一化桥接已过时 | FIXED | CONFIRMED | clip_transform_handle_controller.py:1-18（docstring 重写为像素空间直接模型，注明 AUD-19730） |
| AUD-19804 | search_input.clear() 无状态锁双重重建 | FIXED | CONFIRMED | edit_effects_panel.py:655（`_updating` 初始化）；:1355-1361（clear 持锁）；:1995（锁内直接返回）；:2189-2196（toggle 持锁 + 停 debounce 定时器） |
| AUD-19807 | 注解引用未导入的 CaptionStylePreset | FIXED | CONFIRMED | edit_effects_panel.py:1900-1904（与 BUILTIN_PRESETS 一并局部导入）；:1925（注解处可解析） |
| AUD-20011 | 效果参数回调缺防递归守卫 | FIXED | CONFIRMED | edit_properties_panel.py:4375-4400（`_slider_changed`/`_drag_changed` 开头 `if self._updating: return`）；:4533+/4547+（`_eff_*` 同）；bool/option/choice lambda 同样带守卫 |
| AUD-20209 | 缩略图回填全树扫描 O(n²) | FIXED | CONFIRMED | media_pool_panel.py:6178-6202（`_rebuild_tree_item_index`）；:6218-6219（懒重建）；:4792/:5426/:7088（树重建/清空/结构变化点置脏） |
| AUD-20210 | UI 线程同步 av.open+ffprobe（5s 超时） | FIXED | CONFIRMED | media_pool_panel.py:382（`_probe_proxy_resolution` 模块级）；:447（`ProxyResolutionWorker(QRunnable)`）；:10571+（`_update_disk_proxy_services` 只 stat 即派发）；:10600/:10613（`_on_proxy_resolution_probed`/`_apply_proxy_ready` UI 队列回填） |
| AUD-20213 | 列表视图高亮函数永远空转 | FIXED | CONFIRMED | media_pool_panel.py 仅剩 `_update_timeline_highlight_grid`（:5600），`_update_timeline_highlight_list` 全仓 grep 零命中（函数与 2 处调用均删除） |
| AUD-20301 | _pending_by_key 永不清理+过期帧回显 | FIXED | CONFIRMED | multicam_panel.py:326（每批请求前 `_prune_stale_pending()`）；:346-（超时剪枝实现，5s）；:336-338（tile request_key 匹配）；:371（迟到结果 key 不匹配即丢弃） |
| AUD-20316 | 反向播放实测 fps 冻结 | FIXED | CONFIRMED | playback_hud.py:117-122（`if dt >= 0.2 and delta != 0: self._measured_fps = abs(delta) / dt`；停止语义不变） |
| AUD-20405 | 每帧 glGetUniformLocation + STATIC_DRAW 重建 VBO | FIXED | CONFIRMED（附注） | program_viewer.py:1387-1396（`_tex_uniform_location()` 缓存，program 重建置 None）；:1440-1453（`GL_STREAM_DRAW` 正确 usage + 注释）。**附注**：每帧 glBufferData 上传仍保留（4 顶点 ~64B，成本可忽略），报告已如实披露；对比 source_viewer（AUD-20904）采取的是完全消除每帧上传的方案 |
| AUD-20406 | CPU 上传路径 1-3 次全帧拷贝 | FIXED | CONFIRMED | program_viewer.py:97（`_rgb888_qimage_to_rgba` 预分配 RGBA，单次跨距拷贝 + alpha 广播）；:1103（调用点）；测试 `test_helper_avoids_temporary_full_frame_arrays` 验证无 concatenate/ascontiguousarray 临时全帧 |
| AUD-20411 | 每字幕循环体内 import | FIXED | CONFIRMED | program_viewer.py:2935（`_paint_subtitle_overlay`）；imports 位于 :2949-2962 区（循环体 `for item in self._subtitle_overlays` 在 :2980 之后），帧级一次；graphics 导入失败仅禁用动画 |
| AUD-20502 | 构造期 UI 线程同步探测元数据 | FIXED | CONFIRMED | quick_project_dialog.py:49（`_MetadataProbeWorker(QRunnable)`）；:167（构造只生成扩展名占位，零 IO）；:291-294（QThreadPool 派发 + 回填） |
| AUD-20509 | 目录搜索反复 listdir 无缓存 | FIXED | CONFIRMED | relink_media_dialog.py:480（`_build_directory_index` filename→path 索引）；:421-429（`_index_for` 每目录只建一次）；:515-528（`_search_file_in_directory` 委托同一索引，口径一致） |
| AUD-20604 | wait 超时被忽略+迟到帧穿守卫 | FIXED | CONFIRMED | scene_cut_dialog.py:1492/:1495（pending_close 列表 + `_preview_generation` 代际）；:1671-1678（`wait(1000)` 超时→provider 移入待关闭）；:2117（代际不匹配丢弃旧源结果）；:2092（finished 回调排空待关闭 provider，带运行中 worker 守卫） |
| AUD-20605 | Backspace 承诺为拒绝快捷键未实现 | INVALID（已被修） | **判定成立** | 独立验证：`git show 8307d1f9c -- scene_cut_shortcuts.py` 显示该提交加入 `"Delete": (Key_Delete, Key_Backspace)` 别名（当前 :77）；快捷键表默认键为 "Delete"（:19），Backspace 经别名命中。补回归测试 TestAud20605BackspaceReject（3 例）是合理处置 |
| AUD-20702 | graph widget _updating 无 try/finally | FIXED | CONFIRMED | scene_cut_graph_widget.py 7 处 finally（:751/:770/:911/:929 等，覆盖 set_frame/set_threshold/set_current_frame/两个转发/ThresholdControls.set_threshold） |
| AUD-20714 | request_thumbnail 无完成清理+文档相悖 | FIXED | CONFIRMED | scene_cut_preview_widget.py:849-889（finally 中 `_pending_requests.pop`，含 loader 抛异常路径；docstring 明确同步语义） |
| AUD-20717 | preview widget 三方法锁无 try/finally | FIXED | CONFIRMED | scene_cut_preview_widget.py:628/:697/:709（set_candidate/set_frame_preview/clear 全部 finally 释放） |
| AUD-20803 | 批量操作循环期间不持锁 | FIXED | CONFIRMED | scene_cut_result_model.py:405（`_apply_decision` 只改数据不发信号）；:481（`_run_batch` 循环整体持 `_updating`，decision_changed 延迟到循环后统一发）；:535-594（5 个批量方法全走管线） |
| AUD-20807 | 文档宣称排序但未实现 sort() | FIXED | CONFIRMED | scene_cut_result_model.py:602（`def sort(...)` noqa N802；支持 Frame/Score/Scene 升降序；layoutAboutToBeChanged/layoutChanged；`_current_index` 跟随重映射；其余列 no-op） |
| AUD-20815 | overlay 可见性操作无锁有回环 | FIXED | CONFIRMED | scene_cut_timeline_overlay.py:686-706/:720-745（`_updating` 锁内更新状态与可见性，锁外发 overlay_visibility_changed） |
| AUD-20904 | source_viewer 每帧 uniform 查询+STATIC 全量重传 | FIXED | CONFIRMED | source_viewer.py:533-535（uniform loc 属性缓存）；:564-598（initializeGL 链接后缓存三 uniform；VBO 静态单位 quad）；:797-851（`_render_texture` 无 glGetUniformLocation/glBufferData，翻转经 `u_flip_y` uniform 表达）。AST 测试 `test_render_texture_has_no_per_frame_location_query_or_upload` 锁定 |
| AUD-21003 | keyPressEvent Delete 不查轨道锁 | FIXED | CONFIRMED | subtitle_clip_item.py:534-546（Delete/Backspace 在 `_is_track_locked` 时直接 accept 不发信号，与鼠标/右键路径一致） |
| AUD-21110 | 禁用条纹重置透明度泄漏到波形/名称 | FIXED | CONFIRMED | timeline_items.py:1667-1677（条纹在 `painter.save()`/`restore()` 内以 opacity 1.0 绘制，restore 后 0.5 贯穿波形与名称条） |
| AUD-21203 | 命中测试漏 S/V 分隔条+优先级文档矛盾 | FIXED | CONFIRMED | timeline_layout.py:775+（resolve_timeline_hit 第 2b 步补查 `subtitle_splitter_rect` → `area="splitter"`；docstring 修正为 ruler → V/A splitter → S/V splitter → clip/track/header → empty） |
| AUD-21204 | ppf≤0 不拒绝致除零 | FIXED | CONFIRMED | timeline_layout.py:258-267（`set_pixels_per_frame` ppf≤0 → logger.warning + 保留旧值）；configure 同守卫（:232-243） |
| AUD-21304 | _zoom_updating 多处无 try/finally | FIXED | CONFIRMED | timeline_panel.py:1662-1668/:1687（三处 try/finally：_refresh_zoom_slider_bounds/_on_view_zoom_changed/set_zoom_level） |
| AUD-21306 | HUD 轮询同步磁盘校验最多 200 素材 | FIXED | CONFIRMED | timeline_panel.py:58（`_PROXY_HUD_CACHE_TTL_S = 10.0`）；:239（缓存属性）；:2183-2208（素材集合 hash + TTL 双条件，集合变化或过期重算） |
| AUD-21408 | 锁轨 trim 反馈走死路径 | FIXED | CONFIRMED | timeline_view.py:515（`status_message_requested = pyqtSignal(str)`）；:4022-4023（锁轨 trim 发该信号 + I18N.tr）；`getattr(self,'status_bar')` 死路径全文件 grep 零命中 |
| AUD-21410 | 布局 O(n²)+滚动每 tick 全量重排 | FIXED | CONFIRMED | timeline_view.py:1184（`ordered_index` 字典预建，主循环+字幕循环复用 :1193/:1312）；:1515-1530（三个滚动 handler 改走 `_schedule_scroll_layout_update()` 16ms 单发合并） |
| AUD-21412 | 拖放逐个同步探测阻塞 UI | FIXED | CONFIRMED | timeline_view.py:13911-13919（有界 `ThreadPoolExecutor` ≤4、`drop-probe` 前缀并行探测；失败回退同步路径；drop 保持同步的边界理由已在报告注明——QDropEvent 生命周期限制，成立） |

统计：CONFIRMED 38 + INVALID 判定成立 1 = 39/39。ISSUE 0，Critical 0。

## 红线发现明细

1. **裸 except / 吞错**：新增行零命中。8 处新增 `except Exception` 均留痕（详见 §6 表第 6 项）。存量静默 except（media_pool_panel.py:822/835/1946、scene_cut_dialog.py:217）非本批引入、不在本批指控范围。
2. **UI 线程 GL**：无新增 moderngl；QOpenGLWidget 路径的 gl 调用为 Qt 标准机制（既有），本批是净减负（消除每帧查询/上传）。
3. **HDR**：无 >1.0 值域钳制引入；无归一化假设引入。
4. **601/709**：无色彩矩阵改动。
5. **GLSL**：source_viewer 新增着色器代码字面量合规（`0.5`、`1.0`），无循环边界问题。

## 测试质量附注（非阻塞）

- doc-gap 类条目（19620/19730/38009/38100——后两条在 EF-14）用文档字符串内容断言，属字符串匹配，强度弱但与指控类型（doc-gap）相称。
- `TestAud20405::test_render_texture_structure` 断言 `"GL_STREAM_DRAW"` 字面量存在：若未来升级为完全消除每帧上传（source_viewer 式静态 VBO），该测试会误报——轻微脆弱性，可接受。
- 源码文本 grep 类断言（如 `test_no_import_inside_per_subtitle_loop`、`test_no_status_bar_dead_path_left_in_timeline_view`）可被动态 import 等写法绕过，作为回归护栏足够。
- 负控用例（`test_reverted_*`，part1 约 10 个）是本批测试的突出优点：直接模拟回退后的旧行为并断言差异，回归有效性高于普通正例。

## 回退验证建议（供中心会话抽查）

1. **AUD-20316** — 文件 `src/features/edit/ui/playback_hud.py`（约 :121-122）
   - 当前原文（old）：
     ```python
     if dt >= 0.2 and delta != 0:
         self._measured_fps = abs(delta) / dt
     ```
   - 回退代码（new）：
     ```python
     if dt >= 0.2 and delta > 0:
         self._measured_fps = delta / dt
     ```
   - 应失败节点：`tests/ui/test_ef03_aud_part1.py::TestAud20316ReversePlaybackFps::test_reverse_playback_updates_fps`
2. **AUD-21204** — 文件 `src/features/edit/ui/timeline_layout.py`（约 :261-267）
   - 当前原文（old）：`if ppf <= 0:` 起的 5 行守卫（warning + return）
   - 回退代码（new）：删除该守卫块（直接进入 `if self._pixels_per_frame != ppf:`）
   - 应失败节点：`tests/ui/test_ef03_aud_part2.py::TestAud21204RejectInvalidZoom::test_set_pixels_per_frame_rejects_zero_and_negative`（回退后 ZeroDivisionError/镜像坐标）

## 最终结论

**PASS-with-notes**（Critical 0 / ISSUE 0）

- 39/39 条在当前 HEAD 全部独立核实成立，含 1 条 INVALID（AUD-20605）判定证据充分。
- 非阻塞 notes：
  1. AUD-20405 保留了每帧 glBufferData（改为 STREAM_DRAW + uniform 缓存），属"部分修复但披露如实"，剩余成本可忽略；
  2. 三处行为变化（19511 附件顺序不保证、20502 占位后回填、19403 延迟枚举+降级）均已在报告"行为变化说明"披露，适配的 2 个既有测试核实为最小改动、无断言弱化；
  3. EF-03 提交晚于 EF-14（同为 dev 分支顺序提交，无纪律问题）。
- 建议：允许 `check_fixes.py --mark`（commit 填 bff172ec652f790c9a70c809f0eda6ec3a2031bd；AUD-20605 用 `--note "invalid:已被8307d1f9c修复,补回归"`）。
