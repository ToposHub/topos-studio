"""WD17-G98 修复验证：batch-376 / 377 / 378 shared-ui-audio_mixer 审计发现。

覆盖（每条含正例与反例）：
- AUD-37601  AudioFader._value_from_position 滑轨零长度除零防护
- AUD-37602  无值变化的点击释放不再发射信号对；滚轮/键盘离散增量合并为
             单次 press→(preview)→release 会话（消费端三段式节流据此
             折叠为单条提交，撤销栈不再被空/碎片命令污染）
- AUD-37611  set_meter_mode 不再用宽泛 except 吞掉信号发射中的槽异常
- AUD-37621  LUFS 后端探测缓存 + 失败按"不可用"处理（fail-closed，
             不再显示伪造 LUFS 读数）
- AUD-37622  additive（Shift）solo 解除轨道的联动静音
- AUD-37623  register_track 应用当前 _narrow_layout
- AUD-37624  sync_tracks 回填 solo/mute 后重建 _solo_tracks/_solo_muted
- AUD-37625  solo 激活期间 sync_tracks 不按模型 is_muted 复位联动静音 strip
- AUD-37626  collapse_mixers 在 _master_mixer 的直接父布局上设置对齐
- AUD-37701  关键帧拖动改为本地预览 + 松手一次性提交（无杂散关键帧、
             不逐 move 刷撤销栈）
- AUD-37702  （BLOCKED，消费端接线在 edit_controller）本文件仅固化
             set_keyframe_interp 发射侧契约
- AUD-37706  Dynamics 无实例 tab 显示类默认参数（非范围最小值）
- AUD-37712  Limiter Reset 阻塞信号，一次点击恰好一次 committed 且
             两个旋钮都被完整重置
- AUD-37804  保存预设补齐 effect_chain/output_bus_id/sends/is_bus，
             created_at 用保存时刻
- AUD-37810  路由校验失败阻止提交（_on_committed 与 closeEvent）；
             routing_snapshot 注入完整图后 Bus→Bus 环可检出
- AUD-37811  Send level 拖动期发 routing_preview（实时预览）

UI 测试用 QT_QPA_PLATFORM=offscreen，不依赖真实音频设备。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, QPoint, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent


# ────────────────────────── 测试工具 ──────────────────────────


def _mouse_event(
    event_type: QEvent.Type,
    pos: QPointF,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> QMouseEvent:
    """构造本地坐标鼠标事件（offscreen 测试用）。"""
    return QMouseEvent(
        event_type, pos, pos, button, button, Qt.KeyboardModifier.NoModifier
    )


def _wheel_event(delta_y: int = 120) -> QWheelEvent:
    """构造向上滚一格的滚轮事件。"""
    return QWheelEvent(
        QPointF(10.0, 10.0), QPointF(10.0, 10.0),
        QPoint(0, 0), QPoint(0, delta_y),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


class _ConsumerSim:
    """模拟 track_mixer_widget 的三段式节流消费端。

    pressed → active=True；valueChanged → active 时 preview、否则 commit；
    released → active 时 active=False 并 commit 一次。
    """

    def __init__(self, fader) -> None:
        self.press_count = 0
        self.release_count = 0
        self.previews: list[int] = []
        self.commits: list[int] = []
        self.active = False
        fader.slider_pressed.connect(self._on_pressed)
        fader.slider_released.connect(self._on_released)
        fader.valueChanged.connect(self._on_value)

    def _on_pressed(self) -> None:
        self.active = True
        self.press_count += 1

    def _on_released(self) -> None:
        if not self.active:
            return
        self.active = False
        self.release_count += 1
        self.commits.append(self._last_value)

    def _on_value(self, value: int) -> None:
        self._last_value = value
        if self.active:
            self.previews.append(value)
        else:
            self.commits.append(value)


# ────────────────────────── AUD-37601: 滑轨零长度除零 ──────────────────────────


class TestAud37601ZeroLengthTrack:
    def test_vertical_degenerate_geometry_no_division_error(self, qapp):
        """widget 高度 == knob 高度（宽推子 31px）时 _value_from_position
        返回 min_val 而非抛 ZeroDivisionError。"""
        from src.shared.ui.audio_mixer.audio_fader import AudioFader

        fader = AudioFader(orientation=Qt.Orientation.Vertical, narrow_knob=False)
        fader.resize(60, fader.WIDE_KNOB_HEIGHT)  # 滑轨零长度
        assert fader._value_from_position(5.0) == fader.minimum()

    def test_vertical_degenerate_mouse_paths_do_not_raise(self, qapp):
        """鼠标按下/拖动路径（事件处理器直接调用）在零长度滑轨下不抛异常。"""
        from src.shared.ui.audio_mixer.audio_fader import AudioFader

        fader = AudioFader(orientation=Qt.Orientation.Vertical)
        fader.resize(60, fader.WIDE_KNOB_HEIGHT)
        fader.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(20.0, 5.0)))
        fader.mouseMoveEvent(
            _mouse_event(
                QEvent.Type.MouseMove,
                QPointF(20.0, 3.0),
                button=Qt.MouseButton.LeftButton,
            )
        )
        fader.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(20.0, 5.0)))

    def test_horizontal_degenerate_geometry(self, qapp):
        from src.shared.ui.audio_mixer.audio_fader import AudioFader

        fader = AudioFader(orientation=Qt.Orientation.Horizontal)
        fader.resize(fader.WIDE_KNOB_HEIGHT, 60)  # 横向滑轨零长度
        assert fader._value_from_position(5.0) == fader.minimum()

    def test_normal_geometry_still_maps(self, qapp):
        """反例：正常几何下映射不受守卫影响（垂直推子顶部=最大值）。"""
        from src.shared.ui.audio_mixer.audio_fader import AudioFader

        fader = AudioFader(orientation=Qt.Orientation.Vertical)
        fader.resize(60, 200)
        knob_half = fader.WIDE_KNOB_HEIGHT / 2.0
        top = 0.0 + knob_half      # 滑轨顶端
        bottom = 200.0 - knob_half  # 滑轨底端
        # 顶端 → 最大值，底端 → 最小值
        assert fader._value_from_position(top) == fader.maximum()
        assert fader._value_from_position(bottom) == fader.minimum()


# ────────────────────────── AUD-37602: 提交通道去污染 ──────────────────────────


class TestAud37602CommitChannel:
    def _shown_fader(self, qapp, height: int = 220):
        from src.shared.ui.audio_mixer.audio_fader import AudioFader

        fader = AudioFader(orientation=Qt.Orientation.Vertical)
        fader.resize(60, height)
        fader.show()
        qapp.processEvents()
        return fader

    def test_click_on_knob_without_change_emits_nothing(self, qapp):
        """原地点击 knob（值不变）：整轮按下/释放不发任何信号。"""
        fader = self._shown_fader(qapp)
        consumer = _ConsumerSim(fader)
        knob_pos = fader._position_from_value(fader.value())
        knob_rect = fader._get_knob_rect(knob_pos)
        center = QPointF(knob_rect.center().x(), knob_rect.center().y())

        fader.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, center))
        fader.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, center))

        assert consumer.press_count == 0
        assert consumer.release_count == 0
        assert consumer.commits == []

    def test_groove_click_jump_commits_exactly_once(self, qapp):
        """反例：点击槽道空白跳值 —— 值有变化时仍恰好一次 press + 一次提交。"""
        fader = self._shown_fader(qapp)
        consumer = _ConsumerSim(fader)
        groove_bottom = QPointF(
            fader._get_groove_rect(fader.rect()).center().x(),
            float(fader.height() - 2),
        )

        fader.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, groove_bottom))
        assert consumer.press_count == 1
        assert fader.value() == fader.minimum()  # 底部 = 最小值
        fader.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, groove_bottom))
        assert consumer.release_count == 1
        assert consumer.commits == [fader.minimum()]

    def test_drag_commits_once_on_release(self, qapp):
        """反例：正常拖动多次 move → 多次 preview、恰好一次提交。"""
        fader = self._shown_fader(qapp)
        consumer = _ConsumerSim(fader)
        knob_pos = fader._position_from_value(fader.value())
        center = QPointF(fader._get_groove_rect(fader.rect()).center().x(), knob_pos)

        fader.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, center))
        for y in range(int(knob_pos) - 10, int(knob_pos) - 60, -5):
            fader.mouseMoveEvent(
                _mouse_event(
                    QEvent.Type.MouseMove,
                    QPointF(center.x(), float(y)),
                    button=Qt.MouseButton.LeftButton,
                )
            )
        fader.mouseReleaseEvent(
            _mouse_event(QEvent.Type.MouseButtonRelease, QPointF(center.x(), knob_pos - 50.0))
        )
        assert consumer.press_count == 1
        assert consumer.release_count == 1
        assert len(consumer.previews) > 1
        assert len(consumer.commits) == 1

    def test_wheel_notches_coalesce_into_single_commit(self, qapp):
        """滚 20 格：修复前 20 条提交命令；修复后 1 条（去抖窗口内合并）。"""
        fader = self._shown_fader(qapp)
        fader.setValue(fader.minimum())  # 先就位，再挂消费端
        consumer = _ConsumerSim(fader)

        for _ in range(20):
            fader.wheelEvent(_wheel_event(120))

        # 窗口内：1 次 press，0 次提交，全部走 preview
        assert consumer.press_count == 1
        assert consumer.commits == []
        assert len(consumer.previews) == 20
        # 去抖窗口到期 → 恰好一次 release/提交
        fader._on_synth_timer()
        assert consumer.release_count == 1
        assert consumer.commits == [fader.value()]
        assert fader.value() == fader.minimum() + 20 * fader.singleStep()

    def test_wheel_at_boundary_no_signals(self, qapp):
        """反例：已到顶后再滚 —— 无值变化，不产生任何信号。"""
        fader = self._shown_fader(qapp)
        fader.setValue(fader.maximum())
        consumer = _ConsumerSim(fader)

        for _ in range(5):
            fader.wheelEvent(_wheel_event(120))

        assert consumer.press_count == 0
        assert consumer.commits == []
        fader._on_synth_timer()
        assert consumer.release_count == 0

    def test_home_key_jumps_in_session(self, qapp):
        """Home/End 跳极值也纳入合成会话（单次提交）。"""
        from PyQt6.QtGui import QKeyEvent

        fader = self._shown_fader(qapp)
        fader.setValue(5000)
        consumer = _ConsumerSim(fader)

        fader.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Home, Qt.KeyboardModifier.NoModifier
        ))
        assert fader.value() == fader.maximum()
        assert consumer.press_count == 1
        assert consumer.commits == []
        fader._on_synth_timer()
        assert consumer.commits == [fader.maximum()]

    def test_double_click_at_neutral_no_empty_commit(self, qapp):
        """已在中性位时双击 —— 无信号（此前发一对空 press/release → 空提交）。"""
        fader = self._shown_fader(qapp)
        fader.setValue(fader._neutral_position)
        consumer = _ConsumerSim(fader)
        knob_pos = fader._position_from_value(fader.value())
        center = QPointF(fader._get_groove_rect(fader.rect()).center().x(), knob_pos)

        fader.mouseDoubleClickEvent(_mouse_event(QEvent.Type.MouseButtonDblClick, center))
        assert consumer.press_count == 0
        assert consumer.release_count == 0
        assert consumer.commits == []

    def test_double_click_reset_commits_once(self, qapp):
        """反例：不在中性位时双击 —— 值跳变，恰好一次提交。"""
        fader = self._shown_fader(qapp)
        fader.setValue(2000)  # 偏离中性位 8000
        consumer = _ConsumerSim(fader)
        knob_pos = fader._position_from_value(fader.value())
        center = QPointF(fader._get_groove_rect(fader.rect()).center().x(), knob_pos)

        fader.mouseDoubleClickEvent(_mouse_event(QEvent.Type.MouseButtonDblClick, center))
        assert fader.value() == fader._neutral_position
        assert consumer.press_count == 1
        assert consumer.release_count == 1
        assert consumer.commits == [fader._neutral_position]


# ────────────────────────── AUD-37611: set_meter_mode 异常传播 ──────────────────────────


class TestAud37611MeterModeEmit:
    def test_no_broad_except_in_set_meter_mode(self, qapp):
        """铁律验证：set_meter_mode 不得再用宽泛 except 吞异常。"""
        import inspect

        from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter

        source = inspect.getsource(AudioLevelMeter.set_meter_mode)
        assert "except Exception" not in source
        assert "except:" not in source

    def test_slot_exception_surfaces_not_silenced(self, qapp, monkeypatch):
        """槽内异常必须浮出（PyQt6 经 sys.excepthook 上报），不得静默。"""
        from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter, MeterMode

        meter = AudioLevelMeter(channels=2)

        def broken_slot(_name: str) -> None:
            raise RuntimeError("slot exploded")

        surfaced: list[tuple] = []
        monkeypatch.setattr(
            sys, "excepthook", lambda *args: surfaced.append(args)
        )
        meter.meter_mode_changed.connect(broken_slot)
        meter.set_meter_mode(MeterMode.RMS)
        # 异常 surfaced 到 excepthook（未被执行任何静默吞没），且模式已切换
        assert len(surfaced) == 1
        assert issubclass(surfaced[0][0], RuntimeError)
        assert meter.meter_mode is MeterMode.RMS

    def test_normal_emit_still_works(self, qapp):
        from src.shared.ui.audio_mixer.audio_level_meter import AudioLevelMeter, MeterMode

        meter = AudioLevelMeter(channels=2)
        received: list[str] = []
        meter.meter_mode_changed.connect(received.append)
        meter.set_meter_mode(MeterMode.LUFS)
        assert received == ["LUFS"]
        assert meter.meter_mode is MeterMode.LUFS


# ────────────────────────── AUD-37621: LUFS 后端探测缓存 ──────────────────────────


def _make_panel_with_track(qapp, track_id: str = "t1"):
    from src.shared.ui.audio_mixer import AudioMixerPanel

    panel = AudioMixerPanel()
    panel.register_track(track_id, "Voice", "A1")
    mixer = panel._track_mixers[track_id]
    return panel, mixer


class TestAud37621LufsBackendProbe:
    def _lufs_levels(self, track_id: str = "t1") -> dict:
        return {track_id: {"peak": [-6.0, -6.0], "rms": [-9.0, -9.0],
                           "lufs": [-70.0, -70.0]}}

    def test_probe_failure_fails_closed_and_shows_peak(self, qapp):
        """探测失败按'不可用'处理：显示 peak 并标记降级，绝不显示伪造 LUFS。"""
        from src.shared.ui.audio_mixer.audio_level_meter import MeterMode

        panel, mixer = _make_panel_with_track(qapp)
        mixer._meter.set_meter_mode(MeterMode.LUFS)
        with patch(
            "src.features.edit.core.audio_capabilities.is_scipy_available_for_dsp",
            side_effect=ImportError("no scipy"),
        ) as mock_probe:
            panel.update_per_track_levels(self._lufs_levels())
            assert mock_probe.call_count == 1
            assert panel._lufs_backend_ok is False

        # 峰值读数已送达（而非 -70 的伪造 LUFS），且 meter 标记降级
        assert mixer._meter._value_decibels[0] == pytest.approx(-6.0, abs=0.01)
        assert mixer._meter.is_lufs_backend_unavailable is True

    def test_probe_result_cached_across_frames(self, qapp):
        """缓存：第二次帧更新不再探测（热路径无 import）。"""
        from src.shared.ui.audio_mixer.audio_level_meter import MeterMode

        panel, mixer = _make_panel_with_track(qapp)
        mixer._meter.set_meter_mode(MeterMode.LUFS)
        with patch(
            "src.features.edit.core.audio_capabilities.is_scipy_available_for_dsp",
            return_value=True,
        ) as mock_probe:
            for _ in range(5):
                panel.update_per_track_levels(self._lufs_levels())
            assert mock_probe.call_count == 1
            assert panel._lufs_backend_ok is True
        # 后端可用时显示 LUFS 读数、无降级标记
        assert mixer._meter.is_lufs_backend_unavailable is False

    def test_runtime_error_also_fails_closed(self, qapp):
        from src.shared.ui.audio_mixer.audio_level_meter import MeterMode

        panel, mixer = _make_panel_with_track(qapp)
        mixer._meter.set_meter_mode(MeterMode.LUFS)
        with patch(
            "src.features.edit.core.audio_capabilities.is_scipy_available_for_dsp",
            side_effect=RuntimeError("boom"),
        ):
            panel.update_per_track_levels(self._lufs_levels())
        assert panel._lufs_backend_ok is False


# ────────────────────────── AUD-37622: additive solo 解除联动静音 ──────────────────────────


class TestAud37622AdditiveSolo:
    def test_shift_solo_unmutes_track_removed_from_solo_muted(self, qapp):
        """A 独奏使 B 联动静音；Shift+点 B 的 solo → B 解除静音并加入独奏组。"""
        panel, _ = _make_panel_with_track(qapp, "A")
        panel.register_track("B", "Music", "A2")

        panel.set_track_solo("A", True)          # B 被联动静音
        assert panel._track_mixers["B"].is_mute() is True
        assert "B" in panel._solo_muted

        panel.set_track_solo("B", True, additive=True)
        # 修复点：B 不再"solo 且静音"
        assert panel._track_mixers["B"].is_mute() is False
        assert "B" not in panel._solo_muted
        assert set(panel._solo_tracks) == {"A", "B"}
        assert panel._track_mixers["B"]._is_solo is True

    def test_clear_all_solo_restores_everything_after_additive(self, qapp):
        """additive 后 clear_all_solo：无静音残留（此前 B 的静音永久残留）。"""
        panel, _ = _make_panel_with_track(qapp, "A")
        panel.register_track("B", "Music", "A2")
        panel.set_track_solo("A", True)
        panel.set_track_solo("B", True, additive=True)

        cleared: list = []
        panel.batch_solo_cleared.connect(cleared.extend)
        panel.clear_all_solo()

        assert sorted(cleared) == ["A", "B"]
        assert panel._track_mixers["A"].is_mute() is False
        assert panel._track_mixers["B"].is_mute() is False
        assert panel._solo_muted == []


# ────────────────────────── AUD-37623: register_track 应用窄布局 ──────────────────────────


class TestAud37623RegisterNarrow:
    def test_new_strip_follows_narrow_layout(self, qapp):
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.set_narrow_layout(True)
        panel.register_track("t2", "Music", "A2")
        new_mixer = panel._track_mixers["t2"]
        assert new_mixer._is_narrow is True
        assert new_mixer.minimumWidth() == 70  # set_narrow 的窄宽约束

    def test_wide_layout_unchanged(self, qapp):
        """反例：宽布局下新增 strip 保持统一宽度。"""
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.register_track("t2", "Music", "A2")
        new_mixer = panel._track_mixers["t2"]
        assert new_mixer._is_narrow is False
        assert new_mixer.minimumWidth() == panel._uniform_strip_width


# ────────────────────────── AUD-37624: sync_tracks 重建 solo 记账 ──────────────────────────


class TestAud37624SyncSoloAccounting:
    @staticmethod
    def _tracks(solo_t1: bool) -> list[dict]:
        return [
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": solo_t1},
            {"track_id": "t2", "name": "Music", "tag": "A2",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
        ]

    def test_external_solo_restore_rebuilds_solo_tracks(self, qapp):
        """undo/外部恢复 solo 后，sync_tracks 回填使 _solo_tracks 与显示一致。"""
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.register_track("t2", "Music", "A2")
        # 模拟外部（undo/工程加载）恢复 t1 的 solo：
        panel._track_mixers["t1"].set_solo(True)
        assert panel._solo_tracks == []  # 静默回填不维护记账（bug 现场）

        panel.sync_tracks(self._tracks(solo_t1=True))
        assert panel._solo_tracks == ["t1"]
        # 右键菜单 "Clear All Solo" 的可用性依据恢复
        cleared: list = []
        panel.batch_solo_cleared.connect(cleared.extend)
        panel.clear_all_solo()
        assert cleared == ["t1"]

    def test_no_solo_rebuilds_empty(self, qapp):
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.register_track("t2", "Music", "A2")
        panel.sync_tracks(self._tracks(solo_t1=False))
        assert panel._solo_tracks == []
        assert panel._solo_muted == []


# ────────────────────────── AUD-37625: solo 激活期间不复位联动静音 ──────────────────────────


class TestAud37625SyncKeepsLinkMuted:
    def test_link_muted_strip_stays_muted_during_solo(self, qapp):
        """solo 激活期间 sync_tracks 不把被联动静音的 strip 复位为'未静音'。"""
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.register_track("t2", "Music", "A2")
        panel.set_track_solo("t1", True)
        assert panel._track_mixers["t2"].is_mute() is True

        # timeline 变化触发 sync（模型里 t2 is_muted=False —— 联动静音只是
        # UI 派生态），t2 显示态必须保持静音（与引擎听感一致）
        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": True},
            {"track_id": "t2", "name": "Music", "tag": "A2",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
        ])
        assert panel._track_mixers["t2"].is_mute() is True
        assert panel._solo_muted == ["t2"]

    def test_unmuted_after_solo_clears(self, qapp):
        """反例：solo 清除后 sync —— 联动静音解除，显示回到模型态。"""
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.register_track("t2", "Music", "A2")
        panel.set_track_solo("t1", True)

        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
            {"track_id": "t2", "name": "Music", "tag": "A2",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False},
        ])
        assert panel._track_mixers["t2"].is_mute() is False
        assert panel._solo_muted == []

    def test_explicit_model_mute_not_in_solo_muted(self, qapp):
        """用户显式静音的轨道不进 _solo_muted（solo 清除后仍应保持静音）。"""
        panel, _ = _make_panel_with_track(qapp, "t1")
        panel.register_track("t2", "Music", "A2")
        panel.set_track_solo("t1", True)

        panel.sync_tracks([
            {"track_id": "t1", "name": "Voice", "tag": "A1",
             "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": True},
            {"track_id": "t2", "name": "Music", "tag": "A2",
             "volume": 1.0, "pan": 0.0, "is_muted": True, "is_solo": False},
        ])
        assert "t2" not in panel._solo_muted
        assert panel._track_mixers["t2"].is_mute() is True


# ────────────────────────── AUD-37626: 折叠居中对齐 ──────────────────────────


class TestAud37626CollapseAlignment:
    def _master_item_alignment(self, panel):
        idx = panel._master_container.indexOf(panel._master_mixer)
        assert idx >= 0, "master_mixer 必须是 master_container 的直接子项"
        return panel._master_container.itemAt(idx).alignment()

    def test_collapse_sets_alignment_on_direct_parent_layout(self, qapp):
        panel, _ = _make_panel_with_track(qapp, "t1")
        assert self._master_item_alignment(panel) == Qt.AlignmentFlag(0)
        panel.collapse_mixers(True)
        assert (self._master_item_alignment(panel)
                & Qt.AlignmentFlag.AlignHCenter) == Qt.AlignmentFlag.AlignHCenter
        panel.collapse_mixers(False)
        assert self._master_item_alignment(panel) == Qt.AlignmentFlag(0)


# ────────────────────────── AUD-37701: 关键帧拖动 ──────────────────────────


def _make_curve_widget(qapp, total_frames: int = 1000):
    from src.shared.ui.audio_mixer.automation_lane import AutomationCurveWidget
    from src.shared.keyframe import KeyframeTrack, Keyframe

    track = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
    track.add_keyframe(Keyframe(frame=100, value=1.0))
    track.add_keyframe(Keyframe(frame=500, value=1.5))

    curve = AutomationCurveWidget("volume", 0.0, 2.0, total_frames)
    curve.resize(600, 200)
    curve.set_track(track)
    curve.show()
    qapp.processEvents()
    return curve, track


class TestAud37701KeyframeDrag:
    def test_drag_emits_single_move_on_release(self, qapp):
        """拖动经多个中间帧：全程无 keyframe_moved，松手恰好一次，
        且模型在拖动期间未被污染出杂散关键帧。"""
        curve, track = _make_curve_widget(qapp)
        moves: list[tuple] = []
        curve.keyframe_moved.connect(lambda *a: moves.append(a))

        start = QPointF(curve._frame_to_x(100), curve._value_to_y(1.0))
        curve.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, start))
        assert curve._dragging_kf_frame == 100

        for frame in (200, 300, 350, 400):  # 鼠标扫过的中间帧
            curve.mouseMoveEvent(_mouse_event(
                QEvent.Type.MouseMove,
                QPointF(curve._frame_to_x(frame), curve._value_to_y(1.8)),
                button=Qt.MouseButton.LeftButton,
            ))
            assert moves == []                       # 拖动期不发命令
            assert len(track.keyframes) == 2         # 模型无杂散关键帧
            assert [k.frame for k in track.keyframes] == [100, 500]

        end = QPointF(curve._frame_to_x(400), curve._value_to_y(1.8))
        curve.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, end))

        assert len(moves) == 1
        old_frame, old_val, new_frame, new_val = moves[0]
        assert old_frame == 100
        assert old_val == pytest.approx(1.0)
        assert new_frame == 400
        assert new_val == pytest.approx(1.8, abs=0.05)
        assert curve._dragging_kf_frame is None

    def test_no_move_no_emit(self, qapp):
        """反例：按下后未移动即松手 —— 不发任何命令。"""
        curve, _ = _make_curve_widget(qapp)
        moves: list[tuple] = []
        curve.keyframe_moved.connect(lambda *a: moves.append(a))

        start = QPointF(curve._frame_to_x(100), curve._value_to_y(1.0))
        curve.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, start))
        curve.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, start))
        assert moves == []

    def test_drag_preview_paints_without_touching_model(self, qapp):
        """拖动预览只影响渲染（_drag_preview），模型 track 保持原样。"""
        curve, track = _make_curve_widget(qapp)
        start = QPointF(curve._frame_to_x(100), curve._value_to_y(1.0))
        curve.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, start))
        curve.mouseMoveEvent(_mouse_event(
            QEvent.Type.MouseMove,
            QPointF(curve._frame_to_x(300), curve._value_to_y(1.2)),
            button=Qt.MouseButton.LeftButton,
        ))
        assert curve._drag_preview is not None
        assert curve._drag_preview[0] == 300
        assert [k.frame for k in track.keyframes] == [100, 500]


# ────────────────────────── AUD-37702: 插值切换信号契约（发射侧） ──────────────────────────


class TestAud37702InterpolationSignalContract:
    """AUD-37702 的消费端接线缺失（edit_controller 未 connect
    set_keyframe_interp）在白名单之外，标记 BLOCKED。此处固化发射侧契约，
    供接线后回归：双击关键帧 → AutomationLane 重发带 track_id/param 的
    set_keyframe_interp。
    """

    def test_double_click_emits_interp_change(self, qapp):
        from src.shared.ui.audio_mixer.automation_lane import AutomationLane
        from src.shared.keyframe import KeyframeTrack, Keyframe

        lane = AutomationLane("track-1", "Voice", total_frames=1000)
        track = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        track.add_keyframe(Keyframe(frame=100, value=1.0))
        lane.update_track_data({"volume": track})
        lane._curve.resize(600, 200)
        qapp.processEvents()

        received: list[tuple] = []
        lane.set_keyframe_interp.connect(lambda *a: received.append(a))

        pos = QPointF(lane._curve._frame_to_x(100), lane._curve._value_to_y(1.0))
        lane._curve.mouseDoubleClickEvent(_mouse_event(QEvent.Type.MouseButtonDblClick, pos))

        assert len(received) == 1
        track_id, param, frame, interp = received[0]
        assert (track_id, param, frame, interp) == ("track-1", "volume", 100, "bezier")


# ────────────────────────── AUD-37706: Dynamics 默认参数显示 ──────────────────────────


class TestAud37706DynamicsDefaults:
    def test_tabs_without_instance_show_class_defaults(self, qapp):
        """chain 无实例时三个 tab 显示类默认参数（非范围最小值）。"""
        from src.shared.ui.audio_mixer.dynamics_dialog import DynamicsDialog

        dlg = DynamicsDialog(track_id="t1", track_name="Voice", chain=None)

        comp_threshold = dlg._comp_tab._knobs["threshold_db"]._spin.value()
        comp_release = dlg._comp_tab._knobs["release_ms"]._spin.value()
        assert comp_threshold == pytest.approx(-24.0)   # 默认，而非范围最小 -60
        assert comp_release == pytest.approx(250.0)

        ng_threshold = dlg._ng_tab._knobs["threshold_db"]._spin.value()
        assert ng_threshold == pytest.approx(-40.0)     # 默认，而非 -60

        lim_threshold = dlg._lim_tab._knobs["threshold_db"]._spin.value()
        lim_release = dlg._lim_tab._knobs["release_ms"]._spin.value()
        assert lim_threshold == pytest.approx(-3.0)     # 默认，而非 -20
        assert lim_release == pytest.approx(100.0)

    def test_tab_with_instance_shows_instance_params(self, qapp):
        """反例：有实例时仍显示实例参数。"""
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, TrackEffectInstance, CompressorParams,
        )
        from src.shared.ui.audio_mixer.dynamics_dialog import DynamicsDialog

        comp = CompressorParams(threshold_db=-6.0, ratio=8.0)
        chain = EffectChainParams(effects=(
            TrackEffectInstance(
                effect_type="compressor", params=comp.to_dict(),
                enabled=True, order=0, panel="dynamics",
            ),
        ))
        dlg = DynamicsDialog(track_id="t1", track_name="Voice", chain=chain)
        assert dlg._comp_tab._knobs["threshold_db"]._spin.value() == pytest.approx(-6.0)
        assert dlg._comp_tab._knobs["ratio"]._spin.value() == pytest.approx(8.0)


# ────────────────────────── AUD-37712: Limiter Reset ──────────────────────────


class TestAud37712LimiterReset:
    def test_reset_commits_once_and_resets_both_knobs(self, qapp):
        from src.shared.ui.audio_mixer.limiter_dialog import LimiterDialog

        dlg = LimiterDialog(track_id="t1", track_name="Voice", instance_id="i1")
        committed: list[object] = []
        dlg.limiter_committed.connect(lambda _tid, p: committed.append(p))

        # 偏离默认（直接调内部参数处理，模拟用户调参后的状态）
        dlg._thresh_spin.setValue(-10.0)
        dlg._rel_spin.setValue(500.0)
        assert dlg._thresh_spin.value() == pytest.approx(-10.0)
        assert dlg._rel_spin.value() == pytest.approx(500.0)
        committed.clear()

        dlg._on_reset()

        assert len(committed) == 1                     # 恰好一次提交
        assert committed[0].threshold_db == pytest.approx(-3.0)
        assert committed[0].release_ms == pytest.approx(100.0)
        # 两个旋钮都被完整重置
        assert dlg._thresh_spin.value() == pytest.approx(-3.0)
        assert dlg._rel_spin.value() == pytest.approx(100.0)


# ────────────────────────── AUD-37804: 预设保存补齐字段 ──────────────────────────


class TestAud37804PresetSave:
    def _dialog(self, qapp, source):
        from src.shared.ui.audio_mixer.preset_dialog import PresetDialog

        with patch("src.shared.ui.audio_mixer.preset_dialog.load_presets",
                   return_value=[]):
            dlg = PresetDialog(track_id="t1", track_name="Voice",
                               current_preset_source=source)
        return dlg

    def test_save_preserves_am13_fields_and_fresh_created_at(self, qapp):
        from datetime import datetime
        from src.features.edit.core.mixer_preset import ChannelStripPreset
        from src.features.edit.core.timeline_v2 import Send

        sends = (Send(target_bus_id="bus-1", level=0.5, enabled=True),)
        source = ChannelStripPreset(
            name="old", volume=0.8, effect_chain=object(),
            output_bus_id="bus-1", sends=sends, is_bus=True,
            created_at="2001-01-01T00:00:00",
        )
        dlg = self._dialog(qapp, source)
        dlg._name_edit.setText("My Preset")

        saved: list[ChannelStripPreset] = []
        with patch("src.shared.ui.audio_mixer.preset_dialog.add_preset",
                   side_effect=saved.append), \
             patch("src.shared.ui.audio_mixer.preset_dialog.load_presets",
                   return_value=[]):
            dlg._on_save_clicked()

        assert len(saved) == 1
        preset = saved[0]
        assert preset.name == "My Preset"
        assert preset.effect_chain is source.effect_chain     # FX 链不丢
        assert preset.output_bus_id == "bus-1"                # 路由不丢
        assert preset.sends == sends                          # sends 不丢
        assert preset.is_bus is True                          # is_bus 不丢
        assert preset.created_at != "2001-01-01T00:00:00"     # 保存时刻
        assert preset.created_at.startswith(datetime.now().strftime("%Y-%m-%d"))


# ────────────────────────── AUD-37810: 环检测阻止提交 ──────────────────────────


def _make_routing_dialog(qapp, routing_snapshot=None, with_send: bool = False):
    from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
    from src.features.edit.core.timeline_v2 import Send

    sends = (Send(target_bus_id="B1", level=1.0, enabled=True),) if with_send else ()
    dlg = RoutingDialog(
        track_id="T1", track_name="Track 1",
        output_bus_id=None, sends=sends,
        bus_tracks=[("B1", "Bus 1"), ("B2", "Bus 2")],
        routing_snapshot=routing_snapshot,
    )
    return dlg


class TestAud37810ValidationGate:
    def test_valid_payload_commits(self, qapp):
        """反例：合法路由照常提交。"""
        dlg = _make_routing_dialog(qapp, with_send=True)
        committed: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))
        dlg._on_committed()
        assert len(committed) == 1
        assert committed[0]["output_bus_id"] is None
        assert [s.target_bus_id for s in committed[0]["sends"]] == ["B1"]

    def test_cycle_in_snapshot_blocks_commit(self, qapp):
        """Bus→Bus 环（经 routing_snapshot 注入完整图）：校验失败不提交。"""
        snapshot = {
            "B1": {"output_bus_id": "B2", "sends": [], "is_bus": True},
            "B2": {"output_bus_id": "B1", "sends": [], "is_bus": True},
        }
        dlg = _make_routing_dialog(qapp, routing_snapshot=snapshot)
        committed: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))
        preview: list[dict] = []
        dlg.routing_preview.connect(lambda _tid, p: preview.append(p))

        dlg._on_committed()
        assert committed == []
        assert preview == []
        assert dlg._warning_label.isVisibleTo(dlg)
        assert "环" in dlg._warning_label.text()

    def test_close_event_blocked_on_invalid(self, qapp):
        """关闭路径同样被门控：无效路由不得经 closeEvent 落盘。"""
        from PyQt6.QtGui import QCloseEvent

        snapshot = {
            "B1": {"output_bus_id": "B2", "sends": [], "is_bus": True},
            "B2": {"output_bus_id": "B1", "sends": [], "is_bus": True},
        }
        dlg = _make_routing_dialog(qapp, routing_snapshot=snapshot)
        committed: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))
        dlg.closeEvent(QCloseEvent())
        assert committed == []


# ────────────────────────── AUD-37811: routing_preview 实时预览 ──────────────────────────


class TestAud37811RoutingPreview:
    def test_send_level_drag_emits_preview_then_commit(self, qapp):
        """拖动 send level：期间发 routing_preview（不提交），松手一次提交。"""
        dlg = _make_routing_dialog(qapp, with_send=True)
        slot = dlg._send_slots[0]
        committed: list[dict] = []
        preview: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))
        dlg.routing_preview.connect(lambda _tid, p: preview.append(p))

        slot._on_drag_started()          # 进入拖动
        slot._level_spin.setValue(0.4)   # 拖动期间连续调值
        slot._level_spin.setValue(0.25)

        assert len(preview) == 2                     # 每次调值都有实时预览
        assert committed == []                       # 拖动期不进命令栈
        assert preview[-1]["sends"][0].level == pytest.approx(0.25)

        slot._on_drag_ended()           # 松手
        assert len(committed) == 1
        assert committed[0]["sends"][0].level == pytest.approx(0.25)

    def test_discrete_change_still_commits_directly(self, qapp):
        """反例：非拖动的离散变化（目标切换）直接提交，不发 preview。"""
        dlg = _make_routing_dialog(qapp, with_send=True)
        committed: list[dict] = []
        preview: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))
        dlg.routing_preview.connect(lambda _tid, p: preview.append(p))

        dlg._send_slots[0]._target_combo.setCurrentIndex(2)  # B1 → B2
        assert len(committed) == 1
        assert preview == []
        assert committed[0]["sends"][0].target_bus_id == "B2"
