#version 330

// Qualifier matte denoise: contrast-restore pass.
// The renderer first blurs the matte (small radius, see
// _apply_matte_postprocess), which smooths chroma-noise speckles but also
// softens the edge. This pass re-sharpens the transition around 0.5 with
// smoothstep: noise speckles that blurred toward grey collapse toward 0 or 1,
// while true edges regain their original steepness.

uniform sampler2D u_tex;
uniform float u_strength;  // 0.0-1.0; higher = harder edge restore

in vec2 v_texcoord;
out float f_result;

void main() {
    float m = texture(u_tex, v_texcoord).r;
    if (u_strength <= 0.0) {
        f_result = m;
        return;
    }
    // strength 0 -> k=0.45 (nearly identity), strength 1 -> k=0.05 (hard edge)
    float k = mix(0.45, 0.05, clamp(u_strength, 0.0, 1.0));
    f_result = smoothstep(0.5 - k, 0.5 + k, m);
}
