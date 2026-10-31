"""Provider parity tests: OCIO vs Builtin must agree on the same descriptor.

The Rec.709 contract (P1 fix): for a given ColorSpaceDescriptor, the OCIO
provider and the Builtin provider MUST produce the same output to within a
tight tolerance. If they don't, CPU export (provider-driven) and GPU preview
(shader-driven, which mirrors the builtin math) will visibly disagree on the
same Rec.709 video — the user sees a midtone shift when switching color engine
or between preview and export.

These tests guard that contract across sRGB, Rec.709, PQ, and HLG at a spread
of sample points (dark/mid/bright, around the OETF knees).
"""
import numpy as np
import pytest

from src.shared.color.color_transform_provider import (
    BuiltinColorProvider,
    OCIOColorProvider,
    get_color_provider_diagnostics,
    reset_provider_cache,
)
from src.shared.color.color_space_descriptor import (
    ColorSpaceDescriptor, LINEAR_SCENE,
)
from src.shared.color.types import ColorPrimaries, ColorTransfer, ColorRange


# Sample points spanning the transfer curves (incl. OETF knees at 0.018/0.081).
_SAMPLES = [0.0, 0.018, 0.081, 0.18, 0.3, 0.5, 0.7, 0.9, 1.0]


def _sample_img(values=_SAMPLES) -> np.ndarray:
    """Build a (1, N, 4) float32 ramp image from the sample values."""
    return np.array([[list(zip(values, values, values, [1.0] * len(values)))]],
                    dtype=np.float32).reshape(1, len(values), 4)


@pytest.fixture
def providers():
    """Return (ocio, builtin) providers, skipping if OCIO unavailable."""
    reset_provider_cache()
    ocio = OCIOColorProvider()
    if not ocio.is_available():
        pytest.skip("OCIO not available in this environment")
    return ocio, BuiltinColorProvider()


def _descriptor(transfer, primaries=ColorPrimaries.BT709,
                range_=ColorRange.FULL) -> ColorSpaceDescriptor:
    return ColorSpaceDescriptor(primaries=primaries, transfer=transfer,
                                range=range_)


@pytest.mark.parametrize("transfer, primaries, mode, tol", [
    # Rec.709 is the primary contract: tight tolerance, midtone-critical.
    # SDR curves use ABSOLUTE tolerance (relative blows up near black).
    (ColorTransfer.BT709, ColorPrimaries.BT709, "abs", 1e-3),
    (ColorTransfer.SRGB, ColorPrimaries.BT709, "abs", 1e-3),
    # PQ/HLG (P0-COLOR-05): the bundled minimal.ocio now carries the ×100
    # nits scale (ST-2084) and ×(1/3) scene-light scale (HLG), aligning OCIO
    # with the builtin/GPU absolute-nits contract. RELATIVE tolerance —
    # PQ linear values span 0..10000 nits so absolute diff is meaningless.
    (ColorTransfer.PQ, ColorPrimaries.BT2020, "rel", 5e-4),
    (ColorTransfer.HLG, ColorPrimaries.BT2020, "rel", 5e-4),
])
def test_decode_to_linear_parity(providers, transfer, primaries, mode, tol):
    """OCIO and builtin must decode each transfer -> linear identically.

    P0-COLOR-05 收口：PQ/HLG 的 OCIO-vs-builtin 尺度分歧已在 bundled
    config 中修正（×100 / ×1/3 scale matrices），四个传输函数全部进入
    同一 parity 契约。
    """
    ocio, builtin = providers
    src = _descriptor(transfer, primaries=primaries, range_=ColorRange.FULL)
    img = _sample_img()

    oc_out = ocio.transform(img.copy(), src, LINEAR_SCENE)
    bi_out = builtin.transform(img.copy(), src, LINEAR_SCENE)

    diff = np.abs(oc_out[..., :3] - bi_out[..., :3])
    err = float(np.max(diff / np.maximum(np.abs(bi_out[..., :3]), 1e-6)
                       if mode == "rel" else diff))
    assert err < tol, (
        f"{transfer.value}: OCIO vs builtin {mode} diff {err:.2e} >= {tol:.0e}"
    )


def test_dci_p3_pq_decodes_through_p3_gamut(providers):
    """P0-COLOR-05：DCI-P3+PQ 必须经 p3_pq 空间（P3 gamut）解码。

    旧映射把 (smpte431, smpte2084) 路由到 bt2020_pq，P3 内容被 BT.2020
    gamut 解码——在高饱和 P3 样本上产生数千 nits 的色偏。修复后 OCIO 与
    builtin（P3D65 gamut）一致。
    """
    ocio, builtin = providers
    dci = _descriptor(ColorTransfer.PQ, primaries=ColorPrimaries.P3_DCI,
                      range_=ColorRange.LIMITED)
    assert dci.to_ocio_name() == "p3_pq"

    # 高饱和 P3 样本（红/绿/蓝原色 + 混合色），limited-range 编码侧
    samples = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.9, 0.1, 0.2], [0.2, 0.9, 0.3]]
    img = np.array([[s + [1.0] for s in samples]], dtype=np.float32)

    oc_out = ocio.transform(img.copy(), dci, LINEAR_SCENE)
    bi_out = builtin.transform(img.copy(), dci, LINEAR_SCENE)
    rel = np.abs(oc_out[..., :3] - bi_out[..., :3]) / np.maximum(
        np.abs(bi_out[..., :3]), 1e-6)
    assert float(np.max(rel)) < 5e-4, (
        f"DCI-P3+PQ: OCIO vs builtin max rel {float(np.max(rel)):.2e}"
    )

    # 而错误 gamut（bt2020_pq）会产生显著色偏——量化旧 bug 的严重性
    bt = _descriptor(ColorTransfer.PQ, primaries=ColorPrimaries.BT2020,
                     range_=ColorRange.LIMITED)
    wrong = ocio.transform(img.copy(), bt, LINEAR_SCENE)
    shift = float(np.max(np.abs(oc_out[..., :3] - wrong[..., :3])))
    assert shift > 100.0, (
        "错误 gamut 应产生显著 nits 色偏（否则本测试无法区分映射错误）"
    )


def test_rec709_specific_canonical_values(providers):
    """Rec.709 OETF inverse at the canonical sample points (the contract).

    At code 0.5 the BT.709 OETF inverse is ((0.5+0.099)/1.099)^(1/0.45) ≈
    0.2596 (before any range expansion). Both providers must land here — this
    is the value the GPU shader and builtin already produce, so OCIO matching
    it means all three engines agree.
    """
    ocio, builtin = providers
    src = _descriptor(ColorTransfer.BT709, range_=ColorRange.FULL)
    for code, expected in [(0.0, 0.0), (0.5, 0.2596), (1.0, 1.0)]:
        img = np.array([[[code, code, code, 1.0]]], dtype=np.float32)
        oc = float(ocio.transform(img.copy(), src, LINEAR_SCENE)[0, 0, 0])
        bi = float(builtin.transform(img.copy(), src, LINEAR_SCENE)[0, 0, 0])
        assert abs(oc - expected) < 2e-3, f"OCIO rec709 {code}->{oc} != {expected}"
        assert abs(bi - expected) < 2e-3, f"builtin rec709 {code}->{bi} != {expected}"


def test_roundtrip_rec709_stable(providers):
    """rec709 -> linear -> rec709 must round-trip on both providers."""
    ocio, builtin = providers
    enc = _descriptor(ColorTransfer.BT709, range_=ColorRange.FULL)
    img = _sample_img()

    for provider, name in [(ocio, "ocio"), (builtin, "builtin")]:
        decoded = provider.transform(img.copy(), enc, LINEAR_SCENE)
        reencoded = provider.transform(decoded, LINEAR_SCENE, enc)
        max_diff = float(np.max(np.abs(reencoded[..., :3] - img[..., :3])))
        assert max_diff < 1e-3, f"{name} rec709 round-trip drift {max_diff:.2e}"


# ── P0-COLOR-05：能力诊断（fallback 不静默）─────────────────────────────

class TestProviderDiagnostics:
    """get_color_provider_diagnostics 必须结构化解释 provider 选择。"""

    def test_ocio_active_reports_no_fallback(self):
        """OCIO 可用时：active=ocio、config_loaded=True、无降级原因。"""
        reset_provider_cache()
        diag = get_color_provider_diagnostics()
        if not diag["ocio_importable"]:
            pytest.skip("OCIO not available in this environment")
        assert diag["requested_engine"] == "ocio"
        assert diag["active_provider"] == "ocio"
        assert diag["ocio_config_loaded"] is True
        assert diag["fallback_reason"] == ""

    def test_missing_ocio_reports_reason(self, monkeypatch):
        """模拟 PyOpenColorIO 缺失：builtin 生效且 fallback_reason 说明原因。"""
        import src.shared.color.color_transform_provider as ctp
        monkeypatch.setattr(ctp, "_OCIO_IMPORTABLE", False)
        monkeypatch.setattr(
            "src.shared.color.color_transform_provider._OCIO_IMPORTABLE", False
        )
        ctp.reset_provider_cache()
        diag = ctp.get_color_provider_diagnostics()
        assert diag["active_provider"] == "builtin"
        assert diag["ocio_importable"] is False
        assert diag["fallback_reason"], "降级必须给出原因，不允许静默"
        assert "builtin" in diag["fallback_reason"]
        ctp.reset_provider_cache()

    def test_bad_config_reports_reason(self, monkeypatch):
        """模拟 config 路径无效：builtin 生效且 fallback_reason 说明原因。"""
        import src.shared.color.color_transform_provider as ctp
        ctp.reset_provider_cache()

        class _Cfg:
            def get(self, key, default=None):
                if key == "composite.color_engine":
                    return "ocio"
                if key == "ocio_config_path":
                    return "/nonexistent/does-not-exist.ocio"
                return default

        diag = ctp.get_color_provider_diagnostics(_Cfg())
        if not diag["ocio_importable"]:
            pytest.skip("OCIO not available in this environment")
        assert diag["active_provider"] == "builtin"
        assert diag["ocio_config_loaded"] is False
        assert diag["fallback_reason"], "config 加载失败必须说明原因"
        assert "builtin" in diag["fallback_reason"]
        ctp.reset_provider_cache()
