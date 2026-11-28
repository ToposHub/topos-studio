# 效果与插件兼容矩阵（阶段 6.1）

由 `tools/build_effect_compat_matrix.py` 从效果目录、冻结合同目录、Metal 验证登记表与插件目录生成，**不要手改**：重跑生成器即可更新（`--check` 在文档过期时返回 1）。

口径：`未登记` 表示仓库对该项没有逐效果的记录，矩阵只列出已知事实。「HDR / Alpha / 确定性」一列只对 Metal 对拍验证过的插件是实测结论——语料含负值、零、超白的 HDR 输入，Alpha 要求逐位相同；其余条目仍走各自的 OpenGL 路径，这些属性从未按效果登记。声明 `metal` 的前提是本机（AMD RX 6900 XT）真机 GL↔Metal 对拍通过，证据见 `evidence/effect-metal-parity-2026-11-23.json`；`tests/shared/test_tglsl_metal_parity.py` 每次重跑该门限。

**目前没有任何生产路径在 Metal 上执行效果**：效果一律经 GLSL 的 `EffectPassRunner`，能力预检也不参与路由。本矩阵登记的是能力与证据，不是已启用的行为。

## 总览

| 类别 | 数量 | 图形 / 计算后端 | Metal 状态 |
|---|---:|---|---|
| 内置视频效果 | 11 | OpenGL | 未迁移 |
| 内置 Metal 条目（6.2 helper） | 1 | Metal compute | 已验证；尚无内置效果调用 |
| `.tglsl` 插件 | 40 | 声明 ∩ 宿主可译 | 已验证 21，噪声门限未过 3，未迁移 16 |
| 冻结合同目录中的非视频效果（音频、生成器） | 28 | 非图形 / OpenGL | 不在本次范围 |
| 转场 | 51 | OpenGL 转场着色器 | 2 种在合成内（`cross_dissolve`、`dip_to_color`），其余未迁移 |
| 其他插件类型（`.tai`、节点插件、AI、OIIO） | 5 | 宿主 / provider | 未迁移或不在本次范围 |
| 预设 | 6 | 随所含效果 | — |

冻结合同目录 `catalog_id` 共 36 个效果、51 个转场；旧工程里已不存在的效果 ID 由效果目录的未知效果诊断处理，本矩阵不枚举历史 ID。

## 内置视频效果

| ID | 名称 | 参数数 | 图形后端 | Metal 状态 | HDR / Alpha / 确定性 |
|---|---|---:|---|---|---|
| `blur` | Blur | 19 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `chromogen` | Look Dev | 1 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `defocus` | Defocus | 7 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `denoise` | Denoise | 5 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `edge_detect` | Edge Detect | 10 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `erode_dilate` | Erode / Dilate | 5 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `glow` | Glow | 8 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `layer_style` | Layer Style | 17 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `lib1_posterize` | Posterize (Lib1) | 3 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `sharpen` | Sharpen | 4 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |
| `video_stabilization` | Video Stabilization | 3 | OpenGL | 未迁移（走既有 GLSL） | 未登记 |

## 内置 Metal 条目

均已验证，但尚无内置效果或生产路径调用它们。

| ID | MSL helper | 版本 | HDR | Alpha | 确定性 | 验证 |
|---|---|---|---|---|---|---|
| `builtin:exposure` | `migration_exposure` | 1.0.0 | ✓ | ✓ | ✓ | 已验证 |

## `.tglsl` 插件

| ID | 版本 | 参数数 | 声明 graphics | 声明 compute | 能力 | Metal 状态 | HDR / Alpha / 确定性 | 对拍证据 |
|---|---|---:|---|---|---|---|---|---|
| `Anamorphic_Streak` | 1.0 | 5 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Bleach_Bypass` | 1.0 | 3 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.00390625，max 相对 9.66e-04，99.6% 逐位 |
| `Bokeh_Defocus` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `CRT_Scanlines` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 32，max 相对 9.76e-04，39.7% 逐位 |
| `Channel_Mixer` | 1.0 | 10 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.000488281，max 相对 1.67e-03，99.6% 逐位 |
| `Color_Space_Transform` | 1.0 | 9 | opengl | glsl_fragment | gpu_preview | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Cross_Process` | 1.0 | 2 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.00390625，max 相对 1.30e-02，99.9% 逐位 |
| `Day_Night_Converter` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.0078125，max 相对 9.58e-04，99.2% 逐位 |
| `Duotone_Toner` | 1.0 | 5 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.00195312，max 相对 1.52e-02，85.7% 逐位 |
| `Edge_Ink_Outline` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 16，max 相对 8.78e-04，71.4% 逐位 |
| `Fake_Infrared` | 1.0 | 3 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.0625，max 相对 1.34e-03，99.9% 逐位 |
| `Film_Grain` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 噪声门限未过（不声明 `metal`） | 确定性 ✗（哈希噪声两端不可复现） | 68.2% 逐位 |
| `Film_Halation` | — | 2 | metal, opengl | glsl_fragment, metal_compute | gpu_preview | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0，max 相对 0.00e+00，100.0% 逐位 |
| `Frequency_Split` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.00390625，max 相对 6.67e-02，98.8% 逐位 |
| `Glitch_Blocks` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 噪声门限未过（不声明 `metal`） | 确定性 ✗（哈希噪声两端不可复现） | 53.7% 逐位 |
| `God_Rays` | 1.0 | 8 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Grad_Filter_Exposure` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.03125，max 相对 9.71e-04，85.7% 逐位 |
| `Halation_Extended` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Halftone_Print` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `HighPass_Retouch` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview | 噪声门限未过（不声明 `metal`） | 确定性 ✗（哈希噪声两端不可复现） | 0.0% 逐位 |
| `Kaleido_Mirror` | 1.0 | 3 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.0102539，max 相对 1.39e-02，99.6% 逐位 |
| `LIB1_Gradient_Map` | 0.1.0 | 8 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.000488281，max 相对 2.91e-03，99.6% 逐位 |
| `LIB1_Median_Rank` | 0.1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Lens_Distortion` | 1.0 | 3 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.0136719，max 相对 1.68e-02，99.1% 逐位 |
| `Light_Leak_Corner` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.000244141，max 相对 8.60e-04，99.8% 逐位 |
| `Motion_Blur_Linear` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Pixel_Blocks` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Prism_Aberration` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Radial_Blur_Pro` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Skin_Even_Matte` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Sky_Replace_Matte` | 1.0 | 6 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.0078125，max 相对 3.69e-03，85.7% 逐位 |
| `Soft_Glow_Bloom` | 1.0 | 3 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Three_Way_Tint` | 1.0 | 6 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.00195312，max 相对 9.23e-04，99.9% 逐位 |
| `Tilt_Shift_Focus` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Unsharp_Sharpen` | 1.0 | 4 | opengl | glsl_fragment | gpu_preview, export_opengl | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `Vibrance_Saturation` | 1.0 | 3 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.00195312，max 相对 1.72e-03，98.4% 逐位 |
| `Vignette_Shapes` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 1，max 相对 2.50e-01，99.2% 逐位 |
| `White_Balance_Advanced` | 1.0 | 4 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0.0078125，max 相对 8.79e-04，85.7% 逐位 |
| `bilateral_skin_smooth` | 6.0 (Simplified) | 4 | opengl | glsl_fragment | gpu_preview | 未迁移（OpenGL 兼容路径） | 未登记 | — |
| `warm_tint` | 1.1 | 1 | metal, opengl | glsl_fragment, metal_compute | gpu_preview, export_opengl | **已验证**（声明 `metal`） | HDR 语料通过 / Alpha 逐位 / 确定性 ✓ | max\|Δ\| 0，max 相对 0.00e+00，100.0% 逐位 |

## 冻结合同目录中的非视频效果

| ID | 名称 | 类别 | 后端 | Metal |
|---|---|---|---|---|
| `adjustment_clip` | Adjustment Clip | generator | OpenGL（生成器 / 调整剪辑） | 不在本次范围 |
| `bus_comp` | Bus Compressor | audio | 非图形（音频） | 不在本次范围 |
| `chorus_flanger` | Chorus / Flanger | audio | 非图形（音频） | 不在本次范围 |
| `compressor` | Compressor | audio | 非图形（音频） | 不在本次范围 |
| `convolution_reverb` | Convolution Reverb | audio | 非图形（音频） | 不在本次范围 |
| `de_esser` | De-Esser | audio | 非图形（音频） | 不在本次范围 |
| `delay` | Stereo Delay | audio | 非图形（音频） | 不在本次范围 |
| `dynamic_eq` | Dynamic EQ | audio | 非图形（音频） | 不在本次范围 |
| `dynamics` | Dynamics | audio | 非图形（音频） | 不在本次范围 |
| `exciter` | Exciter | audio | 非图形（音频） | 不在本次范围 |
| `expander` | Expander / Gate | audio | 非图形（音频） | 不在本次范围 |
| `imager` | Stereo Imager | audio | 非图形（音频） | 不在本次范围 |
| `limiter` | Limiter | audio | 非图形（音频） | 不在本次范围 |
| `multiband_comp` | Multiband Compressor | audio | 非图形（音频） | 不在本次范围 |
| `noise_gate` | Expander / Gate | audio | 非图形（音频） | 不在本次范围 |
| `phaser` | Phaser | audio | 非图形（音频） | 不在本次范围 |
| `reverb` | Acoustic Reverb | audio | 非图形（音频） | 不在本次范围 |
| `saturation` | Saturation | audio | 非图形（音频） | 不在本次范围 |
| `solid_color` | Solid Color | generator | OpenGL（生成器 / 调整剪辑） | 不在本次范围 |
| `spatial_panner` | Spatial Panner | audio | 非图形（音频） | 不在本次范围 |
| `spectral_mangler` | Spectral Mangler | audio | 非图形（音频） | 不在本次范围 |
| `strip_eq` | Parametric EQ | audio | 非图形（音频） | 不在本次范围 |
| `subharmonic` | Subharmonic | audio | 非图形（音频） | 不在本次范围 |
| `transient_designer` | Transient Designer | audio | 非图形（音频） | 不在本次范围 |
| `tremolo_auto_pan` | Tremolo / Auto Pan | audio | 非图形（音频） | 不在本次范围 |
| `true_peak_limiter` | True Peak Limiter | audio | 非图形（音频） | 不在本次范围 |
| `voice_changer` | Voice Changer | audio | 非图形（音频） | 不在本次范围 |
| `vst3_hosted` | VST3 plug-in | audio_hosted | 非图形（音频） | 不在本次范围 |

## 转场

| ID | 名称 | 类别 | Metal 状态 |
|---|---|---|---|
| `blur_dissolve` | Blur Dissolve | Dissolve | OpenGL 转场着色器（未迁移） |
| `cross_dissolve` | Cross Dissolve | Dissolve | 预览合成内（2.6）；Metal H.264 导出快路径（3.0） |
| `defocus_dissolve` | Defocus Dissolve | Dissolve | OpenGL 转场着色器（未迁移） |
| `dip_to_color` | Dip To Color | Dissolve | 预览合成内（2.6） |
| `film_dissolve` | Film Dissolve | Dissolve | OpenGL 转场着色器（未迁移） |
| `iris_arrow` | Iris Arrow | Shape | OpenGL 转场着色器（未迁移） |
| `iris_circle` | Iris Circle | Iris | OpenGL 转场着色器（未迁移） |
| `iris_diamond` | Iris Diamond | Iris | OpenGL 转场着色器（未迁移） |
| `iris_ellipse` | Iris Ellipse | Iris | OpenGL 转场着色器（未迁移） |
| `iris_heart` | Iris Heart | Shape | OpenGL 转场着色器（未迁移） |
| `iris_hexagon` | Iris Hexagon | Iris | OpenGL 转场着色器（未迁移） |
| `iris_plus` | Iris Plus | Iris | OpenGL 转场着色器（未迁移） |
| `iris_square` | Iris Square | Iris | OpenGL 转场着色器（未迁移） |
| `iris_star` | Iris Star | Shape | OpenGL 转场着色器（未迁移） |
| `iris_triangle` | Iris Triangle | Shape | OpenGL 转场着色器（未迁移） |
| `lib1_burn_reveal` | Burn Reveal (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_checker_reveal` | Checker Reveal (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_clock_wipe` | Clock Wipe (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_cube_faces` | Cube Faces (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_displacement_bridge` | Displacement Bridge (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_halftone_reveal` | Halftone Reveal (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_hinged_doors` | Hinged Doors (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_kaleido_bridge` | Kaleido Bridge (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_luma_reveal` | Luma Reveal (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_noise_dissolve` | Noise Dissolve (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_page_curl` | Page Curl (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_pixelate_bridge` | Pixelate Bridge (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_ripple_refraction` | Ripple Refraction (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_scanline_slices` | Scanline Slices (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_staggered_tiles` | Staggered Tiles (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_true_push` | True Push (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_venetian_blinds` | Venetian Blinds (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `lib1_voronoi_reveal` | Voronoi Reveal (Lib1) | Motion | OpenGL 转场着色器（未迁移） |
| `motion_blur_down` | Motion Blur Down | Motion Blur | OpenGL 转场着色器（未迁移） |
| `motion_blur_left` | Motion Blur Left | Motion Blur | OpenGL 转场着色器（未迁移） |
| `motion_blur_right` | Motion Blur Right | Motion Blur | OpenGL 转场着色器（未迁移） |
| `motion_blur_up` | Motion Blur Up | Motion Blur | OpenGL 转场着色器（未迁移） |
| `rgb_split_left` | RGB Split Left | Motion Blur | OpenGL 转场着色器（未迁移） |
| `rgb_split_right` | RGB Split Right | Motion Blur | OpenGL 转场着色器（未迁移） |
| `slide_down` | Slide Down | Motion | OpenGL 转场着色器（未迁移） |
| `slide_left` | Slide Left | Motion | OpenGL 转场着色器（未迁移） |
| `slide_right` | Slide Right | Motion | OpenGL 转场着色器（未迁移） |
| `slide_up` | Slide Up | Motion | OpenGL 转场着色器（未迁移） |
| `smooth_cut` | Smooth Cut | Dissolve | OpenGL 转场着色器（未迁移） |
| `spin_blur_ccw` | Spin Blur CCW | Motion Blur | OpenGL 转场着色器（未迁移） |
| `spin_blur_cw` | Spin Blur CW | Motion Blur | OpenGL 转场着色器（未迁移） |
| `whip_pan_left` | Whip Pan Left | Motion Blur | OpenGL 转场着色器（未迁移） |
| `whip_pan_right` | Whip Pan Right | Motion Blur | OpenGL 转场着色器（未迁移） |
| `wipe` | Wipe | Motion | OpenGL 转场着色器（未迁移） |
| `zoom_blur_in` | Zoom Blur In | Motion Blur | OpenGL 转场着色器（未迁移） |
| `zoom_blur_out` | Zoom Blur Out | Motion Blur | OpenGL 转场着色器（未迁移） |

## 其他插件类型

| ID | 类型 | 入口 | Metal |
|---|---|---|---|
| `AI_Upscale_4X` | `.tai` | ONNX Runtime（按 provider 验证，计划 6 范围内未动） | 非图形 |
| `posterize_plugin` | 节点插件（Python） | 宿主与脚本服务 | 未迁移 |
| `vintage_plugin` | 节点插件（Python） | 宿主与脚本服务 | 未迁移 |
| `ai_assistant` | 目录 | AI 助手（非图形） | 不在本次范围 |
| `oiio_topos` | 目录 | OIIO / TOPS 编解码 | 不在本次范围 |

## 预设

| ID | 类型 | 说明 |
|---|---|---|
| `LIB1_Gradient_Tone` | 预设 | 参数集合：后端随它引用的效果 |
| `基础调色` | 预设 | 参数集合：后端随它引用的效果 |
| `模糊柔化` | 预设 | 参数集合：后端随它引用的效果 |
| `绿幕抠像` | 预设 | 参数集合：后端随它引用的效果 |
| `绿幕抠像合成` | 预设 | 参数集合：后端随它引用的效果 |
| `美白调色` | 预设 | 参数集合：后端随它引用的效果 |
