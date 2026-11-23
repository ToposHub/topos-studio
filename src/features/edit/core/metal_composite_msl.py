"""MSL of the Metal Edit composite (plan task 2.6).

The GL preview renders every layer into an 8-bit track FBO and then blends
the track textures bottom-up with ping-pong scratch targets
(``EditGpuPreviewRenderer._composite_tracks``).  The kernels below reproduce
that structure pass by pass on Metal textures, so a composited frame matches
the GL picture to rounding:

* ``edit_layer_*`` — one source (NV12 planes, or a solid colour) into a
  ``rgba8unorm`` track texture at the output size, with the clip transform
  applied as the inverse of the GL vertex quad deformation and the crop
  matte of ``crop_render`` on top; ``edit_crop_matte_pass`` is that matte
  alone, for a solid that GL crops after clearing its track FBO.
* ``edit_track_lut`` — the serial 3D LUT grade of one track texture.
* ``edit_composite_first`` / ``edit_composite_blend`` — the source-over of
  ``_BLEND_FRAGMENT_SHADER_SOURCE`` with the modes of ``shaders/blend.glsl``.
* ``edit_dip_to_color`` — the full-frame colour mix of the ``dip_to_color``
  transition.

Orientation: every texture here is in image space — row 0 is the top of the
picture, as in the native present slot and in the IOSurface the GL viewer
blits.  The GL track FBO stores the same picture upside down;
``inverse_quad_map`` of ``metal_edit_compositor.py`` already includes that
orientation, so the kernels only ever see image coordinates.
"""

from __future__ import annotations

from src.features.color.core.shader_nodes_msl import MSL_LUT3D


EDIT_COMPOSITE_MSL = """
#include <metal_stdlib>
using namespace metal;
""" + MSL_LUT3D + """

constexpr sampler edit_sampler(coord::normalized, filter::linear, address::clamp_to_edge);

// One layer's constants.  The rows map an output pixel centre, in the GL
// track-FBO clip space of the undeformed quad (x right, y down the image,
// [-1, 1]), to the quad's own texcoord space:
//   M⁻¹ · ndc -> (dot(row0.xy, ndc) + row0.z, dot(row1.xy, ndc) + row1.z)
struct edit_layer_params {
    float4 row0;
    float4 row1;
    float4 crop_rect;      // kept rect in image texcoords (x0, y0, x1, y1)
    float4 crop_feather;   // (u feather, v feather, 0, 0)
    int4 flags;            // x: quad coverage test, y: crop matte
};

// The texcoord of the layer under an output pixel; false where the deformed
// quad does not cover the pixel (the GL track FBO is cleared to zero there).
inline bool edit_layer_texcoord(
    constant edit_layer_params &p, uint2 px, uint2 size, thread float2 &tc) {
    float2 ndc = float2(2.0 * (float(px.x) + 0.5) / float(size.x) - 1.0,
                        2.0 * (float(px.y) + 0.5) / float(size.y) - 1.0);
    float2 quad = float2(dot(p.row0.xy, ndc) + p.row0.z,
                         dot(p.row1.xy, ndc) + p.row1.z);
    tc = quad * 0.5 + 0.5;
    if (p.flags.x != 0 && (any(tc < float2(0.0)) || any(tc > float2(1.0)))) {
        return false;
    }
    return true;
}

// crop_render.CROP_FRAGMENT_SHADER: the matte is measured in output
// coordinates, the same for every layer whatever its transform.
inline float edit_crop_alpha(uint2 px, uint2 size, constant edit_layer_params &p) {
    if (p.flags.y == 0) {
        return 1.0;
    }
    float2 tc = (float2(px) + 0.5) / float2(size);
    float4 r = p.crop_rect;
    if (p.crop_feather.x <= 0.0 || p.crop_feather.y <= 0.0) {
        float dx = min(tc.x - r.x, r.z - tc.x);
        float dy = min(tc.y - r.y, r.w - tc.y);
        return step(0.0, min(dx, dy));
    }
    float dx = min(tc.x - r.x, r.z - tc.x) / p.crop_feather.x;
    float dy = min(tc.y - r.y, r.w - tc.y) / p.crop_feather.y;
    float t = clamp(min(dx, dy), -1.0, 0.0) + 1.0;
    return t * t * (3.0 - 2.0 * t);
}

// A layer without a crop is written once, by the same float → 8 bit
// conversion of the texture store that the GL track FBO uses.
inline float4 edit_track_value(float4 color, float alpha) {
    if (alpha >= 1.0) {
        return color;
    }
    // The GL chain stores the layer in its track FBO before the crop pass
    // reads it: round to the 8 bits that FBO holds, then apply the
    // premultiplied matte the way that pass does.
    float4 stored = float4(rint(color.rgb * 255.0) / 255.0, color.a);
    return float4(stored.rgb * alpha, stored.a * alpha);
}

// Limited-range BT.709 NV12 to display RGB, as the GL YUV shader writes it.
inline float3 edit_nv12_rgb(
    texture2d<float> y_plane, texture2d<float> uv_plane, float2 tc) {
    float y = max((y_plane.sample(edit_sampler, tc).r - 16.0 / 255.0) * (255.0 / 219.0), 0.0);
    float2 uv = (uv_plane.sample(edit_sampler, tc).rg - 128.0 / 255.0) * (255.0 / 224.0);
    float3 color = float3(y + 1.5748 * uv.y,
                          y - 0.1873 * uv.x - 0.4681 * uv.y,
                          y + 1.8556 * uv.x);
    return clamp(color, 0.0, 1.0);
}

kernel void edit_layer_nv12(
    texture2d<float> y_plane [[texture(0)]],
    texture2d<float> uv_plane [[texture(1)]],
    texture2d<float, access::write> track [[texture(2)]],
    constant edit_layer_params &p [[buffer(0)]],
    uint2 px [[thread_position_in_grid]]) {
    uint2 size = uint2(track.get_width(), track.get_height());
    if (any(px >= size)) return;
    float2 tc;
    if (!edit_layer_texcoord(p, px, size, tc)) {
        track.write(float4(0.0), px);
        return;
    }
    float4 color = float4(edit_nv12_rgb(y_plane, uv_plane, tc), 1.0);
    track.write(edit_track_value(color, edit_crop_alpha(px, size, p)), px);
}

// A solid is the colour its GL track FBO is cleared with (buffer(1)); with a
// transform the passthrough pass leaves the same transparent surround as a
// video layer.  The store does the 8 bit conversion, as the clear does.
kernel void edit_layer_solid(
    texture2d<float, access::write> track [[texture(0)]],
    constant edit_layer_params &p [[buffer(0)]],
    constant float4 &color [[buffer(1)]],
    uint2 px [[thread_position_in_grid]]) {
    uint2 size = uint2(track.get_width(), track.get_height());
    if (any(px >= size)) return;
    float2 tc;
    if (!edit_layer_texcoord(p, px, size, tc)) {
        track.write(float4(0.0), px);
        return;
    }
    track.write(color, px);
}

// The crop matte as its own pass, over a layer that is already stored in its
// track texture (the GL order for solids: clear, transform, crop).
kernel void edit_crop_matte_pass(
    texture2d<float, access::read> source [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    constant edit_layer_params &p [[buffer(0)]],
    uint2 px [[thread_position_in_grid]]) {
    uint2 size = uint2(out.get_width(), out.get_height());
    if (any(px >= size)) return;
    float4 color = source.read(px);
    float alpha = edit_crop_alpha(px, size, p);
    out.write(float4(color.rgb * alpha, color.a * alpha), px);
}

// Serial 3D LUT over one track texture.  The input is already 8 bit, so no
// further quantisation is needed before the lookup (the single-clip fused
// kernel rounds there because it converts from NV12 in the same pass).
kernel void edit_track_lut(
    texture2d<float, access::read> source [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    texture3d<float> lut [[texture(2)]],
    constant float &strength [[buffer(0)]],
    uint2 px [[thread_position_in_grid]]) {
    if (px.x >= out.get_width() || px.y >= out.get_height()) return;
    float4 color = source.read(px);
    float3 grade = msl_apply_3dlut(color.rgb, lut, edit_sampler, strength);
    out.write(float4(clamp(grade, 0.0, 1.0), color.a), px);
}

// ---- blend.glsl -------------------------------------------------------

inline float edit_blend_lum(float3 c) {
    return dot(c, float3(0.3, 0.59, 0.11));
}

inline float3 edit_blend_clip_color(float3 c) {
    float l = edit_blend_lum(c);
    float mn = min(c.r, min(c.g, c.b));
    float mx = max(c.r, max(c.g, c.b));
    if (mn < 0.0) {
        c = l + (c - l) * l / max(l - mn, 1e-6);
    }
    if (mx > 1.0) {
        c = l + (c - l) * (1.0 - l) / max(mx - l, 1e-6);
    }
    return c;
}

inline float3 edit_blend_set_lum(float3 c, float l) {
    return edit_blend_clip_color(c + (l - edit_blend_lum(c)));
}

inline float edit_blend_sat(float3 c) {
    return max(c.r, max(c.g, c.b)) - min(c.r, min(c.g, c.b));
}

inline float3 edit_blend_set_sat(float3 c, float s) {
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    float md = c.r + c.g + c.b - mx - mn;
    float denom = max(mx - mn, 1e-6);
    float3 sorted_new = float3(0.0, (mx > mn) ? (md - mn) * s / denom : 0.0, s);
    bool3 is_max = (c == float3(mx));
    bool3 is_min = (c == float3(mn));
    float3 out_c;
    out_c.r = is_max.r ? sorted_new.b : (is_min.r ? sorted_new.r : sorted_new.g);
    out_c.g = is_max.g ? sorted_new.b : (is_min.g ? sorted_new.r : sorted_new.g);
    out_c.b = is_max.b ? sorted_new.b : (is_min.b ? sorted_new.r : sorted_new.g);
    return out_c;
}

inline float3 edit_blend_channel(float3 src, float3 dst, int mode) {
    if (mode == 1) {
        return src * dst;
    } else if (mode == 2) {
        return src + dst - src * dst;
    } else if (mode == 3) {
        return mix(2.0 * src * dst, 1.0 - 2.0 * (1.0 - src) * (1.0 - dst),
                   step(0.5, dst));
    } else if (mode == 4) {
        return min(src, dst);
    } else if (mode == 5) {
        return max(src, dst);
    } else if (mode == 6) {
        float3 dodged = min(float3(1.0), dst / max(float3(1.0) - src, float3(1e-6)));
        return mix(dodged, float3(1.0), step(1.0 - 1e-6, src));
    } else if (mode == 7) {
        float3 burned = max(float3(0.0), float3(1.0) - (float3(1.0) - dst) / max(src, float3(1e-6)));
        return mix(burned, float3(0.0), 1.0 - step(1e-6, src));
    } else if (mode == 8) {
        return mix(2.0 * src * dst, 1.0 - 2.0 * (1.0 - src) * (1.0 - dst),
                   step(0.5, src));
    } else if (mode == 9) {
        float3 g_low = ((16.0 * dst - 12.0) * dst + 4.0) * dst;
        float3 g_high = sqrt(max(dst, float3(0.0)));
        float3 g = mix(g_high, g_low, step(dst, float3(0.25)));
        return mix(dst - (1.0 - 2.0 * src) * dst * (1.0 - dst),
                   dst + (2.0 * src - 1.0) * (g - dst),
                   step(0.5, src));
    } else if (mode == 10) {
        return abs(dst - src);
    } else if (mode == 11) {
        return src + dst - 2.0 * src * dst;
    } else if (mode == 12) {
        return edit_blend_set_lum(edit_blend_set_sat(src, edit_blend_sat(dst)), edit_blend_lum(dst));
    } else if (mode == 13) {
        return edit_blend_set_lum(edit_blend_set_sat(dst, edit_blend_sat(src)), edit_blend_lum(dst));
    } else if (mode == 14) {
        return edit_blend_set_lum(src, edit_blend_lum(dst));
    } else if (mode == 15) {
        return edit_blend_set_lum(dst, edit_blend_lum(src));
    }
    return src;
}

// The bottom layer: the GL path draws it over a black texture with the blend
// mode forced to normal, so mix(0, layer.rgb, opacity * a) is layer.rgb * a.
kernel void edit_composite_first(
    texture2d<float, access::read> overlay [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    constant float &opacity [[buffer(0)]],
    uint2 px [[thread_position_in_grid]]) {
    if (px.x >= out.get_width() || px.y >= out.get_height()) return;
    float4 layer = overlay.read(px);
    out.write(float4(layer.rgb * (opacity * layer.a), 1.0), px);
}

kernel void edit_composite_blend(
    texture2d<float, access::read> base [[texture(0)]],
    texture2d<float, access::read> overlay [[texture(1)]],
    texture2d<float, access::write> out [[texture(2)]],
    constant int &blend_mode [[buffer(0)]],
    constant float &opacity [[buffer(1)]],
    uint2 px [[thread_position_in_grid]]) {
    if (px.x >= out.get_width() || px.y >= out.get_height()) return;
    float4 base_color = base.read(px);
    float4 layer = overlay.read(px);
    float3 mixed = edit_blend_channel(layer.rgb, base_color.rgb, blend_mode);
    float a = opacity * layer.a;
    out.write(float4(mix(base_color.rgb, mixed, a), 1.0), px);
}

// _DIP_FRAGMENT_SHADER_SOURCE: c.rgb * (1 - f) + dip_color * f, alpha kept.
// buffer(0) is (r, g, b, factor).
kernel void edit_dip_to_color(
    texture2d<float, access::read> source [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    constant float4 &dip [[buffer(0)]],
    uint2 px [[thread_position_in_grid]]) {
    if (px.x >= out.get_width() || px.y >= out.get_height()) return;
    float4 c = source.read(px);
    float factor = dip.w;
    out.write(float4(c.rgb * (1.0 - factor) + dip.rgb * factor, c.a), px);
}
"""
