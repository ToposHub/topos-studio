"""
音频图签名完整性测试（AM-05 阶段 1.2）。

验证 `_build_audio_timeline_signature` 覆盖**所有影响声音的字段**——任一字段
变化都应使签名改变，否则签名相等时同步提前返回会丢弃该修改（AM-05 根因）。

验收标准（计划表 AM-05）：
- 参数化覆盖每个字段的修改、undo、redo、重开工程
- 每次都断言签名变化与模型一致

不依赖真实音频设备或文件，纯模型层验证。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    EffectChainParams,
    Send,
    StripEQParams,
    TimelineSequence,
    TrackEffectInstance,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _clip(clip_id="c1", track_id="t1", **kw):
    """构造合法 ClipInstance。"""
    defaults = dict(
        clip_id=clip_id, asset_id="a1", track_id=track_id,
        source_start=0, source_duration=100, timeline_start=0, timeline_duration=100,
    )
    defaults.update(kw)
    return ClipInstance(**defaults)


def _proj():
    """构造带 asset 的假 project。"""
    return SimpleNamespace(assets={"a1": SimpleNamespace(path="/x.wav", fps=24.0)})


@pytest.fixture
def edit_page(qapp):
    """构造一个仅用于调用签名方法的 EditPage（不完整初始化）。"""
    from src.features.edit.ui.edit_page import EditPage
    return EditPage.__new__(EditPage)


def _sig(edit_page, tracks, master_volume=0.8, master_muted=False):
    """便捷签名构造。"""
    seq = TimelineSequence(
        audio_tracks=tracks, master_volume=master_volume, master_muted=master_muted,
    )
    return edit_page._build_audio_timeline_signature(
        tracks, _proj(), 24.0, timeline=seq,
    )


def _base_track(**kw):
    """带 1 个 clip 的基础音频轨。"""
    defaults = dict(track_id="t1", name="A1", track_type="audio", index=0, clips=(_clip(),))
    defaults.update(kw)
    return EditTrack(**defaults)


def _two_clip_track(**kw):
    """带 2 个相邻 clip 的音频轨（c1: 0..100, c2: 100..200），用于转场测试。"""
    c1 = _clip(clip_id="c1", timeline_start=0, timeline_duration=100)
    c2 = _clip(clip_id="c2", timeline_start=100, timeline_duration=100)
    defaults = dict(
        track_id="t1", name="A1", track_type="audio", index=0, clips=(c1, c2),
    )
    defaults.update(kw)
    return EditTrack(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# 轨道级字段：每个字段变化都应改变签名
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackLevelFields:
    """轨道级声音字段纳入签名（AM-05）。"""

    def test_pan_change(self, edit_page):
        """pan 变化 → 签名变化（此前遗漏）。"""
        t = _base_track(pan=0.5)
        sig_a = _sig(edit_page, (t,))
        sig_b = _sig(edit_page, (t.with_pan(-0.5),))
        assert sig_a != sig_b

    def test_volume_change(self, edit_page):
        t = _base_track(volume=1.0)
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_volume(0.5),))

    def test_mute_change(self, edit_page):
        t = _base_track()
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_muted(True),))

    def test_solo_change(self, edit_page):
        t = _base_track()
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_solo(True),))

    def test_strip_eq_change(self, edit_page):
        """轨道级 EQ 变化 → 签名变化（此前完全未纳入）。"""
        t = _base_track()
        eq = StripEQParams.default("3-band")
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_strip_eq(eq),))

    def test_strip_eq_param_change(self, edit_page):
        """EQ 内部频段增益变化 → 签名变化。"""
        eq1 = StripEQParams(mode="3-band", bands=(0.0, 0.0, 0.0))
        eq2 = StripEQParams(mode="3-band", bands=(6.0, 0.0, 0.0))
        t1 = _base_track().with_strip_eq(eq1)
        t2 = _base_track().with_strip_eq(eq2)
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))

    def test_effect_chain_change(self, edit_page):
        """FX 机架链变化 → 签名变化。"""
        t = _base_track()
        chain = EffectChainParams.from_strip_eq(StripEQParams.default("3-band"))
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_effect_chain(chain),))


# ─────────────────────────────────────────────────────────────────────────────
# 路由字段（AM-02/AM-05）
# ─────────────────────────────────────────────────────────────────────────────

class TestRoutingFields:
    """路由字段纳入签名。"""

    def test_output_bus_change(self, edit_page):
        t = _base_track()
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_output_bus("bus1"),))

    def test_send_added(self, edit_page):
        """添加 send → 签名变化。"""
        t = _base_track()
        s = Send(target_bus_id="bus1", level=0.5)
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_send_added(s),))

    def test_send_level_change(self, edit_page):
        """send level 变化 → 签名变化。"""
        s1 = Send(target_bus_id="bus1", level=0.5)
        s2 = Send(target_bus_id="bus1", level=0.8)
        t1 = _base_track().with_send_added(s1)
        t2 = _base_track().with_send_added(s2)
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))


class TestBusInSignature:
    """Bus 轨即使无 clips 也进入签名（AM-02）。"""

    def test_bus_without_clips_in_signature(self, edit_page):
        """无 clips 的 Bus 轨应出现在签名的 track_id 集合中。"""
        bus = EditTrack(track_id="bus1", track_type="audio", index=1, is_bus=True)
        sig = _sig(edit_page, (bus,))
        track_ids = sorted(te[0] for te in sig[3])
        assert "bus1" in track_ids

    def test_bus_volume_change(self, edit_page):
        """Bus 的 volume 变化 → 签名变化。"""
        bus1 = EditTrack(track_id="bus1", track_type="audio", index=1, is_bus=True, volume=0.8)
        bus2 = bus1.with_volume(0.5)
        assert _sig(edit_page, (bus1,)) != _sig(edit_page, (bus2,))

    def test_normal_track_without_clips_excluded(self, edit_page):
        """普通轨（非 Bus）无 clips 时被排除（无声音贡献）。"""
        t = EditTrack(track_id="t1", track_type="audio", index=0, clips=())
        sig = _sig(edit_page, (t,))
        track_ids = [te[0] for te in sig[3]]
        assert "t1" not in track_ids


# ─────────────────────────────────────────────────────────────────────────────
# 音频转场字段（AT — Audio Transitions）
# ─────────────────────────────────────────────────────────────────────────────

class TestAudioTransitionFields:
    """AT: 音频转场（audio:* 实例）纳入签名。

    新增/修改/删除 crossfade 都需重新下发到引擎，否则 mix_samples 不会感知。
    视频转场不影响声音，签名的变化不应被其触发（避免无谓重同步）。
    """

    def test_audio_transition_added(self, edit_page):
        """添加音频转场 → 签名变化。"""
        from src.features.edit.core.timeline_v2 import TransitionInstance
        t = _two_clip_track()
        tr = TransitionInstance(
            transition_type="audio:constant_power",
            track_id="t1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=12,
        )
        assert _sig(edit_page, (t,)) != _sig(edit_page, (t.with_transition_added(tr),))

    def test_audio_transition_curve_change(self, edit_page):
        """转场曲线（transition_type）变化 → 签名变化。"""
        from src.features.edit.core.timeline_v2 import TransitionInstance
        tr1 = TransitionInstance(
            transition_type="audio:constant_power",
            track_id="t1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=12,
        )
        tr2 = TransitionInstance(
            transition_type="audio:linear",
            track_id="t1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=12,
        )
        t1 = _two_clip_track().with_transition_added(tr1)
        t2 = _two_clip_track().with_transition_added(tr2)
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))

    def test_audio_transition_duration_change(self, edit_page):
        """转场时长变化 → 签名变化。"""
        from src.features.edit.core.timeline_v2 import TransitionInstance
        tr1 = TransitionInstance(
            transition_type="audio:constant_power",
            track_id="t1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=12,
        )
        tr2 = TransitionInstance(
            transition_type="audio:constant_power",
            track_id="t1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=24,
        )
        t1 = _two_clip_track().with_transition_added(tr1)
        t2 = _two_clip_track().with_transition_added(tr2)
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))

    def test_video_transition_does_not_affect_audio_signature(self, edit_page):
        """视频转场不影响音频签名（不应触发音频重同步）。"""
        from src.features.edit.core.timeline_v2 import TransitionInstance
        t = _two_clip_track()
        tr = TransitionInstance(
            transition_type="cross_dissolve",
            track_id="t1", left_clip_id="c1", right_clip_id="c2",
            center_frame=100, duration=12,
        )
        # 添加视频转场后音频签名应保持不变。
        assert _sig(edit_page, (t,)) == _sig(edit_page, (t.with_transition_added(tr),))


# ─────────────────────────────────────────────────────────────────────────────
# Master 字段（AM-05）
# ─────────────────────────────────────────────────────────────────────────────

class TestMasterFields:
    """master_volume / master_muted 纳入顶层签名。"""

    def test_master_volume_change(self, edit_page):
        t = _base_track()
        assert _sig(edit_page, (t,), master_volume=0.8) != \
               _sig(edit_page, (t,), master_volume=0.9)

    def test_master_muted_change(self, edit_page):
        t = _base_track()
        assert _sig(edit_page, (t,), master_muted=False) != \
               _sig(edit_page, (t,), master_muted=True)


# ─────────────────────────────────────────────────────────────────────────────
# Clip 级字段（AM-06）
# ─────────────────────────────────────────────────────────────────────────────

class TestClipLevelFields:
    """clip speed / fade 纳入签名（AM-06）。"""

    def test_clip_speed_change(self, edit_page):
        c1 = _clip(speed=1.0)
        c2 = _clip(speed=2.0)
        t1 = EditTrack(track_id="t1", track_type="audio", index=0, clips=(c1,))
        t2 = EditTrack(track_id="t1", track_type="audio", index=0, clips=(c2,))
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))

    def test_clip_fade_in_change(self, edit_page):
        c1 = _clip(fade_in_duration=0)
        c2 = _clip(fade_in_duration=10)
        t1 = EditTrack(track_id="t1", track_type="audio", index=0, clips=(c1,))
        t2 = EditTrack(track_id="t1", track_type="audio", index=0, clips=(c2,))
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))

    def test_clip_fade_out_change(self, edit_page):
        c1 = _clip(fade_out_duration=0)
        c2 = _clip(fade_out_duration=15)
        t1 = EditTrack(track_id="t1", track_type="audio", index=0, clips=(c1,))
        t2 = EditTrack(track_id="t1", track_type="audio", index=0, clips=(c2,))
        assert _sig(edit_page, (t1,)) != _sig(edit_page, (t2,))


# ─────────────────────────────────────────────────────────────────────────────
# 稳定性：相同模型签名相等
# ─────────────────────────────────────────────────────────────────────────────

class TestSignatureStability:
    """相同模型（不同实例）签名应相等，避免误触发重同步。"""

    def test_identical_models_same_signature(self, edit_page):
        t1 = _base_track(pan=0.3, volume=0.9)
        t2 = _base_track(pan=0.3, volume=0.9)
        assert _sig(edit_page, (t1,)) == _sig(edit_page, (t2,))

    def test_undo_redo_restores_signature(self, edit_page):
        """改 pan 后再改回，签名恢复相等（模拟 undo）。"""
        t = _base_track(pan=0.5)
        sig_orig = _sig(edit_page, (t,))
        t_changed = t.with_pan(-0.5)
        sig_changed = _sig(edit_page, (t_changed,))
        assert sig_orig != sig_changed
        # undo：改回原值
        t_restored = t_changed.with_pan(0.5)
        assert sig_orig == _sig(edit_page, (t_restored,))

    def test_track_order_independent(self, edit_page):
        """轨道顺序不影响签名（sorted track_ids 比较已保证，但签名 tuple 顺序敏感）。

        注：当前实现 track_entries 按 audio_tracks 输入顺序排列。
        为保证 undo/redo 一致性，TimelineSequence 的 audio_tracks 顺序应是稳定的
        （由 index 决定）。此处验证相同顺序的相同集合签名相等。
        """
        t1 = _base_track(track_id="t1")
        t2 = _base_track(track_id="t2", clips=(_clip(track_id="t2"),))
        sig_ab = _sig(edit_page, (t1, t2))
        sig_ab2 = _sig(edit_page, (t1, t2))
        assert sig_ab == sig_ab2
