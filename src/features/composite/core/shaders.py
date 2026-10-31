"""
Composite shader source strings.

Shared by CompositeRenderer, CompositeExportRenderer, and GraphEvaluatorMixin.
Extracted here to avoid circular imports.

Compute shaders added for compute-intensive operations:
- Blur (large radius Gaussian blur using shared memory)
- Erode/Dilate (morphological operations using shared memory)
"""

# M05.2f：Composite topos_color 的调色数学与 Edit 预览/导出共用
# PRIMARY_GRADE（纯 GLSL 字符串模块，无依赖零循环）——不再维护平行数学。
from src.features.edit.core.primary_grade_shader import (  # noqa: F401
    PRIMARY_GRADE_BODY,
    PRIMARY_GRADE_UNIFORMS,
)

_PASSTHROUGH_VERTEX = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_uv = in_uv;
}
"""

_PASSTHROUGH_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;
void main() {
    fragColor = texture(u_texture, v_uv);
}
"""

_PASSTHROUGH_SUB_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_uv_scale;    // sampled sub-rect size in UV space
uniform vec2 u_uv_offset;   // sampled sub-rect origin in UV space
in vec2 v_uv;
out vec4 fragColor;
void main() {
    // P0-1 GPU tile stitch: sample only the output ROI of a padded tile
    // texture so tiles land in the full-frame canvas without a CPU
    // crop/stitch pass.
    fragColor = texture(u_texture, u_uv_offset + v_uv * u_uv_scale);
}
"""

_BLEND_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;   // A = foreground
uniform sampler2D u_texture_b;   // B = background
uniform sampler2D u_texture_mask; // optional mask
uniform vec2 u_resolution_a;     // A texture pixel dimensions
uniform vec2 u_resolution_b;     // B texture pixel dimensions
uniform vec2 u_resolution_mask;  // Mask texture pixel dimensions
uniform vec2 u_output_resolution; // Output canvas pixel dimensions
uniform vec4 u_output_bbox;      // Output region (x, y, width, height); all layer
                                 // bboxes below are expressed in this space
uniform vec4 u_bbox_a;           // A bbox in output pixel space: x, y, width, height
uniform vec4 u_bbox_b;           // B bbox in output pixel space
uniform vec4 u_bbox_mask;        // Mask bbox in output pixel space
uniform float u_opacity;
uniform int u_blend_mode;
uniform bool u_a_straight;       // true if A is straight alpha (S2-B)
uniform bool u_b_straight;       // true if B is straight alpha (S2-B)
uniform bool u_has_mask;         // whether mask texture is bound
uniform bool u_use_bbox_mapping; // true if bbox mapping should be applied
uniform bool u_alpha_clamp;      // S2-B: clamp alpha in add/subtract modes
uniform bool u_output_unpremult; // S2-B: unpremultiply output to straight alpha
uniform float u_mix;             // Mix control: 0=fully background B, 1=full merge result
uniform int u_mask_channel;      // Mask channel selection: 0=luma, 1=R, 2=G, 3=B, 4=A
uniform bool u_mask_invert;      // Invert mask value
in vec2 v_uv;
out vec4 fragColor;

// S2-A: Sample a layer with bbox mapping support.
// Maps output pixel coordinates to input texture UV space.
// If the output pixel is outside the input's bbox, returns transparent.
//
// Args:
//   tex: Input texture sampler
//   out_uv: Output UV coordinates (normalized 0-1 across the render target)
//   bbox: Input bbox (x, y, width, height) in the output region's space
//   resolution: Input texture pixel dimensions
//   output_bbox: Output region (x, y, width, height) in the same space as
//     bbox; the uploader zeroes its origin and shifts layer bboxes to
//     match, so out_uv * output_bbox.zw lands in that shared space.
//
// Returns:
//   Sampled color, or transparent (0.0, 0.0, 0.0, 0.0) if outside bbox
vec4 sampleLayer(sampler2D tex, vec2 out_uv, vec4 bbox, vec2 resolution, vec4 output_bbox) {
    // Convert output UV to output pixel coordinates
    vec2 out_pixel = out_uv * output_bbox.zw;

    // Compute local pixel coordinates relative to bbox origin
    vec2 local = out_pixel - bbox.xy;

    // Check if pixel is inside bbox (bbox.zw = width, height)
    if (local.x < 0.0 || local.y < 0.0 || local.x >= bbox.z || local.y >= bbox.w) {
        return vec4(0.0);
    }

    // Map local pixel to input texture UV
    // Use half-pixel offset for correct sampling
    vec2 uv = (local + vec2(0.5)) / resolution;

    return texture(tex, uv);
}

// Legacy: Sample a texture at its natural position within the output canvas.
// The input keeps its original pixel dimensions and is placed at the origin
// (bottom-left) of the output canvas.  Pixels outside the input area are
// transparent black.  No auto-fit / stretch.
// NOTE: This function is kept for backward compatibility with old graphs
// that don't have bbox information.
vec4 sampleAtOrigin(sampler2D tex, vec2 uv, vec2 tex_res) {
    vec2 src_uv = uv * u_output_resolution / tex_res;

    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        return vec4(0.0);
    }

    return texture(tex, src_uv);
}

// RGB <-> HSV conversion for HSL blend modes (hue/saturation/color/luminosity).
// Using Rec.709 luma coefficients via the standard RGB<->HSV transform.
vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    // S2-A: Sample A and B with bbox mapping if available
    vec4 a;
    vec4 b;
    vec4 m;

    if (u_use_bbox_mapping) {
        // Use new bbox-based sampling for correct spatial alignment
        a = sampleLayer(u_texture_a, v_uv, u_bbox_a, u_resolution_a, u_output_bbox);
        b = sampleLayer(u_texture_b, v_uv, u_bbox_b, u_resolution_b, u_output_bbox);
        if (u_has_mask) {
            m = sampleLayer(u_texture_mask, v_uv, u_bbox_mask, u_resolution_mask, u_output_bbox);
        }
    } else {
        // Legacy: sample at origin (backward compatibility)
        a = sampleAtOrigin(u_texture_a, v_uv, u_resolution_a);
        b = sampleAtOrigin(u_texture_b, v_uv, u_resolution_b);
        if (u_has_mask) {
            m = texture(u_texture_mask, v_uv);
        }
    }

    // Convert each input to premultiplied independently (in-shader, no temp textures)
    if (u_a_straight) { a.rgb *= a.a; }
    if (u_b_straight) { b.rgb *= b.a; }

    // Compute mask modulation with explicit channel selection (S3-A: replaces
    // the old ambiguous "m.a > 0 ? m.a : m.r" heuristic).
    //   u_mask_channel: 0=luma, 1=R, 2=G, 3=B, 4=A
    //   u_mask_invert:  invert the selected mask value
    float mask_val = 1.0;
    if (u_has_mask) {
        if (u_mask_channel == 0) {
            // Rec.709 luma
            mask_val = dot(m.rgb, vec3(0.2126, 0.7152, 0.0722));
        } else if (u_mask_channel == 1) {
            mask_val = m.r;
        } else if (u_mask_channel == 2) {
            mask_val = m.g;
        } else if (u_mask_channel == 3) {
            mask_val = m.b;
        } else {
            mask_val = m.a;
        }
        if (u_mask_invert) {
            mask_val = 1.0 - mask_val;
        }
    }
    float effect = u_opacity * mask_val;

    // BUG-4 fix: matte/replace modes (copy/stencil/mask) only modulate alpha,
    // not RGB. Standard composite modes modulate both alpha and RGB in
    // premultiplied space as before.
    vec4 fg;
    if (u_blend_mode == 12 || u_blend_mode == 13 || u_blend_mode == 14) {
        // copy / stencil / mask: opacity affects only effective alpha
        fg = vec4(a.rgb, a.a * effect);
    } else {
        fg = vec4(a.rgb * effect, a.a * effect);
    }

    vec3 blended_rgb;
    float out_alpha;

    // For color-blend modes: unpremult to straight, blend, then re-composite.
    // This avoids the darkening artifact on semi-transparent premultiplied pixels.
    vec3 fg_col = fg.a > 0.00001 ? fg.rgb / fg.a : vec3(0.0);
    vec3 bg_col = b.a > 0.00001 ? b.rgb / b.a : vec3(0.0);

    // Blend mode indices must match BLEND_MODES list in blend_node.py
    switch (u_blend_mode) {
        case 0:  // over — A over B (Porter-Duff)
            blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break;
        case 1:  // under — B over A
            blended_rgb = b.rgb + fg.rgb * (1.0 - b.a);
            out_alpha = b.a + fg.a * (1.0 - b.a);
            break;
        case 2:  // add — A + B (no clamping for HDR)
            blended_rgb = b.rgb + fg.rgb;
            out_alpha = b.a + fg.a;
            break;
        case 3:  // subtract — B.rgb - A.rgb, alpha keeps background (NUKE minus)
            blended_rgb = b.rgb - fg.rgb;
            out_alpha = b.a;
            break;
        case 4:  // multiply — unpremult, blend straight, re-composite
            { vec3 col = fg_col * bg_col;
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 5:  // screen
            { vec3 col = 1.0 - (1.0 - fg_col) * (1.0 - bg_col);
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 6:  // overlay
            { vec3 col = mix(
                  2.0 * bg_col * fg_col,
                  1.0 - 2.0 * (1.0 - bg_col) * (1.0 - fg_col),
                  step(0.5, bg_col));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 7:  // difference
            { vec3 col = abs(bg_col - fg_col);
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 8:  // divide (BUG-6 fix: clamp half-float upper bound)
            { vec3 col = bg_col / max(fg_col, vec3(0.00001));
              col = min(col, vec3(65504.0));  // prevent Inf/NaN, half-float max
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 9:  // min
            { vec3 col = min(fg_col, bg_col);
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 10: // max
            { vec3 col = max(fg_col, bg_col);
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 11: // average
            { vec3 col = (fg_col + bg_col) * 0.5;
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 12: // copy — just A, ignore B
            blended_rgb = fg.rgb;
            out_alpha = fg.a;
            break;
        case 13: // stencil — use A's alpha to cut B
            blended_rgb = b.rgb * fg.a;
            out_alpha = b.a * fg.a;
            break;
        case 14: // mask — use B's alpha to mask A
            blended_rgb = fg.rgb * b.a;
            out_alpha = fg.a * b.a;
            break;
        case 15: // in — A only where B is opaque (Porter-Duff A in B)
            blended_rgb = fg.rgb * b.a;
            out_alpha = fg.a * b.a;
            break;
        case 16: // out — A only where B is transparent (Porter-Duff A held out by B)
            blended_rgb = fg.rgb * (1.0 - b.a);
            out_alpha = fg.a * (1.0 - b.a);
            break;
        case 17: // atop — A in B, plus B outside A (Porter-Duff A atop B)
            blended_rgb = fg.rgb * b.a + b.rgb * (1.0 - fg.a);
            out_alpha = b.a;
            break;
        case 18: // xor — A or B, but not both (Porter-Duff A xor B)
            blended_rgb = fg.rgb * (1.0 - b.a) + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a * (1.0 - b.a) + b.a * (1.0 - fg.a);
            break;
        case 19: // color-dodge — lighten bg by fg (Photoshop)
            { // bg / (1 - fg); where fg is opaque white (1.0), result is clamped to max
              vec3 col = bg_col / max(1.0 - fg_col, vec3(0.00001));
              col = min(col, vec3(65504.0));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 20: // color-burn — darken bg by fg (Photoshop)
            { // 1 - (1-bg)/fg; where fg is opaque black (0.0), result is clamped to 0
              vec3 col = 1.0 - (1.0 - bg_col) / max(fg_col, vec3(0.00001));
              col = max(col, vec3(0.0));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 21: // soft-light — W3C standard formula (Photoshop)
            { vec3 col = mix(
                  bg_col - (1.0 - 2.0 * fg_col) * bg_col * (1.0 - bg_col),
                  bg_col + (2.0 * fg_col - 1.0) * (sqrt(max(bg_col, 0.0)) - bg_col),
                  step(0.5, fg_col));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 22: // hard-light — overlay with fg/bg swapped
            { vec3 col = mix(
                  2.0 * fg_col * bg_col,
                  1.0 - 2.0 * (1.0 - fg_col) * (1.0 - bg_col),
                  step(0.5, fg_col));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 23: // linear-light — burn/dodge combined (Photoshop)
            { vec3 col = bg_col + 2.0 * fg_col - 1.0;
              col = clamp(col, vec3(0.0), vec3(65504.0));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 24: // hue — use fg hue, bg sat & value
            { vec3 fg_hsv = rgb2hsv(fg_col);
              vec3 bg_hsv = rgb2hsv(bg_col);
              vec3 col = hsv2rgb(vec3(fg_hsv.x, bg_hsv.y, bg_hsv.z));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 25: // saturation — use bg hue, fg sat, bg value
            { vec3 fg_hsv = rgb2hsv(fg_col);
              vec3 bg_hsv = rgb2hsv(bg_col);
              vec3 col = hsv2rgb(vec3(bg_hsv.x, fg_hsv.y, bg_hsv.z));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 26: // color — use fg hue & sat, bg value
            { vec3 fg_hsv = rgb2hsv(fg_col);
              vec3 bg_hsv = rgb2hsv(bg_col);
              vec3 col = hsv2rgb(vec3(fg_hsv.x, fg_hsv.y, bg_hsv.z));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        case 27: // luminosity — use bg hue & sat, fg value
            { vec3 fg_hsv = rgb2hsv(fg_col);
              vec3 bg_hsv = rgb2hsv(bg_col);
              vec3 col = hsv2rgb(vec3(bg_hsv.x, bg_hsv.y, fg_hsv.z));
              blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
              out_alpha = fg.a + b.a * (1.0 - fg.a); }
            break;
        // ── CP-012: Nuke 补齐（索引须与 BLEND_MODES 一致）──
        case 28: // from — A.rgb - B.rgb（subtract 的镜像）
            blended_rgb = fg.rgb - b.rgb;
            out_alpha = clamp(fg.a, 0.0, 1.0);
            break;
        case 29: // exclusion — A + B - 2AB（软差值，HDR 不截断）
            blended_rgb = fg.rgb + b.rgb - 2.0 * fg.rgb * b.rgb;
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break;
        case 30: // conjoint — 并集，覆盖率封顶 1（Nuke conjoint）
            { float a_sum = fg.a + b.a;
              float a_out = min(a_sum, 1.0);
              blended_rgb = fg.rgb + b.rgb
                          * clamp((a_out - fg.a) / max(b.a, 1e-6), 0.0, 1.0);
              out_alpha = a_out; }
            break;
        case 31: // disjoint — 不重叠并集（alpha 和 ≤1 时直接相加）
            { float a_sum2 = fg.a + b.a;
              if (a_sum2 <= 1.0) {
                  blended_rgb = fg.rgb + b.rgb;
                  out_alpha = a_sum2;
              } else {
                  blended_rgb = fg.rgb + b.rgb
                             * clamp((1.0 - fg.a) / max(b.a, 1e-6), 0.0, 1.0);
                  out_alpha = max(fg.a, b.a);
              } }
            break;
        default:
            blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
    }

    // S2-B: Alpha clamping for add/subtract modes (preserve HDR color, clamp alpha)
    // Only apply alpha clamp for modes that can produce negative/overflow alpha
    // Modes 2 (add) and 3 (subtract) can produce alpha outside [0, 1]
    if (u_alpha_clamp) {
        out_alpha = clamp(out_alpha, 0.0, 1.0);
    }

    // S3-A: Mix control — blend between background B (original) and the merge
    // result by u_mix * mask_val. mix=0 returns pure B, mix=1 returns full
    // merge result. Independent from opacity (opacity modulates the foreground,
    // mix controls the overall effect strength against the background).
    float mix_amount = clamp(u_mix * mask_val, 0.0, 1.0);
    blended_rgb = mix(b.rgb, blended_rgb, mix_amount);
    out_alpha = mix(b.a, out_alpha, mix_amount);

    // S2-B: Output unpremultiply for alpha_policy="output_straight"
    // Convert premultiplied result to straight alpha
    if (u_output_unpremult && out_alpha > 0.00001) {
        blended_rgb = blended_rgb / out_alpha;
    }

    fragColor = vec4(blended_rgb, out_alpha);
}
"""

_BLUR_H_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_radius;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 64;

void main() {
    if (u_radius < 0.5) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    float sigma = max(u_radius * 0.5, 0.1);
    int kernel_size = min(int(ceil(sigma * 3.0)), MAX_KERNEL);
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;

    for (int i = -MAX_KERNEL; i <= MAX_KERNEL; i++) {
        if (abs(i) > kernel_size) continue;
        float x = float(i);
        float weight = exp(-(x * x) / (2.0 * sigma * sigma));
        vec2 offset = vec2(x / u_resolution.x, 0.0);
        color += texture(u_texture, v_uv + offset) * weight;
        weight_sum += weight;
    }

    fragColor = color / weight_sum;
}
"""

_BLUR_V_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_radius;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 64;

void main() {
    if (u_radius < 0.5) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    float sigma = max(u_radius * 0.5, 0.1);
    int kernel_size = min(int(ceil(sigma * 3.0)), MAX_KERNEL);
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;

    for (int i = -MAX_KERNEL; i <= MAX_KERNEL; i++) {
        if (abs(i) > kernel_size) continue;
        float y = float(i);
        float weight = exp(-(y * y) / (2.0 * sigma * sigma));
        vec2 offset = vec2(0.0, y / u_resolution.y);
        color += texture(u_texture, v_uv + offset) * weight;
        weight_sum += weight;
    }

    fragColor = color / weight_sum;
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Multi-type Blur shaders (Box / Gaussian / Bilateral / Motion / Bokeh)
# ─────────────────────────────────────────────────────────────────────────────
# Shared conventions across all blur shaders:
#   u_channel_mask : vec4  — per-channel enable (1.0 = blur, 0.0 = passthrough)
#   u_mix          : float — blend between blurred and original (1.0 = full blur)
#   u_edge_policy  : int   — 0=clamp, 1=wrap, 2=mirror
# The separable H/V pair only applies channel mask on the V (final) pass.
# ─────────────────────────────────────────────────────────────────────────────

# Edge-policy sampling is defined as a `sampleEdge()` function inside each
# single-pass shader (BILATERAL / MOTION / BOKEH) to keep them self-contained.
# The separable V pass also defines its own sampleEdge().

_BLUR_SEPARABLE_H_FRAGMENT = """
#version 330
// Separable blur — horizontal pass.
// u_kernel_mode: 0 = box (uniform), 1 = gaussian.
// Channel mask is NOT applied here; it is applied on the V (final) pass,
// because intermediate H-pass output must contain all channels for the
// V pass to sample a correct neighbourhood.
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_kernel_mode;
uniform int u_edge_policy;     // 0=clamp, 1=wrap, 2=mirror
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 64;

vec4 sampleEdge(sampler2D tex, vec2 uv) {
    if (u_edge_policy == 0) {
        return texture(tex, clamp(uv, vec2(0.0), vec2(1.0)));
    } else if (u_edge_policy == 1) {
        return texture(tex, fract(uv));
    } else {
        vec2 m = fract(uv * 0.5) * 2.0;
        m = mix(m, 2.0 - m, step(1.0, m));
        return texture(tex, m);
    }
}

void main() {
    if (u_radius < 0.5) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    float radius = max(u_radius, 0.5);
    int kernel_size = min(int(ceil(radius)), MAX_KERNEL);
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;

    // T-P1-21：动态循环边界（GL 3.30+ 支持）。原实现用静态 MAX_KERNEL
    // 边界 + 逐迭代 continue 守卫——AMD 上静态循环不因 continue 跳过成本，
    // radius 12 时 129 次迭代只有 25 次有效采样（约 5 倍浪费）。采样集
    // 与原实现逐点相同，语义不变。
    if (u_kernel_mode == 0) {
        // Box: uniform weights.
        for (int i = -kernel_size; i <= kernel_size; i++) {
            vec2 offset = vec2(float(i) / u_resolution.x, 0.0);
            color += sampleEdge(u_texture, v_uv + offset);
            weight_sum += 1.0;
        }
    } else {
        // Gaussian.
        float sigma = max(radius * 0.5, 0.1);
        for (int i = -kernel_size; i <= kernel_size; i++) {
            float x = float(i);
            float weight = exp(-(x * x) / (2.0 * sigma * sigma));
            vec2 offset = vec2(x / u_resolution.x, 0.0);
            color += sampleEdge(u_texture, v_uv + offset) * weight;
            weight_sum += weight;
        }
    }

    fragColor = color / max(weight_sum, 0.0001);
}
"""

_BLUR_SEPARABLE_V_FRAGMENT = """
#version 330
// Separable blur — vertical (final) pass.
// Applies channel mask + mix here, on the completed 2D blur result.
//
// u_original is the PRISTINE input (unit 1), not the H-pass output: the
// mask/mix reference must be the untouched frame. Sampling the H result as
// "original" leaked one horizontal blur into masked-off channels and into
// the mix reference (channel_a=False still blurred alpha horizontally;
// mix=0.5 mixed against an already-H-blurred image).
uniform sampler2D u_texture;
uniform sampler2D u_original;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_kernel_mode;
uniform vec4 u_channel_mask;   // per-channel enable
uniform float u_mix;           // 1.0 = full blur, 0.0 = original
uniform int u_edge_policy;     // 0=clamp, 1=wrap, 2=mirror
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 64;

vec4 sampleEdge(sampler2D tex, vec2 uv, vec2 tex_size) {
    if (u_edge_policy == 0) {
        vec2 c = clamp(uv, vec2(0.0), vec2(1.0));
        return texture(tex, c);
    } else if (u_edge_policy == 1) {
        return texture(tex, fract(uv));
    } else {
        vec2 m = fract(uv * 0.5) * 2.0;
        m = mix(m, 2.0 - m, step(1.0, m));
        return texture(tex, m);
    }
}

void main() {
    vec4 original = texture(u_original, v_uv);

    if (u_radius < 0.5) {
        fragColor = original;
        return;
    }

    float radius = max(u_radius, 0.5);
    int kernel_size = min(int(ceil(radius)), MAX_KERNEL);
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;

    // T-P1-21：动态循环边界（语义与采样集同原实现，见 H pass 注释）。
    if (u_kernel_mode == 0) {
        // Box.
        for (int i = -kernel_size; i <= kernel_size; i++) {
            vec2 offset = vec2(0.0, float(i) / u_resolution.y);
            color += sampleEdge(u_texture, v_uv + offset, u_resolution);
            weight_sum += 1.0;
        }
    } else {
        // Gaussian.
        float sigma = max(radius * 0.5, 0.1);
        for (int i = -kernel_size; i <= kernel_size; i++) {
            float y = float(i);
            float weight = exp(-(y * y) / (2.0 * sigma * sigma));
            vec2 offset = vec2(0.0, y / u_resolution.y);
            color += sampleEdge(u_texture, v_uv + offset, u_resolution) * weight;
            weight_sum += weight;
        }
    }

    vec4 blurred = color / max(weight_sum, 0.0001);

    // Per-channel mix: channels with mask<0.5 keep original, others get blurred.
    vec4 result;
    result.r = mix(original.r, blurred.r, u_channel_mask.r * u_mix);
    result.g = mix(original.g, blurred.g, u_channel_mask.g * u_mix);
    result.b = mix(original.b, blurred.b, u_channel_mask.b * u_mix);
    result.a = mix(original.a, blurred.a, u_channel_mask.a * u_mix);

    fragColor = result;
}
"""

_BLUR_BILATERAL_FRAGMENT = """
#version 330
// Bilateral blur (edge-preserving).
// Single-pass 2D kernel. Spatial + range Gaussian weighting.
// O(r^2) cost — radius is clamped externally to keep this affordable.
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_radius;
uniform float u_edge_preserve;  // 0..1, higher = stronger edge preservation
uniform int u_sample_count;     // effective kernel half-size (quality-driven)
uniform vec4 u_channel_mask;
uniform float u_mix;
uniform int u_edge_policy;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 10;

vec4 sampleEdge(sampler2D tex, vec2 uv) {
    if (u_edge_policy == 0) {
        return texture(tex, clamp(uv, vec2(0.0), vec2(1.0)));
    } else if (u_edge_policy == 1) {
        return texture(tex, fract(uv));
    } else {
        vec2 m = fract(uv * 0.5) * 2.0;
        m = mix(m, 2.0 - m, step(1.0, m));
        return texture(tex, m);
    }
}

void main() {
    vec4 original = texture(u_texture, v_uv);

    if (u_radius < 0.5) {
        fragColor = original;
        return;
    }

    int r = min(min(int(ceil(u_radius)), MAX_KERNEL), max(u_sample_count, 1));
    float sigma_spatial = max(float(r) * 0.5, 1.0);
    // Higher edge_preserve -> smaller sigma_range -> stronger edge stopping.
    float sigma_range = max(0.05, 0.4 * (1.0 - u_edge_preserve));

    vec4 sum = vec4(0.0);
    float weight_sum = 0.0;
    vec3 center_rgb = original.rgb;

    for (int y = -MAX_KERNEL; y <= MAX_KERNEL; y++) {
        if (abs(y) > r) continue;
        for (int x = -MAX_KERNEL; x <= MAX_KERNEL; x++) {
            if (abs(x) > r) continue;

            vec2 offset = vec2(float(x), float(y)) / u_resolution;
            vec4 s = sampleEdge(u_texture, v_uv + offset);

            float dist_sq = float(x * x + y * y);
            float w_space = exp(-dist_sq / (2.0 * sigma_spatial * sigma_spatial));

            vec3 diff = center_rgb - s.rgb;
            float range_sq = dot(diff, diff);
            float w_range = exp(-range_sq / (2.0 * sigma_range * sigma_range));

            float w = w_space * w_range;
            sum += s * w;
            weight_sum += w;
        }
    }

    vec4 blurred = sum / max(weight_sum, 0.0001);

    vec4 result;
    result.r = mix(original.r, blurred.r, u_channel_mask.r * u_mix);
    result.g = mix(original.g, blurred.g, u_channel_mask.g * u_mix);
    result.b = mix(original.b, blurred.b, u_channel_mask.b * u_mix);
    result.a = mix(original.a, blurred.a, u_channel_mask.a * u_mix);

    fragColor = result;
}
"""

_BLUR_MOTION_FRAGMENT = """
#version 330
// Motion blur — line samples along (cos(angle), sin(angle)) direction.
// Single pass, jittered sampling for smooth streaks.
uniform sampler2D u_texture;
uniform sampler2D u_velocity; // signed normalized UV displacement / frame
uniform vec2 u_resolution;
uniform float u_radius;
uniform float u_angle;     // degrees
uniform int u_use_velocity;
uniform float u_shutter;   // frame interval multiplier; 1.0 = one frame
uniform int u_sample_count; // quality-driven sample count (even)
uniform vec4 u_channel_mask;
uniform float u_mix;
uniform int u_edge_policy;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_SAMPLES = 32;

vec4 sampleEdge(sampler2D tex, vec2 uv) {
    if (u_edge_policy == 0) {
        return texture(tex, clamp(uv, vec2(0.0), vec2(1.0)));
    } else if (u_edge_policy == 1) {
        return texture(tex, fract(uv));
    } else {
        vec2 m = fract(uv * 0.5) * 2.0;
        m = mix(m, 2.0 - m, step(1.0, m));
        return texture(tex, m);
    }
}

void main() {
    vec4 original = texture(u_texture, v_uv);

    if ((u_use_velocity == 0 && u_radius < 0.5) || u_shutter <= 0.0) {
        fragColor = original;
        return;
    }

    int n = min(max(u_sample_count, 4), MAX_SAMPLES);
    float rad = u_angle * 3.14159265358979 / 180.0;
    vec2 dir = vec2(cos(rad), sin(rad));
    // Convert pixel radius to UV step, normalised by texture size in that axis
    // to keep the streak visually isotropic.
    vec2 step_uv = dir / max(u_resolution.x, u_resolution.y);
    vec2 velocity = vec2(0.0);
    if (u_use_velocity == 1) {
        // Contract: RG stores signed UV displacement over one frame. This
        // keeps the same shutter unit usable for 2D and future 3D adapters.
        velocity = texture(u_velocity, v_uv).rg;
    }

    vec4 sum = vec4(0.0);
    float weight_sum = 0.0;

    for (int i = 0; i < MAX_SAMPLES; i++) {
        if (i >= n) break;
        // Sample from -radius..+radius centred on 0.
        float t = (float(i) / float(n - 1)) * 2.0 - 1.0;  // -1..1
        float d = t * u_radius;
        vec2 offset = (u_use_velocity == 1)
            ? velocity * (t * u_shutter)
            : step_uv * (d * u_shutter);
        // Jitter along the perpendicular axis to soften banding (very small).
        sum += sampleEdge(u_texture, v_uv + offset);
        weight_sum += 1.0;
    }

    vec4 blurred = sum / max(weight_sum, 0.0001);

    vec4 result;
    result.r = mix(original.r, blurred.r, u_channel_mask.r * u_mix);
    result.g = mix(original.g, blurred.g, u_channel_mask.g * u_mix);
    result.b = mix(original.b, blurred.b, u_channel_mask.b * u_mix);
    result.a = mix(original.a, blurred.a, u_channel_mask.a * u_mix);

    fragColor = result;
}
"""

_BLUR_BOKEH_FRAGMENT = """
#version 330
// Bokeh (lens defocus) blur — disc sampling on the aperture shape.
// Supports: circular aperture (aperture_blades=0) or polygonal
// (5/6/8 blades), anamorphic stretch (cinematic horizontal streak),
// and highlight gain.
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_aperture_blades;   // 0 = circle, else 5/6/8
uniform float u_anamorphic;      // -1..+1, + = horizontal stretch (cinematic)
uniform float u_highlight_gain;  // 0..4, boosts bright sample contribution
uniform int u_sample_count;      // quality-driven sample count
uniform vec4 u_channel_mask;
uniform float u_mix;
uniform int u_edge_policy;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_SAMPLES = 48;

vec4 sampleEdge(sampler2D tex, vec2 uv) {
    if (u_edge_policy == 0) {
        return texture(tex, clamp(uv, vec2(0.0), vec2(1.0)));
    } else if (u_edge_policy == 1) {
        return texture(tex, fract(uv));
    } else {
        vec2 m = fract(uv * 0.5) * 2.0;
        m = mix(m, 2.0 - m, step(1.0, m));
        return texture(tex, m);
    }
}

void main() {
    vec4 original = texture(u_texture, v_uv);

    if (u_radius < 0.5) {
        fragColor = original;
        return;
    }

    int n = min(max(u_sample_count, 8), MAX_SAMPLES);

    // Pixel aspect: convert the disc (defined in pixel space) into UV space
    // so the bokeh stays circular on non-square pixels.
    float pix_aspect = u_resolution.x / max(u_resolution.y, 1.0);
    vec2 px_to_uv = vec2(1.0 / max(u_resolution.x, 1.0),
                         1.0 / max(u_resolution.y, 1.0));

    // Anamorphic stretch (NOT area-preserving): positive u_anamorphic
    // stretches the aperture horizontally, producing the classic cinematic
    // anamorphic horizontal oval/streak bokeh. Negative squashes vertically.
    // stretch 1.0 = circle; up to ~2.5 horizontally at u_anamorphic = +1.
    float stretch = 1.0 + u_anamorphic * 1.5;          // 1.0 .. 2.5
    vec2 anamorphic_scale = vec2(stretch, 1.0 / stretch);

    vec4 sum = vec4(0.0);
    float weight_sum = 0.0;
    float blades = float(u_aperture_blades);

    // Golden-angle spiral for low-discrepancy disc sampling.
    const float GOLDEN = 2.39996323;

    for (int i = 0; i < MAX_SAMPLES; i++) {
        if (i >= n) break;
        float fi = float(i) + 0.5;
        float fn = float(n);
        // Radius distribution: sqrt for uniform disc density.
        float r = sqrt(fi / fn);
        float theta = fi * GOLDEN;

        vec2 disc = vec2(cos(theta), sin(theta)) * r;

        // Polygonal aperture shaping: fold points outside the polygon back in.
        if (blades > 0.5) {
            float seg = 6.28318530718 / blades;
            float a = mod(atan(disc.y, disc.x), seg);
            // Distance from centre to polygon edge at this angle.
            float poly_r = cos(seg * 0.5) / max(cos(a - seg * 0.5), 0.0001);
            poly_r = clamp(poly_r, 0.0, 1.0);
            disc *= poly_r;
        }

        // Apply anamorphic stretch in pixel space, then convert to UV.
        vec2 disc_px = disc * anamorphic_scale * u_radius;
        vec2 offset = disc_px * px_to_uv;
        vec4 s = sampleEdge(u_texture, v_uv + offset);

        // Highlight gain: weight brighter samples more (bokeh "glow").
        float luma = dot(s.rgb, vec3(0.2126, 0.7152, 0.0722));
        float hl = mix(1.0, pow(max(luma, 0.0), u_highlight_gain) + 1.0,
                       step(0.001, u_highlight_gain));
        sum += s * hl;
        weight_sum += hl;
    }

    vec4 blurred = sum / max(weight_sum, 0.0001);

    vec4 result;
    result.r = mix(original.r, blurred.r, u_channel_mask.r * u_mix);
    result.g = mix(original.g, blurred.g, u_channel_mask.g * u_mix);
    result.b = mix(original.b, blurred.b, u_channel_mask.b * u_mix);
    result.a = mix(original.a, blurred.a, u_channel_mask.a * u_mix);

    fragColor = result;
}
"""

_GRADE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_mask;
uniform vec3 u_lift;
uniform vec3 u_gamma;
uniform vec3 u_gain;
uniform vec3 u_offset;
// Extended params fed by _apply_grade (previously missing here, which
// made every dispatched grade node raise KeyError and drop the graph).
uniform float u_blackpoint;
uniform float u_whitepoint;
uniform float u_multiply;
uniform float u_add;
uniform vec3 u_channel_mask;
uniform float u_mix;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    float m = texture(u_mask, v_uv).r;

    vec3 c = (color.rgb - vec3(u_blackpoint))
             / max(u_whitepoint - u_blackpoint, 0.0001);
    vec3 result;
    for (int i = 0; i < 3; i++) {
        float g = c[i];
        g = g * u_gain[i] + u_offset[i];
        g = pow(max(g, 0.0001), 1.0 / max(u_gamma[i], 0.001));
        g = g + u_lift[i];
        result[i] = g;
    }
    result = result * u_multiply + u_add;
    // Per-channel enable + spatial mask + master mix.
    result = mix(color.rgb, result, u_channel_mask * (m * u_mix));

    fragColor = vec4(result, color.a);
}
"""

_TOPOS_COLOR_FRAGMENT = (
    """
#version 330
// Topos Color Stack shader —— M05.2f：调色数学与 Edit 预览/导出共用
// PRIMARY_GRADE_BODY（CPU 权威 GradingMath 的 GLSL 镜像，五路一致：
// CPU 导出/CPU 预览/Edit GPU 预览/GPU 导出/Composite）。此前是独立
// 近似实现（exposure stops / contrast 分数域等参数域不一致，M05.2e
// 的 GPU 顺序测试暴露）——本文件不再维护平行调色数学；mid_detail/
// sharpness 纹理类不属 primary 域（与交付能力矩阵同口径）。
uniform sampler2D u_texture;
uniform sampler2D u_mask;
uniform float u_intensity;
in vec2 v_uv;
out vec4 fragColor;
"""
    + PRIMARY_GRADE_UNIFORMS
    + PRIMARY_GRADE_BODY
    + """
void main() {
    vec4 color = texture(u_texture, v_uv);
    vec3 graded = apply_primary_grade(color.rgb);
    float mask_amount = texture(u_mask, v_uv).r;
    vec3 result = mix(color.rgb, graded, clamp(u_intensity * mask_amount, 0.0, 1.0));
    fragColor = vec4(result, color.a);
}
"""
)

_TRANSFORM_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_m00;
uniform float u_m10;
uniform float u_m01;
uniform float u_m11;
uniform float u_m02;
uniform float u_m12;
uniform int u_filter;  // 0=linear, 1=nearest
in vec2 v_uv;
out vec4 fragColor;

void main() {
    float a = u_m00;
    float b = u_m10;
    float c = u_m01;
    float d = u_m11;
    float tx = u_m02;
    float ty = u_m12;

    float det = a * d - b * c;
    if (abs(det) < 0.0001) {
        fragColor = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }

    float inv_det = 1.0 / det;
    vec2 p = v_uv - vec2(tx, ty);
    vec2 uv;
    uv.x = (d * p.x - c * p.y) * inv_det;
    uv.y = (-b * p.x + a * p.y) * inv_det;

    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        fragColor = vec4(0.0, 0.0, 0.0, 0.0);
    } else if (u_filter == 1) {
        ivec2 tex_size = textureSize(u_texture, 0);
        ivec2 texel = ivec2(uv * vec2(tex_size));
        texel = clamp(texel, ivec2(0), tex_size - ivec2(1));
        fragColor = texelFetch(u_texture, texel, 0);
    } else {
        fragColor = texture(u_texture, uv);
    }
}
"""

_REFORMAT_FRAGMENT = """
#version 330
// Resize / reformat. u_filter: 0=nearest, 1=linear, 2=cubic, 3=lanczos.
uniform sampler2D u_texture;
uniform vec2 u_src_resolution;
uniform vec2 u_dst_resolution;
uniform int u_resize_mode;  // 0=no_resize, 1=fit, 2=fill, 3=distort, 4=center_crop
uniform int u_filter;       // 0=nearest, 1=linear, 2=cubic, 3=lanczos
in vec2 v_uv;
out vec4 fragColor;

// Catmull-Rom bicubic sampler (matches CornerPin shader).
vec4 sample_bicubic(sampler2D tex, vec2 uv, vec2 tex_size) {
    vec2 texel_pos = uv * tex_size - 0.5;
    vec2 texel_f = fract(texel_pos);
    vec2 texel_i = floor(texel_pos);

    vec4 result = vec4(0.0);
    float weight_sum = 0.0;

    for (int j = 0; j < 4; j++) {
        for (int i = 0; i < 4; i++) {
            vec2 offset = vec2(float(i - 1), float(j - 1));
            vec2 sample_pos = (texel_i + offset + 0.5) / tex_size;

            float wx = abs(texel_f.x - offset.x);
            float wy = abs(texel_f.y - offset.y);
            float weight_x = (wx < 1.0) ? (1.5 * wx * wx * wx - 2.5 * wx * wx + 1.0)
                                       : (-0.5 * wx * wx * wx + 2.5 * wx * wx - 4.0 * wx + 2.0);
            float weight_y = (wy < 1.0) ? (1.5 * wy * wy * wy - 2.5 * wy * wy + 1.0)
                                       : (-0.5 * wy * wy * wy + 2.5 * wy * wy - 4.0 * wy + 2.0);
            float weight = weight_x * weight_y;

            result += texture(tex, sample_pos) * weight;
            weight_sum += weight;
        }
    }

    return result / max(weight_sum, 0.00001);
}

// Normalised sinc function.
float sinc(float x) {
    if (abs(x) < 0.0001) return 1.0;
    float px = 3.14159265359 * x;
    return sin(px) / px;
}

// Lanczos-2 (a=2) kernel.
float lanczos2(float x) {
    if (abs(x) >= 2.0) return 0.0;
    return sinc(x) * sinc(x * 0.5);
}

// Separable Lanczos-2 sampler (4 taps per axis).
vec4 sample_lanczos(sampler2D tex, vec2 uv, vec2 tex_size) {
    vec2 texel_pos = uv * tex_size - 0.5;
    vec2 texel_f = vec2(texel_pos.x - floor(texel_pos.x), texel_pos.y - floor(texel_pos.y));
    vec2 texel_i = floor(texel_pos);

    vec4 result = vec4(0.0);
    float weight_sum = 0.0;

    for (int j = -1; j <= 2; j++) {
        for (int i = -1; i <= 2; i++) {
            vec2 offset = vec2(float(i), float(j));
            vec2 sample_pos = (texel_i + offset + 0.5) / tex_size;

            float wx = lanczos2(texel_f.x - offset.x);
            float wy = lanczos2(texel_f.y - offset.y);
            float weight = wx * wy;

            result += texture(tex, sample_pos) * weight;
            weight_sum += weight;
        }
    }

    return result / max(abs(weight_sum), 0.0001);
}

void main() {
    float src_w = u_src_resolution.x;
    float src_h = u_src_resolution.y;
    float dst_w = u_dst_resolution.x;
    float dst_h = u_dst_resolution.y;
    vec2 src_uv;
    bool valid = true;

    if (u_resize_mode == 3) {
        // distort: stretch to fill
        src_uv = v_uv;
    } else if (u_resize_mode == 1) {
        // fit: uniform scale, letterbox
        float scale = min(dst_w / src_w, dst_h / src_h);
        float scaled_w = src_w * scale;
        float scaled_h = src_h * scale;
        vec2 offset = (vec2(dst_w, dst_h) - vec2(scaled_w, scaled_h)) / (2.0 * vec2(dst_w, dst_h));
        vec2 range = vec2(scaled_w, scaled_h) / vec2(dst_w, dst_h);
        src_uv = (v_uv - offset) / range;
        if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0)
            valid = false;
    } else if (u_resize_mode == 2) {
        // fill: uniform scale, crop excess
        float scale = max(dst_w / src_w, dst_h / src_h);
        float scaled_w = src_w * scale;
        float scaled_h = src_h * scale;
        vec2 offset = (vec2(dst_w, dst_h) - vec2(scaled_w, scaled_h)) / (2.0 * vec2(dst_w, dst_h));
        vec2 range = vec2(scaled_w, scaled_h) / vec2(dst_w, dst_h);
        src_uv = (v_uv - offset) / range;
    } else if (u_resize_mode == 4) {
        // center_crop: uniform scale to fill, center and crop
        float scale = max(dst_w / src_w, dst_h / src_h);
        float scaled_w = src_w * scale;
        float scaled_h = src_h * scale;
        vec2 offset = (vec2(dst_w, dst_h) - vec2(scaled_w, scaled_h)) / (2.0 * vec2(dst_w, dst_h));
        vec2 range = vec2(scaled_w, scaled_h) / vec2(dst_w, dst_h);
        src_uv = (v_uv - offset) / range;
    } else {
        // none: place at origin without scaling
        src_uv = v_uv * vec2(dst_w / src_w, dst_h / src_h);
        if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0)
            valid = false;
    }

    if (!valid) {
        fragColor = vec4(0.0);
        return;
    }

    vec2 tex_size = u_src_resolution;

    if (u_filter == 0) {
        // Nearest neighbour (hard pixel edges).
        ivec2 itex_size = textureSize(u_texture, 0);
        ivec2 texel = ivec2(src_uv * vec2(itex_size));
        texel = clamp(texel, ivec2(0), itex_size - ivec2(1));
        fragColor = texelFetch(u_texture, texel, 0);
    } else if (u_filter == 1) {
        // Linear (bilinear) — hardware-accelerated.
        fragColor = texture(u_texture, src_uv);
    } else if (u_filter == 2) {
        // Catmull-Rom bicubic.
        fragColor = sample_bicubic(u_texture, src_uv, tex_size);
    } else {
        // Lanczos-2 (sharper than bicubic, mild ringing).
        fragColor = sample_lanczos(u_texture, src_uv, tex_size);
    }
}
"""

_DEFOCUS_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform int u_mode;              // 0=separable, 1=mipmap
uniform vec2 u_focal_point;      // Normalized focal point (0-1)
uniform float u_focal_size;      // Normalized focal region size
uniform float u_max_radius;      // Maximum blur radius
uniform int u_edge_policy;       // 0=clamp, 1=wrap, 2=mirror
uniform bool u_process_alpha;    // Whether to blur alpha channel
uniform int u_sample_count;      // Quality-based sample count
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 128;

// Calculate blur radius based on distance from focal point
float calculateDefocusRadius(vec2 uv, vec2 focal, float focal_size, float max_radius) {
    vec2 distance = abs(uv - focal);
    // Normalize distance by focal_size to get a 0-1 factor
    float dist_factor = length(distance) / max(focal_size, 0.01);
    // Smooth falloff using quadratic curve
    float radius = max_radius * min(dist_factor * dist_factor, 1.0);
    return radius;
}

// Edge policy sampling
vec4 sampleWithEdgePolicy(sampler2D tex, vec2 uv, vec2 tex_size) {
    vec2 pixel_uv = uv * tex_size;
    
    if (u_edge_policy == 0) {
        pixel_uv = clamp(pixel_uv, vec2(0.5), tex_size - 0.5);
    } else if (u_edge_policy == 1) {
        pixel_uv = mod(pixel_uv, tex_size);
    } else if (u_edge_policy == 2) {
        vec2 normalized = pixel_uv / tex_size;
        vec2 mirrored = fract(normalized * 0.5) * 2.0;
        mirrored = mix(mirrored, 2.0 - mirrored, step(1.0, mirrored));
        pixel_uv = mirrored * tex_size;
    }
    
    return texture(tex, pixel_uv / tex_size);
}

void main() {
    if (u_max_radius < 0.1) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    vec2 tex_size = u_resolution;
    
    // Calculate local blur radius based on distance from focal point
    float local_radius = calculateDefocusRadius(v_uv, u_focal_point, u_focal_size, u_max_radius);
    
    if (local_radius < 0.5) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    // Adaptive sample count
    int samples = min(u_sample_count, MAX_KERNEL);
    samples = max(samples, 3);
    
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;
    
    // Sigma for Gaussian falloff
    float sigma = max(local_radius * 0.5, 0.1);
    
    // Separable mode: two-pass approximation using single shader
    // (actual two-pass is handled by renderer)
    if (u_mode == 0) {
        // Separable Gaussian blur with spatially varying radius
        // T-P1-21：动态循环边界（同 blur H/V pass 说明）。
        for (int i = -samples + 1; i < samples; i++) {
            float x = float(i);
            float weight = exp(-(x * x) / (2.0 * sigma * sigma));
            
            vec2 offset = vec2(x / tex_size.x, 0.0);
            vec2 sample_uv = v_uv + offset;
            
            vec4 sample = sampleWithEdgePolicy(u_texture, sample_uv, tex_size);
            color += sample * weight;
            weight_sum += weight;
        }
    } else {
        // Mipmap mode: approximate defocus using texture LOD
        // This is faster but lower quality
        float lod = log2(local_radius + 1.0);
        color = textureLod(u_texture, v_uv, lod);
        weight_sum = 1.0;
    }
    
    if (weight_sum > 0.0 && u_mode == 0) {
        color /= weight_sum;
    }
    
    // Alpha processing
    if (u_process_alpha) {
        fragColor = color;
    } else {
        vec4 original = texture(u_texture, v_uv);
        fragColor = vec4(color.rgb, original.a);
    }
}
"""

_GLOW_H_FRAGMENT = """
#version 330
// Glow — horizontal (first) pass.
//
// Pipeline (separable two-pass bloom):
//   1. Threshold the bright areas of the input by luminance.
//   2. Apply horizontal Gaussian blur to the bright parts.
// The vertical pass then finishes the 2D blur and composites onto the
// original image (with tint, intensity, mix).
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_threshold;
uniform float u_radius;
uniform int u_sample_count;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 128;

float luma(vec3 c) {
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}

void main() {
    if (u_radius < 0.5) {
        // No blur: just emit the bright (above-threshold) parts.
        vec4 src = texture(u_texture, v_uv);
        float bright = smoothstep(u_threshold, u_threshold + 0.2, luma(src.rgb));
        fragColor = vec4(src.rgb * bright, src.a * bright);
        return;
    }

    int kernel = min(u_sample_count, MAX_KERNEL);
    kernel = max(kernel, 3);

    float sigma = max(u_radius * 0.5, 0.1);
    float denom = 2.0 * sigma * sigma;

    vec4 glow = vec4(0.0);
    float weight_sum = 0.0;

    // T-P1-21：动态循环边界（原静态 128×2=257 次迭代仅 ~2×kernel 有效）。
    for (int i = -kernel; i <= kernel; i++) {
        float x = float(i);
        float w = exp(-(x * x) / denom);

        vec2 offset = vec2(x / u_resolution.x, 0.0);
        vec4 s = texture(u_texture, v_uv + offset);

        float bright = smoothstep(u_threshold, u_threshold + 0.2, luma(s.rgb));

        glow.rgb += s.rgb * bright * w;
        glow.a   += s.a   * bright * w;
        weight_sum += w;
    }

    if (weight_sum > 0.0) {
        glow /= weight_sum;
    }

    fragColor = glow;
}
"""

_GLOW_V_FRAGMENT = """
#version 330
// Glow — vertical (final) pass.
//
// Completes the 2D Gaussian blur on the H-pass output (which holds the
// bright/thresholded-and-blurred pixels), then composites onto the
// original image with screen-blend: result = original + glow * intensity * tint * mix.
//
// Textures:
//   u_texture   (unit 0): the ORIGINAL input image.
//   u_bloom_tex (unit 1): horizontal-pass output (bright + H-blurred).
uniform sampler2D u_texture;     // original input
uniform sampler2D u_bloom_tex;   // H-pass output (bright, h-blurred)
uniform vec2 u_resolution;
uniform float u_radius;
uniform float u_intensity;
uniform vec3 u_tint;
uniform float u_mix;
uniform int u_sample_count;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_KERNEL = 128;

vec4 sampleClamp(sampler2D tex, vec2 uv) {
    return texture(tex, clamp(uv, vec2(0.0), vec2(1.0)));
}

void main() {
    vec4 original = texture(u_texture, v_uv);

    if (u_radius < 0.5 || u_intensity < 0.001 || u_mix < 0.001) {
        fragColor = original;
        return;
    }

    int kernel = min(u_sample_count, MAX_KERNEL);
    kernel = max(kernel, 3);

    float sigma = max(u_radius * 0.5, 0.1);
    float denom = 2.0 * sigma * sigma;

    vec4 glow = vec4(0.0);
    float weight_sum = 0.0;

    // T-P1-21：动态循环边界（同 glow H pass）。
    for (int i = -kernel; i <= kernel; i++) {
        float x = float(i);
        float w = exp(-(x * x) / denom);

        vec2 offset = vec2(0.0, x / u_resolution.y);
        glow += sampleClamp(u_bloom_tex, v_uv + offset) * w;
        weight_sum += w;
    }

    if (weight_sum > 0.0) {
        glow /= weight_sum;
    }

    // Tint + intensity applied to the finished bloom buffer.
    vec3 bloom = glow.rgb * u_intensity * u_tint;

    // Screen-blend style compositing: add glow on top of original.
    // HDR safe: pure addition preserves highlights.
    vec3 result_rgb = original.rgb + bloom * u_mix;

    fragColor = vec4(result_rgb, original.a);
}
"""

_CROP_FRAGMENT = """
#version 330
// Crop. u_outside: 0=black, 1=transparent, 2=repeat, 3=mirror.
uniform sampler2D u_texture;
uniform vec2 u_src_resolution;
uniform vec4 u_crop_rect;  // x, y, width, height in pixels
uniform int u_outside;     // 0=black, 1=transparent, 2=repeat, 3=mirror
in vec2 v_uv;
out vec4 fragColor;

vec2 mirror_uv(vec2 uv) {
    // Triangle-wave mirror in [0,1] domain.
    vec2 m = fract(uv * 0.5) * 2.0;
    return mix(m, 2.0 - m, step(1.0, m));
}

void main() {
    float crop_x = u_crop_rect.x;
    float crop_y = u_crop_rect.y;
    float crop_w = u_crop_rect.z;
    float crop_h = u_crop_rect.w;

    // Map output UV to source pixel, then to source UV
    vec2 src_pixel = v_uv * vec2(crop_w, crop_h) + vec2(crop_x, crop_y);
    vec2 src_uv = src_pixel / u_src_resolution;

    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        if (u_outside == 2) {
            // repeat: wrap UVs into [0,1] range
            src_uv = fract(src_uv);
        } else if (u_outside == 3) {
            // mirror: reflect UVs into [0,1] range
            src_uv = mirror_uv(src_uv);
        } else {
            fragColor = (u_outside == 1) ? vec4(0.0) : vec4(0.0, 0.0, 0.0, 1.0);
            return;
        }
    }

    fragColor = texture(u_texture, src_uv);
}
"""

_INVERT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec4 u_channels;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec4 inverted = vec4(
        mix(color.r, 1.0 - color.r, u_channels.r),
        mix(color.g, 1.0 - color.g, u_channels.g),
        mix(color.b, 1.0 - color.b, u_channels.b),
        mix(color.a, 1.0 - color.a, u_channels.a)
    );
    fragColor = inverted;
}
"""

_SHUFFLE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform int u_r_from;  // 0=R, 1=G, 2=B, 3=A, 4=Zero, 5=One, 6=Luma
uniform int u_g_from;
uniform int u_b_from;
uniform int u_a_from;
in vec2 v_uv;
out vec4 fragColor;

float get_channel(vec4 color, int source) {
    if (source == 0) return color.r;
    if (source == 1) return color.g;
    if (source == 2) return color.b;
    if (source == 3) return color.a;
    if (source == 4) return 0.0;
    if (source == 5) return 1.0;
    if (source == 6) return dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    return 0.0;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    fragColor = vec4(
        get_channel(color, u_r_from),
        get_channel(color, u_g_from),
        get_channel(color, u_b_from),
        get_channel(color, u_a_from)
    );
}
"""

_COPY_CHANNEL_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;  // main image (destination)
uniform sampler2D u_texture_b;  // source channel image
uniform int u_src_channel;  // 0=R, 1=G, 2=B, 3=A
uniform int u_dst_channel;  // 0=R, 1=G, 2=B, 3=A
in vec2 v_uv;
out vec4 fragColor;

float get_ch(vec4 color, int ch) {
    if (ch == 0) return color.r;
    if (ch == 1) return color.g;
    if (ch == 2) return color.b;
    return color.a;
}

void main() {
    vec4 a = texture(u_texture_a, v_uv);
    vec4 b = texture(u_texture_b, v_uv);
    float src_val = get_ch(b, u_src_channel);

    if (u_dst_channel == 0) a.r = src_val;
    else if (u_dst_channel == 1) a.g = src_val;
    else if (u_dst_channel == 2) a.b = src_val;
    else a.a = src_val;

    fragColor = a;
}
"""

_CS_TRANSFORM_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform int u_direction;  // 0 = decode (encoded→Linear), 1 = encode (Linear→encoded)
uniform int u_transfer;   // 0=sRGB, 1=Rec.709, 2=Linear, 3=PQ(ST.2084), 4=HLG, 5=TRAW PWL-log12
uniform int u_primaries;  // gamut for the (de|en)coded side: 0=BT.709, 1=BT.2020, 2=DCI-P3, 3=AP1 (ACEScg)
uniform bool u_input_premultiplied;
in vec2 v_uv;
out vec4 fragColor;

vec3 srgb_to_linear(vec3 c) {
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + 0.055) / 1.055, vec3(2.4));
    return mix(hi, lo, step(c, vec3(0.04045)));
}

vec3 linear_to_srgb(vec3 c) {
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
    return mix(hi, lo, step(c, vec3(0.0031308)));
}

// Rec.709 (BT.709 OETF). Distinct from sRGB: a 0.45-power curve with a
// linear toe (E = 4.5*L for L < 0.018; E = 1.099*L^0.45 - 0.099 otherwise).
// The effective gamma is ~2.4 vs sRGB's ~2.2.
vec3 bt709_to_linear(vec3 c) {
    vec3 lo = c / 4.5;
    vec3 hi = pow(max(c + 0.099, 0.0) / 1.099, vec3(1.0 / 0.45));
    return mix(hi, lo, step(c, vec3(0.081)));
}

vec3 linear_to_bt709(vec3 c) {
    vec3 lo = c * 4.5;
    vec3 hi = 1.099 * pow(max(c, vec3(0.0)), vec3(0.45)) - 0.099;
    return mix(hi, lo, step(c, vec3(0.018)));
}

// PQ (SMPTE ST 2084). Canonical constants. PQ codes [0,1] <-> linear nits
// (0..10000). PQ linear values EXCEED 1.0 (HDR working space in nits).
const float PQ_M1 = 0.1593017578125;
const float PQ_M2 = 78.84375;
const float PQ_C1 = 0.8359375;
const float PQ_C2 = 18.8515625;
const float PQ_C3 = 18.6875;
const float PQ_LMAX = 10000.0;

vec3 pq_to_linear(vec3 c) {
    vec3 cc = clamp(c, 0.0, 1.0);
    vec3 v = pow(cc, vec3(1.0 / PQ_M2));
    vec3 num = max(v - PQ_C1, 0.0);
    vec3 den = max(PQ_C2 - PQ_C3 * v, 1e-10);
    return pow(num / den, vec3(1.0 / PQ_M1)) * PQ_LMAX;
}

vec3 linear_to_pq(vec3 c) {
    vec3 n = clamp(c / PQ_LMAX, 0.0, 1.0);
    vec3 v = pow(n, vec3(PQ_M1));
    vec3 num = PQ_C1 + PQ_C2 * v;
    vec3 den = 1.0 + PQ_C3 * v;
    return pow(num / den, vec3(PQ_M2));
}

// HLG (ARIB STD-B67). Scene-referred OETF; linear result is relative
// (multiply by a display luminance for absolute nits). Codes [0,1].
const float HLG_A = 0.17883277;
const float HLG_B = 0.28466892;
const float HLG_C = 0.55991073;

vec3 hlg_to_linear(vec3 c) {
    vec3 cc = clamp(c, 0.0, 1.0);
    vec3 lo = cc * cc / 3.0;
    vec3 hi = (exp((cc - HLG_C) / HLG_A) + HLG_B) / 12.0;
    return mix(hi, lo, step(cc, vec3(0.5)));
}

vec3 linear_to_hlg(vec3 c) {
    vec3 cc = max(c, 0.0);
    vec3 lo = sqrt(3.0 * cc);
    vec3 hi = HLG_A * log(max(12.0 * cc - HLG_B, 1e-10)) + HLG_C;
    return clamp(mix(hi, lo, step(cc, vec3(1.0 / 12.0))), 0.0, 1.0);
}

// TRAW PWL-log12（RC7；与 cfa_gpu_debayer/cfa_cpu_debayer 同源公式，
// topos_traw_format_plan §3.2 冻结）。归一化口径：编码输入 = 场景线性
// e−/满阱（[0,1]），解码输出同口径（线性 [0,1]，与 PQ 的 nits 域不同——
// TRAW 满阱归一不超 1）。
const float TRAW_S = 8.0;
const float TRAW_FW = 16384.0;
const float TRAW_A = 4095.0 / log2(1.0 + (TRAW_FW - TRAW_S) / TRAW_S);
const float TRAW_K = TRAW_A / (log(2.0) * 2.0 * TRAW_S);
const float TRAW_C0 = TRAW_A - TRAW_K * TRAW_S;

vec3 traw_log_to_linear(vec3 c) {
    vec3 cc = clamp(c, 0.0, 1.0) * 4095.0;   // 归一码 → 12-bit 码
    vec3 toe = (cc - TRAW_C0) / TRAW_K;
    vec3 log_seg = TRAW_S * (exp2(cc / TRAW_A) - 1.0);
    return clamp(mix(log_seg, toe, step(cc, vec3(TRAW_A))), 0.0, TRAW_FW)
        / TRAW_FW;
}

vec3 linear_to_traw_log(vec3 c) {
    vec3 x = clamp(c, 0.0, 1.0) * TRAW_FW;   // 归一线性 → e−
    vec3 toe = TRAW_C0 + TRAW_K * x;
    vec3 log_seg = TRAW_A * log2(1.0 + x / TRAW_S);
    return clamp(mix(log_seg, toe, step(x, vec3(TRAW_S))), 0.0, 4095.0)
        / 4095.0;
}

// Gamut (primaries) matrices in linear light. BT.709 is the working space.
// Column-major (GLSL mat3 fills columns first): each ORIGINAL ROW is written
// as a column so the mat3 represents the row-major ITU matrix M (M * col_vec).
const mat3 BT709_TO_BT2020 = mat3(
    0.6274040, 0.0690970, 0.0163916,
    0.3292820, 0.9195400, 0.0880132,
    0.0433136, 0.0113612, 0.8955950);
const mat3 BT709_TO_P3D65 = mat3(
    0.8224620, 0.0331946, 0.0170827,
    0.1775380, 0.9668054, 0.0723974,
    0.0000000, 0.0000000, 0.9105199);
// BT.709 linear (D65) ↔ ACEScg AP1 linear (ACES ~D60), Bradford-adapted.
// Same published values as the BuiltinColorProvider/_AP1_TO_BT709 so the
// GPU IDT, the provider, and the bundled OCIO config agree exactly.
const mat3 BT709_TO_AP1 = mat3(
    0.6130974, 0.0701937, 0.0206158,
    0.3395231, 0.9163541, 0.1095697,
    0.0473795, 0.0134522, 0.8697855);
const mat3 AP1_TO_BT709 = mat3(
    1.7050502, -0.1302564, -0.0240033,
    -0.6217914, 1.1408043, -0.1289187,
    -0.0832562, -0.0105479, 1.1529220);

// Convert linear RGB from the encoded-side gamut to BT.709 working space.
// Decode path: src_gamut -> BT.709. Encode path: BT.709 -> dst_gamut.
// u_primaries selects the non-BT.709 side (0 = BT.709, i.e. no-op).
vec3 to_working_gamut(vec3 c) {
    if (u_primaries == 1) return inverse(BT709_TO_BT2020) * c;  // BT.2020 -> BT.709
    if (u_primaries == 2) return inverse(BT709_TO_P3D65) * c;   // P3 D65 -> BT.709
    if (u_primaries == 3) return AP1_TO_BT709 * c;              // ACEScg -> BT.709
    return c;
}
vec3 from_working_gamut(vec3 c) {
    if (u_primaries == 1) return BT709_TO_BT2020 * c;  // BT.709 -> BT.2020
    if (u_primaries == 2) return BT709_TO_P3D65 * c;   // BT.709 -> P3 D65
    if (u_primaries == 3) return BT709_TO_AP1 * c;     // BT.709 -> ACEScg
    return c;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    // Transfer functions operate on straight RGB. Decode/encode in straight
    // space, then restore premultiplied RGB so hidden RGB is not colour-shifted
    // by alpha and downstream Porter-Duff math remains valid.
    float alpha = color.a;
    if (u_input_premultiplied && alpha > 0.0001) {
        color.rgb /= alpha;
    }
    if (u_direction == 0) {
        // decode: encoded -> linear
        if (u_transfer == 1) color.rgb = bt709_to_linear(color.rgb);
        else if (u_transfer == 3) color.rgb = pq_to_linear(color.rgb);
        else if (u_transfer == 4) color.rgb = hlg_to_linear(color.rgb);
        else if (u_transfer == 5) color.rgb = traw_log_to_linear(color.rgb);
        else if (u_transfer == 2) { /* linear: no-op */ }
        else color.rgb = srgb_to_linear(color.rgb);
        // gamut: source primaries -> BT.709 working space (wide-gamut HDR)
        color.rgb = to_working_gamut(color.rgb);
    } else {
        // gamut: BT.709 working space -> target primaries (wide-gamut HDR)
        color.rgb = from_working_gamut(color.rgb);
        // encode: linear -> encoded
        if (u_transfer == 1) color.rgb = linear_to_bt709(color.rgb);
        else if (u_transfer == 3) color.rgb = linear_to_pq(color.rgb);
        else if (u_transfer == 4) color.rgb = linear_to_hlg(color.rgb);
        else if (u_transfer == 5) color.rgb = linear_to_traw_log(color.rgb);
        else if (u_transfer == 2) { /* linear: no-op */ }
        else color.rgb = linear_to_srgb(color.rgb);
    }
    if (u_input_premultiplied) {
        color.rgb *= alpha;
    }
    fragColor = color;
}
"""

_COLOR_CORRECT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_mask;
uniform float u_exposure;
uniform float u_contrast;
uniform float u_saturation;
uniform float u_hue_shift;
uniform float u_temperature;
uniform float u_tint;
uniform vec3 u_shadows_lifting;
uniform vec3 u_midtones_gamma;
uniform vec3 u_highlights_gain;
uniform vec4 u_channel_selector;
uniform float u_mix;
in vec2 v_uv;
out vec4 fragColor;

// Convert RGB to HSV
vec3 rgb_to_hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

// Convert HSV to RGB
vec3 hsv_to_rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    vec4 color = texture(u_texture, v_uv);

    // AUD-5306 (S3-A): the ambiguous "m.a > 0 ? m.a : m.r" heuristic is
    // replaced by the deterministic Rec.709 luma — the same default
    // channel the merge/blend contract uses (u_mask_channel=0). No
    // selectable uniform here: this shader's uniform set is pinned to the
    // ColorCorrectNode parameter surface by contract test.
    vec4 mask_sample = texture(u_mask, v_uv);
    float mask_val = dot(mask_sample.rgb, vec3(0.2126, 0.7152, 0.0722));

    // === 1. Exposure: color * pow(2.0, exposure) ===
    vec3 result = color.rgb * pow(2.0, u_exposure);

    // === 2. Contrast: (color - 0.5) * contrast + 0.5 ===
    result = (result - 0.5) * u_contrast + 0.5;

    // === 3. Saturation: mix(luma, color, saturation) ===
    float luma = dot(result, vec3(0.2126, 0.7152, 0.0722));
    result = mix(vec3(luma), result, u_saturation);

    // === 4. Hue shift: rotate in HSV space ===
    if (abs(u_hue_shift) > 0.01) {
        vec3 hsv = rgb_to_hsv(result);
        hsv.x = fract(hsv.x + u_hue_shift / 360.0);
        result = hsv_to_rgb(hsv);
    }

    // === 5. Temperature / Tint ===
    // Temperature shifts yellow-blue axis, tint shifts green-magenta axis
    result.r += u_temperature * 0.1;
    result.b -= u_temperature * 0.1;
    result.g += u_tint * 0.05;
    result.r -= u_tint * 0.025;
    result.b -= u_tint * 0.025;

    // === 6. Shadows/Midtones/Highlights ===
    // Compute tonal weights based on luma
    float tonal_luma = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    float shadow_w = 1.0 - smoothstep(0.0, 0.35, tonal_luma);
    float highlight_w = smoothstep(0.65, 1.0, tonal_luma);
    float midtone_w = 1.0 - shadow_w - highlight_w;

    // Shadows: lift (additive)
    result += u_shadows_lifting * shadow_w;

    // Midtones: gamma (power)
    for (int i = 0; i < 3; i++) {
        float gamma = u_midtones_gamma[i];
        if (abs(gamma - 1.0) > 0.001) {
            float mid_val = result[i];
            mid_val = pow(max(mid_val, 0.0001), 1.0 / max(gamma, 0.001));
            result[i] = mix(result[i], mid_val, midtone_w);
        }
    }

    // Highlights: gain (multiply)
    result += (u_highlights_gain - vec3(1.0)) * result * highlight_w;

    // === 7. Channel selector ===
    result = vec3(
        mix(color.r, result.r, u_channel_selector.r),
        mix(color.g, result.g, u_channel_selector.g),
        mix(color.b, result.b, u_channel_selector.b)
    );
    float alpha = mix(color.a, color.a, u_channel_selector.a);

    // === 8. Mix (blend between original and corrected) ===
    result = mix(color.rgb, result, u_mix * mask_val);

    fragColor = vec4(result, alpha);
}
"""

_PREMULT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    // Straight → Premultiplied: RGB *= A
    fragColor = vec4(color.rgb * color.a, color.a);
}
"""

_UNPREMULT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    // Premultiplied → Straight: RGB /= A (with safe divisor)
    float inv_a = 1.0 / max(color.a, 0.00001);
    // Preserve zero-alpha pixels as black
    vec3 rgb = color.a > 0.00001 ? color.rgb * inv_a : vec3(0.0);
    fragColor = vec4(rgb, color.a);
}
"""

_CLAMP_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec4 u_min_val;
uniform vec4 u_max_val;
uniform vec4 u_channel_mask;  // 1.0 = clamp, 0.0 = pass through
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec4 result;
    for (int i = 0; i < 4; i++) {
        float c = color[i];
        // Only clamp channels where mask is 1.0
        if (u_channel_mask[i] > 0.5) {
            c = clamp(c, u_min_val[i], u_max_val[i]);
        }
        result[i] = c;
    }
    fragColor = result;
}
"""

_MATTE_CLEANUP_FRAGMENT = """
#version 330
// Alpha-aware matte levels operation used by MatteCleanupNode.
uniform sampler2D u_texture;
uniform float u_black_point;
uniform float u_white_point;
uniform float u_gamma;
uniform bool u_invert;
uniform int u_channels;  // 0=alpha only, 1=all channels
in vec2 v_uv;
out vec4 fragColor;

float adjust_level(float value) {
    float span = max(u_white_point - u_black_point, 0.0001);
    float result = clamp((value - u_black_point) / span, 0.0, 1.0);
    result = pow(result, 1.0 / max(u_gamma, 0.0001));
    if (u_invert) {
        result = 1.0 - result;
    }
    return result;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    if (u_channels == 0) {
        color.a = adjust_level(color.a);
    } else {
        color.r = adjust_level(color.r);
        color.g = adjust_level(color.g);
        color.b = adjust_level(color.b);
        color.a = adjust_level(color.a);
    }
    fragColor = color;
}
"""

_DISSOLVE_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;   // A = foreground
uniform sampler2D u_texture_b;   // B = background
uniform vec2 u_resolution_a;
uniform vec2 u_resolution_b;
uniform vec2 u_output_resolution;
uniform float u_mix;
in vec2 v_uv;
out vec4 fragColor;

vec4 sampleAtOrigin(sampler2D tex, vec2 uv, vec2 tex_res) {
    vec2 src_uv = uv * u_output_resolution / tex_res;
    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        return vec4(0.0);
    }
    return texture(tex, src_uv);
}

void main() {
    vec4 a = sampleAtOrigin(u_texture_a, v_uv, u_resolution_a);
    vec4 b = sampleAtOrigin(u_texture_b, v_uv, u_resolution_b);
    // mix=0 -> B, mix=1 -> A
    fragColor = mix(b, a, u_mix);
}
"""

_SET_ALPHA_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_mask_texture;  // optional mask input
uniform bool u_has_mask;
uniform int u_mode;     // 0=constant, 1=luma, 2=r, 3=g, 4=b, 5=a
uniform float u_value;  // constant alpha value
// AUD-5306 (S3-A): explicit mask channel selection (0=luma default, 1=R,
// 2=G, 3=B, 4=A), replacing the "m.a > 0 ? m.a : m.r" heuristic.
uniform int u_mask_channel;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    float new_alpha;

    if (u_mode == 0) {
        // constant
        new_alpha = u_value;
    } else if (u_mode == 1) {
        // luma
        new_alpha = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    } else if (u_mode == 2) {
        // r
        new_alpha = color.r;
    } else if (u_mode == 3) {
        // g
        new_alpha = color.g;
    } else if (u_mode == 4) {
        // b
        new_alpha = color.b;
    } else {
        // a (pass through)
        new_alpha = color.a;
    }

    // Modulate by mask if present
    if (u_has_mask) {
        vec4 m = texture(u_mask_texture, v_uv);
        float mask_val;
        if (u_mask_channel == 1) {
            mask_val = m.r;
        } else if (u_mask_channel == 2) {
            mask_val = m.g;
        } else if (u_mask_channel == 3) {
            mask_val = m.b;
        } else if (u_mask_channel == 4) {
            mask_val = m.a;
        } else {
            // Rec.709 luma (default)
            mask_val = dot(m.rgb, vec3(0.2126, 0.7152, 0.0722));
        }
        new_alpha *= mask_val;
    }

    fragColor = vec4(color.rgb, new_alpha);
}
"""

_HUECORRECT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_mask;
uniform float u_hue_shift;        // Hue shift in degrees (-180 to 180)
uniform float u_saturation_mult;  // Saturation multiplier (0 to 4)
uniform float u_brightness_mult;  // Brightness/value multiplier (0 to 4)
uniform float u_range_low;        // Lower hue bound (degrees, 0-360)
uniform float u_range_high;       // Upper hue bound (degrees, 0-360)
uniform float u_range_softness;   // Softness/transition width (degrees)
uniform float u_mix;              // Blend factor (0=original, 1=corrected)
// AUD-5306 (S3-A): explicit mask channel selection (0=luma default, 1=R,
// 2=G, 3=B, 4=A), replacing the "m.a > 0 ? m.a : m.r" heuristic.
uniform int u_mask_channel;
in vec2 v_uv;
out vec4 fragColor;

// Convert RGB to HSV (hue in 0-1 range, saturation and value in 0-1)
vec3 rgb_to_hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

// Convert HSV to RGB
vec3 hsv_to_rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

// Compute weight for a hue value within a range with softness
// Returns 0.0 outside range, 1.0 inside, with smooth transition
float hue_range_weight(float hue_deg, float range_low, float range_high, float softness) {
    // Normalize hues to 0-360
    hue_deg = fract(hue_deg / 360.0) * 360.0;
    
    // Handle wrap-around case (e.g., range 350-20 covers red-orange)
    float range_len;
    if (range_high >= range_low) {
        range_len = range_high - range_low;
    } else {
        // Wrap-around: range crosses 0/360 boundary
        range_len = (360.0 - range_low) + range_high;
    }
    
    // Full range case (0-360) - affects all pixels
    if (range_len >= 359.0) {
        return 1.0;
    }
    
    // Compute distance from hue to range center
    float dist_to_low, dist_to_high;
    
    if (range_high >= range_low) {
        // Normal case
        dist_to_low = hue_deg - range_low;
        dist_to_high = range_high - hue_deg;
        // Handle wrap for hue outside [low, high]
        if (dist_to_low < 0.0) dist_to_low += 360.0;
        if (dist_to_high < 0.0) dist_to_high += 360.0;
    } else {
        // Wrap-around case
        // Check if hue is in [low, 360] OR [0, high]
        if (hue_deg >= range_low || hue_deg <= range_high) {
            if (hue_deg >= range_low) {
                dist_to_low = hue_deg - range_low;
                dist_to_high = (360.0 - hue_deg) + range_high;
            } else {
                dist_to_low = (360.0 - range_low) + hue_deg;
                dist_to_high = range_high - hue_deg;
            }
        } else {
            // Hue is in the excluded middle region
            return 0.0;
        }
    }
    
    // Minimum distance to either edge
    float min_dist = min(dist_to_low, dist_to_high);
    
    // Apply softness: smooth falloff near edges
    if (softness > 0.001) {
        return smoothstep(0.0, softness, min_dist);
    } else {
        // Hard edge - inside or outside
        return min_dist > 0.001 ? 1.0 : 0.0;
    }
}

void main() {
    vec4 color = texture(u_texture, v_uv);

    // AUD-5306 (S3-A): explicit mask channel selection replaces the
    // ambiguous "m.a > 0 ? m.a : m.r" heuristic (default 0 = Rec.709 luma).
    vec4 mask_sample = texture(u_mask, v_uv);
    float mask_val;
    if (u_mask_channel == 1) {
        mask_val = mask_sample.r;
    } else if (u_mask_channel == 2) {
        mask_val = mask_sample.g;
    } else if (u_mask_channel == 3) {
        mask_val = mask_sample.b;
    } else if (u_mask_channel == 4) {
        mask_val = mask_sample.a;
    } else {
        mask_val = dot(mask_sample.rgb, vec3(0.2126, 0.7152, 0.0722));
    }

    // Convert to HSV
    vec3 hsv = rgb_to_hsv(color.rgb);
    float hue_deg = hsv.x * 360.0;
    
    // Compute selection weight based on hue range
    float weight = hue_range_weight(hue_deg, u_range_low, u_range_high, u_range_softness);
    
    // Apply adjustments weighted by selection
    vec3 hsv_adjusted = hsv;
    
    // Hue shift (in degrees, converted to 0-1 range)
    if (abs(u_hue_shift) > 0.01) {
        hsv_adjusted.x = fract(hsv.x + (u_hue_shift / 360.0) * weight);
    }
    
    // Saturation multiplier
    if (abs(u_saturation_mult - 1.0) > 0.001) {
        hsv_adjusted.y = hsv.y * mix(1.0, u_saturation_mult, weight);
        hsv_adjusted.y = clamp(hsv_adjusted.y, 0.0, 1.0);
    }
    
    // Brightness/value multiplier
    if (abs(u_brightness_mult - 1.0) > 0.001) {
        hsv_adjusted.z = hsv.z * mix(1.0, u_brightness_mult, weight);
        // No clamp for HDR - allow values > 1
    }
    
    // Convert back to RGB
    vec3 result = hsv_to_rgb(hsv_adjusted);
    
    // Mix with original (u_mix controls overall effect strength)
    result = mix(color.rgb, result, u_mix * mask_val);
    
    fragColor = vec4(result, color.a);
}
"""

_LUMA_KEYER_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_black_point;
uniform float u_white_point;
uniform float u_softness;
uniform bool u_invert;
uniform int u_output_mode;  // 0=result (premultiplied), 1=matte (alpha only)
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    float luma = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));

    // Compute matte
    float matte;
    float range = max(u_white_point - u_black_point, 0.0001);

    if (u_softness > 0.001) {
        // Soft key with smoothstep
        float soft_black = u_black_point + u_softness * range;
        float soft_white = u_white_point - u_softness * range;
        matte = smoothstep(u_black_point, max(soft_black, u_black_point + 0.0001), luma)
              * (1.0 - smoothstep(max(soft_white, u_white_point - 0.0001), u_white_point, luma));
    } else {
        // Hard key
        matte = step(u_black_point, luma) * (1.0 - step(u_white_point, luma));
        // Include the white_point itself
        if (luma >= u_white_point) matte = 0.0;
    }

    if (u_invert) {
        matte = 1.0 - matte;
    }

    if (u_output_mode == 1) {
        // Matte output: grayscale
        fragColor = vec4(vec3(matte), 1.0);
    } else {
        // Result output: premultiplied
        fragColor = vec4(color.rgb * matte, matte);
    }
}
"""

_MATTE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_low_threshold;
uniform float u_high_threshold;
uniform float u_low_softness;
uniform float u_high_softness;
uniform bool u_invert;
uniform int u_output_mode;  // 0=matte (grayscale), 1=result (premultiplied)
in vec2 v_uv;
out vec4 fragColor;

// Linear interpolation for soft edge transitions
// Returns matte value based on luminance position in threshold range
float compute_matte(float luma, float low_thresh, float high_thresh,
                    float low_soft, float high_soft) {
    // Clamp softness to prevent invalid ranges
    low_soft = clamp(low_soft, 0.0, 0.5);
    high_soft = clamp(high_soft, 0.0, 0.5);

    // Calculate soft transition boundaries
    // low_thresh + low_soft range = region where matte transitions from 0 to 1
    // high_thresh - high_soft range = region where matte transitions from 1 to 0
    float low_soft_end = low_thresh + low_soft * (high_thresh - low_thresh);
    float high_soft_start = high_thresh - high_soft * (high_thresh - low_thresh);

    // Compute matte using linear interpolation
    float matte = 0.0;

    // Below low threshold: transparent (matte = 0)
    if (luma < low_thresh) {
        matte = 0.0;
    }
    // In low soft transition region: linear ramp from 0 to 1
    else if (luma < low_soft_end) {
        float range = max(low_soft_end - low_thresh, 0.0001);
        matte = (luma - low_thresh) / range;
    }
    // In hard matte region (between soft edges): fully opaque (matte = 1)
    else if (luma < high_soft_start) {
        matte = 1.0;
    }
    // In high soft transition region: linear ramp from 1 to 0
    else if (luma < high_thresh) {
        float range = max(high_thresh - high_soft_start, 0.0001);
        matte = 1.0 - (luma - high_soft_start) / range;
    }
    // Above high threshold: transparent (matte = 0)
    else {
        matte = 0.0;
    }

    return matte;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    float luma = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));

    // Compute matte based on luminance thresholds
    float matte = compute_matte(luma, u_low_threshold, u_high_threshold,
                                u_low_softness, u_high_softness);

    // Apply invert
    if (u_invert) {
        matte = 1.0 - matte;
    }

    // Output based on mode
    if (u_output_mode == 0) {
        // Matte output: grayscale (white = opaque, black = transparent)
        fragColor = vec4(vec3(matte), 1.0);
    } else {
        // Result output: premultiplied (original color * matte)
        fragColor = vec4(color.rgb * matte, matte);
    }
}
"""

_NORMALIZE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_input_min;
uniform float u_input_max;
uniform float u_output_min;
uniform float u_output_max;
uniform bool u_clamp_output;
uniform vec4 u_channel_mask;  // 1.0 = normalize, 0.0 = pass through
in vec2 v_uv;
out vec4 fragColor;

float normalize_value(float value, float in_min, float in_max,
                      float out_min, float out_max, bool clamp_out) {
    // Linear remapping: (value - in_min) / (in_max - in_min) * (out_range) + out_min
    float in_range = in_max - in_min;
    float out_range = out_max - out_min;

    // Avoid division by zero
    if (abs(in_range) < 0.00001) {
        return out_min;
    }

    float normalized = (value - in_min) / in_range;
    float result = normalized * out_range + out_min;

    if (clamp_out) {
        result = clamp(result, out_min, out_max);
    }

    return result;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec4 result;

    for (int i = 0; i < 4; i++) {
        float c = color[i];
        // Only normalize channels where mask is 1.0
        if (u_channel_mask[i] > 0.5) {
            c = normalize_value(c, u_input_min, u_input_max,
                                u_output_min, u_output_max, u_clamp_output);
        }
        result[i] = c;
    }

    fragColor = result;
}
"""

_SHARPEN_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_amount;
uniform float u_radius;
uniform float u_threshold;
uniform int u_channels;  // 0=all, 1=rgb, 2=luminance
in vec2 v_uv;
out vec4 fragColor;

void main() {
    if (u_radius < 0.1 || u_amount < 0.001) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    vec4 center = texture(u_texture, v_uv);

    float sigma = max(u_radius * 0.5, 0.1);
    int kernel_size = min(int(ceil(sigma * 3.0)), 32);

    vec4 blurred = vec4(0.0);
    float weight_sum = 0.0;

    for (int y = -32; y <= 32; y++) {
        if (abs(y) > kernel_size) continue;
        for (int x = -32; x <= 32; x++) {
            if (abs(x) > kernel_size) continue;
            float wx = exp(-(float(x * x)) / (2.0 * sigma * sigma));
            float wy = exp(-(float(y * y)) / (2.0 * sigma * sigma));
            float w = wx * wy;
            vec2 offset = vec2(float(x), float(y)) / u_resolution;
            blurred += texture(u_texture, v_uv + offset) * w;
            weight_sum += w;
        }
    }
    blurred /= weight_sum;

    vec4 diff = center - blurred;
    float diff_luma = dot(diff.rgb, vec3(0.2126, 0.7152, 0.0722));

    if (u_threshold > 0.0) {
        float abs_luma = abs(diff_luma);
        float factor = smoothstep(0.0, u_threshold, abs_luma);
        diff *= factor;
    }

    vec4 result;
    if (u_channels == 2) {
        // Luminance mode
        vec3 rgb = center.rgb + diff_luma * u_amount * vec3(0.2126, 0.7152, 0.0722);
        result = vec4(rgb, center.a);
    } else if (u_channels == 1) {
        // RGB mode (no alpha)
        result = vec4(center.rgb + diff.rgb * u_amount, center.a);
    } else {
        // All channels
        result = center + diff * u_amount;
    }

    fragColor = result;
}
"""

_ERODE_DILATE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform int u_operation;  // 0=erode, 1=dilate
uniform int u_size;
uniform int u_edge_mode;  // 0=extend(clamp), 1=reflect, 2=wrap
uniform int u_channels;   // 0=alpha, 1=all
in vec2 v_uv;
out vec4 fragColor;

const int MAX_RADIUS = 50;

void main() {
    if (u_size <= 0) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    int half_size = u_size / 2;
    vec4 result = texture(u_texture, v_uv);

    for (int y = -MAX_RADIUS; y <= MAX_RADIUS; y++) {
        if (abs(y) > half_size) continue;
        for (int x = -MAX_RADIUS; x <= MAX_RADIUS; x++) {
            if (abs(x) > half_size) continue;

            vec2 offset = vec2(float(x), float(y)) / u_resolution;
            vec2 sample_uv = v_uv + offset;

            // Edge mode handling
            if (u_edge_mode == 0) {
                sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));
            } else if (u_edge_mode == 1) {
                // AUD-5304: triangle-wave mirror — uv in [0, 1] stays
                // identity and out-of-range coords fold back toward the
                // nearest edge (abs(mod(uv, 2.0) - 1.0) alone would flip the
                // whole in-frame image 180 degrees).
                sample_uv = 1.0 - abs(mod(sample_uv, 2.0) - 1.0);
            } else {
                sample_uv = fract(sample_uv);
            }

            vec4 sample_val = texture(u_texture, sample_uv);

            if (u_operation == 0) {
                // Erode: take minimum
                if (u_channels == 0) {
                    result.a = min(result.a, sample_val.a);
                } else {
                    result = min(result, sample_val);
                }
            } else {
                // Dilate: take maximum
                if (u_channels == 0) {
                    result.a = max(result.a, sample_val.a);
                } else {
                    result = max(result, sample_val);
                }
            }
        }
    }

    fragColor = result;
}
"""

_CHROMA_KEYER_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec3 u_screen_color;
uniform float u_tolerance;
uniform float u_softness;
uniform bool u_despill;
uniform float u_edge_width;
uniform int u_output_mode;  // 0=result, 1=matte
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);

    // Distance from screen color in UV space
    vec3 diff = abs(color.rgb - u_screen_color);
    float distance = dot(diff, vec3(0.2126, 0.7152, 0.0722));

    // Compute matte with soft edge — pixels matching the screen colour go
    // transparent (matte 0), the foreground stays opaque (matte 1).
    float matte;
    if (u_softness > 0.0) {
        matte = smoothstep(u_tolerance - u_softness, u_tolerance + u_softness, distance);
    } else {
        matte = 1.0 - step(u_tolerance, distance);
    }

    // Edge refinement: soften matte edges
    if (u_edge_width > 0.0) {
        // Widen the transition zone
        float edge_factor = smoothstep(
            0.0,
            u_edge_width * 0.01,
            abs(matte - 0.5) * 2.0
        );
        matte = mix(0.5, matte, edge_factor);
    }

    // Despill: remove screen color spill from foreground
    if (u_despill) {
        float screen_max = max(u_screen_color.r, max(u_screen_color.g, u_screen_color.b));
        if (screen_max > 0.01) {
            // Reduce the screen-dominant channel proportional to others
            for (int i = 0; i < 3; i++) {
                float screen_ch = (i == 0) ? u_screen_color.r : ((i == 1) ? u_screen_color.g : u_screen_color.b);
                if (screen_ch > 0.5) {
                    float other_max = 0.0;
                    for (int j = 0; j < 3; j++) {
                        if (j != i) {
                            float ch = (j == 0) ? color.r : ((j == 1) ? color.g : color.b);
                            other_max = max(other_max, ch);
                        }
                    }
                    float spill = ((i == 0) ? color.r : ((i == 1) ? color.g : color.b));
                    if (spill > other_max) {
                        float corrected = other_max;
                        if (i == 0) color.r = corrected;
                        else if (i == 1) color.g = corrected;
                        else color.b = corrected;
                    }
                }
            }
        }
    }

    if (u_output_mode == 1) {
        // Matte output
        fragColor = vec4(vec3(matte), 1.0);
    } else {
        // Result output: premultiplied alpha
        fragColor = vec4(color.rgb * matte, matte);
    }
}
"""

_DIFFERENCE_KEYER_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;  // Foreground image
uniform sampler2D u_texture_b;  // Reference background (clean plate)
uniform float u_threshold;      // Difference threshold (below = transparent)
uniform float u_softness;       // Edge softness (0-0.5)
uniform float u_gain;           // Matte gain (amplify difference)
uniform int u_channel_mode;     // 0=luma, 1=rgb, 2=individual
uniform bool u_invert;          // Invert matte
uniform int u_output_mode;      // 0=result (premultiplied), 1=matte (alpha only)
in vec2 v_uv;
out vec4 fragColor;

// Calculate difference based on channel mode
float calculate_difference(vec4 a, vec4 b, int mode) {
    if (mode == 0) {
        // Luma mode: compare luminance
        float luma_a = dot(a.rgb, vec3(0.2126, 0.7152, 0.0722));
        float luma_b = dot(b.rgb, vec3(0.2126, 0.7152, 0.0722));
        return abs(luma_a - luma_b);
    } else if (mode == 1) {
        // RGB mode: compare RGB magnitude
        vec3 diff = abs(a.rgb - b.rgb);
        return dot(diff, vec3(0.2126, 0.7152, 0.0722));
    } else {
        // Individual mode: maximum channel difference
        vec3 diff = abs(a.rgb - b.rgb);
        return max(max(diff.r, diff.g), diff.b);
    }
}

void main() {
    vec4 a = texture(u_texture_a, v_uv);
    vec4 b = texture(u_texture_b, v_uv);

    // Compute difference
    float diff = calculate_difference(a, b, u_channel_mode);

    // Apply gain to amplify small differences
    diff = diff * u_gain;

    // Compute matte with soft threshold
    float matte;
    if (u_softness > 0.001) {
        // Soft keying with smoothstep
        float low = u_threshold - u_softness;
        float high = u_threshold + u_softness;
        // Matte = 1.0 when diff > high (different -> opaque)
        // Matte = 0.0 when diff < low (same -> transparent)
        matte = smoothstep(low, high, diff);
    } else {
        // Hard threshold
        matte = step(u_threshold, diff);
    }

    // Invert if requested
    if (u_invert) {
        matte = 1.0 - matte;
    }

    if (u_output_mode == 1) {
        // Matte output: grayscale
        fragColor = vec4(vec3(matte), 1.0);
    } else {
        // Result output: premultiplied alpha
        fragColor = vec4(a.rgb * matte, matte);
    }
}
"""

_KEYLIGHT_FRAGMENT = """
#version 330
// Keylight 1.2-style chroma keyer.
//
// Pipeline (single fragment pass, but matches AE/Foundry Keylight workflow):
//   1. Project source + screen color into a YCbCr-like colour-difference space.
//   2. screen_balance weights the screen-colour channel so a near-pure
//      green (g=1,r=b=0) maps to a "screen axis" with no residual tint.
//   3. Compute screen-distance d = how far the pixel is from the screen axis.
//   4. Map d -> matte via clip_black/white + gain + soft edge.
//   5. Optional despill: subtract screen-colour contribution from the
//      foreground channels so the matte edge is not contaminated.
//
// Output:
//   u_output_mode 0 → result (premultiplied RGBA)
//   u_output_mode 1 → matte (alpha on all channels)
uniform sampler2D u_texture;
uniform vec3  u_screen_color;     // Screen colour (RGB)
uniform float u_screen_balance;   // 0..1, weights Cb/Cr of the screen axis
uniform float u_clip_black;       // matte input black point (0 = transparent)
uniform float u_clip_white;       // matte input white point (1 = opaque)
uniform float u_gain;             // matte gain multiplier
uniform float u_softness;         // matte edge softness (0..1)
uniform float u_despill_strength; // 0..1
uniform int   u_despill_method;   // 0=none, 1=subtract, 2=scale
uniform float u_alpha_offset;     // matte lift/offset
uniform bool  u_invert;           // invert the final matte
uniform int   u_output_mode;      // 0=result, 1=matte
in vec2 v_uv;
out vec4 fragColor;

const mat3 RGBtoYCBCR_601 = mat3(
     0.2990,    -0.168736,  0.5,
     0.5870,    -0.331264, -0.418688,
     0.1140,     0.5,      -0.081312
);

// Difference between this pixel's chroma and the screen axis chroma,
// measured along the screen-colour direction (screen_balance mixes Cb/Cr).
float screen_distance(vec3 rgb, vec3 screen_rgb, float balance) {
    vec3 ycc = RGBtoYCBCR_601 * rgb;
    vec3 scc = RGBtoYCBCR_601 * screen_rgb;

    // The screen axis: normalised chroma vector of the screen colour.
    vec2 screen_axis = normalize(scc.yz + vec2(0.0001));
    // Perpendicular axis (rotated 90 degrees).
    vec2 perp_axis = vec2(-screen_axis.y, screen_axis.x);

    vec2 chroma = ycc.yz - scc.yz;          // chroma relative to screen
    // Project onto screen axis (signed, then take magnitude along it).
    float along = dot(chroma, screen_axis);
    // Weighted perpendicular distance (balance controls mixing).
    float perp = abs(dot(chroma, perp_axis)) * balance;

    return length(vec2(along, perp));
}

vec3 remove_spill(vec3 rgb, vec3 screen_rgb, float strength, int method) {
    // Find the dominant screen channel.
    float screen_max = max(screen_rgb.r, max(screen_rgb.g, screen_rgb.b));
    if (screen_max < 0.01) return rgb;

    if (method == 1) {
        // Subtract: scale down the dominant screen channel in foreground.
        for (int i = 0; i < 3; i++) {
            float s_ch = (i == 0) ? screen_rgb.r : ((i == 1) ? screen_rgb.g : screen_rgb.b);
            if (s_ch > 0.5) {
                float other_max = 0.0;
                for (int j = 0; j < 3; j++) {
                    if (j != i) {
                        float ch = (j == 0) ? rgb.r : ((j == 1) ? rgb.g : rgb.b);
                        other_max = max(other_max, ch);
                    }
                }
                float spill = (i == 0) ? rgb.r : ((i == 1) ? rgb.g : rgb.b);
                float target = mix(spill, other_max, strength);
                if (i == 0) rgb.r = target;
                else if (i == 1) rgb.g = target;
                else rgb.b = target;
            }
        }
        return rgb;
    } else if (method == 2) {
        // Scale: multiply the screen-channel contribution by (1 - strength).
        vec3 ratio = screen_rgb / max(screen_max, 0.0001);
        return rgb * mix(vec3(1.0), (vec3(1.0) - ratio * 0.5), strength);
    }
    return rgb;
}

void main() {
    vec4 color = texture(u_texture, v_uv);

    // 1. Screen distance in colour-difference space.
    float d = screen_distance(color.rgb, u_screen_color, u_screen_balance);

    // 2. Map distance → matte with clip black/white + gain + offset.
    //    Screen colour (d ≈ 0) → transparent; foreground (d ≥ 1) → opaque.
    float matte = clamp(d, 0.0, 1.0);
    matte = (matte * u_gain) + u_alpha_offset;
    matte = clamp(matte, 0.0, 1.0);

    // 3. Clip with black/white points.
    if (u_clip_white > u_clip_black) {
        matte = (matte - u_clip_black) / max(u_clip_white - u_clip_black, 0.0001);
        matte = clamp(matte, 0.0, 1.0);
    }

    // 4. Soft edge around the transition (smoothstep around 0.5 ± softness/2).
    if (u_softness > 0.001) {
        float lo = max(0.0, 0.5 - u_softness * 0.5);
        float hi = min(1.0, 0.5 + u_softness * 0.5);
        matte = smoothstep(lo, hi, matte);
    }

    if (u_invert) {
        matte = 1.0 - matte;
    }

    // 5. Despill on the foreground (only affects RGB, not the matte).
    vec3 fg = color.rgb;
    if (u_despill_method != 0 && u_despill_strength > 0.001) {
        fg = remove_spill(fg, u_screen_color, u_despill_strength, u_despill_method);
    }

    if (u_output_mode == 1) {
        fragColor = vec4(vec3(matte), 1.0);
    } else {
        // Result: premultiplied alpha output.
        fragColor = vec4(fg * matte, matte);
    }
}
"""

_FUSED_FRAGMENT = """
#version 330
// Fused mega-shader for pass fusion.
// Stages are enabled/disabled via boolean uniforms.
// The GPU compiler eliminates dead code for disabled stages.
//
// Fixed stage order:
//   1. Unpremult   (alpha decode)
//   2. CS Transform (color space conversion)
//   3. Grade        (color correction)
//   4. Invert       (channel inversion)
//   5. Shuffle      (channel remap)
//   6. Premult      (alpha encode)

uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;

// ── Stage enables ──
uniform bool u_enable_unpremult;
uniform bool u_enable_cs_transform;
uniform bool u_enable_grade;
uniform bool u_enable_invert;
uniform bool u_enable_shuffle;
uniform bool u_enable_premult;

// ── Grade uniforms ──
uniform vec3 u_grade_lift;
uniform vec3 u_grade_gamma;
uniform vec3 u_grade_gain;
uniform vec3 u_grade_offset;
// AUD-5307: extended grade parameters, mirroring _GRADE_FRAGMENT. Identity
// initializers keep the stage unchanged when the fused-chain driver does
// not set them (it currently only feeds lift/gamma/gain/offset), and a
// driver that sets the full grade set no longer risks a KeyError-dropped
// frame from missing uniform names.
uniform float u_grade_blackpoint = 0.0;
uniform float u_grade_whitepoint = 1.0;
uniform float u_grade_multiply = 1.0;
uniform float u_grade_add = 0.0;

// ── CS Transform uniforms ──
uniform int u_cs_direction;  // 0=srgb->linear, 1=linear->srgb

// ── Invert uniforms ──
uniform vec4 u_invert_channels;

// ── Shuffle uniforms ──
uniform int u_shuffle_r_from;
uniform int u_shuffle_g_from;
uniform int u_shuffle_b_from;
uniform int u_shuffle_a_from;

// ── Helper functions ──

vec3 srgb_to_linear(vec3 c) {
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + 0.055) / 1.055, vec3(2.4));
    return mix(hi, lo, step(c, vec3(0.04045)));
}

vec3 linear_to_srgb(vec3 c) {
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
    return mix(hi, lo, step(c, vec3(0.0031308)));
}

float shuffle_get_channel(vec4 color, int source) {
    if (source == 0) return color.r;
    if (source == 1) return color.g;
    if (source == 2) return color.b;
    if (source == 3) return color.a;
    if (source == 4) return 0.0;
    if (source == 5) return 1.0;
    if (source == 6) return dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    return 0.0;
}

void main() {
    vec4 color = texture(u_texture, v_uv);

    // Stage 1: Unpremult (premultiplied -> straight)
    if (u_enable_unpremult) {
        float inv_a = 1.0 / max(color.a, 0.00001);
        vec3 rgb = color.a > 0.00001 ? color.rgb * inv_a : vec3(0.0);
        color = vec4(rgb, color.a);
    }

    // Stage 2: CS Transform (sRGB <-> Linear)
    if (u_enable_cs_transform) {
        if (u_cs_direction == 0) {
            color.rgb = srgb_to_linear(color.rgb);
        } else {
            color.rgb = linear_to_srgb(color.rgb);
        }
    }

    // Stage 3: Grade (blackpoint/whitepoint -> gain/offset -> gamma -> lift
    // -> multiply/add; AUD-5307: same operator order as _GRADE_FRAGMENT)
    if (u_enable_grade) {
        vec3 graded;
        for (int i = 0; i < 3; i++) {
            float c = color[i];
            c = (c - u_grade_blackpoint) / max(u_grade_whitepoint - u_grade_blackpoint, 0.0001);
            c = c * u_grade_gain[i] + u_grade_offset[i];
            c = pow(max(c, 0.0001), 1.0 / max(u_grade_gamma[i], 0.001));
            c = c + u_grade_lift[i];
            graded[i] = c;
        }
        graded = graded * u_grade_multiply + u_grade_add;
        color = vec4(graded, color.a);
    }

    // Stage 4: Invert (per-channel)
    if (u_enable_invert) {
        color = vec4(
            mix(color.r, 1.0 - color.r, u_invert_channels.r),
            mix(color.g, 1.0 - color.g, u_invert_channels.g),
            mix(color.b, 1.0 - color.b, u_invert_channels.b),
            mix(color.a, 1.0 - color.a, u_invert_channels.a)
        );
    }

    // Stage 5: Shuffle (channel remap)
    if (u_enable_shuffle) {
        color = vec4(
            shuffle_get_channel(color, u_shuffle_r_from),
            shuffle_get_channel(color, u_shuffle_g_from),
            shuffle_get_channel(color, u_shuffle_b_from),
            shuffle_get_channel(color, u_shuffle_a_from)
        );
    }

    // Stage 6: Premult (straight -> premultiplied)
    if (u_enable_premult) {
        color = vec4(color.rgb * color.a, color.a);
    }

    fragColor = color;
}
"""

_FUSED_COLOR_FRAGMENT = """
#version 330
// Enhanced fused mega-shader for color-only pass fusion.
// Combines grade, color_correct, huecorrect, clamp, normalize stages.
// Stages are enabled/disabled via boolean uniforms.
// GPU compiler eliminates dead code for disabled stages.
//
// Extended stage order (color-only):
//   1. Unpremult     (alpha decode)
//   2. CS Transform  (color space conversion)
//   3. Grade         (lift/gamma/gain/offset)
//   4. Color Correct (exposure/contrast/saturation/hue/temp/tint/shadows/midtones/highlights)
//   5. Hue Correct   (selective hue-based adjustments)
//   6. Normalize     (value range normalization)
//   7. Clamp         (value range clamping)
//   8. Invert        (channel inversion)
//   9. Shuffle       (channel remap)
//   10. Premult      (alpha encode)

uniform sampler2D u_texture;
uniform sampler2D u_mask;  // Optional mask for color_correct and huecorrect
uniform bool u_has_mask;
// AUD-5306 (S3-A): explicit mask channel selection (0=luma default, 1=R,
// 2=G, 3=B, 4=A), replacing the "m.a > 0 ? m.a : m.r" heuristic.
uniform int u_mask_channel;
in vec2 v_uv;
out vec4 fragColor;

// ── Stage enables ──
uniform bool u_enable_unpremult;
uniform bool u_enable_cs_transform;
uniform bool u_enable_grade;
uniform bool u_enable_color_correct;
uniform bool u_enable_huecorrect;
uniform bool u_enable_normalize;
uniform bool u_enable_clamp;
uniform bool u_enable_invert;
uniform bool u_enable_shuffle;
uniform bool u_enable_premult;

// ── Grade uniforms ──
uniform vec3 u_grade_lift;
uniform vec3 u_grade_gamma;
uniform vec3 u_grade_gain;
uniform vec3 u_grade_offset;
uniform float u_grade_blackpoint;
uniform float u_grade_whitepoint;
uniform float u_grade_multiply;
uniform float u_grade_add;
uniform vec3 u_grade_channel_mask;
uniform float u_grade_mix;

// ── CS Transform uniforms ──
uniform int u_cs_direction;  // 0=srgb->linear, 1=linear->srgb

// ── Color Correct uniforms ──
uniform float u_cc_exposure;
uniform float u_cc_contrast;
uniform float u_cc_saturation;
uniform float u_cc_hue_shift;
uniform float u_cc_temperature;
uniform float u_cc_tint;
uniform vec3 u_cc_shadows_lifting;
uniform vec3 u_cc_midtones_gamma;
uniform vec3 u_cc_highlights_gain;
uniform vec4 u_cc_channel_selector;
uniform float u_cc_mix;

// ── Hue Correct uniforms ──
uniform float u_hue_hue_shift;
uniform float u_hue_saturation_mult;
uniform float u_hue_brightness_mult;
uniform float u_hue_range_low;
uniform float u_hue_range_high;
uniform float u_hue_range_softness;
uniform float u_hue_mix;

// ── Normalize uniforms ──
uniform float u_norm_input_min;
uniform float u_norm_input_max;
uniform float u_norm_output_min;
uniform float u_norm_output_max;
uniform bool u_norm_clamp_output;
uniform vec4 u_norm_channel_mask;

// ── Clamp uniforms ──
uniform vec4 u_clamp_min_val;
uniform vec4 u_clamp_max_val;
uniform vec4 u_clamp_channel_mask;

// ── Invert uniforms ──
uniform vec4 u_invert_channels;

// ── Shuffle uniforms ──
uniform int u_shuffle_r_from;
uniform int u_shuffle_g_from;
uniform int u_shuffle_b_from;
uniform int u_shuffle_a_from;

// ── Helper functions ──

vec3 srgb_to_linear(vec3 c) {
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + 0.055) / 1.055, vec3(2.4));
    return mix(hi, lo, step(c, vec3(0.04045)));
}

vec3 linear_to_srgb(vec3 c) {
    vec3 lo = c * 12.92;
    vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
    return mix(hi, lo, step(c, vec3(0.0031308)));
}

vec3 rgb_to_hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

vec3 hsv_to_rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

float shuffle_get_channel(vec4 color, int source) {
    if (source == 0) return color.r;
    if (source == 1) return color.g;
    if (source == 2) return color.b;
    if (source == 3) return color.a;
    if (source == 4) return 0.0;
    if (source == 5) return 1.0;
    if (source == 6) return dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    return 0.0;
}

float normalize_value(float value, float in_min, float in_max,
                      float out_min, float out_max, bool clamp_out) {
    float in_range = in_max - in_min;
    float out_range = out_max - out_min;
    if (abs(in_range) < 0.00001) {
        return out_min;
    }
    float normalized = (value - in_min) / in_range;
    float result = normalized * out_range + out_min;
    if (clamp_out) {
        result = clamp(result, out_min, out_max);
    }
    return result;
}

float hue_range_weight(float hue_deg, float range_low, float range_high, float softness) {
    hue_deg = fract(hue_deg / 360.0) * 360.0;
    float range_len;
    if (range_high >= range_low) {
        range_len = range_high - range_low;
    } else {
        range_len = (360.0 - range_low) + range_high;
    }
    if (range_len >= 359.0) {
        return 1.0;
    }
    float dist_to_low, dist_to_high;
    if (range_high >= range_low) {
        dist_to_low = hue_deg - range_low;
        dist_to_high = range_high - hue_deg;
        if (dist_to_low < 0.0) dist_to_low += 360.0;
        if (dist_to_high < 0.0) dist_to_high += 360.0;
    } else {
        if (hue_deg >= range_low || hue_deg <= range_high) {
            if (hue_deg >= range_low) {
                dist_to_low = hue_deg - range_low;
                dist_to_high = (360.0 - hue_deg) + range_high;
            } else {
                dist_to_low = (360.0 - range_low) + hue_deg;
                dist_to_high = range_high - hue_deg;
            }
        } else {
            return 0.0;
        }
    }
    float min_dist = min(dist_to_low, dist_to_high);
    if (softness > 0.001) {
        return smoothstep(0.0, softness, min_dist);
    } else {
        return min_dist > 0.001 ? 1.0 : 0.0;
    }
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    float mask_val = 1.0;
    if (u_has_mask) {
        // AUD-5306 (S3-A): explicit mask channel selection replaces the
        // ambiguous "m.a > 0 ? m.a : m.r" heuristic (default 0 = luma).
        vec4 mask_sample = texture(u_mask, v_uv);
        if (u_mask_channel == 1) {
            mask_val = mask_sample.r;
        } else if (u_mask_channel == 2) {
            mask_val = mask_sample.g;
        } else if (u_mask_channel == 3) {
            mask_val = mask_sample.b;
        } else if (u_mask_channel == 4) {
            mask_val = mask_sample.a;
        } else {
            mask_val = dot(mask_sample.rgb, vec3(0.2126, 0.7152, 0.0722));
        }
    }

    // Stage 1: Unpremult (premultiplied -> straight)
    if (u_enable_unpremult) {
        float inv_a = 1.0 / max(color.a, 0.00001);
        vec3 rgb = color.a > 0.00001 ? color.rgb * inv_a : vec3(0.0);
        color = vec4(rgb, color.a);
    }

    // Stage 2: CS Transform (sRGB <-> Linear)
    if (u_enable_cs_transform) {
        if (u_cs_direction == 0) {
            color.rgb = srgb_to_linear(color.rgb);
        } else {
            color.rgb = linear_to_srgb(color.rgb);
        }
    }

    // Stage 3: Grade (lift/gamma/gain/offset with blackpoint/whitepoint/multiply/add)
    if (u_enable_grade) {
        // AUD-5307: operator order aligned with _GRADE_FRAGMENT:
        // blackpoint/whitepoint normalize -> gain/offset -> gamma -> lift
        // -> multiply/add (whole vec3) -> channel mask * mask * mix.
        // The previous form applied multiply/add *before* gain, so fused
        // and per-node grade results diverged whenever multiply != 1.0
        // or add != 0.0, and channel-masked channels leaked the
        // multiply/add into disabled channels.
        vec3 graded;
        for (int i = 0; i < 3; i++) {
            float c = color[i];
            // Apply blackpoint/whitepoint normalization
            c = (c - u_grade_blackpoint) / max(u_grade_whitepoint - u_grade_blackpoint, 0.0001);
            // Apply gain/offset
            c = c * u_grade_gain[i] + u_grade_offset[i];
            // Apply gamma
            c = pow(max(c, 0.0001), 1.0 / max(u_grade_gamma[i], 0.001));
            // Apply lift
            c = c + u_grade_lift[i];
            graded[i] = c;
        }
        // Apply multiply/add after lift, like _GRADE_FRAGMENT
        graded = graded * u_grade_multiply + u_grade_add;
        // Apply channel mask + spatial mask + master mix
        graded = mix(color.rgb, graded,
                     u_grade_channel_mask * (mask_val * u_grade_mix));
        color = vec4(graded, color.a);
    }

    // Stage 4: Color Correct (exposure/contrast/saturation/hue/temp/tint/tonal)
    if (u_enable_color_correct) {
        vec3 result = color.rgb;

        // Exposure: color * pow(2.0, exposure)
        result = result * pow(2.0, u_cc_exposure);

        // Contrast: (color - 0.5) * contrast + 0.5
        result = (result - 0.5) * u_cc_contrast + 0.5;

        // Saturation: mix(luma, color, saturation)
        float luma = dot(result, vec3(0.2126, 0.7152, 0.0722));
        result = mix(vec3(luma), result, u_cc_saturation);

        // Hue shift: rotate in HSV space
        if (abs(u_cc_hue_shift) > 0.01) {
            vec3 hsv = rgb_to_hsv(result);
            hsv.x = fract(hsv.x + u_cc_hue_shift / 360.0);
            result = hsv_to_rgb(hsv);
        }

        // Temperature / Tint
        result.r += u_cc_temperature * 0.1;
        result.b -= u_cc_temperature * 0.1;
        result.g += u_cc_tint * 0.05;
        result.r -= u_cc_tint * 0.025;
        result.b -= u_cc_tint * 0.025;

        // Shadows/Midtones/Highlights (tonal range adjustments)
        float tonal_luma = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
        float shadow_w = 1.0 - smoothstep(0.0, 0.35, tonal_luma);
        float highlight_w = smoothstep(0.65, 1.0, tonal_luma);
        float midtone_w = 1.0 - shadow_w - highlight_w;

        // Shadows: lift (additive)
        result += u_cc_shadows_lifting * shadow_w;

        // Midtones: gamma (power)
        for (int i = 0; i < 3; i++) {
            float gamma = u_cc_midtones_gamma[i];
            if (abs(gamma - 1.0) > 0.001) {
                float mid_val = result[i];
                mid_val = pow(max(mid_val, 0.0001), 1.0 / max(gamma, 0.001));
                result[i] = mix(result[i], mid_val, midtone_w);
            }
        }

        // Highlights: gain (multiply)
        result += (u_cc_highlights_gain - vec3(1.0)) * result * highlight_w;

        // Channel selector
        result = vec3(
            mix(color.r, result.r, u_cc_channel_selector.r),
            mix(color.g, result.g, u_cc_channel_selector.g),
            mix(color.b, result.b, u_cc_channel_selector.b)
        );

        // Mix control
        result = mix(color.rgb, result, u_cc_mix * mask_val);

        color = vec4(result, color.a);
    }

    // Stage 5: Hue Correct (selective hue-based adjustments)
    if (u_enable_huecorrect) {
        vec3 hsv = rgb_to_hsv(color.rgb);
        float hue_deg = hsv.x * 360.0;
        float weight = hue_range_weight(hue_deg, u_hue_range_low, u_hue_range_high, u_hue_range_softness);

        vec3 hsv_adjusted = hsv;

        // Hue shift
        if (abs(u_hue_hue_shift) > 0.01) {
            hsv_adjusted.x = fract(hsv.x + (u_hue_hue_shift / 360.0) * weight);
        }

        // Saturation multiplier
        if (abs(u_hue_saturation_mult - 1.0) > 0.001) {
            hsv_adjusted.y = hsv.y * mix(1.0, u_hue_saturation_mult, weight);
            hsv_adjusted.y = clamp(hsv_adjusted.y, 0.0, 1.0);
        }

        // Brightness/value multiplier (HDR-safe, no clamp)
        if (abs(u_hue_brightness_mult - 1.0) > 0.001) {
            hsv_adjusted.z = hsv.z * mix(1.0, u_hue_brightness_mult, weight);
        }

        vec3 result = hsv_to_rgb(hsv_adjusted);
        result = mix(color.rgb, result, u_hue_mix * mask_val);
        color = vec4(result, color.a);
    }

    // Stage 6: Normalize (value range normalization)
    if (u_enable_normalize) {
        vec4 result;
        for (int i = 0; i < 4; i++) {
            float c = color[i];
            if (u_norm_channel_mask[i] > 0.5) {
                c = normalize_value(c, u_norm_input_min, u_norm_input_max,
                                    u_norm_output_min, u_norm_output_max, u_norm_clamp_output);
            }
            result[i] = c;
        }
        color = result;
    }

    // Stage 7: Clamp (value range clamping)
    if (u_enable_clamp) {
        vec4 result;
        for (int i = 0; i < 4; i++) {
            float c = color[i];
            if (u_clamp_channel_mask[i] > 0.5) {
                c = clamp(c, u_clamp_min_val[i], u_clamp_max_val[i]);
            }
            result[i] = c;
        }
        color = result;
    }

    // Stage 8: Invert (per-channel)
    if (u_enable_invert) {
        color = vec4(
            mix(color.r, 1.0 - color.r, u_invert_channels.r),
            mix(color.g, 1.0 - color.g, u_invert_channels.g),
            mix(color.b, 1.0 - color.b, u_invert_channels.b),
            mix(color.a, 1.0 - color.a, u_invert_channels.a)
        );
    }

    // Stage 9: Shuffle (channel remap)
    if (u_enable_shuffle) {
        color = vec4(
            shuffle_get_channel(color, u_shuffle_r_from),
            shuffle_get_channel(color, u_shuffle_g_from),
            shuffle_get_channel(color, u_shuffle_b_from),
            shuffle_get_channel(color, u_shuffle_a_from)
        );
    }

    // Stage 10: Premult (straight -> premultiplied)
    if (u_enable_premult) {
        color = vec4(color.rgb * color.a, color.a);
    }

    fragColor = color;
}
"""

_CORNERPIN_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_h00;
uniform float u_h01;
uniform float u_h02;
uniform float u_h10;
uniform float u_h11;
uniform float u_h12;
uniform float u_h20;
uniform float u_h21;
uniform int u_interpolation;  // 0=bilinear, 1=bicubic
in vec2 v_uv;
out vec4 fragColor;

// Apply inverse homography to find source UV from output UV
// The homography maps source -> output, so we need its inverse
vec2 apply_inverse_homography(vec2 uv) {
    // Forward transform: src -> dst
    // We need to invert: given dst_uv, find src_uv
    // For a homography H with elements h00-h21:
    //   dst_x = (h00*src_x + h01*src_y + h02) / (h20*src_x + h21*src_y + 1)
    //   dst_y = (h10*src_x + h11*src_y + h12) / (h20*src_x + h21*src_y + 1)
    //
    // Inverse: solve for src_x, src_y given dst_x, dst_y
    // Let w = h20*src_x + h21*src_y + 1
    // dst_x * w = h00*src_x + h01*src_y + h02
    // dst_y * w = h10*src_x + h11*src_y + h12
    //
    // Rearranged:
    // (h00 - dst_x*h20)*src_x + (h01 - dst_x*h21)*src_y = dst_x - h02
    // (h10 - dst_y*h20)*src_x + (h11 - dst_y*h21)*src_y = dst_y - h12

    float dx = uv.x;
    float dy = uv.y;

    float a = u_h00 - dx * u_h20;
    float b = u_h01 - dx * u_h21;
    float c = dx - u_h02;
    float d = u_h10 - dy * u_h20;
    float e = u_h11 - dy * u_h21;
    float f = dy - u_h12;

    float det = a * e - b * d;
    if (abs(det) < 0.00001) {
        // Degenerate case
        return vec2(-1.0, -1.0);
    }

    float inv_det = 1.0 / det;
    float src_x = (e * c - b * f) * inv_det;
    float src_y = (a * f - d * c) * inv_det;

    return vec2(src_x, src_y);
}

// Bicubic interpolation (Catmull-Rom kernel)
vec4 sample_bicubic(sampler2D tex, vec2 uv, vec2 tex_size) {
    vec2 texel_pos = uv * tex_size - 0.5;
    vec2 texel_f = fract(texel_pos);
    vec2 texel_i = floor(texel_pos);

    vec4 result = vec4(0.0);
    float weight_sum = 0.0;

    // Catmull-Rom weights
    for (int j = 0; j < 4; j++) {
        for (int i = 0; i < 4; i++) {
            vec2 offset = vec2(float(i - 1), float(j - 1));
            vec2 sample_pos = (texel_i + offset + 0.5) / tex_size;

            // Weight calculation
            float wx = abs(texel_f.x - offset.x);
            float wy = abs(texel_f.y - offset.y);
            float weight_x = (wx < 1.0) ? (1.5 * wx * wx * wx - 2.5 * wx * wx + 1.0)
                                       : (-0.5 * wx * wx * wx + 2.5 * wx * wx - 4.0 * wx + 2.0);
            float weight_y = (wy < 1.0) ? (1.5 * wy * wy * wy - 2.5 * wy * wy + 1.0)
                                       : (-0.5 * wy * wy * wy + 2.5 * wy * wy - 4.0 * wy + 2.0);
            float weight = weight_x * weight_y;

            result += texture(tex, sample_pos) * weight;
            weight_sum += weight;
        }
    }

    return result / max(weight_sum, 0.00001);
}

void main() {
    vec2 src_uv = apply_inverse_homography(v_uv);

    // Check bounds — outside source image is transparent
    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        fragColor = vec4(0.0, 0.0, 0.0, 0.0);
        return;
    }

    vec2 tex_size = u_resolution;

    if (u_interpolation == 1) {
        // Bicubic
        fragColor = sample_bicubic(u_texture, src_uv, tex_size);
    } else {
        // Bilinear (default)
        fragColor = texture(u_texture, src_uv);
    }
}
"""

_REMOVE_CHANNELS_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform bool u_remove_r;
uniform bool u_remove_g;
uniform bool u_remove_b;
uniform bool u_remove_a;
uniform float u_fill_value;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 color = texture(u_texture, v_uv);
    float fill = u_fill_value;

    float r = u_remove_r ? fill : color.r;
    float g = u_remove_g ? fill : color.g;
    float b = u_remove_b ? fill : color.b;
    float a = u_remove_a ? fill : color.a;

    fragColor = vec4(r, g, b, a);
}
"""

# ── LOD (Level of Detail) shaders for viewport-based optimization ──

_DOWNSAMPLE_FRAGMENT = """
#version 330
// High-quality downsample using 2x2 box filter with proper averaging
uniform sampler2D u_texture;
uniform vec2 u_src_resolution;
uniform vec2 u_dst_resolution;
uniform float u_scale;  // Downsample scale factor (e.g., 0.25 for quarter-res)
in vec2 v_uv;
out vec4 fragColor;

void main() {
    // Compute source pixel position
    vec2 src_pixel = v_uv * u_dst_resolution / u_scale;
    vec2 src_uv = src_pixel / u_src_resolution;

    // 2x2 box filter for high-quality downsample
    vec2 texel_size = vec2(1.0) / u_src_resolution;
    vec2 offset = texel_size * 0.5;  // Center of 2x2 block

    vec4 c0 = texture(u_texture, src_uv + vec2(-offset.x, -offset.y));
    vec4 c1 = texture(u_texture, src_uv + vec2(offset.x, -offset.y));
    vec4 c2 = texture(u_texture, src_uv + vec2(-offset.x, offset.y));
    vec4 c3 = texture(u_texture, src_uv + vec2(offset.x, offset.y));

    fragColor = (c0 + c1 + c2 + c3) * 0.25;
}
"""

_UPSAMPLE_FRAGMENT = """
#version 330
// High-quality upsample using bilinear interpolation
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    // Map destination UV to source UV
    vec2 src_uv = v_uv;

    // Bilinear interpolation (default texture sampling)
    fragColor = texture(u_texture, src_uv);
}
"""

# ── LUT3D Shader for LutNode ──
_LUT3D_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler3D u_lut_tex;
uniform float u_intensity;
uniform float u_input_min;
uniform float u_input_max;
uniform float u_output_min;
uniform float u_output_max;
uniform float u_mix;
uniform sampler2D u_mask;
// AUD-5306 (S3-A): explicit mask channel selection (0=luma default, 1=R,
// 2=G, 3=B, 4=A), replacing the "m.a > 0 ? m.a : m.r" heuristic.
uniform int u_mask_channel;
in vec2 v_uv;
out vec4 fragColor;

// HDR-safe LUT application
// Values within [0,1] are transformed by the LUT
// Values outside are linearly extrapolated to preserve HDR data
vec3 apply_lut_hdr_safe(vec3 color, sampler3D lut_tex, float intensity) {
    // SDR part: apply LUT with trilinear interpolation
    vec3 sdr_part = clamp(color, 0.0, 1.0);
    // LUT 网格点 i 对应输入 i/(n-1)，而 GL texel i 中心位于 (i+0.5)/n：
    // 先映射到 texel 中心坐标，消除边缘半 texel 拉伸。
    float lut_n = float(textureSize(lut_tex, 0).x);
    vec3 lut_result = texture(lut_tex, (sdr_part * (lut_n - 1.0) + 0.5) / lut_n).rgb;
    vec3 blended_sdr = mix(sdr_part, lut_result, intensity);

    // Preserve HDR highlights: scale proportionally
    float max_orig = max(max(color.r, color.g), color.b);
    if (max_orig > 1.0) {
        float max_sdr = max(max(sdr_part.r, sdr_part.g), sdr_part.b);
        if (max_sdr > 1e-5) {
            blended_sdr *= (max_orig / max_sdr);
        }
    }

    // Preserve negative values (shadow detail)
    vec3 negative_excess = min(color, vec3(0.0));
    return blended_sdr + negative_excess;
}

void main() {
    vec4 tex_color = texture(u_texture, v_uv);
    vec3 color = tex_color.rgb;

    // AUD-5306 (S3-A): explicit mask channel selection replaces the
    // ambiguous "m.a > 0 ? m.a : m.r" heuristic (default 0 = Rec.709 luma).
    vec4 mask_sample = texture(u_mask, v_uv);
    float mask_val;
    if (u_mask_channel == 1) {
        mask_val = mask_sample.r;
    } else if (u_mask_channel == 2) {
        mask_val = mask_sample.g;
    } else if (u_mask_channel == 3) {
        mask_val = mask_sample.b;
    } else if (u_mask_channel == 4) {
        mask_val = mask_sample.a;
    } else {
        mask_val = dot(mask_sample.rgb, vec3(0.2126, 0.7152, 0.0722));
    }

    // Input range mapping
    // Map [input_min, input_max] to [0, 1] for LUT sampling
    if (u_input_min != 0.0 || u_input_max != 1.0) {
        float range = u_input_max - u_input_min;
        if (range > 1e-5) {
            color = (color - u_input_min) / range;
        }
    }

    // Apply LUT
    vec3 lut_result = apply_lut_hdr_safe(color, u_lut_tex, u_intensity);

    // Output range mapping
    // Map [0, 1] to [output_min, output_max]
    if (u_output_min != 0.0 || u_output_max != 1.0) {
        lut_result = lut_result * (u_output_max - u_output_min) + u_output_min;
    }

    // Mix control: blend between original and LUT result
    if (u_mix < 1.0) {
        vec3 original = tex_color.rgb;
        lut_result = mix(original, lut_result, u_mix);
    }

    // Apply mask modulation
    lut_result = mix(tex_color.rgb, lut_result, mask_val);

    fragColor = vec4(lut_result, tex_color.a);
}
"""

# ── Separable Gaussian Blur Compute Shader (Single Pass with Texture Output) ──
# Optimized for horizontal pass: reads from input texture, writes to output texture
_BLUR_H_COMPUTE_SSBO = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_edge_policy;

// Output: RGBA16F texture bound as image2D
layout(rgba16f) writeonly uniform image2D u_output;

const int MAX_KERNEL = 64;

float gaussian_weight(float x, float sigma) {
    return exp(-(x * x) / (2.0 * sigma * sigma));
}

void main() {
    vec2 tex_size = u_resolution;
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);

    if (global_id.x >= int(tex_size.x) || global_id.y >= int(tex_size.y)) {
        return;
    }

    float radius = max(u_radius, 0.1);
    float sigma = radius * 0.5;
    int kernel_size = min(int(ceil(sigma * 3.0)), MAX_KERNEL);

    vec2 center_uv = vec2(global_id + ivec2(0.5)) / tex_size;
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;

    for (int i = -MAX_KERNEL; i <= MAX_KERNEL; i++) {
        if (abs(i) > kernel_size) continue;

        float x = float(i);
        float weight = gaussian_weight(x, sigma);

        vec2 sample_uv = center_uv + vec2(x / tex_size.x, 0.0);

        // Edge policy
        if (u_edge_policy == 0) {
            sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));
        } else if (u_edge_policy == 1) {
            sample_uv = fract(sample_uv);
        }

        color += texture(u_input, sample_uv) * weight;
        weight_sum += weight;
    }

    imageStore(u_output, global_id, color / max(weight_sum, 0.00001));
}
"""

_BLUR_V_COMPUTE_SSBO = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_edge_policy;

layout(rgba16f) writeonly uniform image2D u_output;

const int MAX_KERNEL = 64;

float gaussian_weight(float y, float sigma) {
    return exp(-(y * y) / (2.0 * sigma * sigma));
}

void main() {
    vec2 tex_size = u_resolution;
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);

    if (global_id.x >= int(tex_size.x) || global_id.y >= int(tex_size.y)) {
        return;
    }

    float radius = max(u_radius, 0.1);
    float sigma = radius * 0.5;
    int kernel_size = min(int(ceil(sigma * 3.0)), MAX_KERNEL);

    vec2 center_uv = vec2(global_id + ivec2(0.5)) / tex_size;
    vec4 color = vec4(0.0);
    float weight_sum = 0.0;

    for (int j = -MAX_KERNEL; j <= MAX_KERNEL; j++) {
        if (abs(j) > kernel_size) continue;

        float y = float(j);
        float weight = gaussian_weight(y, sigma);

        vec2 sample_uv = center_uv + vec2(0.0, y / tex_size.y);

        if (u_edge_policy == 0) {
            sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));
        } else if (u_edge_policy == 1) {
            sample_uv = fract(sample_uv);
        }

        color += texture(u_input, sample_uv) * weight;
        weight_sum += weight;
    }

    imageStore(u_output, global_id, color / max(weight_sum, 0.00001));
}
"""

# ── Erode/Dilate Compute Shader ──
_ERODE_DILATE_COMPUTE = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform int u_operation;  // 0=erode, 1=dilate
uniform int u_size;       // Kernel size
uniform int u_channels;   // 0=alpha, 1=all

layout(rgba16f) writeonly uniform image2D u_output;

const int MAX_RADIUS = 50;

void main() {
    vec2 tex_size = u_resolution;
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);

    if (global_id.x >= int(tex_size.x) || global_id.y >= int(tex_size.y)) {
        return;
    }

    int half_size = min(u_size / 2, MAX_RADIUS);

    vec2 center_uv = vec2(global_id + ivec2(0.5)) / tex_size;
    vec4 result = texture(u_input, center_uv);

    // Process kernel neighborhood
    for (int dy = -MAX_RADIUS; dy <= MAX_RADIUS; dy++) {
        if (abs(dy) > half_size) continue;
        for (int dx = -MAX_RADIUS; dx <= MAX_RADIUS; dx++) {
            if (abs(dx) > half_size) continue;

            vec2 sample_uv = center_uv + vec2(float(dx), float(dy)) / tex_size;
            sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));

            vec4 sample_val = texture(u_input, sample_uv);

            if (u_operation == 0) {
                // Erode: take minimum
                if (u_channels == 0) {
                    result.a = min(result.a, sample_val.a);
                } else {
                    result = min(result, sample_val);
                }
            } else {
                // Dilate: take maximum
                if (u_channels == 0) {
                    result.a = max(result.a, sample_val.a);
                } else {
                    result = max(result, sample_val);
                }
            }
        }
    }

    imageStore(u_output, global_id, result);
}
"""

# ── Shared Memory Blur Compute Shader (Optimized for Large Radius) ──
# This version uses shared memory to cache texture data within work groups
# More efficient for radius > 32 where texture fetches dominate
_BLUR_H_COMPUTE_SHARED = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_edge_policy;

layout(rgba16f) writeonly uniform image2D u_output;

// AUD-5300: the work group caches a 16x16 pixel tile as 16 padded rows of
// 16 + 2 * MAX_KERNEL texels (the old flat s_row[] array was trampled by all
// 16 tile rows and its padding was only partially initialized).
// 16 * 128 * 4 floats = 8192 floats = 32 KB, exactly at (not beyond) the
// GL-guaranteed MAX_COMPUTE_SHARED_MEMORY_SIZE minimum.
const int MAX_KERNEL = 56;  // 16 + 2 * 56 = 128 columns per row
shared vec4 s_tile[16][16 + 2 * MAX_KERNEL];

float gaussian_weight(float x, float sigma) {
    return exp(-(x * x) / (2.0 * sigma * sigma));
}

void main() {
    vec2 tex_size = u_resolution;
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);
    // Out-of-bounds threads must stay alive: they still participate in the
    // cooperative load and reach barrier(); they only skip the imageStore.
    bool active = global_id.x < int(tex_size.x) && global_id.y < int(tex_size.y);

    float sigma = max(u_radius * 0.5, 0.1);
    int kernel_size = min(int(ceil(sigma * 3.0)), MAX_KERNEL);

    ivec2 local_id = ivec2(gl_LocalInvocationID.xy);

    // Base X coordinate for this work group
    int base_x = int(gl_WorkGroupID.x) * 16;
    int row = local_id.y;
    float row_v = (float(global_id.y) + 0.5) / tex_size.y;

    // AUD-5300 fix: cooperative strided load with compile-time-constant
    // bounds. The 16 threads of tile row `row` fill every slot of the padded
    // row [base_x - MAX_KERNEL, base_x + 15 + MAX_KERNEL]; slot s is written
    // by thread (s % 16) exactly once, so the whole padding region read by
    // the convolution below is fully initialized.
    for (int i = local_id.x; i < 16 + 2 * MAX_KERNEL; i += 16) {
        int load_x = base_x - MAX_KERNEL + i;
        float load_u;
        if (load_x < 0) {
            load_u = (u_edge_policy == 0) ? 0.0 : fract(float(load_x) / tex_size.x);
        } else if (load_x >= int(tex_size.x)) {
            load_u = (u_edge_policy == 0) ? 1.0 : fract(float(load_x) / tex_size.x);
        } else {
            load_u = (float(load_x) + 0.5) / tex_size.x;
        }
        s_tile[row][i] = texture(u_input, vec2(load_u, row_v));
    }

    // Synchronize: all threads must finish loading before convolution
    barrier();

    if (active) {
        // Apply Gaussian blur from shared memory
        // Shared index: local_id.x + kernel_size (for padding offset)
        int shared_idx = kernel_size + local_id.x;
        vec4 color = vec4(0.0);
        float weight_sum = 0.0;

        for (int i = -kernel_size; i <= kernel_size; i++) {
            float weight = gaussian_weight(float(i), sigma);
            color += s_tile[row][shared_idx + i] * weight;
            weight_sum += weight;
        }

        imageStore(u_output, global_id, color / max(weight_sum, 0.00001));
    }
}
"""

_BLUR_V_COMPUTE_SHARED = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform float u_radius;
uniform int u_edge_policy;

layout(rgba16f) writeonly uniform image2D u_output;

// AUD-5300: vertical mirror of _BLUR_H_COMPUTE_SHARED — one padded column
// per tile column (the old flat s_column[] array was trampled by all 16 tile
// columns and its padding was only partially initialized).
// 128 * 16 * 4 floats = 8192 floats = 32 KB, exactly at (not beyond) the
// GL-guaranteed MAX_COMPUTE_SHARED_MEMORY_SIZE minimum.
const int MAX_KERNEL = 56;  // 16 + 2 * 56 = 128 rows per column
shared vec4 s_tile[16 + 2 * MAX_KERNEL][16];

float gaussian_weight(float y, float sigma) {
    return exp(-(y * y) / (2.0 * sigma * sigma));
}

void main() {
    vec2 tex_size = u_resolution;
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);
    // Out-of-bounds threads must stay alive: they still participate in the
    // cooperative load and reach barrier(); they only skip the imageStore.
    bool active = global_id.x < int(tex_size.x) && global_id.y < int(tex_size.y);

    float sigma = max(u_radius * 0.5, 0.1);
    int kernel_size = min(int(ceil(sigma * 3.0)), MAX_KERNEL);

    ivec2 local_id = ivec2(gl_LocalInvocationID.xy);

    // Base Y coordinate for this work group
    int base_y = int(gl_WorkGroupID.y) * 16;
    int col = local_id.x;
    float col_u = (float(global_id.x) + 0.5) / tex_size.x;

    // AUD-5300 fix: cooperative strided load with compile-time-constant
    // bounds. The 16 threads of tile column `col` fill every slot of the
    // padded column [base_y - MAX_KERNEL, base_y + 15 + MAX_KERNEL]; slot s
    // is written by thread (s % 16) exactly once, so the whole padding region
    // read by the convolution below is fully initialized.
    for (int i = local_id.y; i < 16 + 2 * MAX_KERNEL; i += 16) {
        int load_y = base_y - MAX_KERNEL + i;
        float load_v;
        if (load_y < 0) {
            load_v = (u_edge_policy == 0) ? 0.0 : fract(float(load_y) / tex_size.y);
        } else if (load_y >= int(tex_size.y)) {
            load_v = (u_edge_policy == 0) ? 1.0 : fract(float(load_y) / tex_size.y);
        } else {
            load_v = (float(load_y) + 0.5) / tex_size.y;
        }
        s_tile[i][col] = texture(u_input, vec2(col_u, load_v));
    }

    barrier();

    if (active) {
        int shared_idx = kernel_size + local_id.y;
        vec4 color = vec4(0.0);
        float weight_sum = 0.0;

        for (int j = -kernel_size; j <= kernel_size; j++) {
            float weight = gaussian_weight(float(j), sigma);
            color += s_tile[shared_idx + j][col] * weight;
            weight_sum += weight;
        }

        imageStore(u_output, global_id, color / max(weight_sum, 0.00001));
    }
}
"""

# ── Morphological Erode/Dilate with Shared Memory ──
_ERODE_DILATE_COMPUTE_SHARED = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform int u_operation;  // 0=erode, 1=dilate
uniform int u_size;
uniform int u_channels;   // 0=alpha, 1=all

layout(rgba16f) writeonly uniform image2D u_output;

// AUD-5302: the old (16 + 100)^2 vec4 tile needed 215,296 B of shared
// memory — far beyond the GL-guaranteed MAX_COMPUTE_SHARED_MEMORY_SIZE
// (32 KB), so the program could not link on most hardware. Tiled rolling
// window instead: only one 16x16 chunk (4 KB) is resident at a time and the
// padded sampling window around the 16x16 output tile is covered in
// multiple passes of (cooperative load -> barrier -> reduce -> barrier).
const int MAX_RADIUS = 50;
const int MAX_CHUNKS = 8;  // ceil((16 + 2 * MAX_RADIUS) / 16) = ceil(116 / 16)
shared vec4 s_chunk[16][16];

void main() {
    vec2 tex_size = u_resolution;
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);
    // AUD-5301/5303: out-of-bounds threads keep participating in the
    // cooperative load and the barriers; they only skip the imageStore.
    bool active = global_id.x < int(tex_size.x) && global_id.y < int(tex_size.y);

    int half_size = min(u_size / 2, MAX_RADIUS);

    ivec2 local_id = ivec2(gl_LocalInvocationID.xy);
    ivec2 base_pos = ivec2(gl_WorkGroupID.xy) * 16;

    // Padded sampling window around this work group's 16x16 output tile
    ivec2 win_origin = base_pos - ivec2(half_size);
    int win_extent = 16 + 2 * half_size;  // window texels per axis

    vec4 result = vec4(0.0);
    if (active) {
        result = texture(u_input, vec2(global_id + ivec2(0.5)) / tex_size);
    }

    // AUD-5301 fix: every window texel is loaded exactly once via a chunk
    // grid — the old corner-block scheme left the four edge strips
    // uninitialized while the kernel still read them. Chunk (ci, cj) covers
    // window-relative offsets [ci * 16, ci * 16 + 15] x [cj * 16, cj * 16 + 15].
    for (int cj = 0; cj < MAX_CHUNKS; cj++) {
        for (int ci = 0; ci < MAX_CHUNKS; ci++) {
            bool chunk_used = (ci * 16 < win_extent) && (cj * 16 < win_extent);
            if (chunk_used) {
                // All 256 threads cooperatively load the 16x16 chunk
                // (clamped to the image = extend edges, unchanged semantics)
                int load_x = win_origin.x + ci * 16 + local_id.x;
                int load_y = win_origin.y + cj * 16 + local_id.y;
                vec2 load_uv = vec2(clamp(load_x, 0, int(tex_size.x) - 1),
                                    clamp(load_y, 0, int(tex_size.y) - 1)) / tex_size;
                s_chunk[local_id.y][local_id.x] = texture(u_input, load_uv);
            }
            barrier();
            if (chunk_used && active) {
                // Reduce over the part of the chunk inside this thread's
                // [-half_size, half_size]^2 structuring element: chunk-local
                // i maps to offset dx = ci * 16 + i - half_size - local_id.x,
                // so valid i span [local_id.x - ci * 16,
                // local_id.x - ci * 16 + 2 * half_size] clamped to [0, 15].
                int dx_base = local_id.x - ci * 16;
                int dy_base = local_id.y - cj * 16;
                int x_lo = max(0, dx_base);
                int x_hi = min(15, dx_base + 2 * half_size);
                int y_lo = max(0, dy_base);
                int y_hi = min(15, dy_base + 2 * half_size);
                for (int j = y_lo; j <= y_hi; j++) {
                    for (int i = x_lo; i <= x_hi; i++) {
                        vec4 sample_val = s_chunk[j][i];

                        if (u_operation == 0) {
                            // Erode: minimum
                            if (u_channels == 0) {
                                result.a = min(result.a, sample_val.a);
                            } else {
                                result = min(result, sample_val);
                            }
                        } else {
                            // Dilate: maximum
                            if (u_channels == 0) {
                                result.a = max(result.a, sample_val.a);
                            } else {
                                result = max(result, sample_val);
                            }
                        }
                    }
                }
            }
            // All reads must finish before the next chunk overwrites s_chunk
            barrier();
        }
    }

    if (active) {
        imageStore(u_output, global_id, result);
    }
}
"""

# ── Deep Merge Shader ──
# Deep compositing: merge two RGBA+Depth images based on Z values.
# Supports depth-based sorting and compositing.
_DEEP_MERGE_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;   // A = foreground (RGBA)
uniform sampler2D u_texture_b;   // B = background (RGBA)
uniform sampler2D u_depth_a;     // A depth channel
uniform sampler2D u_depth_b;     // B depth channel
uniform sampler2D u_texture_mask; // optional mask
uniform vec2 u_resolution_a;     // A texture pixel dimensions
uniform vec2 u_resolution_b;     // B texture pixel dimensions
uniform vec2 u_output_resolution; // Output canvas pixel dimensions
uniform float u_opacity;
uniform bool u_has_depth_a;      // whether A has depth info
uniform bool u_has_depth_b;      // whether B has depth info
uniform bool u_has_mask;         // whether mask texture is bound
uniform int u_depth_mode;        // 0=z_front, 1=z_back, 2=z_merge
uniform float u_depth_tolerance; // tolerance for depth comparison
uniform bool u_a_straight;       // true if A is straight alpha
uniform bool u_b_straight;       // true if B is straight alpha
// AUD-5306 (S3-A): explicit mask channel selection (0=luma default, 1=R,
// 2=G, 3=B, 4=A), replacing the "m.a > 0 ? m.a : m.r" heuristic.
uniform int u_mask_channel;
in vec2 v_uv;
out vec4 fragColor;

// Sample a texture at its natural position within the output canvas.
vec4 sampleAtOrigin(sampler2D tex, vec2 uv, vec2 tex_res, vec2 out_res) {
    vec2 src_uv = uv * out_res / tex_res;
    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        return vec4(0.0);
    }
    return texture(tex, src_uv);
}

// Sample depth at origin
float sampleDepthAtOrigin(sampler2D depth_tex, vec2 uv, vec2 tex_res, vec2 out_res) {
    vec2 src_uv = uv * out_res / tex_res;
    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        return 1.0;  // Far depth for out-of-bounds
    }
    return texture(depth_tex, src_uv).r;
}

void main() {
    // Sample A and B at their natural positions
    vec4 a = sampleAtOrigin(u_texture_a, v_uv, u_resolution_a, u_output_resolution);
    vec4 b = sampleAtOrigin(u_texture_b, v_uv, u_resolution_b, u_output_resolution);

    // Convert to premultiplied if needed
    if (u_a_straight) { a.rgb *= a.a; }
    if (u_b_straight) { b.rgb *= b.a; }

    // Sample depth values
    float depth_a = u_has_depth_a ? sampleDepthAtOrigin(u_depth_a, v_uv, u_resolution_a, u_output_resolution) : 1.0;
    float depth_b = u_has_depth_b ? sampleDepthAtOrigin(u_depth_b, v_uv, u_resolution_b, u_output_resolution) : 1.0;

    // Default depth: far (1.0) for no-depth inputs

    // Compute mask modulation
    float mask_val = 1.0;
    if (u_has_mask) {
        // AUD-5306 (S3-A): explicit mask channel selection replaces the
        // ambiguous "m.a > 0 ? m.a : m.r" heuristic (default 0 = luma).
        vec4 m = texture(u_texture_mask, v_uv);
        if (u_mask_channel == 1) {
            mask_val = m.r;
        } else if (u_mask_channel == 2) {
            mask_val = m.g;
        } else if (u_mask_channel == 3) {
            mask_val = m.b;
        } else if (u_mask_channel == 4) {
            mask_val = m.a;
        } else {
            // Rec.709 luma (default)
            mask_val = dot(m.rgb, vec3(0.2126, 0.7152, 0.0722));
        }
    }
    float effect = u_opacity * mask_val;

    // Modulate foreground alpha by opacity+mask
    vec4 fg = vec4(a.rgb * effect, a.a * effect);

    // Depth-based compositing logic
    vec3 blended_rgb;
    float out_alpha;

    // Depth comparison with tolerance
    float depth_diff = abs(depth_a - depth_b);
    bool depth_similar = depth_diff < u_depth_tolerance;

    if (u_depth_mode == 0) {
        // z_front: foreground (smaller Z) occludes background
        // A is foreground if depth_a < depth_b
        if (depth_a <= depth_b) {
            // A is in front: standard over operation
            blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
        } else {
            // B is in front: B over A
            blended_rgb = b.rgb + fg.rgb * (1.0 - b.a);
            out_alpha = b.a + fg.a * (1.0 - b.a);
        }
    } else if (u_depth_mode == 1) {
        // z_back: background (larger Z) shows through foreground
        // A is background if depth_a > depth_b
        if (depth_a >= depth_b) {
            // A is behind: standard over operation
            blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
        } else {
            // B is behind: B over A
            blended_rgb = b.rgb + fg.rgb * (1.0 - b.a);
            out_alpha = b.a + fg.a * (1.0 - b.a);
        }
    } else {
        // z_merge: depth-weighted merge for similar depths
        if (depth_similar) {
            // Similar depths: weighted average by alpha and depth proximity
            float depth_weight_a = fg.a * (1.0 - depth_diff / u_depth_tolerance);
            float depth_weight_b = b.a * (1.0 - depth_diff / u_depth_tolerance);
            float total_weight = depth_weight_a + depth_weight_b;

            if (total_weight > 0.00001) {
                blended_rgb = (fg.rgb * depth_weight_a + b.rgb * depth_weight_b) / total_weight;
                out_alpha = (fg.a + b.a) * 0.5;  // Approximate merged alpha
            } else {
                // Fallback to standard over
                blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
                out_alpha = fg.a + b.a * (1.0 - fg.a);
            }
        } else {
            // Different depths: standard depth sorting (z_front logic)
            if (depth_a <= depth_b) {
                blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
                out_alpha = fg.a + b.a * (1.0 - fg.a);
            } else {
                blended_rgb = b.rgb + fg.rgb * (1.0 - b.a);
                out_alpha = b.a + fg.a * (1.0 - b.a);
            }
        }
    }

    fragColor = vec4(blended_rgb, out_alpha);
}
"""

# ── Depth Output Shader ──
# Write depth to separate output texture (single channel).
_DEPTH_OUTPUT_FRAGMENT = """
#version 330
uniform sampler2D u_depth_a;     // A depth channel
uniform sampler2D u_depth_b;     // B depth channel
uniform vec2 u_resolution_a;     // A texture pixel dimensions
uniform vec2 u_resolution_b;     // B texture pixel dimensions
uniform vec2 u_output_resolution; // Output canvas pixel dimensions
uniform bool u_has_depth_a;      // whether A has depth info
uniform bool u_has_depth_b;      // whether B has depth info
uniform int u_depth_mode;        // 0=z_front, 1=z_back, 2=z_merge
uniform float u_depth_tolerance; // tolerance for depth comparison
in vec2 v_uv;
out float fragDepth;

// Sample depth at origin
float sampleDepthAtOrigin(sampler2D depth_tex, vec2 uv, vec2 tex_res, vec2 out_res) {
    vec2 src_uv = uv * out_res / tex_res;
    if (src_uv.x < 0.0 || src_uv.x > 1.0 || src_uv.y < 0.0 || src_uv.y > 1.0) {
        return 1.0;  // Far depth for out-of-bounds
    }
    return texture(depth_tex, src_uv).r;
}

void main() {
    float depth_a = u_has_depth_a ? sampleDepthAtOrigin(u_depth_a, v_uv, u_resolution_a, u_output_resolution) : 1.0;
    float depth_b = u_has_depth_b ? sampleDepthAtOrigin(u_depth_b, v_uv, u_resolution_b, u_output_resolution) : 1.0;

    // Output depth based on depth_mode
    float out_depth;

    if (u_depth_mode == 0) {
        // z_front: use front (smaller) depth
        out_depth = min(depth_a, depth_b);
    } else if (u_depth_mode == 1) {
        // z_back: use back (larger) depth
        out_depth = max(depth_a, depth_b);
    } else {
        // z_merge: average for similar depths, otherwise use front
        float depth_diff = abs(depth_a - depth_b);
        if (depth_diff < u_depth_tolerance) {
            out_depth = (depth_a + depth_b) * 0.5;
        } else {
            out_depth = min(depth_a, depth_b);
        }
    }

    fragDepth = out_depth;
}
"""

# ═══════════════════════════════════════════════════════════════════════
# HISTOGRAM COMPUTE SHADERS
#
# GPU-accelerated histogram analysis for percentile normalization.
# Used by NormalizeNode's percentile mode to find appropriate value ranges.
#
# Process:
# 1. HistogramCompute: Build histogram bins using atomic counters
# 2. CDFCompute: Compute cumulative distribution function
# 3. PercentileLookup: Find values at specified percentiles (done on CPU)
#
# Histogram bins: 256 bins for each channel (R, G, B, A)
# Total bins: 256 * 4 = 1024 bins
# ═══════════════════════════════════════════════════════════════════════

# ── Histogram Compute Shader (Atomic Counter Based) ──
# Builds histogram bins from input texture.
# Each pixel contributes to 4 histogram bins (R, G, B, A).
_HISTOGRAM_COMPUTE = """
#version 430
layout(local_size_x = 16, local_size_y = 16) in;

uniform sampler2D u_input;
uniform vec2 u_resolution;
uniform int u_channel_mask;  // bitmask: 1=R, 2=G, 4=B, 8=A

// Histogram buffer: 256 bins * 4 channels = 1024 uints
// Layout: [R_bins 0-255, G_bins 0-255, B_bins 0-255, A_bins 0-255]
layout(std430, binding = 0) buffer HistogramBuffer {
    uint histogram[1024];
};

// Atomic counter for total pixel count (to compute CDF)
layout(std430, binding = 1) buffer CounterBuffer {
    uint total_count;
};

void main() {
    ivec2 global_id = ivec2(gl_GlobalInvocationID.xy);
    
    if (global_id.x >= int(u_resolution.x) || global_id.y >= int(u_resolution.y)) {
        return;
    }
    
    vec2 uv = vec2(global_id + ivec2(0.5)) / u_resolution;
    vec4 color = texture(u_input, uv);
    
    // Convert each channel to bin index (0-255)
    // For HDR values > 1.0, clamp to 255
    // For negative values, clamp to 0
    uint r_bin = uint(clamp(color.r, 0.0, 1.0) * 255.0);
    uint g_bin = uint(clamp(color.g, 0.0, 1.0) * 255.0);
    uint b_bin = uint(clamp(color.b, 0.0, 1.0) * 255.0);
    uint a_bin = uint(clamp(color.a, 0.0, 1.0) * 255.0);
    
    // Increment histogram bins using atomic operations
    // Channel offset: R=0, G=256, B=512, A=768
    if ((u_channel_mask & 1) != 0) {
        atomicAdd(histogram[r_bin], 1u);
    }
    if ((u_channel_mask & 2) != 0) {
        atomicAdd(histogram[256 + g_bin], 1u);
    }
    if ((u_channel_mask & 4) != 0) {
        atomicAdd(histogram[512 + b_bin], 1u);
    }
    if ((u_channel_mask & 8) != 0) {
        atomicAdd(histogram[768 + a_bin], 1u);
    }
    
    // Count total pixels
    atomicAdd(total_count, 1u);
}
"""

# ── Percentile Normalize Fragment Shader ──
# Applies percentile-based normalization using precomputed min/max values.
_PERCENTILE_NORMALIZE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_input_min;   // Value at low percentile
uniform float u_input_max;   // Value at high percentile
uniform float u_output_min;
uniform float u_output_max;
uniform bool u_clamp_output;
uniform vec4 u_channel_mask;  // 1.0 = normalize, 0.0 = pass through
in vec2 v_uv;
out vec4 fragColor;

float normalize_value(float value, float in_min, float in_max,
                      float out_min, float out_max, bool clamp_out) {
    float in_range = in_max - in_min;
    float out_range = out_max - out_min;
    
    // Avoid division by zero
    if (abs(in_range) < 0.00001) {
        return out_min;
    }
    
    float normalized = (value - in_min) / in_range;
    float result = normalized * out_range + out_min;
    
    if (clamp_out) {
        result = clamp(result, out_min, out_max);
    }
    
    return result;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    vec4 result;
    
    for (int i = 0; i < 4; i++) {
        float c = color[i];
        if (u_channel_mask[i] > 0.5) {
            c = normalize_value(c, u_input_min, u_input_max,
                                u_output_min, u_output_max, u_clamp_output);
        }
        result[i] = c;
    }
    
    fragColor = result;
}
"""

# ── Downsampling Fragment Shader for Histogram ──
# Reduces image to smaller size for faster histogram computation.
_HISTOGRAM_DOWNSAMPLE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_src_resolution;
uniform vec2 u_dst_resolution;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    // Box filter downsampling
    vec2 texel_size = 1.0 / u_src_resolution;
    vec2 dst_to_src = u_src_resolution / u_dst_resolution;

    // Compute box filter region
    vec2 sample_center = v_uv * u_src_resolution;
    vec2 sample_min = floor(sample_center - dst_to_src * 0.5);
    vec2 sample_max = floor(sample_center + dst_to_src * 0.5);

    vec4 color = vec4(0.0);
    float count = 0.0;

    // Sample within box (max 4x4 for typical downsampling)
    for (float y = sample_min.y; y <= sample_max.y; y += 1.0) {
        for (float x = sample_min.x; x <= sample_max.x; x += 1.0) {
            vec2 sample_uv = vec2(x + 0.5, y + 0.5) / u_src_resolution;
            sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));
            color += texture(u_texture, sample_uv);
            count += 1.0;
        }
    }

    fragColor = color / max(count, 1.0);
}
"""

# ── Channel Merge Fragment Shader ──
# Merges multiple single-channel inputs into RGBA output.
# Each output channel can be sourced from any input or a constant value.
_CHANNEL_MERGE_FRAGMENT = """
#version 330
uniform sampler2D u_texture_r;  // R input (optional)
uniform sampler2D u_texture_g;  // G input (optional)
uniform sampler2D u_texture_b;  // B input (optional)
uniform sampler2D u_texture_a;  // A input (optional)
uniform bool u_has_r;           // Whether R input is connected
uniform bool u_has_g;           // Whether G input is connected
uniform bool u_has_b;           // Whether B input is connected
uniform bool u_has_a;           // Whether A input is connected
uniform int u_r_source;         // 0=input_r, 1=input_g, 2=input_b, 3=input_a, 4=constant
uniform int u_g_source;
uniform int u_b_source;
uniform int u_a_source;
uniform int u_r_channel;        // 0=r, 1=g, 2=b, 3=a, 4=luma (which channel to extract)
uniform int u_g_channel;
uniform int u_b_channel;
uniform int u_a_channel;
uniform float u_r_constant;     // Constant value when source=constant
uniform float u_g_constant;
uniform float u_b_constant;
uniform float u_a_constant;
in vec2 v_uv;
out vec4 fragColor;

// Extract a channel from a color value
float extract_channel(vec4 color, int channel) {
    if (channel == 0) return color.r;
    if (channel == 1) return color.g;
    if (channel == 2) return color.b;
    if (channel == 3) return color.a;
    if (channel == 4) return dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));  // luma
    return 0.0;
}

// Get channel value from specified source
float get_channel_value(int source, int channel, float constant) {
    if (source == 0) {
        // input_r
        if (u_has_r) {
            return extract_channel(texture(u_texture_r, v_uv), channel);
        }
        return 0.0;
    } else if (source == 1) {
        // input_g
        if (u_has_g) {
            return extract_channel(texture(u_texture_g, v_uv), channel);
        }
        return 0.0;
    } else if (source == 2) {
        // input_b
        if (u_has_b) {
            return extract_channel(texture(u_texture_b, v_uv), channel);
        }
        return 0.0;
    } else if (source == 3) {
        // input_a
        if (u_has_a) {
            return extract_channel(texture(u_texture_a, v_uv), channel);
        }
        return 0.0;
    } else if (source == 4) {
        // constant
        return constant;
    }
    return 0.0;
}

void main() {
    float r_val = get_channel_value(u_r_source, u_r_channel, u_r_constant);
    float g_val = get_channel_value(u_g_source, u_g_channel, u_g_constant);
    float b_val = get_channel_value(u_b_source, u_b_channel, u_b_constant);
    float a_val = get_channel_value(u_a_source, u_a_channel, u_a_constant);
    fragColor = vec4(r_val, g_val, b_val, a_val);
}
"""

# ── Noise Generator Shader ──
# Generates procedural noise textures (Perlin, Simplex, Fractal, Turbulence, Wave)
_NOISE_FRAGMENT = """
#version 330
uniform int u_noise_type;      // 0=perlin, 1=simplex, 2=fractal, 3=turbulence, 4=wave
uniform float u_scale;         // Noise frequency/scale
uniform int u_octaves;         // Number of fractal layers (1-8)
uniform float u_persistence;   // Amplitude decay per octave
uniform float u_lacunarity;    // Frequency increase per octave
uniform int u_seed;            // Random seed
uniform int u_output_channels; // 1=grayscale, 3=RGB, 4=RGBA
uniform bool u_animate;        // Enable animation
uniform float u_animation_speed;
uniform float u_time;          // Animation time
uniform float u_offset_x;
uniform float u_offset_y;
uniform vec2 u_resolution;
in vec2 v_uv;
out vec4 fragColor;

// ── Hash functions for noise ──
// Based on improved Perlin noise and Simplex noise implementations

// 2D hash function - returns pseudorandom gradient vector
vec2 hash22(vec2 p, int seed) {
    // Use seed to offset hash
    float s = float(seed) * 0.001;
    vec2 offset = vec2(s, s * 1.618);

    // Integer hash
    vec3 p3 = fract(vec3(p.x * 0.1031 + offset.x, p.y * 0.1031 + offset.y, p.x + p.y));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract(vec2((p3.x + p3.y) * p3.z, (p3.x + p3.z) * p3.y)) * 2.0 - 1.0;
}

// Simple hash for float
float hash21(vec2 p, int seed) {
    float s = float(seed) * 0.001;
    p = fract(p * vec2(0.1031, 0.1031) + vec2(s, s * 1.618));
    p += dot(p, p.yx + 33.33);
    return fract((p.x + p.y) * p.y);
}

// ── Perlin Noise ──
float perlin_noise(vec2 p, int seed) {
    vec2 i = floor(p);
    vec2 f = fract(p);

    // 5th-order smoothstep for smoother interpolation
    vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);

    // Get gradient vectors at four corners
    vec2 g00 = hash22(i, seed);
    vec2 g10 = hash22(i + vec2(1.0, 0.0), seed);
    vec2 g01 = hash22(i + vec2(0.0, 1.0), seed);
    vec2 g11 = hash22(i + vec2(1.0, 1.0), seed);

    // Compute dot products
    float n00 = dot(g00, f);
    float n10 = dot(g10, f - vec2(1.0, 0.0));
    float n01 = dot(g01, f - vec2(0.0, 1.0));
    float n11 = dot(g11, f - vec2(1.0, 1.0));

    // Bilinear interpolation
    float nx0 = mix(n00, n10, u.x);
    float nx1 = mix(n01, n11, u.x);
    float nxy = mix(nx0, nx1, u.y);

    // Scale to 0-1 range
    return nxy * 0.5 + 0.5;
}

// ── Simplex Noise (2D) ──
// More efficient than Perlin, fewer directional artifacts
float simplex_noise(vec2 p, int seed) {
    // Skew input space to determine which simplex cell we're in
    const float F2 = 0.366025403784;  // (sqrt(3)-1)/2
    const float G2 = 0.211324865405;  // (3-sqrt(3))/6

    vec2 s = p + dot(p, vec2(F2));
    vec2 i = floor(s);
    vec2 f = s - i;

    // Determine which simplex we're in (lower or upper triangle)
    float comp = step(f.x, f.y);
    vec2 i1 = vec2(1.0 - comp, comp);

    // Offsets for middle and last corners
    vec2 i2 = vec2(1.0, 1.0);

    // Unskew back to input space
    vec2 p0 = p - (i - vec2(G2));
    vec2 p1 = p - (i + i1 - vec2(G2));
    vec2 p2 = p - (i + i2 - vec2(G2));

    // Get gradient vectors
    vec2 g0 = hash22(i, seed);
    vec2 g1 = hash22(i + i1, seed);
    vec2 g2 = hash22(i + i2, seed);

    // Compute contributions from three corners
    float t0 = max(0.5 - dot(p0, p0), 0.0);
    float t1 = max(0.5 - dot(p1, p1), 0.0);
    float t2 = max(0.5 - dot(p2, p2), 0.0);

    t0 *= t0 * t0 * t0;
    t1 *= t1 * t1 * t1;
    t2 *= t2 * t2 * t2;

    float n0 = t0 * dot(g0, p0);
    float n1 = t1 * dot(g1, p1);
    float n2 = t2 * dot(g2, p2);

    // Scale to 0-1
    return (n0 + n1 + n2) * 2.5 + 0.5;
}

// ── Fractal Brownian Motion (fBm) ──
// Layered noise for organic detail
float fractal_noise(vec2 p, int octaves, float persistence, float lacunarity, int seed) {
    float value = 0.0;
    float amplitude = 1.0;
    float frequency = 1.0;
    float maxValue = 0.0;

    for (int i = 0; i < 8; i++) {
        if (i >= octaves) break;

        value += perlin_noise(p * frequency, seed + i * 100) * amplitude;
        maxValue += amplitude;

        amplitude *= persistence;
        frequency *= lacunarity;
    }

    return value / max(maxValue, 0.001);
}

// ── Turbulence Noise ──
// Absolute fractal noise for turbulent patterns
float turbulence_noise(vec2 p, int octaves, float persistence, float lacunarity, int seed) {
    float value = 0.0;
    float amplitude = 1.0;
    float frequency = 1.0;
    float maxValue = 0.0;

    for (int i = 0; i < 8; i++) {
        if (i >= octaves) break;

        float n = perlin_noise(p * frequency, seed + i * 100);
        value += abs(n - 0.5) * 2.0 * amplitude;
        maxValue += amplitude;

        amplitude *= persistence;
        frequency *= lacunarity;
    }

    return value / max(maxValue, 0.001);
}

// ── Wave Noise ──
// Sine-based wave patterns
float wave_noise(vec2 p, int seed) {
    float s = float(seed) * 0.01;
    float wave1 = sin(p.x * 6.28318 + s) * 0.5 + 0.5;
    float wave2 = sin(p.y * 6.28318 + s * 1.618) * 0.5 + 0.5;
    float wave3 = sin((p.x + p.y) * 3.14159 + s * 2.718) * 0.5 + 0.5;
    return mix(mix(wave1, wave2, 0.5), wave3, 0.33);
}

void main() {
    // Compute noise coordinate
    vec2 coord = v_uv * u_resolution / u_scale;
    coord += vec2(u_offset_x, u_offset_y) / u_scale;

    // Add animation offset
    if (u_animate) {
        coord += vec2(u_time * u_animation_speed * 0.1, u_time * u_animation_speed * 0.05);
    }

    // Generate noise based on type
    float noise_val;
    if (u_noise_type == 1) {
        noise_val = simplex_noise(coord, u_seed);
    } else if (u_noise_type == 2) {
        noise_val = fractal_noise(coord, u_octaves, u_persistence, u_lacunarity, u_seed);
    } else if (u_noise_type == 3) {
        noise_val = turbulence_noise(coord, u_octaves, u_persistence, u_lacunarity, u_seed);
    } else if (u_noise_type == 4) {
        noise_val = wave_noise(coord, u_seed);
    } else {
        noise_val = perlin_noise(coord, u_seed);
    }

    // Output channels
    if (u_output_channels == 1) {
        // Grayscale output
        fragColor = vec4(noise_val, noise_val, noise_val, 1.0);
    } else if (u_output_channels == 3) {
        // RGB output (same value in all channels)
        fragColor = vec4(noise_val, noise_val, noise_val, 1.0);
    } else {
        // RGBA output
        fragColor = vec4(noise_val, noise_val, noise_val, 1.0);
    }
}
"""

# ── Ramp/Gradient Generator Shader ──
# Generates linear, radial, angular, box, and diamond gradients
_RAMP_FRAGMENT = """
#version 330
uniform int u_ramp_type;       // 0=linear, 1=radial, 2=angular, 3=box, 4=diamond
uniform vec2 u_start_point;    // Start point (normalized 0-1)
uniform vec2 u_end_point;      // End point (normalized 0-1)
uniform vec4 u_start_color;    // Start color
uniform vec4 u_end_color;      // End color
uniform int u_extend_mode;     // 0=clamp, 1=repeat, 2=mirror
uniform vec2 u_center;         // Center for radial/angular
uniform float u_radius;        // Radius for radial
uniform float u_angle_start;   // Start angle for angular (degrees)
uniform float u_angle_end;     // End angle for angular (degrees)
// Box corner colors (clockwise from top-left)
uniform vec4 u_corner0;
uniform vec4 u_corner1;
uniform vec4 u_corner2;
uniform vec4 u_corner3;
uniform vec2 u_resolution;     // Output size for aspect-corrected geometry
in vec2 v_uv;
out vec4 fragColor;

// Normalized UV → aspect-corrected space where 1 unit equals the output
// height.  Radial/angular/diamond gradients stay circular on non-square
// images; linear gradients keep physically correct angles.  Points defined
// in normalized UV (start/end/center) are corrected the same way so the
// gradient geometry matches what the user drew.
vec2 aspect_uv(vec2 uv) {
    float aspect = u_resolution.x / max(u_resolution.y, 1.0);
    return vec2(uv.x * aspect, uv.y);
}

// Apply extend mode to t value
float apply_extend(float t, int mode) {
    if (mode == 0) {
        // Clamp
        return clamp(t, 0.0, 1.0);
    } else if (mode == 1) {
        // Repeat
        return fract(t);
    } else {
        // AUD-5305: Mirror must be the period-2 triangle wave
        // 1.0 - abs(mod(t, 2.0) - 1.0).  The previous period-1 form
        // (fract + fold at 0.5) also inverted t inside [0,1], so the
        // basic gradient interval descended instead of holding identity.
        // Same form as _CROP_FRAGMENT mirror_uv / _LENS_DISTORTION.
        return 1.0 - abs(mod(t, 2.0) - 1.0);
    }
}

// Linear gradient
vec4 linear_ramp(vec2 uv, vec2 start, vec2 end, vec4 color_start, vec4 color_end, int extend) {
    vec2 dir = end - start;
    float len_sq = dot(dir, dir);

    if (len_sq < 0.0001) {
        return color_start;
    }

    // Project point onto line
    vec2 pt = uv - start;
    float t = dot(pt, dir) / len_sq;

    // Apply extend mode
    t = apply_extend(t, extend);

    // Interpolate colors
    return mix(color_start, color_end, t);
}

// Radial gradient
vec4 radial_ramp(vec2 uv, vec2 center, float radius, vec4 color_start, vec4 color_end, int extend) {
    float dist = length(uv - center);
    float t = dist / radius;

    t = apply_extend(t, extend);

    return mix(color_start, color_end, t);
}

// Angular/Conical gradient
vec4 angular_ramp(vec2 uv, vec2 center, float angle_start, float angle_end,
                  vec4 color_start, vec4 color_end, int extend) {
    vec2 offset = uv - center;
    float angle = atan(offset.y, offset.x);

    // Convert to degrees (0-360)
    float degrees = angle * 57.2958 + 180.0;

    // Normalize to 0-1 based on angle range
    float range = angle_end - angle_start;
    if (range <= 0.0) range = 360.0;

    float t = (degrees - angle_start) / range;

    t = apply_extend(t, extend);

    return mix(color_start, color_end, t);
}

// Box gradient (4-corner bilinear interpolation)
vec4 box_ramp(vec2 uv, vec4 c0, vec4 c1, vec4 c2, vec4 c3) {
    // Bilinear interpolation
    // c0 = top-left, c1 = top-right, c2 = bottom-right, c3 = bottom-left
    vec4 top = mix(c0, c1, uv.x);
    vec4 bottom = mix(c3, c2, uv.x);
    return mix(top, bottom, uv.y);
}

// Diamond gradient
vec4 diamond_ramp(vec2 uv, vec2 center, float radius, vec4 color_start, vec4 color_end, int extend) {
    // Manhattan distance for diamond shape
    float dist = abs(uv.x - center.x) + abs(uv.y - center.y);
    float t = dist / radius;

    t = apply_extend(t, extend);

    return mix(color_start, color_end, t);
}

void main() {
    vec4 result;

    if (u_ramp_type == 1) {
        // Radial
        result = radial_ramp(aspect_uv(v_uv), aspect_uv(u_center), u_radius, u_start_color, u_end_color, u_extend_mode);
    } else if (u_ramp_type == 2) {
        // Angular
        result = angular_ramp(aspect_uv(v_uv), aspect_uv(u_center), u_angle_start, u_angle_end,
                             u_start_color, u_end_color, u_extend_mode);
    } else if (u_ramp_type == 3) {
        // Box (4-corner)
        result = box_ramp(v_uv, u_corner0, u_corner1, u_corner2, u_corner3);
    } else if (u_ramp_type == 4) {
        // Diamond
        result = diamond_ramp(aspect_uv(v_uv), aspect_uv(u_center), u_radius, u_start_color, u_end_color, u_extend_mode);
    } else {
        // Linear (default)
        result = linear_ramp(aspect_uv(v_uv), aspect_uv(u_start_point), aspect_uv(u_end_point),
                            u_start_color, u_end_color, u_extend_mode);
    }

    fragColor = result;
}
"""

# ── Constant Gradient/Radial Shader ─────────────────────────────────────────
# GPU generator for Constant node's gradient/radial modes. Unlike _RAMP_FRAGMENT
# (which targets the Ramp effect-node marker path), this shader is compiled
# in-process by ConstantNode._do_evaluate (Constant stays a source node and
# self-manages its texture), and adds a feather/smoothness control on top of
# the linear/radial blend. Coordinates are normalized 0-1, matching the on-
# screen handle params (start_x/y, end_x/y, center_x/y) so dragging a handle
# maps 1:1 to the rendered pixels with no extra conversion.
_CONSTANT_GRADIENT_FRAGMENT = """
#version 330
uniform int   u_mode;          // 0 = linear, 1 = radial
uniform vec2  u_start_point;   // linear start (normalized 0-1)
uniform vec2  u_end_point;     // linear end   (normalized 0-1)
uniform vec2  u_center;        // radial center (normalized 0-1)
uniform float u_radius;        // radial radius (normalized, short-edge basis)
uniform vec4  u_color1;        // primary color (gradient start / radial center)
uniform vec4  u_color2;        // secondary color (gradient end / radial edge)
uniform float u_feather;       // -1..1 (negative = soft/blurry, 0 = linear, positive = contrast)
uniform vec2  u_resolution;    // output pixel dimensions
in  vec2 v_uv;
out vec4 fragColor;

// Bidirectional feather / contrast control on the gradient coordinate t.
// f = 0     -> linear clamp (neutral, identical to the legacy numpy path).
// f > 0     -> contrast up: flat color plateau (width f/2) on each end +
//              smoothstep transition in the middle. Larger f = wider solid
//              regions + steeper, smoother transition.
// f < 0     -> soft/blurry: smoothstep S-curve across the whole range (gentler
//              edge rolloff than linear) plus end contraction (the two ends
//              no longer reach pure color), lowering overall contrast — the
//              "added blur" look.
// In all cases the transition midpoint (t = 0.5) is preserved.
float apply_feather(float t, float f) {
    float tc = clamp(t, 0.0, 1.0);
    if (abs(f) <= 0.001) {
        return tc;
    }
    if (f > 0.0) {
        // Contrast: plateau + smoothstep.
        float pad = min(f, 0.999) * 0.5;
        if (tc <= pad) {
            return 0.0;
        } else if (tc >= 1.0 - pad) {
            return 1.0;
        } else {
            float x = (tc - pad) / (1.0 - 2.0 * pad);
            return x * x * (3.0 - 2.0 * x);
        }
    }
    // Soft / blurry: full-range smoothstep, then contract ends by |f|/2.
    float a = min(-f, 0.999) * 0.5;
    float s = tc * tc * (3.0 - 2.0 * tc);
    return a + s * (1.0 - 2.0 * a);
}

void main() {
    // v_uv comes from the fullscreen quad: uv (0,0)=bottom-left, (1,1)=top-left
    // (NDC up). But the start/end/center params are image-Y-down (0=top, 1=bottom)
    // to match the on-screen handle drag and the legacy numpy path. Flip the Y so
    // the shader works in image space, matching what the user sees.
    vec2 uv = vec2(v_uv.x, 1.0 - v_uv.y);

    float t;
    if (u_mode == 1) {
        // Radial: distance in PIXEL space, normalized by (radius * short edge).
        // Doing it in pixel space (rather than normalized uv space) keeps the
        // gradient circular on non-square resolutions, matching the legacy
        // numpy _make_radial behavior.
        vec2 px = uv * u_resolution;
        vec2 cp = u_center * u_resolution;
        float short_edge = min(u_resolution.x, u_resolution.y);
        float d = distance(px, cp);
        t = d / max(u_radius * short_edge, 0.0001);
    } else {
        // Linear: project uv onto the start->end direction.
        vec2 dir = u_end_point - u_start_point;
        float len2 = dot(dir, dir);
        if (len2 < 0.0001) {
            t = 0.0;
        } else {
            t = dot(uv - u_start_point, dir) / len2;
        }
    }

    t = apply_feather(t, u_feather);
    fragColor = mix(u_color1, u_color2, t);
}
"""

# ── Edge Detection Shader ──
# Sobel, Laplacian, Prewitt, and Canny edge detection
_EDGE_DETECT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform int u_method;          // 0=sobel, 1=laplacian, 2=prewitt, 3=canny
uniform float u_threshold;
uniform float u_threshold_high;
uniform int u_direction;       // 0=both, 1=horizontal, 2=vertical
uniform int u_output_mode;     // 0=edges, 1=gradient, 2=direction
uniform bool u_invert;
uniform float u_output_strength;
uniform int u_kernel_size;     // 3 or 5
uniform float u_preblur;
uniform int u_channel;         // 0=luminance, 1=R, 2=G, 3=B, 4=A
in vec2 v_uv;
out vec4 fragColor;

// Get luminance or specific channel from sample
float get_channel_value(vec4 color, int channel) {
    if (channel == 0) {
        // Luminance
        return dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    } else if (channel == 1) {
        return color.r;
    } else if (channel == 2) {
        return color.g;
    } else if (channel == 3) {
        return color.b;
    } else {
        return color.a;
    }
}

// Sample at offset (with resolution normalization)
// 预模糊（u_preblur 像素半径）：tent 滤波抑制噪点/胶片颗粒引起的伪边缘。
// preblur=0 时退化为单 tap 采样（与原实现等价，无额外开销）。
// 注意：u_preblur 必须真的参与采样，否则 GLSL 编译器剔除该 uniform，
// evaluator 端 prog['u_preblur'] 赋值将 KeyError（节点整体失效）。
float blurred_channel(sampler2D tex, vec2 uv, int channel) {
    float pb = u_preblur;
    if (pb <= 0.0) {
        return get_channel_value(texture(tex, uv), channel);
    }
    vec2 px = vec2(pb, pb) / u_resolution;
    float sum = 0.0;
    sum += get_channel_value(texture(tex, uv), channel) * 4.0;
    sum += get_channel_value(texture(tex, uv + vec2( px.x, 0.0)), channel) * 2.0;
    sum += get_channel_value(texture(tex, uv - vec2( px.x, 0.0)), channel) * 2.0;
    sum += get_channel_value(texture(tex, uv + vec2( 0.0, px.y)), channel) * 2.0;
    sum += get_channel_value(texture(tex, uv - vec2( 0.0, px.y)), channel) * 2.0;
    sum += get_channel_value(texture(tex, uv + px), channel);
    sum += get_channel_value(texture(tex, uv - px), channel);
    sum += get_channel_value(texture(tex, uv + vec2( px.x, -px.y)), channel);
    sum += get_channel_value(texture(tex, uv + vec2(-px.x,  px.y)), channel);
    return sum / 16.0;
}

float sample_offset(sampler2D tex, vec2 uv, vec2 offset, vec2 res, int channel) {
    vec2 pixel_offset = offset / res;
    return blurred_channel(tex, uv + pixel_offset, channel);
}

// ── Sobel Edge Detection ──
// 3x3 kernels
// Gx: [-1 0 1; -2 0 2; -1 0 1]
// Gy: [-1 -2 -1; 0 0 0; 1 2 1]
vec2 sobel_3x3(sampler2D tex, vec2 uv, vec2 res, int channel) {
    float tl = sample_offset(tex, uv, vec2(-1.0, -1.0), res, channel);
    float tm = sample_offset(tex, uv, vec2( 0.0, -1.0), res, channel);
    float tr = sample_offset(tex, uv, vec2( 1.0, -1.0), res, channel);
    float ml = sample_offset(tex, uv, vec2(-1.0,  0.0), res, channel);
    float mr = sample_offset(tex, uv, vec2( 1.0,  0.0), res, channel);
    float bl = sample_offset(tex, uv, vec2(-1.0,  1.0), res, channel);
    float bm = sample_offset(tex, uv, vec2( 0.0,  1.0), res, channel);
    float br = sample_offset(tex, uv, vec2( 1.0,  1.0), res, channel);

    // Sobel kernels
    float gx = -tl + tr - 2.0 * ml + 2.0 * mr - bl + br;
    float gy = -tl - 2.0 * tm - tr + bl + 2.0 * bm + br;

    return vec2(gx, gy);
}

// 5x5 Sobel (extended kernel)
vec2 sobel_5x5(sampler2D tex, vec2 uv, vec2 res, int channel) {
    // Sample 5x5 neighborhood
    float values[25];
    int idx = 0;
    for (int y = -2; y <= 2; y++) {
        for (int x = -2; x <= 2; x++) {
            values[idx] = sample_offset(tex, uv, vec2(float(x), float(y)), res, channel);
            idx++;
        }
    }

    // 5x5 Sobel-like kernels (Scharr)
    // Gx: [-2 -1 0 1 2; -4 -2 0 2 4; -6 -3 0 3 6; -4 -2 0 2 4; -2 -1 0 1 2]
    // Simplified weighted sum
    float gx = 0.0;
    float gy = 0.0;

    // Gx weights (horizontal edges)
    gx += -2.0 * values[0] - 1.0 * values[1] + 1.0 * values[3] + 2.0 * values[4];
    gx += -4.0 * values[5] - 2.0 * values[6] + 2.0 * values[8] + 4.0 * values[9];
    gx += -6.0 * values[10] - 3.0 * values[11] + 3.0 * values[13] + 6.0 * values[14];
    gx += -4.0 * values[15] - 2.0 * values[16] + 2.0 * values[18] + 4.0 * values[19];
    gx += -2.0 * values[20] - 1.0 * values[21] + 1.0 * values[23] + 2.0 * values[24];

    // Gy weights (vertical edges)
    gy += -2.0 * values[0] - 4.0 * values[5] - 6.0 * values[10] - 4.0 * values[15] - 2.0 * values[20];
    gy += -1.0 * values[1] - 2.0 * values[6] - 3.0 * values[11] - 2.0 * values[16] - 1.0 * values[21];
    gy += values[3] + 2.0 * values[8] + 3.0 * values[13] + 2.0 * values[18] + values[23];
    gy += 2.0 * values[4] + 4.0 * values[9] + 6.0 * values[14] + 4.0 * values[19] + 2.0 * values[24];

    return vec2(gx / 12.0, gy / 12.0);
}

// ── Laplacian Edge Detection ──
// 3x3 kernel: [0 1 0; 1 -4 1; 0 1 0] or [1 1 1; 1 -8 1; 1 1 1]
float laplacian_3x3(sampler2D tex, vec2 uv, vec2 res, int channel) {
    float center = sample_offset(tex, uv, vec2(0.0, 0.0), res, channel);
    float top    = sample_offset(tex, uv, vec2(0.0, -1.0), res, channel);
    float bottom = sample_offset(tex, uv, vec2(0.0, 1.0), res, channel);
    float left   = sample_offset(tex, uv, vec2(-1.0, 0.0), res, channel);
    float right  = sample_offset(tex, uv, vec2(1.0, 0.0), res, channel);

    // Laplacian kernel
    return top + bottom + left + right - 4.0 * center;
}

// 5x5 Laplacian (includes diagonal neighbors)
float laplacian_5x5(sampler2D tex, vec2 uv, vec2 res, int channel) {
    float center = sample_offset(tex, uv, vec2(0.0, 0.0), res, channel);

    // 8-connected neighbors
    float top    = sample_offset(tex, uv, vec2(0.0, -1.0), res, channel);
    float bottom = sample_offset(tex, uv, vec2(0.0, 1.0), res, channel);
    float left   = sample_offset(tex, uv, vec2(-1.0, 0.0), res, channel);
    float right  = sample_offset(tex, uv, vec2(1.0, 0.0), res, channel);
    float tl     = sample_offset(tex, uv, vec2(-1.0, -1.0), res, channel);
    float tr     = sample_offset(tex, uv, vec2(1.0, -1.0), res, channel);
    float bl     = sample_offset(tex, uv, vec2(-1.0, 1.0), res, channel);
    float br     = sample_offset(tex, uv, vec2(1.0, 1.0), res, channel);

    // Weighted Laplacian (including diagonals)
    return top + bottom + left + right + 0.5 * (tl + tr + bl + br) - 8.0 * center;
}

// ── Prewitt Edge Detection ──
// Similar to Sobel but with different weights
// Gx: [-1 0 1; -1 0 1; -1 0 1]
// Gy: [-1 -1 -1; 0 0 0; 1 1 1]
vec2 prewitt_3x3(sampler2D tex, vec2 uv, vec2 res, int channel) {
    float tl = sample_offset(tex, uv, vec2(-1.0, -1.0), res, channel);
    float tm = sample_offset(tex, uv, vec2( 0.0, -1.0), res, channel);
    float tr = sample_offset(tex, uv, vec2( 1.0, -1.0), res, channel);
    float ml = sample_offset(tex, uv, vec2(-1.0,  0.0), res, channel);
    float mr = sample_offset(tex, uv, vec2( 1.0,  0.0), res, channel);
    float bl = sample_offset(tex, uv, vec2(-1.0,  1.0), res, channel);
    float bm = sample_offset(tex, uv, vec2( 0.0,  1.0), res, channel);
    float br = sample_offset(tex, uv, vec2( 1.0,  1.0), res, channel);

    float gx = -tl + tr - ml + mr - bl + br;
    float gy = -tl - tm - tr + bl + bm + br;

    return vec2(gx, gy);
}

void main() {
    vec2 res = u_resolution;

    // Get gradient based on method
    vec2 gradient;
    float laplacian_val = 0.0;

    bool use_laplacian = (u_method == 1);

    // Canny (u_method == 3) shares the Sobel gradient stage; its distinct
    // behaviour is the double-threshold classification further below.
    // Without a branch here its gradient stayed (0,0) — every canny edge
    // rendered black and direction mode produced atan(0,0) = NaN.
    bool use_sobel = (u_method == 0) || (u_method == 3);

    if (u_kernel_size == 5) {
        if (use_sobel) {
            gradient = sobel_5x5(u_texture, v_uv, res, u_channel);
        } else if (u_method == 1) {
            laplacian_val = laplacian_5x5(u_texture, v_uv, res, u_channel);
        } else if (u_method == 2) {
            gradient = sobel_3x3(u_texture, v_uv, res, u_channel);  // Prewitt uses 3x3
        }
    } else {
        if (use_sobel) {
            gradient = sobel_3x3(u_texture, v_uv, res, u_channel);
        } else if (u_method == 1) {
            laplacian_val = laplacian_3x3(u_texture, v_uv, res, u_channel);
        } else if (u_method == 2) {
            gradient = prewitt_3x3(u_texture, v_uv, res, u_channel);
        }
    }

    // Compute edge magnitude
    float magnitude;
    if (use_laplacian) {
        magnitude = abs(laplacian_val);
    } else {
        // Direction-specific output
        if (u_direction == 1) {
            // Horizontal only
            magnitude = abs(gradient.x);
        } else if (u_direction == 2) {
            // Vertical only
            magnitude = abs(gradient.y);
        } else {
            // Both directions
            magnitude = length(gradient);
        }
    }

    // Normalize magnitude (approximate)
    magnitude = magnitude / 4.0;

    // Output mode
    float output_val;
    vec4 result;

    if (u_output_mode == 0) {
        // Edges: threshold and output
        float edge = step(u_threshold, magnitude);

        // Canny-like double threshold
        if (u_method == 3) {
            if (magnitude < u_threshold) {
                edge = 0.0;
            } else if (magnitude < u_threshold_high) {
                edge = 0.5;  // Weak edge
            } else {
                edge = 1.0;  // Strong edge
            }
        }

        output_val = edge * u_output_strength;
    } else if (u_output_mode == 1) {
        // Gradient magnitude (raw)
        output_val = magnitude * u_output_strength;
    } else {
        // Direction (encode gradient angle as color)
        if (use_laplacian) {
            output_val = 0.5;  // Laplacian has no direction info
        } else {
            // atan(0, 0) is undefined in GLSL (NaN on flat regions);
            // encode flat pixels as angle 0 instead.
            float angle = 0.0;
            if (dot(gradient, gradient) > 1e-12) {
                angle = atan(gradient.y, gradient.x);
            }
            output_val = (angle + 3.14159) / (2.0 * 3.14159);  // Normalize 0-1
        }
    }

    // Invert if requested
    if (u_invert) {
        output_val = 1.0 - output_val;
    }

    // Preserve original alpha if available
    vec4 original = texture(u_texture, v_uv);
    result = vec4(output_val, output_val, output_val, original.a);

    fragColor = result;
}
"""

# ── Mask/Geometry Shader ──
# Rectangle, Ellipse, Polygon masks using SDF (signed distance field)
_MASK_FRAGMENT = """
#version 330
uniform sampler2D u_texture;  // Optional image input for dual-mode masking
uniform bool u_has_input;
uniform int u_mask_type;       // 0=rectangle, 1=ellipse, 2=polygon
uniform vec2 u_center;         // Center position (normalized 0-1)
uniform vec2 u_size;           // Size (normalized 0-2)
uniform float u_rotation;      // Rotation angle in degrees
uniform float u_feather;       // Feather width (normalized 0-0.5)
uniform bool u_invert;         // Invert mask
uniform int u_output_mode;     // 0=matte (alpha only), 1=result (white matte)
uniform int u_sides;           // Number of sides for polygon (3-32)
in vec2 v_uv;
out vec4 fragColor;

// ── Rotation Transform ──
// Rotate point around center by angle (in radians)
vec2 rotate_point(vec2 p, vec2 center, float angle) {
    float c = cos(angle);
    float s = sin(angle);
    vec2 offset = p - center;
    return vec2(
        offset.x * c - offset.y * s,
        offset.x * s + offset.y * c
    ) + center;
}

// ── Rectangle SDF ──
// Signed distance field for axis-aligned rectangle
// Returns negative inside, positive outside
float sdf_rectangle(vec2 p, vec2 center, vec2 size) {
    vec2 half_size = size * 0.5;
    vec2 d = abs(p - center) - half_size;
    return max(d.x, d.y);
}

// ── Ellipse SDF ──
// Signed distance field for ellipse
float sdf_ellipse(vec2 p, vec2 center, vec2 size) {
    vec2 half_size = size * 0.5;
    // Handle degenerate case
    if (half_size.x < 0.001 || half_size.y < 0.001) {
        return 1.0;
    }
    vec2 d = (p - center) / half_size;
    float dist = length(d) - 1.0;
    // Scale back to original units
    return dist * min(half_size.x, half_size.y);
}

// ── Polygon SDF ──
// Signed distance field for regular polygon (n-sided)
float sdf_polygon(vec2 p, vec2 center, float radius, int sides) {
    // Clamp sides to valid range
    sides = max(3, min(32, sides));
    float n = float(sides);

    // Angle per side
    float angle = 6.28318 / n;  // 2 * PI / n

    // Offset point relative to center
    vec2 offset = p - center;

    // Rotate to align first vertex at top
    float a_offset = 1.5707963 - angle * 0.5;  // PI/2 - angle/2
    float c = cos(a_offset);
    float s = sin(a_offset);
    offset = vec2(offset.x * c - offset.y * s, offset.x * s + offset.y * c);

    // Compute distance to polygon edges
    float r = radius;

    // Sector angle (half of each polygon segment)
    float sector_angle = angle * 0.5;

    // Determine which sector the point is in
    float theta = atan(offset.y, offset.x);
    theta = mod(theta + sector_angle, angle) - sector_angle;

    // Distance from center to polygon edge in this direction
    vec2 edge_point = vec2(r * cos(theta), r * sin(theta));

    // Signed distance: negative inside, positive outside
    float dist = length(offset) - length(edge_point);

    return dist;
}

// ── Feather Function ──
// Linear feather interpolation at distance d from edge
float apply_feather(float d, float feather) {
    if (feather < 0.001) {
        // No feather: hard edge
        return step(0.0, d);  // 1 inside, 0 outside
    }
    // Linear interpolation from 1 to 0 across feather width
    return smoothstep(-feather, feather, d);
}

void main() {
    // Get normalized coordinates
    vec2 uv = v_uv;

    // Apply rotation if needed
    if (abs(u_rotation) > 0.1) {
        float angle_rad = u_rotation * 0.01745329;  // degrees to radians
        uv = rotate_point(uv, u_center, angle_rad);
    }

    // Compute SDF based on mask type
    float dist;

    if (u_mask_type == 0) {
        // Rectangle
        dist = sdf_rectangle(uv, u_center, u_size);
    } else if (u_mask_type == 1) {
        // Ellipse
        dist = sdf_ellipse(uv, u_center, u_size);
    } else {
        // Polygon
        // Use the smaller dimension as radius for uniform scaling
        float radius = min(u_size.x, u_size.y) * 0.5;
        dist = sdf_polygon(uv, u_center, radius, u_sides);
    }

    // Apply feather to get alpha value
    float alpha = apply_feather(dist, u_feather);

    // Invert if requested
    if (u_invert) {
        alpha = 1.0 - alpha;
    }

    // Output mode.  Mode 2 applies the generated alpha to an input image.
    vec4 result;
    if (u_output_mode == 0) {
        // Matte: alpha only (stored in alpha channel, RGB black)
        result = vec4(0.0, 0.0, 0.0, alpha);
    } else if (u_output_mode == 1) {
        // Result: white matte (alpha in all channels)
        result = vec4(alpha, alpha, alpha, alpha);
    } else {
        vec4 source = u_has_input ? texture(u_texture, v_uv) : vec4(0.0);
        // Composite outputs are premultiplied; multiplying RGB and alpha
        // keeps the masked result valid for downstream merge/effect nodes.
        result = vec4(source.rgb * alpha, source.a * alpha);
    }

    fragColor = result;
}
"""

# ── Roto/Bezier Matte Shader ──
# The CPU flattens cubic Bezier segments into a bounded polyline.  Keeping the
# fill test in this shader makes the node usable in both standalone matte and
# image-masking workflows without a CPU raster upload per frame.
_ROTO_FRAGMENT = """
#version 330
const int MAX_POINTS = 64;
uniform sampler2D u_texture;
uniform bool u_has_input;
uniform vec2 u_points[MAX_POINTS];
uniform float u_point_feathers[MAX_POINTS];
uniform int u_point_count;
uniform float u_feather;
uniform float u_opacity;
uniform bool u_invert;
uniform int u_output_mode; // 0=matte, 1=result, 2=masked image
in vec2 v_uv;
out vec4 fragColor;

float distance_to_segment(vec2 p, vec2 a, vec2 b) {
    vec2 ab = b - a;
    float denom = max(dot(ab, ab), 0.0000001);
    float t = clamp(dot(p - a, ab) / denom, 0.0, 1.0);
    return length(p - (a + t * ab));
}

bool point_inside(vec2 p) {
    bool inside = false;
    for (int i = 0; i < MAX_POINTS; ++i) {
        if (i >= u_point_count) {
            break;
        }
        int j = (i == 0) ? (u_point_count - 1) : (i - 1);
        vec2 a = u_points[i];
        vec2 b = u_points[j];
        bool crosses = ((a.y > p.y) != (b.y > p.y));
        if (crosses) {
            float dy = b.y - a.y;
            float safe_dy = (abs(dy) < 0.0000001)
                          ? (dy < 0.0 ? -0.0000001 : 0.0000001)
                          : dy;
            float x_at_y = (b.x - a.x) * (p.y - a.y) / safe_dy + a.x;
            if (p.x < x_at_y) {
                inside = !inside;
            }
        }
    }
    return inside;
}

float edge_feather(vec2 p) {
    float result = 1000000.0;
    float feather = u_feather;
    for (int i = 0; i < MAX_POINTS; ++i) {
        if (i >= u_point_count) {
            break;
        }
        int j = (i + 1) % u_point_count;
        vec2 ab = u_points[j] - u_points[i];
        float denom = max(dot(ab, ab), 0.0000001);
        float t = clamp(dot(p - u_points[i], ab) / denom, 0.0, 1.0);
        float distance = length(p - (u_points[i] + t * ab));
        if (distance < result) {
            result = distance;
            float start_feather = u_point_feathers[i] < 0.0
                                ? u_feather : u_point_feathers[i];
            float end_feather = u_point_feathers[j] < 0.0
                              ? u_feather : u_point_feathers[j];
            feather = mix(start_feather, end_feather, t);
        }
    }
    return max(0.0, feather);
}

void main() {
    float alpha = 0.0;
    if (u_point_count >= 3 && point_inside(v_uv)) {
        float edge = 0.0;
        // Recompute the nearest edge distance alongside its interpolated
        // feather width.  The helper returns the width; the loop below keeps
        // the distance calculation explicit for predictable GLSL 330 code.
        float feather = edge_feather(v_uv);
        float nearest = 1000000.0;
        for (int i = 0; i < MAX_POINTS; ++i) {
            if (i >= u_point_count) {
                break;
            }
            int j = (i + 1) % u_point_count;
            nearest = min(nearest,
                          distance_to_segment(v_uv, u_points[i], u_points[j]));
        }
        edge = nearest;
        alpha = (feather > 0.000001)
              ? (1.0 - smoothstep(0.0, feather, edge))
              : 1.0;
    }
    alpha *= clamp(u_opacity, 0.0, 1.0);
    if (u_invert) {
        alpha = 1.0 - alpha;
    }

    if (u_output_mode == 2) {
        vec4 source = u_has_input ? texture(u_texture, v_uv) : vec4(0.0);
        fragColor = vec4(source.rgb * alpha, source.a * alpha);
    } else if (u_output_mode == 1) {
        fragColor = vec4(alpha, alpha, alpha, alpha);
    } else {
        fragColor = vec4(alpha, alpha, alpha, alpha);
    }
}
"""

# ── A/B Compare / QC Shader ──
# Split and wipe choose one source by an angled screen-space line. Difference
# keeps the absolute RGB delta visible for QC; the optional value mode turns it
# into a single grayscale error magnitude.
_COMPARE_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;
uniform sampler2D u_texture_b;
uniform int u_mode; // 0=split, 1=wipe, 2=difference
uniform float u_split_position;
uniform float u_split_angle;
uniform float u_wipe_position;
uniform float u_difference_scale;
uniform bool u_show_difference_value;
in vec2 v_uv;
out vec4 fragColor;

float side_of_line(vec2 uv, float position, float angle_degrees) {
    float angle = radians(angle_degrees);
    vec2 normal = vec2(cos(angle), sin(angle));
    return dot(uv - vec2(0.5), normal) - (position - 0.5);
}

void main() {
    vec4 a = texture(u_texture_a, v_uv);
    vec4 b = texture(u_texture_b, v_uv);
    if (u_mode == 2) {
        vec3 delta = abs(a.rgb - b.rgb) * max(u_difference_scale, 0.0);
        float value = clamp(max(max(delta.r, delta.g), delta.b), 0.0, 1.0);
        vec3 rgb = u_show_difference_value ? vec3(value) : clamp(delta, 0.0, 1.0);
        fragColor = vec4(rgb, max(a.a, b.a));
        return;
    }

    float position = (u_mode == 1) ? u_wipe_position : u_split_position;
    bool use_b = side_of_line(v_uv, position, u_split_angle) >= 0.0;
    fragColor = use_b ? b : a;
}
"""

# ── Light Wrap Shader ──
# Samples the background around the foreground matte edge. The foreground and
# background are explicitly treated as either straight or premultiplied so the
# output remains a valid premultiplied RGBA image for downstream merges.
_LIGHT_WRAP_FRAGMENT = """
#version 330
uniform sampler2D u_background;
uniform sampler2D u_foreground;
uniform vec2 u_background_resolution;
uniform vec2 u_foreground_resolution;
uniform float u_width;
uniform float u_intensity;
uniform float u_brightness;
uniform float u_falloff;
uniform bool u_background_premultiplied;
uniform bool u_foreground_premultiplied;
uniform bool u_source_alpha;
in vec2 v_uv;
out vec4 fragColor;

const int DIRECTION_COUNT = 8;
const int RING_COUNT = 4;
const vec2 DIRECTIONS[DIRECTION_COUNT] = vec2[DIRECTION_COUNT](
    vec2(1.0, 0.0), vec2(0.7071068, 0.7071068),
    vec2(0.0, 1.0), vec2(-0.7071068, 0.7071068),
    vec2(-1.0, 0.0), vec2(-0.7071068, -0.7071068),
    vec2(0.0, -1.0), vec2(0.7071068, -0.7071068)
);

void main() {
    vec4 background_sample = texture(u_background, v_uv);
    vec4 foreground_sample = texture(u_foreground, v_uv);
    vec3 foreground_rgb = u_foreground_premultiplied
        ? foreground_sample.rgb
        : foreground_sample.rgb * foreground_sample.a;

    float foreground_alpha = clamp(foreground_sample.a, 0.0, 1.0);
    vec3 background_sum = vec3(0.0);
    float background_alpha_sum = 0.0;
    float foreground_alpha_sum = 0.0;
    float sample_weight_sum = 0.0;
    vec2 foreground_pixel = 1.0 / max(u_foreground_resolution, vec2(1.0));
    vec2 background_pixel = 1.0 / max(u_background_resolution, vec2(1.0));

    for (int ring = 1; ring <= RING_COUNT; ring++) {
        float ring_ratio = float(ring) / float(RING_COUNT);
        float ring_weight = pow(max(1.0 - ring_ratio, 0.0),
                                max(u_falloff, 0.1));
        for (int direction = 0; direction < DIRECTION_COUNT; direction++) {
            vec2 direction_value = DIRECTIONS[direction];
            vec2 foreground_uv = v_uv + direction_value * foreground_pixel
                               * u_width * ring_ratio;
            vec2 background_uv = v_uv + direction_value * background_pixel
                               * u_width * ring_ratio;
            vec4 background_value = texture(u_background, background_uv);
            vec4 foreground_value = texture(u_foreground, foreground_uv);
            vec3 background_rgb = u_background_premultiplied
                ? background_value.rgb
                : background_value.rgb * background_value.a;
            background_sum += background_rgb * ring_weight;
            background_alpha_sum += background_value.a * ring_weight;
            foreground_alpha_sum += foreground_value.a * ring_weight;
            sample_weight_sum += ring_weight;
        }
    }

    vec3 wrapped_background = background_sum
        / max(sample_weight_sum, 0.0001);
    float sampled_foreground_alpha = foreground_alpha_sum
        / max(sample_weight_sum, 0.0001);
    float edge_alpha = max(sampled_foreground_alpha - foreground_alpha,
                            foreground_alpha * (1.0 - foreground_alpha));
    float wrap_alpha = clamp(edge_alpha * max(u_intensity, 0.0), 0.0, 1.0);
    if (!u_source_alpha) {
        wrap_alpha = clamp((1.0 - foreground_alpha)
                           * max(u_intensity, 0.0), 0.0, 1.0);
    }

    vec3 wrap_rgb = wrapped_background * max(u_brightness, 0.0) * wrap_alpha;
    float output_alpha = max(foreground_alpha, wrap_alpha);
    fragColor = vec4(foreground_rgb + wrap_rgb, output_alpha);
}
"""

# ── Mask Boolean Merge Shader ──
_MASK_MERGE_FRAGMENT = """
#version 330
uniform sampler2D u_texture_a;
uniform sampler2D u_texture_b;
uniform int u_operation;
uniform bool u_invert_a;
uniform bool u_invert_b;
uniform float u_opacity_a;
uniform float u_opacity_b;
in vec2 v_uv;
out vec4 fragColor;

float matte_value(vec4 sample_value) {
    float luma = dot(sample_value.rgb, vec3(0.2126, 0.7152, 0.0722));
    return clamp(max(sample_value.a, luma), 0.0, 1.0);
}

void main() {
    float a = matte_value(texture(u_texture_a, v_uv));
    float b = matte_value(texture(u_texture_b, v_uv));
    if (u_invert_a) { a = 1.0 - a; }
    if (u_invert_b) { b = 1.0 - b; }
    a *= clamp(u_opacity_a, 0.0, 1.0);
    b *= clamp(u_opacity_b, 0.0, 1.0);

    float value = 0.0;
    if (u_operation == 0) {
        value = min(1.0, a + b);
    } else if (u_operation == 1) {
        value = max(0.0, a - b);
    } else if (u_operation == 2) {
        value = a * b;
    } else if (u_operation == 3) {
        value = max(a, b);
    } else if (u_operation == 4) {
        value = min(a, b);
    } else if (u_operation == 5) {
        value = abs(a - b);
    } else if (u_operation == 6) {
        value = (a + b) * 0.5;
    } else {
        value = 1.0 - (1.0 - a) * (1.0 - b);
    }
    fragColor = vec4(value, value, value, value);
}
"""

_DENOISE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_mask_tex;
uniform bool u_use_mask;
uniform vec2 u_resolution;
uniform float u_strength;
uniform int u_radius;
uniform bool u_luminance_only;
uniform bool u_chroma_only;
in vec2 v_uv;
out vec4 fragColor;

const int MAX_RADIUS = 10;

// Bilateral filter: combines spatial and color range weighting
// Spatial weight: Gaussian based on pixel distance
// Range weight: Gaussian based on color difference
// This preserves edges while smoothing noise

void main() {
    // Skip if radius is zero or strength is negligible
    if (u_radius < 1 || u_strength < 0.001) {
        fragColor = texture(u_texture, v_uv);
        return;
    }

    vec4 center = texture(u_texture, v_uv);

    // Compute spatial sigma based on radius
    float sigma_spatial = max(float(u_radius) * 0.4, 1.0);

    // Range sigma determines edge preservation
    // Higher sigma = more smoothing (less edge preservation)
    // Lower sigma = stronger edge preservation
    float sigma_range = max(0.1, 0.15 * (1.0 - u_strength * 0.5));

    vec4 sum = vec4(0.0);
    float weight_sum = 0.0;

    // Clamp radius to MAX_RADIUS for GLSL loop constraints
    int clamped_radius = min(u_radius, MAX_RADIUS);

    // Bilateral filter kernel
    for (int y = -MAX_RADIUS; y <= MAX_RADIUS; y++) {
        // Skip pixels outside the clamped radius
        if (abs(y) > clamped_radius) continue;

        for (int x = -MAX_RADIUS; x <= MAX_RADIUS; x++) {
            // Skip pixels outside the clamped radius
            if (abs(x) > clamped_radius) continue;

            // Compute offset
            vec2 offset = vec2(float(x), float(y)) / u_resolution;
            vec2 sample_uv = v_uv + offset;

            // Clamp UV to valid range (edge policy: clamp)
            sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));

            vec4 sample_color = texture(u_texture, sample_uv);

            // Spatial weight: Gaussian based on distance
            float dist_sq = float(x * x + y * y);
            float spatial_weight = exp(-dist_sq / (2.0 * sigma_spatial * sigma_spatial));

            // Range weight: Gaussian based on color difference
            vec4 color_diff = center - sample_color;

            // For luminance_only mode, compute range weight based on luminance difference only
            float range_diff_sq;
            if (u_luminance_only) {
                // Luminance difference (Rec. 709 coefficients)
                float center_luma = dot(center.rgb, vec3(0.2126, 0.7152, 0.0722));
                float sample_luma = dot(sample_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                float luma_diff = center_luma - sample_luma;
                range_diff_sq = luma_diff * luma_diff;
            } else if (u_chroma_only) {
                // Chroma difference (ignore luminance)
                // Convert to approximate chroma (Cb, Cr-like)
                vec3 center_chroma = center.rgb - vec3(dot(center.rgb, vec3(0.2126, 0.7152, 0.0722)));
                vec3 sample_chroma = sample_color.rgb - vec3(dot(sample_color.rgb, vec3(0.2126, 0.7152, 0.0722)));
                vec3 chroma_diff = center_chroma - sample_chroma;
                range_diff_sq = dot(chroma_diff, chroma_diff);
            } else {
                // Full color difference (RGB)
                range_diff_sq = dot(color_diff.rgb, color_diff.rgb);
            }

            float range_weight = exp(-range_diff_sq / (2.0 * sigma_range * sigma_range));

            // Combined weight
            float weight = spatial_weight * range_weight;

            sum += sample_color * weight;
            weight_sum += weight;
        }
    }

    // Normalize
    vec4 filtered = sum / max(weight_sum, 0.0001);

    // Mix original and filtered based on strength
    // strength = 0.0: no denoising (original)
    // strength = 1.0: full denoising (filtered)
    vec4 result = mix(center, filtered, u_strength);

    // For luminance_only or chroma_only modes, preserve other components
    if (u_luminance_only) {
        // Only apply denoising to luminance
        float center_luma = dot(center.rgb, vec3(0.2126, 0.7152, 0.0722));
        float filtered_luma = dot(filtered.rgb, vec3(0.2126, 0.7152, 0.0722));
        float result_luma = mix(center_luma, filtered_luma, u_strength);

        // Preserve original chroma
        vec3 center_chroma = center.rgb - vec3(center_luma);
        result.rgb = vec3(result_luma) + center_chroma;
        result.a = center.a;
    } else if (u_chroma_only) {
        // Only apply denoising to chroma
        float center_luma = dot(center.rgb, vec3(0.2126, 0.7152, 0.0722));
        float filtered_luma = dot(filtered.rgb, vec3(0.2126, 0.7152, 0.0722));

        // Compute chroma components
        vec3 center_chroma = center.rgb - vec3(center_luma);
        vec3 filtered_chroma = filtered.rgb - vec3(filtered_luma);
        vec3 result_chroma = mix(center_chroma, filtered_chroma, u_strength);

        // Preserve original luminance
        result.rgb = vec3(center_luma) + result_chroma;
        result.a = center.a;
    }

    // Optional mask input (face_smooth etc.): pixels outside the mask are
    // returned untouched — the effect only ever blends inside mask > 0.
    if (u_use_mask) {
        float mask_amount = texture(u_mask_tex, v_uv).r;
        result = mix(center, result, clamp(mask_amount, 0.0, 1.0));
    }

    fragColor = result;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# 3D Mesh Shaders (Scene3D node)
# ─────────────────────────────────────────────────────────────────────────────
# 阶段一：基础法线着色 + 单方向光（Lambertian diffuse + 环境光）。
# 阶段四将替换为 PBR（cook-torrance BRDF + IBL）。
#
# 注意：与全屏后处理 shader 不同，这里用独立的 vertex shader（带 proj/view/model 矩阵）。
# 顶点布局：交错 '3f 3f 2f' = in_pos(vec3) | in_normal(vec3) | in_uv(vec2)。
# 保持 #version 330 与现有栈一致。

_MESH_VERTEX = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;

out vec3 v_world_pos;
out vec3 v_normal;
out vec2 v_uv;
out vec3 v_tangent;  // 占位（无切线数据时写 0；与 PBR fragment 的 in v_tangent 匹配避免链接错误）

void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    v_world_pos = world.xyz;
    // 法线变换：忽略非均匀缩放的精确性（阶段一用 mat3(model) 足够）
    v_normal = mat3(u_model) * in_normal;
    v_uv = in_uv;
    v_tangent = vec3(0.0);  // 无切线；normal mapping 时用 _MESH_VERTEX_TANGENT
    gl_Position = u_proj * u_view * world;
}
"""

# Model3D 材质升级：带切线的顶点着色器（normal mapping 用）。
# 布局 '3f 3f 2f 3f'（pos/normal/uv/tangent.xyz），配合 MeshData.interleaved_with_tangent。
_MESH_VERTEX_TANGENT = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec3 in_tangent;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;

out vec3 v_world_pos;
out vec3 v_normal;
out vec2 v_uv;
out vec3 v_tangent;

void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    v_world_pos = world.xyz;
    v_normal = mat3(u_model) * in_normal;
    v_uv = in_uv;
    v_tangent = mat3(u_model) * in_tangent;  // 切线随 model 变换
    gl_Position = u_proj * u_view * world;
}
"""

# ─── 阶段五：蒙皮顶点着色器（带骨骼动画）───
# 顶点布局 '3f 3f 2f 4f 4f' = pos(3) | normal(3) | uv(2) | boneIds(4,f32) | weights(4)
# boneIds 用 float 编码（避免 float/int 混合缓冲区），shader 内转 int。
# 每顶点最多 4 根骨骼（w1..w4 加权和≈1），MAX_BONES=64 上限。
# 与 _MESH_PBR_FRAGMENT 配合（片段 shader 不变，只多算蒙皮后的 pos/normal）。
_MESH_VERTEX_SKINNED = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec4 in_bone_ids;     // float 编码的骨骼索引（最多 4）
in vec4 in_bone_weights; // 对应权重（行和≈1）

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
#define MAX_BONES 64
uniform mat4 u_bone_matrices[MAX_BONES];  // 每骨骼的世界矩阵（= joint world * inverse_bind）

out vec3 v_world_pos;
out vec3 v_normal;
out vec2 v_uv;

void main() {
    // 蒙皮：skinned = Σ wᵢ · (bone_matrixᵢ · pos)
    // 注意 bone_matrix 已含 inverse_bind，故直接乘模型空间 pos（仍在 mesh local）。
    vec4 skinned_pos = vec4(0.0);
    vec3 skinned_normal = vec3(0.0);
    // 4 根骨骼展开循环（GLSL 330 允许动态索引，但展开更稳）
    for (int i = 0; i < 4; ++i) {
        float w = in_bone_weights[i];
        if (w <= 0.0) continue;
        int bone_idx = int(in_bone_ids[i] + 0.5);  // float→int
        if (bone_idx < 0 || bone_idx >= MAX_BONES) continue;
        mat4 bm = u_bone_matrices[bone_idx];
        skinned_pos += bm * vec4(in_pos, 1.0) * w;
        skinned_normal += mat3(bm) * in_normal * w;
    }
    // 全零权重（未蒙皮顶点）兜底：用原始 pos/normal
    if (dot(in_bone_weights, vec4(1.0)) <= 0.0) {
        skinned_pos = vec4(in_pos, 1.0);
        skinned_normal = in_normal;
    }

    // 蒙皮后的顶点再过 model 矩阵（mesh local → world，含父子层级）
    vec4 world = u_model * skinned_pos;
    v_world_pos = world.xyz;
    v_normal = mat3(u_model) * skinned_normal;
    v_uv = in_uv;
    gl_Position = u_proj * u_view * world;
}
"""

# P0-07：运动矢量顶点程序。输出当前帧与上一帧的屏幕空间位移，单位为
# normalized UV displacement（与 2D Blur 的 velocity 输入契约一致）。上一帧
# 矩阵由 Scene3D 只在渲染线程维护；首帧/跳帧时由 CPU 将 u_motion_scale 设为 0。
_MESH_MOTION_VERTEX = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
uniform mat4 u_prev_proj;
uniform mat4 u_prev_view;
uniform mat4 u_prev_model;
uniform float u_motion_scale;

out vec2 v_motion;

void main() {
    vec4 current_world = u_model * vec4(in_pos, 1.0);
    vec4 previous_world = u_prev_model * vec4(in_pos, 1.0);
    vec4 current_clip = u_proj * u_view * current_world;
    vec4 previous_clip = u_prev_proj * u_prev_view * previous_world;
    gl_Position = current_clip;

    float current_w = max(abs(current_clip.w), 1e-6);
    float previous_w = max(abs(previous_clip.w), 1e-6);
    vec2 current_uv = current_clip.xy / current_w * 0.5 + vec2(0.5);
    vec2 previous_uv = previous_clip.xy / previous_w * 0.5 + vec2(0.5);
    v_motion = (current_uv - previous_uv) * u_motion_scale;
}
"""

_MESH_MOTION_VERTEX_SKINNED = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec4 in_bone_ids;
in vec4 in_bone_weights;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
uniform mat4 u_prev_proj;
uniform mat4 u_prev_view;
uniform mat4 u_prev_model;
uniform float u_motion_scale;

#define MAX_BONES 64
uniform mat4 u_bone_matrices[MAX_BONES];
uniform mat4 u_prev_bone_matrices[MAX_BONES];

out vec2 v_motion;

void main() {
    vec4 current_local = vec4(0.0);
    vec4 previous_local = vec4(0.0);
    for (int i = 0; i < 4; ++i) {
        float weight = in_bone_weights[i];
        if (weight <= 0.0) continue;
        int bone_idx = int(in_bone_ids[i] + 0.5);
        if (bone_idx < 0 || bone_idx >= MAX_BONES) continue;
        current_local += u_bone_matrices[bone_idx] * vec4(in_pos, 1.0) * weight;
        previous_local += u_prev_bone_matrices[bone_idx] * vec4(in_pos, 1.0) * weight;
    }
    if (dot(in_bone_weights, vec4(1.0)) <= 0.0) {
        current_local = vec4(in_pos, 1.0);
        previous_local = vec4(in_pos, 1.0);
    }

    vec4 current_world = u_model * current_local;
    vec4 previous_world = u_prev_model * previous_local;
    vec4 current_clip = u_proj * u_view * current_world;
    vec4 previous_clip = u_prev_proj * u_prev_view * previous_world;
    gl_Position = current_clip;

    float current_w = max(abs(current_clip.w), 1e-6);
    float previous_w = max(abs(previous_clip.w), 1e-6);
    vec2 current_uv = current_clip.xy / current_w * 0.5 + vec2(0.5);
    vec2 previous_uv = previous_clip.xy / previous_w * 0.5 + vec2(0.5);
    v_motion = (current_uv - previous_uv) * u_motion_scale;
}
"""

_MESH_MOTION_VERTEX_INSTANCED = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec3 in_instance_offset;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
uniform mat4 u_prev_proj;
uniform mat4 u_prev_view;
uniform mat4 u_prev_model;
uniform float u_motion_scale;

out vec2 v_motion;

void main() {
    vec4 local = vec4(in_pos + in_instance_offset, 1.0);
    vec4 current_world = u_model * local;
    vec4 previous_world = u_prev_model * local;
    vec4 current_clip = u_proj * u_view * current_world;
    vec4 previous_clip = u_prev_proj * u_prev_view * previous_world;
    gl_Position = current_clip;

    float current_w = max(abs(current_clip.w), 1e-6);
    float previous_w = max(abs(previous_clip.w), 1e-6);
    vec2 current_uv = current_clip.xy / current_w * 0.5 + vec2(0.5);
    vec2 previous_uv = previous_clip.xy / previous_w * 0.5 + vec2(0.5);
    v_motion = (current_uv - previous_uv) * u_motion_scale;
}
"""

# ─── 阶段六：实例化顶点着色器（grid 复制器）───
# 布局同 _MESH_VERTEX（3f 3f 2f）+ instanced `in vec3 in_instance_offset`。
# offset 仅平移位置（平移不改法线），与 Element 3D 阵列克隆语义一致。
# moderngl 通过 VAO content 的 instance buffer 自动按实例推进（divisor）。
_MESH_VERTEX_INSTANCED = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec3 in_instance_offset;   // 每实例位置偏移（instanced attribute）

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;

out vec3 v_world_pos;
out vec3 v_normal;
out vec2 v_uv;

void main() {
    // offset 加到 mesh local pos（model 矩阵之后的世界变换仍在 u_model 内）
    vec4 world = u_model * vec4(in_pos + in_instance_offset, 1.0);
    v_world_pos = world.xyz;
    // 平移不改法线
    v_normal = mat3(u_model) * in_normal;
    v_uv = in_uv;
    gl_Position = u_proj * u_view * world;
}
"""

# Same world-space varyings as _MESH_VERTEX, but used by auxiliary passes that
# need an instance offset. Keeping this as a separate program avoids issuing
# instanced draws through a VAO whose shader has no instance attribute.
_MESH_VERTEX_INSTANCED_DEPTH = """
#version 330
in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;
in vec3 in_instance_offset;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;

out vec3 v_world_pos;

void main() {
    vec4 world = u_model * vec4(in_pos + in_instance_offset, 1.0);
    v_world_pos = world.xyz;
    gl_Position = u_proj * u_view * world;
}
"""

# ─── 阶段六：深度写入片段着色器（线性深度，R32F）───
# 与 deep_merge 约定一致：1.0=远，0.0=近（merge_evaluator clear 用 1.0）。
# 配合 _MESH_VERTEX（复用 v_world_pos）。第二遍渲染：把每个 mesh 重画到
# depth-only FBO，输出归一化线性深度 (dist-near)/(far-near)，clamp 到 [0,1]。
_MESH_DEPTH_WRITE_FRAGMENT = """
#version 330
in vec3 v_world_pos;

uniform vec3  u_cam_pos;       // 世界空间观察位置
uniform float u_near;          // 投影 near
uniform float u_far;           // 投影 far

out float out_depth;           // R32F 单通道

void main() {
    float dist = length(v_world_pos - u_cam_pos);
    float d = (dist - u_near) / max(u_far - u_near, 1e-4);
    out_depth = clamp(d, 0.0, 1.0);  // 0=近 1=远
}
"""

# Shadow-map depth pass. The colour attachment is an R32F texture and the
# hardware depth attachment still provides nearest-surface rejection. Unlike
# the deep-composite depth pass this stores post-projection depth, which can be
# compared directly after projecting a receiver into light space.
_MESH_SHADOW_DEPTH_FRAGMENT = """
#version 330
out float out_depth;

void main() {
    out_depth = gl_FragCoord.z;
}
"""

# Directional-light shadow catcher. The catcher geometry is rendered into the
# camera output with premultiplied dark colour and alpha equal to shadow
# coverage, so it can be placed over a live-action plate without displaying
# the catcher mesh itself. A fixed 3x3 PCF kernel keeps the loop bounds
# compile-time constants on the macOS GLSL 330 path.
_MESH_SHADOW_CATCHER_FRAGMENT = """
#version 330
in vec3 v_world_pos;

uniform sampler2D u_shadow_map;
uniform mat4 u_light_view_proj;
uniform vec3 u_shadow_color;
uniform float u_shadow_intensity;
uniform float u_shadow_softness;
uniform float u_shadow_bias;

out vec4 out_shadow;

void main() {
    vec4 light_clip = u_light_view_proj * vec4(v_world_pos, 1.0);
    float light_w = max(abs(light_clip.w), 1e-6);
    vec3 light_ndc = light_clip.xyz / light_w;
    vec2 shadow_uv = light_ndc.xy * 0.5 + vec2(0.5);
    if (shadow_uv.x < 0.0 || shadow_uv.x > 1.0
            || shadow_uv.y < 0.0 || shadow_uv.y > 1.0
            || light_ndc.z < -1.0 || light_ndc.z > 1.0) {
        out_shadow = vec4(0.0);
        return;
    }

    float receiver_depth = light_ndc.z * 0.5 + 0.5;
    vec2 texel = 1.0 / vec2(textureSize(u_shadow_map, 0));
    int radius = int(round(clamp(u_shadow_softness, 0.0, 1.0) * 2.0));
    float occluded = 0.0;
    float samples = 0.0;
    for (int y = -2; y <= 2; ++y) {
        for (int x = -2; x <= 2; ++x) {
            if (abs(x) > radius || abs(y) > radius) continue;
            float stored_depth = texture(
                u_shadow_map, shadow_uv + vec2(x, y) * texel).r;
            occluded += step(stored_depth + u_shadow_bias, receiver_depth);
            samples += 1.0;
        }
    }
    float shadow = (occluded / max(samples, 1.0))
                   * max(u_shadow_intensity, 0.0);
    float alpha = clamp(shadow, 0.0, 1.0);
    out_shadow = vec4(u_shadow_color * alpha, alpha);
}
"""

# P0-07：通用几何 AOV pass。复用 Scene3D 的位置/法线/UV varying，避免把
# beauty shader 的光照结果误当作几何诊断数据。Normal 编码到 [0, 1] 便于
# Viewer 直接检查；Position 保留世界空间 HDR 值供 EXR/下游节点使用。
_MESH_AOV_FRAGMENT = """
#version 330
in vec3 v_world_pos;
in vec3 v_normal;
in vec2 v_uv;

uniform int u_aov_mode;  // 0=normal, 1=position, 2=uv, 3=material_id
uniform vec4 u_aov_id;

out vec4 out_aov;

void main() {
    if (u_aov_mode == 0) {
        vec3 normal = normalize(v_normal);
        out_aov = vec4(normal * 0.5 + vec3(0.5), 1.0);
    } else if (u_aov_mode == 1) {
        out_aov = vec4(v_world_pos, 1.0);
    } else if (u_aov_mode == 3) {
        out_aov = u_aov_id;
    } else {
        out_aov = vec4(v_uv, 0.0, 1.0);
    }
}
"""

_MESH_MOTION_FRAGMENT = """
#version 330
in vec2 v_motion;

out vec4 out_motion;

void main() {
    // RG is signed normalized UV displacement for one timeline frame.
    out_motion = vec4(v_motion, 0.0, 1.0);
}
"""


# P7：GPU ID picking 用——每像素输出该 mesh 的 object id（RGBA8 编码，0=背景）。
# 复用 _MESH_VERTEX（同深度 pass，无材质/光照计算），fragment 仅输出常量 id。
# 比 PBR 便宜得多（无纹理采样、无 BRDF、无 IBL），点击拾取时按需渲染。
_MESH_ID_WRITE_FRAGMENT = """
#version 330

// P7：object id 编码为归一化 RGBA（pick_id 拆成 R/G/B 三字节，A=1 表示非背景）。
// pick_id=0（背景）时输出 (0,0,0,0) → 读回时 alpha=0 判定未命中。
uniform vec4 u_object_id;

out vec4 out_id;

void main() {
    out_id = u_object_id;
}
"""


# P7：选中对象描边（inverted hull）——顶点沿法线膨胀 + 背面渲染 + cull front。
# 与 _MESH_VERTEX 同布局（in_pos/in_normal/in_uv），但 vertex 沿 normal 外推
# u_outline_scale（世界单位），fragment 输出常量描边色 u_outline_color。
# 只画选中对象的 mesh，在主渲染后叠加 → 选中物体周围出现描边轮廓。
_MESH_OUTLINE_VERTEX = """
#version 330
layout(location = 0) in vec3 in_pos;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec2 in_uv;

uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
uniform float u_outline_scale;   // 沿法线膨胀量（世界单位）

out vec3 v_world_pos;

void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    vec3 world_normal = normalize(mat3(u_model) * in_normal);
    // 沿法线外推（inverted hull：放大后的背面挡住正面边缘 → 形成描边）
    world.xyz += world_normal * u_outline_scale;
    v_world_pos = world.xyz;
    gl_Position = u_proj * u_view * world;
}
"""

_MESH_OUTLINE_FRAGMENT = """
#version 330
in vec3 v_world_pos;

uniform vec4 u_outline_color;   // 描边色（RGBA，通常亮黄/橙）

out vec4 fragColor;

void main() {
    fragColor = u_outline_color;
}
"""

_MESH_BASIC_FRAGMENT = """
#version 330
in vec3 v_world_pos;
in vec3 v_normal;
in vec2 v_uv;

uniform vec3 u_light_dir;       // 指向光源的方向（已归一化，世界空间）
uniform float u_light_intensity; // 光照强度（>= 0）
uniform vec3 u_object_color;    // 固有色（线性空间，albedo）

out vec4 fragColor;

void main() {
    vec3 N = normalize(v_normal);
    vec3 L = normalize(u_light_dir);

    // Lambertian 漫反射
    float ndotl = max(dot(N, L), 0.0);
    float ambient = 0.25;  // 环境光系数
    float light = ambient + ndotl * u_light_intensity;

    vec3 color = u_object_color * light;
    fragColor = vec4(color, 1.0);
}
"""


# ─── 阶段四：PBR（Cook-Torrance BRDF + IBL）───
# macOS OpenGL 4.1 上限，无 compute shader；IBL 卷积用 fragment shader 完成（见 hdri.py）。
# 输出保持线性 HDR（不做 tonemap —— 色调映射是 color page 职责）。
# PBR 数学参考：learnopengl.com/PBR + Epic Siggraph 2013 "Real Shading"。
# 顶点着色器复用 _MESH_VERTEX（已输出 v_world_pos/v_normal/v_uv）。

_MESH_PBR_FRAGMENT = """
#version 330
in vec3 v_world_pos;
in vec3 v_normal;
in vec2 v_uv;
in vec3 v_tangent;  // Model3D 材质升级：切线（normal mapping 用；无切线 shader 不写，u_has_normal_map=0 时不读）

// ── 材质 ──
uniform vec3  u_albedo;       // 线性 albedo（sRGB→线性 后输入）
uniform float u_metallic;     // 0=电介质 1=金属
uniform float u_roughness;    // 0.01=镜面 1=漫散射

// Model3D 材质升级：材质纹理采样器 + has_map 标志（0=用标量，1=采样贴图）
uniform sampler2D u_base_color_map;   // baseColor/albedo 贴图（sRGB，shader 外或此处转线性）
uniform sampler2D u_normal_map;       // 法线贴图（tangent space）
uniform sampler2D u_orm_map;          // glTF ORM 打包（O=R, R=G, M=B）
uniform sampler2D u_ao_map;           // 环境光遮蔽（或 ORM 的 R 通道）
uniform sampler2D u_emissive_map;     // 自发光贴图
uniform int   u_has_base_color_map;   // 0/1
uniform int   u_has_normal_map;       // 0/1（需 v_tangent，否则禁用）
uniform int   u_has_orm_map;          // 0/1
uniform int   u_has_ao_map;           // 0/1
uniform int   u_has_emissive_map;     // 0/1
uniform float u_emissive_intensity;   // 自发光强度倍率
uniform vec3  u_emissive;             // 自发光标量颜色（无贴图时用）
// normal map 格式：0=OpenGL（默认），1=DirectX（翻转 G 通道）
uniform int   u_normal_format;

// ── 相机 ──
uniform vec3  u_cam_pos;      // 世界空间观察位置

// ── 直接光（最多 4 盏，支持多类型：0=dir 1=point 2=spot）──
#define MAX_LIGHTS 4
#define LIGHT_TYPE_DIR   0   // 平行光（无位置，无衰减）
#define LIGHT_TYPE_POINT 1   // 点光源（有位置，距离衰减）
#define LIGHT_TYPE_SPOT  2   // 聚光灯（有位置+方向+锥角）
uniform int   u_light_count;                  // 有效直接光数（ambient 不计入，<=MAX_LIGHTS）
uniform int   u_light_type[MAX_LIGHTS];       // 0/1/2（见上方宏）
uniform vec3  u_light_dir[MAX_LIGHTS];        // 方向（dir=指向光源；spot=主轴方向，指向被照物）
uniform vec3  u_light_pos[MAX_LIGHTS];        // 位置（point/spot 世界坐标；dir 忽略）
uniform vec3  u_light_color[MAX_LIGHTS];      // 线性颜色
uniform float u_light_intensity[MAX_LIGHTS];  // 强度（>=0）
uniform float u_light_range[MAX_LIGHTS];      // 有效半径（point/spot；0=无限）
uniform float u_light_inner[MAX_LIGHTS];      // 聚光内锥 cos 角（spot）
uniform float u_light_outer[MAX_LIGHTS];      // 聚光外锥 cos 角（spot）
uniform int   u_light_decay_mode[MAX_LIGHTS]; // 0=none 1=linear 2=inverse-square
uniform int   u_light_ies_enabled[MAX_LIGHTS];
uniform vec4  u_light_ies_profile[MAX_LIGHTS]; // normalized samples: 0/30/60/90°

// ── 全局环境光常量（ambient 类型灯贡献累加到此）──
uniform vec3  u_ambient_color;                // 环境光颜色（已含强度）
uniform float u_ambient_intensity;            // 环境光强度倍率（与 u_ambient_color 分离便于 UI）

// ── 曝光（P3 摄影机扩展：EV 值，最终输出乘 pow(2, exposure)）──
uniform float u_exposure;                     // 曝光 EV（0=线性不变，+1=亮一档，-1=暗一档）

// ── IBL 环境光（由 hdri.py 预计算）──
uniform samplerCube u_irradiance_map;   // 漫反射辐照度 cubemap
uniform samplerCube u_prefilter_map;    // 镜面预过滤 cubemap（含 mip，按 roughness 采样）
uniform sampler2D   u_brdf_lut;         // (scale, bias) 查找表
uniform float u_env_intensity;          // 环境光整体强度倍率
uniform float u_prefilter_max_lod;      // prefilter cubemap 最大 mip 层数

out vec4 fragColor;

const float PI = 3.14159265359;

// ── GGX / Trowbridge-Reitz 法线分布 ──
float distribution_ggx(vec3 N, vec3 H, float roughness) {
    float a  = roughness * roughness;
    float a2 = a * a;
    float NdotH = max(dot(N, H), 0.0);
    float NdotH2 = NdotH * NdotH;
    float denom = (NdotH2 * (a2 - 1.0) + 1.0);
    denom = PI * denom * denom;
    return a2 / max(denom, 1e-7);
}

// ── Schlick-GGX 几何（单方向）──
float geometry_schlick_ggx(float NdotV, float roughness) {
    float r = roughness + 1.0;
    float k = (r * r) / 8.0;  // 直接光用 (r+1)²/8
    return NdotV / (NdotV * (1.0 - k) + k);
}

// ── Smith 几何遮蔽（双向组合）──
float geometry_smith(float NdotV, float NdotL, float roughness) {
    return geometry_schlick_ggx(NdotV, roughness) *
           geometry_schlick_ggx(NdotL, roughness);
}

// ── Fresnel-Schlick ──
vec3 fresnel_schlick(float cosTheta, vec3 F0) {
    return F0 + (1.0 - F0) * pow(clamp(1.0 - cosTheta, 0.0, 1.0), 5.0);
}

// ── Fresnel-Schlick（粗糙度版本，用于 IBL 镜面）──
vec3 fresnel_schlick_roughness(float cosTheta, vec3 F0, float roughness) {
    return F0 + (max(vec3(1.0 - roughness), F0) - F0) *
           pow(clamp(1.0 - cosTheta, 0.0, 1.0), 5.0);
}

// ── 灯光衰减（point/spot 通用）──
// UE4 光滑倒数平方衰减：att = clamp(1-(d/r)^4,0,1) / (d^2)
// r<=0 视为无限远（无衰减，返回 1.0）。
float light_attenuation(float dist, float r, int decay_mode) {
    if (decay_mode == 0) {
        return 1.0;
    }
    if (decay_mode == 1) {
        if (r <= 0.0001) {
            return 1.0 / max(dist, 1e-4);
        }
        return max(1.0 - dist / r, 0.0) / max(dist, 1e-4);
    }
    if (r <= 0.0001) {
        return 1.0 / max(dist * dist, 1e-4);
    }
    float att = clamp(1.0 - pow(dist / r, 4.0), 0.0, 1.0);
    return att / (dist * dist + 1e-4);
}

// ── 聚光锥角强度（spot）──
// cosTheta = dot(L, spot_axis)，inner/outer 为锥角 cos 值。
// 返回 0..1：锥内全亮，外锥外全黑，内外之间 smoothstep 过渡。
float spot_cone_intensity(float cosTheta, float innerCos, float outerCos) {
    return clamp((cosTheta - outerCos) / max(innerCos - outerCos, 1e-4), 0.0, 1.0);
}

float ies_profile_intensity(vec4 profile, float cosTheta) {
    float t = clamp(cosTheta, 0.0, 1.0) * 3.0;
    if (t < 1.0) {
        return mix(profile.x, profile.y, t);
    }
    if (t < 2.0) {
        return mix(profile.y, profile.z, t - 1.0);
    }
    return mix(profile.z, profile.w, min(t - 2.0, 1.0));
}

void main() {
    // ── Model3D 材质升级：按 has_map 标志采样材质纹理 ──
    // baseColor 贴图（sRGB→线性近似：^2.2）覆盖标量 u_albedo
    vec3 albedo = u_albedo;
    if (u_has_base_color_map == 1) {
        vec3 tex_col = texture(u_base_color_map, v_uv).rgb;
        // 输入假设 sRGB（color role）；转线性近似（无 OCIO 时的合理近似）
        albedo = pow(tex_col, vec3(2.2));
    }
    // metallic/roughness：ORM 贴图（glTF: M=B, R=G）或独立 metallic/roughness 贴图
    float metallic = u_metallic;
    float roughness = u_roughness;
    if (u_has_orm_map == 1) {
        vec3 orm = texture(u_orm_map, v_uv).rgb;
        roughness = orm.g;
        metallic = orm.b;
    }
    // AO：单独贴图或 ORM 的 R 通道
    float ao = 1.0;
    if (u_has_ao_map == 1) {
        ao = texture(u_ao_map, v_uv).r;
    } else if (u_has_orm_map == 1) {
        ao = texture(u_orm_map, v_uv).r;  // ORM 的 O 通道
    }

    // 法线：normal map（需 TBN）或顶点法线
    vec3 N = normalize(v_normal);
    if (u_has_normal_map == 1) {
        vec3 T = normalize(v_tangent);
        vec3 B = cross(N, T);
        // 采样切线空间法线（[0,1]→[-1,1]）
        vec3 n_tan = texture(u_normal_map, v_uv).rgb * 2.0 - 1.0;
        // DirectX 格式翻转 G 通道
        if (u_normal_format == 1) n_tan.y = -n_tan.y;
        N = normalize(T * n_tan.x + B * n_tan.y + N * n_tan.z);
    }
    vec3 V = normalize(u_cam_pos - v_world_pos);
    float NdotV = max(dot(N, V), 0.0);

    // F0：电介质 0.04，金属用 albedo
    vec3 F0 = mix(vec3(0.04), albedo, metallic);

    vec3 Lo = vec3(0.0);  // 直接光累加（radiance）

    for (int i = 0; i < MAX_LIGHTS; ++i) {
        if (i >= u_light_count) break;  // 动态上界（GLSL 330 for 边界须常量）

        // ── 按灯光类型计算 L（指向光源的方向）+ 衰减 att ──
        vec3 L;
        float att = 1.0;
        int lt = u_light_type[i];
        if (lt == LIGHT_TYPE_DIR) {
            // 平行光：L = 归一化方向，无衰减
            L = normalize(u_light_dir[i]);
        } else {
            // point / spot：从片段位置指向光源
            vec3 toLight = u_light_pos[i] - v_world_pos;
            float dist = length(toLight);
            L = toLight / max(dist, 1e-4);
            att = light_attenuation(
                dist, u_light_range[i], u_light_decay_mode[i]);
            float cosTheta = dot(L, normalize(-u_light_dir[i]));
            if (lt == LIGHT_TYPE_SPOT) {
                // 聚光灯锥角：spot 主轴方向 = u_light_dir[i]（指向被照物）
                att *= spot_cone_intensity(cosTheta, u_light_inner[i], u_light_outer[i]);
            }
            if (u_light_ies_enabled[i] == 1) {
                att *= ies_profile_intensity(u_light_ies_profile[i], cosTheta);
            }
        }

        vec3 H = normalize(V + L);
        float NdotL = max(dot(N, L), 0.0);

        // Cook-Torrance 镜面
        float D = distribution_ggx(N, H, roughness);
        float G = geometry_smith(NdotV, NdotL, roughness);
        vec3  F = fresnel_schlick(max(dot(H, V), 0.0), F0);
        vec3 numerator = D * G * F;
        float denom = 4.0 * NdotV * NdotL + 1e-4;
        vec3 specular = numerator / max(denom, 1e-7);

        // 漫反射（金属几乎无漫反射）
        vec3 kD = (vec3(1.0) - F) * (1.0 - metallic);
        vec3 radiance = u_light_color[i] * u_light_intensity[i] * att;
        Lo += (kD * albedo / PI + specular) * radiance * NdotL;
    }

    // ── IBL 环境光 ──
    // P8-1：u_env_intensity <= 0（navigation/FAST_SOLID profile）时跳过全部
    // IBL 采样（irradiance/prefilter/brdf 三次 cubemap/LUT 采样），节省 GPU 带宽。
    // 静止态 IBL 强度 > 0，走完整 PBR；导航态强度 = 0，分支被 GPU 剔除。
    vec3 ambient = vec3(0.0);
    if (u_env_intensity > 0.0) {
        vec3 kS_amb = fresnel_schlick_roughness(NdotV, F0, roughness);
        vec3 kD_amb = (1.0 - kS_amb) * (1.0 - metallic);

        // 漫反射辐照度
        vec3 irradiance = texture(u_irradiance_map, N).rgb;
        vec3 diffuse_ibl = irradiance * albedo;

        // 镜面预过滤（按 roughness 取 mip）+ BRDF LUT
        vec3 R = reflect(-V, N);
        float lod = roughness * u_prefilter_max_lod;
        vec3 prefiltered = textureLod(u_prefilter_map, R, lod).rgb;
        vec2 brdf = texture(u_brdf_lut, vec2(NdotV, roughness)).rg;
        vec3 specular_ibl = prefiltered * (kS_amb * brdf.x + brdf.y);

        ambient = (kD_amb * diffuse_ibl + specular_ibl) * u_env_intensity;
        // AO 压暗间接光（仅环境，不压直接光）
        ambient *= ao;
    }

    // ── 全局环境光常量（ambient 类型灯贡献）──
    // 与 IBL 环境光叠加：u_ambient_color 已含强度，u_ambient_intensity 是 UI 倍率。
    // 不受 AO 压暗（环境光是均匀填充，AO 仅作用于几何相关的间接光）。
    ambient += u_ambient_color * u_ambient_intensity * albedo;

    // ── 自发光（emissive）──
    vec3 emissive = u_emissive;
    if (u_has_emissive_map == 1) {
        vec3 em_tex = texture(u_emissive_map, v_uv).rgb;
        emissive = pow(em_tex, vec3(2.2));  // sRGB→线性
    }
    emissive *= u_emissive_intensity;

    vec3 color = ambient + Lo + emissive;
    // P3 曝光：最终输出乘 pow(2, exposure)（EV 值，+1=亮一档）
    color *= pow(2.0, u_exposure);
    fragColor = vec4(color, 1.0);
}
"""


# 预览 readback 专用：scene-linear RGBA → sRGB OETF + BGR 重排 + 垂直翻转，
# 一步在 GPU 完成（RGBA8 目标，uint8 读回）。替代时间线 comp clip 预览的
# "f32 全帧读回 + numpy np.power sRGB + flipud + cvtColor" CPU 链（1080p
# 实测 200ms+/帧）。export 保持 linear f32 readback 不变。
_READBACK_SRGB_BGR_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_uv;
out vec4 fragColor;

vec3 linear_to_srgb(vec3 lin) {
    lin = clamp(lin, 0.0, 1.0);
    vec3 lo = lin * 12.92;
    vec3 hi = 1.055 * pow(lin, vec3(1.0 / 2.4)) - 0.055;
    return mix(lo, hi, step(vec3(0.0031308), lin));
}

void main() {
    // 垂直翻转：GL 纹理 bottom-left 原点 → 图像 top-left 原点
    vec2 uv = vec2(v_uv.x, 1.0 - v_uv.y);
    vec3 c = texture(u_texture, uv).rgb;
    vec3 srgb = linear_to_srgb(c);
    // BGR 重排：消费方（Edit 预览合成器）期望 BGR 字节序
    fragColor = vec4(srgb.b, srgb.g, srgb.r, 1.0);
}
"""


# ── A4: Lens Distortion (Brown-Conrady, OpenCV convention) ─────────────────
_LENS_DISTORTION_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform int u_mode;         // 0 = undistort (iterative inverse), 1 = distort (closed form)
uniform float u_k1;
uniform float u_k2;
uniform float u_k3;
uniform float u_p1;
uniform float u_p2;
uniform vec2 u_center;      // normalized principal point
uniform vec2 u_resolution;  // frame size in pixels
uniform int u_edge_mode;    // 0 = black, 1 = clamp, 2 = mirror
in vec2 v_uv;
out vec4 fragColor;

vec2 distortion_delta(vec2 p) {
    float r2 = dot(p, p);
    float radial = 1.0 + u_k1 * r2 + u_k2 * r2 * r2 + u_k3 * r2 * r2 * r2;
    return vec2(
        p.x * (radial - 1.0) + 2.0 * u_p1 * p.x * p.y + u_p2 * (r2 + 2.0 * p.x * p.x),
        p.y * (radial - 1.0) + u_p1 * (r2 + 2.0 * p.y * p.y) + 2.0 * u_p2 * p.x * p.y);
}

vec4 sample_edges(vec2 uv) {
    if (u_edge_mode == 1) {
        return texture(u_texture, clamp(uv, vec2(0.0), vec2(1.0)));
    }
    if (u_edge_mode == 2) {
        // Mirror: triangle wave — uv in [0,1] stays identity, outside
        // reflects back toward the edge (abs(mod(uv,2)-1) would flip the
        // whole in-frame image).
        return texture(u_texture, 1.0 - abs(mod(uv, 2.0) - 1.0));
    }
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        return vec4(0.0, 0.0, 0.0, 0.0);
    }
    return texture(u_texture, uv);
}

void main() {
    // image-space pixel coords (y down), matching the CPU model helper
    vec2 px = vec2(v_uv.x, 1.0 - v_uv.y) * u_resolution;
    vec2 c = u_center * u_resolution;
    vec2 p = (px - c) / u_resolution.y;

    vec2 q = p;
    if (u_mode == 0) {
        // undistort (REMOVE): output pixel samples the input at the FORWARD
        // Brown-Conrady position — the map cv2.initUndistortRectifyMap builds.
        q = p + distortion_delta(p);
    } else {
        // distort (ADD): inverse via radial-divide + tangential-subtract
        // (constant iteration bound).
        for (int i = 0; i < 12; ++i) {
            float r2 = dot(q, q);
            float radial = 1.0 + u_k1 * r2 + u_k2 * r2 * r2 + u_k3 * r2 * r2 * r2;
            radial = max(abs(radial), 1e-9) * sign(radial + 1e-18);
            vec2 t = vec2(
                2.0 * u_p1 * q.x * q.y + u_p2 * (r2 + 2.0 * q.x * q.x),
                u_p1 * (r2 + 2.0 * q.y * q.y) + 2.0 * u_p2 * q.x * q.y);
            q = (p - t) / radial;
        }
    }

    vec2 src_px = q * u_resolution.y + c;
    vec2 src_uv = vec2(src_px.x / u_resolution.x, 1.0 - src_px.y / u_resolution.y);
    fragColor = sample_edges(src_uv);
}
"""


# ── A5: Transform motion blur accumulation ─────────────────────────────────
_MOTION_BLUR_ACCUM_FRAGMENT = """
#version 330
uniform sampler2D u_accum;   // accumulated so far
uniform sampler2D u_sample;  // current sub-frame sample
uniform float u_weight;      // weight of the NEW sample: 1/(i+1)
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 prev = texture(u_accum, v_uv);
    vec4 cur = texture(u_sample, v_uv);
    fragColor = mix(prev, cur, clamp(u_weight, 0.0, 1.0));
}
"""

_MESH_WARP_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_offsets;  // (g+1)x(g+1) RG32F; texel (col, row_img) = grid point offset in px (image space, y down)
uniform int u_grid;           // cells per axis (points per axis = grid+1)
uniform vec2 u_resolution;    // frame size in pixels
uniform int u_edge_mode;      // 0 = black, 1 = clamp, 2 = mirror
in vec2 v_uv;
out vec4 fragColor;

vec4 sample_edges(vec2 uv) {
    if (u_edge_mode == 1) {
        return texture(u_texture, clamp(uv, vec2(0.0), vec2(1.0)));
    }
    if (u_edge_mode == 2) {
        return texture(u_texture, 1.0 - abs(mod(uv, 2.0) - 1.0));
    }
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        return vec4(0.0, 0.0, 0.0, 0.0);
    }
    return texture(u_texture, uv);
}

void main() {
    // Image-space pixel coords (y down), matching the CPU helper.
    vec2 px = vec2(v_uv.x, 1.0 - v_uv.y) * u_resolution;
    vec2 gp = px / u_resolution * float(u_grid);
    ivec2 c = ivec2(min(gp, vec2(float(u_grid) - 1.0)));
    vec2 fr = gp - vec2(c);
    ivec2 hi = ivec2(min(c.x + 1, u_grid), min(c.y + 1, u_grid));
    vec2 o00 = texelFetch(u_offsets, ivec2(c.x, c.y), 0).xy;
    vec2 o10 = texelFetch(u_offsets, ivec2(hi.x, c.y), 0).xy;
    vec2 o01 = texelFetch(u_offsets, ivec2(c.x, hi.y), 0).xy;
    vec2 o11 = texelFetch(u_offsets, ivec2(hi.x, hi.y), 0).xy;
    // Bilinear blend of the four corner displacements (px, image space).
    vec2 d = mix(mix(o00, o10, fr.x), mix(o01, o11, fr.x), fr.y);
    vec2 src_px = px - d;
    vec2 src_uv = vec2(src_px.x / u_resolution.x,
                       1.0 - src_px.y / u_resolution.y);
    fragColor = sample_edges(src_uv);
}
"""

# ── A1 Relight (AOV re-lighting, Lambert MVP) ──────────────────────────────
# Inputs: u_texture = beauty/albedo (unit 0), u_normal_tex = world NORMAL
# encoded to [0,1] (unit 1), u_position_tex = world POSITION HDR (unit 2).
# The light math runs in the linear domain; the result multiplies the input
# RGB and preserves its alpha.  Without a position texture the point light
# treats u_light_position as a direction from the world origin.
_RELIGHT_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_normal_tex;
uniform sampler2D u_position_tex;
uniform bool u_has_position;
uniform int u_light_type;        // 0 = point, 1 = directional
uniform vec3 u_light_position;
uniform vec3 u_light_direction;
uniform float u_intensity;
uniform vec3 u_light_color;
uniform float u_ambient;
uniform int u_normal_decode;     // 0 = encoded [0,1], 1 = signed
uniform bool u_flip_y;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 base = texture(u_texture, v_uv);
    vec3 n = texture(u_normal_tex, v_uv).xyz;
    if (u_normal_decode == 0) {
        n = n * 2.0 - 1.0;
    }
    if (u_flip_y) {
        n.y = -n.y;
    }
    float n_len = length(n);
    n = n_len > 0.0001 ? n / n_len : vec3(0.0, 0.0, 1.0);

    vec3 L;
    if (u_light_type == 0) {
        if (u_has_position) {
            vec3 P = texture(u_position_tex, v_uv).xyz;
            L = u_light_position - P;
        } else {
            L = u_light_position;
        }
    } else {
        L = -u_light_direction;
    }
    float l_len = length(L);
    L = l_len > 0.0001 ? L / l_len : vec3(0.0, 0.0, 1.0);

    float diffuse = max(dot(n, L), 0.0);
    // Lambert MVP — specular/roughness uniforms are reserved for phase 2.
    vec3 lit = base.rgb * (diffuse * u_intensity * u_light_color
                           + vec3(u_ambient));
    fragColor = vec4(lit, base.a);
}
"""

# ── A2 Cryptomatte Select (ID → matte) ─────────────────────────────────────
# Matches a selection set of object ids against the ID texture.  Two
# encodings: rgba24 (this project's allocator id split across RGB/255) and
# float_r (bit-preserving float32 id in the red channel).  u_selected holds
# up to MAX_SELECTED ids; u_output_mode 0 = matte, 1 = input isolated by it.
_CRYPTOMATTE_SELECT_FRAGMENT = """
#version 330
#define MAX_SELECTED 32
uniform sampler2D u_texture;
uniform float u_selected[MAX_SELECTED];
uniform int u_count;
uniform int u_id_encoding;   // 0 = rgba24, 1 = float_r
uniform int u_output_mode;   // 0 = matte, 1 = isolate
in vec2 v_uv;
out vec4 fragColor;

// N-09：float_r 编码的 ID 是 32 位 hash 的位模式，不是连续测量值。
// 位相等（floatBitsToUint）才是正确谓词——绝对 epsilon 会把大量不同
// tiny-hash ID 连同背景 0 一起误选（1e-6 时整段 tiny 区间全命中）。
bool matches_bits(float id) {
    uint id_bits = floatBitsToUint(id);
    for (int i = 0; i < MAX_SELECTED; i++) {
        if (i >= u_count) {
            break;
        }
        if (floatBitsToUint(u_selected[i]) == id_bits) {
            return true;
        }
    }
    return false;
}

// rgba24 编码是精确整数（<2^24 在 float32 中可精确表示），保留 0.5
// 的整数容差比较。
bool matches_int(float id) {
    for (int i = 0; i < MAX_SELECTED; i++) {
        if (i >= u_count) {
            break;
        }
        if (abs(id - u_selected[i]) < 0.5) {
            return true;
        }
    }
    return false;
}

void main() {
    vec4 sample_value = texture(u_texture, v_uv);
    float coverage = 0.0;
    if (u_id_encoding == 0) {
        float id = floor(sample_value.r * 255.0 + 0.5)
                 + floor(sample_value.g * 255.0 + 0.5) * 256.0
                 + floor(sample_value.b * 255.0 + 0.5) * 65536.0;
        coverage = matches_int(id) ? 1.0 : 0.0;
    } else {
        coverage = matches_bits(sample_value.r) ? 1.0 : 0.0;
    }
    if (u_output_mode == 0) {
        fragColor = vec4(coverage, coverage, coverage, coverage);
    } else {
        // Isolate: premultiplied output — alpha carries the matte too,
        // otherwise unselected areas keep a foggy semi-transparent edge.
        fragColor = vec4(sample_value.rgb * coverage,
                         sample_value.a * coverage);
    }
}
"""

# ── A3 Depth tools (ZBlur / Fog / DepthMatte) ──────────────────────────────
# All three sample the depth (red channel) via the shared DEPTH_INPUT_GLSL
# prelude: u_texture unit 0, u_depth unit 1, world mode linearizes with
# u_near/u_far.  ZBlur gathers a Vogel-spiral disc scaled by the
# per-pixel circle of confusion (const loop bound, u_sample_count clamp).
# Shared depth prelude (kept inline: core must not import nodes).
_DEPTH_INPUT_GLSL = """
in vec2 v_uv;
uniform sampler2D u_texture;
uniform sampler2D u_depth;
uniform int u_depth_units;   // 0 = normalized, 1 = world
uniform float u_near;
uniform float u_far;

float depth01() {
    float z = texture(u_depth, v_uv).r;
    if (u_depth_units == 1) {
        return clamp((z - u_near) / max(u_far - u_near, 0.00001),
                     0.0, 1.0);
    }
    return clamp(z, 0.0, 1.0);
}
"""

_ZBLUR_FRAGMENT = """
#version 330
""" + _DEPTH_INPUT_GLSL + """
uniform float u_focus_distance;
uniform float u_focal_range;
uniform float u_max_radius;
uniform int u_sample_count;
uniform bool u_bokeh;
out vec4 fragColor;

const float GOLDEN_ANGLE = 2.39996323;
const int MAX_SAMPLES = 32;

void main() {
    vec4 center = texture(u_texture, v_uv);
    float z = depth01();
    float coc = abs(z - clamp(u_focus_distance, 0.0, 1.0))
              / max(u_focal_range, 0.001);
    coc = smoothstep(0.0, 1.0, coc);
    float radius = u_max_radius * coc;
    if (radius < 0.5) {
        fragColor = center;
        return;
    }
    vec2 texel = 1.0 / max(vec2(textureSize(u_texture, 0)), vec2(1.0));
    vec3 sum = center.rgb;
    float weight_sum = 1.0;
    for (int i = 0; i < MAX_SAMPLES; i++) {
        if (i >= u_sample_count) {
            break;
        }
        float angle = float(i) * GOLDEN_ANGLE;
        float rr = sqrt((float(i) + 0.5) / float(u_sample_count));
        // bokeh: uniform disc weights (hard aperture).  Otherwise a
        // gaussian falloff over the normalised radius softens the disc
        // into a conventional gaussian blur.
        float weight = u_bokeh ? 1.0
                               : exp(-(rr * rr) / (2.0 * 0.4 * 0.4));
        vec2 offset = vec2(cos(angle), sin(angle)) * rr * radius * texel;
        sum += texture(u_texture, v_uv + offset).rgb * weight;
        weight_sum += weight;
    }
    fragColor = vec4(sum / weight_sum, center.a);
}
"""

_FOG_FRAGMENT = """
#version 330
""" + _DEPTH_INPUT_GLSL + """
uniform vec3 u_fog_color;
uniform float u_density;
uniform float u_start;
uniform float u_end;
out vec4 fragColor;

void main() {
    vec4 center = texture(u_texture, v_uv);
    float z = depth01();
    float t = clamp((z - u_start) / max(u_end - u_start, 0.0001),
                    0.0, 1.0);
    float fog_amount = 1.0 - exp(-u_density * t);
    fragColor = vec4(mix(center.rgb, u_fog_color, fog_amount), center.a);
}
"""

_DEPTH_MATTE_FRAGMENT = """
#version 330
""" + _DEPTH_INPUT_GLSL + """
uniform float u_range_start;
uniform float u_range_end;
uniform float u_feather;
out vec4 fragColor;

void main() {
    float z = depth01();
    float f = max(u_feather, 0.0001);
    float m = smoothstep(u_range_start - f, u_range_start + f, z)
            * (1.0 - smoothstep(u_range_end - f, u_range_end + f, z));
    fragColor = vec4(m, m, m, m);
}
"""

# ── B3 God Rays (screen-space volumetric light shafts) ─────────────────────
# Radial march toward u_light_pos (uv) accumulating alpha occlusion with
# decay^i falloff; const loop bound with u_sample_count clamp (GLSL rule).
_GODRAYS_FRAGMENT = """
#version 330
#define MAX_GODRAY_SAMPLES 64
uniform sampler2D u_texture;
uniform sampler2D u_mask;
uniform bool u_use_mask;
uniform vec2 u_light_pos;
uniform float u_density;
uniform float u_weight;
uniform float u_decay;
uniform int u_sample_count;
uniform vec3 u_light_color;
in vec2 v_uv;
out vec4 fragColor;

float occlusion(vec2 uv) {
    vec4 s = u_use_mask ? texture(u_mask, uv)
                        : texture(u_texture, uv);
    return s.a;
}

void main() {
    vec4 center = texture(u_texture, v_uv);
    int count = u_sample_count;
    vec2 delta = (u_light_pos - v_uv) / float(count);
    vec2 uv = v_uv;
    float falloff = 1.0;
    float rays = 0.0;
    float weight_sum = 0.0;
    for (int i = 0; i < MAX_GODRAY_SAMPLES; i++) {
        if (i >= count) {
            break;
        }
        uv += delta;
        rays += occlusion(uv) * falloff;
        weight_sum += falloff;
        falloff *= u_decay;
    }
    rays = weight_sum > 0.0 ? rays / weight_sum : 0.0;
    vec3 lit = center.rgb + rays * u_weight * u_density * u_light_color;
    fragColor = vec4(lit, center.a);
}
"""

# ── B4 Lens kit (chromatic aberration / vignette / lens flare) ─────────────
_CHROMATIC_ABERRATION_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_offset_r;
uniform float u_offset_b;
uniform vec2 u_center;
uniform float u_edge_boost;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec2 aspect = vec2(u_resolution.x / max(u_resolution.y, 1.0), 1.0);
    vec2 delta = (v_uv - u_center) * aspect;
    float dist = length(delta);
    float edge_scale = mix(1.0, clamp(dist * 1.4142, 0.0, 1.0),
                           u_edge_boost);
    vec2 dir = dist > 0.0001 ? delta / dist : vec2(0.0);
    vec2 texel = 1.0 / max(u_resolution, vec2(1.0));
    vec2 shift_r = dir * u_offset_r * edge_scale * texel;
    vec2 shift_b = dir * u_offset_b * edge_scale * texel;
    float r = texture(u_texture, v_uv - shift_r).r;
    float g = texture(u_texture, v_uv).g;
    float b = texture(u_texture, v_uv - shift_b).b;
    float a = texture(u_texture, v_uv).a;
    fragColor = vec4(r, g, b, a);
}
"""

_VIGNETTE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_amount;
uniform float u_radius;
uniform float u_softness;
uniform float u_roundness;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 center = texture(u_texture, v_uv);
    vec2 aspect = vec2(u_resolution.x / max(u_resolution.y, 1.0), 1.0);
    vec2 p = (v_uv - vec2(0.5)) * aspect;
    float n = 2.0 + 6.0 * clamp(u_roundness, 0.0, 1.0);
    float d = pow(pow(abs(p.x), n) + pow(abs(p.y), n), 1.0 / n);
    // Mirror vignette_node.vignette_factor: softness is clamped to radius
    // so smoothstep edge0 < edge1 always holds.
    float softness = clamp(u_softness, 0.001, max(u_radius, 0.001));
    float t = clamp((d - max(u_radius - softness, 0.0)) / softness,
                    0.0, 1.0);
    float smooth_t = t * t * (3.0 - 2.0 * t);
    float factor = 1.0 - clamp(u_amount, 0.0, 1.0) * smooth_t;
    fragColor = vec4(center.rgb * factor, center.a);
}
"""

_LENS_FLARE_FRAGMENT = """
#version 330
#define MAX_FLARE_GHOSTS 8
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform vec2 u_light_pos;
uniform vec3 u_light_color;
uniform float u_intensity;
uniform int u_ghost_count;
uniform float u_ghost_spacing;
uniform float u_ghost_decay;
uniform float u_ghost_size;
uniform float u_halo_radius;
uniform float u_halo_strength;
uniform float u_streak_strength;
uniform float u_streak_width;
uniform bool u_use_occlusion;
uniform int u_blend_mode;    // 0 = screen, 1 = add
in vec2 v_uv;
out vec4 fragColor;

float ghost_glow(vec2 p, vec2 c, float size) {
    // p and c are already in aspect-corrected space; do not re-scale here.
    float d = length(p - c);
    return exp(-d * d / max(size * size, 1e-6));
}

void main() {
    vec4 center = texture(u_texture, v_uv);
    float occlusion = 1.0;
    if (u_use_occlusion) {
        occlusion = texture(u_texture, clamp(u_light_pos, vec2(0.0),
                                             vec2(1.0))).a;
    }
    vec2 aspect = vec2(u_resolution.x / max(u_resolution.y, 1.0), 1.0);
    vec3 flare = vec3(0.0);

    // Ghost chain along the centre<->light axis (mirrored positions).
    vec2 centre_axis = (vec2(0.5) - u_light_pos) * aspect;
    float falloff = u_ghost_decay;
    for (int i = 1; i <= MAX_FLARE_GHOSTS; i++) {
        if (i > u_ghost_count) {
            break;
        }
        vec2 gp = u_light_pos * aspect
                + centre_axis * (u_ghost_spacing * float(i) + 1.0);
        flare += vec3(1.0) * ghost_glow(v_uv * aspect, gp, u_ghost_size)
               * falloff * 0.25;
        falloff *= u_ghost_decay;
    }

    // Halo ring around the light.
    float d_light = length((v_uv - u_light_pos) * aspect);
    float halo = exp(-pow((d_light - u_halo_radius) / max(u_halo_radius
                     * 0.12, 1e-4), 2.0));
    flare += vec3(halo) * u_halo_strength;

    // Horizontal anamorphic streak through the light.
    vec2 rel = (v_uv - u_light_pos) * aspect;
    float streak = exp(-abs(rel.y) / max(u_streak_width, 1e-5))
                 * exp(-abs(rel.x) * 3.0);
    flare += vec3(streak) * u_streak_strength;

    vec3 contribution = flare * u_light_color * u_intensity * occlusion;
    vec3 result;
    if (u_blend_mode == 1) {
        result = center.rgb + contribution;
    } else {
        result = 1.0 - (1.0 - clamp(center.rgb, 0.0, 1.0))
                     * (1.0 - clamp(contribution, 0.0, 1.0));
    }
    fragColor = vec4(result, center.a);
}
"""

# ── CP-002 节点插件效果 shader ──────────────────────────────────────────────
# plugins/node_plugins/{posterize,vintage}_plugin.py 通过 _effect_type marker
# 分发到这里。此前插件 import 失效导致这些 effect 从未可达；import 修复后
# 补齐 GPU 端，避免“可加载但无效果”。

_POSTERIZE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_levels;       // 2..64
uniform int u_channel_mode;   // 0 = rgb, 1 = rgba, 2 = luminance
uniform float u_dither;       // 0/1 白噪声抖动去 banding
uniform vec2 u_resolution;
in vec2 v_uv;
out vec4 fragColor;

float posterize_hash(vec2 p) {
    p = fract(p * vec2(0.1031, 0.1031));
    p += dot(p, p.yx + 33.33);
    return fract((p.x + p.y) * p.y);
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    float levels = max(u_levels, 2.0);
    float step_size = 1.0 / levels;
    vec4 result = c;
    if (u_dither > 0.5) {
        float n = (posterize_hash(v_uv * u_resolution) - 0.5) * step_size;
        result += vec4(n, n, n, 0.0);
    }
    if (u_channel_mode == 1) {
        result = floor(result * levels + 0.5) / levels;
    } else if (u_channel_mode == 2) {
        float l = dot(result.rgb, vec3(0.2126, 0.7152, 0.0722));
        float lq = floor(l * levels + 0.5) / levels;
        if (l > 1e-6) {
            result.rgb *= lq / l;
        } else {
            result.rgb = vec3(lq);
        }
    } else {
        result.rgb = floor(result.rgb * levels + 0.5) / levels;
    }
    fragColor = result;
}
"""

_VINTAGE_TONE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform float u_intensity;    // 0..1 原图↔效果混合
uniform float u_warm_shift;   // -1..1 额外暖色偏移
uniform float u_contrast;     // -0.5..0.5
uniform float u_saturation;   // -1..1
uniform int u_film_type;      // 0 kodak_portra / 1 fuji_pro / 2 agfa_optima / 3 ilford_hp5
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_texture, v_uv);
    vec3 col = c.rgb;

    // 场景线性空间：以 0.18 中灰为锚做对比度
    vec3 anchor = vec3(0.18);
    col = anchor + (col - anchor) * (1.0 + u_contrast);

    // 饱和度（保持 luma）
    float luma = dot(col, vec3(0.2126, 0.7152, 0.0722));
    col = mix(vec3(luma), col, 1.0 + u_saturation);

    // 各胶片乳剂的色调倾向（乘性 bias，HDR 不截断）
    vec3 bias;
    if (u_film_type == 0) {
        bias = vec3(1.04, 1.0, 0.92);        // kodak_portra: 暖、阴影抬升
    } else if (u_film_type == 1) {
        bias = vec3(0.96, 1.03, 1.02);       // fuji_pro: 冷绿
    } else if (u_film_type == 2) {
        bias = vec3(1.03, 0.97, 1.01);       // agfa_optima: 品红
    } else {
        bias = vec3(1.02, 1.0, 0.97);        // ilford_hp5: 去饱和暖调
    }
    bias *= vec3(1.0 + u_warm_shift * 0.5, 1.0, 1.0 - u_warm_shift * 0.5);
    col *= bias;

    fragColor = vec4(mix(c.rgb, col, clamp(u_intensity, 0.0, 1.0)), c.a);
}
"""

_FILM_GRAIN_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec2 u_resolution;
uniform float u_grain_size;       // 0.5..4.0 颗粒尺寸（越大越粗）
uniform float u_intensity;        // 0..1
uniform int u_grain_type;         // 0 fine / 1 medium / 2 coarse
uniform float u_color_grain;      // 0/1 彩色颗粒
uniform float u_luminance_only;   // 0/1 只作用于亮度
uniform float u_seed;             // 颗粒种子（逐帧变化即得动态颗粒）
in vec2 v_uv;
out vec4 fragColor;

float grain_hash(vec2 p, float seed) {
    p = fract(p * vec2(0.1031, 0.1031) + vec2(seed * 0.001, seed * 0.923));
    p += dot(p, p.yx + 33.33);
    return fract((p.x + p.y) * p.y);
}

float grain_value_noise(vec2 p, float seed) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    float a = grain_hash(i, seed);
    float b = grain_hash(i + vec2(1.0, 0.0), seed);
    float c = grain_hash(i + vec2(0.0, 1.0), seed);
    float d = grain_hash(i + vec2(1.0, 1.0), seed);
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y) * 2.0 - 1.0;
}

void main() {
    vec4 tex = texture(u_texture, v_uv);

    // 分辨率无关的颗粒频率：以 1920 宽为基准
    float px_scale = u_resolution.x / 1920.0;
    float density = 1.2;
    if (u_grain_type == 0) {
        density = 2.2;
    } else if (u_grain_type == 2) {
        density = 0.6;
    }
    float scale = max(px_scale * density / max(u_grain_size, 0.1), 0.05);

    vec2 p = v_uv * u_resolution * scale;
    vec3 grain3;
    if (u_color_grain > 0.5) {
        grain3 = vec3(
            grain_value_noise(p, u_seed),
            grain_value_noise(p + 37.0, u_seed + 1.0),
            grain_value_noise(p + 91.0, u_seed + 2.0));
    } else {
        float n = grain_value_noise(p, u_seed);
        grain3 = vec3(n);
    }

    // 场景线性空间：乘性调制对小值友好、大值不过曝
    vec3 result = tex.rgb * (1.0 + grain3 * clamp(u_intensity, 0.0, 1.0) * 0.5);

    if (u_luminance_only > 0.5) {
        float l0 = dot(tex.rgb, vec3(0.2126, 0.7152, 0.0722));
        float l1 = dot(result, vec3(0.2126, 0.7152, 0.0722));
        result = tex.rgb + vec3(l1 - l0);
    }
    fragColor = vec4(result, tex.a);
}
"""

# ── CP-044/045 通用 mask 门控 + mix 混合 ────────────────────────────────────
# 任何带通用 mask 端口的效果节点，其结果经此单 pass 完成：
#   out = mix(base, effect, mask_value * mix)
# base = 效果前输入（mix 混合参照），mask 缺省时 factor = mix。
_GENERIC_MASK_MIX_FRAGMENT = """
#version 330
uniform sampler2D u_texture;    // effect output
uniform sampler2D u_input;      // pre-effect input (mix reference)
uniform sampler2D u_mask;       // mask texture (dummy 1x1 white when absent)
uniform int u_mask_channel;     // 0 luma, 1 R, 2 G, 3 B, 4 A
uniform float u_mask_invert;    // 0/1
uniform float u_mix;            // 0..1 effect blend
uniform float u_has_input;      // 0/1 mix reference available
uniform float u_mask_enabled;   // 0/1 mask connected (0: gate fully open)
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 eff = texture(u_texture, v_uv);
    vec4 base = eff;
    if (u_has_input > 0.5) {
        base = texture(u_input, v_uv);
    }
    float m = 1.0;
    if (u_mask_enabled > 0.5) {
    vec4 mv = texture(u_mask, v_uv);
    if (u_mask_channel == 1) {
        m = mv.r;
    } else if (u_mask_channel == 2) {
        m = mv.g;
    } else if (u_mask_channel == 3) {
        m = mv.b;
    } else if (u_mask_channel == 4) {
        m = mv.a;
    } else {
        m = dot(mv.rgb, vec3(0.2126, 0.7152, 0.0722));
    }
    if (u_mask_invert > 0.5) {
        m = 1.0 - m;
    }
    }
    float t = clamp(clamp(m, 0.0, 1.0) * clamp(u_mix, 0.0, 1.0), 0.0, 1.0);
    fragColor = mix(base, eff, t);
}
"""

# ── CP-011 Keymix：mask 驱动的 A/B 三输入线性混合 ──────────────────────────
# premultiplied-linear 域：out = B + (A - B) * m（RGB 与 alpha 同式）。
_KEYMIX_FRAGMENT = """
#version 330
uniform sampler2D u_a;
uniform sampler2D u_b;
uniform sampler2D u_m;
uniform int u_mask_channel;   // 0 luma, 1 R, 2 G, 3 B, 4 A
uniform float u_mask_invert;  // 0/1
uniform float u_mix;          // 0..1
uniform float u_mask_valid;   // 0/1（无 mask 输入时门全开）
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 a = texture(u_a, v_uv);
    vec4 b = texture(u_b, v_uv);
    float m = 1.0;
    if (u_mask_valid > 0.5) {
        vec4 mv = texture(u_m, v_uv);
        if (u_mask_channel == 1) {
            m = mv.r;
        } else if (u_mask_channel == 2) {
            m = mv.g;
        } else if (u_mask_channel == 3) {
            m = mv.b;
        } else if (u_mask_channel == 4) {
            m = mv.a;
        } else {
            m = dot(mv.rgb, vec3(0.2126, 0.7152, 0.0722));
        }
        if (u_mask_invert > 0.5) {
            m = 1.0 - m;
        }
    }
    float t = clamp(clamp(m, 0.0, 1.0) * clamp(u_mix, 0.0, 1.0), 0.0, 1.0);
    fragColor = b + (a - b) * t;
}
"""

# ── CP-019 DeGrain 组合 pass：颗粒门控的平滑替换 ───────────────────────────
# pass1（复用 denoise 双边滤波）产出平滑估计；本 pass 只在 |orig-smooth|
# 小于颗粒阈值处替换为平滑值（smoothstep 软门），大偏差的真实细节直通。
_DEGRAIN_COMBINE_FRAGMENT = """
#version 330
uniform sampler2D u_texture;    // original
uniform sampler2D u_smooth;     // bilateral-smoothed estimate
uniform float u_threshold;      // grain deviation threshold
uniform float u_amount;         // 0..1
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 orig = texture(u_texture, v_uv);
    vec4 smooth_tex = texture(u_smooth, v_uv);
    vec3 diff = abs(orig.rgb - smooth_tex.rgb);
    float d = max(diff.r, max(diff.g, diff.b));
    // 阈值一半以下完全替换，阈值以上直通（软门）
    float weight = 1.0 - smoothstep(u_threshold * 0.5, u_threshold, d);
    weight *= clamp(u_amount, 0.0, 1.0);
    fragColor = vec4(mix(orig.rgb, smooth_tex.rgb, weight), orig.a);
}
"""

# ── CP-014 ColorLookup：主/R/G/B 曲线查表 ──────────────────────────────────
# LUT 纹理为 256×4 RGBA32F：行 0=R、1=G、2=B、3=master（各取 .r）。
# 每通道 out = master(lut_ch(value))；域外按 LUT 端点值保持（Nuke 行为）。
_COLORLOOKUP_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_lut;        // 256 x 4 (r/g/b/master rows)
uniform float u_input_min;
uniform float u_input_max;
in vec2 v_uv;
out vec4 fragColor;

float lut_row(float v, float row) {
    float dom = max(u_input_max - u_input_min, 1e-6);
    float c = clamp((v - u_input_min) / dom, 0.0, 1.0);
    return texture(u_lut, vec2(c, (row + 0.5) / 4.0)).r;
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    float r = lut_row(lut_row(c.r, 0.0), 3.0);
    float g = lut_row(lut_row(c.g, 1.0), 3.0);
    float b = lut_row(lut_row(c.b, 2.0), 3.0);
    fragColor = vec4(r, g, b, c.a);
}
"""

# ── CP-015 ASC CDL：slope/power/offset + Rec.709 luma 饱和度 ────────────────
# pow 的底数取 max(v, 0)（负值在 CDL 语义下钳到 0）；HDR 上不封顶。
_CDL_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
uniform vec3 u_slope;
uniform vec3 u_offset;
uniform vec3 u_power;
uniform float u_saturation;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_texture, v_uv);
    vec3 outc;
    outc.r = pow(max(c.r * u_slope.r + u_offset.r, 0.0), u_power.r);
    outc.g = pow(max(c.g * u_slope.g + u_offset.g, 0.0), u_power.g);
    outc.b = pow(max(c.b * u_slope.b + u_offset.b, 0.0), u_power.b);
    float luma = dot(outc, vec3(0.2126, 0.7152, 0.0722));
    outc = mix(vec3(luma), outc, u_saturation);
    fragColor = vec4(outc, c.a);
}
"""

# ═══════════════════════════════════════════════════════════════════════
# LIB1 内容库节点效果（计划 L / L04：C01 gradient_map + C17 edge_extend）
# 语义与暂存区组件（content_staging/library_v1）的 CPU oracle 逐式同构；
# 手算向量见各组件 hand_vectors.json。节点 marker 见 nodes/lib1_nodes.py。
# ═══════════════════════════════════════════════════════════════════════

_LIB1_GRADIENT_MAP_FRAGMENT = """
#version 330
// C01：多停靠亮度映射（F16 共享核的 Composite 入口）。
// 查找坐标端点饱和策略（超白/负值映射端点色，非内容 0-1 裁剪）。
uniform sampler2D u_texture;
uniform vec3 u_stop_0;
uniform vec3 u_stop_1;
uniform vec3 u_stop_2;
uniform vec3 u_stop_3;
uniform float u_pos_1;
uniform float u_pos_2;
uniform int u_interp;    // 0=Linear, 1=Smooth
uniform float u_mix;     // Amount
in vec2 v_uv;
out vec4 fragColor;

const vec3 LIB1_GM_LUMA_W = vec3(0.2126, 0.7152, 0.0722);

float lib1_gm_shape(float t) {
    if (u_interp == 1) { return t * t * (3.0 - 2.0 * t); }
    return t;
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_mix < 0.001) { fragColor = c; return; }
    float t = clamp(dot(c.rgb, LIB1_GM_LUMA_W), 0.0, 1.0);
    vec3 mapped;
    if (t <= u_pos_1) {
        float s = lib1_gm_shape(clamp(t / max(u_pos_1, 1e-4), 0.0, 1.0));
        mapped = mix(u_stop_0, u_stop_1, s);
    } else if (t <= u_pos_2) {
        float s = lib1_gm_shape(clamp((t - u_pos_1) / max(u_pos_2 - u_pos_1, 1e-4), 0.0, 1.0));
        mapped = mix(u_stop_1, u_stop_2, s);
    } else {
        float s = lib1_gm_shape(clamp((t - u_pos_2) / max(1.0 - u_pos_2, 1e-4), 0.0, 1.0));
        mapped = mix(u_stop_2, u_stop_3, s);
    }
    fragColor = vec4(mix(c.rgb, mapped, u_mix), c.a);
}
"""

_LIB1_EDGE_EXTEND_DILATE_FRAGMENT = """
#version 330
// C17：一轮 RGB 扩张 pass（乒乓执行 radius 轮，evaluator 调度）。
// 约定：u_texture 的 rgb=当前延伸态、a=solid 标志(0/1)；
// 首轮 u_first=1 时把原始 alpha 按 u_threshold 阈值化为 solid 标志。
// clamp-to-edge 边界与 CPU 核的索引钳制同构（界外邻居=自身，不贡献）。
uniform sampler2D u_texture;
uniform float u_threshold;
uniform int u_first;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 src = texture(u_texture, v_uv);
    float solid = src.a;
    if (u_first == 1) {
        solid = step(u_threshold, src.a);
    }
    if (solid >= 0.5) {
        fragColor = vec4(src.rgb, 1.0);
        return;
    }
    vec2 ts = vec2(textureSize(u_texture, 0));
    vec2 texel = 1.0 / ts;
    vec3 sum = vec3(0.0);
    float cnt = 0.0;
    vec4 n1 = texture(u_texture, v_uv + vec2(-texel.x, 0.0));
    vec4 n2 = texture(u_texture, v_uv + vec2( texel.x, 0.0));
    vec4 n3 = texture(u_texture, v_uv + vec2(0.0, -texel.y));
    vec4 n4 = texture(u_texture, v_uv + vec2(0.0,  texel.y));
    if (n1.a >= 0.5) { sum += n1.rgb; cnt += 1.0; }
    if (n2.a >= 0.5) { sum += n2.rgb; cnt += 1.0; }
    if (n3.a >= 0.5) { sum += n3.rgb; cnt += 1.0; }
    if (n4.a >= 0.5) { sum += n4.rgb; cnt += 1.0; }
    if (cnt > 0.5) {
        fragColor = vec4(sum / cnt, 1.0);
    } else {
        fragColor = vec4(src.rgb, 0.0);
    }
}
"""

_LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT = """
#version 330
// C17：输出组装——延伸 rgb + 原 alpha（color_only 保留原 alpha /
// premultiplied = 延伸 rgb × 原 alpha）。
uniform sampler2D u_texture;    // dilate 链结果（a=solid 标志）
uniform sampler2D u_orig_tex;   // 原始 RGBA
uniform int u_output_mode;      // 0=color_only, 1=premultiplied
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec3 rgb = texture(u_texture, v_uv).rgb;
    float a = texture(u_orig_tex, v_uv).a;
    fragColor = (u_output_mode == 1) ? vec4(rgb * a, a) : vec4(rgb, a);
}
"""


_LIB1_RGB_MATRIX_FRAGMENT = """
#version 330
// C02：3x3 通道矩阵 + RGB 偏置 + strength 混合（rgb_matrix_ref 同构）。
// out = rgb + (M·rgb + bias − rgb)·strength；alpha 透传；HDR 无 clamp。
uniform sampler2D u_texture;
uniform float u_m00; uniform float u_m01; uniform float u_m02;
uniform float u_m10; uniform float u_m11; uniform float u_m12;
uniform float u_m20; uniform float u_m21; uniform float u_m22;
uniform vec3 u_bias;
uniform float u_strength;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_texture, v_uv);
    vec3 t = vec3(u_m00 * c.r + u_m01 * c.g + u_m02 * c.b,
                  u_m10 * c.r + u_m11 * c.g + u_m12 * c.b,
                  u_m20 * c.r + u_m21 * c.g + u_m22 * c.b) + u_bias;
    fragColor = vec4(c.rgb + (t - c.rgb) * u_strength, c.a);
}
"""

_LIB1_DESPILL_FRAGMENT = """
#version 330
// C03：绿幕去溢色（despill_ref 同构）。Subtract=削峰（0..limit 封顶）；
// Scale=乘性双向（ratio clamp ±1 守护，深绿不足最多翻倍）。
uniform sampler2D u_texture;
uniform int u_method;    // 0=Subtract, 1=Scale
uniform float u_limit;
uniform float u_strength;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_strength < 0.001) { fragColor = c; return; }
    float m = max(c.r, c.b);
    float delta = c.g - m;
    float g;
    if (u_method == 0) {
        float spill = clamp(delta, 0.0, u_limit);
        g = c.g - u_strength * spill;
    } else {
        float capped = clamp(delta, -u_limit, u_limit);
        float ratio = clamp(capped / max(c.g, 1.0e-6), -1.0, 1.0);
        g = c.g * (1.0 - u_strength * ratio);
    }
    fragColor = vec4(c.r, g, c.b, c.a);
}
"""

_LIB1_SOLARIZE_FRAGMENT = """
#version 330
// C04：太阳化折返（F17 solarize_ref 同构）。fold(v)=mix(v, 2·thr−v,
// smoothstep(thr−h, thr+h, v))；Luma 档亮度折返色度平移 / RGB 逐通道；
// 线性折返无 clamp（HDR 可折出负值）；amount<0.001 精确恒等。
uniform sampler2D u_texture;
uniform float u_threshold;
uniform float u_softness;
uniform int u_mode;      // 0=Luma, 1=RGB
uniform float u_amount;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_amount < 0.001) { fragColor = c; return; }
    float thr = max(u_threshold, 1.0e-4);
    float h = max(u_softness * thr, 1.0e-4);
    vec3 folded;
    if (u_mode == 0) {
        float v = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
        float m = smoothstep(thr - h, thr + h, v);
        float fv = v * (1.0 - m) + (2.0 * thr - v) * m;
        folded = c.rgb + (fv - v);
    } else {
        vec3 m3 = smoothstep(vec3(thr - h), vec3(thr + h), c.rgb);
        folded = c.rgb * (1.0 - m3) + (2.0 * thr - c.rgb) * m3;
    }
    fragColor = vec4(c.rgb + (folded - c.rgb) * u_amount, c.a);
}
"""


_LIB1_MOSAIC_FRAGMENT = """
#version 330
// C05：马赛克块中心采样 + scene-linear 阶梯量化 + Bayer4x4 抖动
// （mosaic_ref/Pixel_Blocks.tglsl 同构）。texelFetch=精确 NEAREST
// （utility LINEAR 滤波下的偶数块均值差异归零）；像素中心 y-down。
uniform sampler2D u_texture;
uniform float u_block;
uniform float u_levels;
uniform float u_dither;   // 0/1
in vec2 v_uv;
out vec4 fragColor;

const int LIB1_PB_BAYER4[16] = int[16](
    0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5);

void main() {
    vec4 c = texture(u_texture, v_uv);
    vec2 size = vec2(textureSize(u_texture, 0));
    vec2 px = vec2(v_uv.x * size.x, v_uv.y * size.y);
    vec2 bid = floor(px / u_block);
    vec2 center = bid * u_block + 0.5 * u_block;
    ivec2 tc = ivec2(clamp(floor(center), vec2(0.0), size - 1.0));
    vec3 sampled = texelFetch(u_texture, tc, 0).rgb;
    float dith = 0.0;
    if (u_dither > 0.5) {
        vec2 inner = min(px - bid * u_block, vec2(3.0));
        ivec2 bxy = ivec2(inner);
        dith = (float(LIB1_PB_BAYER4[bxy.y * 4 + bxy.x]) + 0.5) / 16.0 - 0.5;
    }
    vec3 q = (floor(sampled * u_levels + dith) + 0.5) / u_levels;
    fragColor = vec4(q, c.a);
}
"""

_LIB1_CRT_FRAGMENT = """
#version 330
// C08：CRT 扫描线（三角波平方收窄）+ RGB Grille 荫罩（3px 周期三通道
// 相位互错）；Full=两场乘积。像素中心坐标（y 与 CPU 行序一致）。
uniform sampler2D u_texture;
uniform int u_mode;          // 0=Scanlines, 1=RGB Grille, 2=Full
uniform float u_scan_count;  // 120..1200
uniform float u_scan_strength;
uniform float u_mask_strength;
in vec2 v_uv;
out vec4 fragColor;

float lib1_crt_fract(float x) { return x - floor(x); }
float lib1_crt_tri(float x) { return 1.0 - abs(x * 2.0 - 1.0); }

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_scan_strength < 0.001 && u_mask_strength < 0.001) {
        fragColor = c; return;
    }
    vec2 size = vec2(textureSize(u_texture, 0));
    float px_y = v_uv.y * size.y;
    float px_x = v_uv.x * size.x;
    float gain_r = 1.0, gain_g = 1.0, gain_b = 1.0;
    if (u_mode == 0 || u_mode == 2) {
        float period = max(size.y / max(u_scan_count, 1.0), 1.0);
        float tri = lib1_crt_tri(lib1_crt_fract(px_y / period));
        tri = tri * tri;
        float scan_gain = 1.0 - u_scan_strength * 0.6 * (1.0 - tri);
        gain_r = scan_gain; gain_g = scan_gain; gain_b = scan_gain;
    }
    if (u_mode == 1 || u_mode == 2) {
        float k = u_mask_strength * 0.8;
        float s = px_x / 3.0;
        gain_r *= 1.0 + (lib1_crt_tri(lib1_crt_fract(s + 0.0)) - 0.5) * k;
        gain_g *= 1.0 + (lib1_crt_tri(lib1_crt_fract(s + 0.33333333)) - 0.5) * k;
        gain_b *= 1.0 + (lib1_crt_tri(lib1_crt_fract(s + 0.66666667)) - 0.5) * k;
    }
    fragColor = vec4(c.rgb * vec3(gain_r, gain_g, gain_b), c.a);
}
"""

_LIB1_GLITCH_FRAGMENT = """
#version 330
// C09：条带故障（Dave Hoskins hash12 与 CPU/glitch .tglsl 逐式同构）。
// hot 概率 = corruption；shift 仅 hot 带；RGB split 常开（0.25 底份）。
// NEAREST 水平采样（钳 [0.5, W−0.5] 后 floor），通道独立偏移。
uniform sampler2D u_texture;
uniform float u_band_height;   // 4..128 px
uniform float u_shift_amount;  // 0..1
uniform float u_rgb_split;     // 0..1
uniform float u_corruption;    // 0..1
uniform float u_seed;          // 0..100
in vec2 v_uv;
out vec4 fragColor;

float lib1_gl_hash12(float x, float y) {
    float a = fract(x * 0.1031);
    float b = fract(y * 0.1031);
    float c = a;
    float d = a * (b + 33.33) + b * (c + 33.33) + c * (a + 33.33);
    float a2 = a + d, b2 = b + d, c2 = c + d;
    return fract((a2 + b2) * c2);
}

float lib1_gl_sample_ch(int ch, float y_px, float x_px, float offset) {
    vec2 size = vec2(textureSize(u_texture, 0));
    float pos = clamp(x_px + offset, 0.5, size.x - 0.5);
    int ix = int(floor(pos));
    int iy = int(floor(y_px));
    ivec2 tc = ivec2(clamp(ix, 0, int(size.x) - 1),
                     clamp(iy, 0, int(size.y) - 1));
    return texelFetch(u_texture, tc, 0)[ch];
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_shift_amount < 0.001 && u_rgb_split < 0.001 && u_corruption < 0.001) {
        fragColor = c; return;
    }
    vec2 size = vec2(textureSize(u_texture, 0));
    float y_px = v_uv.y * size.y;
    float band = floor(y_px / max(u_band_height, 1.0));
    float shifted = band + u_seed * 97.0;
    float n1 = lib1_gl_hash12(shifted, 7.0);
    float n2 = lib1_gl_hash12(shifted, 23.0);
    float hot = n1 >= 1.0 - u_corruption ? 1.0 : 0.0;
    float shift_px = (n2 - 0.5) * u_shift_amount * 0.25 * hot * size.x;
    float split_px = u_rgb_split * 12.0 * (0.25 + 0.75 * hot);
    float x_px = v_uv.x * size.x;
    float r = lib1_gl_sample_ch(0, y_px, x_px, shift_px + split_px);
    float g = lib1_gl_sample_ch(1, y_px, x_px, shift_px);
    float b = lib1_gl_sample_ch(2, y_px, x_px, shift_px - split_px);
    fragColor = vec4(r, g, b, c.a);
}
"""

_LIB1_KALEIDOSCOPE_FRAGMENT = """
#version 330
// C10：N 扇区折叠镜像万花筒（kaleidoscope_ref 同构）。
// folded = a ≤ half ? a : A − a（镜像回 [0, half]），同半径双线性采样
// （LINEAR + clamp-to-edge 与 CPU 索引钳制一致）；amount<0.001 精确恒等。
uniform sampler2D u_texture;
uniform float u_sectors;   // 3..12
uniform float u_rotation;  // 度
uniform vec2 u_center;     // 归一 (u, v)
uniform float u_amount;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_amount < 0.001) { fragColor = c; return; }
    vec2 size = vec2(textureSize(u_texture, 0));
    float n = clamp(floor(u_sectors + 0.5), 3.0, 12.0);
    float cx = u_center.x * (size.x - 1.0);
    float cy = u_center.y * (size.y - 1.0);
    // CPU 像素中心索引坐标（y-down）；uv.y=(j+0.5)/H ↔ 纹理行 j
    float px_x = v_uv.x * size.x - 0.5;
    float px_y = v_uv.y * size.y - 0.5;
    float A = 2.0 * 3.14159265358979 / n;
    float theta = atan(px_y - cy, px_x - cx) + radians(u_rotation);
    float a = theta - A * floor(theta / A);
    float half_a = 0.5 * A;
    float folded = a <= half_a ? a : A - a;
    float r = length(vec2(px_x - cx, px_y - cy));
    float sx = cx + r * cos(folded);
    float sy = cy + r * sin(folded);
    // 索引坐标 → 纹理 uv（texel 中心 (i+0.5)/size；纹理 v 轴与数据行
    // 直接对应——翻转只在屏幕坐标 px_y，不在纹理查找）
    vec2 uv_s = vec2((sx + 0.5) / size.x, (sy + 0.5) / size.y);
    vec3 sampled = texture(u_texture, clamp(uv_s, vec2(0.0), vec2(1.0))).rgb;
    fragColor = vec4(c.rgb + (sampled - c.rgb) * u_amount, c.a);
}
"""


_LIB1_MEDIAN_FRAGMENT = """
#version 330
// C15：小核中值/百分位（median_rank_ref 同构）。edge 填充=钳制索引
// （texelFetch 钳制）；Luma 档亮度取秩、比例回乘（保色相）；amount 混合。
uniform sampler2D u_texture;
uniform int u_radius;      // 1=3x3, 2=5x5
uniform float u_percentile;
uniform int u_mode;        // 0=RGB, 1=Luma
uniform float u_amount;
in vec2 v_uv;
out vec4 fragColor;

const vec3 LIB1_MD_W = vec3(0.2126, 0.7152, 0.0722);

bool lib1_md_less(vec3 a, vec3 b) {
    if (a.r != b.r) return a.r < b.r;
    if (a.g != b.g) return a.g < b.g;
    return a.b < b.b;
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_amount < 0.001) { fragColor = c; return; }
    vec2 size = vec2(textureSize(u_texture, 0));
    ivec2 px = ivec2(clamp(vec2(v_uv.x, v_uv.y) * size, vec2(0.0), size - 1.0));
    int side = 2 * u_radius + 1;
    int n = side * side;
    float vals[25];
    vec3 vecs[25];
    int idx = 0;
    for (int dy = -2; dy <= 2; ++dy) {
        if (dy < -u_radius || dy > u_radius) continue;
        for (int dx = -2; dx <= 2; ++dx) {
            if (dx < -u_radius || dx > u_radius) continue;
            ivec2 tc = ivec2(clamp(px.x + dx, 0, int(size.x) - 1),
                             clamp(px.y + dy, 0, int(size.y) - 1));
            vec3 s = texelFetch(u_texture, tc, 0).rgb;
            vecs[idx] = s;
            vals[idx] = u_mode == 1 ? dot(s, LIB1_MD_W) : 0.0;
            idx += 1;
        }
    }
    // 选第 k 小（k = round(percentile·(n−1))）：rank 计数选择。RGB 档 =
    // **逐通道独立取秩**（CPU np.sort(window, axis=-1) 沿样本轴对每个
    // 通道分别排序——非向量字典序，初版误读）；Luma 档 = 亮度标量取秩
    // + 比例回乘保色相。无数据依赖数组整体移位（规避驱动展开缺陷）。
    int k = int(round(u_percentile * float(n - 1)));
    if (u_mode == 0) {
        vec3 pick = vec3(0.0);
        for (int ch = 0; ch < 3; ++ch) {
            bool taken[25];
            for (int i = 0; i < 25; ++i) taken[i] = false;
            float pickv = 0.0;
            for (int r2 = 0; r2 <= k; ++r2) {
                int best = -1;
                for (int i = 0; i < n; ++i) {
                    if (taken[i]) continue;
                    if (best < 0 || vecs[i][ch] < vecs[best][ch]) best = i;
                }
                taken[best] = true;
                pickv = vecs[best][ch];
            }
            pick[ch] = pickv;
        }
        fragColor = vec4(mix(c.rgb, pick, u_amount), c.a);
    } else {
        bool taken[25];
        for (int i = 0; i < 25; ++i) taken[i] = false;
        float pick_l = 0.0;
        for (int r2 = 0; r2 <= k; ++r2) {
            int best = -1;
            for (int i = 0; i < n; ++i) {
                if (taken[i]) continue;
                if (best < 0 || vals[i] < vals[best]) best = i;
            }
            taken[best] = true;
            pick_l = vals[best];
        }
        float luma = dot(c.rgb, LIB1_MD_W);
        vec3 med = luma > 1.0e-6
            ? c.rgb * (pick_l / max(luma, 1.0e-6))
            : vecs[0];
        fragColor = vec4(mix(c.rgb, med, u_amount), c.a);
    }
}
"""

_LIB1_CONVOLUTION_FRAGMENT = """
#version 330
// C16：3x3 自定义卷积核 + Σ|K| 归一化（<1e-6 回退 1）+ bias 后加
// （convolution_ref 同构）。texelFetch 钳制=CPU 索引钳制；线性无 clamp。
uniform sampler2D u_texture;
uniform float u_k00; uniform float u_k01; uniform float u_k02;
uniform float u_k10; uniform float u_k11; uniform float u_k12;
uniform float u_k20; uniform float u_k21; uniform float u_k22;
uniform float u_normalize;   // 0/1
uniform float u_bias;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec2 size = vec2(textureSize(u_texture, 0));
    ivec2 px = ivec2(clamp(vec2(v_uv.x, v_uv.y) * size, vec2(0.0), size - 1.0));
    vec3 acc = vec3(0.0);
    for (int i = -1; i <= 1; ++i) {
        for (int j = -1; j <= 1; ++j) {
            ivec2 tc = ivec2(clamp(px.x + j, 0, int(size.x) - 1),
                             clamp(px.y + i, 0, int(size.y) - 1));
            float k = (i == -1)
                ? (j == -1 ? u_k00 : (j == 0 ? u_k01 : u_k02))
                : (i == 0 ? (j == -1 ? u_k10 : (j == 0 ? u_k11 : u_k12))
                          : (j == -1 ? u_k20 : (j == 0 ? u_k21 : u_k22)));
            acc += k * texelFetch(u_texture, tc, 0).rgb;
        }
    }
    float d = abs(u_k00) + abs(u_k01) + abs(u_k02)
        + abs(u_k10) + abs(u_k11) + abs(u_k12)
        + abs(u_k20) + abs(u_k21) + abs(u_k22);
    float den = (u_normalize > 0.5 && d >= 1.0e-6) ? d : 1.0;
    vec4 c = texture(u_texture, v_uv);
    fragColor = vec4(acc / den + u_bias, c.a);
}
"""

_LIB1_HALATION_FRAGMENT = """
#version 330
// C20：halation 单尺度降级口径（F06 evaluate_degraded_single_scale 同构）：
// 12-tap 黄金角环（等面积距离 sqrt((i+0.5)/12)·R）逐 tap 软膝门控提取，
// 红重着色 (1+w, 1, 1−0.5w)，加性合成。R = W·(0.004+0.026·radius)。
uniform sampler2D u_texture;
uniform float u_strength;
uniform float u_radius;
uniform float u_red_scatter;
uniform float u_threshold;
in vec2 v_uv;
out vec4 fragColor;

const int LIB1_HL_N = 12;
const float LIB1_HL_GOLDEN = 2.399963229728653;
const float LIB1_HL_KNEE = 0.35;
const vec3 LIB1_HL_W = vec3(0.2126, 0.7152, 0.0722);

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_strength < 0.001) { fragColor = c; return; }
    vec2 size = vec2(textureSize(u_texture, 0));
    float r_px = max(2.0, size.x * (0.004 + 0.026 * u_radius));
    vec3 acc = vec3(0.0);
    for (int i = 0; i < LIB1_HL_N; ++i) {
        float ang = float(i) * LIB1_HL_GOLDEN;
        float dist = r_px * sqrt((float(i) + 0.5) / float(LIB1_HL_N));
        vec2 uv_s = v_uv + vec2(cos(ang) * dist / size.x,
                                sin(ang) * dist / size.y);
        vec3 s = texture(u_texture, clamp(uv_s, vec2(0.0), vec2(1.0))).rgb;
        float luma = dot(s, LIB1_HL_W);
        float t = clamp((luma - u_threshold) / LIB1_HL_KNEE, 0.0, 1.0);
        float m = t * t * (3.0 - 2.0 * t);
        acc += s * m;
    }
    vec3 halo = acc / float(LIB1_HL_N);
    vec3 scatter = vec3(halo.r * (1.0 + u_red_scatter),
                        halo.g,
                        halo.b * (1.0 - 0.5 * u_red_scatter));
    fragColor = vec4(c.rgb + scatter * u_strength, c.a);
}
"""


_LIB1_HALFTONE_FRAGMENT = """
#version 330
// C06：套印网屏 halftone（print_registration_ref 同构）：三版 C/M/Y
// 各自旋转网屏（0/15/30°）+ 套印偏移采样，网点覆盖 1−smoothstep，
// 乘性吸收自己管辖的通道。y 坐标与内容行直接对应（v·size）。
uniform sampler2D u_texture;
uniform int u_sep;          // 0=CMY approx, 1=Luma
uniform vec2 u_offset;      // 套印偏移（px）
uniform float u_paper_base;
uniform float u_ink;        // ink_amount
in vec2 v_uv;
out vec4 fragColor;

const float LIB1_HT_PITCH = 8.0;
const float LIB1_HT_EDGE = 0.06;

float lib1_ht_rot_c(float deg) { return cos(radians(deg)); }
float lib1_ht_rot_s(float deg) { return sin(radians(deg)); }

// 网点覆盖：l=源（0..1 钳）、scr_y/x=旋转网屏坐标
float lib1_ht_cov(float l, float sy, float sx) {
    float r = sqrt(max(1.0 - clamp(l, 0.0, 1.0), 0.0));
    float fy = sy - floor(sy);
    float fx = sx - floor(sx);
    float d = length(vec2(fy - 0.5, fx - 0.5));
    float t = clamp((d - (r - LIB1_HT_EDGE)) / (2.0 * LIB1_HT_EDGE), 0.0, 1.0);
    return 1.0 - t * t * (3.0 - 2.0 * t);
}


// CPU _bilinear 同构：先钳位置 [0, n−1] 再 floor（分数自然 ∈[0,1]，
// 边界处 f=0 精确 texel——CPU 是 clip(pos) 后 floor，非钳索引保外推）
vec3 lib1_ht_bilinear(vec2 size, vec2 pos) {
    pos = clamp(pos, vec2(0.0), size - 1.0);
    vec2 c0 = floor(pos);
    vec2 c1 = min(c0 + 1.0, size - 1.0);
    vec2 f = pos - c0;
    ivec2 a = ivec2(c0), b = ivec2(c1);
    vec3 p00 = texelFetch(u_texture, ivec2(a.x, a.y), 0).rgb;
    vec3 p10 = texelFetch(u_texture, ivec2(b.x, a.y), 0).rgb;
    vec3 p01 = texelFetch(u_texture, ivec2(a.x, b.y), 0).rgb;
    vec3 p11 = texelFetch(u_texture, ivec2(b.x, b.y), 0).rgb;
    return p00 * (1.0 - f.x) * (1.0 - f.y) + p10 * f.x * (1.0 - f.y)
        + p01 * (1.0 - f.x) * f.y + p11 * f.x * f.y;
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_ink < 0.001) { fragColor = c; return; }
    vec2 size = vec2(textureSize(u_texture, 0));
    // 像素中心索引坐标（y-down：CPU 行序 j ↔ uv.y=(j+0.5)/H）
    float px_x = v_uv.x * size.x - 0.5;
    float px_y = v_uv.y * size.y - 0.5;
    vec2 off = u_offset;
    // 各版偏移采样位（clamp-to-edge 双线性）
    // CPU plate_src：sy=yy+dy（yy=j 整数，meshgrid 无像素中心偏移）→
    // 整数位采样（bilinear f=0 精确 texel）
    vec3 s_c = lib1_ht_bilinear(size, vec2(px_x - off.x, px_y - off.y));
    vec3 s_m = lib1_ht_bilinear(size, vec2(px_x, px_y));
    vec3 s_y = lib1_ht_bilinear(size, vec2(px_x + off.x, px_y + off.y));
    float luma_c, luma_m, luma_y;
    if (u_sep == 0) {
        luma_c = s_c.r; luma_m = s_m.g; luma_y = s_y.b;
    } else {
        luma_c = dot(s_c, vec3(0.2126, 0.7152, 0.0722));
        luma_m = luma_c; luma_y = luma_c;
    }
    // 三版网屏（CPU screen(rot, dy, dx) 同构）：C（dy=−off.y, dx=−off.x,
    // 0°）/ M（基准，15°）/ Y（dy=+off.y, dx=+off.x，30°）；
    // sy = sa·qx + ca·qy，sx = ca·qx − sa·qy（rot=[[ca,−sa],[sa,ca]]）
    float qy0 = px_y + 0.5;   // CPU q = yy+dy+0.5（yy=j 整数）
    float qx0 = px_x + 0.5;
    float cov_c = lib1_ht_cov(luma_c,
        (0.0 * (qx0 - off.x) + 1.0 * (qy0 - off.y)) / LIB1_HT_PITCH,
        (1.0 * (qx0 - off.x) - 0.0 * (qy0 - off.y)) / LIB1_HT_PITCH);
    float mc = lib1_ht_rot_c(15.0), ms = lib1_ht_rot_s(15.0);
    float cov_m = lib1_ht_cov(luma_m,
        (ms * qx0 + mc * qy0) / LIB1_HT_PITCH,
        (mc * qx0 - ms * qy0) / LIB1_HT_PITCH);
    float yc = lib1_ht_rot_c(30.0), ys = lib1_ht_rot_s(30.0);
    float cov_y = lib1_ht_cov(luma_y,
        (ys * (qx0 + off.x) + yc * (qy0 + off.y)) / LIB1_HT_PITCH,
        (yc * (qx0 + off.x) - ys * (qy0 + off.y)) / LIB1_HT_PITCH);
    vec3 paper = c.rgb * (1.0 - u_paper_base * u_ink) + u_paper_base * u_ink;
    vec3 out_rgb = vec3(paper.r * (1.0 - u_ink * cov_c),
                        paper.g * (1.0 - u_ink * cov_m),
                        paper.b * (1.0 - u_ink * cov_y));
    fragColor = vec4(out_rgb, c.a);
}
"""

_LIB1_HATCHING_FRAGMENT = """
#version 330
// C07：亮度→墨密度→旋转坐标周期条带 SDF（hatching_ref 同构）。
// 线半宽 h = −AA + (0.5+2AA)·D；layers=2 正交半密度概率并；amount 混合。
uniform sampler2D u_texture;
uniform float u_spacing;
uniform float u_angle;      // 度
uniform int u_layers;       // 1/2
uniform vec3 u_ink_color;
uniform float u_paper;
uniform float u_amount;
in vec2 v_uv;
out vec4 fragColor;

const float LIB1_HA_AA = 0.06;
const vec3 LIB1_HA_W = vec3(0.2126, 0.7152, 0.0722);

float lib1_ha_band_cov(float density, float b) {
    float cell = abs(b - floor(b) - 0.5);
    float h = -LIB1_HA_AA + (0.5 + 2.0 * LIB1_HA_AA) * density;
    float t = clamp((cell - (h - LIB1_HA_AA)) / (2.0 * LIB1_HA_AA), 0.0, 1.0);
    return 1.0 - t * t * (3.0 - 2.0 * t);
}

void main() {
    vec4 c = texture(u_texture, v_uv);
    if (u_amount < 0.001) { fragColor = c; return; }
    vec2 size = vec2(textureSize(u_texture, 0));
    float px_x = v_uv.x * size.x - 0.5 + 0.5;
    float px_y = v_uv.y * size.y - 0.5 + 0.5;
    float luma = dot(c.rgb, LIB1_HA_W);
    float d_in = 1.0 - clamp(luma, 0.0, 1.0);
    float phi = radians(u_angle);
    float sp = max(u_spacing, 1.0);
    float b1 = (px_x * cos(phi) + px_y * sin(phi)) / sp;
    float cov = lib1_ha_band_cov(d_in, b1);
    if (u_layers == 2) {
        float b2 = (-px_x * sin(phi) + px_y * cos(phi)) / sp;
        float cov2 = lib1_ha_band_cov(d_in * 0.5, b2);
        cov = 1.0 - (1.0 - cov) * (1.0 - cov2);
    }
    vec3 hatched = (1.0 - cov) * u_paper + cov * u_ink_color;
    fragColor = vec4(c.rgb + (hatched - c.rgb) * u_amount, c.a);
}
"""


_LIB1_POLAR_FRAGMENT = """
#version 330
// C11：Cartesian↔polar UV 重映射（polar_coordinates_ref 同构）。
// 输出像素极坐标 (θ,r) → 输入 (u,v)：u = 0.5+θ/span、v = r/r_max
// （r_max = radius_scale·min(W,H)/2）；逆变换 F⁻¹ 对称。
// 生产 GL-natural 存储：入口把 v_uv 翻成 CPU 行序（row=(1−v)·H），
// θ/dy 按 ref 的 y-down 口径计算，取数时翻回 GL 行（texel 行号自底起）。
uniform sampler2D u_texture;
uniform int u_mode;          // 0=CartesianToPolar, 1=PolarToCartesian
uniform vec2 u_center;       // 归一 (u, v)
uniform float u_span;        // 度
uniform float u_radius_scale;
uniform int u_filter;        // 0=Nearest, 1=Linear
in vec2 v_uv;
out vec4 fragColor;

vec4 lib1_pc_fetch(vec2 size, float sx, float sy) {
    // 手动采样（Nearest=floor 钳制 / Linear=clamp-edge 双线性），
    // sx/sy 为 CPU 列/行索引坐标
    if (u_filter == 0) {
        // CPU Nearest = floor(pos+0.5)（round 语义）
        vec2 rp = clamp(floor(vec2(sx, sy) + 0.5), vec2(0.0), size - 1.0);
        return texelFetch(u_texture, ivec2(rp.x, size.y - 1.0 - rp.y), 0);
    }
    // 纹素中心边界钳＝CPU 索引钳（REPEAT 纹理下钳 [0,1] 会环绕混色）
    vec2 lo = vec2(0.5) / size;
    vec2 uv = clamp(vec2((sx + 0.5) / size.x,
                         1.0 - (sy + 0.5) / size.y), lo, vec2(1.0) - lo);
    return texture(u_texture, uv);
}

void main() {
    vec2 size = vec2(textureSize(u_texture, 0));
    float col = v_uv.x * size.x - 0.5;
    float row = (1.0 - v_uv.y) * size.y - 0.5;
    float cx = u_center.x * (size.x - 1.0);
    float cy = u_center.y * (size.y - 1.0);
    float span = radians(u_span > 0.0 ? u_span : 360.0);
    float r_max = max(u_radius_scale, 1.0e-9) * 0.5 * min(size.x, size.y);
    float sx, sy;
    if (u_mode == 0) {
        float dx = col - cx;
        float dy = row - cy;
        float theta = atan(dy, dx);
        float u = 0.5 + theta / span;
        float v = length(vec2(dx, dy)) / r_max;
        sx = u * (size.x - 1.0);
        sy = v * (size.y - 1.0);
    } else {
        float uq = size.x > 1.5 ? col / (size.x - 1.0) : 0.0;
        float vq = size.y > 1.5 ? row / (size.y - 1.0) : 0.0;
        float theta = span * (uq - 0.5);
        float rad = vq * r_max;
        sx = cx + rad * cos(theta);
        sy = cy + rad * sin(theta);
    }
    fragColor = lib1_pc_fetch(size, sx, sy);
}
"""

_LIB1_TILE_ARRAY_FRAGMENT = """
#version 330
// C14：格胞阵列（tile_array_ref 同构）。count 格胞 + spacing 内边距 +
// offset 瓦片内环绕相位；空隙 RGB=0/α=0；texel-block 最近邻 floor(u·W)
// （1×1+0 精确恒等）。生产 GL-natural 存储：入口翻 v 成 CPU 行序，
// 取数 texel 行号翻回（size.y−1−yi）。
uniform sampler2D u_texture;
uniform float u_count_x;   // 1..16
uniform float u_count_y;
uniform float u_spacing;   // 0..0.9
uniform vec2 u_offset;     // −1..1 瓦片内相位
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec2 size = vec2(textureSize(u_texture, 0));
    float col = v_uv.x * size.x - 0.5;
    float row = (1.0 - v_uv.y) * size.y - 0.5;
    float tw = size.x / u_count_x;
    float th = size.y / u_count_y;
    float cw = tw * (1.0 - u_spacing);
    float ch = th * (1.0 - u_spacing);
    float i = clamp(floor(col / tw), 0.0, u_count_x - 1.0);
    float j = clamp(floor(row / th), 0.0, u_count_y - 1.0);
    float u_c = (col - i * tw - tw * u_spacing / 2.0) / cw;
    float v_c = (row - j * th - th * u_spacing / 2.0) / ch;
    bool inside = u_c >= 0.0 && u_c < 1.0 && v_c >= 0.0 && v_c < 1.0;
    float u_s = u_c + u_offset.x - floor(u_c + u_offset.x);
    float v_s = v_c + u_offset.y - floor(v_c + u_offset.y);
    float xi = clamp(floor(u_s * size.x), 0.0, size.x - 1.0);
    float yi = clamp(floor(v_s * size.y), 0.0, size.y - 1.0);
    vec3 rgb = texelFetch(u_texture, ivec2(xi, size.y - 1.0 - yi), 0).rgb;
    float alpha = inside ? texelFetch(u_texture, ivec2(xi, size.y - 1.0 - yi), 0).a : 0.0;
    if (!inside) { rgb = vec3(0.0); alpha = 0.0; }
    fragColor = vec4(rgb, alpha);
}
"""


_LIB1_VECTOR_DISPLACE_FRAGMENT = """
#version 330
// C12：向量图驱动位移（vector_displace_ref 同构，多输入）。
// dx=(map.r−neutral)·scale（Pixels 像素 / UV 画幅比例），mask 乘性门控；
// image 在 (row+dy, col+dx) 双线性采样（y 与行序一致）。端口角色契约：
// u_image_tex=image / u_map_tex=vector / u_mask_tex=mask（不可串线）。
uniform sampler2D u_image_tex;  // unit 0
uniform sampler2D u_map_tex;    // unit 1（分派器绑定）
uniform sampler2D u_mask_tex;   // unit 2（有 mask 时分派器绑定；无则剔除）
uniform float u_has_mask;
uniform float u_scale;
uniform float u_neutral;
uniform int u_units;            // 0=Pixels, 1=UV
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec2 size = vec2(textureSize(u_image_tex, 0));
    vec2 mv = texture(u_map_tex, v_uv).rg;
    float dx = (mv.x - u_neutral) * u_scale;
    float dy = (mv.y - u_neutral) * u_scale;
    if (u_units == 1) { dx *= size.x; dy *= size.y; }
    if (u_has_mask > 0.5) {
        vec4 m4 = texture(u_mask_tex, v_uv);
        float m = m4.a;
        dx *= m; dy *= m;
    }
    // CPU：out = bilinear(img, yy+dy, xx+dx)（行序 y-down）。生产存储为
    // GL-natural（CPU top-left 上传前 flipud）：CPU 行+dy ⇔ v 减 dy
    // （v 轴 y-up 与行号反向，同 stmap 的 (1-v) 显式翻转口径）。
    // 边界钳到纹素中心（0.5/size .. 1-0.5/size）＝CPU 的索引钳 [0,n-1]；
    // 钳 [0,1] 在池化纹理默认 REPEAT 下 uv=0/1 会环绕混入对边纹素
    vec2 uv = vec2(v_uv.x + dx / size.x, v_uv.y - dy / size.y);
    vec2 lo = vec2(0.5) / size;
    fragColor = texture(u_image_tex, clamp(uv, lo, vec2(1.0) - lo));
}
"""

_LIB1_STMAP_FRAGMENT = """
#version 330
// C13：绝对 UV 重采样（stmap_ref 同构，多输入）。map 通道 RG/BA 定 uv、
// origin 定原点角（BottomLeft→v 翻转）、invalid=Clamp（边界钳）/Passthrough
// （域外原样）、Nearest=round 索引/Linear 双线性。
uniform sampler2D u_image_tex;
uniform sampler2D u_map_tex;
uniform int u_channels;   // 0=RG, 1=BA
uniform int u_origin;     // 0=BottomLeft, 1=TopLeft
uniform int u_invalid;    // 0=Clamp, 1=Passthrough
uniform int u_filter;     // 0=Nearest, 1=Linear
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec2 size = vec2(textureSize(u_image_tex, 0));
    vec4 mv = texture(u_map_tex, v_uv);
    vec2 mn = u_channels == 0 ? mv.rg : mv.ba;
    float u = mn.x;
    float v = mn.y;
    float px = u * (size.x - 1.0);
    // 生产 GL-natural 存储（CPU top-left 上传前 flipud）：texel 行号自
    // 底部起数，CPU 行 = size.y-1-texel 行。BottomLeft（v=0=图像底部）
    // ⇔ texel 行 = v·(h-1)；TopLeft 反之。初版公式仅在镜像存储下成立。
    float py = (u_origin == 0) ? v * (size.y - 1.0)
                               : (1.0 - v) * (size.y - 1.0);
    if (u_invalid == 1 && (u < 0.0 || u > 1.0 || v < 0.0 || v > 1.0)) {
        fragColor = texture(u_image_tex, v_uv);
        return;
    }
    vec4 out_c;
    if (u_filter == 0) {
        vec2 rp = clamp(floor(vec2(px, py) + 0.5), vec2(0.0), size - 1.0);
        out_c = texelFetch(u_image_tex, ivec2(rp), 0);
    } else {
        // 纹素中心边界钳＝CPU 索引钳（REPEAT 纹理下钳 [0,1] 会环绕混色）
        vec2 lo = vec2(0.5) / size;
        out_c = texture(u_image_tex,
                        clamp((vec2(px, py) + 0.5) / size, lo, vec2(1.0) - lo));
    }
    fragColor = out_c;
}
"""


_LIB1_FREQ_BLUR_FRAGMENT = """
#version 330
// C21 频率分离 pass1/2：可分离盒/高斯低通（frequency_separation_ref
// _separable_blur 同构）。u_direction=0 垂直核 / 1 水平核；核宽 k 随
// radius（Box k=round(r)，Gaussian k=ceil(3σ)、σ=r/3）——常量循环界
// 64 + break（GLSL 330 纪律），权重逐 tap 现算、高斯两轮归一。
uniform sampler2D u_texture;
uniform float u_radius;
uniform int u_filter;      // 0=Box, 1=Gaussian
uniform int u_direction;   // 0=vertical, 1=horizontal
in vec2 v_uv;
out vec4 fragColor;

void main() {
    float radius = u_radius;
    if (radius <= 0.0) { fragColor = texture(u_texture, v_uv); return; }
    int k;
    float inv_w;
    float sigma = max(radius / 3.0, 1.0e-3);
    if (u_filter == 0) {
        k = int(round(radius));
        inv_w = 1.0 / float(2 * k + 1);
    } else {
        k = max(1, int(ceil(3.0 * sigma)));
        inv_w = 0.0;   // 高斯归一在第二循环累计
    }
    float wsum = 0.0;
    vec3 acc = vec3(0.0);
    // CPU 参考（frequency_separation_ref）只模糊 RGB，low 保留原 α
    // （分离载荷在 high.rgb；α 参与模糊会破坏 low/high α 契约）
    float alpha = texture(u_texture, v_uv).a;
    for (int i = -64; i <= 64; ++i) {
        if (i < -k || i > k) continue;   // 对称核从 −64 起：break 会首轮退出
        float wt = (u_filter == 0)
            ? inv_w
            : exp(-float(i * i) / (2.0 * sigma * sigma));
        wsum += wt;
        vec2 size = vec2(textureSize(u_texture, 0));
        vec2 px = v_uv * size;
        float off = float(i);
        vec2 src_px = (u_direction == 0)
            ? vec2(px.x, clamp(px.y + off, 0.5, size.y - 0.5))
            : vec2(clamp(px.x + off, 0.5, size.x - 0.5), px.y);
        acc += texture(u_texture, (floor(src_px) + 0.5) / size).rgb * wt;
    }
    if (u_filter == 1) acc /= max(wsum, 1.0e-6);
    fragColor = vec4(acc, alpha);
}
"""

_LIB1_FREQ_HIGH_FRAGMENT = """
#version 330
// C21 频率分离 pass3：high = rgb − low（带符号不 clamp）；α=0
// （low 保留原 α——分离的载荷在 high.rgb）。
uniform sampler2D u_image_tex;   // unit 0
uniform sampler2D u_low_tex;     // unit 1（分派器绑定）
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec4 c = texture(u_image_tex, v_uv);
    vec4 low = texture(u_low_tex, v_uv);
    fragColor = vec4(c.rgb - low.rgb, 0.0);
}
"""
