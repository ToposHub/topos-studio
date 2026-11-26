"""OpenGL uniforms and shader functions for full-range video RGB."""

from __future__ import annotations

from typing import Any

from src.shared.color.camera_input import CAMERA_LOG_CURVES
from src.shared.color.color_space_descriptor import ACESCG_SCENE, ColorSpaceDescriptor
from src.shared.color.color_transform_provider import VideoSignalLuminance, video_signal_gamut_matrix
from src.shared.color.types import ColorRange, ColorTransfer


_TRANSFER_MODES = {
    "linear": 0,
    "gamma24": 1,
    "gamma26": 2,
    "iec61966_2_1": 3,
    "bt709": 4,
    "bt2020_10": 4,
    "bt2020_12": 5,
    "gamma22": 6,
    "gamma28": 7,
    "acescc": 8,
    "acescct": 9,
    "smpte2084": 10,
    "st2084": 10,
    "arib-std-b67": 11,
    "davinci-intermediate": 12,
    **{transfer.value: curve.mode for transfer, curve in CAMERA_LOG_CURVES.items()},
}


VIDEO_SIGNAL_GLSL = """
uniform int topos_video_src_transfer;
uniform int topos_video_dst_transfer;
uniform mat3 topos_video_gamut;
uniform bool topos_video_rgc;
uniform mat3 topos_video_compressed_gamut;
uniform vec3 topos_video_luminance;
"""

VIDEO_SIGNAL_TRANSFER_GLSL = """
const vec3 topos_video_hlg_weights = vec3(0.2627, 0.6780, 0.0593);
const float topos_video_pq_m1 = 0.1593017578125;
const float topos_video_pq_m2 = 78.84375;
const float topos_video_pq_c1 = 0.8359375;
const float topos_video_pq_c2 = 18.8515625;
const float topos_video_pq_c3 = 18.6875;

vec3 topos_video_signed_power(vec3 rgb, float exponent) {
    return sign(rgb) * pow(abs(rgb), vec3(exponent));
}

vec3 topos_video_decode(vec3 rgb, int transfer, vec3 luminance) {
    if (transfer == 1) {
        return topos_video_signed_power(rgb, 2.4);
    }
    if (transfer == 2) {
        return topos_video_signed_power(rgb, 2.6);
    }
    if (transfer == 3) {
        return mix(pow(max(rgb + 0.055, 0.0) / 1.055, vec3(2.4)),
                   rgb / 12.92, lessThanEqual(rgb, vec3(0.04045)));
    }
    if (transfer == 4 || transfer == 5) {
        float alpha = transfer == 4 ? 1.099 : 1.0993;
        float beta = transfer == 4 ? 0.018 : 0.0181;
        return mix(pow(max(rgb + (alpha - 1.0), 0.0) / alpha, vec3(1.0 / 0.45)),
                   rgb / 4.5, lessThan(rgb, vec3(4.5 * beta)));
    }
    if (transfer == 6 || transfer == 7) {
        return topos_video_signed_power(rgb, transfer == 6 ? 2.2 : 2.8);
    }
    if (transfer == 8) {
        float ceiling = log2(65504.0);
        vec3 power = min(exp2(min(rgb * 17.52 - 9.72, ceiling)), 65504.0);
        return mix(power, 2.0 * power - exp2(-15.0),
                   lessThan(rgb, vec3((-15.0 + 9.72) / 17.52)));
    }
    if (transfer == 9) {
        return mix(exp2(rgb * 17.52 - 9.72), (rgb - 0.0729055341958355) / 10.5402377416545,
                   lessThanEqual(rgb, vec3(0.155251141552511)));
    }
    if (transfer == 10) {
        vec3 v = pow(clamp(rgb, 0.0, 1.0), vec3(1.0 / topos_video_pq_m2));
        vec3 ratio = max(v - topos_video_pq_c1, 0.0) / (topos_video_pq_c2 - topos_video_pq_c3 * v);
        return pow(ratio, vec3(1.0 / topos_video_pq_m1)) * (10000.0 / luminance.x);
    }
    if (transfer == 11) {
        vec3 code = max(rgb, 0.0);
        vec3 scene = mix((exp((code - 0.55991073) / 0.17883277) + 0.28466892) / 12.0,
                         code * code / 3.0, lessThanEqual(code, vec3(0.5)));
        float y = dot(scene, topos_video_hlg_weights);
        float gain = y > 0.0 ? pow(y, luminance.z - 1.0) : 0.0;
        return scene * gain * (luminance.y / luminance.x);
    }
    if (transfer == 12) {
        return mix(exp2(rgb / 0.07329248 - 7.0) - 0.0075, rgb / 10.44426855,
                   lessThanEqual(rgb, vec3(0.02740668)));
    }
    __CAMERA_DECODE__
    return rgb;
}

vec3 topos_video_encode(vec3 rgb, int transfer, vec3 luminance) {
    if (transfer == 1) {
        return topos_video_signed_power(rgb, 1.0 / 2.4);
    }
    if (transfer == 2) {
        return topos_video_signed_power(rgb, 1.0 / 2.6);
    }
    if (transfer == 3) {
        return mix(1.055 * pow(max(rgb, 0.0), vec3(1.0 / 2.4)) - 0.055,
                   12.92 * rgb, lessThanEqual(rgb, vec3(0.0031308)));
    }
    if (transfer == 4 || transfer == 5) {
        float alpha = transfer == 4 ? 1.099 : 1.0993;
        float beta = transfer == 4 ? 0.018 : 0.0181;
        return mix(alpha * pow(max(rgb, 0.0), vec3(0.45)) - (alpha - 1.0),
                   4.5 * rgb, lessThan(rgb, vec3(beta)));
    }
    if (transfer == 6 || transfer == 7) {
        return topos_video_signed_power(rgb, transfer == 6 ? 1.0 / 2.2 : 1.0 / 2.8);
    }
    if (transfer == 8) {
        vec3 toe = (max(rgb, 0.0) + exp2(-15.0)) * 0.5;
        return (log2(mix(rgb, toe, lessThan(rgb, vec3(exp2(-15.0))))) + 9.72) / 17.52;
    }
    if (transfer == 9) {
        return mix((log2(max(rgb, 0.0078125)) + 9.72) / 17.52,
                   rgb * 10.5402377416545 + 0.0729055341958355,
                   lessThanEqual(rgb, vec3(0.0078125)));
    }
    if (transfer == 10) {
        vec3 v = pow(clamp(rgb * (luminance.x / 10000.0), 0.0, 1.0),
                     vec3(topos_video_pq_m1));
        vec3 pq = pow((topos_video_pq_c1 + topos_video_pq_c2 * v) /
                      (1.0 + topos_video_pq_c3 * v), vec3(topos_video_pq_m2));
        return mix(pq, vec3(0.0), lessThanEqual(rgb, vec3(0.0)));
    }
    if (transfer == 11) {
        vec3 display = max(rgb * (luminance.x / luminance.y), 0.0);
        float y = dot(display, topos_video_hlg_weights);
        float gain = y > 0.0 ? pow(y, 1.0 / luminance.z - 1.0) : 0.0;
        vec3 scene = display * gain;
        return mix(0.17883277 * log(max(12.0 * scene - 0.28466892, 1e-10)) + 0.55991073,
                   sqrt(3.0 * scene), lessThanEqual(scene, vec3(1.0 / 12.0)));
    }
    if (transfer == 12) {
        return mix(0.07329248 * (log2(max(rgb, 0.00262409) + 0.0075) + 7.0),
                   rgb * 10.44426855, lessThanEqual(rgb, vec3(0.00262409)));
    }
    __CAMERA_ENCODE__
    return rgb;
}
"""

VIDEO_SIGNAL_TRANSFER_GLSL = VIDEO_SIGNAL_TRANSFER_GLSL.replace("__CAMERA_DECODE__", "\n".join(
    f"""if (transfer == {curve.mode}) {{
        vec3 linear = (exp2((rgb - ({curve.log_offset})) / {curve.log_slope})
                      - {curve.linear_offset}) / {curve.linear_slope};
        vec3 toe = (rgb - {curve.encoded_break}) / {curve.toe_slope} + ({curve.linear_break});
        return mix(linear, toe, lessThan(rgb, vec3({curve.encoded_break})));
    }}""" for curve in CAMERA_LOG_CURVES.values()
)).replace("__CAMERA_ENCODE__", "\n".join(
    f"""if (transfer == {curve.mode}) {{
        vec3 encoded = {curve.log_slope} * log2(max(rgb * {curve.linear_slope} + {curve.linear_offset}, 1e-10))
                       + ({curve.log_offset});
        vec3 toe = (rgb - ({curve.linear_break})) * {curve.toe_slope} + {curve.encoded_break};
        return mix(encoded, toe, lessThan(rgb, vec3({curve.linear_break})));
    }}""" for curve in CAMERA_LOG_CURVES.values()
))

VIDEO_SIGNAL_GLSL += VIDEO_SIGNAL_TRANSFER_GLSL + """
vec3 topos_aces_reference_compression(vec3 ap1) {
    float achromatic = max(ap1.r, max(ap1.g, ap1.b));
    if (achromatic == 0.0) return ap1;
    // ACES 1.3 Reference Gamut Compression in scene-linear AP1.
    const vec3 threshold = vec3(0.815, 0.803, 0.880);
    const vec3 scale = vec3(0.327301919, 0.285938054, 0.146821454);
    vec3 distance = (achromatic - ap1) / abs(achromatic);
    vec3 excess = max(distance - threshold, 0.0);
    vec3 compressed = threshold + excess / pow(1.0 + pow(excess / scale, vec3(1.2)),
                                               vec3(1.0 / 1.2));
    return achromatic - min(distance, compressed) * abs(achromatic);
}

vec3 topos_convert_video_signal(vec3 rgb) {
    vec3 linear_rgb = topos_video_decode(rgb, topos_video_src_transfer, topos_video_luminance);
    linear_rgb = topos_video_gamut * linear_rgb;
    if (topos_video_rgc) {
        linear_rgb = topos_video_compressed_gamut * topos_aces_reference_compression(linear_rgb);
    }
    return topos_video_encode(linear_rgb, topos_video_dst_transfer, topos_video_luminance);
}
"""

# Scalar/vector arithmetic is shared. GLSL's boolean mix and explicit relational
# built-ins map to Metal select and vector comparison overloads.
VIDEO_SIGNAL_TRANSFER_MSL = (
    "\nstatic inline bool3 topos_less_equal(float3 a, float3 b) { return a <= b; }\n"
    "static inline bool3 topos_less(float3 a, float3 b) { return a < b; }\n"
    + VIDEO_SIGNAL_TRANSFER_GLSL.replace("vec3", "float3")
    .replace("const ", "constant ")
    .replace("\nfloat3 topos_video_", "\nstatic inline float3 topos_video_")
    .replace("lessThanEqual(", "topos_less_equal(")
    .replace("lessThan(", "topos_less(")
    .replace("mix(", "select(")
)


def can_render_video_signal_gl(source: ColorSpaceDescriptor, destination: ColorSpaceDescriptor) -> bool:
    """Whether the shader can render the full-range descriptors."""
    if (source.range != ColorRange.FULL or destination.range != ColorRange.FULL
            or source.transfer.value not in _TRANSFER_MODES
            or destination.transfer.value not in _TRANSFER_MODES):
        return False
    try:
        video_signal_gamut_matrix(source.primaries, destination.primaries)
    except ValueError:
        return False
    return True


def video_signal_uniform_values(
    source: ColorSpaceDescriptor, destination: ColorSpaceDescriptor,
) -> tuple[int, int, tuple[float, ...]]:
    """Return transfer modes and the shared CPU gamut matrix in GL column order."""
    if not can_render_video_signal_gl(source, destination):
        raise ValueError(f"Unsupported OpenGL video signal transform: {source} to {destination}")
    source_transfer = ColorTransfer.LINEAR if source == destination else source.transfer
    destination_transfer = ColorTransfer.LINEAR if source == destination else destination.transfer
    gamut = video_signal_gamut_matrix(source.primaries, destination.primaries)
    return (_TRANSFER_MODES[source_transfer.value], _TRANSFER_MODES[destination_transfer.value],
            tuple(gamut.T.ravel().tolist()))


def bind_video_signal_transform(
    program: Any, source: ColorSpaceDescriptor, destination: ColorSpaceDescriptor,
    *, reference_gamut_compression: bool = False, luminance: VideoSignalLuminance | None = None,
) -> None:
    """Upload the video signal transform to a ModernGL program."""
    if reference_gamut_compression:
        _, destination_mode, compressed_gamut = video_signal_uniform_values(ACESCG_SCENE, destination)
        source_mode, _, gamut = video_signal_uniform_values(source, ACESCG_SCENE)
        program["topos_video_compressed_gamut"].value = compressed_gamut
    else:
        source_mode, destination_mode, gamut = video_signal_uniform_values(source, destination)
    program["topos_video_rgc"].value = reference_gamut_compression
    program["topos_video_src_transfer"].value = source_mode
    program["topos_video_dst_transfer"].value = destination_mode
    program["topos_video_gamut"].value = gamut
    if "topos_video_luminance" in program:
        program["topos_video_luminance"].value = video_signal_luminance_uniform(luminance)


def video_signal_luminance_uniform(luminance: VideoSignalLuminance | None) -> tuple[float, float, float]:
    if luminance is None:
        return 100.0, 1000.0, 1.2
    return (luminance.reference_white_nits, luminance.hlg_peak_nits or 1000.0,
            luminance.hlg_system_gamma or 1.2)
