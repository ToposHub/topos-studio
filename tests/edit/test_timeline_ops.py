"""
Timeline Operations Tests
时间线纯函数操作测试。

测试覆盖：
- split_clip: 分割剪辑边界测试
- trim_clip_left/right: 裁切边界测试
- move_clip: 移动剪辑测试
- delete_clip: 删除剪辑测试
- add_transition: 添加转场测试
- 帧转换函数测试

验收标准：
- 所有边界情况覆盖
- 纯函数无 PyQt 依赖
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams
from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TransitionInstance,
    TimelineMarker,
)
from src.features.edit.core.timeline_ops import (
    split_clip,
    trim_clip_left,
    trim_clip_right,
    move_clip,
    delete_clip,
    add_transition,
    find_snap_points,
    snap_to_nearest,
    validate_timeline,
    TimelineOperationError,
)
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.shared.media.metadata import Rational


# ================================================================
# Test Fixtures (测试数据)
# ================================================================


@pytest.fixture
def empty_sequence() -> TimelineSequence:
    """创建空时间线。"""
    return TimelineSequence(
        timeline_id="seq1",
        name="Test Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
    )


@pytest.fixture
def track_with_two_clips() -> EditTrack:
    """创建包含两个剪辑的轨道。"""
    clip1 = ClipInstance(
        clip_id="clip1",
        asset_id="asset1",
        track_id="track1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
    )
    clip2 = ClipInstance(
        clip_id="clip2",
        asset_id="asset1",
        track_id="track1",
        timeline_start=100,
        timeline_duration=50,
        source_start=100,
        source_duration=50,
        speed=1.0,
    )
    return EditTrack(
        track_id="track1",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(clip1, clip2),
    )


@pytest.fixture
def sequence_with_track(track_with_two_clips: EditTrack) -> TimelineSequence:
    """创建包含轨道的时间线。"""
    return TimelineSequence(
        timeline_id="seq1",
        name="Test Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        sample_rate=48000,
        video_tracks=(track_with_two_clips,),
    )


# ================================================================
# ClipInstance Tests (剪辑实例测试)
# ================================================================


class TestClipInstance:
    """ClipInstance 基础测试。"""

    def test_clip_creation(self):
        """测试剪辑创建。"""
        clip = ClipInstance(
            clip_id="test1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        assert clip.clip_id == "test1"
        assert clip.timeline_end == 100
        assert clip.source_end == 99

    def test_timeline_to_source_frame(self):
        """测试时间线帧到素材帧转换。"""
        clip = ClipInstance(
            clip_id="test1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=100,
            source_duration=100,
            speed=1.0,
        )
        assert clip.timeline_to_source_frame(0) == 100
        assert clip.timeline_to_source_frame(50) == 150

    def test_timeline_to_source_frame_with_speed(self):
        """测试变速剪辑的帧转换。"""
        clip = ClipInstance(
            clip_id="test1",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=50,  # 2倍速：100帧素材显示为50帧
            source_start=0,
            source_duration=100,
            speed=2.0,
        )
        # 时间线第0帧对应素材第0帧
        assert clip.timeline_to_source_frame(0) == 0
        # 时间线第25帧对应素材第50帧
        assert clip.timeline_to_source_frame(25) == 50

    # ==================== SpeedParams Tests (变速参数测试) ====================

    def test_timeline_to_source_frame_with_speed_params_slow(self):
        """测试 SpeedParams 半速剪辑的帧转换。"""
        clip = ClipInstance(
            clip_id="test_slow",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=200,  # 半速：100帧素材显示为200帧
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=0.5),
        )
        # 时间线第0帧对应素材第0帧
        assert clip.timeline_to_source_frame(0) == 0
        # 时间线第100帧对应素材第50帧
        assert clip.timeline_to_source_frame(100) == 50
        # 时间线第199帧对应素材第99帧
        assert clip.timeline_to_source_frame(199) == 99

    def test_timeline_to_source_frame_with_speed_params_fast(self):
        """测试 SpeedParams 两倍速剪辑的帧转换。"""
        clip = ClipInstance(
            clip_id="test_fast",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=50,  # 两倍速：100帧素材显示为50帧
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=2.0),
        )
        # 时间线第0帧对应素材第0帧
        assert clip.timeline_to_source_frame(0) == 0
        # 时间线第25帧对应素材第50帧
        assert clip.timeline_to_source_frame(25) == 50
        # 时间线第49帧对应素材第98帧
        assert clip.timeline_to_source_frame(49) == 98

    def test_timeline_to_source_frame_with_speed_params_reverse(self):
        """测试 SpeedParams 反向播放剪辑的帧转换。"""
        clip = ClipInstance(
            clip_id="test_reverse",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,  # 正常速度反向
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=1.0, reverse=True),
        )
        # 时间线第0帧对应素材第99帧（反向起点）
        assert clip.timeline_to_source_frame(0) == 99
        # 时间线第50帧对应素材第49帧
        assert clip.timeline_to_source_frame(50) == 49
        # 时间线第99帧对应素材第0帧（反向终点）
        assert clip.timeline_to_source_frame(99) == 0

    def test_timeline_to_source_frame_with_speed_params_reverse_fast(self):
        """测试 SpeedParams 反向两倍速剪辑的帧转换。"""
        clip = ClipInstance(
            clip_id="test_reverse_fast",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=50,  # 两倍速反向：100帧素材显示为50帧
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=2.0, reverse=True),
        )
        # 时间线第0帧对应素材第99帧（反向起点）
        assert clip.timeline_to_source_frame(0) == 99
        # 时间线第25帧对应素材第49帧
        assert clip.timeline_to_source_frame(25) == 49
        # 时间线第49帧对应素材第1帧（接近终点）
        assert clip.timeline_to_source_frame(49) == 1

    def test_source_to_timeline_frame_with_speed_params(self):
        """测试 SpeedParams 素材帧到时间线帧转换。"""
        clip = ClipInstance(
            clip_id="test_source_to_timeline",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=50,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=2.0),
        )
        # 素材第0帧对应时间线第0帧
        assert clip.source_to_timeline_frame(0) == 0
        # 素材第50帧对应时间线第25帧
        assert clip.source_to_timeline_frame(50) == 25

    def test_effective_speed_method(self):
        """测试 effective_speed() 方法。"""
        # 测试 float 类型 speed
        clip_float = ClipInstance(
            clip_id="test_float",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=2.0,
        )
        assert clip_float.effective_speed() == 2.0

        # 测试 SpeedParams 类型 speed
        clip_params = ClipInstance(
            clip_id="test_params",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=50,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=2.0),
        )
        assert clip_params.effective_speed() == 2.0

    def test_invalid_speed_raises(self):
        """测试无效速度抛出异常。"""
        with pytest.raises(ValueError):
            ClipInstance(
                clip_id="test1",
                asset_id="asset1",
                track_id="track1",
                timeline_start=0,
                timeline_duration=100,
                source_start=0,
                source_duration=100,
                speed=0.05,  # 低于最小值
            )

    def test_invalid_opacity_raises(self):
        """测试无效透明度抛出异常。"""
        with pytest.raises(ValueError):
            ClipInstance(
                clip_id="test1",
                asset_id="asset1",
                track_id="track1",
                timeline_start=0,
                timeline_duration=100,
                source_start=0,
                source_duration=100,
                opacity=1.5,  # 超出范围
            )


# ================================================================
# Split Clip Tests (分割剪辑测试)
# ================================================================


class TestSplitClip:
    """split_clip 操作测试。"""

    def test_split_at_middle(self, sequence_with_track: TimelineSequence):
        """测试在中间分割。"""
        new_seq = split_clip(sequence_with_track, "clip1", 50)

        # 应该有三个剪辑（clip1 分成两半 + clip2）
        track = new_seq.get_track_by_id("track1")
        assert len(track.clips) == 3

        # 第一个剪辑
        left = track.get_clip_by_id("clip1")
        assert left.timeline_start == 0
        assert left.timeline_duration == 50
        assert left.source_start == 0
        assert left.source_duration == 50

        # 新创建的剪辑
        right = track.clips[1]  # 按时间排序应该是第二个
        assert right.timeline_start == 50
        assert right.timeline_duration == 50
        assert right.source_start == 50
        assert right.source_duration == 50

    def test_split_reuses_decoder_cursor_across_cut(
        self,
        sequence_with_track: TimelineSequence,
    ):
        """A blade split of contiguous source frames keeps one decode stream."""
        split_sequence = split_clip(sequence_with_track, "clip1", 50)
        render_plan = TimelineRenderPlan(split_sequence)

        before_cut = render_plan.get_frame_plan(49).layers[0]
        after_cut = render_plan.get_frame_plan(50).layers[0]

        assert before_cut.clip_id == "clip1"
        assert after_cut.clip_id != before_cut.clip_id
        assert after_cut.decoder_stream_key == before_cut.decoder_stream_key

    def test_split_near_start_raises(self, sequence_with_track: TimelineSequence):
        """测试在起始帧附近分割抛出异常。"""
        with pytest.raises(TimelineOperationError):
            split_clip(sequence_with_track, "clip1", 0)  # 不能在起始帧分割

    def test_split_near_end_raises(self, sequence_with_track: TimelineSequence):
        """测试在结束帧附近分割抛出异常。"""
        with pytest.raises(TimelineOperationError):
            split_clip(sequence_with_track, "clip1", 100)  # 不能在结束帧分割

    def test_split_nonexistent_clip_raises(self, empty_sequence: TimelineSequence):
        """测试分割不存在的剪辑抛出异常。"""
        with pytest.raises(TimelineOperationError):
            split_clip(empty_sequence, "nonexistent", 50)


# ================================================================
# Trim Clip Tests (裁切剪辑测试)
# ================================================================


class TestTrimClip:
    """trim_clip_left/right 操作测试。"""

    def test_trim_left_basic(self, sequence_with_track: TimelineSequence):
        """测试左侧裁切。"""
        new_seq = trim_clip_left(sequence_with_track, "clip1", 20)

        clip = new_seq.get_clip_by_id("clip1")
        assert clip.timeline_start == 20
        assert clip.timeline_duration == 80
        assert clip.source_start == 20
        assert clip.source_duration == 80

    def test_trim_right_basic(self, sequence_with_track: TimelineSequence):
        """测试右侧裁切。"""
        new_seq = trim_clip_right(sequence_with_track, "clip1", 80)

        clip = new_seq.get_clip_by_id("clip1")
        assert clip.timeline_start == 0
        assert clip.timeline_duration == 80
        assert clip.source_duration == 80

    def test_trim_left_to_zero_raises(self, sequence_with_track: TimelineSequence):
        """测试裁切到零长度抛出异常。"""
        with pytest.raises(TimelineOperationError):
            trim_clip_left(sequence_with_track, "clip1", 100)

    def test_trim_left_before_start_raises(self, sequence_with_track: TimelineSequence):
        """测试向左扩展抛出异常（第一版不支持）。"""
        with pytest.raises(TimelineOperationError):
            trim_clip_left(sequence_with_track, "clip1", -10)

    def test_trim_overlap_with_next_raises(self, sequence_with_track: TimelineSequence):
        """测试裁切导致与后一剪辑重叠抛出异常。"""
        # clip2 从帧100开始
        with pytest.raises(TimelineOperationError):
            trim_clip_right(sequence_with_track, "clip1", 110)


# ================================================================
# Move Clip Tests (移动剪辑测试)
# ================================================================


class TestMoveClip:
    """move_clip 操作测试。"""

    def test_move_same_track(self, sequence_with_track: TimelineSequence):
        """测试同轨移动。"""
        new_seq = move_clip(sequence_with_track, "clip2", "track1", 150)

        clip = new_seq.get_clip_by_id("clip2")
        assert clip.timeline_start == 150
        assert clip.timeline_duration == 50  # 时长不变

    def test_move_overlap_raises(self, sequence_with_track: TimelineSequence):
        """测试移动导致重叠抛出异常。"""
        # clip1 占用 [0, 100)
        with pytest.raises(TimelineOperationError):
            move_clip(sequence_with_track, "clip2", "track1", 50)  # 会与 clip1 重叠

    def test_move_nonexistent_clip_raises(self, empty_sequence: TimelineSequence):
        """测试移动不存在的剪辑抛出异常。"""
        with pytest.raises(TimelineOperationError):
            move_clip(empty_sequence, "nonexistent", "track1", 100)


# ================================================================
# Delete Clip Tests (删除剪辑测试)
# ================================================================


class TestDeleteClip:
    """delete_clip 操作测试。"""

    def test_delete_basic(self, sequence_with_track: TimelineSequence):
        """测试删除剪辑。"""
        new_seq = delete_clip(sequence_with_track, "clip1")

        track = new_seq.get_track_by_id("track1")
        assert len(track.clips) == 1
        assert track.clips[0].clip_id == "clip2"

    def test_delete_nonexistent_raises(self, sequence_with_track: TimelineSequence):
        """测试删除不存在的剪辑抛出异常。"""
        with pytest.raises(TimelineOperationError):
            delete_clip(sequence_with_track, "nonexistent")


# ================================================================
# Transition Tests (转场测试)
# ================================================================


class TestTransition:
    """add_transition 操作测试。"""

    def test_add_transition_basic(self, sequence_with_track: TimelineSequence):
        """测试添加转场。"""
        new_seq = add_transition(sequence_with_track, "clip1", "clip2", 12)

        track = new_seq.get_track_by_id("track1")
        assert len(track.transitions) == 1

        trans = track.transitions[0]
        assert trans.left_clip_id == "clip1"
        assert trans.right_clip_id == "clip2"
        assert trans.duration == 12
        assert trans.center_frame == 100

    def test_add_transition_non_adjacent_raises(self, sequence_with_track: TimelineSequence):
        """测试添加转场到不相邻剪辑抛出异常。"""
        # clip1 和 clip2 是相邻的，但如果我们添加第三个剪辑在中间
        # 先分割 clip1，然后测试不相邻的情况
        split_seq = split_clip(sequence_with_track, "clip1", 50)

        # 现有剪辑：clip1(0-50), new_clip(50-100), clip2(100-150)
        # clip1 和 clip2 不相邻
        with pytest.raises(TimelineOperationError):
            add_transition(split_seq, "clip1", "clip2", 12)


# ================================================================
# Track Tests (轨道测试)
# ================================================================


class TestEditTrack:
    """EditTrack 测试。"""

    def test_track_overlap_validation(self):
        """测试轨道重叠验证。"""
        clip1 = ClipInstance(
            clip_id="c1",
            asset_id="a1",
            track_id="t1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        clip2 = ClipInstance(
            clip_id="c2",
            asset_id="a1",
            track_id="t1",
            timeline_start=50,  # 与 clip1 重叠
            timeline_duration=50,
            source_start=0,
            source_duration=50,
        )

        with pytest.raises(ValueError):
            EditTrack(track_id="t1", clips=(clip1, clip2))

    def test_get_adjacent_clips(self, track_with_two_clips: EditTrack):
        """测试获取相邻剪辑。"""
        prev, next_ = track_with_two_clips.get_adjacent_clips("clip1")
        assert prev is None
        assert next_.clip_id == "clip2"

        prev, next_ = track_with_two_clips.get_adjacent_clips("clip2")
        assert prev.clip_id == "clip1"
        assert next_ is None


# ================================================================
# Transform Params Tests (变换参数测试)
# ================================================================


class TestTransformParams:
    """TransformParams 测试。"""

    def test_default_values(self):
        """测试默认值。"""
        transform = TransformParams()
        assert transform.translate_x == 0.0
        assert transform.scale_x == 1.0
        assert transform.rotation == 0.0

    def test_invalid_scale_raises(self):
        """测试无效缩放抛出异常。"""
        with pytest.raises(ValueError):
            TransformParams(scale_x=-1.0)  # 负缩放不允许


# ================================================================
# Crop Params Tests (裁切参数测试)
# ================================================================


class TestCropParams:
    """CropParams 测试。"""

    def test_default_values(self):
        """测试默认值。"""
        crop = CropParams()
        assert crop.left == 0.0
        assert crop.total_horizontal_crop == 0.0

    def test_excessive_crop_raises(self):
        """测试过度裁切抛出异常。"""
        with pytest.raises(ValueError):
            CropParams(left=0.6)  # 超过50%

    def test_total_crop_validation(self):
        """测试总裁切验证。"""
        with pytest.raises(ValueError):
            CropParams(left=0.5, right=0.5)  # 总裁切100%


# ================================================================
# Snap Tests (吸附测试)
# ================================================================


class TestSnap:
    """吸附操作测试。"""

    def test_find_snap_points(self, sequence_with_track: TimelineSequence):
        """测试查找吸附点。"""
        snap_points = find_snap_points(sequence_with_track, playhead_frame=25)

        # 应包含：0, 25(播放头), 100, 150(剪辑边界)
        assert 0 in snap_points
        assert 25 in snap_points
        assert 100 in snap_points
        assert 150 in snap_points

    def test_snap_to_nearest(self):
        """测试吸附到最近点。"""
        snap_points = (0, 100, 200)

        # 在阈值范围内吸附
        assert snap_to_nearest(5, snap_points, threshold=10) == 0
        assert snap_to_nearest(95, snap_points, threshold=10) == 100

        # 超出阈值不吸附
        assert snap_to_nearest(50, snap_points, threshold=10) == 50


# ================================================================
# Validation Tests (验证测试)
# ================================================================


class TestValidation:
    """时间线验证测试。"""

    def test_valid_sequence(self, sequence_with_track: TimelineSequence):
        """测试有效时间线。"""
        errors = validate_timeline(sequence_with_track)
        assert len(errors) == 0

    def test_sequence_with_invalid_transition(self, sequence_with_track: TimelineSequence):
        """测试无效转场的时间线。"""
        # 创建一个引用不存在剪辑的转场
        bad_trans = TransitionInstance(
            transition_id="bad_trans",
            transition_type="cross_dissolve",
            track_id="track1",
            left_clip_id="nonexistent",
            right_clip_id="clip2",
            center_frame=100,
            duration=12,
        )

        track = sequence_with_track.video_tracks[0]
        bad_track = EditTrack(
            track_id=track.track_id,
            name=track.name,
            track_type=track.track_type,
            index=track.index,
            clips=track.clips,
            transitions=(bad_trans,),
        )

        bad_seq = TimelineSequence(
            timeline_id=sequence_with_track.timeline_id,
            name=sequence_with_track.name,
            fps=sequence_with_track.fps,
            width=sequence_with_track.width,
            height=sequence_with_track.height,
            sample_rate=sequence_with_track.sample_rate,
            video_tracks=(bad_track,),
        )

        errors = validate_timeline(bad_seq)
        assert len(errors) > 0
        assert any("nonexistent" in e for e in errors)


# ================================================================
# Serialization Tests (序列化测试)
# ================================================================


class TestSerialization:
    """序列化测试。"""

    def test_clip_roundtrip(self):
        """测试剪辑序列化往返。"""
        clip = ClipInstance(
            clip_id="test",
            asset_id="asset1",
            track_id="track1",
            timeline_start=0,
            timeline_duration=100,
            source_start=50,
            source_duration=100,
            speed=1.5,
            opacity=0.8,
        )

        data = clip.to_dict()
        restored = ClipInstance.from_dict(data)

        assert restored.clip_id == clip.clip_id
        assert restored.timeline_start == clip.timeline_start
        assert restored.speed == clip.speed
        assert restored.opacity == clip.opacity

    def test_track_roundtrip(self, track_with_two_clips: EditTrack):
        """测试轨道序列化往返。"""
        data = track_with_two_clips.to_dict()
        restored = EditTrack.from_dict(data)

        assert restored.track_id == track_with_two_clips.track_id
        assert len(restored.clips) == len(track_with_two_clips.clips)

    def test_sequence_roundtrip(self, sequence_with_track: TimelineSequence):
        """测试时间线序列化往返。"""
        data = sequence_with_track.to_dict()
        restored = TimelineSequence.from_dict(data)

        assert restored.timeline_id == sequence_with_track.timeline_id
        assert restored.fps.num == sequence_with_track.fps.num
        assert restored.width == sequence_with_track.width


# ================================================================
# No PyQt Import Test (无 PyQt 依赖测试)
# ================================================================


class TestNoPyQtDependency:
    """验证模块不依赖 PyQt。"""

    def test_timeline_ops_no_qt(self):
        """timeline_ops 不导入 PyQt。"""
        import src.features.edit.core.timeline_ops as ops_module
        module_names = [name for name in dir(ops_module) if not name.startswith("_")]

        # 检查是否有 PyQt 相关的导入
        for attr in module_names:
            obj = getattr(ops_module, attr)
            # 确保没有 PyQt 类型
            if hasattr(obj, "__module__"):
                assert "PyQt" not in obj.__module__

    def test_timeline_v2_no_qt(self):
        """timeline_v2 不导入 PyQt。"""
        import src.features.edit.core.timeline_v2 as v2_module
        module_names = [name for name in dir(v2_module) if not name.startswith("_")]

        for attr in module_names:
            obj = getattr(v2_module, attr)
            if hasattr(obj, "__module__"):
                assert "PyQt" not in obj.__module__


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
