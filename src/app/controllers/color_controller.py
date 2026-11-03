"""ColorControllerMixin - extracted from MainWindow.

Contains methods related to color grading, masks, LUT, denoise, deblur,
crop, brushes, and AI plugin processing.

职责（Responsibilities）:
    - 调色台控制层：遮罩窗口（形状/群组/层级/复制/清除）、跟踪器
      （Tracker2D 绑定/启动/进度/失败重跑/Corner Pin 导出）、LUT
      （应用/强度/重置/反编译）、AI 遮罩（BiRefNet/SAM）、画笔蒙版、
      裁切（crop）、降噪/去模糊（含 source bake 持久化）以及 GLSL
      插件实例的添加与 AI 处理触发。
    - 本 Mixin 不持有状态模型：所有 ``self.*`` 属性引用解析到宿主
      MainWindow 实例（engine、canvas、mask_panel、tracker_panel 等）。

与引擎/页面的边界（Boundary）:
    - 数据与渲染归 ``engine``（ImageEngine）：本层只读 ``engine.layers``
      /``mask_shapes``，经 ``engine.*`` API 触发实际计算，再通过
      ``self.request_render()`` 请求异步重绘，绝不直接操作 OpenGL。
    - 视图归各面板与 canvas：本层负责把数据变更同步回
      ``mask_panel.update_shapes_list``、``canvas.update_mask_overlay``
      等 UI 入口，并处理面板/画布之间的选中态互斥（brush/SAM/crop）。

信号协议（Signal protocol）:
    - 入站：mask_panel 的 ``shape_added``/``shape_selected``/
      ``shape_updated``/``group_added``、canvas 的
      ``mask_shape_selected``、tracker 面板参数与导航信号、
      denoise/deblur 引擎的完成/错误信号等，在 MainWindow 中连接
      到本 Mixin 的 ``on_*``/``_on_*`` 槽。
    - 出站：无自定义 Qt 信号；通过 ``engine.mask_shapes_changed.emit()``
      通知引擎重建遮罩纹理，通过 ``request_render()``/``_trigger_autosave()``
      驱动重绘与自动保存。
"""
from __future__ import annotations

import os
import logging
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Union

if TYPE_CHECKING:
    from src.shared.tracking.track_data import TrackingBinding

from PyQt6.QtWidgets import QMessageBox
import numpy as np

from src.features.color.core.layer import MaskShape, MaskGroup
from src.features.color.core.plugin_manager import get_plugin_manager
from src.shared.io.file_io import FileIO
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class ColorControllerMixin:
    """Mixin providing color/mask/LUT/denoise/deblur/crop/brush controller methods.

    Intended to be mixed into MainWindow so that all ``self.*`` attribute
    references resolve against the MainWindow instance.
    """

    # ------------------------------------------------------------------
    # Mask shape operations
    # ------------------------------------------------------------------

    def _push_mask_structure_snapshot(self) -> None:
        """遮罩结构改动前提交撤销快照（BUG-C4-01 修复入口）。

        遮罩窗口的增/删/清空/建组/层级调整都是用户可见的数据变更，必须
        在改动数据树之前把当前图层序列化状态压入撤销栈，语义与
        :meth:`on_duplicate_mask_shape` / :meth:`on_copy_masks_to_layer_requested`
        一致：``copy_image=False``（非破坏性改动，只回滚遮罩/调色序列化
        状态，不复制源图像像素）。参数微调（``process_mask_update`` 防抖
        路径）刻意不入栈，不要在此处覆盖该语义。

        生产 ``ImageEngine`` 恒有 ``history``；宿主处于极简/半拆卸状态
        （无 history）时跳过提交而不是抛错（AUD-2302：tracker pattern
        提交路径的直驱单测宿主不带 history）。
        """
        history = getattr(self.engine, "history", None)
        if history is None:
            return
        history.push_state(
            self.engine.img_full_original, self.engine.layers,
            self.engine.active_layer_index, copy_image=False)

    # ------------------------------------------------------------------
    # N03: Window/形状写路径的图模式路由（typed handle）
    # ------------------------------------------------------------------

    def _graph_tool_route(self):
        """图编辑上下文的目标（graph_host, node_id）；非路由态返回 None。

        判定与 grade/qualifier 手势一致（B06.2）：选中 Corrector 或
        B16.5 flag 图链接栈。无法定位节点 → None（回落 legacy，手势
        不静默丢失）。
        """
        graph_host = getattr(self, "color_graph_host", None)
        if graph_host is None or not graph_host.graph_edit_context_active():
            return None
        node_id = graph_host.active_grade_node_id()
        if not node_id:
            return None
        return graph_host, node_id

    def _submit_window_structure(self, graph_route, layer,
                                 node_id: str | None = None) -> bool:
        """窗口结构变更（增/删/复制/建组/层级/清空）→ 一次图命令。

        活对象已先行变更（画布/面板即时反馈）；本方法把**整域序列化
        快照**提交为权威（id 保留——绑定/选中稳定）。在途参数手势先
        收束（避免权威分叉）。``node_id`` 缺省为路由活动节点；跨节点
        复制时传目标节点。
        """
        graph_host, active_node_id = graph_route
        target_node = node_id or active_node_id
        if graph_host.grade_gesture_open():
            graph_host.end_grade_gesture()
        if node_id is None:
            graph_host.align_engine_active_layer(target_node)
        shapes = [item.to_dict(None) for item in layer.mask_shapes]
        return graph_host.submit_window_structural_change(target_node, shapes)

    def _submit_window_attribute_patches(self, graph_route,
                                         patches: dict) -> None:
        """窗口参数拖动（防抖队列）→ 单形状 patch 并入手势事务。"""
        graph_host, node_id = graph_route
        if not patches:
            return
        if not graph_host.grade_gesture_open():
            counter = int(getattr(self, '_graph_gesture_counter', 0)) + 1
            self._graph_gesture_counter = counter
            self._graph_gesture_tid = f"window-{counter}"
            graph_host.begin_grade_gesture(self._graph_gesture_tid)
        for shape_id, attributes in patches.items():
            graph_host.submit_window_shape_update(
                node_id, shape_id, attributes, self._graph_gesture_tid)
        self._tool_flush_timer().start()

    def on_mask_shape_added(self, shape_type: str) -> None:
        """Handle mask shape addition with context-aware placement."""
        self.mask_panel.btn_brush.setChecked(False)
        self.mask_panel.btn_sam.setChecked(False)
        layer = self.engine.get_active_layer()
        if not layer:
            return

        # BUG-C4-01：新增遮罩是用户可见的结构变更，改动前提交撤销快照
        # （图模式下 undo 走图命令——跳过 legacy 快照，改完后提交结构命令）
        graph_route = self._graph_tool_route()
        if graph_route is None:
            self._push_mask_structure_snapshot()

        # 实例化新遮罩形状
        shape = MaskShape("curve" if shape_type == "polygon" else shape_type)
        if shape_type == "polygon":
            shape.name = "Polygon"
        video_panel = getattr(self.mask_panel, "video_panel", None)
        if video_panel is not None and not video_panel.isHidden():
            image_size = getattr(self.canvas, "logical_size", None)
            image_height = image_size.height() if image_size is not None else None
            if isinstance(image_height, (int, float)) and image_height > 0:
                aspect = image_size.width() / image_size.height()
                video_panel.set_frame_aspect(aspect)
                if shape_type == "circle":
                    shape.size = [0.15, 0.15 * aspect]
                    shape.feather = 4.13
                elif shape_type == "rect":
                    shape.size = [0.4, 0.4]
                    shape.edge_softness = [1.25] * 4

        # 上下文感知：检查当前是否选中了一个群组
        target_group: Optional[MaskGroup] = None
        if self.canvas.active_shape_id:
            selected_item = layer.get_mask_item_by_id(self.canvas.active_shape_id)
            if isinstance(selected_item, MaskGroup):
                target_group = selected_item

        # 如果选中了群组，则添加为子元素；否则添加到根目录
        if target_group:
            target_group.add_child(shape)
        else:
            layer.mask_shapes.append(shape)

        # 设置 dirty 标记并更新 UI
        layer.is_dirty = True
        layer.mask_dirty = True

        # Leaving pen mode clears scene selection. Do it before activating the
        # new window, otherwise Circle/Rect/Gradient are immediately deselected.
        if shape_type not in ("curve", "polygon"):
            self.canvas.set_drawing_mode(False)
        self.mask_panel.update_shapes_list(layer.mask_shapes)
        self.canvas.update_mask_overlay(layer)
        self.canvas.set_active_shape(shape.id)
        self.mask_panel.select_shape(shape.id)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)

        if shape_type in ("curve", "polygon"):
            self.canvas.set_mask_creation_mode(shape_type)
            self.canvas.set_drawing_mode(True)
            self.status_bar.showMessage("Pen Tool Active", 5000)
        if graph_route is not None:
            self._submit_window_structure(graph_route, layer)
        self.request_render()
        self._trigger_autosave()

    def on_mask_shape_removed(self, shape_id) -> None:
        """删除指定 ID 的遮罩（支持群组内部嵌套项）。

        递归查找待删项，清理画布选中态与绘制模式，同步 Mask 面板、
        跟踪器绑定与图层面板后请求重绘和自动保存。

        Args:
            shape_id: 待删除的遮罩/群组 ID。
        """
        layer = self.engine.get_active_layer()
        if layer:
            # 修复：使用递归查找替代浅层遍历，支持删除群组内部的嵌套遮罩
            if not layer.get_mask_item_by_id(shape_id): return
            # BUG-C4-01：删除遮罩前提交撤销快照（找不到目标时不产生空快照）
            # 图模式下 undo 走图命令（结构快照提交）
            graph_route = self._graph_tool_route()
            if graph_route is None:
                self._push_mask_structure_snapshot()
            previous_id = self.canvas.active_shape_id
            self.canvas.set_drawing_mode(False)
            layer.remove_shape(shape_id)
            if previous_id and layer.get_mask_item_by_id(previous_id) is not None:
                self.canvas.set_active_shape(previous_id)
            else:
                self.canvas.set_active_shape(None)
            self.canvas.update_mask_overlay(layer)
            self.mask_panel.update_shapes_list(list(layer.mask_shapes))
            # 若删除的是当前激活窗口，跟踪器同步解绑（树更新后会自动
            # 选中最后一项并重新触发绑定）
            self._on_tracker_shape_selected(getattr(self.canvas, "active_shape_id", None))
            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
            if graph_route is not None:
                self._submit_window_structure(graph_route, layer)
            self.request_render()
            self._trigger_autosave()

    # ------------------------------------------------------------------
    # P1-COLOR-09: 窗口批量复制（单一 undo 单元）
    # ------------------------------------------------------------------
    def _clone_mask_item(self, item):
        """深拷贝一个 MaskShape/MaskGroup 并递归换新 id。"""
        clone = MaskShape.from_dict(item.to_dict()) \
            if isinstance(item, MaskShape) else MaskGroup.from_dict(item.to_dict())
        self._reassign_mask_ids(clone)
        return clone

    def _reassign_mask_ids(self, item) -> None:
        """递归为克隆的遮罩/群组生成全新 uuid，避免 id 冲突。"""
        item.id = str(uuid.uuid4())
        if isinstance(item, MaskGroup):
            for child in item.children:
                self._reassign_mask_ids(child)

    def on_duplicate_mask_shape(self, shape_id: str) -> None:
        """原地复制选定的窗口（保持锁定/隐藏等全部属性，id 全新）。"""
        layer = self.engine.get_active_layer()
        if not layer:
            return
        source = layer.get_mask_item_by_id(shape_id)
        if source is None:
            return
        graph_route = self._graph_tool_route()
        if graph_route is None:
            self.engine.history.push_state(
                self.engine.img_full_original, self.engine.layers,
                self.engine.active_layer_index, copy_image=False)
        clone = self._clone_mask_item(source)
        if isinstance(source, MaskGroup):
            # 组复制：放到根级，避免自嵌套
            layer.mask_shapes.append(clone)
        else:
            try:
                idx = layer.mask_shapes.index(source)
                layer.mask_shapes.insert(idx + 1, clone)
            except ValueError:
                layer.mask_shapes.append(clone)
        layer.is_dirty = True
        layer.mask_dirty = True
        self.mask_panel.update_shapes_list(layer.mask_shapes)
        self.canvas.update_mask_overlay(layer)
        self.canvas.set_active_shape(clone.id)
        self.mask_panel.select_shape(clone.id)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        if graph_route is not None:
            self._submit_window_structure(graph_route, layer)
        self.request_render()

    def on_copy_masks_to_layer_requested(self) -> None:
        """把当前图层的全部窗口复制到另一个图层（弹出图层选择菜单）。"""
        from PyQt6.QtWidgets import QMenu

        source = self.engine.get_active_layer()
        if source is None or not source.mask_shapes:
            self.status_bar.showMessage(
                I18N.tr("mask.copy_no_windows", "当前图层没有可复制的窗口"), 3000)
            return
        others = [(i, l) for i, l in enumerate(self.engine.layers)
                  if l is not source]
        if not others:
            self.status_bar.showMessage(
                I18N.tr("mask.copy_no_layers", "没有其他图层可接收窗口"), 3000)
            return
        menu = QMenu(self)
        by_action = {}
        for idx, layer in others:
            act = menu.addAction(layer.name or f"Layer {idx + 1}")
            by_action[act] = layer
        chosen = menu.exec(self.mask_panel.mapToGlobal(
            self.mask_panel.rect().center()))
        target = by_action.get(chosen) if chosen is not None else None
        if target is None:
            return
        # N03：图模式跨节点复制——目标 legacy 层反查节点；不可解析
        # （纯 legacy 层）时保持 legacy 行为
        graph_route = self._graph_tool_route()
        target_node_id = None
        if graph_route is not None:
            target_node_id = graph_route[0].node_id_for_legacy_layer(
                getattr(target, "id", ""))
        if graph_route is None or not target_node_id:
            self.engine.history.push_state(
                self.engine.img_full_original, self.engine.layers,
                self.engine.active_layer_index, copy_image=False)
        for item in source.mask_shapes:
            target.mask_shapes.append(self._clone_mask_item(item))
        target.is_dirty = True
        target.mask_dirty = True
        self.mask_panel.update_shapes_list(target.mask_shapes
                                           if target is self.engine.get_active_layer()
                                           else source.mask_shapes)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.status_bar.showMessage(
            I18N.tr("mask.copy_done",
                    "已复制 {count} 个窗口到「{name}」",
                    count=len(source.mask_shapes), name=target.name or target.id[:8]),
            4000)
        if graph_route is not None and target_node_id:
            self._submit_window_structure(graph_route, target,
                                          node_id=target_node_id)
        self.request_render()

    def on_all_masks_cleared(self) -> None:
        """Handle the signal when all masks are cleared via the reset button."""
        # Disable SAM mode if it's active
        self.canvas.set_sam_mode(False)

        # Clear SAM points from engine and canvas
        if hasattr(self.engine, 'reset_sam_points'):
            self.engine.reset_sam_points()
        if hasattr(self.canvas, 'clear_sam_points'):
            self.canvas.clear_sam_points()

        # BUG-C4-01：清空所有遮罩前提交撤销快照；无可清内容时不产生空快照
        # 图模式：只清活动节点（权威域=该节点的窗口），undo 走图命令
        graph_route = self._graph_tool_route()
        if graph_route is None:
            if any(lyr.mask_shapes or lyr.ai_matte for lyr in self.engine.layers):
                self._push_mask_structure_snapshot()
            targets = list(self.engine.layers)
        else:
            targets = [self.engine.get_active_layer()] \
                if self.engine.get_active_layer() else []

        # Clear masks（图模式限定活动节点；legacy 清全部层）
        for layer in targets:
            # Clear all mask shapes from the layer (including Bitmap AI masks from BiRefNet/SAM)
            layer.mask_shapes.clear()

            # Clear AI matte data (legacy BiRefNet/SAM results stored directly)
            layer.ai_matte = None
            layer.ai_matte_enabled = False

            layer.mask_dirty = True
            layer.is_dirty = True

        # Emit signal to notify engine that masks have changed (triggers mask texture update)
        self.engine.mask_shapes_changed.emit()

        # Reset canvas state
        self.canvas.set_active_shape(None)
        self.canvas.set_drawing_mode(False)

        # 跟踪器同步解绑（mask_panel 的清空在 blockSignals 内，不会触发选中信号）
        self._on_tracker_shape_selected(None)

        # Update the mask panel UI to reflect empty list
        self.mask_panel.update_shapes_list([])

        # Update layer panel to refresh mask icons
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)

        if graph_route is not None and targets:
            self._submit_window_structure(graph_route, targets[0])
        # Request render to update the viewport
        self.request_render()
        self._trigger_autosave()
        self.status_bar.showMessage("All masks cleared", 2000)

    def on_mask_shape_updated(self, shape_id, params) -> None:
        """接收遮罩参数变更并入队，经 ``mask_update_timer`` 防抖批量提交。

        Args:
            shape_id: 发生变更的遮罩 ID。
            params: 变更字段字典（feather/opacity/center/size 等），
                稍后由 :meth:`process_mask_update` 统一写入图层。
        """
        self.pending_mask_updates.setdefault(shape_id, {}).update(params)
        if not self.mask_update_timer.isActive(): self.mask_update_timer.start()

    def _sync_window_geometry_controls(self, shape: MaskShape) -> None:
        """Show displayed geometry rather than the untransformed reference."""
        from src.features.color.tracking.tracking_evaluator import capture_base_geometry
        getter = getattr(self.engine, "get_tracked_geometry", None)
        geometry = getter(shape) if callable(getter) else None
        if not isinstance(geometry, dict):
            geometry = capture_base_geometry(shape)
        video_panel = getattr(self.mask_panel, "video_panel", None)
        image_size = getattr(self.canvas, "logical_size", None)
        if video_panel is not None and image_size is not None and image_size.height() > 0:
            video_panel.set_frame_aspect(image_size.width() / image_size.height())
        if video_panel is not None:
            video_panel.set_geometry(geometry)
        self.mask_panel.set_transform_params(geometry["center"], geometry["size"], geometry["rotation"])

    def on_mask_shape_selected(self, shape_id) -> None:
        """画布侧选中遮罩：同步 Mask 面板参数与变换控件。

        区分 MaskGroup（仅 opacity）与 MaskShape（feather/opacity/
        blur/clean_black/clean_white + center/size/rotation）。

        Args:
            shape_id: 新选中的遮罩 ID。
        """
        self.canvas.set_active_shape(shape_id)
        self.mask_panel.select_shape(shape_id)
        layer = self.engine.get_active_layer()
        if layer:
            s = layer.get_mask_item_by_id(shape_id)
            if s:
                if isinstance(s, MaskGroup):
                    self.mask_panel.set_params(0, s.opacity, 0, 0, 0)
                    self.mask_panel.set_transform_visible('group')
                elif isinstance(s, MaskShape):
                    self.mask_panel.set_params(s.feather, s.opacity, getattr(s, 'blur', 0), getattr(s, 'clean_black', 0), getattr(s, 'clean_white', 0))
                    self._sync_window_geometry_controls(s)
                    self.mask_panel.set_transform_visible(s.type)

    def on_panel_shape_selected(self, shape_id) -> None:
        """Mask 面板列表选中遮罩：同步画布选中态与面板控件。

        与 :meth:`on_mask_shape_selected` 类似，但不回写面板列表，
        避免面板→面板的信号递归。

        Args:
            shape_id: 新选中的遮罩 ID。
        """
        self.canvas.set_active_shape(shape_id)
        layer = self.engine.get_active_layer()
        if layer:
            s = layer.get_mask_item_by_id(shape_id)
            if s:
                if isinstance(s, MaskGroup):
                    self.mask_panel.set_params(0, s.opacity, 0, 0, 0)
                    self.mask_panel.set_transform_visible('group')
                elif isinstance(s, MaskShape):
                    self.mask_panel.set_params(s.feather, s.opacity, getattr(s, 'blur', 0), getattr(s, 'clean_black', 0), getattr(s, 'clean_white', 0))
                    self._sync_window_geometry_controls(s)
                    self.mask_panel.set_transform_visible(s.type)

    # 可跟踪的几何窗口类型（位图/画笔蒙版是像素级数据，无几何区域可跟踪）
    TRACKABLE_SHAPE_TYPES = ("circle", "rect", "curve", "gradient")

    def _iter_trackable_shapes(self) -> List[MaskShape]:
        """当前图层中可跟踪的形状（叶节点，排除群组/位图/画笔）。

        N03：图编辑上下文优先读**选中节点的合并投影**（compound 内
        成员/图命令写入的窗口都正确），legacy 上下文读活动层。
        """
        layer = self.engine.get_active_layer()
        graph_route = self._graph_tool_route()
        if graph_route is not None:
            payload = graph_route[0].panel_layer_payload(graph_route[1])
            if payload:
                from src.features.color.core.layer import Layer
                layer = Layer.from_dict(payload)
        if layer is None:
            return []

        def flatten(items):
            result = []
            for item in items:
                if isinstance(item, MaskGroup):
                    result.extend(flatten(item.children))
                else:
                    result.append(item)
            return result

        return [
            shape for shape in flatten(layer.mask_shapes)
            if getattr(shape, "type", "") in self.TRACKABLE_SHAPE_TYPES
        ]

    @staticmethod
    def _tracker_display_name(shape) -> str:
        """与 Masks 面板窗口列表一致的显示名。"""
        return str(getattr(shape, "name", None) or
                   getattr(shape, "type", "window").capitalize())

    def _refresh_tracker_targets(self, bound_shape_id: Optional[str]) -> None:
        """把全部可跟踪窗口填入 Tracker 工具栏目标下拉并选中当前绑定。"""
        pairs = [
            (shape.id, self._tracker_display_name(shape))
            for shape in self._iter_trackable_shapes()
        ]
        self.tracker_panel.toolbar.set_targets(pairs, bound_shape_id)

    def _first_trackable_shape_id(self) -> Optional[str]:
        """返回第一个可跟踪窗口的 ID（无则 None），用于自动绑定回落。"""
        shapes = self._iter_trackable_shapes()
        return shapes[0].id if shapes else None

    def _tracker_result_layer(self, binding):
        """N03：按绑定节点身份解析结果同步层。

        异步跟踪任务完成时活动层/选中可能已切走——绑定捕获的 node_id
        是唯一可信归属（不按当时的 root active_index 猜）。无身份
        （legacy 绑定）或解析失败（节点已删）回落活动层。
        """
        layer = self.engine.get_active_layer()
        if getattr(binding, "node_id", ""):
            graph_host = getattr(self, "color_graph_host", None)
            if graph_host is not None:
                payload = graph_host.panel_layer_payload(binding.node_id)
                if payload:
                    from src.features.color.core.layer import Layer
                    layer = Layer.from_dict(payload)
        return layer

    def _on_tracker_shape_selected(self, shape_id: str) -> None:
        """把当前蒙版窗口绑定到 TrackerPanel。

        TrackerPanel 的空态会主动禁用参数控件。此前遮罩面板和跟踪面板
        没有连接，所以即使用户已经选中了 Window，跟踪器仍永久停留在
        ``_bound_shape is None`` 的空态。
        """
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(shape_id) if layer else None
        if shape is None or isinstance(shape, MaskGroup):
            self._tracker_shape_id = None
            self.tracker_panel.set_bound_shape(None)
            self._refresh_tracker_targets(None)
            return

        self._tracker_shape_id = shape_id
        shape_name = self._tracker_display_name(shape)
        self.tracker_panel.set_bound_shape(shape_name)
        self._refresh_tracker_targets(shape_id)
        self._load_tracker_binding(shape_id)
        self._sync_tracker_overlay(shape)

    def _on_tracker_target_changed(self, target_id: str) -> None:
        """Tracker 工具栏目标下拉切换：按形状 id 重新绑定。"""
        if not target_id or target_id == getattr(self, "_tracker_shape_id", None):
            return
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(target_id) if layer else None
        if shape is None or isinstance(shape, MaskGroup):
            return
        # 同步画布与 Masks 列表的选中态，保持三处一致
        self.canvas.set_active_shape(target_id)
        self.mask_panel.select_shape(target_id)
        self._on_tracker_shape_selected(target_id)

    def _load_tracker_binding(self, shape_id: str) -> None:
        """按当前 ColorSession 恢复当前窗口的曲线和关键帧。"""
        binding = None
        session_manager = getattr(self, "_color_session_manager", None)
        session = (
            session_manager.get_current_session()
            if session_manager is not None
            else None
        )
        clip_id = str(getattr(session, "clip_id", "") or "")
        if clip_id:
            store = getattr(self.engine, "tracking_store", None)
            if store is not None:
                binding = store.get_binding(clip_id, shape_id)

        self._sync_tracker_clip_range()
        self.tracker_panel.set_tracking_binding(binding)
        if binding is not None:
            self.tracker_panel.graph.set_frame_range(binding.source_frame_start, binding.source_frame_end)
            base = binding.base_geometry or {}
            settings = dict(base.get("_tracker_settings", {}))
            # Upgrade the former default preset on the next analysis, without
            # rewriting stored results merely by opening a window. Explicit
            # non-default legacy presets and new-version preferences survive.
            revision = settings.get("solver_revision", 0)
            if (isinstance(revision, (int, float)) and revision < 2
                    and settings.get("algorithm", "region") == "region"
                    and settings.get("feature_count", 12) == 12
                    and settings.get("failure_policy", "stop") == "stop"):
                settings.update(feature_count=64, failure_policy="recapture")
            self.tracker_panel.set_params({**settings,
                "mode": binding.mode.value, "channels": base.get("_motion_channels", {})})
        controller = getattr(self, "video_controller", None)
        fps = getattr(controller, "fps", 24.0)
        if isinstance(fps, (int, float)) and fps > 0:
            self.tracker_panel.graph.set_timebase(float(fps))
        _, source_frame = self._tracker_render_context()
        self.tracker_panel.set_current_frame(source_frame)
        self._tracker_params = self.tracker_panel.get_params()

    def _on_matte_preview_requested(self, style: str) -> None:
        """P1-COLOR-08: qualifier 面板请求切换/关闭 matte 预览。

        style: 'highlight'/'black'/'matte'，空字符串 = 关闭预览。
        同步 qualifier 面板按钮态，保持与 viewport 菜单的 mask toggle 一致。
        """
        engine = getattr(self, 'engine', None)
        if engine is None:
            return
        if style:
            engine.set_mask_preview_mode(True, style)
        else:
            engine.set_mask_preview_mode(False)
        panel = getattr(self, 'qualifier_panel', None)
        if panel is not None and hasattr(panel, 'set_matte_preview_active'):
            panel.set_matte_preview_active(
                bool(style), style or 'highlight')

    def on_send_to_tracker_requested(self) -> None:
        """将遮罩面板当前选中的窗口发送到 TrackerPanel。"""
        shape_id = getattr(self.canvas, "active_shape_id", None)
        if not shape_id:
            self.status_bar.showMessage("请先选择一个蒙版窗口", 3000)
            return

        self._on_tracker_shape_selected(shape_id)
        dock = getattr(self, "dock_tracker", None)
        if dock is not None:
            dock.setVisible(True)
            dock.raise_()
        self.status_bar.showMessage("已将当前窗口发送到跟踪器", 2000)

    def _on_tracker_dock_shown(self, visible: bool) -> None:
        """Tracker dock 变为可见时，自动绑定 Mask 当前提亮的窗口（达芬奇行为）。

        仅在当前绑定与激活形状不一致时同步，避免跟踪运行中反复重绑；
        激活形状已失效（如被删除/是群组）时回落到第一个可跟踪窗口，
        保证"画了形状 → 打开 Tracker"永远有绑定目标，不会卡在空态。
        """
        if not visible:
            # 跟踪器关闭时移除画布上的跟踪区域框；跟踪数据保留在
            # binding 中，重新打开时自动恢复
            if hasattr(self.canvas, "hide_tracker_overlay"):
                self.canvas.hide_tracker_overlay()
            return
        self._sync_tracker_clip_range()
        shape_id = getattr(self.canvas, "active_shape_id", None)
        if shape_id:
            layer = self.engine.get_active_layer()
            resolved = layer.get_mask_item_by_id(shape_id) if layer else None
            if resolved is None or isinstance(resolved, MaskGroup):
                # 选中项不可跟踪（群组/已删除）：回落到第一个可跟踪窗口
                shape_id = self._first_trackable_shape_id()
        else:
            # 无选中形状但存在可跟踪窗口时自动绑定第一个
            shape_id = self._first_trackable_shape_id()
        if shape_id == getattr(self, "_tracker_shape_id", None):
            # 绑定未变：dock 隐藏期间覆盖层被守卫跳过，这里补画
            layer = self.engine.get_active_layer()
            shape = layer.get_mask_item_by_id(shape_id) if layer else None
            if shape is not None and not isinstance(shape, MaskGroup):
                self._sync_tracker_overlay(shape)
            return
        self._on_tracker_shape_selected(shape_id)

    def _on_tracker_params_changed(self, params: dict) -> None:
        """保存当前跟踪参数，并实时更新搜索区域覆盖层。"""
        self._tracker_params = dict(params)
        shape_id = getattr(self, "_tracker_shape_id", None)
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(shape_id) if layer and shape_id else None
        if shape is not None and not isinstance(shape, MaskGroup):
            clip_id, _ = self._tracker_render_context()
            binding = self.engine.tracking_store.get_binding(clip_id, shape_id) if clip_id else None
            channels = params.get("channels")
            if binding is not None and binding.base_geometry is not None and isinstance(channels, dict):
                if binding.base_geometry.get("_motion_channels") != channels:
                    self._push_mask_structure_snapshot()
                    binding.base_geometry["_motion_channels"] = dict(channels)
                    binding.result_version += 1
                    layer.mask_dirty = True
                    self.canvas.update_mask_overlay(layer)
                    self._sync_window_geometry_controls(shape)
                    self.request_render()
                    self._trigger_autosave()
            self._sync_tracker_overlay(shape)

    def _clear_tracker_tracking(self) -> None:
        """Clear the selected window's tracking through the existing store."""
        clip_id, _ = self._tracker_render_context()
        shape_id = getattr(self, "_tracker_shape_id", None)
        if not clip_id or not shape_id:
            return
        binding = self.engine.tracking_store.get_binding(clip_id, shape_id)
        if binding is None:
            return
        self._cancel_tracker_tracking()
        self.tracking_service.invalidate(clip_id=clip_id)
        self._push_mask_structure_snapshot()
        binding.clear_results(keep_manual=False)
        if binding.base_geometry is not None:
            binding.base_geometry.pop("_frame_geometry", None)
        self.tracker_panel.set_tracking_binding(binding)
        layer = self.engine.get_active_layer()
        if layer is not None:
            layer.mask_dirty = True
            self.canvas.update_mask_overlay(layer)
        self.request_render()
        self._trigger_autosave()

    # ------------------------------------------------------------------
    # 跟踪结果应用（渲染端）
    # ------------------------------------------------------------------

    def _tracker_render_context(self) -> tuple[str, int]:
        """给 engine.tracking_context_provider 用：clip_id + 实时帧号。

        播放/逐帧浏览时 session.source_frame 不会实时更新，
        因此优先取 video_controller.current_frame。
        """
        session = self._tracker_session()
        clip_id = str(getattr(session, "clip_id", "") or "")
        frame = getattr(getattr(self, "video_controller", None), "current_frame", None)
        if frame in (None, ""):
            frame = getattr(self, "_last_color_source_frame", None)
        if frame in (None, ""):
            frame = getattr(session, "source_frame", None)
        try:
            return clip_id, int(frame or 0)
        except (TypeError, ValueError):
            return clip_id, 0

    def _on_color_tracking_frame_changed(self, source_frame: int) -> None:
        """视频帧切换：重建受跟踪影响的蒙版纹理并同步曲线播放头。"""
        engine = getattr(self, "engine", None)
        if engine is None or not hasattr(engine, "refresh_tracking_masks"):
            return
        try:
            updated = engine.refresh_tracking_masks(
                int(source_frame), preview=(self._is_video_playing()
                    or bool(getattr(self, '_viewport_scrub_active', False))
                    or bool(getattr(self, '_tracker_graph_scrubbing', False))))
        except Exception:  # noqa: BLE001 - 帧刷新失败不允许阻塞播放管线
            logger.warning("跟踪蒙版随帧刷新失败", exc_info=True)
            return
        if updated and not self._is_video_playing():
            # 暂停态 seek：蒙版纹理已重建，这里补一次视口重绘
            self.request_render()
        dock = getattr(self, "dock_tracker", None)
        if dock is not None and dock.isVisible():
            self.tracker_panel.set_current_frame(int(source_frame))
            stable = getattr(self.tracker_panel, "stabilizer", None)
            if stable is not None:
                stable.graph.set_current_frame(int(source_frame))
        layer = engine.get_active_layer()
        shape_id = getattr(self.canvas, "active_shape_id", None)
        shape = layer.get_mask_item_by_id(shape_id) if layer is not None and shape_id else None
        if isinstance(shape, MaskShape) and not self._is_video_playing():
            self._sync_window_geometry_controls(shape)
            params = getattr(self, "_tracker_params", None)
            if isinstance(params, dict) and params.get("interactive"):
                self._sync_tracker_overlay(shape)

    def _on_color_tracking_play_state_changed(self, is_playing: bool) -> None:
        """Restore still-quality masks after the live playback preview tier."""
        controller = getattr(self, 'video_controller', None)
        if not is_playing and controller is not None:
            self._on_color_tracking_frame_changed(int(controller.current_frame))

    def _on_tracker_pattern_committed(self, region: tuple) -> None:
        """画布拖动 pattern 框：写回窗口几何并同步面板/渲染（达芬奇语义）。"""
        layer = self.engine.get_active_layer()
        shape = (
            layer.get_mask_item_by_id(getattr(self, "_tracker_shape_id", None))
            if layer else None
        )
        if shape is None or isinstance(shape, MaskGroup):
            return
        try:
            x, y, w, h = (float(v) for v in region)
        except (TypeError, ValueError):
            return
        if not np.isfinite([x, y, w, h]).all() or w <= 0.0 or h <= 0.0:
            return
        # AUD-2302：拖拽提交是用户可见的几何变更，改动前提交撤销快照
        # （与 on_mask_shape_removed 同语义；目标缺失/区域非法时不产生
        # 空快照，参数防抖微调仍走 process_mask_update 刻意不入栈）
        self._push_mask_structure_snapshot()
        from src.features.color.tracking.tracking_evaluator import apply_transform_to_geometry, capture_base_geometry
        geometry = self.engine.get_tracked_geometry(shape)
        if not isinstance(geometry, dict):
            geometry = capture_base_geometry(shape)
        px, py, pw, ph = self._tracker_region_from_shape(shape)
        image = self.engine.img_full_original
        if image is None:
            return
        height, width = image.shape[:2]
        sx, sy = w / max(pw, 1e-6), h / max(ph, 1e-6)
        delta = np.array([[sx, 0.0, (x - sx * px) * width],
                          [0.0, sy, (y - sy * py) * height], [0.0, 0.0, 1.0]])
        desired = apply_transform_to_geometry(geometry, delta, (width, height))
        self.on_mask_shape_updated(shape.id, {k: desired[k] for k in
            ("center", "size", "rotation", "points", "grad_points") if k in desired})
        self.process_mask_update()
        self._sync_tracker_overlay(shape)

    def _on_tracker_search_committed(self, region: tuple) -> None:
        """画布拖动 search 框：由 search/pattern 面积比反推搜索倍率。"""
        layer = self.engine.get_active_layer()
        shape = (
            layer.get_mask_item_by_id(getattr(self, "_tracker_shape_id", None))
            if layer else None
        )
        if shape is None or isinstance(shape, MaskGroup):
            return
        try:
            _sx, _sy, sw, sh = (float(v) for v in region)
            px, _py, pw, ph = self._tracker_region_from_shape(shape)
        except (TypeError, ValueError):
            return
        if pw <= 0 or ph <= 0 or sw <= 0 or sh <= 0:
            return
        multiplier = max(1.0, min(8.0, ((sw / pw) + (sh / ph)) * 0.5))
        params = dict(self.tracker_panel.get_params())
        if abs(float(params.get("search_multiplier", 2.0)) - multiplier) < 1e-3:
            return
        params["search_multiplier"] = round(multiplier, 2)
        self.tracker_panel.set_params({"search_multiplier": params["search_multiplier"]})
        self._on_tracker_params_changed(params)

    def _on_tracker_failed_marker_clicked(self, frame: int) -> None:
        """画布点击失败标记：跳转到该失败帧便于手动修正。"""
        self._seek_tracker_frame(int(frame))

    @staticmethod
    def _region_from_geometry(center, size) -> tuple[float, float, float, float]:
        """由归一化 center/size 计算钳制到画面内的区域 (x, y, w, h)。"""
        cx, cy = float(center[0]), float(center[1])
        width, height = max(float(size[0]), 0.001), max(float(size[1]), 0.001)
        x = max(0.0, min(1.0, cx - width / 2.0))
        y = max(0.0, min(1.0, cy - height / 2.0))
        width = min(width, 1.0 - x)
        height = min(height, 1.0 - y)
        return x, y, width, height

    def _tracker_region_from_shape(self, shape) -> tuple[float, float, float, float]:
        """窗口区域：有跟踪结果时按当前帧求值几何计算。"""
        get_geometry = getattr(self.engine, "get_tracked_geometry", None)
        geometry = get_geometry(shape) if callable(get_geometry) else None
        if not isinstance(geometry, dict):
            from src.features.color.tracking.tracking_evaluator import capture_base_geometry
            geometry = capture_base_geometry(shape)
        center, size = geometry.get("center"), geometry.get("size")
        if not isinstance(center, (list, tuple, np.ndarray)) or not isinstance(size, (list, tuple, np.ndarray)):
            raise ValueError("Window geometry must contain a center and size")
        if len(center) != 2 or len(size) != 2 or not np.isfinite([*center, *size]).all():
            raise ValueError("Window geometry must contain finite coordinate pairs")
        kind = geometry.get("type", getattr(shape, "type", ""))
        if kind == "circle":
            size = [float(value) * 2.0 for value in size]
        elif kind == "curve" and geometry.get("points"):
            points = np.asarray([p[k] for p in geometry["points"] for k in ("pos", "c1", "c2")])
            low, high = points.min(axis=0), points.max(axis=0)
            center, size = (low + high) / 2.0, np.maximum(high - low, .01)
        elif kind == "gradient" and geometry.get("grad_points"):
            points = np.asarray(geometry["grad_points"])
            low, high = points.min(axis=0), points.max(axis=0)
            center, size = (low + high) / 2.0, np.maximum(high - low, .1)
        return self._region_from_geometry(center, size)

    def _tracker_search_from_pattern(
            self, pattern: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        """按面板搜索倍率推导 search 区域。"""
        params = getattr(self, "_tracker_params", None)
        if params is None:
            params = self.tracker_panel.get_params()
        multiplier = max(float(params.get("search_multiplier", 2.0)), 1.0)
        px, py, pw, ph = pattern
        search_w = min(pw * multiplier, 1.0)
        search_h = min(ph * multiplier, 1.0)
        search_x = max(0.0, min(1.0 - search_w, px + pw / 2.0 - search_w / 2.0))
        search_y = max(0.0, min(1.0 - search_h, py + ph / 2.0 - search_h / 2.0))
        return search_x, search_y, search_w, search_h

    def _sync_tracker_overlay(self, shape) -> None:
        """将窗口几何转换为归一化 pattern/search 区域并绘制覆盖层。

        仅在跟踪器 dock 可见时绘制：纯 Mask 工作流（添加/选中遮罩）
        不应在画布上出现跟踪区域框；dock 显示时由
        _on_tracker_dock_shown 主动触发同步。
        """
        dock = getattr(self, "dock_tracker", None)
        if dock is not None and not dock.isVisible():
            return
        params = getattr(self, "_tracker_params", None)
        if isinstance(params, dict) and not params.get("interactive", False):
            self.canvas.hide_tracker_overlay()
            return
        image = getattr(self.engine, "img_full_original", None)
        if image is None:
            return

        pattern = self._tracker_region_from_shape(shape)
        search = self._tracker_search_from_pattern(pattern)
        height, width = image.shape[:2]
        self.canvas.show_tracker_overlay(width, height, pattern, search)
        overlay = self.canvas.tracker_overlay
        interactive = bool(params.get("interactive", False)) if isinstance(params, dict) else False
        overlay.set_interactive(interactive)
        clip_id, frame = self._tracker_render_context()
        binding = self.engine.tracking_store.get_binding(clip_id, shape.id) if clip_id else None
        if binding is not None:
            from src.features.color.ui.tracker_overlay import FeaturePointStatus
            from src.shared.tracking.point_manager import active_observations
            obs = active_observations(binding.observations.get(frame, ()), binding)
            points = [(o.x / width, o.y / height, FeaturePointStatus.VALID) for o in obs]
            ids = [o.feature_id for o in obs]
            excluded = [self._tracker_map_feature(binding, p, frame, inverse=False)
                        for p in (binding.base_geometry or {}).get("_excluded_points", [])]
            if excluded:
                keep = [all(np.hypot((p[0] - x) * width, (p[1] - y) * height) > 5.0
                            for x, y in excluded) for p in points]
                points = [p for p, visible in zip(points, keep) if visible]
                ids = [fid for fid, visible in zip(ids, keep) if visible]
            for fid, coords in (binding.base_geometry or {}).get("_user_points", {}).items():
                if int(fid) not in ids and int(fid) not in binding.excluded_feature_ids:
                    x, y = self._tracker_map_feature(binding, coords, frame, inverse=False)
                    # LK observation indices may change after redetection. A
                    # user point retains its own ID and replaces nearby auto
                    # observations so it remains selectable across frames.
                    keep = [np.hypot((p[0] - x) * width, (p[1] - y) * height) > 5.0 for p in points]
                    points = [p for p, visible in zip(points, keep) if visible]
                    ids = [point_id for point_id, visible in zip(ids, keep) if visible]
                    ids.append(int(fid))
                    points.append((x, y, FeaturePointStatus.VALID))
            overlay.set_features(points, ids, binding.locked_feature_ids)

    def _tracker_point_binding(self) -> tuple[MaskShape | MaskGroup | None, TrackingBinding | None, int]:
        """Resolve selected window and its current binding for point edits."""
        shape_id = getattr(self, "_tracker_shape_id", None)
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(shape_id) if layer is not None and shape_id else None
        clip_id, frame = self._tracker_render_context()
        binding = self.engine.tracking_store.get_binding(clip_id, shape_id) if clip_id and shape_id else None
        return shape, binding, frame

    def _tracker_map_feature(self, binding: TrackingBinding, point: Sequence[float],
                             frame: int, inverse: bool) -> tuple[float, float]:
        """Convert a normalized point between original and current reference."""
        image = self.engine.img_full_original
        if image is None:
            return float(point[0]), float(point[1])
        h, w = image.shape[:2]
        transform = binding.transform_at(frame)
        matrix = transform.matrix_np if transform is not None else np.eye(3)
        if inverse:
            matrix = np.linalg.inv(matrix)
        result = matrix @ [point[0] * w, point[1] * h, 1.0]
        return float(result[0] / result[2] / w), float(result[1] / result[2] / h)

    def _on_tracker_insert_point_requested(self) -> None:
        overlay = self.canvas.tracker_overlay
        if overlay.set_insert_mode(self.tracker_panel.btn_insert_point.isChecked()):
            self.canvas.setFocus()

    def _on_tracker_feature_inserted(self, x: float, y: float) -> None:
        shape, binding, frame = self._tracker_point_binding()
        if not isinstance(shape, MaskShape):
            return
        self._push_mask_structure_snapshot()
        if binding is None:
            binding, _ = self._ensure_tracker_binding(shape)
        points = binding.base_geometry.setdefault("_user_points", {})
        all_ids = [o.feature_id for obs in binding.observations.values() for o in obs]
        fid = max([*all_ids, *(int(k) for k in points), 999999]) + 1
        points[str(fid)] = list(self._tracker_map_feature(binding, [x, y], frame, inverse=True))
        binding.lock_feature(fid)
        self.tracker_panel.btn_insert_point.setChecked(False)
        self._sync_tracker_overlay(shape)
        self._trigger_autosave()

    def _on_tracker_set_point_requested(self) -> None:
        shape, binding, frame = self._tracker_point_binding()
        fid = self.canvas.tracker_overlay.selected_feature_id
        if binding is None or fid is None:
            return
        self._push_mask_structure_snapshot()
        obs = next((o for o in binding.observations.get(frame, ()) if o.feature_id == fid), None)
        if obs is not None:
            image = self.engine.img_full_original
            point = [obs.x / image.shape[1], obs.y / image.shape[0]]
            binding.base_geometry.setdefault("_user_points", {})[str(fid)] = list(
                self._tracker_map_feature(binding, point, frame, inverse=True))
        binding.lock_feature(fid)
        self._sync_tracker_overlay(shape)
        self._trigger_autosave()

    def _on_tracker_delete_point_requested(self) -> None:
        shape, binding, frame = self._tracker_point_binding()
        fid = self.canvas.tracker_overlay.selected_feature_id
        if binding is None or fid is None:
            return
        self._push_mask_structure_snapshot()
        point = binding.base_geometry.setdefault("_user_points", {}).pop(str(fid), None)
        if point is None:
            obs = next((o for o in binding.observations.get(frame, ()) if o.feature_id == fid), None)
            if obs is not None:
                image = self.engine.img_full_original
                point = self._tracker_map_feature(binding,
                    [obs.x / image.shape[1], obs.y / image.shape[0]], frame, inverse=True)
        if point is not None:
            binding.base_geometry.setdefault("_excluded_points", []).append(list(point))
        binding.unlock_feature(fid)
        binding.exclude_feature(fid)
        self._sync_tracker_overlay(shape)
        self.tracker_panel.set_selected_feature(None)
        self._trigger_autosave()

    def _tracker_session(self):
        """返回当前 ColorSession（无会话管理器或无会话时为 None）。"""
        manager = getattr(self, "_color_session_manager", None)
        return manager.get_current_session() if manager is not None else None

    def _tracker_frame_context(self) -> tuple[str, str, int, int, int]:
        """返回当前跟踪任务所需的 clip/media/frame 范围。"""
        session = self._tracker_session()
        clip_id = str(getattr(session, "clip_id", "") or "")
        media_path = str(
            getattr(session, "source_path", "")
            or getattr(self.engine, "current_file_path", "")
            or getattr(self, "current_file_path", "")
            or ""
        )
        reference = getattr(getattr(self, "video_controller", None), "current_frame", None)
        if not isinstance(reference, (int, float)):
            reference = getattr(session, "source_frame", None)
        if reference is None:
            reference = getattr(getattr(self, "video_controller", None), "current_frame", 0)
        reference = int(reference or 0)

        # BUG-C4-04：frame_end 用 -1 哨兵初始化。原先 0/0 使下方
        # ``frame_end < frame_start`` 在"无时间线剪辑"时恒为 False，
        # total_frames 回退分支不可达——跟踪范围塌缩为 [0,0]、参考帧被
        # 钳到 0（会话恢复早于时间线加载时即触发）。哨兵使回退在
        # 剪辑缺失或剪辑时长无效时都能生效。
        frame_start, frame_end = 0, -1
        timeline_id = str(getattr(session, "timeline_id", "") or "")
        timeline = getattr(self, "_current_timeline_sequences", {}).get(timeline_id)
        clip = timeline.get_clip_by_id(clip_id) if timeline and clip_id else None
        if clip is not None:
            frame_start = max(0, int(getattr(clip, "source_start", 0) or 0))
            duration = int(getattr(clip, "source_duration", 0) or 0)
            if duration > 0:
                frame_end = frame_start + duration - 1

        if frame_end < frame_start:
            # 剪辑缺失/时长无效：回退到媒体总帧数推导跟踪范围。
            total_frames = getattr(getattr(self, "video_controller", None), "total_frames", None)
            try:
                frame_end = max(frame_start, int(total_frames) - 1)
            except (TypeError, ValueError):
                frame_end = frame_start
        reference = max(frame_start, min(reference, frame_end))
        return clip_id, media_path, reference, frame_start, frame_end

    def _tracker_params_to_engine_factory(self, params: dict) -> None:
        """将面板参数注入每个异步 worker 使用的 Tracker2D。"""
        from src.features.color.tracking.feature_detector import DetectorConfig
        from src.features.color.tracking.window_points import WindowPointDetector
        from src.features.color.tracking.tracker_2d import Tracker2D, TrackerConfig
        from src.features.color.tracking.types import FailurePolicy

        feature_count = max(4, min(1000, int(params.get("feature_count", 64))))
        policy = {
            "stop": FailurePolicy.STOP,
            "hold": FailurePolicy.HOLD_LAST,
            "recapture": FailurePolicy.RECAPTURE,
        }.get(str(params.get("failure_policy", "recapture")), FailurePolicy.RECAPTURE)
        min_confidence = max(0.0, min(1.0, float(params.get("min_confidence", 0.35))))
        config = TrackerConfig(
            weak_confidence=min_confidence,
            valid_confidence=max(0.60, min_confidence),
            failure_policy=policy,
            adaptive_appearance=True,
            lk_consensus_windows=(15, 21, 31) if (
                params.get("algorithm", "region") == "region"
                and params.get("mode", "trs") in ("translation", "trs")
            ) else (),
        )
        detector_config = DetectorConfig(
            max_features=feature_count,
            min_features=max(4, min(feature_count, 8)),
        )
        shape, binding, frame = self._tracker_point_binding()
        seeds, excluded = [], []
        if binding is not None:
            pattern = binding.pattern_region
            for key, target in (("_user_points", seeds), ("_excluded_points", excluded)):
                source = (binding.base_geometry or {}).get(key, {})
                values = source.values() if isinstance(source, dict) else source
                for point in values:
                    x, y = self._tracker_map_feature(binding, point, frame, inverse=False)
                    target.append(((x - pattern[0]) / max(pattern[2], 1e-6),
                                   (y - pattern[1]) / max(pattern[3], 1e-6)))
        service = getattr(self, "tracking_service", None)
        if service is not None:
            service.set_tracker_factory(
                lambda: Tracker2D(
                    config=config,
                    detector=WindowPointDetector(detector_config, tuple(seeds), tuple(excluded),
                                                 params.get("algorithm") == "point"),
                )
            )

    def _ensure_tracker_binding(self, shape):
        """创建或更新当前窗口的 TrackingBinding。"""
        from src.features.color.tracking.track_data import TrackingBinding
        from src.features.color.tracking.tracking_evaluator import capture_base_geometry
        from src.features.color.tracking.types import TrackingMode

        clip_id, media_path, reference, frame_start, frame_end = self._tracker_frame_context()
        if not clip_id or not media_path:
            raise ValueError("当前窗口没有可用于跟踪的时间线剪辑或媒体文件")

        shape_id = str(getattr(shape, "id", "") or "")
        pattern = self._tracker_region_from_shape(shape)
        search = self._tracker_search_from_pattern(pattern)
        params = self.tracker_panel.get_params()
        mode = TrackingMode(str(params.get("mode", "trs")))
        store = self.engine.tracking_store
        binding = store.get_binding(clip_id, shape_id)
        if binding is None:
            binding = TrackingBinding(
                binding_id=str(uuid.uuid4()),
                clip_id=clip_id,
                shape_id=shape_id,
                shape_name=str(getattr(shape, "name", None) or "Window 1"),
            )
            store.add_binding(binding)
        # N03 typed handle：绑定携带图节点身份（异步任务完成后按此解析
        # 归属节点，不按当时活动层猜；legacy 上下文为空串）
        graph_route = self._graph_tool_route()
        if graph_route is not None:
            graph_host = graph_route[0]
            binding.node_id = graph_route[1]
            binding.compound_path = graph_host._tool_compound_path()
            binding.session_id = getattr(
                graph_host._coordinator, "session_id", "")

        # Results are expressed relative to the original reference. Starting
        # another pass at the playhead must preserve earlier frames and edits.
        has_results = bool(binding.transforms or binding.manual_corrections)
        previous = binding.transform_at(reference) if has_results else None
        self._tracker_reference_matrix = previous.matrix_np if previous is not None else np.eye(3)
        binding.shape_name = str(getattr(shape, "name", None) or "Window 1")
        binding.pattern_region = pattern
        binding.search_region = search
        if not has_results:
            binding.reference_frame = reference
        binding.source_frame_start = frame_start
        binding.source_frame_end = frame_end
        binding.mode = mode
        if not has_results or binding.base_geometry is None:
            metadata = {k: v for k, v in (binding.base_geometry or {}).items() if k.startswith("_")}
            binding.base_geometry = {**capture_base_geometry(shape), **metadata}
        binding.base_geometry["_motion_channels"] = dict(params.get("channels", {}))
        binding.base_geometry["_tracker_settings"] = {
            **{k: v for k, v in params.items() if k not in ("interactive", "correction_mode", "channels")},
            "solver_revision": 3,
        }
        self.tracker_panel.set_tracking_binding(binding)
        self.tracker_panel.set_current_frame(reference)
        self.tracker_panel.graph.set_frame_range(frame_start, frame_end)
        return binding, (clip_id, shape_id, media_path, reference, frame_start, frame_end)

    def _start_tracker_auto_tracking(self) -> None:
        """执行 Tracker 中间的自动方向按钮。

        播放头在参考帧右侧时优先向后，左侧时优先向前；随后补齐另一
        方向，覆盖整个片段。TrackingService 会合并两个方向的结果。
        """
        self._start_tracker_tracking("auto")

    def _start_tracker_tracking(
        self, direction: str, *, single_frame: bool = False
    ) -> None:
        """从 TrackerPanel 启动本地异步跟踪。

        ``single_frame`` 对应 DaVinci 的单帧跟踪按钮：仍以绑定的参考帧
        作为模板，只把任务范围限制为参考帧相邻的一帧，避免误用播放
        导航语义。
        """
        shape_id = getattr(self, "_tracker_shape_id", None)
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(shape_id) if layer and shape_id else None
        if shape is None or isinstance(shape, MaskGroup):
            self.status_bar.showMessage("请先选择一个可跟踪的蒙版窗口", 3000)
            return

        try:
            self._push_mask_structure_snapshot()
            binding, context = self._ensure_tracker_binding(shape)
            clip_id, shape_id, media_path, reference, frame_start, frame_end = context
            from src.features.color.tracking.tracker_2d import TrackDirection
            from src.features.color.ui.tracker_status import PanelState
            if direction == "auto":
                current = int(getattr(
                    getattr(self, "video_controller", None),
                    "current_frame", reference))
                first = (
                    TrackDirection.BACKWARD
                    if current >= reference else TrackDirection.FORWARD
                )
                second = (
                    TrackDirection.FORWARD
                    if first == TrackDirection.BACKWARD
                    else TrackDirection.BACKWARD
                )
                directions = (first, second)
            else:
                directions = {
                    "forward": (TrackDirection.FORWARD,),
                    "backward": (TrackDirection.BACKWARD,),
                    "both": (TrackDirection.FORWARD, TrackDirection.BACKWARD),
                }.get(direction, (TrackDirection.FORWARD, TrackDirection.BACKWARD))
            request_start, request_end = frame_start, frame_end
            if single_frame:
                if direction == "forward":
                    request_start = reference
                    request_end = min(frame_end, reference + 1)
                elif direction == "backward":
                    request_start = max(frame_start, reference - 1)
                    request_end = reference
            params = self.tracker_panel.get_params()
            self._tracker_params_to_engine_factory(params)
            self._active_tracker_task = (clip_id, shape_id)
            self._active_tracker_directions = directions
            self._tracker_single_step_target = (
                request_end if direction == "forward" else request_start
            ) if single_frame else None
            self.tracker_panel.set_state(PanelState.RUNNING)
            from src.features.color.tracking.frame_sampler import AnalysisQuality
            quality = {
                "draft": AnalysisQuality.DRAFT,
                "standard": AnalysisQuality.NORMAL,
                "high": AnalysisQuality.HIGH,
            }.get(str(params.get("quality", "standard")), AnalysisQuality.NORMAL)
            analysis_geometry = dict(self.engine.get_tracked_geometry(shape) or binding.base_geometry)
            # The analysis cache must distinguish edited point sets, algorithm
            # selection and solver settings, even when window geometry matches.
            analysis_geometry["_analysis_settings"] = {
                **{k: v for k, v in params.items() if k not in ("interactive", "correction_mode")},
                "appearance_model": "verified_rolling_v1",
                "flow_model": "multiscale_median_v1",
            }
            analysis_geometry["_point_selection"] = {
                k: (binding.base_geometry or {}).get(k) for k in ("_user_points", "_excluded_points")}
            self.tracking_service.start(
                clip_id=clip_id,
                shape_id=shape_id,
                media_path=media_path,
                pattern_region=binding.pattern_region,
                search_region=binding.search_region,
                reference_frame=reference,
                frame_start=request_start,
                frame_end=request_end,
                mode=binding.mode,
                quality=quality,
                directions=directions,
                shape_geometry=analysis_geometry,
            )
        except Exception as exc:  # noqa: BLE001 - UI must recover to failed state
            logger.exception("启动跟踪失败")
            self.tracker_panel.set_failure_reason(str(exc))
            from src.features.color.ui.tracker_status import PanelState
            self.tracker_panel.set_state(PanelState.FAILED)
            self.status_bar.showMessage(f"启动跟踪失败：{exc}", 5000)

    def _retrack_tracker_failed(self) -> None:
        """只重跑当前绑定中的失败帧。"""
        self._tracker_single_step_target = None
        shape_id = getattr(self, "_tracker_shape_id", None)
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(shape_id) if layer and shape_id else None
        if shape is None or isinstance(shape, MaskGroup):
            return
        try:
            binding, context = self._ensure_tracker_binding(shape)
            clip_id, shape_id, media_path, reference, frame_start, frame_end = context
            from src.features.color.tracking.frame_sampler import AnalysisQuality
            from src.features.color.ui.tracker_status import PanelState
            params = self.tracker_panel.get_params()
            if not binding.failed_frames():
                self.status_bar.showMessage("当前没有需要重跟踪的失败帧", 2500)
                self.tracker_panel.set_tracking_binding(binding)
                return
            self._tracker_params_to_engine_factory(params)
            quality = {
                "draft": AnalysisQuality.DRAFT,
                "standard": AnalysisQuality.NORMAL,
                "high": AnalysisQuality.HIGH,
            }.get(str(params.get("quality", "standard")), AnalysisQuality.NORMAL)
            self._active_tracker_task = (clip_id, shape_id)
            from src.features.color.tracking.tracker_2d import TrackDirection
            self._active_tracker_directions = (
                TrackDirection.FORWARD, TrackDirection.BACKWARD
            )
            self.tracker_panel.set_state(PanelState.RUNNING)
            analysis_geometry = dict(self.engine.get_tracked_geometry(shape) or binding.base_geometry)
            analysis_geometry["_analysis_settings"] = {
                **{k: v for k, v in params.items() if k not in ("interactive", "correction_mode")},
                "appearance_model": "verified_rolling_v1",
                "flow_model": "multiscale_median_v1",
            }
            analysis_geometry["_point_selection"] = {
                k: (binding.base_geometry or {}).get(k) for k in ("_user_points", "_excluded_points")}
            self.tracking_service.retrack_failed(
                clip_id=clip_id,
                shape_id=shape_id,
                binding_transforms=binding.transforms,
                media_path=media_path,
                pattern_region=binding.pattern_region,
                search_region=binding.search_region,
                reference_frame=reference,
                frame_start=frame_start,
                frame_end=frame_end,
                mode=binding.mode,
                quality=quality,
                shape_geometry=analysis_geometry,
            )
        except Exception as exc:  # noqa: BLE001 - UI must recover
            logger.exception("重跟踪失败帧失败")
            self.tracker_panel.set_failure_reason(str(exc))
            from src.features.color.ui.tracker_status import PanelState
            self.tracker_panel.set_state(PanelState.FAILED)

    def _reset_tracker_current_frame(self) -> None:
        """删除当前帧手动修正，恢复自动跟踪结果。"""
        active = getattr(self, "_active_tracker_task", None)
        shape_id = getattr(self, "_tracker_shape_id", None)
        if not shape_id:
            return
        clip_id = active[0] if active is not None else str(
            getattr(self._tracker_session(), "clip_id", "") or ""
        )
        if not clip_id:
            return
        binding = self.engine.tracking_store.get_binding(clip_id, shape_id)
        if binding is None:
            return
        frame = int(getattr(self.tracker_panel.graph.view, "_current_frame", 0))
        self._push_mask_structure_snapshot()
        binding.clear_manual_correction(frame)
        self.tracker_panel.set_tracking_binding(binding)
        layer = self.engine.get_active_layer()
        if layer is not None:
            layer.mask_dirty = True
            self.canvas.update_mask_overlay(layer)
        self.request_render()
        self._trigger_autosave()
        layer = self.engine.get_active_layer()
        shape = layer.get_mask_item_by_id(shape_id) if layer is not None else None
        if isinstance(shape, MaskShape):
            self.canvas.update_mask_overlay(layer)
            self._sync_window_geometry_controls(shape)
            self._sync_tracker_overlay(shape)

    def _export_tracker_corner_pin(self) -> None:
        """导出当前绑定的 Corner Pin JSON。"""
        from PyQt6.QtWidgets import QFileDialog
        active = getattr(self, "_active_tracker_task", None)
        shape_id = getattr(self, "_tracker_shape_id", None)
        clip_id = active[0] if active is not None else str(
            getattr(self._tracker_session(), "clip_id", "") or ""
        )
        binding = (
            self.engine.tracking_store.get_binding(clip_id, shape_id)
            if clip_id and shape_id else None
        )
        image = getattr(self.engine, "img_full_original", None)
        if binding is None or image is None:
            self.status_bar.showMessage("当前没有可导出的跟踪绑定", 3000)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Corner Pin", "corner_pin.json", "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            from src.features.color.tracking.corner_pin import write_corner_pin_export
            height, width = image.shape[:2]
            write_corner_pin_export(binding, int(width), int(height), path)
            self.status_bar.showMessage(f"Corner Pin 已导出：{path}", 4000)
        except Exception as exc:  # noqa: BLE001 - report export failure in UI
            logger.exception("Corner Pin 导出失败")
            self.status_bar.showMessage(f"Corner Pin 导出失败：{exc}", 5000)

    def _tracker_task_matches(self, task_key: str) -> bool:
        """判断回调归属：task_key 是否为当前激活的跟踪任务。"""
        active = getattr(self, "_active_tracker_task", None)
        return active is not None and task_key == self.tracking_service.task_key(*active)

    def _on_tracker_task_started(self, task_key: str, _generation: int) -> None:
        """TrackingService 任务启动回调：面板进入 RUNNING 态。"""
        if self._tracker_task_matches(task_key):
            from src.features.color.ui.tracker_status import PanelState
            self.tracker_panel.set_state(PanelState.RUNNING)

    def _on_tracker_task_progress(self, task_key: str, _generation: int, done: int, total: int) -> None:
        """TrackingService 进度回调：更新面板进度条。"""
        if self._tracker_task_matches(task_key):
            self.tracker_panel.set_progress(done, total, 0, self.tracker_panel.feature_spin.value())

    def _on_tracker_task_finished(self, task_key: str, _generation: int, result: object) -> None:
        """跟踪任务完成回调：把 transforms 合入绑定并即时刷新蒙版。

        结果按帧写入 ``binding.apply_result``，记录前向/后向覆盖标记，
        随后立即调用 ``engine.refresh_tracking_masks`` 让当前帧的蒙版
        马上生效（达芬奇行为），不等待下一次播放。
        """
        if not self._tracker_task_matches(task_key):
            return
        active = getattr(self, "_active_tracker_task", None)
        if active is None:
            return
        clip_id, shape_id = active
        binding = self.engine.tracking_store.get_binding(clip_id, shape_id)
        if binding is None:
            return
        from src.features.color.tracking.types import TrackTransform
        reference_matrix = getattr(self, "_tracker_reference_matrix", np.eye(3))
        for frame, transform in (getattr(result, "transforms", {}) or {}).items():
            transform = TrackTransform.from_matrix(
                frame, transform.matrix_np @ reference_matrix,
                transform.confidence, transform.status, transform.failure_reason)
            binding.apply_result(transform, (getattr(result, "observations", {}) or {}).get(frame))
        directions = getattr(self, "_active_tracker_directions", ())
        from src.features.color.tracking.tracker_2d import TrackDirection
        binding.has_forward = binding.has_forward or TrackDirection.FORWARD in directions
        binding.has_backward = binding.has_backward or TrackDirection.BACKWARD in directions
        self.tracker_panel.set_tracking_binding(binding)
        target = getattr(self, "_tracker_single_step_target", None)
        if target is not None:
            self._tracker_single_step_target = None
            self._seek_tracker_frame(int(target))
        elif (directions and not getattr(result, "cancelled", False)
              and not self.tracking_service.is_running(clip_id, shape_id)):
            # A completed pass leaves the viewer at its analyzed endpoint,
            # including a pass started in the middle of a trimmed clip.
            completed = [frame for frame in
                         (binding.transforms or getattr(result, "transforms", {}))
                         if binding.source_frame_start <= frame <= binding.source_frame_end]
            if completed:
                endpoint = (max(completed) if directions[-1] == TrackDirection.FORWARD
                            else min(completed))
                self._seek_tracker_frame(int(endpoint))
        self._trigger_autosave()
        # 跟踪结果即时生效：当前帧若落在结果范围内，立刻重建蒙版并重绘，
        # 无需等待下一次播放/手动刷新（达芬奇行为）
        layer = self._tracker_result_layer(binding)
        shape = layer.get_mask_item_by_id(shape_id) if layer is not None else None
        if isinstance(shape, MaskShape):
            self.canvas.update_mask_overlay(layer)
            self._sync_window_geometry_controls(shape)
            self._sync_tracker_overlay(shape)
        refresh = getattr(self.engine, "refresh_tracking_masks", None)
        if callable(refresh):
            try:
                if refresh():
                    if not self._is_video_playing():
                        self.request_render()
            except Exception:  # noqa: BLE001 - 应用失败不掩盖跟踪完成状态
                logger.warning("跟踪完成后立即应用蒙版失败", exc_info=True)
        if self.tracking_service.is_running(clip_id, shape_id):
            from src.features.color.ui.tracker_status import PanelState
            self.tracker_panel.set_state(PanelState.RUNNING)

    def _on_tracker_task_failed(self, task_key: str, _generation: int, message: str) -> None:
        """跟踪任务失败回调：面板显示失败原因并进入 FAILED 态。"""
        if self._tracker_task_matches(task_key):
            self._tracker_single_step_target = None
            from src.features.color.ui.tracker_status import PanelState
            self.tracker_panel.set_failure_reason(message)
            self.tracker_panel.set_state(PanelState.FAILED)

    def _on_tracker_task_cancelled(self, task_key: str, _generation: int) -> None:
        """Stop analysis while retaining already completed window tracking."""
        if self._tracker_task_matches(task_key):
            self._tracker_single_step_target = None
            active = getattr(self, "_active_tracker_task", None)
            binding = self.engine.tracking_store.get_binding(*active) if active is not None else None
            if binding is not None and binding.frame_range_with_results() is not None:
                self.tracker_panel.set_tracking_binding(binding)
                return
            from src.features.color.ui.tracker_status import PanelState
            self.tracker_panel.set_state(PanelState.EMPTY)

    def _cancel_tracker_tracking(self) -> None:
        """取消当前激活的跟踪任务（若有）。"""
        self._tracker_single_step_target = None
        active = getattr(self, "_active_tracker_task", None)
        if active is not None:
            self.tracking_service.cancel(*active)

    def _on_tracker_graph_frame_clicked(self, frame: int) -> None:
        """跟踪曲线图点击帧：跳转播放头到该帧。"""
        if getattr(self, '_tracker_graph_scrubbing', False):
            self.on_video_scrub_moved(self._color_source_frame_to_local_frame(int(frame)))
            return
        if getattr(self, '_tracker_graph_settle_pending', False):
            self._tracker_graph_settle_pending = False
            self.on_video_seek(self._color_source_frame_to_local_frame(int(frame)))
            self.tracker_panel.set_current_frame(int(frame))
            return
        self._seek_tracker_frame(int(frame))

    def _on_tracker_graph_scrub_started(self, frame: int) -> None:
        """Use the viewport's nonblocking scrub lane in source coordinates."""
        self._tracker_graph_scrubbing = True
        self._tracker_graph_settle_pending = False
        self.on_video_scrub_started(self._color_source_frame_to_local_frame(int(frame)))

    def _on_tracker_graph_scrub_finished(self, frame: int) -> None:
        """Let the final graph seek settle at the exact decoded frame."""
        self._tracker_graph_scrubbing = False
        self._tracker_graph_settle_pending = True
        self.on_video_scrub_finished(self._color_source_frame_to_local_frame(int(frame)))

    def _sync_tracker_clip_range(self) -> None:
        """Initialize the scrub ruler even before the first window is created."""
        panel = getattr(self, 'tracker_panel', None)
        controller = getattr(self, 'video_controller', None)
        if panel is None or controller is None:
            return
        size = getattr(getattr(self, "canvas", None), "logical_size", None)
        if size is not None:
            width, height = size.width(), size.height()
            if (isinstance(width, (int, float)) and isinstance(height, (int, float))
                    and np.isfinite(width) and np.isfinite(height)):
                panel.set_source_frame_size(int(width), int(height))
        _clip, _media, current, first, last = self._tracker_frame_context()
        panel.graph.set_frame_range(first, last)
        fps = getattr(controller, 'fps', 24.0)
        if isinstance(fps, (int, float)) and fps > 0.0:
            panel.graph.set_timebase(float(fps))
        panel.set_current_frame(current)
        sync_stabilizer = getattr(self, "_sync_stabilizer_panel", None)
        if callable(sync_stabilizer) and hasattr(self, "stabilization_service"):
            sync_stabilizer()

    def _on_tracker_nav_requested(self, action: str) -> None:
        """Tracker 工具栏导航（first/prev/next/last）受片段帧范围约束。"""
        try:
            _clip_id, _media, reference, frame_start, frame_end = self._tracker_frame_context()
        except Exception:
            return
        current = int(getattr(getattr(self, "video_controller", None), "current_frame", reference))
        target = {
            "first": frame_start,
            "prev": max(frame_start, current - 1),
            "next": min(frame_end, current + 1),
            "last": frame_end,
        }.get(action, current)
        self._seek_tracker_frame(target)

    def _seek_tracker_frame(self, frame: int) -> None:
        """把播放头 seek 到指定帧并同步 Tracker 面板当前帧。"""
        controller = getattr(self, "video_controller", None)
        seek = getattr(controller, "seek_to_frame", None)
        if callable(seek):
            to_local = getattr(self, "_color_source_frame_to_local_frame", None)
            seek(int(to_local(frame)) if callable(to_local) else int(frame))
        self.tracker_panel.set_current_frame(int(frame))

    # ------------------------------------------------------------------
    # Mask group & hierarchy
    # ------------------------------------------------------------------

    def on_mask_group_added(self, selected_ids: Optional[List[str]] = None) -> None:
        """Handle mask group addition with multi-select packaging support.

        Args:
            selected_ids: 可选的选中遮罩 ID 列表。如果提供且非空，则将这些遮罩打包进新群组；
                         否则创建一个空的群组（上下文感知放置）。
        """
        layer = self.engine.get_active_layer()
        if not layer:
            return

        # BUG-C4-01：建组（打包既有窗口或新建空组）是结构变更，先提交撤销快照
        # 图模式下 undo 走图命令
        graph_route = self._graph_tool_route()
        if graph_route is None:
            self._push_mask_structure_snapshot()

        # 情况 1：多选打包建组
        if selected_ids and len(selected_ids) > 0:
            # 创建新群组
            group = MaskGroup("Group")

            # 遍历选中的 ID，收集对应的遮罩对象
            items_to_package: List[Union[MaskShape, MaskGroup]] = []
            for sid in selected_ids:
                item = layer.get_mask_item_by_id(sid)
                if item:
                    items_to_package.append(item)

            # 按原始顺序排序（保持用户在树中看到的顺序）
            # 通过遍历 mask_shapes 来确定顺序
            ordered_items: List[Union[MaskShape, MaskGroup]] = []

            def collect_in_order(items: list) -> None:
                """递归收集有序的遮罩项。"""
                for item in items:
                    if item in items_to_package:
                        ordered_items.append(item)
                    if isinstance(item, MaskGroup):
                        collect_in_order(item.children)

            collect_in_order(layer.mask_shapes)

            # 从原位置移除这些项
            for item in ordered_items:
                layer._remove_item_recursive(layer.mask_shapes, item.id)

            # 将它们加入新群组
            for item in ordered_items:
                group.add_child(item)

            # 将群组添加到根目录
            layer.mask_shapes.append(group)

            # 设置 dirty 标记并更新 UI
            layer.is_dirty = True
            layer.mask_dirty = True

            self.mask_panel.update_shapes_list(layer.mask_shapes)
            self.canvas.update_mask_overlay(layer)
            self.canvas.set_active_shape(group.id)
            self.mask_panel.select_shape(group.id)
            if graph_route is not None:
                self._submit_window_structure(graph_route, layer)
            self.request_render()
            self._trigger_autosave()
            self.status_bar.showMessage(f"Packed {len(ordered_items)} items into new Group", 2000)
            return

        # 情况 2：空选择，创建空群组（上下文感知放置）
        group = MaskGroup("Group")

        # 上下文感知：检查当前是否选中了另一个群组
        target_group: Optional[MaskGroup] = None
        if self.canvas.active_shape_id:
            selected_item = layer.get_mask_item_by_id(self.canvas.active_shape_id)
            if isinstance(selected_item, MaskGroup):
                target_group = selected_item

        # 如果选中了群组，则添加为子元素；否则添加到根目录
        if target_group:
            target_group.add_child(group)
        else:
            layer.mask_shapes.append(group)

        # 设置 dirty 标记并更新 UI
        layer.is_dirty = True
        layer.mask_dirty = True

        self.mask_panel.update_shapes_list(layer.mask_shapes)
        self.canvas.update_mask_overlay(layer)
        self.canvas.set_active_shape(group.id)
        self.mask_panel.select_shape(group.id)
        if graph_route is not None:
            self._submit_window_structure(graph_route, layer)
        self.request_render()
        self._trigger_autosave()
        self.status_bar.showMessage(f"Mask Group added: {group.name}", 2000)

    def on_mask_hierarchy_updated(self, structure: list) -> None:
        """处理树形控件拖拽后的层级变更，重组底层数据结构。

        Args:
            structure: 嵌套字典列表，格式为 [{"id": "...", "children": [...]}, ...]
        """
        layer = self.engine.get_active_layer()
        if not layer:
            return

        # BUG-C4-01：层级拖拽重建数据树前提交撤销快照。必须在
        # ``extract_items`` 之前——该步骤会就地清空群组 children，压栈晚了
        # 快照就会捕获被拆散的中间态。图模式下 undo 走图命令。
        graph_route = self._graph_tool_route()
        if graph_route is None:
            self._push_mask_structure_snapshot()

        # 内部递归函数：提取所有元素到扁平字典，并清空群组的 children
        all_items: Dict[str, Union[MaskShape, MaskGroup]] = {}

        def extract_items(items: list) -> None:
            """递归提取所有遮罩元素到扁平字典，同时清空群组的 children。"""
            for item in items:
                all_items[item.id] = item
                if isinstance(item, MaskGroup):
                    # 必须先备份子节点，再清空，最后对备份进行递归
                    old_children = list(item.children)
                    item.children = []
                    extract_items(old_children)

        # 从当前 layer.mask_shapes 提取所有元素
        extract_items(layer.mask_shapes)

        # 内部递归函数：根据新结构重建数据树
        def build_from_struct(struct_list: list) -> list:
            """根据结构描述递归重建数据树。

            消费 ``all_items`` 中的项（pop），未消费的项在调用侧用于
            BUG-C4-03 的兜底（追加回根级，绝不静默丢弃）。
            """
            result: List[Union[MaskShape, MaskGroup]] = []
            for node in struct_list:
                item_id = node.get("id")
                if not item_id or item_id not in all_items:
                    continue

                item = all_items.pop(item_id)

                # 如果是群组且有子节点，递归填充 children
                if isinstance(item, MaskGroup) and "children" in node:
                    item.children = build_from_struct(node["children"])

                result.append(item)

            return result

        # 重建数据树并赋值
        layer.mask_shapes = build_from_struct(structure)

        # BUG-C4-03：原树中存在但新结构缺失的遮罩不得静默丢弃（否则配合
        # 不可撤销的重建操作就是不可逆数据丢失）。把未被结构消费的项按
        # 原顺序追加回根级，并记录警告 + 数量，保留可观测性。
        if all_items:
            dropped_ids = list(all_items.keys())
            logger.warning(
                "Mask hierarchy structure missing %d item(s) present in the "
                "layer: %s — appended back at root level (BUG-C4-03 guard)",
                len(dropped_ids), dropped_ids,
            )
            layer.mask_shapes.extend(all_items.values())

        # 设置 dirty 标记并触发渲染
        layer.is_dirty = True
        layer.mask_dirty = True
        if graph_route is not None:
            self._submit_window_structure(graph_route, layer)
        self.request_render()
        self._trigger_autosave()

        # 修复 Qt 原生拖拽导致 ItemWidget 丢失的问题，强制重新挂载 UI
        self.mask_panel.update_shapes_list(layer.mask_shapes)

        self.status_bar.showMessage("Mask hierarchy updated", 2000)

    # ------------------------------------------------------------------
    # AI mask / BiRefNet / SAM
    # ------------------------------------------------------------------

    def on_birefnet_requested(self) -> None:
        """Handle BiRefNet background removal request with progress feedback."""
        if self.engine.img_full_original is None:
            return
        self.status_bar.showMessage("Removing Background (AI Processing)...", 0)
        self.progress_bar.setRange(0, 0)  # Indeterminate mode
        self.progress_bar.show()
        self.engine.request_birefnet()

    def on_ai_process_finished(self) -> None:
        """Handle AI process completion - hide progress and show success message."""
        self.progress_bar.hide()
        self.status_bar.showMessage("Background Removed Successfully", 5000)

    def on_ai_sam_toggled(self, enabled) -> None:
        """Smart Brush（SAM）开关：进入时先编码，退出时隐藏进度条。

        开启时互斥关闭画笔/取色器/裁切工具，触发
        ``engine.request_sam_encode()`` 并清空旧采样点。

        Args:
            enabled: True 表示进入 SAM 智能笔刷模式。
        """
        self.canvas.set_sam_mode(enabled)
        if enabled:
            self.mask_panel.btn_brush.setChecked(False)
            self.qualifier_panel.set_picker_active(False)
            if hasattr(self.crop_panel, 'set_crop_active'): self.crop_panel.set_crop_active(False)
            # Show initializing status and progress bar
            self.status_bar.showMessage("Smart Brush: Initializing AI Model... Please wait.", 0)
            self.progress_bar.setRange(0, 0)  # Indeterminate mode
            self.progress_bar.show()
            # Trigger encoding
            self.engine.request_sam_encode()
            self.engine.reset_sam_points()
        else:
            self.progress_bar.hide()
            self.status_bar.showMessage("Smart Brush Deactivated", 2000)

    def on_sam_encode_finished(self) -> None:
        """Slot called when SAM encoding is complete."""
        self.canvas.set_sam_ready(True)
        self.progress_bar.hide()
        self.status_bar.showMessage("Smart Brush Active - Click object to segment", 0)

    # ------------------------------------------------------------------
    # Brush tools
    # ------------------------------------------------------------------

    def _iter_brush_shapes(self, layer) -> List[MaskShape]:
        """递归遍历图层中的画笔形状（穿透 MaskGroup）。

        AUD-2301：根级 ``layer.mask_shapes`` 可包含 MaskGroup（仅类属性
        ``TYPE``，无实例 ``type``），直接 ``s.type`` 会 AttributeError 且槽内
        未捕获导致进程崩溃；画笔蒙版也可能被拖进群组。按
        ``_iter_trackable_shapes`` 的扁平化模式递归展开后，再用防御式
        ``getattr`` 判断类型。
        """
        def flatten(items):
            result = []
            for item in items:
                if isinstance(item, MaskGroup):
                    result.extend(flatten(item.children))
                else:
                    result.append(item)
            return result

        return [s for s in flatten(layer.mask_shapes)
                if getattr(s, "type", None) == "brush"]

    def on_brush_toggled(self, enabled) -> None:
        """Handle brush mode toggle from mask panel."""
        self.canvas.set_brush_mode(enabled)
        if enabled:
            self.mask_panel.btn_sam.setChecked(False)
            self.qualifier_panel.set_picker_active(False)
            if hasattr(self.crop_panel, 'set_crop_active'): self.crop_panel.set_crop_active(False)
            self.status_bar.showMessage("Brush Tool Active - Paint to create mask", 0)
            # Create a new brush shape if none exists
            layer = self.engine.get_active_layer()
            if layer:
                # Check if there's already a brush shape (recursive: may
                # live inside a MaskGroup)
                brush_shapes = self._iter_brush_shapes(layer)
                brush_shape = brush_shapes[0] if brush_shapes else None

                if not brush_shape:
                    # Create a new brush shape
                    from src.features.color.core.layer import MaskShape
                    brush_shape = MaskShape(MaskShape.BRUSH)
                    brush_shape.name = "Brush Mask"
                    layer.mask_shapes.append(brush_shape)
                    layer.mask_dirty = True
                    layer.is_dirty = True
                    self.mask_panel.update_shapes_list(layer.mask_shapes)
                    self.canvas.set_active_shape(brush_shape.id)
        else:
            self.status_bar.showMessage("Brush Tool Deactivated", 2000)

    def on_brush_params_changed(self, params) -> None:
        """Handle brush parameter changes from mask panel."""
        self.canvas.set_brush_params(params)

        # Sync eraser state to canvas
        is_eraser = params.get("is_eraser", False)
        self.canvas.brush_cursor.set_eraser_mode(is_eraser)

        # Update the active brush shape's default parameters
        layer = self.engine.get_active_layer()
        graph_route = self._graph_tool_route()
        if layer:
            for s in self._iter_brush_shapes(layer):
                if s.id == self.canvas.active_shape_id:
                    s.brush_size = params["size"]
                    s.brush_feather = params["feather"]
                    s.brush_opacity = params["opacity"]
                    s.brush_flow = params["flow"]
                    if graph_route is not None:
                        # N03：画笔默认参数 → 图命令（权威；活对象已同值更新）
                        self._submit_window_attribute_patches(
                            graph_route, {s.id: {
                                "brush_size": params["size"],
                                "brush_feather": params["feather"],
                                "brush_opacity": params["opacity"],
                                "brush_flow": params["flow"]}})
                    break

    def on_canvas_brush_params_updated(self, params) -> None:
        """Handle brush parameter updates from canvas (keyboard shortcuts).

        This syncs changes made via keyboard shortcuts back to the mask panel UI.
        """
        # Update mask panel drag values to reflect the new values
        self.mask_panel.brush_size_drag.blockSignals(True)
        self.mask_panel.brush_feather_drag.blockSignals(True)
        self.mask_panel.brush_opacity_drag.blockSignals(True)
        self.mask_panel.brush_flow_drag.blockSignals(True)

        self.mask_panel.brush_size_drag.setValue(params.get("size", 50))
        self.mask_panel.brush_feather_drag.setValue(params.get("feather", 0))
        self.mask_panel.brush_opacity_drag.setValue(params.get("opacity", 1.0) * 100)
        self.mask_panel.brush_flow_drag.setValue(params.get("flow", 1.0) * 100)

        self.mask_panel.brush_size_drag.blockSignals(False)
        self.mask_panel.brush_feather_drag.blockSignals(False)
        self.mask_panel.brush_opacity_drag.blockSignals(False)
        self.mask_panel.brush_flow_drag.blockSignals(False)

        # Sync eraser state to mask panel
        is_eraser = params.get("is_eraser", False)
        self.mask_panel.btn_eraser.blockSignals(True)
        self.mask_panel.btn_eraser.setChecked(is_eraser)
        self.mask_panel.btn_eraser.blockSignals(False)

    def on_brush_stroke_added(self, points, params) -> None:
        """Handle brush stroke completion - save stroke data to the active brush shape."""
        layer = self.engine.get_active_layer()
        if not layer:
            return

        # BUG-C4-01：每条落笔都是一个可撤销单元，写入前提交撤销快照
        # 图模式下 undo 走图命令
        graph_route = self._graph_tool_route()
        if graph_route is None:
            self._push_mask_structure_snapshot()

        # Find the active brush shape (recursive: may live inside a MaskGroup)
        brush_shapes = self._iter_brush_shapes(layer)
        brush_shape = brush_shapes[0] if brush_shapes else None

        if not brush_shape:
            # Create a new brush shape if none exists
            from src.features.color.core.layer import MaskShape
            brush_shape = MaskShape(MaskShape.BRUSH)
            brush_shape.name = "Brush Mask"
            layer.mask_shapes.append(brush_shape)
            self.mask_panel.update_shapes_list(layer.mask_shapes)
            self.canvas.set_active_shape(brush_shape.id)

        # Initialize brush_strokes list if not exists
        if not hasattr(brush_shape, 'brush_strokes'):
            brush_shape.brush_strokes = []

        # Add the new stroke
        stroke = {
            'points': points,
            'size': params.get('size', 50),
            'feather': params.get('feather', 0),
            'opacity': params.get('opacity', 1.0),
            'flow': params.get('flow', 1.0),
            'is_eraser': params.get('is_eraser', False)
        }
        brush_shape.brush_strokes.append(stroke)

        # Update brush shape default params
        brush_shape.brush_size = params.get('size', 50)
        brush_shape.brush_feather = params.get('feather', 0)
        brush_shape.brush_opacity = params.get('opacity', 1.0)
        brush_shape.brush_flow = params.get('flow', 1.0)

        # Mark layer as dirty for mask update
        layer.mask_dirty = True
        layer.is_dirty = True

        if graph_route is not None:
            # N03：画笔落笔（新建形状/追加笔画）→ 结构图命令
            self._submit_window_structure(graph_route, layer)
        # Request render to update the mask
        self.request_render()
        self._trigger_autosave()

    # ------------------------------------------------------------------
    # Mask update processing
    # ------------------------------------------------------------------

    def _maybe_record_mask_keyframes(self, layer, updated_fields) -> None:
        """P1-COLOR-09: mask 改动写关键帧。

        语义与 grade 参数一致（专业调色台习惯）：
        - 形状已有任一 mask 轨道：该形状的改动自动写入当前帧关键帧；
        - 录制模式（grading 面板 ◆）开启：全部改动形状记录。
        无当前帧（静态图/未播放）时不记录。
        """
        engine = getattr(self, 'engine', None)
        frame = getattr(engine, 'current_grade_frame', None)
        if frame is None or not updated_fields:
            return
        panel = getattr(self, 'grading_panel', None)
        record_mode = bool(getattr(panel, 'keyframe_mode', False))
        tracks = getattr(layer, 'grade_keyframes', None) or {}
        from src.features.color.core.grade_keyframes import (
            MASK_KEYFRAME_FIELDS, record_mask_keyframe, shape_has_mask_tracks,
        )
        wrote = False
        for shape_id, fields in updated_fields:
            animatable = [f for f in fields if f in MASK_KEYFRAME_FIELDS]
            if not animatable:
                continue
            if record_mode or shape_has_mask_tracks(tracks, shape_id):
                if record_mask_keyframe(layer, int(frame), shape_id, animatable):
                    wrote = True
        if wrote:
            refresh = getattr(self, '_refresh_grade_keyframe_ui', None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass

    def process_mask_update(self) -> None:
        """把 ``pending_mask_updates`` 队列中的遮罩参数批量写入当前图层。

        由 ``mask_update_timer`` 周期触发：MaskGroup 只接受 opacity/
        visible/invert/locked/operation；MaskShape 逐字段写属性。
        写入后按需记录 mask 关键帧（P1-COLOR-09），置 dirty 标记，
        请求重绘，并回写面板滑条（仅当更新的是当前激活形状）。
        """
        layer = self.engine.get_active_layer()
        if not layer: return
        # N03：图模式快照待提交 patch（活对象先行同值更新，图命令随后
        # 提交为权威——补丁只收实际命中活形状的项）
        graph_route = self._graph_tool_route()
        graph_patches: dict = {}
        updates_applied = False
        last_updated_shape = None
        updated_fields = []

        # We process all pending updates, but keep track of the last shape updated
        # to ensure the UI panel reflects the correct values.
        for shape_id, params in self.pending_mask_updates.items():
            s = layer.get_mask_item_by_id(shape_id)
            if s:
                if graph_route is not None:
                    graph_patches[shape_id] = dict(params)
                recorded_fields = list(params)
                if isinstance(s, MaskGroup):
                    # MaskGroup 只支持 opacity 参数
                    if "opacity" in params:
                        s.opacity = params["opacity"]
                    if "visible" in params:
                        s.visible = params["visible"]
                    if "invert" in params:
                        s.invert = params["invert"]
                    if "locked" in params:
                        s.locked = params["locked"]
                    if "operation" in params:
                        s.operation = params["operation"]
                elif isinstance(s, MaskShape):
                    geometry_fields = {"center", "size", "rotation", "points", "grad_points"}
                    geometry_updates = {k: v for k, v in params.items() if k in geometry_fields}
                    tracked_edit = False
                    if geometry_updates:
                        clip_id, frame = self._tracker_render_context()
                        store = getattr(self.engine, "tracking_store", None)
                        binding = store.get_binding(clip_id, shape_id) if store is not None and clip_id else None
                        if binding is not None and binding.base_geometry is not None and (
                                binding.transforms or binding.manual_corrections):
                            from src.features.color.tracking.window_edit import edit_tracked_geometry
                            geometry = self.engine.get_tracked_geometry(s)
                            image = getattr(self.engine, "img_full_original", None)
                            if isinstance(geometry, dict) and image is not None:
                                desired = {**geometry, **geometry_updates}
                                mode = self.tracker_panel.get_params().get("correction_mode", "clip")
                                edit_tracked_geometry(binding, desired, frame,
                                                      (image.shape[1], image.shape[0]), mode)
                                tracked_edit = True
                                recorded_fields = [k for k in params if k not in geometry_fields]
                                self.tracker_panel.set_tracking_binding(binding)
                    for k, v in params.items():
                        if tracked_edit and k in geometry_fields:
                            continue
                        if k == 'center':
                            s.center = list(v)
                        elif k == 'size':
                            s.size = list(v)
                        elif k == 'points':
                            s.points = v
                        else:
                            setattr(s, k, v)
                last_updated_shape = s
                updates_applied = True
                # P1-COLOR-09: 记录本次真正提交的数值字段（供关键帧录制）
                updated_fields.append((shape_id, recorded_fields))

        self.pending_mask_updates.clear()

        if updates_applied:
            layer.is_dirty = True; layer.mask_dirty = True
            self._maybe_record_mask_keyframes(layer, updated_fields)
            if graph_route is not None:
                # 图模式：活对象更新已完成（预览），patch 并入手势事务
                self._submit_window_attribute_patches(graph_route,
                                                      graph_patches)
            self.request_render()
            # UI Sync: Update sliders ONLY if the current active shape in UI was the one updated.
            # If multiple shapes were updated, the last one in the dict takes precedence for UI display.
            if last_updated_shape and (self.canvas.active_shape_id == last_updated_shape.id):
                if isinstance(last_updated_shape, MaskGroup):
                    self.mask_panel.set_params(0, last_updated_shape.opacity, 0, 0, 0)
                elif isinstance(last_updated_shape, MaskShape):
                    self.mask_panel.set_params(
                        last_updated_shape.feather,
                        last_updated_shape.opacity,
                        getattr(last_updated_shape, 'blur', 0),
                        getattr(last_updated_shape, 'clean_black', 0),
                        getattr(last_updated_shape, 'clean_white', 0)
                    )
                    self._sync_window_geometry_controls(last_updated_shape)
            self.canvas.update_mask_overlay(layer)
            self._trigger_autosave()

    # ------------------------------------------------------------------
    # N03: Qualifier 面板写入路由（图模式 typed handle / legacy 直写）
    # ------------------------------------------------------------------

    def on_qualifier_params_changed(self, params: dict) -> None:
        """Qualifier 面板参数变化：图编辑上下文经 qualifier 图命令（一次
        手势一次 undo），legacy 上下文直写引擎（N03 前唯一路径）。"""
        if getattr(self, "_color_stack_hydration_in_progress", False):
            return  # 程序化回填回流（同值幂等；不开事务不双写）
        graph_host = getattr(self, "color_graph_host", None)
        if graph_host is not None and graph_host.graph_edit_context_active():
            if self._route_qualifier_to_graph(graph_host, params):
                self._invalidate_video_render_params_if_playing()
                return
        self.engine.set_qualifier_params(params)
        self._invalidate_video_render_params_if_playing()

    def _tool_flush_timer(self):
        """工具域手势自冲刷定时器（吸管/部分滑条/防抖队列无 interaction
        包裹——400ms 静默后自动 flush，杜绝在途事务悬挂到下一手势）。"""
        from PyQt6.QtCore import QObject, QTimer
        timer = getattr(self, "_tool_graph_flush_timer", None)
        if timer is None:
            parent = self if isinstance(self, QObject) else None
            timer = QTimer(parent)
            timer.setSingleShot(True)
            timer.setInterval(400)
            timer.timeout.connect(self._flush_tool_gesture)
            self._tool_graph_flush_timer = timer
        return timer

    def _flush_tool_gesture(self) -> None:
        graph_host = getattr(self, "color_graph_host", None)
        if graph_host is not None and graph_host.grade_gesture_open():
            graph_host.end_grade_gesture()

    def _flush_qualifier_gesture(self) -> None:
        # 兼容别名（N03-b 期名称；测试直呼）
        self._flush_tool_gesture()

    def _route_qualifier_to_graph(self, graph_host, params: dict) -> bool:
        """Qualifier 手势图路由（与 ``_route_grading_to_graph`` 同型）。

        - 图侧：``set_node_qualifier`` merge patch 并入手势事务（面板
          ``interaction_ended`` 或 400ms 静默自冲刷 → 一次 command）；
          diff 基为权威 qualifier 投影（未变化的键不提交）。
        - 预览：legacy 引擎同值直写（活动层经 source_layer_id 对齐），
          flush 后投影收敛到同一值。
        - 返回 False：无法定位目标节点——回落 legacy 路径，手势不静默
          丢失；服务端拒绝（锁等）时返回 True（完全拒绝，不走 legacy
          绕过）。
        """
        node_id = graph_host.active_grade_node_id()
        if not node_id:
            return False
        if not isinstance(params, dict) or not params:
            graph_host.align_engine_active_layer(node_id)
            return True
        if not graph_host.grade_gesture_open():
            counter = int(getattr(self, '_graph_gesture_counter', 0)) + 1
            self._graph_gesture_counter = counter
            self._graph_gesture_tid = f"qualifier-{counter}"
            graph_host.begin_grade_gesture(self._graph_gesture_tid)
        current = graph_host.panel_qualifier_params() or {}
        patch = {key: value for key, value in params.items()
                 if current.get(key) != value}
        if patch:
            graph_host.submit_qualifier_change(
                node_id, patch, self._graph_gesture_tid)
        self._tool_flush_timer().start()
        graph_host.align_engine_active_layer(node_id)
        self.engine.set_qualifier_params(params)  # 同值预览通道
        return True

    # ------------------------------------------------------------------
    # LUT operations
    # ------------------------------------------------------------------

    def _graph_resource_route(self):
        """R10：图模式资源路由——图会话 READY 且有活动 Corrector 时返回
        正式命令适配器，否则 None（旧模式保留 legacy 行为）。"""
        controller = getattr(self, 'color_resource_controller', None)
        host = getattr(self, 'color_graph_host', None)
        coordinator = getattr(host, '_coordinator', None)
        from src.app.controllers.color_graph_controller import GraphSessionState
        if controller is None or coordinator is None or \
                coordinator.state is not GraphSessionState.READY:
            return None
        return controller

    def apply_lut_from_path(self, lut_path) -> None:
        """对当前图层应用指定路径的 LUT，成功后请求重绘与自动保存。

        R10：图模式经正式命令（注册为工程资源 → set_lut），legacy 引擎
        不再双写。

        Args:
            lut_path: LUT 文件（.cube 等）的绝对路径。
        """
        route = self._graph_resource_route()
        if route is not None:
            from src.shared.color.lut_resource_registry import (
                get_lut_resource_session,
            )
            results = get_lut_resource_session().import_files([str(lut_path)])
            if results and results[0].entry is not None:
                route.activate_resource(
                    {"id": results[0].entry.uri, "kind": "lut",
                     "available": True,
                     "resource_uri": results[0].entry.uri})
            return
        if self.engine.apply_lut(lut_path):
            self.request_render(); self._trigger_autosave()

    def on_lut_intensity_changed(self, intensity) -> None:
        """LUT 强度滑条变化：播放中走防抖渲染，暂停态直接重绘。

        R10：图模式经 set_lut_strength 命令（legacy 引擎不双写）。

        Args:
            intensity: 0.0-1.0 的 LUT 混合强度。
        """
        route = self._graph_resource_route()
        if route is not None:
            route.edit('set_lut_strength', {'strength': float(intensity),
                                            'phase': 'commit'})
            return
        self.engine.set_lut_intensity(intensity)
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_lut_reset(self) -> None:
        """重置当前图层的 LUT 并重绘。R10：图模式经 remove_lut 命令。"""
        route = self._graph_resource_route()
        if route is not None:
            route.edit('remove_lut', {})
            return
        self.engine.reset_lut(); self.request_render(); self._trigger_autosave()

    def on_lut_removed(self, lut_path: str) -> None:
        """LUT 面板移除某 LUT：若是当前图层激活的 LUT 则触发重置。

        Args:
            lut_path: 被移除的 LUT 文件路径。
        """
        layer = self.engine.get_active_layer()
        if layer and layer.active_lut_path == lut_path:
            self.on_lut_reset()

    def on_decompile_lut_requested(self, path) -> None:
        """LUT 反编译：把 LUT 逆向拆解为调色参数并刷新面板。

        AUD-2307：反编译期间不再 ``QApplication.processEvents()`` 刷 UI
        （同步重入会让用户在等待时关闭文件/切换页面，反编译随后继续写
        engine 产生竞态）；``decompile_lut`` 抛异常时恢复状态栏并记录
        完整堆栈，原先 timeout=0 的 "Decompiling..." 消息会永久残留。

        Args:
            path: 待反编译的 LUT 文件路径。
        """
        if self.engine.img_full_original is None: return

        self.status_bar.showMessage(f"Decompiling LUT: {os.path.basename(path)}...", 0)

        try:
            results = self.engine.decompile_lut(path)
        except Exception:  # noqa: BLE001 - UI 必须恢复状态栏而不是上抛异常
            logger.exception("LUT 反编译异常：%s", path)
            self.status_bar.showMessage("LUT Decompile Failed.", 5000)
            return

        if results:
            self.update_from_layer(self.engine.get_active_layer())
            self.request_render()
            self.status_bar.showMessage("LUT Decompiled Successfully!", 5000)
            self._trigger_autosave()
        else:
            self.status_bar.showMessage("LUT Decompile Failed.", 5000)

    # ------------------------------------------------------------------
    # Video Denoise (M5: 时域+空域，非破坏、实时)
    # ------------------------------------------------------------------

    def _push_video_denoise_params(self, params, **kwargs) -> None:
        """把视频降噪参数直发渲染器，失败记警告日志（AUD-2303）。

        仅用于 engine 尚无公共入口的渲染器专属能力（历史重置、调试
        叠加）：原先三处 ``except Exception: pass`` 静默吞错会让面板
        记录态与渲染器实际状态无日志地分歧，此通道统一收口并保留
        ``exc_info`` 堆栈供排查。

        Args:
            params: :class:`VideoDenoiseParams` 实例。
            **kwargs: 透传渲染器 setter 的附加参数（如 debug_overlay）。
        """
        renderer = getattr(self.engine, 'renderer', None)
        if renderer is None or not hasattr(renderer, 'set_video_denoise_params'):
            return
        try:
            renderer.set_video_denoise_params(params, **kwargs)
        except Exception:  # noqa: BLE001 - 参数下发失败降级为日志，不阻塞面板管线
            logger.warning("set_video_denoise_params 下发失败", exc_info=True)

    def on_video_denoise_changed(self, params_dict) -> None:
        """降噪面板视频模式参数变化：经 engine 统一入口记录并下发渲染器。"""
        from src.shared.video.denoise.params import VideoDenoiseParams

        params = VideoDenoiseParams.from_dict(params_dict or {})
        # AUD-2303：优先走 engine 公共入口（负责记录 video_denoise_params
        # 供项目保存，并把参数以带日志的方式推给渲染器），不再由 app 层
        # 越层直连 renderer 并吞掉失败。
        engine_entry = getattr(self.engine, 'set_video_denoise_state', None)
        if callable(engine_entry):
            engine_entry(params.to_dict())
        else:
            self.engine.video_denoise_params = params
            self._push_video_denoise_params(params)
        # 暂停/静态预览时立即重绘当前帧；播放中下一帧自动生效
        self.request_render()
        # M4.2: 暂停态开启时域时调度邻帧窗口（窗口重放需要邻帧数据）
        if getattr(params, 'temporal', None) is not None and params.temporal.is_active():
            schedule = getattr(self, '_schedule_video_denoise_window', None)
            if callable(schedule):
                schedule()

    def on_video_denoise_history_reset(self) -> None:
        """UI 请求重置时域历史（渲染器专属能力，engine 无公共入口）。"""
        renderer = getattr(self.engine, 'renderer', None)
        if renderer is not None and hasattr(renderer, 'reset_video_denoise_history'):
            try:
                renderer.reset_video_denoise_history()
            except Exception:  # noqa: BLE001 - AUD-2303：吞错必须留日志
                logger.warning("reset_video_denoise_history 失败", exc_info=True)
        self.request_render()

    def on_video_denoise_debug_overlay_toggled(self, enabled: bool) -> None:
        """M7: 运动置信度调试叠加开关（video_denoise_debug_overlay）。"""
        renderer = getattr(self.engine, 'renderer', None)
        params = getattr(self.engine, 'video_denoise_params', None)
        if renderer is None or params is None:
            return
        if not hasattr(renderer, 'set_video_denoise_params'):
            return
        self._push_video_denoise_params(params, debug_overlay=bool(enabled))
        self.request_render()

    # ------------------------------------------------------------------
    # Denoise
    # ------------------------------------------------------------------

    def _capture_bake_task_context(self) -> tuple:
        """发起 denoise/deblur 时捕获任务上下文（AUD-2305）。

        记录 (文件路径, 活动图层索引)。完成回调据此判定长耗时任务
        运行期间用户是否已打开新文件或切换图层，避免结果 ``set_image``
        到切换后的上下文、污染新文件 undo 栈、bake EXR 挂到错误工程。
        """
        return (getattr(self.engine, 'current_file_path', None),
                getattr(self.engine, 'active_layer_index', None))

    def _bake_task_context_matches(self, context: tuple) -> bool:
        """完成回调侧校验任务上下文是否仍然一致（AUD-2305）。"""
        return context is not None and context == self._capture_bake_task_context()

    def _discard_bake_result(self, kind: str, context: tuple) -> None:
        """上下文已变更：丢弃 denoise/deblur 结果并恢复面板（AUD-2305）。"""
        logger.warning(
            "%s 结果丢弃：任务运行期间图像上下文已变更（发起时 %r / 当前 %r）",
            kind, context, self._capture_bake_task_context())
        panel = self.denoise_panel if kind == "denoise" else self.deblur_panel
        panel.apply_btn.setEnabled(True)
        self.progress_bar.hide()
        self.status_bar.showMessage(
            f"{kind.capitalize()} discarded: image context changed", 5000)

    def on_denoise_requested(self, mode, params) -> None:
        """降噪面板请求应用降噪：准备图像并交给降噪引擎异步处理。

        Args:
            mode: 降噪模式标识（由降噪面板定义）。
            params: 降噪参数字典。
        """
        if self.engine.img_full_original is None:
            return

        self.status_bar.showMessage(f"Processing {mode} Denoise... (This may take a while)", 0)
        self.denoise_panel.apply_btn.setEnabled(False)

        # Show and start indeterminate progress
        self.progress_bar.setRange(0, 0) # Indeterminate mode
        self.progress_bar.show()

        # Get original image to apply denoise on
        image_to_process = self.engine.img_full_original.copy()

        # Convert to float32 0-1 as expected by denoise_engine
        if image_to_process.dtype == np.uint8:
            image_to_process = image_to_process.astype(np.float32) / 255.0
        elif image_to_process.dtype == np.uint16:
            image_to_process = image_to_process.astype(np.float32) / 65535.0

        if self.denoise_panel.engine.apply_denoise(image_to_process, mode, params):
            # AUD-2305：任务真正启动才记录上下文（busy 拒绝时不覆盖）
            self._denoise_task_ctx = self._capture_bake_task_context()

    def on_denoise_finished(self, denoised_img) -> None:
        """降噪完成：以 EXR source bake 形式落盘并保持 HDR 精度。

        推送 undo 状态，设置 Split View 参考图，把结果写回引擎
        （img_source_full 保留原图），并把 EXR 路径记入工程以便持久化。

        AUD-2305：先校验任务发起时的 (文件路径, 图层索引) 上下文，
        任务运行期间用户已切换文件/图层则丢弃结果（不写 undo、不落
        盘、不动 bake 引用）。直接调用而无发起上下文时按兼容语义应用。

        Args:
            denoised_img: BGR float32 0-1 的降噪结果。
        """
        # denoised_img is BGR float32 0-1. Preserve it to keep HDR/RAW precision!
        import uuid

        task_ctx = getattr(self, '_denoise_task_ctx', None)
        self._denoise_task_ctx = None
        if task_ctx is not None and not self._bake_task_context_matches(task_ctx):
            self._discard_bake_result("denoise", task_ctx)
            return

        self.engine.history.push_state(self.engine.img_full_original, self.engine.layers, self.engine.active_layer_index)

        # Set the current image (before denoise) as the reference for Split View comparison
        self.engine.set_reference_image(self.engine.img_full_original)

        # Generate previews from denoised image
        low = FileIO._resize_to_fit(denoised_img, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(denoised_img, FileIO.HIGH_RES_SIZE)

        # Update engine for live rendering (img_source_full stays as original)
        self.engine.set_image(denoised_img, high, low, is_new_file=False)

        # AUD-2306：工作显示已被降噪结果替换，crop 会话快照（若有）过期
        self._crop_preview_restore = None

        # Delete previous source bake if re-applying
        if self.engine.source_bake_file_name:
            FileIO.delete_baked_layer(self.engine.source_bake_file_name)

        # Save denoised result as EXR in baked cache (persisted via .topos project).
        # AUD-2304：denoised_img 已是 float32（denoise engine 契约），
        # FileIO.save_baked_layer 自带 dtype 归一，去掉控制器的全图
        # astype(float32) 拷贝（4K float32 一次 ~100MB+ 内存带宽）。
        bake_file_name = f"source_bake_{uuid.uuid4()}.exr"
        FileIO.save_baked_layer(bake_file_name, denoised_img)
        self.engine.source_bake_file_name = bake_file_name
        self.engine.source_bake_type = "denoise"

        self.layer_panel.set_source_bake_type("denoise")
        self.denoise_panel.apply_btn.setEnabled(True)
        self.denoise_panel.set_baked_state(True)
        self.progress_bar.hide()
        self.status_bar.showMessage("Denoise Applied & Saved to Project", 5000)
        self.request_render()

        # Trigger autosave to persist the source bake reference
        if self.current_file_path:
            self._trigger_autosave()

    def on_denoise_error(self, err_msg) -> None:
        """降噪失败：恢复按钮、隐藏进度条并弹出错误对话框。

        Args:
            err_msg: 降噪引擎上报的错误信息。
        """
        self._denoise_task_ctx = None
        self.denoise_panel.apply_btn.setEnabled(True)
        self.progress_bar.hide()
        self.status_bar.showMessage(f"Denoise Error: {err_msg}", 5000)
        QMessageBox.critical(self, "Denoise Error", err_msg)

    def on_denoise_reset_requested(self) -> None:
        """重置降噪：删除 source bake 并从真实原图恢复（保留 RAW 参数）。"""
        if not self.current_file_path or self.engine.img_source_full is None:
            return

        # AUD-2306：工作显示即将恢复为源图，crop 会话快照（若有）过期
        self._crop_preview_restore = None

        # ★ 保存当前 RAW 参数，防止重置降噪时丢失色彩空间设置
        saved_raw_params = None
        if hasattr(self.engine, 'current_raw_params'):
            saved_raw_params = self.engine.current_raw_params.copy()

        # 1. Delete source bake EXR file
        if self.engine.source_bake_file_name:
            FileIO.delete_baked_layer(self.engine.source_bake_file_name)
            self.engine.source_bake_file_name = None
            self.engine.source_bake_type = None

        # 2. Restore from true original (img_source_full is never modified by denoise)
        full = self.engine.img_source_full.copy()
        low = FileIO._resize_to_fit(full, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(full, FileIO.HIGH_RES_SIZE)

        # 3. Reload into engine (使用 is_new_file=False 避免重置 RAW 状态)
        self.engine.set_image(full, high, low, is_new_file=False)

        # ★ 恢复 RAW 参数并重新渲染
        if saved_raw_params and saved_raw_params.get('enabled', False):
            self.engine.on_raw_params_changed(saved_raw_params)

        self.layer_panel.set_source_bake_type(None)
        self.denoise_panel.set_baked_state(False)
        self.request_render()
        self.status_bar.showMessage("Denoise Reset to Original", 3000)

        # Trigger autosave to clear the source bake reference from project
        self._trigger_autosave()

    # ------------------------------------------------------------------
    # Deblur
    # ------------------------------------------------------------------

    def on_deblur_requested(self, mode, params) -> None:
        """去模糊面板请求应用 AI Deblur：准备图像并交给引擎异步处理。

        Args:
            mode: 去模糊模式标识。
            params: 去模糊参数字典。
        """
        if self.engine.img_full_original is None:
            return

        self.status_bar.showMessage("Processing AI Deblur... (This may take a while)", 0)
        self.deblur_panel.apply_btn.setEnabled(False)

        # Show and start indeterminate progress
        self.progress_bar.setRange(0, 0) # Indeterminate mode
        self.progress_bar.show()

        # Get original image to apply deblur on
        image_to_process = self.engine.img_full_original.copy()

        # Convert to float32 0-1
        if image_to_process.dtype == np.uint8:
            image_to_process = image_to_process.astype(np.float32) / 255.0
        elif image_to_process.dtype == np.uint16:
            image_to_process = image_to_process.astype(np.float32) / 65535.0

        if self.deblur_panel.engine.apply_denoise(image_to_process, mode, params):
            # AUD-2305：任务真正启动才记录上下文（busy 拒绝时不覆盖）
            self._deblur_task_ctx = self._capture_bake_task_context()

    def on_deblur_finished(self, deblurred_img) -> None:
        """去模糊完成：以 EXR source bake 形式落盘（流程同降噪）。

        AUD-2305：先校验任务发起时的 (文件路径, 图层索引) 上下文，
        任务运行期间用户已切换文件/图层则丢弃结果（不写 undo、不落
        盘、不动 bake 引用）。直接调用而无发起上下文时按兼容语义应用。

        Args:
            deblurred_img: BGR float32 0-1 的去模糊结果。
        """
        import uuid

        task_ctx = getattr(self, '_deblur_task_ctx', None)
        self._deblur_task_ctx = None
        if task_ctx is not None and not self._bake_task_context_matches(task_ctx):
            self._discard_bake_result("deblur", task_ctx)
            return

        self.engine.history.push_state(self.engine.img_full_original, self.engine.layers, self.engine.active_layer_index)
        self.engine.set_reference_image(self.engine.img_full_original)

        low = FileIO._resize_to_fit(deblurred_img, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(deblurred_img, FileIO.HIGH_RES_SIZE)

        self.engine.set_image(deblurred_img, high, low, is_new_file=False)

        # AUD-2306：工作显示已被去模糊结果替换，crop 会话快照（若有）过期
        self._crop_preview_restore = None

        # Delete previous source bake if re-applying
        if self.engine.source_bake_file_name:
            FileIO.delete_baked_layer(self.engine.source_bake_file_name)

        # Save deblurred result as EXR in baked cache (persisted via .topos project).
        # AUD-2304：deblurred_img 已是 float32，去掉冗余的全图 astype 拷贝。
        bake_file_name = f"source_bake_{uuid.uuid4()}.exr"
        FileIO.save_baked_layer(bake_file_name, deblurred_img)
        self.engine.source_bake_file_name = bake_file_name
        self.engine.source_bake_type = "deblur"

        self.layer_panel.set_source_bake_type("deblur")
        self.deblur_panel.apply_btn.setEnabled(True)
        self.deblur_panel.set_baked_state(True)
        self.progress_bar.hide()
        self.status_bar.showMessage("Deblur Applied & Saved to Project", 5000)
        self.request_render()

        # Trigger autosave to persist the source bake reference
        if self.current_file_path:
            self._trigger_autosave()

    def on_deblur_error(self, err_msg) -> None:
        """去模糊失败：恢复按钮、隐藏进度条并弹出错误对话框。

        Args:
            err_msg: 去模糊引擎上报的错误信息。
        """
        self._deblur_task_ctx = None
        self.deblur_panel.apply_btn.setEnabled(True)
        self.progress_bar.hide()
        self.status_bar.showMessage(f"Deblur Error: {err_msg}", 5000)
        QMessageBox.critical(self, "Deblur Error", err_msg)

    def on_deblur_reset_requested(self) -> None:
        """重置去模糊：删除 source bake 并从真实原图恢复（保留 RAW 参数）。"""
        if not self.current_file_path or self.engine.img_source_full is None:
            return

        # AUD-2306：工作显示即将恢复为源图，crop 会话快照（若有）过期
        self._crop_preview_restore = None

        # ★ 保存当前 RAW 参数，防止重置去模糊时丢失色彩空间设置
        saved_raw_params = None
        if hasattr(self.engine, 'current_raw_params'):
            saved_raw_params = self.engine.current_raw_params.copy()

        # 1. Delete source bake EXR file
        if self.engine.source_bake_file_name:
            FileIO.delete_baked_layer(self.engine.source_bake_file_name)
            self.engine.source_bake_file_name = None
            self.engine.source_bake_type = None

        # 2. Restore from true original (img_source_full is never modified by deblur)
        full = self.engine.img_source_full.copy()
        low = FileIO._resize_to_fit(full, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(full, FileIO.HIGH_RES_SIZE)

        # 3. Reload into engine (使用 is_new_file=False 避免重置 RAW 状态)
        self.engine.set_image(full, high, low, is_new_file=False)

        # ★ 恢复 RAW 参数并重新渲染
        if saved_raw_params and saved_raw_params.get('enabled', False):
            self.engine.on_raw_params_changed(saved_raw_params)

        self.layer_panel.set_source_bake_type(None)
        self.deblur_panel.set_baked_state(False)
        self.request_render()
        self.status_bar.showMessage("Deblur Reset to Original", 3000)

        # Trigger autosave to clear the source bake reference from project
        self._trigger_autosave()

    # ------------------------------------------------------------------
    # Crop
    # ------------------------------------------------------------------

    def on_crop_params_changed(self, params) -> None:
        """裁切参数（rect/rotation/flip 等）变化：透传给画布做预览。

        Args:
            params: 裁切面板的参数字典。
        """
        self.canvas.set_crop_params(params)

    def _stash_crop_preview_context(self) -> None:
        """进入 crop 预览前保存可恢复的工作显示上下文（AUD-2306）。

        crop 会话会把工作显示（img_high/img_low 显示金字塔）替换为未
        bake 的源图。仅当确有会丢失的工作态时才保存快照：存在
        denoise/deblur source bake，或此前已应用过裁切
        （``img_full_original`` 已 ≠ 源图）。全新打开、未 bake、未裁切
        的图像源图与工作显示同内容，无需恢复。

        快照持有显示金字塔的引用（``set_image`` 每次都生成新数组，
        不会原地修改旧数组），退出会话时原样回传，恢复零 resize、
        零 EXR 读盘。
        """
        engine = self.engine
        self._crop_preview_restore = None
        if engine is None or getattr(engine, 'img_source_full', None) is None:
            return

        bake_name = getattr(engine, 'source_bake_file_name', None)
        crop_params = getattr(engine, 'crop_params', None) or {}
        crop_applied = bool(crop_params) and (
            bool(set(crop_params) - {"rect", "rotation"})
            or tuple(crop_params.get("rect", (0.0, 0.0, 1.0, 1.0)))
            != (0.0, 0.0, 1.0, 1.0)
            or float(crop_params.get("rotation", 0.0) or 0.0) != 0.0
        )
        high = getattr(engine, 'img_high', None)
        if not (bake_name or crop_applied) or high is None:
            return

        self._crop_preview_restore = {
            'full': getattr(engine, 'img_full_original', None),
            'high': high,
            'low': getattr(engine, 'img_low', None),
            'file': getattr(engine, 'current_file_path', None)
            or getattr(self, 'current_file_path', None),
        }

    def _restore_crop_preview_context(self) -> None:
        """退出 crop 会话（含取消/空裁切框）时恢复进入前的工作显示。

        AUD-2306：原先关闭分支只弹消息，视口停留在未 bake 的源图，
        而 engine.source_bake_* 与 layer_panel 的 bake 标记不变——
        显示态与持久化态分裂，保存工程再载入又变回 bake 版。裁切
        ``apply`` 路径由 :meth:`on_crop_applied` 消费新状态并作废快照；
        会话期间切换了文件则旧快照不再适用，直接丢弃。
        """
        stash = getattr(self, '_crop_preview_restore', None)
        self._crop_preview_restore = None
        if not stash:
            return

        engine = self.engine
        current = getattr(engine, 'current_file_path', None) \
            or getattr(self, 'current_file_path', None)
        if stash.get('file') != current:
            logger.warning("crop 会话期间文件已切换，放弃恢复进入前的工作显示")
            return

        try:
            engine.set_image(stash.get('full'), stash.get('high'),
                             stash.get('low'), is_new_file=False)
        except Exception:  # noqa: BLE001 - 恢复失败只记日志，不阻断退出流程
            logger.warning("恢复 crop 会话前的工作显示失败", exc_info=True)
            return
        self.update_canvas()
        self.request_render()

    def on_crop_toggled(self, active) -> None:
        """Handle crop tool activation/deactivation.

        AUD-2306：激活时先保存当前工作显示快照，再把源图载入用于重
        调裁切框；退出（取消/空裁切框）时恢复快照，保证视口回到进入
        crop 前的 bake/工作图像，而不是停留在未 bake 的原图。
        """
        self.canvas.set_crop_mode(active)
        if active:
            # Auto-disable brush tool when activating crop
            self.mask_panel.btn_brush.setChecked(False)
            self.mask_panel.btn_sam.setChecked(False)
            self.qualifier_panel.set_picker_active(False)

            # When activating crop tool, show the original image for re-adjusting crop box
            if self.engine and self.engine.img_source_full is not None:
                # AUD-2306：换源图前保存当前工作显示（退出时恢复）
                self._stash_crop_preview_context()
                # Reload source image to show original for crop adjustment
                high = FileIO._resize_to_fit(self.engine.img_source_full, FileIO.HIGH_RES_SIZE)
                low = FileIO._resize_to_fit(self.engine.img_source_full, FileIO.LOW_RES_SIZE)
                self.engine.set_image(self.engine.img_source_full, high, low, is_new_file=False)
                self.update_canvas()
            self.status_bar.showMessage("Crop tool activated (Press C or click button to toggle)", 3000)
        else:
            self._restore_crop_preview_context()
            self.status_bar.showMessage("Crop tool deactivated", 2000)

    def on_crop_reset_requested(self) -> None:
        """Reset crop rect to full image bounds."""
        # Activate crop tool if not already active
        if not self.crop_panel.crop_active:
            self.crop_panel.set_crop_active(True)
        self.canvas.set_crop_mode(True)
        # Reset crop rect to full image bounds
        self.canvas.reset_crop_to_image()
        self.status_bar.showMessage("Crop rect reset to full image", 2000)

    def on_crop_reset_all_requested(self) -> None:
        """Reset all crop parameters including rect, rotation, and flips."""
        # Activate crop tool if not already active
        if not self.crop_panel.crop_active:
            self.crop_panel.set_crop_active(True)
        # Reset crop rect to full image
        self.canvas.reset_crop_to_image()
        self.canvas.set_crop_mode(True)
        # Reset rotation to 0
        self.crop_panel.rot_slider.setValue(0)
        self.status_bar.showMessage("All crop parameters reset", 2000)

    def on_crop_applied(self, panel_params=None) -> None:
        """确认裁切：把画布裁切框数据提交引擎并重置视图。

        读取 ``canvas.get_crop_data`` 组装最终参数（rect/rotation/
        center/size），调用 ``engine.apply_crop`` 后确认裁切、按新
        尺寸重置场景并自动 fit 到窗口。

        Args:
            panel_params: 面板侧参数（当前未使用，数据以画布为准）。
        """
        crop_data = self.canvas.get_crop_data()
        if crop_data:
            final_params = {
                "rect": crop_data["rect"], # Legacy/Fallback
                "rotation": crop_data["rotation"],
                "center": crop_data["center"], # (norm_x, norm_y)
                "size": crop_data["size"],     # (norm_w, norm_h)
            }
            self.engine.apply_crop(final_params)

            # AUD-2306：裁切已应用，engine 已重建为新工作态；随后的
            # crop_toggled(False) 不得把旧快照恢复回去（作废恢复快照）
            self._crop_preview_restore = None

            # 【关键修复】确认裁切：清零预览角度，防止二次旋转 Bug
            self.canvas.confirm_crop()

            # Ensure canvas updates its scene rect to match the new image dimensions
            # treating it as a new source to fit window and reset view
            self.update_canvas(is_new_source=True)
            self.request_render()

            # Lightroom-style: 裁切应用后自动居中填满视图
            self.canvas.fit_to_window()

            # Note: crop_panel.set_crop_active(False) is already called in _set_crop_active_internal
            # to avoid infinite loop, so we don't call it here again
            self.status_bar.showMessage("Crop applied", 2000)

    # ------------------------------------------------------------------
    # Masks panel visibility
    # ------------------------------------------------------------------

    def _on_masks_panel_visibility_changed(self, visible: bool) -> None:
        """Handle Mask panel visibility change.

        Task 3: 当 Mask 面板隐藏时，重置工具状态并更新 overlay 可见性。

        修正（达芬奇行为）：窗口形状的显示属于视图状态，不随 Masks 面板
        关闭而消失——切到 Tracker 面板时形状必须保持可见、可点选，否则
        跟踪器拿不到目标。隐藏面板只取消进行中的绘制工具；打开面板时
        确保形状显示开启。
        """
        if visible:
            # 打开 Masks 面板：确保画布形状可见（用户可能在视口菜单关过）
            self.canvas.set_overlay_visible(True)
        else:
            # 只重置工具状态；保留形状 overlay 与选中状态
            self.canvas.cancel_active_tools()

    # ------------------------------------------------------------------
    # Plugin
    # ------------------------------------------------------------------

    def add_plugin_to_current_layer(self, plugin_id, plugin_name) -> None:
        """
        Add a plugin instance to the currently selected layer.

        Args:
            plugin_id: The plugin identifier (filename without extension)
            plugin_name: Display name for the plugin
        """
        # R10：图模式经正式资源命令（add_effect → typed target → 图命令；
        # legacy Layer 不再双写）。旧模式保留 legacy 行为。
        # 注意：import 必须在分支外——分支内局部 import 会把名字绑定到
        # 整个函数作用域，legacy 路径将 UnboundLocalError。
        from src.features.color.core.plugin_manager import get_plugin_manager
        route = self._graph_resource_route()
        if route is not None:
            metadata = get_plugin_manager().get_plugin(plugin_id) or {}
            route.activate_effect(
                {"id": plugin_id, "kind": "effect", "name": plugin_name,
                 "available": True, "parameters": metadata.get("params", [])})
            return

        # Check if an active layer exists
        if self.engine.active_layer_index < 0:
            self.status_bar.showMessage("No layer selected. Please select a layer first.", 3000)
            return

        # Get the current layer
        layer = self.engine.layers[self.engine.active_layer_index]

        # Fetch plugin defaults
        plugin = get_plugin_manager().get_plugin(plugin_id)
        if not plugin:
            self.status_bar.showMessage(f"Plugin not found: {plugin_id}", 3000)
            return

        # Create default params from plugin metadata
        default_params = {}
        for param in plugin.get('params', []):
            # Use direct key access to ensure we get the actual default value
            default_params[param['var_name']] = param.get('default', 0.0)
            # Debug: print default values
            logger.info(f"Setting default for {param['var_name']}: {default_params[param['var_name']]}")

        # Add plugin to the layer
        layer.add_plugin(plugin_id, default_params)

        # Update UI（D2：旧 PluginPanel 已删除——Effects dock 宿主新浏览器）
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.dock_plugin.setVisible(True)
        self.dock_plugin.raise_()
        self.request_render()

        self.status_bar.showMessage(f"Added effect: {plugin_name}", 3000)
        self._trigger_autosave()

    # D2（N03-e）：_on_ai_plugin_process_requested 随旧 PluginPanel 实例删除——
    # AI 效果处理入口由 Effects 浏览器的实例操作承载（R03 命令路径）。
