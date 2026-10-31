"""Composite-plugin source node owned by the Teaching Animation feature.

Renders a shared Teaching Animation scene into the Composite graph as a
normal RGBA texture.  The class physically lives here — not in
``src/features/composite/nodes`` — so the Composite package carries no
Teaching Animation implementation; the feature module plugs it into the
Composite ``NODE_REGISTRY`` via ``register_node_type`` when it enables
(see ``module.register_actions``).  Note this is distinct from the
scene-model node of the same name in ``core/model.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from src.features.composite.nodes.base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.nodes.node_state import NodeState
from .core.contracts import AnimationFrame, RenderContext
from .core.professional import read_scene_color_pipeline
from .render.frame_adapter import FrameAdapter
from .scene_store import get_shared_scene_store


LOGGER = logging.getLogger(__name__)


class MotionGraphicNode(Node):
    """Render the scene identified by ``scene_id`` to a normal RGBA texture."""

    node_type = "motion_graphic"
    node_title = "Motion Graphic"
    header_color = "#9b59b6"
    SEARCH_ALIASES = ["motion graphic", "teaching animation", "教学动画"]
    DESCRIPTION = "输出共享 Teaching Animation 场景的 RGBA 图像"
    TAGS = ["input", "teaching", "animation"]
    PARAM_DEFS = [ParamDef("scene_id", "str", "")]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [Port("image", "output", "image")]
        store = get_shared_scene_store()
        # Every newly inserted node owns a fresh scene reference by default.
        # Existing scenes are only selected when the graph loader restores the
        # serialized ``scene_id`` parameter afterward.
        from .core.model import TeachingSceneDocument
        default_scene = TeachingSceneDocument.create_default(name="Motion Graphic Scene")
        store.register(default_scene)
        # AUD-27201: remember the default scene this instance created so the
        # load/repoint paths can unregister it instead of orphaning it in the
        # shared store (orphaned defaults used to be persisted with every
        # save/load cycle and accumulated without bound).
        self._owned_default_scene = default_scene
        self.parameters = {"scene_id": default_scene.scene_id}
        self._texture = None
        self._size = (0, 0)
        self._revision = 0
        self._effect_bridge = None
        self._effect_bridge_gl = None
        # AUD-27203: last-rendered-frame cache.  Motion graphic nodes are
        # dynamic sources (the evaluator re-evaluates them for every displayed
        # frame); without a frame-level cache each evaluation rebuilt the
        # renderer (HTML: browser session per frame; SVG: CPU re-rasterize).
        self._cached_frame_key: Optional[Tuple[str, int, int, int, int]] = None
        self._unsubscribe = get_shared_scene_store().subscribe(self._scene_changed)

    # ------------------------------------------------------------------ #
    # AUD-27201: default-scene ownership
    # ------------------------------------------------------------------ #

    def _release_owned_default_scene(self) -> None:
        """Unregister the default scene this node created, if still pristine.

        Only removes the scene when it is bit-for-bit the default we created
        (never edited in the MG editor).  An edited scene may hold user work
        and stays registered; a scene referenced by a timeline clip also stays
        (removal is skipped conservatively — see ``cleanup``).
        """
        owned = self._owned_default_scene
        if owned is None:
            return
        if self.parameters.get("scene_id") == owned.scene_id:
            # The node still points at it — it is not an orphan.
            return
        store = get_shared_scene_store()
        if store.get(owned.scene_id) == owned:
            store.remove(owned.scene_id)
        self._owned_default_scene = None

    @classmethod
    def from_dict(cls, data: dict) -> "MotionGraphicNode":
        """Deserialize, then drop the constructor's default scene (AUD-27201).

        ``Node.from_dict`` instantiates the class (registering a fresh default
        scene) and then overwrites ``scene_id`` with the serialized value —
        the fresh default would otherwise be orphaned on every load.
        """
        node = super().from_dict(data)
        node._release_owned_default_scene()
        return node

    def set_parameter(self, key: str, value: Any) -> None:
        previous_scene_id = self.parameters.get("scene_id")
        super().set_parameter(key, value)
        if key == "scene_id" and value != previous_scene_id:
            # Repointing the node at another scene orphans the default scene
            # created in ``__init__`` — release it (AUD-27201).
            self._release_owned_default_scene()

    def _scene_changed(self, scene: Any) -> None:
        if scene.scene_id == self.parameters.get("scene_id"):
            self.state = NodeState.DIRTY

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
                     resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
        from src.features.composite.core.eval_context import CompositeEvalContext
        gl = ctx.gl if isinstance(ctx, CompositeEvalContext) else ctx
        scene_id = str(self.parameters.get("scene_id", ""))
        scene = get_shared_scene_store().get(scene_id)
        if scene is None or gl is None:
            return {}
        frame_number = int(getattr(ctx, "frame_number", 0) or 0)
        frame_number = min(max(0, frame_number), scene.duration_frames - 1)
        from .render.scene_renderer_router import (
            backend_for_scene, select_scene_renderer,
        )
        backend = backend_for_scene(scene)
        render_context = RenderContext(
            fps=scene.fps, width=scene.width, height=scene.height,
            backend=backend, transparent=True,
        )
        # The HTML renderer captures premultiplied RGBA for transparent scenes
        # (AlphaMode.PREMULT, html_scene_renderer_v2.py); the SVG renderer emits
        # straight alpha.  Report the truth so the compositor does not
        # double-convert the alpha of an HTML scene (black fringing / halos).
        alpha_premultiplied = backend == "html"
        # AUD-27203: skip the whole prepare → render → cleanup cycle when the
        # requested frame is already in this node's texture.  The key covers
        # scene identity, scene revision (bumped on every store register), the
        # clamped frame index and the raster size — any change re-renders.
        frame_key = (
            scene_id,
            get_shared_scene_store().revision(scene_id),
            frame_number,
            scene.width,
            scene.height,
        )
        frame_cached = (
            self._cached_frame_key == frame_key
            and self._texture is not None
            and self._size == (scene.width, scene.height)
        )
        renderer = None
        if not frame_cached:
            renderer = select_scene_renderer(scene)
            renderer.prepare(scene, render_context)
            try:
                frame = renderer.render_frame(AnimationFrame(frame_number, scene.fps))
                rgba = FrameAdapter.from_rendered_frame(frame).data
            except (ImportError, RuntimeError, ValueError, OSError) as exc:
                # AUD-27202: fail-soft, matching the SVG path's ``scene is
                # None`` return above — an unavailable HTML backend
                # (Playwright not installed / HtmlRenderError) or a renderer
                # error must not crash the composite GL evaluate loop, but it
                # is no longer silently swallowed: the failure is logged so
                # domain errors stay diagnosable.  Truly unexpected errors
                # propagate to the evaluator's own warning handler, which
                # covers the prepare() path the same way.
                LOGGER.warning(
                    "motion_graphic node %s: scene %s frame %d render failed "
                    "(%s: %s); emitting no output this frame",
                    self.id, scene_id, frame_number,
                    type(exc).__name__, exc, exc_info=True,
                )
                return {}
            finally:
                renderer.cleanup()
        if self._texture is None or self._size != (scene.width, scene.height):
            if self._texture is not None:
                self._texture.release()
            self._texture = gl.texture((scene.width, scene.height), 4, dtype="u1")
            self._size = (scene.width, scene.height)
        if not frame_cached:
            self._texture.write(rgba)
            self._cached_frame_key = frame_key
            # Revision — "content version, bumped when texture content
            # changes" — only moves when pixels actually changed, so repeated
            # evaluations of the same frame let downstream caches hold.
            self._revision += 1
        color_pipeline = read_scene_color_pipeline(scene.metadata)
        gpu_effects = tuple()
        bridge_output = False
        try:
            from .render.effect_capabilities import read_scene_gpu_effects, RenderProfile
            from .render.composite_effect_bridge import CompositeEffectBridge

            gpu_effects = read_scene_gpu_effects(scene)
            if gpu_effects:
                if self._effect_bridge is None or self._effect_bridge_gl is not gl:
                    if self._effect_bridge is not None:
                        self._effect_bridge.cleanup()
                    self._effect_bridge = CompositeEffectBridge(gl)
                    self._effect_bridge_gl = gl
                profile = RenderProfile.final() if render_mode in {"export", "full"} else RenderProfile.preview()
                effect_result = self._effect_bridge.apply(
                    self._texture.glo, scene.width, scene.height, gpu_effects,
                    profile=profile, color_pipeline=color_pipeline,
                )
                output_texture_id = effect_result.texture_id
                bridge_output = True
            else:
                output_texture_id = self._texture.glo
        except (ImportError, RuntimeError, TypeError, ValueError):
            # GPU effects are an optional acceleration path.  The source SVG
            # texture remains valid when the shared runner cannot initialize.
            output_texture_id = self._texture.glo
        if bridge_output:
            # Only the GPU-effect bridge path approaches the declared working
            # space / bit depth (f2/f4 scratch surfaces).
            reported_color_space = color_pipeline.working_space
            reported_bit_depth = color_pipeline.bit_depth
        else:
            # AUD-27204: this path delivers the u1 texture — 8-bit, tonemapped,
            # display-referred sRGB data even when the scene declares an HDR
            # pipeline (the SVG/HTML backends rasterize to rgba8).  Report what
            # is actually delivered so downstream HDR/display transforms do
            # not double-process tonemapped pixels; the declared pipeline and
            # the hdr_fallback downgrade stay visible in render_diagnostics.
            reported_color_space = "srgb"
            reported_bit_depth = 8
        return {"image": TextureResult(
            texture_id=output_texture_id,
            width=scene.width,
            height=scene.height,
            revision=self._revision,
            alpha_premultiplied=alpha_premultiplied,
            color_space=reported_color_space,
            bit_depth=reported_bit_depth,
            render_diagnostics=(
                {
                    "gpu_effects": [item.effect_type for item in gpu_effects],
                    "color_pipeline": color_pipeline.to_dict(),
                    "hdr_fallback": "svg_8bit_tonemap" if color_pipeline.is_hdr else "",
                }
                if gpu_effects or color_pipeline.is_hdr else None
            ),
        )}

    def cleanup(self) -> None:
        if self._effect_bridge is not None:
            self._effect_bridge.cleanup()
            self._effect_bridge = None
            self._effect_bridge_gl = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._texture is not None:
            self._texture.release()
            self._texture = None
        self._cached_frame_key = None
        # AUD-27201 note: the node's scene is deliberately NOT removed from
        # the shared store here — timeline clips reference scenes by id and
        # this Qt-free node cannot see the timeline, so deletion-time removal
        # could dangle a clip.  The load/repoint paths above already reclaim
        # the "create-then-discard" default scenes, which is what used to
        # accumulate across save/load cycles.


__all__ = ["MotionGraphicNode"]
