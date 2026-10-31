"""ProRes 预览/导出解码隔离的回归保证。

产品决策：预览允许 4:2:0（VT 硬解，p010）；导出必须全质量（CPU 路径，
ProRes 4:2:2 解码后的 rgb48le 16bit / yuv422p10le planar，无色度丢弃、
无分辨率损失）。VT 源挂载后这两条路必须保持隔离——本测试锁定该契约。
"""

from __future__ import annotations

import sys

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VIDEO = REPO_ROOT / "tests" / "test_videos" / "2K_prores422HQ.mov"

pytestmark = pytest.mark.skipif(
    not VIDEO.exists(), reason="ProRes test fixture missing")

# W01：VT 挂载断言仅 macOS；Windows 预览为 CPU planar 设计路径（非回归）
_vt_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="VT 挂载断言仅 macOS；Windows 预览为 CPU planar 设计路径")


@pytest.fixture(scope="module")
def service_with_clip():
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    service = VideoFrameProviderService()
    clip_id = service.open_clip(str(VIDEO))
    assert clip_id
    yield service, clip_id
    try:
        service.close_clip(clip_id)
    except Exception:
        pass


@_vt_only
def test_prores_attaches_vt_source(service_with_clip):
    """预览路径：ProRes 挂 VT 硬解源（deferred VT 帧）。"""
    service, clip_id = service_with_clip
    from src.shared.media.gpu_frame import GpuDecodedFrame

    gpu = service.request_gpu_frame(clip_id, 3, "proxy")
    assert gpu is not None, "VT 源未挂载或解码失败"
    try:
        assert isinstance(gpu, GpuDecodedFrame)
        assert getattr(gpu, "deferred_import", "") == "macos_vt_pixel_buffer"
    finally:
        if isinstance(gpu, GpuDecodedFrame):
            gpu.release.release()


def test_prores_export_cpu_full_quality(service_with_clip):
    """导出路径：CPU master 解码保持全质量（≥10bit，无降采样格式）。"""
    service, clip_id = service_with_clip

    frame = service.request_frame(clip_id=clip_id, frame_index=3,
                                  quality="master")
    assert frame is not None, "CPU master 解码失败"
    fmt = str(getattr(frame, "pixel_format", "") or "")
    planar = bool(getattr(frame, "is_planar", False))
    bit_depth = int(getattr(frame, "bit_depth", 0) or 0)

    full_formats = ("rgb48le", "yuv422p10le", "yuv422p16le", "rgb64le")
    assert (fmt in full_formats or (planar and bit_depth >= 10)), (
        f"导出路径疑似降质量: fmt={fmt} planar={planar} bit_depth={bit_depth}"
    )


def test_video_node_export_never_requests_vt(service_with_clip):
    """节点层隔离：render_mode 含 'export' 时 _decode_frame 不发起 VT 请求
    （is_export 分支），即使 clip session 挂着 VT 源。"""
    service, clip_id = service_with_clip
    from src.features.composite.nodes.video_frame_node import VideoFrameNode

    node = VideoFrameNode()
    node.parameters["file_path"] = str(VIDEO)
    node._use_service = True
    node._video_context = _StubVideoContext(service, clip_id)

    called = {"gpu": 0}

    def _counting_gpu(**kw):
        called["gpu"] += 1
        return None

    node._video_context.request_gpu_frame_for_node = _counting_gpu
    node._clip_id = clip_id

    # export 同步路径：解码不得触 VT（返回 CPU 帧或 None 均可）
    result = node._decode_frame(3, "export")
    assert called["gpu"] == 0, (
        f"export 求值发起了 {called['gpu']} 次 VT 请求——预览/导出隔离被破坏"
    )
    # 返回值若是 GPU 帧对象则说明隔离失效
    from src.shared.media.gpu_frame import GpuDecodedFrame
    assert not isinstance(result, GpuDecodedFrame)


class _StubVideoContext:
    """仅暴露 _decode_frame 所需的最小接口。"""

    def __init__(self, service, clip_id):
        self._service = service
        self._clip = clip_id

    def get_clip_info(self, clip_id):
        return self._service.get_clip_info(clip_id)

    def request_frame_for_node(self, **kw):
        return self._service.request_frame(
            clip_id=self._clip,
            frame_index=int(kw.get("timeline_frame", 0) or 0),
            quality="master",
        )

    def has_vt_source_for_node(self, clip_id):
        return True

    def request_gpu_frame_for_node(self, **kw):  # 被 counting 版替换
        return None
