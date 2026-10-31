# 剪辑页面时间线面板隐藏BUG审计与AI优化计划

**创建日期**: 2026-06-03  
**审计范围**: 时间线面板核心文件  
**优化目标**: 修复隐藏BUG，提升稳定性和用户体验

---

## 一、审计概述

### 1.1 审计文件清单

| 文件 | 行数 | 职责 |
|------|------|------|
| `src/features/edit/ui/timeline_panel.py` | 1546 | 时间线面板主控件（传输控制、工具栏、JKL快捷键） |
| `src/features/edit/ui/timeline_view.py` | 8205 | 时间线视图核心（可视化、交互、命令执行） |
| `src/features/edit/ui/timeline_layout.py` | 589 | 布局引擎（坐标计算、hit-test） |
| `src/features/edit/ui/timeline_interaction.py` | 220 | 交互状态机 |
| `src/features/edit/core/edit_playback_controller.py` | 1005 | 播放控制器 |
| `src/features/edit/core/timeline_v2.py` | 1445 | 时间线数据模型 |

### 1.2 严重级别定义

| 级别 | 定义 | 修复优先级 |
|------|------|-----------|
| **Critical** | 导致崩溃、数据损坏、内存泄漏 | 立即修复 |
| **High** | 功能异常、UI不同步、用户体验严重受损 | 高优先级 |
| **Medium** | 边缘情况异常、潜在问题 | 中等优先级 |
| **Low** | 代码质量问题、改进建议 | 低优先级 |

---

## 二、发现的隐藏BUG清单

### BUG-01: _clear_items() 不清理 _roll_handles (内存泄漏)

**严重级别**: Critical  
**文件**: `src/features/edit/ui/timeline_view.py`  
**位置**: 行 992-1014

**问题描述**:
`_clear_items()` 方法负责清理时间线场景中的所有图形项，但遗漏了 `_roll_handles` 字典的清理。这导致：
1. 旧的 RollHandle 对象永远留在场景中（幽灵元素）
2. 内存泄漏（每次重新加载时间线都会累积）
3. 可能导致悬空引用和意外行为

**当前代码**:
```python
def _clear_items(self) -> None:
    """Clear all timeline items from scene."""
    # Remove all clip and transition items
    for item in list(self._items_map.values()):
        self._scene.removeItem(item)

    # Remove track headers
    for header in list(self._track_headers.values()):
        self._scene.removeItem(header)

    # Remove gap indicators (Phase 3.3)
    for gap_item in list(self._gap_items.values()):
        self._scene.removeItem(gap_item)

    # Remove marker items (Phase 5.1)
    for marker_item in list(self._marker_items.values()):
        self._scene.removeItem(marker_item)

    # Clear mappings
    self._items_map.clear()
    self._track_headers.clear()
    self._gap_items.clear()
    self._marker_items.clear()
    # 缺少: _roll_handles 清理!
```

**修复步骤**:
1. 在 `_clear_items()` 方法末尾添加 `_roll_handles` 清理逻辑
2. 遍历 `_roll_handles` 并从场景中移除每个 handle
3. 清空 `_roll_handles` 字典

**修复代码**:
```python
def _clear_items(self) -> None:
    """Clear all timeline items from scene."""
    # ... 现有代码 ...

    # Remove roll handles (BUG-01 fix)
    for handle in list(self._roll_handles.values()):
        self._scene.removeItem(handle)
    self._roll_handles.clear()
```

**验证方法**:
1. 打开一个包含多个片段的时间线
2. 执行多次 `load_timeline()` 或切换到其他时间线再返回
3. 使用 `len(self._scene.items())` 检查场景项数量是否稳定
4. 确认没有残留的 RollHandle 对象

---

### BUG-02: _delete_selected_transitions() 绕过 _execute_command()

**严重级别**: Critical  
**文件**: `src/features/edit/ui/timeline_view.py`  
**位置**: 行 4930-4963

**问题描述**:
`_delete_selected_transitions()` 直接调用 `_command_stack.execute()` 而不是 `_execute_command()`，绕过了重要的后处理逻辑：
1. `command_executed` 信号不发射
2. `timeline_changed` 信号不发射
3. 增量更新逻辑被跳过
4. 外部监听器（如 EditPage、MainWindow）无法感知变化

**当前代码**:
```python
def _delete_selected_transitions(self) -> None:
    # ... 
    for trans_id in selected_transition_ids:
        for track in self._timeline.all_tracks:
            for trans in track.transitions:
                if trans.transition_id == trans_id:
                    cmd = RemoveTransitionCommand.create(...)
                    # BUG: 直接使用 _command_stack.execute
                    result = self._command_stack.execute(cmd, self._timeline)
                    # ...
```

**修复步骤**:
1. 重构为使用 `_execute_command()` 方法
2. 由于需要批量删除，使用 `MacroCommand` 封装多个删除命令
3. 或者在循环结束后手动发射 `timeline_changed` 信号

**修复代码**:
```python
def _delete_selected_transitions(self) -> None:
    """Delete all selected transitions."""
    if self._timeline is None:
        return

    selected_transition_ids = list(self._selection.selected_transition_ids)
    if not selected_transition_ids:
        return

    # Collect all remove commands
    sub_commands = []
    for trans_id in selected_transition_ids:
        for track in self._timeline.all_tracks:
            for trans in track.transitions:
                if trans.transition_id == trans_id:
                    cmd = RemoveTransitionCommand.create(
                        self._timeline, trans_id, track.track_id
                    )
                    sub_commands.append(cmd)
                    break

    if not sub_commands:
        return

    # Execute as single atomic MacroCommand via _execute_command
    if len(sub_commands) == 1:
        self._execute_command(sub_commands[0])
    else:
        macro = MacroCommand(
            command_id=f"delete_transitions_{selected_transition_ids[0][:8]}",
            label="Delete transitions",
            sub_commands=tuple(sub_commands),
        )
        self._execute_command(macro)

    # Clear selection
    self.set_selection(TimelineSelection())
```

**验证方法**:
1. 选中一个或多个转场效果
2. 按 Delete 键删除
3. 检查 `timeline_changed` 信号是否发射
4. 确认 EditPage/MainWindow 正确响应变化
5. 验证 Undo/Redo 功能正常

---

### BUG-03: _move_selected_clips() 同样绕过 _execute_command()

**严重级别**: Critical  
**文件**: `src/features/edit/ui/timeline_view.py`  
**位置**: 行 5449-5512

**问题描述**:
与 BUG-02 类似，`_move_selected_clips()` 直接使用 `_command_stack.execute()` 而不是 `_execute_command()`，导致：
1. 信号不发射
2. 增量更新被跳过
3. 调用 `load_timeline()` 进行全量重建（性能问题+闪烁）

**当前代码**:
```python
def _move_selected_clips(self, primary_clip_id, old_start, new_start):
    # ...
    result = self._command_stack.execute(macro, self._timeline)
    if isinstance(result, tuple):
        result = result[0]
    if result is not None:
        self._timeline = result

    # Reload timeline to show all changes (全量重建!)
    self.load_timeline(self._timeline)
```

**修复步骤**:
1. 将 `_command_stack.execute()` + `load_timeline()` 替换为 `_execute_command()`
2. `_execute_command()` 会自动处理增量更新和信号发射

**修复代码**:
```python
def _move_selected_clips(
    self,
    primary_clip_id: str,
    old_start: int,
    new_start: int
) -> None:
    # ... 构建 sub_commands 的代码保持不变 ...

    if not sub_commands:
        return

    # Execute as single atomic MacroCommand via _execute_command (BUG-03 fix)
    macro = MacroCommand(
        command_id=f"multi_move_{primary_clip_id[:8]}",
        label="Move selected clips",
        sub_commands=tuple(sub_commands),
    )
    self._execute_command(macro)  # 自动处理增量更新和信号
```

**验证方法**:
1. 选中多个片段
2. 拖动其中一个进行批量移动
3. 确认没有视觉闪烁
4. 检查 `timeline_changed` 信号是否发射
5. 验证 Undo/Redo 功能正常

---

### BUG-04: FPS 截断导致时间码漂移

**严重级别**: High  
**文件**: `src/features/edit/ui/timeline_panel.py`  
**位置**: 行 780, 808, 1476, 1536

**问题描述**:
`_frame_to_timecode()` 和 `_timecode_to_frame()` 使用 `int(self._fps)` 截断浮点帧率，对于非整数帧率（如 29.97fps、23.976fps）会导致：
1. 时间码计算累积误差
2. 长时间线（>10分钟）时间码显示不准确
3. 时间码输入与帧位置不匹配

**示例**:
- 29.97fps 截断为 29fps
- 1分钟 = 1800帧 (实际应为 1798帧)
- 10分钟误差约 18帧 (0.6秒)

**当前代码**:
```python
def _frame_to_timecode(self, frame: int) -> str:
    fps = int(self._fps) if self._fps > 0 else int(DEFAULT_FPS)  # BUG: 截断
    hours = frame // (fps * 3600)
    # ...
```

**修复步骤**:
1. 将 `int()` 替换为 `round()`
2. 或者使用精确的 Rational 帧率计算
3. 统一 panel 和 playback_controller 的时间码计算逻辑

**修复代码**:
```python
def _frame_to_timecode(self, frame: int) -> str:
    # BUG-04 fix: 使用 round() 代替 int()
    fps = round(self._fps) if self._fps > 0 else round(DEFAULT_FPS)
    
    hours = frame // (fps * 3600)
    minutes = (frame // (fps * 60)) % 60
    seconds = (frame // fps) % 60
    frames = frame % fps
    
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

def _timecode_to_frame(self, timecode: str) -> Optional[int]:
    # ...
    if len(parts) == 4:
        hours, minutes, seconds, frames = [int(p) for p in parts]
        # BUG-04 fix: 使用 round() 代替 int()
        fps = round(self._fps) if self._fps > 0 else round(DEFAULT_FPS)
        return hours * fps * 3600 + minutes * fps * 60 + seconds * fps + frames
    # ...
```

**验证方法**:
1. 设置帧率为 29.97fps
2. 跳转到时间码 00:10:00:00
3. 检查实际帧位置是否正确（应为 17982 帧）
4. 验证时间码输入/输出一致性

---

### BUG-05: comp_clip 媒体类型错误分类

**严重级别**: High  
**文件**: `src/features/edit/ui/timeline_view.py`  
**位置**: 行 3329

**问题描述**:
在 `_on_clip_moved_impl()` 中，`comp_clip`（合成片段）的媒体类型被错误分类为 audio，导致跨轨道拖拽行为异常。

**当前代码**:
```python
# Use item._media_type which is correctly set based on track type
clip_track_type = "video" if item._media_type in ("video", "image") else "audio"
# BUG: comp_clip 不在 ("video", "image") 中，被当作 audio 处理
```

**修复步骤**:
1. 将 `comp_clip` 添加到视频类型判断中

**修复代码**:
```python
# BUG-05 fix: 包含 comp_clip 在视频类型判断中
clip_track_type = "video" if item._media_type in ("video", "image", "comp_clip") else "audio"
```

**验证方法**:
1. 创建一个合成片段 (comp_clip)
2. 尝试将其从视频轨道拖到另一个视频轨道
3. 确认拖拽行为正确（不会被错误地路由到音频轨道）

---

### BUG-06: _on_playhead_changed() 总是暂停播放

**严重级别**: High  
**文件**: `src/features/edit/ui/timeline_panel.py`  
**位置**: 行 1070-1093

**问题描述**:
`_on_playhead_changed()` 在每次播放头位置变化时都调用 `pause()`，这会中断正在进行的播放，导致：
1. 播放中拖动标尺会停止播放
2. 无法实现 "播放中scrub" 的专业编辑功能

**当前代码**:
```python
def _on_playhead_changed(self, frame: int) -> None:
    if self._updating:
        return
    
    self._updating = True
    try:
        # BUG: 总是暂停播放
        self._playback_controller.pause()
        self._playback_controller.seek(frame)
        # ...
```

**修复步骤**:
1. 检测当前是否正在播放
2. 如果正在播放，只执行 seek 而不暂停
3. 或者提供一个参数控制是否暂停

**修复代码**:
```python
def _on_playhead_changed(self, frame: int) -> None:
    """Handle playhead position change from view."""
    if self._updating:
        return
    
    self._updating = True
    try:
        # BUG-06 fix: 只在非播放状态下精确seek
        if not self._playback_controller.is_playing():
            self._playback_controller.seek(frame)
        else:
            # 播放中：只更新位置，不暂停
            self._playback_controller.seek(frame)
        
        # Update timecode display
        self._update_timecode_display()
        
        # Emit seek signal
        self.seek_requested.emit(frame)
        
    finally:
        self._updating = False
```

**验证方法**:
1. 开始播放时间线
2. 在播放过程中拖动标尺
3. 确认播放不会中断
4. 确认 seek 位置正确更新

---

### BUG-07: TimelineChanges NamedTuple 使用可变默认值

**严重级别**: Medium  
**文件**: `src/features/edit/ui/timeline_view.py`  
**位置**: 行 86-110

**问题描述**:
`TimelineChanges` NamedTuple 使用空列表 `[]` 作为默认值。虽然 NamedTuple 是不可变的，但默认值列表本身是可变的，可能导致：
1. 意外的状态共享
2. 如果某处修改了默认列表，会影响所有后续实例

**当前代码**:
```python
class TimelineChanges(NamedTuple):
    added_clips: List[ClipInstance] = []  # 可变默认值
    removed_clip_ids: List[str] = []      # 可变默认值
    # ...
```

**修复步骤**:
1. 将 `List` 替换为 `tuple`（不可变）
2. 或者使用 `dataclass(frozen=True)` 替代 NamedTuple
3. 或者确保所有创建处都显式传入新列表

**修复代码**:
```python
class TimelineChanges(NamedTuple):
    """Timeline changes detected for incremental update."""
    added_clips: tuple[ClipInstance, ...] = ()
    removed_clip_ids: tuple[str, ...] = ()
    modified_clips: tuple[ClipInstance, ...] = ()
    added_transitions: tuple[TransitionInstance, ...] = ()
    removed_transition_ids: tuple[str, ...] = ()
    modified_transitions: tuple[TransitionInstance, ...] = ()
    added_markers: tuple[TimelineMarker, ...] = ()
    removed_marker_ids: tuple[str, ...] = ()
    modified_markers: tuple[TimelineMarker, ...] = ()
    track_changes: dict[str, Any] = None  # dict 是必要的
    full_reload_needed: bool = False
```

**验证方法**:
1. 创建多个 TimelineChanges 实例
2. 确认它们不共享状态
3. 运行现有测试确保无回归

---

### BUG-08: _on_space_pressed() 使用 isVisible() 检查不可靠

**严重级别**: Medium  
**文件**: `src/features/edit/ui/timeline_panel.py`  
**位置**: 行 691-698

**问题描述**:
`_on_space_pressed()` 使用 `self.isVisible()` 检查面板是否可见，但对于停靠面板（QDockWidget），即使面板被其他面板遮挡或不在当前标签页，`isVisible()` 也可能返回 True。

**当前代码**:
```python
def _on_space_pressed(self) -> None:
    """Handle Space key press (play/pause toggle)."""
    if not self.isVisible():  # 不可靠
        return
    # ...
```

**修复步骤**:
1. 使用 `hasFocus()` 或检查焦点是否在面板子控件中
2. 或者使用 `underMouse()` 检查鼠标是否在面板上

**修复代码**:
```python
def _on_space_pressed(self) -> None:
    """Handle Space key press (play/pause toggle)."""
    # BUG-08 fix: 使用更可靠的焦点检查
    if not self._is_panel_active():
        return
    # ...

def _is_panel_active(self) -> bool:
    """Check if panel is active and should receive shortcuts."""
    # 检查面板可见且有焦点（或子控件有焦点）
    if not self.isVisible():
        return False
    # 检查焦点是否在本面板或其子控件中
    focus_widget = QApplication.focusWidget()
    if focus_widget is None:
        return False
    return self.isAncestorOf(focus_widget) or focus_widget == self
```

**验证方法**:
1. 将焦点设置到其他面板（如媒体池）
2. 按空格键
3. 确认不会触发时间线面板的播放/暂停
4. 将焦点设置到时间线面板
5. 按空格键确认正常触发

---

### BUG-09: 轨道高度约束硬编码不一致

**严重级别**: Medium  
**文件**: `src/features/edit/ui/timeline_view.py`  
**位置**: 行 2104 ( `_handle_track_boundary_drag`)

**问题描述**:
`_handle_track_boundary_drag()` 使用硬编码的 `max(20, min(160, ...))` 约束轨道高度，而其他地方可能使用不同的常量，导致行为不一致。

**修复步骤**:
1. 定义统一的轨道高度常量
2. 在所有相关位置使用这些常量

**修复代码**:
```python
# 在文件顶部定义常量
MIN_TRACK_HEIGHT = 24
MAX_TRACK_HEIGHT = 200

# 在 _handle_track_boundary_drag 中使用
def _handle_track_boundary_drag(self, ...):
    # BUG-09 fix: 使用统一常量
    new_height = max(MIN_TRACK_HEIGHT, min(MAX_TRACK_HEIGHT, calculated_height))
```

**验证方法**:
1. 拖动轨道边界到最小/最大值
2. 确认约束行为一致
3. 确认与其他轨道高度预设兼容

---

### BUG-10: _get_asset_duration() 返回 100000 作为后备值

**严重级别**: Medium  
**文件**: `src/features/edit/core/edit_playback_controller.py`  
**位置**: 需要确认具体行号

**问题描述**:
`_get_asset_duration()` 在无法获取素材时长时返回 100000 作为后备值，这个任意值可能导致：
1. 裁剪约束不正确
2. 播放范围计算异常

**修复步骤**:
1. 使用更合理的后备值（如基于时间线时长的估算）
2. 或者返回 None 并在调用处处理

**验证方法**:
1. 使用时长未知的素材
2. 检查裁剪和播放行为是否合理

---

## 三、实施计划

### Phase 1: Critical 级别修复 (立即执行)

| 任务 | BUG编号 | 预计时间 | 验证方式 |
|------|---------|----------|----------|
| 修复 _clear_items() 内存泄漏 | BUG-01 | 15分钟 | 场景项数量测试 |
| 修复 _delete_selected_transitions() 信号问题 | BUG-02 | 30分钟 | 信号发射测试 |
| 修复 _move_selected_clips() 信号问题 | BUG-03 | 20分钟 | 信号发射+增量更新测试 |

### Phase 2: High 级别修复 (高优先级)

| 任务 | BUG编号 | 预计时间 | 验证方式 |
|------|---------|----------|----------|
| 修复 FPS 截断问题 | BUG-04 | 20分钟 | 29.97fps 时间码测试 |
| 修复 comp_clip 媒体类型分类 | BUG-05 | 10分钟 | 跨轨道拖拽测试 |
| 修复播放中scrub中断 | BUG-06 | 25分钟 | 播放中拖动标尺测试 |

### Phase 3: Medium 级别修复 (中等优先级)

| 任务 | BUG编号 | 预计时间 | 验证方式 |
|------|---------|----------|----------|
| 修复 TimelineChanges 可变默认值 | BUG-07 | 30分钟 | 单元测试 |
| 修复空格键检查不可靠 | BUG-08 | 20分钟 | 多面板焦点测试 |
| 修复轨道高度约束不一致 | BUG-09 | 15分钟 | 拖拽边界测试 |
| 修复 _get_asset_duration 后备值 | BUG-10 | 20分钟 | 未知时长素材测试 |

---

## 四、测试验证清单

### 4.1 内存与资源测试

- [ ] 反复加载/卸载时间线，检查场景项数量稳定
- [ ] 检查 `_roll_handles` 在清理后为空
- [ ] 长时间使用后内存占用稳定

### 4.2 信号与同步测试

- [ ] 删除转场后 `timeline_changed` 信号发射
- [ ] 批量移动片段后 `timeline_changed` 信号发射
- [ ] Undo/Redo 后 UI 正确更新
- [ ] EditPage/MainWindow 正确响应时间线变化

### 4.3 时间码测试

- [ ] 24fps 时间码正确
- [ ] 29.97fps 时间码正确（10分钟误差<1帧）
- [ ] 23.976fps 时间码正确
- [ ] 时间码输入/输出往返一致

### 4.4 交互测试

- [ ] comp_clip 跨视频轨道拖拽正确
- [ ] 播放中拖动标尺不中断播放
- [ ] 空格键只在时间线面板激活时响应
- [ ] 轨道高度拖拽约束一致

---

## 五、注意事项

1. **增量更新兼容性**: 修复 BUG-02/BUG-03 时，确保增量更新逻辑能正确处理转场删除和批量移动
2. **信号递归保护**: 修复 BUG-06 时，注意 `_updating` 标志的使用，避免信号递归
3. **向后兼容**: 修复 BUG-07 时，确保所有使用 TimelineChanges 的代码兼容新的 tuple 类型
4. **测试覆盖**: 每个修复都应添加或更新相应的单元测试

---

## 六、附录：代码位置索引

| 符号 | 文件 | 行号 |
|------|------|------|
| `_clear_items` | timeline_view.py | 992 |
| `_delete_selected_transitions` | timeline_view.py | 4930 |
| `_move_selected_clips` | timeline_view.py | 5449 |
| `_execute_command` | timeline_view.py | 2434 |
| `_frame_to_timecode` | timeline_panel.py | 772 |
| `_timecode_to_frame` | timeline_panel.py | 789 |
| `_on_playhead_changed` | timeline_panel.py | 1070 |
| `_on_space_pressed` | timeline_panel.py | 691 |
| `_on_clip_moved_impl` | timeline_view.py | 3280 |
| `TimelineChanges` | timeline_view.py | 86 |
| `_roll_handles` | timeline_view.py | 359 |
