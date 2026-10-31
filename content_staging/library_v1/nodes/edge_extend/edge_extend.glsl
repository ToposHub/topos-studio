// C17 lib1_edge_extend —— GPU shader 草稿（L04 dispatch 提案配套）。
// 形态：composite shaders.py 新增 _LIB1_EDGE_EXTEND_FRAGMENT（Mac 集成者合入）。
//
// 生产形态 = 每轮扩张一个 pass（与 references/edge_extend_ref.py 的迭代语义
// 严格同构；不存在单 pass 等价形式——分层前景下多轮均值传播不可折叠）：
//   evaluator 对 u_iterations 轮做乒乓 FBO，每轮本 shader 执行一次：
//     非 solid 像素若有 solid 4-邻居 → RGB 置为 solid 邻居均值并转 solid
//   全部轮次完成后由 dispatch 尾段按 u_output_mode 决定 alpha/premult。
// 循环上限为编译期常量（4 邻居常量展开）；迭代轮数由调度器控制（非 shader 循环）。

uniform sampler2D u_input_tex;   // 乒乓输入：rgb=当前延伸态，a=solid 标志(0/1)
uniform int u_pass;              // 0=rgb 传播 pass；1=output 组装 pass
uniform int u_output_mode;       // 0=color_only, 1=premultiplied（仅 pass 1 用）

const vec3 _UNUSED = vec3(0.0);

void main() {
    ivec2 ts = textureSize(u_input_tex, 0);
    ivec2 p = ivec2(gl_FragCoord.xy);
    ivec2 cl = clamp(p, ivec2(0), ts - ivec2(1));
    vec4 src = texelFetch(u_input_tex, cl, 0);

    if (u_pass == 0) {
        // 一轮扩张：solid 标志走 alpha 通道（0=未实，1=已实）
        if (src.a >= 0.5) {
            fragColor = src;   // 已实像素不动
            return;
        }
        vec3 sum = vec3(0.0);
        float cnt = 0.0;
        // 4-邻居常量展开（无循环，红线安全）
        vec4 n1 = texelFetch(u_input_tex, clamp(p + ivec2(-1, 0), ivec2(0), ts - ivec2(1)), 0);
        vec4 n2 = texelFetch(u_input_tex, clamp(p + ivec2( 1, 0), ivec2(0), ts - ivec2(1)), 0);
        vec4 n3 = texelFetch(u_input_tex, clamp(p + ivec2( 0,-1), ivec2(0), ts - ivec2(1)), 0);
        vec4 n4 = texelFetch(u_input_tex, clamp(p + ivec2( 0, 1), ivec2(0), ts - ivec2(1)), 0);
        if (n1.a >= 0.5) { sum += n1.rgb; cnt += 1.0; }
        if (n2.a >= 0.5) { sum += n2.rgb; cnt += 1.0; }
        if (n3.a >= 0.5) { sum += n3.rgb; cnt += 1.0; }
        if (n4.a >= 0.5) { sum += n4.rgb; cnt += 1.0; }
        if (cnt > 0.5) {
            fragColor = vec4(sum / cnt, 1.0);   // 本轮转实
        } else {
            fragColor = src;                     // 下一轮继续等待
        }
        return;
    }

    // output pass：恢复原 alpha（solid 标志通道由调度器另存的原 alpha 替换）
    float orig_a = src.a;   // 生产版：原 alpha 存于第二张纹理/通道，此处示意
    vec3 rgb = src.rgb;
    fragColor = (u_output_mode == 1) ? vec4(rgb * orig_a, orig_a) : vec4(rgb, orig_a);
}
