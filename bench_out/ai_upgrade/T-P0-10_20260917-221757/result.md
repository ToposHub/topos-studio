# T-P0-10 TOPOS native 路由与 probe 单一化

状态：**DONE**

## 实现点
- **detect_media_backend**：MediaSourceFactory.detect_media_backend(path)：TPIC/TPIM 内容嗅探唯一入口，带文件签名缓存；_get_backend_order 复用
- **probe_single_source**：probe_video_metadata → probe_video_metadata_cached：TOPOS 文件 native-only（不再先走必失败的 PyAV probe，VideoStream.width 无效日志消除）
- **probe_cache**：MediaProbeResult（metadata+backend+reason_code+签名）由 import/缩略图/导出/Composite/AI 分析共享；签名变化自动失效
- **failure_once**：native 失败按 path+签名+reason 只 warning 一次；分类 TOPOS_CODEC_LIB_NOT_FOUND / TOPOS_CODEC_INCOMPATIBLE / TOPOS_FILE_OPEN_FAILED
- **lib_resolution**：topos_binding.resolve_topos_codec_library() 三态（ok/incompatible/not_found，带 ABI 校验）；topos_source._get_shared_codec 与快照层同源
- **identity**：probe_media_metadata 持久化 media_backend + media_identity(size+mtime_ns)，无机器绝对 dylib 路径
- **high_depth_bypass**：timeline_export._request_high_depth_decoded_frame 对 TOPOS 分流回主渲染路径

## 集成验证
- metadata: 1920x1080 / 25fps / 96 frames / yuv422p10le / 10-bit / topos_native
- assertions: import(probe)+thumbnail(read_topos_frame_rgb)+Composite video_frame(create→open→probe→read_frame) 全链路无 width 无效日志；重复 probe 缓存命中零新 native open

素材：`tests/test_videos/2K_topos_proxy.mov`

## 测试
- tests/media/test_topos_probe_unification.py 18 passed
- test_topos_source+test_media_source_factory 37 passed; test_topos_source_export_passthrough 5 passed
- codec_probe_fix/quick_project/color_vs_edit/runtime_snapshot 31 passed; edit_gpu/color_monitor/high_bit_depth/resource_leak/topos_raw_sequence 120 passed

## 已知说明
- 旧 dylib 场景经注入 TOPOS_CODEC_LIB=非法文件模拟：reason_code=TOPOS_CODEC_INCOMPATIBLE，metadata=None，不伪装成功
- ffmpeg/ffprobe CLI 本机不可用由 reason_code 显式记录（见 X-TEST-00 环境）
