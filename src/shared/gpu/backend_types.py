"""P2-01: four independent backend dimensions (plan §1.2 / §3.2 / §4.1).

A single ``backend`` enum cannot express the real choice space; CUDA is a
compute/video ecosystem, not a windowing graphics backend.  Each dimension
is independently selectable and independently serializable, with ``auto``
as the neutral value and a ``force_`` prefix convention for regression and
diagnostics (plan §4.1).

Boundary rules: this module lives in ``src/shared/gpu`` and must not import
``src.features.*`` or ``src.app.*`` (module_boundary_rules.md).

Thread boundary: parsing/serialization here is pure string work with no
GL access and no shared state — safe to call from any thread.  The only
environmental read is ``os.environ`` in ``BackendSelection.from_env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional

FORCE_PREFIX = "force_"
ENV_PREFIX = "TOPOS_BACKEND_"


class GraphicsBackendKind(str, Enum):
    """Windowing/raster backend: textures, FBO/render targets, present."""

    AUTO = "auto"
    OPENGL = "opengl"
    METAL = "metal"
    D3D11 = "d3d11"
    D3D12 = "d3d12"

    @classmethod
    def default(cls) -> "GraphicsBackendKind":
        """Neutral default (``auto``) for this dimension."""
        return cls.AUTO


class ComputeBackendKind(str, Enum):
    """Image-compute backend: denoise, scopes, histograms, scaling."""

    AUTO = "auto"
    GLSL_FRAGMENT = "glsl_fragment"
    GLSL_COMPUTE = "glsl_compute"
    METAL_COMPUTE = "metal_compute"
    CUDA = "cuda"
    DIRECTML = "directml"
    CPU = "cpu"

    @classmethod
    def default(cls) -> "ComputeBackendKind":
        """Neutral default (``auto``) for this dimension."""
        return cls.AUTO


class DecodeBackendKind(str, Enum):
    """Compressed-video decode backend (plan §1.2 DecodeBackend)."""

    AUTO = "auto"
    CPU = "cpu"
    VIDEOTOOLBOX = "videotoolbox"
    NVDEC = "nvdec"
    D3D11VA = "d3d11va"
    QSV = "qsv"
    AMF = "amf"

    @classmethod
    def default(cls) -> "DecodeBackendKind":
        """Neutral default (``auto``) for this dimension."""
        return cls.AUTO


class InteropBackendKind(str, Enum):
    """Decode-surface <-> render-resource sharing backend (plan §3.3)."""

    AUTO = "auto"
    NONE = "none"
    IOSURFACE = "iosurface"
    CVMETAL = "cvmetal"
    D3D_SHARED = "d3d_shared"
    CUDA_INTEROP = "cuda_interop"

    @classmethod
    def default(cls) -> "InteropBackendKind":
        """Neutral default (``auto``) for this dimension."""
        return cls.AUTO


def parse_dimension(enum_cls, raw: Optional[str], *, dimension: str) -> tuple[Enum, bool]:
    """Parse one dimension string with diagnostics, never silent fallback.

    Accepts the enum values plus a ``force_`` prefix (regression/diagnostic
    convention, plan §4.1); the forced bit is returned separately so callers
    can report it.  Unknown values raise ``ValueError`` — per .clinerules a
    wrong configuration must surface, not silently downgrade.

    ``enum_cls`` is one of the four ``*BackendKind`` enums above (it must
    provide ``default()``); the return is ``(kind, forced)``.
    """
    if raw is None:
        return enum_cls.default(), False
    text = str(raw).strip().lower()
    forced = False
    if text.startswith(FORCE_PREFIX):
        forced = True
        text = text[len(FORCE_PREFIX):]
    try:
        return enum_cls(text), forced
    except ValueError:
        valid = ", ".join(v.value for v in enum_cls)
        raise ValueError(
            f"invalid {dimension} backend {raw!r}; expected one of: {valid}"
        ) from None


@dataclass(frozen=True)
class BackendSelection:
    """The four-dim selection actually requested for a session/operation."""

    graphics: GraphicsBackendKind = GraphicsBackendKind.AUTO
    compute: ComputeBackendKind = ComputeBackendKind.AUTO
    decode: DecodeBackendKind = DecodeBackendKind.AUTO
    interop: InteropBackendKind = InteropBackendKind.AUTO
    graphics_forced: bool = False
    compute_forced: bool = False
    decode_forced: bool = False
    interop_forced: bool = False

    def as_dict(self) -> dict[str, object]:
        """Serialize to plain strings (schema-versioned by caller)."""
        return {
            "graphics": self.graphics.value,
            "compute": self.compute.value,
            "decode": self.decode.value,
            "interop": self.interop.value,
            "forced": {
                "graphics": self.graphics_forced,
                "compute": self.compute_forced,
                "decode": self.decode_forced,
                "interop": self.interop_forced,
            },
        }

    def to_json(self) -> str:
        """Compact JSON form of :meth:`as_dict` (sorted keys, stable diff)."""
        import json
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "BackendSelection":
        """Inverse of :meth:`as_dict`; missing keys default to auto/unforced.

        The legacy ``forced`` mapping (bool per dimension) and the
        ``force_`` string prefix both set the forced bits; unknown values
        raise ``ValueError`` via :func:`parse_dimension`.
        """
        forced = data.get("forced") if isinstance(data.get("forced"), Mapping) else {}
        g, gf = parse_dimension(
            GraphicsBackendKind, data.get("graphics"), dimension="graphics")
        c, cf = parse_dimension(
            ComputeBackendKind, data.get("compute"), dimension="compute")
        d, df = parse_dimension(
            DecodeBackendKind, data.get("decode"), dimension="decode")
        i, iff = parse_dimension(
            InteropBackendKind, data.get("interop"), dimension="interop")
        return cls(
            graphics=g, compute=c, decode=d, interop=i,
            graphics_forced=gf or bool(forced.get("graphics")),
            compute_forced=cf or bool(forced.get("compute")),
            decode_forced=df or bool(forced.get("decode")),
            interop_forced=iff or bool(forced.get("interop")),
        )

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "BackendSelection":
        """Read ``TOPOS_BACKEND_GRAPHICS`` etc.; ``force_`` prefix honored."""
        values = os.environ if env is None else env
        return cls.from_dict({
            "graphics": values.get(f"{ENV_PREFIX}GRAPHICS"),
            "compute": values.get(f"{ENV_PREFIX}COMPUTE"),
            "decode": values.get(f"{ENV_PREFIX}DECODE"),
            "interop": values.get(f"{ENV_PREFIX}INTEROP"),
        })


def all_dimension_values() -> dict[str, list[str]]:
    """Serializable value space (for config schema docs and validation)."""
    return {
        "graphics": [v.value for v in GraphicsBackendKind],
        "compute": [v.value for v in ComputeBackendKind],
        "decode": [v.value for v in DecodeBackendKind],
        "interop": [v.value for v in InteropBackendKind],
    }
