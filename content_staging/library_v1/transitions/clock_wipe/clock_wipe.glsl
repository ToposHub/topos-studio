// X01 clock_wipe —— GLSL procedural_mask 扩展草稿（L03 接缝提案配套）。
// 目标位置：src/features/edit/core/gpu_transition_shaders.py 的
// proceduralural_mask() 新增 kind 分支（由 Mac 集成者合入；本文件是草案）。
// uniform 约定沿用该文件现有块：u_progress / u_transition_kind /
// u_center（新增 vec2）/ u_angle（复用为 start_angle 度）/ u_reverse
//（复用为逆时针开关）/ u_softness（复用为归一角宽）。
// CPU/GPU 同构口径见 transitions/clock_wipe/clock_wipe_core.py。

// kind == CLOCK_WIPE（建议常量 5）：
{
    vec2 ts = vec2(float(u_resolution.x), float(u_resolution.y));
    vec2 uv_px = v_texcoord;                       // 与现有 wipe 分支同源
    float aspect = ts.x / ts.y;
    vec2 c = u_center;                             // 归一中心
    float dx = (uv_px.x - c.x) * aspect;
    float dy = c.y - uv_px.y;                      // 北=+y
    float theta = atan(dx, dy);                    // 北 0，顺时针正
    float start = radians(u_angle);                // start_angle（度）
    float delta = theta - start;
    if (u_reverse != 0) { delta = -delta; }
    float rel = mod(delta, 6.283185307179586);     // 2π
    float sweep = u_progress * 6.283185307179586;
    float feather = max(u_softness * 6.283185307179586, 1e-6);
    float m = clamp((sweep - rel) / feather, 0.0, 1.0);
    // 端点精确（与 CPU 核一致）：main() 在 u_progress<=0.0/>=1.0 时
    // 走现有常数分支（0.0 / 1.0），不经本段。
    mask_value = m;
}
