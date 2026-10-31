# 复核报告 — [AUD-2301] brush 槽遍历根级遮罩对 MaskGroup 访问 `.type` 致 AttributeError/进程崩溃

复核人：复核子智能体（独立复核）｜日期：2026-09-28

## [AUD-2301] on_brush_toggled / on_brush_stroke_added 对根级 MaskGroup 直接访问 `s.type` 抛 AttributeError，槽内未捕获致进程崩溃

- **结论：成立（维持 Critical）**
  - 循环代码、MaskGroup 无 `type` 属性、群组可出现在根级 `layer.mask_shapes`、Qt 信号直连无兜底，四项要素全部独立验证通过；未发现任何可推翻或降级的证据。

- **独立证据：**

  1. 槽内遍历直接访问 `s.type`，无 try/except（src/app/controllers/color_controller.py）：
     ```python
     # on_brush_toggled, 1170-1177
     layer = self.engine.get_active_layer()
     if layer:
         brush_shape = None
         for s in layer.mask_shapes:
             if s.type == "brush":        # MaskGroup 无 type → AttributeError
     ```
     `on_brush_stroke_added` 1246-1249 为相同循环。

  2. MaskGroup 确无 `type` 属性（src/features/color/core/layer.py:242-344）：
     - 257 行仅有类属性 `TYPE = "group"`（大写，Python 属性查找大小写敏感，`s.type` 解析不到它）；
     - `__init__`（264-273）只设置 id/name/visible/invert/locked/operation/opacity/children/expanded；
     - 全文件唯一 `@property` 是 MaskShape.has_per_point_feather（123 行），无 `__getattr__`、无 `type` property。`MaskGroup().type` 必然 AttributeError。

  3. MaskGroup 真实出现在根级 `layer.mask_shapes` 的多条写入路径：
     - color_controller.py:1043（on_mask_group_added 空选择分支 `layer.mask_shapes.append(group)`，无需任何选中即可触发）；
     - color_controller.py:1014（多选打包建组）、124（组复制到根级）、1103（on_mask_hierarchy_updated 树拖拽重建后群组可在根级）；
     - layer.py:550（Layer.add_group）、layer.py:1068（Layer.from_dict 工程反序列化根级群组恢复）。

  4. 信号为真实 Qt 信号槽直连，且发射点无异常包裹、全库无自定义 excepthook 兜底：
     - src/app/main_window.py:1586 `self.mask_panel.brush_toggled.connect(self.on_brush_toggled)`
     - src/app/main_window.py:1590 `self.canvas.brush_stroke_added.connect(self.on_brush_stroke_added)`
     - 发射点：src/features/color/ui/mask_panel.py:869-874（按钮 toggled 即 emit）、src/features/color/ui/canvas.py:5997（`_finish_brush_stroke` 直接 emit，无 try/except）；
     - `grep -rn "excepthook" --include="*.py" src/` 无任何结果 → 未安装自定义 `sys.excepthook`。

  5. 后果等级评估：PyQt5.5+ / PyQt6 的文档化默认行为是——槽（由 Qt 信号槽机制调用的 Python callable）内未处理的 Python 异常经 `sys.excepthook` 报告后由 PyQt 调用 `qFatal()`（默认 `abort()`）终止进程。本项目未覆盖 excepthook，故默认 qFatal 生效 → 整个应用崩溃，非仅槽中断。可复现路径：打开图像 → 不选中任何项点 Group 按钮（空群组入根级）→ 点画笔按钮 → 崩溃。

- **与原报告的出入：**
  - 核心论述无出入，引用行号（1174-1177、1246-1249、layer.py:242-273）与实际代码一致，`_iter_trackable_shapes`/`_tracker_display_name` 的防御式 `getattr` 对照证据也属实（color_controller.py:280、287）。
  - 补充（非矛盾）：原报告未列出的第三处同型缺陷——`on_brush_params_changed` 1204 行 `if s.type == "brush" and ...` 同样遍历根级且已连接（main_window.py:1587），修复时应一并处理；此外工程文件反序列化（layer.py:1068）也会把根级群组带回来，即使用户当次未建组，载入旧工程后同样触发。

- **修复建议确认：** 同意原建议——遍历改用递归扁平化（复用 `_iter_trackable_shapes` 的 flatten 逻辑）+ `getattr(s, "type", None) == "brush"`，并对 on_brush_params_changed 的 1204 行同步修复。
