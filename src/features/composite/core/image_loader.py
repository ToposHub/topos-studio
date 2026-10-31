"""
Image Loader - Async image decoding for ImageInput nodes.

Decodes images on a worker thread to avoid blocking the GPU render thread.
The render thread only does texture upload from pre-decoded CPU arrays.

Architecture:
- ImageDecodeWorker: QThread that decodes images using cv2/OpenEXR
- ImageLoader: singleton managing workers, caching file mtime
- Flow: file_path change -> cancel old -> submit decode -> signal ready
  -> render thread uploads -> graph re-evaluates

States for each load request:
- MISSING: no file_path set
- LOADING: worker thread decoding
- READY_CPU: decoded numpy array available, awaiting GPU upload
- UPLOADED_GPU: texture on GPU, ready for rendering
- FAILED: decode error (bad file, unsupported format)
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex, QObject

logger = logging.getLogger(__name__)


# ── Load States ──

class ImageLoadState:
    MISSING = "missing"
    LOADING = "loading"
    READY_CPU = "ready_cpu"
    UPLOADED_GPU = "uploaded_gpu"
    FAILED = "failed"


@dataclass
class DecodedImage:
    """CPU-side decoded image data, ready for GPU upload."""
    file_path: str
    data: np.ndarray          # float32, RGB/RGBA, shape (h, w, c)
    width: int
    height: int
    components: int           # 1, 3, or 4
    mtime: float              # file modification time when decoded
    color_space_hint: str     # "srgb" or "linear" based on file extension
    bit_depth: str = ""       # "uint8", "uint16", "float16", "float32"
    has_alpha: bool = False   # True if source file contained an alpha channel
    # Deep compositing support
    depth_data: Optional[np.ndarray] = None  # float32, single channel Z data, shape (h, w)
    has_depth: bool = False   # True if source file contained a depth (Z) channel
    # A6: multi-layer EXR support
    exr_part: str = ""        # part name this image came from ("" = single part)
    exr_parts: list = field(default_factory=list)   # all part names in the file
    exr_channels: list = field(default_factory=list)  # channels in the selected part
    extra_channels: dict = field(default_factory=dict)  # name -> float32 (h, w)


# ── Decode Worker ──

class ImageDecodeWorker(QThread):
    """Worker thread that decodes a single image file.

    Emits decode_completed with DecodedImage on success,
    or decode_failed with (file_path, error_message) on failure.
    """
    decode_completed = pyqtSignal(object)  # DecodedImage
    decode_failed = pyqtSignal(str, str)   # file_path, error_message

    def __init__(
        self,
        file_path: str,
        request_id: int,
        parent=None,
        on_completed: Optional[Callable[[DecodedImage], None]] = None,
        on_failed: Optional[Callable[[str, str], None]] = None,
        exr_part: str = "",
        extra_channels: Optional[list] = None,
    ) -> None:
        super().__init__(parent)
        self.file_path = file_path
        self.request_id = request_id
        self.exr_part = exr_part or ""
        self.extra_channels = list(extra_channels) if extra_channels else None
        self._cancelled = False
        self._on_completed = on_completed
        self._on_failed = on_failed

    def cancel(self) -> None:
        """Mark this decode as cancelled."""
        self._cancelled = True

    def set_callbacks(self, on_completed=None, on_failed=None) -> None:
        """Attach delivery callbacks (call before start(), on the owner thread).

        The loader's closures capture ``self`` so deliveries can be matched
        against the active-worker identity.
        """
        self._on_completed = on_completed
        self._on_failed = on_failed

    def run(self) -> None:
        try:
            result = decode_image_file(
                self.file_path,
                exr_part=self.exr_part,
                extra_channels=self.extra_channels)
            if self._cancelled:
                return
            if result is not None:
                if self._on_completed is not None:
                    self._on_completed(result)
                self.decode_completed.emit(result)
            else:
                if self._on_failed is not None:
                    self._on_failed(self.file_path, "Failed to decode image")
                self.decode_failed.emit(self.file_path, "Failed to decode image")
        except Exception as e:
            if not self._cancelled:
                if self._on_failed is not None:
                    self._on_failed(self.file_path, str(e))
                self.decode_failed.emit(self.file_path, str(e))


def decode_image_file(file_path: str,
                      exr_part: str = "",
                      extra_channels: Optional[list] = None,
                      ) -> Optional[DecodedImage]:
    """Decode an image file to a float32 numpy array.

    Supported formats:
    - Standard via cv2: JPEG, PNG 8/16-bit, TIFF 8/16-bit, BMP, DPX
    - HDR via cv2: .hdr (Radiance HDR)
    - EXR via OpenEXR (float16 / float32) with optional alpha channel
    - 16-bit PNG / TIFF preserved with proper normalisation

    Returns DecodedImage or None if file cannot be read.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    mtime = os.path.getmtime(file_path)
    lower_path = file_path.lower()

    # ── EXR via OpenEXR (preferred) ──
    if lower_path.endswith(".exr"):
        result = _decode_exr(file_path, mtime, exr_part=exr_part,
                             extra_channels=extra_channels)
        if result is not None:
            return result
        # Fall through to cv2 fallback if OpenEXR fails

    # ── HDR via cv2 ──
    if lower_path.endswith(".hdr"):
        result = _decode_hdr(file_path, mtime)
        if result is not None:
            return result

    # ── General decode via cv2 ──
    img = cv2.imread(file_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None

    # Track source bit depth before normalisation
    src_bit_depth = _dtype_to_bit_depth(img.dtype)
    has_alpha = False

    # Convert BGR/BGRA to RGB/RGBA
    if img.ndim == 3:
        if img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
            has_alpha = True
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    elif img.ndim == 2:
        # Grayscale -> single channel with 3rd dim
        img = img[:, :, np.newaxis]

    # Normalise to float32
    img = _normalise_to_float32(img)

    h, w = img.shape[:2]
    components = img.shape[2] if img.ndim == 3 else 1

    # Infer colour space from extension
    if lower_path.endswith(_LINEAR_EXTENSIONS):
        color_space_hint = "linear"
    else:
        color_space_hint = "srgb"

    return DecodedImage(
        file_path=file_path,
        data=np.ascontiguousarray(img),
        width=w,
        height=h,
        components=components,
        mtime=mtime,
        color_space_hint=color_space_hint,
        bit_depth=src_bit_depth,
        has_alpha=has_alpha or components == 4,
    )


# ── EXR decode helpers ──

def probe_exr_header(file_path: str) -> Optional[dict]:
    """A6: enumerate EXR parts and channels without decoding pixels.

    Supports both binding generations (OpenEXR 2.x ``MultiPartInputFile``
    and 3.x ``File.parts``); single-part files come back as one part with
    an empty name.  Returns {"parts": [{name, channels}]}, or None when
    OpenEXR is unavailable / the file is not readable as EXR.
    """
    try:
        import OpenEXR
    except ImportError:
        return None
    try:
        parts = []
        if hasattr(OpenEXR, "MultiPartInputFile"):
            mp = OpenEXR.MultiPartInputFile(file_path)
            for i in range(mp.parts()):
                part = mp.part(i)
                header = part.header()
                parts.append({
                    "name": part.name() or f"part{i}",
                    "channels": sorted(header["channels"].keys()),
                    "type": str(part.type()),
                })
        elif hasattr(OpenEXR, "File"):
            # header_only=True keeps the probe allocation-free — opening a
            # multi-layer production EXR without it decodes EVERY part's
            # pixels into memory just to list channel names.
            f = OpenEXR.File(file_path, header_only=True)
            for pt in getattr(f, "parts", []):
                ch_attr = pt.header.get("channels", [])
                # 3.x: list of Channel objects carrying a .name
                names = sorted(
                    c.name if hasattr(c, "name") else str(c)
                    for c in ch_attr)
                parts.append({
                    "name": (pt.name() or "") if callable(pt.name) else str(pt.name or ""),
                    "channels": names,
                })
        else:
            header = OpenEXR.InputFile(file_path).header()
            parts.append({
                "name": "",
                "channels": sorted(header["channels"].keys()),
            })
        return {"parts": parts}
    except Exception as e:
        logger.warning(f"probe_exr_header failed for {file_path}: {e}")
        return None


def _decode_exr3_part(file_path: str, mtime: float,
                      exr_part: str = "",
                      extra_channels: Optional[list] = None,
                      ) -> Optional[DecodedImage]:
    """OpenEXR 3.x multi-part decode (bindings without MultiPartInputFile).

    The 2.x path in ``_decode_exr`` selects parts via MultiPartInputFile;
    on 3.x bindings that class does not exist and the deprecated InputFile
    silently reads part 0 — an ``exr_part`` selection would be ignored.
    Uses ``File(separate_channels=True)`` and reads the named part.
    Returns None on any failure (the caller falls back to the legacy path).
    """
    import OpenEXR

    def _val(obj, name, default=None):
        v = getattr(obj, name, default)
        return v() if callable(v) else v

    def _pname(part, i: int) -> str:
        return str(_val(part, "name") or f"part{i}")

    f = OpenEXR.File(file_path, separate_channels=True)
    parts = list(getattr(f, "parts", []))
    if not parts:
        return None
    names = [_pname(p, i) for i, p in enumerate(parts)]
    if exr_part and exr_part in names:
        idx = names.index(exr_part)
    else:
        if exr_part:
            logger.warning(
                f"EXR part {exr_part!r} not found in {file_path}; "
                "reading part 0")
        idx = 0
    part = parts[idx]
    # width/height are methods on some 3.x builds — resolve either form.
    width = int(_val(part, "width", 0) or 0)
    height = int(_val(part, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    channels = dict(_val(part, "channels") or {})  # name -> Channel

    def _pixels(name: str) -> Optional[np.ndarray]:
        ch = channels.get(name)
        if ch is None:
            return None
        try:
            return np.asarray(ch.pixels, dtype=np.float32)
        except Exception:
            return None

    has_alpha = "A" in channels
    request = ["R", "G", "B", "A"] if has_alpha else ["R", "G", "B"]
    # AUD-48008: luminance-only (Y-channel) EXRs keep the pixel data in "Y".
    # Broadcast it to any missing R/G/B so a greyscale matte decodes to grey
    # instead of the previous silent all-black zero fill.
    y_plane: Optional[np.ndarray] = None
    y_read = False
    arrays = []
    for name in request:
        px = _pixels(name)
        if px is None and name != "A" and "Y" in channels:
            if not y_read:
                y_plane = _pixels("Y")
                if y_plane is None:
                    logger.warning(
                        f"EXR part {names[idx]!r} in {file_path} declares a "
                        "Y channel but it could not be read; falling back to "
                        "zero fill.")
                y_read = True
            px = y_plane
        if px is None:
            px = (np.ones((height, width), np.float32) if name == "A"
                  else np.zeros((height, width), np.float32))
        arrays.append(px)
    img = np.ascontiguousarray(np.stack(arrays, axis=-1))

    depth_data = None
    has_depth = False
    for candidate in ["Z", "depth", "Depth", "Z.depth", "Z0"]:
        px = _pixels(candidate)
        if px is not None:
            depth_data = np.ascontiguousarray(px)
            has_depth = True
            break

    extra_out: dict = {}
    if extra_channels:
        for name in extra_channels:
            px = _pixels(name)
            if px is not None:
                extra_out[name] = np.ascontiguousarray(px)

    bit_depth = 32
    for ch in channels.values():
        dt = getattr(getattr(ch, "pixels", None), "dtype", None)
        if dt is not None and np.dtype(dt).itemsize == 2:
            bit_depth = 16
        break

    return DecodedImage(
        file_path=file_path,
        data=img,
        width=width,
        height=height,
        components=len(request),
        mtime=mtime,
        color_space_hint="linear",
        exr_part=names[idx] or "",
        exr_parts=names,
        exr_channels=sorted(channels.keys()),
        extra_channels=extra_out,
        bit_depth=bit_depth,
        has_alpha=has_alpha,
        depth_data=depth_data,
        has_depth=has_depth,
    )


def _decode_exr(file_path: str, mtime: float,
                exr_part: str = "",
                extra_channels: Optional[list] = None,
                ) -> Optional[DecodedImage]:
    """Decode an EXR file using OpenEXR if available.

    Supports float16 (HALF) and float32 (FLOAT) channels.
    Also reads depth channel (Z) for deep compositing support.
    A6: multi-part files honour ``exr_part``; named ``extra_channels``
    are lazily decoded into ``DecodedImage.extra_channels``.

    Returns None (and falls back to cv2) if OpenEXR is not installed.
    """
    try:
        import OpenEXR
        import Imath
    except ImportError:
        return None

    try:
        # 3.x bindings have no MultiPartInputFile — dispatch the named-part
        # selection to the 3.x reader before the 2.x-style legacy path can
        # silently ignore it (deprecated InputFile reads part 0).
        if exr_part and not hasattr(OpenEXR, "MultiPartInputFile"):
            result3 = _decode_exr3_part(
                file_path, mtime, exr_part=exr_part,
                extra_channels=extra_channels)
            if result3 is not None:
                return result3
            # fall through: legacy path logs + reads part 0 as a fallback

        part_names = []
        if exr_part and hasattr(OpenEXR, "MultiPartInputFile"):
            mp = OpenEXR.MultiPartInputFile(file_path)
            part_names = [mp.part(i).name() or f"part{i}"
                          for i in range(mp.parts())]
            if exr_part in part_names:
                exr_file = mp.part(part_names.index(exr_part))
            else:
                logger.warning(
                    f"EXR part {exr_part!r} not found in {file_path}; "
                    "reading part 0")
                exr_file = OpenEXR.InputFile(file_path)
        else:
            exr_file = OpenEXR.InputFile(file_path)
            if hasattr(OpenEXR, "MultiPartInputFile"):
                try:
                    mp = OpenEXR.MultiPartInputFile(file_path)
                    part_names = [mp.part(i).name() or f"part{i}"
                                  for i in range(mp.parts())]
                except Exception:
                    part_names = []
        header = exr_file.header()
        dw = header["dataWindow"]
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1

        # Determine available channels
        channels = list(header["channels"].keys())
        has_alpha = "A" in channels

        # Check for depth channel (common names: Z, depth, Depth, Z.depth)
        depth_channel_name = None
        for candidate in ["Z", "depth", "Depth", "Z.depth", "Z0"]:
            if candidate in channels:
                depth_channel_name = candidate
                break
        has_depth = depth_channel_name is not None

        # Pick channels to read
        if has_alpha:
            request = ["R", "G", "B", "A"]
            components = 4
        else:
            request = ["R", "G", "B"]
            components = 3

        # Determine pixel type from channel metadata
        ch_type = header["channels"].get("R", header["channels"].get("Y"))
        if ch_type is None:
            return None

        src_bit_depth = _openexr_type_to_bit_depth(ch_type)

        # Read channels; fall back to zero-filled if channel missing.
        # AUD-48008: luminance-only (Y-channel) EXRs keep the pixel data in
        # "Y".  Broadcast it to any missing R/G/B channel so a greyscale
        # matte decodes to grey instead of an all-black image.
        FLOAT = Imath.PixelType(Imath.PixelType.FLOAT)
        y_plane: Optional[np.ndarray] = None
        y_read = False
        arrays = []
        for ch_name in request:
            if ch_name in channels:
                raw = exr_file.channel(ch_name, FLOAT)
                arr = np.frombuffer(raw, dtype=np.float32).reshape(height, width)
            elif ch_name != "A" and "Y" in channels:
                if not y_read:
                    try:
                        raw_y = exr_file.channel("Y", FLOAT)
                        y_plane = np.frombuffer(
                            raw_y, dtype=np.float32).reshape(height, width)
                    except Exception as e:
                        logger.warning(
                            f"Failed to read Y channel of {file_path}: {e}")
                        y_plane = None
                    y_read = True
                arr = (y_plane if y_plane is not None
                       else np.zeros((height, width), dtype=np.float32))
            else:
                # Missing channel — fill with defaults
                if ch_name == "A":
                    arr = np.ones((height, width), dtype=np.float32)
                else:
                    arr = np.zeros((height, width), dtype=np.float32)
            arrays.append(arr)

        img = np.stack(arrays, axis=-1)  # (h, w, c)
        img = np.ascontiguousarray(img)

        # Read depth channel if available
        depth_data = None
        if has_depth and depth_channel_name:
            try:
                raw_depth = exr_file.channel(depth_channel_name, FLOAT)
                depth_data = np.frombuffer(raw_depth, dtype=np.float32).reshape(height, width)
                depth_data = np.ascontiguousarray(depth_data)
                # Handle common depth conventions:
                # - Infinite depth (very large values) -> clamp to valid range
                # - Some EXRs use 0 for "no depth" -> treat as far plane (1.0)
                # We normalize depth to 0.0-1.0 range if it's not already
                # (assuming 0 = closest, max = farthest)
                # For production use, users should normalize depth before export
            except Exception as e:
                logger.warning(f"Failed to read depth channel {depth_channel_name}: {e}")
                has_depth = False
                depth_data = None

        # A6: lazily decode requested extra channels (AOV passthrough)
        extra_out: dict = {}
        if extra_channels:
            for name in extra_channels:
                if name in channels:
                    try:
                        raw = exr_file.channel(name, FLOAT)
                        extra_out[name] = np.ascontiguousarray(
                            np.frombuffer(raw, dtype=np.float32).reshape(
                                height, width))
                    except Exception as e:
                        logger.warning(f"Failed to read extra channel {name}: {e}")

        part_name = getattr(exr_file, "name", lambda: "")()
        return DecodedImage(
            file_path=file_path,
            data=img,
            width=width,
            height=height,
            components=components,
            mtime=mtime,
            color_space_hint="linear",  # EXR is always scene-linear
            exr_part=part_name or "",
            exr_parts=list(part_names) if part_names else [],
            exr_channels=sorted(channels),
            extra_channels=extra_out,
            bit_depth=src_bit_depth,
            has_alpha=has_alpha,
            depth_data=depth_data,
            has_depth=has_depth,
        )

    except Exception as e:
        logger.warning(f"OpenEXR decode failed for {file_path}: {e}")
        return None


def _decode_hdr(file_path: str, mtime: float) -> Optional[DecodedImage]:
    """Decode an HDR (Radiance) file via cv2."""
    try:
        img = cv2.imread(file_path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYCOLOR)
        if img is None:
            return None

        # HDR files come back as float32 BGR
        src_bit_depth = "float32"
        has_alpha = False

        if img.ndim == 3 and img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        elif img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
            has_alpha = True
        elif img.ndim == 2:
            img = img[:, :, np.newaxis]

        h, w = img.shape[:2]
        components = img.shape[2] if img.ndim == 3 else 1

        return DecodedImage(
            file_path=file_path,
            data=np.ascontiguousarray(img.astype(np.float32)),
            width=w,
            height=h,
            components=components,
            mtime=mtime,
            color_space_hint="linear",
            bit_depth=src_bit_depth,
            has_alpha=has_alpha,
        )
    except Exception:
        return None


# ── Utility helpers ──

def _dtype_to_bit_depth(dtype: np.dtype) -> str:
    """Map numpy dtype to a human-readable bit-depth string."""
    if dtype == np.uint8:
        return "uint8"
    elif dtype == np.uint16:
        return "uint16"
    elif dtype == np.float16:
        return "float16"
    elif dtype == np.float32:
        return "float32"
    elif dtype == np.float64:
        return "float32"  # treat as float32 after conversion
    return ""


def _normalise_to_float32(img: np.ndarray) -> np.ndarray:
    """Normalise an image array to float32 in [0, 1] (for LDR) or native range (HDR)."""
    if img.dtype == np.uint8:
        return img.astype(np.float32) / 255.0
    elif img.dtype == np.uint16:
        return img.astype(np.float32) / 65535.0
    elif img.dtype == np.float16:
        return img.astype(np.float32)
    elif img.dtype == np.float64:
        return img.astype(np.float32)
    elif img.dtype == np.float32:
        return img
    else:
        return img.astype(np.float32)


def _openexr_type_to_bit_depth(channel_type) -> str:
    """Map an OpenEXR Imath.Channel pixel type to a bit-depth string."""
    try:
        import Imath
        ptype = channel_type.type
        if ptype == Imath.PixelType.HALF:
            return "float16"
        elif ptype == Imath.PixelType.FLOAT:
            return "float32"
        elif ptype == Imath.PixelType.UINT:
            return "uint32"
    except Exception:
        pass
    return ""


# Recognised extensions that imply linear / HDR pixel data
_LINEAR_EXTENSIONS = (".exr", ".hdr", ".dpx", ".sxr", ".mxr")


# ── Image Loader ──

class ImageLoader(QObject):
    """Manages async image decoding for ImageInput nodes.

    Thread safety: all public methods are safe to call from any thread.
    Results are delivered via on_decoded/on_failed callbacks (called from
    the worker thread), which should submit work to the render thread's
    command queue. Qt signals are also emitted but require an event loop.
    """

    def __init__(self, parent=None,
                 on_decoded=None, on_failed=None) -> None:
        super().__init__(parent)
        self._mutex = QMutex()
        self._active_workers: dict = {}  # node_id -> ImageDecodeWorker
        self._cancelled_workers: list = []  # Workers pending GC after cancel
        # AUD-48007: strong references for decode workers that outlive the
        # bounded wait in cancel_all().  Dropping the last Python reference
        # to a RUNNING QThread lets the cyclic GC destroy the C++ thread
        # object mid-run and abort the process, so timed-out workers are
        # parked here until they finish.  The list is pruned periodically
        # (see _prune_zombie_workers_unlocked); workers that never exit are
        # deliberately leaked to process teardown.
        self._zombie_workers: List[ImageDecodeWorker] = []
        self._request_counter = 0
        # Direct callbacks (called from worker thread, no event loop needed)
        self._on_decoded = on_decoded
        self._on_failed = on_failed

    def request_decode(self, node_id: str, file_path: str,
                       exr_part: str = "",
                       extra_channels: Optional[list] = None) -> None:
        """Request async image decode for a node.

        Cancels any in-progress decode for the same node_id.
        If file_path is empty, just cancels without starting a new decode.
        ``exr_part``/``extra_channels`` participate in the freshness check:
        a decode started under different EXR settings is never delivered.
        """
        self._mutex.lock()
        try:
            # AUD-48010: cancelled workers return early from run() and never
            # deliver, so without a sweep here the finished QThread objects
            # would linger in _cancelled_workers until some unrelated
            # delivery.  Prune on every request/cancel entry point.
            self._cleanup_finished_workers_unlocked()
            # Cancel existing worker for this node
            self._cancel_worker_unlocked(node_id)

            if not file_path:
                return

            self._request_counter += 1
            request_id = self._request_counter

            worker = ImageDecodeWorker(
                file_path,
                request_id,
                exr_part=exr_part,
                extra_channels=extra_channels,
            )
            # Identity-based freshness: the callback carries the worker so
            # delivery can be matched against the ACTIVE worker object —
            # comparing only file_path let a superseded part/channel
            # selection deliver stale pixels for the same file.
            worker.set_callbacks(
                on_completed=lambda img, nid=node_id, w=worker:
                    self._on_decode_completed(nid, img, w),
                on_failed=lambda path, err, nid=node_id, w=worker:
                    self._on_decode_failed(nid, path, err, w),
            )
            worker.setObjectName(f"ImageDecodeWorker-{request_id}")
            # Auto-cleanup when thread finishes
            worker.finished.connect(worker.deleteLater)

            self._active_workers[node_id] = worker
            worker.start()
        finally:
            self._mutex.unlock()

    def cancel_for_node(self, node_id: str) -> None:
        """Cancel any in-progress decode for a node."""
        self._mutex.lock()
        try:
            # AUD-48010: sweep finished cancelled workers (see request_decode).
            self._cleanup_finished_workers_unlocked()
            self._cancel_worker_unlocked(node_id)
        finally:
            self._mutex.unlock()

    def cancel_all(self) -> None:
        """Cancel all in-progress decodes and wait for threads to finish.

        AUD-48007: workers still running after the bounded wait are moved
        to ``_zombie_workers`` instead of losing their last reference —
        the GC destroying a running QThread aborts the process.  They stay
        strongly referenced until they exit (pruned on later cleanup
        passes; never-exiting workers leak intentionally to process
        teardown).
        """
        self._mutex.lock()
        try:
            for node_id in list(self._active_workers.keys()):
                self._cancel_worker_unlocked(node_id)
            # Collect all workers that need waiting
            to_wait = list(self._cancelled_workers)
        finally:
            self._mutex.unlock()

        # Wait outside mutex to avoid deadlock
        for w in to_wait:
            w.wait(2000)  # Max 2s per thread

        self._mutex.lock()
        try:
            timed_out = {w for w in to_wait if w.isRunning()}
            for w in timed_out:
                logger.warning(
                    "ImageDecodeWorker %s did not stop within 2000ms after "
                    "cancel; keeping a strong reference until it exits",
                    w.objectName())
            self._zombie_workers.extend(timed_out)
            # Keep only workers that are still running AND not already
            # parked in the zombie list; new cancels may have been appended
            # while the mutex was released during the wait.
            self._cancelled_workers = [
                w for w in self._cancelled_workers
                if w.isRunning() and w not in timed_out
            ]
            self._prune_zombie_workers_unlocked()
        finally:
            self._mutex.unlock()

    def _cancel_worker_unlocked(self, node_id: str) -> None:
        """Cancel and remove worker for node_id. Must hold mutex."""
        worker = self._active_workers.pop(node_id, None)
        if worker is not None:
            worker.cancel()
            # Keep reference so GC doesn't collect while still running
            self._cancelled_workers.append(worker)

    def _cleanup_finished_workers_unlocked(self) -> None:
        """Remove cancelled/zombie workers that have finished. Must hold mutex."""
        if self._cancelled_workers:
            self._cancelled_workers = [
                w for w in self._cancelled_workers if w.isRunning()
            ]
        self._prune_zombie_workers_unlocked()

    def _prune_zombie_workers_unlocked(self) -> None:
        """Drop keep-alive references for zombie workers that have exited.

        Called from every decode completion/failure and every cancel pass,
        so the keep-alive list cannot grow without bound while decoding
        continues.  Must hold mutex.
        """
        if self._zombie_workers:
            self._zombie_workers = [
                w for w in self._zombie_workers if w.isRunning()
            ]

    def _on_decode_completed(self, node_id: str, decoded: DecodedImage,
                             worker=None) -> None:
        """Handle completed decode from worker thread."""
        self._mutex.lock()
        active = self._active_workers.get(node_id)
        # Identity check: only the worker object that is STILL registered
        # may deliver. A superseded request (same file, different EXR
        # part/channel selection) is rejected here even when its run()
        # already passed the cancel flag; the part-0 fallback delivery of
        # the SAME worker still passes (worker is active).
        is_current = active is not None and (worker is None or active is worker)
        if is_current:
            self._active_workers.pop(node_id, None)
        self._cleanup_finished_workers_unlocked()
        self._mutex.unlock()

        if is_current and self._on_decoded:
            self._on_decoded(node_id, decoded)

    def _on_decode_failed(self, node_id: str, file_path: str, error: str,
                          worker=None) -> None:
        """Handle failed decode from worker thread."""
        self._mutex.lock()
        active = self._active_workers.get(node_id)
        is_current = active is not None and (worker is None or active is worker)
        if is_current:
            self._active_workers.pop(node_id, None)
        self._cleanup_finished_workers_unlocked()
        self._mutex.unlock()

        if is_current and self._on_failed:
            self._on_failed(node_id, file_path, error)
