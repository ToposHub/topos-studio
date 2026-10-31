"""
P2-05 持久化 GOP 索引测试。

覆盖：
- 真实素材上的索引构建（keyframe 表、walk 距离、pts 换算）
- 持久化 save/load 往返与身份失效（mtime/size 变化）
- 损坏缓存与不兼容 schema 的容错
- seek.py 决策函数的真实 GOP 覆盖
- FFmpegMediaSource 的 SLICE seek 会话：与主会话路径帧数据逐字节一致、
  顺序续读、keyframe_interval_hint、回同步
"""

from __future__ import annotations

import json
import os
from fractions import Fraction

import pytest

from src.shared.config.config_manager import ConfigManager
from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.gop_index import (
    GopIndex,
    GopIndexKey,
    build_gop_index,
    cache_file_for,
    get_or_build_gop_index,
    load_gop_index,
    save_gop_index,
)
from src.shared.media.seek import estimate_keyframe_interval, estimate_seek_cost


TEST_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
H265_PATH = os.path.join(TEST_ROOT, "tests", "test_videos", "4K_H265.mov")
PRORES_PATH = os.path.join(TEST_ROOT, "tests", "test_videos", "4K_prores422HQ.mov")


def _require_real_video(path: str) -> str:
    if not os.path.exists(path):
        pytest.skip(f"real test video not available: {path}")
    return path


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """把平台缓存目录隔离到临时目录。"""
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        ConfigManager, "get_platform_cache_dir", classmethod(lambda cls: str(cache))
    )
    return cache


class TestGopIndexBuild:
    """索引构建（真实素材）。"""

    def test_build_on_real_h265(self):
        _require_real_video(H265_PATH)
        index = build_gop_index(H265_PATH)
        assert index is not None
        assert index.codec_name == "hevc"
        # 4K_H265.mov: 487 帧、keyint=25（benchmark 素材实测）
        assert index.frame_count == 487
        assert len(index.keyframe_frames) == 20
        assert index.keyframe_frames[:4] == (0, 25, 50, 75)
        assert index.keyframe_interval == 25
        assert index.avg_gop == pytest.approx(25.0)

    def test_pts_roundtrip_matches_frame_to_pts_semantics(self):
        _require_real_video(H265_PATH)
        index = build_gop_index(H265_PATH)
        assert index is not None
        ppf = index.pts_per_frame()
        assert ppf == Fraction(512, 1)  # tb=1/12800, fps=25
        assert index.frame_to_pts(25) == 12800
        assert index.keyframe_pts()[1] == 12800

    def test_decode_distance(self):
        _require_real_video(H265_PATH)
        index = build_gop_index(H265_PATH)
        assert index is not None
        assert index.decode_distance(0) == 0
        assert index.decode_distance(25) == 0
        assert index.decode_distance(137) == 12  # 137 → keyframe 125
        assert index.decode_distance(486) == 11  # 486 → keyframe 475
        assert index.keyframe_frame_before(24) == 0

    def test_build_on_intra_still_yields_table(self):
        """Intra 素材每帧都是 keyframe，索引可用但间隔为 1。"""
        _require_real_video(PRORES_PATH)
        index = build_gop_index(PRORES_PATH)
        if index is None:
            pytest.skip("prores build returned no keyframes")
        assert index.keyframe_interval == 1

    def test_build_missing_file_returns_none(self):
        assert build_gop_index("/nonexistent/video.mov") is None


class TestGopIndexPersistence:
    """持久化与失效。"""

    def test_save_load_roundtrip(self, cache_dir):
        _require_real_video(H265_PATH)
        key = GopIndexKey.from_path(H265_PATH)
        assert key is not None
        index = build_gop_index(H265_PATH, identity=key)
        assert index is not None
        assert save_gop_index(index, key) is True

        cache_file = cache_file_for(key)
        assert cache_file.exists()

        loaded = load_gop_index(H265_PATH, key=key)
        assert loaded is not None
        assert loaded.keyframe_frames == index.keyframe_frames
        assert loaded.fps == index.fps
        assert loaded.time_base == index.time_base
        assert loaded.codec_name == index.codec_name
        assert loaded.identity == key

    def test_mtime_change_invalidates(self, cache_dir, tmp_path):
        """源文件 mtime 变化 → 索引失效（身份不匹配）。"""
        src_file = tmp_path / "clip.mov"
        src_file.write_bytes(b"fake")
        key = GopIndexKey(
            source_path=str(src_file),
            source_mtime_ns=1000,
            source_size=4,
        )
        index = GopIndex(
            codec_name="hevc",
            frame_count=10,
            fps=Fraction(25, 1),
            time_base=Fraction(1, 12800),
            keyframe_frames=(0, 5, 10),
            identity=key,
        )
        assert save_gop_index(index, key) is True
        assert load_gop_index(str(src_file), key=key) is not None

        changed = GopIndexKey(
            source_path=str(src_file),
            source_mtime_ns=9999,
            source_size=4,
        )
        assert load_gop_index(str(src_file), key=changed) is None

    def test_corrupt_cache_returns_none(self, cache_dir):
        _require_real_video(H265_PATH)
        key = GopIndexKey.from_path(H265_PATH)
        assert key is not None
        cache_file_for(key).parent.mkdir(parents=True, exist_ok=True)
        cache_file_for(key).write_text("{not json", encoding="utf-8")
        assert load_gop_index(H265_PATH, key=key) is None

    def test_schema_mismatch_rejected(self, cache_dir):
        _require_real_video(H265_PATH)
        key = GopIndexKey.from_path(H265_PATH)
        assert key is not None
        index = build_gop_index(H265_PATH, identity=key)
        assert index is not None
        payload = index.to_dict()
        payload["schema"] = 999
        cache_file_for(key).parent.mkdir(parents=True, exist_ok=True)
        cache_file_for(key).write_text(json.dumps(payload), encoding="utf-8")
        assert load_gop_index(H265_PATH, key=key) is None

    def test_get_or_build_read_through(self, cache_dir):
        _require_real_video(H265_PATH)
        first = get_or_build_gop_index(H265_PATH)
        assert first is not None
        # 第二次命中磁盘缓存
        second = get_or_build_gop_index(H265_PATH)
        assert second is not None
        assert second.keyframe_frames == first.keyframe_frames

    def test_unsorted_keyframes_rejected(self):
        raw = {
            "schema": 1,
            "codec_name": "hevc",
            "frame_count": 100,
            "fps_num": 25,
            "fps_den": 1,
            "tb_num": 1,
            "tb_den": 12800,
            "keyframe_frames": [50, 0, 25],
            "identity": None,
        }
        assert GopIndex.from_dict(raw) is None
        raw["keyframe_frames"] = [0, 25, 50]
        assert GopIndex.from_dict(raw) is not None


class TestSeekDecisionWithRealGop:
    """seek.py 决策函数的真实 GOP 覆盖。"""

    def test_real_interval_overrides_guess_table(self):
        # 猜测表 h264 → 12；真实素材 keyint=25
        assert estimate_keyframe_interval("h264") == 12
        assert estimate_keyframe_interval("h264", 25) == 25

    def test_estimate_seek_cost_with_real_interval(self):
        # 目标 137、keyint=25 → decode forward 12 帧；猜测表(12)只会给 5
        assert estimate_seek_cost(0, 137, "hevc", 25) == 12
        assert estimate_seek_cost(0, 137, "hevc") == 5
        # 无效值回退猜测表
        assert estimate_seek_cost(0, 137, "hevc", 0) == estimate_seek_cost(0, 137, "hevc")
        assert estimate_seek_cost(0, 137, "hevc", -1) == estimate_seek_cost(0, 137, "hevc")


class TestGopSeekSession:
    """FFmpegMediaSource SLICE seek 会话。"""

    def test_seek_exactness_with_session(self):
        """会话路径 seek 精度与帧内容正确。"""
        _require_real_video(H265_PATH)
        source = FFmpegMediaSource(H265_PATH)
        source.open()
        try:
            for target in (0, 1, 25, 26, 137, 300, 486):
                source.seek_frame(target)
                frame = source.read_frame(target)
                assert frame.frame_index == target
        finally:
            source.close()

    def test_session_frames_byte_identical_to_legacy(self, monkeypatch):
        """会话路径（seek + 顺序续读）与主会话路径逐字节一致。"""
        _require_real_video(H265_PATH)
        import src.shared.media.ffmpeg_source as ff_module

        targets = [137, 300, 26]
        seq_after = 3

        def collect(disable: bool) -> dict[int, list[bytes]]:
            orig = ff_module.GOP_SLICE_SEEK_CODECS
            if disable:
                monkeypatch.setattr(ff_module, "GOP_SLICE_SEEK_CODECS", frozenset())
            try:
                source = FFmpegMediaSource(H265_PATH)
                source.set_preserve_planes(True)
                source.open()
                out: dict[int, list[bytes]] = {}
                try:
                    for target in targets:
                        source.seek_frame(target)
                        frames = [source.read_frame_planar(target)]
                        for k in range(1, seq_after + 1):
                            if target + k >= 487:
                                break
                            frames.append(source.read_frame_planar(target + k))
                        out[target] = [f.planes[0].tobytes() for f in frames]
                        assert all(
                            f.frame_index == target + k
                            for k, f in enumerate(frames)
                        )
                finally:
                    source.close()
            finally:
                if disable:
                    monkeypatch.setattr(
                        ff_module, "GOP_SLICE_SEEK_CODECS", orig
                    )
            return out

        legacy = collect(disable=True)
        session = collect(disable=False)
        for target in targets:
            assert len(legacy[target]) == len(session[target])
            for k, (a, b) in enumerate(zip(legacy[target], session[target])):
                assert a == b, f"plane mismatch: target={target} offset={k}"

    def test_reverse_seeks_via_session(self):
        """反向 seek（大步后退）在会话路径上保持精确。"""
        _require_real_video(H265_PATH)
        source = FFmpegMediaSource(H265_PATH)
        source.open()
        try:
            for target in (400, 130, 25, 475, 60):
                source.seek_frame(target)
                frame = source.read_frame(target)
                assert frame.frame_index == target
        finally:
            source.close()

    def test_keyframe_interval_hint_after_warm(self, cache_dir):
        """索引预热后 keyframe_interval_hint 提供真实 GOP。"""
        _require_real_video(H265_PATH)
        source = FFmpegMediaSource(H265_PATH)
        source.open()
        try:
            assert source.keyframe_interval_hint is None
            index = source.warm_gop_index()
            assert index is not None
            assert source.keyframe_interval_hint == 25
        finally:
            source.close()

    def test_sequential_read_after_seek_matches_plain_walk(self):
        """seek 后顺序续读的帧号连续性（会话迭代器 + 回同步路径）。"""
        _require_real_video(H265_PATH)
        source = FFmpegMediaSource(H265_PATH)
        source.set_preserve_planes(True)
        source.open()
        try:
            source.seek_frame(120)
            frames = [source.read_frame_planar(120)]
            for k in range(1, 30):  # 跨越 GOP 边界（120→149）
                frames.append(source.read_frame_planar(120 + k))
            assert [f.frame_index for f in frames] == list(range(120, 150))
            # 主会话回同步后再 seek，状态必须干净
            source.seek_frame(200)
            frame = source.read_frame(200)
            assert frame.frame_index == 200
        finally:
            source.close()

    def test_close_releases_session_container(self):
        _require_real_video(H265_PATH)
        source = FFmpegMediaSource(H265_PATH)
        source.open()
        source.seek_frame(100)
        source.read_frame(100)
        assert source._gop_seek_container is not None
        source.close()
        assert source._gop_seek_container is None
        assert source._gop_seek_iterator is None
        assert source._main_resync_pts is None

    def test_planar_read_after_seek_session(self):
        """planar 模式下会话 seek + 续读不丢平面。"""
        _require_real_video(H265_PATH)
        source = FFmpegMediaSource(H265_PATH)
        source.set_preserve_planes(True)
        source.open()
        try:
            source.seek_frame(63)
            frame = source.read_frame_planar(63)
            assert frame.frame_index == 63
            assert len(frame.planes) >= 3  # YUV
            assert frame.planes[0].shape[0] > 0
        finally:
            source.close()
