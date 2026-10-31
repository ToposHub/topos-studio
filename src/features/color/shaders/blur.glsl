#version 330

uniform sampler2D u_tex;
uniform vec2 u_direction; // (1.0/w, 0.0) or (0.0, 1.0/h)
uniform float u_radius;

in vec2 v_texcoord;
out float f_result;

float gaussian(float x, float sigma) {
    return exp(-(x * x) / (2.0 * sigma * sigma));
}

void main() {
    float center_val = texture(u_tex, v_texcoord).r;
    if (u_radius <= 0.0) {
        f_result = center_val;
        return;
    }

    float sigma = max(u_radius * 0.5, 0.1);
    float total_w = gaussian(0.0, sigma);
    float result = center_val * total_w;

    int r = int(ceil(u_radius));
    for (int i = 1; i <= r; i++) {
        float w = gaussian(float(i), sigma);
        result += texture(u_tex, v_texcoord + u_direction * float(i)).r * w;
        result += texture(u_tex, v_texcoord - u_direction * float(i)).r * w;
        total_w += 2.0 * w;
    }

    f_result = result / total_w;
}
