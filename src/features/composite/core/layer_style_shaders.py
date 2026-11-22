"""Shared GLSL for the layer-style alpha mask and compositing passes."""

PSD_RGB_TRANSFER_GLSL = """
vec3 psdEncodeRgb(vec3 rgb) {
    vec3 magnitude = abs(rgb);
    vec3 encoded = mix(magnitude * 12.92,
                       1.055 * pow(magnitude, vec3(1.0 / 2.4)) - 0.055,
                       step(vec3(0.0031308), magnitude));
    return sign(rgb) * encoded;
}

vec3 psdDecodeRgb(vec3 rgb) {
    vec3 magnitude = abs(rgb);
    vec3 decoded = mix(magnitude / 12.92,
                       pow((magnitude + 0.055) / 1.055, vec3(2.4)),
                       step(vec3(0.04045), magnitude));
    return sign(rgb) * decoded;
}

vec3 psdEncodePremult(vec3 rgb, float alpha) {
    return alpha > 0.0 ? psdEncodeRgb(rgb / alpha) * alpha : rgb;
}

vec3 psdDecodePremult(vec3 rgb, float alpha) {
    return alpha > 0.0 ? psdDecodeRgb(rgb / alpha) * alpha : rgb;
}
"""

_STYLE_GAUSSIAN = """
    float sigma = max(radius * 0.45, 0.001);
    float extent = floor(radius + 0.5);
    float total = sampleMask(uv, channel);
    float weightSum = 1.0;
    if (radius <= 0.0) {
        return total;
    }
    for (int index = 1; index <= 128; ++index) {
        float distance = float(index);
        if (distance > extent) {
            break;
        }
        float scaled = distance / sigma;
        float weight = exp(-0.5 * scaled * scaled);
        vec2 offset = direction * distance;
        total += (sampleMask(uv - offset, channel) + sampleMask(uv + offset, channel)) * weight;
        weightSum += 2.0 * weight;
    }
    return total / weightSum;
"""

LAYER_STYLE_MASK_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_glow_radius;
uniform float u_shadow_radius;
uniform float u_glow_opacity;
uniform float u_shadow_opacity;
in vec2 v_uv;
out vec4 fragColor;

float sampleMask(vec2 uv, int channel) {
    if (any(lessThan(uv, vec2(0.0))) || any(greaterThanEqual(uv, vec2(1.0)))) {
        return 0.0;
    }
    return texture(u_texture, uv).a;
}

float gaussian(vec2 uv, vec2 direction, float radius, int channel) {
""" + _STYLE_GAUSSIAN + """
}

void main() {
    vec2 direction = vec2(1.0 / u_resolution.x, 0.0);
    float glow = u_glow_opacity > 0.0 ? gaussian(v_uv, direction, u_glow_radius, 0) : 0.0;
    float shadow = u_shadow_opacity > 0.0 ? gaussian(v_uv, direction, u_shadow_radius, 0) : 0.0;
    fragColor = vec4(glow, shadow, 0.0, 0.0);
}
"""

LAYER_STYLE_COMPOSITE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_style_masks;
uniform vec2 u_resolution;
uniform vec2 u_shadow_offset;
uniform vec3 u_overlay_color;
uniform vec3 u_glow_color;
uniform vec3 u_shadow_color;
uniform float u_overlay_opacity;
uniform float u_glow_opacity;
uniform float u_shadow_opacity;
uniform float u_glow_radius;
uniform float u_shadow_radius;
uniform bool u_premultiplied;
uniform bool u_psd_srgb;
uniform float u_glow_range;
in vec2 v_uv;
out vec4 fragColor;
""" + PSD_RGB_TRANSFER_GLSL + """

float sampleMask(vec2 uv, int channel) {
    if (any(lessThan(uv, vec2(0.0))) || any(greaterThanEqual(uv, vec2(1.0)))) {
        return 0.0;
    }
    return texture(u_style_masks, uv)[channel];
}

float gaussian(vec2 uv, vec2 direction, float radius, int channel) {
""" + _STYLE_GAUSSIAN + """
}

void layerUnder(inout vec4 foreground, vec3 color, float alpha) {
    float coverage = (1.0 - foreground.a) * alpha;
    foreground += vec4(color * coverage, coverage);
}

void main() {
    vec4 pixel = texture(u_texture, v_uv);
    if (!u_premultiplied) {
        pixel.rgb *= pixel.a;
    }
    vec3 overlayColor = u_overlay_color;
    vec3 glowColor = u_glow_color;
    vec3 shadowColor = u_shadow_color;
    if (u_psd_srgb) {
        pixel.rgb = psdEncodePremult(pixel.rgb, pixel.a);
        overlayColor = psdEncodeRgb(overlayColor);
        glowColor = psdEncodeRgb(glowColor);
        shadowColor = psdEncodeRgb(shadowColor);
    }
    pixel.rgb = mix(pixel.rgb, overlayColor * pixel.a, u_overlay_opacity);
    vec2 direction = vec2(0.0, 1.0 / u_resolution.y);
    if (u_glow_opacity > 0.0) {
        float glow = gaussian(v_uv, direction, u_glow_radius, 0);
        glow = clamp(glow / max(0.01, u_glow_range), 0.0, 1.0);
        layerUnder(pixel, glowColor, glow * u_glow_opacity);
    }
    if (u_shadow_opacity > 0.0) {
        float shadow = gaussian(v_uv - u_shadow_offset / u_resolution, direction, u_shadow_radius, 1);
        layerUnder(pixel, shadowColor, shadow * u_shadow_opacity);
    }
    if (u_psd_srgb) {
        pixel.rgb = psdDecodePremult(pixel.rgb, pixel.a);
    }
    if (!u_premultiplied && pixel.a > 0.0) {
        pixel.rgb /= pixel.a;
    }
    fragColor = pixel;
}
"""
