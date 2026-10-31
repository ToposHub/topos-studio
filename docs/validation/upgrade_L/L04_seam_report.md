# L04 节点执行/能力接缝 · 收口报告（2026-10-25）

> 角色：Mac 主集成者。试点：C01 gradient_map + C17 edge_extend（alpha 子门首个）。
> 方案：patches/proposal_l04_node_seams.md（§C01/§C17/纪律）按计划落地。

## 接线（完整链：marker→shader→library→evaluator→dispatch→registry→capability）

| 文件 | 变更 |
|---|---|
| `src/features/composite/nodes/lib1_nodes.py`（新） | 生产节点类（marker 协议与 sharpen 等单输入效果逐字一致；C01 tile_padding=0/C17=radius 动态）；**暂存区草案改再导出**（单一事实源） |
| `core/shaders.py` | `_LIB1_GRADIENT_MAP_FRAGMENT`（F16 共享核同式）+ `_LIB1_EDGE_EXTEND_DILATE_FRAGMENT`（一轮扩张 pass：rgb=延伸态/a=solid 标志，首轮 u_first 阈值化）+ `_LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT`（原 alpha 恢复/premult 组装） |
| `evaluators/shader_library.py` | 三个程序注册（_SHADER_SOURCES + STANDARD_NAMES → utility_shaders） |
| `evaluators/filter_evaluator.py` | `_apply_lib1_gradient_map`（_apply_simple_per_pixel）；`_apply_lib1_edge_extend`（乒乓 radius 轮 + 双纹理输出组装，AUD-6001 by-id 释放纪律，finally 释放） |
| `evaluators/base_evaluator.py` | dispatch 单输入段两分支 + **纪律红线：未知 lib1_\* marker raise RuntimeError（被既有 per-node 异常处理接住：AUD-5806 exception 级日志带栈+池纹理回滚+节点空输出——精确诊断、不静默透传、不炸整渲染）** |
| `core/node_engine.py` | NODE_REGISTRY（101 键）+ Filter 分类；修复集成中自查引入的 PointCloud3DNode 拼写 NameError |
| `core/node_capabilities.py` | 两条显式条目（C01 fusible=True/padding=0；C17 fusible=False/padding=8 上限，实际随 radius）；check_registry_consistency 零问题 |

## 证据（tests/composite/test_lib1_nodes.py 11 绿 + registry/capability/migration 子集 368 绿）

环境同 L02/L03。命令：

    .venv/bin/python -m pytest tests/composite/test_lib1_nodes.py tests/content_library_v1/ -v
    .venv/bin/python -m pytest tests/composite/ -k "registry or capabilit or node_engine or lib1 or plugin or snapshot or migration"

- **真实 GL**：三 fragment 编译；C01 像素对拍 F16 oracle（Linear/Smooth 两档，
  atol 6e-4）；**C17 乒乓链**（dilate×3 + output）像素对拍 edge_extend oracle
  **max diff 1.5e-8**（alpha_fringe_hair 夹具，含 hair/彩边/premult 模式）。
  过程中自查修复测试 harness 两处真实缺陷：moderngl 默认 wrap=REPEAT（末列
  邻域环绕到首列——已设 clamp 对齐 CPU 索引钳制语义）与读回方向 flipud 误用。
- registry/categories/capability 一致性（check_registry_consistency 全空）；
  graph migrate + from_dict 往返保留 lib1 节点与参数。
- 回归：composite registry/capability/migration/plugin/snapshot 子集 368 绿
  1 跳 1 xfail 零新增失败。

## 口径与未验（如实）

- evaluator 消费链完整调用（GraphEvaluatorMixin 真图渲染、tile 分块 padding=radius
  动态值、CompositeExportRenderer 导出、图保存/Undo）→ L20 端到端。
- 生产 GPUResourcePool 纹理 wrap 语义与测试 harness 的 clamp 假设一致性 →
  L20 真图渲染时复核（若池纹理为 REPEAT，边界行为需在 evaluator 或池层对齐）。
- C17 分块（tile）语义：capability 声明 padding=8 上限 + 节点 _tile_padding
  动态值，tile_padding_computer 消费路径未实测 → L20。
- 未知 lib1_* raise 的用户可见文案走 logger.exception 路径——UI 面板诊断
  呈现随 L05 内容库入口一起。
