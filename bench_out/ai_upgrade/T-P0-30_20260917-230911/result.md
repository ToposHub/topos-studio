# T-P0-30 Native VT bridge 前置协商与安全回退

状态：**DONE**

## 实现
- **pure_function**：can_use_native_gpu_bridge(sequence, project, encoder_config, snapshot, export_config)：encoder 创建前全量前置协商，返回 eligible/reason_code/required_output_surface
- **checks**：输出 codec/pix_fmt→surface 映射、奇数尺寸、字幕内容（空轨不阻断）、alpha、VideoToolbox 快照门、转场、逐 clip：效果/调色/合成、transform/crop/opacity/fade、源像素格式（422→topos_422_to_nv12_not_implemented；未知→fail-closed）
- **not_selected**：bridge_not_selected:<reason> 记录到日志 + telemetry root cause + exporter.bridge_selection——与运行中 GPU 失败显式区分
- **init_failure_cleanup**：bridge 初始化失败→清理半成品输出文件、provider=None、记录 bridge_init_failed:<detail>；此刻未写帧，普通 encoder 重新选择
- **protective_error**：_ExternalGpuSurfaceEncoder.encode_frame 对 CPU frame 的保护性报错保留，并有回归测试钉死（预检后不可达）

## 六组场景
- **topos_422_to_h264**：blocked: topos_422_to_nv12_not_implemented
- **topos_422_to_hevc**：blocked: topos_422_to_nv12_not_implemented
- **h264_nv12_to_h264**：eligible (surface nv12)
- **blur**：blocked: clip_effects_present
- **transition**：blocked: transitions_present
- **subtitles**：blocked: subtitles_requested（有时间线字幕内容时）；空字幕轨不阻断
- **odd_size**：blocked: odd_resolution_not_supported

## 真实导出
- **asset**：tests/test_videos/2K_topos_proxy.mov → H.264, TOPOS_EXPORT_NATIVE_VT=1, 8 帧
- **result**：导出成功走 CPU fallback；probe: h264/yuv420p/8帧；bridge_selection.reason_code=topos_422_to_nv12_not_implemented；telemetry.root_cause=bridge_not_selected:...
- **no_pseudo_success**：无 unsupported_topos_metal_output_format 运行中失败；输出完整可解码

## 测试
- tests/edit/test_native_gpu_bridge_eligibility.py 23 passed
- test_timeline_export + export_stage_telemetry + topos_source_export_passthrough 54 passed, 5 skipped
