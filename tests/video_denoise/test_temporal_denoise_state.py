"""时域降噪状态机单元测试（纯 CPU，对应计划 M4.4 全部重置条件）。"""

import pytest

from src.features.color.core.video_temporal_denoise import TemporalDenoiseState

pytestmark = pytest.mark.media_core


def _key(v=1):
    return ("key", v)


class TestTemporalDenoiseState:
    def test_first_frame_no_history(self):
        s = TemporalDenoiseState()
        assert s.prepare("clip", 10, 1, _key(), (1920, 1080)) is False

    def test_consecutive_frames_reuse_history(self):
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(), (1920, 1080))
        assert s.prepare("clip", 11, 1, _key(), (1920, 1080)) is True
        assert s.accumulated_frames == 2

    def test_frame_jump_resets(self):
        """seek / 丢帧（帧号不连续）→ 重置历史。"""
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(), (1920, 1080))
        s.prepare("clip", 11, 1, _key(), (1920, 1080))
        assert s.prepare("clip", 20, 1, _key(), (1920, 1080)) is False

    def test_clip_change_resets(self):
        """切换视频文件 → 不得串帧。"""
        s = TemporalDenoiseState()
        s.prepare("clipA", 10, 1, _key(), (1920, 1080))
        s.prepare("clipA", 11, 1, _key(), (1920, 1080))
        assert s.prepare("clipB", 12, 1, _key(), (1920, 1080)) is False

    def test_generation_change_resets(self):
        """seek/静态预览世代变化 → 重置。"""
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 7, _key(), (1920, 1080))
        s.prepare("clip", 11, 7, _key(), (1920, 1080))
        assert s.prepare("clip", 12, 8, _key(), (1920, 1080)) is False

    def test_params_change_resets(self):
        """参数改变 → 历史失效。"""
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(1), (1920, 1080))
        s.prepare("clip", 11, 1, _key(1), (1920, 1080))
        assert s.prepare("clip", 12, 1, _key(2), (1920, 1080)) is False

    def test_size_change_resets(self):
        """分辨率改变（proxy↔master / 窗口缩放）→ 重置。"""
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(), (1920, 1080))
        s.prepare("clip", 11, 1, _key(), (1920, 1080))
        assert s.prepare("clip", 12, 1, _key(), (960, 540)) is False

    def test_same_frame_reuses_without_advance(self):
        """同帧重绘（调色参数变化）→ 历史复用，不重置不推进。"""
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(), (1920, 1080))
        s.prepare("clip", 11, 1, _key(), (1920, 1080))
        before = s.accumulated_frames
        assert s.prepare("clip", 11, 1, _key(), (1920, 1080)) is True
        assert s.accumulated_frames == before

    def test_reverse_playback_continuity(self):
        """倒放（帧号递减）保持连续，方向切换时重置。"""
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(), (1920, 1080))
        s.prepare("clip", 11, 1, _key(), (1920, 1080))
        # 正放 -> 倒放：方向切换，重置
        assert s.prepare("clip", 10, 1, _key(), (1920, 1080)) is False
        # 持续倒放：连续
        assert s.prepare("clip", 9, 1, _key(), (1920, 1080)) is True

    def test_scene_cut_note(self):
        s = TemporalDenoiseState()
        s.prepare("clip", 10, 1, _key(), (1920, 1080))
        s.note_scene_cut()
        assert s.scene_id == 1
        assert s.accumulated_frames == 1
