# ADR 0004 — shader_compiler 双发射器重构：EffectSpec 权威化 + 结构化 uniform + 覆盖率门禁冻结

- **Status:** Accepted（门禁自本文档起冻结；实施按效果逐个推进，每效果一 PR 一台账行）
- **Date:** 2026-09-11
- **Plan ref:** `docs/metal_backend_migration_and_cross_platform_gpu_plan.md` v2 §5 Phase 6/7（P6-02~04、P7-01~04）、§7 迁移规则
- **Supersedes:** 无；继承 ADR 0003（双轨决策、A/B 方法学、A′ 平台决策）
- **Implementation:** `src/features/color/core/shader_compiler.py`、`src/features/shared/effects/effect_spec.py`、`src/features/color/core/shader_nodes.py`（后续 per-effect PR）
- **复核触发:** 任一门禁连续两次失败（该效果回退 gl_only 并立项专项）；A′ 主机器（Apple Silicon）到位（G3 基线重测）；Phase 10 启动

## Context

Phase 6/7 剩余的 L 级项（P6-02~04、P7-01~04）是同一项重构：把
`shader_compiler.py` 的单发射器（GLSL 文本拼装）改为 GLSL/MSL 双发射器，
并让插件 shader 的进入方式摆脱对 DCTL ABI 文本同一性的依赖。计划 v2 明确
要求：**实施前在 ADR 中冻结覆盖率门禁**——本文档即该冻结。

现状实测（2026-09-11，作为门禁基线）：

- `shader_compiler.py` 1,082 行；`shader_code.append` 186 处（其中 f-string
  129 处）；`uniform` 词法命中 161 处。
- 正则命名空间机制（`:395/:401/:734/:738`）对插件源码做 `re.sub`：改写
  `process(` 函数名与参数变量名加 `L{i}_P{p_idx}_` / `u_P{p_idx}_` 前缀——
  依赖 DCTL ABI 的文本同一性，MSL 无等价契约（标识符规则不同、无同名
  uniform 语义、struct 打包规则不同）。
- `shader_nodes.py` 实际有 **11 个** `*_SNIPPET` 常量。计划 P7-03 列 10 个
  （漏 `LUT3D_SNIPPET`），以本 ADR 为准（数据修偏）。
- composite 侧 `shaders.py` 7,337 行 / 102 个 `#version`，单项最大工作量，
  独立台账推进。
- 已有基建（门禁可直接复用）：
  - `EffectSpec` 已携带结构化信息：`param_defs`（ParamDef）、
    `param_to_uniform`（非恒等映射表）、`shader_kind`、`shader_sources`、
    `dispatch_key`、`structural_param_names`——**结构化输入的承载者已存在**，
    升权威即扩字段 + 收敛消费方，无需新建 IR。
  - golden 基建：`tests/color/test_color_engine_triple_golden.py`
    （ModernGL standalone 真实执行 shader，SDR 1e-3 / PQ·HLG 2e-3 rel 容差）、
    `tests/color/test_color_e2e_consistency_matrix.py`。
  - P6-06 方法论：MSL kernel + numpy 参考 + 实测容差门禁 +
    numpy dtype ↔ MSL struct 逐字段对齐（`kernels_color.py` 模式）。
- P4-05 实测：本机（Intel + AMD 6800，A′ 降级路径）Metal compute + CPU
  staging 慢于 fragment → **fragment 优先**；compute 仅在有实测收益处。
- A′ 平台决策：本机以回归门禁为主；性能正式基线在 Apple Silicon 重测。

## Decision

1. **`EffectSpec` 升为双后端权威输入（P7-01）。** 插件 shader 以 manifest
   （参数名 + 类型 + uniform 映射 + structural 参数）进入编译器；正则源码
   改写降级为 GLSL emitter 内部的命名空间实现细节——允许其继续存在，但
   **MSL emitter 禁止依赖它**（MSL 命名空间由生成的 struct/函数前缀承担）。
2. **结构化 uniform 绑定（P7-02）。** 单一参数 manifest
   （`name → {glsl_type, msl_type, offset, array_len}`）驱动两侧代码生成：
   GL 侧生成 uniform 声明与 `glUniform*`/uniform location 解析；MSL 侧生成
   `struct`（按 P6-06 已验证的 dtype↔struct 对齐规则）与 buffer 绑定。
   uniform 相关的 f-string 注入全部收敛到 manifest 驱动；代码骨架 f-string
   仅限结构拼接（缩进/语句顺序），**不允许重排任何浮点表达式**（保 G1）。
3. **MSL emitter 覆盖范围（P7-03）＝ 11 个 SNIPPET 常量 + 基础调色骨架**，
   按效果逐个迁移；每个效果在台账中登记状态机 `gl_only → dual →
   msl_verified`。`dual` = 双 emitter 均可编译该效果；`msl_verified` = G2/G3
   通过且台账留证。
4. **Composite shaders（P7-04）独立台账**，不并入 color 管线重构批次；
   逐效果 A/B（ADR 0003 方法学）+ 覆盖率登记，进度不阻塞 color 主线。
5. **film grain / soft clip**：P6-06 已交付 MSL compute 内核 + numpy 参考
   （`kernels_color.py`），台账首行登记为 `msl_verified(compute)`；其
   fragment 形态接入随 P6-02~04 渲染管线重构。

### 覆盖率门禁（冻结——实施期不得单方放宽）

| 门禁 | 定义 | 判定标准 | 基建 |
|---|---|---|---|
| **G1 GL 回归（位级）** | 重构不得改变现有 OpenGL 渲染输出 | golden corpus（固定输入帧 × 全 catalog 效果 × ≥2 组参数 × 单/多层）RGBA8 输出**位级一致**；触碰 shader_compiler/effect_spec/shader_nodes 的每个 PR 必须全绿 | 扩展 `test_color_e2e_consistency_matrix` + triple golden harness（ModernGL standalone） |
| **G2 MSL 画质** | 每个效果 MSL vs GL 同负载 | SDR 8-bit 输出 ≤1/255 per channel；HDR ≤2e-3 rel（与 triple golden 容差一致）；有 numpy 参考的效果按 P6-06 门禁（确定性位级 + 参考容差） | per-effect 对拍测试 + ModernGL standalone / Metal 真机 |
| **G3 性能（A′ 降级）** | 迁移不得造成性能回退 | 本机：p50 帧时延劣化 >5% 即阻断（回归门禁）；正式 A/B 基线在 Apple Silicon 到位后重测归档 | `tools/bench_render_ab_metal_gl.py` 方法论（ADR 0003） |
| **G4 覆盖率台账** | 「覆盖率」可审计、无静默部分状态 | `docs/adr/0004a-shader-migration-ledger.md` 逐效果登记 状态/日期/PR/证据；「迁移完成」= catalog 全部 `msl_verified` + G1~G3 绿；每 PR 更新，CI 可引用 | 台账文件 |
| **G5 插件 ABI** | 现有 `.tglsl` 零修改可用 | 全部存量插件：GL 路径渲染输出与重构前位级一致（G1 corpus 覆盖）；manifest 校验失败（参数/uniform 映射缺失、类型未知）→ 显式编译错误，禁止静默丢参数 | `tests/color` 插件套件 + G1 corpus |

### 迁移顺序（按效果逐个，fragment 优先）

base grading → curves → LUT3D → qualifier/mask → mixer/warper →
blur/sharpen → blend modes →（film grain / soft clip 的 fragment 形态接入）
→ 插件 manifest 化 → composite shaders（P7-04 单列）。
顺序内每一步独立可交付、独立可回退；中断不产生半迁移状态（G4 保证）。

## 被否决的替代方案

| 方案 | 否决理由 |
|---|---|
| 新建中间表示（IR） | 计划 v2 已定：扩展 `EffectSpec`，不新建 IR——第二份语义清单必然与两台发射器漂移 |
| 正则 GLSL→MSL 机翻 | DCTL ABI 文本同一性无 MSL 等价契约；翻译期语义漂移不可检测（P6-06 实测：连 `dot()` 内建都会引发哈希链整格翻转，文本层面无从发现） |
| MTLArgumentBuffer | 与 GL 侧 `glUniform*` 无单一事实源对应物，破坏决策 2 的 manifest 单源；收益仅在极端 uniform 数量场景（不存在） |
| 一次性重写 composite `shaders.py` | 7,337 行单项最大；违反 ADR 0003 决策 2（渐进双轨） |
| 软件渲染（llvmpipe）做 golden 基线 | 本机已有真实 GL；引入第三种浮点实现反而制造假阳性差异 |
| 放宽 G1 为容差一致 | 「现有 OpenGL 输出像素不变」是 P7-01 的验收原文；容差化会把重构期的语义漂移稀释进噪声，失去回归门禁意义（G2 才是容差门禁） |

## Consequences

- 正面：门禁先行使重构期间任何像素级回归立即暴露；台账使「覆盖率」从
  口头承诺变为可审计数据；MSL emitter 可在任意时点安全中止——已完成
  效果保持已验证状态，不产生半迁移残骸。
- 约束：G1 位级一致 ⇒ GL emitter 重构只允许结构整理（拼接方式/缩进/
  语句归位），**不允许重排或改写任何浮点表达式**；`shader_nodes.py`
  各 SNIPPET 在迁移期间为「GLSL 权威文本」，MSL 版本是其翻译而非并行
  演化（变更必须两侧同步 + G2 复验）。
- 约束：G5 要求插件 manifest 化时对存量 `.tglsl` 全量跑 G1 corpus；
  插件作者可见行为（参数名、tooltip）不得变化。
- 数据修偏：计划 P7-03「10 个 `*_SNIPPET` 常量」应为 **11 个**
  （`LUT3D_SNIPPET` 遗漏）；P7-02「129 处 f-string uniform 注入」实测为
  **186 处 append / 129 处 f-string / 161 处 uniform 命中**，门禁按
  实测口径执行。
- 台账位置：`docs/adr/0004a-shader-migration-ledger.md`（随本 ADR 建立，
  首行 = P6-06 film grain / soft clip）。
