"""P1-01 多机位波形自动同步测试。

纯 DSP（互相关滞后恢复）→ 组偏移写回 → worker 编排 → 真实素材解码对齐。

语义约定（与 SwitchMulticamAngleCommand 一致）：
``lag > 0`` = 参考内容在该角度素材中出现得更晚（该机位开录更早），
切换命令读取 ``base + sync_offset``，因此 ``offset_frames = lag_seconds * fps``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.multicam import (
    build_multicam_group_from_assets,
    group_with_sync_offsets,
    resolve_angle_source_frame,
)
from src.features.edit.core.multicam_sync import (
    best_lag_samples,
    compute_sync_alignments,
    decimate_mean,
    downmix_to_mono,
    normalize_signal,
)


RATE = 8000
REAL_VIDEO = Path(__file__).resolve().parents[2] / "tests" / "test_videos" / "2K_h264.mp4"


def _burst_signal(seconds: float = 4.0, seed: int = 7, rate: int = RATE) -> np.ndarray:
    """带 3 个尖锐脉冲事件的环境噪声信号（互相关的理想目标）。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    x = rng.normal(0, 0.05, n).astype(np.float32)
    for start in (int(1.0 * rate), int(2.0 * rate), int(3.0 * rate)):
        width = 160
        x[start:start + width] += np.hanning(width).astype(np.float32) * 0.9
    return x


def _noise(seconds: float = 4.0, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(
        0, 0.05, int(seconds * RATE)).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# 纯 DSP 基础
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestDspHelpers:

    def test_downmix_stereo_mean(self):
        pcm = np.array([[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]], dtype=np.float32)
        assert np.allclose(downmix_to_mono(pcm), [2.0, 3.0, 4.0])

    def test_downmix_mono_passthrough(self):
        x = np.array([1.0, 2.0], dtype=np.float32)
        assert downmix_to_mono(x) is x

    def test_decimate_mean_factor(self):
        x = np.arange(12, dtype=np.float32)
        out = decimate_mean(x, 6)
        assert out.shape == (2,)
        assert np.allclose(out, [2.5, 8.5])

    def test_decimate_passthrough_small_factor(self):
        x = np.arange(5, dtype=np.float32)
        assert decimate_mean(x, 1) is x

    def test_normalize_unit_std(self):
        x = np.linspace(-3, 3, 1000).astype(np.float32) * 10.0
        n = normalize_signal(x)
        assert abs(n.mean()) < 1e-5
        assert abs(n.std() - 1.0) < 1e-3

    def test_normalize_silent_is_finite(self):
        n = normalize_signal(np.zeros(64, dtype=np.float32))
        assert np.all(np.isfinite(n))


# ─────────────────────────────────────────────────────────────────────────────
# 互相关滞后
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestBestLag:

    def test_recovers_positive_lag(self):
        # 角度 B 开录更早：B 里事件出现得更晚（前面多 d 样本噪声）
        d = int(0.35 * RATE)
        ref = _burst_signal(seed=7)
        other = np.concatenate([
            np.random.default_rng(1).normal(0, 0.05, d).astype(np.float32),
            ref,
        ])
        lag, conf = best_lag_samples(ref, other, int(2.0 * RATE))
        assert abs(lag - d) <= 2
        assert conf > 0.3

    def test_recovers_negative_lag(self):
        # 角度 B 开录更晚：其素材里事件更靠前（缺了头部 d 样本）
        d = int(0.25 * RATE)
        full = _burst_signal(seconds=5.0, seed=3)
        ref = full
        other = full[d:]
        lag, conf = best_lag_samples(ref, other, int(2.0 * RATE))
        assert abs(lag + d) <= 2
        assert conf > 0.3

    def test_identical_signals_lag_zero_high_confidence(self):
        ref = _burst_signal(seed=11)
        lag, conf = best_lag_samples(ref, ref.copy(), int(2.0 * RATE))
        assert lag == 0
        assert conf > 0.99

    def test_uncorrelated_noise_low_confidence(self):
        # 纯噪声（无共享事件结构）——峰值相关应很低
        ref = _noise(seed=1)
        other = _noise(seed=2)
        _, conf = best_lag_samples(ref, other, int(2.0 * RATE))
        assert conf < 0.3

    def test_search_window_clamps_lag(self):
        d = int(1.5 * RATE)  # 真实偏移超出 ±0.5s 搜索窗
        ref = _burst_signal(seed=7)
        other = np.concatenate([
            np.random.default_rng(1).normal(0, 0.05, d).astype(np.float32),
            ref,
        ])
        lag, _ = best_lag_samples(ref, other, int(0.5 * RATE))
        assert abs(lag) <= int(0.5 * RATE)


# ─────────────────────────────────────────────────────────────────────────────
# 组对齐结果
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_core
class TestComputeAlignments:

    def test_reference_zero_and_frame_conversion(self):
        d = int(0.5 * RATE)  # 0.5s
        ref = _burst_signal(seed=5)
        other = np.concatenate([
            np.random.default_rng(2).normal(0, 0.05, d).astype(np.float32),
            ref,
        ])
        signals = {"cam_a": ref, "cam_b": other}
        result = compute_sync_alignments(
            signals, RATE, "cam_a",
            fps_by_asset={"cam_a": 24.0, "cam_b": 24.0},
            max_shift_seconds=2.0,
        )
        assert result["cam_a"].offset_frames == 0
        assert result["cam_a"].confidence == 1.0
        b = result["cam_b"]
        assert abs(b.lag_seconds - 0.5) < 0.01
        assert b.offset_frames == 12  # 0.5s * 24fps
        assert b.confidence > 0.3
        assert b.error == ""

    def test_missing_reference_marks_errors(self):
        signals = {"a": _burst_signal(), "b": _burst_signal(seed=9)}
        result = compute_sync_alignments(
            signals, RATE, "missing",
            fps_by_asset={"a": 24.0, "b": 24.0},
        )
        assert all(r.error == "missing_or_short_reference" for r in result.values())

    def test_short_segment_error(self):
        result = compute_sync_alignments(
            {"a": _burst_signal(), "b": np.zeros(100, dtype=np.float32)},
            RATE, "a", fps_by_asset={"a": 24.0, "b": 24.0},
        )
        assert result["b"].error == "segment_too_short"
        assert result["a"].offset_frames == 0


@pytest.mark.edit_core
class TestGroupOffsetHelpers:

    def test_group_with_sync_offsets_updates_members(self):
        group = build_multicam_group_from_assets(["a1", "a2", "a3"])
        updated = group_with_sync_offsets(group, {"a2": 30, "a3": -12})
        by_id = {a.asset_id: a for a in updated.angles}
        assert by_id["a1"].sync_offset == 0   # 未给出的保持
        assert by_id["a2"].sync_offset == 30
        assert by_id["a3"].sync_offset == -12
        assert updated.group_id == group.group_id

    def test_resolve_angle_source_frame_matches_switch_semantics(self):
        # 片段当前指向 a1（offset 0），playhead=40 处切到 a2（offset +12）：
        # 命令读 a2 源 = base(40) + 12 → 网格帧号必须一致
        frame = resolve_angle_source_frame(
            clip_source_start=0, clip_timeline_start=0,
            active_sync_offset=0, target_sync_offset=12,
            playhead_frame=40, total_frames=500,
        )
        assert frame == 52

    def test_resolve_angle_source_frame_clamps(self):
        assert resolve_angle_source_frame(
            0, 0, 0, 0, 10, 0) == 0
        assert resolve_angle_source_frame(
            0, 0, 0, 900, 10, 500) == 499

    def test_resolve_active_angle_returns_playhead_position(self):
        # 目标 = 当前角度 → 网格显示正在播放的那一帧
        assert resolve_angle_source_frame(
            20, 10, 7, 7, 35, 500) == 45


# ─────────────────────────────────────────────────────────────────────────────
# Worker 编排（monkeypatch 解码入口，不触真实文件）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.edit_ui
class TestMulticamSyncWorker:

    def test_worker_emits_payload(self, qapp, monkeypatch):
        from src.features.edit.core import multicam_sync

        # 假解码返回 48kHz 契约信号（worker 内部再降采样到 8k）
        high_rate = 48000
        d = int(0.5 * high_rate)
        base = _burst_signal(seconds=4.0, seed=4, rate=high_rate)
        late = np.concatenate([
            np.random.default_rng(8).normal(0, 0.05, d).astype(np.float32),
            base,
        ])

        def fake_decode(path, seconds, output_rate=48000, block_samples=48000,
                        start_seconds=0.0):
            if "b" in path:
                return late, ""
            return base, ""

        monkeypatch.setattr(multicam_sync, "_decode_mono_segment", fake_decode)

        results: dict = {}
        failures: list = []

        from src.features.edit.core.multicam_sync import MulticamSyncJob, MulticamSyncWorker
        worker = MulticamSyncWorker(
            [MulticamSyncJob("a", "/x/a.mp4", 24.0),
             MulticamSyncJob("b", "/x/b.mp4", 24.0)],
            reference_asset_id="a",
        )
        worker.sync_finished.connect(lambda payload: results.update(payload))
        worker.sync_failed.connect(failures.append)
        worker.run()  # 直接在当前线程执行（不走事件循环）

        assert failures == []
        assert results["a"]["offset_frames"] == 0
        b = results["b"]
        assert b["offset_frames"] == 12  # 0.5s * 24fps
        assert b["confidence"] > 0.3
        assert b["error"] == ""

    def test_worker_reference_failure(self, qapp, monkeypatch):
        from src.features.edit.core import multicam_sync

        def fake_decode(path, seconds, output_rate=48000, block_samples=48000,
                        start_seconds=0.0):
            return None, "no_audio_stream"

        monkeypatch.setattr(multicam_sync, "_decode_mono_segment", fake_decode)

        failures: list = []
        finished: list = []
        from src.features.edit.core.multicam_sync import MulticamSyncJob, MulticamSyncWorker
        worker = MulticamSyncWorker(
            [MulticamSyncJob("a", "/x/a.mp4", 24.0),
             MulticamSyncJob("b", "/x/b.mp4", 24.0)],
            reference_asset_id="a",
        )
        worker.sync_finished.connect(finished.append)
        worker.sync_failed.connect(failures.append)
        worker.run()

        assert finished == []
        assert failures == ["no_audio_stream"]


# ─────────────────────────────────────────────────────────────────────────────
# 真实素材：解码 → 降采样 → 相关恢复偏移
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.real_media
@pytest.mark.skipif(not REAL_VIDEO.exists(), reason="real test video missing")
class TestRealMaterialAlignment:

    def test_real_audio_alignment_recovers_shift(self):
        from src.features.edit.core.multicam_sync import _decode_mono_segment

        mono, err = _decode_mono_segment(str(REAL_VIDEO), seconds=3.0)
        assert mono is not None and err == "", f"decode failed: {err}"
        assert mono.size >= 48000 * 2  # 至少 2 秒

        factor = 6
        low = decimate_mean(mono, factor)
        rate = 48000 // factor

        # 模拟机位 B 开录更早：其素材窗口比参考早 d_full 个样本，
        # 同一内容在 B 中出现得更晚 → 期望 lag = +d
        d_full = int(0.42 * 48000)
        d_low = d_full // factor
        start = rate  # 参考窗口从 1s 处取
        n = min(rate, low.size - start - d_low - 8)
        assert n > rate // 2
        ref = low[start:start + n]
        other = low[start - d_low:start - d_low + n]
        assert other.size == n

        lag, conf = best_lag_samples(ref, other, rate)
        assert abs(lag - d_low) <= 2, f"lag={lag} expected~{d_low} conf={conf}"
        assert conf > 0.3
