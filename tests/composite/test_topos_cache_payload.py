"""P0-7/P1-11 TOPOS-backed node disk-cache payload tests.

The Composite disk layer now routes payloads through the native TOPOS
.toos format when the native image codec is available, with a
diagnosable (logged-once) npz fallback per P1-11.  These tests pin:

- the format policy: auto prefers TOPOS when available, falls back to
  npz when it is not (this machine's shipped builds predate the image
  layer, so the fallback path is what runs here — and must work);
- HDR payloads never enter the 12-bit TOPOS range (lossless npz
  instead) — testable without the native library;
- round-trips stay lossless on the npz path and manifest-provenanced on
  both paths;
- the TOPOS round-trip itself is skip-guarded: it runs wherever a
  current native image codec is present (CI with a rebuilt lib, or a
  dev machine via TOPOS_CODEC_LIB).
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.render.disk_cache_store import DiskCacheStore
from src.features.composite.render import topos_cache_codec as tcc

pytestmark = pytest.mark.composite_core


@pytest.fixture()
def store(tmp_path):
    st = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
    yield st


def _payload(h=48, w=64, seed=3, hdr=False):
    rng = np.random.default_rng(seed)
    rgba = rng.random((h, w, 4)).astype(np.float32)
    rgba[..., 3] = np.clip(rgba[..., 3], 0.0, 1.0)
    if hdr:
        rgba[..., 0] *= 4.0  # push red beyond 1.0
    return rgba


class TestFormatPolicy:
    def test_auto_falls_back_to_npz_without_native_codec(
            self, store, monkeypatch):
        monkeypatch.setattr(
            tcc, "probe_topos_payload_codec",
            lambda: tcc.ToposPayloadInfo(available=False))
        rgba = _payload()
        assert store.store("k1", rgba) is True
        meta = store.load_metadata("k1")
        assert meta["format"] == "npz_f16"
        back = store.load("k1")
        assert back is not None and back.shape == rgba.shape
        np.testing.assert_allclose(back, rgba, rtol=0, atol=1e-3)

    def test_hdr_payload_uses_half_toos(self, store, monkeypatch):
        # G6：HDR 载荷走 HALF .toos（image_profile 4，f16 存储）——
        # 12-bit 固定域装不下的事实不变，但载体不再是未压缩 npz。
        monkeypatch.setattr(
            tcc, "probe_topos_payload_codec",
            lambda: tcc.ToposPayloadInfo(available=True, abi_version=1))
        assert tcc.payload_is_ldr(_payload(hdr=False)) is True
        assert tcc.payload_is_ldr(_payload(hdr=True)) is False
        rgba = _payload(hdr=True)
        store.store("hdr", rgba)
        meta = store.load_metadata("hdr")
        assert meta["format"] == tcc.FORMAT_ID_FLOAT
        # 读回精度 = f16 存储域（与 npz 回退逐位同精度）
        expected = rgba.astype(np.float16).astype(np.float32)
        np.testing.assert_allclose(store.load("hdr"), expected,
                                   rtol=0, atol=0)

    def test_explicit_npz_mode_is_honoured(self, store, monkeypatch):
        monkeypatch.setenv("TOPOS_NODE_CACHE_FORMAT", "npz")
        monkeypatch.setattr(
            tcc, "probe_topos_payload_codec",
            lambda: tcc.ToposPayloadInfo(available=True, abi_version=1))
        store.store("k2", _payload(seed=5))
        assert store.load_metadata("k2")["format"] == "npz_f16"

    def test_entries_and_eviction_see_both_payload_kinds(self, store,
                                                         monkeypatch):
        monkeypatch.setattr(
            tcc, "probe_topos_payload_codec",
            lambda: tcc.ToposPayloadInfo(available=False))
        store.store("e1", _payload(seed=1))
        store.store("e2", _payload(seed=2, hdr=True))
        entries = store.entries()
        assert set(entries) == {"e1", "e2"}
        store.delete("e1")
        assert store.has("e1") is False
        assert store.has("e2") is True


class TestToposRoundTrip:
    @pytest.fixture()
    def _native(self):
        if not tcc.probe_topos_payload_codec().available:
            pytest.skip("native TOPOS image codec not available in this "
                        "environment (P1-11 fallback is exercised instead)")
        import tempfile
        return tempfile.mkdtemp(prefix="topos_cache_rt_")

    def test_round_trip_within_12bit_quantization(self, _native):
        rgba = _payload(64, 96, seed=11)
        path = os.path.join(_native, "entry.toos")
        manifest = tcc.encode_toos_payload(path, rgba)
        # P0-7 manifest provenance.
        for field in ("format", "width", "height", "codec", "profile",
                      "pix_layout", "bit_depth", "alpha_bit_depth",
                      "alpha_mode"):
            assert field in manifest, field
        assert manifest["format"] == tcc.FORMAT_ID
        assert manifest["alpha_mode"] == "straight"
        back = tcc.decode_toos_payload(path, 96, 64)
        d = np.abs(back - rgba)
        # 12-bit quantization: ≤ 1/4095 per channel, in practice ~half.
        assert float(d.max()) <= 1.0 / 4095.0 + 1e-4
        assert float(d.mean()) <= 1.0 / 4095.0 / 4.0

    def test_hdr_rejected_before_encode(self, _native):
        path = os.path.join(_native, "hdr.toos")
        with pytest.raises(ValueError):
            tcc.encode_toos_payload(path, _payload(hdr=True))
        assert not os.path.exists(path)


class TestHalfPayloadRoundtrip:
    """G6：HDR/超界载荷的 HALF .toos 载体（image_profile 4）——f16 存储
    精度与 npz 回退逐位一致，体积经 rans2 压缩显著下降。"""

    def test_hdr_payload_half_roundtrip(self, tmp_path) -> None:
        rng = np.random.default_rng(3)
        h, w = 24, 32
        rgba = np.zeros((h, w, 4), np.float32)
        rgba[..., 0] = -2.0 + 6.0 * np.arange(w)[None, :] / w
        rgba[..., 1] = rng.random((h, w)) * 0.2
        rgba[..., 2] = 0.05 * np.sin(np.arange(w)[None, :] / 5.0) + 1.5
        rgba[0, 0, 2] = 12.0  # HDR 高光
        rgba[..., 3] = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :].repeat(h, 0)

        out = tmp_path / "hdr.toos"
        manifest = tcc.encode_toos_payload_float(str(out), rgba)
        assert manifest["format"] == tcc.FORMAT_ID_FLOAT
        # AUD-8505: manifest 键 file_bytes 已与消费方统一为 "bytes"
        assert out.is_file() and manifest["bytes"] > 0

        decoded = tcc.decode_toos_payload(str(out), w, h)
        assert decoded.shape == (h, w, 4)
        # f16 存储域逐位往返（crf=20 = M1 实测 HDR 无损窗口中点）——
        # 与 npz f16 回退精度完全一致
        expected = rgba.astype(np.float16).astype(np.float32)
        np.testing.assert_array_equal(decoded, expected)

    def test_hdr_payload_smaller_than_raw(self, tmp_path) -> None:
        # 平滑载荷（真实节点缓存内容形态；随机噪声本就不可压）
        yy, xx = np.mgrid[0:48, 0:96]
        rgba = np.zeros((48, 96, 4), np.float32)
        rgba[..., 0] = -1.5 + 4.0 * xx / 96
        rgba[..., 1] = 0.3 * np.sin(xx / 7.0) + 2.0
        rgba[..., 2] = yy / 48.0 * 0.5
        rgba[..., 3] = 0.8
        out = tmp_path / "hdr2.toos"
        manifest = tcc.encode_toos_payload_float(str(out), rgba)
        assert manifest["bytes"] < rgba.nbytes * 0.6
        assert manifest["bytes"] < rgba.astype(np.float16).nbytes
