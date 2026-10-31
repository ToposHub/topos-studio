# EF-03 修复报告 — 子运行 1/2（src/features/edit/ui，20 条 Suggestion）

- 分支：`dev`（未做任何 git add/commit/branch/push）
- 白名单：仅 `src/features/edit/ui/`；测试文件 `tests/ui/test_ef03_aud_part1.py`
- 结论：**20/20 FIXED**（0 INVALID / 0 DUPLICATE / 0 BLOCKED）

## 逐条结论

| ID | 结论 | 修复说明 | 位置（文件:行） |
|----|------|----------|------------------|
| AUD-19403 | FIXED | 设备枚举（`QMediaDevices.audioInputs()` 同步系统调用）改为 `QTimer.singleShot(0, …)` 延迟到事件循环补扫，构造期零阻塞；`reload_devices` 加防御（AttributeError/RuntimeError/OSError → 日志 + 降级 System Default），因延迟执行环境相对构造期可能变化 | adr_take_sheet_panel.py:401, 461-486 |
| AUD-19407 | FIXED | 菜单打开不再无条件同步重扫插件目录：新增 `_plugins_directory_signature()`（目录 mtime + 各子目录/plugin.json mtime 的轻量签名，无 JSON 解析）与 `_refresh_plugins_if_changed()` 门控；签名未变 → 复用现有 registry，变化/不可读 → 全量重扫 | ai_assistant_composer.py:663-710, 340-343 |
| AUD-19412 | FIXED | 锁定/拒绝按钮按 decision 状态门控（locked → 锁定禁用；rejected → 拒绝禁用；替换在所有状态保留） | ai_decision_inspector.py:68-81 |
| AUD-19511 | FIXED | 图片附件解码与 JPEG 编码移入 QThreadPool worker（`_ImageAttachmentWorker` + 队列信号回 UI）；`add_image_attachment` 只做 null 校验即受理；`image_files` 菜单与 `_add_file_attachment` 图片分支的 `QImage(path)` 整图解码移入 worker；批次 0 成功仍提示 | ai_edit_assistant_panel.py:83-122, 506-512, 2586-2647, 2824-2838, 2898-2905 |
| AUD-19513 | FIXED | 无动画目标（无 Run Drawer 或 drawer 无 `set_live_status`）时不再启动 90ms shimmer 定时器；`_animate_activity_status` 目标消失时自愈停表 | ai_edit_assistant_panel.py:1759-1790, 1830-1837 |
| AUD-19620 | FIXED | 模块 docstring 与置顶行为对齐：明确置顶构成 = removed 条目 + 预检 warnings（音乐版权/降级 effect，AUD-19619 起已高风险置顶）；“替换”在 diff 中表现为 removed+added 对（其 removed 半边随 removed 置顶），不再声称存在独立分类 | ai_timeline_diff.py:3-8, 164-168 |
| AUD-19700 | FIXED | Find 命中后显式补发 `cue_selected`（`select_clip` 的 `_building_ui` 锁保留 —— 控制器驱动的反向同步仍被抑制，不产生回环） | caption_workspace.py:503-513 |
| AUD-19730 | FIXED | 重写模块 docstring：TransformParams 已是像素空间模型，无归一化↔像素桥接（`_params_dict` 为直接字段拷贝）；清理残留的 "normalized↔pixel bridge" 注释 | clip_transform_handle_controller.py:1-19, 90-91, 134 |
| AUD-19804 | FIXED | 新增 `_updating` 状态锁：`clear()` 与 `_toggle_search_visibility` 的程序化 `search_input.clear()` 持锁 + 停 debounce 定时器，`_on_search_changed` 锁内直接返回 → 消除双重全量重建 | edit_effects_panel.py:651-654, 1349-1360, 1990-1997, 2183-2200 |
| AUD-19807 | FIXED | `CaptionStylePreset` 随 `BUILTIN_PRESETS` 一并局部导入，未导入名字的注解可解析 | edit_effects_panel.py:1896-1903 |
| AUD-20011 | FIXED | 两组 effect 参数 builder 的全部回调（`_slider_changed`/`_drag_changed`/`_eff_slider_changed`/`_eff_drag_changed` + bool/option/choice lambda）加 `self._updating` 防递归守卫（8 处） | edit_properties_panel.py:4373-4402, 4430-4434, 4453-4460, 4533-4556, 4579-4608 |
| AUD-20209 | FIXED | `_on_thumbnail_ready` 改用懒重建的 path→items 索引（`_rebuild_tree_item_index`，树重建/清空点置脏），每张缩略图 O(1) 命中；整体 O(n·P) → O(n+P) | media_pool_panel.py:4792-4795, 5428-5429, 6181-6206, 6208-6226, 7090-7091 |
| AUD-20210 | FIXED | 探测提取为模块级 `_probe_proxy_resolution`（顺带消除裸 `except: pass` → debug 日志），新增 `ProxyResolutionWorker`/`ProxyResolutionSignals`；`_update_disk_proxy_services` 只做 stat 即派发 worker，服务回填移到 UI 线程队列回调 `_on_proxy_resolution_probed`/`_apply_proxy_ready` | media_pool_panel.py:389-438, 443-483, 4797-4801, 10570-10630, 10664-10684 |
| AUD-20213 | FIXED | 删除永远空转的列表视图高亮函数及其 2 处调用（查找的 "timelines" 顶级节点已不存在）；grid 高亮路径保留 | media_pool_panel.py:5588-5601 |
| AUD-20301 | FIXED | `_pending_by_key` 值改为 (asset_id, 发起时间戳)；每批请求前 `_prune_stale_pending()` 清理超时（5s）未应答请求防缓慢泄漏；迟到结果与 tile 当前 `request_key` 不匹配时丢弃，不再回显过期帧 | multicam_panel.py:42-44, 157-159, 322-382 |
| AUD-20316 | FIXED | 反向播放（delta<0）时实测 fps 用 `abs(delta)/dt` 更新，不再冻结旧值；正向/停止语义不变 | playback_hud.py:117-124 |
| AUD-20405 | FIXED | 每帧重建的 quad VBO 改 `GL_STREAM_DRAW`（原 STATIC_DRAW 与每帧上传语义相悖）；`u_texture` UniformLocation 缓存（`_tex_uniform_location()`，program 重建时置 None），不再每帧 `glGetUniformLocation` | program_viewer.py:153, 851-853, 925-926, 1387-1396, 1438-1454 |
| AUD-20406 | FIXED | `_rgb888_qimage_to_rgba` 预分配 RGBA 目标：RGB 一次跨距拷贝入位 + alpha 广播填充，取代 concatenate（+ ascontiguousarray）的 1-2 份全帧临时数组 | program_viewer.py:110-137 |
| AUD-20411 | FIXED | `render_plan.from_clip` 与 `caption_graphic_animation_state` 的 import 提升到 `_paint_subtitle_overlay` 帧级（循环体外）；graphics 导入失败仅禁用动画，不影响字幕本体绘制 | program_viewer.py:2944-2962, 2991-2993, 3031 |
| AUD-20502 | FIXED | 构造期只同步生成占位元数据（扩展名判定，零 IO）；真实探测经 `_MetadataProbeWorker`（QThreadPool）后台执行，完成后队列信号回填媒体信息标签 + Auto 分辨率；对话框先显示占位再回填 | quick_project_dialog.py:44-71, 167-204, 291-336, 373-406 |

## 行为变化说明（需评审知悉）

1. **AUD-19511**：附件插入顺序不再保证（后台编码完成顺序）；同一消息的附件集合语义不变。两条既有集成测试已按新语义最小适配（等待任务 settle + 附件种类按多重集断言）：`tests/ui/test_ai_assistant_panel_p0_integration.py`。
2. **AUD-20502**：对话框弹出速度变快，媒体信息区在探测完成前显示占位值（分辨率 1920x1080/fps 24.0/时长 "—"），完成后自动回填；探测完成前点击“创建”时 Auto fps 兜底 24.0（与旧行为的未缓存路径一致）。
3. **AUD-19403**：延迟补扫若遇枚举环境异常（如测试替身替换核心类）降级为 System Default 并记 warning 日志。`tests/edit/test_adr_take_sheet_ui.py` 的 `_FakeManager` 补齐了与真实类一致的 `list_input_devices` 接口。

## 验证输出摘要

1. **py_compile**（14 个源文件 + 3 个测试文件）：`FINAL_COMPILE_OK`
2. **新测试**：`.venv/bin/python -m pytest tests/ui/test_ef03_aud_part1.py -q -p no:cacheprovider`
   → **51 passed**（每条修复均有正例 + 反例回退护栏）
3. **定向既有测试**（无新增失败）：
   - `tests/ui/test_ai_timeline_diff.py + test_ai_assistant_composer.py + tests/edit/test_playback_hud.py + test_caption_workspace.py` → 91 passed
   - `tests/ui/test_ai_assistant_panel_p0_integration.py + tests/edit/test_adr_take_sheet_ui.py + tests/app/test_gap010_ai_edit_controller.py` → 93 passed
   - `tests/edit/test_multicam_panel_grid.py + test_clip_transform_handle_controller.py + test_quick_project.py` → 22 passed, 5 skipped（skipped 为预存）
   - `tests/edit/test_program_viewer_transform_contract.py + test_program_viewer_toolbar.py + test_transition_preview_mode.py + test_edit_properties_panel_layout.py` → 61 passed
   - `tests/edit/test_media_pool.py + test_media_pool_panel_refresh.py` → 51 passed
   - `tests/ui/test_ai_edit_assistant_m5_ui.py + test_p1_result_action_acceptance.py + test_p7_plugin_status_panel.py` → 32 passed
   - `tests/edit/test_multicam.py` → 18 passed
4. **回退敏感性抽查**（2 条，临时还原修复 → 对应测试 FAILED → 还原修复）：
   - AUD-20316：还原 `delta >= 0` 旧条件 → `TestAud20316ReversePlaybackFps::test_reverse_playback_updates_fps` **FAILED**（1 failed, 1 passed）→ 还原修复后全绿。
   - AUD-19412：移除按钮 setEnabled 门控 → `TestAud19412DecisionButtonGating` 2 个测试 **FAILED** → 还原修复后全绿。
5. 未整目录跑 `tests/edit`（4K 挂起测试）；未出现 GPU 位精确 / x265 10-bit / VT 探针类预存失败干扰。

## 改动文件清单

源码（均在白名单 `src/features/edit/ui/`）：

- src/features/edit/ui/adr_take_sheet_panel.py
- src/features/edit/ui/ai_assistant_composer.py
- src/features/edit/ui/ai_decision_inspector.py
- src/features/edit/ui/ai_edit_assistant_panel.py
- src/features/edit/ui/ai_timeline_diff.py
- src/features/edit/ui/caption_workspace.py
- src/features/edit/ui/clip_transform_handle_controller.py
- src/features/edit/ui/edit_effects_panel.py
- src/features/edit/ui/edit_properties_panel.py
- src/features/edit/ui/media_pool_panel.py
- src/features/edit/ui/multicam_panel.py
- src/features/edit/ui/playback_hud.py
- src/features/edit/ui/program_viewer.py
- src/features/edit/ui/quick_project_dialog.py

测试：

- tests/ui/test_ef03_aud_part1.py（新建，51 个用例）
- tests/ui/test_ai_assistant_panel_p0_integration.py（AUD-19511 异步语义的最小适配：等待 settle + 顺序无关断言）
- tests/edit/test_adr_take_sheet_ui.py（_FakeManager 补齐 list_input_devices 接口）
