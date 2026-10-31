# Shader 迁移台账（ADR 0004 门禁 G4）

> 状态机：`gl_only` → `dual`（双 emitter 可编译）→ `msl_verified`（G2/G3 通过，证据在案）。
> 「迁移完成」= color catalog 全部效果 `msl_verified` + G1~G3 绿（composite 见下半表，独立推进）。
> 每个效果一 PR 一行；禁止静默部分状态（.clinerules:11）。

## 门禁基建状态

| 项 | 状态 | 日期 | 备注 |
|---|---|---|---|
| G1 golden corpus | ✅ 基线归档（v2 含插件） | 2026-09-11 | `tests/color/test_shader_golden_corpus.py`（composition 生产路径，12 用例 × 2 图案 = 24 哈希，GPU 指纹绑定）；v2 增补真实插件栈用例（HighPass_Retouch 单插件 + HighPass×bilateral 双插件）并在 P7-01 重构**之前**归档；基线 `tests/color/golden/shader_golden_corpus.json`；重归档 `TOPOS_UPDATE_SHADER_GOLDEN=1`。待补：curves/hsl/warper/lut3d fixture 用例 |
| G2 MSL 画质门禁 | ✅ 方法论已验证 | 2026-09-11 | P6-06 模式（numpy 参考 + 实测容差 + 确定性位级）；per-effect 落地随迁移 PR |
| G3 性能门禁 | ⏸ 待 A′ 主机器 | — | 本机仅回归（劣化 >5% 阻断）；正式基线待 Apple Silicon |

**G1 建设中的发现（2026-09-11，P7-01 已裁决）**：`ShaderPipeline.build_shader`
（融合多层路径，`async_renderer._do_compile` 调用）无条件发射
`apply_blur_sharpen(...)` 调用，但其 snippet 清单**不含
`BLUR_SHARPEN_SNIPPET`**——该路径编译必败，错误仅记日志后吞掉，程序置
None。生产渲染实际全部走 `build_composition_shader`（逐层 + matte），故
用户不可见。**裁决：删除**（commit `e842aeb3`）——`update_uniforms` /
`_do_compile` / `RenderCommand.COMPILE_PIPELINE` 同链清除（全仓库零生产方、
零消费者；`pipeline.program` 无外部读取者）；补 include 会复活一条与
composition 路径平行演化的第二实现，违反 ADR 0003 渐进双轨。

**基建：结构化 uniform 绑定（2026-09-11，P7-02，commit `d8fda7a3`）**：
`composition_uniforms.py` 单一 manifest（`name → {glsl_type, msl_type,
components, offset}`）驱动 GL 声明生成（文本与手写基线逐行一致，G1 24 条
位级通过）与 MSL struct 布局（P6-06 对齐规则，P7-03 直接消费）；值提取与
上传唯一化（async/image_engine 两套手写实现合一）。随附两项行为裁决：
①fx/blur 空组总是写中性默认——修复多 layer 共享缓存 program 时前一 layer
的 fx 值残留（跨 layer 串参数，旧 async 实现跳过空组所致）；②修复 P7-01
遗留：image_engine 插件上传循环误枚举 bindings 列表。grain-only pass
（image_engine）刻意保持显式手设（u_opacity=0 使 grading 结果被 mix 丢弃，
部分 uniform 策略为语义所需，不经过全量提取器）。

## Color 管线（shader_nodes.py 11 SNIPPET + 基础骨架 + 插件）

| 效果/单元 | 相关 SNIPPET | 状态 | 日期 | PR/commit | 证据（G1/G2/G3） |
|---|---|---|---|---|---|
| film grain | `FILM_GRAIN_SNIPPET` | `msl_verified(compute + fragment)` | 2026-09-12 | `55ac02fb`（P6-06）+ P7-03 第六批 | compute（P6-06）：numpy 参考位级确定性 + ≤1 fp16 ulp；fragment（P7-03）：composition 内联形态 vs 同一 P6-06 numpy 参考 ≤2 fp16 ulp（真机 AMD 6800；`tests/color/test_metal_composition_grain_softclip.py`），同设备两次渲染逐位相同，GL↔MTL 噪声相位不同属预期（不同 hash 实现、同分布）以零偏+分位数统计门佐证；**contract(off) 仅在 grain 激活时文件级注入**（hash 链对 FMA 收缩极端敏感；MSL 无 push/pop 作用域、语句拆分被 AIR 跨语句重关联击穿，均实测）；GL 路径未触碰；G3：待 A′ 重测 |
| soft clip | `SOFT_CLIP_SNIPPET` | `msl_verified(compute + fragment)` | 2026-09-12 | `55ac02fb`（P6-06）+ P7-03 第六批 | compute（P6-06）：同上；fragment（P7-03）：composition 内联形态标准对拍 SDR ≤1/255 + HDR ≤2e-3 rel（toe 双分支/shoulder 双分支/disabled 守卫直通/负 contrast；GL main `if (u_sc_enabled)` 同形守卫），纯数学无噪声；pow ≤2 fp16 ulp（实测 1）；G3：待 A′ 重测 |
| base grading | `GRADING_SNIPPET` | `msl_verified(fragment)` | 2026-09-12 | P7-03 首批 | G2 实测通过：`tests/color/test_metal_composition_grading.py` 13 SDR 用例 ≤1/255 + 5 HDR 用例 ≤2e-3 rel（真机 AMD 6800，Metal fragment vs GL composition 同负载逐像素；覆盖 exposure/temp/tint/contrast±/lift-gamma-gain/shadows±/highlights±/vibrance/mixer 无关项/mid_detail+sharpness 邻域采样/hue±wrap/组合全分支）；emitter 骨架 `msl_composition.py`（struct 由 P7-02 manifest 生成）；G3：待 A′ 重测（本机无生产接线无回归面） |
| curves + hsl_curve | `CURVES_SNIPPET` | `msl_verified(fragment)` | 2026-09-12 | P7-03 第二批 | G2 实测通过：`tests/color/test_metal_composition_curves_lut.py` curves 2 SDR + 2 HDR（外推 x<0/x>1、暗部/高光软性保护、通道平衡校正）+ hsl 2 SDR + 1 HDR（六曲线：LumSat/HueHue/HueSat/HueLum/SatSat/SatLum）≤1/255、≤2e-3 rel（真机 AMD 6800）；fixture 与生产纹理布局一致（curves (256,1)×3、hsl (6,256)×1，f4/LINEAR/clamp）；翻译要点：GLSL 分量循环的采样坐标随通道 x 而异且结果取该 texel 第 i 分量（`texture(...)[i]`），MSL helper 用运行期向量下标选择——首版误取 `.r` 被 G2 当场拦截（门禁有效性的实证）；wrap 策略为绑定期对齐项（Metal=ClampToEdge，GL 生产默认 REPEAT 在 t=1.0 混入 row 0，P6-02 消费时核对）；G3：待 A′ 重测 |
| lut3d | `LUT3D_SNIPPET` | `msl_verified(fragment)` | 2026-09-12 | P7-03 第二批 | G2 实测通过：同上 harness，2 SDR（intensity 0.7/1.0 三线性采样）+ 1 HDR（max_orig>1 比例缩放 + 负值加法偏移分支）≤1/255、≤2e-3 rel；四效果串联（grading→curves→hsl→lut3d）SDR+HDR 各 1 用例通过（管线顺序实证）；`textureSize(lut_tex,0).x` → `get_width()`；Metal 无 RGB32Float，fixture 以 RGBA32Float 承载（rgb 分量与 GL 侧 3 通道数据逐值一致）；G3：待 A′ 重测 |
| mixer | `MIXER_SNIPPET` | `msl_verified(fragment)` | 2026-09-12 | P7-03 第三批 | G2 实测通过：`tests/color/test_metal_composition_mixer_warper.py` 3 SDR（通道选择/全 8 通道叠加/与 grading 叠加，SDR 斜坡覆盖 8 中心 mask 重叠区）+ 1 HDR（`sign(color)` 负值色相保持）≤1/255、≤2e-3 rel（真机 AMD 6800）；8 通道 vec3 uniform 恒在基础声明（无 manifest 旗标，仅门 GL main 调用位）——MSL 调用点从 struct 平铺分量重建 float3；GLSL const 局部数组 + 运行期下标 MSL 同语义；G3：待 A′ 重测 |
| warper | `WARPER_SNIPPET` | `msl_verified(fragment)` | 2026-09-12 | P7-03 第三批 | G2 实测通过：同上 harness，2 SDR（饱和域扭曲/与 grading 叠加）+ 1 HDR（负值 sign 路径）≤1/255、≤2e-3 rel；sat<0.02 早退分支由灰阶条覆盖；纹理 fixture 与生产布局一致（(w,h) 2 通道 f4 = RG32Float，LINEAR/clamp；`warper_params` 传真实数组，extractor 读 `shape[0]` 为 grid_size——grid_size 在两侧同属未消费形参，ABI 保留）；mixer→warper 串联 SDR+HDR 各 1 用例通过（管线顺序实证）；G3：待 A′ 重测 |
| blur/sharpen | `BLUR_SHARPEN_SNIPPET` | `msl_verified(fragment)`（抖动噪声门） | 2026-09-12 | P7-03 第四批 | G2 实测通过（门禁形态按效果类适配，P6-06 先例）：直通路径位级（既有 neutral 用例）；抖动路径——实测两侧 sin-hash 抖动相位本质不同（hash 图 18.6% 像素完全分歧，sin 大参数降精度被 hash 放大），改用**零中值 + 冻结分位数门**（全用例 p50=0、符号偏差≈0.000，SDR 尾界 p99≤23/p99.9≤44/max≤75 LSB 按实测×1.5 冻结）+ **相位 0 numpy 参考实现交叉验证**（bilateral/motion/fast 双侧零偏同噪声族；bokeh 逐 tap 非线性 lum² boost 改判等偏判据）——翻译结构性错误由 oracle 交叉验证拦截；USM/四模糊/混合分支全覆盖；`radians()` 无 MSL 内建 → 显式 π/180 展开；17 测试（`tests/color/test_metal_composition_blur_sharpen.py`）；G3：待 A′ 重测 |
| mixer | `MIXER_SNIPPET` | `gl_only` | — | — | — |
| warper | `WARPER_SNIPPET` | `gl_only` | — | — | — |
| mask | `MASK_SNIPPET` | ✅ 死代码删除 | 2026-09-12 | P7-03 第五批 | 全仓库零引用（词边界核查：无 include、无调用方；composite 的 sdf_rectangle 为独立实现非同一符号）——按 P7-05 死常量删除先例移除；GLSL 侧无行为变化，G1 corpus 位级通过 |
| blend modes | `BLEND_MODES_SNIPPET` | `msl_verified(fragment)` | 2026-09-12 | P7-03 第五批 | G2 实测通过：`tests/color/test_metal_composition_blend_modes.py` 19 模式（Layer.BLEND_MODES 0~18 全分支含 HSV 色相系 12~15）× SDR ≤1/255 + 渐变 matte×opacity 0.7 混合语义 + HDR 4 模式（Screen/Dodge/Hue/Addition）≤2e-3 rel（真机 AMD 6800）；本批迁移后 MSL emitter 补齐 composition main 尾部语义（matte texture(5) 采样 + `mix(原色, blended, mask*opacity)` epilog）——对拍从此覆盖真实混合路径，不再依赖 Normal+白 matte+opacity 1 等价基准；blend 无编译旗标（main 恒定调用）；G3：待 A′ 重测 |
| qualifier | `QUALIFIER_SNIPPET` | 归属待裁决 | — | — | composition main 无调用（Phase 2 起 qualifier 在 matte 生成侧计算）；composition MSL 迁移不需要；matte 生成管线的 MSL 化归 P6-02~04 消费范围 |
| 插件 manifest 化 | （DCTL 正则改写 → EffectSpec manifest） | ✅ 完成（编译入口权威化，GL 侧） | 2026-09-11 | `e842aeb3`（P7-01） | G1：4 条插件用例（单插件/双插件 P0-P1 命名空间）位级一致；`plugin_binding_from_instance` 以 EffectSpec 为权威 manifest（参数名+类型+uniform 映射+structural），复用 catalog `_dctl_param_to_pdef` 单一转换源；正则改写保留为 GL emitter 内部细节；类型未知 ValueError（G5）；16 条单测（`tests/color/test_plugin_manifest_binding.py`）覆盖 ABI 不漂移/失败语义/缓存键（值参数不重编译、select 与 enabled 重编译）；存量 `.tglsl` 零修改（G5） |
| 插件 manifest MSL | （P7-03 尾项，2026-09-12 调研） | 🔶 待设计裁决 | — | — | **范围调研结论**：`.tglsl` 插件源为任意形态 GLSL（`texture(u_input_tex, uv)` 采样、参数名为全局 uniform、用户自定义 helper）——不存在 SNIPPET 那样的逐条翻译路径，需要**受限 GLSL→MSL 源改写器**（texture() → .sample()、sampler 注入、uniform → struct 字段访问、process → msl_process_P{n} 前缀）或双源插件机制，属设计裁决非机械迁移；裁决前 MSL emitter 对含插件管线维持显式 ValueError（G4：不静默跳过），不阻塞其余效果的 Metal 消费（P6-02~04）；建议与 P6-02 消费设计一并裁决 |

## Composite（shaders.py 7,337 行 / 102 `#version`，P7-04 独立推进）

| 效果 | 状态 | 日期 | PR/commit | 证据 |
|---|---|---|---|---|
| （待逐效果登记——Phase 6/7 重构触及 composite 时填充） | — | — | — | — |

## 参考 compute/解码内核（已交付，非本台账推进范围，仅备忘）

| 单元 | 位置 | 日期 | 备注 |
|---|---|---|---|
| blur/erode-dilate MSL | `src/shared/gpu/metal/kernels.py` | P4-02 | 默认仍 fragment（P4-05 实测） |
| histogram/CDF MSL | `src/shared/gpu/metal/kernels_histogram.py` | P4-03 | Normalize 百分位链 |
| YUV→RGBA16F MSL | `src/shared/gpu/metal/kernels_video.py` | P5-04 | NV12/P010 零拷贝链路 |
