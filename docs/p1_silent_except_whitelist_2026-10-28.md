# P1 静默异常清理·白名单登记（2026-10-28 终态）

> 起点 1270 处 except...:pass + 20 处裸 except:。终态：裸 except **0**；
> pass 体 handler 1210 处，全部为 16 个批次逐处 triage 后判定 KEEP 的设计内吞没
> （资源清理/析构、GL 状态恢复、可选依赖探测、幂等降级、带注释的契约化降级）。
> 60 处升级为日志（该记），1 处该抛类经人工复审单独提交（composite_render_output
> 导出收尾失败不再交付坏文件）。分类明细见各批次提交说明。

## NEEDS-LOGGER 待人工定（模块无 logging 设施，按纪律未新建 logger）

node_properties_panel.py:2488 拾色器写入 / edit_service.py:814 克隆参数不一致 /
node_graph_commands.py:178,268,305 undo 恢复失败×3 / camera3d_node.py:227 fov联动 /
timeline_view_model.py:410 订阅者异常 / verified_ingest.py:113 进度回调 /
tool_facade.py:561 MCP目录 / insights/runtime.py:153 on_done /
transaction_service.py:631 审计sink / audio_sync.py:195+interpretation.py:147 from_dict /
render_cache.py:175 漏收即绿缓存 / export_service.py:911 清单更新整体吞

## 剩余 KEEP 位点按目录分布（file:count 全量见 JSON 附件说明）

- src/features/edit/core: 209
- src/app/controllers: 143
- src/features/composite/ui: 118
- src/features/composite/nodes: 106
- src/features/edit/ui: 73
- src/shared/media: 54
- src/shared/video: 49
- src/features/edit/ai_editing: 43
- src/features/composite/render3d: 43
- src/features/composite/render: 33
- src/shared/export: 28
- src/features/color/core: 24
- src/shared/media/macos_vt: 22
- src/features/ai_harness: 20
- src/shared/media/windows_hw: 17
- src/features/shared/effects: 15
- src/shared/gpu: 14
- src/features/composite/evaluators: 14
- src/app/integrations/file_explorer: 14
- src/features/teaching_animation/render: 12
- src/shared/project: 11
- src/shared/io: 9
- src/features/teaching_animation: 9
- src/features/edit/core/realtime_playback: 9
- src/features/teaching_animation/render/html_renderer_spike: 9
- src/features/composite: 8
- src/features/color/ui: 8
- src/shared/ai_models/generation: 7
- src/features/ai_workflow/adapters: 7
- src/shared/control_surfaces/tangent: 6
- src/features/teaching_animation/ui: 6
- src/app/ui: 6
- src/features/edit/core/scene_cut: 5
- src/features/composite/core: 5
- src/features/deliver/ui: 5
- src/shared/codec: 4
- src/shared/diagnostics: 4
- src/app: 3
- src/shared/color: 3
- src/shared/ai_models: 3
- src/features/teaching_animation/core: 3
- src/features/ai_workflow/runtime: 3
- src/shared/db: 2
- src/shared/tracking: 2
- src/shared/ui/widgets: 2
- src/features/edit/insights: 2
- src/features/edit/core/captions: 2
- src/features/teaching_animation/ai: 2
- src/features/deliver/core: 2
- src/features/ai_workflow/ui: 2
- src: 1
- src/shared/config: 1
- src/shared/i18n: 1
- src/shared/gpu/metal: 1
- src/shared/video/denoise: 1
- src/shared/ui/audio_mixer: 1
- src/features/edit/adapters: 1
- src/features/composite/utils: 1
- src/features/ai_harness/tests: 1
- src/features/ai_workflow/tests: 1

**总计 1210 处**（判定标准与逐处理由见 16 个批次提交 [P1-B01]~[P1-B03]）
