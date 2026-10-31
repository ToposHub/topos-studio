"""
Timeline Data Models Unit Tests
测试 TimelineClip、TimelineTrack、Timeline 数据模型。

覆盖：
- TimelineClip 创建、验证、计算属性
- 帧转换函数（timeline_to_source_frame, source_to_timeline_frame）
- 速度调整和剪辑编辑
- TimelineTrack 剪辑管理、重叠检测
- Timeline 多轨道管理
"""

import pytest
from src.shared.media.timeline import (
    TimelineClip,
    TimelineTrack,
    Timeline,
    MIN_SPEED,
    MAX_SPEED,
)
from src.shared.media.metadata import Rational, FPS_24, FPS_30, VideoMetadata


class TestTimelineClipBasic:
    """TimelineClip 基础功能测试"""

    def test_basic_creation(self) -> None:
        """测试基本创建"""
        clip = TimelineClip(
            source_path="/test/video.mp4",
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )
        assert clip.source_path == "/test/video.mp4"
        assert clip.source_start == 100
        assert clip.source_end == 199
        assert clip.timeline_start == 0
        assert clip.timeline_end == 100
        assert clip.speed == 1.0
        assert clip.is_enabled == True
        assert clip.clip_id is not None

    def test_auto_clip_id(self) -> None:
        """测试自动生成 clip_id"""
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        assert clip1.clip_id != clip2.clip_id

    def test_frozen_dataclass(self) -> None:
        """测试不可变性"""
        clip = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        with pytest.raises(AttributeError):
            clip.speed = 2.0  # frozen=True 不允许修改

    def test_hash_and_equality(self) -> None:
        """测试哈希和相等比较"""
        clip1 = TimelineClip(
            clip_id="test-clip-1",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            clip_id="test-clip-1",
            source_start=100, source_end=199,  # 不同内容
            timeline_start=200, timeline_end=300,
        )
        # 相等比较基于 clip_id
        assert clip1 == clip2
        assert hash(clip1) == hash(clip2)


class TestTimelineClipValidation:
    """TimelineClip 验证逻辑测试"""

    def test_negative_source_start_error(self) -> None:
        """测试负数 source_start 错误"""
        with pytest.raises(ValueError, match="source_start cannot be negative"):
            TimelineClip(
                source_start=-1,
                source_end=100,
            )

    def test_negative_source_end_error(self) -> None:
        """测试负数 source_end 错误"""
        with pytest.raises(ValueError, match="source_end cannot be negative"):
            TimelineClip(
                source_start=0,
                source_end=-1,
            )

    def test_source_start_greater_than_end_error(self) -> None:
        """测试 source_start > source_end 错误"""
        with pytest.raises(ValueError, match="source_start .* cannot be greater than"):
            TimelineClip(
                source_start=100,
                source_end=50,
            )

    def test_negative_timeline_start_error(self) -> None:
        """测试负数 timeline_start 错误"""
        with pytest.raises(ValueError, match="timeline_start cannot be negative"):
            TimelineClip(
                source_start=0,
                source_end=100,
                timeline_start=-1,
                timeline_end=100,
            )

    def test_timeline_start_equals_end_error(self) -> None:
        """测试 timeline_start == timeline_end 错误"""
        with pytest.raises(ValueError, match="timeline_start .* must be less than"):
            TimelineClip(
                source_start=0,
                source_end=100,
                timeline_start=100,
                timeline_end=100,
            )

    def test_speed_below_minimum_error(self) -> None:
        """测试速度低于最小值错误"""
        with pytest.raises(ValueError, match="speed must be between"):
            TimelineClip(
                source_start=0,
                source_end=100,
                timeline_start=0,
                timeline_end=100,
                speed=MIN_SPEED - 0.01,
            )

    def test_speed_above_maximum_error(self) -> None:
        """测试速度高于最大值错误"""
        with pytest.raises(ValueError, match="speed must be between"):
            TimelineClip(
                source_start=0,
                source_end=100,
                timeline_start=0,
                timeline_end=100,
                speed=MAX_SPEED + 0.01,
            )

    def test_speed_at_boundaries(self) -> None:
        """测试速度边界值"""
        # 最小速度
        clip_min = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=1000,  # 100帧 / 0.1 = 1000帧
            speed=MIN_SPEED,
        )
        assert clip_min.speed == MIN_SPEED

        # 最大速度
        clip_max = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=25,  # 100帧 / 4.0 = 25帧
            speed=MAX_SPEED,
        )
        assert clip_max.speed == MAX_SPEED

    def test_metadata_validation(self) -> None:
        """测试元数据帧范围验证"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=FPS_24,
            total_frames=1000,
            codec_name="h264",
            codec_profile="High",
            pixel_format="yuv420p",
            bit_depth=8,
        )

        # 有效范围
        clip_valid = TimelineClip(
            source_start=0,
            source_end=999,  # 最后一帧
            timeline_start=0,
            timeline_end=1000,
            metadata=metadata,
        )
        assert clip_valid.metadata == metadata

        # 超出范围
        with pytest.raises(ValueError, match="source_end .* exceeds source total frames"):
            TimelineClip(
                source_start=0,
                source_end=1000,  # 超出 total_frames
                timeline_start=0,
                timeline_end=1001,
                metadata=metadata,
            )


class TestTimelineClipComputedProperties:
    """TimelineClip 计算属性测试"""

    def test_duration_frames(self) -> None:
        """测试 duration_frames 计算"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )
        assert clip.duration_frames == 100

    def test_source_duration(self) -> None:
        """测试 source_duration 计算"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )
        # source_duration = source_end - source_start + 1
        assert clip.source_duration == 100

    def test_effective_duration(self) -> None:
        """测试 effective_duration 计算"""
        # 正常速度
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
            speed=1.0,
        )
        assert clip1.effective_duration == 100

        # 2倍速度
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=50,
            speed=2.0,
        )
        # 100帧 / 2.0 = 50帧
        assert clip2.effective_duration == 50

        # 半速
        clip3 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=200,
            speed=0.5,
        )
        # 100帧 / 0.5 = 200帧
        assert clip3.effective_duration == 200


class TestTimelineClipFrameConversion:
    """TimelineClip 帧转换函数测试"""

    def test_timeline_to_source_frame_normal_speed(self) -> None:
        """测试正常速度下的帧转换"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )

        # timeline 0 -> source 100
        assert clip.timeline_to_source_frame(0) == 100
        # timeline 50 -> source 150
        assert clip.timeline_to_source_frame(50) == 150
        # timeline 99 -> source 199
        assert clip.timeline_to_source_frame(99) == 199

    def test_timeline_to_source_frame_double_speed(self) -> None:
        """测试双倍速度下的帧转换"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=50,  # 100帧 / 2.0 = 50帧
            speed=2.0,
        )

        # timeline 0 -> source 100
        assert clip.timeline_to_source_frame(0) == 100
        # timeline 25 -> source 150 (25 * 2 = 50, +100 = 150)
        assert clip.timeline_to_source_frame(25) == 150
        # timeline 49 -> source 198 (49 * 2 = 98, +100 = 198)
        assert clip.timeline_to_source_frame(49) == 198

    def test_timeline_to_source_frame_half_speed(self) -> None:
        """测试半速下的帧转换"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=200,  # 100帧 / 0.5 = 200帧
            speed=0.5,
        )

        # timeline 0 -> source 100
        assert clip.timeline_to_source_frame(0) == 100
        # timeline 100 -> source 150 (100 * 0.5 = 50, +100 = 150)
        assert clip.timeline_to_source_frame(100) == 150
        # timeline 198 -> source 199 (198 * 0.5 = 99, +100 = 199)
        assert clip.timeline_to_source_frame(198) == 199

    def test_timeline_to_source_frame_outside_range_error(self) -> None:
        """测试帧不在范围内错误"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )

        with pytest.raises(ValueError, match="Timeline frame .* is outside clip range"):
            clip.timeline_to_source_frame(-1)

        with pytest.raises(ValueError, match="Timeline frame .* is outside clip range"):
            clip.timeline_to_source_frame(100)  # 等于 timeline_end，超出范围

    def test_source_to_timeline_frame_normal_speed(self) -> None:
        """测试正常速度下的反向帧转换"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )

        # source 100 -> timeline 0
        assert clip.source_to_timeline_frame(100) == 0
        # source 150 -> timeline 50
        assert clip.source_to_timeline_frame(150) == 50
        # source 199 -> timeline 99
        assert clip.source_to_timeline_frame(199) == 99

    def test_source_to_timeline_frame_double_speed(self) -> None:
        """测试双倍速度下的反向帧转换"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=50,
            speed=2.0,
        )

        # source 100 -> timeline 0
        assert clip.source_to_timeline_frame(100) == 0
        # source 150 -> timeline 25 ((150-100) / 2 = 25)
        assert clip.source_to_timeline_frame(150) == 25
        # source 199 -> timeline 49 ((199-100) / 2 = 49.5 -> 49)
        assert clip.source_to_timeline_frame(199) == 49

    def test_source_to_timeline_frame_outside_range_error(self) -> None:
        """测试源帧不在范围内错误"""
        clip = TimelineClip(
            source_start=100,
            source_end=199,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )

        with pytest.raises(ValueError, match="Source frame .* is outside clip range"):
            clip.source_to_timeline_frame(99)  # 小于 source_start

        with pytest.raises(ValueError, match="Source frame .* is outside clip range"):
            clip.source_to_timeline_frame(200)  # 大于 source_end


class TestTimelineClipFactoryMethods:
    """TimelineClip 工厂方法测试"""

    def test_with_speed(self) -> None:
        """测试速度调整"""
        clip = TimelineClip(
            clip_id="test-clip",
            source_path="/test/video.mp4",
            source_start=0,
            source_end=99,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )

        fast_clip = clip.with_speed(2.0)
        assert fast_clip.speed == 2.0
        assert fast_clip.duration_frames == 50  # 100帧 / 2.0
        assert fast_clip.clip_id == "test-clip"
        assert fast_clip.source_path == "/test/video.mp4"

    def test_with_trim(self) -> None:
        """测试剪辑修剪"""
        clip = TimelineClip(
            clip_id="test-clip",
            source_start=0,
            source_end=99,
            timeline_start=0,
            timeline_end=100,
            speed=1.0,
        )

        trimmed = clip.with_trim(source_start=20, source_end=79)
        assert trimmed.source_start == 20
        assert trimmed.source_end == 79
        assert trimmed.source_duration == 60  # 79 - 20 + 1
        assert trimmed.clip_id == "test-clip"

    def test_with_trim_partial(self) -> None:
        """测试部分修剪"""
        clip = TimelineClip(
            source_start=0,
            source_end=99,
            timeline_start=0,
            timeline_end=100,
        )

        # 只修改 source_end
        trimmed = clip.with_trim(source_end=49)
        assert trimmed.source_start == 0  # 不变
        assert trimmed.source_end == 49

    def test_with_offset(self) -> None:
        """测试时间线偏移"""
        clip = TimelineClip(
            clip_id="test-clip",
            source_start=0,
            source_end=99,
            timeline_start=100,
            timeline_end=200,
        )

        moved = clip.with_offset(50)
        assert moved.timeline_start == 150
        assert moved.timeline_end == 250
        assert moved.clip_id == "test-clip"

    def test_freeze(self) -> None:
        """测试 freeze 方法"""
        clip = TimelineClip(
            source_start=0,
            source_end=99,
            timeline_start=0,
            timeline_end=100,
        )
        frozen = clip.freeze()
        assert frozen == clip  # frozen=True 的 dataclass 直接返回 self


class TestTimelineTrackBasic:
    """TimelineTrack 基础功能测试"""

    def test_empty_track(self) -> None:
        """测试空轨道"""
        track = TimelineTrack(name="Empty Track")
        assert len(track.clips) == 0
        assert track.duration_frames == 0

    def test_track_with_clips(self) -> None:
        """测试带剪辑的轨道"""
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )

        track = TimelineTrack(
            name="Video Track",
            clips=(clip1, clip2),
        )
        assert len(track.clips) == 2
        assert track.duration_frames == 200

    def test_track_type_validation(self) -> None:
        """测试轨道类型验证"""
        track_video = TimelineTrack(track_type="video")
        assert track_video.track_type == "video"

        track_audio = TimelineTrack(track_type="audio")
        assert track_audio.track_type == "audio"

        with pytest.raises(ValueError, match="Invalid track_type"):
            TimelineTrack(track_type="invalid")

    def test_opacity_validation(self) -> None:
        """测试不透明度验证"""
        track = TimelineTrack(opacity=0.5)
        assert track.opacity == 0.5

        with pytest.raises(ValueError, match="opacity must be between"):
            TimelineTrack(opacity=-0.1)

        with pytest.raises(ValueError, match="opacity must be between"):
            TimelineTrack(opacity=1.1)


class TestTimelineTrackOverlapDetection:
    """TimelineTrack 重叠检测测试"""

    def test_no_overlap(self) -> None:
        """测试无重叠的剪辑"""
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )

        track = TimelineTrack(clips=(clip1, clip2))
        assert len(track.clips) == 2

    def test_adjacent_clips(self) -> None:
        """测试相邻剪辑（边界接触）"""
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )

        # timeline_end=100 和 timeline_start=100 是相邻，不重叠
        track = TimelineTrack(clips=(clip1, clip2))
        assert len(track.clips) == 2

    def test_overlap_error(self) -> None:
        """测试重叠剪辑错误"""
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=50, timeline_end=150,  # 与 clip1 重叠
        )

        with pytest.raises(ValueError, match="Clips overlap"):
            TimelineTrack(clips=(clip1, clip2))

    def test_overlap_detection_in_with_clip_added(self) -> None:
        """测试添加剪辑时的重叠检测"""
        clip1 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        track = TimelineTrack(clips=(clip1,))

        # 添加不重叠的剪辑
        clip2 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )
        track_with_clip2 = track.with_clip_added(clip2)
        assert len(track_with_clip2.clips) == 2

        # 添加重叠的剪辑
        clip3 = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=50, timeline_end=150,
        )
        with pytest.raises(ValueError, match="Clip overlaps"):
            track.with_clip_added(clip3)


class TestTimelineTrackQueries:
    """TimelineTrack 查询方法测试"""

    def test_get_clip_at(self) -> None:
        """测试获取指定位置的剪辑"""
        clip1 = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            clip_id="clip-2",
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )
        track = TimelineTrack(track_id="video", clips=(clip1, clip2))

        assert track.get_clip_at(0).clip_id == "clip-1"
        assert track.get_clip_at(50).clip_id == "clip-1"
        assert track.get_clip_at(100).clip_id == "clip-2"
        assert track.get_clip_at(150).clip_id == "clip-2"
        assert track.get_clip_at(200) is None  # 超出范围

    def test_get_clips_in_range(self) -> None:
        """测试获取范围内的剪辑"""
        clip1 = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            clip_id="clip-2",
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )
        clip3 = TimelineClip(
            clip_id="clip-3",
            source_start=0, source_end=99,
            timeline_start=200, timeline_end=300,
        )
        track = TimelineTrack(clips=(clip1, clip2, clip3))

        # 范围完全在 clip1 内
        clips = track.get_clips_in_range(0, 50)
        assert len(clips) == 1
        assert clips[0].clip_id == "clip-1"

        # 范围跨两个剪辑
        clips = track.get_clips_in_range(50, 150)
        assert len(clips) == 2
        assert clips[0].clip_id == "clip-1"
        assert clips[1].clip_id == "clip-2"

        # 范围跨所有剪辑
        clips = track.get_clips_in_range(0, 300)
        assert len(clips) == 3

    def test_get_clip_by_id(self) -> None:
        """测试按 ID 获取剪辑"""
        clip1 = TimelineClip(clip_id="clip-1", timeline_start=0, timeline_end=1)
        clip2 = TimelineClip(clip_id="clip-2", timeline_start=1, timeline_end=2)
        track = TimelineTrack(clips=(clip1, clip2))

        assert track.get_clip_by_id("clip-1") == clip1
        assert track.get_clip_by_id("clip-2") == clip2
        assert track.get_clip_by_id("non-existent") is None


class TestTimelineTrackEditing:
    """TimelineTrack 编辑方法测试"""

    def test_with_clip_added(self) -> None:
        """测试添加剪辑"""
        track = TimelineTrack()
        clip = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )

        new_track = track.with_clip_added(clip)
        assert len(new_track.clips) == 1
        assert new_track.clips[0] == clip
        assert track.clips == ()  # 原轨道不变（不可变）

    def test_with_clip_removed(self) -> None:
        """测试移除剪辑"""
        clip1 = TimelineClip(clip_id="clip-1", timeline_start=0, timeline_end=1)
        clip2 = TimelineClip(clip_id="clip-2", timeline_start=1, timeline_end=2)
        track = TimelineTrack(clips=(clip1, clip2))

        new_track = track.with_clip_removed("clip-1")
        assert len(new_track.clips) == 1
        assert new_track.clips[0].clip_id == "clip-2"

    def test_with_clip_updated(self) -> None:
        """测试更新剪辑"""
        clip1 = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            clip_id="clip-2",
            source_start=0, source_end=99,
            timeline_start=100, timeline_end=200,
        )
        track = TimelineTrack(clips=(clip1, clip2))

        # 更新 clip1（移动位置）
        updated_clip1 = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=99,
            timeline_start=50, timeline_end=150,
        )

        # 这会导致与 clip2 重叠
        with pytest.raises(ValueError, match="Updated clip overlaps"):
            track.with_clip_updated(updated_clip1)

        # 更新 clip1 到有效位置
        valid_clip1 = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=49,
            timeline_start=0, timeline_end=50,
        )
        new_track = track.with_clip_updated(valid_clip1)
        assert new_track.clips[0].timeline_end == 50


class TestTimelineBasic:
    """Timeline 基础功能测试"""

    def test_empty_timeline(self) -> None:
        """测试空时间线"""
        timeline = Timeline(name="Empty Timeline")
        assert len(timeline.tracks) == 0
        assert timeline.duration_frames == 0

    def test_timeline_with_tracks(self) -> None:
        """测试带轨道的时间线"""
        clip = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        track = TimelineTrack(clips=(clip,))
        timeline = Timeline(tracks=(track,))

        assert len(timeline.tracks) == 1
        assert timeline.duration_frames == 100

    def test_timeline_fps(self) -> None:
        """测试时间线帧率"""
        timeline = Timeline(fps=FPS_30)
        assert timeline.fps == FPS_30

        timeline_custom = Timeline(fps=Rational(120, 1))
        assert timeline_custom.fps.to_float() == 120.0

    def test_track_type_properties(self) -> None:
        """测试轨道类型属性"""
        video_track = TimelineTrack(track_type="video")
        audio_track = TimelineTrack(track_type="audio")
        subtitle_track = TimelineTrack(track_type="subtitle")

        timeline = Timeline(tracks=(video_track, audio_track, subtitle_track))

        assert len(timeline.video_tracks) == 1
        assert len(timeline.audio_tracks) == 1
        assert len(timeline.subtitle_tracks) == 1


class TestTimelineQueries:
    """Timeline 查询方法测试"""

    def test_get_track_by_id(self) -> None:
        """测试按 ID 获取轨道"""
        track1 = TimelineTrack(track_id="track-1")
        track2 = TimelineTrack(track_id="track-2")
        timeline = Timeline(tracks=(track1, track2))

        assert timeline.get_track_by_id("track-1") == track1
        assert timeline.get_track_by_id("track-2") == track2
        assert timeline.get_track_by_id("non-existent") is None

    def test_get_clip_at(self) -> None:
        """测试获取指定位置的剪辑"""
        clip1 = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        clip2 = TimelineClip(
            clip_id="clip-2",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        track1 = TimelineTrack(track_id="track-1", clips=(clip1,))
        track2 = TimelineTrack(track_id="track-2", clips=(clip2,))
        timeline = Timeline(tracks=(track1, track2))

        clips = timeline.get_clip_at(50)
        assert "track-1" in clips
        assert "track-2" in clips
        assert clips["track-1"].clip_id == "clip-1"
        assert clips["track-2"].clip_id == "clip-2"

    def test_get_clip_at_with_type_filter(self) -> None:
        """测试带类型过滤的获取剪辑"""
        clip = TimelineClip(timeline_start=0, timeline_end=100)  # 设置有效的时间范围
        video_track = TimelineTrack(track_id="video", track_type="video", clips=(clip,))
        audio_track = TimelineTrack(track_id="audio", track_type="audio")
        timeline = Timeline(tracks=(video_track, audio_track))

        # 只获取视频轨道
        clips = timeline.get_clip_at(50, track_type="video")
        assert "video" in clips
        assert "audio" not in clips


class TestTimelineEditing:
    """Timeline 编辑方法测试"""

    def test_with_track_added(self) -> None:
        """测试添加轨道"""
        timeline = Timeline()
        track = TimelineTrack(track_id="new-track")

        new_timeline = timeline.with_track_added(track)
        assert len(new_timeline.tracks) == 1
        assert new_timeline.tracks[0] == track

    def test_with_track_removed(self) -> None:
        """测试移除轨道"""
        track1 = TimelineTrack(track_id="track-1")
        track2 = TimelineTrack(track_id="track-2")
        timeline = Timeline(tracks=(track1, track2))

        new_timeline = timeline.with_track_removed("track-1")
        assert len(new_timeline.tracks) == 1
        assert new_timeline.tracks[0].track_id == "track-2"

    def test_with_clip_added(self) -> None:
        """测试在轨道中添加剪辑"""
        track = TimelineTrack(track_id="video")
        timeline = Timeline(tracks=(track,))
        clip = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )

        new_timeline = timeline.with_clip_added("video", clip)
        assert len(new_timeline.tracks[0].clips) == 1

        # 轨道不存在
        with pytest.raises(ValueError, match="Track not found"):
            timeline.with_clip_added("non-existent", clip)

    def test_with_clip_removed(self) -> None:
        """测试在轨道中移除剪辑"""
        clip = TimelineClip(clip_id="clip-1")
        track = TimelineTrack(track_id="video", clips=(clip,))
        timeline = Timeline(tracks=(track,))

        new_timeline = timeline.with_clip_removed("video", "clip-1")
        assert len(new_timeline.tracks[0].clips) == 0

    def test_with_clip_updated(self) -> None:
        """测试在轨道中更新剪辑"""
        clip = TimelineClip(
            clip_id="clip-1",
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
        )
        track = TimelineTrack(track_id="video", clips=(clip,))
        timeline = Timeline(tracks=(track,))

        updated_clip = TimelineClip(
            clip_id="clip-1",
            source_start=50, source_end=99,
            timeline_start=0, timeline_end=50,
        )
        new_timeline = timeline.with_clip_updated("video", updated_clip)
        assert new_timeline.tracks[0].clips[0].source_start == 50


class TestTimelineConstants:
    """Timeline 常量测试"""

    def test_speed_constants(self) -> None:
        """测试速度范围常量"""
        assert MIN_SPEED == 0.1
        assert MAX_SPEED == 4.0