// A1: 共享图层混合公式（GPU 预览 + GPU 导出 include）。
// 与 src/features/edit/core/blend_modes.py 的 NumPy 实现保持公式一致
// （修改任何一侧必须同步另一侧，验收靠公式数值表 + PSNR 对比）。
// 约定：输入输出均为 [0,1] 非预乘 RGB；mode 编号与 ClipBlendMode.code 一致。

float blend_lum(vec3 c) {
    return dot(c, vec3(0.3, 0.59, 0.11));
}

vec3 blend_clip_color(vec3 c) {
    float l = blend_lum(c);
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

vec3 blend_set_lum(vec3 c, float l) {
    return blend_clip_color(c + (l - blend_lum(c)));
}

float blend_sat(vec3 c) {
    return max(c.r, max(c.g, c.b)) - min(c.r, min(c.g, c.b));
}

vec3 blend_set_sat(vec3 c, float s) {
    // 按分量排序：min / mid / max，重建后按原位放回（无循环实现）
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    float md = c.r + c.g + c.b - mx - mn;
    float denom = max(mx - mn, 1e-6);
    vec3 sorted_new = vec3(0.0, (mx > mn) ? (md - mn) * s / denom : 0.0, s);
    bvec3 is_max = equal(c, vec3(mx));
    bvec3 is_min = equal(c, vec3(mn));
    vec3 out_c;
    out_c.r = is_max.r ? sorted_new.b : (is_min.r ? sorted_new.r : sorted_new.g);
    out_c.g = is_max.g ? sorted_new.b : (is_min.g ? sorted_new.r : sorted_new.g);
    out_c.b = is_max.b ? sorted_new.b : (is_min.b ? sorted_new.r : sorted_new.g);
    return out_c;
}

vec3 blend_channel(vec3 src, vec3 dst, int mode) {
    if (mode == 1) {           // MULTIPLY
        return src * dst;
    } else if (mode == 2) {    // SCREEN
        return src + dst - src * dst;
    } else if (mode == 3) {    // OVERLAY
        return mix(2.0 * src * dst, 1.0 - 2.0 * (1.0 - src) * (1.0 - dst),
                   step(0.5, dst));
    } else if (mode == 4) {    // DARKEN
        return min(src, dst);
    } else if (mode == 5) {    // LIGHTEN
        return max(src, dst);
    } else if (mode == 6) {    // COLOR_DODGE
        vec3 dodged = min(vec3(1.0), dst / max(1.0 - src, vec3(1e-6)));
        return mix(dodged, vec3(1.0), step(1.0 - 1e-6, src));
    } else if (mode == 7) {    // COLOR_BURN
        vec3 burned = max(vec3(0.0), 1.0 - (1.0 - dst) / max(src, vec3(1e-6)));
        return mix(burned, vec3(0.0), 1.0 - step(1e-6, src));
    } else if (mode == 8) {    // HARD_LIGHT
        return mix(2.0 * src * dst, 1.0 - 2.0 * (1.0 - src) * (1.0 - dst),
                   step(0.5, src));
    } else if (mode == 9) {    // SOFT_LIGHT
        vec3 g_low = ((16.0 * dst - 12.0) * dst + 4.0) * dst;
        vec3 g_high = sqrt(max(dst, vec3(0.0)));
        vec3 g = mix(g_high, g_low, step(dst, vec3(0.25)));
        return mix(dst - (1.0 - 2.0 * src) * dst * (1.0 - dst),
                   dst + (2.0 * src - 1.0) * (g - dst),
                   step(0.5, src));
    } else if (mode == 10) {   // DIFFERENCE
        return abs(dst - src);
    } else if (mode == 11) {   // EXCLUSION
        return src + dst - 2.0 * src * dst;
    } else if (mode == 12) {   // HUE
        return blend_set_lum(blend_set_sat(src, blend_sat(dst)), blend_lum(dst));
    } else if (mode == 13) {   // SATURATION
        return blend_set_lum(blend_set_sat(dst, blend_sat(src)), blend_lum(dst));
    } else if (mode == 14) {   // COLOR
        return blend_set_lum(src, blend_lum(dst));
    } else if (mode == 15) {   // LUMINOSITY
        return blend_set_lum(dst, blend_lum(src));
    }
    return src;                // 0 / 未知 → NORMAL
}
