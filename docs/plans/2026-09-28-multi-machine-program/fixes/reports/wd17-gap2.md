# WD17-GAP2 修复报告 — 补测 GAP-007 / GAP-027（批 D 最后覆盖缺口·文件/主窗侧）

- 代理批次：WD17-GAP2
- 分支：`dev`（未做任何 git 分支切换/commit/push 操作）
- 日期：2026-10-01
- 计划复选框：未改动（由主会话负责标记）
- 源文件改动：**零**。两个目标源文件（`src/app/controllers/file_controller.py`、
  `src/app/main_window.py`）均未改一行，也未做任何"可测性提纯"类侵入；
  全部用例以 mixin 未绑定调用（host 替身）+ `MainWindow(test_mode=True)` +
  monkeypatch 实现。
- 新增测试：`tests/app/test_gap007_file_controller.py`（68 用例）、
  `tests/app/test_gap027_main_window.py`（35 用例）

## 状态总表

| GAP | 模块 | 基线覆盖（GAP_REPORT） | 状态 | 新增用例 |
|-----|------|----------------------|------|---------|
| GAP-007 | `src/app/controllers/file_controller.py`（22%，缺 3070 行） | 22% | **FIXED**（补测完成：计划点名的工程打开/保存/恢复错误路径、关闭序列、菜单接线全部落网） | 68 |
| GAP-027 | `src/app/main_window.py`（19%，缺 942 行） | 19% | **FIXED**（补测完成：页面上下文、标题持久化标签、快捷键接线、启动诊断、引擎错误上报；closeEvent 停机序列已由 wd2-g9 覆盖，本批补其余高风险面） | 35 |

运行结果：

```
.venv/bin/python -m pytest tests/app/test_gap007_file_controller.py \
  tests/app/test_gap027_main_window.py -q
→ 103 passed
```

回归：既有的 file_controller/main_window 相关套件
（test_project_save_routing / test_fix_wd2g9_aud / test_window_geometry_clamp /
test_page_controller_coverage / test_dock_layout_controller_coverage /
test_project_manager_startup）262 全绿；`tests/app` 全目录 1378 passed，
仅 `test_gap002_edit_controller.py`（另一并行代理的未完成文件，`MediaAsset`
构造签名不匹配，与本批无关，单跑亦失败）有 24-27 例失败。

覆盖率（pytest-cov，可选数据）：

- file_controller.py：相关既有套件基线 15% → 加入本批后 **21%**
  （缺失行 3398 → 3154，本批新覆盖约 244 语句）。
- main_window.py：本批文件单独运行即达 **38%**（455/1201 语句，基线 19%）
  ；tests/app 全目录合并为 41%。

## GAP-007 — file_controller.py：覆盖的高风险路径清单

### 工程打开/恢复（真实 .topos zip，tmp_path 构造）

- `_read_project_mode_from_manifest`：正常读取、旧名 `single_asset` →
  `standalone_image` 重命名映射、键缺失默认、损坏档案返回 None、
  manifest 缺失返回 None（5 例）
- `_check_project_matches_work_mode`：模式匹配放行（无弹窗）、
  video 工程 × image 模式拒绝 + 切换模式提示框、manifest 不可读时放行
  交由加载路径报错（3 例）
- `_open_project_path`：文件不存在拒绝；已有工程时 close_project 被取消
  → 中止；standalone manifest 路由到图像加载器且不强制切页；
  timeline 工程加载后从 Color 切到 Edit 页 + MG preset 根同步；
  加载失败（工程引用未替换）如实返回 False（5 例）
- `_load_standalone_project`：IO 异常 → critical 框且状态不被半途污染；
  结果 None → critical；内容含时间线/无图像素材 → 拒绝（保护现有状态）；
  成功水合（工程/路径/序列、save_router 路径、脏标记清零、按工程路径重开
  图像、最近工程 + last_project_path + 窗口标题）（5 例）
- `_load_edit_project`：edit 模块禁用防御退出；工作模式不符中止；
  **autosave 恢复分支**（Yes → 从恢复文件加载但活跃路径保持原文件；
  No → discard_autosave 防每次启动重复弹窗）；结果 None → critical；
  成功路径（活跃时间线序列回填 + playhead/zoom UI 状态恢复）；无序列时
  回退临时 V1/A1 时间线（6 例）
- `_report_content_migrations`：迁移条目上状态栏；空条目静默（P2-13）（1 例）

### 关闭序列

- `_has_unsaved_changes` 四态矩阵（无工程/未命名/脏/干净）（1 例）
- `_confirm_save_before_close`：无变更直接放行；Save 成功放行；
  **保存抛异常 → 中止关闭**（不静默丢弃）；保存后仍未保存（用户取消
  另存为）→ 中止；Discard → `_user_discarded_changes` 置位（AUD-2800）；
  Cancel → 中止（6 例）
- `_persist_active_project_before_close`：Discard 后跳过主工程回写；
  save 失败仅记日志不阻断停机；合成 flush 失败仍继续保存（AUD-2800）（3 例）
- `_clear_all_dirty_states` 四标志复位（P0-5）（1 例）
- `_stop_gpu_renderer_deferred`：超时 → 推迟销毁入 `_LEAKED_GPU_OBJECTS`
  （P1-THREAD-01）；正常停机不入表；stop 抛异常跳过；旧式无参 stop 的
  TypeError 回退（4 例，模块级列表用 fixture 快照/裁剪隔离）

### 工程生命周期与媒体分发

- `new_project`：关闭被取消中止；video 模式建时间线文档；image 模式重置
  调色状态（3 例）
- `close_project`：MG 编辑器激活时关编辑器不关工程（macOS 惯例）；
  无工程媒体会话清理（不保存卸载、状态清空、MG 根脱离、标题复位）；
  全空 noop（3 例）
- `_open_media_path`：Color 页视频拒绝；Edit 页加载视频；图像邻接
  .topos 工程直接打开；无工程回退 load_image（4 例）
- `dragEnterEvent` / `dropEvent`：URL 拖入接受/无 URL 忽略；拖放统一走
  Open Media 路由（AUD-2801 回归锁）；空拖放 noop（3 例）
- `_get_work_mode`：flag 读取异常回退 "all"（1 例）

### 菜单/最近文件接线

- `_add_to_recent_files`：前置/去重/上限截断 + 菜单重建触发；空路径忽略（2 例）
- `_open_recent_file`：存在路径打开；缺失路径忽略；无 sender noop（3 例）

### 元数据异步加载（R0-7）与杂项

- `_MetadataLoader`：缓存命中不再派发 worker（get_metadata 仅一次）；
  in-flight 同路径去重（线程池 start 打桩）；worker 异常回退空 metadata
  且不外泄（3 例）
- `_on_metadata_ready`：只刷新当前文件（1 例）
- `_absolutize_composite_paths`：存在文件的相对路径改写；缺失/绝对路径
  保留；空输入 noop（3 例）
- `_sync_mg_preset_project_root`：委托 + None 脱离（teaching_animation
  存在时）（1 例）

## GAP-027 — main_window.py：覆盖的高风险路径清单

### PageContext / PageContextManager（P9 页面切换上下文，纯逻辑）

- `PageContext.to_dict/from_dict` 全字段往返；缺失键默认值（2 例）
- 当前/每页最后上下文追踪；页面 dirty 状态追踪与 any_page_dirty（2 例）
- 三个上下文工厂：edit→color（clip/帧号/调色栈透传）、edit→composite
  （deliver_source=composite）、→deliver（color/timeline 来源页路由）（3 例）

### 窗口状态（标题工作模式标签）

- `_update_title_work_mode`：studio 追加 Image/Video Mode；模式切换剥离
  旧标签不叠加；已含标签幂等；非 studio 产品跳过；flag 异常容忍不抛（5 例）

### 启动音频后端诊断（AM-01）

- `_run_audio_backend_diagnostics`：降级 → 状态栏永久告警 widget +
  快照存 `_audio_capabilities`（供 EQ/LUFS 读取）；健康静默；探测失败
  不阻断启动（3 例）

### eventFilter 快捷键接线（菜单/动作侧）

- Space：Color 页已加载视频 → 触发走带并吃掉事件；无视频不吃；
  **文本控件聚焦时抑制**（输入框 Space 不被劫持）；非走带页不吃（4 例）
- Composite 逐帧键：→ 键驱动 comp clip 下一帧 + 控制栏帧号回写；
  Image Color 模式禁用；非按键事件透传（3 例）

### Space 首拒链 handler 守卫分支

- `_handle_motion_graphic_space_playback`：可见 "MotionGraphicEditor"
  Tool 窗口首拒（隐藏后让键）（1 例）
- `_handle_color_space_playback_shortcut`：is_video_loaded 抛异常不崩（1 例）
- `_handle_composite_space_playback_shortcut`：timeline-bound 走带
  play/pause 双向；无 controller 降级拒绝；Image Color 模式拒绝（3 例）
- `_focus_is_within`：None/非 widget 容器守卫；真实 widget 树判定（2 例）
- `_raise_edit_viewer_dock`：C++ 对象已析构（sip.delete）与属性缺失均安全（1 例）

### 引擎错误上报

- `on_engine_error`：进度条隐藏复位 + 状态栏消息 + critical 框（1 例）

### 脚本 API 只读时间线上下文（P1-COLOR-24）

- `_script_timeline_context`：无时间线/工程时空返回；list_clips 排序 +
  has_grade 标记（带曲线数据的栈判为已调色）；clip_grade 快照字段；
  缺 clip/缺栈/未知 what 返回 None（4 例）

## 未覆盖路径及原因

- **真机 GPU/GL 路径**（`_init_gpu_pipeline`、`_init_source_gpu_pipeline`、
  color monitor PBO readback 系列、`_init_timeline_preview_renderer`）：
  需要真实 GL 上下文与渲染线程，按约束跳过（已有 gpu_renderer 标记的
  专门套件覆盖部分）。
- **完整 create_panels / setup_connections / setup_docks 面板树**（两文件
  合计约 700+ 行）：实例化全部面板需要真机 GL/WebEngine/音频栈；
  `MainWindow(test_mode=True)` 分支外的路径以既有 tests/visual 套件覆盖。
- **视频播放/scrub 循环与色页时间线 clip 播放同步**（file_controller
  L4700-6800 的大块）：依赖 EnhancedVideoPlaybackEngine 真实解码与信号
  循环，属 tests/color、tests/media 域；本批只覆盖其中的路由/拒绝分支。
- **Quick Project 创建流程**（`_on_quick_project_requested` /
  `_create_and_open_quick_project`）：对话框 exec + 延迟 UI 装配 +
  `EditProject.create_quick_project` 深链，已有独立套件
  （test_project_manager_startup 等）覆盖入口。
- **真实文件系统权限错误**（只读目录写配置、exiftool 子进程超时）：
  exiftool 依赖真实子进程，get_metadata 已打桩；权限错误路径以
  OSError 模拟覆盖（保存失败、IO 异常）。
- **网络更新检查**（`UpdateChecker.start` 真实请求）：关闭路径的
  quit/wait 已由 wd2-g9 覆盖；真实网络不在本批范围。
- closeEvent 完整停机序列与 `_shutdown_playback_pipeline` 幂等：已由
  `tests/app/test_fix_wd2g9_aud.py`（AUD-3500~3503）覆盖，本批不重复。

## 测试基建说明（无源码侵入的实现手段）

- mixin 方法以 `FileOpsMixin.method(host, ...)` 未绑定调用，host 为
  SimpleNamespace 逻辑替身（沿既有 test_project_save_routing.py 惯例）；
  host 内对混入互调方法（`_clear_all_dirty_states` 等）委托回真实实现。
- `QMessageBox` 经 monkeypatch 换成继承真实类的记录替身（保留
  `StandardButton` 供被测代码比较），question 返回值可配置。
- MainWindow 用模块级 `MainWindow(test_mode=True)` fixture（不建面板、
  不碰 GL），teardown 走真实 close 停引擎线程再 deleteLater（防
  QThread 析构 abort，同 wd2-g9 基座）；逐用例清理实例级替身恢复类级解析。
- `.topos` 工程包用 zipfile 真实构造于 tmp_path；文件存在性/路径判断
  均走真实 os.path。
