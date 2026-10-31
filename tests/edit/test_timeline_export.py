"""
Timeline Export Tests
测试时间线导出功能。

## 覆盖范围

- TimelineExportConfig 配置验证
- ExportSource, ExportRangeType, ExportCodec 枚举
- TimelineExporter 基本功能
- 估算函数（时长、大小）
- DeliverPage UI 基本功能

## 测试策略

- 使用 Mock 模拟依赖组件
- 测试正常情况和边界情况
- 验证错误处理
"""

from __future__ import annotations

import pytest
import tempfile
import os
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np

# 导入测试模块
from src.features.edit.core.timeline_export import (
    TimelineExporter,
    TimelineExportConfig,
    ExportSource,
    ExportRangeType,
    ExportCodec,
    create_timeline_exporter,
    estimate_export_duration,
    estimate_export_size,
)
from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack, ClipInstance
from src.features.edit.core.project import EditProject, MediaAsset
from src.shared.media.metadata import Rational, VideoMetadata
from src.shared.export.gpu_trail import ExportGpuTrail


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def sample_sequence() -> TimelineSequence:
    """创建示例时间线序列"""
    # 创建视频轨道
    video_track = EditTrack(
        track_id="track_v1",
        name="Video 1",
        track_type="video",
        index=0,
    )

    # 创建简单的时间线序列（使用 mock）
    sequence = MagicMock(spec=TimelineSequence)
    sequence.name = "Test Timeline"
    sequence.sequence_id = "seq_001"
    sequence.duration_frames = 100
    sequence.fps = 24.0
    sequence.settings = MagicMock()
    sequence.settings.width = 1920
    sequence.settings.height = 1080
    sequence.video_tracks = [video_track]
    sequence.all_tracks = [video_track]

    return sequence


@pytest.fixture
def sample_project() -> EditProject:
    """创建示例项目"""
    project = MagicMock(spec=EditProject)
    project.name = "Test Project"
    project.active_timeline_id = "seq_001"
    project.fps = 24.0

    # Mock get_asset_by_id
    def get_asset_by_id(asset_id: str) -> Optional[MediaAsset]:
        if asset_id == "asset_001":
            asset = MagicMock(spec=MediaAsset)
            asset.asset_id = "asset_001"
            asset.path = "/tmp/test_video.mp4"
            asset.media_type = "video"
            asset.display_name = "Test Video"
            asset.metadata = {
                "width": 1920,
                "height": 1080,
                "fps": 24.0,
                "total_frames": 100,
            }
            return asset
        return None

    project.get_asset_by_id = get_asset_by_id

    return project


@pytest.fixture
def sample_frame_provider() -> Any:
    """创建示例帧提供器"""
    provider = MagicMock()

    # Mock open_clip
    provider.open_clip.return_value = "clip_session_001"

    # Mock request_frame
    def request_frame(clip_id: str, frame: int, quality: str = "master") -> Optional[Any]:
        decoded_frame = MagicMock()
        decoded_frame.frame_index = frame
        decoded_frame.pixel_format = "bgr24"
        decoded_frame.width = 1920
        decoded_frame.height = 1080
        decoded_frame.data = np.zeros((1080, 1920, 3), dtype=np.uint8)
        return decoded_frame

    provider.request_frame = request_frame
    provider.close_clip = MagicMock()

    return provider


# =============================================================================
# TimelineExportConfig Tests
# =============================================================================


def test_timeline_exporter_exposes_fail_closed_gpu_trail(
    sample_project, sample_sequence, sample_frame_provider,
):
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )
    assert exporter.gpu_trail_snapshot is None
    exporter._gpu_trail = ExportGpuTrail("test-export")
    exporter._record_gpu_stage("encode_submit", "cpu_encoder")
    exporter._record_gpu_fallback("cpu_encoder_surface_path")
    snapshot = exporter.gpu_trail_snapshot
    assert snapshot is not None
    assert snapshot.stage("encode_submit").backend == "cpu_encoder"
    assert snapshot.full_gpu_claim_reason() == "trail_stage_missing:decode"


def test_explicit_native_gpu_bridge_selects_metal_and_avoids_second_encoder(
    sample_project, sample_sequence, sample_frame_provider,
):
    bridge = SimpleNamespace(
        handles_timeline_video=True,
        output_path="/tmp/native-bridge.mp4",
        width=1920,
        height=1080,
    )
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
        gpu_encode_surface_provider=bridge,
    )
    assert exporter._timeline_graphics_backend == "metal"

    encoder_config = exporter._create_encoder_config(
        TimelineExportConfig(
            codec=ExportCodec.H264,
            output_path="/tmp/native-bridge.mp4",
            resolution=(1920, 1080),
            fps=24.0,
        )
    )
    encoder = exporter._create_encoder(
        "/tmp/native-bridge.mp4",
        encoder_config,
    )
    assert encoder.__class__.__name__ == "_ExternalGpuSurfaceEncoder"
    assert encoder.supports_audio is True


def test_export_config_default_values():
    """测试默认配置值"""
    config = TimelineExportConfig()

    assert config.source == ExportSource.TIMELINE
    assert config.range_type == ExportRangeType.ENTIRE
    assert config.codec == ExportCodec.PRORES_422
    assert config.include_audio == True
    assert config.output_path == ""


def test_bitrate_quality_maps_to_encoder_bitrate(
    sample_project, sample_sequence, sample_frame_provider,
):
    """Deliver bitrate mode must reach FFmpeg as bps, not as CRF."""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        resolution=(1920, 1080),
        fps=24.0,
        quality_mode="bitrate",
        quality=12_000_000_000,
    )

    encoder_config = exporter._create_encoder_config(config)

    assert encoder_config.bitrate == 12_000_000_000
    assert encoder_config.crf is None


def test_cbr_quality_maps_to_fixed_rate_control(
    sample_project, sample_sequence, sample_frame_provider,
):
    """CBR mode must provide a target plus matching min/max/VBV limits."""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        resolution=(1920, 1080),
        fps=24.0,
        quality_mode="cbr",
        quality=12_000_000_000,
    )

    encoder_config = exporter._create_encoder_config(config)

    assert encoder_config.bitrate == 12_000_000_000
    assert encoder_config.min_bitrate == 12_000_000_000
    assert encoder_config.max_bitrate == 12_000_000_000
    assert encoder_config.buffer_size == 24_000_000_000
    assert encoder_config.crf is None


def test_hevc_pixel_format_override_selects_8bit_tier(
    sample_project, sample_sequence, sample_frame_provider,
):
    """HEVC 8-bit 交付档：pixel_format=yuv420p → 直通档 + profile main。

    （2026-09-16 平面直通扩展；未设置时保持 10-bit 默认，直配
    TimelineExportConfig 的既有调用方行为不变。）
    """
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )
    config = TimelineExportConfig(
        codec=ExportCodec.HEVC,
        resolution=(1920, 1080),
        fps=24.0,
        pixel_format="yuv420p",
    )

    encoder_config = exporter._create_encoder_config(config)

    assert encoder_config.codec == "hevc"
    assert encoder_config.pix_fmt == "yuv420p"
    assert encoder_config.profile == "main"


def test_hevc_default_tier_stays_10bit_with_normalized_profile(
    sample_project, sample_sequence, sample_frame_provider,
):
    """HEVC 无覆盖时保持 yuv420p10le 默认；profile 占位/冲突值按位深归一。"""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )
    config = TimelineExportConfig(
        codec=ExportCodec.HEVC,
        resolution=(1920, 1080),
        fps=24.0,
        codec_profile="auto",  # UI "auto" 占位不得直传 avcodec
    )

    encoder_config = exporter._create_encoder_config(config)

    assert encoder_config.pix_fmt == "yuv420p10le"
    assert encoder_config.profile == "main10"

    config.pixel_format = "yuv420p"
    config.codec_profile = "main10"  # 与 8-bit 档冲突 → 位深（pix_fmt）优先
    encoder_config = exporter._create_encoder_config(config)
    assert encoder_config.pix_fmt == "yuv420p"
    assert encoder_config.profile == "main"


def test_export_config_validation_empty_path():
    """测试配置验证：空输出路径"""
    config = TimelineExportConfig(
        output_path="",  # 空路径
    )

    errors = config.validate()
    assert "output_path is required" in errors


def test_export_config_validation_timeline_source():
    """测试配置验证：Timeline 来源需要 timeline_id"""
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id=None,  # 缺少 timeline_id
        output_path="/tmp/output.mov",
    )

    errors = config.validate()
    assert "Timeline source requires timeline_id" in errors


def test_export_config_validation_custom_range():
    """测试配置验证：自定义范围"""
    config = TimelineExportConfig(
        range_type=ExportRangeType.CUSTOM,
        start_frame=100,
        end_frame=50,  # end < start，无效
        output_path="/tmp/output.mov",
    )

    errors = config.validate()
    assert "end_frame must be >= start_frame" in errors


def test_export_config_validation_invalid_resolution():
    """测试配置验证：无效分辨率"""
    config = TimelineExportConfig(
        resolution=(0, 1080),  # width <= 0，无效
        output_path="/tmp/output.mov",
    )

    errors = config.validate()
    assert "Invalid resolution" in errors


def test_export_config_validation_invalid_fps():
    """测试配置验证：无效帧率"""
    config = TimelineExportConfig(
        fps=-1.0,  # fps < 0，无效
        output_path="/tmp/output.mov",
    )

    errors = config.validate()
    assert "fps must be positive" in errors


def test_export_config_valid():
    """测试有效配置"""
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="seq_001",
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.PRORES_422HQ,
        output_path="/tmp/output.mov",
    )

    errors = config.validate()
    assert len(errors) == 0


@pytest.mark.parametrize("pixel_format", ("rgb48le", "rgba64le"))
def test_extract_high_bit_depth_rgb_frame_converts_to_uint8_bgr(pixel_format):
    """10/12-bit master decode must not saturate or swap channels on export."""
    channels = 4 if pixel_format.startswith("rgba") else 3
    source = np.zeros((1, 1, channels), dtype=np.uint16)
    source[0, 0, :3] = (65535, 32768, 0)  # RGB full-scale: red, mid green, no blue
    if channels == 4:
        source[0, 0, 3] = 65535
    decoded = SimpleNamespace(data=source, pixel_format=pixel_format, planes=())

    exporter = TimelineExporter.__new__(TimelineExporter)
    result = exporter._extract_frame_data(decoded)

    assert result is not None
    assert result.dtype == np.uint8
    assert result.shape == (1, 1, 3)
    assert tuple(result[0, 0]) == (0, 128, 255)  # BGR after scale + channel swap


def test_high_bit_depth_frame_uses_dedicated_rgb8_export_source(monkeypatch):
    """CPU export must avoid the unreliable rgb48 conversion path."""
    class FakeSource:
        def __init__(self, path, output_format):
            assert path == "/tmp/ten-bit.mov"
            assert output_format == "rgb24"
            self.opened = False

        def open(self):
            self.opened = True

        def read_frame(self, frame_index):
            assert self.opened is True
            assert frame_index == 7
            return SimpleNamespace(
                data=np.array([[[9, 8, 7]]], dtype=np.uint8),
                pixel_format="rgb24", planes=(),
            )

    import src.shared.media.ffmpeg_source as ffmpeg_source

    monkeypatch.setattr(ffmpeg_source, "FFmpegMediaSource", FakeSource)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_rgb8_sources = {}

    result = exporter._get_export_rgb8_frame("/tmp/ten-bit.mov", 7, "asset-10bit")

    assert result is not None
    assert tuple(result[0, 0]) == (7, 8, 9)


def test_high_depth_planar_yuv_conversion_keeps_uint16_precision():
    """10-bit planar source values must reach the compositor as BGR uint16."""
    # BT.709 limited-range neutral grey: Y=512, Cb=Cr=512.  Use a 2x2 luma
    # plane so cv2's chroma upsample path is exercised as it is in export.
    planes = (
        np.full((2, 2), 512, dtype=np.uint16),
        np.full((1, 1), 512, dtype=np.uint16),
        np.full((1, 1), 512, dtype=np.uint16),
    )
    metadata = SimpleNamespace(color_range="limited", color_matrix="bt709")
    exporter = TimelineExporter.__new__(TimelineExporter)

    result = exporter._convert_yuv_planes_to_bgr16(
        planes, "yuv420p10le", metadata,
    )

    assert result is not None
    assert result.dtype == np.uint16
    assert result.shape == (2, 2, 3)
    # 10-bit code 512 is deliberately not an 8-bit-aligned value.  The
    # resulting full-range sample must therefore retain meaningful low bits.
    assert int(result[0, 0, 0]) > 0
    assert int(result[0, 0, 0]) % 257 != 0
    assert np.max(np.abs(result.astype(np.int32) - result[..., :1].astype(np.int32))) <= 1


def test_high_depth_export_source_uses_planar_decoder(monkeypatch):
    """A 10-bit delivery must bypass the unreliable rgb48 decode conversion."""
    class FakeSource:
        def __init__(self, path, preserve_planes, output_mode):
            assert path == "/tmp/ten-bit.mov"
            assert preserve_planes is True
            assert output_mode == "planar_yuv"
            self.opened = False

        def open(self):
            self.opened = True

        def read_frame(self, frame_index):
            assert self.opened is True
            assert frame_index == 7
            return SimpleNamespace(
                planes=(
                    np.full((2, 2), 512, dtype=np.uint16),
                    np.full((1, 1), 512, dtype=np.uint16),
                    np.full((1, 1), 512, dtype=np.uint16),
                ),
                pixel_format="yuv420p10le",
                metadata=SimpleNamespace(color_range="limited", color_matrix="bt709"),
            )

    import src.shared.media.ffmpeg_source as ffmpeg_source

    monkeypatch.setattr(ffmpeg_source, "FFmpegMediaSource", FakeSource)
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._export_high_depth_sources = {}

    result = exporter._get_export_high_depth_frame("/tmp/ten-bit.mov", 7, "asset-10bit")

    assert result is not None
    assert result.dtype == np.uint16


def test_high_precision_compositor_quantizes_to_uint16():
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._use_high_precision_export = True

    result = exporter._quantize_export_frame(np.array([[[0.5, 0.25, 1.0]]], dtype=np.float32))

    assert result.dtype == np.uint16
    assert tuple(result[0, 0]) == (32768, 16384, 65535)


# =============================================================================
# Enum Tests
# =============================================================================


def test_export_source_enum():
    """测试 ExportSource 枚举"""
    assert ExportSource.COLOR_IMAGE.value == "color_image"
    assert ExportSource.COMPOSITE_OUTPUT.value == "composite"
    assert ExportSource.TIMELINE.value == "timeline"


def test_export_range_type_enum():
    """测试 ExportRangeType 枚举"""
    assert ExportRangeType.ENTIRE.value == "entire"
    assert ExportRangeType.IN_OUT.value == "in_out"
    assert ExportRangeType.SELECTED.value == "selected"
    assert ExportRangeType.CUSTOM.value == "custom"


def test_export_codec_enum():
    """测试 ExportCodec 枚举"""
    assert ExportCodec.H264.value == "h264"
    assert ExportCodec.HEVC.value == "hevc"
    assert ExportCodec.PRORES_422.value == "prores_422"
    assert ExportCodec.PRORES_422HQ.value == "prores_422hq"
    assert ExportCodec.PRORES_4444.value == "prores_4444"
    assert ExportCodec.DNXHR_SQ.value == "dnxhr_sq"
    assert ExportCodec.DNXHR_HQ.value == "dnxhr_hq"
    assert ExportCodec.PNG_SEQUENCE.value == "png_sequence"
    assert ExportCodec.TIFF_SEQUENCE.value == "tiff_sequence"


# =============================================================================
# TimelineExporter Tests
# =============================================================================


def test_exporter_init(sample_project, sample_sequence, sample_frame_provider):
    """测试导出器初始化"""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    assert exporter._project == sample_project
    assert exporter._sequence == sample_sequence
    assert exporter._frame_provider == sample_frame_provider
    assert exporter.is_exporting() == False


def test_exporter_get_progress_initial(sample_project, sample_sequence, sample_frame_provider):
    """测试初始进度"""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    current, total, percent = exporter.get_progress()
    assert current == 0
    assert total == 0
    assert percent == 0.0


def test_exporter_cancel_export(sample_project, sample_sequence, sample_frame_provider):
    """测试取消导出"""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    exporter.cancel_export()

    assert exporter._cancel_requested == True


def test_create_timeline_exporter(sample_project, sample_sequence):
    """测试工厂函数"""
    # 创建 mock frame provider
    mock_provider = MagicMock()

    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=mock_provider,
    )

    assert exporter._project == sample_project
    assert exporter._sequence == sample_sequence


def test_exporter_resolves_source_color_metadata():
    """导出器应使用首个视频素材的已持久化色彩元数据。"""
    asset = MagicMock(spec=MediaAsset)
    asset.is_video = True
    asset.metadata = {
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_matrix": "bt2020nc",
        "color_range": "limited",
    }

    clip = MagicMock()
    clip.asset_id = "asset_001"
    track = MagicMock()
    track.track_type = "video"
    track.clips = (clip,)
    sequence = MagicMock()
    sequence.all_tracks = (track,)
    project = MagicMock()
    project.assets = {"asset_001": asset}

    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._sequence = sequence
    exporter._project = project

    color_metadata = exporter._resolve_export_color_metadata()

    assert color_metadata.primaries == "bt2020"
    assert color_metadata.transfer == "smpte2084"
    assert color_metadata.matrix == "bt2020nc"
    assert color_metadata.range == "limited"


# =============================================================================
# Estimate Functions Tests
# =============================================================================


def test_estimate_export_duration_entire(sample_sequence):
    """测试估算时长：整个时间线"""
    config = TimelineExportConfig(
        range_type=ExportRangeType.ENTIRE,
    )

    duration = estimate_export_duration(config, sample_sequence)

    # 100 frames / 24 fps = ~4.17 seconds
    assert duration == pytest.approx(100 / 24.0, rel=0.01)


def test_estimate_export_duration_custom(sample_sequence):
    """测试估算时长：自定义范围"""
    config = TimelineExportConfig(
        range_type=ExportRangeType.CUSTOM,
        start_frame=10,
        end_frame=50,  # 40 frames
    )

    duration = estimate_export_duration(config, sample_sequence)

    # 40 frames / 24 fps = ~1.67 seconds
    assert duration == pytest.approx(40 / 24.0, rel=0.01)


def test_estimate_export_size_prores(sample_sequence):
    """测试估算大小：ProRes"""
    config = TimelineExportConfig(
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.PRORES_422,
    )

    size = estimate_export_size(config, sample_sequence)

    # ProRes 422: ~147 Mbps
    # 100 frames / 24 fps = ~4.17 seconds
    # Size ≈ 147 Mbps * 4.17s / 8 ≈ 76 MB
    assert size > 0
    # 验证估算范围（合理误差）
    expected_size = int(147_000_000 * (100 / 24.0) / 8 * 1.02)
    assert size == pytest.approx(expected_size, rel=0.1)


def test_estimate_export_size_h264(sample_sequence):
    """测试估算大小：H.264"""
    config = TimelineExportConfig(
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.H264,
    )

    size = estimate_export_size(config, sample_sequence)

    # H.264 HD: ~12 Mbps
    assert size > 0
    # 验证估算范围
    expected_size = int(12_000_000 * (100 / 24.0) / 8 * 1.02)
    assert size == pytest.approx(expected_size, rel=0.2)


# =============================================================================
# DeliverPage Tests (UI)
# =============================================================================


@pytest.mark.skip(reason="DeliverPage requires QApplication - run in GUI test environment")
def test_deliver_page_init():
    """测试 DeliverPage 初始化"""
    from src.features.deliver.ui.deliver_page import DeliverPage

    page = DeliverPage()

    assert page._project is None
    assert page._sequence is None
    assert page.is_exporting() == False


@pytest.mark.skip(reason="DeliverPage requires QApplication - run in GUI test environment")
def test_deliver_page_set_project():
    """测试设置项目"""
    from src.features.deliver.ui.deliver_page import DeliverPage

    page = DeliverPage()
    project = MagicMock()

    page.set_project(project)

    assert page._project == project


@pytest.mark.skip(reason="DeliverPage requires QApplication - run in GUI test environment")
def test_deliver_page_set_sequence():
    """测试设置时间线序列"""
    from src.features.deliver.ui.deliver_page import DeliverPage

    page = DeliverPage()
    sequence = MagicMock()
    sequence.name = "Test Timeline"

    page.set_sequence(sequence)

    assert page._sequence == sequence
    assert page._current_timeline_name == "Test Timeline"


@pytest.mark.skip(reason="DeliverPage requires QApplication - run in GUI test environment")
def test_deliver_page_get_export_config_no_sequence():
    """测试获取配置：无时间线"""
    from src.features.deliver.ui.deliver_page import DeliverPage

    page = DeliverPage()

    config = page.get_export_config()

    # 无序列时返回 None
    assert config is None


@pytest.mark.skip(reason="DeliverPage requires QApplication - run in GUI test environment")
def test_deliver_page_get_export_config_valid():
    """测试获取配置：有效配置"""
    from src.features.deliver.ui.deliver_page import DeliverPage

    page = DeliverPage()
    page.set_sequence(MagicMock())
    page._sequence.sequence_id = "seq_001"
    page._sequence.name = "Test"
    page._sequence.fps = 24.0

    # 设置输出路径
    page._path_input.setText("/tmp/output.mov")

    config = page.get_export_config()

    # 应返回有效配置（如果路径已设置）
    assert config is not None
    assert config.output_path == "/tmp/output.mov"


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.integration
def test_export_workflow_mock(sample_project, sample_sequence, sample_frame_provider):
    """测试导出流程基本逻辑"""
    # 创建导出器
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    # 验证导出器状态
    assert exporter._project == sample_project
    assert exporter._sequence == sample_sequence

    # 测试帧范围计算
    config = TimelineExportConfig(
        range_type=ExportRangeType.ENTIRE,
    )

    start, end = exporter._calculate_frame_range(config)
    assert start == 0
    assert end == sample_sequence.duration_frames

    # 测试自定义范围计算
    config_custom = TimelineExportConfig(
        range_type=ExportRangeType.CUSTOM,
        start_frame=10,
        end_frame=50,
    )

    start, end = exporter._calculate_frame_range(config_custom)
    assert start == 10
    assert end == 50


# =============================================================================
# Edge Cases Tests
# =============================================================================


def test_export_config_empty_timeline_name():
    """测试空时间线名称"""
    config = TimelineExportConfig(
        timeline_id="",  # 空 ID
        output_path="/tmp/output.mov",
    )

    errors = config.validate()
    # 空 timeline_id 对于非 TIMELINE 来源是有效的
    if config.source == ExportSource.TIMELINE:
        assert "Timeline source requires timeline_id" in errors


def test_estimate_export_duration_negative_end(sample_sequence):
    """测试估算时长：负数结束帧（表示到末尾）"""
    config = TimelineExportConfig(
        range_type=ExportRangeType.CUSTOM,
        start_frame=0,
        end_frame=-1,  # -1 表示到末尾
    )

    duration = estimate_export_duration(config, sample_sequence)

    # -1 会被替换为总帧数
    assert duration == pytest.approx(100 / 24.0, rel=0.01)


def test_exporter_multiple_cancel(sample_project, sample_sequence, sample_frame_provider):
    """测试多次取消"""
    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    # 多次取消应该不会出错
    exporter.cancel_export()
    exporter.cancel_export()
    exporter.cancel_export()

    assert exporter._cancel_requested == True


# =============================================================================
# Comp Clip Export Tests (Phase 8)
# =============================================================================


def test_get_layer_frame_comp_clip_resolves_to_video_source(
    sample_project, sample_sequence, sample_frame_provider,
):
    """Comp clip 导出时应解析到原始视频素材，不依赖 Composite UI。"""
    # Arrange: add a comp_clip asset whose original is a video
    comp_asset = MagicMock(spec=MediaAsset)
    comp_asset.asset_id = "comp_asset_001"
    comp_asset.path = "topos://comp_clip/comp_asset_001"
    comp_asset.media_type = "comp_clip"
    comp_asset.is_comp_clip = True
    comp_asset.metadata = {"original_asset_id": "asset_001"}

    original_get_asset = sample_project.get_asset_by_id

    def patched_get_asset(asset_id: str):
        if asset_id == "comp_asset_001":
            return comp_asset
        return original_get_asset(asset_id)

    sample_project.get_asset_by_id = patched_get_asset

    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    layer = MagicMock()
    layer.asset_id = "comp_asset_001"
    layer.source_frame = 10

    # Act
    result = exporter._get_layer_frame(layer)

    # Assert: should have resolved through the original video asset
    assert result is not None
    assert result.shape == (1080, 1920, 3)


def test_get_layer_frame_comp_clip_resolves_to_image_source(
    sample_project, sample_sequence, sample_frame_provider,
):
    """Comp clip from an image source resolves correctly during export."""
    # Arrange: create an image source asset
    img_asset = MagicMock(spec=MediaAsset)
    img_asset.asset_id = "img_asset_001"
    img_asset.path = "/tmp/test_image.png"
    img_asset.media_type = "image"

    comp_asset = MagicMock(spec=MediaAsset)
    comp_asset.asset_id = "comp_asset_002"
    comp_asset.path = "topos://comp_clip/comp_asset_002"
    comp_asset.media_type = "comp_clip"
    comp_asset.is_comp_clip = True
    comp_asset.metadata = {"original_asset_id": "img_asset_001"}

    original_get_asset = sample_project.get_asset_by_id

    def patched_get_asset(asset_id: str):
        if asset_id == "comp_asset_002":
            return comp_asset
        if asset_id == "img_asset_001":
            return img_asset
        return original_get_asset(asset_id)

    sample_project.get_asset_by_id = patched_get_asset

    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    # Patch _get_image_frame to return a dummy image
    dummy_img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    exporter._get_image_frame = lambda path: dummy_img if path == "/tmp/test_image.png" else None

    layer = MagicMock()
    layer.asset_id = "comp_asset_002"
    layer.source_frame = 0

    # Act
    result = exporter._get_layer_frame(layer)

    # Assert
    assert result is not None
    assert result.shape == (1080, 1920, 3)


def test_get_layer_frame_comp_clip_no_original_returns_none(
    sample_project, sample_sequence, sample_frame_provider,
):
    """Empty comp clip (no original_asset_id) returns None during export."""
    comp_asset = MagicMock(spec=MediaAsset)
    comp_asset.asset_id = "comp_asset_003"
    comp_asset.path = "topos://comp_clip/comp_asset_003"
    comp_asset.media_type = "comp_clip"
    comp_asset.is_comp_clip = True
    comp_asset.metadata = {}  # No original_asset_id

    original_get_asset = sample_project.get_asset_by_id

    def patched_get_asset(asset_id: str):
        if asset_id == "comp_asset_003":
            return comp_asset
        return original_get_asset(asset_id)

    sample_project.get_asset_by_id = patched_get_asset

    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    layer = MagicMock()
    layer.asset_id = "comp_asset_003"
    layer.source_frame = 0

    # Act
    result = exporter._get_layer_frame(layer)

    # Assert: empty comp clip has no source, should return None
    assert result is None


def test_get_layer_frame_comp_clip_original_not_found_returns_none(
    sample_project, sample_sequence, sample_frame_provider,
):
    """Comp clip with missing original asset returns None during export."""
    comp_asset = MagicMock(spec=MediaAsset)
    comp_asset.asset_id = "comp_asset_004"
    comp_asset.path = "topos://comp_clip/comp_asset_004"
    comp_asset.media_type = "comp_clip"
    comp_asset.is_comp_clip = True
    comp_asset.metadata = {"original_asset_id": "nonexistent_asset"}

    original_get_asset = sample_project.get_asset_by_id

    def patched_get_asset(asset_id: str):
        if asset_id == "comp_asset_004":
            return comp_asset
        return original_get_asset(asset_id)

    sample_project.get_asset_by_id = patched_get_asset

    exporter = TimelineExporter(
        project=sample_project,
        sequence=sample_sequence,
        frame_provider=sample_frame_provider,
    )

    layer = MagicMock()
    layer.asset_id = "comp_asset_004"
    layer.source_frame = 0

    # Act
    result = exporter._get_layer_frame(layer)

    # Assert: original asset missing, should return None
    assert result is None
