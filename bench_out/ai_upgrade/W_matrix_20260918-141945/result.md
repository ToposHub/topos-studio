# §10 真实工作流回放矩阵 — 可执行批次

- 日期：20260918-141945；每工作流 24 帧
- 结果：6/6 PASS（W01、W03、W04、W06、W07、W08）
- W02 试点已 PASS（独立目录）；W05 保持部分（绿幕 fixture 缺失）——诚实状态。

| 工作流 | 结论 | 关键证据 |
|---|---|---|
| W01 | PASS | {"plan_apply_1": {"applied": true, "error": null, "revision": 1}, "plan_apply_2": {"applied": true, "error": null, "revision": 2}, "export": {"success": true, "cancelled": false, "cause": "", "full_gpu_blockers": ["stage |
| W03 | PASS | {"results": {"h264": {"export": {"success": true, "cancelled": false, "cause": "", "full_gpu_blockers": ["stage_backend_cpu:decode:provider_session", "stage_backend_cpu:render:opengl", "stage_backend_cpu:encode_submit:cp |
| W04 | PASS | {"export": {"success": true, "cancelled": false, "cause": "", "full_gpu_blockers": ["stage_missing:decode", "stage_backend_cpu:render:opengl", "stage_backend_cpu:encode_submit:cpu_encoder", "encode_backend_cpu:h264", "cp |
| W06 | PASS | {"deliverable_apply": {"applied": true, "error": null, "revision": 1}, "identity_remove": {"applied": true, "error": null, "revision": 2}, "tinted_export": {"success": true, "cancelled": false, "cause": "", "full_gpu_blo |
| W07 | PASS | {"portrait_cancel": {"success": false, "cancelled": true, "cause": "", "full_gpu_blockers": ["stage_backend_cpu:decode:provider_session", "stage_backend_cpu:render:opengl", "stage_backend_cpu:encode_submit:cpu_encoder",  |
| W08 | PASS | {"model_capabilities": [{"id": "local_media_analysis", "state": "not_installed"}, {"id": "asr_local", "state": "not_installed"}, {"id": "visual_caption_local", "state": "not_installed"}], "models_honest_unavailable": tru |