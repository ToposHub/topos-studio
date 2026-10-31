"""Volume node (B1, downgraded path) — CPU raymarched density volumes.

Renders a dense ``.npz`` volume (see ``render3d/volume_grid.py`` for the
dependency decision) with a front-to-back Beer–Lambert raymarch along the
view axis.  Emission/temperature and the GPU scene3d pass are deferred
until the OpenVDB packaging gate passes.

``file_path`` accepts a frame pattern for volume sequences — a ``####``
run or ``%04d`` is substituted with the evaluation frame number, matching
the image_input convention.
"""

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox
    from src.features.composite.core.frame_sequence import (
        has_frame_pattern,
        resolve_frame_pattern_path,
    )

logger = logging.getLogger(__name__)

# AUD-7704: CPU gather memory budget.  The raymarch gathers an
# n_samples × h × w float32 slab stack before integrating; with the
# PARAM_DEFS-sanctioned extremes (output 8192×8192, step_count 128 —
# ~34GB) the allocation killed the render thread with MemoryError, which
# the evaluator's outer handler turned into an unexplainable blank frame.
# The gather is downscaled to stay within this budget instead.
_MAX_SAMPLE_GATHER_BYTES = 512 * 1024 * 1024  # 512MB of float32 slabs


def _resolved_file_stat_signature(path: str) -> str:
    """R-21（2026-10-14 审计）：已解析文件的 stat 指纹进内容签名。

    ``runtime_file_signature`` 只驱动重求值；内部 ``_gpu_content_sig``
    此前只含 path/frame/params——同路径 .npz 被覆盖后旧 GPU 纹理仍
    命中，密度热更新不生效。纳秒 mtime 覆盖同秒同尺寸修改；缺文件/
    出错用 "missing" 使签名必然失配（不输出旧图）。
    """
    import os
    try:
        stat = os.stat(path)
        return f"{int(stat.st_mtime_ns)}:{stat.st_size}"
    except OSError:
        return "missing"


def budget_output_size(steps: int, height: int, width: int,
                       max_bytes: int = _MAX_SAMPLE_GATHER_BYTES,
                       ) -> Tuple[int, int]:
    """Clamp the output resolution to the slab-gather memory budget.

    The gather allocates ``steps × height × width`` float32 values (the
    actual sample count never exceeds ``steps`` — see
    :func:`sample_indices` — so this bound is conservative).  When the
    requested size would exceed ``max_bytes``, both axes are scaled by the
    same factor until the allocation fits.  Returns
    ``(effective_height, effective_width)``.
    """
    required = max(1, int(steps)) * max(1, int(height)) \
        * max(1, int(width)) * 4
    if required <= max_bytes:
        return max(1, int(height)), max(1, int(width))
    scale = (float(max_bytes) / float(required)) ** 0.5
    eff_h = max(1, int(int(height) * scale))
    eff_w = max(1, int(int(width) * scale))
    return eff_h, eff_w


def sample_indices(depth: int, steps: int) -> "np.ndarray":
    """Depth-axis sample positions for the requested step budget."""
    stride = max(1, int(np.ceil(depth / max(1, steps))))
    return np.arange(0, depth, stride)


def compute_transmittance(slab: "np.ndarray", sigma: float,
                          step_world: float) -> "np.ndarray":
    """Beer–Lambert front-view transmittance of a sampled slab stack.

    ``slab`` holds the density at the sampled depth positions (n, H, W);
    each sample represents ``step_world`` world units, so the integrated
    optical depth is independent of how many samples the step budget
    chose (thin volumes no longer under-integrate).
    """
    transmittance = np.ones(slab.shape[1:], dtype=np.float32)
    for d in range(int(slab.shape[0])):
        transmittance *= np.exp(
            -slab[d] * float(sigma) * float(step_world)).astype(np.float32)
    return transmittance


class Volume3DNode(Node):
    """Absorption-only volume renderer (CPU raymarch MVP)."""

    node_type = "volume3d"
    # CP-044: 3D 场景载荷族，不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Volume 3D"
    i18n_title_key = "nodes.volume3d.title"
    header_color = "#7a5a8a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "volume", "vdb", "smoke", "fog volume", "体积", "烟雾", "云",
    ]
    DESCRIPTION = "稠密体积渲染（CPU raymarch MVP；.vdb 需待 OpenVDB 打包门）"
    TAGS = ["3d", "volume", "vdb"]

    PARAM_DEFS = [
        ParamDef("file_path", "str", "", group="Volume"),
        ParamDef("density_multiplier", "float", 1.0, min_val=0.0,
                 max_val=20.0, step=0.05, group="Volume", animated=True),
        ParamDef("absorption_color", "color", (1.0, 1.0, 1.0, 1.0),
                 group="Volume", animated=True),
        ParamDef("step_count", "int", 32, min_val=8, max_val=128,
                 step=8, group="Render"),
        ParamDef("output_width", "int", 480, min_val=1, max_val=8192,
                 step=1, group="Render"),
        ParamDef("output_height", "int", 360, min_val=1, max_val=8192,
                 step=1, group="Render"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs: list = []
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "file_path": "",
            "density_multiplier": 1.0,
            "absorption_color": (1.0, 1.0, 1.0, 1.0),
            "step_count": 32,
            "output_width": 480,
            "output_height": 360,
        }
        self.load_error = ""
        # AUD-7700: self-managed GL texture (constant_node pattern). The
        # volume node is not in CACHE_OWNED_TYPES (owns_color_texture=
        # False, node_capabilities.py), so the evaluator never releases
        # its texture on cache eviction — an unheld, unreleased texture
        # created per evaluation leaked one RGBA32F texture per uncached
        # evaluation (frame scrubbing accumulated until GPU OOM).
        self._gpu_texture: Optional[Any] = None
        self._gpu_content_sig: Optional[tuple] = None
        # Set when the held texture is released/rebuilt so the evaluator
        # invalidates prior cache entries still holding the old glo.
        self._cache_invalidated_needed: bool = False

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, Any]) -> bool:
        # Sequences (``####``/``%04d`` patterns) are per-frame sources.
        return has_frame_pattern(str(params.get("file_path", "") or ""))

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {}

    def runtime_file_signature(self) -> str:
        """Cache dimension for on-disk changes (path/mtime/size)."""
        import os
        path = str(self.parameters.get("file_path", "") or "")
        if not path or not os.path.isfile(path):
            return f"{path}:missing"
        try:
            stat = os.stat(path)
            return f"{path}:{int(stat.st_mtime)}:{stat.st_size}"
        except OSError:
            return f"{path}:missing"

    def cleanup(self) -> None:
        """Release the self-managed GL texture (AUD-7700).

        Called by the render-side cleanup dispatch (composite_renderer's
        _handle_cleanup_node / renderer teardown), always on the thread
        that owns the GL context.
        """
        if self._gpu_texture is not None:
            try:
                self._gpu_texture.release()
            except Exception:
                logger.debug("Volume texture release failed", exc_info=True)
            self._gpu_texture = None
            self._gpu_content_sig = None

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        from ..render3d.volume_grid import (
            VolumeDependencyError,
            load_volume,
        )

        self.load_error = ""
        path = str(self.parameters.get("file_path", ""))
        if not path:
            self.error_message = "Volume: no file set"
            return {}
        frame = 0
        if ctx is not None:
            frame = int(getattr(ctx, "frame_number", 0) or 0)
        path = resolve_frame_pattern_path(path, frame)

        depth_params = (path, frame, render_mode)
        # AUD-7700: the content signature covers every input of the CPU
        # raymarch AND the upload — a match means the held texture is
        # still valid, so the expensive recomputation is skipped and, more
        # importantly, no new texture is created (previous behaviour: one
        # new GL texture per evaluation, never released).
        width = max(1, int(self.parameters.get("output_width", 480)))
        height = max(1, int(self.parameters.get("output_height", 360)))
        steps = int(self.parameters.get("step_count", 32))
        if render_mode == "preview":
            steps = max(8, steps // 2)
        # AUD-7704: bound the CPU gather (steps × h × w float32 slabs) to a
        # fixed memory budget — extreme but PARAM_DEFS-legal parameters
        # (8192² × 128 ≈ 34GB) previously reached MemoryError on the render
        # thread and surfaced as an unexplainable blank frame.
        eff_height, eff_width = budget_output_size(
            steps, height, width, _MAX_SAMPLE_GATHER_BYTES)
        downsample_note = ""
        if (eff_height, eff_width) != (height, width):
            logger.warning(
                "Volume: gather budget exceeded (%d steps × %dx%d ≈ %.0fMB); "
                "downsampling output to %dx%d",
                steps, width, height,
                steps * height * width * 4 / (1024 * 1024),
                eff_width, eff_height)
            downsample_note = (
                f"Volume: output downsampled to {eff_width}x{eff_height} "
                "(memory budget)")
        width = eff_width
        height = eff_height
        sigma = max(0.0, float(self.parameters.get(
            "density_multiplier", 1.0)))
        color = self.parameters.get("absorption_color",
                                    (1.0, 1.0, 1.0, 1.0))
        absorption = np.asarray(color[:3], dtype=np.float32)
        content_sig = (
            depth_params, _resolved_file_stat_signature(path),
            width, height, steps, sigma,
            tuple(float(v) for v in absorption),
        )
        if (self._gpu_texture is not None
                and self._gpu_content_sig == content_sig):
            return {"result": TextureResult(
                texture_id=self._gpu_texture.glo, width=width, height=height,
                # AUD-7701 修复：RGB = absorption × alpha 是预乘形式。
                # 之前声明 alpha_premultiplied=False，下游 Merge 对直通
                # alpha 输入会再乘一次 alpha（u_a_straight 预乘），体积层
                # 被系统性压暗（absorption × alpha²）。按预乘数据如实标记。
                alpha_premultiplied=True,
                color_space="linear",
            )}

        try:
            grid = load_volume(path)
        except VolumeDependencyError as exc:
            self.load_error = str(exc)
            self.error_message = f"Volume: {exc}"
            return {}
        except Exception as exc:
            self.load_error = str(exc)
            self.error_message = f"Volume: {exc}"
            return {}
        self.error_message = ""
        if downsample_note:
            # AUD-7704: surface the degradation (set after the load-success
            # clear so the notice survives a successful render).
            self.error_message = downsample_note

        depth, vh, vw = grid.shape

        # Orthographic front view along the depth axis with a fixed
        # square viewport covering the volume's (H, W) extent.
        #
        # Integration consistency: sample every `stride` voxels and give
        # each sample a world length of total_depth / sample_count (=
        # stride · spacing up to the last partial step).  The previous
        # depth/steps vs depth//steps mismatch made thin volumes
        # integrate to a fraction of their true optical depth.
        indices = sample_indices(depth, steps)
        n_samples = len(indices)
        step_world = (depth * float(grid.spacing)) / max(1, n_samples)
        ys = np.clip(
            (np.linspace(0, vh - 1, height)).round().astype(np.int64),
            0, vh - 1)
        xs = np.clip(
            (np.linspace(0, vw - 1, width)).round().astype(np.int64),
            0, vw - 1)
        # Gather only the sampled slabs — the full-depth gather would
        # materialise D×h×w floats for no reason (176MB at 256³/480p).
        slabs = grid.density[np.ix_(indices, ys, xs)]  # (n, h, w)

        transmittance = compute_transmittance(slabs, sigma, step_world)

        # Beer-Lambert absorption: colour the slab by how much light each
        # channel lost. transmittance is (h, w); broadcast absorption (3,)
        # against a trailing new axis so the multiply stays well-formed.
        alpha = 1.0 - transmittance
        frame_img = np.ones((height, width, 4), dtype=np.float32)
        frame_img[..., :3] = absorption[None, None, :] * alpha[..., None]
        frame_img[..., 3] = alpha

        try:
            gl = getattr(ctx, "gl", ctx)
            data = np.ascontiguousarray(np.flipud(frame_img))
            # AUD-7700: release the previous texture before creating the
            # replacement and flag cache invalidation — stale cache entries
            # still hold the released glo (constant_node pattern). Runs on
            # the render thread, so a direct release is safe.
            if self._gpu_texture is not None:
                self._gpu_texture.release()
                self._gpu_texture = None
                self._cache_invalidated_needed = True
            self._gpu_texture = gl.texture((width, height), 4,
                                           data=data.tobytes(),
                                           dtype="f4")
            self._gpu_content_sig = content_sig
            self.bump_output_revision("result")
            return {"result": TextureResult(
                texture_id=self._gpu_texture.glo, width=width, height=height,
                # AUD-7701 修复：RGB = absorption × alpha 是预乘形式。
                # 之前声明 alpha_premultiplied=False，下游 Merge 对直通
                # alpha 输入会再乘一次 alpha（u_a_straight 预乘），体积层
                # 被系统性压暗（absorption × alpha²）。按预乘数据如实标记。
                alpha_premultiplied=True,
                color_space="linear",
            )}
        except Exception as exc:
            logger.warning("Volume upload failed: %s", exc)
            return {}
