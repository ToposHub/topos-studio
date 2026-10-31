# F4.1 · 场景分组与组前/片段/组后处理 — 验证台账

- 日期：2026-10-24
- 提交：`ef0993564`
- 计划条目：docs/feature_plan_F_davinci_2026-10-22.md §F4.1
- 调研：Explore 全量盘点（color_stack 三层现状/消费点/缓存指纹/向量形态，
  2026-10-24）——差距结论：缺组实体、组前后顺序、"共享 look 之上保留
  各镜头微调"的叠加解析（旧模型改共享栈即改全组，微调无处安放）。
- 测试：tests/edit/test_color_group.py（13）＋回归 213 绿
  （color stack 40/预览 grade/LUT 精确性/scene_cut 字段保留/schema
  v3/save router/切换持久化/双击打开）

## 1. 交付清单

| 组件 | 内容 |
|---|---|
| 组实体 | `color_group.py`：`ColorGroupData`（成员=(timeline_id, clip_id) 跨时间线引用、pre/post 组栈、机位标签）+ `group_for_clip` 查询 |
| 领域命令 | `SetColorGroupsCommand` 受限事务（before/after 快照、冲突整体拒绝零半写、undo 对称）+ 建组/加成员/移成员/解组四工厂（唯一归属强制） |
| 持久化 | `EditProject.color_groups` + IO settings 键（识别键 group_id+members；旧工程无键=空表；未知载荷保留）；roundtrip 精确 |
| 微调层 | `ClipInstance.color_trim_stack_id`（None 默认=旧工程字节不变；split/复制经 dataclass replace 全字段保留契约自动继承） |
| 解析单点 | `color_chain.py`：`resolve_color_chain`（pre→主栈→trim→post）+ `chain_extra_params`（高级操作段显式 `_unsupported_advanced` 标记，不静默丢）+ `chain_stack_ids`（指纹输入） |
| 四路消费 | 预览快照/CPU（顺序 GradingMath）；导出快照/CPU（顺序应用 + **分解 LUT fast-path 链段门**——fast-path 只覆盖主参数，链段存在时走全 float，否则组段被静默跳过＝实测缺陷已修）；预览 GPU（scratch 纹理乒乓链 pass）；Metal 导出（deep-u16/planar-u8/packed 三 grade 分支 mid-FBO 乒乓，`_gpu_grade_uniforms` 返回链序列） |
| 缓存 | 链栈 id 入 `build_render_cache_fingerprint` 的 stack_ids（漏收即"绿缓存"） |

## 2. 验收 → 证据

| 计划验收 | 证据 |
|---|---|
| 同场景 ≥12 镜头、两类机位共同改 look，各镜头微调保留 | `test_group_look_change_preserves_per_clip_trim`（12 镜头两机位：组 look 10→40 生效；每镜头 trim（exposure 递增）逐镜头不同且组变更后 trim 段不变） |
| 移组/复制/删除/Undo/重开一致 | 命令事务 4 项（建/加/移/解组 + undo 对称 + 冲突拒绝零半写 + 唯一归属拒绝）+ IO roundtrip（跨时间线成员/pre 栈/机位标签精确保留；旧工程空表）+ split 保留微调引用 |
| 导出一致 | `test_exporter_applies_chain_sequentially`：成片 CPU 输出 == 主→trim→post 逐段 GradingMath oracle（±1 LSB）；预览/导出快照同键 `chain_extra_params` |
| 组前后顺序有画质向量 | `test_order_pre_clip_post_not_commutative`：pre(contrast)→post(exposure) 与交换顺序输出不同（非线性参数不可交换）+ 恒等链零改变（atol 1e-7） |
| 源→工作与输出变换不重复 | `test_group_stacks_carry_no_idt_odt`：组载荷字段集不含任何 transform/IDT/ODT（管线级语义 F5.1 收口，组栈只含调色参数） |
| 已有等价能力验证后复用 | 复用：`asset_default/clip_local` 双层 + 同栈引用共享语义保留不变；`UseSharedGradeCommand`/GradeClipboard/`update_color_stacks` 发布链/指纹机制全部沿用（调研 §复用清单） |

## 3. 顺手修复（pre-existing 缺陷）

- `edit_page._on_clip_open_in_source`：duck-typed 资产（无 `.path`）与工程
  （无 `source_patch_overrides`）两处裸访问 AttributeError 被外层吞——双击
  打开整体失效（`test_handler_opens_asset_and_seeks` 此前红）。getattr
  防御（与 F3.4 成片同口径）+ 过期测试替身（open_asset 不收 F2.1/F2.10
  kwargs）更新。

## 4. 登记的后续片（不阻塞验收）

1. Color 页 UI：组标签页（Pre-clip/Clip/Post-clip 视图切换）与组管理面板
   （模型/命令已就绪；本轮交付渲染链与数据层）。
2. 链段高级操作（curves/LUT/soft-clip）的逐段成片支持（当前 primary 域
   显式标记 + 告警跳过，与 has_advanced_operations 同语义不静默）。
3. 组栈的 `trackings`（蒙版跟踪）链段语义。
4. Composite 页 color_evaluator 链消费（当前 Edit 预览/成片/Metal 已接；
   Composite 经 clip_stack 解析未带组——登记差异）。
5. 分裂新片段的组归属策略（当前不自动入组，与 multicam 显式语义一致）。

## 5. 诚实边界

- 链组合 = 逐段顺序应用（非线性参数无代数合并）——四条渲染路径逐段
  一致；CPU float 与 GPU shader 逐段同源（primary_grade_shader 与
  GradingMath 逐行一致为既有合同）。
- 链段栈含 curves/LUT 时 primary 预览/导出跳过该段并告警（不静默丢），
  完整渲染需后续片 2。
- Metal 导出链 pass 未经真机 GPU 逐位对拍（headless 无 GL 上下文）——
  shader 同源合同 + CPU oracle 已锚，真机对拍登记后续。
