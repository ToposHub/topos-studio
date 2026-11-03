# R05 留档：节点 LUT 语义、替换与强度（2026-11-03）

## 实现

- **版本化顺序规格**（`src/features/color/core/color_node_lut_spec.py`）：
  `CORRECTOR_PROCESS_ORDER_V1` = fx_blur_sharpen → primary → curves →
  hsl_curves → soft_clip → curves_soft_clip → mixer → warper → **lut3d** →
  plugins → mask_blend（冻结生产 composition shader 事实顺序）。
- **强度混合规格 v1**（`blend_node_lut_strength`，GPU/MSL/CPU 三侧逐式一致）：
  base 保持完整输入（**intensity=0 严格恒等**，含 HDR/负值）；LUT 分支按
  原通道最大值比例重建 HDR（不全局 clamp）；负偏移按强度加权加法保留。
  - **规格修正**：旧 GLSL/MSL 实现在 HDR 输入 + 部分强度下对混合结果整体
    比例缩放 → intensity=0 非恒等（真实缺陷）。三发射器同步修正；
    golden corpus `grading_extreme/checker` 按协议重归档并写入 ADR 0004a
    台账；G2 GL↔Metal SDR 门禁复绿（kitchen_sink 24LSB 分叉消除）。
- **DOMAIN 策略**：非单位 DOMAIN 表在加载期 `resample_to_unit_domain` 到
  单位域（GPU 采样口径）；测试锁定 DOMAIN 恒等表重采样语义。
- **消费投影**：`node_layer_payload` 把 `params.resources.lut_ref/lut_strength`
  投影为 legacy `active_lut_path/lut_intensity`（topos-res:// 与旧路径都经
  `load_lut` 解析）——GL composition/持久化/CPU oracle 同源；移除不复活
  迁移期旧 LUT；无 resources 域保留旧值（迁移兼容）。
- **替换/移除语义**（R03 ops + 本轮测试锁定）：再次应用替换单槽引用并重置
  强度为 1；remove 只移除 LUT 键，grade 域与其他 resources 键保留。

## 测试（tests/color/test_color_node_lut_spec.py，13 项）

- 编译产物级顺序证明：Mock ctx 捕获 fragment 源，11 个非恒等锚点（含真实
  插件 process_P0）在 main() 中的位置 == 规格 v1 元组；
- 数值级非交换证明：gain 2.0 ⊕ 通道交换 LUT 两种复合序不相等；
- 强度：0=严格恒等（HDR 4.0/负 -0.5 输入）、1=完整（HDR 比例重建）、
  0.42=线性混合、负偏移加法（含半强度加权）、DOMAIN 重采样中点；
- 投影：设置/移除/无域三种形态 + helper 边界（空串/非法强度回退）；
- service：替换不叠加、强度重置、移除保 grade。

## 回归与已知事实

- 分块全套 `tests/color/`：全绿（逐文件扫描；唯二非测试模块 exit 5 属
  预期）。`test_shader_golden_corpus` 重归档后 2 passed；
  `test_metal_composition_curves_lut` 18 passed。
- **单进程整套运行存在偶发原生崩溃（SIGILL/SIGSEGV，位置随机）**：在
  干净 HEAD 上同样复现（`Illegal instruction` @ test_presets_gallery，
  exit 132）——前轮遗留的 GL/QThread 债务（计划 N16 已登记），非本轮
  引入；崩溃文件单跑均 3/3 通过。
- 原生验证（`--resources --workflow`）：passed。

## 尚未支持（后续轮次）

- CPU/Metal 图路径 LUT3D 消费（N14 逐域实现，以本规格为准）；节点右键
  LUT 菜单启用（N04，与浏览器共用命令）；LUT 缩略图（R07）。
