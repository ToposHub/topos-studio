# T-P1-10 TOPOS identity 直通快路径

状态：**DONE**

## 性能（48+8 帧协议，同硬件同素材）
- 改前 identity→topos_proxy：**10.15fps**（主 render path）
- 改后 identity→topos_proxy：**80.05fps**（7.9x；阶段目标 ≥80 达标）
- codec-only 参考：90.22fps（直通达到 88.7%）
- 编码器仍为 CPU：delivery_path=topos_direct_planar_cpu_encode，不虚报 GPU

## 实现
- **plan**：timeline_render_plan.DirectToposDeliveryPlan + build_direct_topos_delivery_plan()：严格 eligibility 纯函数（单一启用视频轨/单一连续 clip/源输出均 TOPOS 422 10-bit/尺寸 fps 等于源/无效果调色几何转场字幕 alpha 音频），任一不满足返回 reason 并走普通 render path
- **delivery**：TimelineExporter._export_direct_topos：ToposMediaSource.read_frame planar 帧直接交 ToposVideoEncoder——零 topos_frame_to_rgb/BGR48/resize/重复 FFmpeg source
- **honesty**：delivery_path=topos_direct_planar_cpu_encode（ExportOutcome + telemetry）；legacy trail 显式 encode_submit=cpu_encoder + fallback=topos_direct_planar_cpu_encode，基准表不出现误导性 full_gpu/unknown
- **cancel**：逐帧检查取消；encoder.abort() 清理；无目标文件/.topos-tmp 残留（测试钉死）
- **metadata_fallback**：asset 元数据不全时用共享 probe 缓存补全（T-P0-10 单次探测复用，不重复探测）

## 验证
- **output_probe**：topos / yuv422p10le / 1920x1080 / 25fps / 48 帧（native probe）
- **pixel_check**：首/中/尾帧 Y 平面均值直通输出 vs 源直读 |Δmean| ≤ 0.5（bit 级近一致）；Y ≤ 1023
- **cancel**：第 4 帧取消 → outcome.cancelled；无输出文件；无 .topos-tmp 残留
- **fallback**：带效果 clip → delivery_path=''（普通路径），导出成功

## 测试
- tests/media/test_topos_direct_delivery.py 14 passed
- test_topos_timeline_export + test_topos_source_export_passthrough 27 passed
- timeline_export + stage_telemetry + passthrough 68 passed
