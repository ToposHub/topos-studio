"""R04：VFR 的实际播放路径与查询复杂度。

复现（2026-11-09 复查 R04）：

- 导入器已保存 ``metadata["pts_index"]``，但 ``_build_time_domain`` 不
  消费；``set_source_time_index`` 生产无调用方且无身份校验；
- Source tick 正放/倒放均按 ``elapsed × float fps``（CFR 数学），VFR
  素材帧距不均时播放头错位；
- 音频起播/seek/循环回卷按帧直传（平均 fps 语义），与视频秒域不一致；
- Color ``vfr_frame_for_seconds`` 每次查询遍历全部 points 构造秒列
  （10 万点单次查询 O(n)）。

本组钉死修复后的行为：打开即消费 pts_index（清洗政策显式）；tick/
音频共用同一 PTS 秒域；迟到/异素材索引拒绝；CFR 回退如实报告；
反查 O(log n)（100k 点压测成本不随片长线性增长）。
"""

from __future__ import annotations

import time as _time
from typing import Any

import pytest

from src.features.edit.core.project import MediaAsset
from src.features.edit.core.scene_cut.models import (
    FrameTimePoint, SourceFrameTimeIndex,
)
from src.features.edit.ui.source_viewer import SourceViewerWidget

pytestmark = pytest.mark.edit_ui

# 10 帧不规则 VFR 秒表（CFR 24fps 数学与 PTS 秒域在此明显分歧）
_PTS = [0.0, 0.05, 0.10, 0.30, 0.35, 0.40, 0.80, 0.85, 0.90, 1.00]


def _asset(asset_id: str = "vfr", total: int = 10, fps: float = 24.0,
           pts: list[float] | None = None, path: str | None = None,
           **extra: Any) -> MediaAsset:
    metadata = {"fps": fps, "total_frames": total, "width": 640,
                "height": 480}
    if pts is not None:
        metadata["pts_index"] = pts
        metadata["is_vfr"] = True
    metadata.update(extra)
    return MediaAsset(
        asset_id=asset_id,
        path=path or f"/tmp/r04_{asset_id}_{total}.mov",
        media_type="video", display_name=f"{asset_id}.mov",
        metadata=metadata)


def _widget(qapp: Any) -> SourceViewerWidget:
    widget = SourceViewerWidget()
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    widget._start_source_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
    widget._pause_source_audio = lambda: None  # type: ignore[method-assign]
    widget._load_video_frame = lambda *_a, **_k: None  # type: ignore[method-assign]
    return widget


def _playing_widget(qapp: Any, *, pts: list[float] | None = None,
                    fps: float = 24.0) -> SourceViewerWidget:
    widget = _widget(qapp)
    widget.open_asset(_asset(pts=pts if pts is not None else _PTS,
                             total=len(pts or _PTS), fps=fps))
    widget._is_playing = True
    widget._playback_start_time = _time.perf_counter()
    widget._playback_start_frame = 0
    return widget


class _FakeAudioManager:
    def __init__(self) -> None:
        self.played: list[int] = []
        self.seeked: list[int] = []
        self.scrubbed: list[int] = []

    def play(self, start_frame: int = 0) -> None:
        self.played.append(int(start_frame))

    def seek(self, frame: int) -> None:
        self.seeked.append(int(frame))

    def preview_scrub_audio(self, frame: int,
                            duration_ms: int | None = None) -> bool:
        self.scrubbed.append(int(frame))
        return True


# ── 打开即消费 pts_index（含清洗政策） ─────────────────────────────

def test_open_consumes_imported_pts_index_and_reports_vfr(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(pts=_PTS))
    assert widget.source_time_mode == "vfr_pts"
    domain = widget.source_time_domain
    assert domain is not None and domain.time_index is not None
    assert domain.frame_to_seconds(3) == pytest.approx(0.30)
    assert domain.seconds_to_frame(0.82) == 6  # 最近邻


def test_no_index_reports_cfr_explicitly(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(pts=None))
    assert widget.source_time_mode == "cfr"
    assert widget.source_time_domain.time_index is None


def test_from_pts_seconds_sanitizes_nan_duplicates_and_backwards():
    dirty = [0.0, float("nan"), 0.10, 0.10, 0.05, "junk", 0.30, float("inf")]
    index = SourceFrameTimeIndex.from_pts_seconds("a", "fp", dirty)
    assert index is not None
    frames = [p.source_frame for p in index.points]
    seconds = [p.seconds for p in index.points]
    # NaN/junk/inf 丢弃；重复(0.10)与回退(0.05)只保留首个
    assert frames == [0, 2, 6]
    assert seconds == pytest.approx([0.0, 0.10, 0.30])
    assert index.average_fps == pytest.approx(2 / 0.30)


def test_from_pts_seconds_below_two_points_returns_none():
    assert SourceFrameTimeIndex.from_pts_seconds(
        "a", "fp", [float("nan"), 1.0]) is None
    assert SourceFrameTimeIndex.from_pts_seconds("a", "fp", []) is None
    assert SourceFrameTimeIndex.from_pts_seconds("a", "fp", None) is None


def test_open_with_unsalvageable_pts_falls_back_to_cfr(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(pts=[float("nan"), 0.5]))
    assert widget.source_time_mode == "cfr"  # 清洗失败显式回退


def test_seconds_column_is_cached_and_matches_points():
    index = SourceFrameTimeIndex.from_pts_seconds("a", "fp", _PTS)
    assert index is not None
    first = index.seconds_column()
    assert first is index.seconds_column()  # 懒缓存：同一对象
    assert list(first) == _PTS


# ── 播放 tick：PTS 秒域（正放/倒放/CFR 回退） ───────────────────────

def test_forward_tick_targets_pts_seconds_not_average_fps(qapp):
    widget = _playing_widget(qapp)
    # elapsed=0.32s：PTS 域最近邻是 0.30s（帧 3）；CFR 数学会给 7
    widget._playback_start_time = _time.perf_counter() - 0.32
    widget._on_playback_tick()
    assert widget._current_frame == 3


def test_reverse_tick_steps_back_through_pts_seconds(qapp):
    widget = _playing_widget(qapp)
    widget._reverse_playback = True
    widget._playback_start_frame = 9  # 1.00s
    widget._current_frame = 9
    # elapsed=0.75s → 0.25s → 最近邻 0.30s（帧 3）；CFR 数学会直接到 0
    widget._playback_start_time = _time.perf_counter() - 0.75
    widget._on_playback_tick()
    assert widget._current_frame == 3


def test_cfr_tick_math_unchanged_without_index(qapp):
    widget = _playing_widget(qapp, pts=None)
    widget.open_asset(_asset(pts=None, total=240))
    widget._is_playing = True
    widget._playback_start_frame = 0
    widget._playback_start_time = _time.perf_counter() - 0.32
    widget._on_playback_tick()
    assert widget._current_frame == int(0.32 * 24.0)


def test_frame_after_elapsed_clamps_at_media_bounds(qapp):
    widget = _playing_widget(qapp)
    # 正放越界：最近邻钳到末帧（tick 再按 limit/min 收口）
    assert widget._frame_after_elapsed(0, 10.0) == 9
    # 倒放到 0 秒前：钳到帧 0
    assert widget._frame_after_elapsed(9, 10.0, reverse=True) == 0


# ── 音频：同一秒域换算 ─────────────────────────────────────────────

def test_audio_position_frame_maps_via_pts_seconds(qapp):
    widget = _playing_widget(qapp)
    # 帧 3 = 0.30s → 音频帧钟 24fps → round(7.2) = 7（CFR 直传会是 3）
    assert widget._audio_position_frame(3) == 7
    assert widget._audio_position_frame(0) == 0


def test_audio_position_frame_identity_for_cfr(qapp):
    widget = _playing_widget(qapp, pts=None)
    widget.open_asset(_asset(pts=None, total=240))
    assert widget._audio_position_frame(117) == 117


def test_start_and_seek_audio_use_seconds_domain(qapp):
    widget = _playing_widget(qapp)
    fake = _FakeAudioManager()
    widget._scrub_audio_manager = fake  # type: ignore[assignment]
    # _widget() 把 _start_source_audio 换成了 no-op 桩——直接调用真实现
    SourceViewerWidget._start_source_audio(widget, 3)
    widget._seek_source_audio(3)
    assert fake.played == [7]
    assert fake.seeked == [7]


def test_scrub_audio_preview_uses_seconds_domain(qapp):
    widget = _playing_widget(qapp)
    fake = _FakeAudioManager()
    widget._scrub_audio_manager = fake  # type: ignore[assignment]
    widget._scrub_audio_last_emit_ms = 0.0
    widget._request_scrub_audio_preview(3, force=True)
    assert fake.scrubbed == [7]


def test_loop_wrap_reanchors_clock_and_seeks_audio(qapp):
    widget = _playing_widget(qapp)
    widget._loop_enabled = True
    widget._in_point, widget._out_point = 2, 5
    fake = _FakeAudioManager()
    widget._scrub_audio_manager = fake  # type: ignore[assignment]
    widget._playback_start_frame = 5
    widget._handle_playback_end((2, 5))
    assert widget._playback_start_frame == 2
    # 回卷音频位置：帧 2 = 0.10s → round(0.10 × 24) = 2（秒域换算路径）
    assert fake.seeked == [widget._audio_position_frame(2)]


# ── 索引注入：身份校验（迟到 A 不误注 B） ──────────────────────────

def test_stale_time_index_rejected_identity_mismatch(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(asset_id="A", pts=_PTS))
    before = widget.source_time_domain

    other = SourceFrameTimeIndex.from_pts_seconds("B", "other-fp", _PTS)
    widget.set_source_time_index(other)
    assert widget.source_time_domain.time_index is before.time_index

    current_fp = before.media_fingerprint
    wrong_fp = SourceFrameTimeIndex.from_pts_seconds(
        "A", "not-the-fingerprint", _PTS)
    widget.set_source_time_index(wrong_fp)
    assert widget.source_time_domain.time_index is before.time_index


def test_matching_index_injection_still_accepted(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(asset_id="A", pts=None))
    domain = widget.source_time_domain
    assert domain.time_index is None
    fresh = SourceFrameTimeIndex.from_pts_seconds(
        "A", domain.media_fingerprint, _PTS)
    widget.set_source_time_index(fresh)
    assert widget.source_time_domain.time_index is fresh
    assert widget.source_time_mode == "vfr_pts"


def test_late_index_for_previous_asset_rejected_after_switch(qapp):
    widget = _widget(qapp)
    widget.open_asset(_asset(asset_id="A", pts=_PTS))
    stale = widget.source_time_domain.time_index
    widget.open_asset(_asset(asset_id="B", pts=None, total=240))
    assert widget.source_time_mode == "cfr"
    widget.set_source_time_index(stale)  # A 的索引迟到
    assert widget.source_time_mode == "cfr"  # 不误注


# ── 反查复杂度：O(log n)（100k 点压测） ─────────────────────────────

def _big_index(n: int) -> SourceFrameTimeIndex:
    # 非均匀 VFR：抖动步长（严格递增），避免均匀 CFR 退化的巧合
    seconds: list[float] = []
    t = 0.0
    for i in range(n):
        t += 0.02 + (i % 7) * 0.001
        seconds.append(round(t, 6))
    return SourceFrameTimeIndex.from_pts_seconds("big", "fp", seconds)


def test_reverse_query_cost_does_not_scale_with_length():
    from src.features.color.core.color_time_domain import vfr_frame_for_seconds

    small = _big_index(1_000)
    large = _big_index(100_000)

    def _avg_us(index: SourceFrameTimeIndex, probes: int) -> float:
        t0 = _time.perf_counter()
        for i in range(probes):
            vfr_frame_for_seconds(index, (i * 7919 % 999_997) / 1000.0)
            index.seconds_to_nearest_source_frame(
                (i * 104729 % 999_997) / 1000.0)
        return (_time.perf_counter() - t0) / probes * 1e6

    small_us = _avg_us(small, 2_000)
    large_us = _avg_us(large, 2_000)
    # 线性实现下 100k/1k ≈ 100×；O(log n) 下应同量级（放宽容差抗抖动）
    assert large_us < max(small_us * 8.0, 60.0), (
        f"reverse query scales with length: {small_us:.1f}µs → "
        f"{large_us:.1f}µs")
    assert large_us < 1_000.0  # 绝对上限：单次查询远低于 1ms


def test_reverse_query_matches_brute_force_nearest():
    import random

    from src.features.color.core.color_time_domain import vfr_frame_for_seconds

    rng = random.Random(4)
    seconds: list[float] = []
    t = 0.0
    for i in range(500):
        t += rng.uniform(0.01, 0.09)
        seconds.append(round(t, 6))
    index = SourceFrameTimeIndex.from_pts_seconds("q", "fp", seconds)

    for _ in range(200):
        probe = rng.uniform(0.0, t)
        expected = min(range(len(seconds)),
                       key=lambda i: (abs(seconds[i] - probe), -i))
        # 与既有 tie 语义一致：并列取较早帧（brute force 的 min+(-i) 同向）
        assert vfr_frame_for_seconds(index, probe) == expected
        assert index.seconds_to_nearest_source_frame(probe) == expected


# ── 真实 VFR 容器端到端（PyAV 生成 → 真实 demux PTS → 域链路） ──────

# 不规则帧距（ms）：40/40/70/40/90/50/90/50/90 —— 均值差 > 25% 判 VFR
_VFR_PTS_MS = (0, 40, 80, 150, 190, 280, 330, 420, 470, 560)


def _write_real_vfr_file(tmp_path) -> str:
    """PyAV 生成小尺寸真 VFR MP4（逐帧显式 PTS，非均匀间隔）。"""
    av = pytest.importorskip("av")
    import numpy as np
    from fractions import Fraction

    path = str(tmp_path / "r04_real_vfr.mp4")
    container = av.open(path, mode="w")
    stream = container.add_stream("libx264", rate=25)
    stream.width, stream.height = 64, 48
    stream.pix_fmt = "yuv420p"
    # 显式细粒度时间基（1ms）承载非均匀帧距；rate 只作名义帧率
    stream.codec_context.time_base = Fraction(1, 1000)
    try:
        for pts_ms in _VFR_PTS_MS:
            frame = av.VideoFrame(64, 48, "yuv420p")
            frame.pts = pts_ms
            for plane, arr in zip(
                    frame.planes,
                    (np.full((48, 64), 128, np.uint8),
                     np.full((24, 32), 128, np.uint8),
                     np.full((24, 32), 128, np.uint8))):
                plane.update(arr)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return path


def test_real_vfr_container_end_to_end(qapp, tmp_path):
    from src.shared.media.ffmpeg_source import (
        extract_pts_index_seconds, looks_vfr,
    )

    path = _write_real_vfr_file(tmp_path)
    pts = extract_pts_index_seconds(path)
    assert pts, "真实容器 demux 应产出 PTS 秒表"
    assert looks_vfr(pts), "非均匀帧距应判 VFR"
    assert len(pts) == len(_VFR_PTS_MS)
    expected = [ms / 1000.0 for ms in _VFR_PTS_MS]
    assert pts == pytest.approx(expected, abs=2e-4)  # 容器往返保真

    # 逐点：打开即建域 → 帧/秒往返 + 音频位置同一秒域
    widget = _widget(qapp)
    asset = _asset(asset_id="real", total=len(pts), fps=25.0, pts=pts,
                   path=path)
    widget.open_asset(asset)
    assert widget.source_time_mode == "vfr_pts"
    domain = widget.source_time_domain
    for i, seconds in enumerate(expected):
        assert domain.frame_to_seconds(i) == pytest.approx(seconds, abs=2e-4)
        assert domain.seconds_to_frame(seconds + 1e-4) == i
        assert widget._audio_position_frame(i) == round(seconds * 25.0)
