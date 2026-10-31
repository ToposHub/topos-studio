"""F8.3 · 多 take 候选/Audition——验收测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.audition import (
    Audition,
    AuditionTake,
    flatten_audition,
    mark_take_offline,
    switch_take,
)

pytestmark = pytest.mark.edit_core


def _audition():
    return Audition(
        timeline_id="tl1", track_id="v1", timeline_start=100,
        takes=(
            AuditionTake(take_id="t1", asset_id="a1",
                         source_start=0, source_duration=48, label="Take A"),
            AuditionTake(take_id="t2", asset_id="a2",
                         source_start=10, source_duration=72, label="Take B"),
            AuditionTake(take_id="t3", asset_id="a3",
                         source_start=0, source_duration=48, label="Take C"),
        ),
        active_take_id="t1")


class TestModel:
    def test_roundtrip(self):
        a = _audition()
        restored = Audition.from_dict(a.to_dict())
        assert restored == a
        assert restored.active_take().label == "Take A"


class TestSwitchTake:
    def test_switch_same_length_no_shift(self):
        """同长度 take：时间线不动（same_length）。"""
        result = switch_take(_audition(), "t3")
        assert result.placement == "same_length"
        assert result.timeline_shift == 0
        assert result.audition.active_take_id == "t3"

    def test_switch_longer_ripple_shifts_neighbors(self):
        """更长 take + ripple：邻片后移量可预览（24 帧差）。"""
        result = switch_take(_audition(), "t2", placement="ripple")
        assert result.placement == "ripple"
        assert result.timeline_shift == 24  # 72-48
        # 非破坏性：三个候选都还在
        assert len(result.audition.takes) == 3

    def test_switch_longer_fit_clips_to_current(self):
        """更长 take + fit：裁到现时长（时间线不动），fitted 标记。"""
        result = switch_take(_audition(), "t2", placement="fit")
        assert result.timeline_shift == 0
        assert result.fitted is True
        assert result.placement == "same_length"

    def test_offline_take_not_activatable(self):
        """缺源候选不可激活，其他候选不受影响。"""
        offline = mark_take_offline(_audition(), "t2")
        with pytest.raises(ValueError, match="offline"):
            switch_take(offline, "t2")
        # 其他候选可切
        result = switch_take(offline, "t3")
        assert result.audition.active_take_id == "t3"
        # 候选仍保留（不丢）
        assert len(offline.takes) == 3

    def test_active_take_offline_falls_back(self):
        """活动 take 缺源：回退首个可用候选（容器不丢）。"""
        offline = mark_take_offline(_audition(), "t1")
        assert offline.active_take_id == "t2"  # 回退
        # 全部 offline → active=None 容器仍在
        all_off = mark_take_offline(
            mark_take_offline(offline, "t2"), "t3")
        assert all_off.active_take_id is None
        assert len(all_off.takes) == 3

    def test_mixed_fps_duration_math(self):
        """混合 fps：候选时长按时间线 fps 换算。"""
        audition = _audition()
        # t2 素材 48fps、时间线 24fps：72 源帧 → 36 时间线帧 → 差 -12
        result = switch_take(
            audition, "t2", placement="ripple",
            source_fps_by_asset={"a2": (48, 1)})
        assert result.timeline_shift == 36 - 48


class TestFlatten:
    def test_flatten_keeps_other_takes(self):
        """拆平=活动 take 固化 + 其余候选保留（明确 flatten 不丢）。"""
        active, rest = flatten_audition(_audition())
        assert active.take_id == "t1"
        assert {t.take_id for t in rest} == {"t2", "t3"}

    def test_flatten_without_active_refused(self):
        no_active = Audition(
            takes=(AuditionTake(take_id="t1", asset_id="a1"),),
            active_take_id=None)
        with pytest.raises(ValueError, match="无活动"):
            flatten_audition(no_active)


# ================================================================
# M15.3 · fit 有效时长契约（持久化/短 take 策略）
# ================================================================

class TestFitWindows:
    def test_fit_long_take_persists_effective_window(self):
        from src.features.edit.core.audition import (
            Audition, AuditionTake, switch_take,
        )
        a = Audition(
            audition_id="a1", timeline_id="t", track_id="v1",
            timeline_start=0,
            takes=(
                AuditionTake(take_id="short", asset_id="x",
                             source_start=0, source_duration=50),
                AuditionTake(take_id="long", asset_id="y",
                             source_start=0, source_duration=120),
            ),
            active_take_id="short")
        result = switch_take(a, "long", placement="fit")
        assert result.fitted
        win = result.audition.fit_windows["long"]
        assert win["effective_frames"] == 50   # 裁到现容器时长
        assert win["short_policy"] == "hole"
        # 持久化往返
        rt = Audition.from_dict(result.audition.to_dict())
        assert rt.fit_windows["long"]["effective_frames"] == 50

    def test_fit_short_take_records_full_window_and_policy(self):
        from src.features.edit.core.audition import (
            Audition, AuditionTake, switch_take,
        )
        a = Audition(
            audition_id="a2", timeline_id="t", track_id="v1",
            timeline_start=0,
            takes=(
                AuditionTake(take_id="long", asset_id="y",
                             source_start=0, source_duration=100),
                AuditionTake(take_id="short", asset_id="x",
                             source_start=0, source_duration=40),
            ),
            active_take_id="long")
        result = switch_take(a, "short", placement="fit",
                             fit_short_policy="hold_last")
        assert not result.fitted  # 短 take 不裁
        win = result.audition.fit_windows["short"]
        assert win["effective_frames"] == 40   # 全窗
        assert win["short_policy"] == "hold_last"

    def test_ripple_does_not_record_fit_window(self):
        from src.features.edit.core.audition import (
            Audition, AuditionTake, switch_take,
        )
        a = Audition(
            audition_id="a3", timeline_id="t", track_id="v1",
            timeline_start=0,
            takes=(
                AuditionTake(take_id="s", asset_id="x",
                             source_start=0, source_duration=50),
                AuditionTake(take_id="l", asset_id="y",
                             source_start=0, source_duration=120),
            ),
            active_take_id="s")
        result = switch_take(a, "l", placement="ripple")
        assert result.audition.fit_windows == {}
        assert result.timeline_shift == 70

    def test_old_projects_without_fit_windows_load(self):
        from src.features.edit.core.audition import Audition
        a = Audition.from_dict({
            "audition_id": "old", "timeline_id": "t", "track_id": "v1",
            "timeline_start": 0, "takes": [], "active_take_id": None})
        assert a.fit_windows == {}
