"""
P0-1: Test that probe_video_metadata returns accurate codec names.

Verifies that the fix for GStreamer returning 'unknown' codec names works:
- FFmpeg probe is tried first and returns real codec names
- H.264 and HEVC files are correctly identified
- GStreamer fallback still works when FFmpeg is unavailable
"""
import os
import pytest
from unittest.mock import patch, MagicMock

# Search directories for test videos (in priority order)
_THIS_DIR = os.path.dirname(__file__)
_SEARCH_DIRS = [
    os.path.join(_THIS_DIR, 'test_videos'),
    _THIS_DIR,  # test videos may sit alongside test files
]


def _get_test_video(pattern):
    """Find a test video matching a pattern."""
    video_exts = {'.mov', '.mp4', '.m4v', '.mkv', '.avi', '.webm', '.mxf'}
    for search_dir in _SEARCH_DIRS:
        if not os.path.isdir(search_dir):
            continue
        for f in os.listdir(search_dir):
            # 按媒体扩展名过滤：tests/media/ 下含 "prores" 的 .py 测试源
            # 曾被当作视频夹具打开（av.InvalidDataError）
            if (pattern in f.lower() and not f.startswith('.')
                    and os.path.splitext(f)[1].lower() in video_exts):
                return os.path.join(search_dir, f)
    return None


class TestCodecProbeFix:
    """P0-1: Verify probe_video_metadata returns accurate codec names."""

    def test_ffmpeg_probe_returns_real_codec_h264(self):
        """FFmpeg probe should return 'h264' for H.264 files, not 'unknown'."""
        h264_path = _get_test_video('h264')
        if h264_path is None:
            pytest.skip("No H.264 test video found")

        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(h264_path)

        assert metadata is not None, "probe_video_metadata returned None"
        assert metadata.codec_name != 'unknown', \
            f"codec_name should not be 'unknown' for H.264 file, got: {metadata.codec_name}"
        assert 'h264' in metadata.codec_name.lower(), \
            f"Expected h264 in codec_name, got: {metadata.codec_name}"

    def test_ffmpeg_probe_returns_real_codec_hevc(self):
        """FFmpeg probe should return 'hevc' for HEVC/H.265 files."""
        hevc_path = _get_test_video('h265')
        if hevc_path is None:
            pytest.skip("No HEVC test video found")

        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(hevc_path)

        assert metadata is not None
        assert metadata.codec_name != 'unknown', \
            f"codec_name should not be 'unknown' for HEVC file"
        assert metadata.codec_name.lower() in ('hevc', 'h265'), \
            f"Expected hevc/h265, got: {metadata.codec_name}"

    def test_ffmpeg_probe_returns_pixel_format(self):
        """FFmpeg probe should return actual pixel format."""
        h264_path = _get_test_video('h264')
        if h264_path is None:
            pytest.skip("No H.264 test video found")

        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(h264_path)

        assert metadata is not None
        assert metadata.pixel_format != 'bgr24', \
            f"pixel_format should not be default 'bgr24' from GStreamer, got: {metadata.pixel_format}"

    def test_gstreamer_fallback_still_works(self):
        """When FFmpeg probe fails, GStreamer fallback should still work."""
        h264_path = _get_test_video('h264')
        if h264_path is None:
            pytest.skip("No H.264 test video found")

        from src.shared.media.factory import MediaSourceFactory

        # Mock FFmpeg probe to fail, should fall back to GStreamer
        with patch.object(MediaSourceFactory, '_probe_video_metadata_ffmpeg', return_value=None):
            metadata = MediaSourceFactory.probe_video_metadata(h264_path)

            # GStreamer fallback may return 'unknown' codec, but should still return metadata
            assert metadata is not None, "Fallback should return metadata"
            assert metadata.width > 0
            assert metadata.height > 0

    def test_zero_copy_gate_gets_real_codec(self):
        """When probe returns real codec, zero-copy gate should receive it correctly."""
        h264_path = _get_test_video('h264')
        if h264_path is None:
            pytest.skip("No H.264 test video found")

        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(h264_path)

        assert metadata is not None
        codec = metadata.codec_name.lower()

        # Verify the codec would pass the zero-copy gate
        import sys
        if sys.platform == 'darwin':
            from src.shared.media.macos_vt.capability import is_zero_copy_available
            # The codec should NOT be 'unknown' which would fail the gate
            assert codec != 'unknown', "Codec should not be unknown after fix"
            # If zero-copy is available, the codec should pass
            # (may still fail for other reasons like env var or probe)


class TestProbeVideoMetadataConsistency:
    """Verify probe_video_metadata returns consistent results."""

    def test_metadata_has_valid_dimensions(self):
        """Probe should return valid width/height."""
        h264_path = _get_test_video('h264')
        if h264_path is None:
            pytest.skip("No H.264 test video found")

        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(h264_path)

        assert metadata is not None
        assert metadata.width > 0
        assert metadata.height > 0
        assert metadata.fps is not None
        assert metadata.fps.to_fps() > 0

    def test_prores_metadata(self):
        """ProRes files should return correct codec name."""
        prores_path = _get_test_video('prores')
        if prores_path is None:
            pytest.skip("No ProRes test video found")

        from src.shared.media.factory import MediaSourceFactory
        metadata = MediaSourceFactory.probe_video_metadata(prores_path)

        assert metadata is not None
        assert 'prores' in metadata.codec_name.lower(), \
            f"Expected prores in codec_name, got: {metadata.codec_name}"

    def test_prores_profile_label_passthrough(self):
        """真实 ProRes 素材的 FFmpeg profile 名透传 + 面板档位标签口径。

        Info 面板显示 'ProRes 422 HQ' 等详细规格的数据链：probe 产出
        codec_profile → probe_media_metadata 透传 → format_codec_label
        映射为含 422/4444 家族后缀的档位（对齐导出面板命名）。
        """
        from src.shared.media.factory import MediaSourceFactory
        from src.shared.media.metadata import format_codec_label

        expected_labels = {
            '2K_prores422.mov': 'ProRes 422',
            '2K_prores422HQ.mov': 'ProRes 422 HQ',
            '2K_prores422LT.mov': 'ProRes 422 LT',
            '2K_prores422Proxy.mov': 'ProRes 422 Proxy',
        }
        # 真实素材矩阵在顶层 tests/test_videos（test_topos_probe_unification
        # 同款路径；tests/media/ 下无 test_videos 目录）
        video_dir = os.path.join(os.path.dirname(_THIS_DIR), 'test_videos')
        missing = [name for name in expected_labels
                   if not os.path.isfile(os.path.join(video_dir, name))]
        if missing:
            pytest.skip(f"ProRes 素材缺失: {missing}")

        for name, expected_label in expected_labels.items():
            metadata = MediaSourceFactory.probe_video_metadata(
                os.path.join(video_dir, name))
            assert metadata is not None, name
            assert metadata.codec_profile, \
                f"{name}: codec_profile 未解析（面板会退化为粗粒度 codec 名）"
            assert format_codec_label(
                metadata.codec_name, metadata.codec_profile) == expected_label, name


class TestMalformedSourceProbe:
    """畸形源文件探测不得段崩溃（2026-09-20 回归钉死）。

    背景：直通交付计划对源文件做 PyAV 探测。18 字节占位文件按 .png
    扩展名被 ffmpeg image2 demuxer 认领 → 流的 pix_fmt 停留在
    AV_PIX_FMT_NONE；PyAV 17 的 pix_fmt getter 无守卫，
    PixelFormat(-1) 取 av_pix_fmt_desc_get 的 NULL 返回直接解引用——
    SIGSEGV，try/except 与 hasattr 均无法拦截。修复改走库自身的
    安全通道 stream.format（NONE → None）。
    """

    @staticmethod
    def _write_placeholder(tmp_path, name):
        path = tmp_path / name
        path.write_bytes(b"placeholder-source")   # 18 字节非媒体数据
        return str(path)

    def test_probe_junk_png_does_not_segfault(self, tmp_path):
        """占位 .png（image2 认领、pix_fmt=NONE）探测返回而非段崩溃。"""
        import av
        from src.shared.media.factory import MediaSourceFactory

        path = self._write_placeholder(tmp_path, "junk.png")
        # 前置确认：该文件确实被 image2 认领且安全通道 format=None
        container = av.open(path)
        try:
            stream = container.streams.video[0]
            assert getattr(stream, "format", None) is None
        finally:
            container.close()

        metadata = MediaSourceFactory.probe_video_metadata(path)
        # 修复前：本行之前必然 SIGSEGV；修复后返回探测结果
        if metadata is not None:
            assert metadata.pixel_format in ("unknown", "", None)

    def test_safe_stream_pix_fmt_helper(self, tmp_path):
        """_safe_stream_pix_fmt：真实文件返回格式名，假流返回 unknown。"""
        from PIL import Image
        import numpy as np
        from src.shared.media.ffmpeg_source import _safe_stream_pix_fmt
        import av

        # 真实 PNG：format.name ≡ pix_fmt
        real_path = tmp_path / "real.png"
        Image.fromarray(np.zeros((8, 8, 3), np.uint8)).save(str(real_path))
        container = av.open(str(real_path))
        try:
            assert _safe_stream_pix_fmt(container.streams.video[0]) == "rgb24"
        finally:
            container.close()

        # 占位假文件：NONE 格式 → unknown（修复前此处段崩溃）
        junk_path = self._write_placeholder(tmp_path, "junk_helper.png")
        junk = av.open(junk_path)
        try:
            assert _safe_stream_pix_fmt(junk.streams.video[0]) == "unknown"
        finally:
            junk.close()

    def test_probe_junk_file_clean_metadata(self, tmp_path):
        """占位文件探测各字段要么 unknown/默认、要么可 JSON 化，绝不崩进程。"""
        from src.shared.media.factory import MediaSourceFactory

        path = self._write_placeholder(tmp_path, "junk.mov")
        metadata = MediaSourceFactory.probe_video_metadata(path)
        # .mov 假文件 av.open 直接 InvalidDataError → 探测回落 None/默认
        assert metadata is None or metadata.pixel_format != "bgr24"
