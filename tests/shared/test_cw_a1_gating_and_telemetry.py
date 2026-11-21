"""CW-A1 遥测生命周期回归测试。

覆盖三处审查修复：
1. 频谱门控：EQ 对话框点 ✕ 关闭（QDialog 默认 hide 非 destroy）后必须
   重算门控——否则引擎带着无消费者常开 FFT（finished 信号修复）。
2. 面板 deregister_track 后重算频谱门控（与 LUFS 门控对称）。
3. CompressorDialog.set_sidechain_level 存在且遥测分发不因单个对话框
   缺可选 API 中断兄弟表计（Reverb/Delay 输出表）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel
from src.shared.ui.audio_mixer.compressor_dialog import CompressorDialog


class TestSpectrumGatingLifecycle:
    """EQ 对话框开/关 → spectrum_metering_changed 精确翻转。"""

    def test_eq_badge_dialog_close_stops_metering(self, qapp):
        w = TrackMixerWidget(track_id="T1", track_name="Track 1")
        emitted: list = []
        w.spectrum_metering_changed.connect(lambda on: emitted.append(bool(on)))

        w._open_eq_from_badge()
        dlg = w._eq_dialog
        assert dlg is not None and dlg.isVisible()
        assert emitted[-1] is True, "EQ 对话框打开应开启频谱计算"

        # 点 ✕ / Esc 关闭：QDialog 只 hide 不 destroy —— finished 必须重算门控
        dlg.close()
        assert emitted[-1] is False, "EQ 对话框关闭（hide）应停止频谱计算"

    def test_strip_eq_instance_dialog_close_cleans_dict(self, qapp):
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, TrackEffectInstance,
        )
        w = TrackMixerWidget(track_id="T2", track_name="Track 2")
        chain = EffectChainParams(effects=(
            TrackEffectInstance(effect_type="strip_eq", params={},
                                enabled=True, order=0,
                                instance_id="eq-inst-1", panel="fx"),
        ))
        w.set_effect_chain(chain)
        emitted: list = []
        w.spectrum_metering_changed.connect(lambda on: emitted.append(bool(on)))

        inst = chain.effects[0]
        w._open_strip_eq_editor(inst)
        dlg = w._strip_eq_dialogs.get("eq-inst-1")
        assert dlg is not None and dlg.isVisible()
        assert emitted[-1] is True

        dlg.close()
        # 字典引用被清理（不留死 wrapper），门控重算为关
        assert "eq-inst-1" not in w._strip_eq_dialogs
        assert emitted[-1] is False


class TestPanelDeregisterRecompute:
    """strip 移除后频谱门控重算（与 LUFS 门控对称）。"""

    def test_deregister_track_recomputes_spectrum(self, qapp):
        panel = AudioMixerPanel()
        panel.register_track("T1", "Track 1", "A1")
        # 模拟"该 strip 的 EQ 对话框曾开启"的门控滞留态
        panel._spectrum_metering_active = True
        emitted: list = []
        panel.spectrum_metering_changed.connect(lambda on: emitted.append(bool(on)))

        panel.deregister_track("T1")
        assert panel._spectrum_metering_active is False
        assert emitted == [False], "strip 移除应触发频谱门控重算并关闭"


class TestCompressorSidechainTelemetry:
    """独立压缩器对话框检源表 + 遥测分发不中断兄弟表计。"""

    def test_set_sidechain_level_updates_meter(self, qapp):
        dlg = CompressorDialog("t1", "Track", "comp-1", None)
        dlg.set_sidechain_level(-6.0)
        assert dlg.m_sidechain._value_db == pytest.approx(-6.0)

    def test_telemetry_dispatch_survives_missing_optional_api(self, qapp):
        """一个缺可选 API 的对话框不应打断同帧 Reverb/Delay 表计更新。"""

        class _ReverbStub:
            def __init__(self) -> None:
                self.updated: list = []

            def update_output_meter(self, db: float) -> None:
                self.updated.append(db)

        class _BrokenComp:
            _instance_id = "comp-9"

            # 故意不提供 set_sidechain_level（修复前 CompressorDialog 即如此）

        w = TrackMixerWidget(track_id="T3", track_name="Track 3")
        reverb = _ReverbStub()
        w._compressor_dialogs = {"comp-9": _BrokenComp()}
        w._reverb_dialogs = {"rv-1": reverb}

        # 修复前：AttributeError 逃出（只 catch RuntimeError），Reverb 表收不到
        w.update_track_telemetry({
            "peak": [-18.0, -19.0],
            "sc": {"comp-9": -12.0},
        })
        assert reverb.updated == [-18.0], "Reverb 输出表应照常收到更新"

    def test_panel_dispatches_peak_only_payload(self, qapp):
        """面板必须派发纯 peak 载荷（无 gr/sc/spectrum）——Reverb/Delay/EQ
        输出表只吃 peak；旧门控会让无动态链的轨上这些表永久冻结。"""

        class _TelemetryStrip:
            def __init__(self) -> None:
                self.payloads: list = []

            # 35a4451d 起 update_per_track_levels 读取表模式决定
            # peak/rms/lufs 取值键——stub 不实现会 AttributeError。
            # None = 默认 peak 模式。
            @property
            def display_meter_mode(self):  # noqa: D401
                return None

            def update_audio_level(self, vals: list) -> None:
                pass

            def update_track_telemetry(self, payload: dict) -> None:
                self.payloads.append(payload)

        panel = AudioMixerPanel()
        strip = _TelemetryStrip()
        panel._track_mixers["T9"] = strip  # type: ignore[assignment]
        panel.update_per_track_levels({"T9": {
            "peak": [-12.0, -13.0], "rms": [-14.0, -15.0], "pre": -11.0,
        }})
        assert strip.payloads, "纯 peak 载荷也必须派发给 update_track_telemetry"
        assert strip.payloads[0]["pre"] == -11.0


class TestMixerAddUnits:
    """Mixer Add 菜单：Dynamics 单一整体 + Spatial Panner 入口。"""

    def test_add_dynamics_unit_shows_single_row(self, qapp):
        """添加 Dynamics → FX 列表出现一行 'Dynamics'，链内三实例。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        w = TrackMixerWidget(track_id="T4", track_name="Track 4")
        committed: list = []
        w.track_effects_committed.connect(
            lambda tid, chain: committed.append(chain))
        w._effect_add("dynamics")
        assert w._effect_chain is not None
        types = sorted(e.effect_type for e in w._effect_chain.effects)
        assert types == ["compressor", "expander", "limiter"]
        assert all(getattr(e, "panel", "fx") == "dynamics"
                   for e in w._effect_chain.effects)
        assert committed, "添加 Dynamics 应提交命令栈"
        # FX 列表可见反馈：单一 Dynamics 行
        labels = [w._effect_list.item(i).text()
                  for i in range(w._effect_list.count())]
        assert "Dynamics" in labels, f"FX 列表应显示 Dynamics 行: {labels}"
        assert len(labels) == 1, "三个实例只显示一行"

    def test_add_dynamics_twice_opens_panel_not_duplicate(self, qapp):
        """重复添加 Dynamics 不再叠加三实例（幂等）。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        w = TrackMixerWidget(track_id="T5", track_name="Track 5")
        w._effect_add("dynamics")
        n1 = len(w._effect_chain.effects)
        w._effect_add("dynamics")
        assert len(w._effect_chain.effects) == n1, "重复添加应幂等"

    def test_remove_dynamics_removes_all_three(self, qapp):
        """右键删除 Dynamics 行 → 三实例整体移除。"""
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        w = TrackMixerWidget(track_id="T6", track_name="Track 6")
        w._effect_add("dynamics")
        w._effect_remove_dynamics()
        assert w._effect_chain is None or not w._effect_chain.effects

    def test_spatial_panner_add_is_persistent(self, qapp):
        """Add 菜单的 Spatial Panner = 启用默认参数并提交命令栈（固定到轨），
        再打开面板；已启用时幂等不重复提交。⤢ 按钮高亮指示。"""
        from src.features.edit.core.timeline_v2 import SpatialPannerParams
        w = TrackMixerWidget(track_id="T10", track_name="Track 10")
        committed: list = []
        w.panner_committed.connect(lambda tid, payload: committed.append(payload))
        w._add_spatial_panner()
        assert committed and committed[0]["panner"].get("enabled") is True
        assert w._panner.get("enabled") is True
        assert "#28D8F0" in w._panner_expand_btn.styleSheet(), "⤢ 应高亮"
        n = len(committed)
        w._add_spatial_panner()
        assert len(committed) == n, "已启用时不应重复提交"


class TestCompositeDynamicsUnit:
    """clip 级 Dynamics 单实例复合（EG→Comp→Lim），属性面板不拆分。"""

    def test_dynamics_effect_compresses_and_telemetrizes(self) -> None:
        import numpy as np
        from src.features.edit.core.audio_dsp import EffectChain
        from src.features.edit.core.timeline_v2 import DynamicsParams

        p = DynamicsParams.default().to_dict()
        p["enabled"] = True
        p["comp"]["threshold_db"] = -20.0
        p["comp"]["ratio"] = 4.0
        p["lim"]["threshold_db"] = -6.0

        sr = 48000.0
        t = np.arange(9600) / sr
        sig = np.stack([0.8 * np.sin(2 * np.pi * 440.0 * t)] * 2).astype(np.float32)

        chain = EffectChain(sr)
        chain.set_params([("dynamics", p, True, "dyn-1")])
        out = chain.process(sig.copy())
        assert float(np.max(np.abs(out))) < 0.7, "复合单元应压缩+限幅"
        assert chain.collect_gain_reduction().get("dyn-1") is not None,             "复合单元应产出 GR 遥测"

    def test_dynamics_block_equivalence(self) -> None:
        import numpy as np
        from src.features.edit.core.audio_dsp import EffectChain
        from src.features.edit.core.timeline_v2 import DynamicsParams

        p = DynamicsParams.default().to_dict()
        p["enabled"] = True
        p["comp"]["threshold_db"] = -18.0
        sr = 48000.0
        rng = np.random.default_rng(7)
        sig = (rng.standard_normal((2, 9600)) * 0.5).astype(np.float32)

        full = EffectChain(sr); full.set_params([("dynamics", p, True, "d")])
        whole = full.process(sig.copy())
        split = EffectChain(sr); split.set_params([("dynamics", p, True, "d")])
        a = split.process(sig[:, :4800].copy())
        b = split.process(sig[:, 4800:].copy())
        merged = np.concatenate([a, b], axis=1)
        assert float(np.max(np.abs(whole[:, 100:] - merged[:, 100:]))) < 1e-3

    def test_clip_dynamics_dialog_roundtrip(self, qapp):
        """params→chain→params 往返一致；面板提交回传 DynamicsParams。"""
        from src.shared.ui.audio_mixer.dynamics_dialog import (
            ClipDynamicsDialog, _chain_to_dynamics_params, _dynamics_params_to_chain,
        )
        from src.features.edit.core.timeline_v2 import DynamicsParams

        dp = DynamicsParams.default()
        back = _chain_to_dynamics_params(_dynamics_params_to_chain(dp))
        assert back.to_dict() == dp.to_dict()

        dlg = ClipDynamicsDialog("clip1", "Clip A", "fx-9", dp, None)
        got: list = []
        dlg.dynamics_committed.connect(lambda tid, prm: got.append((tid, prm)))
        dlg.show()
        dlg._panel._on_drag_released()
        assert got and got[0][0] == "clip1" and isinstance(got[0][1], DynamicsParams)

    def test_clip_dynamics_registered_in_adapter(self, qapp) -> None:
        from src.features.edit.ui.clip_audio_effect_dialog_adapter import ClipAudioEffectDialogManager
        from src.features.mixer.effects.panels.dynamics import StudioDynamicsWidget

        manager = ClipAudioEffectDialogManager()
        dialog = manager.open_dialog("dynamics", "clip", "fx", {})
        assert isinstance(dialog.panel, StudioDynamicsWidget)
        manager.close_all()

    def test_spectrum_flows_from_mixer(self):
        """频谱端到端：开频谱门控 → mix_samples → collect_spectra 出 bins。"""
        from unittest.mock import Mock
        import numpy as np
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder

        sr = 48000
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=np.stack([sig, sig]))

        mixer = AudioMixer()
        mixer.set_sample_rate(float(sr))
        mixer.add_track("t1", dec)
        mixer.set_spectrum_metering(True)
        mixer.mix_samples(0, 24.0, n)
        spectra = mixer.collect_spectra()
        assert "t1" in spectra, "开频谱后 mix_samples 应推送频谱窗"
        freqs, dbs = spectra["t1"]
        assert len(freqs) == 96 and len(dbs) == 96
        # 440Hz 附近应有能量峰（对数 bins 覆盖 20Hz..20kHz）
        peak_i = int(np.argmax(dbs))
        peak_f = float(freqs[peak_i])
        assert 300.0 < peak_f < 650.0, f"440Hz 信号峰值应落在附近 bin: {peak_f:.1f}Hz"
