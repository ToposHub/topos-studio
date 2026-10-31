#version 330

uniform sampler2D u_tex;
uniform float u_radius;
uniform int u_operation;  // 0 = Min (Erode), 1 = Max (Dilate)
uniform vec2 u_texel_size;
uniform int u_direction;  // 0 = horizontal pass, 1 = vertical pass

in vec2 v_texcoord;
out float f_result;

// Box morphology (min/max) is exactly separable: running the horizontal and
// vertical passes back-to-back equals the full square-window scan, at
// 2*(2r+1) taps instead of (2r+1)^2. The renderer invokes this shader twice
// per operation (H then V) via _apply_matte_postprocess.
void main() {
    float result = texture(u_tex, v_texcoord).r;
    int r = int(ceil(u_radius));

    if (r <= 0) {
        f_result = result;
        return;
    }

    vec2 dir = (u_direction == 0) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);

    for (int i = -r; i <= r; i++) {
        if (i == 0) continue;
        float val = texture(u_tex, v_texcoord + dir * float(i) * u_texel_size).r;
        if (u_operation == 0) {
            result = min(result, val);
        } else {
            result = max(result, val);
        }
    }

    f_result = result;
}
