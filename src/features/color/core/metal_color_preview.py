"""Worker-owned Metal grading for Color preview and its full-frame scope input."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from src.features.color.core.color_graph_entry import SharedColorGraphEntry
from src.features.color.core.color_graph_metal import MetalGraphCancelled
from src.features.color.core.color_graph_param_source import graph_node_layer_provider
from src.features.color.core.color_graph_snapshot import ColorGraphSnapshot
from src.features.color.core.color_graph_texture_metal import (
    ColorGraphMetalTextureExecutor,
    texture_layer_domain_errors,
)
from src.features.color.core.frame_color_description import DisplayOutputDescription
from src.features.color.core.metal_color_io import MetalColorIO
from src.features.color.core.metal_color_surface import MetalColorSharedSurface
from src.features.color.core.metal_frame_publish import ColorFrameMetalPublisher, SUBMITTED
from src.features.color.core.scope_types import ScopeDemand
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.present_port import SurfacePublisher
from src.shared.gpu.present_frame import PresentColorInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColorMetalSurface:
    layer: Any
    publisher: SurfacePublisher


class ColorMetalPreview:
    """Keep GL source preparation and consumers on the existing worker.

    The shared fp16 surface preserves the Color renderer's working space and
    source geometry. Native presentation consumes the Metal graph texture
    directly. The editor and scope panels consume a GL copy of the same grade.
    """

    def __init__(self, device: Any, surface: ColorMetalSurface) -> None:
        from src.features.color.ui.metal_color_viewer import ColorMetalViewerPresenter

        self.surface = surface
        self._ctx = MetalComputeContext(device=device)
        self._graph = SharedColorGraphEntry(self._ctx)
        self._executor = ColorGraphMetalTextureExecutor(self._ctx)
        self._io = MetalColorIO(self._ctx)
        self._frames = ColorFrameMetalPublisher(self._ctx)
        self._presenter = ColorMetalViewerPresenter(self._ctx, surface)
        self._shared_surfaces: dict[str, MetalColorSharedSurface] = {}
        self._graph_sizes: dict[bool, tuple[int, int]] = {}
        self._scope_engine = None
        self._sequence = 0
        self._reason = ""
        self.last_submitted_serial = 0

    def fallback(self, reason: str) -> None:
        if reason != self._reason:
            logger.info("Color Metal preview fallback (%s)", reason)
            self._reason = reason

    def _shared_surface(self, target: str, size: tuple[int, int]) -> MetalColorSharedSurface:
        shared = self._shared_surfaces.get(target)
        if shared is None or shared.size != size:
            new_surface = MetalColorSharedSurface(self._ctx.device, size)
            if shared is not None:
                shared.release()
            self._shared_surfaces[target] = shared = new_surface
        return shared

    def grade(
        self,
        snapshot: ColorGraphSnapshot,
        source_frame: int | None,
        source: Any,
        destination: Any,
        *,
        cancelled: Callable[[], bool],
        display: tuple[int, int, int] | None,
        scope_output: bool = False,
    ) -> bool:
        """Return success only after the compatible GL texture is complete."""
        self._sequence += 1
        request_id = f"color-preview-{self._sequence}"
        retained_by_display = False
        try:
            width, height = source.size
            outcome = self._graph.plan_frame(snapshot, size=(width, height))
            if outcome.plan is None or outcome.capability is None or not outcome.capability.executable:
                self.fallback("graph_plan: " + ", ".join(outcome.diagnostics))
                return False

            provider = graph_node_layer_provider(
                snapshot.graph, frame=source_frame, preview_grade=snapshot.preview_grade)
            # GL maps full-image windows to the current ROI, while the Metal
            # executor rasterizes normalized windows in its target texture.
            for render_pass in outcome.plan.passes:
                if render_pass.op not in ("corrector", "shared_corrector"):
                    continue
                layer = provider(render_pass.node_id)
                if isinstance(layer, Mapping) and layer.get("visible") is not False:
                    if layer.get("mask_shapes"):
                        self.fallback("window_crop_geometry")
                        return False
                    issues = texture_layer_domain_errors(layer)
                    if issues:
                        self.fallback("param_domain_unsupported: " + ", ".join(issues))
                        return False

            if cancelled():
                raise MetalGraphCancelled(request_id, -1)
            shared = self._shared_surface(
                "scope_grade" if scope_output else "viewer_grade", (width, height))
            if self._graph_sizes.get(scope_output) != (width, height):
                self._graph_sizes[scope_output] = (width, height)
                self._executor.retain_preview_sizes(frozenset(self._graph_sizes.values()))
            source_texture = shared.copy_from_gl(source)
            result = self._executor.execute_plan(
                outcome.plan, source_texture, request_id=request_id,
                source_size=(width, height),
                cancel_check=lambda _request: cancelled(),
                layer_param_provider=provider,
            )
            if result.diagnostic_code == "cancelled":
                raise MetalGraphCancelled(request_id, -1)
            if not result.published:
                self.fallback(result.diagnostic_code or "graph_execution")
                return False
            if cancelled():
                raise MetalGraphCancelled(request_id, -1)

            output = result.output_texture
            display_profile = DisplayOutputDescription("sdr_passthrough")
            self._io.encode_for_display(output, display_profile, target=shared.texture)
            self._ctx.finish()
            shared.copy_to_gl(destination)
            self._reason = ""
            if display is not None:
                serial, generation, surface_generation = display
                try:
                    status = self._frames.render_frame(
                        (width, height),
                        lambda target: self._io.encode_for_display(
                            output, display_profile, target=target,
                            composite_viewport_background=True),
                        frame=serial, generation_id=generation,
                        color_info=PresentColorInfo(color_range="full", color_transfer="srgb"),
                        working_release=lambda: self._executor.release_revision(request_id),
                        extra={"surface_generation": surface_generation},
                    )
                    retained_by_display = status == SUBMITTED
                    if retained_by_display:
                        self.last_submitted_serial = serial
                except Exception:
                    logger.warning("Color Metal display encoding failed", exc_info=True)
            return True
        except MetalGraphCancelled:
            raise
        except Exception as exc:
            self.fallback(f"{type(exc).__name__}: {exc}")
            return False
        finally:
            if not retained_by_display:
                self._executor.release_revision(request_id)

    def render_scope(self, demand: ScopeDemand, source: Any, destination: Any) -> bool:
        """Publish an unmasked scope into the existing GPU-only panel texture."""
        from src.features.color.core.metal_scope_engine import MetalScopeEngine

        try:
            if self._scope_engine is None:
                self._scope_engine = MetalScopeEngine(self._ctx)
            source_surface = self._shared_surface("scope_input", source.size)
            display_surface = self._shared_surface("scope_display", destination.size)
            source_texture = source_surface.copy_from_gl(source)
            frame = self._scope_engine.encode_display(demand, source_texture)
            if frame is None:
                self._ctx.discard_pending()
                return False
            target, size = frame.textures[demand.mode]
            if size != destination.size:
                self._ctx.discard_pending()
                return False
            self._io.encode_for_display(
                target, DisplayOutputDescription("sdr_passthrough"),
                target=display_surface.texture)
            self._ctx.finish()
            display_surface.copy_to_gl(destination)
            return True
        except Exception:
            self._ctx.discard_pending()
            logger.debug("Color Metal scope failed; running GL", exc_info=True)
            return False

    def poll(self, generation: int, presented: Callable[[int, int, bool], None]) -> None:
        active_port = self._presenter.port
        if active_port is not None:
            active_port.set_request(generation=generation)
        surface = self.surface.publisher.current()
        for published in self._frames.poll():
            frame = published.present
            if (frame.generation_id != generation
                    or frame.extra["surface_generation"] != surface.generation
                    or not surface.is_usable()):
                self._frames.ack(frame, "stale_surface_or_source")
                continue
            def on_presented(ready: Any, transaction: Any = published.transaction) -> None:
                self._frames.note_display_done(transaction)
                presented(ready.frame, ready.generation_id, True)

            try:
                # Color's input is already laid out in viewport coordinates.
                # The host may stop above the transport which occludes the
                # bottom of this image in the GL layout.
                display_width = surface.drawable_size[0]
                accepted = self._presenter.submit(
                    published, image_size=(frame.width, frame.height),
                    display_rect_px=(0.0, 0.0, float(display_width),
                                     display_width * frame.height / frame.width),
                    on_presented=on_presented, on_released=self._frames.ack,
                )
            except Exception:
                logger.warning("Color Metal present submission failed", exc_info=True)
                accepted = False
            if not accepted:
                self._frames.ack(frame, "present_not_attached")
                presented(frame.frame, frame.generation_id, False)
        for outcome in self._presenter.poll():
            if outcome.event == "released" and outcome.reason.startswith("present_failed"):
                presented(outcome.frame.frame, outcome.frame.generation_id, False)

    def release(self) -> None:
        for surface in self._shared_surfaces.values():
            surface.release()
        self._shared_surfaces.clear()
        if self._scope_engine is not None:
            self._scope_engine.release()
            self._scope_engine = None
        port = self._presenter.port
        if port is not None:
            deadline = time.monotonic() + 2.0
            closed, _outcomes = port.close_when_idle()
            while not closed and time.monotonic() < deadline:
                time.sleep(0.002)
                closed, _outcomes = port.close_when_idle()
            if not closed:
                logger.warning("Color Metal drawable still in flight during shutdown")
                return
        if self._frames.shutdown():
            self._executor.release_pool()
            self._io.release()
