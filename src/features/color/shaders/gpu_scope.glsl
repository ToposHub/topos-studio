// =============================================================================
// gpu_scope.glsl — Native GPU scope pipeline for Topos Color
// =============================================================================
// GLSL 330 baseline. Uses raster + additive blending density accumulation.
// No compute / SSBO / atomics — only FBO, float textures and POINTS draws.
//
// Stages (see docs/gpu_scope_implementation_plan_2026-08-05.md §4):
//   1. SCOPE_SAMPLE_FRAGMENT        : downscale final front texture to the
//                                     analysis resolution (RGBA16F sample tex;
//                                     alpha carries optional GPU mask weight).
//   2. SCOPE_WAVEFORM_ACCUM_VERTEX  : per-sample vertex, derives source pixel
//                                     coords from gl_VertexID + texelFetch.
//   2b. (accumulation via additive blending, no fragment shader body needed)
//   3. SCOPE_WAVEFORM_DISPLAY       : tone-map RGB16F density -> RGBA8 with
//                                     horizontal graticule baked in.
//   4. SCOPE_VECTORSCOPE_ACCUM_VERTEX: per-sample vertex, RGB->Rec709 Cb/Cr.
//   5. SCOPE_VECTORSCOPE_DISPLAY    : tone-map R16F density -> RGBA8 with
//                                     circular graticule + color targets.
//
// Conventions: all numeric literals carry a float suffix, loop bounds are
// constants, output varyings use ``v_`` prefix, uniforms use ``u_`` prefix.
// =============================================================================

// -----------------------------------------------------------------------------
// 1. Sample / downscale pass (full-screen quad, passthrough vertex reused)
// -----------------------------------------------------------------------------
// Fragment: copies the front texture into the smaller analysis texture while
// retaining a small over/under-range window.  The waveform keeps 0..1 as the
// legal 10-bit range, but values up to 1.2 (and down to -0.1) remain visible
// above/below the 1023/0 guides as clipping diagnostics.
#define SCOPE_SAMPLE_FRAGMENT """
#version 330
uniform sampler2D u_texture;
uniform sampler2D u_mask_texture;
uniform bool u_use_mask;
uniform vec4 u_mask_uv_rect; // [offset_x, offset_y, scale_x, scale_y]
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    vec3 c = texture(u_texture, v_texcoord).rgb;
    vec2 mask_uv = u_mask_uv_rect.xy + v_texcoord * u_mask_uv_rect.zw;
    float mask = u_use_mask ? texture(u_mask_texture, mask_uv).r : 1.0;
    // Keep enough headroom to show signal excursions without allowing NaN /
    // unbounded HDR values to poison the additive density pass.  The display
    // shader marks values above 1.0 red and values below 0.0 blue.
    f_color = vec4(clamp(c, -0.1, 1.2), clamp(mask, 0.0, 1.0));
}
"""

// -----------------------------------------------------------------------------
// 2. Waveform accumulation (POINTS draw, gl_VertexID-driven)
// -----------------------------------------------------------------------------
// One vertex per analysis pixel. The vertex shader derives the source pixel
// coordinate from gl_VertexID (sample_w x sample_h) and maps it to:
//   x_ndc : horizontal position (left -> right)
//   y_ndc : mapped from the selected channel value [-0.1,1.2] (bottom -> top)
// R/G/B are accumulated in three separate passes (R then G then B) so the
// RGBA16F density texture records per-channel counts; alpha additionally
// records signed over/under-range density for clipping diagnostics.
//
// u_channel selects which channel: 0=R, 1=G, 2=B, 3=luma (BT.709 Y).
// u_range_min/u_range_max define the value axis range (default -0.1 .. 1.2 to
// match the CPU reference, HDR headroom included).
#define SCOPE_WAVEFORM_ACCUM_VERTEX """
#version 330
uniform sampler2D u_sample_tex;
uniform vec2 u_sample_size;     // (width, height) of the analysis texture
uniform vec2 u_density_size;    // (width, height) of the waveform density FBO
uniform int u_channel;          // 0=R, 1=G, 2=B, 3=luma
uniform float u_range_min;      // value mapped to bottom (e.g. -0.1)
uniform float u_range_max;      // value mapped to top (e.g. 1.2)
out float v_weight;
out float v_range_flag; // +1 over 100%, -1 below 0%, 0 in legal range
void main() {
    int idx = gl_VertexID;
    int sx = idx % int(u_sample_size.x);
    int sy = idx / int(u_sample_size.x);
    vec4 sample_rgba = texelFetch(u_sample_tex, ivec2(sx, sy), 0);
    vec3 c = sample_rgba.rgb;
    float sample_weight = sample_rgba.a;
    float val = (u_channel == 0) ? c.r
        : ((u_channel == 1) ? c.g
        : ((u_channel == 2) ? c.b
        : dot(c, vec3(0.2126, 0.7152, 0.0722))));
    v_range_flag = val > 1.0 ? 1.0 : (val < 0.0 ? -1.0 : 0.0);
    // Map horizontal source position to NDC [-1, 1].
    // Pixel center u in [0,1]; offset by half a density cell so columns
    // align with bin centers and never clip at the edges.
    float u = (float(sx) + 0.5) / u_sample_size.x;
    float x_ndc = u * 2.0 - 1.0;
    // Map value to NDC y. range_min -> -1, range_max -> +1, clamp outside.
    float t = (val - u_range_min) / (u_range_max - u_range_min);
    t = clamp(t, 0.0, 1.0);
    float y_ndc = t * 2.0 - 1.0;
    // Snap y to the center of a density row so points land inside a cell.
    float half_cell = 1.0 / u_density_size.y;
    y_ndc = clamp(y_ndc, -1.0 + half_cell, 1.0 - half_cell);
    gl_Position = vec4(x_ndc, y_ndc, 0.0, 1.0);
    // A 320px analysis row is intentionally cheaper than the display FBO
    // (normally 1024px wide).  Splat each sample across the corresponding
    // horizontal footprint so the scope is a continuous trace instead of a
    // column of isolated one-pixel dots.  Minimum 2.0: Apple GPUs report
    // corner gl_PointCoord values for 1-px sprites, which the fragment
    // Gaussian would discard (losing most samples at dense analysis tiers).
    float horizontal_coverage = u_density_size.x / max(u_sample_size.x, 1.0);
    gl_PointSize = clamp(horizontal_coverage * 0.98, 2.0, 3.0);
    v_weight = sample_weight;
}
"""

// Fragment: write the channel weight into the matching color channel so
// additive blending accumulates per-channel density in RGB16F.
#define SCOPE_WAVEFORM_ACCUM_FRAGMENT """
#version 330
uniform int u_channel;
in float v_weight;
in float v_range_flag;
out vec4 f_color;
void main() {
    // Only render fragments inside the point; gl_PointCoord is in [0,1].
    vec2 pc = gl_PointCoord - vec2(0.5);
    float radius2 = dot(pc, pc) * 4.0;
    if (radius2 > 1.0) {
        discard;
    }
    // Gaussian-ish splat: bright core plus a soft halo. Additive blending
    // turns overlapping samples into the luminous trails seen in broadcast
    // scopes while retaining channel separation in sparse areas.
    float splat = exp(-radius2 * 4.0);
    vec3 w = vec3(0.0);
    if (u_channel == 0)      w.r = v_weight * splat;
    else if (u_channel == 1) w.g = v_weight * splat;
    else if (u_channel == 2) w.b = v_weight * splat;
    else w = vec3(v_weight * splat);  // luma: equal RGB -> white trace
    // RGB keeps the original channel density, including out-of-range
    // samples. Alpha is a signed diagnostic density: positive = over-range,
    // negative = under-range. Additive blending preserves the sign.
    f_color = vec4(w, v_range_flag * v_weight * splat);
}
"""

// -----------------------------------------------------------------------------
// 3. Waveform display / tone-map pass (full-screen quad)
// -----------------------------------------------------------------------------
// Tone-maps the RGB16F density texture into an RGBA8 parade-style waveform
// with gamma boost and horizontal graticule lines baked into the shader so
// QPainter never needs to redraw the background each frame.
#define SCOPE_WAVEFORM_DISPLAY_FRAGMENT """
#version 330
uniform sampler2D u_density;
uniform vec2 u_density_size;
uniform float u_max_inv;     // 1.0 / max density seen (precomputed on CPU)
uniform float u_range_min;
uniform float u_range_max;
in vec2 v_texcoord;
out vec4 f_color;

// Draw a 1px horizontal graticule line at normalized height h in [0,1].
float graticule(float h) {
    float pixel = 1.0 / u_density_size.y;
    float d = abs(v_texcoord.y - h);
    return 1.0 - smoothstep(0.0, pixel * 1.5, d);
}

float value_to_uv(float value) {
    return clamp(
        (value - u_range_min) / max(u_range_max - u_range_min, 0.000001),
        0.0, 1.0
    );
}

void main() {
    vec2 texel = 1.0 / u_density_size;
    // A small vertical convolution creates the phosphor-like persistence of
    // a reference scope. The horizontal taps close any residual gaps from
    // the lower-resolution analysis grid without smearing the image across
    // neighbouring columns.
    vec4 density_rgba = texture(u_density, v_texcoord) * 0.40;
    density_rgba += texture(u_density, v_texcoord + vec2(0.0, texel.y)) * 0.16;
    density_rgba += texture(u_density, v_texcoord - vec2(0.0, texel.y)) * 0.16;
    density_rgba += texture(u_density, v_texcoord + vec2(0.0, texel.y * 2.0)) * 0.06;
    density_rgba += texture(u_density, v_texcoord - vec2(0.0, texel.y * 2.0)) * 0.06;
    density_rgba += texture(u_density, v_texcoord + vec2(texel.x, 0.0)) * 0.08;
    density_rgba += texture(u_density, v_texcoord - vec2(texel.x, 0.0)) * 0.08;
    vec3 density = density_rgba.rgb;
    float over_density = max(density_rgba.a, 0.0);
    float under_density = max(-density_rgba.a, 0.0);
    // Unlike the old per-pixel max normalization, a fixed density scale
    // preserves intensity: dense regions become pale/white, sparse regions
    // remain visible as dim coloured trails.
    vec3 norm_density = clamp(density * u_max_inv, 0.0, 1.0);
    vec3 mapped = pow(norm_density, vec3(0.54));
    mapped = clamp(mapped * 1.32, 0.0, 1.0);
    // RGB channels that occupy the same value bin are additive light, not
    // three independent opaque strokes. Lift their common component toward
    // white so dense neutral/white parts of the image read like DaVinci's
    // pale waveform core while isolated channel energy stays saturated.
    float overlap = min(mapped.r, min(mapped.g, mapped.b));
    float white_lift = overlap * 0.24
        + smoothstep(0.12, 0.45, overlap) * 0.32;
    mapped = clamp(mapped + white_lift, 0.0, 1.0);
    // Out-of-range diagnostics.  Keep the legal waveform colour underneath,
    // then lift excursions above 100% in red and below 0% in blue.  The same
    // fixed density normalization is used so a single clipped pixel remains
    // dim while a solid clipped band becomes unmistakable.
    float over_alert = clamp(pow(over_density * u_max_inv, 0.54) * 1.45, 0.0, 1.0);
    float under_alert = clamp(pow(under_density * u_max_inv, 0.54) * 1.45, 0.0, 1.0);
    mapped = mix(mapped, vec3(1.0, 0.035, 0.0), over_alert);
    mapped = mix(mapped, vec3(0.02, 0.20, 1.0), under_alert);
    // The value axis includes -10%..120% headroom. Place the familiar
    // 0%/100% guides at their actual value positions instead of the texture
    // edges (which represent -10%/120%).
    float line_0 = graticule(value_to_uv(0.0));
    float line_100 = graticule(value_to_uv(1.0));
    float grid = 0.0;
    // 10-bit-style minor divisions (0, 128, ... 1023), matching the
    // reference scope's evenly spaced amber graticule.
    for (int i = 1; i < 8; i++) {
        grid = max(grid, graticule(value_to_uv(float(i) / 8.0)) * 0.55);
    }
    // DaVinci's waveform uses a restrained amber graticule. Keep it bright
    // enough to read through the black background without competing with
    // saturated signal traces.
    vec3 grid_rgb = vec3(grid) * vec3(0.30, 0.23, 0.025);
    grid_rgb = max(grid_rgb, line_0 * vec3(0.38, 0.28, 0.025));
    grid_rgb = max(grid_rgb, line_100 * vec3(0.38, 0.28, 0.025));
    vec3 out_rgb = max(mapped, grid_rgb);
    f_color = vec4(out_rgb, 1.0);
}
"""

// -----------------------------------------------------------------------------
// 4. Vectorscope accumulation (POINTS draw, gl_VertexID-driven)
// -----------------------------------------------------------------------------
// One vertex per analysis pixel. Converts RGB to Rec.709 Cb/Cr (matching the
// CPU reference exactly: Cb = -0.169R -0.331G +0.500B,
// Cr = 0.500R -0.419G -0.081B) and maps (Cb, Cr) into the density texture.
// u_zoom scales the mapping (1.0 or 2.0); only the display mapping changes.
#define SCOPE_VECTORSCOPE_ACCUM_VERTEX """
#version 330
uniform sampler2D u_sample_tex;
uniform vec2 u_sample_size;
uniform vec2 u_density_size;   // square, e.g. 800x800
uniform float u_zoom;          // 1.0 or 2.0
out float v_weight;
void main() {
    int idx = gl_VertexID;
    int sx = idx % int(u_sample_size.x);
    int sy = idx / int(u_sample_size.x);
    vec4 sample_rgba = texelFetch(u_sample_tex, ivec2(sx, sy), 0);
    vec3 c = sample_rgba.rgb;
    // Rec.709 (matches CPU REC709_CB_COEFF / REC709_CR_COEFF in
    // vectorscope_widget.py:30-31).
    float cb = dot(c, vec3(-0.169, -0.331, 0.500));
    float cr = dot(c, vec3( 0.500, -0.419, -0.081));
    // Cb,Cr are in roughly [-0.5, 0.5]. Map to NDC with zoom.
    // Display convention mirrors the CPU widget: x = Cb, y = -Cr (Qt Y flip
    // handled by drawing the texture top-down in the presenter).
    float x_ndc = cb * 2.0 * u_zoom;
    float y_ndc = cr * 2.0 * u_zoom;
    // Snap toward the nearest cell center so points always fall in a bin.
    float half_cell = 1.0 / u_density_size.x;
    x_ndc = clamp(x_ndc, -1.0 + half_cell, 1.0 - half_cell);
    y_ndc = clamp(y_ndc, -1.0 + half_cell, 1.0 - half_cell);
    gl_Position = vec4(x_ndc, y_ndc, 0.0, 1.0);
    gl_PointSize = 1.0;
    v_weight = sample_rgba.a;
}
"""

#define SCOPE_VECTORSCOPE_ACCUM_FRAGMENT """
#version 330
in float v_weight;
out float f_density;
void main() {
    // Unconditional unit write. gl_PointCoord must NOT be used here: the
    // point is 1 px wide and Apple GPUs report arbitrary corner coordinates
    // for 1-px sprites, so a PointCoord mask would silently discard most
    // samples (measured ~90% loss on Apple silicon).
    f_density = v_weight;
}
"""

// -----------------------------------------------------------------------------
// 5. Vectorscope display / tone-map pass (full-screen quad)
// -----------------------------------------------------------------------------
// Tone-maps the R16F density into a colourful vectorscope, draws the circular
// graticule (75% / 50% / 25% rings), the six color targets and the skin-tone
// line — all procedurally, matching the CPU widget's visual reference.
#define SCOPE_VECTORSCOPE_DISPLAY_FRAGMENT """
#version 330
uniform sampler2D u_density;
uniform vec2 u_density_size;
uniform float u_max_inv;       // 1.0 / max density
uniform float u_zoom;          // 1.0 or 2.0
in vec2 v_texcoord;
out vec4 f_color;

// Distance from scope center in Cb/Cr space. v_texcoord in [0,1]^2 maps to
// Cb,Cr in [-0.5,0.5]/zoom.
vec2 to_cbcr(vec2 uv) {
    vec2 ndc = uv * 2.0 - 1.0;        // [-1,1]
    return ndc * 0.5 / u_zoom;        // back to Cb/Cr units
}

// Reverse-map a Cb/Cr pair to a display RGB (Y=0.5 mid gray, as the CPU
// reference does at vectorscope_widget.py:257).
vec3 cbcr_to_rgb(float cb, float cr) {
    float y = 0.5;
    float r = y + 1.402 * cr;
    float g = y - 0.344136 * cb - 0.714136 * cr;
    float b = y + 1.772 * cb;
    return clamp(vec3(r, g, b), 0.0, 1.0);
}

// Draw a ring of given radius (in Cb/Cr units) and line width (in uv units).
float ring(float radius_cb, float width_uv, vec2 cbcr_center) {
    float d = abs(length(cbcr_center) - radius_cb);
    return 1.0 - smoothstep(0.0, width_uv, d);
}

void main() {
    vec2 uv = v_texcoord;
    vec2 cbcr = to_cbcr(uv);
    float sat = length(cbcr);

    float density = texture(u_density, uv).r;
    float norm = density * u_max_inv;
    float brightness = pow(norm, 0.7) * 2.0;          // CPU uses *2.0 cap
    brightness = clamp(brightness, 0.0, 1.0);
    vec3 heat = cbcr_to_rgb(cbcr.x, cbcr.y);
    // Saturation boost (CPU uses 1.5x).
    vec3 boosted = mix(vec3(dot(heat, vec3(0.2126, 0.7152, 0.0722))), heat, 1.5);
    vec3 out_rgb = boosted * brightness;
    float alpha = pow(norm, 0.6);

    // ---- graticule (procedural) ----
    float width_uv = 1.5 / u_density_size.x;
    // 75% / 50% / 25% saturation rings (radius in Cb/Cr units).
    float g75 = ring(0.5 * 0.75, width_uv, cbcr);
    float g50 = ring(0.5 * 0.50, width_uv, cbcr);
    float g25 = ring(0.5 * 0.25, width_uv * 0.6, cbcr);
    float grid = max(g75 * 0.30, max(g50 * 0.22, g25 * 0.12));
    vec3 grid_color = vec3(0.20, 0.20, 0.20);

    // center cross
    float cross_w = 2.0 / u_density_size.x;
    float cross =
        (1.0 - smoothstep(0.0, cross_w, abs(cbcr.x))) * 0.4 +
        (1.0 - smoothstep(0.0, cross_w, abs(cbcr.y))) * 0.4;

    // skin tone line at 123 degrees, length 0.75 of radius
    float ang = radians(123.0);
    vec2 dir = vec2(cos(ang), sin(ang));
    float along = dot(cbcr, dir);
    float perp = abs(dot(cbcr, vec2(-dir.y, dir.x)));
    float skin = (along > 0.0 && along < 0.5 * 0.75)
        ? (1.0 - smoothstep(0.0, width_uv, perp)) * 0.5 : 0.0;
    vec3 skin_color = vec3(1.0, 0.71, 0.47);

    // Mask everything outside the 100% ring.
    if (sat > 0.5) {
        out_rgb = vec3(0.0);
        alpha = 0.0;
        grid = 0.0;
        cross = 0.0;
        skin = 0.0;
    }

    vec3 col = out_rgb;
    col = max(col, grid * grid_color);
    col = max(col, cross * grid_color);
    col = max(col, skin * skin_color);
    alpha = clamp(max(alpha, max(grid, max(cross, skin))), 0.0, 1.0);
    // Background fill so the scope reads on any panel colour.
    col += vec3(0.02, 0.02, 0.02) * (1.0 - alpha);
    f_color = vec4(col, 1.0);
}
"""

// -----------------------------------------------------------------------------
// 6. Shared passthrough vertex (quad-based display passes).
// -----------------------------------------------------------------------------
// Kept here so the shader file is self-contained: the shared GpuScopeEngine
// builds every quad pass from this vertex shader, independent of either
// feature's shader library.
#define SCOPE_PASSTHROUGH_VERTEX """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""

// -----------------------------------------------------------------------------
// 7. Histogram accumulation (POINTS draw, per-channel columns).
// -----------------------------------------------------------------------------
// One vertex per analysis pixel. x = code value (0..1) of the selected
// channel; y is a golden-ratio scatter so per-texel counts stay far below
// the float16 limit while the reduce pass can still recover exact column
// totals. Channel density is written to .r/.g/.b exactly like the waveform
// fragment, so SCOPE_WAVEFORM_ACCUM_FRAGMENT is reused as-is.
#define SCOPE_HISTOGRAM_ACCUM_VERTEX """
#version 330
uniform sampler2D u_sample_tex;
uniform vec2 u_sample_size;
uniform vec2 u_density_size;
uniform int u_channel;         // 0=R, 1=G, 2=B
out float v_weight;
out float v_range_flag;
void main() {
    int idx = gl_VertexID;
    int sx = idx % int(u_sample_size.x);
    int sy = idx / int(u_sample_size.x);
    vec4 sample_rgba = texelFetch(u_sample_tex, ivec2(sx, sy), 0);
    vec3 c = sample_rgba.rgb;
    float val = (u_channel == 0) ? c.r : ((u_channel == 1) ? c.g : c.b);
    val = clamp(val, 0.0, 1.0);
    float x_ndc = val * 2.0 - 1.0;
    float spread = fract(float(idx) * 0.61803398875);
    float y_ndc = spread * 2.0 - 1.0;
    float half_cell = 1.0 / u_density_size.x;
    x_ndc = clamp(x_ndc, -1.0 + half_cell, 1.0 - half_cell);
    y_ndc = clamp(y_ndc, -1.0 + half_cell, 1.0 - half_cell);
    gl_Position = vec4(x_ndc, y_ndc, 0.0, 1.0);
    gl_PointSize = 1.0;
    v_weight = sample_rgba.a;
    v_range_flag = 0.0;
}
"""

// Unit-splat fragment for the histogram accumulation: an unconditional
// channel write (the point is 1 px; see the vectorscope accum fragment for
// why gl_PointCoord must not be used here).
#define SCOPE_HISTOGRAM_ACCUM_FRAGMENT """
#version 330
uniform int u_channel;
in float v_weight;
in float v_range_flag;
out vec4 f_color;
void main() {
    vec3 w = vec3(0.0);
    if (u_channel == 0)      w.r = v_weight;
    else if (u_channel == 1) w.g = v_weight;
    else                     w.b = v_weight;
    f_color = vec4(w, 0.0);
}
"""

// -----------------------------------------------------------------------------
// 8. Histogram column reduction (full-screen quad -> 512x1 RGBA16F).
// -----------------------------------------------------------------------------
// Sums the scattered point density vertically per column and stores
// log1p(count) per channel, exactly like the CPU widget's log scale.
// HIST_DENSITY_ROWS must match the pool's histogram density height.
#define SCOPE_HISTOGRAM_REDUCE_FRAGMENT """
#version 330
uniform sampler2D u_density;
const int HIST_DENSITY_WIDTH = 512;
const int HIST_DENSITY_ROWS = 256;
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    int col = int(clamp(v_texcoord.x, 0.0, 1.0) * float(HIST_DENSITY_WIDTH - 1) + 0.5);
    vec3 sum = vec3(0.0);
    for (int row = 0; row < HIST_DENSITY_ROWS; row++) {
        sum += texelFetch(u_density, ivec2(col, row), 0).rgb;
    }
    f_color = vec4(log(sum + vec3(1.0)), 1.0);
}
"""

// -----------------------------------------------------------------------------
// 9. Histogram display (full-screen quad, curve fill).
// -----------------------------------------------------------------------------
// Draws the three channel curves as translucent fills over black with
// 0% / 50% / 100% graticule lines. Fixed normalization (see waveform) keeps
// the pass free of any density readback.
#define SCOPE_HISTOGRAM_DISPLAY_FRAGMENT """
#version 330
uniform sampler2D u_hist_sum;
uniform vec2 u_display_size;
uniform float u_max_inv;       // 1.0 / log1p(expected peak count)
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    int col = int(clamp(v_texcoord.x, 0.0, 1.0) * 511.0 + 0.5);
    vec3 log_sum = texelFetch(u_hist_sum, ivec2(col, 0), 0).rgb;
    vec3 norm = clamp(log_sum * u_max_inv, vec3(0.0), vec3(1.0));
    float y = v_texcoord.y;
    vec3 colors[3] = vec3[3](
        vec3(1.0, 0.23, 0.23),
        vec3(0.23, 1.0, 0.23),
        vec3(0.23, 0.47, 1.0)
    );
    vec3 fill = vec3(0.0);
    vec3 trace = vec3(0.0);
    float trace_width = 1.35 / u_display_size.y;
    for (int channel = 0; channel < 3; channel++) {
        float distance_to_curve = abs(y - norm[channel]);
        float line = 1.0 - smoothstep(trace_width, trace_width * 2.0, distance_to_curve);
        float under_curve = step(y, norm[channel]);
        fill += colors[channel] * under_curve * 0.15;
        trace += colors[channel] * line * 0.78;
    }
    vec3 background = vec3(0.137, 0.137, 0.149);
    vec3 col3 = clamp(background + fill + trace, vec3(0.0), vec3(1.0));
    // Graticule: vertical 0% / 50% / 100% references plus the baseline.
    float px = 1.5 / u_display_size.x;
    float g0 = 1.0 - smoothstep(0.0, px, v_texcoord.x);
    float g50 = 1.0 - smoothstep(0.0, px, abs(v_texcoord.x - 0.5));
    float g100 = 1.0 - smoothstep(0.0, px, abs(v_texcoord.x - 1.0));
    float base = 1.0 - smoothstep(0.0, 1.5 / u_display_size.y, y);
    vec3 grid = vec3(0.20) * max(max(g0, g50 * 0.7), max(g100 * 0.7, base * 0.6));
    f_color = vec4(max(col3, grid), 1.0);
}
"""

// -----------------------------------------------------------------------------
// 10. CIE 1931 chromaticity accumulation (POINTS draw).
// -----------------------------------------------------------------------------
// Transfer-function linearisation (sRGB / BT.709 / PQ / HLG / scene) and the
// primaries-dependent linear RGB -> XYZ matrix mirror the CPU widget math
// (vectorscope_widget.py). xy chromaticity maps into the [0,0.8]x[0,0.9]
// plot window; density is prescaled by 1/64 so even a single-colour frame
// stays far below the float16 limit (the display pass compensates).
#define SCOPE_CIE_ACCUM_VERTEX """
#version 330
uniform sampler2D u_sample_tex;
uniform vec2 u_sample_size;
uniform vec2 u_density_size;
uniform int u_transfer;        // 0 srgb, 1 bt709, 2 pq, 3 hlg, 4 scene
uniform int u_primaries;       // 0 bt709, 1 p3, 2 bt2020
out float v_weight;

float srgb_eotf(float c) {
    float cc = clamp(c, 0.0, 1.0);
    return cc <= 0.04045 ? cc / 12.92 : pow((cc + 0.055) / 1.055, 2.4);
}

vec3 pq_code_to_nits(vec3 code) {
    vec3 v = pow(clamp(code, 0.0, 1.0), vec3(1.0 / 78.84375));
    vec3 num = max(v - vec3(0.8359375), vec3(0.0));
    vec3 den = max(vec3(18.8515625) - vec3(18.6875) * v, vec3(0.0000001));
    return pow(num / den, vec3(1.0 / 0.1593017578125)) * vec3(10000.0);
}

vec3 hlg_code_to_scene(vec3 code) {
    vec3 e = clamp(code, 0.0, 1.0);
    vec3 low = (e * e) / vec3(3.0);
    vec3 high = (exp((e - vec3(0.55991073)) / vec3(0.17883277)) + vec3(0.28466892))
        / vec3(12.0);
    return mix(high, low, step(e, vec3(0.5)));
}

vec3 linearize(vec3 c) {
    if (u_transfer == 0) {
        return vec3(srgb_eotf(c.r), srgb_eotf(c.g), srgb_eotf(c.b));
    }
    if (u_transfer == 1) {
        return pow(clamp(c, 0.0, 1.0), vec3(2.4));
    }
    if (u_transfer == 2) {
        return pq_code_to_nits(c) / vec3(100.0);
    }
    if (u_transfer == 3) {
        return hlg_code_to_scene(c);
    }
    return max(c, vec3(0.0));
}

vec3 rgb_to_xyz(vec3 rgb) {
    if (u_primaries == 1) {
        return vec3(
            dot(rgb, vec3(0.4866, 0.2657, 0.1982)),
            dot(rgb, vec3(0.2290, 0.6917, 0.0793)),
            dot(rgb, vec3(0.0000, 0.0451, 1.0439)));
    }
    if (u_primaries == 2) {
        return vec3(
            dot(rgb, vec3(0.6370, 0.1446, 0.1689)),
            dot(rgb, vec3(0.2627, 0.6780, 0.0593)),
            dot(rgb, vec3(0.0000, 0.0281, 1.0610)));
    }
    return vec3(
        dot(rgb, vec3(0.4124, 0.3576, 0.1805)),
        dot(rgb, vec3(0.2126, 0.7152, 0.0722)),
        dot(rgb, vec3(0.0193, 0.1192, 0.9505)));
}

void main() {
    int idx = gl_VertexID;
    int sx = idx % int(u_sample_size.x);
    int sy = idx / int(u_sample_size.x);
    vec4 sample_rgba = texelFetch(u_sample_tex, ivec2(sx, sy), 0);
    vec3 lin = max(linearize(sample_rgba.rgb), vec3(0.0));
    vec3 xyz = rgb_to_xyz(lin);
    float ssum = xyz.x + xyz.y + xyz.z;
    if (ssum <= 0.000000001) {
        // Reject: park the vertex outside the clip volume. gl_PointSize
        // must stay >= 1.0 (0.0 is an INVALID_VALUE on strict drivers).
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
        gl_PointSize = 1.0;
        v_weight = 0.0;
        return;
    }
    float cx = xyz.x / ssum;
    float cy = xyz.y / ssum;
    if (cx < 0.0 || cx >= 0.8 || cy < 0.0 || cy >= 0.9) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
        gl_PointSize = 1.0;
        v_weight = 0.0;
        return;
    }
    float x_ndc = (cx / 0.8) * 2.0 - 1.0;
    float y_ndc = (cy / 0.9) * 2.0 - 1.0;
    float half_cell = 1.0 / u_density_size.x;
    x_ndc = clamp(x_ndc, -1.0 + half_cell, 1.0 - half_cell);
    y_ndc = clamp(y_ndc, -1.0 + half_cell, 1.0 - half_cell);
    gl_Position = vec4(x_ndc, y_ndc, 0.0, 1.0);
    gl_PointSize = 1.0;
    v_weight = sample_rgba.a * 0.015625;  // /64 prescale against f16 overflow
}
"""

// -----------------------------------------------------------------------------
// 11. CIE chromaticity display (full-screen quad).
// -----------------------------------------------------------------------------
// Tone-maps the density with the CPU widget's blue->cyan->white heat LUT.
// All plot chrome (spectral locus, gamut triangles, grid, axes, white point)
// is QPainter annotation drawn by CieGpuScopeWidget over the blitted texture
// — text and per-preset line styles are impractical in GLSL and the chrome
// must match the reference DisplayCAL-style plot exactly.
#define SCOPE_CIE_DISPLAY_FRAGMENT """
#version 330
uniform sampler2D u_density;
uniform float u_max_inv;       // 64.0 / expected peak (compensates prescale)
in vec2 v_texcoord;
out vec4 f_color;

vec3 heat_color(float t) {
    vec3 c1 = vec3(10.0, 60.0, 160.0) / vec3(255.0);
    vec3 c2 = vec3(60.0, 220.0, 235.0) / vec3(255.0);
    vec3 c3 = vec3(235.0, 240.0, 245.0) / vec3(255.0);
    if (t < 0.25) {
        return mix(vec3(0.0), c1, t / 0.25);
    }
    if (t < 0.6) {
        return mix(c1, c2, (t - 0.25) / 0.35);
    }
    return mix(c2, c3, (t - 0.6) / 0.4);
}

void main() {
    float density = texture(u_density, v_texcoord).r;
    float norm = clamp(density * u_max_inv, 0.0, 1.0);
    vec3 heat = heat_color(pow(norm, 0.45));
    // Subtle plot floor so the window reads as an area, not a void.
    vec3 col = max(heat, vec3(0.012));
    f_color = vec4(clamp(col, vec3(0.0), vec3(1.0)), 1.0);
}
"""

// -----------------------------------------------------------------------------
// 12. False Color / Zebra display (single pass over the source texture).
// -----------------------------------------------------------------------------
// Piecewise-constant exposure stops applied in the shader; aspect-fit letter
// box rect is computed on the render-thread caller and passed as uv scale /
// offset. Mirrors FALSE_COLOR_SDR_STOPS / FALSE_COLOR_HDR_STOPS.
#define SCOPE_FALSECOLOR_FRAGMENT """
#version 330
uniform sampler2D u_sample_tex;
uniform vec2 u_uv_scale;
uniform vec2 u_uv_offset;
uniform int u_fc_mode;         // 0 sdr, 1 hdr, 2 zebra
uniform int u_fc_transfer;     // 0 srgb, 1 bt709, 2 pq, 3 hlg, 4 scene
uniform float u_zebra_threshold;
in vec2 v_texcoord;
out vec4 f_color;

float pq_code_to_nits1(float code) {
    float v = pow(clamp(code, 0.0, 1.0), 1.0 / 78.84375);
    float num = max(v - 0.8359375, 0.0);
    float den = max(18.8515625 - 18.6875 * v, 0.0000001);
    return pow(num / den, 1.0 / 0.1593017578125) * 10000.0;
}

// BT.2408 anchor interpolation (0/25/50/75/90/100 % -> 0/1/50/203/640/1000).
float hlg_display_nits(float code) {
    float e = clamp(code, 0.0, 1.0);
    if (e < 0.25) return mix(0.0, 1.0, e / 0.25);
    if (e < 0.5) return mix(1.0, 50.0, (e - 0.25) / 0.25);
    if (e < 0.75) return mix(50.0, 203.0, (e - 0.5) / 0.25);
    if (e < 0.9) return mix(203.0, 640.0, (e - 0.75) / 0.15);
    return mix(640.0, 1000.0, (e - 0.9) / 0.1);
}

vec3 false_color_sdr(float v) {
    if (v < 0.04) return vec3(0.0, 0.0, 130.0);
    if (v < 0.10) return vec3(45.0, 0.0, 140.0);
    if (v < 0.20) return vec3(120.0, 0.0, 150.0);
    if (v < 0.30) return vec3(255.0, 0.0, 0.0);
    if (v < 0.40) return vec3(255.0, 96.0, 0.0);
    if (v < 0.50) return vec3(255.0, 220.0, 0.0);
    if (v < 0.60) return vec3(0.0, 255.0, 0.0);
    if (v < 0.70) return vec3(0.0, 200.0, 120.0);
    if (v < 0.80) return vec3(0.0, 220.0, 255.0);
    if (v < 0.90) return vec3(0.0, 120.0, 255.0);
    if (v < 1.00) return vec3(255.0, 0.0, 255.0);
    return vec3(255.0);
}

vec3 false_color_hdr(float nits) {
    if (nits < 1.0) return vec3(0.0, 0.0, 0.0);
    if (nits < 50.0) return vec3(0.0, 0.0, 139.0);
    if (nits < 100.0) return vec3(0.0, 0.0, 255.0);
    if (nits < 200.0) return vec3(0.0, 255.0, 0.0);
    if (nits < 400.0) return vec3(0.0, 200.0, 150.0);
    if (nits < 1000.0) return vec3(0.0, 255.0, 255.0);
    if (nits < 2000.0) return vec3(255.0, 255.0, 0.0);
    if (nits < 4000.0) return vec3(255.0, 140.0, 0.0);
    if (nits < 10000.0) return vec3(255.0, 0.0, 0.0);
    return vec3(255.0);
}

void main() {
    vec2 suv = v_texcoord * u_uv_scale + u_uv_offset;
    if (suv.x < 0.0 || suv.x > 1.0 || suv.y < 0.0 || suv.y > 1.0) {
        f_color = vec4(0.0, 0.0, 0.0, 1.0);
        return;
    }
    vec3 c = texture(u_sample_tex, suv).rgb;
    float luma = dot(c, vec3(0.2126, 0.7152, 0.0722));
    if (u_fc_mode == 2) {
        float gray = clamp(luma, 0.0, 1.2) / 1.2;
        vec3 col = vec3(gray);
        vec2 cell = floor(gl_FragCoord.xy / 8.0);
        float stripe = mod(cell.x + cell.y, 2.0);
        if (luma >= u_zebra_threshold && stripe < 1.0) {
            col = vec3(1.0);
        }
        f_color = vec4(col, 1.0);
        return;
    }
    if (u_fc_mode == 1) {
        float nits;
        if (u_fc_transfer == 2) {
            nits = pq_code_to_nits1(luma);
        } else if (u_fc_transfer == 3) {
            nits = hlg_display_nits(luma);
        } else {
            nits = luma * 100.0;
        }
        f_color = vec4(false_color_hdr(nits) / vec3(255.0), 1.0);
        return;
    }
    f_color = vec4(false_color_sdr(luma) / vec3(255.0), 1.0);
}
"""
