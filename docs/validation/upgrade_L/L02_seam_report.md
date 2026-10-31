# L02 滤镜跨页接缝 · 收口报告（2026-10-25）

> 角色：Mac 主集成者（本机串行窗口）。计划：§8 L02。
> HEAD：见 git log（本报告随 L02 提交入库）。

## 修复的共享接缝（§2.2 单一写入者文件，窄改）

`src/features/shared/effects/dctl_wrapper.py` 新增 **跨页 ABI 别名层**：

1. 参数名改写：插件体裸参数名 → `u_<name>`（\b 词边界正则，与 Color 侧
   shader_compiler 逐参数改写同语义；单插件单程序无需层索引）；
2. 引擎 uniform 别名：`u_input_tex→u_texture`、`u_tex_size→u_resolution`、
   `u_aspect_ratio→(u_resolution.x/u_resolution.y)`（W/H 与
   composition_uniforms 同口径）；
3. 宿主共享 helper 按需注入：`rgb2hsv/hsv2rgb`（shader_nodes.
   COMMON_DEFINITIONS 原文）与 `fast_blur`+`blur_rand`（shader_nodes.py
   原文）——仅插件引用且未自定义时注入；插件自定义宿主 helper 显式报错
   （契约本就禁止，Color 侧同罚）；
4. 保留字参数名（GLSL 关键字/内建/process 形参）显式 ValueError——对齐
   shader_compiler AUD-0832 黑名单策略（连带发现并修正 L 试点自身的
   `mix` 参数名冲突：F16/F19 参数改名 `amount`）。

修复前证据（L00 §5.1）：按 Color 约定书写的真实插件在 Edit runner 包装后
undeclared identifier 编译失败 → warning + no-op。

## 真实执行证据（tests/shared/test_dctl_cross_page_abi.py，10 绿）

环境：macOS darwin 24.3.0 / GL 4.1 ATI-6.1.13 / AMD Radeon RX 6800 /
standalone ModernGL context（require 330）。命令：

    .venv/bin/python -m pytest tests/shared/test_dctl_cross_page_abi.py -v

1. **全库编译**：35 个生产 `.tglsl` 全部经 `wrap_dctl_process` 真实编译
   （含 Halation_Extended 的宿主 helper 依赖与 HighPass_Retouch 的
   fast_blur 依赖——正是别名层修复的两类真实失败样本）。
2. **点处理子门（F16）**：真实渲染（runner 顶点着色器 + 全屏 quad +
   float32 FBO）vs 独立 CPU oracle，4 组参数（默认/amount 0.5/Smooth/
   自定义停靠位）全图 atol 6e-4 对拍通过。
3. **纹理邻域子门（F19）**：3 组参数（3×3 中值/5×5 p=0.25/Luma p=1.0）
   对拍通过（12×12 随机图 + 盐椒脉冲）。
4. 打包器→编译产物：`_pack_plugin_params` 输出的每个 `u_<name>` uniform
   在链接产物中真实存在（未声明/被优化即失败）。
5. 保留字拒绝（mix/color 形参名）与引擎 uniform 别名改写（字符串级断言）。

**口径声明**：渲染 harness 按 EffectPassRunner 的几何/命名约定复刻
（_PASSTHROUGH_VERTEX + _QUAD_VERTICES + 同 uniform 打包口径），非
`EffectPassRunner.run()` 完整调用链；后者连同时间线效果链消费在 L20 验收。
Color 侧发布件经 `tests/color/test_builtin_plugin_compile.py` 真实
ShaderPipeline 编译（37 插件 39 用例绿）。

## 发布（拟定路径 §2.1 落地）

- `plugins/LIB1_Gradient_Map.tglsl`、`plugins/LIB1_Median_Rank.tglsl`
  （暂存草稿原文；plugin_id=文件名 stem，i18n_name 前缀对齐）。
- 真实 EffectCatalog 注册验证：`plugin:LIB1_*` spec 存在、参数表/preview_cost/
  export 能力（gpu_preview+export_opengl）/select 结构键断言（10 绿内）。
- Edit 页效果面板经 effect_registry 自动聚合 catalog——菜单可见性沿用
  既有 33 插件同一路径，无新增旁路。
- 状态：F16/F19 就 §10 表为 **integrated**（真实 GL 编译+渲染+注册；
  LUT 烘出/Metal/CPU 导出矩阵仍 untested，L20/L21 补）；暂存区 manifest
  仍标 component_ready（暂存组件口径），本报告承载 integrated 证据。

## 回归

- `tests/shared/ tests/plugins/ tests/content_library_v1/`：2611 绿；
  36 失败 + 92 错误全部为**存量基线**（AI 端点/缓存/追踪域；stash 干净树
  复验同样失败，与本改动无关）。
- `tests/color/test_builtin_plugin_compile.py` 39 绿（含 2 个新 LIB1）；
  `tests/edit/test_effect_catalog_capabilities.py` 36 绿。

## 未验清单（如实）

- 真实时间线 clip 效果链端到端（Edit 页 GPU 预览/GL 导出走
  EffectPassRunner.run 的完整调用链）→ L20。
- LUT 烘出（F16 点处理在输入域冻结下可表达；F19 邻域不可表达——矩阵
  如实 untested/unsupported）。
- Metal/CPU 导出后端；多 pass 类效果（F06 等原生 renderer 支持）不在本包。
- M05.2 完整载荷矩阵未收口——生产跨页链声明按其口径保留。
