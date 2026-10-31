"""
Timeline Render Plan Tests
时间线渲染计划测试。

测试覆盖：
1. 给定 frame 0，返回第一个 clip 信息
2. 给定 clip 间隙的 frame，返回空
3. 多轨重叠时返回多个层
4. transition 区域返回混合信息
5. disabled clip 不参与渲染
6. mute/solo 轨道状态处理
7. speed 参数正确计算 source frame
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)
from src.features.edit.core.timeline_render_plan import (
    TimelineRenderPlan,
    FrameRenderPlan,
    ClipLayerInfo,
    TransitionBlendInfo,
    calculate_source_frame,
    get_effective_speed,
    is_frame_in_clip,
    get_transition_at_frame,
    calculate_transition_blend,
    is_track_renderable,
)
from src.shared.media.metadata import Rational


# ── Fixtures ──


@pytest.fixture
def simple_clip() -> ClipInstance:
    """创建简单的 clip fixture"""
    return ClipInstance(
        clip_id="clip-001",
        asset_id="asset-001",
        track_id="track-001",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
    )


@pytest.fixture
def second_clip() -> ClipInstance:
    """创建第二个 clip fixture（紧接第一个）"""
    return ClipInstance(
        clip_id="clip-002",
        asset_id="asset-002",
        track_id="track-001",
        timeline_start=100,
        timeline_duration=50,
        source_start=0,
        source_duration=50,
        speed=1.0,
    )


@pytest.fixture
def gap_clip() -> ClipInstance:
    """创建有间隙的 clip fixture"""
    return ClipInstance(
        clip_id="clip-003",
        asset_id="asset-003",
        track_id="track-001",
        timeline_start=200,  # 与 clip-002 有间隙（100-200 间隙）
        timeline_duration=50,
        source_start=0,
        source_duration=50,
        speed=1.0,
    )


@pytest.fixture
def speed_clip() -> ClipInstance:
    """创建有速度参数的 clip fixture"""
    return ClipInstance(
        clip_id="clip-speed",
        asset_id="asset-speed",
        track_id="track-001",
        timeline_start=0,
        timeline_duration=50,  # 50 帧时间线
        source_start=0,
        source_duration=100,  # 100 帧素材
        speed=2.0,  # 两倍速
    )


@pytest.fixture
def slow_clip() -> ClipInstance:
    """创建慢速 clip fixture"""
    return ClipInstance(
        clip_id="clip-slow",
        asset_id="asset-slow",
        track_id="track-001",
        timeline_start=0,
        timeline_duration=100,  # 100 帧时间线
        source_start=0,
        source_duration=50,  # 50 帧素材
        speed=SpeedParams(speed=0.5),  # 半速
    )


@pytest.fixture
def disabled_clip() -> ClipInstance:
    """创建禁用的 clip fixture"""
    return ClipInstance(
        clip_id="clip-disabled",
        asset_id="asset-disabled",
        track_id="track-001",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
        is_enabled=False,
    )


@pytest.fixture
def simple_track(simple_clip: ClipInstance) -> EditTrack:
    """创建简单轨道 fixture"""
    return EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(simple_clip,),
    )


@pytest.fixture
def two_clip_track(simple_clip: ClipInstance, second_clip: ClipInstance) -> EditTrack:
    """创建两 clip 轨道 fixture"""
    return EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(simple_clip, second_clip),
    )


@pytest.fixture
def gap_track(second_clip: ClipInstance, gap_clip: ClipInstance) -> EditTrack:
    """创建有间隙的轨道 fixture"""
    return EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(second_clip, gap_clip),
    )


@pytest.fixture
def upper_track() -> EditTrack:
    """创建上层轨道 fixture"""
    clip = ClipInstance(
        clip_id="clip-upper",
        asset_id="asset-upper",
        track_id="track-002",
        timeline_start=50,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
        opacity=0.5,
    )
    return EditTrack(
        track_id="track-002",
        name="Video 2",
        track_type="video",
        index=1,  # 上层
        clips=(clip,),
    )


@pytest.fixture
def muted_track() -> EditTrack:
    """创建静音轨道 fixture"""
    clip = ClipInstance(
        clip_id="clip-muted",
        asset_id="asset-muted",
        track_id="track-muted",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
    )
    return EditTrack(
        track_id="track-muted",
        name="Muted Track",
        track_type="video",
        index=0,
        clips=(clip,),
        is_muted=True,
    )


@pytest.fixture
def solo_track() -> EditTrack:
    """创建 solo 轨道 fixture"""
    clip = ClipInstance(
        clip_id="clip-solo",
        asset_id="asset-solo",
        track_id="track-solo",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
    )
    return EditTrack(
        track_id="track-solo",
        name="Solo Track",
        track_type="video",
        index=1,
        clips=(clip,),
        is_solo=True,
    )


@pytest.fixture
def simple_sequence(simple_track: EditTrack) -> TimelineSequence:
    """创建简单时间线序列 fixture"""
    return TimelineSequence(
        timeline_id="seq-001",
        name="Test Sequence",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(simple_track,),
    )


@pytest.fixture
def multi_track_sequence(simple_track: EditTrack, upper_track: EditTrack) -> TimelineSequence:
    """创建多轨时间线序列 fixture"""
    return TimelineSequence(
        timeline_id="seq-multi",
        name="Multi Track Sequence",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(simple_track, upper_track),
    )


@pytest.fixture
def transition() -> TransitionInstance:
    """创建转场 fixture"""
    return TransitionInstance(
        transition_id="trans-001",
        transition_type="cross_dissolve",
        track_id="track-001",
        left_clip_id="clip-001",
        right_clip_id="clip-002",
        center_frame=100,
        duration=24,
        alignment="centered",
    )


@pytest.fixture
def transition_track(
    simple_clip: ClipInstance,
    second_clip: ClipInstance,
    transition: TransitionInstance
) -> EditTrack:
    """创建带转场的轨道 fixture"""
    return EditTrack(
        track_id="track-001",
        name="Video 1",
        track_type="video",
        index=0,
        clips=(simple_clip, second_clip),
        transitions=(transition,),
    )


@pytest.fixture
def transition_sequence(transition_track: EditTrack) -> TimelineSequence:
    """创建带转场的时间线序列 fixture"""
    return TimelineSequence(
        timeline_id="seq-trans",
        name="Transition Sequence",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(transition_track,),
    )


# ── 辅助函数测试 ──


class TestCalculateSourceFrame:
    """测试 calculate_source_frame 函数"""

    def test_normal_speed_frame_0(self, simple_clip: ClipInstance):
        """测试正常速度下帧 0 的源帧"""
        source_frame = calculate_source_frame(simple_clip, 0)
        assert source_frame == 0

    def test_normal_speed_frame_50(self, simple_clip: ClipInstance):
        """测试正常速度下帧 50 的源帧"""
        source_frame = calculate_source_frame(simple_clip, 50)
        assert source_frame == 50

    def test_fast_speed_frame_25(self, speed_clip: ClipInstance):
        """测试两倍速下帧 25 的源帧（应为 50）"""
        source_frame = calculate_source_frame(speed_clip, 25)
        assert source_frame == 50

    def test_slow_speed_frame_50(self, slow_clip: ClipInstance):
        """测试半速下帧 50 的源帧（应为 25）"""
        source_frame = calculate_source_frame(slow_clip, 50)
        assert source_frame == 25

    def test_out_of_range_raises(self, simple_clip: ClipInstance):
        """测试帧超出范围时抛出异常"""
        with pytest.raises(ValueError):
            calculate_source_frame(simple_clip, 100)  # clip 结束帧

        with pytest.raises(ValueError):
            calculate_source_frame(simple_clip, -1)  # clip 起始前


class TestGetEffectiveSpeed:
    """测试 get_effective_speed 函数"""

    def test_float_speed(self, simple_clip: ClipInstance):
        """测试 float 类型速度"""
        speed = get_effective_speed(simple_clip)
        assert speed == 1.0

    def test_speed_params(self, slow_clip: ClipInstance):
        """测试 SpeedParams 类型速度"""
        speed = get_effective_speed(slow_clip)
        assert speed == 0.5

    def test_fast_speed(self, speed_clip: ClipInstance):
        """测试快速播放"""
        speed = get_effective_speed(speed_clip)
        assert speed == 2.0


class TestIsFrameInClip:
    """测试 is_frame_in_clip 函数"""

    def test_frame_in_clip(self, simple_clip: ClipInstance):
        """测试帧在 clip 内"""
        assert is_frame_in_clip(simple_clip, 0)
        assert is_frame_in_clip(simple_clip, 50)
        assert is_frame_in_clip(simple_clip, 99)

    def test_frame_out_of_clip(self, simple_clip: ClipInstance):
        """测试帧不在 clip 内"""
        assert not is_frame_in_clip(simple_clip, -1)
        assert not is_frame_in_clip(simple_clip, 100)
        assert not is_frame_in_clip(simple_clip, 200)


class TestCalculateTransitionBlend:
    """测试 calculate_transition_blend 函数"""

    def test_start_of_transition(self, transition: TransitionInstance):
        """测试转场起始帧"""
        blend = calculate_transition_blend(transition, transition.start_frame)
        assert blend == 0.0

    def test_end_of_transition(self, transition: TransitionInstance):
        """测试转场结束帧"""
        blend = calculate_transition_blend(transition, transition.end_frame - 1)
        # 24帧转场，最后一帧 blend = 23/24 ≈ 0.96
        assert blend == pytest.approx(23 / 24, rel=0.01)

    def test_middle_of_transition(self, transition: TransitionInstance):
        """测试转场中间帧"""
        mid_frame = transition.start_frame + transition.duration // 2
        blend = calculate_transition_blend(transition, mid_frame)
        assert blend == pytest.approx(0.5, rel=0.1)


class TestIsTrackRenderable:
    """测试 is_track_renderable 函数"""

    def test_normal_track(self, simple_track: EditTrack):
        """测试正常轨道"""
        assert is_track_renderable(simple_track, False)

    def test_disabled_track(self):
        """测试禁用轨道"""
        track = EditTrack(
            track_id="track-disabled",
            name="Disabled",
            track_type="video",
            index=0,
            is_enabled=False,
        )
        assert not is_track_renderable(track, False)

    def test_muted_track(self, muted_track: EditTrack):
        """测试静音轨道"""
        assert not is_track_renderable(muted_track, False)

    def test_solo_track_without_solo(self, solo_track: EditTrack):
        """测试 solo 轨道但没有 solo 激活"""
        # 没有 solo 激活时，solo 轨道也渲染
        assert is_track_renderable(solo_track, False)

    def test_non_solo_track_with_solo(self, simple_track: EditTrack):
        """测试非 solo 轨道但有 solo 激活"""
        # 有 solo 激活时，非 solo 轨道不渲染
        assert not is_track_renderable(simple_track, True)

    def test_solo_track_with_solo(self, solo_track: EditTrack):
        """测试 solo 轨道且有 solo 激活"""
        assert is_track_renderable(solo_track, True)


# ── TimelineRenderPlan 测试 ──


class TestTimelineRenderPlanBasic:
    """基础测试"""

    def test_frame_0_returns_first_clip(self, simple_sequence: TimelineSequence):
        """验收标准：给定 frame 0，返回第一个 clip 信息"""
        plan = TimelineRenderPlan(simple_sequence)
        frame_plan = plan.get_frame_plan(0)

        assert frame_plan.has_content
        assert frame_plan.layer_count == 1

        layer = frame_plan.layers[0]
        assert layer.clip_id == "clip-001"
        assert layer.source_frame == 0
        assert layer.track_index == 0

    def test_frame_in_gap_returns_empty(self, gap_track: EditTrack):
        """验收标准：给定 clip 间隙的 frame，返回空"""
        sequence = TimelineSequence(
            timeline_id="seq-gap",
            name="Gap Sequence",
            fps=Rational(24, 1),
            video_tracks=(gap_track,),
        )
        plan = TimelineRenderPlan(sequence)

        # 150 在间隙中（100-200）
        frame_plan = plan.get_frame_plan(150)

        assert not frame_plan.has_content
        assert frame_plan.layer_count == 0

    def test_frame_in_second_clip(self, two_clip_track: EditTrack):
        """测试第二个 clip 的帧"""
        sequence = TimelineSequence(
            timeline_id="seq-two",
            name="Two Clip Sequence",
            fps=Rational(24, 1),
            video_tracks=(two_clip_track,),
        )
        plan = TimelineRenderPlan(sequence)

        frame_plan = plan.get_frame_plan(110)

        assert frame_plan.has_content
        assert frame_plan.layer_count == 1

        layer = frame_plan.layers[0]
        assert layer.clip_id == "clip-002"
        assert layer.source_frame == 10

    def test_blade_split_reuses_contiguous_decode_stream(self):
        """A contiguous same-source cut must not cold-start a decoder."""
        left = ClipInstance(
            clip_id="clip-left",
            asset_id="asset-shared",
            track_id="track-001",
            timeline_start=0,
            timeline_duration=10,
            source_start=40,
            source_duration=10,
            speed=1.0,
        )
        right = ClipInstance(
            clip_id="clip-right",
            asset_id="asset-shared",
            track_id="track-001",
            timeline_start=10,
            timeline_duration=10,
            source_start=50,
            source_duration=10,
            speed=1.0,
        )
        sequence = TimelineSequence(
            timeline_id="seq-continuous-cut",
            fps=Rational(24, 1),
            video_tracks=(EditTrack(
                track_id="track-001",
                track_type="video",
                clips=(left, right),
            ),),
        )

        render_plan = TimelineRenderPlan(sequence)

        assert render_plan.get_frame_plan(9).layers[0].decoder_stream_key == "clip-left"
        assert render_plan.get_frame_plan(10).layers[0].decoder_stream_key == "clip-left"

    def test_source_jump_keeps_independent_decode_stream(self):
        """A jump within one asset must retain a separate decoder cursor."""
        left = ClipInstance(
            clip_id="clip-left",
            asset_id="asset-shared",
            track_id="track-001",
            timeline_start=0,
            timeline_duration=10,
            source_start=40,
            source_duration=10,
            speed=1.0,
        )
        right = ClipInstance(
            clip_id="clip-right",
            asset_id="asset-shared",
            track_id="track-001",
            timeline_start=10,
            timeline_duration=10,
            source_start=80,
            source_duration=10,
            speed=1.0,
        )
        sequence = TimelineSequence(
            timeline_id="seq-source-jump",
            fps=Rational(24, 1),
            video_tracks=(EditTrack(
                track_id="track-001",
                track_type="video",
                clips=(left, right),
            ),),
        )

        layer = TimelineRenderPlan(sequence).get_frame_plan(10).layers[0]

        assert layer.decoder_stream_key == "clip-right"


class TestTimelineRenderPlanMultiTrack:
    """多轨测试"""

    def test_multi_track_returns_multiple_layers(
        self,
        multi_track_sequence: TimelineSequence
    ):
        """验收标准：多轨重叠时返回多个层"""
        plan = TimelineRenderPlan(multi_track_sequence)

        # 帧 75 在两个轨道重叠区域（50-150）
        frame_plan = plan.get_frame_plan(75)

        assert frame_plan.has_content
        assert frame_plan.layer_count == 2

        # 检查轨道顺序（下层先，上层后）
        track_indices = [layer.track_index for layer in frame_plan.layers]
        assert track_indices == [0, 1]

    def test_upper_track_opacity(self, multi_track_sequence: TimelineSequence):
        """测试上层轨道透明度"""
        plan = TimelineRenderPlan(multi_track_sequence)

        frame_plan = plan.get_frame_plan(75)

        # 上层 clip opacity=0.5
        upper_layer = frame_plan.layers[1]
        assert upper_layer.opacity == 0.5

    def test_non_overlapping_frame(self, multi_track_sequence: TimelineSequence):
        """测试不重叠的帧"""
        plan = TimelineRenderPlan(multi_track_sequence)

        # 帧 25 只有下层有内容
        frame_plan = plan.get_frame_plan(25)

        assert frame_plan.layer_count == 1
        assert frame_plan.layers[0].track_index == 0


class TestTimelineRenderPlanTransition:
    """转场测试"""

    def test_transition_returns_blend_info(
        self,
        transition_sequence: TimelineSequence
    ):
        """验收标准：transition 区域返回混合信息"""
        plan = TimelineRenderPlan(transition_sequence)

        # 转场中心帧
        center_frame = 100
        frame_plan = plan.get_frame_plan(center_frame)

        assert frame_plan.transition_count == 1

        trans_info = frame_plan.transitions[0]
        assert trans_info.transition_type == "cross_dissolve"
        assert trans_info.left_clip.clip_id == "clip-001"
        assert trans_info.right_clip.clip_id == "clip-002"
        assert trans_info.blend_factor == pytest.approx(0.5, rel=0.1)

    def test_transition_layers(self, transition_sequence: TimelineSequence):
        """测试转场区域的 layer"""
        plan = TimelineRenderPlan(transition_sequence)

        frame_plan = plan.get_frame_plan(100)

        # Native transitions expose both sides to the compositor, including
        # the edge-sampled side of a normal adjacent cut.
        assert frame_plan.layer_count == 2
        assert frame_plan.transition_count == 1

        # Both layers are exposed to the native compositor and marked as
        # participating in the transition.
        assert all(layer.is_in_transition for layer in frame_plan.layers)
        assert {layer.clip.clip_id for layer in frame_plan.layers} == {"clip-001", "clip-002"}

        # transition 信息包含两个 clip
        trans = frame_plan.transitions[0]
        assert trans.left_clip.clip_id == "clip-001"
        assert trans.right_clip.clip_id == "clip-002"

    def test_transition_blend_progression(self, transition_sequence: TimelineSequence):
        """测试转场混合比例递进"""
        plan = TimelineRenderPlan(transition_sequence)

        transition = transition_sequence.video_tracks[0].transitions[0]

        # 转场起始帧
        start_plan = plan.get_frame_plan(transition.start_frame)
        assert start_plan.transitions[0].blend_factor == 0.0

        # 转场结束帧
        end_plan = plan.get_frame_plan(transition.end_frame - 1)
        assert end_plan.transitions[0].blend_factor > 0.9


class TestWipeTransition:
    """Wipe 转场测试：计划层产出 + 遮罩生成。

    验证 wipe 不再降级为简单 blend，而是产出双方完整 opacity 的 blend info，
    由合成阶段用 compute_wipe_mask 做空间混合。
    """

    @pytest.fixture
    def wipe_sequence(self, simple_clip: ClipInstance, second_clip: ClipInstance) -> TimelineSequence:
        """带 wipe 转场的时间线。"""
        wipe = TransitionInstance(
            transition_id="trans-wipe",
            transition_type="wipe",
            track_id="track-001",
            left_clip_id="clip-001",
            right_clip_id="clip-002",
            center_frame=100,
            duration=24,
            alignment="centered",
            params={"direction": "left", "softness": 0.05},
        )
        track = EditTrack(
            track_id="track-001",
            name="Video 1",
            track_type="video",
            index=0,
            clips=(simple_clip, second_clip),
            transitions=(wipe,),
        )
        return TimelineSequence(
            timeline_id="seq-wipe",
            name="Wipe Sequence",
            fps=Rational(24, 1),
            width=1920,
            height=1080,
            video_tracks=(track,),
        )

    def test_wipe_blend_info_preserves_opacity(self, wipe_sequence: TimelineSequence):
        """Wipe 在转场区域内应保留双方完整 opacity（空间遮罩由合成阶段处理）。"""
        plan = TimelineRenderPlan(wipe_sequence)
        frame_plan = plan.get_frame_plan(100)  # 转场中心

        assert frame_plan.transition_count == 1
        trans = frame_plan.transitions[0]
        assert trans.transition_type == "wipe"
        # 关键：wipe 不做 opacity 交叉淡入，双方 opacity 都应保留（== clip opacity 默认 1.0）
        assert trans.left_opacity == pytest.approx(1.0)
        assert trans.right_opacity == pytest.approx(1.0)
        assert trans.blend_factor == pytest.approx(0.5, rel=0.1)

    def test_wipe_blend_mode(self, wipe_sequence: TimelineSequence):
        """Wipe blend info 映射到 BlendMode.WIPE。"""
        from src.features.edit.core.timeline_render_plan import BlendMode
        plan = TimelineRenderPlan(wipe_sequence)
        trans = plan.get_frame_plan(100).transitions[0]
        assert trans.get_blend_mode() == BlendMode.WIPE

    def test_wipe_mask_generation(self):
        """compute_wipe_mask 生成正确形状与端点值的逐像素遮罩。"""
        from src.features.edit.core.transition_wipe import compute_wipe_mask
        import numpy as np

        # progress 0 → 全左（mask 全 0），progress 1 → 全右（mask 全 1）
        m0 = compute_wipe_mask(64, 48, 0.0, {"direction": "left"})
        assert m0.shape == (48, 64) and m0.dtype == np.float32
        assert m0.max() == 0.0
        m1 = compute_wipe_mask(64, 48, 1.0, {"direction": "left"})
        assert m1.min() == 1.0

        # left 方向 0.5：左半 mask≈1（右帧），右半 mask≈0（左帧）
        m = compute_wipe_mask(64, 48, 0.5, {"direction": "left", "softness": 0.0})
        assert m[24, 5] > 0.9
        assert m[24, 58] < 0.1

    def test_wipe_params_passed_through(self, wipe_sequence: TimelineSequence):
        """Wipe params（direction/softness）随 transition 透传，供合成阶段读取。"""
        plan = TimelineRenderPlan(wipe_sequence)
        trans = plan.get_frame_plan(100).transitions[0]
        assert trans.transition.params.get("direction") == "left"
        assert trans.transition.params.get("softness") == 0.05


class TestTimelineRenderPlanDisabled:
    """禁用状态测试"""

    def test_disabled_clip_not_rendered(self):
        """验收标准：disabled clip 不参与渲染"""
        disabled_clip = ClipInstance(
            clip_id="clip-disabled",
            asset_id="asset-001",
            track_id="track-001",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            is_enabled=False,
        )

        track = EditTrack(
            track_id="track-001",
            name="Video 1",
            track_type="video",
            index=0,
            clips=(disabled_clip,),
        )

        sequence = TimelineSequence(
            timeline_id="seq-disabled",
            name="Disabled Sequence",
            fps=Rational(24, 1),
            video_tracks=(track,),
        )

        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)

        assert not frame_plan.has_content
        assert frame_plan.layer_count == 0


class TestTimelineRenderPlanMuteSolo:
    """静音/Solo 状态测试"""

    def test_mute_track_not_rendered(self, muted_track: EditTrack):
        """验收标准：Mute track 不参与渲染"""
        sequence = TimelineSequence(
            timeline_id="seq-muted",
            name="Muted Sequence",
            fps=Rational(24, 1),
            video_tracks=(muted_track,),
        )

        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)

        assert not frame_plan.has_content

    def test_solo_track_only_rendered(
        self,
        simple_track: EditTrack,
        solo_track: EditTrack
    ):
        """验收标准：Solo track 只有 solo 的轨道参与"""
        sequence = TimelineSequence(
            timeline_id="seq-solo",
            name="Solo Sequence",
            fps=Rational(24, 1),
            video_tracks=(simple_track, solo_track),
        )

        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)

        # 只有 solo 轨道的 clip
        assert frame_plan.layer_count == 1
        assert frame_plan.layers[0].clip_id == "clip-solo"
        assert frame_plan.solo_active

    def test_non_solo_track_ignored_when_solo_active(
        self,
        simple_track: EditTrack,
        solo_track: EditTrack
    ):
        """测试 solo 激活时非 solo 轨道被忽略"""
        sequence = TimelineSequence(
            timeline_id="seq-solo",
            name="Solo Sequence",
            fps=Rational(24, 1),
            video_tracks=(simple_track, solo_track),
        )

        plan = TimelineRenderPlan(sequence)

        # 帧 50 在 simple_track 范围内，但 solo 激活
        frame_plan = plan.get_frame_plan(50)

        # simple_track 被忽略
        assert frame_plan.layer_count == 1
        assert frame_plan.layers[0].clip_id == "clip-solo"


class TestTimelineRenderPlanSpeed:
    """速度参数测试"""

    def test_fast_speed_source_frame(self):
        """测试快速播放的源帧映射"""
        speed_clip = ClipInstance(
            clip_id="clip-speed",
            asset_id="asset-001",
            track_id="track-001",
            timeline_start=0,
            timeline_duration=50,
            source_start=0,
            source_duration=100,
            speed=2.0,
        )

        track = EditTrack(
            track_id="track-001",
            name="Video 1",
            track_type="video",
            index=0,
            clips=(speed_clip,),
        )

        sequence = TimelineSequence(
            timeline_id="seq-speed",
            name="Speed Sequence",
            fps=Rational(24, 1),
            video_tracks=(track,),
        )

        plan = TimelineRenderPlan(sequence)

        # 时间线帧 25 对应源帧 50
        frame_plan = plan.get_frame_plan(25)
        assert frame_plan.layers[0].source_frame == 50

    def test_slow_speed_source_frame(self):
        """测试慢速播放的源帧映射"""
        slow_clip = ClipInstance(
            clip_id="clip-slow",
            asset_id="asset-001",
            track_id="track-001",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=50,
            speed=SpeedParams(speed=0.5),
        )

        track = EditTrack(
            track_id="track-001",
            name="Video 1",
            track_type="video",
            index=0,
            clips=(slow_clip,),
        )

        sequence = TimelineSequence(
            timeline_id="seq-slow",
            name="Slow Sequence",
            fps=Rational(24, 1),
            video_tracks=(track,),
        )

        plan = TimelineRenderPlan(sequence)

        # 时间线帧 50 对应源帧 25
        frame_plan = plan.get_frame_plan(50)
        assert frame_plan.layers[0].source_frame == 25


class TestTimelineRenderPlanCache:
    """缓存测试"""

    def test_cache_hit(self, simple_sequence: TimelineSequence):
        """测试缓存命中"""
        plan = TimelineRenderPlan(simple_sequence)

        # 第一次计算
        first_plan = plan.get_frame_plan(0)

        # 第二次应从缓存返回
        second_plan = plan.get_frame_plan(0)

        # 应是同一个对象（缓存）
        assert first_plan is second_plan

    def test_clear_cache(self, simple_sequence: TimelineSequence):
        """测试清除缓存"""
        plan = TimelineRenderPlan(simple_sequence)

        # 第一次计算
        first_plan = plan.get_frame_plan(0)

        # 清除缓存
        plan.clear_cache()

        # 第二次应重新计算
        second_plan = plan.get_frame_plan(0)

        # 应是不同对象
        assert first_plan is not second_plan


class TestTimelineRenderPlanBatchQuery:
    """批量查询测试"""

    def test_get_frame_range_plan(self, simple_sequence: TimelineSequence):
        """测试获取帧范围计划"""
        plan = TimelineRenderPlan(simple_sequence)

        # 获取帧 0-10 的计划
        range_plan = plan.get_frame_range_plan(0, 10)

        assert len(range_plan) == 10
        for frame, frame_plan in range_plan.items():
            assert frame_plan.frame == frame
            assert frame_plan.has_content

    def test_find_clips_in_range(self, two_clip_track: EditTrack):
        """测试查找帧范围内的 clip"""
        sequence = TimelineSequence(
            timeline_id="seq-two",
            name="Two Clip Sequence",
            fps=Rational(24, 1),
            video_tracks=(two_clip_track,),
        )

        plan = TimelineRenderPlan(sequence)

        # 查找帧 50-150 范围内的 clip
        clips = plan.find_clips_in_range(50, 150)

        # 两个 clip 都在这个范围内
        assert len(clips) == 2


# ── FrameRenderPlan 测试 ──


class TestFrameRenderPlan:
    """FrameRenderPlan 测试"""

    def test_get_layers_by_track(self, multi_track_sequence: TimelineSequence):
        """测试按轨道过滤 layer"""
        plan = TimelineRenderPlan(multi_track_sequence)
        frame_plan = plan.get_frame_plan(75)

        # 获取 track-001 的 layer
        track_001_layers = frame_plan.get_layers_by_track("track-001")
        assert len(track_001_layers) == 1
        assert track_001_layers[0].track_index == 0

        # 获取 track-002 的 layer
        track_002_layers = frame_plan.get_layers_by_track("track-002")
        assert len(track_002_layers) == 1
        assert track_002_layers[0].track_index == 1


# ── ClipLayerInfo 测试 ──


class TestClipLayerInfo:
    """ClipLayerInfo 测试"""

    def test_properties(self, simple_clip: ClipInstance, simple_track: EditTrack):
        """测试 ClipLayerInfo 属性"""
        sequence = TimelineSequence(
            timeline_id="seq-001",
            name="Test",
            fps=Rational(24, 1),
            video_tracks=(simple_track,),
        )

        plan = TimelineRenderPlan(sequence)
        frame_plan = plan.get_frame_plan(0)
        layer = frame_plan.layers[0]

        assert layer.clip_id == simple_clip.clip_id
        assert layer.asset_id == simple_clip.asset_id
        assert layer.source_frame == 0
        assert layer.track_name == simple_track.name


# ── 性能测试 ──


class TestTimelineRenderPlanPerformance:
    """性能测试"""

    def test_large_timeline(self):
        """测试大型时间线性能"""
        # 创建 100 个 clip
        clips = []
        for i in range(100):
            clip = ClipInstance(
                clip_id=f"clip-{i}",
                asset_id=f"asset-{i}",
                track_id="track-001",
                timeline_start=i * 100,
                timeline_duration=100,
                source_start=0,
                source_duration=100,
                speed=1.0,
            )
            clips.append(clip)

        track = EditTrack(
            track_id="track-001",
            name="Video 1",
            track_type="video",
            index=0,
            clips=tuple(clips),
        )

        sequence = TimelineSequence(
            timeline_id="seq-large",
            name="Large Sequence",
            fps=Rational(24, 1),
            video_tracks=(track,),
        )

        plan = TimelineRenderPlan(sequence)

        # 查询多个帧
        for frame in [0, 100, 500, 1000, 5000, 9900]:
            frame_plan = plan.get_frame_plan(frame)
            # 大型时间线也应该快速响应
            assert frame_plan.layer_count == 1


# ── Track Visibility Rendering Tests ──


class TestTrackVisibilityRenderable:
    """Verify is_track_renderable respects is_visible."""

    def test_visible_track_is_renderable(self):
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        assert is_track_renderable(track, has_solo=False) is True

    def test_hidden_track_not_renderable(self):
        track = EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            is_visible=False,
        )
        assert is_track_renderable(track, has_solo=False) is False

    def test_hidden_overrides_enabled(self):
        """Even if enabled, hidden tracks should not render."""
        track = EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            is_visible=False, is_enabled=True,
        )
        assert is_track_renderable(track, has_solo=False) is False

    def test_hidden_overrides_solo(self):
        """Hidden solo tracks should still not render."""
        track = EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            is_visible=False, is_solo=True,
        )
        assert is_track_renderable(track, has_solo=True) is False


class TestHiddenTrackNotInRenderPlan:
    """Verify hidden tracks are excluded from frame render plans."""

    def test_hidden_video_track_excluded(self, simple_clip: ClipInstance):
        v1 = EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(simple_clip,),
        )
        v2 = EditTrack(
            track_id="v2", name="V2", track_type="video", index=1,
            clips=(ClipInstance(
                clip_id="clip-v2", asset_id="a2", track_id="v2",
                timeline_start=0, timeline_duration=100,
                source_start=0, source_duration=100, speed=1.0,
            ),),
            is_visible=False,
        )
        seq = TimelineSequence(
            timeline_id="seq1", name="Test",
            fps=Rational(24, 1),
            video_tracks=(v1, v2),
        )
        plan = TimelineRenderPlan(seq)
        frame_plan = plan.get_frame_plan(0)
        # Only V1 should appear
        assert frame_plan.layer_count == 1
        assert any(l.clip.clip_id == "clip-001" for l in frame_plan.layers)
        assert not any(l.clip.clip_id == "clip-v2" for l in frame_plan.layers)

    def test_hidden_audio_track_excluded(self):
        audio_clip = ClipInstance(
            clip_id="aclip", asset_id="aa", track_id="a1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=100, speed=1.0,
        )
        a1 = EditTrack(
            track_id="a1", name="A1", track_type="audio", index=0,
            clips=(audio_clip,),
            is_visible=False,
        )
        seq = TimelineSequence(
            timeline_id="seq1", name="Test",
            fps=Rational(24, 1),
            audio_tracks=(a1,),
        )
        plan = TimelineRenderPlan(seq)
        frame_plan = plan.get_frame_plan(0)
        assert frame_plan.layer_count == 0


# ---------------- F06（2026-09-27）：转场查找区间索引 ----------------

class TestTransitionIndexScaling:
    """F06：转场查找 = 二分 + 前缀最大 end 剪枝，不随转场数线性增长；
    命中语义与线性扫描逐项一致（含重叠转场取声明序首个）。"""

    @staticmethod
    def _track_with_transitions(count: int, seed: int = 7):
        import random

        from src.features.edit.core.timeline_v2 import EditTrack, TransitionInstance
        rng = random.Random(seed)
        trans = []
        for i in range(count):
            start = rng.randrange(0, 100_000)
            duration = rng.choice([0, 5, 30, 120])
            trans.append(TransitionInstance(
                transition_id=f"t{i}", track_id="v1",
                left_clip_id="a", right_clip_id="b",
                center_frame=start, duration=max(1, duration),
            ))
        track = EditTrack(track_id="v1", name="V1", track_type="video",
                          index=0, clips=(), transitions=tuple(trans))
        return track

    @staticmethod
    def _linear_reference(track, frame):
        for trans in track.transitions:
            if trans.start_frame <= frame < trans.end_frame:
                return trans
        return None

    def test_matches_linear_scan_on_random_data(self) -> None:
        track = self._track_with_transitions(300, seed=11)
        for frame in range(0, 100_000, 97):
            assert get_transition_at_frame(track, frame) == \
                self._linear_reference(track, frame), frame

    def test_empty_and_miss_fast_path(self) -> None:
        track = self._track_with_transitions(0)
        assert get_transition_at_frame(track, 5) is None
        track = self._track_with_transitions(2000, seed=3)
        # 剪枝判空：远端帧 O(log n) 返回 None
        assert get_transition_at_frame(track, 999_999) is None

    def test_query_scaling_sublinear(self) -> None:
        """2000 转场 × 2000 次查询 < 0.5s（线性扫描外推约需数秒）。"""
        import time

        track = self._track_with_transitions(2000, seed=5)
        start = time.perf_counter()
        for frame in range(0, 200_000, 100):
            get_transition_at_frame(track, frame)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"2000 查询耗时 {elapsed:.3f}s"
