# batch-053 审查报告 — src/features/composite/core/shaders.py

批次：batch-053 [P1] 主题 src/features/composite/core//
编号区间：AUD-5300–AUD-5399（实际使用 AUD-5300–AUD-5312）
说明：本文件为纯 GLSL 源码字符串常量模块（仅 `_DEPTH_INPUT_GLSL` 字符串拼接，无 Python 函数/类），审查聚焦 GLSL·HDR 正确性、计算着色器共享内存与屏障语义、性能模式。所有行号均经通读核对；`_BLUR_*_COMPUTE_SHARED`/`_ERODE_DILATE_COMPUTE_SHARED` 的使用路径已在 `src/features/composite/render/compute_shader_manager.py:454-456,573-574` 交叉确认（radius>32 / size>16 时选中）。

## src/features/composite/core/shaders.py (行数: 7,113)

### 发现

- [AUD-5300] [Critical] 共享内存版模糊计算着色器的 padding 协作装载只填充每 16 个槽位中的一个，卷积读取未初始化共享内存
  - 位置: src/features/composite/core/shaders.py:3797-3832（H 左/右装载）、3841-3846（H 读取）、3895-3930（V 装载）、3937-3942（V 读取）
  - 证据:
    ```glsl
    // H pass 左 padding（3797-3800）
    for (int k = 1; k <= kernel_size; k++) {
        int load_idx = shared_idx - k;
        if (local_id.x == (k - 1) % 16) {
            int load_x = base_x + local_id.x - k;
    // H pass 卷积读取（3841-3844）
    for (int i = -kernel_size; i <= kernel_size; i++) {
        float weight = gaussian_weight(float(i), sigma);
        int sample_idx = shared_idx + i;
        color += s_row[sample_idx] * weight;
    ```
  - 影响: 守卫 `local_id.x == (k-1)%16` 使 k=1..16 全部写同一槽 `kernel-1`（k=17..32 写 `kernel-17`，依此类推），左侧只填 {kernel-1, kernel-17, kernel-33, kernel-49}，右侧只填 {kernel+16, kernel+32, kernel+48, kernel+64}；中间槽位从未写入。GLSL shared 数组内容未定义，卷积窗口（shared_idx±kernel）对这些槽的读取输出垃圾像素。compute_shader_manager.py:454 在 radius>32 时强制走此路径（kernel_size≥49），大半径模糊结果必然损坏；即便 radius 较小（kernel_size≥2）该路径也存在未初始化读取。V pass（s_column）为同一模式的复制。
  - 建议: 改为按线程跨步装载：`for (int k = local_id.x + 1; k <= kernel_size; k += 16) { s_row[shared_idx - k] = load(base_x - k); ... }`（右/上/下同构），保证 [kernel-kernel, kernel+15+kernel] 全部被写；或直接弃用 SHARED 变体、统一走已验证正确的 SSBO 版本。
  - 置信度: 高

- [AUD-5301] [Critical] _ERODE_DILATE_COMPUTE_SHARED 仅装载 4 个角块，padding 边条区域未初始化即被形态学核读取
  - 位置: src/features/composite/core/shaders.py:3992-4022（装载）、4029-4031（读取）
  - 证据:
    ```glsl
    // 仅角块（例：左上，3992-3997）
    if (local_id.x < padding && local_id.y < padding) {
        int load_x = base_pos.x + local_id.x - padding;
        int load_y = base_pos.y + local_id.y - padding;
        ...
        s_block[local_id.y][local_id.x] = texture(u_input, load_uv);
    }
    // 卷积读取（4029-4031）
    for (int dy = -half_size; dy <= half_size; dy++) {
        for (int dx = -half_size; dx <= half_size; dx++) {
            vec4 sample_val = s_block[sy + dy][sx + dx];
    ```
  - 影响: 卷积需要的窗口是 [0, 15+2*half]^2，中心 16×16 由各线程自装载，4 个角块由角线程装载，但上/下/左/右四条边条（如行 0..padding-1 × 列 padding..padding+15）从未写入。half_size=1 时（size=2/3）线程 (x,0) 读 s_block[sy-1][sx] 即命中未装载的顶条。compute_shader_manager.py:573-574 在 size>16 时选中此变体，腐蚀/膨胀输出为未定义内容（渲染错误）。
  - 建议: 用跨步循环让 256 个线程合作覆盖整个 (16+2p)×(16+2p) 窗口（每线程 ceil((16+2p)^2/256) 个位置），barrier 后再卷积；或同样回退到非 SHARED 的 _ERODE_DILATE_COMPUTE。
  - 置信度: 高

- [AUD-5302] [Critical] _ERODE_DILATE_COMPUTE_SHARED 声明约 210KB 共享内存，超出 GL 保证上限与常见硬件实际上限，程序无法建立
  - 位置: src/features/composite/core/shaders.py:3962
  - 证据:
    ```glsl
    // Shared memory: 16x16 block + kernel padding
    shared vec4 s_block[16 + 100][16 + 100];  // +50 on each side
    ```
  - 影响: 116×116×16B = 215,296 字节。OpenGL 4.3 对 MAX_COMPUTE_SHARED_MEMORY_SIZE 的最低保证仅 32KB，常见桌面 GPU 每工作组可用量为 48–100/128KB（Intel 更低）。此着色器在绝大多数硬件上链接/启动失败，size>16 的腐蚀/膨胀计算路径直接不可用（节点报错或回退）。
  - 建议: 用 tiled 分块（halo 分批装载、多次 barrier）把每工作组共享内存压到 ≤32KB；或删除该变体统一使用纹理采样的 _ERODE_DILATE_COMPUTE（MAX_RADIUS=50 时 101×101 次采样在多数尺寸下可接受）。
  - 置信度: 高

- [AUD-5304] [Critical] _ERODE_DILATE_FRAGMENT 的 reflect 边界模式公式把画面内坐标整体 180° 翻转
  - 位置: src/features/composite/core/shaders.py:2486
  - 证据:
    ```glsl
    // Edge mode handling
    if (u_edge_mode == 0) {
        sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));
    } else if (u_edge_mode == 1) {
        sample_uv = abs(mod(sample_uv, 2.0) - 1.0);
    } else {
        sample_uv = fract(sample_uv);
    }
    ```
  - 影响: `abs(mod(uv,2)-1)` 对 uv∈[0,1] 映射为 1-uv（整个采样邻域绕 (0.5,0.5) 翻转，中心像素读到图像对角位置）；越界值也绕 0.5 反射而非贴边镜像（uv=-0.1→0.9，正确应为 0.1）。本文件 `_LENS_DISTORTION_FRAGMENT`/`_MESH_WARP_FRAGMENT` 已用正确形式并注释说明（6620-6623：“abs(mod(uv,2)-1) would flip the whole in-frame image”）。选 reflect 模式时腐蚀/膨胀结果完全错误。
  - 建议: 改为 `sample_uv = 1.0 - abs(mod(sample_uv, 2.0) - 1.0);`（与 6623 行一致）。
  - 置信度: 高

- [AUD-5303] [Major] 三个共享内存计算着色器在 barrier() 之前提前 return，分叉屏障属未定义行为
  - 位置: src/features/composite/core/shaders.py:3773-3775+3835（BLUR_H_SHARED）、3876-3878+3932（BLUR_V_SHARED）、3970-3972+4024（ERODE_DILATE_SHARED）
  - 证据:
    ```glsl
    if (global_id.x >= int(tex_size.x) || global_id.y >= int(tex_size.y)) {
        return;
    }
    ...（装载）...
    // Synchronize: all threads must finish loading before convolution
    barrier();
    ```
  - 影响: GLSL 规范要求 barrier() 由工作组内全部 invocation 到达。图像宽/高非 16 整数倍时，边缘工作组的部分线程提前 return、其余线程执行 barrier()——规范级未定义行为，在部分驱动上表现为挂起或装载/卷积乱序（崩溃风险 + 渲染错误）。
  - 建议: 越界线程不 return，改为置 `bool active = in_bounds;`，inactive 线程仍参与装载与 barrier，仅跳过最后的 imageStore。
  - 置信度: 高

- [AUD-5305] [Major] _RAMP_FRAGMENT 的 mirror 延展模式是周期 1 三角波，[0,1] 内的 t 也被反转
  - 位置: src/features/composite/core/shaders.py:4731-4738
  - 证据:
    ```glsl
    } else {
        // Mirror
        float t_mod = fract(t);
        if (t_mod > 0.5) {
            return 1.0 - t_mod;
        }
        return t_mod;
    }
    ```
  - 影响: 镜像延展应只在 t 越界时反射（[0,1] 内恒等）；此实现把 t=0.75 映射为 0.25、t=1.25 映射为 0.25（正确为 0.75），渐变在基本区间内先升后降，选 mirror 的 linear/radial/angular/diamond 渐变形状错误。同文件 _CROP_FRAGMENT 的 mirror_uv（1513-1517）与 6623 行均为正确的周期 2 形式。
  - 建议: 改为 `return 1.0 - abs(mod(t, 2.0) - 1.0);`。
  - 置信度: 高

- [AUD-5306] [Major] 六个着色器仍保留 S3-A 已在混合着色器中废除的歧义 mask 启发式 `m.a > 0 ? m.a : m.r`
  - 位置: src/features/composite/core/shaders.py:1851（_COLOR_CORRECT）、2069（_SET_ALPHA）、2172（_HUECORRECT）、3119（_FUSED_COLOR）、3537（_LUT3D）、4116（_DEEP_MERGE）
  - 证据:
    ```glsl
    vec4 mask_sample = texture(u_mask, v_uv);
    float mask_val = mask_sample.a > 0.0 ? mask_sample.a : mask_sample.r;
    ```
    对照本文件 169-171 行注释：“S3-A: ... replaces the old ambiguous 'm.a > 0 ? m.a : m.r' heuristic”（_BLEND_FRAGMENT 已改为 u_mask_channel 显式选择）。
  - 影响: 对 a=0 但 RGB 有值的 mask（如预乘 matte 或纯灰度图载入 RGBA），alpha 被忽略、改用红色通道；对预乘 mask 的 a 通道与 RGB 含义不一致时选错调制量，六个节点的遮罩行为与 merge 节点不一致且不可预期。
  - 建议: 与 S3-A 对齐：增加 `u_mask_channel`/`u_has_mask` uniform 并显式选通道（参照 _BLEND_FRAGMENT 173-190 行实现）。
  - 置信度: 中

- [AUD-5307] [Major] _FUSED_COLOR 的 grade 阶段算子顺序与 _GRADE 不一致（且 _FUSED 缺少扩展参数），融合与逐节点渲染结果分叉
  - 位置: src/features/composite/core/shaders.py:3141-3152（_FUSED_COLOR）对照 919-929（_GRADE）；_FUSED 缺参见 2822-2825
  - 证据:
    ```glsl
    // _FUSED_COLOR（3144-3152）：multiply/add 在 gain 之前
    c = (c - u_grade_blackpoint) / max(u_grade_whitepoint - u_grade_blackpoint, 0.0001);
    c = c * u_grade_multiply + u_grade_add;
    c = c * u_grade_gain[i] + u_grade_offset[i];
    c = pow(max(c, 0.0001), 1.0 / max(u_grade_gamma[i], 0.001));
    c = c + u_grade_lift[i];
    // _GRADE（924-929）：multiply/add 在 lift 之后
    g = g * u_gain[i] + u_offset[i];
    g = pow(max(g, 0.0001), 1.0 / max(u_gamma[i], 0.001));
    g = g + u_lift[i];
    ...
    result = result * u_multiply + u_add;
    ```
  - 影响: multiply≠1 或 add≠0 且 gain/gamma/lift 非恒等时，融合 mega-shader 输出与逐节点管线不同（缓存/融合渲染与直接渲染不一致）；另 _FUSED（非 COLOR 版）完全没有 blackpoint/whitepoint/multiply/add uniform，若渲染器按 grade 全参数集设置 uniform 将再次触发 903-905 行注释所述的 KeyError 丢图问题。
  - 建议: 统一算子顺序（blackpoint→gain/offset→gamma→lift→multiply/add，与 _GRADE 一致）；为 _FUSED 补齐扩展参数或在融合判定中排除带这些参数的 grade 节点。
  - 置信度: 中

- [AUD-5308] [Suggestion] _COLOR_CORRECT_FRAGMENT 的 alpha 通道选择器是无操作（mix 两端相同）
  - 位置: src/features/composite/core/shaders.py:1907
  - 证据:
    ```glsl
    float alpha = mix(color.a, color.a, u_channel_selector.a);
    ```
  - 影响: `u_channel_selector.a` 对输出无任何影响（该着色器本就不修改 alpha），死表达式；UI 若暴露“alpha 通道参与校正”选项则形同虚设。
  - 建议: 删除该行直接用 `color.a`，或实现真正的 alpha 校正门控并同步 _FUSED_COLOR 第 4 阶段。
  - 置信度: 高

- [AUD-5309] [Suggestion] _CS_TRANSFORM 每像素调用 inverse() 求常量矩阵的逆
  - 位置: src/features/composite/core/shaders.py:1762-1763
  - 证据:
    ```glsl
    if (u_primaries == 1) return inverse(BT709_TO_BT2020) * c;  // BT.2020 -> BT.709
    if (u_primaries == 2) return inverse(BT709_TO_P3D65) * c;   // P3 D65 -> BT.709
    ```
  - 影响: 逆矩阵在每个片元求值（4K 约 830 万像素/遍），而 AP1 方向已示范应预置两个常量矩阵（1753-1756 行 AP1_TO_BT709）。纯浪费的每像素开销。
  - 建议: 增加 `const mat3 BT2020_TO_BT709 = ...; const mat3 P3D65_TO_BT709 = ...;` 常量并直接相乘。
  - 置信度: 高

- [AUD-5310] [Suggestion] _NOISE_FRAGMENT 三个输出通道分支结果完全相同，u_output_channels 无实际作用
  - 位置: src/features/composite/core/shaders.py:4677-4686
  - 证据:
    ```glsl
    if (u_output_channels == 1) {
        // Grayscale output
        fragColor = vec4(noise_val, noise_val, noise_val, 1.0);
    } else if (u_output_channels == 3) {
        // RGB output (same value in all channels)
        fragColor = vec4(noise_val, noise_val, noise_val, 1.0);
    } else {
        // RGBA output
        fragColor = vec4(noise_val, noise_val, noise_val, 1.0);
    }
    ```
  - 影响: 灰度/RGB/RGBA 模式输出一致，参数是死配置；若 RGB 模式本意是每通道去相关噪声（如 shuffle 配合），功能缺失。
  - 建议: RGB/RGBA 模式用不同 seed 或坐标偏移生成通道独立噪声，或删除该 uniform 分支。
  - 置信度: 高（分支相同的事实）；中（是否有意为之）

- [AUD-5311] [Suggestion] 多个片元着色器仍使用“静态最大边界 + continue”循环，未跟进 T-P1-21 的动态边界改造
  - 位置: src/features/composite/core/shaders.py:417-418 与 451-452（_BLUR_H/_V_FRAGMENT）、2412-2415（_SHARPEN）、2474-2477（_ERODE_DILATE_FRAGMENT）、3606-3607 与 3662-3663（_BLUR_*_COMPUTE_SSBO）、5683-5689（_DENOISE）、674-677（_BILATERAL）
  - 证据:
    ```glsl
    for (int y = -32; y <= 32; y++) {        // _SHARPEN 2412
        if (abs(y) > kernel_size) continue;
    ...
    for (int y = -MAX_RADIUS; y <= MAX_RADIUS; y++) {   // _ERODE_DILATE 2474
        if (abs(y) > half_size) continue;
    ```
  - 影响: 与 T-P1-21 注释（519-521 行：AMD 上静态循环不因 continue 跳过成本，radius 12 时约 5 倍浪费）描述的已修复模式同源。_SHARPEN 最坏 65×65=4225 次迭代、_ERODE_DILATE 101×101=10201 次，小参数时绝大多数迭代空转，4K 实时基准下为可量化的性能损失。
  - 建议: 按 T-P1-21 模式改为 `for (int y = -kernel_size; y <= kernel_size; y++)` 动态边界（GL 3.30+ 支持），采样集不变。
  - 置信度: 高

- [AUD-5312] [Suggestion] 计算着色器版模糊仅处理 edge_policy 0/1，mirror(2) 被静默降级且两条路径行为不一致
  - 位置: src/features/composite/core/shaders.py:3615-3619（SSBO H）、3670-3674（SSBO V）、3803-3807/3821-3825（SHARED H）
  - 证据:
    ```glsl
    if (u_edge_policy == 0) {
        sample_uv = clamp(sample_uv, vec2(0.0), vec2(1.0));
    } else if (u_edge_policy == 1) {
        sample_uv = fract(sample_uv);
    }
    // 无 == 2 分支；_BLUR_SEPARABLE_*_FRAGMENT 的 sampleEdge 支持 0/1/2
    ```
  - 影响: 片元路径（_BLUR_SEPARABLE_H/V 的 sampleEdge，495-505 行）支持 mirror(2)；计算路径收到 2 时 SSBO 版不做任何处理（UV 越界靠采样器状态兜底）、SHARED 版按 wrap 处理，同一参数下 GPU/片元路径与计算路径输出不一致。
  - 建议: 计算路径补 mirror 分支（`1.0 - abs(mod(uv,2.0)-1.0)`），或在渲染器侧将 mirror 请求固定路由到片元路径。
  - 置信度: 中（取决于渲染器是否会把 2 传给计算路径）

### 通过项
- 线程安全：模块为纯 GLSL 字符串常量 + `_DEPTH_INPUT_GLSL` 字符串拼接（6835-6851），无 Python 运行逻辑、无可变共享状态，无 UI 线程/GL 调用问题
- GPU 资源生命周期：N/A——不创建/释放任何 ModernGL 对象（仅源码文本，由渲染器负责编译与 deferred-release）
- 信号防递归：N/A——无 Qt 信号/控件
- 状态提交一致性（undo/redo/revision/持久化）：N/A——无状态
- 异常与取消路径：N/A——无 Python 代码路径（_EDGE_DETECT 4964-4965 注释已防御 uniform 被 compiler 剔除导致 evaluator KeyError 的风险）
- GLSL 类型规范：抽查全部数值字面量均带 `.0` 或为合法科学计数（如 1e-10）；循环边界为常量或 GL 3.30/430 允许的动态形式；vec/mat 构造无隐式截断问题
- HDR 正确性（总体）：divide/color-dodge 有 65504 半精度上限（254-257、304-307）、linear-light 有界 clamp（333-336）、LUT3D 有 HDR 高光保持与负值保留（3508-3529）、huecorrect 亮度不 clamp（2196-2199）、PQ/HLG/TRAW 传递函数常数与公式抽查无误；_READBACK_SRGB_BGR 的 clamp(0,1) 属 8bit 预览读回的合理用途
- 输入验证（总体）：除所列边界问题外，除法普遍有 max(...,eps) 防护（如 920、1938、2765、3765 等）；退化矩阵有 det 守卫（1087-1092、3355-3358）；蒙皮骨骼索引有范围守卫（5880、5963）
- 类型注解：N/A——模块无函数/类定义
