"""Point cloud node (B2) — PLY/xyz or tracker-data point rendering.

Data sources (plan B2):
* ``file``    — ``.ply`` (trimesh, vertex colours honoured) / ``.xyz``
* ``tracker`` — a TrackerNode's track_data JSON (per-frame translations
  become planar points); the UI copies the tracker's serialized data via
  :meth:`set_track_data` (live graph references are follow-up wiring)

Rendering is the deterministic CPU rasterizer in
``render3d/point_cloud.py`` (own evaluation, particle precedent); the
Scene3D GL_POINTS pass is the follow-up integration.
"""

import logging
from typing import Any, Dict, Optional

import numpy as np

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

logger = logging.getLogger(__name__)

COLOR_MODES = ["flat", "vertex_color", "depth_ramp"]
SOURCES = ["file", "tracker"]


class PointCloud3DNode(Node):
    """Render a point cloud as an image (fit-to-frame orthographic)."""

    node_type = "pointcloud3d"
    # CP-044: 3D 场景载荷族，不参与通用 mask。
    GENERIC_MASK_MIX = False

    node_title = "Point Cloud 3D"
    i18n_title_key = "nodes.pointcloud3d.title"
    header_color = "#7a5a8a"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "point cloud", "ply", "pointcloud", "点云", "点云渲染",
    ]
    DESCRIPTION = "PLY/xyz 点云或跟踪数据点云渲染（对位检查参考）"
    TAGS = ["3d", "pointcloud", "reference"]

    PARAM_DEFS = [
        ParamDef("source", "option", "file", labels=SOURCES,
                 group="Point Cloud"),
        ParamDef("file_path", "str", "", group="Point Cloud"),
        ParamDef("track_data", "str", "{}", group="Point Cloud",
                 hidden=True),
        ParamDef("output_width", "int", 640, min_val=1, max_val=8192,
                 step=1, group="Output"),
        ParamDef("output_height", "int", 480, min_val=1, max_val=8192,
                 step=1, group="Output"),
        ParamDef("point_size", "int", 2, min_val=1, max_val=16, step=1,
                 group="Render", animated=True),
        ParamDef("color_mode", "option", "flat", labels=COLOR_MODES,
                 group="Render"),
        ParamDef("point_color", "color", (0.85, 0.9, 1.0, 1.0),
                 group="Render", animated=True),
        ParamDef("opacity", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Render", animated=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs: list = []
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "source": "file",
            "file_path": "",
            "track_data": "{}",
            "output_width": 640,
            "output_height": 480,
            "point_size": 2,
            "color_mode": "flat",
            "point_color": (0.85, 0.9, 1.0, 1.0),
            "opacity": 1.0,
        }
        self.load_error = ""
        # AUD-7110 修复：自管 GPU 纹理（text/particle 节点同款模式）。
        # 之前每次求值直接 gl.texture(...) 新建纹理，节点不持有引用、
        # resource_pool/缓存都不接管释放（owns_color_texture=False），
        # 动画参数逐帧重求值即泄漏一张 w×h RGBA32F 纹理。
        # 内容签名变化时先 release 再重建，并在 cleanup() 释放。
        self._gpu_texture: Optional[Any] = None
        self._gpu_ctx: Optional[Any] = None
        self._last_content_sig: Optional[tuple] = None
        self._revision: int = 0
        # 纹理 release+重建后置 True：求值器据此失效仍引用旧 glo 的缓存
        # 条目（本节点与下游消费者，见 base_evaluator 的处理钩子）。
        self._cache_invalidated_needed: bool = False

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, Any]) -> bool:
        return False

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

    # ── Tracker import helper ──────────────────────────────────────────
    def set_track_data(self, data: Dict[str, Any]) -> None:
        import json
        self.set_parameter("track_data", json.dumps(
            data or {}, ensure_ascii=False))

    # ── Evaluation (own CPU rendering — particle precedent) ────────────
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        from ..render3d.point_cloud import (
            load_point_cloud,
            points_from_track_data,
            rasterize,
        )
        self.load_error = ""
        width = max(1, int(self.parameters.get("output_width", 640)))
        height = max(1, int(self.parameters.get("output_height", 480)))
        source = self.parameters.get("source", "file")

        points = None
        colors = None
        try:
            if source == "tracker":
                import json
                try:
                    data = json.loads(
                        str(self.parameters.get("track_data", "{}")))
                except (TypeError, ValueError):
                    data = {}
                points, _w, _h = points_from_track_data(data)
                if len(points) == 0:
                    raise ValueError("no valid track points")
            else:
                path = str(self.parameters.get("file_path", ""))
                if not path:
                    raise ValueError("no point cloud file set")
                cloud = load_point_cloud(path)
                points = cloud.positions
                colors = cloud.colors
        except Exception as exc:
            self.load_error = str(exc)
            self.error_message = f"Point Cloud: {exc}"
            return {}
        self.error_message = ""

        color_mode = self.parameters.get("color_mode", "flat")
        if color_mode not in COLOR_MODES:
            color_mode = "flat"
        color = self.parameters.get("point_color", (0.85, 0.9, 1.0, 1.0))
        flat = (float(color[0]), float(color[1]), float(color[2]))
        opacity = min(1.0, max(0.0,
                     float(self.parameters.get("opacity", 1.0))))

        # Transparent background: the cloud composites over whatever is
        # below it; ``opacity`` scales the POINT alpha once (writing it
        # into the background too would square it for empty pixels).
        frame = rasterize(points, colors, width, height,
                          point_size=int(self.parameters.get("point_size",
                                                            2)),
                          color_mode=color_mode, flat_color=flat,
                          background=(0.0, 0.0, 0.0, 0.0),
                          fit=True)
        frame[..., 3] *= opacity

        # Upload (defensive — no GL surface keeps the error state).
        try:
            gl = getattr(ctx, "gl", ctx)
            # AUD-7110：内容签名覆盖全部影响像素的参数（含文件 mtime/size
            # 与 animated 参数的当前值）。签名/GL 上下文未变时复用自管
            # 纹理，不再每次求值新建一张无人释放的 GPU 纹理。
            content_sig = (
                source,
                str(self.parameters.get("file_path", "")),
                self.runtime_file_signature(),
                str(self.parameters.get("track_data", "{}")),
                width, height,
                int(self.parameters.get("point_size", 2)),
                color_mode,
                flat,
                opacity,
            )
            sig_changed = content_sig != self._last_content_sig
            ctx_changed = self._gpu_ctx is not gl
            if self._gpu_texture is not None and (sig_changed or ctx_changed):
                if not ctx_changed:
                    # 上下文已换代时不可跨上下文 release（对象随旧上下文
                    # 一并消亡），仅解除引用（particle 节点同款语义）。
                    try:
                        self._gpu_texture.release()
                    except Exception:
                        pass
                self._gpu_texture = None
            if self._gpu_texture is None:
                data = np.ascontiguousarray(np.flipud(frame))
                self._gpu_texture = gl.texture(
                    (width, height), 4, data=data.tobytes(), dtype="f4")
                self._gpu_ctx = gl
                self._last_content_sig = content_sig
                self._revision += 1
                self._cache_invalidated_needed = True
            from src.features.composite.core.format import ImageFormat
            return {"result": TextureResult(
                texture_id=self._gpu_texture.glo, width=width, height=height,
                revision=self._revision,
                format=ImageFormat(width=width, height=height,
                                   name="Point Cloud"),
                bbox=BBox.full_frame(width, height),
                alpha_premultiplied=False,
                color_space="linear",
            )}
        except Exception as exc:
            logger.warning("Point cloud upload failed: %s", exc)
            return {}

    def cleanup(self) -> None:
        """释放自管 GPU 纹理（AUD-7110：纹理生命周期绑定节点）。"""
        if self._gpu_texture is not None:
            try:
                self._gpu_texture.release()
            except Exception:
                pass
            self._gpu_texture = None
            self._cache_invalidated_needed = True
        self._gpu_ctx = None
