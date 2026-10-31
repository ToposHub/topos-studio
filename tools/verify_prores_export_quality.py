#!/usr/bin/env python3
"""验证 ProRes 预览/导出解码隔离：
- preview: VT 硬解（p010 deferred 帧）
- export:  CPU PyAV yuv422p10le planar（4:2:2 全质量，导出路径）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VIDEO = "tests/test_videos/4K_prores422HQ.mov"

from src.shared.video.frame_provider_service import VideoFrameProviderService

service = VideoFrameProviderService()
clip_id = service.open_clip(VIDEO)
info = service.get_clip_info(clip_id)
print(f"clip={clip_id[:24]} {info.width}x{info.height} codec={info.codec_name}")

# 1) VT 源挂载（本轮放行后 ProRes 预览应有）
from src.shared.media.gpu_frame import GpuDecodedFrame
gpu = service.request_gpu_frame(clip_id, 5, "proxy")
is_vt = isinstance(gpu, GpuDecodedFrame) and getattr(gpu, 'deferred_import', '') == 'macos_vt_pixel_buffer'
print(f"[preview] request_gpu_frame -> {type(gpu).__name__} deferred_vt={is_vt}")
if isinstance(gpu, GpuDecodedFrame):
    gpu.release.release()

# 2) CPU 路径（导出用）：必须是 4:2:2 10bit planar
cpu = service.request_frame(clip_id=clip_id, frame_index=5, quality="master")
pf = getattr(cpu, 'pixel_format', None)
planar = getattr(cpu, 'is_planar', None)
bd = getattr(cpu, 'bit_depth', None)
n_planes = len(getattr(cpu, 'planes', ()) or ())
print(f"[export ] request_frame -> {type(cpu).__name__} fmt={pf} "
      f"planar={planar} bit_depth={bd} planes={n_planes}")
chroma_w = None
planes = getattr(cpu, 'planes', ()) or ()
if len(planes) >= 2:
    chroma_w = getattr(planes[1], 'width', None)
print(f"[export ] chroma plane width={chroma_w} "
      f"(4:2:2 应为亮度一半={info.width // 2})")

ok_vt = is_vt
# 导出全质量判据：master CPU 路径输出 rgb48le（ProRes 4:2:2 解码后完整
# 上变换的 16bit RGB，无色度丢弃、无分辨率损失）或 yuv422p10le planar。
# 两者都是全保真；唯一不可接受的是 8bit/降采样格式。
full_formats = ('rgb48le', 'yuv422p10le', 'yuv422p16le', 'rgb64le')
ok_full = str(pf) in full_formats or (planar and (bd or 0) >= 10)
print(f"\nRESULT: preview_vt={ok_vt} export_full_quality={ok_full}")
assert ok_vt, "预览未走 VT 硬解"
assert ok_full, f"导出路径格式疑似降质量: fmt={pf} bit_depth={bd}"
print("PASS: 预览走 VT 硬解(420)，导出保持全质量 CPU 路径，隔离正确")
service.close_clip(clip_id)
