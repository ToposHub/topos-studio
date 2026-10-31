# F3.1 · 统一音频图与嵌套语义复核——台账

> 日期：2026-10-23；基线 commit：`203e239ec`（feat/f-plan）。输入：嵌套音频专项审计（7 节精查）。

## 1. 复核结论（既有闭环保持）

三路径（实时/成片/Stem）共用 `flatten_nested_audio_tracks` 纯函数与同一 retime/FX/automation 求值原语；双层嵌套/防环/深度 8 上限/resolver 递归查找、导出 strict 阻断（编码器前）、Stem 对话框开箱预检——测试矩阵完备，保持不动。

## 2. 本批实现

| 项 | 内容 | 测试 |
|---|---|---|
| 子音轨 crossfade 静默丢 → 结构化诊断 | `nested_audio_transition_dropped`（nested_sequence.py flatten；clip 级 fade_in/out 仍保留） | gate 新增 2 用例（非 strict 诊断 + strict 抛错知情 + 虚拟轨仍在场） |
| **Deliver 入队前嵌套音频预检**（消除"任务运行才失败"的不一致） | `video_export_preflight._check_nested_audio`：渲染期必阻断的 code（retime/fps/depth/cycle/resolver-missing/child-missing）→ **BLOCKING**；静默降级（转场丢弃）→ **WARNING**；`include_audio=False` 跳过；与 Stem 对话框预检对齐 | 新文件 7 用例（BLOCKING×2/WARNING/跳过/健康通过/缺上下文跳过/编排注册） |
| **预览不再静默** | edit_page 展开收集 diagnostics → `_last_nested_audio_diagnostics` 属性 + `signal_hub.nested_audio_diagnostics_changed` 信号（UI 波形/状态提示的接入钩子）+ 逐条 warning 日志 | 实时壳测试：父 retime 嵌套 → 属性含 code + 信号发射 |
| freeze 门触发形态补测 | freeze_source_frame → `nested_audio_retime_unsupported`（此前零测试） | gate 新增 1 用例 |
| clip 级 fade 存活断言 | fade_in/out_duration 经 flatten 保留（此前无断言） | gate 新增 1 用例 |

## 3. 登记的后续差量（带设计约束，不在本批实施）

| 差量 | 现状 | 约束/路径 |
|---|---|---|
| 父 retime 子音频重采样混入（时长对齐视频） | 整实例跳过（现在三处可见：预览信号/入队 BLOCKING/渲染 strict） | 自然路径＝复用 stem_export WSOLA 保调通道 + AudioDecoder 重采样；触及共享 flatten/渲染管线，属可审查大包，建议随 F3.2（实时保调）一并设计 |
| 子序列混合 fps 帧域重映射 | 跳过 + BLOCKING | **视频侧对 mixed fps 无守卫（子帧直读）**——音频单侧修会加剧声画不同步，须 AV 两侧一致设计 |
| 子音轨 crossfade 完整平移/等效渲染 | 丢弃 + WARNING | 依赖父 retime 语义先定 |
| Ref-only 孙序列 resolver 缺失 | BLOCKING（入队显式化，本批已覆盖其可见性） | 加载时全量 promote 或 U03 扩展——归 F6.2 重连域 |

## 4. 测试汇总

本批影响域 79 passed（nested flatten/gate/preflight×2）；全局音频回归（parity/stem/audio_graph）保持绿。

## 5. 未验证

- 真实对白素材的父 retime 嵌套听感——当前语义为"显式拒绝"，无可听结果（见 §3 第一项实现后补）。
- NTSC 相邻样本边界：F0.4 精确向量 + D06 既有测试覆盖非嵌套路径；嵌套路径在 mixed-fps 实现前不涉及。
