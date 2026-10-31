"""
ColorStackHydrator — bidirectional sync between ColorStackData and UI panels + engine.

When switching between clips the grading panel was the only UI restored.
Curves, HSL, LUT, soft clip, mixer parameters were written to the engine
layers but the corresponding UI panels were left out of sync.  This module
centralises all hydration logic in one place so that both the engine and
every panel are restored atomically.

Typical call-sites:
* ``_load_color_stack_to_grading_panel``  (page_controller)  — loading
* ``_on_grading_panel_changed`` / save helpers                        — collecting
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Iterator, Optional

import numpy as np

from src.shared.project.color_stack import ColorStackData

logger = logging.getLogger(__name__)

# AUD-24202: 会话内快照往返的 inline 二进制通道键。``Layer.to_dict(None)``
# （assets_dir=None 是全库内存快照约定，磁盘持久化路径才传 assets_dir）不
# 序列化 BITMAP 蒙版 bitmap_data 与 AI matte；不补通道时切 clip 往返会
# 静默丢 AI 蒙版。base64 PNG（uint8）与磁盘持久化格式同保真，且保持
# JSON 安全。键名以下划线开头，``Layer.from_dict`` 对未知键忽略。
_BITMAP_INLINE_KEY = "_inline_bitmap_png_b64"
_AI_MATTE_INLINE_KEY = "_inline_ai_matte_png_b64"

# MaskShape.BITMAP 的字面量（避免为常量整体导入 layer 模块）
_BITMAP_MASK_TYPE = "bitmap"


def _encode_bitmap_png(data: Any) -> Optional[str]:
    """Encode a float32 [0,1] 2-D mask as base64 PNG (None on failure)."""
    try:
        import cv2

        arr = np.asarray(data)
        if arr.ndim != 2:
            return None
        ok, encoded = cv2.imencode(
            ".png", (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8))
        if not ok:
            return None
        return base64.b64encode(encoded.tobytes()).decode("ascii")
    except Exception:
        logger.debug("inline bitmap encode failed", exc_info=True)
        return None


def _decode_bitmap_png(payload: str) -> Optional[np.ndarray]:
    """Decode a base64 PNG back to a float32 [0,1] 2-D mask (None on failure)."""
    try:
        import cv2

        encoded = np.frombuffer(base64.b64decode(payload), dtype=np.uint8)
        img = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return (img / 255.0).astype(np.float32)
    except Exception:
        logger.debug("inline bitmap decode failed", exc_info=True)
        return None


def _iter_mask_items(items: Any) -> Iterator[Any]:
    """Yield live mask shapes/groups recursively (guards non-sequence mocks)."""
    if not isinstance(items, (list, tuple)):
        return
    for item in items:
        yield item
        children = getattr(item, "children", None)
        if isinstance(children, (list, tuple)):
            yield from _iter_mask_items(children)


def _iter_mask_dicts(items: Any) -> Iterator[dict]:
    """Yield serialized mask dicts recursively (group children included)."""
    if not isinstance(items, (list, tuple)):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        yield item
        children = item.get("children")
        if isinstance(children, (list, tuple)):
            yield from _iter_mask_dicts(children)


# ================================================================
# AUD-24202: 会话内快照往返的 BITMAP/AI-matte inline 通道（模块级公开，
# 供 timeline clip 栈提交 / 调色剪贴板等生产提交路径复用）
# ================================================================


def attach_inline_bitmaps(layer: Any, snapshot: dict) -> None:
    """Attach inline base64-PNG payloads the snapshot itself cannot carry.

    ``Layer.to_dict(None)`` drops BITMAP ``bitmap_data`` and ``ai_matte``
    (repo-wide convention: only the disk path passes ``assets_dir``).
    Without this channel the in-session collect → apply roundtrip
    silently loses AI masks. Guarded with isinstance checks so test
    doubles (MagicMock) fall through without attribute errors.
    """
    bitmaps: dict[Any, str] = {}
    for item in _iter_mask_items(getattr(layer, "mask_shapes", None)):
        if getattr(item, "type", None) != _BITMAP_MASK_TYPE:
            continue
        data = getattr(item, "bitmap_data", None)
        if data is None:
            continue
        encoded = _encode_bitmap_png(data)
        if encoded is not None:
            bitmaps[getattr(item, "id", None)] = encoded
    if bitmaps:
        for shape_dict in _iter_mask_dicts(snapshot.get("mask_shapes")):
            if shape_dict.get("type") != _BITMAP_MASK_TYPE:
                continue
            if shape_dict.get("bitmap_file"):
                continue  # 磁盘路径已持久化，无需 inline
            payload = bitmaps.get(shape_dict.get("id"))
            if payload is not None:
                shape_dict[_BITMAP_INLINE_KEY] = payload
    ai_matte = getattr(layer, "ai_matte", None)
    if ai_matte is not None and not snapshot.get("ai_matte_file"):
        encoded = _encode_bitmap_png(ai_matte)
        if encoded is not None:
            snapshot[_AI_MATTE_INLINE_KEY] = encoded


def restore_inline_bitmaps(layer: Any, snapshot: dict) -> None:
    """Restore BITMAP/AI-matte payloads attached by ``attach_inline_bitmaps``.

    ``Layer.from_dict`` ignores the inline keys; this patches the decoded
    arrays onto the freshly restored live objects (no layer.py change).
    """
    inline: dict[Any, str] = {}
    for shape_dict in _iter_mask_dicts(snapshot.get("mask_shapes")):
        payload = shape_dict.get(_BITMAP_INLINE_KEY)
        if isinstance(payload, str):
            inline[shape_dict.get("id")] = payload
    if inline:
        for item in _iter_mask_items(getattr(layer, "mask_shapes", None)):
            if getattr(item, "type", None) != _BITMAP_MASK_TYPE:
                continue
            if getattr(item, "bitmap_data", None) is not None:
                continue
            payload = inline.get(getattr(item, "id", None))
            if payload is None:
                continue
            decoded = _decode_bitmap_png(payload)
            if decoded is not None:
                item.bitmap_data = decoded
    ai_payload = snapshot.get(_AI_MATTE_INLINE_KEY)
    if isinstance(ai_payload, str) and getattr(layer, "ai_matte", None) is None:
        decoded = _decode_bitmap_png(ai_payload)
        if decoded is not None:
            layer.ai_matte = decoded


# ================================================================
# Hydrator
# ================================================================

class ColorStackHydrator:
    """Unified manager for syncing color-stack data with UI panels and engine."""

    # ------------------------------------------------------------------
    # Apply stack → engine layers
    # ------------------------------------------------------------------

    def apply_stack_to_engine(
        self,
        stack: ColorStackData,
        engine: Any,
    ) -> None:
        """Write the stack state into the engine.

        schema v2（P0-COLOR-02 统一契约）：
        - 栈携带完整 Layer 快照（``stack.layers`` 非空）时按**完整恢复**：
          直接重建 engine.layers（含蒙版/限定器/变形器/效果/插件/混合/
          烘焙/图层身份），并对齐 active_layer_index。
        - 旧栈（v1，无快照）回退为把平铺精简键广播到所有层。

        This mirrors the logic that previously lived in
        ``_load_color_stack_to_grading_panel``.
        """
        if not engine:
            logger.debug("apply_stack_to_engine: no engine, skipping")
            return

        # P1-COLOR-13: per-clip 视频降噪。video_denoise_params 是引擎级
        # 单值——切 clip 时若不先重置，上一个 clip 开着的降噪会串扰本
        # clip；栈携带参数时按快照恢复并推送到渲染器。
        try:
            engine.video_denoise_params = None
            vd_state = getattr(stack, "video_denoise", None)
            if vd_state and hasattr(engine, "set_video_denoise_state"):
                engine.set_video_denoise_state(vd_state)
            elif vd_state:
                from src.shared.video.denoise.params import (
                    VideoDenoiseParams,
                )
                engine.video_denoise_params = VideoDenoiseParams.from_dict(
                    vd_state)
        except Exception:
            logger.debug("apply_stack_to_engine: video_denoise restore "
                         "failed", exc_info=True)

        layers_snapshot = getattr(stack, "layers", None) or ()
        if layers_snapshot:
            from src.features.color.core.layer import Layer

            # AUD-24200: stack.layers 源自工程文件（外部数据），逐条容错——
            # 单条损坏只跳过该层并 warning，不中断整个水合留下半应用状态；
            # engine.layers 只在有效层集合构建完成后一次性替换（原子）。
            restored: list = []
            dropped = 0
            for index, entry in enumerate(layers_snapshot):
                try:
                    layer = Layer.from_dict(dict(entry))
                except Exception:
                    dropped += 1
                    logger.warning(
                        "apply_stack_to_engine: corrupt layer #%d in stack "
                        "%s skipped",
                        index, stack.stack_id[:8], exc_info=True,
                    )
                    continue
                self._restore_inline_bitmaps(layer, entry)
                restored.append(layer)
            if dropped:
                logger.warning(
                    "apply_stack_to_engine: %d/%d layers dropped from stack "
                    "%s (corrupt entries)",
                    dropped, len(layers_snapshot), stack.stack_id[:8],
                )
            if restored:
                engine.layers = restored
                idx = int(getattr(stack, "active_layer_index", 0) or 0)
                engine.active_layer_index = (
                    idx if 0 <= idx < len(restored) else 0
                )
                logger.debug(
                    "apply_stack_to_engine: full restore %d layers from stack %s",
                    len(restored), stack.stack_id[:8],
                )
            else:
                # 全部层损坏：整体回退，保留当前 engine 状态（不做半应用）。
                logger.warning(
                    "apply_stack_to_engine: all %d layers of stack %s are "
                    "corrupt; keeping current engine layers",
                    len(layers_snapshot), stack.stack_id[:8],
                )
        elif getattr(engine, "layers", None):
            params = stack.grading_params
            for layer in engine.layers:
                # v1 栈不携带 Layer 快照：先整体重置再写平铺键。两个
                # clip 引用同一源素材时 page_controller 不重新 load、
                # engine.layers 不重建——若只覆写平铺键，上一剪辑的蒙版/
                # 限定器/效果/插件/变形/关键帧会全部泄漏到本剪辑，并在
                # 下一次调色提交时被当作权威快照永久化。
                if hasattr(layer, "reset_params"):
                    layer.reset_params()
                layer.grading_params = dict(params)
                if stack.curves_data:
                    layer.curves_data = dict(stack.curves_data)
                else:
                    layer.curves_data = {}
                if stack.hsl_curve_data:
                    layer.hsl_curve_data = dict(stack.hsl_curve_data)
                else:
                    layer.hsl_curve_data = {}
                if stack.mixer_params:
                    layer.mixer_params = dict(stack.mixer_params)
                else:
                    layer.mixer_params = {}
                if stack.soft_clip_params:
                    layer.soft_clip_params = dict(stack.soft_clip_params)
                else:
                    layer.soft_clip_params = {}
                # v1 栈不携带曲线侧栏状态：重置为恒等默认，防止上一剪辑
                # 的侧栏值（四参数柔化裁切/强度）泄漏到本剪辑
                from src.features.color.core.curves_soft_clip import (
                    default_curves_edit,
                    default_curves_soft_clip,
                )
                layer.curves_soft_clip = default_curves_soft_clip()
                layer.curves_edit = default_curves_edit()
                # LUT
                layer.active_lut_path = stack.lut_path
                layer.lut_intensity = stack.lut_intensity
        else:
            logger.debug("apply_stack_to_engine: no engine layers, skipping")
            return

        # 恢复跟踪绑定（阶段 1）：旧工程无 trackings 键时得到空存储，不影响其他字段。
        # 切换片段时用目标栈整体替换，避免上一个片段的绑定泄漏到当前片段。
        tracking_store = getattr(engine, "tracking_store", None)
        if tracking_store is not None:
            from src.features.color.tracking.tracking_store import TrackingStore

            # ``ColorStackData`` 曾在 edit core 与 shared project 各有一份
            # 实现；旧工程或旧运行时对象可能还没有 trackings 属性。缺失
            # 时按空存储恢复，不能在进入 Color 页面时阻断整个应用。
            restored = TrackingStore.from_dict(
                getattr(stack, "trackings", {}) or {}
            )
            tracking_store.clear()
            for binding in restored.all_bindings():
                tracking_store.add_binding(binding)

        logger.debug("apply_stack_to_engine: applied stack %s", stack.stack_id[:8])

    # ------------------------------------------------------------------
    # Apply stack → UI panels
    # ------------------------------------------------------------------

    def apply_stack_to_panels(
        self,
        stack: ColorStackData,
        panels: dict[str, Any],
    ) -> None:
        """Push all parameters from *stack* into the corresponding UI panels.

        *panels* is a plain dict whose values are the panel widget instances.
        Missing or incompatible panels are skipped with a debug log.
        """
        self._apply_grading_to_panel(stack, panels.get("grading_panel"))
        self._apply_curves_to_panel(stack, panels.get("curves_panel"))
        self._apply_curves_palette_to_panel(stack, panels.get("curves_palette"))
        self._apply_hsl_to_panel(stack, panels.get("hsl_panel"))
        self._apply_mixer_to_panel(stack, panels.get("mixer_panel"))
        self._apply_lut_to_panel(stack, panels.get("lut_panel"))
        self._apply_video_denoise_to_panel(
            stack, panels.get("denoise_panel"))

    def _apply_video_denoise_to_panel(self, stack, panel) -> None:
        """P1-COLOR-13: 栈的视频降噪参数回显到降噪面板（视频模式页）。"""
        if panel is None:
            return
        vd_state = getattr(stack, "video_denoise", None)
        setter = getattr(panel, "set_video_params_dict", None)
        if vd_state and callable(setter):
            try:
                setter(vd_state)
            except Exception:
                logger.debug("video denoise panel sync failed", exc_info=True)

    # ------------------------------------------------------------------
    # AUD-24202: 会话内快照往返的 BITMAP/AI-matte inline 通道
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_inline_bitmaps(layer: Any, snapshot: dict) -> None:
        attach_inline_bitmaps(layer, snapshot)

    @staticmethod
    def _restore_inline_bitmaps(layer: Any, snapshot: dict) -> None:
        restore_inline_bitmaps(layer, snapshot)

    # ------------------------------------------------------------------
    # Collect current state → ColorStackData
    # ------------------------------------------------------------------

    def collect_stack_from_engine_and_panels(
        self,
        stack_id: str,
        engine: Any,
        panels: dict[str, Any],
    ) -> ColorStackData:
        """Read current parameters from panels/engine and build a ColorStackData.

        Used when saving — captures everything visible, not just grading_params.

        schema v2：engine 有图层时同时捕获完整 Layer 快照，并**从快照派生**
        平铺精简键（grading/curves/hsl/mixer/soft_clip/lut），保证两套视图
        一致；面板仅在没有可用图层时作为数据源。
        """
        grading_params: dict[str, Any] = {}
        curves_data: dict[str, Any] = {}
        hsl_curve_data: dict[str, Any] = {}
        mixer_params: dict[str, Any] = {}
        soft_clip_params: dict[str, Any] = {}
        curves_soft_clip: dict[str, Any] = {}
        curves_edit: dict[str, Any] = {}
        lut_path: str | None = None
        lut_intensity: float = 1.0

        # 完整 Layer 快照 + RAW（engine 级）
        layers_snapshot: tuple[dict[str, Any], ...] = ()
        active_layer_index = 0
        raw_params: dict[str, Any] = {}
        if engine and getattr(engine, "layers", None):
            # 测试替身/旧引擎对象可能没有 to_dict —— 快照不可用（抛错或
            # 返回非 dict，如 MagicMock）时退回面板收集（旧行为）。
            try:
                pairs: list[tuple[Any, dict]] = []
                for layer in engine.layers:
                    snapshot = layer.to_dict(None)
                    if isinstance(snapshot, dict):
                        pairs.append((layer, snapshot))
            except AttributeError:
                pairs = []
            idx = getattr(engine, "active_layer_index", 0)
            idx = idx if isinstance(idx, int) and idx >= 0 else 0
            if pairs:
                # AUD-24202: 为 to_dict(None) 无法携带的 BITMAP/AI-matte
                # 数据补 inline 通道（会话内往返不再丢 AI 蒙版）。
                for layer, snapshot in pairs:
                    self._attach_inline_bitmaps(layer, snapshot)
                snapshots = [snapshot for _, snapshot in pairs]
                layers_snapshot = tuple(snapshots)
                active_layer_index = (
                    idx if idx < len(snapshots) else 0
                )
                raw_params = dict(
                    getattr(engine, "current_raw_params", None) or {}
                )

                # 从活动图层派生平铺键（两套视图保持一致）
                src = engine.layers[active_layer_index]
                grading_params = dict(getattr(src, "grading_params", {}) or {})
                curves_data = dict(getattr(src, "curves_data", {}) or {})
                hsl_curve_data = dict(getattr(src, "hsl_curve_data", {}) or {})
                mixer_params = dict(getattr(src, "mixer_params", {}) or {})
                soft_clip_params = dict(
                    getattr(src, "soft_clip_params", {}) or {}
                )
                # schema v3 平铺键：从活动图层派生侧栏状态（两套视图一致）
                curves_soft_clip = dict(
                    getattr(src, "curves_soft_clip", {}) or {}
                )
                curves_edit = dict(getattr(src, "curves_edit", {}) or {})
                lut_path = getattr(src, "active_lut_path", None)
                lut_intensity = getattr(src, "lut_intensity", 1.0)

        if not layers_snapshot:
            # Fallback: panels（旧行为）
            gp = panels.get("grading_panel")
            if gp is not None and hasattr(gp, "get_current_params"):
                grading_params = gp.get_current_params()

            cp = panels.get("curves_panel")
            if cp is not None and hasattr(cp, "get_curves_data"):
                curves_data = cp.get_curves_data()

            # 曲线面板侧栏（无引擎图层时的兜底收集；模型形状态直读）
            palette = panels.get("curves_palette")
            if palette is not None:
                getter = getattr(palette, "get_layer_state", None)
                if callable(getter):
                    sidebar_state = getter() or {}
                    curves_soft_clip = sidebar_state.get("curves_soft_clip") or {}
                    curves_edit = sidebar_state.get("curves_edit") or {}

            hp = panels.get("hsl_panel")
            if hp is not None and hasattr(hp, "get_curves_data"):
                hsl_curve_data = hp.get_curves_data()

            mp = panels.get("mixer_panel")
            if mp is not None and hasattr(mp, "params"):
                mixer_params = {k: list(v) for k, v in mp.params.items()}

            # 旧 SoftClipPanel 已删（任务 9）：无引擎图层时 legacy 8 参数
            # 无面板可读，保持空 dict（图层路径始终权威）

            if engine and getattr(engine, "layers", None):
                layer = engine.layers[0]
                lut_path = getattr(layer, "active_lut_path", None)
                lut_intensity = getattr(layer, "lut_intensity", 1.0)

        # 蒙版跟踪数据（阶段 1）：从 engine 的跟踪存储读取序列化结果。
        trackings_data: dict[str, Any] = {}
        tracking_store = getattr(engine, "tracking_store", None)
        if tracking_store is not None:
            trackings_data = tracking_store.to_dict()

        # AUD-24201: 采集 per-clip 视频降噪状态（P1-COLOR-13），与 apply
        # 路径（59-72 行恢复该键）对称——否则按本方法文档用途（"Used when
        # saving"）作保存边界时会把降噪状态静默清空。engine 级单值，
        # get_video_denoise_state() 返回序列化 dict（未启用为 None）；
        # 测试替身/异常时安全降级为不携带。
        video_denoise: dict[str, Any] | None = None
        if engine:
            getter = getattr(engine, "get_video_denoise_state", None)
            if callable(getter):
                try:
                    vd_state = getter()
                except Exception:
                    logger.debug(
                        "collect video denoise state failed", exc_info=True)
                    vd_state = None
                if isinstance(vd_state, dict):
                    video_denoise = vd_state

        return ColorStackData(
            stack_id=stack_id,
            asset_id=None,
            name="Default",
            grading_params=grading_params,
            curves_data=curves_data,
            hsl_curve_data=hsl_curve_data,
            mixer_params=mixer_params,
            soft_clip_params=soft_clip_params,
            curves_soft_clip=curves_soft_clip,
            curves_edit=curves_edit,
            lut_path=lut_path,
            lut_intensity=lut_intensity,
            trackings=trackings_data,
            layers=layers_snapshot,
            active_layer_index=active_layer_index,
            raw_params=raw_params,
            video_denoise=video_denoise,
        )

    # ------------------------------------------------------------------
    # Convenience: apply engine + panels in one shot
    # ------------------------------------------------------------------

    def hydrate(
        self,
        stack: ColorStackData,
        engine: Any,
        panels: dict[str, Any],
    ) -> None:
        """Apply *stack* to both engine and panels, then trigger render."""
        self.apply_stack_to_engine(stack, engine)
        self.apply_stack_to_panels(stack, panels)
        if engine and hasattr(engine, "render_pipeline"):
            engine.render_pipeline()
        logger.debug("hydrate: stack %s fully applied", stack.stack_id[:8])

    # ------------------------------------------------------------------
    # Private helpers — per-panel apply
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_grading_to_panel(stack: ColorStackData, panel: Any) -> None:
        if panel is None:
            return
        if not hasattr(panel, "set_params"):
            logger.debug("grading_panel missing or incompatible API, skipping")
            return

        params = stack.grading_params
        lift = params.get("lift", (0, 0, 0))
        gamma = params.get("gamma", (0, 0, 0))
        gain = params.get("gain", (0, 0, 0))
        exposure = params.get("exposure", 0)
        temp = params.get("temp", 0)
        tint = params.get("tint", 0)
        contrast = params.get("contrast", 0)
        shadow = params.get("shadow", 0)
        saturation = params.get("saturation", 0)
        vibrance = params.get("vibrance", 0)
        mid_detail = params.get("mid_detail", 0)
        hue_shift = params.get("hue_shift", 0)

        panel.set_params(
            lift, gamma, gain,
            exposure, temp, tint,
            contrast, shadow, saturation,
            vibrance, mid_detail, hue_shift,
            highlights=params.get("highlights", 0),
        )
        panel.current_stack_id = stack.stack_id

    @staticmethod
    def _apply_curves_to_panel(stack: ColorStackData, panel: Any) -> None:
        if panel is None:
            return
        if not hasattr(panel, "set_curves_data"):
            logger.debug("curves_panel missing or incompatible API, skipping")
            return
        if stack.curves_data:
            panel.set_curves_data(stack.curves_data, emit_changed=False)
        else:
            panel.reset(emit_changed=False)

    @staticmethod
    def _apply_curves_palette_to_panel(stack: ColorStackData, panel: Any) -> None:
        """曲线面板侧栏回填（任务 7）：活动层快照优先，平铺键兜底。

        v2+ 工程的权威状态在 Layer 快照内（``curves_soft_clip`` /
        ``curves_edit``）；平铺键只服务无快照的旧格式。``set_layer_state``
        屏蔽一切回写，不会触发渲染或撤销快照。
        """
        if panel is None:
            return
        setter = getattr(panel, "set_layer_state", None)
        if not callable(setter):
            logger.debug("curves_palette missing set_layer_state, skipping")
            return
        layers = getattr(stack, "layers", None) or ()
        if layers:
            idx = getattr(stack, "active_layer_index", 0) or 0
            if not isinstance(idx, int) or not (0 <= idx < len(layers)):
                idx = 0
            entry = layers[idx]
            if isinstance(entry, dict):
                setter({
                    "curves_soft_clip": entry.get("curves_soft_clip") or {},
                    "curves_edit": entry.get("curves_edit") or {},
                })
                return
        setter({
            "curves_soft_clip": getattr(stack, "curves_soft_clip", None) or {},
            "curves_edit": getattr(stack, "curves_edit", None) or {},
        })

    @staticmethod
    def _apply_hsl_to_panel(stack: ColorStackData, panel: Any) -> None:
        if panel is None:
            return
        if not hasattr(panel, "set_curves_data"):
            logger.debug("hsl_panel missing or incompatible API, skipping")
            return
        if stack.hsl_curve_data:
            panel.set_curves_data(stack.hsl_curve_data)
        else:
            panel.reset(emit_changed=False)

    @staticmethod
    def _apply_mixer_to_panel(stack: ColorStackData, panel: Any) -> None:
        if panel is None:
            return
        if not hasattr(panel, "set_params"):
            logger.debug("mixer_panel missing or incompatible API, skipping")
            return
        if stack.mixer_params:
            panel.set_params(stack.mixer_params)

    @staticmethod
    def _apply_lut_to_panel(stack: ColorStackData, panel: Any) -> None:
        if panel is None:
            return
        if not hasattr(panel, "set_selected_lut"):
            logger.debug("lut_panel missing or incompatible API, skipping")
            return
        panel.set_selected_lut(stack.lut_path)
        panel.set_intensity(stack.lut_intensity)

    # ------------------------------------------------------------------
    # Private helpers — per-panel collect
    # ------------------------------------------------------------------
    # 旧 SoftClipPanel 的 _apply_soft_clip_to_panel / _read_soft_clip_params
    # 已随面板一并删除（任务 9）：legacy 8 参数 soft_clip_params 只保留
    # 渲染与工程数据兼容路径，无 UI 读写。
