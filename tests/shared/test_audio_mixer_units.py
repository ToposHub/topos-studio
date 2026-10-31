"""
Audio Mixer 单元测试

覆盖：
1. 单位换算函数（linear_to_db / db_to_linear / pan_to_balance / balance_to_pan）
   - 往返换算精度
   - 量程边界 clamp
2. AudioMixer.mix_samples 的 per-track peak 输出（tuple 返回值）
   - 各轨峰值 = amp * volume
   - muted / 非 solo 轨不出现
   - 返回值结构 (output, per_track_peaks)
3. AudioMixerPanel.sync_tracks 的 diff 同步行为
   - 新增 / 移除 / 已存在回填

这些测试不需要真实音频设备，纯数据/逻辑层验证。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.ui.audio_mixer.track_mixer_widget import (
    linear_to_db,
    db_to_linear,
    pan_to_balance,
    balance_to_pan,
    MASTER_TRACK_ID,
    VOLUME_DB_MIN,
    VOLUME_DB_MAX,
    VOLUME_LINEAR_MAX,
    BALANCE_MIN,
    BALANCE_MAX,
)


# =============================================================================
# 1. 单位换算
# =============================================================================

class TestLinearDbConversion:
    """linear_to_db / db_to_linear 往返换算与边界。"""

    @pytest.mark.parametrize("vol", [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0])
    def test_roundtrip_linear_db(self, vol):
        """线性→dB→线性 往返误差 < 0.01。

        注：vol=2.0 时 linear_to_db(2.0)=+6.02dB 被 clamp 到 VOLUME_DB_MAX=6.0，
        往返后有 ~0.2% 损失（0.02dB，人耳不可分辨），故容差放宽到 0.01 线性。
        """
        db = linear_to_db(vol)
        back = db_to_linear(db)
        assert abs(back - vol) < 0.01, f"{vol} -> db {db} -> {back}"

    def test_linear_unity_is_zero_db(self):
        """1.0 线性增益 = 0 dB（中性点）。"""
        assert linear_to_db(1.0) == pytest.approx(0.0, abs=1e-9)

    def test_db_zero_is_linear_unity(self):
        assert db_to_linear(0.0) == pytest.approx(1.0, abs=1e-9)

    def test_linear_clamps_to_db_min(self):
        """0.0 线性映射到 dB 下限（-60），不是 -inf。"""
        assert linear_to_db(0.0) == VOLUME_DB_MIN
        assert linear_to_db(-1.0) == VOLUME_DB_MIN  # 负值也 clamp

    def test_db_max_clamps_linear_to_two(self):
        """VOLUME_DB_MAX 对应的线性值 ≈ VOLUME_LINEAR_MAX。

        VOLUME_DB_MAX=6.0 与 VOLUME_LINEAR_MAX=2.0 不是精确对应
       （20*log10(2.0)=6.0206），db_to_linear(6.0)≈1.9953，差 0.2%。
        远超上限的值（如 +24dB）才被精确 clamp 到 2.0。
        """
        assert db_to_linear(VOLUME_DB_MAX) == pytest.approx(VOLUME_LINEAR_MAX, abs=0.01)
        assert db_to_linear(100.0) == VOLUME_LINEAR_MAX  # 远超上限，精确 clamp

    def test_db_min_clamps_linear(self):
        """极小 dB 值换算的线性值仍 clamp 在 [0, 2] 内（趋近于 0）。"""
        assert db_to_linear(VOLUME_DB_MIN) >= 0.0
        assert 0.0 <= db_to_linear(-1000.0) < 1e-6

    def test_monotonic(self):
        """线性增益越大，dB 越大（在有效区间内单调）。"""
        vals = [0.1, 0.25, 0.5, 1.0, 1.5, 2.0]
        dbs = [linear_to_db(v) for v in vals]
        assert dbs == sorted(dbs)


class TestPanBalanceConversion:
    """pan_to_balance / balance_to_pan 往返换算与边界。"""

    @pytest.mark.parametrize("pan", [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0])
    def test_roundtrip_pan_balance(self, pan):
        bal = pan_to_balance(pan)
        back = balance_to_pan(bal)
        # 整数化精度损失最多 1/50 = 0.02
        assert abs(back - pan) <= 0.02, f"{pan} -> bal {bal} -> {back}"

    def test_pan_center_is_balance_zero(self):
        assert pan_to_balance(0.0) == 0

    def test_balance_zero_is_pan_center(self):
        assert balance_to_pan(0) == 0.0

    def test_pan_left_is_balance_min(self):
        assert pan_to_balance(-1.0) == BALANCE_MIN

    def test_pan_right_is_balance_max(self):
        assert pan_to_balance(1.0) == BALANCE_MAX

    def test_pan_clamps(self):
        """超出 [-1,1] 的 pan 被 clamp。"""
        assert pan_to_balance(-5.0) == BALANCE_MIN
        assert pan_to_balance(5.0) == BALANCE_MAX

    def test_balance_clamps(self):
        """超出 [-50,50] 的 balance 被 clamp。"""
        assert balance_to_pan(-999) == -1.0
        assert balance_to_pan(999) == 1.0

    def test_balance_fifty_is_pan_one(self):
        assert balance_to_pan(BALANCE_MAX) == 1.0
        assert balance_to_pan(BALANCE_MIN) == -1.0


# =============================================================================
# 2. AudioMixer.mix_samples per-track peak
# =============================================================================

class _FakeDecoder:
    """返回固定振幅正弦波的假解码器，供 mix_samples 测试。"""

    def __init__(self, amp: float = 0.5, freq: float = 220.0):
        self.amp = amp
        self.freq = freq

    def decode_samples_for_frame(self, frame, fps, num_samples):
        t = np.linspace(0, 1, num_samples, dtype=np.float32)
        sig = (self.amp * np.sin(2 * np.pi * self.freq * t)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


@pytest.fixture
def audio_mixer():
    """构造一个空的 AudioMixer（需要 QApplication 因为用 QMutex）。"""
    from src.features.edit.core.audio_playback import AudioMixer
    return AudioMixer()


class TestMixSamplesPerTrackPeaks:
    """mix_samples 返回 (output, per_track_peaks) 且峰值正确。"""

    def test_returns_tuple(self, audio_mixer):
        """返回值必须是 (ndarray, dict) 二元组。"""
        audio_mixer.add_track(
            "t1", _FakeDecoder(0.5),
            __import__(
                "src.features.edit.core.audio_playback", fromlist=["AudioTrackState"]
            ).AudioTrackState(track_id="t1", volume=1.0),
        )
        result = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert isinstance(result, tuple) and len(result) == 2
        out, peaks = result
        assert isinstance(out, np.ndarray)
        assert isinstance(peaks, dict)

    def test_peak_equals_amp_times_volume(self, audio_mixer):
        """立体声中心为 balance unity：峰值 ≈ amp * volume。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.5),
                              AudioTrackState(track_id="t1", volume=1.0))
        _, peaks = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert "t1" in peaks
        l, r = peaks["t1"]
        assert abs(l - 0.5) < 0.02
        assert abs(r - 0.5) < 0.02

    def test_peak_scales_with_volume(self, audio_mixer):
        """volume=0.5 时立体声中心峰值减半。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.4),
                              AudioTrackState(track_id="t1", volume=0.5))
        _, peaks = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert abs(peaks["t1"][0] - 0.4 * 0.5) < 0.02

    def test_muted_track_excluded(self, audio_mixer):
        """muted 轨不出现在 peaks 中。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.9),
                              AudioTrackState(track_id="t1", volume=1.0, muted=True))
        _, peaks = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert "t1" not in peaks

    def test_non_solo_track_excluded_when_solo_exists(self, audio_mixer):
        """有 solo 轨时，非 solo 轨被排除。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.5),
                              AudioTrackState(track_id="t1", volume=1.0, solo=True))
        audio_mixer.add_track("t2", _FakeDecoder(0.5),
                              AudioTrackState(track_id="t2", volume=1.0, solo=False))
        _, peaks = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert "t1" in peaks
        assert "t2" not in peaks

    def test_multiple_tracks_each_reported(self, audio_mixer):
        """多轨各自报告立体声 balance 居中峰值。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.5),
                              AudioTrackState(track_id="t1", volume=1.0))
        audio_mixer.add_track("t2", _FakeDecoder(0.3),
                              AudioTrackState(track_id="t2", volume=1.0))
        _, peaks = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert set(peaks.keys()) == {"t1", "t2"}
        assert abs(peaks["t1"][0] - 0.5) < 0.02
        assert abs(peaks["t2"][0] - 0.3) < 0.02

    def test_silent_track_not_in_peaks(self, audio_mixer):
        """无信号的轨（amp=0）不出现在 peaks（peak 为 0 时省略）。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.0),
                              AudioTrackState(track_id="t1", volume=1.0))
        _, peaks = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert "t1" not in peaks

    def test_output_shape_unchanged(self, audio_mixer):
        """返回的 output 形状仍是 (channels, samples)。"""
        from src.features.edit.core.audio_playback import AudioTrackState
        audio_mixer.add_track("t1", _FakeDecoder(0.5),
                              AudioTrackState(track_id="t1", volume=1.0))
        out, _ = audio_mixer.mix_samples(0, 24.0, 480, 2)
        assert out.shape == (2, 480)


# =============================================================================
# 3. AudioMixerPanel.sync_tracks diff 同步（需要 qapp）
# =============================================================================

class TestSyncTracks:
    """sync_tracks 增删 + 静默回填。"""

    def test_adds_missing_strips(self, qapp):
        from src.shared.ui.audio_mixer import AudioMixerPanel
        panel = AudioMixerPanel()
        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
            {"track_id": "t2", "name": "Music", "tag": "A2",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
        ])
        assert set(panel._track_mixers.keys()) == {"t1", "t2"}

    def test_removes_obsolete_strips(self, qapp):
        from src.shared.ui.audio_mixer import AudioMixerPanel
        panel = AudioMixerPanel()
        panel.register_track("t1", "Voice", "A1")
        panel.register_track("t2", "Music", "A2")
        panel.register_track("t3", "SFX", "A3")
        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
        ])
        assert set(panel._track_mixers.keys()) == {"t1"}
        assert "t2" not in panel._track_mixers
        assert "t3" not in panel._track_mixers

    def test_backfills_values_silently(self, qapp):
        """已存在的 strip 静默回填 volume/pan/mute，不触发信号。"""
        from src.shared.ui.audio_mixer import AudioMixerPanel
        panel = AudioMixerPanel()
        emitted = []
        panel.track_volume_changed.connect(lambda *a: emitted.append(a))
        panel.register_track("t1", "Voice", "A1")
        emitted.clear()
        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 0.5, "pan": -0.5, "is_muted": True, "is_solo": False},
        ])
        # 回填不应触发 track_volume_changed
        assert emitted == []
        m = panel._track_mixers["t1"]
        assert abs(m.get_volume_linear() - 0.5) < 1e-6
        assert abs(m.get_pan_linear() - (-0.5)) < 0.02
        assert m.is_mute() is True

    def test_empty_tracks_clears_all(self, qapp):
        from src.shared.ui.audio_mixer import AudioMixerPanel
        panel = AudioMixerPanel()
        panel.register_track("t1", "Voice", "A1")
        panel.sync_tracks([])
        assert panel._track_mixers == {}

    def test_preserves_existing_on_partial_change(self, qapp):
        """轨道集合不变时，strip 复用（不重建），只回填值。"""
        from src.shared.ui.audio_mixer import AudioMixerPanel
        panel = AudioMixerPanel()
        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
        ])
        mixer_obj_before = panel._track_mixers["t1"]
        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 0.75, "pan": 0.25, "is_muted": False, "is_solo": False},
        ])
        mixer_obj_after = panel._track_mixers["t1"]
        # 同一个 widget 实例（复用，不重建）
        assert mixer_obj_before is mixer_obj_after
        assert abs(mixer_obj_after.get_volume_linear() - 0.75) < 1e-6


# =============================================================================
# 4. 推子 dB↔slider 映射（W2 重写后的分段线性映射）
# =============================================================================

class TestFaderDbSliderMapping:
    """_db_to_slider / _slider_to_db 往返与关键点。

    这些方法在 TrackMixerWidget 实例上，需要构造一个 widget 来访问。
    映射特性（达芬奇同款 IEC 对齐标度）：
      - 0 dB → 8000（unity，对应电平表顶部 0dBFS）
      - +6 dB → 10000（顶部，增益裕量上限，与数据层 volume≤2.0 一致）
      - -60 dB → 0（底部，无死区）
      - 正 dB 段 [0,+6] → [8000,10000] 独立线性映射
      - 负 dB 段 [-60,0] → [0,8000] 走 IEC 标度并归一化（消除底部死区）
    """

    @pytest.fixture
    def mixer_widget(self, qapp):
        from src.shared.ui.audio_mixer import TrackMixerWidget
        w = TrackMixerWidget("test-track-001", "Test", "A1", is_master=False)
        return w

    @pytest.mark.parametrize("db", [
        0.0, 3.0, 6.0,              # 正 dB 段 [0, +6]
        -3.0, -6.0, -12.0,          # 负 dB 高段
        -20.0, -30.0, -40.0,        # 负 dB 中段
        -50.0, -60.0,               # 负 dB 低段
    ])
    def test_roundtrip_db_slider(self, mixer_widget, db):
        """dB→slider→dB 往返误差 < 0.5 dB（slider 是 10000 档整数精度）。"""
        slider = mixer_widget._db_to_slider(db)
        back = mixer_widget._slider_to_db(slider)
        assert abs(back - db) < 0.5, f"{db} -> slider {slider} -> {back}"

    def test_zero_db_maps_to_neutral(self, mixer_widget):
        """0 dB → 8000（unity，对应电平表顶部）。"""
        assert mixer_widget._db_to_slider(0.0) == 8000

    def test_max_db_maps_to_top(self, mixer_widget):
        """+6 dB → 10000（推子顶部）。"""
        assert mixer_widget._db_to_slider(VOLUME_DB_MAX) == 10000

    def test_min_db_maps_to_bottom(self, mixer_widget):
        """-60 dB → 0（推子底部，无死区）。"""
        assert mixer_widget._db_to_slider(VOLUME_DB_MIN) == 0

    def test_clamps_out_of_range(self, mixer_widget):
        """超出范围的 dB 被 clamp 到 [min, max] 对应的 slider 端点。"""
        assert mixer_widget._db_to_slider(100.0) == 10000  # 远超 +6
        assert mixer_widget._db_to_slider(-100.0) == 0     # 远低于 -60

    def test_slider_zero_is_min_db(self, mixer_widget):
        """slider 0 → -60 dB（底部无死区）。"""
        assert mixer_widget._slider_to_db(0) == pytest.approx(VOLUME_DB_MIN, abs=1e-6)

    def test_slider_top_is_max_db(self, mixer_widget):
        """slider 10000 → +6 dB。"""
        assert mixer_widget._slider_to_db(10000) == pytest.approx(VOLUME_DB_MAX, abs=1e-6)

    def test_monotonic_increasing(self, mixer_widget):
        """dB 递增 → slider 严格递增。"""
        dbs = [-60, -50, -40, -30, -24, -18, -12, -6, -3, 0, 3, 6]
        sliders = [mixer_widget._db_to_slider(d) for d in dbs]
        for i in range(len(sliders) - 1):
            assert sliders[i] < sliders[i + 1], f"{dbs[i]} -> {sliders[i]} not < {dbs[i+1]} -> {sliders[i+1]}"


# =============================================================================
# 5. IEC 标度逆函数（W2 iec_inverse）
# =============================================================================

class TestIecInverse:
    """iec_scale / iec_inverse 互逆性。"""

    @pytest.mark.parametrize("db", [-70.0, -65.0, -55.0, -45.0, -35.0, -25.0, -20.0,
                                    -15.0, -12.0, -6.0, -3.0, 0.0])
    def test_inverse_of_scale(self, db):
        """iec_inverse(iec_scale(db)) ≈ db（覆盖 IEC 完整定义域 [-70, 0]）。"""
        from src.shared.ui.audio_mixer.audio_level_meter import iec_scale, iec_inverse
        s = iec_scale(db)
        back = iec_inverse(s)
        assert abs(back - db) < 0.5, f"{db} -> scale {s} -> {back}"

    def test_scale_of_inverse(self):
        """iec_scale(iec_inverse(s)) ≈ s（覆盖标度值区间 [0, 1.0]）。"""
        from src.shared.ui.audio_mixer.audio_level_meter import iec_scale, iec_inverse
        for s in [0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.85, 0.9, 1.0]:
            db = iec_inverse(s)
            back = iec_scale(db)
            assert abs(back - s) < 0.01, f"{s} -> db {db} -> {back}"
