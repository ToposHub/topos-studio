"""
Channel Layout 导入/存储测试（§4.1）。

验证（计划表 §4.1）：
- 导入时从 PyAV 音频流提取 channel_layout（mono/stereo/5.1/7.1）。
- 存储到 MediaAsset.metadata["audio_info"]["channel_layout"]。
- MediaAsset.audio_channel_layout property 读取。
- 导出时 encoder 用 ChannelLayout 写正确 layout（已在 §4.1 video_encoder 测试）。
- ChannelLayout.from_av_layout_name 解析 PyAV layout 名称。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.channel_layout import ChannelLayout


class TestChannelLayoutParsing:
    """ChannelLayout 解析 PyAV layout 名称。"""

    def test_parse_stereo(self):
        assert ChannelLayout.from_av_layout_name("stereo") == ChannelLayout.STEREO

    def test_parse_mono(self):
        assert ChannelLayout.from_av_layout_name("mono") == ChannelLayout.MONO

    def test_parse_51(self):
        assert ChannelLayout.from_av_layout_name("5.1") == ChannelLayout.SURROUND_5_1

    def test_parse_51_side(self):
        """PyAV 的 5.1(side) 变体。"""
        assert ChannelLayout.from_av_layout_name("5.1(side)") == ChannelLayout.SURROUND_5_1

    def test_parse_71(self):
        assert ChannelLayout.from_av_layout_name("7.1") == ChannelLayout.SURROUND_7_1

    def test_parse_unknown_raises(self):
        """未知 layout 抛 ValueError（拒绝而非截断）。"""
        with pytest.raises(ValueError):
            ChannelLayout.from_av_layout_name("hexagonal")


class TestProbeAudioInfoChannelLayout:
    """_probe_audio_info 提取 channel_layout。"""

    def test_probe_extracts_channel_layout_stereo(self):
        """探测立体声文件提取 channel_layout='stereo'。"""
        from src.features.edit.core.project import _probe_audio_info

        # Mock PyAV
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.type = 'audio'
        mock_stream.channels = 2
        mock_stream.rate = 48000
        mock_stream.codec.name = 'aac'
        mock_layout = MagicMock()
        mock_layout.name = 'stereo'
        mock_stream.layout = mock_layout
        mock_container.streams = [mock_stream]

        with patch('av.open', return_value=mock_container):
            info = _probe_audio_info("/fake/path.wav")

        assert info is not None
        assert info["channel_layout"] == "stereo"
        assert info["channels"] == 2

    def test_probe_extracts_channel_layout_51(self):
        """探测 5.1 文件提取 channel_layout='5.1'。"""
        from src.features.edit.core.project import _probe_audio_info

        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.type = 'audio'
        mock_stream.channels = 6
        mock_stream.rate = 48000
        mock_stream.codec.name = 'ac3'
        mock_layout = MagicMock()
        mock_layout.name = '5.1'
        mock_stream.layout = mock_layout
        mock_container.streams = [mock_stream]

        with patch('av.open', return_value=mock_container):
            info = _probe_audio_info("/fake/surround.mov")

        assert info["channel_layout"] == "5.1"
        assert info["channels"] == 6

    def test_probe_extracts_channel_layout_71(self):
        """探测 7.1 文件提取 channel_layout='7.1'。"""
        from src.features.edit.core.project import _probe_audio_info

        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.type = 'audio'
        mock_stream.channels = 8
        mock_layout = MagicMock()
        mock_layout.name = '7.1'
        mock_stream.layout = mock_layout
        mock_container.streams = [mock_stream]

        with patch('av.open', return_value=mock_container):
            info = _probe_audio_info("/fake/71.mov")

        assert info["channel_layout"] == "7.1"

    def test_probe_no_audio_returns_none(self):
        """无音频流返回 None。"""
        from src.features.edit.core.project import _probe_audio_info

        mock_container = MagicMock()
        mock_video_stream = MagicMock()
        mock_video_stream.type = 'video'
        mock_container.streams = [mock_video_stream]

        with patch('av.open', return_value=mock_container):
            info = _probe_audio_info("/fake/video_only.mov")

        assert info is None

    def test_probe_layout_missing_returns_none_layout(self):
        """layout 属性访问异常时 channel_layout=None（不崩溃）。"""
        from src.features.edit.core.project import _probe_audio_info

        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.type = 'audio'
        mock_stream.channels = 2
        mock_stream.rate = 48000
        # layout 访问抛异常（模拟旧 PyAV 无 layout）
        mock_stream.layout = property(lambda self: (_ for _ in ()).throw(AttributeError))
        # 上面赋值不行，用 side_effect 方式：直接删 layout 让 hasattr False
        del mock_stream.layout
        mock_container.streams = [mock_stream]

        with patch('av.open', return_value=mock_container):
            info = _probe_audio_info("/fake/legacy.wav")

        assert info is not None
        assert info["channel_layout"] is None


class TestMediaAssetChannelLayout:
    """MediaAsset.audio_channel_layout property。"""

    def test_property_reads_from_metadata(self):
        from src.features.edit.core.project import MediaAsset
        asset = MediaAsset(
            asset_id="a1", path="/fake.wav", media_type="audio",
            display_name="test",
            metadata={"audio_info": {"channels": 6, "channel_layout": "5.1"}},
        )
        assert asset.audio_channel_layout == "5.1"
        assert asset.audio_channels == 6

    def test_property_none_when_missing(self):
        from src.features.edit.core.project import MediaAsset
        asset = MediaAsset(
            asset_id="a1", path="/fake.wav", media_type="audio",
            display_name="test",
            metadata={},
        )
        assert asset.audio_channel_layout is None

    def test_property_stereo(self):
        from src.features.edit.core.project import MediaAsset
        asset = MediaAsset(
            asset_id="a1", path="/fake.wav", media_type="audio",
            display_name="test",
            metadata={"audio_info": {"channels": 2, "channel_layout": "stereo"}},
        )
        assert asset.audio_channel_layout == "stereo"


class TestImportExportLayoutRoundtrip:
    """导入 layout → 导出 layout 一致性。"""

    def test_layout_consistent_from_count(self):
        """导入的 channel count 能映射回 ChannelLayout（导出用）。"""
        # 导入探测得到 channels + channel_layout
        test_cases = [
            (1, "mono", ChannelLayout.MONO),
            (2, "stereo", ChannelLayout.STEREO),
            (6, "5.1", ChannelLayout.SURROUND_5_1),
            (8, "7.1", ChannelLayout.SURROUND_7_1),
        ]
        for channels, layout_name, expected in test_cases:
            # 导入存储
            audio_info = {"channels": channels, "channel_layout": layout_name}
            # 导出时从 channel_layout 解析
            parsed = ChannelLayout.from_av_layout_name(audio_info["channel_layout"])
            assert parsed == expected
            assert parsed.channel_count == channels
