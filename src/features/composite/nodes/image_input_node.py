"""
Image Input Node - Loads an image from disk into the composite graph.

Supports:
- Standard formats (JPEG, PNG 8-bit) via cv2
- HDR / high bit-depth: EXR (float16/float32), HDR, DPX via OpenEXR / cv2
- PNG / TIFF 16-bit via cv2 IMREAD_UNCHANGED
- Alpha channel passthrough (RGB → RGBA, or keep native channels)
- color_space parameter: auto / srgb / linear / rec709 / aces_cg / aces2065_1
- premultiplied flag: whether the image carries premultiplied alpha
- missing_policy: error (raise) / black (fallback) / checker (fallback)
- reload: force re-read from disk, mtime-based auto-reload
- frame_sequence: reserved interface for image-sequence playback

Async loading flow:
1. file_path changes -> ImageLoader starts async decode on worker thread
2. Decode completes -> DecodedImage delivered to render thread
3. Render thread calls upload_from_cpu() -> GPU texture created
4. _do_evaluate() returns the uploaded texture

For synchronous paths (export), upload_to_gpu() still works directly.
"""

import os
import queue
import threading
import logging
from typing import Any, Dict, Optional

import numpy as np
import moderngl

logger = logging.getLogger(__name__)

from .base_node import Node, Port, TextureResult, ParamDef
from src.features.composite.core.format import ImageFormat, BBox
from src.features.composite.core.color_space import ColorSpace
from src.features.composite.core.image_loader import ImageLoadState, DecodedImage, decode_image_file
from src.features.composite.core.frame_sequence import has_frame_pattern


# ── Constants ──

# Recognised extensions that imply linear / HDR pixel data
_LINEAR_EXTENSIONS = (".exr", ".hdr", ".dpx", ".sxr", ".mxr")

# Extensions that may carry 16-bit integer data
_16BIT_EXTENSIONS = (".png", ".tif", ".tiff")

# Missing-file fallback policies
class MissingPolicy:
    ERROR = "error"        # Mark node as FAILED
    BLACK = "black"        # Produce a small black texture
    CHECKER = "checker"    # Produce a small checkerboard texture


# AUD-6901: decoded sequence frames kept for instant back-scrub.  Each entry
# is a full-resolution CPU image, so the count is deliberately small.
_SEQ_RESULT_CACHE_FRAMES = 3


def _make_checkerboard(width: int = 64, height: int = 64,
                       block: int = 8) -> np.ndarray:
    """Generate a checkerboard RGBA float32 array for missing-file fallback."""
    h, w = height, width
    data = np.zeros((h, w, 4), dtype=np.float32)
    for y in range(h):
        for x in range(w):
            if ((x // block) + (y // block)) % 2 == 0:
                data[y, x] = [0.6, 0.6, 0.6, 1.0]
            else:
                data[y, x] = [0.3, 0.3, 0.3, 1.0]
    return np.ascontiguousarray(data)


def _make_black(width: int = 64, height: int = 64) -> np.ndarray:
    """Generate a solid black RGBA float32 array for missing-file fallback."""
    data = np.zeros((height, width, 4), dtype=np.float32)
    data[:, :, 3] = 1.0
    return np.ascontiguousarray(data)


class ImageInputNode(Node):
    """Loads an image file from disk.

    Parameters
    ----------
    file_path : str
        Absolute or relative path to the image file.
    color_space : str
        Colour-space tag for the loaded pixels.
        "auto" infers from file extension / bit-depth; user can override.
    premultiplied : bool
        True if the RGB channels are already multiplied by alpha.
    missing_policy : str
        What to do when the file cannot be found or decoded:
        "error"   – mark the node as FAILED
        "black"   – substitute a small black texture
        "checker" – substitute a small checkerboard texture
    raw_bit_depth : str
        Hint for decode: "auto", "uint8", "uint16", "float16", "float32".
        Only "auto" currently drives real behaviour; the rest are metadata
        for downstream pipeline decisions.
    frame_start / frame_end / frame_padding : reserved for image sequences.
    """

    node_type = "image_input"
    node_title = "Image Input"
    header_color = "#4a7fb5"  # Blue

    # Metadata
    SEARCH_ALIASES = ["image", "read", "load", "file", "input", "图片", "图像", "读取", "加载"]
    DESCRIPTION = "从磁盘加载图像文件"
    TAGS = ["input", "source", "image"]

    PARAM_DEFS = [
        ParamDef("file_path", "str", ""),
        # A6: multi-layer EXR support
        ParamDef("exr_part", "str", ""),
        ParamDef("exr_extra_channels", "str", ""),
        ParamDef("asset_id", "str", ""),  # P7.2: Asset-backed node support
        ParamDef("media_role", "option", "plate",
                 labels=["plate", "bg", "fg", "matte", "depth", "other"]),
        ParamDef("color_space", "option", "auto",
                 labels=["auto", "srgb", "linear", "rec709",
                         "aces_cg", "aces2065_1"]),
        ParamDef("premultiplied", "bool", False),
        ParamDef("missing_policy", "option", "error",
                 labels=["error", "black", "checker"]),
        # Optional source dimensions supplied by the Media Pool.  The image
        # loader still replaces these with the decoded dimensions at runtime;
        # keeping the metadata on the graph lets UI geometry and ROI planning
        # use the real source size before the first frame finishes loading.
        ParamDef("width", "int", 0, min_val=0, max_val=65536, hidden=True),
        ParamDef("height", "int", 0, min_val=0, max_val=65536, hidden=True),
        ParamDef("raw_bit_depth", "option", "auto",
                 labels=["auto", "uint8", "uint16", "float16", "float32"]),
        # Frame-sequence parameters (reserved — not yet consumed by loader)
        ParamDef("frame_start", "int", 1, min_val=1, max_val=10000000),
        ParamDef("frame_end", "int", 1, min_val=1, max_val=10000000),
        ParamDef("frame_padding", "int", 4, min_val=1, max_val=10),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.outputs = [
            Port("image", "output", "image")
        ]
        self.parameters = {
            "file_path": "",
            "exr_part": "",
            "exr_extra_channels": "",
            "asset_id": "",  # P7.2: Asset-backed node support
            "media_role": "plate",  # P7.2: Media role (plate/bg/fg/matte/depth/other)
            "color_space": "auto",
            "premultiplied": False,
            "missing_policy": "error",
            "width": 0,
            "height": 0,
            "raw_bit_depth": "auto",
            "frame_start": 1,
            "frame_end": 1,
            "frame_padding": 4,
        }
        self._gpu_texture = None  # moderngl.Texture
        self._gpu_depth_texture: Optional[moderngl.Texture] = None  # Depth texture (single channel)
        self._width: int = 0
        self._height: int = 0
        self._components: int = 0
        self._revision: int = 0  # bumped on each GPU upload
        self._is_fallback: bool = False  # True if current texture is a placeholder

        # Decoded image metadata (preserved even after CPU data is released)
        self._decoded_bit_depth: str = ""   # "uint8", "uint16", "float16", "float32"
        self._decoded_has_alpha: bool = False
        self._decoded_has_depth: bool = False  # True if source file had depth channel

        # Async loading state
        self._load_state: str = ImageLoadState.MISSING
        self._cpu_data: Optional[np.ndarray] = None
        self._cpu_width: int = 0
        self._cpu_height: int = 0
        self._cpu_components: int = 0
        self._cpu_mtime: float = 0.0
        self._cpu_color_space_hint: str = ""
        self._cpu_bit_depth: str = ""
        self._cpu_has_alpha: bool = False
        self._cpu_depth_data: Optional[np.ndarray] = None  # CPU-side depth data
        self._cpu_has_depth: bool = False
        self._last_uploaded_mtime: float = 0.0
        self._last_requested_exr_part: str = ""
        self._last_requested_exr_extra: list = []
        # Sequence (####) support: last frame this node actually loaded.
        self._last_sequence_frame: Optional[int] = None
        # AUD-6902: set when this node releases a self-owned GL texture so
        # the evaluator invalidates prior cache entries (stale glo).  Same
        # protocol as constant/particle/text/scene3d self-managed sources.
        self._cache_invalidated_needed: bool = False
        # AUD-6901: background decode for image sequences (#### / %04d).
        # The renderer's async ImageLoader cannot resolve pattern paths (it
        # stats the literal pattern, which is never a file), so sequence
        # playback used to fall into upload_to_gpu()'s synchronous
        # disk-decode on EVERY frame — hundreds of ms per 4K EXR, on the
        # render thread.  A single daemon worker per node decodes the
        # current (+next) frame off-thread; the render thread keeps serving
        # the previous frame's texture until the decoded result is picked
        # up by a later evaluation.
        self._seq_lock = threading.Lock()
        self._seq_jobs: "queue.Queue" = queue.Queue()
        self._seq_thread: Optional[threading.Thread] = None
        self._seq_shutdown: bool = False
        # key = (frame, pattern_path, exr_part, extra_channels) -> DecodedImage
        self._seq_results: Dict[tuple, DecodedImage] = {}
        # resolved path -> error message (sticky per source file)
        self._seq_failures: Dict[str, str] = {}
        self._seq_applied_key: Optional[tuple] = None

    # ── Load State ──

    @property
    def load_state(self) -> str:
        return self._load_state

    @property
    def is_ready(self) -> bool:
        """True if GPU texture is available for rendering."""
        return self._gpu_texture is not None and self._load_state == ImageLoadState.UPLOADED_GPU

    @property
    def is_fallback(self) -> bool:
        """True if the current texture is a placeholder (missing file)."""
        return self._is_fallback

    @property
    def decoded_bit_depth(self) -> str:
        """Bit-depth of the last successfully decoded image."""
        return self._decoded_bit_depth

    @property
    def decoded_has_alpha(self) -> bool:
        """Whether the last decoded image carried an alpha channel."""
        return self._decoded_has_alpha

    # ── Async decode result handlers ──

    def set_decoded_image(self, decoded: DecodedImage):
        """Store CPU-decoded image data from async loader.

        Called on the render thread after ImageLoader completes decode.
        Does NOT create GPU texture yet — that happens in upload_from_cpu().
        """
        self._cpu_data = decoded.data
        self._cpu_width = decoded.width
        self._cpu_height = decoded.height
        self._cpu_components = decoded.components
        self._cpu_mtime = decoded.mtime
        self._cpu_color_space_hint = decoded.color_space_hint
        # Preserve extended metadata if the loader provided it
        self._cpu_bit_depth = getattr(decoded, "bit_depth", "") or ""
        self._cpu_has_alpha = getattr(decoded, "has_alpha", False) or decoded.components == 4
        # Deep compositing support
        self._cpu_depth_data = getattr(decoded, "depth_data", None)
        self._cpu_has_depth = getattr(decoded, "has_depth", False) or self._cpu_depth_data is not None
        self._load_state = ImageLoadState.READY_CPU
        self._dirty = True

    def set_load_failed(self, error_message: str):
        """Mark the node as failed to load."""
        self._load_state = ImageLoadState.FAILED
        self.error_message = error_message
        self._cpu_data = None

    def set_load_loading(self):
        """Mark the node as loading (async decode in progress).

        Snapshots the requested EXR slice so the reload check compares
        request-against-request — a requested-but-missing part falls back
        at decode time and must not cause an endless re-decode loop.
        """
        self._load_state = ImageLoadState.LOADING
        self._last_requested_exr_part = str(
            self.parameters.get("exr_part", "") or "")
        extra_raw = str(self.parameters.get("exr_extra_channels", "") or "")
        self._last_requested_exr_extra = (
            [c.strip() for c in extra_raw.split(",") if c.strip()]
            if extra_raw else [])

    # ── GPU Upload ──

    def upload_from_cpu(self, ctx) -> bool:
        """Upload CPU-decoded data to GPU texture.

        Called on the render thread. Creates a GPU texture from the
        pre-decoded numpy array. Returns True if upload succeeded.
        """
        if self._cpu_data is None:
            return False

        try:
            # Decoders produce CPU arrays with a top-left image origin, while
            # the composite GL pipeline samples textures with bottom-left UV
            # origin. Flip once at the CPU->GL upload boundary.
            data = np.ascontiguousarray(np.flipud(self._cpu_data))
            w = self._cpu_width
            h = self._cpu_height
            components = self._cpu_components

            # Release old textures
            # AUD-6902: a released glo can be recycled by the GL driver for
            # a new texture while stale evaluator cache entries still hold
            # the old name — flag the release so the evaluator drops this
            # node's (and downstream) cached entries before storing the new
            # one (same protocol as ConstantNode / particle / text / scene3d).
            if self._gpu_texture is not None:
                self._gpu_texture.release()
                self._gpu_texture = None
                self._cache_invalidated_needed = True
            if self._gpu_depth_texture is not None:
                self._gpu_depth_texture.release()
                self._gpu_depth_texture = None
                self._cache_invalidated_needed = True

            self._gpu_texture = ctx.texture(
                (w, h), components,
                data=data.tobytes(),
                dtype='f4',
            )
            self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self._width = w
            self._height = h
            self._components = components

            # Upload depth texture if available
            if self._cpu_depth_data is not None:
                try:
                    depth_data = np.ascontiguousarray(np.flipud(self._cpu_depth_data))
                    # Create single-channel float texture for depth
                    self._gpu_depth_texture = ctx.texture(
                        (w, h), 1,
                        data=depth_data.tobytes(),
                        dtype='f4',
                    )
                    self._gpu_depth_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
                except Exception as e:
                    logger.warning(f"Depth texture upload error: {e}")
                    self._gpu_depth_texture = None
                    self._cpu_has_depth = False

            self._dirty = False
            self._revision += 1
            self._last_uploaded_mtime = self._cpu_mtime
            self._load_state = ImageLoadState.UPLOADED_GPU
            self._is_fallback = False

            # Persist metadata after successful upload
            self._decoded_bit_depth = self._cpu_bit_depth
            self._decoded_has_alpha = self._cpu_has_alpha
            self._decoded_has_depth = self._cpu_has_depth and self._gpu_depth_texture is not None

            # Release CPU data after successful upload
            self._cpu_data = None
            self._cpu_depth_data = None
            return True

        except Exception as e:
            logger.warning(f"GPU upload error: {e}")
            self._load_state = ImageLoadState.FAILED
            self.error_message = str(e)
            return False

    def needs_upload(self) -> bool:
        """Check if CPU data is waiting to be uploaded to GPU."""
        return self._load_state == ImageLoadState.READY_CPU and self._cpu_data is not None

    def needs_file_mtime_check(self, file_path: str) -> bool:
        """Check if the source changed since the last decode/upload.

        Besides the file mtime this covers A6 EXR selection drift: an
        exr_part / exr_extra_channels edit changes WHICH content the node
        should show without touching the file, so it must trigger a reload.
        """
        if not file_path or not os.path.isfile(file_path):
            return False
        # EXR slice drift — independent of the file's mtime
        part_raw = str(self.parameters.get("exr_part", "") or "")
        extra_raw = str(self.parameters.get("exr_extra_channels", "") or "")
        extra_list = ([c.strip() for c in extra_raw.split(",") if c.strip()]
                      if extra_raw else [])
        if (part_raw != self._last_requested_exr_part
                or extra_list != self._last_requested_exr_extra):
            return True
        try:
            current_mtime = os.path.getmtime(file_path)
            return current_mtime != self._last_uploaded_mtime
        except OSError:
            return False

    # ── Fallback texture ──

    def _upload_fallback(self, ctx) -> bool:
        """Create a placeholder texture based on missing_policy.

        Returns True if a placeholder was uploaded.
        """
        policy = self.parameters.get("missing_policy", MissingPolicy.ERROR)
        if policy == MissingPolicy.ERROR:
            return False

        size = 64
        if policy == MissingPolicy.BLACK:
            data = _make_black(size, size)
        elif policy == MissingPolicy.CHECKER:
            data = _make_checkerboard(size, size)
        else:
            return False

        # Release old texture (AUD-6902: flag stale-glo invalidation, see
        # upload_from_cpu).
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None
            self._cache_invalidated_needed = True

        self._gpu_texture = ctx.texture(
            (size, size), 4,
            data=data.tobytes(),
            dtype='f4',
        )
        self._gpu_texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._width = size
        self._height = size
        self._components = 4
        self._revision += 1
        self._load_state = ImageLoadState.UPLOADED_GPU
        self._is_fallback = True
        self._dirty = False
        self._decoded_bit_depth = ""
        self._decoded_has_alpha = True
        return True

    # ── Reload ──

    def reload(self):
        """Mark node as needing a full reload from disk.

        Resets the uploaded mtime so the next evaluate() will re-read
        the file even if the mtime has not changed.  Cached sequence-frame
        decodes (AUD-6901) are dropped too — a forced reload means the
        pixels on disk may have changed.
        """
        self._last_uploaded_mtime = 0.0
        self._dirty = True
        self._load_state = ImageLoadState.MISSING
        with self._seq_lock:
            self._seq_results.clear()
            self._seq_failures.clear()
            self._seq_applied_key = None

    # ── Frame Sequence ──

    @classmethod
    def params_require_frame_cache(cls, params) -> bool:
        """Sequence paths (``####`` / ``%04d``) are per-frame sources."""
        return has_frame_pattern(str(params.get("file_path", "") or ""))

    def resolve_frame_path(self, frame: int) -> str:
        """Resolve a frame number into a file path using sequence settings.

        Supports paths with a ``#`` placeholder pattern (e.g.
        ``/shots/comp/plate.####.exr``), printf ``%04d`` patterns, or a
        simple numeric suffix.  Wired into the synchronous evaluation path
        (export / Render-in-Place bake); the async interactive loader
        still serves single files.
        """
        path = self.parameters.get("file_path", "")
        if not path:
            return ""
        from src.features.composite.core.frame_sequence import (
            resolve_frame_pattern_path,
        )
        padding = self.parameters.get("frame_padding", 4)
        resolved = resolve_frame_pattern_path(path, frame,
                                              padding=int(padding))
        if resolved != path:
            return resolved
        return path

    # ── Async sequence decode (AUD-6901) ──

    def _sequence_request_key(self, frame: int) -> tuple:
        """Identity of one sequence decode request (frame + slice settings)."""
        path = str(self.parameters.get("file_path", "") or "")
        part = str(self.parameters.get("exr_part", "") or "")
        extra_raw = str(self.parameters.get("exr_extra_channels", "") or "")
        extra = (tuple(c.strip() for c in extra_raw.split(",") if c.strip())
                 if extra_raw else ())
        return (int(frame), path, part, extra)

    def _ensure_sequence_worker(self) -> None:
        """Lazily start the single daemon decode thread for this node."""
        thread = self._seq_thread
        if thread is not None and thread.is_alive():
            return
        self._seq_shutdown = False
        thread = threading.Thread(
            target=self._sequence_worker_loop,
            name=f"ImageSeqDecode-{self.id}",
            daemon=True)
        self._seq_thread = thread
        thread.start()

    def _sequence_worker_loop(self) -> None:
        """Decode queued sequence frames off the render thread.

        Pure CPU work (``decode_image_file``); results are handed to the
        render thread through the lock-guarded result map — the worker
        never touches GL objects or node GL state.
        """
        while not self._seq_shutdown:
            try:
                job = self._seq_jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            key, path, exr_part, extra = job
            try:
                decoded = decode_image_file(
                    path, exr_part=exr_part,
                    extra_channels=list(extra) if extra else None)
                error = "" if decoded is not None else f"Cannot decode: {path}"
            except Exception as exc:  # keep the worker alive on decoder bugs
                logger.warning("Sequence decode %s raised: %s", path, exc)
                decoded = None
                error = f"Decode error: {exc}"
            with self._seq_lock:
                if decoded is not None:
                    self._seq_results[key] = decoded
                    while len(self._seq_results) > _SEQ_RESULT_CACHE_FRAMES:
                        self._seq_results.pop(next(iter(self._seq_results)))
                else:
                    self._seq_failures[path] = error
                    self._seq_results.pop(key, None)

    def _shutdown_sequence_worker(self) -> None:
        """Stop the background decoder and drop cached CPU frames."""
        self._seq_shutdown = True
        try:
            self._seq_jobs.put_nowait(None)
        except queue.Full:  # pragma: no cover - unbounded queue
            pass
        with self._seq_lock:
            self._seq_results.clear()
            self._seq_failures.clear()
            self._seq_applied_key = None

    def _begin_sequence_frame(self, frame: int, render_mode: str) -> bool:
        """Start a background decode for ``frame``; False → use sync path.

        Called on the render thread when the sequence playhead moves.
        Returns True when the async path took over (a cached result exists,
        a decode was queued, or the frame is a known failure) — in that
        case the caller must NOT trigger the synchronous upload path; the
        previous frame's texture keeps rendering until the result arrives.
        """
        if str(render_mode) in ("export", "full"):
            # Exact-frame synchronous decode for export/bake correctness.
            return False
        if self._gpu_texture is None:
            # First paint: keep the one-time synchronous load (same as a
            # single-file source) so the node does not evaluate empty.
            return False
        key = self._sequence_request_key(frame)
        resolved = self.resolve_frame_path(int(frame))
        if not resolved or not os.path.isfile(resolved):
            # Missing frame file: let the sync path apply missing_policy.
            return False
        with self._seq_lock:
            if key in self._seq_results:
                return True  # cached — picked up right after this call
            if resolved in self._seq_failures:
                # Sticky failure for this exact file: keep the previous
                # texture; do not resubmit a full decode every evaluate.
                self.error_message = self._seq_failures[resolved]
                return True
        self._ensure_sequence_worker()
        self._seq_jobs.put((key, resolved, key[2], key[3]))
        # Prefetch the next frame (forward playback direction).
        next_key = self._sequence_request_key(frame + 1)
        next_resolved = self.resolve_frame_path(frame + 1)
        with self._seq_lock:
            have_next = (next_key in self._seq_results
                         or next_resolved in self._seq_failures)
        if (not have_next and next_resolved
                and os.path.isfile(next_resolved)):
            self._seq_jobs.put((next_key, next_resolved,
                                next_key[2], next_key[3]))
        return True

    def _apply_sequence_result(self, frame: int) -> bool:
        """Apply a completed background decode for ``frame`` (if any).

        Returns True when fresh CPU data was staged via set_decoded_image
        (READY_CPU) — the caller should upload it in the same evaluation.
        """
        key = self._sequence_request_key(frame)
        with self._seq_lock:
            if self._seq_applied_key == key:
                return False
            decoded = self._seq_results.get(key)
        if decoded is None:
            return False
        self.set_decoded_image(decoded)
        with self._seq_lock:
            self._seq_applied_key = key
        return True

    # ── Synchronous Path (legacy / export) ──

    def upload_to_gpu(self, ctx, frame=None):
        """Synchronous load + upload. Used by export path and legacy flow.

        Blocks the calling thread for disk I/O and image decode.
        Prefer upload_from_cpu() for interactive use.  ``frame`` resolves
        sequence patterns (``####`` / ``%04d``) in ``file_path``.
        """
        path = self.parameters.get("file_path", "")
        if not path:
            return
        if frame is not None:
            path = self.resolve_frame_path(int(frame))

        exr_part = str(self.parameters.get("exr_part", "") or "")
        extra_raw = str(self.parameters.get("exr_extra_channels", "") or "")
        extra_list = [c.strip() for c in extra_raw.split(",")
                      if c.strip()] if extra_raw else None
        self._last_requested_exr_part = exr_part
        self._last_requested_exr_extra = list(extra_list or [])
        decoded = decode_image_file(path, exr_part=exr_part,
                                    extra_channels=extra_list)
        if decoded is None:
            # Try fallback
            if not self._upload_fallback(ctx):
                self._load_state = ImageLoadState.FAILED
                self.error_message = f"Cannot decode: {path}"
            return

        self.set_decoded_image(decoded)
        self.upload_from_cpu(ctx)

    # ── Evaluation ──

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:
        # Unwrap CompositeEvalContext to get the actual ModernGL context.
        # Source nodes receive CompositeEvalContext for business-level context
        # (runtime_context, timeline_clip_gpu_provider), but GL operations
        # like ctx.texture() need the real moderngl.Context.
        from src.features.composite.core.eval_context import CompositeEvalContext
        frame_number = None
        if isinstance(ctx, CompositeEvalContext):
            frame_number = getattr(ctx, "frame_number", None)
            # Unwrap CompositeEvalContext to get the actual ModernGL context.
            # Source nodes receive CompositeEvalContext for business-level context
            # (runtime_context, timeline_clip_gpu_provider), but GL operations
            # like ctx.texture() need the real moderngl.Context.
            ctx = ctx.gl

        # Upload pending CPU data if available
        if self.needs_upload():
            self.upload_from_cpu(ctx)

        # Sequence sources: a changed frame number means changed content.
        if frame_number is not None:
            path = str(self.parameters.get("file_path", "") or "")
            if has_frame_pattern(path):
                try:
                    frame_number = int(frame_number)
                except (TypeError, ValueError):
                    frame_number = None
                if frame_number is not None:
                    if self._last_sequence_frame != frame_number:
                        self._last_sequence_frame = frame_number
                        # AUD-6901: preview playback decodes sequence frames
                        # on the background worker and keeps serving the
                        # previous texture until the result lands.  Export /
                        # full renders (and first paint) still use the exact
                        # synchronous path below.
                        if not self._begin_sequence_frame(
                                frame_number, render_mode):
                            self.reload()
                            self._load_state = ImageLoadState.MISSING
                    # Pick up a decode that completed since this frame was
                    # first requested (also re-applies cached frames on
                    # back-scrub without a re-decode).
                    if self._apply_sequence_result(frame_number):
                        if self.needs_upload():
                            self.upload_from_cpu(ctx)

        if self._dirty and self._load_state != ImageLoadState.READY_CPU:
            # Legacy sync path: only if no async loader is driving us
            if self._load_state not in (ImageLoadState.LOADING, ImageLoadState.READY_CPU):
                # AUD-6900: any parameter edit marks the node dirty, but
                # most edits (premultiplied, frame_padding, …) cannot change
                # the decoded pixels — the previous code re-ran a full
                # synchronous disk read + decode on the render thread for
                # every parameter tweak. needs_file_mtime_check() already
                # encodes the decode-relevant inputs (resolved path, EXR
                # slice, file mtime); when a texture is live and none of
                # them moved, keep it and just clear the dirty flag.
                # Failure states keep the old retry behaviour.
                check_path = (self.resolve_frame_path(int(frame_number))
                              if frame_number is not None
                              else str(self.parameters.get("file_path",
                                                           "") or ""))
                content_unchanged = (
                    self._gpu_texture is not None
                    and self._load_state == ImageLoadState.UPLOADED_GPU
                    and bool(check_path)
                    and os.path.isfile(check_path)
                    and not self.needs_file_mtime_check(check_path))
                if content_unchanged:
                    self._dirty = False
                else:
                    self.upload_to_gpu(ctx, frame=frame_number)

        # If we still have no texture, try fallback (for missing files)
        if self._gpu_texture is None:
            if not self._upload_fallback(ctx):
                return {}

        if self._gpu_texture is None:
            return {}

        fmt = ImageFormat(
            width=self._width,
            height=self._height,
            name="Image Input",
        ) if self._width > 0 and self._height > 0 else None
        bbox = BBox.full_frame(self._width, self._height) if fmt else None

        premultiplied = self.parameters.get("premultiplied", False)

        # Check if we have valid depth texture
        has_depth = self._decoded_has_depth and self._gpu_depth_texture is not None
        depth_texture_id = self._gpu_depth_texture.glo if has_depth else None

        return {
            "image": TextureResult(
                texture_id=self._gpu_texture.glo,
                width=self._width,
                height=self._height,
                revision=self._revision,
                format=fmt,
                bbox=bbox,
                alpha_premultiplied=premultiplied,
                color_space=self._detect_color_space(),
                # Deep compositing support
                depth_texture_id=depth_texture_id,
                has_deep_data=has_depth,
            )
        }

    def cleanup(self) -> None:
        """Release GPU resources."""
        if self._gpu_texture is not None:
            self._gpu_texture.release()
            self._gpu_texture = None
        if self._gpu_depth_texture is not None:
            self._gpu_depth_texture.release()
            self._gpu_depth_texture = None
        self._cpu_data = None
        self._cpu_depth_data = None
        self._load_state = ImageLoadState.MISSING
        self._is_fallback = False
        self._decoded_has_depth = False
        # AUD-6901: stop the background sequence decoder (daemon thread,
        # no join — the render thread must not block on teardown).
        self._shutdown_sequence_worker()

    # ── Color-space detection ──

    def _detect_color_space(self) -> str:
        """Infer colour space from parameters, decode hint, or file extension.

        Priority:
        1. Explicit user override (color_space != "auto")
        2. Hint from decoded image metadata
        3. File extension heuristic
        4. Fallback to sRGB
        """
        override = self.parameters.get("color_space", "auto")
        if override and override != "auto":
            return override

        # Use hint from decoded image if available
        if self._cpu_color_space_hint:
            return self._cpu_color_space_hint

        path = self.parameters.get("file_path", "").lower()
        if path.endswith(_LINEAR_EXTENSIONS):
            return ColorSpace.LINEAR
        return ColorSpace.SRGB

    # ── Serialization ──

    def to_dict(self) -> dict:
        """Serialize node to dict for save/load."""
        d = super().to_dict()
        # Store extended metadata so downstream can inspect without re-decode
        meta = d.get("_meta") if isinstance(d.get("_meta"), dict) else {}
        meta.update({
            "bit_depth": self._decoded_bit_depth,
            "has_alpha": self._decoded_has_alpha,
            "is_fallback": self._is_fallback,
            "has_depth": self._decoded_has_depth,
        })
        d["_meta"] = meta
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ImageInputNode":
        """Deserialize node from dict."""
        node = super().from_dict(data)
        meta = data.get("_meta", {})
        node._decoded_bit_depth = meta.get("bit_depth", "")
        node._decoded_has_alpha = meta.get("has_alpha", False)
        node._is_fallback = meta.get("is_fallback", False)
        node._decoded_has_depth = meta.get("has_depth", False)
        return node
