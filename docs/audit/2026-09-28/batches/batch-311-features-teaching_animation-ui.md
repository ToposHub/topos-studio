# batch-311 — src/features/teaching_animation/ui/

编号区间：AUD-31100–AUD-31199。审查日期：2026-09-28。

## src/features/teaching_animation/ui/storyboard_panel.py (行数: 86)

### 发现
- [AUD-31101] [Suggestion] set_scenes() 的选中 id 在新列表中不存在时静默丢失选中，且不回退到第 0 行
  - 位置: src/features/teaching_animation/ui/storyboard_panel.py:49-52, 59-63
  - 证据:
    ```python
        if selected:
            self.select_scene(selected)
        elif self.list_widget.count():
            self.list_widget.setCurrentRow(0)
    ...
        def select_scene(self, scene_id: str) -> None:
            for index in range(self.list_widget.count()):
                if self.list_widget.item(index).data(256) == scene_id:
                    self.list_widget.setCurrentRow(index)
                    return
    ```
  - 影响: `selected` 非空但已不在列表中时（外部删除场景后刷新、id 过期），select_scene 遍历无命中直接 return：列表有项却无 currentItem、不发 sceneSelected，`current_scene_id` 变为 None，面板与编辑器选中态脱节；且后续 _duplicate_current/_delete_current 因取不到 id 而静默失效。elif 的 row-0 回退分支在此场景下被跳过。
  - 建议: select_scene 返回 bool（或 set_scenes 内先检查 id 是否存在），未命中时回退 setCurrentRow(0)，与"selected 为空"分支行为对齐。
  - 置信度: 高

- [AUD-31102] [Suggestion] ItemDataRole 使用魔法数字 256
  - 位置: src/features/teaching_animation/ui/storyboard_panel.py:46, 57, 61
  - 证据:
    ```python
            item.setData(256, scene.scene_id)
    ...
        return str(item.data(256)) if item is not None else None
    ...
                if self.list_widget.item(index).data(256) == scene_id:
    ```
  - 影响: 256 即 Qt.ItemDataRole.UserRole；魔法数字降低可读性，且同值若被其他 setData 调用占用会发生静默数据覆盖（当前文件无冲突，风险为可维护性层面）。
  - 建议: 定义模块级常量或直接使用 `Qt.ItemDataRole.UserRole`。
  - 置信度: 高

- [AUD-31103] [Suggestion] _rows_moved 依赖"拖拽后 currentItem 即被拖项"这一 Qt 隐式行为，无当前项时重排事件静默丢失
  - 位置: src/features/teaching_animation/ui/storyboard_panel.py:77-86
  - 证据:
    ```python
        current = self.list_widget.currentItem()
        current_row = self.list_widget.currentRow()
        if current is not None and current_row >= 0:
            self.reorderRequested.emit(str(current.data(256)), current_row)
    ```
  - 影响: 行已在 QListWidget 中完成移动（rowsMoved 触发时视图状态已变更），但若 currentItem 为空（例如列表重建后未选中），reorderRequested 不发射：UI 显示的顺序与宿主 self._scene_ids 顺序永久不一致，且无任何日志。注释自述依赖 current-item 而非 Qt 的 destination-row 约定，属于对未文档化行为的依赖。
  - 建议: 无当前项时记录警告日志；或改为从 model 的 start/end 参数反解元素 id 与目标位置，不依赖 current 状态。
  - 置信度: 中（正常交互下拖拽会先按下选中，当前项通常存在；空选中路径为边缘情况）

### 通过项
- 信号防递归：set_scenes 在 clear+重建期间 blockSignals(True/False)（42/48 行），防止程序化重建触发 currentItemChanged 风暴；重建后显式 select_scene/setCurrentRow 引发的单次 sceneSelected 由下游守卫消化（motion_graphic_editor_window.py:1685-1686 `_scene_selected` 对相同 scene_id 直接 return），无递归回路。
- 状态提交一致性：select_scene 依据 scene_id 精确匹配行；_selection_changed 仅在 current 非空时发射。
- 线程安全 / GPU 资源 / GLSL·HDR：纯 Qt 控件面板，无 OpenGL 调用、无渲染数据操作。
- 异常与取消路径：无吞异常的宽泛 except；按钮无场景时直接不发信号（_duplicate_current/_delete_current 的 if 守卫）。
- 输入验证：set_scenes 接受 tuple/list 双形态；current_scene_id 对 currentItem 为 None 返回 None。
- 性能模式：select_scene 为 O(n) 行扫描，n 为场景数（个位数量级），无性能问题。
- 类型注解：完整（信号、构造器、回调均有注解）。
