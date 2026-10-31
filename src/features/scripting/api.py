"""
Topos Color Scripting API.

Provides a clean, intuitive Python interface for automated image processing
and color grading.  Users interact with a ``topos`` object that wraps
ImageEngine operations.

Usage in scripts::

    import topos
    topos.open("/path/to/image.jpg")
    topos.grade(exposure=0.5, contrast=15)
    topos.save("/output/graded.jpg")

API versioning (P1-COLOR-24):

- ``API_VERSION`` 常量声明当前 API 版本（``"1.0"``）。
- 脚本可在头部声明 ``# topos-api: 1.0``；主版本不匹配时拒绝执行并
  给出迁移提示，次版本高于当前时输出警告但继续。
- 查询：``topos.api_version()``。
"""

import os
import re
import sys
import numpy as np
import logging

logger = logging.getLogger(__name__)

from PyQt6.QtWidgets import QMainWindow

from src.features.scripting.engine import ScriptAbortError

#: 当前脚本 API 版本。主版本变化 = 破坏性变更；次版本 = 向后兼容新增。
API_VERSION = "1.0"
API_VERSION_TUPLE = (1, 0)

_API_DECL_RE = re.compile(r"^#\s*topos-api:\s*(\d+)\.(\d+)\s*$", re.MULTILINE)


def _project_luts_root() -> str:
    """项目根 ``luts/`` 目录（随附 .cube 所在地，AUD-25902）。

    api.py 位于 ``<root>/src/features/scripting/``，回溯 4 层才是项目根
    （旧实现回溯 3 层得到 ``<root>/src``，把相对 LUT 名全部送往空的
    ``src/luts/``）。不复用 ``paths.get_lut_dir()``：它经
    ``get_project_root()`` 的 3 层回溯解析到 ``<root>/src``（该历史
    问题在其自身 docstring 中有记载），会重演同一缺陷。冻结
    （PyInstaller）形态下随可执行文件解析。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(base, "luts")


def parse_api_requirement(code: str):
    """解析脚本头部的 ``# topos-api: X.Y`` 声明。

    返回 (major, minor) 元组；未声明返回 None。只扫描前 40 行，
    避免在大脚本上全文正则。
    """
    head = "\n".join(code.splitlines()[:40])
    match = _API_DECL_RE.search(head)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    return None


def check_api_compatibility(required, current=API_VERSION_TUPLE):
    """校验脚本声明的 API 版本与当前版本的兼容性。

    返回 (ok, message)：
    - ok=False：主版本不匹配，message 含迁移提示；
    - ok=True 且 message 非空：次版本高于当前（向后兼容警告）；
    - ok=True 且 message 为空：完全兼容/未声明。
    """
    if required is None:
        return True, ""
    if required[0] != current[0]:
        return False, (
            f"脚本声明 API {required[0]}.{required[1]}，当前为 {current[0]}.{current[1]}："
            f"主版本不兼容。请按 API {current[0]}.x 迁移脚本，"
            f"或升级 Topos Color 后重试。"
        )
    if required[1] > current[1]:
        return True, (
            f"脚本声明 API {required[0]}.{required[1]}，当前为 {current[0]}.{current[1]}："
            f"脚本可能使用了本版本尚不支持的新特性。"
        )
    return True, ""


def diff_snapshots(before: dict, after: dict):
    """对比两个 grade 快照，返回可读的变更清单（dry-run diff 基础件）。

    每个差异一条 ``"参数名: 旧值 -> 新值"``；仅存在于单侧的键标注
    ``added``/``removed``。嵌套 tuple/list 逐分量对比。
    """
    changes = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key, None)
        new = after.get(key, None)
        if key not in before:
            changes.append(f"{key}: added {new!r}")
        elif key not in after:
            changes.append(f"{key}: removed {old!r}")
        elif repr(old) != repr(new):
            changes.append(f"{key}: {old!r} -> {new!r}")
    return changes



def _interpolate_curve_255(points: list) -> np.ndarray:
    """Convert control points [(input_255, output_ui_255), ...] to a 256-entry LUT.

    Input and output coordinates are in 0-255 range and follow the
    CurvesWidget convention **including its inverted Y axis** (Qt screen
    coordinates): y=0 is the top of the widget (maximum output, white)
    and y=255 is the bottom (minimum output, black).  The identity curve
    is ``[(0, 255), (255, 0)]``.

    AUD-25900: the LUT is derived as ``(255.0 - y) / 255.0``, exactly like
    ``CurvesWidget._compute_lut`` (src/shared/ui/widgets/curves_widget.py),
    so a curve applied from a script renders identically after the UI
    reloads the same points from ``layer.curves_data``.

    Output LUT values are normalized to 0.0-1.0 range.

    Uses linear interpolation between control points with extrapolation
    at boundaries (matches CurvesWidget.calculate_curve behavior).
    """
    if not points:
        return np.linspace(0.0, 1.0, 256, dtype=np.float32)

    pts = sorted(points, key=lambda p: p[0])
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    ys = np.array([p[1] for p in pts], dtype=np.float32)

    if len(xs) < 2:
        # y is in UI (inverted) coordinates: convert with (255 - y) / 255.
        default = (255.0 - ys[0]) / 255.0 if len(ys) > 0 else 0.5
        return np.full(256, default, dtype=np.float32)

    lut = np.zeros(256, dtype=np.float32)

    if len(xs) == 2:
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0
        lut = (255.0 - (ys[0] + slope * (np.arange(256, dtype=np.float32) - xs[0]))) / 255.0
        # AUD-25903: 两点路径不再 clip(0,1)——多点 PCHIP 路径与
        # CurvesWidget._compute_lut 对控制点之外均做线性外推且不裁切
        # （curves LUT 属调色语义数据，外推段是设计行为；HDR 高光/阴影
        # 信息不得被无理由裁切）。两点与多点的同一调色意图现在得到同
        # 一 LUT。
        return lut

    xi = np.arange(256, dtype=np.float32)

    try:
        from scipy.interpolate import PchipInterpolator
    except ModuleNotFoundError:
        return (255.0 - np.interp(xi, xs, ys).astype(np.float32)) / 255.0

    pchip = PchipInterpolator(xs, ys)
    
    mask_mid = (xi >= xs[0]) & (xi <= xs[-1])
    res = np.zeros(256, dtype=np.float32)
    res[mask_mid] = pchip(xi[mask_mid])
    
    mask_left = xi < xs[0]
    if np.any(mask_left):
        dx = max(0.1, (xs[1] - xs[0]) * 0.1)
        y_start = float(pchip(xs[0]))
        y_next = float(pchip(xs[0] + dx))
        slope = (y_next - y_start) / dx
        res[mask_left] = ys[0] + slope * (xi[mask_left] - xs[0])
    
    mask_right = xi > xs[-1]
    if np.any(mask_right):
        dx = max(0.1, (xs[-1] - xs[-2]) * 0.1)
        y_end = float(pchip(xs[-1]))
        y_prev = float(pchip(xs[-1] - dx))
        slope = (y_end - y_prev) / dx
        res[mask_right] = ys[-1] + slope * (xi[mask_right] - xs[-1])

    # res holds y in UI (inverted) coordinates — flip like CurvesWidget does.
    return (255.0 - res) / 255.0


def _build_default_hsl_lut():
    """Build default HSL curve LUT with identity values.
    
    Returns (6, 256) float32 array with:
    - LumSat[0]: 1.0 (multiplier)
    - HueHue[1]: 0.0 (shift)
    - HueSat[2]: 1.0 (multiplier)
    - HueLum[3]: 1.0 (multiplier)
    - SatSat[4]: 1.0 (multiplier)
    - SatLum[5]: 1.0 (multiplier)
    """
    lut = np.zeros((6, 256), dtype=np.float32)
    lut[0] = 1.0
    lut[2] = 1.0
    lut[3] = 1.0
    lut[4] = 1.0
    lut[5] = 1.0
    return lut


def _interpolate_hsl_curve_255(points, curve_type: str):
    """Convert HSL curve control points to LUT values.
    
    Args:
        points: List of (x_255, y_255) tuples in UI coordinate range
        curve_type: 'LumSat', 'HueHue', 'HueSat', 'HueLum', 'SatSat', 'SatLum'
    
    Y coordinate conventions (matching HSLCurvesWidget):
    - Multiplier types (sat/lum): Y=127.5 is 1.0, range 0.0-2.0
      Formula: value = (255.0 - y) / 127.5
    - Shift type (HueHue): Y=127.5 is 0.0, range -0.5 to +0.5
      Formula: shift = (127.5 - y) / 255.0
    
    Returns: 256-entry float array with correct output range
    """
    if not points:
        if curve_type == 'HueHue':
            return np.zeros(256, dtype=np.float32)
        else:
            return np.ones(256, dtype=np.float32)
    
    pts = sorted(points, key=lambda p: p[0])
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    ys_ui = np.array([p[1] for p in pts], dtype=np.float32)
    
    is_shift = curve_type == 'HueHue'
    
    if is_shift:
        ys_internal = (127.5 - ys_ui) / 255.0
    else:
        ys_internal = (255.0 - ys_ui) / 127.5
    
    if len(xs) < 2:
        default_val = ys_internal[0] if len(ys_internal) > 0 else (0.0 if is_shift else 1.0)
        return np.full(256, default_val, dtype=np.float32)
    
    lut = np.zeros(256, dtype=np.float32)
    
    if len(xs) == 2:
        slope = (ys_internal[1] - ys_internal[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0
        lut = ys_internal[0] + slope * (np.arange(256, dtype=np.float32) - xs[0])
        if is_shift:
            return np.clip(lut, -0.5, 0.5)
        else:
            return np.clip(lut, 0.0, 2.0)
    
    xi = np.arange(256, dtype=np.float32)
    
    try:
        from scipy.interpolate import PchipInterpolator
    except ModuleNotFoundError:
        res = np.interp(xi, xs, ys_internal).astype(np.float32)
        if is_shift:
            return np.clip(res, -0.5, 0.5)
        return np.clip(res, 0.0, 2.0)

    pchip = PchipInterpolator(xs, ys_internal)
    
    mask_mid = (xi >= xs[0]) & (xi <= xs[-1])
    res = np.zeros(256, dtype=np.float32)
    res[mask_mid] = pchip(xi[mask_mid])
    
    mask_left = xi < xs[0]
    if np.any(mask_left):
        dx = max(0.1, (xs[1] - xs[0]) * 0.1)
        y_start = float(pchip(xs[0]))
        y_next = float(pchip(xs[0] + dx))
        slope = (y_next - y_start) / dx
        res[mask_left] = ys_internal[0] + slope * (xi[mask_left] - xs[0])
    
    mask_right = xi > xs[-1]
    if np.any(mask_right):
        dx = max(0.1, (xs[-1] - xs[-2]) * 0.1)
        y_end = float(pchip(xs[-1]))
        y_prev = float(pchip(xs[-1] - dx))
        slope = (y_end - y_prev) / dx
        res[mask_right] = ys_internal[-1] + slope * (xi[mask_right] - xs[-1])
    
    if is_shift:
        return np.clip(res, -0.5, 0.5)
    else:
        return np.clip(res, 0.0, 2.0)


class ToposAPI:
    """User-facing scripting API.

    All methods that touch ImageEngine go through the BlockingBridge so they
    execute safely on the main thread.
    """

    def __init__(self, engine, bridge):
        self._engine = engine
        self._bridge = bridge
        self._undo_started = False
        self._main_window = None
        self._composite_api = None
        # P1-COLOR-24: 宿主注入的只读时间线上下文 provider
        #（main 线程函数：list_clips() / clip_grade(clip_id)）
        self._timeline_provider = None

    # ==================================================================
    # API versioning (P1-COLOR-24)
    # ==================================================================

    def api_version(self) -> str:
        """返回当前脚本 API 版本（如 "1.0"）。"""
        return API_VERSION

    # ==================================================================
    # Timeline traversal / dry-run diff (P1-COLOR-24)
    # ==================================================================

    def list_clips(self):
        """返回当前时间线的 clip 只读清单（按时间线起点排序）。

        每个 clip 一个 dict：clip_id / asset_id / track_id / timeline_start /
        timeline_duration / source_start / source_duration / color_stack_id /
        has_grade（调色栈是否有实际调色）。无时间线时返回 []。
        """
        provider = self._timeline_provider
        if provider is None:
            return []
        try:
            return self._bridge.call_sync(lambda: provider("list_clips")) or []
        except Exception as exc:
            logger.warning("script list_clips failed: %s", exc)
            return []

    def clip_grade(self, clip_id: str):
        """返回某个 clip 的调色快照（grading_params + lut_path）。

        与 ``grade_snapshot`` 的 diff 组合可做批量应用前的变更预览。
        clip 不存在或无调色栈时返回 None。
        """
        provider = self._timeline_provider
        if provider is None:
            return None
        try:
            return self._bridge.call_sync(
                lambda: provider("clip_grade", clip_id))
        except Exception as exc:
            logger.warning("script clip_grade(%s) failed: %s", clip_id, exc)
            return None

    def grade_snapshot(self):
        """返回当前活动图层的调色快照（grading_params 深拷贝 + LUT）。

        典型用法（dry-run diff）::

            before = topos.grade_snapshot()
            topos.grade(exposure=0.4)
            for line in topos.diff_grade(before, topos.grade_snapshot()):
                topos.print(line)
        """
        def _do():
            layer = self._engine.get_active_layer()
            if layer is None:
                return None
            return {
                "grading_params": dict(layer.grading_params),
                "lut_path": getattr(layer, "active_lut_path", None),
                "lut_intensity": getattr(layer, "lut_intensity", 1.0),
                "qualifier_enabled": bool(
                    (getattr(layer, "qualifier_params", None) or {}).get(
                        "enabled", False)),
            }
        try:
            return self._bridge.call_sync(_do)
        except Exception as exc:
            logger.warning("script grade_snapshot failed: %s", exc)
            return None

    @staticmethod
    def diff_grade(before: dict, after: dict):
        """对比两个快照（grade_snapshot/clip_grade 输出），返回变更清单。"""
        if not before or not after:
            return []
        return diff_snapshots(before, after)


    @property
    def composite(self):
        """统一 Composite API（与 AI/MCP 使用相同的 JSON 操作协议）。

        示例::

            topos.composite.add_keyframe(
                node_id, "scale", 0, 1.0,
            )
            topos.composite.set_interpolation(
                node_id, "scale", 0, "bezier",
            )
        """
        if self._composite_api is None:
            from src.features.scripting.composite_api import CompositeScriptAPI
            self._composite_api = CompositeScriptAPI(
                self._bridge,
                lambda: getattr(self._find_main_window(), "composite_page", None),
            )
        return self._composite_api

    # ==================================================================
    # Document
    # ==================================================================

    def open(self, path: str) -> None:
        """Load an image file into the engine.

        Supports RAW, EXR, HEIC, TIFF, PSD and common raster formats.
        """
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            raise FileNotFoundError(f"Image not found: {path}")

        def _do_open():
            from src.shared.io.file_io import FileIO
            full, high, low = FileIO.load_image(path)
            if full is None:
                raise RuntimeError(f"Failed to load image: {path}")
            self._engine.set_image(full, high, low, is_new_file=True, path=path)

            # Sync canvas & UI state (aligned with main_window.load_image)
            mw = self._find_main_window()
            if mw is not None:
                mw.update_canvas(is_new_source=True)
                mw.canvas.fit_to_window()
                self._engine.sync_all_resources()
                mw.update_from_layer(self._engine.get_active_layer())
                mw.layer_panel.update_layers(
                    self._engine.layers,
                    self._engine.active_layer_index
                )

        self._bridge.call_sync(_do_open)

    def save(self, path: str, format: str = None, quality: int = 95) -> str:
        """Export the current graded result to *path*.

        Returns the output file path.
        """
        path = os.path.abspath(os.path.expanduser(path))
        out_dir = os.path.dirname(path)
        os.makedirs(out_dir, exist_ok=True)

        # Determine extension from format hint or path
        if format:
            fmt_ext = {
                "JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr",
                "TIFF": ".tif", "WebP": ".webp",
            }
            ext = fmt_ext.get(format, os.path.splitext(path)[1].lower())
            if not path.lower().endswith(ext):
                path = os.path.splitext(path)[0] + ext

        def _do_save():
            img = self._engine.get_current_image(force_full_res=True)
            if img is None:
                raise RuntimeError("No image is currently loaded")
            from src.shared.io.file_io import FileIO
            options = {"quality": quality}
            success = FileIO.save_image(path, img, options=options)
            if not success:
                raise RuntimeError(f"Failed to save image: {path}")
            return path

        return self._bridge.call_sync(_do_save)

    def save_project(self) -> None:
        """Save the .topos project package for the current image."""
        def _do_save():
            mw = self._find_main_window()
            if mw and hasattr(mw, 'save_router'):
                mw.save_router.save()
        self._bridge.call_sync(_do_save)

    # ==================================================================
    # Layer Management
    # ==================================================================

    def add_layer(self, name: str = None) -> int:
        """Add a new layer. Returns its index."""
        def _do():
            layer = self._engine.add_layer()
            if name:
                layer.name = name
            return self._engine.active_layer_index
        return self._bridge.call_sync(_do)

    def remove_layer(self, index: int) -> None:
        """Remove a layer by index."""
        self._bridge.call_sync(lambda: self._engine.remove_layer(index))

    def select_layer(self, index: int) -> None:
        """Set the active layer."""
        self._bridge.call_sync(lambda: self._engine.set_active_layer(index))

    def set_layer_visible(self, index: int, visible: bool) -> None:
        """Toggle layer visibility."""
        def _do():
            if 0 <= index < len(self._engine.layers):
                self._engine.layers[index].visible = visible
                self._commit_render()
        self._bridge.call_sync(_do)

    def set_layer_opacity(self, index: int, opacity: float) -> None:
        """Set layer opacity (0.0-1.0)."""
        def _do():
            if 0 <= index < len(self._engine.layers):
                self._engine.layers[index].opacity = opacity
                self._commit_render()
        self._bridge.call_sync(_do)

    def set_layer_blend_mode(self, index: int, mode: str) -> None:
        """Set blend mode (Normal, Multiply, Screen, Overlay, ...)."""
        def _do():
            if 0 <= index < len(self._engine.layers):
                self._engine.layers[index].blend_mode = mode
                self._commit_render()
        self._bridge.call_sync(_do)

    def duplicate_layer(self, index: int) -> int:
        """Duplicate a layer. Returns new layer index."""
        def _do():
            self._engine.duplicate_layer(index)
            return self._engine.active_layer_index
        return self._bridge.call_sync(_do)

    def reset_layer(self, index: int = None) -> None:
        """Reset all parameters on a layer (default: active layer)."""
        def _do():
            if index is None:
                layer = self._engine.get_active_layer()
            elif 0 <= index < len(self._engine.layers):
                layer = self._engine.layers[index]
            else:
                return
            if layer:
                layer.reset_params()
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # Render commit helper (AUD-25904)
    # ==================================================================

    def _invalidate_video_render_params_if_loaded(self) -> None:
        """S5.C 等价的视频缓存失效（主线程侧执行）。

        与 UI 侧 ``layer_controller._invalidate_video_render_params_
        if_playing`` 同一语义：视频已加载（或当前文件是视频）时清除
        video_controller 缓存的渲染参数，否则视频播放期间脚本级调色
        会被旧参数缓存覆盖。必须在 bridge 的 ``_do`` 闭包内（主线程）
        调用。
        """
        mw = self._find_main_window()
        if mw is None:
            return
        video_controller = getattr(mw, "video_controller", None)
        if video_controller is None or not hasattr(
            video_controller, "invalidate_cached_render_params"
        ):
            return
        is_loaded = False
        is_video_loaded = getattr(video_controller, "is_video_loaded", None)
        try:
            is_loaded = (
                bool(is_video_loaded()) if callable(is_video_loaded) else False
            )
        except Exception:
            is_loaded = False
        if is_loaded or getattr(mw, "current_file_is_video", False):
            video_controller.invalidate_cached_render_params()

    def _commit_render(self) -> None:
        """渲染提交统一入口：render_pipeline + 视频缓存失效（AUD-25904）。

        仅在主线程（``_do`` 闭包内）调用。
        """
        self._engine.render_pipeline()
        self._invalidate_video_render_params_if_loaded()

    # ==================================================================
    # Primary Color Grading
    # ==================================================================

    def grade(self, exposure=None, contrast=None, saturation=None,
              vibrance=None, temperature=None, tint=None,
              highlights=None, shadows=None,
              hue_shift=None, mid_detail=None, sharpness=None,
              lift=None, gamma=None, gain=None) -> None:
        """Set multiple grading parameters at once on the active layer.

        Only provided keyword arguments are changed.  This is more efficient
        than calling individual setters because it triggers only one render.
        """
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")
            gp = layer.grading_params
            if exposure is not None:
                gp["exposure"] = exposure
            if contrast is not None:
                gp["contrast"] = contrast
            if saturation is not None:
                gp["saturation"] = saturation
            if vibrance is not None:
                gp["vibrance"] = vibrance
            if temperature is not None:
                gp["temp"] = temperature
            if tint is not None:
                gp["tint"] = tint
            if highlights is not None:
                gp["highlights"] = highlights
            if shadows is not None:
                gp["shadow"] = shadows
            if hue_shift is not None:
                gp["hue_shift"] = hue_shift
            if mid_detail is not None:
                gp["mid_detail"] = mid_detail
            if sharpness is not None:
                gp["sharpness"] = sharpness
            if lift is not None:
                gp["lift"] = tuple(lift)
            if gamma is not None:
                gp["gamma"] = tuple(gamma)
            if gain is not None:
                gp["gain"] = tuple(gain)
            self._commit_render()
        self._bridge.call_sync(_do)

    # Individual setters (convenience wrappers)

    def set_exposure(self, value: float) -> None:
        self.grade(exposure=value)

    def set_contrast(self, value: float) -> None:
        self.grade(contrast=value)

    def set_saturation(self, value: float) -> None:
        self.grade(saturation=value)

    def set_vibrance(self, value: float) -> None:
        self.grade(vibrance=value)

    def set_temperature(self, value: float) -> None:
        self.grade(temperature=value)

    def set_tint(self, value: float) -> None:
        self.grade(tint=value)

    def set_highlights(self, value: float) -> None:
        self.grade(highlights=value)

    def set_shadows(self, value: float) -> None:
        self.grade(shadows=value)

    def set_hue_shift(self, value: float) -> None:
        self.grade(hue_shift=value)

    def set_mid_detail(self, value: float) -> None:
        self.grade(mid_detail=value)

    def set_sharpness(self, value: float) -> None:
        self.grade(sharpness=value)

    def set_lift(self, h: float, s: float, b: float) -> None:
        self.grade(lift=(h, s, b))

    def set_gamma(self, h: float, s: float, b: float) -> None:
        self.grade(gamma=(h, s, b))

    def set_gain(self, h: float, s: float, b: float) -> None:
        self.grade(gain=(h, s, b))

    # ==================================================================
    # Curves
    # ==================================================================

    def set_curves(self, master=None, red=None, green=None, blue=None) -> None:
        """Apply RGB curves.  Each curve is a list of (input, output) pairs.

        The *master* curve applies uniformly to all three channels.
        Individual *red*, *green*, *blue* curves override the per-channel
        values set by *master*.

        **Coordinates are in 0-255 range** and follow the CurvesWidget
        convention, whose **Y axis is inverted** (Qt screen coordinates):
        y=0 is the top of the curve widget (output pushed toward white)
        and y=255 is the bottom (output pushed toward black).  The
        identity curve is ``[(0, 255), (255, 0)]``; dragging a point
        *up* (smaller y) brightens, *down* (larger y) darkens.

        Example (brighten midtones)::

            topos.set_curves(master=[(0, 255), (128, 100), (255, 0)])

        Note: Internally the LUT array uses RGB channel order (index 0=Red,
        1=Green, 2=Blue), matching the CurvesWidget implementation.
        """
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")
            
            lut = np.zeros((256, 1, 3), dtype=np.float32)
            lut[:, 0, 0] = np.linspace(0.0, 1.0, 256)
            lut[:, 0, 1] = np.linspace(0.0, 1.0, 256)
            lut[:, 0, 2] = np.linspace(0.0, 1.0, 256)
            
            curves_data = {}
            
            if master:
                m_lut = _interpolate_curve_255(master)
                lut[:, 0, 0] = m_lut
                lut[:, 0, 1] = m_lut
                lut[:, 0, 2] = m_lut
                curves_data['M'] = [(float(x), float(y)) for x, y in master]
            else:
                curves_data['M'] = [(0.0, 255.0), (255.0, 0.0)]
            
            if red:
                lut[:, 0, 0] = _interpolate_curve_255(red)
                curves_data['R'] = [(float(x), float(y)) for x, y in red]
            else:
                curves_data['R'] = [(0.0, 255.0), (255.0, 0.0)]
            
            if green:
                lut[:, 0, 1] = _interpolate_curve_255(green)
                curves_data['G'] = [(float(x), float(y)) for x, y in green]
            else:
                curves_data['G'] = [(0.0, 255.0), (255.0, 0.0)]
            
            if blue:
                lut[:, 0, 2] = _interpolate_curve_255(blue)
                curves_data['B'] = [(float(x), float(y)) for x, y in blue]
            else:
                curves_data['B'] = [(0.0, 255.0), (255.0, 0.0)]
            
            self._engine.apply_curves(lut)
            layer.curves_data = curves_data
            # AUD-25904: apply_curves 内部触发渲染，仍需视频缓存失效。
            self._invalidate_video_render_params_if_loaded()
        self._bridge.call_sync(_do)

    def reset_curves(self) -> None:
        """Reset curves to linear."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.curves_lut = None
                layer.curves_data = {}
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # HSL Curves
    # ==================================================================

    def set_hsl_curve(self, curve_type: str, points: list) -> None:
        """Set an HSL curve.

        curve_type: one of 'LumSat', 'HueHue', 'HueSat', 'HueLum', 'SatSat', 'SatLum'
        points: list of (input_255, output_255) pairs.
        
        **Coordinates are in 0-255 range** (matches UI widget coordinates).
        
        Y coordinate conventions:
        - Multiplier types (LumSat, HueSat, HueLum, SatSat, SatLum):
          Y=127.5 means 1.0 (no change), Y=0 means 2.0x, Y=255 means 0.0x
        - Shift type (HueHue):
          Y=127.5 means 0.0 shift, Y=0 means +0.5 shift, Y=255 means -0.5 shift
        """
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")

            type_index = {
                "LumSat": 0, "HueHue": 1, "HueSat": 2,
                "HueLum": 3, "SatSat": 4, "SatLum": 5,
            }
            idx = type_index.get(curve_type)
            if idx is None:
                raise ValueError(
                    f"Unknown HSL curve type: {curve_type}. "
                    f"Valid: {list(type_index.keys())}"
                )

            if layer.hsl_curve_lut is None:
                layer.hsl_curve_lut = _build_default_hsl_lut()
            
            if layer.hsl_curve_data is None:
                layer.hsl_curve_data = {}
            
            layer.hsl_curve_lut[idx] = _interpolate_hsl_curve_255(points, curve_type)
            layer.hsl_curve_data[curve_type] = [(float(x), float(y)) for x, y in points]
            self._engine.apply_hsl_curve(layer.hsl_curve_lut)
            # AUD-25904: apply_hsl_curve 内部触发渲染，仍需视频缓存失效。
            self._invalidate_video_render_params_if_loaded()
        self._bridge.call_sync(_do)

    def reset_hsl_curves(self) -> None:
        """Reset all HSL curves to default."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.hsl_curve_lut = None
                layer.hsl_curve_data = {}
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # LUT
    # ==================================================================

    def apply_lut(self, path: str, intensity: float = 1.0) -> None:
        """Apply a 3D LUT file (.cube).  Path can be absolute or relative
        to the project ``luts/`` directory."""
        def _do():
            if not os.path.isabs(path):
                path_resolved = os.path.join(_project_luts_root(), path)
            else:
                path_resolved = path
            if not os.path.exists(path_resolved):
                raise FileNotFoundError(f"LUT file not found: {path_resolved}")
            self._engine.apply_lut(path_resolved)
            layer = self._engine.get_active_layer()
            if layer:
                layer.lut_intensity = intensity
            self._commit_render()
        self._bridge.call_sync(_do)

    def set_lut_intensity(self, intensity: float) -> None:
        """Adjust LUT intensity (0.0-1.0)."""
        def _do():
            self._engine.set_lut_intensity(intensity)
            # AUD-25904: 引擎方法内部触发渲染，仍需视频缓存失效。
            self._invalidate_video_render_params_if_loaded()
        self._bridge.call_sync(_do)

    def remove_lut(self) -> None:
        """Remove the active LUT from the current layer."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.active_lut_path = None
                layer.lut_intensity = 1.0
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # Qualifier (Secondary Color)
    # ==================================================================

    def set_qualifier(self, hue_center=None, hue_width=None, hue_soft=None,
                      sat_low=None, sat_high=None,
                      sat_soft_low=None, sat_soft_high=None,
                      lum_low=None, lum_high=None,
                      lum_soft_low=None, lum_soft_high=None,
                      invert=None) -> None:
        """Configure the HSL qualifier for secondary color grading."""
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")
            qp = layer.qualifier_params
            qp["enabled"] = True
            if hue_center is not None:
                qp["hue_center"] = hue_center
            if hue_width is not None:
                qp["hue_width"] = hue_width
            if hue_soft is not None:
                qp["hue_soft"] = hue_soft
            if sat_low is not None:
                qp["sat_low"] = sat_low
            if sat_high is not None:
                qp["sat_high"] = sat_high
            if sat_soft_low is not None:
                qp["sat_soft_low"] = sat_soft_low
            if sat_soft_high is not None:
                qp["sat_soft_high"] = sat_soft_high
            if lum_low is not None:
                qp["lum_low"] = lum_low
            if lum_high is not None:
                qp["lum_high"] = lum_high
            if lum_soft_low is not None:
                qp["lum_soft_low"] = lum_soft_low
            if lum_soft_high is not None:
                qp["lum_soft_high"] = lum_soft_high
            if invert is not None:
                qp["invert"] = invert
            self._commit_render()
        self._bridge.call_sync(_do)

    def enable_qualifier(self, enabled: bool = True) -> None:
        """Enable or disable the qualifier."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.qualifier_params["enabled"] = enabled
                self._commit_render()
        self._bridge.call_sync(_do)

    def reset_qualifier(self) -> None:
        """Reset qualifier to default (disabled)."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.qualifier_params = {
                    "enabled": False,
                    "hue_enabled": True, "hue_center": 0.5, "hue_width": 1.0, "hue_soft": 0.1,
                    "sat_enabled": True, "sat_low": 0.0, "sat_high": 1.0,
                    "sat_soft_low": 0.0, "sat_soft_high": 0.0,
                    "lum_enabled": True, "lum_low": 0.0, "lum_high": 1.0,
                    "lum_soft_low": 0.0, "lum_soft_high": 0.0,
                    "blur": 0.0, "clean_black": 0.0, "clean_white": 0.0,
                    "invert": False,
                }
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # Soft Clip (Filmic Tone Mapping)
    # ==================================================================

    def set_soft_clip(self, enabled=True, high=None, white_point=None,
                      shoulder_strength=None, low=None, black_point=None,
                      toe_strength=None, exposure=None, contrast=None) -> None:
        """Configure filmic tone mapping."""
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")
            sp = layer.soft_clip_params
            sp["enabled"] = enabled
            if high is not None:
                sp["high"] = high
            if white_point is not None:
                sp["white_point"] = white_point
            if shoulder_strength is not None:
                sp["shoulder_strength"] = shoulder_strength
            if low is not None:
                sp["low"] = low
            if black_point is not None:
                sp["black_point"] = black_point
            if toe_strength is not None:
                sp["toe_strength"] = toe_strength
            if exposure is not None:
                sp["exposure"] = exposure
            if contrast is not None:
                sp["contrast"] = contrast
            self._commit_render()
        self._bridge.call_sync(_do)

    def reset_soft_clip(self) -> None:
        """Reset soft clip to default."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.soft_clip_params = {
                    "enabled": False, "high": 1.0, "white_point": 1.1,
                    "shoulder_strength": 1.0, "low": 0.0, "black_point": -0.1,
                    "toe_strength": 1.0, "exposure": 0.0, "contrast": 0.0,
                }
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # Film Grain
    # ==================================================================

    def set_film_grain(self, intensity=None, size=None, monochrome=None,
                       shadows=None, midtones=None, highlights=None,
                       seed=None, animated=None) -> None:
        """Configure film grain effect (applied globally).

        P1-COLOR-13: seed 不再隐式取墙钟时间——只在显式传入时更新，
        animated=True 时 seed 随帧号确定推进（视频颗粒动画）。
        """
        def _do():
            fp = self._engine.film_grain_params
            fp["enabled"] = True
            if intensity is not None:
                fp["intensity"] = intensity
            if size is not None:
                fp["size"] = size
            if monochrome is not None:
                fp["monochrome"] = monochrome
            if shadows is not None:
                fp["shadows"] = shadows
            if midtones is not None:
                fp["midtones"] = midtones
            if highlights is not None:
                fp["highlights"] = highlights
            if seed is not None:
                fp["seed"] = float(seed)
            if animated is not None:
                fp["animated"] = bool(animated)
            self._commit_render()
        self._bridge.call_sync(_do)

    def reset_film_grain(self) -> None:
        """Reset film grain."""
        def _do():
            self._engine.film_grain_params = {
                "enabled": False, "intensity": 0.0, "size": 2.0,
                "monochrome": True, "shadows": 0.0, "midtones": 1.0,
                "highlights": 0.0, "seed": 0.0, "animated": False,
            }
            self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # RGB Mixer
    # ==================================================================

    def set_mixer(self, **kwargs) -> None:
        """Set color mixer parameters.

        Each kwarg is a color name (red, orange, yellow, green, aqua, blue,
        purple, magenta) mapping to a dict with optional keys
        ``hue``, ``saturation``, ``lightness`` (or ``luminance``).
        
        Values are converted internally to list format [hue, saturation, luminance]
        for renderer compatibility.
        
        Example::
        
            topos.set_mixer(red={"hue": 20, "saturation": 10, "luminance": -5})
            topos.set_mixer(orange={"hue": 15})  # Only change hue
        """
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")
            mp = layer.mixer_params
            valid_colors = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
            
            for color, values in kwargs.items():
                if color not in valid_colors:
                    continue
                
                if isinstance(values, dict):
                    current = mp.get(color)
                    if not isinstance(current, list) or len(current) != 3:
                        current = [0, 0, 0]
                    
                    hue = values.get('hue', current[0])
                    sat = values.get('saturation', current[1])
                    lum = values.get('luminance', values.get('lightness', current[2]))
                    
                    mp[color] = [
                        max(-100, min(100, int(hue))),
                        max(-100, min(100, int(sat))),
                        max(-100, min(100, int(lum)))
                    ]
                elif isinstance(values, (list, tuple)) and len(values) == 3:
                    mp[color] = [max(-100, min(100, int(v))) for v in values]
            
            self._commit_render()
        self._bridge.call_sync(_do)

    def reset_mixer(self) -> None:
        """Reset mixer to default."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                layer.mixer_params = {}
                self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # Blur / Sharpen Effects
    # ==================================================================

    def set_blur_sharpen(self, mode=None, sharpen=None, texture=None,
                         halo=None, radius=None, iterations=None,
                         enabled=True) -> None:
        """Configure blur/sharpen effects."""
        def _do():
            layer = self._engine.get_active_layer()
            if not layer:
                raise RuntimeError("No active layer")
            ep = layer.effects_params
            ep["enabled"] = enabled
            if mode is not None:
                ep["mode"] = mode
            if sharpen is not None:
                ep["sharpen"] = sharpen
            if texture is not None:
                ep["texture"] = texture
            if halo is not None:
                ep["halo"] = halo
            if radius is not None:
                ep["radius"] = radius
            if iterations is not None:
                ep["iterations"] = iterations
            self._commit_render()
        self._bridge.call_sync(_do)

    # ==================================================================
    # Crop / Rotation / Flip
    # ==================================================================

    def crop(self, x: float, y: float, width: float, height: float) -> None:
        """Crop image. Coordinates are normalized (0.0-1.0)."""
        def _do():
            params = {
                "rect": (x, y, width, height),
                "center": (x + width / 2, y + height / 2),
                "size": (width, height),
                "rotation": 0.0,
            }
            self._engine.apply_crop(params)
        self._bridge.call_sync(_do)

    def rotate(self, degrees: float) -> None:
        """Set rotation for the next crop operation (degrees)."""
        def _do():
            existing = self._engine.crop_params
            existing["rotation"] = degrees
        self._bridge.call_sync(_do)

    def flip(self, horizontal: bool = False, vertical: bool = False) -> None:
        """Flip the current image."""
        if horizontal:
            self._bridge.call_sync(lambda: self._engine.flip_image(horizontal=True))
        if vertical:
            self._bridge.call_sync(lambda: self._engine.flip_image(horizontal=False))

    # ==================================================================
    # Batch Processing
    # ==================================================================

    def batch(self, paths: list, callback, output_dir: str = None,
              format: str = "JPEG", quality: int = 95,
              on_progress=None, on_error: str = "continue") -> list:
        """Process multiple images with *callback*.

        ``callback(topos)`` is called for each image with the topos object
        ready (image loaded, default layer active).

        If *on_progress* is provided, it is called as
        ``on_progress(current, total, path)`` after each file.

        *on_error* controls error handling:
        - ``"continue"`` (default): skip failed files, print a warning
        - ``"stop"``: abort on first error

        If *output_dir* is omitted (None), results are written to a
        ``graded`` subdirectory next to each source file (created
        automatically) — source files are never overwritten.

        Returns a list of output file paths (only successfully processed).
        """
        total = len(paths)
        results = []
        errors = []
        for i, file_path in enumerate(paths):
            if self._bridge.is_aborted():
                raise ScriptAbortError("Script aborted during batch")

            self.set_progress(i, total)
            if on_progress:
                on_progress(i, total, file_path)

            try:
                self.open(file_path)

                # Call the user's callback
                callback(self)

                if output_dir:
                    out_dir = output_dir
                else:
                    # AUD-25901: output_dir=None must never overwrite the
                    # source file — default to a sibling "graded" directory
                    # (created by save()) so originals stay recoverable.
                    out_dir = os.path.join(
                        os.path.dirname(os.path.abspath(file_path)), "graded"
                    )
                name, _ = os.path.splitext(os.path.basename(file_path))
                fmt_ext = {
                    "JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr",
                    "TIFF": ".tif", "WebP": ".webp",
                }
                ext = fmt_ext.get(format, ".jpg")
                out_path = os.path.join(out_dir, name + ext)

                result = self.save(out_path, format=format, quality=quality)
                results.append(result)
                logger.info(f"  Exported: {result}")
            except (ScriptAbortError, KeyboardInterrupt):
                raise
            except Exception as e:
                if on_error == "stop":
                    raise
                logger.error(f"  [Warning] Failed to process {file_path}: {e}")
                errors.append((file_path, str(e)))

        self.set_progress(total, total)
        if on_progress:
            on_progress(total, total, "")
        if errors:
            logger.error(f"[Batch] {len(errors)}/{total} file(s) failed.")
        return results

    def list_files(self, directory: str, extensions: list = None,
                   recursive: bool = False) -> list:
        """List image files in *directory*.

        Returns a sorted list of absolute file paths.
        """
        if extensions is None:
            extensions = [
                ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr",
                ".bmp", ".webp", ".heic", ".heif",
                ".arw", ".cr2", ".cr3", ".nef", ".dng", ".orf",
                ".raf", ".sr2", ".srf", ".rw2", ".pef", ".x3f",
                ".psd",
            ]

        directory = os.path.abspath(directory)
        files = []

        if recursive:
            for root, _dirs, filenames in os.walk(directory):
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() in extensions:
                        files.append(os.path.join(root, fn))
        else:
            for fn in os.listdir(directory):
                if os.path.splitext(fn)[1].lower() in extensions:
                    files.append(os.path.join(directory, fn))

        files.sort()
        return files

    # ==================================================================
    # Utility / Info
    # ==================================================================

    def get_image_size(self) -> tuple:
        """Returns (width, height) of the current image."""
        def _do():
            img = self._engine.img_full_original
            if img is not None:
                h, w = img.shape[:2]
                return (w, h)
            return (0, 0)
        return self._bridge.call_sync(_do)

    def get_layer_count(self) -> int:
        """Returns number of layers."""
        return self._bridge.call_sync(lambda: len(self._engine.layers))

    def get_active_layer_index(self) -> int:
        """Returns the active layer index."""
        return self._bridge.call_sync(lambda: self._engine.active_layer_index)

    def get_grading_params(self) -> dict:
        """Returns a copy of the active layer's grading parameters."""
        def _do():
            layer = self._engine.get_active_layer()
            if layer:
                return dict(layer.grading_params)
            return {}
        return self._bridge.call_sync(_do)

    def print(self, message: str) -> None:
        """Print to the script console."""
        print(message)

    # ==================================================================
    # Undo / Redo Transactions
    # ==================================================================

    def _auto_undo_snapshot(self):
        """Auto-save undo snapshot if begin_undo was not called manually.

        Non-critical: if the snapshot fails (e.g. main thread busy),
        the script can still run — the user just won't be able to
        undo all changes in one step.
        """
        if not self._undo_started:
            try:
                self.begin_undo()
            except Exception:
                # Main thread may be temporarily unresponsive or in an
                # unusual state; skip the snapshot so the script can proceed.
                self._undo_started = True

    def begin_undo(self) -> None:
        """Mark the start of an undo group.

        Saves a snapshot of the current engine state.  All operations
        between ``begin_undo()`` and ``end_undo()`` (or script end)
        can be undone in one step via Ctrl+Z.
        """
        def _do():
            if self._engine.img_full_original is not None:
                self._engine.history.push_state(
                    self._engine.img_full_original,
                    self._engine.layers,
                    self._engine.active_layer_index,
                )
        self._bridge.call_sync(_do)
        self._undo_started = True

    def end_undo(self) -> None:
        """Mark the end of an undo group (no-op, future extensibility)."""
        self._undo_started = False

    # ==================================================================
    # Progress Feedback
    # ==================================================================

    def set_progress(self, current: int, total: int) -> None:
        """Update the progress bar in the status area."""
        # Find the ScriptEngine via the bridge's parent
        parent = self._bridge.parent()
        if parent is not None:
            parent.progress_updated.emit(current, total)

    def set_status(self, message: str) -> None:
        """Update the status bar message text."""
        parent = self._bridge.parent()
        if parent is not None:
            parent.status_message.emit(message)

    # ==================================================================
    # Dialog API
    # ==================================================================

    def input_text(self, label: str, default: str = "") -> str | None:
        """Show an input dialog and return the entered text, or None if cancelled."""
        def _do():
            from PyQt6.QtWidgets import QInputDialog
            parent_widget = self._find_main_window()
            value, ok = QInputDialog.getText(
                parent_widget, "Topos Color", label, text=default
            )
            return value if ok else None
        return self._bridge.call_sync(_do)

    def input_number(self, label: str, default: float = 0.0,
                     min_val: float = None, max_val: float = None,
                     decimals: int = 3) -> float | None:
        """Show a number input dialog. Returns None if cancelled."""
        def _do():
            from PyQt6.QtWidgets import QInputDialog
            parent_widget = self._find_main_window()
            value, ok = QInputDialog.getDouble(
                parent_widget, "Topos Color", label,
                value=default,
                min=min_val if min_val is not None else -2147483647,
                max=max_val if max_val is not None else 2147483647,
                decimals=decimals,
            )
            return value if ok else None
        return self._bridge.call_sync(_do)

    def input_choice(self, label: str, items: list, default: int = 0) -> str | None:
        """Show a choice dialog. Returns the selected item text, or None if cancelled."""
        def _do():
            from PyQt6.QtWidgets import QInputDialog
            parent_widget = self._find_main_window()
            if not items:
                return None
            default_idx = max(0, min(default, len(items) - 1))
            value, ok = QInputDialog.getItem(
                parent_widget, "Topos Color", label,
                items, current=default_idx, editable=False
            )
            return value if ok else None
        return self._bridge.call_sync(_do)

    def input_file(self, label: str = "Select file",
                   filter: str = "All Files (*)") -> str | None:
        """Show a file open dialog. Returns the selected path, or None."""
        def _do():
            from PyQt6.QtWidgets import QFileDialog
            parent_widget = self._find_main_window()
            path, _ = QFileDialog.getOpenFileName(
                parent_widget, label, "", filter
            )
            return path if path else None
        return self._bridge.call_sync(_do)

    def input_folder(self, label: str = "Select folder") -> str | None:
        """Show a folder selection dialog. Returns the selected path, or None."""
        def _do():
            from PyQt6.QtWidgets import QFileDialog
            parent_widget = self._find_main_window()
            path = QFileDialog.getExistingDirectory(
                parent_widget, label
            )
            return path if path else None
        return self._bridge.call_sync(_do)

    def confirm(self, message: str, title: str = "Topos Color") -> bool:
        """Show a Yes/No confirmation dialog. Returns True for Yes."""
        def _do():
            from PyQt6.QtWidgets import QMessageBox
            parent_widget = self._find_main_window()
            reply = QMessageBox.question(
                parent_widget, title, message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes
        return self._bridge.call_sync(_do)

    def _find_main_window(self):
        """Find the active MainWindow for dialog parenting (cached)."""
        if self._main_window is not None:
            return self._main_window
        from PyQt6.QtWidgets import QApplication
        # Prefer visible QMainWindow instances
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible() and isinstance(widget, QMainWindow):
                self._main_window = widget
                return widget
        # Fallback: any top-level widget (prevents dialogs from hiding behind)
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible():
                self._main_window = widget
                return widget
        # Last resort: the desktop as parent
        return None

    # ==================================================================
    # Presets
    # ==================================================================

    def capture_still(self, name: str = None) -> str:
        """Capture a still from the current viewport and save as a preset.

        Args:
            name: Optional preset name. Auto-generated from datetime if None.

        Returns:
            The preset folder path.
        """
        def _do():
            from datetime import datetime as dt
            if name is None:
                preset_name = dt.now().strftime("Still_%Y%m%d_%H%M%S")
            else:
                preset_name = name

            mw = self._find_main_window()
            if mw is None:
                raise RuntimeError("Main window not found")

            if self._engine.img_high is None:
                raise RuntimeError("No image loaded")

            rendered_img = self._engine.grab_viewport_image()
            if rendered_img is None:
                raise RuntimeError("Failed to render image")

            current_folder = mw.presets_panel.get_current_folder()
            preset_folder = os.path.join(current_folder, preset_name)
            os.makedirs(preset_folder, exist_ok=True)

            # Save thumbnail
            import cv2
            import numpy as np
            if rendered_img.dtype == np.float32:
                save_img = (np.clip(rendered_img, 0.0, 1.0) * 255.0).astype(np.uint8)
            else:
                save_img = rendered_img.copy()

            h, w = save_img.shape[:2]
            max_thumb = 512
            if max(h, w) > max_thumb:
                scale = max_thumb / max(h, w)
                save_img = cv2.resize(save_img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

            thumbnail_path = os.path.join(preset_folder, "thumbnail.jpg")
            success, encoded = cv2.imencode('.jpg', save_img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if success:
                encoded.tofile(thumbnail_path)

            # Save grade.json with ALL layers including plugins
            import json
            grade_data = {
                "version": "1.0",
                "layers": [l.to_dict(assets_dir=None) for l in self._engine.layers],
                "active_layer_index": self._engine.active_layer_index
            }
            grade_path = os.path.join(preset_folder, "grade.json")
            with open(grade_path, 'w', encoding='utf-8') as f:
                json.dump(grade_data, f, indent=2, ensure_ascii=False)

            mw.presets_panel.scan_presets()
            return preset_folder

        return self._bridge.call_sync(_do)

    def apply_preset(self, preset_name: str, folder: str = "User") -> None:
        """Apply a preset grade by name.

        Args:
            preset_name: Name of the preset to apply.
            folder: "User" or "System" (default: "User").
        """
        def _do():
            mw = self._find_main_window()
            if mw is None:
                raise RuntimeError("Main window not found")

            presets_root = os.path.join(os.getcwd(), "Presets")
            grade_path = os.path.join(presets_root, folder, preset_name, "grade.json")
            if not os.path.exists(grade_path):
                raise FileNotFoundError(f"Preset not found: {grade_path}")

            mw.on_apply_grade_from_preset(grade_path)

        self._bridge.call_sync(_do)

    def list_presets(self, folder: str = None) -> list:
        """List available presets.

        Args:
            folder: Optional folder path to scan. Defaults to User folder.

        Returns:
            List of preset names.
        """
        def _do():
            presets_root = os.path.join(os.getcwd(), "Presets")
            if folder is None:
                scan_dir = os.path.join(presets_root, "User")
            else:
                scan_dir = os.path.join(presets_root, folder)

            if not os.path.isdir(scan_dir):
                return []

            presets = []
            for name in sorted(os.listdir(scan_dir)):
                preset_dir = os.path.join(scan_dir, name)
                if os.path.isdir(preset_dir) and not name.startswith('.'):
                    grade_path = os.path.join(preset_dir, "grade.json")
                    if os.path.exists(grade_path):
                        presets.append(name)
            return presets

        return self._bridge.call_sync(_do)

    # ==================================================================
    # Script Menu Registration
    # ==================================================================

    def register_action(self, name: str, callback) -> None:
        """Register a named action that appears in the Scripts menu.

        ``callback(topos)`` receives this API object when triggered.
        """
        def _do():
            engine = self._bridge.parent()
            if engine is not None:
                engine.register_action(name, callback)
        self._bridge.call_sync(_do)
