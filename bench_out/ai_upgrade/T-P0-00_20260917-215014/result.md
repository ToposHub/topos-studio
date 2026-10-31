# T-P0-00 单一能力状态模型

- 状态：**DONE**
- runtime snapshot_id：`6c502d4eca200dcf…`

## 条目状态

- `codec.topos` — AVAILABLE
- `codec.topos.profiles` — AVAILABLE
- `encode.videotoolbox` — AVAILABLE
- `gpu.display` — AVAILABLE
- `gpu.metal` — AVAILABLE
- `media.ffmpeg.cli` — UNAVAILABLE（reason_code=FFMPEG_CLI_NOT_FOUND）
- `media.ffmpeg.pyav` — AVAILABLE
- `media.ffprobe.cli` — UNAVAILABLE（reason_code=FFPROBE_CLI_NOT_FOUND）
- `models.nemo_sortformer` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `models.qwen3_align` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `models.qwen3_asr` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `models.qwen3_tts_customvoice` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `models.qwen3_vl_8b_awq` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `models.rapidocr_ocr` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `models.siglip2_base` — UNAVAILABLE（reason_code=MODEL_MANAGER_NOT_PROVIDED）
- `render.gl` — FALLBACK（reason_code=GL_PROBE_DEFERRED_TO_RUNTIME）

## 注入点

- capability_manifest.build_manifest(runtime_snapshot=...) → manifest.runtime + topos export 门禁
- toposctl._capabilities_offline → build_manifest(runtime_snapshot=build_runtime_capability_snapshot())
- AiEditHostBridgeImpl.get_capability_manifest → 进程级缓存快照 + invalidate_runtime_capability_snapshot()
- PreflightContext.runtime_snapshot → _check_runtime_capability fail-closed 门禁

## 验证

- tests/edit/ai_editing/test_runtime_capability_snapshot.py 24 passed
- tool_facade/toposctl/toposctl_offline/capability_status/taep_contracts 117 passed
- video_export_preflight/video_export_formats/media_capability_manifest 48 passed
- ai_edit_controller/context_service/edit_api 49 passed

## 范围说明

- effect/node/preset 级 preview_backend/export_backend 声明属 T-P1-20（Effect Capability Contract），本卡只建立快照基础与交付门禁
- GL 真实后端由渲染/导出 trail 证明（T-P0-20/T-P0-31），快照默认 FALLBACK/GL_PROBE_DEFERRED_TO_RUNTIME
