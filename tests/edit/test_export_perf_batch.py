"""性能批次一/二（导出侧）focused tests。

- P1-2：导出解码会话按 clip 身份分流 + LRU 上限；
- P0-EXPORT-AUDIO：音频渲染/写入拆分与后台线程契约；
- P0-EXPORT-CACHE：comp-clip 帧级导出缓存（opt-in、质量中立）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from collections import OrderedDict

from src.features.edit.core.timeline_export import TimelineExporter
from src.shared.export.render_cache import DiskRenderCache, SegmentRenderKey


def _make_exporter() -> TimelineExporter:
    ex = TimelineExporter.__new__(TimelineExporter)
    ex._project = None
    from src.features.edit.core.timeline_v2 import TimelineSequence
    ex._sequence = TimelineSequence("export-perf", "Export perf", width=192, height=108)
    ex._sequence_resolver = None
    ex._frame_provider = None
    ex._open_clips: "OrderedDict[str, str]" = OrderedDict()
    ex._export_rgb8_sources = {}
    ex._export_high_depth_sources = {}
    ex._segment_cache = None
    ex._cancel_requested = False
    ex._use_high_precision_export = False
    ex._export_color_metadata = None
    ex._audio_render_thread = None
    ex._audio_master_pcm = None
    ex._audio_render_error = None
    return ex


# ---------------------------------------------------------------------------
# P1-2: clip 级会话分流 + LRU
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self):
        self.opened: list[str] = []
        self.closed: list[str] = []
        self._n = 0

    def open_clip(self, path, share_by_path=True, defer_memory_proxy=False):
        assert share_by_path is False, "导出必须走独立会话"
        self._n += 1
        clip_id = f"clip-{self._n}"
        self.opened.append(clip_id)
        return clip_id

    def close_clip(self, clip_id):
        self.closed.append(clip_id)
        return True


class TestLayerSessionKey:
    def test_prefers_clip_identity(self):
        layer = SimpleNamespace(clip_id="c1", asset_id="a1")
        assert TimelineExporter._layer_session_key(layer) == "c1"

    def test_falls_back_to_asset_id(self):
        layer = SimpleNamespace(clip_id="", asset_id="a1")
        assert TimelineExporter._layer_session_key(layer) == "a1"


class TestOpenExportVideoSession:
    def test_same_key_reuses_single_session(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_SESSION_CACHE", "4")
        ex = _make_exporter()
        provider = _FakeProvider()
        ex._frame_provider = provider
        first = ex._open_export_video_session("/a.mp4", "clip1")
        again = ex._open_export_video_session("/a.mp4", "clip1")
        assert first == again
        assert provider.opened == [first]
        assert provider.closed == []

    def test_different_clips_same_asset_get_distinct_sessions(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_SESSION_CACHE", "4")
        ex = _make_exporter()
        ex._frame_provider = _FakeProvider()
        a = ex._open_export_video_session("/a.mp4", "clip1")
        b = ex._open_export_video_session("/a.mp4", "clip2")
        assert a != b

    def test_lru_evicts_oldest_beyond_cap(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_SESSION_CACHE", "2")  # 实际下限 = 4
        ex = _make_exporter()
        provider = _FakeProvider()
        ex._frame_provider = provider
        sessions = [
            ex._open_export_video_session(f"/{n}.mp4", n) for n in "abcde"
        ]
        assert all(sessions)
        # a 最久未用 → 被逐出
        assert provider.closed == [sessions[0]]
        assert "a" not in ex._open_clips
        # 重新请求 a → 重新打开（正确性不受逐出影响）
        assert ex._open_export_video_session("/a.mp4", "a") not in sessions[:4]
        assert len(provider.opened) == 6

    def test_touch_refreshes_lru_order(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_SESSION_CACHE", "2")  # 实际下限 = 4
        ex = _make_exporter()
        provider = _FakeProvider()
        ex._frame_provider = provider
        sessions = {
            n: ex._open_export_video_session(f"/{n}.mp4", n) for n in "abcd"
        }
        # touch a 后 a 不再是最旧；打开 e 触发逐出 → 逐出的是 b
        ex._open_export_video_session("/a.mp4", "a")
        ex._open_export_video_session("/e.mp4", "e")
        assert provider.closed == [sessions["b"]]
        assert "b" not in ex._open_clips and "a" in ex._open_clips


# ---------------------------------------------------------------------------
# P0-EXPORT-AUDIO: 渲染/写入拆分
# ---------------------------------------------------------------------------


class TestAudioSplit:
    def test_render_master_returns_none_without_encoder(self):
        ex = _make_exporter()
        ex._encoder = None
        assert ex._render_audio_master(0, 10, SimpleNamespace()) is None

    def test_worker_stores_pcm_from_render(self, monkeypatch):
        ex = _make_exporter()
        pcm = np.zeros((2, 100), dtype=np.float32)
        monkeypatch.setattr(
            ex, "_render_audio_master", lambda s, e, c, encoder=None: pcm,
        )
        ex._audio_export_worker(0, 10, SimpleNamespace())
        assert ex._audio_master_pcm is pcm
        assert ex._audio_render_error is None

    def test_worker_captures_render_exception(self, monkeypatch):
        ex = _make_exporter()

        def _boom(_s, _e, _c, encoder=None):
            raise RuntimeError("mixer exploded")

        monkeypatch.setattr(ex, "_render_audio_master", _boom)
        ex._audio_export_worker(0, 10, SimpleNamespace())
        assert ex._audio_master_pcm is None
        assert isinstance(ex._audio_render_error, RuntimeError)

    def test_join_clears_thread_reference(self):
        """join 后线程引用清空：下一次导出不会误等旧线程。"""
        ex = _make_exporter()
        release = threading.Event()

        def _worker():
            release.wait(1.0)

        thread = threading.Thread(target=_worker, daemon=True)
        ex._audio_render_thread = thread
        thread.start()
        releaser = threading.Thread(target=release.set)
        releaser.start()
        ex._join_audio_render_thread()
        thread.join()
        releaser.join()
        assert ex._audio_render_thread is None

    def test_join_noop_without_thread(self):
        ex = _make_exporter()
        ex._join_audio_render_thread()
        assert ex._audio_render_thread is None


# ---------------------------------------------------------------------------
# P0-EXPORT-CACHE: 帧级导出缓存
# ---------------------------------------------------------------------------


class TestSegmentCache:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("TOPOS_EXPORT_SEGMENT_CACHE", raising=False)
        monkeypatch.delenv("TOPOS_EXPORT_SEGMENT_CACHE_DIR", raising=False)
        ex = _make_exporter()
        assert ex._segment_cache_config() is None

    def test_requires_explicit_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE", "1")
        monkeypatch.delenv("TOPOS_EXPORT_SEGMENT_CACHE_DIR", raising=False)
        ex = _make_exporter()
        assert ex._segment_cache_config() is None
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE_DIR", str(tmp_path / "cache"))
        cache = ex._segment_cache_config()
        assert cache is not None
        assert cache.root == tmp_path / "cache"

    def test_key_tracks_graph_content_and_output_contract(self, monkeypatch):
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE", "1")
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE_DIR", "ignored-on-key")
        ex = _make_exporter()
        layer = SimpleNamespace(clip_id="c1", asset_id="a1", source_frame=7)
        graph = {"nodes": [{"id": "n1", "type": "text"}]}

        key_a = ex._comp_frame_cache_key("g1", graph, layer)
        key_b = ex._comp_frame_cache_key("g1", dict(graph), layer)
        key_c = ex._comp_frame_cache_key(
            "g1", {"nodes": [{"id": "n1", "type": "text", "text": "changed"}]},
            layer,
        )
        # 内容相同（新 dict 对象）→ 同键；内容变化 → 不同键
        assert key_a.digest == key_b.digest
        assert key_a.digest != key_c.digest

        layer2 = SimpleNamespace(clip_id="c1", asset_id="a1", source_frame=8)
        assert ex._comp_frame_cache_key(
            "g1", graph, layer2,
        ).digest != key_a.digest

    def test_frame_roundtrip_is_lossless(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE", "1")
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE_DIR", str(tmp_path))
        ex = _make_exporter()
        cache = ex._segment_cache_config()
        layer = SimpleNamespace(clip_id="c1", asset_id="a1", source_frame=3)
        key = ex._comp_frame_cache_key("g1", {"v": 1}, layer)

        frame = np.random.default_rng(0).integers(
            0, 255, (108, 192, 3), dtype=np.uint8,
        )
        ex._comp_frame_cache_put(cache, key, frame)
        restored = ex._comp_frame_cache_get(cache, key)
        assert restored is not None
        np.testing.assert_array_equal(restored, frame)

    def test_corrupt_artifact_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE", "1")
        monkeypatch.setenv("TOPOS_EXPORT_SEGMENT_CACHE_DIR", str(tmp_path))
        ex = _make_exporter()
        cache = ex._segment_cache_config()
        layer = SimpleNamespace(clip_id="c1", asset_id="a1", source_frame=3)
        key = ex._comp_frame_cache_key("g1", {"v": 1}, layer)
        cache.put(key, b"not-a-png", {})
        assert ex._comp_frame_cache_get(cache, key) is None

    def test_key_rejects_zero_frame_range(self):
        with pytest.raises(ValueError):
            SegmentRenderKey(
                source_fingerprint="x", in_frame=5, out_frame=5,
                retime={}, layer_stack=(), effects=(), grade=(),
                color_management=(), output_format=(1, 1),
                renderer_backend="b", renderer_version="v",
            )
