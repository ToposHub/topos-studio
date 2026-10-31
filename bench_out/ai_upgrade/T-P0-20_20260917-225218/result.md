# T-P0-20 导出阶段 telemetry 与 GPU truthfulness

状态：**DONE**

## 实现
- **schema**：src/shared/export/export_stage_telemetry.py：topos-export-stage-telemetry/1
- **stages**：probe/decode/cpu_to_gpu_upload/render/gpu_to_cpu_download/encode_submit/encode/mux/queue_wait（未采样段显式 backend=none）
- **per_stage_fields**：backend、wall p50/p95、gpu submit 均值/p50/p95、gpu wait、CPU upload/download bytes、sample count、段级 fallback reason
- **full_gpu**：必经 decode/render/encode_submit 全 GPU + encode 硬件编码器 + 无 cpu_encoder_surface_path 等 CPU 标记 + 像素传输为 0；否则 blockers 机器可读
- **root_cause**：fallback 根因 first-wins（后续症状不覆盖）；归因环形缓冲保留 frame_index + origin（上限 64）

## 接线
- TimelineExporter：render/encode/decode/mux wall 计时埋点；_record_gpu_stage/_record_gpu_fallback 同写 legacy trail 与统一 telemetry
- ExportOutcome.stage_telemetry：export_timeline 结束时经 dataclasses.replace 附加
- video_export_worker → _post_export(stage_telemetry=…) → complete_host_export/export_telemetry → finalize_export_with_probe
- manifest[export_telemetry] → JobHandle.result_manifest → topos.get_job_status 可读

## 四类路径验证
- **cpu_only**：full_gpu=false；blockers: stage_backend_cpu:*/cpu_path_marker:cpu_encoder_surface_path/encode_backend_cpu
- **gpu_render_cpu_encode**：full_gpu=false；encode_submit CPU 显式标记
- **native_surface**：full_gpu=true；9 阶段完备；wall p50/p95 + gpu submit 分位有效
- **gpu_init_failure**：根因 first-wins（metal_context_init_failed 不被 cpu_fallback 覆盖）；归因含 frame_index/origin

## 测试
- tests/export/test_export_stage_telemetry.py 10 passed
- frame_trace_schema+timeline_export+macos_gpu_encode_surface 73 passed, 5 skipped
- tests/export 104+12 passed
- topos_source_export_passthrough + edit_gpu_pipeline 5 passed
