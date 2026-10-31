#version 330

uniform sampler2D u_input_tex;
in vec2 v_texcoord;
out float f_matte;

uniform bool u_h_enabled;
uniform float u_h_center;
uniform float u_h_width;
uniform float u_h_soft;

uniform bool u_s_enabled;
uniform float u_s_low;
uniform float u_s_high;
uniform float u_s_s_low;
uniform float u_s_s_high;

uniform bool u_l_enabled;
uniform float u_l_low;
uniform float u_l_high;
uniform float u_l_s_low;
uniform float u_l_s_high;

uniform bool u_invert;

vec3 rgb_to_hsl(vec3 color) {
    vec3 c = max(color, vec3(0.0));
    float max_orig = max(max(c.r, c.g), c.b);
    if (max_orig > 1.0) c /= max_orig;
    
    float min_val = min(min(c.r, c.g), c.b);
    float max_val = max(max(c.r, c.g), c.b);
    float delta = max_val - min_val;
    float h = 0.0;
    float s = 0.0;
    float l = (max_val + min_val) / 2.0;

    if (delta > 0.00001) {
        s = l < 0.5 ? delta / max((max_val + min_val), 0.00001) : delta / max((2.0 - max_val - min_val), 0.00001);
        if (c.r == max_val) h = (c.g - c.b) / delta + (c.g < c.b ? 6.0 : 0.0);
        else if (c.g == max_val) h = (c.b - c.r) / delta + 2.0;
        else h = (c.r - c.g) / delta + 4.0;
        h /= 6.0;
    }
    if (max_orig > 1.0) l *= max_orig;
    return vec3(h, s, l);
}

void main() {
    vec3 color = texture(u_input_tex, v_texcoord).rgb;
    vec3 hsl = rgb_to_hsl(color);
    
    float matte = 1.0;

    if (u_h_enabled) {
        float h = fract(hsl.x + 0.5);
        float dist = abs(h - u_h_center);
        if (dist > 0.5) dist = 1.0 - dist;
        float half_w = u_h_width * 0.5;
        matte *= 1.0 - smoothstep(half_w, half_w + max(u_h_soft, 0.0001), dist);
    }

    if (u_s_enabled) {
        float s = hsl.y;
        float s_matte = 1.0;
        if (u_s_low > 0.0 || u_s_s_low > 0.0) s_matte *= smoothstep(u_s_low - max(u_s_s_low, 0.0001), u_s_low, s);
        if (u_s_high < 1.0 || u_s_s_high > 0.0) s_matte *= (1.0 - smoothstep(u_s_high, u_s_high + max(u_s_s_high, 0.0001), s));
        matte *= s_matte;
    }

    if (u_l_enabled) {
        float l = hsl.z;
        float l_matte = 1.0;
        if (u_l_low > 0.0 || u_l_s_low > 0.0) l_matte *= smoothstep(u_l_low - max(u_l_s_low, 0.0001), u_l_low, l);
        if (u_l_high < 1.0 || u_l_s_high > 0.0) l_matte *= (1.0 - smoothstep(u_l_high, u_l_high + max(u_l_s_high, 0.0001), l));
        matte *= l_matte;
    }

    if (u_invert) matte = 1.0 - matte;
    f_matte = clamp(matte, 0.0, 1.0);
}
