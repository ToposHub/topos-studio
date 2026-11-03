# N02 — key 边/Qualifier 图域化（2026-11-04）

计划任务 N02（P0）：把 key 依赖显式编进 plan 执行——key_out 真实产物、
key_in 消费（含 Invert Key Input）、组合顺序 GL/CPU 一致定义；**移除
无依据的常白/图像 R 通道替代**（缺产物/缺边一律 fail-closed）。

## 交付（提交 065d435 + 菜单启用）

### GL 执行器（color_graph_executor.py）
- **key 通道**：`key_outputs[(node_id, port)]` 与图像输出分离。
  Corrector 的最终 matte（qualifier/window（matte_provider）→ 外部
  key 相乘，`invert_key_in` 先取反）登记为本节点 `key_out` 产物；
  KeyMixer 混合结果登记为 `key` 产物；Outside 补集登记为 `key_out`。
- **消费**：corrector `key_in` 边 → 外部 key 组合；KeyMixer/Alpha
  Output/Outside 从 key 通道解析真实 matte；Parallel Mixer 声明的
  key 槽按 slot 对齐解析（未声明槽保留 spec §1 的全权重白）。
- **fail-closed**：key 引用无产物 → `key_output_missing`；KeyMixer/
  AlphaOutput/Outside 无 key 边 → `key_input_missing`。全部不发布
  半帧。白 matte 仅存两处 spec 依据场景（blend 无 key 语义、parallel
  未声明槽）。
- 执行器中间体（invert/multiply）入 lease 池管理；宿主 provider 纹理
  不 lease（归属宿主缓存）。

### CPU oracle（color_graph_cpu.py）
- `run_plan_cpu` 同一组合顺序：`_cpu_node_matte` = qualifier
  （keyer numpy 移植）→ 窗口（combine_masks_cpu，不可评估形状显式
  UnsupportedNode）→ 外部 key（invert 先取反再相乘）；corrector 以
  `mask=` 传入 oracle；key 产物登记同 GL。
- KeyMixer/Outside/AlphaOutput/Parallel key 槽与 GL 同解析与同
  fail-closed 语义（三端「缺 key」分歧消除：GL 曾白、CPU 曾黑、
  Metal 曾直通——现在 GL/CPU 拒绝，Metal 显式禁用）。
- `production_layer_domain_errors` 新增：qualifier 非中性 / mask_shapes
  为空间变化域，不在点态基础域（裸 oracle/能力矩阵/**N13 pointwise
  LUT 导出**都据此拒绝——修复了 N13「带 qualifier 的图导出成功但少
  效果」的漏洞）；`spatial_mask_supplied` 供 run_plan_cpu 显式供 mask
  的路径放行。

### Metal（color_graph_metal.py）
- key 通道（key_out 产物/key_in 消费/补集）Metal 未接线——按「不支持
  显式禁用」：`METAL_CAPABILITY_CATALOG` 将 key_mixer/outside/
  alpha_output 置 unsupported（N14 跨后端域扩展恢复）；corrector 的
  key_in 边与 parallel 声明 key 槽内联守卫拒绝；缺 key 白 matte 死路
  全部改为显式异常。

### UI
- Invert Key Input 菜单启用（此前 enabled=False "not yet supported"）
  ——写入走 `set_node_option("key_invert")` → `params.key_settings.
  invert_key_in`，执行侧 GL/CPU 均消费。

## 验证

- 新测试 tests/color/test_color_graph_key_channel.py（6）：
  1. qualifier key_out：GL 上传回读 ↔ CPU 移植逐像素对拍（梯度源）；
  2. key_in 消费：invert → 1−q；不 invert + 自带 qualifier → qA×qB
     （GL+CPU 双断言）；
  3. Outside 反相互补（1−qA）+ KeyMixer intersect（qA∩(1−qA)）；
  4. fail-closed：无 qualifier 的 key 引用 → key_output_missing（CPU
     UnsupportedNode + GL 不发布）；alpha 无 key 边 → key_input_missing；
  5. 窗口 key_out（circle）+ CPU 对拍 + 界内外数值断言。
- 回归：keys/mixers/gl_execution/cpu_parity/backend_capabilities/
  resource_execution/graph_lut_export/m06 keyer/qualifier/mask CPU +
  Metal 结构测试 + service/UI contract —— 190+ 项全绿。
- 原生验证：`TOPOS_COLOR_GRAPH_RENDER=1 ... --workflow` →
  status=passed（JSON 存本目录）。

## 已知边界（如实登记）

- qualifier 后处理（blur/clean_black/clean_white/denoise）在 CPU 图
  路径显式拒绝（`qualifier_postprocess_active`）；GL 生产路径经宿主
  `_apply_matte_postprocess` 支持（async_renderer，未改）。
- bitmap/curve/brush 窗口在 CPU 图路径显式拒绝（mask_shapes_cpu 的
  unsupported 报告语义）；GL 生产 matte_provider 支持位图（宿主路径）。
- Metal key 通道归 N14；`GraphExecutionResult` 的 alpha 独立发布字段
  归 N01（alpha_output 作为最终 pass 时其输出即 matte 纹理，已可消费）。
- 白 matte 保留的两处为 spec 依据语义（corrector 无 key 语义的 blend
  全效果；parallel 未声明 key 槽的全权重），非「无依据替代」。
