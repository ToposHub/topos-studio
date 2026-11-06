# KF-07 · 发布清单与旧入口切换

状态：**过渡版本已定义，最终切换待前置验收**（见文末门禁）。

## 过渡版本写权限策略（防双录）

当前是"旧 UI 与新 UI 同时出现"的过渡版本。每个用户动作的真实写权限
归属如下（E09 路由保证单路径，代码层已成立并有测试）：

| 用户动作 | 写入口 | 权威域 |
| --- | --- | --- |
| 关键帧面板：添加/删除/移动/转换/动态属性/粘贴（grade/qualifier/**window**） | `batch_edit_keyframes` 批处理命令（KF-03；窗口轨经 `track_name_is_writable` 谓词 + 形状存在性校验） | `node.params.grade_keyframes` |
| 关键帧面板：复制（copy 请求） | 宿主捕获不可变载荷（正式剪贴板，无工程写） | 会话域剪贴板 |
| 关键帧面板：行锁/启停 | `set_edit_lock` / `set_bypass` 既有命令 | `node.params` / `node.enabled` |
| 关键帧面板：auto 列 | `KeyframeAutoRecorder`（会话域，KF-04） | 不落工程（持久范围待 KF-00） |
| 调色手势（色轮/曲线等）+ auto 开 | `LegacyGradingAdapter` 手势事务，flush 前并入录制批次（KF-04） | base patch + 关键帧同一历史条目 |
| 调色手势（无图编辑上下文） | 旧 LayerController 自动录制（既有语义不动） | `layer.grade_keyframes` |
| 播放/seek/程序化回填/undo/只读投影 | 无写入口（origin 隔离） | — |

- 手势路由按 `graph_edit_context_active()` 二选一，同一手势不会同时
  进两条通道（B06.2 既有设计）。
- 两域并存读路径统一走 `node_layer_payload` 合并视图（params 胜出、
  墓碑压制 legacy）；面板投影、求值、导出、徽标同源。窗口轨预览
  逐帧求值在 `graph_node_layer_provider(frame)` 内补写形状（与导出
  侧 `evaluate_mask_tracks_payload` 同函数同基底）。
- 面板 EditRequest 的 Sizing/FX 目标显式跳过并记录原因（与
  TOOL_COVERAGE.md 一致），不产生静默半写；窗口轨已开放图域写入
  （形状删除/复制仍归 Layer 域 shape 事务——轨道随形状事务清理，
  批命令按形状存在性 fail-closed）。

## 已验证项与证据（测试为证，均在本仓库回归）

| 项 | 证据 |
| --- | --- |
| 一次动作一个历史条目 + 对称 Undo | `tests/app/test_color_keyframes_host.py::TestRealWriteEntry`（真实 ProjectCommandStack） |
| 批量/相邻帧交换/冲突整批拒/锁定拒 | `tests/color/test_color_graph_keyframe_batch.py` |
| 删除不复活（保存/重开/undo） | `tests/color/test_color_graph_keyframe_persistence.py` |
| 手势自动录制单条目 + origin 隔离 | `tests/app/test_color_keyframe_recorder.py` |
| 面板契约（程序性更新零回声/手势才发 seek） | `tests/ui/test_color_keyframes_panel.py` |
| 播放头双向同步/会话切换/过期请求丢弃 | `tests/app/test_color_keyframes_host.py` |
| 导出逐帧求值（窗口/多节点/fast-path matte） | `tests/edit/test_export_mask_keyframes.py`、`tests/edit/test_m06_qualifier_export.py` |
| 播放中保存不固化插值临时值 | `tests/color/test_color_graph_keyframe_persistence.py` |
| 编解码往返/拒绝反例/身份稳定 | `tests/color/test_keyframe_tracks.py` |
| 键盘命令（方括号导航/删除/复制粘贴/Esc） | `tests/ui/test_color_keyframes_panel.py` |
| 正式剪贴板（复制捕获/删源后粘贴/跨 context/分量数签名拒） | `tests/app/test_color_keyframes_host.py::TestRealWriteEntry`（复检批次） |
| qualifier 写入 canonical 全名（裸字段名回归） | `tests/app/test_color_keyframes_host.py::test_qualifier_add_writes_canonical_track_name` |
| 窗口轨图域写入 + 形状存在性 fail-closed + 预览逐帧补写 | `tests/app/test_color_keyframes_host.py`（窗口三例） |
| 时域标志覆盖 legacy 载荷 + 下游传播 + 节点缓存绕过 | `tests/color/test_color_graph_node_cache.py::TestTemporalAnimatedFlag` + 执行器两例 |

## 剩余不等价项（如实登记）

> 2026-11-07 复检批次已消解第 1、2、7 项（保留原文备查，标 ✅）：

1. ✅ **跨片段剪贴板**（已实现）：复制时面板发 copy 请求，宿主立即
   解析并保存不可变值/动态属性/相对帧偏移/能力签名（值分量数）；
   粘贴优先用捕获载荷——源键删除、源片段离开后仍可粘贴；分量数与
   目标轨不匹配整批拒；无捕获且跨 context 整批拒。剩余语义（调色
   复制按 Color/Sizing 模式筛内容）随 Sizing 轨道（第 3 项）。
2. ✅ **窗口轨道图域写入**（已实现）：`track_name_is_writable` 谓词
   准入 + 形状存在性 fail-closed；预览逐帧补写形状与导出同函数；
   形状删除/复制仍归 Layer 域 shape 事务（轨道随形状清理，批命令
   拒绝写已删形状）。
3. **Sizing / FX 轨道**：仅身份登记，全部能力 ✗（原因见
   TOOL_COVERAGE.md；槽位坐标归属待 KF-00 OP-16）。
4. **动态权重非默认档位**：统一权重函数按 Hermite 兼容语义求值，
   真实档位映射 PENDING-KF00（Dissolve 滑杆量测表未填）。
5. **auto 关闭态改既有锚点**：Resolve 参考行为（修改当前/邻近锚点）
   未实现——关闭 auto 的手势不触碰关键帧（保守，待 KF-00 OP-15）。
6. **GPU 导出窗口 matte**：CPU 导出已贯通；GPU 导出的窗口 matte 属
   既有高级能力门裁决域（本计划未改动其语义）。
7. ✅ **时域标志**（已闭环）：编译器 `key_frame_animated` 判据覆盖
   legacy 载荷起源轨道并沿拓扑传播给下游；GL 执行器节点缓存以该
   标志绕过（静像暂停 seek 不命中陈旧纹理）。Metal/CPU 执行器无
   节点缓存（无对应面）。
8. **auto 开关持久化**：会话域状态，不进工程（持久范围待 KF-00）。

## 最终切换门禁（旧写入口退役的前置条件）

1. KF-00 量测表填满（动态权重/Dissolve 档位/首帧锚点/碰撞覆盖）。
2. 真机验收：真实工程写入 → 保存 → 重开 → 全管线导出（V01–V08 中
   依赖实机的项逐条留证）。
3. 上表不等价项逐项决策（实现或显式接受）。

满足后执行切换（一次性提交）：

- 色轮 ◆ 录制/快照/删除按钮改读统一服务（LayerController
  `_maybe_record_grade_keyframes` 退役或降级为兼容 shim）；
- Window 录制入口（ColorController `_maybe_record_mask_keyframes`）
  同步收口；
- Viewer ruler / 节点徽标 / Clip Strip / 工具重置 / 脚本 API 改读
  同一投影（徽标已读合并视图，其余逐个核对消费者）；
- 移除旧快捷键重复入口；演示 fixture 保留为面板手势检查专用
  （不进生产数据/工程——现状已成立）。
