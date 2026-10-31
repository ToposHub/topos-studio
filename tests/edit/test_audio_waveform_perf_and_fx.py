"""Waveform generation speed, gain-linked display and clip-FX waveform tests.

覆盖三块新能力：
- _PeakAggregator：向量化块聚合与旧逐帧 ufunc.at 算法逐位一致
- 磁盘缓存：round-trip 数据一致 + 失效键（mtime/size 变化）
- ClipEffectWaveformWorker：离线效果渲染（压缩器峰值下降 / 区间对齐 / 失败路径）
- ClipItem.set_waveform_gain：纯视觉缩放（不失效 path 缓存）+ 效果指纹
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication, QGraphicsScene

from src.features.edit.core.audio_waveform import (
    MAX_WAVEFORM_SAMPLES,
    ClipEffectWaveformRenderer,
    ClipEffectWaveformWorker,
    WaveformData,
    WaveformExtractorWorker,
    _PeakAggregator,
    _waveform_disk_cache_dir,
    load_waveform_from_disk,
    save_waveform_to_disk,
)

pytestmark = pytest.mark.edit_core

WAV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "test_audio", "wav")


def _first_wav() -> str | None:
    if not os.path.isdir(WAV_DIR):
        return None
    for name in sorted(os.listdir(WAV_DIR)):
        if name.endswith(".wav"):
            return os.path.join(WAV_DIR, name)
    return None


def _reference_peaks(blocks: list[np.ndarray], spb: int,
                     capacity: int = MAX_WAVEFORM_SAMPLES):
    """旧实现：逐块 np.minimum.at / np.maximum.at（作为对照真值）。"""
    ch = blocks[0].shape[0]
    pmin = np.zeros((ch, capacity), np.float32)
    pmax = np.zeros((ch, capacity), np.float32)
    seen = 0
    for b in blocks:
        n = b.shape[1]
        pos = seen + np.arange(n, dtype=np.int64)
        bk = pos // spb
        valid = bk < capacity
        if np.any(valid):
            vb, vs = bk[valid], b[:, valid]
            for c in range(ch):
                np.minimum.at(pmin[c], vb, vs[c])
                np.maximum.at(pmax[c], vb, np.abs(vs[c]))
        seen += n
    count = int(np.ceil(seen / spb))
    return pmin[:, :count], pmax[:, :count]


# ================================================================
# _PeakAggregator - vectorized block aggregation
# ================================================================

class TestPeakAggregator:

    @pytest.mark.parametrize("channels,frame_sizes,n_frames,seed", [
        (2, "random", 200, 1),
        (1, "random", 150, 2),
        (2, [30000], 5, 3),       # 跨 64K flush 边界
        (1, [65535], 3, 4),
        (1, [65537], 7, 5),
        (2, [1], 50, 6),          # 极小块
        (1, [1000], 10, 7),       # 尾部不满 bucket
    ])
    def test_matches_legacy_scatter_aggregation(
            self, channels, frame_sizes, n_frames, seed) -> None:
        rng = np.random.default_rng(seed)
        blocks = []
        for _ in range(n_frames):
            size = (int(rng.integers(1, 5000)) if frame_sizes == "random"
                    else frame_sizes[0])
            blocks.append(
                (rng.standard_normal((channels, size)).astype(np.float32) * 0.95))

        agg = _PeakAggregator(channels=channels, estimated_duration_seconds=0.0)
        for b in blocks:
            agg.add(b)
        result = agg.finish()
        assert result is not None
        got_min = result[0].reshape(-1, channels).T
        got_max = result[1].reshape(-1, channels).T

        ref_min, ref_max = _reference_peaks(blocks, agg._spb)
        assert got_min.shape[1] == ref_min.shape[1]
        # 逐位一致（min/max 都是确定性的精确聚合）
        np.testing.assert_array_equal(got_min, ref_min)
        np.testing.assert_array_equal(got_max, ref_max)

    def test_all_negative_block_keeps_abs_peak(self) -> None:
        """全负样本段的峰值幅值必须保留（reduceat 修正回归）。"""
        block = np.full((1, 100), -0.9, dtype=np.float32)
        agg = _PeakAggregator(channels=1, estimated_duration_seconds=0.0)
        agg.add(block)
        result = agg.finish()
        assert result is not None
        peaks_max = result[1]
        assert peaks_max.max() == pytest.approx(0.9, abs=1e-6)
        assert result[0].min() == pytest.approx(-0.9, abs=1e-6)

    def test_bucket_span_grow_preserves_data(self) -> None:
        """grow（spb 翻倍）后继续添加的样本折入正确的新 bucket。"""
        rng = np.random.default_rng(11)
        agg = _PeakAggregator(channels=1, estimated_duration_seconds=0.0)
        spb = agg._spb
        pre = (rng.random((1, spb * 10)).astype(np.float32) - 0.5)
        agg.add(pre)
        agg._flush()
        agg._grow()
        assert agg._spb == spb * 2
        # 样本 [10*spb, 11*spb) → 旧 bucket 10 → 新 bucket 5
        post = np.full((1, spb), -0.9, dtype=np.float32)
        agg.add(post)
        result = agg.finish()
        assert result is not None
        got_min = result[0].reshape(-1, 1).T
        got_max = result[1].reshape(-1, 1).T
        assert got_min[0, 5] == pytest.approx(-0.9, abs=1e-6)
        assert got_max[0, 5] == pytest.approx(0.9, abs=1e-6)

    def test_finish_empty_returns_none(self) -> None:
        agg = _PeakAggregator(channels=2, estimated_duration_seconds=0.0)
        assert agg.finish() is None


# ================================================================
# Disk cache
# ================================================================

def _patch_cache_dir(tmp_path, monkeypatch):
    """Point the disk cache at an isolated per-test directory."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "src.features.edit.core.audio_waveform._waveform_disk_cache_dir",
        lambda: str(cache_dir))
    return cache_dir


class TestDiskCache:

    def _waveform(self, asset_id: str = "disk-1") -> WaveformData:
        rng = np.random.default_rng(21)
        peaks_min = -rng.random(512).astype(np.float32)
        peaks_max = rng.random(512).astype(np.float32)
        return WaveformData(
            asset_id=asset_id,
            peaks_min=peaks_min,
            peaks_max=peaks_max,
            samples_per_second=2048.0,
            duration_seconds=0.25,
            channels=1,
        )

    def test_round_trip(self, tmp_path, monkeypatch) -> None:
        source = tmp_path / "a.wav"
        source.write_bytes(b"fake-audio")
        _patch_cache_dir(tmp_path, monkeypatch)
        original = self._waveform()
        save_waveform_to_disk(str(source), original)

        loaded = load_waveform_from_disk("disk-1", str(source))
        assert loaded is not None
        np.testing.assert_array_equal(loaded.peaks_min, original.peaks_min)
        np.testing.assert_array_equal(loaded.peaks_max, original.peaks_max)
        assert loaded.samples_per_second == original.samples_per_second
        assert loaded.duration_seconds == original.duration_seconds
        assert loaded.channels == original.channels
        assert loaded.is_complete

    def test_invalidation_on_file_change(self, tmp_path, monkeypatch) -> None:
        source = tmp_path / "a.wav"
        source.write_bytes(b"v1")
        _patch_cache_dir(tmp_path, monkeypatch)
        save_waveform_to_disk(str(source), self._waveform())
        # 同内容重读命中
        assert load_waveform_from_disk("x", str(source)) is not None
        # 文件内容（mtime/size）变化后必须 miss
        time.sleep(0.01)
        source.write_bytes(b"v2-with-different-size")
        assert load_waveform_from_disk("x", str(source)) is None

    def test_disabled_by_env(self, tmp_path, monkeypatch) -> None:
        source = tmp_path / "a.wav"
        source.write_bytes(b"fake")
        monkeypatch.setenv("TOPOS_WAVEFORM_DISK_CACHE", "0")
        assert _waveform_disk_cache_dir() is None
        # 保存/加载都是 no-op，不抛错
        save_waveform_to_disk(str(source), self._waveform())
        assert load_waveform_from_disk("x", str(source)) is None


# ================================================================
# Clip-effect waveform rendering
# ================================================================

class TestClipEffectWaveformWorker:

    def test_compressor_lowers_peaks(self, qapp: QApplication) -> None:
        wav = _first_wav()
        if wav is None:
            pytest.skip("test WAV assets unavailable")
        try:
            import av  # noqa: F401
        except ImportError:
            pytest.skip("PyAV unavailable")

        entries = [("compressor",
                    {"threshold_db": -30.0, "ratio": 8.0, "attack_ms": 5.0,
                     "release_ms": 100.0, "enabled": True}, True)]
        results: dict = {}
        worker = ClipEffectWaveformWorker("clip-1", wav, 0.5, 2.0, entries)
        worker.effect_waveform_ready.connect(
            lambda cid, d: results.__setitem__("ok", d))
        worker.effect_waveform_failed.connect(
            lambda cid, e: results.__setitem__("err", e))
        worker.start()
        deadline = time.monotonic() + 15.0
        while not results and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        worker.wait(5000)

        assert "err" not in results, results.get("err")
        data = results.get("ok")
        assert data is not None
        assert data.is_complete
        assert data.channels >= 1
        # ~2s @2048 bucket/s
        expected = int(2.0 * 2048)
        assert abs(data.num_samples // data.channels - expected) < expected * 0.2
        # 强压缩（-30dB/8:1）后的显示包络必须明显低于原素材
        assert data.peaks_max.mean() < 0.5

    def test_missing_file_fails_gracefully(self, qapp: QApplication) -> None:
        results: dict = {}
        worker = ClipEffectWaveformWorker(
            "clip-x", "/nonexistent/audio.wav", 0.0, 1.0, [])
        worker.effect_waveform_failed.connect(
            lambda cid, e: results.__setitem__("err", e))
        worker.start()
        deadline = time.monotonic() + 5.0
        while "err" not in results and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        worker.wait(5000)
        assert "err" in results

    def test_renderer_queues_and_serializes(self, qapp: QApplication) -> None:
        """管理器串行派发，播放期间只排队不启动。"""
        renderer = ClipEffectWaveformRenderer()
        renderer.set_playback_active(True)
        renderer.request_effect_waveform("c1", "/x.wav", 0.0, 1.0, [])
        renderer.request_effect_waveform("c2", "/y.wav", 0.0, 1.0, [])
        assert list(renderer._queued.keys()) == ["c1", "c2"]
        # 播放期间不启动 worker
        assert renderer._worker is None
        renderer.set_playback_active(False)
        # 播放结束后才启动（worker 会因文件不存在而失败，不影响断言）
        if renderer._worker is not None:
            renderer._worker.wait(5000)
        renderer._queued.clear()


# ================================================================
# ClipItem gain-linked display + effect fingerprint
# ================================================================

def _make_clip_item(qapp: QApplication, effects: tuple = ()):
    from src.features.edit.ui.timeline_items import ClipItem
    item = ClipItem(
        "clip-g1", "track-1", "a.wav",
        0, 100, 10.0, 0,
        source_start=0, source_duration=100,
        media_type="audio",
        asset_id="asset-g1",
        asset_path="/tmp/a.wav",
        effects=effects,
    )
    # scene 需要与 item 同生命周期，否则临时 scene 被 GC 时会删除 item
    scene = QGraphicsScene()
    scene.addItem(item)
    item._test_scene_ref = scene
    return item


class TestClipItemGainAndFx:

    def test_set_waveform_gain_is_visual_only(self, qapp: QApplication) -> None:
        item = _make_clip_item(qapp)
        item._waveform_path_cache = ["dummy-path"]
        item._waveform_path_cache_key = "k"
        item.set_waveform_gain(1.5)
        assert item._waveform_gain == 1.5
        # path 缓存保持有效（等比缩放在绘制期完成）
        assert item._waveform_path_cache == ["dummy-path"]
        assert item._waveform_path_cache_key == "k"
        item.set_waveform_gain(99.0)   # 越界钳制
        assert item._waveform_gain == 2.0
        item.set_waveform_gain(-1.0)
        assert item._waveform_gain == 0.0
        # 同值不触发 update（幂等）
        item.set_waveform_gain(0.0)

    def test_effect_fingerprint_stability_and_bypass(self, qapp: QApplication) -> None:
        fx_on = (SimpleNamespace(effect_type="compressor",
                                 params={"threshold_db": -20.0, "enabled": True},
                                 order=0, is_enabled=True),)
        item = _make_clip_item(qapp, effects=fx_on)
        fp1 = item._clip_effect_fingerprint()
        assert fp1
        # 参数不同 → 指纹不同
        fx_tweak = (SimpleNamespace(effect_type="compressor",
                                    params={"threshold_db": -10.0, "enabled": True},
                                    order=0, is_enabled=True),)
        item._effects = fx_tweak
        assert item._clip_effect_fingerprint() != fp1
        # bypass（enabled=False）不参与波形 → 空指纹
        fx_bypass = (SimpleNamespace(effect_type="compressor",
                                     params={"threshold_db": -20.0, "enabled": True},
                                     order=0, is_enabled=False),)
        item._effects = fx_bypass
        assert item._clip_effect_fingerprint() == ""
        # 相同参数 → 相同指纹（排序稳定）
        item._effects = fx_on
        assert item._clip_effect_fingerprint() == fp1

    def test_video_effects_ignored(self, qapp: QApplication) -> None:
        fx = (SimpleNamespace(effect_type="gaussian_blur",
                              params={"radius": 2.0, "enabled": True},
                              order=0, is_enabled=True),)
        item = _make_clip_item(qapp, effects=fx)
        assert item._clip_effect_entries() == []
        assert item._clip_effect_fingerprint() == ""

    def test_dispose_disconnects_renderer(self, qapp: QApplication) -> None:
        item = _make_clip_item(qapp)
        renderer = ClipEffectWaveformRenderer()
        item._effect_waveform_signal_connected = True
        item._effect_waveform_renderer_ref = renderer
        item.dispose()
        assert item._effect_waveform_renderer_ref is None
        assert item._effect_waveform is None
