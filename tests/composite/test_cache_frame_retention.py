"""P1-8 frame-scoped cache retention tests.

TL-C03 / P1-8: the timeline preview path used to clear the entire eval
cache every frame, forcing static branches (grade/LUT/mask/text — cache
keys without a frame component, P0-2) to re-render every frame. The
replacement is a frame-scoped sweep (``CacheManager.retain_frame``):
entries not touched at the current frame are evicted (frame-dependent
keys can never hit again once the frame advances; superseded parameter
versions get fresh keys), while entries touched this frame stay warm.

Layers verified here:
1. Unit: sweep semantics against a stub resource pool (no GL).
2. Execution: a static branch stays warm across frames on the real
   timeline-preview path; a parameter change re-renders only the
   affected cone; scrub residue is reclaimed one frame later.
3. Real-video gate: constant→grade branch merged over a real H.264
   source — static-branch recompute must drop to zero on warm frames
   while the video branch keeps re-rendering.
"""

import copy
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core

W, H = 160, 120

_VIDEO = os.path.join(os.path.dirname(__file__), '..',
                      'test_videos', '2K_h264.mp4')
_GATE_ARTIFACT = os.path.join(os.path.dirname(__file__), '..',
                              'media', 'results',
                              'p18_retention_gate.json')


# ── Part 1: unit tests (stub pool, no GL) ────────────────────────────

class _StubPool:
    """Records release_texture_by_id calls; CacheManager needs no more."""

    def __init__(self):
        self.released = []

    def release_texture_by_id(self, tex_id):
        self.released.append(tex_id)


def _entry(tex_id: int):
    from src.features.composite.nodes.base_node import TextureResult
    from src.features.composite.render.cache_manager import CacheEntry
    tex = TextureResult(texture_id=tex_id, width=W, height=H)
    return CacheEntry(outputs={"out": tex}, owned_texture_ids={tex_id})


def _manager():
    from src.features.composite.render.cache_manager import CacheManager
    pool = _StubPool()
    return CacheManager(resource_pool=pool, max_entries=64,
                        max_memory_mb=512.0), pool


class TestRetainFrameSemantics:
    def test_current_frame_entries_are_retained(self):
        cm, pool = _manager()
        cm.set_frame_number(10)
        cm.put("k1", _entry(1))
        assert cm.retain_frame() == 0
        assert cm.get("k1") is not None
        assert pool.released == []

    def test_stale_frame_entries_evicted_and_released(self):
        cm, pool = _manager()
        cm.set_frame_number(10)
        cm.put("old", _entry(1))
        cm.set_frame_number(11)
        cm.put("new", _entry(2))
        evicted = cm.retain_frame()
        assert evicted == 1
        assert cm.get("old") is None          # counts a miss; entry gone
        assert cm.get("new") is not None
        assert pool.released == [1]
        assert cm.stats.evictions >= 1

    def test_hit_refresh_keeps_entry_warm(self):
        cm, pool = _manager()
        cm.set_frame_number(10)
        cm.put("static", _entry(1))
        cm.set_frame_number(11)
        assert cm.get("static") is not None   # touched at frame 11
        assert cm.retain_frame() == 0
        assert cm.get("static") is not None

    def test_source_category_never_swept(self):
        from src.features.composite.render.cache_manager import CacheCategory
        cm, pool = _manager()
        cm.set_frame_number(10)
        cm.put("src", _entry(1), category=CacheCategory.SOURCE)
        cm.set_frame_number(12)
        assert cm.retain_frame() == 0
        assert cm.get("src") is not None

    def test_pinned_entries_respected_until_unpinned(self):
        cm, pool = _manager()
        cm.set_frame_number(10)
        cm.put("p", _entry(1))
        cm.pin("p")
        cm.set_frame_number(11)
        assert cm.retain_frame() == 0         # pinned survives
        assert cm.entry_count() == 1          # (no get(): it would restamp)
        cm.end_frame()                        # pin released
        assert cm.retain_frame() == 1
        assert cm.entry_count() == 0
        assert pool.released == [1]

    def test_explicit_min_frame(self):
        cm, pool = _manager()
        cm.set_frame_number(10)
        cm.put("a", _entry(1))
        cm.put("b", _entry(2))
        assert cm.retain_frame(min_frame=100) == 2
        assert pool.released == [1, 2]
        assert cm.entry_count() == 0


# ── Part 2: execution tests (real GL, timeline-preview path) ─────────

class _StubConfig:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key):
        return self._values.get(key)


def _static_graph(gain_r=1.2):
    return {
        "composite_id": "retention-gate",
        "fps": 25.0, "width": W, "height": H,
        "nodes": [
            {"id": "src", "type": "constant", "position": [0, 0],
             "parameters": {"color_r": 0.7, "color_g": 0.4, "color_b": 0.2,
                            "width": W, "height": H}},
            {"id": "g1", "type": "grade", "position": [160, 0],
             "parameters": {"gain_r": gain_r, "gain_g": 1.0,
                            "gain_b": 0.8}},
            {"id": "out", "type": "output", "position": [320, 0],
             "parameters": {"format_mode": "custom",
                            "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "src", "src_port_name": "result",
             "dst_node_id": "g1", "dst_port_name": "image"},
            {"src_node_id": "g1", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }


@pytest.fixture(scope="module")
def _gl_available():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    ctx.release()


class _Sequence:
    """One renderer, several renders; per-frame cache stat snapshots."""

    def __init__(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer,
        )
        self.renderer = CompositeFrameRenderer(
            config_manager=_StubConfig({"pass_fusion_mode": "off"}))
        self.stats = []

    def render(self, graph, frame):
        img = self.renderer.render_comp_clip_frame(
            graph, timeline_frame=frame, quality="preview")
        assert img is not None, "preview render produced no output"
        # The cache manager is created lazily on first GL init — never
        # cache the reference across the first render.
        cm = self.renderer._get_export_renderer()._cache_manager
        assert cm is not None, "renderer GL init did not create cache"
        s = cm.stats
        self.stats.append({"frame": frame, "hits": s.hits,
                           "misses": s.misses,
                           "entries": cm.entry_count()})
        return img

    def close(self):
        self.renderer.cleanup()


class TestPreviewRetentionExecution:
    def test_static_branch_stays_warm_across_frames(self, qapp,
                                                    _gl_available):
        seq = _Sequence()
        try:
            g = _static_graph()
            img0 = seq.render(g, 0)
            img1 = seq.render(g, 1)
            img2 = seq.render(g, 2)
        finally:
            seq.close()
        # Static content: frames must be bit-identical.
        assert np.array_equal(img0, img1)
        assert np.array_equal(img1, img2)
        # Frames 1 and 2 must produce zero cache misses — the old
        # per-frame clear forced every node to re-render every frame.
        d1 = (seq.stats[1]["hits"] - seq.stats[0]["hits"],
              seq.stats[1]["misses"] - seq.stats[0]["misses"])
        d2 = (seq.stats[2]["hits"] - seq.stats[1]["hits"],
              seq.stats[2]["misses"] - seq.stats[1]["misses"])
        assert d1[1] == 0 and d1[0] > 0, f"frame1 stats {seq.stats}"
        assert d2[1] == 0 and d2[0] > 0, f"frame2 stats {seq.stats}"

    def test_param_change_recomputes_only_affected_cone(self, qapp,
                                                        _gl_available):
        seq = _Sequence()
        try:
            g0 = _static_graph(gain_r=1.2)
            img0 = seq.render(g0, 0)
            g1 = copy.deepcopy(g0)
            for node in g1["nodes"]:
                if node["id"] == "g1":
                    node["parameters"]["gain_r"] = 1.8
            img1 = seq.render(g1, 1)
        finally:
            seq.close()
        # The new parameter must take effect (no stale downstream hit):
        # boosting red gain raises the red channel.
        assert float(img1[..., 2].mean()) > float(img0[..., 2].mean())
        # Only grade + output (the downstream cone) may miss; the
        # unchanged constant must hit.
        d = (seq.stats[1]["hits"] - seq.stats[0]["hits"],
             seq.stats[1]["misses"] - seq.stats[0]["misses"])
        assert d[1] == 2, f"expected 2 misses (grade+output): {seq.stats}"
        assert d[0] >= 1, f"constant node should hit: {seq.stats}"

    def test_scrub_residue_reclaimed_next_frame(self, qapp, _gl_available):
        seq = _Sequence()
        try:
            seq.render(_static_graph(gain_r=1.2), 0)   # gain A
            seq.render(_static_graph(gain_r=1.5), 0)   # gain B, same frame
            seq.render(_static_graph(gain_r=1.5), 1)   # advance
        finally:
            seq.close()
        # Gain-A entries were not touched after the frame advanced and
        # must be swept; only the current working set remains.
        final = seq.stats[2]["entries"]
        assert final <= 4, f"scrub residue not reclaimed: {seq.stats}"


# ── Part 3: real-video gate (P0-6) ───────────────────────────────────

def _video_graph(video_path):
    return {
        "composite_id": "retention-video-gate",
        "fps": 25.0, "width": W, "height": H,
        "nodes": [
            {"id": "c1", "type": "constant", "position": [0, 0],
             "parameters": {"color_r": 0.5, "color_g": 0.5,
                            "color_b": 0.5, "width": W, "height": H}},
            {"id": "g1", "type": "grade", "position": [160, 0],
             "parameters": {"gain_r": 1.1, "gain_g": 0.9,
                            "gain_b": 1.0}},
            {"id": "v1", "type": "video_frame", "position": [0, 200],
             "parameters": {"file_path": video_path}},
            {"id": "bl", "type": "blend", "position": [320, 100],
             "parameters": {"blend_mode": "over", "opacity": 1.0,
                            "mix": 1.0}},
            {"id": "out", "type": "output", "position": [480, 100],
             "parameters": {"format_mode": "custom",
                            "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "c1", "src_port_name": "result",
             "dst_node_id": "g1", "dst_port_name": "image"},
            {"src_node_id": "g1", "src_port_name": "result",
             "dst_node_id": "bl", "dst_port_name": "A"},
            {"src_node_id": "v1", "src_port_name": "image",
             "dst_node_id": "bl", "dst_port_name": "B"},
            {"src_node_id": "bl", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }


class TestRealVideoRetentionGate:
    def test_static_branch_zero_recompute_over_real_video(self, qapp,
                                                          _gl_available):
        if not os.path.exists(_VIDEO):
            pytest.skip(f"real test video missing: {_VIDEO}")
        seq = _Sequence()
        per_frame = []
        imgs = []
        try:
            g = _video_graph(_VIDEO)
            for frame in range(8):
                img = seq.render(g, frame)
                imgs.append(
                    img.astype(np.float32).mean(axis=(0, 1)).tolist())
        finally:
            seq.close()
        # Real video must actually decode: not all frames identical.
        means = np.array(imgs)
        assert float(np.ptp(means[:, :3])) > 1e-4, \
            "video frames identical — decode is not advancing"
        # Warm frames: static branch (constant+grade) must hit while
        # only the video cone (blend+output) recomputes.
        gate_rows = []
        for prev, cur in zip(seq.stats, seq.stats[1:]):
            hits = cur["hits"] - prev["hits"]
            misses = cur["misses"] - prev["misses"]
            gate_rows.append({"frame": cur["frame"], "hits": hits,
                              "misses": misses})
            assert misses <= 2, (
                f"frame {cur['frame']}: static branch re-rendering "
                f"(misses={misses}, expected <=2 for blend+output)")
            assert hits >= 2, (
                f"frame {cur['frame']}: static branch not cached "
                f"(hits={hits}, expected >=2 for constant+grade)")
        os.makedirs(os.path.dirname(_GATE_ARTIFACT), exist_ok=True)
        with open(_GATE_ARTIFACT, "w", encoding="utf-8") as fh:
            json.dump({"video": os.path.basename(_VIDEO),
                       "frames": len(seq.stats),
                       "per_frame": gate_rows}, fh, indent=1)
