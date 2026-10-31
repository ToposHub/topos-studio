"""
AudioMetadata Unit Tests
测试 AudioMetadata 音频元数据模型。

覆盖：
- 基本创建和验证
- total_samples 计算
- is_compressed 判断
- is_pcm 判断
- bitrate 计算
- 常见音频格式
"""

import pytest
from src.shared.media.metadata import AudioMetadata, COMPRESSED_AUDIO_CODECS, UNCOMPRESSED_AUDIO_CODECS


class TestAudioMetadataBasic:
    """AudioMetadata 基础功能测试"""

    def test_basic_creation(self) -> None:
        """测试基本创建"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            channel_layout='stereo'
        )
        assert audio.sample_rate == 48000
        assert audio.channels == 2
        assert audio.channel_layout == 'stereo'

    def test_full_creation(self) -> None:
        """测试完整创建"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            channel_layout='stereo',
            duration_seconds=60.0,
            start_pts=0,
            codec_name='pcm_s16le',
            bit_depth=16
        )
        assert audio.duration_seconds == 60.0
        assert audio.start_pts == 0
        assert audio.codec_name == 'pcm_s16le'
        assert audio.bit_depth == 16

    def test_invalid_sample_rate(self) -> None:
        """无效采样率"""
        with pytest.raises(ValueError, match="Invalid sample rate"):
            AudioMetadata(sample_rate=0, channels=2)

        with pytest.raises(ValueError, match="Invalid sample rate"):
            AudioMetadata(sample_rate=-48000, channels=2)

    def test_invalid_channels(self) -> None:
        """无效声道数"""
        with pytest.raises(ValueError, match="Invalid channel count"):
            AudioMetadata(sample_rate=48000, channels=0)

        with pytest.raises(ValueError, match="Invalid channel count"):
            AudioMetadata(sample_rate=48000, channels=-2)

    def test_invalid_bit_depth(self) -> None:
        """无效位深度"""
        with pytest.raises(ValueError, match="Invalid bit depth"):
            AudioMetadata(sample_rate=48000, channels=2, bit_depth=12)

        with pytest.raises(ValueError, match="Invalid bit depth"):
            AudioMetadata(sample_rate=48000, channels=2, bit_depth=99)


class TestAudioMetadataTotalSamples:
    """total_samples 计算测试"""

    def test_total_samples_basic(self) -> None:
        """基本计算"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            duration_seconds=60.0
        )
        assert audio.total_samples == 2880000  # 48000 * 60

    def test_total_samples_cd_quality(self) -> None:
        """CD 质量"""
        audio = AudioMetadata(
            sample_rate=44100,
            channels=2,
            duration_seconds=180.0  # 3 分钟
        )
        assert audio.total_samples == 7938000  # 44100 * 180

    def test_total_samples_none_duration(self) -> None:
        """无时长返回 None"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2
        )
        assert audio.total_samples is None


class TestAudioMetadataCompressed:
    """is_compressed 判断测试"""

    def test_pcm_is_not_compressed(self) -> None:
        """PCM 不是压缩格式"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_s16le'
        )
        assert audio.is_compressed is False

    def test_mp3_is_compressed(self) -> None:
        """MP3 是压缩格式"""
        audio = AudioMetadata(
            sample_rate=44100,
            channels=2,
            codec_name='mp3'
        )
        assert audio.is_compressed is True

    def test_aac_is_compressed(self) -> None:
        """AAC 是压缩格式"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='aac'
        )
        assert audio.is_compressed is True

    def test_flac_is_compressed(self) -> None:
        """FLAC 是压缩格式（无损压缩）"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='flac'
        )
        assert audio.is_compressed is True

    def test_none_codec_not_compressed(self) -> None:
        """无编码器名称不判断为压缩"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2
        )
        assert audio.is_compressed is False


class TestAudioMetadataPCM:
    """is_pcm 判断测试"""

    def test_pcm_s16le_is_pcm(self) -> None:
        """pcm_s16le 是 PCM"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_s16le'
        )
        assert audio.is_pcm is True

    def test_pcm_s24le_is_pcm(self) -> None:
        """pcm_s24le 是 PCM"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_s24le'
        )
        assert audio.is_pcm is True

    def test_pcm_f32le_is_pcm(self) -> None:
        """pcm_f32le 是 PCM"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_f32le'
        )
        assert audio.is_pcm is True

    def test_mp3_not_pcm(self) -> None:
        """MP3 不是 PCM"""
        audio = AudioMetadata(
            sample_rate=44100,
            channels=2,
            codec_name='mp3'
        )
        assert audio.is_pcm is False

    def test_none_codec_not_pcm(self) -> None:
        """无编码器名称不判断为 PCM"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2
        )
        assert audio.is_pcm is False


class TestAudioMetadataLossless:
    """is_lossless 判断测试"""

    def test_flac_is_lossless(self) -> None:
        """FLAC 是无损"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='flac'
        )
        assert audio.is_lossless is True

    def test_alac_is_lossless(self) -> None:
        """ALAC 是无损"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='alac'
        )
        assert audio.is_lossless is True

    def test_pcm_is_not_lossless_compressed(self) -> None:
        """PCM 不是无损压缩（它是无压缩）"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_s16le'
        )
        assert audio.is_lossless is False

    def test_mp3_not_lossless(self) -> None:
        """MP3 不是无损"""
        audio = AudioMetadata(
            sample_rate=44100,
            channels=2,
            codec_name='mp3'
        )
        assert audio.is_lossless is False


class TestAudioMetadataBitrate:
    """bitrate 计算测试"""

    def test_pcm_bitrate(self) -> None:
        """PCM 比特率"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_s16le',
            bit_depth=16
        )
        # 48000 * 2 * 16 = 1536000 bps
        assert audio.bitrate == 1536000

    def test_24bit_pcm_bitrate(self) -> None:
        """24-bit PCM 比特率"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='pcm_s24le',
            bit_depth=24
        )
        # 48000 * 2 * 24 = 2304000 bps
        assert audio.bitrate == 2304000

    def test_compressed_bitrate_none(self) -> None:
        """压缩格式比特率返回 None"""
        audio = AudioMetadata(
            sample_rate=44100,
            channels=2,
            codec_name='mp3'
        )
        assert audio.bitrate is None

    def test_no_bit_depth_bitrate_none(self) -> None:
        """无位深度返回 None"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2
        )
        assert audio.bitrate is None


class TestAudioMetadataBytesPerSample:
    """bytes_per_sample 计算测试"""

    def test_16bit_bytes(self) -> None:
        """16-bit 每样本字节"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            bit_depth=16
        )
        assert audio.bytes_per_sample == 4  # (16/8) * 2

    def test_24bit_bytes(self) -> None:
        """24-bit 每样本字节"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            bit_depth=24
        )
        assert audio.bytes_per_sample == 6  # (24/8) * 2

    def test_5_1_channels(self) -> None:
        """5.1 声道"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=6,
            bit_depth=16
        )
        assert audio.bytes_per_sample == 12  # (16/8) * 6

    def test_no_bit_depth_default(self) -> None:
        """无位深度默认 16-bit"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2
        )
        assert audio.bytes_per_sample == 4  # 默认 16-bit


class TestCommonAudioFormats:
    """常见音频格式测试"""

    def test_cd_quality(self) -> None:
        """CD 质量"""
        audio = AudioMetadata(
            sample_rate=44100,
            channels=2,
            channel_layout='stereo',
            codec_name='pcm_s16le',
            bit_depth=16,
            duration_seconds=74 * 60  # 74 分钟（CD 最大时长）
        )
        assert audio.is_pcm is True
        assert audio.is_compressed is False
        assert audio.total_samples == 44100 * 74 * 60

    def test_dvd_quality(self) -> None:
        """DVD 质量"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=6,
            channel_layout='5.1',
            codec_name='ac3',
            duration_seconds=120.0
        )
        assert audio.is_pcm is False
        assert audio.is_compressed is True

    def test_bluray_quality(self) -> None:
        """蓝光质量"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=8,
            channel_layout='7.1',
            codec_name='pcm_s24le',
            bit_depth=24
        )
        assert audio.is_pcm is True
        assert audio.bytes_per_sample == 24  # (24/8) * 8

    def test_streaming_aac(self) -> None:
        """流媒体 AAC"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            channel_layout='stereo',
            codec_name='aac'
        )
        assert audio.is_compressed is True
        assert audio.bitrate is None


class TestCodecSets:
    """编码器集合测试"""

    def test_compressed_set_contains_mp3(self) -> None:
        """压缩集合包含 MP3"""
        assert 'mp3' in COMPRESSED_AUDIO_CODECS

    def test_compressed_set_contains_aac(self) -> None:
        """压缩集合包含 AAC"""
        assert 'aac' in COMPRESSED_AUDIO_CODECS

    def test_compressed_set_contains_flac(self) -> None:
        """压缩集合包含 FLAC"""
        assert 'flac' in COMPRESSED_AUDIO_CODECS

    def test_uncompressed_set_contains_pcm(self) -> None:
        """无压缩集合包含 PCM"""
        assert 'pcm_s16le' in UNCOMPRESSED_AUDIO_CODECS
        assert 'pcm_s24le' in UNCOMPRESSED_AUDIO_CODECS
        assert 'pcm_f32le' in UNCOMPRESSED_AUDIO_CODECS

    def test_uncompressed_set_not_contains_mp3(self) -> None:
        """无压缩集合不包含 MP3"""
        assert 'mp3' not in UNCOMPRESSED_AUDIO_CODECS