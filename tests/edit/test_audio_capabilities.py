"""
Audio backend capabilities detection 单元测试（AM-01 阶段 0.1）。

验证 `audio_capabilities` 模块作为 DSP 后端可用性单一真相源的正确性，
以及 DSP 层（StripEQFilter / LoudnessMeter）正确收口到该模块后的降级行为：
- scipy 可用 → EQ/LUFS 正常生效
- scipy 不可用（模拟）→ EQ/LUFS 标记后端不可用，UI 能据此区分"静音"与"降级"

不依赖真实声卡，全部在内存中验证。

注意：DSP 层运行时查询 capabilities（非加载时缓存），因此测试只需 monkeypatch
capabilities 探测函数即可让 EQ/LUFS 进入降级态，无需 reload DSP 模块
（reload 会污染 sys.modules 导致类身份断裂，破坏其他测试隔离）。
"""

from __future__ import annotations

import numpy as np
import pytest


def test_capability_result_defaults():
    """CapabilityResult 默认不可用且无原因。"""
    from src.features.edit.core.audio_capabilities import CapabilityResult

    r = CapabilityResult(False)
    assert r.available is False
    assert r.reason is None
    r2 = CapabilityResult(True, None)
    assert r2.available is True


def test_audio_capabilities_aggregate_flags():
    """AudioCapabilities 聚合标志正确计算。"""
    from src.features.edit.core.audio_capabilities import (
        AudioCapabilities,
        CapabilityResult,
    )

    # 全可用
    ok = AudioCapabilities(
        numpy=CapabilityResult(True),
        pyav=CapabilityResult(True),
        scipy=CapabilityResult(True),
        output_device=CapabilityResult(True),
    )
    assert ok.eq_lufs_available is True
    assert ok.all_critical_available is True
    assert ok.disabled_components() == []

    # scipy 缺失：EQ/LUFS 降级，但 numpy+pyav 仍在 → critical 不全
    degraded = AudioCapabilities(
        numpy=CapabilityResult(True),
        pyav=CapabilityResult(True),
        scipy=CapabilityResult(False, "scipy.signal 不可用"),
        output_device=CapabilityResult(True),
    )
    assert degraded.eq_lufs_available is False
    assert degraded.all_critical_available is False
    disabled = degraded.disabled_components()
    assert any("SciPy" in d for d in disabled)
    assert not any("numpy" in d for d in disabled)


def test_disabled_components_order_stable():
    """disabled_components 顺序固定（numpy→pyav→scipy→device）以便可测试。"""
    from src.features.edit.core.audio_capabilities import (
        AudioCapabilities,
        CapabilityResult,
    )

    all_bad = AudioCapabilities(
        numpy=CapabilityResult(False, "x"),
        pyav=CapabilityResult(False, "x"),
        scipy=CapabilityResult(False, "x"),
        output_device=CapabilityResult(False, "x"),
    )
    comps = all_bad.disabled_components()
    assert "numpy" in comps[0]
    assert "PyAV" in comps[1]
    assert "SciPy" in comps[2]
    assert "Audio Output Device" in comps[3]


def test_probe_detects_installed_backends():
    """在当前 venv（scipy/pyav/numpy 已安装）下探测应返回可用。"""
    from src.features.edit.core.audio_capabilities import probe_audio_capabilities

    caps = probe_audio_capabilities(check_output_device=False)
    assert caps.numpy.available is True
    assert caps.scipy.available is True
    assert caps.pyav.available is True
    # 不检测设备时 output_device 为 unknown 三态（AUD-15203：未知 ≠ 可用，
    # 也不计入 disabled_components——那意味着"不可用"）
    assert caps.output_device.available is False
    assert caps.output_device.unknown is True
    assert caps.output_device.reason is not None
    assert "Audio Output Device" not in caps.disabled_components()


def test_probe_scipy_uses_signal_subpath():
    """scipy 探测走 scipy.signal.lfilter（与 DSP 实际依赖点一致）。"""
    from src.features.edit.core.audio_capabilities import _probe_scipy

    r = _probe_scipy()
    assert r.available is True
    assert r.reason is None


def test_get_capabilities_caches_and_resets():
    """get_audio_capabilities 惰性缓存，reset 后重新探测。"""
    from src.features.edit.core import audio_capabilities as mod

    mod.reset_audio_capabilities_cache()
    c1 = mod.get_audio_capabilities(check_output_device=False)
    c2 = mod.get_audio_capabilities()
    # 同一缓存实例（frozen dataclass，不可变，可安全共享引用）
    assert c1 is c2

    mod.reset_audio_capabilities_cache()
    c3 = mod.get_audio_capabilities(check_output_device=False)
    # reset 后是新探测（内容相同但实例不同）
    assert c3.eq_lufs_available == c1.eq_lufs_available


# ─────────────────────────────────────────────────────────────────────────────
# DSP 层收口验证：模拟 scipy 不可用，确认 EQ/LUFS 报告后端降级
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def scipy_unavailable(monkeypatch):
    """模拟 scipy 不可用：让 capabilities 探测返回 False。

    DSP 层（StripEQFilter/LoudnessMeter）运行时查询 capabilities，
    monkeypatch 后立刻生效，无需 reload 模块（保持测试隔离）。
    """
    from src.features.edit.core import audio_capabilities as mod

    monkeypatch.setattr(mod, "_probe_scipy", lambda: mod.CapabilityResult(False, "simulated"))
    mod.reset_audio_capabilities_cache()
    yield
    # 恢复真实探测（monkeypatch 自动 undo，但需清缓存让下次重新探测）
    mod.reset_audio_capabilities_cache()


def test_strip_eq_reports_backend_unavailable(scipy_unavailable):
    """scipy 不可用时，StripEQFilter.is_backend_available 为 False、is_active 为 False。"""
    from src.features.edit.core.audio_dsp import StripEQFilter
    from src.features.edit.core.timeline_v2 import StripEQParams

    eq = StripEQFilter(sample_rate=48000.0)
    params = StripEQParams(
        enabled=True, mode="3-band",
        bands=(6.0, 0.0, -3.0), gain=0.0,
        low_cut_enable=False, low_cut=20.0,
        high_cut_enable=False, high_cut=20000.0,
    )
    eq.set_params(params)

    assert eq.is_backend_available is False
    # 后端不可用 → 即使 params.enabled，is_active 也为 False（静默旁路）
    assert eq.is_active is False
    # process 应原样返回（旁路）
    samples = np.zeros((2, 256), dtype=np.float32)
    out = eq.process(samples)
    assert np.array_equal(out, samples)


def test_loudness_meter_reports_backend_unavailable(scipy_unavailable):
    """scipy 不可用时，LoudnessMeter.is_backend_available 为 False，读数恒为 -70。"""
    from src.features.edit.core.loudness_meter import LoudnessMeter

    lm = LoudnessMeter(sample_rate=48000.0, channels=2)
    assert lm.is_backend_available is False

    # 喂入满幅信号，LUFS 仍应保持 -70（后端旁路）
    loud = np.ones((2, 4800), dtype=np.float32) * 0.5
    lm.process(loud)
    assert lm.momentary_lufs == -70.0
    assert lm.integrated_lufs == -70.0


def test_strip_eq_active_when_backend_available():
    """默认环境（scipy 可用）下 EQ 正常生效。"""
    from src.features.edit.core.audio_dsp import StripEQFilter
    from src.features.edit.core.timeline_v2 import StripEQParams

    eq = StripEQFilter(sample_rate=48000.0)
    assert eq.is_backend_available is True
    # 无参数时不 active
    assert eq.is_active is False

    params = StripEQParams(
        enabled=True, mode="3-band",
        bands=[6.0, 0.0, 0.0], gain=0.0,
        low_cut_enable=False, low_cut=20.0,
        high_cut_enable=False, high_cut=20000.0,
    )
    eq.set_params(params)
    assert eq.is_active is True
    assert eq.is_backend_available is True


def test_loudness_meter_active_when_backend_available():
    """默认环境下 LoudnessMeter 正常测量。"""
    from src.features.edit.core.loudness_meter import LoudnessMeter

    lm = LoudnessMeter(sample_rate=48000.0, channels=2)
    assert lm.is_backend_available is True

    # 1kHz 正弦 -23dBFS（振幅 0.0708）→ 应接近 -23 LUFS（校准点）
    t = np.arange(48000, dtype=np.float64) / 48000.0
    tone = 0.0708 * np.sin(2 * np.pi * 1000.0 * t)
    stereo = np.stack([tone, tone], axis=0).astype(np.float32)
    lm.process(stereo)
    # 校准点附近（容差较宽，仅验证后端在工作）
    assert lm.momentary_lufs > -40.0
