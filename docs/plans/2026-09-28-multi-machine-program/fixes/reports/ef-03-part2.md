# EF-03 part2 修复报告 — src/features/edit/ui Suggestion 级（19 条）

- 批次：EF-03 子运行 2/2
- 分支：dev（未做任何 git add/commit；改动留工作树，与 part1 共存）
- 白名单目录：src/features/edit/ui/（未触碰其他目录文件；REMEDIATION_PLAN.md 未改动）

## 逐条结论

| ID | 结论 | 一句说明 | 文件:行号 |
|----|------|---------|----------|
| AUD-20509 | FIXED | 目录/自动搜索改为每次运行构建一次 filename→path 索引（`_build_directory_index`），`_on_relink_by_directory`/`_on_auto_search` 每个目录只 listdir 一次，`_search_file_in_directory` 委托同一索引保证口径一致 | relink_media_dialog.py:371-437（两处调用点 + `_index_for` 缓存）、:472-505（索引构建）、:507-524（委托） |
| AUD-20604 | FIXED | `_init_frame_provider` 检查 `wait(1000)` 返回值：超时把旧 provider 移入 `_providers_pending_close`，由 worker 的 finished 回调（无运行中 worker 时）安全关闭；新增 `_preview_generation` 代际计数，`_on_preview_frames_ready` 丢弃旧源结果；`set_source` 重置 `_current_index=-1` + `model.set_current_index(-1)` | scene_cut_dialog.py:1494-1502（状态）、:1665-1700（init provider）、:1705-1731（set_source 重置）、:2079-2102（finished 清理）、:2113-2134（代际守卫） |
| AUD-20605 | INVALID（已被修） | 提交 8307d1f9c 已在 `scene_cut_shortcuts.event_matches` 的别名表中把 Backspace 作为 Delete 的无修饰键别名（`"Delete": (Key_Delete, Key_Backspace)`），行为与文档一致；本次仅补回归测试 | scene_cut_shortcuts.py:76-84（证据）；测试见 test_ef03_aud_part2.py::TestAud20605BackspaceReject |
| AUD-20702 | FIXED | MiniScrubber.set_frame、SceneCutGraphWidget.set_threshold/set_current_frame/_on_graph_frame_clicked/_on_scrubber_frame_changed、ThresholdControls.set_threshold 共 6 处 `_updating` 全部 try/finally 包裹 | scene_cut_graph_widget.py:772-779、:914-920、:932-940、:978-984、:994-1000、:1118-1126 |
| AUD-20714 | FIXED | `request_thumbnail` 完成时（含 loader 抛异常）在 finally 中删除 `_pending_requests` 条目；docstring 明确 loader_func 为同步语义 | scene_cut_preview_widget.py:849-889 |
| AUD-20717 | FIXED | set_candidate/set_frame_preview/clear 的 `_updating` 全部 try/finally 包裹（set_candidate 分支体一并重排缩进进 try 块） | scene_cut_preview_widget.py:591-635、:680-705、:709-716 |
| AUD-20803 | FIXED | 新增 `_apply_decision`（只改数据 + dataChanged，不发 decision_changed）与 `_run_batch`（循环期间整体持 `_updating`，所有 decision_changed 延迟到循环结束后统一发）；5 个批量方法（accept_high_confidence/reject_warnings/accept_all/reject_all/reset_all）走同一管线；set_decision 重构复用 `_apply_decision`，锁外发信号 | scene_cut_result_model.py:374-441（set_decision/_apply_decision）、:481-524（_run_batch）、:525-600（5 个批量方法） |
| AUD-20807 | FIXED | 实现 `sort()`：支持 Frame/Score/Scene 列（升/降序），layoutAboutToBeChanged/layoutChanged 通知，`_current_index` 跟随原候选对象重映射；其余列 no-op | scene_cut_result_model.py:602-648 |
| AUD-20815 | FIXED | show_overlay/hide_overlay 与 set_candidates/select_candidate 统一套用 `_updating` 锁（锁内更新状态与 `_update_visibility`，锁外发 overlay_visibility_changed），回环窗口被守卫 | scene_cut_timeline_overlay.py:832-863 |
| AUD-20904 | FIXED | initializeGL 链接后缓存 `_u_texture_loc/_u_display_rect_loc/_u_flip_y_loc`；顶点着色器新增 `u_display_rect`/`u_flip_y` uniform，VBO 固定为静态单位 quad，`_render_texture` 不再每帧 glGetUniformLocation / glBufferData（无每帧重传），texcoord 翻转经 uniform 表达（GPU 共享纹理翻转、CPU QImage 不翻转，与旧逐顶点 UV 逐位等价） | source_viewer.py:532-536（缓存属性）、:564-596（着色器 + 缓存）、:797-851（_render_texture） |
| AUD-21003 | FIXED | keyPressEvent 的 Delete/Backspace 在 `_is_track_locked` 时直接 accept 不发信号，与鼠标/右键菜单路径一致 | subtitle_clip_item.py:534-546 |
| AUD-21110 | FIXED | 禁用/锁定条纹图案改在 painter.save()/restore() 内以 opacity 1.0 绘制，restore 后 0.5 透明度贯穿波形与名称条 | timeline_items.py:1667-1677 |
| AUD-21203 | FIXED | resolve_timeline_hit 对齐 is_in_splitter：补查 `subtitle_splitter_rect`（命中返回 area="splitter"），docstring 修正为实际优先级（ruler 区 → V/A splitter → S/V splitter → clip/track/header → empty） | timeline_layout.py:792-818 |
| AUD-21204 | FIXED | set_pixels_per_frame/configure 拒绝 ppf ≤ 0（logger.warning + 保留旧值），x_to_frame 不再有除零/镜像坐标风险 | timeline_layout.py:257-267、:232-243 |
| AUD-21304 | FIXED | `_refresh_zoom_slider_bounds`/`_on_view_zoom_changed`/`set_zoom_level` 三处 `_zoom_updating` 全部 try/finally（与既有 `_reset_empty_timeline_zoom` 模式一致） | timeline_panel.py:1660-1668、:1737-1743、:2468-2474 |
| AUD-21306 | FIXED | `_collect_proxy_hud_stats` 结果按素材集合 hash + 10s TTL 缓存（`_PROXY_HUD_CACHE_TTL_S`），HUD 500ms 轮询在集合未变时不再重复触发最多 200 次加锁磁盘校验；素材变化或 TTL 过期自动重算 | timeline_panel.py:55-58（常量）、:234-239（缓存属性）、:2163-2205（实现） |
| AUD-21408 | FIXED | 锁轨数值 trim 反馈改发 `status_message_requested` 信号（I18N.tr），删除 `getattr(self,'status_bar')` 死路径 | timeline_view.py:4033-4039 |
| AUD-21410 | FIXED | `_layout_track_items` 将 `all_ordered_tracks()` 提升到循环外一次并预建 `ordered_index` 字典（主循环 + 字幕循环复用，O(n²)→O(n)）；三个分区滚动条 handler 改走 `_schedule_scroll_layout_update()` 单发定时器（16ms）合并为每帧一次全量重排 | timeline_view.py:655-661（定时器）、:1194-1200、:1323-1326（提升）、:1526-1561（合并） |
| AUD-21412 | FIXED | `_handle_external_file_drop` 对不在项目中的新文件先经有界 ThreadPoolExecutor（≤4 线程，`drop-probe`）并行探测元数据，多文件阻塞从 N×probe 降为 ~(N/4)×probe；探测失败回退原同步路径保持逐文件错误处理；drop 保持同步（QDropEvent 仅在本 handler 存活，无法安全延后） | timeline_view.py:13911-13973 |

统计：FIXED 18 / INVALID 1（AUD-20605，已被提交 8307d1f9c 修复）/ DUPLICATE 0 / BLOCKED 0

## 测试

新建 `tests/ui/test_ef03_aud_part2.py`：70 个用例，覆盖全部 19 条（含 INVALID 条目的行为回归）。行为类测试直接驱动组件（qapp fixture，无模态对话框——QMessageBox 打桩；无真实 GL 上下文——GL 修复用 AST 结构断言）；Widget 构造过重的类（SceneCutDialog/TimelinePanel/TimelineView）用 `Cls.__new__` / 轻量子类注入最小协作对象。

### 验证关卡输出摘要

1. `py_compile` 全部 12 个改动源文件 + 测试文件 → `PY_COMPILE_OK`
2. `.venv/bin/python -m pytest tests/ui/test_ef03_aud_part2.py -q -p no:cacheprovider` → **70 passed in 2.58s**
3. 定向既有测试（均无新增失败）：
   - tests/edit/test_timeline_hit_and_drop.py → 24 passed
   - tests/edit/test_timeline_view_layout.py → 70 passed
   - tests/edit/test_timeline_external_file_drop.py → 7 passed（AUD-21412 直接相关）
   - tests/edit/test_scene_cut_result_model.py → 56 passed（AUD-20803/20807 直接相关）
   - tests/edit/test_scene_cut_graph_widget.py → 3 passed
   - tests/edit/test_timeline_panel_zoom_coalescing.py → 7 passed（AUD-21304 直接相关）
   - tests/edit/test_fix_wd13g67_aud.py + tests/edit/test_fix_ui2_aud3.py（同模块历史审计修复回归）→ 36 passed
   - tests/edit/test_scene_cut_session_memory.py + tests/edit/test_clip_item_trim_preview.py → 10 passed
4. 已知预存失败类别（GPU 位精确、x265 10-bit、VT 探针）未涉及，未触碰。

### 回退敏感性验证（抽 2 条）

- AUD-21304（timeline_panel `_on_view_zoom_changed` 还原为无 try/finally）→ `TestAud21304ZoomLock::test_on_view_zoom_changed_releases_lock_on_exception` **FAILED**（`assert True is False`）→ 还原修复后复跑全绿。
- AUD-21204（删除 `set_pixels_per_frame` 的 ppf≤0 守卫）→ `TestAud21204RejectInvalidZoom::test_set_pixels_per_frame_rejects_zero_and_negative` **FAILED**（ZeroDivisionError）→ 还原修复后复跑全绿。

## 改动文件清单

源（均在白名单内）：
- src/features/edit/ui/relink_media_dialog.py
- src/features/edit/ui/scene_cut_dialog.py
- src/features/edit/ui/scene_cut_graph_widget.py
- src/features/edit/ui/scene_cut_preview_widget.py
- src/features/edit/ui/scene_cut_result_model.py
- src/features/edit/ui/scene_cut_timeline_overlay.py
- src/features/edit/ui/source_viewer.py
- src/features/edit/ui/subtitle_clip_item.py
- src/features/edit/ui/timeline_items.py
- src/features/edit/ui/timeline_layout.py
- src/features/edit/ui/timeline_panel.py
- src/features/edit/ui/timeline_view.py

测试（新建）：
- tests/ui/test_ef03_aud_part2.py

备注：
- AUD-20604 的 `_on_preview_worker_finished` 延迟关闭带"无运行中 worker 才关闭"守卫，防止 closeEvent 场景下僵尸 worker 仍持有 provider 解码锁时阻塞 UI 线程（与 .clinerules 的 GPU/IO 释放约束一致）。
- AUD-21412 评估过"复用媒体池异步导入管线"方案：dropEvent 之后 QDropEvent 失效且后续落点逻辑（scene_pos/accept/ignore）强依赖事件对象，完整异步化属大重构，超出 Suggestion 级最小改动，故采用有界并行探测方案并已在报告中注明边界。
