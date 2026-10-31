# batch-247 审查报告 [P2] src/features/color/shaders/

编号区间：AUD-24700–AUD-24799
审查文件：1 个（shaders.py，669 行，全文通读）
消费者核实：RAW_VIEW_FRAGMENT 用于视频预览（src/features/color/core/async_renderer.py:810,835）与全分辨率导出（src/features/color/core/image_engine.py:3095-3096，渲染进 f4 float 纹理）；LUT3D_* 全库无引用（死代码，活副本在 src/features/color/core/../composite/core/shaders.py:3491）；LUT 纹理均以 LINEAR 过滤创建（async_renderer.py:3991、_upload_3dlut、image_engine.py:3170、_load_lut_sync）。

## src/features/color/shaders/shaders.py (行数: 669)

### 发现

- [AUD-24700] [Critical] RAW_VIEW 饱和度/色彩增强路径将颜色 clamp 到 [0,1] 后做 HSV 往返，HDR 高光与负值全部丢失
  - 位置: src/features/color/shaders/shaders.py:481
  - 证据:
    ```glsl
    // 6. Saturation & Vibrance (Color Boost)
    if (u_raw_saturation != 1.0 || u_raw_color_boost != 0.0) {
        vec3 hsv = rgb2hsv(clamp(color, 0.0, 1.0));
        ...
        color = hsv2rgb(hsv);
    }
    ```
  - 影响: 只要 saturation≠1 或 color_boost≠0，>1.0 的高光和 <0.0 的负值（如 u_raw_lift 产生）被夹到 [0,1] 并整体替换 color，域外信息永久丢失；该 shader 同时用于预览与全分辨率导出（image_engine.py:3096 写入 f4 float 纹理），导出数据被破坏，且后续 log gamma 编码（u_raw_gamma 7–17 依赖 >1.0 输入区分高光）失效。违反铁律“HDR 内部数据不得无理由 clamp(0.0,1.0)”；同文件 LUT3D（147-165）与 shadows（455-461）均做了域外补偿，唯独此处没有。
  - 建议: 仿照 apply_lut_hdr_safe 的模式：对 clamp 后的值做 HSV 调整，再按 max>1 的比例缩放回高光并加回负值 excess。
  - 置信度: 高

- [AUD-24701] [Critical] RAW_VIEW 锐化/中间细节的邻域采样未应用 BGR swizzle 与 sRGB→线性→ProPhoto 预处理，与中心像素色彩空间不一致
  - 位置: src/features/color/shaders/shaders.py:434-437, 445-448（对照 405-423）
  - 证据:
    ```glsl
    // 中心像素（412-423）: srgb_to_linear + ProPhoto 矩阵；405-407: bgr swizzle
    vec3 l = get_linear_color(texture(u_texture, v_texcoord - vec2(off.x, 0.0)).rgb);
    vec3 r = get_linear_color(texture(u_texture, v_texcoord + vec2(off.x, 0.0)).rgb);
    vec3 u = get_linear_color(texture(u_texture, v_texcoord - vec2(0.0, off.y)).rgb);
    vec3 d = get_linear_color(texture(u_texture, v_texcoord + vec2(0.0, off.y)).rgb);
    vec3 laplacian = color * 4.0 - (l + r + u + d);
    ```
  - 影响: 非RAW（sRGB）源 + sharpness/mid_detail>0 时，Laplacian 混合“线性 ProPhoto 中心值”与“sRGB gamma 域邻域值”（数值差约 2.3 倍）：均匀中灰区 laplacian ≈ 4×0.21 − 4×0.5 < 0，图像整体压暗甚至黑块；视频 BGR 帧（async_renderer.py:2557 设置 u_bgr_swizzle）邻域 R/B 通道颠倒，产生强彩边。导出路径（image_engine.py:3096，JPEG 时 u_is_srgb_source=True）同样受影响 → 导出渲染错误。
  - 建议: 提取统一的 fetch_and_prepare(vec2 uv) 帮助函数，对每次采样先做 swizzle + srgb_to_linear + ProPhoto 矩阵，再进 get_linear_color。
  - 置信度: 高

- [AUD-24702] [Major] F-Gamut 色彩空间矩阵是 Rec.2020 矩阵的逐字节复制
  - 位置: src/features/color/shaders/shaders.py:597-601（对照 507-511）
  - 证据:
    ```glsl
    else if (u_raw_color_space == 13) {
        // Target: Fujifilm F-Gamut (Linear D65)
        mat3 prophoto_to_fgamut = mat3(
             1.2006095, -0.0699261,  0.0055390,
            -0.0574933,  1.0806111, -0.0407803,
            -0.1431162, -0.0106850,  1.0352414
    ```
    与 507-511 的 prophoto_to_2020 九个系数完全相同。
  - 影响: 选择 "Fujifilm F-Gamut"（u_raw_color_space==13，image_engine.py cs_map 含该映射项）时实际执行 ProPhoto→BT.2020 变换，预览与导出颜色错误。数值验证（Bradford CAT，ProPhoto D50 → 目标 D65）：用 Fujifilm 公布的 F-Gamut 基色（R 0.7347/0.2653, G 0.1424/0.8576, B 0.0991/−0.0040）计算的矩阵为 [[1.0696,−0.0069,−0.0627],[−0.0062,0.9563,0.0499],[0.0036,−0.0119,1.0083]]，与代码值在百分位即偏离（如 [0][0]≈1.070 vs 1.201）；两个不同色域产生 7 位小数全同的矩阵在数学上不可能。
  - 建议: 用正确 F-Gamut 基色重新生成矩阵替换；顺带复核其余 13 个目标色域矩阵（本次已数值验证 sRGB/Adobe/Rec.2020/DisplayP3 四个为正确）。
  - 置信度: 高

- [AUD-24703] [Major] RAW_VIEW 的 3D LUT 采样未做半 texel 中心修正，与本文件 LUT3D 已修复的实现不一致
  - 位置: src/features/color/shaders/shaders.py:664（对照 148-151）
  - 证据:
    ```glsl
    // LUT3D（已修复）:
    float lut_n = float(textureSize(lut_tex, 0).x);
    vec3 lut_result = texture(lut_tex, (sdr_part * (lut_n - 1.0) + 0.5) / lut_n).rgb;
    // RAW_VIEW（未修复）:
    if (u_lut_enabled) {
        color = texture(u_lut_tex, clamp(color, 0.0, 1.0)).rgb;
    }
    ```
  - 影响: 所有 LUT 纹理均以 (LINEAR, LINEAR) 过滤创建（async_renderer.py:3991、_upload_3dlut，image_engine.py:3170、_load_lut_sync），网格点 i 的 texel 中心位于 (i+0.5)/n；直接以 [0,1] 坐标采样导致 LUT 两端各半 texel 的映射拉伸——输入接近 0/1 的值偏差最大（33 点 LUT 端点偏移约 1.5%），启用 input LUT 的预览与导出色彩不准。同文件 148-151 的注释已明确描述并修复了该问题，此处为遗漏。
  - 建议: 复用与 LUT3D 相同的 (v*(n−1)+0.5)/n 映射（可提取公共 GLSL 函数供两处共享）。
  - 置信度: 高

- [AUD-24704] [Suggestion] LUT3D_VERTEX/LUT3D_FRAGMENT 为无引用死代码，与 composite 特性的活副本形成双份维护
  - 位置: src/features/color/shaders/shaders.py:114-198
  - 证据: 全库 grep 仅命中定义处（shaders.py:115,127）与 composite 的独立副本（src/features/composite/core/shaders.py:3491 `_LUT3D_FRAGMENT`，被 shader_library.py:44,168 实际使用）；composite 副本已额外演化出 u_mask 采样。
  - 影响: 两份实现并行漂移（活副本加了 mask，本文件版本没有）；在本文件修 LUT bug 不会生效，误导后续维护者。
  - 建议: 删除本文件的 LUT3D_*，或以注释指向 composite 活副本。
  - 置信度: 高

- [AUD-24705] [Suggestion] u_mix 混合基准使用未经输入域映射的原始纹理值
  - 位置: src/features/color/shaders/shaders.py:191-194
  - 证据:
    ```glsl
    // Mix control: blend between original and LUT result
    if (u_mix < 1.0) {
        vec3 original = tex_color.rgb;
        lut_result = mix(original, lut_result, u_mix);
    }
    ```
    而 lut_result 基于 174-179 输入域映射后的 color 计算。
  - 影响: 当 u_input_min/max 非默认且 u_mix<1 时，混合两侧处于不同坐标域，结果不连续/不符合预期。本文件副本当前未被使用，但 composite 活副本（shaders.py:3560 附近）同样存在此逻辑。
  - 建议: 在输入域映射前保存 color 副本，用映射后的值作为 mix 基准。
  - 置信度: 中

- [AUD-24706] [Suggestion] 顶点着色器局部变量 radians 遮蔽 GLSL 内置函数名
  - 位置: src/features/color/shaders/shaders.py:25
  - 证据:
    ```glsl
    float radians = -u_rotation_angle * 3.14159265359 / 180.0;
    float cos_a = cos(radians);
    ```
  - 影响: 语法合法但脆弱——后续任何人向该 shader 添加内置 radians() 调用会编译失败或语义混淆。
  - 建议: 改名 angle_rad 并使用内置 radians(u_rotation_angle)。
  - 置信度: 高

- [AUD-24707] [Suggestion] SPLIT_VIEW 分割线宽度为 UV 空间常量，非像素/宽高比自适应
  - 位置: src/features/color/shaders/shaders.py:107-110
  - 证据:
    ```glsl
    float line_width = 0.0015;
    if (abs(v_texcoord.x - u_split_pos) < line_width) {
        f_color = vec4(1.0, 1.0, 1.0, 1.0);
    }
    ```
  - 影响: 线的像素宽度随窗口宽度变化：宽窗口下线过粗、窄窗口（或高分屏缩放）下不可见，分割线视觉不一致。
  - 建议: 传入 viewport 宽度 uniform，将线宽换算为 1–2 像素的 UV 宽度。
  - 置信度: 高

### 通过项

- GLSL 严格类型字面量逐个核对通过：所有数字显式 .0 或有效科学计数（1e-5 等），`1.0 / textureSize(u_texture, 0)` 依赖的 ivec2→vec2 隐式转换为 GLSL 330 合法行为
- 色彩空间矩阵列主序书写一致性验证通过：sRGB↔ProPhoto 两矩阵互为精确逆矩阵（数值验证至 1e-6）；ProPhoto→sRGB/Adobe/Rec.2020/DisplayP3 与 Bradford CAT 独立计算结果一致至 ~1e-4（初级精度差异）；Adobe 与 sRGB 共享 G 行是两者 R/B 基色与白点相同时的数学必然，非复制错误
- 所有 log/pow 负数与零输入防护（max(...,ε)、step 分支切换点与定义域）检查通过，无 NaN/Inf 传播路径（vlog 的 max(c+d, 0.0) 因 d>0 且 cut>d 安全）
- 阴影（455-461）、高光（464-477）、LUT3D HDR 外推（154-165）均保留域外值补偿，无未补偿 clamp（481 行除外，见 AUD-24700；622 行 CMYK 模拟的 clamp 属印刷模拟的有意 SDR 化）
- 除法零值防护通过：mapped_max/max_c 有 max_c>0.1 守卫；CMYK 分母有 1e-6 epsilon 且结果 clamp
- 无 for 循环，不涉及循环上限常量约束；性能模式：锐化/中间细节分支在参数为 0 时完全跳过 8 次邻域纹理采样，预览级开销可接受
- Python 侧为纯字符串常量模块：无线程、GPU 对象、信号、状态提交、异常路径，类型注解维度不适用
- 12 个 gamma 编码函数常数与官方公式抽查一致（S-Log3、LogC3/Apple Log、ACEScct、V-Log、HLG、Log3G10、C-Log2/3、F-Log/F-Log2、DaVinci Intermediate）
