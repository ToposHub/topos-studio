// X09 true_push —— 双源 UV 重采样 GLSL 草稿（L03 接缝提案配套）。
// 现状：gpu_transition_shaders.py 只有"几何 mask × 双源原位混合"，无双源
// UV 位移通道——本段是 L03 要开的窄接缝草案（由 Mac 集成者合入）。
// 建议形态：GpuTransitionMaskSpec 新增 kind=RESAMPLE_PUSH（或 effect_mode=6
// 复用 u_direction），main() 分支按接缝选择双源各自位移后的采样：

// effect_mode == PUSH（direction=left 示意；其余方向轴/符号交换）：
{
    // 像素中心制：v_texcoord 已是像素中心插值坐标
    float coord = v_texcoord.x;                 // left/right 用 x，up/down 用 y
    float off = u_progress;                     // push 位移量（实例 curve 已缓动）
    vec3 outgoing;
    if (coord + off <= 1.0) {
        // A 段：源坐标恒在 [0,1] 内（u+p<=1）——无需边界处理
        outgoing = texture(u_left_tex, v_texcoord + vec2(off, 0.0)).rgb;
    } else {
        // B 段：源坐标 = u+p-1 ∈ (0,1]
        outgoing = texture(u_right_tex, v_texcoord + vec2(off - 1.0, 0.0)).rgb;
    }
    // 接缝 u_seam = 1.0 - u_progress；可选 u_push_soft 羽化混合（权重域）。
    // 端点精确：u_progress<=0.0 → 纯 u_left_tex；>=1.0 → 纯 u_right_tex
    //（与 CPU 核一致；main() 现有端点分支复用）。
    // mask 语义：本 kind 无 mask（spec.mask 走常数 1，混合由采样选择完成）。
    fragColor = vec4(outgoing, 1.0);
}

// right 方向：A 段 u-p>=0 采样 (u-p)，B 段采样 (u-p+1)；接缝 = u_progress。
// up/down：coord 换 v_texcoord.y，位移在 y 分量。
