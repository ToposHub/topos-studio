"""Color-transform provider — pluggable colour-management backend.

Provides a strategy interface (``ColorTransformProvider``) so the composite
engine can run colour conversions through either:

- ``OCIOColorProvider`` — OpenColorIO processors (the default / required
  engine). Enables arbitrary IDT/ODT transforms defined in an OCIO config,
  accurate transfer functions, and (when the config defines them) gamut
  transforms. Drives the structured ``transform(img, src, dst)`` API.
- ``BuiltinColorProvider`` — built-in numpy math (sRGB↔linear, Rec.709↔linear,
  full↔limited range). Zero external dependencies; used as a graceful
  fallback when OCIO is unavailable (e.g. during development).

OCIO is a REQUIRED dependency (see requirements.txt). ``get_color_provider``
defaults to OCIO and falls back to builtin only if OCIO is missing.

Two APIs:
  - ``transform(img, src, dst)`` — structured ColorSpaceDescriptor (new,
    handles transfer + range + gamut). Preferred.
  - ``export_encode(img, src_str, dst_str)`` — legacy string API
    (sRGB/linear only). Retained for backward compat.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from functools import lru_cache
from typing import Optional, Protocol, runtime_checkable

import numpy as np

from src.shared.color.types import ColorPrimaries, ColorRange, ColorTransfer, is_hdr_transfer

logger = logging.getLogger(__name__)


# ── Colour-space name normalisation ────────────────────────────────────────
# Map the various strings used across the codebase to canonical tokens.
# BuiltinProvider only understands "linear" and "srgb"; OCIO understands
# any name defined in the loaded config.
_BUILTIN_ALIASES = {
    "srgb": "srgb",
    "srgb_tx": "srgb",
    "display": "srgb",
    "linear": "linear",
    "scene-linear": "linear",
    "lin": "linear",
    # ACEScg (AP1 primaries, linear transfer, ACES ~D60 white)
    "acescg": "acescg",
    "aces_cg": "acescg",
    "acescg_scene": "acescg",
    "ap1_linear": "acescg",
}


def _normalise_space(name: Optional[str]) -> str:
    """Normalise a colour-space name to lowercase canonical form."""
    if not name:
        return "linear"
    return _BUILTIN_ALIASES.get(name.lower(), name.lower())


def _ocio_space_name(name: Optional[str]) -> str:
    """Map a canonical space name to the OCIO config's space name.

    The builtin provider and codebase call the working space "linear", but the
    bundled minimal.ocio names it "lin". Without this mapping, OCIO processor
    lookups for src/dst "linear" silently fail (the space doesn't exist) and
    the image is returned unchanged — masking OCIO as a no-op.
    """
    canonical = _normalise_space(name)
    if canonical in ("linear", "scene-linear"):
        return "lin"
    return canonical


# ── M19（R01）：typed 转换结果 —— 不冒充转换 ──────────────────────────────
#
# 背景：OCIO provider 的 processor 缺失/执行失败一律返回原输入（调用方
# 无从区分"转换完成"与"根本没转"）。typed API 把结果状态显式化，交付
# 预检据此拒绝或明示降级；legacy transform 的静默回退也进失败记账，
# 不再不可见。

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.shared.color.color_space_descriptor import ColorSpaceDescriptor


#: 结果状态 ∈ converted（确实转换）/ identity_src_eq_dst（本就无需转换，
#: 如 range/transfer/primaries 全同）/ provider_unavailable /
#: processor_missing / apply_failed / invalid_input_shape
TRANSFORM_OK_STATUSES = ("converted", "identity_src_eq_dst")


@dataclass(frozen=True)
class TransformResult:
    """色彩转换的类型化结果（M19）。

    ``image`` 始终可用（失败时为原输入——供降级显示），但 ``ok=False``
    时**不得**把 image 当作已转换数据进入交付。
    """

    image: np.ndarray
    status: str
    detail: str = ""
    provider: str = ""
    src_desc: str = ""
    dst_desc: str = ""

    @property
    def ok(self) -> bool:
        return self.status in TRANSFORM_OK_STATUSES


# 失败记账（模块级；legacy transform/export_encode 的静默回退在此可见）
_transform_failures: dict = {}
_transform_failures_lock = threading.Lock()


def _record_transform_failure(status: str, provider: str, src, dst) -> None:
    with _transform_failures_lock:
        key = (status, provider, str(src), str(dst))
        _transform_failures[key] = _transform_failures.get(key, 0) + 1


def last_transform_failures() -> dict:
    """最近进程内转换失败记账 {(status, provider, src, dst): count}。

    交付预检/UI 据此呈现"哪些转换实际没转"——静默恒等降级不再不可见。
    """
    with _transform_failures_lock:
        return dict(_transform_failures)


def reset_transform_failure_accounting() -> None:
    with _transform_failures_lock:
        _transform_failures.clear()


# ── Provider interface ────────────────────────────────────────────────────

@runtime_checkable
class ColorTransformProvider(Protocol):
    """Pluggable colour-transform backend.

    Implementations convert RGBA float images between colour spaces. Two
    methods:
      - ``transform(img, src, dst)`` — the structured-descriptor API (new).
        Converts between ``ColorSpaceDescriptor`` spaces, handling transfer
        function, primaries, and range. Preferred for new code.
      - ``export_encode(img, src_str, dst_str)`` — the legacy string API
        (sRGB/linear only). Retained for backward compat; thin wrapper.
    """

    @property
    def name(self) -> str:
        """Provider identifier ('builtin' / 'ocio')."""
        ...

    def is_available(self) -> bool:
        """Whether this provider is actually usable right now.

        Built-in is always available. OCIO is available only when
        PyOpenColorIO imports AND a config is loaded.
        """
        ...

    def transform(self, img: np.ndarray,
                  src: "ColorSpaceDescriptor",
                  dst: "ColorSpaceDescriptor") -> np.ndarray:
        """Convert an image between two structured color spaces.

        Handles transfer function (sRGB/BT.709/linear), range
        (full/limited), and (for the OCIO provider) primaries/gamut. Works
        on (h, w, 3) or (h, w, 4) float32 in [0,1]. If src == dst, returns
        the input unchanged. Values are clamped to [0,1] for non-linear
        outputs (the caller handles bit-depth).

        Args:
            img: input image (BGR or RGB float32; channel order is
                preserved — only the first 3 channels are transformed).
            src: source ColorSpaceDescriptor.
            dst: destination ColorSpaceDescriptor.

        Returns:
            Converted image (same shape/dtype).
        """
        ...

    def export_encode(self, rgba: np.ndarray, src_space: str,
                      dst_space: str) -> np.ndarray:
        """Convert an RGBA float32 image from ``src_space`` to ``dst_space``.

        Legacy string-based API (sRGB/linear only). Used by file_output's
        export path. Operates on a (h, w, 4) float32 array. New code should
        use ``transform`` with ColorSpaceDescriptor.
        """
        ...


# ── Built-in provider (sRGB ↔ linear numpy math) ──────────────────────────

def _srgb_to_linear_np(rgba: np.ndarray) -> np.ndarray:
    """sRGB → linear, IEC 61966-2-1 EOTF (inverse).

    Matches the GPU cs_transform shader: ``((c + 0.055) / 1.055) ^ 2.4``.
    (file_output's legacy numpy path previously omitted the ``+ 0.055``,
    producing slightly-too-dark exports; the provider uses the correct
    formula, aligning export with the live preview's IDT/ODT.)

    Only RGB channels are converted; alpha is passed through unchanged
    (alpha is a linear opacity value, not a colour).
    """
    rgb = rgba[..., :3]
    mask = rgb <= 0.04045
    converted = np.where(
        mask,
        rgb / 12.92,
        np.power(np.maximum(rgb + 0.055, 0.0) / 1.055, 2.4),
    )
    out = rgba.copy()
    out[..., :3] = converted
    return out


def _linear_to_srgb_np(rgba: np.ndarray) -> np.ndarray:
    """Linear → sRGB, IEC 61966-2-1 OETF (forward).

    Matches the GPU cs_transform shader: ``1.055 * c^(1/2.4) - 0.055``.
    Only RGB channels are converted; alpha is passed through unchanged.
    """
    rgb = rgba[..., :3]
    mask = rgb <= 0.0031308
    converted = np.where(
        mask,
        12.92 * rgb,
        1.055 * np.power(np.maximum(rgb, 0.0), 1.0 / 2.4) - 0.055,
    )
    out = rgba.copy()
    out[..., :3] = converted
    return out
#: F5.1 公开别名：显示编码单点（shared/color/display_encode.py）直接使用
#: 传输函数本体而非完整 transform 管线——同 primaries/range 时管线即纯
#: transfer；且免受 provider 选择/降级影响（OCIO 路径对非 (h,w,c) 形状会
#: 异常后静默回退为恒等，显示编码不允许该降级，见 display_encode 合同测试）。
linear_to_srgb_np = _linear_to_srgb_np


# ── Rec.709 transfer functions (BT.709 OETF / BT.1886 EOTF) ──────────────
#
# BT.709 camera OETF (scene light → signal):
#   E = 4.5 * L            for L < 0.018
#   E = 1.099 * L^0.45 - 0.099   otherwise
# The matching display EOTF (BT.1886) is approximated by the inverse power
# 2.4 on the high segment. For video compositing these are close enough to
# the sRGB curve that the difference is subtle but real (BT.709 has slightly
# higher effective contrast), and crucially they let us tag video exports
# with the correct transfer instead of forcing sRGB.

def _bt709_to_linear_np(rgba: np.ndarray) -> np.ndarray:
    """Rec.709-encoded → linear (inverse of the BT.709 OETF).

    Inverse of ``_linear_to_bt709_np``: for E >= 0.081, L = ((E + 0.099)/1.099)^(1/0.45);
    for E < 0.081, L = E / 4.5. This is the OETF inverse used by the IDT so
    that encode→decode round-trips exactly. (BT.1886 display EOTF is a
    separate curve; the working-space decode uses the OETF inverse to stay
    reversible.) Only RGB channels converted.
    """
    rgb = rgba[..., :3]
    mask = rgb < 0.081  # inverse of the 0.018 OETF knee
    converted = np.where(
        mask,
        rgb / 4.5,
        np.power(np.maximum(rgb + 0.099, 0.0) / 1.099, 1.0 / 0.45),
    )
    out = rgba.copy()
    out[..., :3] = converted
    return out


def _linear_to_bt709_np(rgba: np.ndarray) -> np.ndarray:
    """Linear → Rec.709-encoded (BT.709 OETF forward).

    E = 4.5 * L            for L < 0.018
    E = 1.099 * L^0.45 - 0.099   otherwise
    Only RGB channels converted.
    """
    rgb = rgba[..., :3]
    mask = rgb < 0.018
    converted = np.where(
        mask,
        4.5 * rgb,
        1.099 * np.power(np.maximum(rgb, 0.0), 0.45) - 0.099,
    )
    out = rgba.copy()
    out[..., :3] = converted
    return out


def _signed_power_rgb(rgba: np.ndarray, exponent: float) -> np.ndarray:
    """Apply a power law to RGB, preserving negative excursions and alpha."""
    out = rgba.copy()
    rgb = rgba[..., :3]
    out[..., :3] = np.copysign(np.power(np.abs(rgb), exponent), rgb)
    return out


# ── Range conversion (full ↔ limited) ────────────────────────────────────
#
# Video pixels use limited ("legal", "TV") range: Y in 16-235, C in 16-240
# (for 8-bit). Still images use full ("PC", "JPEG") range 0-255. When
# converting between linear working space (always full/normalized [0,1])
# and a limited-range target, we must compress/expand around the 16/235
# boundaries. Applied around the transfer-function step in numpy.

_LIMITED_BLACK = 16.0 / 255.0   # 0.0627
_LIMITED_WHITE = 235.0 / 255.0  # 0.9216
_LIMITED_SCALE = 255.0 / (235.0 - 16.0)  # ~1.164


def _full_to_limited_np(rgba: np.ndarray) -> np.ndarray:
    """Compress full-range [0,1] RGB to limited-range [16/255, 235/255].

    Applied to RGB only (luma-equivalent scaling); alpha passes through.
    """
    rgb = rgba[..., :3]
    converted = rgb / _LIMITED_SCALE + _LIMITED_BLACK
    out = rgba.copy()
    out[..., :3] = converted
    return out


def _limited_to_full_np(rgba: np.ndarray) -> np.ndarray:
    """Expand limited-range RGB to full-range [0,1]. Inverse of full→limited."""
    rgb = rgba[..., :3]
    converted = (rgb - _LIMITED_BLACK) * _LIMITED_SCALE
    out = rgba.copy()
    out[..., :3] = np.clip(converted, 0.0, 1.0)
    return out


# ── HDR transfer functions (PQ / HLG) ────────────────────────────────────
#
# Stage 2: PQ (SMPTE ST 2084, HDR10) and HLG (ARIB STD-B67) transfer
# functions. Math ported from color_decode.py ColorConverter (constants at
# color_decode.py:166-176). PQ works in absolute nits (0–10000); the linear
# result of pq_to_linear is in nits, so HDR working space is scene-linear
# but NOT normalized to [0,1] — values can exceed 1.0.

# PQ constants (SMPTE ST 2084). These are the canonical published constants
# (NOT the typo'd values in color_decode.py, which produce ~0 codes). The
# forward EOTF is E' = ((c1 + c2*V^m1) / (1 + c3*V^m1))^m2 with V = L/10000.
_PQ_M1 = 0.1593017578125
_PQ_M2 = 78.84375
_PQ_C1 = 0.8359375
_PQ_C2 = 18.8515625
_PQ_C3 = 18.6875
_PQ_LMAX = 10000.0  # peak luminance (nits)

# HLG constants (ARIB STD-B67)
_HLG_A = 0.17883277
_HLG_B = 0.28466892
_HLG_C = 0.55991073


def _pq_to_linear_np(rgba: np.ndarray) -> np.ndarray:
    """PQ-encoded → linear (nits). SMPTE ST 2084 inverse EOTF.

    Returns linear light in nits (0–10000). Values can exceed 1.0 — HDR
    working space is not normalized. Only RGB channels converted.
    """
    rgb = np.clip(np.nan_to_num(rgba[..., :3], nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
    v = np.power(rgb, 1.0 / _PQ_M2)
    numerator = np.maximum(v - _PQ_C1, 0.0)
    denominator = np.maximum(_PQ_C2 - _PQ_C3 * v, 1e-10)
    linear = np.power(numerator / denominator, 1.0 / _PQ_M1) * _PQ_LMAX
    out = rgba.copy()
    out[..., :3] = linear
    return out


def _linear_to_pq_np(rgba: np.ndarray) -> np.ndarray:
    """Linear (nits) → PQ-encoded. SMPTE ST 2084 forward EOTF.

    Input is linear nits (0–10000); output is PQ codes [0,1]. Only RGB
    channels converted.
    """
    rgb = np.clip(rgba[..., :3] / _PQ_LMAX, 0.0, 1.0)
    v = np.power(rgb, _PQ_M1)
    numerator = _PQ_C1 + _PQ_C2 * v
    denominator = 1.0 + _PQ_C3 * v
    pq = np.power(numerator / denominator, _PQ_M2)
    out = rgba.copy()
    out[..., :3] = pq
    return out


def _hlg_to_linear_np(rgba: np.ndarray) -> np.ndarray:
    """HLG-encoded → linear (relative). ARIB STD-B67 inverse OETF.

    For E' <= 0.5: L = E'^2 / 3; for E' > 0.5: L = (exp((E'-C)/A) + B) / 12.
    Result is relative scene light (HLG is scene-referred); multiply by a
    display luminance later for absolute nits. Only RGB channels converted.
    """
    rgb = np.clip(np.nan_to_num(rgba[..., :3], nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
    low = rgb <= 0.5
    converted = np.where(
        low,
        np.square(rgb) / 3.0,
        (np.exp((rgb - _HLG_C) / _HLG_A) + _HLG_B) / 12.0,
    )
    out = rgba.copy()
    out[..., :3] = converted
    return out


def _linear_to_hlg_np(rgba: np.ndarray) -> np.ndarray:
    """Linear (relative) → HLG-encoded. ARIB STD-B67 forward OETF.

    For L <= 1/12: E' = sqrt(3*L); for L > 1/12: E' = A*ln(12*L - B) + C.
    Only RGB channels converted.
    """
    rgb = np.clip(rgba[..., :3], 0.0, None)
    low = rgb <= 1.0 / 12.0
    converted = np.where(
        low,
        np.sqrt(3.0 * rgb),
        _HLG_A * np.log(np.maximum(12.0 * rgb - _HLG_B, 1e-10)) + _HLG_C,
    )
    out = rgba.copy()
    out[..., :3] = np.clip(converted, 0.0, 1.0)
    return out


# ── HDR tone mapping (preview: HDR → SDR on a normal display) ────────────


def _tonemap_reinhard_np(rgba: np.ndarray, target_peak: float = 100.0) -> np.ndarray:
    """Reinhard tone map HDR linear (nits) → SDR linear [0,1].

    Used for HDR preview on an SDR display: L_out = (L/target) / (1 + L/target).
    ``target_peak`` is the SDR display peak in nits (default 100). Only RGB.
    """
    rgb = rgba[..., :3]
    normalized = rgb / target_peak
    mapped = normalized / (1.0 + normalized)
    out = rgba.copy()
    out[..., :3] = mapped
    return out


def _tonemap_aces_np(rgba: np.ndarray, target_peak: float = 100.0) -> np.ndarray:
    """Approximate ACES tonemap for HDR → SDR preview.

    A simplified Narkowicz ACES approximation: good highlight rolloff with
    less desaturation than Reinhard in the midtones. ``target_peak`` scales
    the input. Only RGB.
    """
    rgb = rgba[..., :3] / target_peak
    a = 2.51
    b = 0.03
    c = 2.43
    d = 0.59
    e = 0.14
    mapped = np.clip((rgb * (a * rgb + b)) / (rgb * (c * rgb + d) + e), 0.0, 1.0)
    out = rgba.copy()
    out[..., :3] = mapped
    return out


# ── Gamut (primaries) conversion matrices ────────────────────────────────
#
# 3×3 matrices converting linear RGB between primaries, derived from the
# standard CIE chromaticities via the standard "RGB→XYZ→RGB" derivation
# (luminance-normalized). These are the ITU/standard values, not Bradford-
# adapted approximations — accurate for BT.709 / BT.2020 / DCI-P3 (D65).
#
# Each matrix M maps M @ rgb_src → rgb_dst (linear, D65 white). We compose
# via the BT.709 reference (working space) so we only need to/from BT.709.

# BT.709 → BT.2020 (linear RGB)
_BT709_TO_BT2020 = np.array([
    [0.6274040, 0.3292820, 0.0433136],
    [0.0690970, 0.9195400, 0.0113612],
    [0.0163916, 0.0880132, 0.8955950],
], dtype=np.float32)

# BT.709 → DCI-P3 D65 (linear RGB)
_BT709_TO_P3D65 = np.array([
    [0.8224620, 0.1775380, 0.0000000],
    [0.0331946, 0.9668054, 0.0000000],
    [0.0170827, 0.0723974, 0.9105199],
], dtype=np.float32)

# BT.709 linear (D65) → ACEScg AP1 linear (ACES ~D60), Bradford-adapted.
# Standard published ACES conversion (matches the GPU cs_transform shader).
_BT709_TO_AP1 = np.array([
    [0.6130974, 0.3395231, 0.0473795],
    [0.0701937, 0.9163541, 0.0134522],
    [0.0206158, 0.1095697, 0.8697855],
], dtype=np.float32)

# ACEScg → BT.709 linear (inverse of the above; rows sum to 1.0 so ACES
# white maps to BT.709 white without a clip).
_AP1_TO_BT709 = np.array([
    [1.7050502, -0.6217914, -0.0832562],
    [-0.1302564, 1.1408043, -0.0105479],
    [-0.0240033, -0.1289187, 1.1529220],
], dtype=np.float32)

# Preserve XYZ using the specified DCI white (x=0.314, y=0.351),
# matching Resolve RCM with tone and gamut mapping disabled. Each RGB
# space's normalized primary matrix uses reference white Y=1.
_BT709_TO_P3DCI = np.array([
    [0.898951560399, 0.194049203885, 0.0],
    [0.031820755353, 0.926803234824, 0.0],
    [0.019654168872, 0.083295807766, 1.047585277121],
])

# Academy S-2014-003 TRA1, converting linear AP0 to linear AP1.
_AP0_TO_AP1 = np.array([
    [1.4514393161, -0.2365107469, -0.2149285693],
    [-0.0765537734, 1.1762296998, -0.0996759264],
    [0.0083161484, -0.0060324498, 0.9977163014],
])

_VIDEO_BT709_TO_GAMUT = {
    ColorPrimaries.BT709: np.eye(3),
    # Derived from the reference chromaticities. The legacy rounded matrix
    # moves some frame values across the discontinuous BT.2020 10-bit knee.
    ColorPrimaries.BT2020: np.array([
        [0.62740389593470, 0.32928303837788, 0.04331306568742],
        [0.06909728935823, 0.91954039507546, 0.01136231556631],
        [0.01639143887515, 0.08801330787723, 0.89559525324762],
    ]),
    ColorPrimaries.P3_DISPLAY: np.array([
        [0.82246196871436, 0.17753803128564, 0.0],
        [0.03319419885096, 0.96680580114904, 0.0],
        [0.01708263072112, 0.07239744066396, 0.91051992861492],
    ]),
    ColorPrimaries.P3_DCI: _BT709_TO_P3DCI,
    ColorPrimaries.AP0: np.linalg.solve(_AP0_TO_AP1, _BT709_TO_AP1),
    ColorPrimaries.AP1: _BT709_TO_AP1.astype(np.float64),
    # Blackmagic Design DaVinci Wide Gamut v1.1, D65.
    ColorPrimaries.DAVINCI_WIDE_GAMUT: np.array([
        [1.51667204, -0.28147805, -0.14696363],
        [-0.46491710, 1.25142378, 0.17488461],
        [0.06484905, 0.10913934, 0.76141462],
    ]) @ np.array([
        [0.412390799266, 0.357584339384, 0.180480788402],
        [0.212639005872, 0.715168678768, 0.072192315361],
        [0.019330818716, 0.119194779795, 0.950532152250],
    ]),
}


@lru_cache(maxsize=49)
def video_signal_gamut_matrix(source: ColorPrimaries, destination: ColorPrimaries) -> np.ndarray:
    """Return a read-only linear-light matrix applied as ``rgb @ matrix.T``."""
    try:
        from_709_to_source = _VIDEO_BT709_TO_GAMUT[source]
        from_709_to_target = _VIDEO_BT709_TO_GAMUT[destination]
    except KeyError as exc:
        raise ValueError(f"Unsupported video gamut: {exc.args[0].value}") from exc
    matrix = np.linalg.solve(from_709_to_source.T, from_709_to_target.T).T
    matrix.setflags(write=False)
    return matrix


@dataclass(frozen=True)
class VideoSignalLuminance:
    """Luminance mapping for a normalized video signal transform.

    One unit of relative linear light equals ``reference_white_nits``. PQ
    decodes to absolute nits. The HLG system OOTF additionally needs the
    reference display peak and gamma, assuming zero display black.
    """

    reference_white_nits: float
    hlg_peak_nits: float | None = None
    hlg_system_gamma: float | None = None

    def __post_init__(self) -> None:
        for name in ("reference_white_nits", "hlg_peak_nits", "hlg_system_gamma"):
            value = getattr(self, name)
            if value is None and name != "reference_white_nits":
                continue
            if value is None or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if (self.hlg_peak_nits is None) != (self.hlg_system_gamma is None):
            raise ValueError("HLG peak luminance and system gamma must be set together")


_VIDEO_DECODE = {
    ColorTransfer.SRGB: _srgb_to_linear_np,
    ColorTransfer.BT709: _bt709_to_linear_np,
    ColorTransfer.BT2020_10: _bt709_to_linear_np,
}
_VIDEO_ENCODE = {
    ColorTransfer.SRGB: _linear_to_srgb_np,
    ColorTransfer.BT709: _linear_to_bt709_np,
    ColorTransfer.BT2020_10: _linear_to_bt709_np,
}
_VIDEO_GAMMAS = {
    ColorTransfer.GAMMA22: 2.2,
    ColorTransfer.GAMMA24: 2.4,
    ColorTransfer.GAMMA26: 2.6,
    ColorTransfer.GAMMA28: 2.8,
}
_ACESCC_KNEE = 2.0 ** -15
_ACESCCT_KNEE = 0.0078125
_ACESCCT_SLOPE = 10.5402377416545
_ACESCCT_OFFSET = 0.0729055341958355
_HLG_LUMA = (0.2627, 0.6780, 0.0593)


def _video_hlg_transfer(
    rgb: np.ndarray, to_linear: bool, luminance: VideoSignalLuminance,
) -> np.ndarray:
    peak = luminance.hlg_peak_nits
    gamma = luminance.hlg_system_gamma
    if peak is None or gamma is None:
        raise ValueError("HLG conversion requires display peak and system gamma")
    scale = peak / luminance.reference_white_nits
    weights = np.asarray(_HLG_LUMA, dtype=rgb.dtype)
    if to_linear:
        code = np.maximum(rgb, 0.0)
        scene = np.where(code <= 0.5, np.square(code) / 3.0,
                         (np.exp((code - _HLG_C) / _HLG_A) + _HLG_B) / 12.0)
        y = (scene @ weights)[..., None]
        # Avoid evaluating a negative exponent at black for gamma below 1.
        factor = np.power(y, gamma - 1.0, out=np.zeros_like(y), where=y > 0.0)
        return scene * factor * scale
    display = np.maximum(rgb / scale, 0.0)
    y = (display @ weights)[..., None]
    # Invert the RGB-scaled OOTF by first recovering its scene luminance.
    factor = np.power(y, 1.0 / gamma - 1.0, out=np.zeros_like(y), where=y > 0.0)
    scene = display * factor
    return np.where(scene <= 1.0 / 12.0, np.sqrt(3.0 * scene),
                    _HLG_A * np.log(np.maximum(12.0 * scene - _HLG_B, 1e-10)) + _HLG_C)


def _video_transfer(rgb: np.ndarray, transfer: ColorTransfer, *, to_linear: bool,
                    luminance: VideoSignalLuminance | None) -> np.ndarray:
    if transfer == ColorTransfer.LINEAR:
        return rgb
    formula = (_VIDEO_DECODE if to_linear else _VIDEO_ENCODE).get(transfer)
    if formula is not None:
        return formula(rgb)
    gamma = _VIDEO_GAMMAS.get(transfer)
    if gamma is not None:
        return _signed_power_rgb(rgb, gamma if to_linear else 1.0 / gamma)
    if transfer == ColorTransfer.BT2020_12:
        alpha, beta = 1.0993, 0.0181
        if to_linear:
            power = np.power(np.maximum(rgb + alpha - 1.0, 0.0) / alpha, 1.0 / 0.45)
            return np.where(rgb < 4.5 * beta, rgb / 4.5, power)
        power = alpha * np.power(np.maximum(rgb, 0.0), 0.45) - (alpha - 1.0)
        return np.where(rgb < beta, 4.5 * rgb, power)
    if transfer == ColorTransfer.ACESCC:
        if to_linear:
            log2_value = rgb * 17.52 - 9.72
            limit = math.log2(65504.0)
            value = np.where(log2_value >= limit, 65504.0, np.exp2(np.minimum(log2_value, limit)))
            return np.where(log2_value <= -15.0, 2.0 * (value - 2.0 ** -16), value)
        value = np.where(rgb < _ACESCC_KNEE, (np.maximum(rgb, 0.0) + _ACESCC_KNEE) * 0.5, rgb)
        return (np.log2(value) + 9.72) / 17.52
    if transfer == ColorTransfer.ACESCCT:
        if to_linear:
            cut = _ACESCCT_KNEE * _ACESCCT_SLOPE + _ACESCCT_OFFSET
            return np.where(rgb <= cut, (rgb - _ACESCCT_OFFSET) / _ACESCCT_SLOPE,
                            np.exp2(rgb * 17.52 - 9.72))
        power = (np.log2(np.maximum(rgb, _ACESCCT_KNEE)) + 9.72) / 17.52
        return np.where(rgb <= _ACESCCT_KNEE, _ACESCCT_SLOPE * rgb + _ACESCCT_OFFSET, power)
    if transfer == ColorTransfer.DAVINCI_INTERMEDIATE:
        if to_linear:
            return np.where(rgb <= 0.02740668, rgb / 10.44426855,
                            np.exp2(rgb / 0.07329248 - 7.0) - 0.0075)
        power = 0.07329248 * (np.log2(np.maximum(rgb, 0.00262409) + 0.0075) + 7.0)
        return np.where(rgb <= 0.00262409, rgb * 10.44426855, power)
    if is_hdr_transfer(transfer):
        if luminance is None:
            raise ValueError("HDR conversion requires a reference white luminance")
        if transfer == ColorTransfer.HLG:
            return _video_hlg_transfer(rgb, to_linear, luminance)
        if to_linear:
            return _pq_to_linear_np(rgb) / luminance.reference_white_nits
        return np.where(rgb <= 0.0, 0.0, _linear_to_pq_np(rgb * luminance.reference_white_nits))
    raise ValueError(f"Unsupported video transfer: {transfer.value}")


def _apply_primaries_np(img: np.ndarray, src_prim: str, dst_prim: str) -> np.ndarray:
    """Convert linear RGB between primaries (gamut), BT.709-centric.

    BT.709 is the working-space reference; conversions compose via it.
    Only the first 3 channels are transformed (matmul on RGB). No-op when
    src == dst or either is unknown.
    """
    if src_prim == dst_prim or src_prim == "unknown" or dst_prim == "unknown":
        return img
    # Build the composite matrix: src→BT.709 (inverse of BT.709→src), then
    # BT.709→dst. We only have to/from BT.709 matrices, so invert as needed.
    def to709(prim):
        if prim == "bt709":
            return np.eye(3, dtype=np.float32)
        if prim == "bt2020":
            return np.linalg.inv(_BT709_TO_BT2020)
        if prim in ("smpte431", "smpte432"):  # DCI-P3 / Display P3
            return np.linalg.inv(_BT709_TO_P3D65)
        if prim in ("ap1", "acescg"):  # ACEScg (AP1, linear)
            return _AP1_TO_BT709
        return np.eye(3, dtype=np.float32)

    def from709(prim):
        if prim == "bt709":
            return np.eye(3, dtype=np.float32)
        if prim == "bt2020":
            return _BT709_TO_BT2020
        if prim in ("smpte431", "smpte432"):
            return _BT709_TO_P3D65
        if prim in ("ap1", "acescg"):
            return _BT709_TO_AP1
        return np.eye(3, dtype=np.float32)

    m = from709(dst_prim) @ to709(src_prim)
    rgb = img[..., :3]
    # matmul on the last axis: treat (...,3) as row vectors → (...,3) @ m.T
    converted = rgb @ m.T
    out = img.copy()
    out[..., :3] = converted
    return out


class BuiltinColorProvider:
    """Built-in colour transform using numpy math.

    Implements sRGB/Rec.709/PQ/HLG ↔ linear transfer functions, full↔limited
    range, BT.709↔BT.2020↔DCI-P3 gamut (primaries) conversion, and Reinhard/
    ACES HDR tone mapping. Zero external dependencies; used as the fallback
    when OCIO is unavailable (OCIO is preferred for production colour work).
    """

    name = "builtin"

    def is_available(self) -> bool:
        return True

    def probe_transform(self, src, dst) -> Optional[str]:
        """M19：builtin 数学覆盖全部描述符域（transfer/range/primaries），
        无 processor 概念——恒可转。"""
        return None

    def transform_signal(
        self, img: np.ndarray, src: ColorSpaceDescriptor, dst: ColorSpaceDescriptor,
        *, luminance: VideoSignalLuminance | None = None,
    ) -> np.ndarray:
        """Convert full-range RGB(A) without clipping floating SDR or ACES values.

        This is a colorimetric conversion used at project signal boundaries.
        Display rendering (such as an ACES Output Transform) and integer signal
        packing belong to the caller. Input RGB is straight and alpha is copied
        without conversion.
        """
        if src.range != ColorRange.FULL or dst.range != ColorRange.FULL:
            raise ValueError("Video RGB must be expanded to full range by the decoder")
        if img.ndim < 1 or img.shape[-1] not in (3, 4) or img.dtype.kind != "f":
            raise ValueError("Expected a floating-point RGB or RGBA signal")
        if src == dst:
            return img
        if any(desc.transfer == ColorTransfer.HLG and desc.primaries != ColorPrimaries.BT2020
               for desc in (src, dst)):
            raise ValueError("HLG conversion requires Rec.2020 primaries")
        matrix = video_signal_gamut_matrix(src.primaries, dst.primaries)
        working_dtype = np.result_type(img.dtype, np.float32)
        rgb = img[..., :3].astype(working_dtype, copy=False)
        linear = _video_transfer(rgb, src.transfer, to_linear=True, luminance=luminance)
        if src.primaries != dst.primaries:
            linear = linear @ matrix.astype(working_dtype, copy=False).T
        encoded = _video_transfer(linear, dst.transfer, to_linear=False, luminance=luminance)
        if img.shape[-1] == 3:
            return encoded.astype(img.dtype, copy=False)
        result = img.copy()
        result[..., :3] = encoded
        return result

    def transform(self, img: np.ndarray, src, dst) -> np.ndarray:
        """Structured-descriptor transform (see ColorTransformProvider)."""
        from src.shared.color.types import ColorTransfer, ColorRange

        if src == dst:
            return img

        out = img

        # 1. Range expand: if source is limited-range, expand to full first
        #    (only meaningful before linearizing; limited is a property of
        #    the encoded signal). AUD-32510: unconditional on entry — the
        #    previous ``dst.range != LIMITED`` guard left LIMITED→LIMITED
        #    conversions (e.g. REC709_VIDEO→HDR10_VIDEO) decoding the
        #    [16/255, 235/255] compressed signal as full-range, shifting
        #    colours systematically. This now matches the OCIO provider,
        #    which expands unconditionally (see OCIOColorProvider.transform).
        if src.range == ColorRange.LIMITED:
            out = _limited_to_full_np(out)

        # 2. Transfer function: decode src → linear, then linear → dst.
        #    Decode source to linear. HDR (PQ/HLG) produces values in nits
        #    (can exceed 1.0) — working space stays linear, unclamped.
        if src.transfer == ColorTransfer.SRGB:
            out = _srgb_to_linear_np(out)
        elif src.transfer == ColorTransfer.BT709:
            out = _bt709_to_linear_np(out)
        elif src.transfer == ColorTransfer.GAMMA24:
            out = _signed_power_rgb(out, 2.4)
        elif src.transfer == ColorTransfer.PQ:
            out = _pq_to_linear_np(out)
        elif src.transfer == ColorTransfer.HLG:
            out = _hlg_to_linear_np(out)
        # LINEAR / others: leave as-is

        # 3. Gamut (primaries) conversion in linear light. Working space is
        #    BT.709; convert linear RGB src→dst primaries.
        if src.primaries != dst.primaries:
            out = _apply_primaries_np(out, src.primaries.value, dst.primaries.value)

        # 4. Encode linear → dst transfer.
        if dst.transfer == ColorTransfer.SRGB:
            out = _linear_to_srgb_np(out)
        elif dst.transfer == ColorTransfer.BT709:
            out = _linear_to_bt709_np(out)
        elif dst.transfer == ColorTransfer.GAMMA24:
            out = _signed_power_rgb(out, 1.0 / 2.4)
        elif dst.transfer == ColorTransfer.PQ:
            out = _linear_to_pq_np(out)
        elif dst.transfer == ColorTransfer.HLG:
            out = _linear_to_hlg_np(out)
        # LINEAR / others: leave as-is

        # 5. Range compress: if dst is limited-range, compress from full.
        #    AUD-32510: unconditional on exit (mirrors the OCIO provider) —
        #    LIMITED→LIMITED conversions must end re-compressed.
        if dst.range == ColorRange.LIMITED:
            out = _full_to_limited_np(out)

        # Clamp non-linear outputs to [0,1]. Linear (incl. HDR-nits) stays
        # unclamped so HDR working-space values survive.
        if dst.transfer != ColorTransfer.LINEAR:
            out = np.clip(out, 0.0, 1.0)
        return out

    def export_encode(self, rgba: np.ndarray, src_space: str,
                      dst_space: str) -> np.ndarray:
        src = _normalise_space(src_space)
        dst = _normalise_space(dst_space)
        if src == dst:
            return rgba
        # Builtin knows sRGB ↔ linear ↔ ACEScg (gamut-only + transfer compose).
        if src == "srgb" and dst == "linear":
            return _srgb_to_linear_np(rgba)
        if src == "linear" and dst == "srgb":
            return _linear_to_srgb_np(rgba)
        # ACEScg is linear AP1: conversions against "linear" are pure gamut;
        # conversions against sRGB compose decode/encode with the gamut.
        if src == "acescg" and dst == "linear":
            return _apply_primaries_np(rgba, "ap1", "bt709")
        if src == "linear" and dst == "acescg":
            return _apply_primaries_np(rgba, "bt709", "ap1")
        if src == "srgb" and dst == "acescg":
            return _apply_primaries_np(_srgb_to_linear_np(rgba), "bt709", "ap1")
        if src == "acescg" and dst == "srgb":
            return _linear_to_srgb_np(_apply_primaries_np(rgba, "ap1", "bt709"))
        # Unknown combination — log and pass through (don't crash export).
        logger.warning(
            f"BuiltinColorProvider: no conversion for {src}→{dst}, "
            f"passing through unchanged."
        )
        return rgba


# ── OCIO provider (optional, soft dependency) ─────────────────────────────

# OCIO is imported lazily so the module loads even when PyOpenColorIO is
# not installed. The availability flag is probed once at import time.
try:
    import PyOpenColorIO as _OCIO  # type: ignore
    _OCIO_IMPORTABLE = True
except Exception:  # ImportError or any OCIO init failure
    _OCIO = None
    _OCIO_IMPORTABLE = False


def is_ocio_available() -> bool:
    """Whether PyOpenColorIO is importable."""
    return _OCIO_IMPORTABLE


# Path to the bundled minimal OCIO config, used as a fallback when the user
# enables OCIO but hasn't supplied their own config.ocio path.
_BUNDLED_OCIO_CONFIG = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "ocio", "minimal.ocio"
))


def get_bundled_ocio_config_path() -> str:
    """Absolute path to the bundled minimal OCIO config (may not exist)."""
    return _BUNDLED_OCIO_CONFIG


class OCIOColorProvider:
    """Colour transform via OpenColorIO processors.

    Soft dependency: if PyOpenColorIO isn't installed, ``is_available()``
    returns False and the factory falls back to the built-in provider.
    """

    name = "ocio"

    def __init__(self, config_path: Optional[str] = None):
        self._config: Optional[object] = None
        self._processor_cache: dict = {}
        if not _OCIO_IMPORTABLE:
            return
        # Resolve config path: explicit > bundled fallback.
        path = config_path or _BUNDLED_OCIO_CONFIG
        if path and os.path.isfile(path):
            try:
                self._config = _OCIO.Config.CreateFromFile(path)
                logger.info(f"OCIO config loaded: {path}")
            except Exception as e:
                logger.warning(f"Failed to load OCIO config '{path}': {e}")
                self._config = None
        else:
            logger.warning(
                f"OCIO enabled but no config found (tried '{path}'). "
                f"Falling back to built-in provider."
            )

    def is_available(self) -> bool:
        return _OCIO_IMPORTABLE and self._config is not None

    def _get_processor(self, src_space: str, dst_space: str):
        """Get (and cache) an OCIO CPU processor for src→dst."""
        key = (src_space, dst_space)
        if key in self._processor_cache:
            return self._processor_cache[key]
        try:
            proc = self._config.getProcessor(src_space, dst_space)
            cpu = proc.getDefaultCPUProcessor()
            self._processor_cache[key] = cpu
            return cpu
        except Exception as e:
            logger.warning(
                f"OCIO: no processor for {src_space}→{dst_space}: {e}"
            )
            return None

    def probe_transform(self, src, dst) -> Optional[str]:
        """M19：探测 src→dst 是否可转（None=可转；否则失败状态串）。"""
        if not self.is_available():
            return "provider_unavailable"
        src_ocio = src.to_ocio_name() if hasattr(src, "to_ocio_name") \
            else _ocio_space_name(src)
        dst_ocio = dst.to_ocio_name() if hasattr(dst, "to_ocio_name") \
            else _ocio_space_name(dst)
        if src_ocio == dst_ocio:
            return None
        if self._get_processor(src_ocio, dst_ocio) is None:
            return "processor_missing"
        return None

    def probe_exact_transform(self, src: ColorSpaceDescriptor,
                              dst: ColorSpaceDescriptor) -> str | None:
        """Probe a processor without accepting legacy fallback color spaces."""
        if not self.is_available():
            return "provider_unavailable"
        source = src.exact_ocio_name()
        destination = dst.exact_ocio_name()
        if source is None or destination is None:
            return "processor_missing"
        cpu = self._get_processor(source, destination)
        if cpu is None or (source != destination and cpu.isNoOp()):
            return "processor_missing"
        return None

    def export_encode(self, rgba: np.ndarray, src_space: str,
                      dst_space: str) -> np.ndarray:
        if not self.is_available():
            _record_transform_failure(
                "provider_unavailable", self.name, src_space, dst_space)
            return rgba
        src = _ocio_space_name(src_space)
        dst = _ocio_space_name(dst_space)
        if src == dst:
            return rgba
        cpu = self._get_processor(src, dst)
        if cpu is None:
            # M19（R01）：processor 缺失 = 该转换没有发生——记账可见，
            # 不再静默恒等
            _record_transform_failure(
                "processor_missing", self.name, src, dst)
            return rgba
        try:
            # OCIO's applyRGBA operates on a flat (n, 4) float32 buffer.
            # Reshape (h, w, 4) → (-1, 4), apply, reshape back.
            orig_shape = rgba.shape
            flat = np.ascontiguousarray(rgba.reshape(-1, 4), dtype=np.float32)
            cpu.applyRGBA(flat)
            return flat.reshape(orig_shape)
        except Exception as e:
            logger.warning(f"OCIO export_encode failed ({src}→{dst}): {e}")
            _record_transform_failure(
                "apply_failed", self.name, src, dst)
            return rgba

    def transform(self, img: np.ndarray, src, dst) -> np.ndarray:
        """Structured-descriptor transform via OCIO + numpy range handling.

        OCIO handles the transfer function (and gamut when the config
        defines it). Range (full/limited) is handled in numpy around the
        OCIO call, since OCIO operates on normalized [0,1] values and does
        not model signal range.
        """
        from src.shared.color.types import ColorRange

        if not self.is_available():
            _record_transform_failure(
                "provider_unavailable", self.name, src, dst)
            return img
        if src == dst:
            return img

        # M19.1b（R01）：扁平 (N,3)/(N,4) 像素批是合法输入——按 (1,N,c)
        # 参与转换（此前 ndim!=3 直接跳过 OCIO 静默返回原输入，flat RGB
        # 冒充已转换）。其余非法形状记账 invalid_input_shape 并原样返回
        # （不冒充转换；调用方经 last_transform_failures 可见）。
        flat_batch = (isinstance(img, np.ndarray) and img.ndim == 2
                      and img.shape[-1] in (3, 4))
        if flat_batch:
            converted = self.transform(
                img[np.newaxis, ...], src, dst)
            return (converted[0] if isinstance(converted, np.ndarray)
                    else converted)
        if not (isinstance(img, np.ndarray) and img.ndim == 3
                and img.shape[-1] in (3, 4)):
            _record_transform_failure(
                "invalid_input_shape", self.name, src, dst)
            return img

        out = img

        # 1. Expand source limited-range → full before OCIO.
        if src.range == ColorRange.LIMITED:
            out = _limited_to_full_np(out)

        # 2. OCIO transfer-function (and gamut) conversion.
        src_ocio = src.to_ocio_name()
        dst_ocio = dst.to_ocio_name()
        if src_ocio != dst_ocio:
            cpu = self._get_processor(src_ocio, dst_ocio)
            if cpu is None:
                # M19（R01）：processor 缺失——转换未发生，记账可见
                _record_transform_failure(
                    "processor_missing", self.name, src, dst)
            else:
                try:
                    orig_shape = out.shape
                    nchan = orig_shape[-1] if out.ndim == 3 else 4
                    if nchan >= 3:
                        flat = np.ascontiguousarray(
                            out.reshape(-1, nchan)[..., :4 if nchan >= 4 else 3]
                            .astype(np.float32),
                        )
                        # Pad to 4 channels if needed (OCIO applyRGBA needs 4).
                        if flat.shape[-1] == 3:
                            alpha = np.ones((flat.shape[0], 1), dtype=np.float32)
                            flat = np.concatenate([flat, alpha], axis=-1)
                        cpu.applyRGBA(flat)
                        flat = flat[..., :orig_shape[-1] if out.ndim == 3 else 4]
                        out = flat.reshape(orig_shape)
                except Exception as e:
                    logger.warning(f"OCIO transform failed ({src}→{dst}): {e}")
                    _record_transform_failure(
                        "apply_failed", self.name, src, dst)

        # 3. Compress to limited-range if dst is limited.
        if dst.range == ColorRange.LIMITED:
            out = _full_to_limited_np(out)

        # Clamp non-linear outputs.
        if not dst.is_linear:
            out = np.clip(out, 0.0, 1.0)
        return out


# The OCIO Studio configs pin both the ACES reference implementation and the
# list of available display encodings. Each target specifies its rendering
# transform explicitly because the selected display may have several views.
_ACES_CONFIGS = {
    "1.3": "studio-config-v2.2.0_aces-v1.3_ocio-v2.4",
    "2.0": "studio-config-v4.0.0_aces-v2.0_ocio-v2.5",
}
_ACES_OUTPUT_VIEWS = {
    "1.3": {
        "rec709-g24": ("Rec.1886 Rec.709 - Display", "ACES 1.0 - SDR Video"),
        "srgb": ("sRGB - Display", "ACES 1.0 - SDR Video"),
        "display-p3": ("Display P3 - Display", "ACES 1.0 - SDR Video"),
        "rec2020-g24": ("Rec.1886 Rec.2020 - Display", "ACES 1.1 - SDR Video (P3 lim)"),
        "dci-p3-g26-d60-sim": ("P3-DCI - Display", "ACES 1.0 - SDR Cinema (D60 sim on DCI)"),
        "rec2100-pq": ("Rec.2100-PQ - Display", "ACES 1.1 - HDR Video (1000 nits & Rec.2020 lim)"),
        "rec2100-hlg": ("Rec.2100-HLG - Display", "ACES 1.1 - HDR Video (1000 nits & Rec.2020 lim)"),
    },
    "2.0": {
        "rec709-g24": ("Rec.1886 Rec.709 - Display", "ACES 2.0 - SDR 100 nits (Rec.709)"),
        "srgb": ("sRGB - Display", "ACES 2.0 - SDR 100 nits (Rec.709)"),
        "display-p3": ("Display P3 - Display", "ACES 2.0 - SDR 100 nits (P3 D65)"),
        "rec2100-pq": ("Rec.2100-PQ - Display", "ACES 2.0 - HDR 1000 nits (Rec.2020)"),
        "rec2100-hlg-p3-lim": ("Rec.2100-HLG - Display", "ACES 2.0 - HDR 1000 nits (P3 D65)"),
    },
}
_ACES_SOURCE_NAMES = {
    (ColorPrimaries.AP0, ColorTransfer.LINEAR): "ACES2065-1",
    (ColorPrimaries.AP1, ColorTransfer.LINEAR): "ACEScg",
    (ColorPrimaries.AP1, ColorTransfer.ACESCC): "ACEScc",
    (ColorPrimaries.AP1, ColorTransfer.ACESCCT): "ACEScct",
}


@dataclass(frozen=True)
class AcesOutputView:
    aces_version: str
    target: str
    config_id: str
    display: str
    view: str


def resolve_aces_output_view(aces_version: str, target: str) -> AcesOutputView:
    """Resolve the exact built-in OCIO config and Display/View identifiers."""
    if aces_version not in _ACES_CONFIGS:
        raise ValueError(f"Unsupported ACES version: {aces_version!r}")
    pair = _ACES_OUTPUT_VIEWS[aces_version].get(target)
    if pair is None:
        raise ValueError(f"ACES {aces_version} has no registered output for {target!r}")
    return AcesOutputView(aces_version, target, _ACES_CONFIGS[aces_version], *pair)


class _AcesCpuProcessor(Protocol):
    def applyRGB(self, pixels: np.ndarray) -> None: ...


@lru_cache(maxsize=len(_ACES_CONFIGS))
def _load_aces_config(config_id: str) -> "_OCIO.Config":
    if not _OCIO_IMPORTABLE:
        raise ValueError("OpenColorIO is unavailable")
    try:
        return _OCIO.Config.CreateFromBuiltinConfig(config_id)
    except _OCIO.Exception as exc:
        raise ValueError(f"OpenColorIO lacks the required ACES config {config_id!r}") from exc


@lru_cache(maxsize=64)
def _aces_display_processor(
    output: AcesOutputView, source: str, reference_gamut_compression: bool,
) -> "_OCIO.Processor":
    config = _load_aces_config(output.config_id)
    display = _OCIO.DisplayViewTransform(src=source, display=output.display, view=output.view)
    transform = display
    if reference_gamut_compression:
        if output.aces_version != "1.3":
            raise ValueError("Reference gamut compression is available only for ACES 1.3")
        look = _OCIO.LookTransform(src=source, dst=source, looks="ACES 1.3 Reference Gamut Compression")
        transform = _OCIO.GroupTransform(transforms=[look, display])
    try:
        return config.getProcessor(transform)
    except _OCIO.Exception as exc:
        raise ValueError(f"OpenColorIO lacks the ACES processor for {output.target!r}") from exc


@lru_cache(maxsize=64)
def _aces_output_processor(
    output: AcesOutputView, source: str, reference_gamut_compression: bool,
) -> _AcesCpuProcessor:
    return _aces_display_processor(output, source, reference_gamut_compression).getDefaultCPUProcessor()


def aces_output_gpu_shader(aces_version: str, target: str) -> "_OCIO.GpuShaderDesc":
    """Extract the reference ODT for scene-linear AP1 input and its tables."""
    output = resolve_aces_output_view(aces_version, target)
    processor = _aces_display_processor(output, "ACEScg", False)
    shader = _OCIO.GpuShaderDesc.CreateShaderDesc()
    shader.setLanguage(_OCIO.GPU_LANGUAGE_GLSL_1_3)
    shader.setFunctionName("topos_aces_ocio")
    shader.setResourcePrefix("topos_aces_")
    shader.setAllowTexture1D(False)
    processor.getDefaultGPUProcessor().extractGpuShaderInfo(shader)
    return shader


@lru_cache(maxsize=1)
def _aces_rgc_processor() -> _AcesCpuProcessor:
    config = _load_aces_config(_ACES_CONFIGS["1.3"])
    look = _OCIO.LookTransform(
        src="ACEScg", dst="ACEScg", looks="ACES 1.3 Reference Gamut Compression")
    return config.getProcessor(look).getDefaultCPUProcessor()


def apply_aces_reference_gamut_compression(pixels: np.ndarray) -> None:
    """Apply the reference ACES 1.3 look to contiguous float32 AP1 RGB."""
    _aces_rgc_processor().applyRGB(pixels)


def apply_aces_output_transform(
    img: np.ndarray, source: ColorSpaceDescriptor, *, aces_version: str, target: str,
    reference_gamut_compression: bool = False,
) -> np.ndarray:
    """Render straight RGB(A) in an ACES working encoding to a pinned target."""
    source_name = _ACES_SOURCE_NAMES.get((source.primaries, source.transfer))
    if source_name is None or source.range != ColorRange.FULL:
        raise ValueError("Expected a full-range ACES source encoding")
    if img.ndim < 1 or img.shape[-1] not in (3, 4) or img.dtype.kind != "f":
        raise ValueError("Expected a floating-point RGB or RGBA signal")
    output = resolve_aces_output_view(aces_version, target)
    processor = _aces_output_processor(output, source_name, reference_gamut_compression)
    pixels = np.array(img[..., :3], dtype=np.float32, order="C", copy=True)
    if pixels.size:
        processor.applyRGB(pixels)
    if img.shape[-1] == 3:
        return pixels.astype(img.dtype, copy=False)
    result = img.copy()
    result[..., :3] = pixels
    return result


# ── Factory ───────────────────────────────────────────────────────────────

# Module-level cache so we don't rebuild the provider/config on every call.
_provider_cache: dict = {}
# Structured record of WHY the active provider was chosen (or why OCIO was
# rejected) — surfaced by get_color_provider_diagnostics so fallbacks are
# explainable instead of silent (P0-COLOR-05).
_provider_diagnostics: dict = {}
# AUD-32511: export worker threads (parallel_frame_render) and the UI thread
# call get_color_provider()/get_color_provider_diagnostics() concurrently.
# clear()+update() on the diagnostics dict is not atomic (readers could see
# an empty/half-updated record) and concurrent first-calls raced duplicate
# Config.CreateFromFile builds that overwrote each other. RLock because
# get_color_provider calls _record_diagnostics while already holding it.
_provider_lock = threading.RLock()


def _record_diagnostics(engine: str, provider, reason: str,
                        config_path: str = "") -> None:
    with _provider_lock:
        _provider_diagnostics.clear()
        _provider_diagnostics.update({
            "requested_engine": engine,
            "ocio_importable": _OCIO_IMPORTABLE,
            "ocio_config_path": config_path or _BUNDLED_OCIO_CONFIG,
            "ocio_config_loaded": bool(
                _OCIO_IMPORTABLE and getattr(provider, "name", "") == "ocio"
            ),
            "active_provider": getattr(provider, "name", "unknown"),
            "fallback_reason": reason,
        })


def get_color_provider_diagnostics(config_manager=None) -> dict:
    """P0-COLOR-05：色彩引擎能力诊断（结构化，非静默）。

    返回当前 provider 选择过程的结构化报告：
      - requested_engine: 配置请求的引擎（"ocio"/"builtin"）
      - ocio_importable: PyOpenColorIO 是否可导入
      - ocio_config_path: 尝试加载的 config 路径（显式或 bundled）
      - ocio_config_loaded: OCIO config 是否成功加载
      - active_provider: 实际生效的 provider（"ocio"/"builtin"）
      - fallback_reason: 降级原因（"" = 无降级）

    若 provider 尚未构建（诊断先于任何 transform 调用），会先构建一次
    以填充诊断。调用方（启动日志/状态栏）据此给出明确提示，而不是
    静默吞掉 OCIO 加载失败。
    """
    get_color_provider(config_manager)
    with _provider_lock:
        return dict(_provider_diagnostics)


def get_color_provider(config_manager=None) -> ColorTransformProvider:
    """Get the active colour-transform provider.

    Selection logic:
    1. Read ``composite.color_engine`` from config (default "ocio" — OCIO
       is now a required dependency).
    2. If "ocio": try OCIOColorProvider; if unavailable (OCIO not installed
       or no config), fall back to builtin with a warning.
    3. If "builtin": always use BuiltinColorProvider.
    4. Cache the result so repeated calls are cheap.

    Args:
        config_manager: ConfigManager (or dict-like). If None, probes OCIO
            and falls back to builtin.

    Returns:
        A ColorTransformProvider instance (always usable — never None).
    """
    engine = "ocio"
    ocio_path = ""
    if config_manager is not None:
        try:
            engine = config_manager.get("composite.color_engine", "ocio") or "ocio"
            ocio_path = config_manager.get("ocio_config_path", "") or ""
        except Exception:
            pass

    cache_key = (engine, ocio_path)
    # AUD-32511: check + build + publish under one lock — concurrent first
    # calls from export workers previously each ran Config.CreateFromFile
    # and raced overwriting the cache entry.
    with _provider_lock:
        cached = _provider_cache.get(cache_key)
        if cached is not None:
            return cached

        provider: ColorTransformProvider
        if engine == "ocio":
            ocio = OCIOColorProvider(config_path=ocio_path or None)
            if ocio.is_available():
                provider = ocio
                _record_diagnostics(engine, provider, "", ocio_path)
            else:
                if not _OCIO_IMPORTABLE:
                    reason = (
                        "PyOpenColorIO 未安装（pip install opencolorio），"
                        "已回退 builtin numpy 引擎"
                    )
                else:
                    reason = (
                        f"OCIO config 加载失败（路径 '{ocio_path or _BUNDLED_OCIO_CONFIG}'），"
                        f"已回退 builtin numpy 引擎"
                    )
                logger.warning(f"OCIO provider unavailable: {reason}")
                provider = BuiltinColorProvider()
                _record_diagnostics(engine, provider, reason, ocio_path)
        else:
            provider = BuiltinColorProvider()
            _record_diagnostics(engine, provider, "", ocio_path)

        _provider_cache[cache_key] = provider
        return provider


def ensure_ocio_available() -> bool:
    """Verify OCIO is importable (it's now a required dependency).

    Returns True if PyOpenColorIO imports. Call at startup to give a clear
    error when the required dependency is missing.
    """
    return _OCIO_IMPORTABLE


def transform_color_typed(img: np.ndarray, src, dst,
                           config_manager=None) -> TransformResult:
    """M19：类型化色彩转换入口——不冒充转换。

    非法输入形状（非 (h,w,3|4) float）/provider 不可用/processor 缺失/
    执行失败都返回明确 status（image 为原输入供降级显示），调用方
    （交付预检等）据此拒绝或明示降级，不得把 ok=False 的 image 当作
    已转换数据。
    """
    provider = get_color_provider(config_manager)
    src_desc = str(getattr(src, "name", src))
    dst_desc = str(getattr(dst, "name", dst))
    if not isinstance(img, np.ndarray) or img.ndim != 3 \
            or img.shape[-1] not in (3, 4) \
            or not np.issubdtype(img.dtype, np.floating):
        return TransformResult(
            image=img if isinstance(img, np.ndarray) else np.zeros((1, 1, 3), np.float32),
            status="invalid_input_shape",
            detail=f"需要 (h,w,3|4) float 数组，实际 {getattr(img, 'shape', type(img).__name__)} {getattr(img, 'dtype', '')}",
            provider=getattr(provider, "name", ""),
            src_desc=src_desc, dst_desc=dst_desc)
    if src == dst:
        return TransformResult(
            image=img, status="identity_src_eq_dst",
            provider=getattr(provider, "name", ""),
            src_desc=src_desc, dst_desc=dst_desc)
    if not provider.is_available():
        _record_transform_failure(
            "provider_unavailable", getattr(provider, "name", ""),
            src_desc, dst_desc)
        return TransformResult(
            image=img, status="provider_unavailable",
            provider=getattr(provider, "name", ""),
            src_desc=src_desc, dst_desc=dst_desc)
    probe = getattr(provider, "probe_transform", None)
    if callable(probe):
        failure = probe(src, dst)
        if failure:
            return TransformResult(
                image=img, status=failure,
                provider=getattr(provider, "name", ""),
                src_desc=src_desc, dst_desc=dst_desc)
    try:
        out = provider.transform(img, src, dst)
    except Exception as exc:  # noqa: BLE001 — 执行失败类型化，不冒充
        _record_transform_failure(
            "apply_failed", getattr(provider, "name", ""),
            src_desc, dst_desc)
        return TransformResult(
            image=img, status="apply_failed", detail=str(exc),
            provider=getattr(provider, "name", ""),
            src_desc=src_desc, dst_desc=dst_desc)
    return TransformResult(
        image=out, status="converted",
        provider=getattr(provider, "name", ""),
        src_desc=src_desc, dst_desc=dst_desc)


def transform_color(img: np.ndarray, src, dst, config_manager=None) -> np.ndarray:
    """Convenience: convert an image between two ColorSpaceDescriptors.

    Resolves the active provider and calls its ``transform``. This is the
    preferred entry point for color conversion throughout the app — callers
    don't need to know which provider is active.

    Args:
        img: (h, w, 3|4) float32 image.
        src: source ColorSpaceDescriptor.
        dst: destination ColorSpaceDescriptor.
        config_manager: optional ConfigManager for provider selection.

    Returns:
        Converted image.
    """
    provider = get_color_provider(config_manager)
    return provider.transform(img, src, dst)


def reset_provider_cache() -> None:
    """Clear the provider cache (call when config changes)."""
    with _provider_lock:
        _provider_cache.clear()
        _provider_diagnostics.clear()
