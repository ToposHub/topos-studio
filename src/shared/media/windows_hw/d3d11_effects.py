"""HLSL effect capability declarations for the D3D11 render graph.

These are source contracts for the native shader compiler.  They do not claim
that a machine has loaded the kernels; runtime coverage must come from the
native bridge and the real-material golden tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

__all__ = ["D3D11EffectSpec", "D3D11_EFFECTS", "d3d11_effect_capability"]


@dataclass(frozen=True)
class D3D11EffectSpec:
    effect_id: str
    shader_id: str
    variants: Tuple[str, ...]
    temporal: bool = False
    fallback_backend: str = "opengl"


_EFFECT_SPECS = (
    D3D11EffectSpec("blur", "d3d11.effect.blur", ("box", "gaussian", "bilateral")),
    D3D11EffectSpec("sharpen", "d3d11.effect.sharpen", ("unsharp",)),
    D3D11EffectSpec("glow", "d3d11.effect.glow", ("threshold", "soft")),
    D3D11EffectSpec("erode", "d3d11.effect.morphology", ("erode",)),
    D3D11EffectSpec("dilate", "d3d11.effect.morphology", ("dilate",)),
    D3D11EffectSpec("edge", "d3d11.effect.edge", ("sobel",)),
    D3D11EffectSpec("denoise", "d3d11.effect.denoise", ("spatial", "temporal"), temporal=True),
    D3D11EffectSpec("defocus", "d3d11.effect.defocus", ("gaussian", "disc")),
)
D3D11_EFFECTS = {item.effect_id: item for item in _EFFECT_SPECS}


def d3d11_effect_capability(effect_id: str, variant: str = "") -> dict[str, object]:
    """Return a product-facing capability declaration, never a fake success."""
    spec = D3D11_EFFECTS.get(effect_id.strip().lower())
    if spec is None:
        return {"supported": False, "effect": effect_id, "reason": "effect_not_declared"}
    variant_name = variant.strip().lower()
    if variant_name and variant_name not in spec.variants:
        return {
            "supported": False,
            "effect": spec.effect_id,
            "variant": variant_name,
            "reason": "effect_variant_not_declared",
        }
    return {
        "supported": True,
        "effect": spec.effect_id,
        "variant": variant_name or spec.variants[0],
        "shader_id": spec.shader_id,
        "temporal": spec.temporal,
        "fallback_backend": spec.fallback_backend,
        "runtime_gate": "native_d3d11_renderer_and_golden_test",
    }

