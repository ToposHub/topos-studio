"""Present GPU frames into a Metal layer without going through OpenGL.

Metal migration plan task 2.1.  The Edit preview currently computes eligible
frames with Metal, lands them in an IOSurface, imports that surface into
OpenGL and presents through the GL slot pool.  This module is the other end
of the native path: it takes a frame that is already a Metal texture and puts
it on a ``CAMetalLayer`` with one render pass.

Thread contract:

- The GUI thread owns the window side: it creates and removes the layer and
  publishes the surface state (drawable size, scale, visibility, size
  generation) through :class:`SurfacePublisher`.
- The thread that owns the Metal device (the render thread) calls
  :meth:`MetalPresentPort.poll`.  Everything that touches Metal — taking a
  drawable, encoding, committing — happens there.
- Any thread may call :meth:`MetalPresentPort.submit`; it only queues.
- The driver's completion and presentation handlers only record facts.
  Leases are returned and callbacks run on the owner thread in ``poll``.

Frame lifetime: ``pending -> in_flight -> done``.  A frame is *released*
exactly once, when its present command buffer has completed **and** it is no
longer the picture on screen.  The on-screen frame is kept after it was
presented so that a resize, zoom or pan can show it again without a new
render; it is released when a newer frame is committed, or at shutdown.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

from src.shared.gpu.metal.context import _autoreleasepool

logger = logging.getLogger(__name__)

_UNSET = object()


class MetalPresentError(RuntimeError):
    """The present pass could not be built or a frame could not be presented."""


PRESENT_MSL = """
#include <metal_stdlib>
using namespace metal;

// Picture rectangle inside the drawable in normalized coordinates with the
// origin at the bottom-left corner (Metal framebuffer convention), the colour
// shown around it and the row order of the source texture.
struct PresentParams {
    float4 rect;
    float4 background;
    int flip_y;
    int3 pad;
};

struct PresentVertexOut {
    float4 position [[position]];
    float2 uv;
};

vertex PresentVertexOut present_vertex(uint vid [[vertex_id]]) {
    const float2 corners[3] = {
        float2(-1.0, -1.0), float2(3.0, -1.0), float2(-1.0, 3.0)};
    const float2 p = corners[vid];
    PresentVertexOut out;
    out.position = float4(p, 0.0, 1.0);
    out.uv = float2((p.x + 1.0) * 0.5, (p.y + 1.0) * 0.5);
    return out;
}

fragment float4 present_fragment(PresentVertexOut in [[stage_in]],
                                 texture2d<float> frame [[texture(0)]],
                                 sampler present_sampler [[sampler(0)]],
                                 constant PresentParams &params [[buffer(0)]]) {
    const float2 local = (in.uv - params.rect.xy) / params.rect.zw;
    // Outside the picture: keep the background colour.
    if (any(local < 0.0) || any(local > 1.0)) {
        return params.background;
    }
    const float2 tex = params.flip_y != 0 ? float2(local.x, 1.0 - local.y) : local;
    return float4(frame.sample(present_sampler, tex).rgb, 1.0);
}
"""

# The viewer background the GL path clears to.
DEFAULT_BACKGROUND = (0.0980392157, 0.0980392157, 0.0980392157, 1.0)

# rect(4) + background(4) + flip_y(1) + pad(3) as float32 / int32.
_PRESENT_PARAMS = np.dtype(
    [("rect", "<f4", 4), ("background", "<f4", 4), ("flip_y", "<i4"), ("pad", "<i4", 3)])


def build_present_params(
    rect: Tuple[float, float, float, float],
    *,
    flip_y: bool,
    background: Tuple[float, float, float, float] = DEFAULT_BACKGROUND,
) -> np.ndarray:
    """Pack the fragment parameters (``rect`` normalized, origin bottom-left)."""
    values = np.zeros(1, dtype=_PRESENT_PARAMS)
    values["rect"] = np.asarray(rect, dtype=np.float32)
    values["background"] = np.asarray(background, dtype=np.float32)
    values["flip_y"] = 1 if flip_y else 0
    return values


def normalized_rect(
    display_rect_px: Tuple[float, float, float, float],
    drawable_size: Tuple[int, int],
) -> Tuple[float, float, float, float]:
    """Convert a top-left-origin rectangle to the shader's convention.

    Both the rectangle and the surface dimensions must use the same units.
    Qt widget rectangles use logical units, while Metal drawable sizes use
    physical pixels.
    """
    x, y, width, height = (float(value) for value in display_rect_px)
    surface_w = float(max(1, int(drawable_size[0])))
    surface_h = float(max(1, int(drawable_size[1])))
    return (
        x / surface_w,
        1.0 - (y + height) / surface_h,
        width / surface_w,
        height / surface_h,
    )


# Row order of the source texture, by the convention of the producer:
# compute output and IOSurface-backed textures start with the top row.
ORIGIN_TOP_LEFT = "top_left"
ORIGIN_BOTTOM_LEFT = "bottom_left"


@dataclass(frozen=True)
class SurfaceState:
    """What the GUI thread published about the layer.

    ``generation`` changes whenever the drawable size, the scale or the screen
    changes; a frame rendered for an older generation is stale for the
    viewport even when its timeline generation is current.
    """

    generation: int = 0
    drawable_size: Tuple[int, int] = (0, 0)
    scale: float = 1.0
    visible: bool = False

    def is_usable(self) -> bool:
        return self.visible and self.drawable_size[0] > 0 and self.drawable_size[1] > 0


class SurfacePublisher:
    """Thread-safe holder of the latest :class:`SurfaceState`."""

    def __init__(self, initial: Optional[SurfaceState] = None) -> None:
        self._lock = threading.Lock()
        self._state = initial or SurfaceState()

    def publish(self, **changes: Any) -> SurfaceState:
        """Replace some fields; the generation advances unless given explicitly."""
        with self._lock:
            current = self._state
            fields = {
                "generation": current.generation,
                "drawable_size": current.drawable_size,
                "scale": current.scale,
                "visible": current.visible,
            }
            changed = any(fields[key] != value for key, value in changes.items())
            fields.update(changes)
            if changed and "generation" not in changes:
                fields["generation"] = current.generation + 1
            self._state = SurfaceState(**fields)
            return self._state

    def current(self) -> SurfaceState:
        with self._lock:
            return self._state


@dataclass
class PresentRequest:
    """What a frame must match when it reaches the viewport (checked again)."""

    generation: int = 0
    scrub_token: Optional[int] = None
    render_size: Optional[Tuple[int, int]] = None
    deadline_ns: int = 0


@dataclass
class HeldFrame:
    """One frame on its way to the screen, plus who to tell."""

    frame: Any                              # GpuPresentFrame with a native descriptor
    rect: Tuple[float, float, float, float]
    display_rect_px: Tuple[float, float, float, float]
    on_presented: Optional[Callable[[Any], None]] = None
    on_released: Optional[Callable[[Any, str], None]] = None
    on_displayed: Optional[Callable[[Any], None]] = None
    token: int = 0                          # assigned when committed
    status: str = "pending"                 # pending | in_flight | done
    released: bool = False
    #: A repeated showing of the on-screen frame (resize, zoom, pan) carries
    #: no lease of its own; ``source`` is the record whose texture it draws.
    source: Optional["HeldFrame"] = None
    #: Travel with the frame: a later submission must not loosen what an
    #: earlier waiting frame has to satisfy.
    scrub_token: Optional[int] = None
    deadline_ns: int = 0

    @property
    def is_repeat(self) -> bool:
        return self.source is not None


@dataclass
class PresentOutcome:
    """Something :meth:`MetalPresentPort.poll` decided about one frame."""

    frame: Any
    event: str                              # presented | released
    reason: str = ""                        # why a frame was released


class DrawableSink:
    """Where presented frames go.  ``CAMetalLayerSink`` is the real one."""

    pixel_format: int = 0

    def next_drawable(self) -> Optional[Any]:
        """Return a renderable texture, or ``None`` when none is free right now."""
        raise NotImplementedError

    @staticmethod
    def drawable_texture(drawable: Any) -> Any:
        """The color attachment texture of a drawable."""
        raise NotImplementedError

    def present(self, command_buffer: Any, drawable: Any) -> None:
        """Show ``drawable`` once ``command_buffer`` completes."""
        raise NotImplementedError

    def on_presented(self, drawable: Any, callback: Callable[[], None]) -> None:
        """Run ``callback`` when ``drawable`` reaches the screen (best effort)."""
        raise NotImplementedError

    def discard_ready(self) -> None:
        """Give back a drawable that was fetched but no frame will use."""

    def close(self) -> None:
        """Stop handing out drawables; whatever was fetched is given back."""


class MetalPresentPass:
    """The render pass that samples one frame texture into one drawable."""

    def __init__(self, context: Any, pixel_format: int) -> None:
        import Metal

        self._context = context
        self._metal = Metal
        self._device = context.device
        library, error = self._device.newLibraryWithSource_options_error_(
            PRESENT_MSL, None, None)
        if library is None:
            raise MetalPresentError(
                "present shader compile failed: "
                f"{getattr(error, 'localizedDescription', lambda: error)()}")
        descriptor = Metal.MTLRenderPipelineDescriptor.alloc().init()
        descriptor.setLabel_("topos.native_present")
        descriptor.setVertexFunction_(library.newFunctionWithName_("present_vertex"))
        descriptor.setFragmentFunction_(library.newFunctionWithName_("present_fragment"))
        descriptor.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(
            int(pixel_format))
        pipeline, error = self._device.newRenderPipelineStateWithDescriptor_error_(
            descriptor, None)
        if pipeline is None:
            raise MetalPresentError(
                "present pipeline creation failed: "
                f"{getattr(error, 'localizedDescription', lambda: error)()}")
        self._pipeline = pipeline
        sampler = Metal.MTLSamplerDescriptor.alloc().init()
        sampler.setMinFilter_(Metal.MTLSamplerMinMagFilterLinear)
        sampler.setMagFilter_(Metal.MTLSamplerMinMagFilterLinear)
        sampler.setMipFilter_(Metal.MTLSamplerMipFilterNotMipmapped)
        # The binding renamed the address-mode selectors; support both.
        for axis in ("S", "T"):
            setter = getattr(sampler, f"set{axis}AddressMode_", None)
            if setter is None:
                setter = getattr(sampler, f"setAddressMode{axis}_")
            setter(Metal.MTLSamplerAddressModeClampToEdge)
        self._sampler = self._device.newSamplerStateWithDescriptor_(sampler)
        queue = getattr(context, "command_queue", None)
        if queue is None:
            # MetalComputeContext keeps its queue private; it is the same
            # device and queue every compute dispatch of this frame used.
            queue = context._queue  # noqa: SLF001
        self._queue = queue
        self._params_cache: Dict[Tuple[Any, ...], Any] = {}

    def draw(
        self,
        held: HeldFrame,
        drawable: Any,
        sink: DrawableSink,
        *,
        on_complete: Callable[[Any], None],
        on_actual_present: Callable[[], None],
    ) -> None:
        """Encode and commit one present.  Owner thread only."""
        Metal = self._metal
        descriptor = held.source.frame.native if held.is_repeat else held.frame.native
        flip_y = descriptor.origin != ORIGIN_BOTTOM_LEFT
        background = tuple(getattr(descriptor, "background", None) or DEFAULT_BACKGROUND)
        key = (tuple(round(float(value), 6) for value in held.rect), flip_y, tuple(background))
        params = self._params_cache.get(key)
        if params is None:
            params = self._context.create_buffer_from_numpy(
                build_present_params(held.rect, flip_y=flip_y, background=background))
            if len(self._params_cache) >= 16:
                self._params_cache.clear()
            self._params_cache[key] = params

        render_pass = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
        attachment = render_pass.colorAttachments().objectAtIndexedSubscript_(0)
        attachment.setTexture_(sink.drawable_texture(drawable))
        attachment.setLoadAction_(Metal.MTLLoadActionClear)
        attachment.setStoreAction_(Metal.MTLStoreActionStore)
        attachment.setClearColor_(background)
        command_buffer = self._queue.commandBuffer()
        encoder = command_buffer.renderCommandEncoderWithDescriptor_(render_pass)
        encoder.setRenderPipelineState_(self._pipeline)
        encoder.setVertexBuffer_offset_atIndex_(params, 0, 0)
        encoder.setFragmentBuffer_offset_atIndex_(params, 0, 0)
        encoder.setFragmentTexture_atIndex_(descriptor.resource, 0)
        encoder.setFragmentSamplerState_atIndex_(self._sampler, 0)
        encoder.drawPrimitives_vertexStart_vertexCount_(
            Metal.MTLPrimitiveTypeTriangle, 0, 3)
        encoder.endEncoding()
        # The driver's handlers only record; the owner thread acts in poll().
        command_buffer.addCompletedHandler_(on_complete)
        sink.on_presented(drawable, on_actual_present)
        sink.present(command_buffer, drawable)
        command_buffer.commit()


class CAMetalLayerSink(DrawableSink):
    """Drawables of one ``CAMetalLayer``, fetched off the owner's thread.

    ``nextDrawable`` waits for the display to hand back a drawable when none
    is free, and ``allowsNextDrawableTimeout = False`` does not change that
    on every system: measured on macOS 15, the layer still sat for about a
    second before answering nil.  Asked from the render thread's poll loop,
    that stalls every frame the viewer shows, so the layer is asked on a
    helper thread instead: :meth:`next_drawable` hands over a drawable that
    has arrived and returns ``None`` at once otherwise.  At most one
    drawable waits and one request is in progress; a drawable nobody takes is
    given back by :meth:`discard_ready`.
    """

    _FETCH_THREAD = "topos-metal-drawable"

    def __init__(self, layer: Any, pixel_format: int) -> None:
        self._layer = layer
        self.pixel_format = int(pixel_format)
        self._lock = threading.Lock()
        self._ready: List[Any] = []
        self._fetching = False
        self._closed = False

    def next_drawable(self) -> Optional[Any]:
        with self._lock:
            if self._ready:
                return self._ready.pop()
            if self._closed:
                return None
            fetch = not self._fetching
            self._fetching = self._fetching or fetch
        if fetch:
            threading.Thread(target=self._fetch, name=self._FETCH_THREAD, daemon=True).start()
        return None

    def _fetch(self) -> None:
        """Ask the layer; may block.  Runs on the helper thread only."""
        drawable = None
        try:
            with _autoreleasepool():
                drawable = self._layer.nextDrawable()
        except Exception:  # noqa: BLE001 - a failed fetch is "no drawable"
            logger.debug("CAMetalLayerSink: nextDrawable failed", exc_info=True)
        with self._lock:
            self._fetching = False
            if drawable is not None and not self._closed:
                self._ready.append(drawable)

    def discard_ready(self) -> None:
        with self._lock:
            self._ready.clear()            # the references go; the layer recycles them

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ready.clear()
            self._layer = None

    @staticmethod
    def drawable_texture(drawable: Any) -> Any:
        return drawable.texture()

    def present(self, command_buffer: Any, drawable: Any) -> None:
        command_buffer.presentDrawable_(drawable)

    def on_presented(self, drawable: Any, callback: Callable[[], None]) -> None:
        add_handler = getattr(drawable, "addPresentedHandler_", None)
        if callable(add_handler):
            add_handler(lambda _drawable: callback())


class MetalPresentPort:
    """Presents native frames from the thread that owns the Metal device.

    At most ``max_in_flight`` drawables are handed out at once and at most
    ``max_pending`` frames wait to be shown.  A frame that no longer matches
    the request, that missed its deadline, or that cannot be presented is
    released — its lease goes back and the last picture stays on screen.

    A lease-owning frame is released exactly once, when all of these hold:
    its own present completed, a newer frame replaced it on screen, and no
    repeated showing still draws its texture.
    """

    def __init__(
        self,
        sink: DrawableSink,
        presenter: Optional[MetalPresentPass] = None,
        publisher: Optional[SurfacePublisher] = None,
        *,
        max_in_flight: int = 2,
        max_pending: int = 2,
        wall_ns: Optional[Callable[[], int]] = None,
        stall_timeout_s: float = 1.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if int(max_in_flight) < 1 or int(max_pending) < 1:
            raise ValueError("max_in_flight and max_pending must be >= 1")
        if float(stall_timeout_s) <= 0.0:
            raise ValueError("stall_timeout_s must be > 0")
        self._sink = sink
        self._presenter = presenter
        self.publisher = publisher or SurfacePublisher()
        self._max_in_flight = int(max_in_flight)
        self._max_pending = int(max_pending)
        self._wall_ns = wall_ns or time.time_ns
        self._clock = clock or time.monotonic
        # A present that was committed but whose completion never came (the
        # display asleep or occluded).  Its frames cannot be released, so the
        # owner stops feeding the port until the GPU reports back.
        self._stall_timeout_s = float(stall_timeout_s)
        self._in_flight_since: Optional[float] = None
        self._stalled = False
        # A frame has been waiting for a drawable since this moment; the
        # layer hands none out while it is off screen or out of drawables.
        self._waiting_drawable_since: Optional[float] = None
        self._starved = False
        self._lock = threading.Lock()
        self._pending: Deque[HeldFrame] = deque()
        self._closing = False
        # Frames pushed out of a full wait queue.  They are released by the
        # owner thread in poll(), never on the submitting thread.
        self._displaced: Deque[HeldFrame] = deque()
        self._request = PresentRequest()
        self._records: Dict[int, HeldFrame] = {}
        self._next_token = 1
        self._displayed: Optional[HeldFrame] = None
        # Latest-wins for scrubbing: once a frame with another scrub token (or
        # a playback frame, token None) was submitted, older waiting frames
        # of the previous token are stale.
        self._latest_scrub_token: Optional[int] = None
        self._has_submission = False
        self._completed: Deque[Tuple[int, Optional[str]]] = deque()
        self._displayed_callbacks: Deque[Tuple[Optional[Callable[[Any], None]], Any]] = deque()
        self.stats: Dict[str, int] = {
            "submitted": 0, "presented": 0, "released": 0, "actually_presented": 0,
            "no_drawable": 0, "stale": 0, "past_deadline": 0, "displaced": 0,
            "failed": 0, "stalled": 0, "starved": 0,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def in_flight(self) -> int:
        """Frames whose present command buffer has not completed."""
        with self._lock:
            return self._in_flight_locked()

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def displayed_frame(self) -> Optional[Any]:
        """The picture on screen (kept until a newer frame replaces it)."""
        with self._lock:
            return self._displayed.frame if self._displayed is not None else None

    @property
    def held_leases(self) -> int:
        """Lease-owning frames not released yet."""
        with self._lock:
            return sum(1 for record in self._records.values() if not record.is_repeat)

    @property
    def stalled(self) -> bool:
        """The port cannot show a frame right now; the owner shows frames
        another way until it recovers.

        Either a committed present has not completed within the stall timeout
        (its textures may still be read by the display, so they are kept), or
        a frame has been waiting that long for a drawable the layer does not
        hand out (``starved``): off screen, or out of drawables.
        """
        with self._lock:
            return self._stalled or self._starved

    def current_request(self) -> PresentRequest:
        with self._lock:
            return replace(self._request)

    # ------------------------------------------------------------------
    # 任意线程：发布请求与入队
    # ------------------------------------------------------------------

    def set_request(self, **changes: Any) -> PresentRequest:
        """Publish what the next frame must match (generation, scrub token …)."""
        with self._lock:
            for key, value in changes.items():
                if not hasattr(self._request, key):
                    raise TypeError(f"unknown request field {key!r}")
                setattr(self._request, key, value)
            return replace(self._request)

    def submit(
        self,
        frame: Any,
        *,
        rect: Tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
        display_rect_px: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
        on_presented: Optional[Callable[[Any], None]] = None,
        on_released: Optional[Callable[[Any, str], None]] = None,
        on_displayed: Optional[Callable[[Any], None]] = None,
        scrub_token: Any = _UNSET,
        deadline_ns: int = 0,
    ) -> bool:
        """Queue one native frame; the newest waiting frame wins.

        A full wait queue pushes out its oldest frame — it is older than this
        one, so it would never be shown anyway — and that frame's lease is
        returned by the owner thread.  ``False`` only means the frame was not
        taken at all (the caller keeps its lease).

        ``scrub_token`` and ``deadline_ns`` belong to this frame and are fixed
        at submission: a later frame cannot loosen what a waiting frame has
        to satisfy.  Unless given, both are read from the descriptor — the
        producer stores them in ``extra`` (``None`` means playback, not scrub).
        ``on_presented`` reports a committed present. ``on_displayed`` reports
        the drawable's display notification, when the sink provides one.
        """
        extra = getattr(frame, "extra", None)
        extra = extra if isinstance(extra, dict) else {}
        if scrub_token is _UNSET:
            scrub_token = (extra["scrub_token"] if "scrub_token" in extra
                           else getattr(frame, "scrub_token", None))
        if deadline_ns == 0:
            deadline_ns = int(extra.get("deadline_ns") or 0)
        held = HeldFrame(
            frame=frame, rect=tuple(rect), display_rect_px=tuple(display_rect_px),
            on_presented=on_presented, on_released=on_released, on_displayed=on_displayed,
            scrub_token=scrub_token, deadline_ns=int(deadline_ns or 0),
        )
        with self._lock:
            if self._closing:
                return False
            while len(self._pending) >= self._max_pending:
                self._displaced.append(self._pending.popleft())
                self.stats["displaced"] += 1
            self._pending.append(held)
            self._latest_scrub_token = scrub_token
            self._has_submission = True
            self.stats["submitted"] += 1
        return True

    def represent_displayed(self, rect: Tuple[float, float, float, float]) -> bool:
        """Show the on-screen frame again with a new picture rectangle.

        For a resize, zoom or pan while nothing new is rendered.  A waiting
        repeat is replaced; real frames waiting to be shown are never displaced
        by one — they carry the new rectangle themselves.
        """
        with self._lock:
            source = self._displayed
            if self._closing or source is None or source.released:
                return False
            self._pending = deque(item for item in self._pending if not item.is_repeat)
            if len(self._pending) >= self._max_pending:
                return False
            self._pending.append(HeldFrame(
                frame=source.frame, rect=tuple(rect),
                display_rect_px=source.display_rect_px, source=source,
            ))
        return True

    # ------------------------------------------------------------------
    # 驱动线程：只记录
    # ------------------------------------------------------------------

    def _record_completion(self, token: int, error: Any) -> None:
        with self._lock:
            self._completed.append((token, None if error is None else str(error)))

    def _record_actual_present(self, callback: Optional[Callable[[Any], None]], frame: Any) -> None:
        with self._lock:
            self._displayed_callbacks.append((callback, frame))

    # ------------------------------------------------------------------
    # 资源线程：呈现与回收
    # ------------------------------------------------------------------

    def poll(self) -> List[PresentOutcome]:
        """Reclaim finished frames, then present what is waiting.  Owner thread."""
        outcomes = self._apply_completions()
        with self._lock:
            was_stalled, was_starved = self._stalled, self._starved
            now = self._clock()
            self._stalled = (
                self._in_flight_since is not None
                and now - self._in_flight_since > self._stall_timeout_s
            )
            self._starved = (
                self._waiting_drawable_since is not None
                and now - self._waiting_drawable_since > self._stall_timeout_s
            )
            if self._stalled and not was_stalled:
                self.stats["stalled"] += 1
            if self._starved and not was_starved:
                self.stats["starved"] += 1
        outcomes.extend(self._release_displaced())
        outcomes.extend(self._present_pending())
        with self._lock:
            # A frame that just got its drawable ended the wait this poll.
            self._starved = (
                self._waiting_drawable_since is not None
                and self._clock() - self._waiting_drawable_since > self._stall_timeout_s
            )
            waiting = bool(self._pending)
        if not waiting:
            # A drawable fetched for a frame that is gone would sit unused.
            # (Sinks that fetch synchronously have nothing to give back.)
            discard = getattr(self._sink, "discard_ready", None)
            if callable(discard):
                discard()
            with self._lock:
                self._waiting_drawable_since = None
        return outcomes

    def _release_displaced(self) -> List[PresentOutcome]:
        """Return the leases of frames a newer submission pushed out."""
        with self._lock:
            displaced = list(self._displaced)
            self._displaced.clear()
        outcomes: List[PresentOutcome] = []
        for held in displaced:
            outcome = self._release(held, "displaced")
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _in_flight_locked(self) -> int:
        """Caller holds the lock."""
        return sum(1 for record in self._records.values() if record.status == "in_flight")

    def _apply_completions(self) -> List[PresentOutcome]:
        with self._lock:
            completed = list(self._completed)
            self._completed.clear()
            displayed = list(self._displayed_callbacks)
            self._displayed_callbacks.clear()
            self.stats["actually_presented"] += len(displayed)
            for token, _error in completed:
                record = self._records.get(token)
                if record is not None:
                    record.status = "done"
            if self._in_flight_locked() == 0:
                self._in_flight_since = None
        outcomes: List[PresentOutcome] = []
        for callback, frame in displayed:
            if callback is not None:
                try:
                    callback(frame)
                except Exception:
                    logger.exception("MetalPresentPort: on_displayed failed")
        for token, error in completed:
            record = self._records.get(token)
            if record is not None and error is not None:
                self.stats["failed"] += 1
                outcome = self._release(record, f"present_failed:{error}")
                if outcome is not None:
                    outcomes.append(outcome)
        outcomes.extend(self._release_finished())
        return outcomes

    def _release_finished(self) -> List[PresentOutcome]:
        """Release what is finished and no longer needed for the screen."""
        outcomes: List[PresentOutcome] = []
        with self._lock:
            drawing = {id(record.source) for record in self._records.values()
                       if record.is_repeat and record.status == "in_flight"}
            stale = [
                record for record in self._records.values()
                if record.status == "done" and not record.released
                and (record.is_repeat or (
                    record is not self._displayed and id(record) not in drawing))
            ]
        for record in stale:
            outcome = self._release(record, "replaced")
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _present_pending(self) -> List[PresentOutcome]:
        outcomes: List[PresentOutcome] = []
        while True:
            with self._lock:
                if self._closing or not self._pending or self._in_flight_locked() >= self._max_in_flight:
                    return outcomes
                held = self._pending.popleft()
            reason = self._rejection_reason(held)
            if reason:
                self.stats["past_deadline" if reason == "past_deadline" else "stale"] += 1
                outcome = self._release(held, reason)
                if outcome is not None:
                    outcomes.append(outcome)
                continue
            if self._presenter is None:
                outcome = self._release(held, "presenter_unavailable")
                if outcome is not None:
                    outcomes.append(outcome)
                continue
            drawable = self._sink.next_drawable()
            if drawable is None:
                # Keep the last picture and try again on the next poll; the
                # deadline check retires the frame once it is no longer in time.
                self.stats["no_drawable"] += 1
                with self._lock:
                    self._pending.appendleft(held)
                    if self._waiting_drawable_since is None:
                        self._waiting_drawable_since = self._clock()
                return outcomes
            with self._lock:
                self._waiting_drawable_since = None
            with self._lock:
                if held.is_repeat and self._in_flight_locked() > 0:
                    # Something newer is already on its way: skip the repeat.
                    return outcomes
                token = self._next_token
                self._next_token += 1
                held.token = token
                held.status = "in_flight"
                self._records[token] = held
            try:
                self._presenter.draw(
                    held, drawable, self._sink,
                    on_complete=lambda command_buffer, _token=token: self._record_completion(
                        _token, command_buffer.error()),
                    on_actual_present=lambda callback=held.on_displayed, frame=held.frame:
                    self._record_actual_present(callback, frame),
                )
            except Exception as error:
                logger.warning("MetalPresentPort: present failed: %s", error)
                self.stats["failed"] += 1
                with self._lock:
                    self._records.pop(token, None)
                outcome = self._release(held, f"present_failed:{error}")
                if outcome is not None:
                    outcomes.append(outcome)
                continue
            with self._lock:
                if self._in_flight_since is None:
                    self._in_flight_since = self._clock()
                if not held.is_repeat:
                    self._displayed = held
            self.stats["presented"] += 1
            if held.on_presented is not None:
                try:
                    held.on_presented(held.frame)
                except Exception:
                    logger.exception("MetalPresentPort: on_presented failed")
            outcomes.append(PresentOutcome(held.frame, "presented"))
            outcomes.extend(self._release_finished())

    def _in_flight_locked(self) -> int:
        """Caller holds the lock."""
        return sum(1 for record in self._records.values() if record.status == "in_flight")

    def _rejection_reason(self, held: HeldFrame) -> str:
        surface = self.publisher.current()
        if not surface.is_usable():
            return "stale_surface_unavailable"
        frame = held.frame
        request = self.current_request()
        if request.generation and int(getattr(frame, "generation_id", 0)) != request.generation:
            return "stale_generation"
        if held.is_repeat:
            with self._lock:
                source = held.source
                if source is None or source is not self._displayed or source.released:
                    return "stale_repeat"
            return ""
        if request.scrub_token is not None:
            # Explicitly invalidated by the owner (a newer request exists).
            if held.scrub_token != request.scrub_token:
                return "stale_scrub_token"
        elif self._has_submission and held.scrub_token != self._latest_scrub_token:
            # A frame with another token — or a playback frame — was submitted
            # after this one: this one is no longer what the user asked for.
            return "stale_scrub_token"
        size = (int(getattr(frame, "width", 0)), int(getattr(frame, "height", 0)))
        if request.render_size is not None and size != tuple(request.render_size):
            return "stale_render_size"
        if held.deadline_ns and self._wall_ns() >= int(held.deadline_ns):
            return "past_deadline"
        return ""

    def _release(self, held: HeldFrame, reason: str) -> Optional[PresentOutcome]:
        """Return a frame's lease exactly once.  Owner thread."""
        with self._lock:
            if held.released:
                return None
            held.released = True
            if held.token:
                self._records.pop(held.token, None)
            if self._displayed is held:
                self._displayed = None
            if held.is_repeat:
                # A repeated showing holds no lease: it is simply forgotten, so
                # callers never see a "released" event for a frame still on screen.
                return None
            self.stats["released"] += 1
        if held.on_released is not None:
            try:
                held.on_released(held.frame, reason)
            except Exception:
                logger.exception("MetalPresentPort: on_released failed")
        return PresentOutcome(held.frame, "released", reason)

    def close_when_idle(self) -> Tuple[bool, List[PresentOutcome]]:
        """Stop submissions and close after the final GPU read completes.

        Poll on the owner thread. While a command still reads a source
        texture, its lease stays with the port.
        """
        with self._lock:
            self._closing = True
        outcomes = self._apply_completions()
        if self.in_flight:
            return False, outcomes
        outcomes.extend(self.shutdown())
        return True, outcomes

    def shutdown(self) -> List[PresentOutcome]:
        """Release everything the port still holds.  Owner thread, at teardown.

        Frames whose present has not completed are released as well.  Call it
        only after the GPU was confirmed idle
        (``MetalFrameTransactionPool.drain`` returned ``True``); otherwise a
        released texture may still be read by the pending present.
        """
        with self._lock:
            self._closing = True
        outcomes = self._apply_completions()
        with self._lock:
            waiting = [*self._pending, *self._displaced]
            self._pending.clear()
            self._displaced.clear()
            records = list(self._records.values())
            self._stalled = False
            self._in_flight_since = None
        for held in waiting + records:
            outcome = self._release(held, "shutdown")
            if outcome is not None:
                outcomes.append(outcome)
        close = getattr(self._sink, "close", None)
        if callable(close):
            close()
        return outcomes
