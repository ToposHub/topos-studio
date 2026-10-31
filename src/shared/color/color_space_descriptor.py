"""Structured color-space descriptor: primaries + transfer + range.

The legacy ``ColorSpace`` (features/composite/core/color_space.py) is a
4-value string enum (linear/srgb/display/unknown) that conflates color
**primaries** (gamut) with the **transfer function** (gamma curve) and the
**range** (full/limited). Real-world color management needs these three
orthogonally:

    sRGB still image  = (BT.709 primaries, sRGB OETF, full range)
    Rec.709 video      = (BT.709 primaries, BT.709/BT.1886 OETF, limited range)

Both share the BT.709 primaries, but differ in transfer function and range —
exactly the "video looks higher-contrast / has headroom" difference.

This module defines ``ColorSpaceDescriptor``, a structured descriptor reusing
the already-complete FFmpeg-mapped enums in ``src/shared/color/types.py``
(``ColorPrimaries`` / ``ColorTransfer`` / ``ColorRange``).

The descriptor is the single currency for the new color pipeline:
  - source nodes tag their output with a descriptor
  - the IDT converts source-descriptor → scene-linear
  - the ODT converts scene-linear → display-descriptor (preference-driven)
  - export converts scene-linear → output-descriptor + tags the bitstream

It serializes trivially to/from a dict (for project/pref persistence) and
maps to OCIO space names (for the OCIO provider).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional

from src.shared.color.types import ColorPrimaries, ColorTransfer, ColorRange

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColorSpaceDescriptor:
    """A structured color space: primaries + transfer + range.

    Attributes:
        primaries: color gamut (ColorPrimaries enum value).
        transfer: transfer function / gamma curve (ColorTransfer enum value).
        range: pixel value range (ColorRange enum value).
    """

    primaries: ColorPrimaries = ColorPrimaries.BT709
    transfer: ColorTransfer = ColorTransfer.LINEAR
    range: ColorRange = ColorRange.FULL

    # ── Serialization ────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, str]:
        """Serialize to a plain dict (JSON-safe) for project/pref storage."""
        return {
            "primaries": self.primaries.value,
            "transfer": self.transfer.value,
            "range": self.range.value,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ColorSpaceDescriptor":
        """Deserialize from a dict; tolerates missing/None → LINEAR defaults."""
        if not data:
            return LINEAR_SCENE
        return cls(
            primaries=_enum_from_value(ColorPrimaries, data.get("primaries"), ColorPrimaries.BT709),
            transfer=_enum_from_value(ColorTransfer, data.get("transfer"), ColorTransfer.LINEAR),
            range=_enum_from_value(ColorRange, data.get("range"), ColorRange.FULL),
        )

    # ── OCIO mapping ─────────────────────────────────────────────────

    def to_ocio_name(self) -> str:
        """Map to an OCIO color space name in the bundled config.

        The bundled minimal.ocio defines spaces named "lin", "srgb",
        "rec709", "acescg" (see ocio/minimal.ocio). Known combinations
        resolve via ``_OCIO_NAME_MAP``; combinations missing from the
        bundled config resolve via the explicit fallback table
        ``_OCIO_FALLBACK_NAME_MAP`` (transfer curve preserved, gamut
        approximated) with a once-per-combination ``logger.warning`` —
        never silently to linear (AUD-32500): a silent "lin" fallback
        makes the OCIO provider treat an encoded signal as scene-linear
        and skip the ODT encode entirely.
        """
        if self.transfer == ColorTransfer.LINEAR:
            # ACEScg is scene-linear AP1 — a distinct config space, not the
            # BT.709-gamut working space "lin".
            if self.primaries == ColorPrimaries.AP1:
                return "acescg"
            if self.primaries != ColorPrimaries.BT709:
                # No scene-linear space for this gamut exists in the
                # bundled config; "lin" keeps the transfer but drops the
                # gamut matrix. That is a real colour error, so surface it
                # instead of folding silently into the BT.709 working space.
                _warn_ocio_fallback(
                    (self.primaries.value, ColorTransfer.LINEAR.value),
                    OCIO_LINEAR_NAME,
                    reason="scene-linear space for this gamut not in bundled "
                           "config; gamut matrix dropped",
                )
            return OCIO_LINEAR_NAME
        # primaries + transfer combine into a named display/output space.
        prim = self.primaries.value
        combo = (prim, self.transfer.value)
        name = _OCIO_NAME_MAP.get(combo)
        if name is not None:
            return name
        fallback = _OCIO_FALLBACK_NAME_MAP.get(combo)
        if fallback is None:
            # Not mapped and no explicit fallback row: keep the transfer
            # curve with a conservative SDR space (PQ/HLG-ish transfers get
            # their curve-preserving HDR space). Still logged — never a
            # silent "lin".
            tf = self.transfer.value
            if tf in ("smpte2084", "st2084"):
                fallback = "bt2020_pq"
            elif tf == "arib-std-b67":
                fallback = "bt2020_hlg"
            else:
                fallback = "srgb"
        _warn_ocio_fallback(
            combo, fallback,
            reason="no exact OCIO space in bundled config; transfer curve "
                   "preserved, gamut approximated",
        )
        return fallback

    @classmethod
    def from_ocio_name(cls, name: str) -> "ColorSpaceDescriptor":
        """Reverse of to_ocio_name; linear names → LINEAR_SCENE."""
        if name in (OCIO_LINEAR_NAME, "linear", "scene_linear", "scene-linear"):
            return LINEAR_SCENE
        if name in ("acescg", "aces_cg", "ap1"):
            return ACESCG_SCENE
        for (prim, tf), n in _OCIO_NAME_MAP.items():
            if n == name:
                return cls(
                    primaries=_enum_from_value(ColorPrimaries, prim, ColorPrimaries.BT709),
                    transfer=_enum_from_value(ColorTransfer, tf, ColorTransfer.LINEAR),
                    range=_OCIO_SPACE_RANGE.get(n, ColorRange.FULL),
                )
        return LINEAR_SCENE

    # ── Convenience ──────────────────────────────────────────────────

    @property
    def is_linear(self) -> bool:
        """Whether this descriptor represents scene-linear (no transfer)."""
        return self.transfer == ColorTransfer.LINEAR

    def with_transfer(self, transfer: ColorTransfer) -> "ColorSpaceDescriptor":
        """Return a copy with a different transfer function."""
        return replace(self, transfer=transfer)

    def __str__(self) -> str:
        return f"{self.primaries.value}/{self.transfer.value}/{self.range.value}"


# ── Presets ───────────────────────────────────────────────────────────
#
# The canonical spaces the pipeline deals with. Working space is always
# scene-linear; sources and display/output spaces are these.

#: Scene-linear working space (graph internal). Always BT.709 primaries,
#: linear transfer, full range. This is the canonical graph working space.
LINEAR_SCENE = ColorSpaceDescriptor(
    primaries=ColorPrimaries.BT709,
    transfer=ColorTransfer.LINEAR,
    range=ColorRange.FULL,
)

#: ACEScg scene-linear (AP1 primaries, linear transfer, ACES ~D60 white).
#: The standard ACES compositing working space. Sources tagged ``aces_cg``
#: decode into the BT.709-gamut working space via this descriptor's matrix.
ACESCG_SCENE = ColorSpaceDescriptor(
    primaries=ColorPrimaries.AP1,
    transfer=ColorTransfer.LINEAR,
    range=ColorRange.FULL,
)

#: Standard sRGB still image (JPEG/PNG/WebP). BT.709 primaries (sRGB shares
#: Rec.709 primaries), IEC 61966-2-1 transfer, full range 0-255.
SRGB_IMAGE = ColorSpaceDescriptor(
    primaries=ColorPrimaries.BT709,
    transfer=ColorTransfer.SRGB,   # iec61966_2_1
    range=ColorRange.FULL,
)

#: Standard Rec.709 SDR video (H.264/HEVC 8-bit). BT.709 primaries,
#: BT.709 transfer (~gamma 2.4 / BT.1886), limited range 16-235.
REC709_VIDEO = ColorSpaceDescriptor(
    primaries=ColorPrimaries.BT709,
    transfer=ColorTransfer.BT709,
    range=ColorRange.LIMITED,
)

# ── Stage-2 HDR / wide-gamut presets ────────────────────────────────────

#: HDR10 (BT.2020 + PQ, limited range, 10-bit). The standard HDR video
#: container — UHD HDR streams. Linear working values are in nits (0–10000).
HDR10_VIDEO = ColorSpaceDescriptor(
    primaries=ColorPrimaries.BT2020,
    transfer=ColorTransfer.PQ,
    range=ColorRange.LIMITED,
)

#: HLG broadcast HDR (BT.2020 + ARIB STD-B67, limited range). Used by HDR
#: television broadcasts; backward-compatible with SDR on legacy displays.
HLG_VIDEO = ColorSpaceDescriptor(
    primaries=ColorPrimaries.BT2020,
    transfer=ColorTransfer.HLG,
    range=ColorRange.LIMITED,
)

#: Display P3 (Apple/wide-gamut display) with sRGB transfer, full range.
#: Common for modern color-managed stills and DCI-adjacent video.
DISPLAY_P3_SRGB = ColorSpaceDescriptor(
    primaries=ColorPrimaries.P3_DISPLAY,
    transfer=ColorTransfer.SRGB,
    range=ColorRange.FULL,
)


# ── OCIO name mapping ─────────────────────────────────────────────────
#
# Maps (primaries.value, transfer.value) → OCIO space name in the bundled
# minimal.ocio config. Kept in sync with that config. MVP only needs the
# BT.709 + sRGB/Rec.709 combinations; HDR/wide-gamut combos are reserved
# for stage 2.

_OCIO_NAME_MAP: Dict[tuple, str] = {
    # Must stay in sync with ocio/minimal.ocio color space names.
    # BT.709 gamut
    ("bt709", "iec61966_2_1"): "srgb",          # sRGB stills
    ("bt709", "bt709"): "rec709",                # Rec.709 SDR video
    ("bt709", "gamma22"): "srgb",                # approx; treated as sRGB
    # ACEScg (AP1 scene-linear; to_ocio_name short-circuits for LINEAR but
    # from_ocio_name needs the reverse entry).
    ("ap1", "linear"): "acescg",
    # Stage-2: HDR / wide gamut (matches the bundled config's space names).
    ("bt2020", "smpte2084"): "bt2020_pq",        # HDR10 (BT.2020 + PQ)
    ("bt2020", "arib-std-b67"): "bt2020_hlg",    # HLG broadcast HDR
    ("smpte431", "smpte2084"): "p3_pq",          # DCI-P3 + PQ (P0-COLOR-05:
    # previously fell back to bt2020_pq, decoding P3 content through the
    # BT.2020 gamut — a silent colour shift; the config now defines p3_pq)
    ("smpte432", "iec61966_2_1"): "display_p3",  # Display P3 + sRGB transfer
}

# AUD-32500: explicit fallback for combinations the bundled config does not
# define but the preference UI offers (BT709/P3/BT2020 × sRGB/BT709/PQ/HLG).
# Selection rule: preserve the transfer curve wherever the config has a space
# with that curve (treating an encoded signal as linear is the worse error),
# accept a gamut approximation, and report it once per combination. Keeping
# this table explicit — rather than defaulting everything to "lin" — means
# the OCIO provider still applies an ODT encode instead of silently passing
# range-compressed linear data through as display-encoded output.
_OCIO_FALLBACK_NAME_MAP: Dict[tuple, str] = {
    # BT.2020 gamut with SDR curves → curve-preserving BT.709 spaces
    # (gamut narrowed; warned).
    ("bt2020", "iec61966_2_1"): "srgb",
    ("bt2020", "bt709"): "rec709",
    ("bt2020", "gamma22"): "srgb",
    # PQ/HLG curves under BT.709 gamut → the config's PQ/HLG spaces
    # (gamut widened; warned).
    ("bt709", "smpte2084"): "bt2020_pq",
    ("bt709", "arib-std-b67"): "bt2020_hlg",
    # P3 variants: DCI-P3 (smpte431) and Display P3 (smpte432) share the
    # display_p3 / p3_pq spaces closely enough for a warned approximation.
    ("smpte431", "iec61966_2_1"): "display_p3",
    ("smpte431", "bt709"): "display_p3",
    ("smpte431", "arib-std-b67"): "bt2020_hlg",
    ("smpte432", "bt709"): "display_p3",
    ("smpte432", "smpte2084"): "p3_pq",
    ("smpte432", "arib-std-b67"): "bt2020_hlg",
}

# Combinations already warned about (one warning per combination per process;
# to_ocio_name sits on render paths, so unbounded logging would spam exports).
_OCIO_WARNED_COMBOS: set = set()


def _warn_ocio_fallback(combo: tuple, fallback: str, reason: str) -> None:
    """Log (once per combination) that a descriptor maps to a fallback space.

    Module-level set of warned combos; ``set.add`` is atomic under the GIL,
    so concurrent render threads can at worst duplicate a single warning.
    """
    if combo in _OCIO_WARNED_COMBOS:
        return
    _OCIO_WARNED_COMBOS.add(combo)
    logger.warning(
        "Color space '%s/%s' has no OCIO space in the bundled config "
        "(ocio/minimal.ocio): falling back to '%s' — %s. Define the space "
        "in the bundled config to make this mapping exact.",
        combo[0], combo[1], fallback, reason,
    )

# Reverse range hint per OCIO space name. OCIO space names don't encode range,
# so this table records the range each space is conventionally used with, so
# descriptor round-trips preserve LIMITED (e.g. rec709) instead of collapsing
# everything to FULL. Unknown spaces default to FULL (the still-image norm).
_OCIO_SPACE_RANGE: Dict[str, ColorRange] = {
    "rec709": ColorRange.LIMITED,    # broadcast/web video (16-235)
    "bt2020_pq": ColorRange.LIMITED, # HDR10 is limited-range
    "bt2020_hlg": ColorRange.LIMITED,
    "p3_pq": ColorRange.LIMITED,     # DCI/HDR P3 delivery is limited-range
    # "srgb", "display_p3", "lin" → FULL (still images / linear working space)
}


# OCIO scene-linear space name in the bundled config.
OCIO_LINEAR_NAME = "lin"


# ── Helpers ───────────────────────────────────────────────────────────


def _enum_from_value(enum_cls, value, default):
    """Look up an enum member by value, returning ``default`` if not found."""
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return default


def descriptor_for_source_kind(is_video: bool) -> ColorSpaceDescriptor:
    """Smart default descriptor based on whether the source is video or image.

    - Video/timeline source → REC709_VIDEO (BT.709 transfer, limited range)
    - Still image source → SRGB_IMAGE (sRGB transfer, full range)

    Used by the preview/ODT smart-default logic when the user has not
    explicitly chosen a color space in preferences.
    """
    return REC709_VIDEO if is_video else SRGB_IMAGE


# ── Preference-driven descriptor builders ──────────────────────────────


# ── Project-level color management overrides (P0-COLOR-04) ──────────────
#
# 工程级色彩管理覆盖层：工程 settings["color_management"] 在打开时注入，
# 优先于用户全局偏好（ConfigManager），关闭/新建工程时清空。这样工程
# 换机器/交接时色彩管理设定随行走，且不写回（污染）用户全局偏好。
# 只在 UI 线程读写（打开工程/渲染参数构建），无需加锁。

_PROJECT_COLOR_OVERRIDES: dict = {}

#: 随工程持久化的 color.* 键（与 DEFAULT_CONFIG / 偏好页一致）
COLOR_MANAGEMENT_SETTING_KEYS: tuple = (
    "color.preview.timeline_primaries",
    "color.preview.timeline_transfer",
    "color.preview.timeline_range",
    "color.preview.image_primaries",
    "color.preview.image_transfer",
    "color.preview.image_range",
    "color.output.timeline_primaries",
    "color.output.timeline_transfer",
    "color.output.timeline_range",
    "color.output.image_primaries",
    "color.output.image_transfer",
    "color.output.image_range",
    "color.preview.hdr_tonemap",
    "color.preview.hdr_target_peak",
    "color.output.hdr_metadata",
    "color.smart_default",
)


def set_project_color_overrides(overrides) -> None:
    """注入工程级 color.* 设定（空/None 等价于清空）。"""
    global _PROJECT_COLOR_OVERRIDES
    _PROJECT_COLOR_OVERRIDES = (
        {k: v for k, v in dict(overrides).items() if v is not None}
        if overrides else {}
    )


def clear_project_color_overrides() -> None:
    """清空工程级覆盖（关闭/新建工程、直开图片时调用）。"""
    global _PROJECT_COLOR_OVERRIDES
    _PROJECT_COLOR_OVERRIDES = {}


def get_project_color_overrides() -> dict:
    return dict(_PROJECT_COLOR_OVERRIDES)


def collect_color_management_settings() -> dict:
    """收集当前用户全局偏好中的色彩管理设定，供保存进工程。

    保存工程时随 settings["color_management"] 落盘；ConfigManager 临时
    实例与主实例读同一配置文件（偏好页确认时已落盘）。

    AUD-32502：读取失败（ConfigManager 不可导入/构造、单键读取异常）
    必须留下日志线索——此前宽泛 ``except Exception`` 零日志，工程保存时
    色彩管理设定可能被静默丢弃，换机/交接后无从排查。
    """
    values: dict = {}
    try:
        from src.shared.config.config_manager import ConfigManager
        manager = ConfigManager()
    except (ImportError, OSError) as e:
        logger.warning(
            "collect_color_management_settings: ConfigManager unavailable "
            "(%s); saving project without color_management settings", e,
        )
        return values
    for key in COLOR_MANAGEMENT_SETTING_KEYS:
        try:
            value = manager.get(key)
        except Exception as e:  # noqa: BLE001 单键失败不拖垮整组，但留痕
            logger.warning(
                "collect_color_management_settings: failed to read %r: %s",
                key, e,
            )
            continue
        if value is not None:
            values[key] = value
    return values


def apply_project_color_context(color_management: dict) -> None:
    """M19.2 · 三页统一工程色彩上下文注入。

    工程加载/切换时由 Edit/Composite/Deliver 任一入口调用一次——把
    ``settings["color_management"]`` 载荷注入进程级覆盖层，使
    ``descriptor_from_config``（三页共用）读出同一工作/显示配置。
    空载荷等价清空（回退用户全局偏好）。三页注入同一函数、单点存
    储——防止各页自行读盘产生漂移。
    """
    set_project_color_overrides(color_management or {})


def descriptor_from_config(config_manager, scope: str, kind: str,
                           is_video: bool) -> ColorSpaceDescriptor:
    """Build a descriptor from color-management preference keys.

    Reads ``color.{scope}.{kind}_*`` keys (scope = "preview"|"output",
    kind = "timeline"|"image"). Project-level overrides
    (``set_project_color_overrides``) take precedence over the user's global
    preferences.

    AUD-32501: the fallback when primaries/transfer keys are missing is
    controlled by ``color.smart_default`` (the preference-page checkbox):

    - smart default **on** (default): infer the canonical descriptor from the
      source kind (timeline→Rec.709 video, image→sRGB still).
    - smart default **off**: the user opted out of guessing — fall back to
      the neutral scene-linear descriptor instead of silently applying the
      video/image inference. (Previously this key was persisted and offered
      in the UI but never read: a dead config that contradicted this
      docstring.)

    Args:
        config_manager: ConfigManager (or dict-like with .get).
        scope: "preview" or "output".
        kind: "timeline" or "image".
        is_video: whether the current source is video/timeline (for the
            smart-default fallback when keys are absent).

    Returns:
        A ColorSpaceDescriptor resolved from preferences.
    """
    def _cfg(key, default=None):
        if key in _PROJECT_COLOR_OVERRIDES:
            return _PROJECT_COLOR_OVERRIDES[key]
        if config_manager is None:
            return default
        try:
            return config_manager.get(key, default)
        except Exception:
            return default

    primaries = _cfg(f"color.{scope}.{kind}_primaries")
    transfer = _cfg(f"color.{scope}.{kind}_transfer")
    rng = _cfg(f"color.{scope}.{kind}_range")
    smart_default = _cfg("color.smart_default", True)

    # Smart-default fallback (AUD-32501): the flag now actually decides
    # between the source-kind inference and the neutral linear descriptor.
    if not primaries or not transfer:
        if smart_default:
            return descriptor_for_source_kind(is_video)
        return LINEAR_SCENE

    return ColorSpaceDescriptor(
        primaries=_enum_from_value(ColorPrimaries, primaries, ColorPrimaries.BT709),
        transfer=_enum_from_value(ColorTransfer, transfer, ColorTransfer.LINEAR),
        range=_enum_from_value(ColorRange, rng, ColorRange.FULL if kind == "image" else ColorRange.LIMITED),
    )


def available_primaries() -> list:
    """Color primaries offered in the preference UI.

    Stage 2: BT.709 (sRGB/Rec.709), Display P3 (modern wide-gamut displays),
    and BT.2020 (UHD/HDR). DCI-P3 (smpte431, digital cinema XYZ) is reserved.
    """
    return [ColorPrimaries.BT709, ColorPrimaries.P3_DISPLAY, ColorPrimaries.BT2020]


def available_transfers() -> list:
    """Transfer functions offered in the preference UI.

    P0-COLOR-04：补充 PQ/HLG —— 描述符与偏好页均已支持 HDR 传输函数，
    能力声明与 UI 保持一致（BT.2020+PQ=HDR10、BT.2020+HLG=广播 HDR）。
    """
    return [
        ColorTransfer.SRGB,    # sRGB curve (~2.2) — stills
        ColorTransfer.BT709,   # BT.709 OETF (~2.4) — video
        ColorTransfer.LINEAR,  # scene-linear (rare for preview/output)
        ColorTransfer.PQ,      # SMPTE ST 2084 — HDR10
        ColorTransfer.HLG,     # ARIB STD-B67 — HDR broadcast
    ]
