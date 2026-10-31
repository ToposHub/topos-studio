"""DeliverControllerMixin — export/deliver handlers extracted from MainWindow.

职责（Responsibilities）:
    - 交付页（Deliver）控制层：单文件导出（panel/quick export，支持
      Color 与 Composite 两个来源、色彩管理、ICC 嵌入、按格式转换位深）、
      LUT 导出（Delivery Matrix 有损确认）、时间线视频导出（后台
      QThread + 状态栏暂停/取消控件 + 已用时长统计）、Export As /
      SRT 字幕导出，以及批量导出队列（checkpoint 断点续跑、暂停/恢复、
      队列持久化与恢复提示）。
    - 本 Mixin 不持有状态模型：所有 ``self.*`` 属性引用解析到宿主
      MainWindow 实例（engine、crop_panel、config_manager、
      _video_exporter 等）。

与引擎/页面的边界（Boundary）:
    - 像素获取归 ``engine``（get_current_image(force_full_res=True)）与
      composite 离线渲染（``_render_composite_full_res``）；本层只做
      编码/落盘/UI 反馈，不直接操作 OpenGL。
    - 导出对话框/导出面板归 shared.export UI；本层消费其 settings
      字典（path/options/export_source 等）并驱动 FileIO /
      TimelineExporter / LutExporter。

信号协议（Signal protocol）:
    - 入站：导出面板的 export_requested、Quick Export 对话框确认、
      批量队列进度信号（``_connect_full_resolution_progress_signal``）。
    - 出站：无自定义 Qt 信号；通过 exporter 的 on_progress/
      on_finished/on_error 回调更新状态栏与进度条，通过
      ``_open_export_folder`` 打开输出目录。
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time

import numpy as np

from PyQt6.QtWidgets import QApplication, QMessageBox, QFileDialog

from src.shared.io.file_io import FileIO
from src.shared.export.export_panel import generate_export_filename
from src.shared.export.batch_queue_store import (
    BatchQueueItem,
    STATUS_PENDING,
    STATUS_RENDERING,
    STATUS_DONE,
    STATUS_FAILED,
    save_batch_queue,
    load_batch_queue,
    clear_batch_queue,
    has_restorable_items,
)
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


def _config_bool(value, default: bool = False) -> bool:
    """Safely read boolean config value, handling None correctly.

    Unlike `value or default`, this returns False when value is explicitly False.
    """
    return default if value is None else bool(value)


def _encode_composite_linear_to_srgb(img: np.ndarray,
                                      config_manager=None) -> np.ndarray:
    """Encode a scene-linear composite readback for a still-image export.

    The composite graph runs in linear light and the export readback is
    linear; display formats need an OETF applied to match the preview (whose
    viewer applies a linear→display ODT for display only). This honours the
    user's ``color.output.image_*`` preferences (transfer + primaries) when
    set, so e.g. a Rec.709 output preference encodes via the BT.709 OETF
    instead of sRGB. Falls back to a builtin sRGB OETF when the preference
    keys are absent or the provider is unavailable. Only the first 3
    channels are transformed (works on BGR or RGB; alpha passes through).

    Args:
        img: (h, w, 3|4) float32 scene-linear readback.
        config_manager: optional ConfigManager; when supplied and the
            ``color.output.image_*`` keys are present, the output color space
            is resolved from them (smart-default aware).
    """
    # Resolve the user's output color-space preference (still-image scope).
    # Falls back to sRGB when keys are missing (descriptor_from_config applies
    # the smart-default logic), so behaviour is unchanged for users who never
    # configured color preferences.
    # [AUD-2508] 降级路径分相位收敛：import 缺失是预期环境差异（warning）；
    # 偏好解析失败是配置问题（warning + 堆栈）；只有 transform_color 的
    # 意外失败才说明色彩管线本身出 bug —— 用 error + 完整堆栈大声留痕，
    # 不再让一个宽泛 except 把三类原因静默折叠成同一条 sRGB 降级。
    try:
        from src.shared.color.color_space_descriptor import (
            descriptor_from_config, LINEAR_SCENE,
        )
        from src.shared.color.color_transform_provider import transform_color
    except ImportError as e:
        logger.warning(
            f"preference-driven image encode unavailable "
            f"(color provider missing: {e}); using sRGB fallback"
        )
    else:
        try:
            dst = descriptor_from_config(
                config_manager, "output", "image", is_video=False)
        except Exception as e:
            logger.warning(
                "failed to resolve output color space from preferences "
                f"({type(e).__name__}: {e}); using sRGB fallback",
                exc_info=True,
            )
        else:
            try:
                return transform_color(img, LINEAR_SCENE, dst, config_manager)
            except Exception as e:
                # 色彩管理意外错误：保留降级（导出不中断），但必须可诊断——
                # error 级 + 堆栈，导出与预览的色彩差异由此可溯源。
                logger.error(
                    f"color-management transform failed during image encode "
                    f"({type(e).__name__}: {e}); degrading to builtin sRGB "
                    f"OETF — exported colors may not match the preview",
                    exc_info=True,
                )
    # Builtin sRGB fallback (also covers: provider missing / bad preferences).
        rgb = img[..., :3]
        # BUG-A3-03：显示编码前负值先钳 0——超采样振铃/高增益产生的负值
        # 不得落入线性段被 12.92* 放大成越界编码（-0.5 → -6.46 回绕伪影）；
        # 正值高光保持不钳（维持 HDR 上滚，只做 OETF）。
        rgb = np.maximum(rgb, 0.0)
        mask = rgb <= 0.0031308
        converted = np.where(
            mask,
            12.92 * rgb,
            1.055 * np.power(rgb, 1.0 / 2.4) - 0.055,
        )
        out = img.copy()
        out[..., :3] = converted
        return out


class DeliverControllerMixin:
    """Mixin providing export and deliver handlers.

    Intended to be mixed into MainWindow so that all ``self.*`` attribute
    references resolve against the MainWindow instance.
    """

    # ------------------------------------------------------------------
    # Panel & Quick Export
    # ------------------------------------------------------------------

    def perform_panel_export(self, settings) -> None:
        """导出面板单文件导出：全分辨率渲染 → 合成应用 → 色彩管理 → 落盘。

        自动应用未确认的裁切；按 ``apply_composite`` 离线渲染当前
        composite 图（EXR/HDR/.toos 保持线性），转换导出色彩空间并按
        目标格式转换位深，成功后可选嵌入 ICC 并打开输出目录。

        Args:
            settings: 导出面板设置字典（path/options/apply_composite 等）。
        """
        # Auto-apply crop if tool is still active
        if self.crop_panel.is_crop_active():
            self.crop_panel.set_crop_active(False)
            QApplication.processEvents()

        path = settings["path"]
        # Apply preserve_metadata from config
        if "options" not in settings:
            settings["options"] = {}
        settings["options"]["preserve_metadata"] = _config_bool(self.config_manager.get("preserve_metadata"), default=True)
        self.status_bar.showMessage(f"Exporting to {os.path.basename(path)}...", 0)
        QApplication.processEvents()

        full_res_img = self.engine.get_current_image(force_full_res=True)

        if full_res_img is None:
            self.status_bar.showMessage("Export failed: Could not render image.", 5000)
            QMessageBox.critical(self, "Export Error", "Failed to render full resolution image.")
            return

        # 应用 Composite 合成效果（与批量导出一致）。
        # 单文件导出用当前内存里的 composite 图（composite_page.node_graph），
        # 因为用户正在编辑的就是这张图——直接离线渲染当前节点图即可，
        # 不需要从 .topos 重新读取（那是批量导出多文件才需要的）。
        apply_composite = settings.get("apply_composite", False)
        if (apply_composite and full_res_img is not None
                and getattr(self, '_composite_available', False)
                and getattr(self, 'composite_page', None) is not None):
            # EXR/HDR keeps scene-linear (HDR scene-referred); display formats
            # get linear→sRGB encoded inside _render_composite_full_res so the
            # export matches the preview (whose viewer applies the ODT).
            # 2026-09-21 复查：.toos 浮点档同保持线性（F2 契约 half=线性，
            # 编码器冻结 transfer=8）——此前显示编码后落盘被 linear 标签
            # 二次解读（发灰）。
            keep_linear = FileIO.toos_export_keeps_linear(
                path, settings.get("options"))
            try:
                composite_img = self._render_composite_full_res(
                    full_res_img, keep_linear=keep_linear
                )
                if composite_img is not None:
                    full_res_img = composite_img
                else:
                    logger.warning(
                        "Single-file export: composite render returned None, "
                        "using Color output"
                    )
            except Exception as e:
                logger.error(
                    f"Single-file export: composite render failed: {e}, "
                    f"using Color output"
                )

        # Color management: convert to export color space if needed
        source_space = getattr(self, '_source_color_space', None)
        full_res_img = self.color_manager.convert_for_export(full_res_img,
                                                             source_space=source_space)

        # --- 根据导出格式安全转换数据类型 ---
        # [AUD-2507] '.webp' 与其他 8bit 显示格式同组（OpenCV WebP 编码
        # 不接受 float32/uint16 输入）
        ext = os.path.splitext(path)[1].lower()
        if full_res_img.dtype == np.float32:
            if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp']:
                full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 255.0).astype(np.uint8)
            elif ext in ['.tif', '.tiff']:
                full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 65535.0).astype(np.uint16)
        elif full_res_img.dtype == np.uint16:
            if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp']:
                full_res_img = (full_res_img / 256).astype(np.uint8)

        if FileIO.save_image(path, full_res_img, options=settings["options"]):
            # Embed ICC profile in exported file (JPEG/PNG/TIFF only)
            if _config_bool(self.config_manager.get("embed_icc_profile"), default=True):
                self.color_manager.embed_icc(path)

            self.status_bar.showMessage(f"Export successful: {os.path.basename(path)}", 5000)
            self.config_manager.set("last_path", os.path.dirname(path)); self.config_manager.save_config()
            self._open_export_folder(path)
        else:
            self.status_bar.showMessage("Export failed!", 5000)
            QMessageBox.critical(self, "Export Error", f"Failed to save image to {path}")

    def perform_quick_export(self, settings) -> None:
        """执行导出 — 支持 Color 和 Composite 两个来源。"""
        # Auto-apply crop if tool is still active
        if self.crop_panel.is_crop_active():
            self.crop_panel.set_crop_active(False)
            QApplication.processEvents()

        path = settings["path"]
        # Apply preserve_metadata from config
        if "options" not in settings:
            settings["options"] = {}
        settings["options"]["preserve_metadata"] = _config_bool(self.config_manager.get("preserve_metadata"), default=True)

        # 根据导出来源分发
        export_source = settings.get("export_source", "color")
        if export_source == "composite" and self._composite_available and self.composite_page:
            export_img = self._export_composite(settings)
        else:
            export_img = self._export_color(settings)

        if export_img is None:
            self.status_bar.showMessage("Export failed: Could not render image.", 5000)
            QMessageBox.critical(self, "Export Error", "Failed to render image.")
            return

        # Color management: convert to export color space if needed
        source_space = getattr(self, '_source_color_space', None)
        export_img = self.color_manager.convert_for_export(export_img,
                                                           source_space=source_space)

        # 根据导出格式安全转换数据类型
        # [AUD-2507] '.webp' 与其他 8bit 显示格式同组（见 perform_panel_export）
        ext = os.path.splitext(path)[1].lower()
        if export_img.dtype == np.float32:
            if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp']:
                export_img = (np.clip(export_img, 0.0, 1.0) * 255.0).astype(np.uint8)
            elif ext in ['.tif', '.tiff']:
                export_img = (np.clip(export_img, 0.0, 1.0) * 65535.0).astype(np.uint16)
        elif export_img.dtype == np.uint16:
            # BUG-A3-02：与 perform_panel_export/_batch_export_single_file 同口径，
            # 8 位显示格式必须 /256 降位——uint16 直传编码器会写盘失败或被
            # 底层钳制，三条导出路径行为不一致。
            if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp']:
                export_img = (export_img / 256).astype(np.uint8)

        if FileIO.save_image(path, export_img, options=settings["options"]):
            # Embed ICC profile in exported file (JPEG/PNG/TIFF only)
            if _config_bool(self.config_manager.get("embed_icc_profile"), default=True):
                self.color_manager.embed_icc(path)

            self.status_bar.showMessage(f"Export successful: {os.path.basename(path)}", 5000)
            self.config_manager.set("last_path", os.path.dirname(path))
            self.config_manager.save_config()
            self._open_export_folder(path)
        else:
            self.status_bar.showMessage("Export failed!", 5000)
            QMessageBox.critical(self, "Export Error", f"Failed to save image to {path}")

    def _export_color(self, settings) -> 'np.ndarray | None':
        """导出 Color pipeline 输出（原有行为）。"""
        is_quick = settings.get("quick_export", True)
        target_size = settings.get("options", {}).get("resize")
        path = settings["path"]

        if is_quick:
            self.status_bar.showMessage(f"Quick exporting to {os.path.basename(path)}...", 0)
            QApplication.processEvents()

            export_img = self.engine.grab_viewport_image(target_size)

            if export_img is None:
                self.status_bar.showMessage("Quick export failed, falling back to full render...", 3000)
                export_img = self.engine.get_current_image(force_full_res=True)
        else:
            self.status_bar.showMessage(f"Exporting (high quality) to {os.path.basename(path)}...", 0)
            QApplication.processEvents()

            export_img = self.engine.get_current_image(force_full_res=True)

            if target_size and export_img is not None:
                import cv2
                export_img = cv2.resize(export_img, target_size, interpolation=cv2.INTER_LANCZOS4)

        return export_img

    def _export_composite(self, settings) -> 'np.ndarray | None':
        """导出 Composite pipeline 输出。"""
        is_quick = settings.get("quick_export", True)
        target_size = settings.get("options", {}).get("resize")
        path = settings["path"]
        # EXR/HDR keeps scene-linear; all display formats need sRGB encoding.
        # .toos 浮点档同保持线性（2026-09-21 复查：F2 契约 half=线性，
        # 显示编码后落盘会被 linear 标签二次解读）。
        keep_linear = FileIO.toos_export_keeps_linear(
            path, settings.get("options"))

        if is_quick and self.composite_page.renderer:
            # Quick: readback 已有 proxy 输出
            self.status_bar.showMessage(f"Quick exporting composite to {os.path.basename(path)}...", 0)
            QApplication.processEvents()

            export_img = self.composite_page.renderer.readback_output()

            if export_img is None:
                self.status_bar.showMessage("Composite readback failed, falling back to Color...", 3000)
                return self._export_color(settings)

            # readback_output() is scene-linear (the viewer's ODT is
            # display-only and not baked in). Encode for display formats.
            if not keep_linear:
                export_img = _encode_composite_linear_to_srgb(export_img, self.config_manager)

            if target_size:
                import cv2
                export_img = cv2.resize(export_img, target_size, interpolation=cv2.INTER_LANCZOS4)

            return export_img
        else:
            # Full-res: Color full render → upload to GPU → composite evaluate → readback
            self.status_bar.showMessage("Rendering full-res Color pipeline...", 0)
            QApplication.processEvents()

            color_img = self.engine.get_current_image(force_full_res=True)
            if color_img is None:
                return self._export_color(settings)

            self.status_bar.showMessage("Evaluating composite at full resolution...", 0)
            QApplication.processEvents()

            export_img = self._render_composite_full_res(
                color_img, keep_linear=keep_linear
            )

            if export_img is None:
                self.status_bar.showMessage("Composite export failed, falling back to Color...", 3000)
                return self._export_color(settings)

            if target_size:
                import cv2
                export_img = cv2.resize(export_img, target_size, interpolation=cv2.INTER_LANCZOS4)

            return export_img

    def _normalize_standalone_graph_for_batch(self, node_graph) -> None:
        """规范化独立图片图，使其在批量导出（无 timeline provider）下可渲染。

        背景：旧版本 NodeGraph.from_dict() 调用的 migrate_color_input_to_topos_color
        会把独立图片图的 ``color_input`` 节点错误迁移成
        ``timeline_clip_input → topos_color``，其中 timeline_clip_input 的
        timeline_id/clip_id/asset_id 全为空（无 timeline backing）。
        批量导出没有 timeline_clip_gpu_provider，这种空 source 节点求值时
        返回空 dict，导致整条图断流、composite 效果丢失。

        本方法就地修复（只改内存中的临时图，不碰磁盘 .topos）：把"空的
        timeline_clip_input"替换为 color_input 节点，从而复用 _render_composite_full_res
        里 color_texture_id 的注入机制（base_evaluator 会把 snapshot 的 color
        texture 注入到 color_input 节点）。

        判定"空"的条件：timeline_id、clip_id、asset_id 全为空 —— 这表示该节点
        没有任何真实的 timeline/clip 引用，纯粹是 migrate 生成的占位 source。

        Args:
            node_graph: 批量导出从 .topos 反序列化得到的临时 NodeGraph。
        """
        from src.features.composite.nodes.color_node import COLORNode
        from src.features.composite.nodes.base_node import Connection

        # 找出所有"空"的 timeline_clip_input（无真实 timeline/clip/asset 引用）
        empty_source_ids = []
        for nid, node in node_graph.nodes.items():
            if getattr(node, "node_type", "") != "timeline_clip_input":
                continue
            params = getattr(node, "parameters", {}) or {}
            tid = (params.get("timeline_id") or "").strip()
            cid = (params.get("clip_id") or "").strip()
            aid = (params.get("asset_id") or "").strip()
            if not tid and not cid and not aid:
                empty_source_ids.append(nid)

        if not empty_source_ids:
            return  # 没有空 source，图结构正常，无需处理

        logger.info(
            f"[BATCH] Normalizing standalone graph: replacing "
            f"{len(empty_source_ids)} empty timeline_clip_input node(s) "
            f"with color_input (nodes: {empty_source_ids})"
        )

        for src_id in empty_source_ids:
            # 收集该 source 节点的所有出边（src_port 可能是 "output"）
            outgoing = [c for c in node_graph.connections if c.src_node_id == src_id]

            # 移除旧节点（remove_node 会自动清掉它的连接）
            node_graph.remove_node(src_id)

            # 用同 id 创建 color_input 节点（保持 id 稳定，便于下游连接）
            color_node = COLORNode(node_id=src_id)
            node_graph.add_node(color_node)

            # 重建出边：color_input 的输出端口是 "result"
            for old_conn in outgoing:
                node_graph.connections.append(Connection(
                    src_node_id=src_id,
                    src_port_name="result",
                    dst_node_id=old_conn.dst_node_id,
                    dst_port_name=old_conn.dst_port_name,
                ))

    def _resolve_export_frame(self, frame_number, node_graph) -> int:
        """Pick the animation frame for a still composite export (T02 fix).

        Precedence:
          1. An explicit ``frame_number`` passed by the caller (none today,
             reserved for a future "export current frame / specific frame"
             UI).
          2. The composite page's current playhead frame, when exporting the
             in-memory graph the user is editing (single-file / panel paths).
          3. Frame 0, when batch-exporting a deserialised temp graph (each
             file renders from its own first frame — no UI playhead exists).
        """
        if frame_number is not None:
            return max(0, int(frame_number))
        if node_graph is not None:
            # Batch path: deserialised temp graph, no UI playhead.
            return 0
        page = getattr(self, 'composite_page', None)
        if page is None:
            return 0
        getter = getattr(page, '_current_keyframe_frame', None)
        if callable(getter):
            try:
                return max(0, int(getter()))
            except Exception:
                return 0
        return 0

    def _resolve_export_fps(self, fps, node_graph) -> float:
        """Pick the fps for a still composite export (T02 fix)."""
        if fps is not None:
            try:
                return max(1.0, float(fps))
            except (TypeError, ValueError):
                pass
        if node_graph is None:
            page = getattr(self, 'composite_page', None)
            if page is not None:
                getter = getattr(page, 'comp_clip_fps', None)
                if callable(getter):
                    try:
                        return max(1.0, float(getter()))
                    except Exception:
                        pass
        return 24.0

    def _render_composite_full_res(self, color_img: 'np.ndarray',
                                   node_graph=None,
                                   keep_linear: bool = False,
                                   frame_number: 'int | None' = None,
                                   fps: 'float | None' = None,
                                   render_pass: str = "beauty") -> 'np.ndarray | None':
        """执行全分辨率 composite 求值 + readback。

        Color 全分辨率输入已经由 ``AsyncRenderer`` 在线程中生成；若
        CompositeRenderer 已启动，CompositeExportProvider 会把这次求值
        投递到同一渲染线程上下文。无 Composite render thread 的 CLI/测试
        场景才保留独立 context fallback。

        Uses CompositeExportProvider to avoid deep imports from composite.render.

        Args:
            color_img: BGR float32/uint8 ndarray (Color pipeline 输出).
            node_graph: 可选的 NodeGraph 实例。批量导出时传入从 .topos 反序列化的
                临时图，离线渲染该文件自己的 composite，不触碰 UI 的 composite_page。
                为 None 时（单文件导出路径）回退到 self.composite_page.node_graph。
            keep_linear: 为 True 时跳过 linear→sRGB 编码，返回场景线性结果。
                仅 HDR/EXR 导出需要；其余显示格式默认编码为 sRGB 以匹配预览
                （viewer 的 linear→sRGB ODT 仅作用于显示，导出路径本会跳过）。
            frame_number: 动画求值的目标帧。None（默认）时：

                * 单文件 / 面板导出（``node_graph`` 也为 None）：取
                  ``composite_page._current_keyframe_frame()``，与预览保持一致
                  （P0 基线 §3.4 修复：之前静默落到第 0 帧，与时间线/预览分歧）。
                * 批量导出（传入了反序列化的临时图）：保留第 0 帧，因为每个文件
                  都是从自己的起始帧开始离线渲染，没有 UI 播放头概念。

            fps: 合成帧率，传给 ``NodeGraph.snapshot`` 供表达式 ``time`` 使用。
                None 时回退到 ``composite_page.comp_clip_fps()``（单文件路径）或
                24.0（批量路径）。
            render_pass: Semantic pass to read back from the evaluated graph.
                ``beauty`` is the default; supported auxiliary passes are
                selected without changing the graph or its cached inputs.
        """
        try:
            from src.features.composite.export_provider import CompositeExportProvider
        except ImportError:
            raise RuntimeError("Composite feature is unavailable")

        h, w = color_img.shape[:2]

        # The live CompositeRenderer owns the only valid export GL context.
        # Build a CPU-only snapshot here and let that thread upload the color
        # source.  Creating a temporary context/texture in the controller
        # would still execute OpenGL on the UI thread even though evaluation
        # itself is dispatched asynchronously.
        render_thread = getattr(
            getattr(self, 'composite_page', None), 'renderer', None
        )
        render_export = getattr(render_thread, 'render_export', None)
        render_thread_ready = callable(render_export)
        if render_thread_ready:
            if getattr(render_thread, 'is_running', True) is False:
                render_thread_ready = False
            is_running = getattr(render_thread, 'isRunning', None)
            if render_thread_ready and callable(is_running):
                try:
                    render_thread_ready = bool(is_running())
                except Exception:
                    render_thread_ready = False

        if render_thread_ready:
            try:
                ng = (
                    node_graph
                    if node_graph is not None
                    else self.composite_page.node_graph
                )
                resolved_frame = self._resolve_export_frame(frame_number, node_graph)
                resolved_fps = self._resolve_export_fps(fps, node_graph)
                snapshot = ng.snapshot(
                    color_texture_id=0,
                    color_width=w,
                    color_height=h,
                    frame_number=resolved_frame,
                    fps=resolved_fps,
                )
                interactive_scheduler = None
                if node_graph is None:
                    interactive_scheduler = getattr(
                        render_thread, '_scheduler', None
                    )
                provider = CompositeExportProvider(self.config_manager)
                export_result = provider.render_full_res(
                    snapshot=snapshot,
                    interactive_scheduler=interactive_scheduler,
                    render_pass=render_pass,
                    color_image=color_img,
                    render_thread=render_thread,
                )
                if export_result.error:
                    logger.error(
                        f"Composite render-thread export error: {export_result.error}"
                    )
                img = export_result.image
                if (img is not None and not keep_linear
                        and str(render_pass or "beauty") == "beauty"):
                    img = _encode_composite_linear_to_srgb(
                        img, self.config_manager
                    )
                return img
            except Exception as e:
                logger.error(f"Render-thread composite export error: {e}")
                return None

        # [AUD-2500] Fallback 仅限无 UI surface 的 CLI/测试路径。UI 会话中
        # 绝不在 UI 线程创建/使用 GL context（.clinerules「UI 线程不调 GPU」）：
        # render thread 不可用时直接报错并返回 None，调用方（panel/quick/
        # batch）已有“回退 Color 输出”处理。此前该分支在 UI 线程创建
        # context/纹理，且 ctx 从不 release、makeCurrent 后无 doneCurrent——
        # 批量导出多文件时每个文件泄漏一个 GL context，并与 QOpenGLWidget
        # 的 surface 状态互相干扰（崩溃/渲染状态损坏风险）。
        gl_viewport = getattr(getattr(self, 'canvas', None), 'gl_viewport', None)
        if gl_viewport is not None:
            logger.error(
                "Composite export: render thread unavailable in a UI "
                "session; refusing to run OpenGL on the UI thread "
                "(exporter falls back to the Color output)"
            )
            return None

        ctx = None
        try:
            import moderngl
            ctx = moderngl.create_context()

            # BGR→RGB, uint8→[0,1] float, flipud for OpenGL
            import cv2
            rgb = cv2.cvtColor(color_img, cv2.COLOR_BGR2RGB)
            if rgb.dtype == np.uint8:
                rgb = rgb.astype(np.float32) / 255.0
            # dtype='f2' 上传需要 float16 字节布局，否则 moderngl 会报
            # data size mismatch（float32 数据 = 2x 期望字节数）
            tex_data = np.flipud(rgb).astype(np.float16)

            # 上传为 GPU texture (f2 = half float, GPU 精度足够)
            color_tex = ctx.texture((w, h), 3, data=tex_data.tobytes(), dtype='f2')
            color_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

            try:
                # 创建全分辨率 snapshot
                # 批量导出传入临时 node_graph（离线渲染该文件的 composite），
                # 单文件导出不传，回退到 UI 的 composite_page.node_graph
                ng = node_graph if node_graph is not None else self.composite_page.node_graph
                # T02 fix: resolve the animation frame so a still export of
                # an animated composite matches the preview instead of silently
                # rendering at frame 0. See P0 baseline §3.4.
                resolved_frame = self._resolve_export_frame(frame_number, node_graph)
                resolved_fps = self._resolve_export_fps(fps, node_graph)
                snapshot = ng.snapshot(
                    color_texture_id=color_tex.glo,
                    color_width=w,
                    color_height=h,
                    frame_number=resolved_frame,
                    fps=resolved_fps,
                )

                # 获取交互式调度器（如果可用）以复用 dirty tracking
                # 临时图的 generation/topology_revision 与交互式 scheduler 无关，
                # 种子数据无意义，故批量分支传 None 走全量求值
                interactive_scheduler = None
                if node_graph is None:
                    cp_renderer = getattr(self.composite_page, 'renderer', None)
                    if cp_renderer is not None:
                        interactive_scheduler = cp_renderer._scheduler

                # 使用 export provider 而非直接 import render 内部类
                provider = CompositeExportProvider(self.config_manager)
                export_result = provider.render_full_res(
                    snapshot=snapshot,
                    interactive_scheduler=interactive_scheduler,
                    render_pass=render_pass,
                    color_image=color_img,
                    render_thread=render_thread if render_thread_ready else None,
                )

                if export_result.error:
                    logger.error(f"Composite export render error: {export_result.error}")

                # Composite graph runs in scene-linear (the IDT decodes the
                # sRGB Color texture to linear at the graph boundary). The
                # export readback therefore returns LINEAR values. Display
                # formats (PNG/JPEG/TIFF) and the live viewer expect sRGB-
                # encoded pixels — the viewer applies a linear→sRGB ODT for
                # display only; the export path would otherwise skip it and
                # save linear values, which look dark. Encode here so EVERY
                # caller of _render_composite_full_res (panel export, quick
                # export, batch export, Output-node render) gets correct,
                # preview-matching output. Color-source exports never call
                # this method (they bypass the composite), so there is no
                # double-encode risk.
                img = export_result.image
                if (img is not None and not keep_linear
                        and str(render_pass or "beauty") == "beauty"):
                    img = _encode_composite_linear_to_srgb(img, self.config_manager)
                return img
            finally:
                color_tex.release()

        except Exception as e:
            logger.error(f"-res composite export error: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            # [AUD-2500] CLI/测试 fallback 也必须归还资源：显式释放
            # ModernGL context 包装。该路径没有 makeCurrent 任何 Qt
            # surface（UI 路径在上面已被拒绝），因此无需 doneCurrent。
            if ctx is not None:
                try:
                    ctx.release()
                except Exception:
                    logger.debug(
                        "composite fallback GL context release failed",
                        exc_info=True,
                    )

    def _open_export_folder(self, path: str) -> None:
        """Open the containing folder after export if configured."""
        if not self.config_manager.get("open_folder_after_export"):
            return
        folder = os.path.dirname(path)
        try:
            if platform.system() == 'Darwin':
                subprocess.Popen(['open', folder])
            elif platform.system() == 'Windows':
                os.startfile(folder)
            else:
                subprocess.Popen(['xdg-open', folder])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # LUT Export
    # ------------------------------------------------------------------

    def perform_lut_export(self, settings) -> None:
        """执行LUT导出"""
        from src.features.color.core.lut_exporter import LutExporter

        path = settings["path"]
        lut_format = settings.get("lut_format", "cube")
        lut_size = settings.get("lut_size", 33)
        lut_title = settings.get("lut_title", "Topos LUT")

        # 获取所有可见图层
        all_layers = self.engine.layers
        visible_layers = [l for l in all_layers if getattr(l, 'visible', True)]
        if not visible_layers:
            self.status_bar.showMessage("LUT export failed: No visible layers.", 5000)
            QMessageBox.critical(self, "LUT Export Error", "No visible layers found.")
            return

        # 检查是否有调色效果
        has_any_effects = any(l.has_grading_effects() for l in visible_layers)
        if not has_any_effects:
            reply = QMessageBox.question(
                self,
                I18N.tr("export.lut_no_grading_title", "LUT导出"),
                I18N.tr("export.lut_no_grading_msg", "当前图层没有调色效果，导出的LUT将是线性（无调整）。\n\n是否继续导出？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        # P1-COLOR-25: Delivery Matrix —— 空间/纹理效果无法用 LUT 表达，
        # 有损导出前必须显式确认
        from src.features.color.core.delivery_matrix import build_delivery_matrix_report
        from src.features.color.ui.delivery_matrix_dialog import DeliveryMatrixDialog
        lut_report = build_delivery_matrix_report(visible_layers, "lut")
        if not DeliveryMatrixDialog.confirm(self, lut_report):
            return

        self.status_bar.showMessage(f"Generating {lut_size}x{lut_size}x{lut_size} LUT...", 0)
        QApplication.processEvents()

        try:
            # 创建LUT导出器
            exporter = LutExporter()

            # 定义回调函数
            def on_finished(output_path) -> None:
                self.status_bar.showMessage(f"LUT exported successfully: {os.path.basename(output_path)}", 5000)
                self.config_manager.set("last_path", os.path.dirname(output_path))
                self.config_manager.save_config()
                exporter.cleanup()

            def on_error(error_msg) -> None:
                self.status_bar.showMessage("LUT export failed!", 5000)
                QMessageBox.critical(self, "LUT Export Error", f"Failed to generate LUT:\n{error_msg}")
                exporter.cleanup()

            def on_progress(percent) -> None:
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(percent)
                self.progress_bar.show()
                QApplication.processEvents()

            # 执行导出（应用所有可见图层效果）
            exporter.export_lut(
                visible_layers,
                path,
                size=lut_size,
                format_type=lut_format,
                title=lut_title,
                on_finished=on_finished,
                on_error=on_error,
                on_progress=on_progress
            )

            self.progress_bar.hide()

        except Exception as e:
            self.progress_bar.hide()
            self.status_bar.showMessage("LUT export failed!", 5000)
            QMessageBox.critical(self, "LUT Export Error", f"Failed to export LUT:\n{str(e)}")

    # ------------------------------------------------------------------
    # Video Export (Timeline)
    # ------------------------------------------------------------------

    def _set_video_export_controls(self, active: bool, paused: bool = False) -> None:
        """Show or hide the status-bar controls for a dialog-launched export."""
        pause_btn = getattr(self, "_video_export_pause_btn", None)
        cancel_btn = getattr(self, "_video_export_cancel_btn", None)
        if pause_btn is None or cancel_btn is None:
            return
        pause_btn.setVisible(active)
        cancel_btn.setVisible(active)
        pause_btn.setEnabled(active)
        cancel_btn.setEnabled(active)
        pause_btn.setText(
            I18N.tr("export.resume", "继续")
            if paused else I18N.tr("export.pause", "暂停")
        )

    def _toggle_video_export_pause(self) -> None:
        """Toggle pause for the video export started from Quick Export."""
        exporter = getattr(self, "_video_exporter", None)
        if exporter is None or not exporter.is_exporting:
            return
        paused = bool(getattr(self, "_video_export_paused", False))
        if paused:
            pause_started = getattr(self, "_video_export_pause_started_at", None)
            if pause_started is not None:
                self._video_export_paused_total += max(
                    0.0, time.perf_counter() - pause_started
                )
            exporter.resume()
            self._video_export_pause_started_at = None
            self._video_export_paused = False
            self._set_video_export_controls(True, False)
            self.status_bar.showMessage(
                I18N.tr("export.video_export_resumed", "视频导出已继续"), 0
            )
        else:
            exporter.pause()
            self._video_export_pause_started_at = time.perf_counter()
            self._video_export_paused = True
            self._set_video_export_controls(True, True)
            self.status_bar.showMessage(
                I18N.tr("export.video_export_paused", "视频导出已暂停"), 0
            )

    def _cancel_video_export(self) -> None:
        """Cancel a video export launched from the Quick Export dialog."""
        exporter = getattr(self, "_video_exporter", None)
        if exporter is None or not exporter.is_exporting:
            return
        exporter.cancel()
        cancel_btn = getattr(self, "_video_export_cancel_btn", None)
        if cancel_btn is not None:
            cancel_btn.setEnabled(False)
        self.status_bar.showMessage(
            I18N.tr("export.video_export_canceling", "正在取消视频导出..."), 0
        )

    def _video_export_elapsed(self) -> float:
        """视频导出已用秒数（扣除暂停时间）。"""
        started = getattr(self, "_video_export_started_at", None)
        if started is None:
            return 0.0
        paused_total = float(getattr(self, "_video_export_paused_total", 0.0))
        pause_started = getattr(self, "_video_export_pause_started_at", None)
        if pause_started is not None:
            paused_total += max(0.0, time.perf_counter() - pause_started)
        return max(0.0, time.perf_counter() - started - paused_total)

    def perform_video_export(self, settings) -> None:
        """
        执行视频导出 —— 在后台 QThread 中运行 TimelineExporter。

        流程：
        1. 获取当前 EditProject + TimelineSequence
        2. 构造 TimelineExportConfig（自动判断 IN_OUT / ENTIRE 范围）
        3. 创建 VideoExporter（QThread worker）
        4. 连接进度/成功/失败回调，启动后台导出
        """
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig,
            ExportSource,
            ExportRangeType,
            ExportCodec,
        )
        from src.shared.export.video_export_worker import VideoExporter

        # ── [AUD-2501] 防重入：导出进行中拒绝二次启动 ──
        # 此前直接覆盖 `_video_exporter` 引用：旧 QThread 失联（cleanup 只
        # 清理新的），两个 ffmpeg 可能同时写同一输出文件导致输出损坏，
        # 且旧 worker 的临时文件/句柄无人清理。
        existing_exporter = getattr(self, "_video_exporter", None)
        if existing_exporter is not None and bool(
            getattr(existing_exporter, "is_exporting", False)
        ):
            QMessageBox.warning(
                self,
                I18N.tr("export.video_export_in_progress_title", "视频导出进行中"),
                I18N.tr(
                    "export.video_export_in_progress_msg",
                    "已有视频导出正在运行。请等待其完成，或先取消当前导出，"
                    "再开始新的导出。",
                ),
            )
            return

        # ── 获取当前时间线与项目 ──
        edit_page = getattr(self, "edit_page", None)
        sequence = getattr(edit_page, "_current_timeline", None) if edit_page is not None else None
        project = getattr(self, "_current_edit_project", None)

        if sequence is None or project is None:
            QMessageBox.critical(
                self,
                I18N.tr("export.video_no_timeline_title", "无法导出视频"),
                I18N.tr("export.video_no_timeline_msg",
                        "当前没有可导出的时间线。请先在剪辑页面创建或打开一个时间线。"),
            )
            return

        # ── 校验 I/O 点 ──
        has_range = settings.get("has_range", False)
        range_start = settings.get("range_start")
        range_end = settings.get("range_end")
        if has_range and range_start is not None and range_end is not None and range_end < range_start:
            QMessageBox.warning(self, "Video Export", "Invalid In/Out range. Will export entire timeline.")
            has_range = False

        range_type = ExportRangeType.IN_OUT if has_range else ExportRangeType.ENTIRE

        # ── 映射 codec 字符串 → ExportCodec 枚举 ──
        codec_str = settings.get("codec", "h264")
        codec_map = {
            "h264": ExportCodec.H264,
            "hevc": ExportCodec.HEVC,
            "gif": ExportCodec.GIF,
            "prores_proxy": ExportCodec.PRORES_PROXY,
            "prores_lt": ExportCodec.PRORES_LT,
            "prores_422": ExportCodec.PRORES_422,
            "prores_422hq": ExportCodec.PRORES_422HQ,
            "prores_4444": ExportCodec.PRORES_4444,
            "prores_4444xq": ExportCodec.PRORES_4444XQ,
            "dnxhr_lb": ExportCodec.DNXHR_LB,
            "dnxhr_sq": ExportCodec.DNXHR_SQ,
            "dnxhr_hq": ExportCodec.DNXHR_HQ,
            "dnxhr_hqx": ExportCodec.DNXHR_HQX,
            "dnxhr_444": ExportCodec.DNXHR_444,
            "topos_proxy": ExportCodec.TOPOS_PROXY,
            "topos_lt": ExportCodec.TOPOS_LT,
            "topos": ExportCodec.TOPOS,
            "topos_hq": ExportCodec.TOPOS_HQ,
            "topos_4444": ExportCodec.TOPOS_4444,   # 复验 P1-08：六档全登记
            "topos_4444xq": ExportCodec.TOPOS_4444XQ,
            "topos_lp": ExportCodec.TOPOS_LP,       # v1.8（ADR-C056）帧间档
            "topos_netproxy": ExportCodec.TOPOS_NETPROXY,  # v1.9 P2 网络代理档
            # M4 R8（2026-09-21 复查补）：Quick Export/通用入口此前漏登
            # topos_raw——面板可选但此处 ValueError 断线
            "topos_raw": ExportCodec.TOPOS_RAW,
            "topos_image_sequence": ExportCodec.TOPOS_IMAGE_SEQUENCE,
        }
        # 复验 P1-08：未知 codec 显式失败——此前静默回退 H.264，用户选择
        # Topos 高档位却得到 H.264 文件（最小复现 10）
        codec_enum = codec_map.get(codec_str)
        if codec_enum is None:
            raise ValueError(
                f"Video Export: 未知 codec {codec_str!r}（合法："
                f"{sorted(codec_map)}）——拒绝静默回退 H.264")

        # ── 构造导出配置 ──
        include_audio = settings.get("include_audio", True)
        audio_codec = settings.get("audio_codec", "aac")
        audio_bitrate = settings.get("audio_bitrate", 192)

        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=getattr(sequence, "timeline_id", None),
            range_type=range_type,
            resolution=settings.get("resolution"),
            fps=settings.get("fps") or None,
            codec=codec_enum,
            quality=settings.get("quality"),  # CRF (H.264/HEVC)
            gif_color_count=int(settings.get("gif_color_count", 256)),
            has_alpha=bool(settings.get("alpha", False)),  # Topos 等 Alpha codec
            # M4 R8：档位二级选择透传（topos_raw 位深×比率 12 档 / LP 等
            # 家族档）——此前 Quick Export 路径丢失 profile，RAW 恒回退
            # 默认锚档 raw12-4
            codec_profile=settings.get("codec_profile"),
            include_audio=include_audio,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            audio_sample_rate=48000,
            audio_channels=2,
            output_path=settings["path"],
        )

        # 配置校验
        errors = config.validate()
        if errors:
            QMessageBox.critical(self, "Video Export Error", "\n".join(errors))
            return

        # P1-COLOR-25: Delivery Matrix —— 时间线视频导出走 CPU 管线，
        # 曲线/HSL/混色器/LUT/蒙版/限定器/插件等不会被应用；有损导出前确认
        from src.features.color.core.delivery_matrix import (
            engine_video_denoise_enabled, report_from_color_stacks,
            sequence_color_stacks, sequence_video_clip_ids)
        from src.features.color.ui.delivery_matrix_dialog import DeliveryMatrixDialog
        # P1-COLOR-13 复查：引擎开着视频降噪时如实报告导出会丢失
        denoise_on = False
        try:
            denoise_on = engine_video_denoise_enabled(self.engine)
        except Exception:
            pass
        video_report = report_from_color_stacks(
            sequence_color_stacks(sequence, project), "video",
            video_denoise_enabled=denoise_on,
            tracking_clip_ids=sequence_video_clip_ids(sequence))
        if not DeliveryMatrixDialog.confirm(self, video_report):
            return

        # ── 启动后台导出 ──
        self.status_bar.showMessage(
            I18N.tr("export.video_export_starting", "正在启动视频导出..."), 0
        )
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self._video_export_started_at = time.perf_counter()
        self._video_export_paused_total = 0.0
        self._video_export_pause_started_at = None
        self._video_export_paused = False
        self._set_video_export_controls(True, False)
        QApplication.processEvents()

        # 持有引用避免被 GC（导出结束后 cleanup 时清掉）
        self._video_exporter = VideoExporter(parent=self)

        def on_progress(current, total, percent) -> None:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(percent))
            elapsed = max(self._video_export_elapsed(), 1e-6)
            fps = float(current) / elapsed if current > 0 else 0.0
            fps_text = f" · {fps:.1f} fps" if fps > 0.0 else ""
            msg = I18N.tr("export.video_export_progress",
                          "正在导出视频: {current}/{total} 帧 ({percent}%){fps}",
                          current=current, total=total, percent=int(percent),
                          fps=fps_text)
            self.status_bar.showMessage(msg, 0)
            QApplication.processEvents()

        def on_finished(output_path) -> None:
            self.progress_bar.hide()
            filename = os.path.basename(output_path)
            self.status_bar.showMessage(
                I18N.tr("export.video_export_success", "视频导出成功: {filename}",
                        filename=filename),
                5000,
            )
            self.config_manager.set("last_path", os.path.dirname(output_path))
            self.config_manager.save_config()
            self._open_export_folder(output_path)
            self._cleanup_video_exporter()

        def on_error(error_msg) -> None:
            self.progress_bar.hide()
            if error_msg == "EXPORT_CANCELLED":
                self.status_bar.showMessage(
                    I18N.tr("export.video_export_cancelled", "视频导出已取消"), 5000
                )
            else:
                self.status_bar.showMessage(
                    I18N.tr("export.video_export_failed", "视频导出失败"), 5000
                )
                QMessageBox.critical(
                    self,
                    I18N.tr("export.video_export_failed", "视频导出失败"),
                    f"Failed to export video:\n{error_msg}",
                )
            self._cleanup_video_exporter()

        self._video_exporter.export(
            project=project,
            sequence=sequence,
            config=config,
            on_finished=on_finished,
            on_error=on_error,
            on_progress=on_progress,
            on_post_export=settings.get("_post_export"),
        )

    def _cleanup_video_exporter(self) -> None:
        """清理 VideoExporter 资源。"""
        if getattr(self, "_video_export_paused", False):
            pause_started = getattr(self, "_video_export_pause_started_at", None)
            if pause_started is not None:
                self._video_export_paused_total += max(
                    0.0, time.perf_counter() - pause_started
                )
        self._set_video_export_controls(False, False)
        exporter = getattr(self, "_video_exporter", None)
        if exporter is not None:
            try:
                exporter.cleanup()
            except Exception as e:
                logger.warning(f"VideoExporter cleanup failed: {e}")
            self._video_exporter = None
        self._video_export_started_at = None
        self._video_export_pause_started_at = None
        self._video_export_paused = False

    # ------------------------------------------------------------------
    # Export File As (quick export dialog)
    # ------------------------------------------------------------------

    def export_file_as(self) -> None:
        """快速导出功能（支持图像、LUT 和视频导出）"""
        from src.shared.export.quick_export_dialog import QuickExportDialog

        last_path = (self.config_manager.get("default_export_path")
                     or self.config_manager.get("last_path")
                     or os.path.expanduser("~"))
        default_name = "untitled"
        orig_size = (0, 0)
        cache_size = (0, 0)

        if self.engine.img_full_original is not None:
            h, w = self.engine.img_full_original.shape[:2]
            orig_size = (w, h)

        # 获取缓存尺寸（img_high 的尺寸，最大 4000 像素）
        if self.engine.img_high is not None:
            h, w = self.engine.img_high.shape[:2]
            cache_size = (w, h)

        if self.current_file_path:
            default_name = os.path.splitext(os.path.basename(self.current_file_path))[0] + "_exported"

        # 检查当前图层是否有调色效果
        active_layer = self.engine.get_active_layer()
        has_grading = active_layer.has_grading_effects() if active_layer else False

        # 检测 Composite 输出是否可用
        has_composite_output = False
        if self._composite_available and self.composite_page:
            has_composite_output = self.composite_page.has_connected_output()

        # 提取当前时间线信息（用于视频导出页面）
        timeline_info = self._build_timeline_info()
        # Work mode drives the default export tab so the dialog opens on the
        # tab that matches the active workflow, regardless of the current page.
        from src.shared.feature_flags import get_work_mode
        work_mode = get_work_mode(self.config_manager)
        if work_mode == "image":
            initial_export_type = QuickExportDialog.EXPORT_IMAGE
        else:  # video (default)
            initial_export_type = QuickExportDialog.EXPORT_VIDEO

        dialog = QuickExportDialog(
            self,
            default_dir=last_path,
            default_name=default_name,
            original_size=orig_size,
            cache_size=cache_size,
            has_grading=has_grading,
            default_format=self.config_manager.get("default_export_format") or "JPEG",
            default_quality=self.config_manager.get("default_export_quality") or 85,
            has_composite_output=has_composite_output,
            timeline_info=timeline_info,
            initial_export_type=initial_export_type,
            work_mode=work_mode,
        )

        if dialog.exec():
            settings = dialog.get_settings()
            export_type = settings.get("export_type")
            if export_type == "lut":
                self.perform_lut_export(settings)
            elif export_type == "video":
                # [AUD-2502] codec 等导出参数校验失败以 ValueError 上抛是
                # perform_video_export 的既定契约（AI host bridge 等编程调用
                # 方依赖异常判定“启动失败”，见 ai_edit_host_bridge_impl.py
                # 的 start_export try/except）。但本方法是 Qt 事件处理器：
                # 异常不能就此逃进 Qt 信号分发（PyQt6 默认只打 stderr
                # traceback，用户看不到任何提示），必须就地拦截给出可见反馈。
                try:
                    self.perform_video_export(settings)
                except ValueError as exc:
                    logger.error(f"Video export rejected: {exc}")
                    self.status_bar.showMessage(f"Video export failed: {exc}", 7000)
                    QMessageBox.critical(
                        self,
                        I18N.tr("export.video_export_failed", "视频导出失败"),
                        str(exc),
                    )
            else:
                self.perform_quick_export(settings)

    # ==================== E12: Deliver 菜单宿主回调 ====================
    # 此前 on_export_current_source/settings/timeline/composite_graph、
    # on_queue_start/stop 六个 MenuActionDescriptor 因宿主回调缺失被
    # MenuBuilder 静默跳过，Deliver 菜单只剩 SRT 一项。

    def _deliver_workspace(self):
        """E12: 取 Deliver 工作区（DeliverWorkspace），不可用时返回 None。"""
        ws = getattr(self, "deliver_page", None)
        if ws is None or not hasattr(ws, "controller"):
            return None
        return ws

    def _goto_deliver_page(self) -> bool:
        """切到 Deliver 页；页面不可用时给状态提示并返回 False。"""
        if getattr(self, "_deliver_available", False) and self.deliver_page is not None:
            self.switch_to_page("Deliver")
            return True
        self.status_bar.showMessage("Deliver 页不可用", 5000)
        return False

    def on_export_timeline(self) -> None:
        """Deliver > Export Timeline（E12）：切到 Deliver 页并按当前
        设置面板草稿走“加入队列并开始”的完整守卫流程（preflight、
        有损交付确认与页面按钮一致）。"""
        if not self._goto_deliver_page():
            return
        ws = self._deliver_workspace()
        panel = getattr(ws, "settings_panel", None) if ws else None
        if panel is None or not hasattr(panel, "_on_add_and_start_clicked"):
            self.status_bar.showMessage("Deliver 导出面板不可用", 5000)
            return
        panel._on_add_and_start_clicked()

    def on_export_current_source(self) -> None:
        """Deliver > Export Current Source（E12）：切到 Deliver 页并把
        设置面板的时间线预选为当前活动时间线。"""
        if not self._goto_deliver_page():
            return
        ws = self._deliver_workspace()
        panel = getattr(ws, "settings_panel", None) if ws else None
        timeline_id = None
        timeline = getattr(getattr(self, "edit_page", None), "_current_timeline", None)
        if timeline is not None:
            timeline_id = getattr(timeline, "timeline_id", None)
        if panel is not None and timeline_id:
            set_active = getattr(panel, "set_active_timeline", None)
            if callable(set_active):
                try:
                    set_active(timeline_id)
                except Exception:  # noqa: BLE001 预选失败不阻断切页
                    logger.debug("prefill active timeline failed", exc_info=True)
        self.status_bar.showMessage(
            "Deliver 页已就绪：当前时间线已预选，请确认导出设置", 5000
        )

    def on_export_settings(self) -> None:
        """Deliver > Export Settings...（E12）：打开发送页的导出设置。"""
        if self._goto_deliver_page():
            self.status_bar.showMessage("请在 Deliver 设置面板配置导出参数", 5000)

    def on_export_composite_graph(self) -> None:
        """Deliver > Export Composite Graph（E12）：切到 Composite 页并
        打开快速导出（对话框在合成输出可用时提供 Composite Graph 导出）。"""
        if getattr(self, "_composite_available", False) and self.composite_page is not None:
            self.switch_to_page("Composite")
        self.export_file_as()

    def on_queue_start(self) -> None:
        """Deliver > Start Queue（E12）：启动视频导出队列。"""
        ws = self._deliver_workspace()
        controller = getattr(ws, "controller", None) if ws else None
        if controller is None:
            self.status_bar.showMessage("导出队列不可用", 5000)
            return
        self._goto_deliver_page()
        controller.start_queue()
        self.status_bar.showMessage("导出队列已启动", 4000)

    def on_queue_stop(self) -> None:
        """Deliver > Stop Queue（E12）：取消当前任务并暂停队列调度。"""
        ws = self._deliver_workspace()
        controller = getattr(ws, "controller", None) if ws else None
        if controller is None:
            self.status_bar.showMessage("导出队列不可用", 5000)
            return
        try:
            controller.cancel_current()
        except Exception:  # noqa: BLE001 取消失败仍要暂停调度
            logger.debug("cancel current queue job failed", exc_info=True)
        controller.pause_queue()
        self.status_bar.showMessage("导出队列已停止", 4000)

    # ==================== P10.3: SRT 字幕导出 ====================

    def export_srt(self) -> None:
        """导出当前时间线的字幕为 .srt 文件。

        从 edit_page 拿到当前 TimelineSequence，调用 srt_exporter.export_srt。
        弹出 QFileDialog 选择保存路径（默认在用户的导出目录）。
        """
        if not getattr(self, 'edit_page', None) or not self.edit_page._current_timeline:
            QMessageBox.warning(None, "Subtitle Export",
                                "No active timeline. Open a timeline with subtitle tracks first.")
            return

        timeline = self.edit_page._current_timeline
        # 检查是否有字幕轨
        if not timeline.subtitle_tracks:
            QMessageBox.information(None, "Subtitle Export",
                                    "No subtitle tracks on the current timeline.")
            return

        # 默认路径
        last_path = (self.config_manager.get("default_export_path")
                     or self.config_manager.get("last_path")
                     or os.path.expanduser("~"))
        default_name = "subtitles.srt"
        if getattr(timeline, 'name', None):
            base = os.path.splitext(os.path.basename(timeline.name))[0]
            default_name = f"{base}.srt"
        default_path = os.path.join(last_path, default_name)

        # 文件选择对话框
        file_path, _ = QFileDialog.getSaveFileName(
            None,
            "Export Subtitles (SRT)",
            default_path,
            "SubRip Subtitle (*.srt);;All Files (*)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".srt"):
            file_path += ".srt"

        # 执行导出
        from src.features.deliver.srt_exporter import export_srt
        try:
            ok = export_srt(timeline, file_path)
            if ok:
                clips_count = sum(
                    len(t.subtitle_clips)
                    for t in timeline.subtitle_tracks
                    if t.is_enabled and t.is_visible
                )
                QMessageBox.information(
                    None, "Subtitle Export",
                    f"Exported subtitles to:\n{file_path}\n\n({clips_count} clips)"
                )
            else:
                QMessageBox.critical(None, "Subtitle Export",
                                     "Failed to export subtitles. Check the log for details.")
        except Exception as exc:
            logger.exception("SRT export failed")
            QMessageBox.critical(None, "Subtitle Export",
                                 f"Export failed: {exc}")

    def _build_timeline_info(self):
        """
        从 edit_page 提取当前时间线信息，供视频导出页面使用。

        返回 dict:            has_timeline: bool — 是否有可导出的时间线
            name: str — 时间线名字（用作视频导出文件名默认值）
            duration_frames: int — 总帧数
            has_range: bool — 是否已设置 In/Out 点
            range_start/range_end: int — In/Out 点（帧）
            width/height: int — 时间线分辨率
            fps: float — 时间线帧率
        无可用时间线时返回 {"has_timeline": False, ...}。
        """
        empty = {
            "has_timeline": False,
            "name": "",
            "duration_frames": 0,
            "has_range": False,
            "range_start": 0,
            "range_end": 0,
            "width": 0,
            "height": 0,
            "fps": 0.0,
        }
        try:
            edit_page = getattr(self, "edit_page", None)
            if edit_page is None:
                return empty
            seq = getattr(edit_page, "_current_timeline", None)
            if seq is None:
                return empty

            from src.features.edit.core.timeline_v2 import (
                effective_timeline_duration_frames,
                resolve_timeline_in_out,
            )

            # An empty timeline is still a valid black deliverable.  Use the
            # shared effective duration so Deliver, Edit playback and export
            # expose the same default canvas and In/Out behavior.
            duration = effective_timeline_duration_frames(seq)
            stored_range_start = getattr(seq, "range_start", None)
            stored_range_end = getattr(seq, "range_end", None)

            resolved_range = resolve_timeline_in_out(
                stored_range_start,
                stored_range_end,
                duration,
            )
            has_range = resolved_range is not None
            range_start, range_end = resolved_range or (0, max(0, duration - 1))

            # 时间线名字（用作视频导出文件名）
            name = getattr(seq, "name", "") or ""

            # 分辨率：TimelineSequence 的 width/height 是直接字段（不是 settings.width）
            width = int(getattr(seq, "width", 0) or 0)
            height = int(getattr(seq, "height", 0) or 0)
            if width == 0 or height == 0:
                # 尝试从第一个可见 clip 的 asset 推断
                for track in getattr(seq, "video_tracks", ()) or ():
                    for clip in track.clips:
                        asset = None
                        project = getattr(self, "_current_edit_project", None)
                        if project is not None:
                            asset = project.get_asset_by_id(clip.asset_id) if hasattr(project, "get_asset_by_id") else None
                        if asset is not None:
                            md = getattr(asset, "metadata", None) or {}
                            w = md.get("width", 0)
                            h = md.get("height", 0)
                            if w and h:
                                width, height = int(w), int(h)
                                break
                    if width and height:
                        break

            # 帧率
            fps = 0.0
            seq_fps = getattr(seq, "fps", None)
            if seq_fps is not None:
                if hasattr(seq_fps, "to_fps"):
                    fps = float(seq_fps.to_fps())
                else:
                    try:
                        fps = float(seq_fps)
                    except (TypeError, ValueError):
                        fps = 0.0

            return {
                "has_timeline": duration > 0,
                "name": name,
                "duration_frames": duration,
                "has_range": bool(has_range),
                "range_start": int(range_start) if range_start is not None else 0,
                "range_end": int(range_end) if range_end is not None else 0,
                "width": width,
                "height": height,
                "fps": fps,
            }
        except Exception as e:
            logger.warning(f"_build_timeline_info failed: {e}")
            return empty

    # ------------------------------------------------------------------
    # Batch Export
    # ------------------------------------------------------------------

    def perform_batch_export(self, settings) -> None:
        """执行批量导出；Color full-res GPU pass 在线程中执行。"""
        # [AUD-2509] 防重入：批量导出进行中拒绝二次启动。此前直接覆盖引擎
        # 状态快照——快照会记录“已被第 N 个文件污染的引擎状态”（层列表/
        # raw 参数/file_path 均是批处理中间态），完成后恢复到错误状态；
        # _batch_index=0 还会让两条 singleShot 链交叉处理同一文件列表。
        # （暂停中的批量导出同样算“进行中”：应先恢复/取消，而不是重开。）
        if getattr(self, "_batch_running", False):
            QMessageBox.warning(
                self,
                "Batch Export",
                "A batch export is already in progress. Wait for it to "
                "finish, or cancel / resume it before starting a new one.",
            )
            return

        files = settings.get("files", [])
        if not files:
            QMessageBox.warning(self, "Batch Export", "No files to export. Please add files first.")
            return

        export_dir = settings.get("export_dir", "")
        if not export_dir or not os.path.isdir(export_dir):
            QMessageBox.warning(self, "Batch Export", "Please select a valid export directory.")
            return

        # 判断是否为"恢复续渲"：恢复的队列项与当前 files 匹配时复用其状态
        restored_items = getattr(self, '_restored_queue_items', None)
        is_resume = (
            restored_items is not None
            and len(restored_items) == len(files)
            and all(r.file_path == f for r, f in zip(restored_items, files))
        )

        # 自动保存当前正在编辑的图，防止批量导出时调色/composite 丢失。
        # 批量导出会重新从 .topos 读取渲染，没存 .topos 的图调色会丢。
        # 仅全新导出需要（续渲时状态已在恢复前持久化）。
        if not is_resume:
            self._auto_save_current_file_before_batch()

        # 保存引擎当前状态
        self._batch_original_image = self.engine.img_full_original.copy() if self.engine.img_full_original is not None else None
        self._batch_original_layers = None
        if self.engine.layers:
            self._batch_original_layers = []
            for layer in self.engine.layers:
                from src.features.color.core.layer import Layer
                new_layer = Layer.from_dict(layer.to_dict())
                new_layer.id = layer.id
                self._batch_original_layers.append(new_layer)
        self._batch_original_active_index = self.engine.active_layer_index
        self._batch_original_raw_params = None
        if hasattr(self.engine, 'current_raw_params') and self.engine.current_raw_params:
            self._batch_original_raw_params = self.engine.current_raw_params.copy()
        self._batch_original_file_path = self.engine.current_file_path
        self._batch_original_is_srgb = self.engine.is_current_image_srgb
        self._batch_original_raw_metadata = self.engine.current_raw_metadata.copy() if self.engine.current_raw_metadata else None
        self._batch_original_last_raw_params = self.engine._last_applied_raw_params.copy() if hasattr(self.engine, '_last_applied_raw_params') and self.engine._last_applied_raw_params else None

        # 初始化批量导出状态
        self._batch_settings = settings
        self._batch_files = files
        self._batch_export_dir = export_dir
        self._batch_index = 0
        self._batch_success_count = 0
        self._batch_failed = []
        self._batch_cancelled = False
        self._batch_file_processing = False
        # [AUD-2509] 批量导出生命周期标志：perform_batch_export 置位，
        # _batch_export_complete 复位；二次启动据此拒绝（见方法入口）。
        self._batch_running = True
        self._connect_full_resolution_progress_signal()
        # 暂停/恢复：暂停时不调度下一个文件，resume 时重新触发 singleShot 链
        self._batch_paused = False
        # 队列项状态（用于持久化和 UI 状态显示）
        if is_resume:
            # 恢复续渲：复用已恢复的队列项状态（保留 done/failed 标记）
            self._batch_items = list(restored_items)
            self._batch_skip_done = True
            self._restored_queue_items = None
            self._restored_queue_settings = None
        else:
            # 全新批量导出：所有项从 pending 开始
            self._batch_items = [BatchQueueItem(file_path=f) for f in files]
            self._batch_skip_done = False

        # 持久化初始队列（即使中途崩溃也能恢复进度）
        save_batch_queue(self._batch_items, settings)

        # 切换UI到批量导出模式
        self.export_panel.start_batch_ui(len(files))
        self.status_bar.showMessage(f"Starting batch export of {len(files)} files...", 0)

        # 开始处理第一个文件
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._process_next_batch_file)

    def _auto_save_current_file_before_batch(self) -> None:
        """批量导出前自动保存当前图的 .topos，防止调色/composite 丢失。

        批量导出会重新从 .topos 读取渲染，没存 .topos 的图调色会丢。
        这里只对 ``current_file_path``（当前正在编辑的图）做保存 —— 只有它
        在内存里有最新状态。队列里其他没存 .topos 的图无法自动保存（状态
        不在内存），需用户自己逐张打开编辑并保存。
        """
        current_file = getattr(self, 'current_file_path', None)
        if not current_file:
            return  # 没有当前文件，跳过

        # 当前图不在导出队列就没必要为它存
        try:
            queue_files = self.export_panel.batch_list.get_all_files()
        except Exception:
            queue_files = []
        if current_file not in queue_files:
            return

        try:
            # 1. 先 flush composite（把内存里的节点图持久化）。
            #    对独立图片，flush 内部会 _save_from_composite() 带 composite_graph_data。
            if hasattr(self, '_flush_composite_to_edit_before_leave'):
                self._flush_composite_to_edit_before_leave()

            # 2. 再保存当前工程的 color layers。
            #    注意：不能只调 library save（它不带 composite_graph），
            #    也不能只调 composite save（它不带 color layers）。两者都要。
            from src.shared.project.save_router import SaveContext
            self.save_router.save_with_context(SaveContext.LIBRARY_SINGLE)
            logger.info(f"[BATCH] Auto-saved current file before batch export: {current_file}")
        except Exception as e:
            logger.warning(f"[BATCH] Auto-save current file failed: {e}")
            # 保存失败不阻塞批量导出，继续（最坏情况是该图用旧 .topos 或空 layers）

    def _cancel_batch_export(self) -> None:
        """取消批量导出"""
        self._batch_cancelled = True
        # The synchronous compatibility wrapper waits in a nested Qt event
        # loop, so the UI remains able to handle this button while the render
        # thread is busy.  Forward cancellation to the active request instead
        # of waiting for the current full-resolution file to finish naturally.
        try:
            cancel = getattr(self.engine, 'cancel_full_resolution', None)
            if callable(cancel):
                cancel()
        except Exception:
            logger.debug("Cancelling active full-resolution request failed", exc_info=True)
        self.status_bar.showMessage("Cancelling batch export...", 3000)

    def _connect_full_resolution_progress_signal(self) -> None:
        """Connect full-res progress once for the batch-export UI."""
        if getattr(self, '_full_res_progress_connected', False):
            return
        signal = getattr(self.engine, 'full_resolution_progress', None)
        connect = getattr(signal, 'connect', None)
        if not callable(connect):
            return
        connect(self._on_full_resolution_progress)
        self._full_res_progress_connected = True

    def _on_full_resolution_progress(
        self, request_id: str, percent: int, phase: str
    ) -> None:
        """Show the active Color render stage in the global batch progress."""
        if not getattr(self, '_batch_file_processing', False):
            return
        files = getattr(self, '_batch_files', None) or []
        total = len(files)
        if total <= 0:
            return
        current = max(0, min(total - 1, int(getattr(self, '_batch_index', 0))))
        bounded_percent = max(0, min(100, int(percent)))
        progress_bar = getattr(self, 'progress_bar', None)
        if progress_bar is not None:
            try:
                progress_bar.setRange(0, total * 100)
                progress_bar.setValue(current * 100 + bounded_percent)
                progress_bar.show()
            except Exception:
                logger.debug("full-resolution progress bar update failed", exc_info=True)
        # Avoid rewriting the status bar for every layer progress event.
        if phase != getattr(self, '_last_full_res_progress_phase', None):
            self._last_full_res_progress_phase = phase
            try:
                file_name = os.path.basename(files[current])
                self.status_bar.showMessage(
                    f"Rendering {current + 1}/{total}: {file_name} "
                    f"({bounded_percent}%, {phase})",
                    0,
                )
            except Exception:
                logger.debug("full-resolution progress status update failed", exc_info=True)

    def _pause_batch_export(self) -> None:
        """暂停批量导出。

        不中断当前正在渲染的文件，等当前文件完成后停止调度下一个。
        resume() 时从断点继续。
        """
        if not getattr(self, '_batch_files', None) or self._batch_index >= len(self._batch_files):
            return  # 没在跑批量导出，忽略
        self._batch_paused = True
        self.export_panel.set_batch_paused(True)
        self.status_bar.showMessage(
            I18N.tr("export.batch_paused", "批量导出已暂停，点击继续以恢复"), 0
        )

    def _resume_batch_export(self) -> None:
        """恢复（继续）批量导出。"""
        if not getattr(self, '_batch_files', None):
            return
        self._batch_paused = False
        self.export_panel.set_batch_paused(False)
        # If the current image is inside a checkpoint, its nested event loop
        # will observe the resume and finish the same item.  Scheduling a
        # second item here would re-enter the batch state machine.
        if getattr(self, "_batch_file_processing", False):
            return
        # 重新触发 singleShot 链（_process_next_batch_file 会从当前 _batch_index 继续）
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._process_next_batch_file)

    def _batch_checkpoint(self) -> bool:
        """Pump UI events and honor batch cancel/pause between render stages.

        Full-resolution Color rendering is submitted to ``AsyncRenderer``;
        this checkpoint keeps the batch state machine responsive while the
        synchronous compatibility wrapper waits for that worker result.  The
        remaining checkpoints cover loading, compositing, conversion, and
        saving, and cancellation is never misreported as a failed item.
        """
        QApplication.processEvents()
        while getattr(self, "_batch_paused", False) and not getattr(
            self, "_batch_cancelled", False
        ):
            QApplication.processEvents()
            time.sleep(0.01)
        return not getattr(self, "_batch_cancelled", False)

    def _process_next_batch_file(self) -> None:
        """处理下一个批量导出文件（UI 调度，GPU 渲染在线程中执行）"""
        # 暂停时直接返回，不调度下一个；resume() 会重新触发链
        if self._batch_paused:
            return

        total = len(self._batch_files)

        if self._batch_index >= total or self._batch_cancelled:
            self._batch_export_complete()
            return

        file_path = self._batch_files[self._batch_index]
        item = self._batch_items[self._batch_index] if self._batch_index < len(self._batch_items) else None
        self._batch_file_processing = True
        try:
            self.export_panel.set_batch_progress(self._batch_index, total)
            # 全局状态栏进度条：切到别的页面后仍可见（export_panel 可能被隐藏）
            self._update_global_batch_progress(self._batch_index, total)
            self.status_bar.showMessage(f"Exporting {self._batch_index + 1}/{total}: {os.path.basename(file_path)}...", 0)
            QApplication.processEvents()

            # 跳过已完成项（从恢复队列续渲时）
            if item is not None and item.status == STATUS_DONE and self._batch_skip_done:
                logger.info(f"Skipping already-done item: {os.path.basename(file_path)}")
            else:
                # 标记渲染中 + 记录开始时间 + 持久化
                if item is not None:
                    item.status = STATUS_RENDERING
                    item.started_at = time.time()
                    item.error = None
                    self._update_item_ui_status(self._batch_index, STATUS_RENDERING)
                    save_batch_queue(self._batch_items, self._batch_settings)

                try:
                    output_path = self._batch_export_single_file(file_path, self._batch_index + 1)
                    if output_path:
                        self._batch_success_count += 1
                        if item is not None:
                            item.status = STATUS_DONE
                            item.output_path = output_path
                            item.finished_at = time.time()
                            self._update_item_ui_status(self._batch_index, STATUS_DONE)
                        logger.info(f": {os.path.basename(file_path)} -> {os.path.basename(output_path)}")
                    else:
                        # Cancellation is cooperative.  Keep the current item
                        # pending so a later resume can retry it instead of
                        # recording a misleading render failure.
                        if item is not None:
                            if self._batch_cancelled:
                                item.status = STATUS_PENDING
                                item.error = "cancelled by user"
                            else:
                                self._batch_failed.append(os.path.basename(file_path))
                                item.status = STATUS_FAILED
                                item.error = "render returned no output"
                            item.finished_at = time.time()
                            self._update_item_ui_status(
                                self._batch_index,
                                STATUS_PENDING if self._batch_cancelled else STATUS_FAILED,
                            )
                        elif not self._batch_cancelled:
                            self._batch_failed.append(os.path.basename(file_path))
                except Exception as e:
                    self._batch_failed.append(os.path.basename(file_path))
                    if item is not None:
                        item.status = STATUS_FAILED
                        item.error = str(e)
                        item.finished_at = time.time()
                        self._update_item_ui_status(self._batch_index, STATUS_FAILED)
                    logger.error(f"exporting {file_path}: {e}")
                    import traceback
                    traceback.print_exc()
                # 每个文件处理完都持久化（崩溃也能恢复到这个文件）
                save_batch_queue(self._batch_items, self._batch_settings)
        except Exception as e:
            self._batch_failed.append(os.path.basename(file_path))
            if item is not None:
                item.status = STATUS_FAILED
                item.error = str(e)
                item.finished_at = time.time()
                self._update_item_ui_status(self._batch_index, STATUS_FAILED)
            logger.error(f"error in batch processing {file_path}: {e}")
            import traceback
            traceback.print_exc()
            save_batch_queue(self._batch_items, self._batch_settings)
        finally:
            self._batch_file_processing = False
            self._batch_index += 1
            # 暂停时不调度下一个（resume 时重新触发）
            if not self._batch_paused:
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, self._process_next_batch_file)

    def _update_global_batch_progress(self, current: int, total: int) -> None:
        """更新状态栏常驻进度条，切页面后仍可见。"""
        progress_bar = getattr(self, 'progress_bar', None)
        if progress_bar is not None:
            try:
                progress_bar.setRange(0, max(total * 100, 1))
                progress_bar.setValue(max(0, int(current)) * 100)
                progress_bar.show()
            except Exception as e:
                logger.debug(f"update global progress bar failed: {e}")

    def _update_item_ui_status(self, index: int, status: str) -> None:
        """更新队列列表中某项的状态显示（颜色点）。"""
        try:
            batch_list = getattr(self.export_panel, 'batch_list', None)
            if batch_list is None or index >= batch_list.count():
                return
            batch_list.set_item_status(index, status)
        except Exception as e:
            logger.debug(f"update item ui status failed: {e}")

    def _batch_extract_camera_wb(self, file_path: str) -> list:
        """提取 RAW 文件的相机白平衡（批量导出逐文件调用）。

        [AUD-2504] 根因修复：
        - 优先复用 ``load_image`` 刚在 FileIO 内打开的 RAW 句柄
          （``_current_raw_obj``，``_raw_lock`` 保护）——此前每个 RAW 文件
          被 ``rawpy.imread`` 打开两次（load 一次、WB 提取一次），第二次
          打开失败时 ``except Exception: pass`` 静默落到无日志的魔术值；
        - 提取失败不再静默：区分 rawpy 缺失（ImportError）与解码/读取
          错误（warning + 堆栈），回退值显式命名；
        - 回退值保持与 ``FileIO._get_normalized_camera_wb`` /
          ``ImageEngine`` 默认一致的「WB 未知」日光启发式（R×2/B×1.5，
          G 归一），保证同一文件在批量导出与交互渲染两条路径下、即使
          提取失败也产生相同的渲染结果（导出与预览语义一致）。

        Args:
            file_path: RAW 源文件路径。

        Returns:
            长度 4 的 G 归一白平衡增益列表 ``[r, 1.0, b, 1.0]``；永不抛异常。
        """
        wb_unknown_heuristic = [2.0, 1.0, 1.5, 1.0]

        # 快路径：load_image 已持有该文件打开的 RAW 句柄（锁内读取，与
        # raw worker 的 update_raw_decoding 串行化）。
        try:
            with FileIO._raw_lock:
                raw_obj = FileIO._current_raw_obj
                if (raw_obj is not None
                        and FileIO._current_raw_path == file_path):
                    return list(FileIO._get_normalized_camera_wb(raw_obj))
        except Exception:
            logger.warning(
                "[BATCH] reading camera WB from the cached RAW handle failed "
                "for %s; retrying with a one-shot open",
                file_path, exc_info=True,
            )

        # 慢路径：缓存句柄不可用/不匹配（非常规路径），自行打开一次。
        try:
            import rawpy
            with rawpy.imread(file_path) as raw:
                return list(FileIO._get_normalized_camera_wb(raw))
        except ImportError:
            logger.warning(
                "[BATCH] rawpy unavailable; camera WB unknown for %s; "
                "using daylight heuristic %s",
                file_path, wb_unknown_heuristic,
            )
        except Exception:
            logger.warning(
                "[BATCH] camera WB extraction failed for %s; using WB-unknown "
                "daylight heuristic %s",
                file_path, wb_unknown_heuristic, exc_info=True,
            )
        return list(wb_unknown_heuristic)

    def _batch_load_image(self, file_path: str) -> tuple:
        """在后台线程加载批量导出源图，UI 线程仅泵事件等待。

        [AUD-2505] 根因修复：``FileIO.load_image`` 是纯同步 I/O + 全量解码
        （rawpy RAW / PSD / 大 JPEG，4000 万像素 RAW 可达数秒），此前直接
        在 UI 线程调用导致整个窗口冻结（``_batch_checkpoint`` 只在阶段间
        泵事件，覆盖不到加载内部）。加载移入 daemon 线程后，等待循环沿
        用 ``_batch_checkpoint`` 的泵事件习语，绘制/取消/暂停事件持续
        流转；取消语义不变（加载不可中断，完成后由下一个 checkpoint
        生效）。

        Args:
            file_path: 源文件路径。

        Returns:
            ``FileIO.load_image`` 的 ``(full, high, low)`` 元组；加载抛错
            时对应元素为 None（与 load 失败同语义）。
        """
        result: dict = {"payload": (None, None, None)}

        def _load() -> None:
            try:
                result["payload"] = FileIO.load_image(file_path)
            except Exception:
                logger.error(
                    "[BATCH] image load failed: %s", file_path, exc_info=True
                )
                result["payload"] = (None, None, None)

        worker = threading.Thread(
            target=_load, name="BatchImageLoad", daemon=True)
        worker.start()
        while worker.is_alive():
            QApplication.processEvents()
            time.sleep(0.01)
        return result["payload"]

    def _batch_export_single_file(self, file_path: str, index: int) -> str:
        """导出单个文件；全分辨率 Color pass 在线程中完成。"""
        # 1. 加载图像（[AUD-2505] 解码在后台线程执行，UI 线程泵事件等待）
        if not self._batch_checkpoint():
            return None
        full, high, low = self._batch_load_image(file_path)
        if full is None:
            logger.error(f"to load image: {file_path}")
            return None
        if not self._batch_checkpoint():
            return None

        # 2. 加载 .topos 项目文件中的调色参数
        project_path = f"{os.path.splitext(file_path)[0]}.topos"
        if os.path.exists(project_path):
            project_data = self.save_router.load_single_asset_data(project_path)
            if isinstance(project_data, dict):
                from src.features.color.core.layer import Layer
                self.engine.layers = project_data.get('layers') or [Layer("Layer 1")]
                raw_params = project_data.get('raw_params')
                if raw_params and hasattr(self.engine, 'current_raw_params'):
                    self.engine.current_raw_params = raw_params
            elif isinstance(project_data, list):
                self.engine.layers = project_data
            else:
                from src.features.color.core.layer import Layer
                self.engine.layers = [Layer("Layer 1")]
        else:
            # BUG-A3-01: 无 .topos 侧车时也必须初始化 project_data，
            # 否则下方翻转状态恢复 `isinstance(project_data, dict)` 触发
            # UnboundLocalError，整批队列里未保存过 .topos 的文件全部失败。
            project_data = None
            from src.features.color.core.layer import Layer
            self.engine.layers = [Layer("Layer 1")]

        self.engine.active_layer_index = 0
        if not self._batch_checkpoint():
            return None

        # 3. 直接设置渲染所需的字段，不调用 set_image 以避免触发预览渲染。
        # render_full_resolution() 会把不可变源图提交给 AsyncRenderer；
        # 纹理创建、完整管线与读回均在线程上下文执行。
        self.engine.img_full_original = full.copy()
        self.engine.img_source_full = full.copy()
        self.engine.current_file_path = file_path

        # Restore flip state from project file
        if isinstance(project_data, dict):
            flip_state = project_data.get('flip_state')
            if flip_state:
                self.engine.set_flip_state(flip_state)

        # 设置 sRGB 标志和 RAW 元数据（full-res render request 依赖这些字段）
        is_raw = file_path and os.path.splitext(file_path)[1].lower() in self.engine.RAW_EXTENSIONS
        self.engine.is_current_image_srgb = not is_raw
        if not is_raw:
            self.engine.current_raw_metadata = {"camera_wb": [1.0, 1.0, 1.0, 1.0]}
            self.engine._last_applied_raw_params = {
                "enabled": False, "exposure": 0.0,
                "user_wb": [1.0, 1.0, 1.0, 1.0],
                "color_space": "sRGB", "gamma": "Linear"
            }
        else:
            # RAW 文件：从文件中提取真实的相机白平衡，避免使用上一个文件的残留值
            camera_wb = self._batch_extract_camera_wb(file_path)
            self.engine.current_raw_metadata = {"camera_wb": camera_wb}
            self.engine._last_applied_raw_params = {
                "enabled": False, "exposure": 0.0,
                "user_wb": list(camera_wb),
                "color_space": "sRGB", "gamma": "sRGB"
            }

        # 4. 提交全分辨率渲染并等待线程结果（不在此线程调用 OpenGL）
        full_res_img = self.engine.get_current_image(force_full_res=True)
        if full_res_img is None:
            logger.error(f"to render image: {file_path}")
            return None
        if not self._batch_checkpoint():
            return None

        # 4b. 可选：套用该文件自己的 composite 合成效果
        # 批量导出作为"渲染队列"：每个文件用自己 .topos 里保存的节点图，
        # 离线渲染最终效果。临时 NodeGraph 是局部变量，不触碰 UI 的 composite_page。
        apply_composite = self._batch_settings.get("apply_composite", False)
        if apply_composite and isinstance(project_data, dict) and full_res_img is not None:
            if not self._batch_checkpoint():
                return None
            composite_graph_data = project_data.get('composite_graph')
            if composite_graph_data:
                try:
                    from src.features.composite.core.node_engine import NodeGraph
                    temp_graph = NodeGraph.from_dict(composite_graph_data)
                    # 规范化独立图片图：旧版本 migrate_color_input_to_topos_color
                    # 会把 color_input 错误迁移成"空的 timeline_clip_input + topos_color"，
                    # 但批量导出没有 timeline provider，timeline_clip_input 会断流输出空。
                    # 把这种空 source 节点改回 color_input，复用 color_texture 注入机制。
                    self._normalize_standalone_graph_for_batch(temp_graph)
                    batch_keep_linear = os.path.splitext(file_path)[1].lower() == '.exr'
                    composite_img = self._render_composite_full_res(
                        full_res_img, node_graph=temp_graph,
                        keep_linear=batch_keep_linear,
                    )
                    if composite_img is not None:
                        full_res_img = composite_img
                    else:
                        logger.warning(
                            f"Composite render returned None for {file_path}, "
                            f"using Color output"
                        )
                except Exception as e:
                    logger.error(
                        f"Composite render failed for {file_path}: {e}, "
                        f"using Color output"
                    )
            else:
                # 该文件没有 composite 图，自动 fallback 到 Color 输出。
                # 用 warning（非 debug）：批量导出读不到 composite 通常意味着
                # 用户在 Composite 页加的效果没保存进 .topos（Color/Library 页
                # Ctrl+S 旧版本会丢 composite），需要可见日志帮助定位。
                logger.warning(
                    f"No composite graph found in .topos for {file_path}, "
                    f"using Color output. If you expected composite effects, "
                    f"re-open the image in Composite page and save (Ctrl+S) "
                    f"to persist the graph."
                )

        # 5. 生成输出文件名
        if not self._batch_checkpoint():
            return None
        output_filename = self._batch_generate_filename(file_path, index)
        output_path = os.path.join(self._batch_export_dir, output_filename)

        # 6. 格式转换
        # [AUD-2507] '.webp' 必须在 8bit 量化分支里：批量命名支持 WebP
        # （_batch_generate_filename 的 fmt_map），此前 float32 图直传
        # OpenCV WebP 编码器（不接受 float32）保存失败。
        ext = os.path.splitext(output_path)[1].lower()
        if full_res_img.dtype == np.float32:
            if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp']:
                full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 255.0).astype(np.uint8)
            elif ext in ['.tif', '.tiff']:
                full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 65535.0).astype(np.uint16)
        elif full_res_img.dtype == np.uint16:
            if ext in ['.jpg', '.jpeg', '.png', '.bmp', '.webp']:
                full_res_img = (full_res_img / 256).astype(np.uint8)

        # 7. 保存文件
        if not self._batch_checkpoint():
            return None
        options = self._batch_settings.get("options", {})
        if FileIO.save_image(output_path, full_res_img, options=options):
            return output_path
        # [AUD-2507] 保存失败与渲染失败必须可区分：此前统一返回 None，被
        # _process_next_batch_file 标成 "render returned no output"，把
        # IO/编码错误误导成渲染问题。抛出由调用方记录真实原因。
        raise OSError(f"failed to save output image: {output_path}")

    def _batch_generate_filename(self, input_path: str, index: int) -> str:
        """Generate output filename based on settings"""
        settings = self._batch_settings
        name, _ = os.path.splitext(os.path.basename(input_path))
        fmt_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr", "TIFF": ".tif", "WebP": ".webp"}
        output_ext = fmt_map.get(settings.get("format", "JPEG"), ".jpg")

        return generate_export_filename(
            mode=settings.get("naming_mode", "Original Filename"),
            original_name=name, output_ext=output_ext,
            index=index, seq_digits=settings.get("seq_digits", 3),
            suffix=settings.get("suffix", "_exported"),
            prefix=settings.get("prefix", "img_"),
            custom_pattern=settings.get("custom_pattern", "{name}_{n:03d}")
        )

    def _batch_export_complete(self) -> None:
        """批量导出完成，恢复引擎状态"""
        total = len(self._batch_files)
        self.export_panel.set_batch_progress(total, total)
        # 重置全局进度条
        progress_bar = getattr(self, 'progress_bar', None)
        if progress_bar is not None:
            try:
                progress_bar.setRange(0, max(total * 100, 1))
                progress_bar.setValue(total * 100)
            except Exception:
                pass

        # 恢复引擎原始状态
        # [AUD-2506] 无条件回写快照值：此前用 `is not None` 判断，把「快照
        # 值为 None（导出前就是空）」误当「未快照」跳过恢复——导出前未打开
        # 图时（current_file_path/layers/img_full_original 均为空），完成后
        # engine 残留最后一个批文件的状态（file_path/图层/raw 元数据），
        # 后续 Ctrl+S 会把当前（可能是空的）调色状态写进那个源文件的
        # .topos。快照字段在 perform_batch_export 中必定先于批处理写入，
        # None 即表示原始值为空。
        try:
            self.engine.layers = (
                self._batch_original_layers
                if self._batch_original_layers is not None
                else []
            )
            self.engine.active_layer_index = (
                self._batch_original_active_index
                if self._batch_original_active_index is not None
                else 0
            )
            if hasattr(self.engine, 'current_raw_params'):
                self.engine.current_raw_params = (
                    self._batch_original_raw_params
                    if self._batch_original_raw_params is not None
                    else {"enabled": False}
                )
            # None → 显式还原为「无当前文件」（引擎初始态），不残留批文件路径
            self.engine.current_file_path = self._batch_original_file_path
            self.engine.is_current_image_srgb = self._batch_original_is_srgb
            # raw 元数据/参数必须保持 dict 形态（渲染链用 .get 读取）；
            # 空快照回退到引擎初始默认值
            self.engine.current_raw_metadata = (
                self._batch_original_raw_metadata
                if self._batch_original_raw_metadata is not None
                else {"camera_wb": [2.0, 1.0, 1.5, 1.0]}
            )
            self.engine._last_applied_raw_params = (
                self._batch_original_last_raw_params
                if self._batch_original_last_raw_params is not None
                else {"enabled": False}
            )
            if self._batch_original_image is not None:
                self.engine.img_full_original = self._batch_original_image
                self.engine.img_source_full = self._batch_original_image.copy()
                low = FileIO._resize_to_fit(self._batch_original_image, FileIO.LOW_RES_SIZE)
                high = FileIO._resize_to_fit(self._batch_original_image, FileIO.HIGH_RES_SIZE)
                self.engine.set_image(self._batch_original_image, high, low, is_new_file=False)
            else:
                # 快照时无图：显式清空，不残留最后一个批文件的全分辨率源图
                self.engine.img_full_original = None
                self.engine.img_source_full = None
        except Exception as e:
            logger.error(f"restoring engine state: {e}")
            import traceback
            traceback.print_exc()

        # 恢复UI
        self.export_panel.finish_batch_ui()
        self.export_panel.set_batch_paused(False)
        self._batch_paused = False
        self._batch_file_processing = False
        # [AUD-2509] 生命周期结束：允许下一次批量导出启动
        self._batch_running = False

        # 队列文件清理：全部成功完成才删除；有失败/取消则保留供下次恢复
        has_unfinished = any(
            item.status in (STATUS_PENDING, STATUS_RENDERING, STATUS_FAILED)
            for item in getattr(self, '_batch_items', [])
        )
        if not has_unfinished and not self._batch_failed:
            clear_batch_queue()

        if self._batch_cancelled:
            msg = f"Batch export cancelled. Exported {self._batch_success_count}/{total} files."
            QMessageBox.information(self, "Batch Export Cancelled", msg)
            self.status_bar.showMessage(msg, 5000)
        elif self._batch_failed:
            msg = f"Exported {self._batch_success_count}/{total} files. Failed: {', '.join(self._batch_failed)}"
            QMessageBox.warning(self, "Batch Export", msg)
            self.status_bar.showMessage(msg, 5000)
        else:
            msg = f"Successfully exported {self._batch_success_count} files"
            QMessageBox.information(self, "Batch Export Complete", msg)
            self.status_bar.showMessage(msg, 5000)

        # 触发重新渲染当前图像
        self.request_render()

    # ------------------------------------------------------------------
    # 队列持久化：启动恢复 + 关闭兜底
    # ------------------------------------------------------------------

    def _has_restorable_queue(self) -> bool:
        """启动时检查是否存在未完成（可恢复）的队列项。"""
        try:
            return has_restorable_items()
        except Exception as e:
            logger.debug(f"_has_restorable_queue check failed: {e}")
            return False

    def _prompt_restore_queue(self) -> None:
        """启动时弹窗询问是否恢复上次的未完成队列。

        用户选"是"→ 把队列项灌回 export_panel 列表（带状态标记），
        等用户手动点"开始导出"续渲；选"否"→ 清空队列文件。
        """
        try:
            items, settings = load_batch_queue()
        except Exception as e:
            logger.error(f"load restorable queue failed: {e}")
            return

        restorable = [it for it in items if it.is_restorable and os.path.exists(it.file_path)]
        # 文件已不存在的项视为无法恢复
        gone = [it for it in items if it.is_restorable and not os.path.exists(it.file_path)]
        if gone:
            logger.info(f"Skipping {len(gone)} items whose source files no longer exist")

        if not restorable:
            # 没有可恢复项，清掉残留的队列文件
            clear_batch_queue()
            return

        count = len(restorable)
        title = I18N.tr("export.restore_queue_title", "恢复渲染队列")
        prompt = I18N.tr(
            "export.restore_queue_prompt",
            "发现 {count} 个未完成的导出任务，是否恢复队列？",
        ).replace("{count}", str(count))

        reply = QMessageBox.question(
            self, title, prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )

        if reply == QMessageBox.StandardButton.Yes:
            # 把可恢复项灌回列表（export_panel.restore_items 会清空重建并标记状态）
            self.export_panel.restore_items([it.to_dict() for it in restorable])
            # 缓存 settings 和队列项，供用户点"开始导出"时续渲
            self._restored_queue_items = restorable
            self._restored_queue_settings = settings
            self.status_bar.showMessage(
                I18N.tr("export.restore_loaded", "已恢复 {count} 个任务，点击开始导出以继续").replace("{count}", str(count)),
                5000,
            )
        else:
            clear_batch_queue()
            self._restored_queue_items = None
            self._restored_queue_settings = None

    def _persist_batch_queue_on_close(self) -> None:
        """closeEvent 兜底：确保活跃队列已落盘。

        渲染过程中每个文件处理完都会实时持久化，这里只做兜底，
        防止极端情况下（如 SIGKILL 前最后一刻）遗漏。
        """
        items = getattr(self, '_batch_items', None)
        settings = getattr(self, '_batch_settings', None)
        if items:
            try:
                save_batch_queue(items, settings)
            except Exception as e:
                logger.debug(f"persist batch queue on close failed: {e}")
