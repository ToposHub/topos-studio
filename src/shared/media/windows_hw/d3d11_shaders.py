"""Small, deterministic HLSL source snippets used by the native shader ABI.

The native bridge owns compilation and resource binding.  Keeping shader IDs
and source in a Python registry makes plan validation and golden-test coverage
independent of a particular vendor SDK.
"""

from __future__ import annotations

__all__ = ["D3D11_SHADER_SOURCES", "required_shader_ids"]

_COMMON = """
struct GradeConstants {
    float exposure;
    float contrast;
    float saturation;
    float3 lift;
    float3 gamma;
    float3 gain;
};
""".strip()

D3D11_SHADER_SOURCES: dict[str, str] = {
    "d3d11.decode.nv12_p010": _COMMON + "\nTexture2D<float> y_plane : register(t0);\nTexture2D<float2> uv_plane : register(t1);",
    "d3d11.resize.linear": _COMMON + "\nTexture2D<float4> source_tex : register(t0);",
    "d3d11.grade.primary": _COMMON + "\nTexture2D<float4> source_tex : register(t0);",
    "d3d11.output.nv12": _COMMON + "\nRWTexture2D<float> y_target : register(u0);\nRWTexture2D<float2> uv_target : register(u1);",
    "d3d11.output.p010": _COMMON + "\nRWTexture2D<uint> y_target : register(u0);\nRWTexture2D<uint> uv_target : register(u1);",
    "d3d11.output.rgba16f": _COMMON + "\nRWTexture2D<float4> rgba_target : register(u0);",
    "d3d11.composite.blend": _COMMON + "\nTexture2D<float4> layer_tex : register(t0);\nTexture2D<float4> accum_tex : register(t1);",
    "d3d11.transition.wipe": _COMMON + "\nTexture2D<float> mask_tex : register(t2);",
    "d3d11.transition.iris": _COMMON + "\nTexture2D<float> mask_tex : register(t2);",
    "d3d11.transition.slide": _COMMON + "\nTexture2D<float> mask_tex : register(t2);",
    "d3d11.transition.user": _COMMON + "\nTexture2D<float> mask_tex : register(t2);",
}

for _effect_name in ("blur", "sharpen", "glow", "morphology", "edge", "denoise", "defocus"):
    D3D11_SHADER_SOURCES[f"d3d11.effect.{_effect_name}"] = _COMMON + (
        f"\nTexture2D<float4> source_tex : register(t0);\n"
        f"// { _effect_name } variants are selected by a constant-buffer enum."
    )


def required_shader_ids() -> tuple[str, ...]:
    """Return the stable shader registry keys for native bridge validation."""
    return tuple(sorted(D3D11_SHADER_SOURCES))
