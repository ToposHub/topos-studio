"""LayerOpsMixin -- layer/render/grading/undo handlers extracted from MainWindow.

职责（Responsibilities）:
    - 图层操作控制层：图层增删/选择/移动/显隐/重命名/复制粘贴/栅格化，
      面板参数 → 引擎的调色入口（grading 12 元组、curves/HSL 曲线、
      mixer/warper/effects/film grain/RAW），渲染请求调度
      （``request_render`` 的 transient/full 升级规则、播放期防抖）、
      帧就绪/渲染完成回调（GPU scope demand、遮罩覆盖层刷新），以及
      undo/redo（Composite 页优先、SAM 点撤销拦截）与各类 reset。
    - 本 Mixin 不持有状态模型：所有 ``self.*`` 属性引用解析到宿主
      MainWindow 实例（engine、grading_panel、layer_panel、canvas 等）。

与引擎/页面的边界（Boundary）:
    - 像素与参数模型归 ``engine``（ImageEngine/grade_keyframes）：本层
      只调用 ``engine.grade_image``/``set_*_params`` 等入口并处理
      播放中/暂停态的渲染策略（invalidate cached params + debounce），
      不直接操作 OpenGL 或着色器。
    - UI 归各面板：本层经 ``update_from_layer`` 把图层参数回写到
      grading/curves/mixer/effects/qualifier/LUT/warper 等面板，
      并同步 tangent 调色台桥。

信号协议（Signal protocol）:
    - 入站：layer_panel 的 selected/added/removed/moved/visibility/
      renamed 信号、各调色面板的 params_changed/changed 信号、
      renderer 的 frame_ready、画布取色器 color_picked。
    - 出站：无自定义 Qt 信号；通过 ``request_render``/
      ``_debounce_grading_render`` 驱动渲染，通过
      ``_trigger_autosave`` 与 ``_refresh_grade_keyframe_ui`` 触发
      持久化与关键帧 UI 刷新。
"""

from __future__ import annotations

import copy as copy_mod
import logging
import os
import time

from typing import Any, Optional

import cv2
import numpy as np

from PyQt6.QtWidgets import QMessageBox, QGraphicsScene
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from src.features.color.core.layer import MaskShape, MaskGroup


def _scope_diag():
    """S01 分段诊断（可关闭；计数器 + 最后上下文，无逐帧日志）。

    惰性取模块属性（测试可替换进程级单例）。"""
    from src.features.color.core.scope_diagnostics import scope_diagnostics
    return scope_diagnostics
from src.shared.io.file_io import FileIO
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class _CacheCleanupWorker(QRunnable):
    """AUD-2909: 在线程池执行孤立 EXR 缓存扫描/删除。

    ``FileIO.cleanup_orphaned_cache`` 会遍历整个缓存目录；多层 4K 工程的
    缓存目录很大时，在 UI 线程同步执行会造成可感知卡顿（掉帧/无响应观感）。
    """

    class _Signals(QObject):
        finished = pyqtSignal()

    def __init__(self, active_files: Any, parent: Optional[QObject] = None) -> None:
        super().__init__()
        self._active_files = active_files
        self.signals = self._Signals(parent)

    def run(self) -> None:
        try:
            FileIO.cleanup_orphaned_cache(self._active_files)
        except Exception:
            logger.warning("orphaned baked cache cleanup failed", exc_info=True)
        self.signals.finished.emit()


class _AiPluginRestoreWorker(QRunnable):
    """AUD-2909: 在线程池逐个加载 AI 插件 EXR 结果，逐条回主线程提交上传。

    EXR 磁盘读取是重 I/O；BGR→RGB 转换（纯 numpy/cv2，无 Qt/GPU 对象）
    也留在本线程，主线程 slot 只做缓存写入与渲染命令提交。
    """

    class _Signals(QObject):
        #: (layer_id, instance_id, BGR ndarray)——id 均为引擎侧的不透明值
        #: （Layer.id 是 str uuid），信号用 object 承载，不做类型强转。
        one_ready = pyqtSignal(object, object, object)
        finished = pyqtSignal()

    def __init__(self, requests: Any, parent: Optional[QObject] = None) -> None:
        super().__init__()
        self._requests = list(requests)
        self.signals = self._Signals(parent)

    def run(self) -> None:
        for layer_id, instance_id, filename in self._requests:
            try:
                result_array = FileIO.load_ai_plugin_result(filename)
            except Exception:
                logger.warning(
                    "AI plugin result load failed: %s", filename, exc_info=True)
                result_array = None
            if result_array is not None:
                self.signals.one_ready.emit(layer_id, instance_id, result_array)
        self.signals.finished.emit()


class LayerOpsMixin:
    """Mixin providing layer, render, grading, and undo/redo controller methods.

    Intended to be mixed into MainWindow so that all ``self.*`` attribute
    references resolve against the MainWindow instance.
    """

    # ------------------------------------------------------------------
    # Layer update / selection
    # ------------------------------------------------------------------

    def update_from_layer(self, layer) -> None:
        """把指定图层的全部调色参数回写到各调色面板（单向同步）。

        涵盖 grading 参数、curves/HSL 曲线、mixer/effects/soft_clip/
        qualifier/LUT/warper、最后一个遮罩的参数及分辨率信息，并同步
        tangent 调色台。

        Args:
            layer: 目标图层对象（None 时直接返回）。
        """
        if not layer: return
        p = layer.grading_params
        self.grading_panel.set_params(
            p["lift"], p["gamma"], p["gain"], p["exposure"], p["temp"],
            p["tint"], p["contrast"], p["shadow"], p["saturation"],
            p.get("vibrance", 0), p.get("mid_detail", 0), p.get("hue_shift", 0),
            highlights=p.get("highlights", 0),
        )
        tonal_panel = getattr(self.grading_panel, 'tonal_saturation_panel', None)
        if tonal_panel is not None:
            tonal_panel.set_params(
                p.get("lift_saturation", 0),
                p.get("gamma_saturation", 0),
                p.get("gain_saturation", 0))
        tangent_bridge = getattr(self, "tangent_color_bridge", None)
        if tangent_bridge is not None:
            tangent_bridge.sync_from_panel()

        if layer.curves_data:
            self.curves_widget.set_curves_data(layer.curves_data)
        elif layer.curves_lut is not None:
            self.curves_widget.set_lut(layer.curves_lut)
        else:
            self.curves_widget.reset(emit_changed=False)

        # 曲线面板侧栏（四参数柔化裁切 + Edit 强度）：随图层切换整体回填，
        # 草稿不跨目标泄漏（set_layer_state 屏蔽一切回写）
        curves_panel = getattr(self, "curves_panel", None)
        if curves_panel is not None:
            curves_panel.set_layer_state({
                "curves_soft_clip": getattr(layer, "curves_soft_clip", None) or {},
                "curves_edit": getattr(layer, "curves_edit", None) or {},
            })

        # HSL Curve
        if layer.hsl_curve_data:
            self.hsl_curves_panel.set_curves_data(layer.hsl_curve_data)
        elif layer.hsl_curve_lut is not None:
            # If only LUT exists, convert to curve data (fallback)
            self.hsl_curves_panel.reset(emit_changed=False)
        else:
            self.hsl_curves_panel.reset(emit_changed=False)

        self.mixer_panel.set_params(layer.mixer_params)
        self.effects_panel.set_params_dict(layer.effects_params)
        # 旧 SoftClipPanel 已删（任务 9）：旧 8 参数 soft_clip_params 保留
        # 在图层上供渲染/工程兼容，无面板回显（新 UI 为 curves_panel 侧栏，
        # 已在上方 set_layer_state 回填）
        self.qualifier_panel.set_params(layer.qualifier_params)
        self.lut_panel.set_selected_lut(layer.active_lut_path)
        self.lut_panel.set_intensity(layer.lut_intensity)
        self.warper_panel.set_params(layer.warper_params)
        if layer.mask_shapes:
            s = layer.mask_shapes[-1]
            if isinstance(s, MaskGroup):
                self.mask_panel.set_params(0, s.opacity, 0, 0, 0)
            elif isinstance(s, MaskShape):
                self.mask_panel.set_params(s.feather, s.opacity, getattr(s, 'blur', 0), getattr(s, 'clean_black', 0), getattr(s, 'clean_white', 0))
        if self.engine.img_full_original is not None:
             h, w = self.engine.img_full_original.shape[:2]
             self.info_panel.update_metadata({"Resolution": f"{w} x {h}"})

    def on_layer_selected(self, index) -> None:
        """图层面板选中变化：切换引擎激活层并回填所有面板与画布。"""
        self.mask_panel.btn_brush.setChecked(False)
        self.mask_panel.btn_sam.setChecked(False)
        self.engine.set_active_layer(index)
        layer = self.engine.get_active_layer()
        self.update_from_layer(layer)
        if layer: self.mask_panel.update_shapes_list(layer.mask_shapes)

        # Update plugin panel content (dock visibility is controlled by user via Layout menu)
        self.plugin_panel.set_layer(layer)

        self.update_canvas()

    def _push_layer_history(self) -> None:
        """结构/重置类图层操作前压入撤销快照（AUD-2903）。

        图层增/删/移/显隐/重命名与全局重置此前全程不压栈：Ctrl+Z 会跳过
        结构变更回退到更早快照（如删层后撤销连带回滚更早的调色），而
        ``_trigger_autosave`` 已把这些不可撤销的变更持久化，撤销粒度与
        磁盘状态脱节。与 ``on_layer_reset`` 对齐，统一在变更前压栈。
        ``copy_image=False``：这些操作不触碰源像素（源帧按约定不可变），
        只封存图层序列化状态，降低快照成本。
        """
        engine = self.engine
        history = getattr(engine, 'history', None)
        if history is None or getattr(engine, 'img_full_original', None) is None:
            return  # 无图像会话：push_state 本身也是 no-op，这里显式跳过
        history.push_state(
            engine.img_full_original,
            engine.layers,
            engine.active_layer_index,
            copy_image=False,
        )

    def on_layer_added(self) -> None:
        """添加新图层：更新图层面板、选中并触发自动保存。"""
        # AUD-2903：结构变更前压栈，使新增图层可撤销。
        self._push_layer_history()
        self.engine.add_layer()
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.on_layer_selected(self.engine.active_layer_index)
        self._trigger_autosave()

    def on_layer_removed(self, index) -> None:
        """删除指定索引的图层并刷新选中态。"""
        # 引擎对最后一层拒绝删除（静默 no-op）：仅在实际会删除时压栈，
        # 避免产生一个"撤销后无任何变化"的空步骤（AUD-2903）。
        if len(self.engine.layers) > 1 and 0 <= index < len(self.engine.layers):
            self._push_layer_history()
        self.engine.remove_layer(index)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.on_layer_selected(self.engine.active_layer_index)
        self._trigger_autosave()

    def on_layer_moved(self, old_idx, new_idx) -> None:
        """拖拽调整图层顺序：搬移图层并把激活索引移到新位置。"""
        if 0 <= old_idx < len(self.engine.layers) and 0 <= new_idx < len(self.engine.layers):
            # AUD-2903：顺序调整前压栈，使图层移动可撤销。
            self._push_layer_history()
            layer = self.engine.layers.pop(old_idx)
            self.engine.layers.insert(new_idx, layer)
            self.engine.active_layer_index = new_idx
            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
            self.request_render()
            self._trigger_autosave()

    def on_layer_visibility_changed(self, index, visible) -> None:
        """图层可见性开关：写回图层属性并重绘。"""
        if 0 <= index < len(self.engine.layers):
            # AUD-2903：显隐切换前压栈（仅在值真正变化时，避免程序化
            # no-op 调用产生空撤销步骤）。
            if getattr(self.engine.layers[index], 'visible', None) != visible:
                self._push_layer_history()
            self.engine.layers[index].visible = visible
            self.request_render()
            self._trigger_autosave()

    def on_layer_renamed(self, index, name) -> None:
        """图层重命名：写回图层名称并触发自动保存。"""
        if 0 <= index < len(self.engine.layers):
            # AUD-2903：重命名前压栈（仅值变化时）。
            if getattr(self.engine.layers[index], 'name', None) != name:
                self._push_layer_history()
            self.engine.layers[index].name = name
            self._trigger_autosave()

    # ------------------------------------------------------------------
    # Layer Copy/Paste/Duplicate
    # ------------------------------------------------------------------

    def on_layer_copy(self, index: int = None) -> None:
        """复制当前图层的调色参数到剪贴板。"""
        if index is None:
            index = self.engine.active_layer_index
        if not (0 <= index < len(self.engine.layers)):
            return
        self.engine.copy_layer(index)
        self.status_bar.showMessage(f"已复制图层 '{self.engine.layers[index].name}' 的参数", 2000)

    def on_layer_paste(self, index: int = None) -> None:
        """将剪贴板中的图层参数粘贴到当前图层。"""
        if index is None:
            index = self.engine.active_layer_index
        if self.engine.paste_layer(index):
            self.status_bar.showMessage(f"已粘贴图层参数到 '{self.engine.layers[index].name}'", 2000)
            self.update_from_layer(self.engine.get_active_layer())
            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
            # P1-COLOR-16：粘贴修改了调色参数，与 duplicate 一致触发自动保存
            # （此前仅靠面板刷新间接保存，粘贴后未保存即退出会丢参数）。
            self._trigger_autosave()
        else:
            self.status_bar.showMessage("剪贴板为空，无法粘贴", 2000)

    def on_layer_duplicate(self, index: int = None) -> None:
        """复制当前图层并创建新图层。"""
        if index is None:
            index = self.engine.active_layer_index
        new_layer = self.engine.duplicate_layer(index)
        if new_layer:
            self.status_bar.showMessage(f"已复制图层为 '{new_layer.name}'", 2000)
            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
            self.on_layer_selected(self.engine.active_layer_index)
            self._trigger_autosave()

    # ------------------------------------------------------------------
    # Apply Grade from Library / Presets
    # ------------------------------------------------------------------

    def _render_current_video_frame_for_grading(self) -> tuple[bool, int]:
        """Refresh the current video frame through the active video pipeline.

        The current EnhancedVideoPlaybackEngine no longer exposes the old
        ``video_controller.decoder`` object. Its supported synchronous Color
        path is ``render_static_preview_frame()``, which resolves the exact
        source frame and updates ImageEngine before the grade is rendered.
        """
        video_controller = getattr(self, 'video_controller', None)
        if video_controller is None:
            return False, 0

        current_frame = int(getattr(video_controller, 'current_frame', 0) or 0)
        render_static = getattr(video_controller, 'render_static_preview_frame', None)
        if callable(render_static):
            try:
                return bool(render_static(current_frame)), current_frame
            except Exception:
                logger.exception(
                    "Failed to render current video frame %s for grading",
                    current_frame,
                )
                return False, current_frame

        # Compatibility fallback for older video adapters that still expose a
        # CPU decoder. The active v3 engine does not use this branch.
        decoder = getattr(video_controller, 'decoder', None)
        get_frame = getattr(decoder, 'get_frame', None)
        if not callable(get_frame):
            return False, current_frame
        try:
            frame = get_frame(current_frame)
            if frame is None:
                return False, current_frame
            process_frame = getattr(video_controller, '_process_decoded_frame', None)
            if not isinstance(frame, np.ndarray) and callable(process_frame):
                frame = process_frame(frame)
            if not isinstance(frame, np.ndarray):
                return False, current_frame
            self.engine.set_image(
                frame,
                frame,
                frame,
                is_new_file=False,
                is_video=True,
                video_path=getattr(video_controller, 'current_path', None),
            )
            self.engine.render_pipeline(render_reason='grading')
            return True, current_frame
        except Exception:
            logger.exception(
                "Failed to refresh legacy video frame %s for grading",
                current_frame,
            )
            return False, current_frame

    def on_apply_grade_from_library(self, source_path: str) -> None:
        """从 Library 中键点击应用调色效果到当前图片/视频。

        类似达芬奇的 "Apply Grade" 功能：加载源图片/视频的 .topos 项目文件，
        将源项目的所有图层（含调色参数、曲线、遮罩、非 AI 插件）应用到当前图片。
        AI 蒙版、AI 效果、栅格化数据会被跳过（计算密集且依赖源文件）。

        Args:
            source_path: 源文件的路径（图片或视频）
        """
        # 检查当前是否已加载图片/视频
        is_video_mode = getattr(self, 'current_file_is_video', False)
        source_is_video = source_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.flv', '.wmv', '.webm'))

        logger.info(f"Grade] 目标模式: {'视频' if is_video_mode else '图像'}, 源文件: {os.path.basename(source_path)} ({'视频' if source_is_video else '图像'})")

        if not is_video_mode:
            if self.engine.get_current_image() is None:
                self.status_bar.showMessage(I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return
        else:
            if not self.video_controller or not self.video_controller.is_video_loaded():
                self.status_bar.showMessage(I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return

        # 加载源文件的调色项目
        source_topos = self.save_router._get_project_path_for_file(source_path)
        project_data = self.save_router.load_single_asset_data(source_topos) if source_topos else None
        if not project_data:
            self.status_bar.showMessage(
                I18N.tr("status.no_grade_found", f"未找到调色数据: {os.path.basename(source_path)}"), 3000)
            return

        # 提取源 layers
        source_layers = None
        source_active_idx = 0
        if isinstance(project_data, dict):
            source_layers = project_data.get('layers')
        elif isinstance(project_data, list):
            source_layers = project_data

        if not source_layers or len(source_layers) == 0:
            self.status_bar.showMessage(
                I18N.tr("status.no_grade_found", f"未找到调色数据: {os.path.basename(source_path)}"), 3000)
            return

        logger.info(f"Grade] 找到 {len(source_layers)} 个图层，开始应用全部调色...")

        # 保存历史状态（用于撤销）- 仅图像模式
        if not is_video_mode and self.engine.img_full_original is not None:
            self.engine.history.push_state(
                self.engine.img_full_original,
                self.engine.layers,
                self.engine.active_layer_index
            )

        # ---- 深拷贝源图层并剥离 AI 数据 ----
        new_layers = []
        for src_layer in source_layers:
            layer = copy_mod.deepcopy(src_layer)

            # 剥离 AI 蒙版
            layer.ai_matte = None
            layer.ai_matte_enabled = True  # 保留开关，用户可重新计算

            # 剥离栅格化数据
            layer.is_baked = False
            layer.baked_file_name = None
            layer.baked_texture_id = None

            # 剥离 AI 插件（仅保留 dctl 插件）
            layer.plugins = [p for p in layer.plugins if p.get("type", "dctl") == "dctl"]

            # 剥离 BITMAP 类型遮罩（AI 生成的位图蒙版）和 BRUSH 遮罩（依赖源图片内容）
            from src.features.color.core.layer import MaskShape
            layer.mask_shapes = [s for s in layer.mask_shapes
                                 if s.type not in (MaskShape.BITMAP, MaskShape.BRUSH)]

            new_layers.append(layer)

        # 替换引擎的图层
        self.engine.layers = new_layers
        self.engine.active_layer_index = min(source_active_idx, len(new_layers) - 1)

        # 为新图层对象重新提交 GPU 辅助纹理（LUT、Warper）
        # AUD-2900：layer_id 必须用 layer.id（UUID 字符串）——渲染端按
        # layer.get('id') 从 layer_textures 取纹理，REMOVE_LAYER/CLEAR_AUX_TEX
        # 也按 UUID 清理；id(layer)（内存地址）与其永不相等，会导致 LUT/Warper
        # 静默不生效且产生无法回收的孤儿 GPU 纹理。
        from src.features.color.core.async_renderer import RenderCommand
        for layer in new_layers:
            l_id = layer.id
            # LUT 3D
            if layer.active_lut_path and self.engine.renderer:
                self.engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': l_id, 'type': 'lut3d', 'path': layer.active_lut_path
                })
            # Warper 网格
            if layer.warper_params is not None and self.engine.renderer:
                h, w, c = layer.warper_params.shape
                self.engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': l_id, 'type': 'warper',
                    'data': layer.warper_params.astype(np.float32).tobytes(),
                    'size': (w, h)
                })

        # 同步所有 UI 面板
        active_layer = self.engine.get_active_layer()
        if active_layer:
            self.update_from_layer(active_layer)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        if active_layer:
            self.plugin_panel.set_layer(active_layer)

        # 立即刷新曲线/HSL 曲线 LUT（跳过节流延迟）
        self.curves_widget.flush()
        if self.hsl_curves_panel.hsl_curves_widget:
            self.hsl_curves_panel.hsl_curves_widget.flush()

        # 触发重新渲染和自动保存
        if is_video_mode:
            # 视频模式：暂停视频，重新渲染当前帧（应用新的调色参数）
            was_playing = self.video_controller.is_playing
            current_frame_num = int(getattr(self.video_controller, 'current_frame', 0) or 0)
            logger.info(f"Grade] 视频模式：暂停播放，重新渲染帧 {current_frame_num}")

            if was_playing:
                self.video_controller.stop_playback()

            # 通过当前 v3 视频管线获取并渲染精确帧。
            rendered, _ = self._render_current_video_frame_for_grading()
            if rendered:
                # 更新 Canvas
                self.update_canvas(is_new_source=False)
                self.video_controller.frame_changed.emit(current_frame_num)

            # 如果之前在播放，恢复播放
            if was_playing:
                # 等待一帧让渲染完成，然后恢复播放
                logger.info("Grade] 等待渲染完成，准备恢复播放...")
                QTimer.singleShot(100, lambda: self.video_controller.start_playback())
        else:
            # 图像模式：正常渲染
            self.request_render()

        self._trigger_autosave()

        source_name = os.path.basename(source_path)
        source_type = "视频" if source_is_video else "图片"
        self.status_bar.showMessage(
            I18N.tr("status.grade_applied", f"已应用{source_type}调色: {source_name}"), 3000)

    def on_apply_grade_from_preset(self, topos_path: str) -> None:
        """从 Presets 面板应用调色效果到当前图片/视频。

        加载 .topos 项目文件，将预设的所有图层（含调色参数、曲线、遮罩、非 AI 插件）
        应用到当前图片。AI 蒙版、AI 效果、栅格化数据、画笔遮罩会被剥离。

        Args:
            topos_path: .topos 预设文件的路径
        """
        is_video_mode = getattr(self, 'current_file_is_video', False)

        if not is_video_mode:
            if self.engine.get_current_image() is None:
                self.status_bar.showMessage(
                    I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return
        else:
            if not self.video_controller or not self.video_controller.is_video_loaded():
                self.status_bar.showMessage(
                    I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return

        # 加载 .topos 预设项目
        project_data = self.save_router.load_single_asset_data(topos_path)
        if not project_data:
            self.status_bar.showMessage(
                f"无法加载预设: {os.path.basename(topos_path)}", 3000)
            return

        source_layers = project_data.get('layers')
        source_active_idx = project_data.get('active_layer_index', 0)
        if not source_layers:
            self.status_bar.showMessage(
                f"预设无调色数据: {os.path.basename(topos_path)}", 3000)
            return

        # 保存历史状态（用于撤销）
        if not is_video_mode and self.engine.img_full_original is not None:
            self.engine.history.push_state(
                self.engine.img_full_original,
                self.engine.layers,
                self.engine.active_layer_index
            )

        # ---- Deep copy 并剥离 AI 数据 ----
        new_layers = self._prepare_preset_layers(source_layers)

        self.engine.layers = new_layers
        self.engine.active_layer_index = min(source_active_idx, len(new_layers) - 1)

        # 重新提交 GPU 辅助纹理
        # AUD-2900：layer_id 用 layer.id（UUID），与渲染端 layer_textures 键
        # 及 REMOVE_LAYER/CLEAR_AUX_TEX 清理键一致（同 on_apply_grade_from_library）。
        from src.features.color.core.async_renderer import RenderCommand
        for layer in new_layers:
            l_id = layer.id
            if layer.active_lut_path and self.engine.renderer:
                self.engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': l_id, 'type': 'lut3d', 'path': layer.active_lut_path
                })
            if layer.warper_params is not None and self.engine.renderer:
                h, w, c = layer.warper_params.shape
                self.engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': l_id, 'type': 'warper',
                    'data': layer.warper_params.astype(np.float32).tobytes(),
                    'size': (w, h)
                })

        # 同步 UI 面板
        active_layer = self.engine.get_active_layer()
        if active_layer:
            self.update_from_layer(active_layer)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        if active_layer:
            self.plugin_panel.set_layer(active_layer)

        # 刷新曲线/HSL 曲线 LUT
        self.curves_widget.flush()
        if self.hsl_curves_panel.hsl_curves_widget:
            self.hsl_curves_panel.hsl_curves_widget.flush()

        # 渲染 + 自动保存
        if is_video_mode:
            current_frame_num = int(getattr(self.video_controller, 'current_frame', 0) or 0)
            playing_state = getattr(self.video_controller, 'is_playing', False)
            was_playing = bool(playing_state() if callable(playing_state) else playing_state)
            if was_playing:
                # EnhancedVideoPlaybackEngine exposes stop_playback(); older
                # adapters may provide pause_playback(), so support both.
                pause_playback = getattr(self.video_controller, 'pause_playback', None)
                if callable(pause_playback):
                    pause_playback()
                else:
                    self.video_controller.stop_playback()
            rendered, _ = self._render_current_video_frame_for_grading()
            if rendered:
                self.update_canvas(is_new_source=False)
                self.video_controller.frame_changed.emit(current_frame_num)
            if was_playing:
                QTimer.singleShot(100, lambda: self.video_controller.start_playback())
        else:
            self.request_render()

        self._trigger_autosave()

        preset_name = os.path.splitext(os.path.basename(topos_path))[0]
        layer_count = len(new_layers)
        msg = I18N.tr("status.preset_applied", f"已应用预设调色: {preset_name}") if layer_count == 1 else \
              I18N.tr("status.preset_applied_multi", f"已应用预设 ({layer_count} 图层): {preset_name}")
        self.status_bar.showMessage(msg, 3000)

    # ------------------------------------------------------------------
    # Capture Still
    # ------------------------------------------------------------------

    def on_capture_still(self) -> None:
        """从当前视口抓取静帧并保存为预设（.topos 项目格式）。

        A preset is a color-grade container, not a standalone video project.
        In video mode we therefore decode the current source frame, make sure
        the ImageEngine has that frame before rendering, and use the generated
        still thumbnail as the preset's image asset.  The original video path
        is deliberately not passed to ``SaveRouter`` because standalone video
        projects are rejected by the project schema; applying the preset still
        uses the serialized layers and works for the whole video.
        """
        is_video_mode = bool(getattr(self, 'current_file_is_video', False))
        was_playing = False

        if is_video_mode:
            video_controller = getattr(self, 'video_controller', None)
            if (
                video_controller is None
                or not video_controller.is_video_loaded()
            ):
                self.status_bar.showMessage(
                    I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return

            was_playing = bool(getattr(video_controller, 'is_playing', False))
            if was_playing:
                video_controller.stop_playback()

            rendered, current_frame = self._render_current_video_frame_for_grading()
            if not rendered:
                self.status_bar.showMessage(
                    f"Capture failed: could not render video frame {current_frame}",
                    3000,
                )
                if was_playing:
                    QTimer.singleShot(100, video_controller.start_playback)
                return
        elif self.engine.img_high is None:
            self.status_bar.showMessage(
                I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
            return

        # 渲染当前调色后的图像
        rendered_img = self.engine.grab_viewport_image()
        if rendered_img is None:
            self.status_bar.showMessage("Capture failed: could not render image", 3000)
            if was_playing:
                QTimer.singleShot(100, self.video_controller.start_playback)
            return

        from datetime import datetime as dt
        name = dt.now().strftime("Still_%Y%m%d_%H%M%S")

        current_folder = self.presets_panel.get_current_folder()
        topos_path = os.path.join(current_folder, f"{name}.topos")
        thumb_path = os.path.join(current_folder, f"{name}.thumb.jpg")

        if os.path.exists(topos_path):
            reply = QMessageBox.question(
                self,
                I18N.tr("presets.overwrite_title", "Overwrite"),
                I18N.tr("presets.overwrite_msg", f"Preset '{name}' already exists. Overwrite?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                if was_playing:
                    QTimer.singleShot(100, self.video_controller.start_playback)
                return

        # 缩略图 + 全分辨率参考帧 sidecar（P1-COLOR-04：参考 wipe 用）。
        # M09.2（S12）：float 抓帧链闭合——FBO f4 回读的 float 帧写入
        # .still.exr 侧车（HDR 值保留，JPEG 仍写供低精度消费方）；回读
        # 失败降级纯 JPEG（不阻断保存）。
        from src.features.color.core.preset_meta import STILL_IMAGE_MAX, write_still_images
        hdr_frame = None
        try:
            hdr_frame = self.engine.render_full_image_for_scope(
                max_dim=max(STILL_IMAGE_MAX, 4096))
        except Exception:
            hdr_frame = None
        write_still_images(topos_path, rendered_img, hdr_image=hdr_frame)

        # 保存为 .topos 项目文件（完整调色数据）
        # Video presets must reference an image because standalone video
        # projects are intentionally rejected by the project schema.
        image_path = (
            thumb_path if is_video_mode
            else getattr(self, 'current_file_path', '') or ''
        )
        saved = self.save_router._save_single_asset_project(
            topos_path, image_path, self.engine,
            composite_graph_data=self._get_composite_graph_data(),
            composite_viewer_state=self._get_composite_state_data(),
        )

        if saved:
            # Gallery 元数据 sidecar：来源 clip / 帧号 / source tag / 时间
            from src.features.color.core.preset_meta import (
                PresetMeta, now_iso, save_preset_meta,
            )
            save_preset_meta(
                topos_path,
                PresetMeta(created_at=now_iso(),
                           **self._collect_still_capture_meta()))

        if was_playing:
            QTimer.singleShot(100, self.video_controller.start_playback)

        if not saved:
            self.status_bar.showMessage(
                I18N.tr("presets.capture_failed", "静帧保存失败"), 3000)
            return

        self.presets_panel.scan_presets()
        self.status_bar.showMessage(
            I18N.tr("presets.captured", f"静帧已抓取: {name}"), 3000)

    # ------------------------------------------------------------------
    # Gallery in Presets (P1-COLOR-04)
    # ------------------------------------------------------------------

    def _collect_still_capture_meta(self) -> dict:
        """静帧抓取元数据：PageContext 帧号/clip + asset 名与 source tag。"""
        from src.features.color.core.preset_meta import PresetMeta

        fields: dict = {}
        mgr = getattr(self, '_page_context_manager', None)
        ctx = None
        if mgr is not None:
            try:
                ctx = mgr.get_current_context()
            except Exception:
                ctx = None
        if ctx is not None:
            for key in ("clip_id", "asset_id", "timeline_frame", "source_frame"):
                value = getattr(ctx, key, None)
                if value is not None:
                    fields[key] = value
        project = getattr(self, '_current_edit_project', None)
        assets = getattr(project, 'assets', None) or {}
        asset = assets.get(fields.get("asset_id")) if assets else None
        if asset is not None:
            if getattr(asset, 'display_name', None):
                fields.setdefault("display_name", asset.display_name)
            asset_meta = getattr(asset, 'metadata', None) or {}
            for src_key, meta_key in (("color_transfer", "source_transfer"),
                                      ("color_primaries", "source_primaries"),
                                      ("color_range", "source_range")):
                if asset_meta.get(src_key):
                    fields.setdefault(meta_key, asset_meta[src_key])
        # 只保留 PresetMeta 认识的字段
        valid = set(PresetMeta.__dataclass_fields__)
        return {k: v for k, v in fields.items() if k in valid}

    def _prepare_preset_layers(self, source_layers):
        """深拷贝预设图层并剥离 AI/烘焙/画笔数据（可安全应用到其他素材）。"""
        from src.features.color.core.layer import MaskShape
        new_layers = []
        for src_layer in source_layers:
            layer = copy_mod.deepcopy(src_layer)
            layer.ai_matte = None
            layer.ai_matte_enabled = True
            layer.is_baked = False
            layer.baked_file_name = None
            layer.baked_texture_id = None
            layer.plugins = [p for p in layer.plugins
                             if p.get("type", "dctl") == "dctl"]
            layer.mask_shapes = [s for s in layer.mask_shapes
                                 if s.type not in (MaskShape.BITMAP, MaskShape.BRUSH)]
            new_layers.append(layer)
        return new_layers

    def on_apply_preset_categories_requested(self, topos_path: str,
                                             categories=None) -> None:
        """分类应用预设调色（P1-COLOR-04：Paste Attributes 语义）。

        ``categories``: None=弹类别对话框（GradePasteDialog），或类别
        集合（如 {"primary", "windows"}）直接应用。

        目标为 Color 页活动 timeline clip 时走 ``_paste_grade_to_clip``
        单一 undo 命令；standalone 图像/视频走引擎层（单次 history push）。
        """
        from src.features.color.core.grade_clipboard import GradeClipboard

        if not self._validate_preset_apply_target():
            return
        project_data = self.save_router.load_single_asset_data(topos_path)
        source_layers = (project_data or {}).get('layers')
        if not source_layers:
            self.status_bar.showMessage(
                f"无法加载预设: {os.path.basename(topos_path)}", 3000)
            return
        source_layers = self._prepare_preset_layers(source_layers)

        # 临时剪贴板：分类应用不写脏用户 Copy Grade 的全局剪贴板
        clipboard = GradeClipboard()
        clipboard.copy_grade(source_layers)

        if categories is None:
            available = set(clipboard.copied_categories)
            chosen = None
            try:
                from src.features.color.ui.grade_paste_dialog import (
                    GradePasteDialog,
                )
                chosen = GradePasteDialog.get_categories(None, available)
            except Exception:
                chosen = set(available)
            if not chosen:
                return
            categories = set(chosen)
        target_categories = set(categories)
        if not target_categories:
            return

        # 1. timeline clip：单一 undo 命令通道
        strip = getattr(self, 'color_timeline_clip_strip', None)
        active_clip_id = getattr(strip, '_active_clip_id', None) if strip else None
        paste_to_clip = getattr(self, '_paste_grade_to_clip', None)
        if active_clip_id and callable(paste_to_clip):
            if paste_to_clip(active_clip_id, target_categories):
                self.status_bar.showMessage(
                    I18N.tr("presets.categories_applied",
                            f"已分类应用预设调色: {os.path.basename(topos_path)}"),
                    3000)
            return

        # 2. standalone：引擎层应用（单次 history push = 单一 undo）
        engine = getattr(self, 'engine', None)
        if engine is None or not getattr(engine, 'layers', None):
            return
        is_video_mode = bool(getattr(self, 'current_file_is_video', False))
        video_controller = getattr(self, 'video_controller', None)
        was_playing = False
        if is_video_mode and video_controller is not None:
            was_playing = bool(getattr(video_controller, 'is_playing', False))
            if was_playing:
                video_controller.stop_playback()
        if not is_video_mode and getattr(engine, 'img_full_original', None) is not None:
            engine.history.push_state(
                engine.img_full_original, engine.layers,
                engine.active_layer_index)

        applied = clipboard.paste_onto_layers(
            engine.layers, categories=target_categories)
        if not applied:
            if was_playing:
                QTimer.singleShot(100, video_controller.start_playback)
            return

        # LUT / warper 辅助纹理重新提交（粘贴可能更换了路径/网格）
        # AUD-2900：layer_id 用 layer.id（UUID），与渲染端 layer_textures 键
        # 及 REMOVE_LAYER/CLEAR_AUX_TEX 清理键一致（同 on_apply_grade_from_library）。
        from src.features.color.core.async_renderer import RenderCommand
        for layer in engine.layers:
            layer_id = layer.id
            if getattr(layer, 'active_lut_path', None) and engine.renderer:
                engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer_id, 'type': 'lut3d',
                    'path': layer.active_lut_path,
                })
            if getattr(layer, 'warper_params', None) is not None and engine.renderer:
                h, w, _ = layer.warper_params.shape
                engine.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer_id, 'type': 'warper',
                    'data': layer.warper_params.astype(np.float32).tobytes(),
                    'size': (w, h),
                })

        # 同步 UI 面板
        active_layer = engine.get_active_layer()
        if active_layer:
            update_from_layer = getattr(self, 'update_from_layer', None)
            if callable(update_from_layer):
                update_from_layer(active_layer)
        layer_panel = getattr(self, 'layer_panel', None)
        if layer_panel is not None:
            layer_panel.update_layers(
                engine.layers, engine.active_layer_index)

        # 渲染（视频走当前帧重渲染）
        if is_video_mode:
            rendered, _ = self._render_current_video_frame_for_grading()
            if rendered:
                update_canvas = getattr(self, 'update_canvas', None)
                if callable(update_canvas):
                    update_canvas(is_new_source=False)
        else:
            request_render = getattr(self, 'request_render', None)
            if callable(request_render):
                request_render()
        if was_playing:
            QTimer.singleShot(100, video_controller.start_playback)
        self.status_bar.showMessage(
            I18N.tr("presets.categories_applied",
                    f"已分类应用预设调色: {os.path.basename(topos_path)}"), 3000)

    def _validate_preset_apply_target(self) -> bool:
        """应用前提校验：已加载图像或视频，否则状态栏提示。"""
        is_video_mode = bool(getattr(self, 'current_file_is_video', False))
        if not is_video_mode:
            if self.engine.get_current_image() is None:
                self.status_bar.showMessage(
                    I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return False
        else:
            video_controller = getattr(self, 'video_controller', None)
            if not video_controller or not video_controller.is_video_loaded():
                self.status_bar.showMessage(
                    I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
                return False
        return True

    def on_preset_reference_requested(self, topos_path: str) -> None:
        """把预设静帧加载为参考 still，开启 wipe 对比（不改工程状态）。

        M09（S12）：EXR float32 优先——HDR 值直接进参考纹理与 scope
        消费；仅存 JPEG 时明示精度降级（此前恒走 8-bit JPEG，float
        抓帧链在参考侧断开）。
        """
        from src.features.color.core.preset_meta import load_still_best
        img, precision = load_still_best(topos_path)
        if img is None or not self.engine.load_reference_still(img):
            self.status_bar.showMessage(
                I18N.tr("presets.reference_failed", "预设没有可用的参考帧"), 3000)
            return
        if precision == "jpeg8":
            self.status_bar.showMessage(
                I18N.tr("presets.reference_jpeg_fallback",
                        "参考帧无 EXR——以 8-bit JPEG 精度加载"), 3000)
        canvas = getattr(self, 'canvas', None)
        sync = getattr(canvas, '_sync_reference_menu_states', None)
        if callable(sync):
            try:
                sync()
            except Exception:
                pass
        self.status_bar.showMessage(
            I18N.tr("presets.reference_loaded",
                    "预设已加载为参考帧（拖动分割线对比）"), 3000)

    # ------------------------------------------------------------------
    # Auto Color / Color Match (P1-COLOR-21)
    # ------------------------------------------------------------------

    def _auto_color_analysis_image(self):
        """Auto Balance 分析用未调色源帧（图像/视频当前帧）。"""
        img = getattr(self.engine, 'img_full_original', None)
        if img is None:
            img = getattr(self.engine, 'img_high', None)
        return img

    @staticmethod
    def _balance_metrics(stats, target_luma):
        """Auto Balance 展示指标：色偏 / 亮度偏移 / chroma 偏移。"""
        import numpy as _np
        m = _np.array(stats.mean)
        return {
            "channel_delta": float(_np.max(_np.abs(m - m.mean()))),
            "luma_delta": float(abs(stats.luma_median - target_luma)),
            "chroma_delta": float(abs(stats.chroma_mean - 0.30)),
        }

    def _ask_auto_color(self, suggestion, before_metrics, after_metrics,
                        title=""):
        """弹建议对话框；无头/测试环境退化为全部建议项（饱和度除外）。"""
        try:
            from src.features.color.ui.auto_color_dialog import (
                AutoColorDialog,
            )
            return AutoColorDialog.get_application(
                None, suggestion, before_metrics, after_metrics, title)
        except Exception:
            values = suggestion.items()
            keys = {"wb", "exposure", "contrast"}
            if values.get("saturation"):
                keys.add("saturation")
            return keys

    def on_auto_color_requested(self) -> None:
        """Auto Balance：分析未调色源，给出参数建议（可撤销应用）。"""
        from src.features.color.core.auto_color import (
            TARGET_LUMA, FrameStats, auto_balance, predict,
        )
        img = self._auto_color_analysis_image()
        if img is None:
            self.status_bar.showMessage(
                I18N.tr("status.no_image_loaded", "请先打开一张图片或视频"), 3000)
            return
        try:
            stats = FrameStats.from_image(img)
        except Exception:
            self.status_bar.showMessage(
                I18N.tr("autocolor.failed", "自动校色分析失败"), 3000)
            return
        suggestion = auto_balance(stats)
        predicted = predict(stats, suggestion.items())
        keys = self._ask_auto_color(
            suggestion,
            self._balance_metrics(stats, TARGET_LUMA),
            self._balance_metrics(predicted, TARGET_LUMA),
            title=I18N.tr("autocolor.title", "Auto Color"),
        )
        if keys:
            self._apply_auto_suggestion(suggestion, keys)

    def on_match_to_still_requested(self, topos_path: str) -> None:
        """Color Match：把当前调色结果匹配到 Gallery 静帧（增量建议）。"""
        from src.features.color.core.auto_color import (
            FrameStats, difference_metrics, match_to_reference, predict,
        )
        from src.features.color.core.preset_meta import load_still_best
        # M09（S12）：Color Match 参考统计优先 float32 EXR（HDR 值参与
        # 分布匹配；JPEG 回退同参考帧路径）
        ref_img, _precision = load_still_best(topos_path)
        cur_img = self.engine.grab_viewport_image()
        if ref_img is None or cur_img is None:
            self.status_bar.showMessage(
                I18N.tr("presets.reference_failed", "预设没有可用的参考帧"), 3000)
            return
        cur = FrameStats.from_image(cur_img)
        ref = FrameStats.from_image(ref_img)
        suggestion = match_to_reference(cur, ref)
        predicted = predict(cur, suggestion.items())
        keys = self._ask_auto_color(
            suggestion,
            difference_metrics(cur, ref),
            difference_metrics(predicted, ref),
            title=I18N.tr("autocolor.match_title", "Color Match"),
        )
        if keys:
            self._apply_auto_suggestion(suggestion, keys)

    def _apply_auto_suggestion(self, suggestion, keys) -> None:
        """把选中的建议应用到当前目标。

        timeline clip（Color 页活动 clip）→ 修改快照层后经
        ``_paste_grade_to_clip`` 单一 undo 命令；standalone → 单次
        history push 后就地应用。
        """
        param_map = {}
        if "wb" in keys:
            param_map["temp"] = suggestion.temp
            param_map["tint"] = suggestion.tint
        if "exposure" in keys:
            param_map["exposure"] = suggestion.exposure
        if "contrast" in keys:
            param_map["contrast"] = suggestion.contrast
        if "saturation" in keys:
            param_map["saturation"] = suggestion.saturation
        if not param_map:
            return
        engine = getattr(self, 'engine', None)
        if engine is None:
            return
        active_index = getattr(engine, 'active_layer_index', 0) or 0
        if not (0 <= active_index < len(getattr(engine, 'layers', []) or [])):
            return

        def _apply_to_layer(layer) -> None:
            p = layer.grading_params
            for key, value in param_map.items():
                base = float(p.get(key, 0.0) or 0.0)
                merged = float(np.clip(
                    value if suggestion.absolute else base + value,
                    -100.0, 100.0))
                p[key] = merged
                if key == "temp":
                    p["temperature"] = merged  # 标准名镜像（UI/测试读它）
            layer.is_dirty = True
            layer.mask_dirty = True

        # 1. timeline clip：快照层修改后走单一 undo 命令通道
        strip = getattr(self, 'color_timeline_clip_strip', None)
        active_clip_id = getattr(strip, '_active_clip_id', None) if strip else None
        paste_to_clip = getattr(self, '_paste_grade_to_clip', None)
        if active_clip_id and callable(paste_to_clip):
            from src.features.color.core.grade_clipboard import GradeClipboard
            patched = [copy_mod.deepcopy(l) for l in engine.layers]
            _apply_to_layer(patched[active_index])
            # 临时剪贴板：自动校色不经全局 Grade 剪贴板中转，
            # 不覆盖用户已复制的内容
            clipboard = GradeClipboard()
            clipboard.copy_grade(patched)
            if paste_to_clip(active_clip_id, {"primary"},
                             clipboard=clipboard):
                self.status_bar.showMessage(
                    I18N.tr("autocolor.applied", "自动校色已应用（可撤销）"), 3000)
            return

        # 2. standalone：单次 history push = 单一 undo
        if getattr(engine, 'img_full_original', None) is not None:
            engine.history.push_state(
                engine.img_full_original, engine.layers,
                engine.active_layer_index)
        active_layer = engine.get_active_layer()
        _apply_to_layer(active_layer)

        update_from_layer = getattr(self, 'update_from_layer', None)
        if callable(update_from_layer):
            update_from_layer(active_layer)
        layer_panel = getattr(self, 'layer_panel', None)
        if layer_panel is not None:
            layer_panel.update_layers(engine.layers, engine.active_layer_index)

        if bool(getattr(self, 'current_file_is_video', False)):
            rendered, _ = self._render_current_video_frame_for_grading()
            if rendered:
                update_canvas = getattr(self, 'update_canvas', None)
                if callable(update_canvas):
                    update_canvas(is_new_source=False)
        else:
            request_render = getattr(self, 'request_render', None)
            if callable(request_render):
                request_render()
        self.status_bar.showMessage(
            I18N.tr("autocolor.applied", "自动校色已应用（可撤销）"), 3000)

    # ------------------------------------------------------------------
    # AI Baking methods (栅格化)
    # ------------------------------------------------------------------

    def on_layer_bake(self, index: int = None) -> None:
        """栅格化当前图层，缓存AI计算结果。"""
        if index is None:
            index = self.engine.active_layer_index
        if not (0 <= index < len(self.engine.layers)):
            return
        layer = self.engine.layers[index]
        if layer.is_baked:
            self.status_bar.showMessage(f"图层 '{layer.name}' 已栅格化", 2000)
            return
        self.engine.bake_layer(index)
        self.status_bar.showMessage(f"正在栅格化图层 '{layer.name}'...", 2000)

    def on_layer_unbake(self, index: int = None) -> None:
        """解冻当前图层，清除缓存重新计算。"""
        if index is None:
            index = self.engine.active_layer_index
        if not (0 <= index < len(self.engine.layers)):
            return
        layer = self.engine.layers[index]
        if not layer.is_baked:
            self.status_bar.showMessage(f"图层 '{layer.name}' 未栅格化", 2000)
            return
        self.engine.unbake_layer(index)
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.status_bar.showMessage(f"已解冻图层 '{layer.name}'", 2000)

    def on_layer_baking_finished(self, index: int) -> None:
        """栅格化完成后的处理。"""
        if not (0 <= index < len(self.engine.layers)):
            return
        layer = self.engine.layers[index]
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.status_bar.showMessage(f"图层 '{layer.name}' 栅格化完成", 2000)
        self._trigger_autosave()

    # ------------------------------------------------------------------
    # Render pipeline
    # ------------------------------------------------------------------

    def request_render(self, is_full_res=False, render_reason='grading') -> None:
        """请求一次渲染：播放中先失效缓存参数，暂停态按需升级 full 渲染。

        任何图层还有待上传的 CPU mask（mask_dirty）时，transient 渲染
        会跳过遮罩纹理更新，因此自动把 render_reason 升级为 'full'。
        full_res 路径走同步全分辨率渲染并直接回调 on_render_finished。

        Args:
            is_full_res: True 时走同步全分辨率导出路径。
            render_reason: 渲染原因标签（grading/full 等）。
        """
        try:
            self._invalidate_video_render_params_if_playing()
            if is_full_res:
                result = self.engine.render_full_resolution()
                if result is not None: self.on_render_finished(result, is_full_res)
            else:
                # 任何图层还有待上传的 CPU mask 时必须走 'full'：transient
                # 渲染会跳过 _update_mask_textures，mask 面板/画笔/显隐等
                # 改动将停留在旧纹理上，直到下一次 full 渲染才补上。
                if render_reason != 'full' and any(
                    getattr(l, 'mask_dirty', False)
                    for l in (getattr(self.engine, 'layers', None) or [])
                ):
                    render_reason = 'full'
                self.engine.render_pipeline(is_full_res=False, render_reason=render_reason)
            # Update layer indicator icons for the active layer
            active_layer = self.engine.get_active_layer()
            if active_layer:
                self.layer_panel.update_layer_icons(self.engine.active_layer_index, active_layer)
        except Exception as e:
            # AUD-2902: 不再用无堆栈的单行 error 吞掉整个渲染管线的异常——
            # 保留完整堆栈（含引擎不变量破坏/提交失败的具体位置），并让
            # 用户在状态栏看到渲染失败而非停留在旧帧。
            logger.exception(f"request_render failed (reason={render_reason}): {e}")
            status_bar = getattr(self, 'status_bar', None)
            if status_bar is not None:
                try:
                    status_bar.showMessage(
                        I18N.tr("status.render_failed", f"渲染失败: {e}"), 5000)
                except Exception:
                    logger.debug("request_render: status bar unavailable", exc_info=True)

    def on_frame_ready(self, tex_id) -> None:
        """renderer 前台纹理发布回调：提交 GPU scope 需求并收尾渲染。

        Scope 分析作为 GPU demand 针对已完成纹理提交，此处刻意不做
        CPU 快照/回读——实时 Color 页示波器必须保持 GPU-only。

        Args:
            tex_id: 已发布的前台纹理 ID。
        """
        # The renderer emits this only after publishing a completed front
        # texture. Scope analysis is submitted as a GPU demand against that
        # texture. There is deliberately no CPU snapshot/readback here:
        # realtime Color-page scopes must remain GPU-only.
        need_scope = self._is_scope_panel_visible() or self._is_curves_panel_visible()
        is_playing = self._scope_playback_is_active()
        if need_scope:
            self._submit_gpu_scope_demand(is_playing=is_playing)
        self.on_render_finished(None, False)

    # ------------------------------------------------------------------
    # GPU scope demand management
    # ------------------------------------------------------------------
    def _submit_gpu_scope_demand(self, *, is_playing: bool, force: bool = False) -> None:
        """Build and submit the latest ``ScopeDemand`` to the renderer.

        Standalone scopes are rate-limited and coalesced. Curve underlays
        are stills: frozen during playback/scrubbing/adjustment, deduplicated
        by final image and canvas geometry, and disabled on inactive tabs.

        Idle high-quality pass: when paused and not interacting, after a
        150 ms quiet window we submit one demand at the high-quality
        analysis size (768x432) so the final scope is crisp.
        """
        try:
            is_playing = is_playing or self._scope_playback_is_active()
            engine = self.engine
            if engine is None or engine.renderer is None:
                _scope_diag().record("submit", "no_renderer")
                return
            interacting = bool(getattr(engine, 'is_interacting', False))
            curves_frozen = is_playing or interacting or self._scope_scrub_is_active()
            scope_visible = self._is_scope_panel_visible()
            curves_overlay = self._is_curves_panel_visible() and not curves_frozen
            if not scope_visible and not curves_overlay:
                _scope_diag().record("submit", "panel_hidden",
                                     source_revision=engine.scope_source_revision)
                return
            if not getattr(engine.renderer, 'gpu_scope_enabled', False):
                # 含 shader 编译失败后的永久关闭（S01：必须可诊断）
                _scope_diag().record("submit", "gpu_lane_disabled",
                                     source_revision=engine.scope_source_revision)
                return
            from src.features.color.core.scope_types import (
                ScopeDemand, DEFAULT_ANALYSIS_SIZE, PLAYBACK_ANALYSIS_SIZE,
                HIGH_QUALITY_ANALYSIS_SIZE,
                MAX_HZ_PLAYBACK, MAX_HZ_INTERACTING, MAX_HZ_IDLE,
            )
            now = time.monotonic()
            # Pick the refresh budget based on state.
            if is_playing:
                # Playback uses the quality tier that still measured ~7x
                # faster than the CPU waveform on real project footage.
                max_hz, analysis = MAX_HZ_PLAYBACK, PLAYBACK_ANALYSIS_SIZE
            elif interacting:
                # Keep grading interaction visually consistent with playback.
                # 320x180 made the 1024px display FBO reveal vertical sample
                # columns while the user dragged a control; 512x288 remains
                # comfortably within the GPU scope budget.
                max_hz, analysis = MAX_HZ_INTERACTING, PLAYBACK_ANALYSIS_SIZE
            else:
                max_hz, analysis = MAX_HZ_IDLE, DEFAULT_ANALYSIS_SIZE
            min_interval = 1.0 / max_hz if max_hz > 0.0 else 0.0
            last = float(getattr(self, '_gpu_scope_last_submit', 0.0))

            # Idle high-quality top-up: after 150 ms of quiet, upgrade the
            # analysis size for a single crisp frame (plan §5.2).
            idle_quiet_s = 0.150
            last_activity = float(getattr(self, '_gpu_scope_last_activity', 0.0))
            if (not is_playing and not interacting
                    and (now - last_activity) > idle_quiet_s):
                analysis = HIGH_QUALITY_ANALYSIS_SIZE
            if is_playing or interacting:
                self._gpu_scope_last_activity = now

            panel = getattr(self, 'scope_panel', None)
            if panel is None:
                _scope_diag().record("submit", "panel_missing")
                return
            state = panel.demand_state()
            active_layer = engine.get_active_layer()
            mask_layer_id = None
            mask_enabled = False
            if bool(getattr(engine, 'show_mask_mode', False)) and active_layer is not None:
                mask_layer_id = getattr(active_layer, 'id', None)
                mask_enabled = bool(mask_layer_id)
            # The panel-side revision only changes on mode/size/zoom changes
            # (see ScopePanel.bump_demand_revision callers). It is NOT bumped
            # per submit, so a frame produced from this demand still matches
            # the panel's current revision when it arrives.
            revision = panel.gpu_demand_revision
            curves_mode = self._current_curve_mode()
            curves_size, curves_line_width = (1024, 512), 1.0
            curves_key = None
            if curves_overlay:
                # A peak-normalized histogram must not alternate between
                # 320/512/768 sampling grids for the same paused image. Each
                # grid changes bin counts and causes visible vertical breathing.
                analysis = HIGH_QUALITY_ANALYSIS_SIZE
                curves_size, curves_line_width = self._active_curve_canvas().histogram_display_metrics()
                curves_key = (
                    engine.scope_source_revision, curves_mode,
                    curves_size, curves_line_width, mask_layer_id, mask_enabled,
                )
                # A paused curve underlay is a still, not another live scope.
                # Suppress repeated requests for the same completed image.
                curves_overlay = curves_key != getattr(self, '_curves_scope_completed_key', None)
                if not scope_visible and (
                    not curves_overlay
                    or curves_key == getattr(self, '_curves_scope_pending_key', None)
                ):
                    _scope_diag().record("submit", "curves_dedup_skip",
                                         source_revision=engine.scope_source_revision)
                    return
            if not force and not curves_overlay and now - last < min_interval:
                _scope_diag().record("submit", "rate_limited",
                                     detail=f"interval={min_interval:.3f}s")
                return
            self._gpu_scope_last_submit = now
            demand = ScopeDemand(
                revision=revision,
                source_revision=engine.scope_source_revision,
                mode=state['mode'] if scope_visible else 'histogram',
                display_size=state['display_size'],
                analysis_size=analysis,
                vectorscope_zoom=state['vectorscope_zoom'],
                is_playing=is_playing,
                mask_layer_id=mask_layer_id,
                mask_enabled=mask_enabled,
                histogram_overlay=curves_overlay,
                curves_display_size=curves_size,
                curves_line_width=curves_line_width,
                curves_mode=curves_mode,
                scope_transfer=state.get('scope_transfer', 'srgb'),
                scope_primaries=state.get('scope_primaries', 'bt709'),
                falsecolor_mode=state.get('falsecolor_mode', 'sdr'),
                zebra_threshold=state.get('zebra_threshold', 1.0),
            )
            # Keep the exact freshness contract used for the newest queued
            # request. GPU_SCOPE may analyse a newer front texture by the time
            # it drains, which is valid; an older frame must never be shown.
            self._latest_gpu_scope_demand = demand
            engine.request_gpu_scope(demand)
            _scope_diag().record_activity(now)
            if curves_overlay:
                self._curves_scope_pending_key = curves_key
        except Exception as exc:
            _scope_diag().record("submit", "submit_exception",
                                 detail=str(exc)[:120])
            logger.debug("GPU scope demand submit failed: %s", exc)

    def _on_scope_demand_changed(self) -> None:
        """Submit a GPU refresh after a mode/zoom/size-only change."""
        if not (self._is_scope_panel_visible() or self._is_curves_panel_visible()):
            return
        is_playing = self._scope_playback_is_active()
        self._submit_gpu_scope_demand(is_playing=is_playing, force=True)

    def _current_curve_mode(self) -> str:
        mode = getattr(getattr(self, 'curves_panel', None), 'curve_mode', 'Custom')
        return mode if isinstance(mode, str) else 'Custom'

    def _active_curve_canvas(self) -> Any:
        if self._current_curve_mode() != 'Custom':
            return self.hsl_curves_panel.hsl_curves_widget
        return self.curves_widget

    def _scope_playback_is_active(self) -> bool:
        """Check both the source player and Color's timeline transport."""
        video = getattr(self, 'video_controller', None)
        if bool(getattr(video, 'is_playing', False)):
            return True
        get_timeline = getattr(self, '_color_timeline_playback_controller', None)
        timeline = get_timeline() if callable(get_timeline) else None
        return bool(getattr(timeline, 'is_playing', False))

    def _scope_scrub_is_active(self) -> bool:
        """Include the viewport bar and shared timeline's drag preview state."""
        return bool(
            getattr(self, '_curves_scope_scrubbing', False)
            or getattr(self, '_viewport_scrub_active', False)
            or getattr(getattr(self, 'edit_page', None), '_scrub_active', False)
        )

    def _on_scope_scrub_state_changed(self, active: bool) -> None:
        """Retain the underlay throughout a drag, including pause callbacks."""
        self._curves_scope_scrubbing = active
        if active:
            self.engine.discard_pending_gpu_scope()
            self._latest_gpu_scope_demand = None
            self._curves_scope_pending_key = None
        else:
            # The bar emits its exact seek immediately after scrub_finished.
            # Let that request enter the render queue before refreshing scopes.
            QTimer.singleShot(150, self._on_scope_demand_changed)

    def _on_scope_play_state_changed(
        self, is_playing: bool, speed: float = 0.0, direction: int = 1,
    ) -> None:
        """Freeze the curve underlay on play and refresh it after pause."""
        if is_playing:
            self.engine.discard_pending_gpu_scope()
            self._latest_gpu_scope_demand = None
            self._curves_scope_pending_key = None
        elif self._is_scope_panel_visible() or self._is_curves_panel_visible():
            self._submit_gpu_scope_demand(is_playing=False, force=True)

    def _on_scope_visibility_changed(self, visible: bool) -> None:
        """Start/stop the low-priority GPU scope lane with the dock."""
        if not (self._is_scope_panel_visible() or self._is_curves_panel_visible()):
            try:
                self.engine.discard_pending_gpu_scope()
            except Exception:
                pass
            self._latest_gpu_scope_demand = None
            self._curves_scope_pending_key = None
            return
        self._gpu_scope_last_submit = 0.0
        self._on_scope_demand_changed()

    def _on_curves_scope_visibility_changed(self, visible: bool) -> None:
        """Use dock exposure, including tab switches, rather than isVisible alone."""
        self._curves_scope_dock_active = visible
        self._curves_scope_pending_key = None
        self._on_scope_visibility_changed(visible)

    def _on_analysis_scope_visibility_changed(self, visible: bool) -> None:
        """Do not run a standalone scope behind another dock tab."""
        self._analysis_scope_dock_active = visible
        self._on_scope_visibility_changed(visible)

    def _freeze_curves_scope_for_interaction(self) -> None:
        """Drop queued curve work and reject late descriptors at gesture start."""
        self.engine.discard_pending_gpu_scope()
        self._latest_gpu_scope_demand = None
        self._curves_scope_pending_key = None

    def on_gpu_scope_ready(self, frame) -> None:
        """Forward a ``ScopeGpuFrame`` to the panel if still visible."""
        if not (self._is_scope_panel_visible() or self._is_curves_panel_visible()) or frame is None:
            _scope_diag().record("panel", "not_visible_on_return",
                                 revision=getattr(frame, "revision", None))
            return
        from src.features.color.core.scope_types import is_frame_valid_for_demand

        if not is_frame_valid_for_demand(
            frame, getattr(self, '_latest_gpu_scope_demand', None)
        ):
            _scope_diag().record("panel", "stale_return",
                                 revision=getattr(frame, "revision", None),
                                 source_revision=getattr(
                                     frame, "source_revision", None))
            return
        try:
            panel = getattr(self, 'scope_panel', None)
            if panel is not None and self._is_scope_panel_visible():
                panel.update_gpu_scope(frame)
            curves_widget = self._active_curve_canvas() if getattr(self, 'curves_widget', None) is not None else None
            if (curves_widget is not None and self._is_curves_panel_visible()
                    and not bool(getattr(self.engine, 'is_interacting', False))
                    and not self._scope_playback_is_active()
                    and not self._scope_scrub_is_active()
                    and getattr(frame, 'curves_mode', 'Custom') == self._current_curve_mode()
                    and frame.curves_histogram_texture_id is not None):
                curves_widget.set_scope_frame(
                    frame.curves_histogram_texture_id,
                    *frame.curves_histogram_size,
                )
                pending = getattr(self, '_curves_scope_pending_key', None)
                if pending is not None:
                    self._curves_scope_completed_key = (frame.source_revision, *pending[1:])
                    self._curves_scope_pending_key = None
            _scope_diag().record_activity()
        except Exception as exc:
            _scope_diag().record("panel", "present_exception",
                                 detail=str(exc)[:120])
            logger.debug("GPU scope present failed: %s", exc)

    def on_scope_snapshot_ready(self, result_img: Optional[np.ndarray]) -> None:
        """Ignore legacy CPU snapshots unless explicitly enabled."""
        if not bool(getattr(self.engine, 'scope_snapshots_enabled', False)):
            return
        if self._is_scope_panel_visible() and result_img is not None:
            self._update_scope_panel(result_img)

    def _is_scope_panel_visible(self) -> bool:
        """Return whether the actual ScopePanel is exposed to the user."""
        dock = getattr(self, 'dock_analysis', None)
        panel = getattr(self, 'scope_panel', None)
        return bool(
            dock is not None
            and panel is not None
            and getattr(self, '_analysis_scope_dock_active', None) is not False
            and dock.isVisible()
            and panel.isVisible()
        )

    def _is_curves_panel_visible(self) -> bool:
        """Return whether the integrated curve histogram is exposed."""
        dock = getattr(self, 'dock_curves', None)
        panel = getattr(self, 'curves_panel', None)
        if panel is None:
            panel = getattr(self, 'curves_widget', None)
        return bool(
            dock is not None
            and panel is not None
            and getattr(self, '_curves_scope_dock_active', None) is not False
            and dock.isVisible()
            and panel.isVisible()
        )

    def _on_color_output_changed(self, texture_id: int, width: int, height: int) -> None:
        """Handle ColorOutput render completion — update Composite page if active.

        AUD-2904: 本回调每帧触发（4K/60fps 基准下每秒 60 次）。此前的
        ``print`` + 多条 info 级 ``[COMPOSITE-DEBUG]`` 插桩已移除/降级为
        debug——每帧同步 stdout/日志 I/O 与 f-string 构造本身就是可测的
        开销，且控制台被调试噪声淹没。
        """
        if self._composite_adapter and self._composite_available:
            co_rev = self.engine.color_output.revision
            self._composite_adapter.on_color_output_changed(texture_id, width, height, co_rev)
            return

        if self._composite_available and self.composite_page:
            self.composite_page.mark_color_dirty()

        # Phase 5: Invalidate Edit preview cache when color changes
        if self.current_page == "Edit" and hasattr(self, '_edit_preview_needs_composite_refresh'):
            self._edit_preview_needs_composite_refresh = True

        if self.current_page == "Composite" and self._composite_available and self.composite_page:
            if not self.composite_page._is_active:
                logger.debug("[COMPOSITE-DEBUG] Setting _is_active = True")
                self.composite_page._is_active = True

            if getattr(self, '_composite_startup_pending', False):
                self._composite_startup_pending = False
                logger.debug("[COMPOSITE-DEBUG] Startup swap: canvas → CompositeViewer")
                if self.composite_viewer and self.dock_viewport:
                    self.dock_viewport.setWidget(self.composite_viewer)

            if self.composite_page._renderer is None:
                logger.debug("[COMPOSITE-DEBUG] Initializing renderer...")
                self.composite_page.initialize_renderer()
                logger.debug(
                    "[COMPOSITE-DEBUG] Renderer after init: %s",
                    self.composite_page._renderer,
                )
            else:
                logger.debug("[COMPOSITE-DEBUG] Renderer already exists")
            co_rev = self.engine.color_output.revision
            logger.debug(
                "[COMPOSITE-DEBUG] Calling update_color_output, tex_id=%s, rev=%s",
                texture_id, co_rev,
            )
            self.composite_page.update_color_output(texture_id, width, height, co_rev)

            # First-launch-on-Composite: the normal switch_to_page path was
            # skipped (same-page early return), so enter_page_hook never ran.
            # Run it now to bind the graph, wire the Properties panel widget,
            # and call composite_page.on_enter(). Idempotent guard prevents
            # re-running on every color_output_changed afterwards.
            if getattr(self, '_composite_enter_needed_on_startup', False):
                self._composite_enter_needed_on_startup = False
                try:
                    self.enter_page_hook("Composite")
                except Exception as e:
                    logger.warning(f"Composite startup enter_page_hook failed: {e}")

    def _composite_startup_fallback(self) -> None:
        """Fallback: swap to CompositeViewer if no color output arrived within timeout.

        This handles the case where no file is auto-loaded on startup.
        """
        # AUD-2904: 一次性启动回退的调试 print 移除；需要排查时走 debug 日志。
        logger.debug(
            "[COMPOSITE-STARTUP] fallback fired, pending=%s",
            getattr(self, '_composite_startup_pending', False),
        )
        if self._composite_adapter:
            self._composite_adapter.composite_startup_fallback()
            return
        if not getattr(self, '_composite_startup_pending', False):
            return  # Already activated via color_output_changed
        self._composite_startup_pending = False
        if self.current_page == "Composite" and self._composite_available:
            if self.composite_viewer and self.dock_viewport:
                self.dock_viewport.setWidget(self.composite_viewer)
            self.composite_page._is_active = True
            self.composite_page.initialize_renderer()
            self.composite_viewer.update()

            # First-launch-on-Composite: enter_page_hook was skipped during
            # startup (same-page early return in switch_to_page). Run it now
            # so the Properties panel is wired and the graph is bound.
            # Skip when the timeline clip context was already established by the
            # video startup path — re-entering would re-run bind logic.
            ctx_already_established = getattr(
                self, '_composite_timeline_context_established', False)
            if (getattr(self, '_composite_enter_needed_on_startup', False)
                    and not ctx_already_established):
                self._composite_enter_needed_on_startup = False
                try:
                    self.enter_page_hook("Composite")
                except Exception as e:
                    # AUD-2901: 此前引用未定义的局部名 _logger（仅存在于
                    # _on_color_output_changed 内），except 分支自身抛
                    # NameError 并沿 QTimer 回调中断启动回退流程。改用模块级
                    # logger，保持"记录并继续"的容错语义。
                    logger.warning(f"Composite fallback enter_page_hook failed: {e}")

            # Same reasoning as MainWindowAdapter.composite_startup_fallback:
            # when starting on the Composite page, the Color engine has loaded
            # the image but render_pipeline() never emits color_output_changed,
            # so the first Composite evaluation would never fire. Kick off a
            # ColorOutput render now if this graph needs it.
            if self.composite_page.requires_color_output_input():
                engine = getattr(self, 'engine', None)
                if engine is not None:
                    co = engine.color_output
                    if not co.is_valid or co.is_dirty:
                        try:
                            engine.request_color_output()
                        except Exception:
                            logger.exception("[COMPOSITE-STARTUP] request_color_output failed in fallback")

    def on_render_finished(self, result_img, is_full_res) -> None:
        """渲染完成收尾：失效场景背景层、刷新遮罩覆盖层与撤销按钮态。

        ``result_img`` 是导出/legacy 同步渲染结果，刻意不转发给
        scopes（示波器 UI 仅由 GPU 纹理描述符信号更新）。

        Args:
            result_img: 同步渲染结果图像（异步路径为 None）。
            is_full_res: 是否为全分辨率渲染。
        """
        # 强制令场景的背景层失效，确保 OpenGL 纹理更新后能立即触发 drawBackground
        self.canvas.scene.invalidate(self.canvas.scene.sceneRect(), QGraphicsScene.SceneLayer.BackgroundLayer)
        self.canvas.viewport().update()

        # ``result_img`` is an export/legacy synchronous-render result. It is
        # intentionally not forwarded to scopes, otherwise this callback
        # would reintroduce the CPU/Numpy/QPainter realtime path. Scope UI is
        # updated only by the GPU texture descriptor signal.

        active_layer = self.engine.get_active_layer()
        # 更新遮罩覆盖层：有 mask 时显示，无 mask 时清理残留的 overlay items
        if active_layer:
            if len(getattr(active_layer, 'mask_shapes', [])) > 0 or len(self.canvas.mask_items) > 0:
                self.canvas.update_mask_overlay(active_layer)
        self.update_undo_redo_actions()

    def _update_scope_panel(self, result_img: np.ndarray) -> None:
        """Push a final BGR float image to scopes, preserving mask analysis.

        P1-COLOR-07: scope 输入源选择——source 模式喂未调色原图、
        reference 模式喂参考 still（无则不更新），graded 走原有路径。
        """
        source = getattr(self.scope_panel, 'scope_source', 'graded')
        if source == 'source':
            orig = getattr(self.engine, 'img_full_original', None)
            if orig is None or orig.size == 0:
                return
            if orig.dtype == np.uint8:
                img = orig.astype(np.float32) / 255.0
            elif orig.dtype == np.uint16:
                img = orig.astype(np.float32) / 65535.0
            else:
                img = orig.astype(np.float32)
            self.scope_panel.update_scope(np.ascontiguousarray(img),
                                          source='source')
            return
        if source == 'reference':
            getter = getattr(self.engine, 'get_reference_still_cpu', None)
            ref = getter() if callable(getter) else None
            if ref is None or ref.size == 0:
                return
            self.scope_panel.update_scope(ref, source='reference')
            return

        mask_data = None
        active_layer = self.engine.get_active_layer()
        if active_layer and hasattr(self.engine, 'show_mask_mode') and self.engine.show_mask_mode:
            mask_data = self._get_layer_mask_for_scope(active_layer, result_img.shape[:2])
        if mask_data is not None:
            self.scope_panel.update_scope(result_img, mask_data)
        else:
            self.scope_panel.update_scope(result_img)

    def _get_layer_mask_for_scope(self, layer, target_size: tuple) -> Optional[np.ndarray]:
        """
        Get the combined mask data for a layer, resized to target size.

        This is used for vectorscope to display only the colors from masked areas.

        Args:
            layer: The active layer containing mask shapes and/or AI matte
            target_size: Target (height, width) for the mask

        Returns:
            numpy.ndarray: Mask array (float32, 0.0-1.0), shape (H, W), or None if no mask
        """
        try:
            # Check if layer has any mask data
            has_mask_shapes = layer.mask_shapes and any(s.visible for s in layer.mask_shapes)
            has_ai_matte = layer.ai_matte is not None and layer.ai_matte_enabled

            if not has_mask_shapes and not has_ai_matte:
                return None

            h, w = target_size

            # Render mask shapes if present
            if has_mask_shapes:
                # Use the engine's mask rendering logic
                final_mask = self.engine._render_mask_items_recursive(
                    layer.mask_shapes, w, h,
                    self.engine.img_full_original.shape[1] if self.engine.img_full_original is not None else w
                )
            else:
                final_mask = np.zeros((h, w), dtype=np.float32)

            # Combine with AI matte if present
            if has_ai_matte:
                ai_matte_resized = cv2.resize(layer.ai_matte, (w, h), interpolation=cv2.INTER_LINEAR)
                if np.all(final_mask == 0):
                    final_mask = ai_matte_resized
                else:
                    op = layer.ai_matte_operation
                    if op == 'add':
                        final_mask = np.maximum(final_mask, ai_matte_resized)
                    elif op == 'subtract':
                        final_mask = np.clip(final_mask - ai_matte_resized, 0.0, 1.0)
                    elif op == 'intersect':
                        final_mask = final_mask * ai_matte_resized

            # Check if mask has any content
            if np.max(final_mask) < 0.1:
                return None

            return final_mask

        except Exception as e:
            logger.error(f"getting layer mask for scope: {e}")
            return None

    # ------------------------------------------------------------------
    # Grading parameter change handlers
    # ------------------------------------------------------------------

    # grading_changed 元组 → grading_params 键（与 grade_image 一致）
    _GRADING_SIGNAL_KEYS = (
        "lift", "gamma", "gain", "exposure", "temp", "tint",
        "contrast", "shadow", "saturation", "vibrance",
        "mid_detail", "hue_shift", "highlights",
    )

    def on_grading_changed(self, *args) -> None:
        """调色面板参数变化：写入引擎并按播放态选择渲染策略。

        关键帧模式下（或参数已动画）把修改记录为当前帧关键帧
        （P1-COLOR-03）；播放中失效缓存参数并防抖渲染，暂停态直接
        ``request_render``。

        Args:
            *args: lift/gamma/gain/exposure/temp/tint/contrast/shadow/
                saturation/vibrance/mid_detail/hue_shift/highlights 依次取值。
        """
        if self.engine.get_current_image() is None: return
        self.engine.grade_image(*args)
        # P1-COLOR-03: 关键帧模式下 / 参数已动画时，把修改记录为当前帧关键帧
        self._maybe_record_grade_keyframes(args)
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        # P0-5: debounce during playback to avoid render-queue flooding
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_tonal_saturation_changed(self, lift_sat: float,
                                    gamma_sat: float,
                                    gain_sat: float) -> None:
        """色调区间饱和度面板变化：写入引擎并按播放态选择渲染策略。

        P1-COLOR-03 关键帧暂不覆盖这三个参数（关键帧轨道参数名映射
        基于 LGG 信号元组）；后续如需动画，扩展 _maybe_record_grade_
        keyframes 的参数名映射即可。
        """
        if self.engine.get_current_image() is None:
            return
        self.engine.set_tonal_saturation(lift_sat, gamma_sat, gain_sat)
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def _maybe_record_grade_keyframes(self, args: tuple) -> None:
        """P1-COLOR-03: 调色修改写关键帧。

        语义（与专业调色台一致）：
        - 参数已有轨道：修改自动写入当前帧关键帧（无需开录制模式）；
        - 录制模式（grading 面板 ◆）：全部参数记录当前帧关键帧，
          作为开始动画某参数的入口。
        无当前帧（静态图/未播放）时不记录。写入发生在滑条手势的
        interaction 边界内，撤销粒度 = 一次手势一次 history push。
        """
        engine = self.engine
        frame = getattr(engine, 'current_grade_frame', None)
        if frame is None:
            return
        layer = engine.get_active_layer() if hasattr(engine, 'get_active_layer') else None
        if layer is None:
            return
        panel = getattr(self, 'grading_panel', None)
        record_mode = bool(getattr(panel, 'keyframe_mode', False))
        tracks = getattr(layer, 'grade_keyframes', None) or {}
        wrote = False
        for name, value in zip(self._GRADING_SIGNAL_KEYS, args):
            if record_mode or name in tracks:
                if engine.record_grade_keyframe(frame, name, value):
                    wrote = True
        if wrote:
            self._refresh_grade_keyframe_ui()

    def _refresh_grade_keyframe_ui(self) -> None:
        """关键帧变化后回显面板状态与 ruler 标记。"""
        engine = self.engine
        layer = engine.get_active_layer() if hasattr(engine, 'get_active_layer') else None
        tracks = getattr(layer, 'grade_keyframes', None) or {}
        frame = getattr(engine, 'current_grade_frame', None)

        panel = getattr(self, 'grading_panel', None)
        if panel is not None and hasattr(panel, 'set_keyframe_state'):
            has_kf_here = any(
                t.get_keyframe(int(frame)) is not None
                for t in tracks.values()
            ) if frame is not None else False
            panel.set_keyframe_state(
                bool(getattr(panel, 'keyframe_mode', False)),
                track_count=len(tracks),
                has_current_frame_kf=has_kf_here,
            )

        # ruler 标记：track 帧号为源帧，ruler 为 clip 本地帧，需经转换
        try:
            source_frames = engine.active_grade_keyframe_frames()
            converter = getattr(self, '_color_source_frame_to_local_frame', None)
            if callable(converter):
                local_frames = [int(converter(int(f))) for f in source_frames]
            else:
                local_frames = [int(f) for f in source_frames]
            bar = getattr(getattr(self, 'canvas', None), 'video_control_bar', None)
            if bar is not None and hasattr(bar, 'set_keyframe_frames'):
                bar.set_keyframe_frames(local_frames)
        except Exception:
            pass

    def on_keyframe_mode_toggled(self, checked: bool) -> None:
        """P1-COLOR-03: 关键帧录制模式开关。"""
        frame = getattr(self.engine, 'current_grade_frame', None)
        if checked and frame is None:
            self.status_bar.showMessage(
                I18N.tr("grading.keyframe_need_video",
                        "关键帧需要视频：先播放或定位到某一帧"), 3000)
        self._refresh_grade_keyframe_ui()

    def on_keyframe_snapshot_requested(self) -> None:
        """P1-COLOR-03: 当前帧为所有已动画参数写关键帧。"""
        engine = self.engine
        frame = getattr(engine, 'current_grade_frame', None)
        if frame is None:
            self.status_bar.showMessage(
                I18N.tr("grading.keyframe_need_video",
                        "关键帧需要视频：先播放或定位到某一帧"), 3000)
            return
        layer = engine.get_active_layer()
        if layer is None:
            return
        from src.features.color.core.grade_keyframes import (
            GRADE_KEYFRAME_PARAMS, parse_mask_track_name,
            record_mask_keyframe,
        )
        writable = [
            name for name in list(layer.grade_keyframes.keys())
            if name in GRADE_KEYFRAME_PARAMS and name in layer.grading_params
        ]
        # P1-COLOR-09: mask.<shape_id>.<field> 轨道一并快照（值取形状
        # 当前属性，与 grade 参数"当前值即关键帧"语义一致）
        mask_track_names = [
            name for name in list(layer.grade_keyframes.keys())
            if parse_mask_track_name(name) is not None
        ]
        if not writable and not mask_track_names:
            return
        # 先压栈后修改：undo 弹出的是快照前的轨道状态（与全库 push 约定一致）
        self.engine.history.push_state(
            self.engine.img_full_original,
            self.engine.layers,
            self.engine.active_layer_index,
            copy_image=False,
        )
        wrote = 0
        for name in writable:
            engine.record_grade_keyframe(frame, name, layer.grading_params[name])
            wrote += 1
        for name in mask_track_names:
            shape_id, field = parse_mask_track_name(name)
            if record_mask_keyframe(layer, int(frame), shape_id, [field]):
                wrote += 1
        if wrote:
            self._refresh_grade_keyframe_ui()
            self.status_bar.showMessage(
                I18N.tr("grading.keyframe_snapshot_done",
                        "已在帧 {frame} 记录 {count} 个参数关键帧",
                        frame=int(frame), count=wrote), 3000)

    def on_keyframe_remove_requested(self) -> None:
        """P1-COLOR-03: 删除当前帧的所有调色关键帧。"""
        engine = self.engine
        frame = getattr(engine, 'current_grade_frame', None)
        if frame is None:
            return
        layer = engine.get_active_layer()
        if layer is None:
            return
        has_kf_here = any(
            track.get_keyframe(int(frame)) is not None
            for track in (getattr(layer, 'grade_keyframes', None) or {}).values()
        )
        if not has_kf_here:
            return
        # 先压栈后修改，保证一次 Ctrl+Z 即可撤销删除
        engine.history.push_state(
            engine.img_full_original,
            engine.layers,
            engine.active_layer_index,
            copy_image=False,
        )
        engine.remove_grade_keyframe(int(frame))
        self._refresh_grade_keyframe_ui()

    def on_raw_params_changed(self, params) -> None:
        """RAW 面板参数变化：转发引擎（含失效播放缓存）。"""
        if self.engine.get_current_image() is None: return
        self.engine.on_raw_params_changed(params)
        self._invalidate_video_render_params_if_playing()

    def on_mixer_changed(self, params) -> None:
        """通道混合器参数变化：写引擎 + 播放态防抖渲染。"""
        if self.engine.get_current_image() is None: return
        self.engine.set_mixer_params(params)
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_warper_changed(self, grid_data) -> None:
        """变形网格（Warper）数据变化：写引擎 + 播放态防抖渲染。"""
        if self.engine.get_current_image() is None: return
        self.engine.set_warper_params(grid_data)
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_blur_sharpen_changed(self, params) -> None:
        """模糊/锐化（effects）参数变化：写引擎 + 播放态防抖渲染。"""
        if self.engine.get_current_image() is None: return
        self.engine.set_effects_params(params)
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_film_grain_changed(self, params) -> None:
        """Handle Film Grain changes with video playback debounce."""
        if self.engine.get_current_image() is None: return
        self.engine.set_film_grain_params(params)
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_film_grain_reseed_requested(self) -> None:
        """P1-COLOR-13: 显式换颗粒种子（确定性步进）。"""
        if self.engine.get_current_image() is None: return
        self.engine.reseed_film_grain()
        self._invalidate_video_render_params_if_playing()
        if not self._is_video_playing():
            self.request_render()

    def on_curves_changed(self, lut, curves_data) -> None:
        """RGB 曲线变化：应用 LUT 并把曲线数据存回当前图层。"""
        if self.engine.get_current_image() is None: return
        self.engine.apply_curves(lut)
        layer = self.engine.get_active_layer()
        if layer: layer.curves_data = curves_data
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    def on_hsl_curve_changed(self, lut, curves_data) -> None:
        """Handle HSL curve changes."""
        if self.engine.get_current_image() is None: return
        self.engine.apply_hsl_curve(lut)
        layer = self.engine.get_active_layer()
        if layer: layer.hsl_curve_data = curves_data
        # S5.C: 播放中调色参数变化时更新 cached render params
        self._invalidate_video_render_params_if_playing()
        if self._is_video_playing():
            self._debounce_grading_render()
        else:
            self.request_render()

    # ------------------------------------------------------------------
    # Video playback debounce helpers
    # ------------------------------------------------------------------

    def _invalidate_video_render_params_if_playing(self, *args) -> None:
        """S5.C: 如果视频已加载，清除缓存的渲染参数。"""
        if not self.video_controller or not hasattr(self.video_controller, 'invalidate_cached_render_params'):
            return

        is_loaded = False
        if hasattr(self.video_controller, 'is_video_loaded'):
            try:
                is_loaded = bool(self.video_controller.is_video_loaded())
            except Exception:
                is_loaded = False

        if is_loaded or getattr(self, 'current_file_is_video', False):
            self.video_controller.invalidate_cached_render_params()

    def _is_video_playing(self) -> bool:
        """P0-5: Check whether the video playback engine is currently playing."""
        return (self.video_controller is not None
                and getattr(self.video_controller, 'is_playing', False))

    def _debounce_grading_render(self) -> None:
        """P0-5: Request a debounced render for grading changes during playback.

        During playback we coalesce rapid slider events into a single render by
        restarting a short timer on every event.  When the timer finally fires
        (no new change within 25 ms), we issue ONE lightweight render with the
        latest parameters.

        When the video is *not* playing the caller should use the normal
        ``self.request_render()`` path for immediate responsiveness.
        """
        self._grading_debounce_timer.start()  # restart — always fire with latest params

    def _debounced_grading_render(self) -> None:
        """P0-5: Timer callback — submit the actual render after debounce window.

        During video playback the playback loop already uses the latest grading
        params (via invalidate_cached_render_params).  Queuing an extra HQ
        RENDER_FRAME here competes with RENDER_VIDEO_FRAME in the render thread
        and causes visible jitter, so we skip it while playing.
        """
        if self._is_video_playing():
            return  # playback loop handles rendering with updated params
        self.request_render()

    def _on_video_play_state_changed(self, is_playing: bool) -> None:
        """P0-5: Stop the grading debounce timer when playback stops.

        When playback stops we don't want a stale debounced render to fire;
        the stop/pause handler already triggers an immediate high-quality
        render, so any pending debounce is unnecessary.
        """
        if not is_playing:
            self._grading_debounce_timer.stop()
        else:
            # G-P0（黑屏修复）：播放开始时作废在途的降噪邻帧窗口请求。
            # 后台取帧即使已用独立解码会话，结果也不再投递（暂停态才有意义）。
            provider_obj = getattr(self, '_video_denoise_window_provider_obj', None)
            if provider_obj is not None:
                try:
                    provider_obj.clear()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # HSL / Curves picker helpers
    # ------------------------------------------------------------------

    def on_hsl_picker_toggled(self, active: bool) -> None:
        """Handle HSL Curves picker toggle - activate/deactivate eyedropper mode."""
        self.canvas.set_hsl_picker_mode(active)

    def on_hsl_color_for_curves(self, r: float, g: float, b: float) -> None:
        """Handle color picked from canvas for HSL curves - add control point."""
        # Add point to current curve
        self.hsl_curves_panel.add_point_from_color(r, g, b)
        # Deactivate picker after picking
        self.hsl_curves_panel.deactivate_picker()
        self.canvas.set_hsl_picker_mode(False)

    def on_curves_picker_toggled(self, active: bool) -> None:
        """Handle RGB Curves picker toggle - activate/deactivate eyedropper mode."""
        self.canvas.set_curves_picker_mode(active)

    def on_curves_color_for_curves(self, r: float, g: float, b: float) -> None:
        """Handle color picked from canvas for RGB curves - add control point."""
        # Add point to current curve
        self.curves_widget.add_point_from_color(r, g, b)
        # Deactivate picker after picking
        self.curves_widget.deactivate_picker()
        self.canvas.set_curves_picker_mode(False)

    # ------------------------------------------------------------------
    # Cache cleanup / AI plugin restore
    # ------------------------------------------------------------------

    def _cleanup_baked_cache(self) -> None:
        """清理不再被任何图层或 source bake 引用的孤立 EXR 缓存文件。

        AUD-2909: 引用集收集（读 engine 状态）仍在调用线程完成；目录扫描/
        删除（``cleanup_orphaned_cache``）移到线程池执行，不再阻塞 UI 线程。
        """
        active_files = set()
        for layer in self.engine.layers:
            if getattr(layer, 'baked_file_name', None):
                active_files.add(layer.baked_file_name)
            # AI Plugin Results
            for plugin in getattr(layer, 'plugins', []):
                ai_result_file = plugin.get("ai_result_file")
                if ai_result_file:
                    active_files.add(ai_result_file)
        if getattr(self.engine, 'source_bake_file_name', None):
            active_files.add(self.engine.source_bake_file_name)
        # 信号对象挂在宿主（QObject）上；宿主不是 QObject 时（无头测试桩）
        # 无 parent，靠 self._cache_cleanup_worker 持引用防 GC。
        signal_parent = self if isinstance(self, QObject) else None
        worker = _CacheCleanupWorker(active_files, signal_parent)
        worker.signals.finished.connect(self._on_cache_cleanup_finished)
        self._cache_cleanup_worker = worker  # 持引用防 GC（QRunnable 无 parent）
        QThreadPool.globalInstance().start(worker)

    def _on_cache_cleanup_finished(self) -> None:
        """AUD-2909: 缓存清理 worker 完成回调（主线程，释放引用）。"""
        self._cache_cleanup_worker = None

    def _restore_ai_plugin_results(self) -> None:
        """从 EXR 缓存加载 AI 插件处理结果并上传到 GPU 纹理。

        AUD-2909: 逐个 EXR 磁盘读取（多层多 AI 插件的 4K EXR）移到线程池；
        每条结果经 queued 信号回主线程写 CPU 缓存并提交 GPU 上传
        （``renderer.submit`` 只是入队，渲染线程消费），图层 dirty 标记
        留在主线程。工程切换时的迟到结果按图层是否仍在 engine 判定丢弃。
        """
        requests = []
        layers_by_id = {}
        for layer in self.engine.layers:
            found = False
            for plugin in getattr(layer, 'plugins', []):
                if plugin.get("type") == "ai" and plugin.get("ai_result_file"):
                    requests.append(
                        (layer.id, plugin["instance_id"], plugin["ai_result_file"]))
                    found = True
            if found:
                layers_by_id[layer.id] = layer
        if not requests:
            return
        if getattr(self, '_ai_plugin_restore_inflight', False):
            logger.debug("AI plugin restore already running; skipping")
            return
        self._ai_plugin_restore_inflight = True
        self._ai_plugin_restore_layers = layers_by_id
        signal_parent = self if isinstance(self, QObject) else None
        worker = _AiPluginRestoreWorker(requests, signal_parent)
        worker.signals.one_ready.connect(self._on_ai_plugin_result_ready)
        worker.signals.finished.connect(self._on_ai_plugin_restore_finished)
        self._ai_plugin_restore_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_ai_plugin_result_ready(self, layer_id: int, instance_id: int,
                                   result_array: Any) -> None:
        """AUD-2909: 主线程收 AI 插件结果 → CPU 缓存 + GPU 上传提交。"""
        from src.features.color.core.async_renderer import RenderCommand

        layer = getattr(self, '_ai_plugin_restore_layers', {}).get(layer_id)
        if layer is None or not any(
                candidate is layer for candidate in getattr(self.engine, 'layers', ())):
            # 工程已切换/图层已移除：丢弃迟到结果，不污染新工程缓存。
            return
        # 存入 CPU 缓存（保持磁盘 BGR 契约，供再次保存）
        self.engine.ai_plugin_result_cache[(layer_id, instance_id)] = result_array.copy()
        # GPU 上传契约是 RGB：磁盘 EXR 存的是 BGR，上传前
        # 转换（P0-COL-03：此前 BGR 原样上传导致红蓝互换）
        if self.engine.renderer:
            upload_rgb = (
                cv2.cvtColor(result_array, cv2.COLOR_BGR2RGB)
                if result_array.ndim == 3 and result_array.shape[2] == 3
                else result_array
            )
            self.engine.renderer.submit(
                RenderCommand.UPLOAD_AI_PLUGIN_RESULT, {
                    'layer_id': layer_id,
                    'instance_id': instance_id,
                    'image_array': upload_rgb
                }
            )
        layer.is_dirty = True

    def _on_ai_plugin_restore_finished(self) -> None:
        """AUD-2909: AI 插件结果恢复 worker 完成回调（主线程，释放引用）。"""
        self._ai_plugin_restore_inflight = False
        self._ai_plugin_restore_worker = None
        self._ai_plugin_restore_layers = {}

    # ------------------------------------------------------------------
    # Reset / Undo / Redo
    # ------------------------------------------------------------------

    def on_adjustments_reset(self) -> None:
        """重置当前图层的调整（discard_changes）并回填面板与 RAW。"""
        if self.engine.get_current_image() is None: return
        # AUD-2903：discard_changes 修改活动图层参数，先压栈保证可撤销。
        self._push_layer_history()
        self.engine.discard_changes(); self.request_render(); self.update_from_layer(self.engine.get_active_layer())
        self.raw_panel.reset_all()

    def on_reset_all_click(self) -> None:
        """Reset All 按钮：可选确认后重置全部图层、翻转与 RAW。"""
        if self.engine.get_current_image() is None: return
        # Reset tool modes (close inspector etc.)
        self.canvas.set_tool_mode('none')
        do_reset = True
        if self.config_manager.get("confirm_before_reset"):
            confirm = QMessageBox.question(self, "Confirm Reset", "Are you sure you want to reset ALL layers?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            do_reset = (confirm == QMessageBox.StandardButton.Yes)
        if do_reset:
            # AUD-2903：全局重置前压栈，使 Reset All（含翻转复位）可撤销。
            self._push_layer_history()
            self.engine.reset_all_layers()
            self.engine.reset_flip()
            self.raw_panel.reset_all()
            self.request_render()
            self.update_from_layer(self.engine.get_active_layer())
            self.status_bar.showMessage("All layers reset.", 3000)

    def on_layer_reset(self, index) -> None:
        """重置指定图层参数（先压栈保证可撤销，含关键帧轨道清空）。"""
        if 0 <= index < len(self.engine.layers):
            # 先压栈后重置，使 reset（含关键帧轨道清空）可撤销
            self.engine.history.push_state(
                self.engine.img_full_original,
                self.engine.layers,
                self.engine.active_layer_index,
                copy_image=False,
            )
            self.engine.layers[index].reset_params()
            # 刷新图层面板以更新调色图标等状态
            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
            if index == self.engine.active_layer_index:
                self.update_from_layer(self.engine.layers[index])
            self.request_render()
            self.status_bar.showMessage(f"Layer '{self.engine.layers[index].name}' reset.", 3000)

    def _edit_undo_redo_target(self, want_redo: bool = False):
        """E11: Edit 页激活时返回应驱动的时间线/媒体池命令栈持有者。

        焦点在媒体池 dock 内 → media_pool_panel；否则时间线
        （TimelineView）。对应栈无可执行历史时返回 None（回退引擎路由）。
        """
        if not getattr(self, "_edit_available", False):
            return None
        if getattr(self, "current_page", None) != "Edit":
            return None

        def _inside(widget, host) -> bool:
            while widget is not None:
                if widget is host:
                    return True
                widget = widget.parentWidget()
            return False

        from PyQt6.QtWidgets import QApplication
        focus = QApplication.focusWidget()
        media_pool = getattr(self, "media_pool_panel", None)
        if media_pool is not None and focus is not None and _inside(focus, media_pool):
            has_history = media_pool.can_redo() if want_redo else media_pool.can_undo()
            return media_pool if has_history else None

        panel = getattr(self, "timeline_panel", None)
        view = (
            panel.get_timeline_view()
            if panel is not None and hasattr(panel, "get_timeline_view")
            else getattr(panel, "timeline_view", None)
        )
        if view is None:
            return None
        has_history = view.can_redo() if want_redo else view.can_undo()
        return view if has_history else None

    def undo(self) -> None:
        """撤销：Composite 页激活时优先其 undo；SAM 模式撤销采样点；
        Edit 页时间线/媒体池命令栈有历史时驱动对应栈（E11）；
        否则回退引擎历史并同步 UI。"""
        # E11: 时间线编辑此前只进 TimelineView 独立命令栈，全局菜单
        # Undo 永远 disabled 且不作用于时间线历史。
        versions = getattr(self, "_clip_version_request_host", None)
        version_target = versions.history_target() if versions is not None else None
        if version_target is not None and not getattr(self.canvas, "sam_mode_active", False):
            version_target.undo()
            self.update_undo_redo_actions()
            return
        edit_target = self._edit_undo_redo_target()
        if edit_target is not None:
            edit_target.undo()
            self.update_undo_redo_actions()
            return

        # Composite page undo takes priority when active
        if self._composite_available and self.composite_page and self.composite_page._is_active:
            self.composite_page.undo()
            return

        # SAM mode interception: undo SAM points instead of layer history
        if self.canvas.sam_mode_active:
            self.engine.undo_sam_point()
            self.canvas.undo_sam_point_ui()
            return

        if self.engine.undo():
            self._sync_ui_after_undo_redo()

    def redo(self) -> None:
        """重做：Composite 页激活时优先其 redo；Edit 页命令栈有历史时
        驱动对应栈（E11）；否则引擎历史并同步 UI。"""
        versions = getattr(self, "_clip_version_request_host", None)
        version_target = versions.history_target(redo=True) if versions is not None else None
        if version_target is not None and not getattr(self.canvas, "sam_mode_active", False):
            version_target.redo()
            self.update_undo_redo_actions()
            return
        edit_target = self._edit_undo_redo_target(want_redo=True)
        if edit_target is not None:
            edit_target.redo()
            self.update_undo_redo_actions()
            return

        # Composite page redo takes priority when active
        if self._composite_available and self.composite_page and self.composite_page._is_active:
            self.composite_page.redo()
            return

        if self.engine.redo():
            self._sync_ui_after_undo_redo()

    def _sync_ui_after_undo_redo(self) -> None:
        """撤销/重做后同步更新所有 UI 面板。"""
        # A worker started before undo must not overwrite the restored store.
        service = getattr(self, "tracking_service", None)
        if service is not None:
            service.invalidate()
        self._tracker_single_step_target = None
        stabilizer = getattr(self, "stabilization_service", None)
        if stabilizer is not None:
            stabilizer.invalidate()
            self.stabilization_plan_service.invalidate()
            self._stabilizer_params_timer.stop()
            self._stabilizer_task = None
            self.tracker_panel.stabilizer.set_running(False)
            self._sync_stabilizer_panel()
        # 更新画布
        self.update_canvas()

        # 更新图层列表
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)

        # 更新调色面板
        layer = self.engine.get_active_layer()
        if layer:
            self.update_from_layer(layer)
            self.mask_panel.update_shapes_list(layer.mask_shapes)
            self.plugin_panel.set_layer(layer)

        # 更新遮罩覆盖层
        self.canvas.update_mask_overlay(layer)

        shape_id = getattr(self.canvas, "active_shape_id", None)
        if shape_id and layer is not None and layer.get_mask_item_by_id(shape_id) is not None:
            self.on_mask_shape_selected(shape_id)
            self._load_tracker_binding(shape_id)
        else:
            self._on_tracker_shape_selected(None)

        # P1-COLOR-03: 撤销/重做会改变关键帧轨道，回显 ◆ 按钮态与 ruler 标记
        try:
            self._refresh_grade_keyframe_ui()
        except Exception:
            pass

        # Engine history also owns whole-frame stabilization. Commit the
        # restored snapshot to the clip so Edit/export/save see the same frame.
        if stabilizer is not None:
            self._commit_stabilizer_change()

    def on_color_picked(self, h, s, v, first) -> None:
        """画布取色器拾色：把 HSV 值交给 qualifier 面板（拖拽加色逻辑）。

        Args:
            h/s/v: 拾取的色相/饱和度/明度（HSV，与 shader 的
                rgb_to_qualifier_hsv 同一语义）。
            first: True 表示拖拽起点（重置 qualifier 范围）。
        """
        # Delegate to qualifier panel to handle drag-to-add logic
        self.qualifier_panel.process_picked_color(h, s, v, reset_ranges=first)
        # Parameters are updated via signal from qualifier_panel to engine
