"""Primary grade GLSL shared by Edit preview and timeline export.

Keep intermediate HDR/negative RGB through hue rotation; only the delivery
fragment quantizes to its target range. CPU numerical authority is GradingMath.
"""

PRIMARY_GRADE_UNIFORMS = """
uniform vec3 u_lift;
uniform vec3 u_gamma;
uniform vec3 u_gain;
uniform float u_exposure;
uniform float u_temp;
uniform float u_tint;
uniform float u_contrast;
uniform float u_shadow;
uniform float u_highlights;
uniform float u_saturation;
uniform float u_vibrance;
uniform float u_hue_shift;
"""

PRIMARY_GRADE_BODY = """
vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + 1.0e-10)), d / (q.x + 1.0e-10), q.x);
}

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

vec3 apply_primary_grade(vec3 color) {
    float exp_stops = (u_exposure / 100.0) * (u_exposure / 100.0) * 6.0;
    if (u_exposure < 0.0) exp_stops = -exp_stops;
    color *= pow(2.0, exp_stops);
    color *= vec3(1.0 + u_temp / 200.0, 1.0 - u_tint / 200.0, 1.0 - u_temp / 200.0);

    if (u_contrast != 0.0) {
        color = sign(color) * pow(max(abs(color), vec3(1.0e-10)) / 0.18, vec3(1.0 + u_contrast / 100.0)) * 0.18;
    }

    vec3 lift_adjustment = (u_lift - 1.0) * 0.2;
    color = color * u_gain * (1.0 - lift_adjustment) + lift_adjustment;
    vec3 gamma_power = exp2(-log2(max(u_gamma, vec3(0.01))));
    for (int i = 0; i < 3; ++i) {
        if (color[i] > 0.0 && color[i] <= 1.0) {
            color[i] = pow(color[i], gamma_power[i]);
        } else if (color[i] > 1.0) {
            color[i] = gamma_power[i] * (color[i] - 1.0) + 1.0;
        } else {
            color[i] *= gamma_power[i];
        }
    }

    // RENDER-02/R31：暗部保护——与 Color 权威 GRADING_SNIPPET /
    // CPUGradingMath 逐条一致（gamma 之后、shadows 之前；luma<0 的
    // 当前像素做 <-0.5 软压缩与通道平衡）。此前导出复制体缺失该级，
    // 同工程 CPU/Color/导出三路颜色分叉（审计实测 u8 差 179）。
    float luma_post_grading = dot(color, vec3(0.299, 0.587, 0.114));
    if (luma_post_grading < 0.0) {
        for (int i = 0; i < 3; ++i) {
            float x = color[i];
            if (x < -0.5) {
                x = -0.5 - (abs(x + 0.5) * 0.1);
            }
            color[i] = x;
        }
        float min_channel = min(min(color.r, color.g), color.b);
        float max_channel = max(max(color.r, color.g), color.b);
        if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
            float balance_factor = clamp(abs(min_channel) * 0.25, 0.0, 0.15);
            color = mix(color, vec3(luma_post_grading), balance_factor);
        }
    }

    if (u_shadow != 0.0) {
        float shadow_factor = 1.0 + u_shadow / 100.0;
        if (shadow_factor > 0.0) {
            vec3 bounded = clamp(color, 0.0, 1.0);
            color = pow(bounded, vec3(1.0 / shadow_factor)) + (color - bounded);
        }
    }
    if (u_highlights != 0.0) {
        float max_channel = max(max(color.r, color.g), color.b);
        if (max_channel > 0.1) {
            float mapped = max_channel;
            if (u_highlights < 0.0) {
                float k = abs(u_highlights / 100.0) * 2.0;
                mapped = 0.1 + (max_channel - 0.1) / (1.0 + k * (max_channel - 0.1));
            } else {
                mapped = 0.1 + (max_channel - 0.1) * (1.0 + (u_highlights / 100.0) * 0.5);
            }
            color *= mapped / max_channel;
        }
    }

    float gray = dot(color, vec3(0.299, 0.587, 0.114));
    if (u_vibrance != 0.0) {
        float saturation_extent = max(color.r, max(color.g, color.b)) - gray;
        float boost = (u_vibrance / 100.0) * (1.0 - clamp(saturation_extent, 0.0, 1.0));
        color = mix(color, vec3(gray), -boost);
    }
    if (u_hue_shift != 0.0) {
        vec3 hsv = rgb2hsv(color);
        hsv.x = fract(hsv.x + u_hue_shift / 360.0);
        color = hsv2rgb(hsv);
    }
    // R31：saturation 用 post-hue 灰度（CPU 参考在 hue 后重算 gray，
    // 此前复用 hue 前的 gray 造成 hue+sat 组合分叉）
    gray = dot(color, vec3(0.299, 0.587, 0.114));
    if (u_saturation != 0.0) {
        color = mix(vec3(gray), color, 1.0 + u_saturation / 100.0);
    }
    return color;
}
"""

PRIMARY_GRADE_FRAGMENT = (
    "#version 330\n"
    "uniform sampler2D u_input_tex;\n"
    + PRIMARY_GRADE_UNIFORMS
    + "in vec2 v_texcoord;\nout vec4 f_color;\n"
    + PRIMARY_GRADE_BODY
    + """
void main() {
    vec4 source = texture(u_input_tex, v_texcoord);
    f_color = vec4(clamp(apply_primary_grade(source.rgb), 0.0, 1.0), source.a);
}
"""
)
