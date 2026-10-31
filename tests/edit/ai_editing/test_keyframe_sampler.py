"""TAEP M8 — ShotSpan 派生 + 关键帧抽样 + manifest 测试。

验收（§18.1 / §18.6）：

- 从 scene cut 边界派生 shot span（首个从 0，末个到 total_frames）。
- 代表帧取 shot 中点（避免单帧幻觉）。
- head_mid_tail 关键帧抽样；极短 shot 收敛到 1-2 帧。
- KeyframeManifest 只记帧索引（不含图像/embedding）；config_hash 随策略变化。
- 无 cut → 单 shot 覆盖全片。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.keyframe_sampler import (
    STRATEGY_HEAD_MID_TAIL,
    KeyframeEntry,
    KeyframeManifest,
    ShotSpan,
    StubFrameSampler,
    build_keyframe_manifest,
    derive_shot_spans,
)


def _cuts(*frames: int) -> list[dict]:
    return [{"frame": f, "confidence": 1.0, "kind": "hard_cut"} for f in frames]


# ================================================================
# ShotSpan
# ================================================================


def test_shot_span_duration_and_bounds():
    s = ShotSpan(shot_index=0, start_frame=0, end_frame=100)
    assert s.duration == 100
    assert s.shot_index == 0


def test_shot_span_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        ShotSpan(shot_index=0, start_frame=10, end_frame=10)  # end == start
    with pytest.raises(ValueError):
        ShotSpan(shot_index=0, start_frame=10, end_frame=5)  # end < start
    with pytest.raises(ValueError):
        ShotSpan(shot_index=-1, start_frame=0, end_frame=10)


def test_representative_frame_is_midpoint():
    """代表帧取中点（§18.1：避免单帧幻觉）。"""
    s = ShotSpan(shot_index=0, start_frame=0, end_frame=100)
    assert s.representative_frame() == 50


def test_representative_frame_short_shot_uses_start():
    """极短 shot（duration < 2）用首帧。"""
    s = ShotSpan(shot_index=0, start_frame=10, end_frame=11)
    assert s.representative_frame() == 10


def test_keyframe_indices_head_mid_tail():
    s = ShotSpan(shot_index=0, start_frame=0, end_frame=100)
    kf = s.keyframe_indices()
    assert kf == (0, 50, 99)  # head, mid, tail


def test_keyframe_indices_short_shot_two_frames():
    """2 帧 shot：首 + 尾（2 帧）。"""
    s = ShotSpan(shot_index=0, start_frame=0, end_frame=2)
    assert s.keyframe_indices() == (0, 1)


def test_keyframe_indices_single_frame_shot():
    """单帧 shot（duration=1）：只首帧。"""
    s = ShotSpan(shot_index=0, start_frame=5, end_frame=6)
    assert s.keyframe_indices() == (5,)


def test_keyframe_indices_unsupported_strategy_rejected():
    s = ShotSpan(shot_index=0, start_frame=0, end_frame=10)
    with pytest.raises(ValueError):
        s.keyframe_indices(strategy="even")


# ================================================================
# derive_shot_spans
# ================================================================


def test_derive_shot_spans_from_cuts():
    """scene cuts [0, 100, 200] + total=300 → 3 个 shot。"""
    spans = derive_shot_spans(_cuts(0, 100, 200), total_frames=300)
    assert [(s.shot_index, s.start_frame, s.end_frame) for s in spans] == [
        (0, 0, 100), (1, 100, 200), (2, 200, 300),
    ]


def test_derive_shot_spans_no_cuts_single_shot():
    """无 cut → 单 shot 覆盖全片。"""
    spans = derive_shot_spans([], total_frames=500)
    assert len(spans) == 1
    assert spans[0].start_frame == 0
    assert spans[0].end_frame == 500


def test_derive_shot_spans_dedup_and_sort():
    """重复 / 无序 / 越界 cut 安全。"""
    spans = derive_shot_spans(
        [{"frame": 200}, {"frame": 100}, {"frame": 100}, {"frame": -5}, {"frame": 9999}],
        total_frames=300,
    )
    # -5 和 9999 被过滤；100 去重；边界 {0, 100, 200} → 3 个 shot
    assert [(s.start_frame, s.end_frame) for s in spans] == [
        (0, 100), (100, 200), (200, 300),
    ]


def test_derive_shot_spans_total_zero_empty():
    assert derive_shot_spans(_cuts(10), total_frames=0) == ()


def test_derive_shot_spans_shot_index_sequential():
    spans = derive_shot_spans(_cuts(10, 20, 30), total_frames=40)
    assert [s.shot_index for s in spans] == [0, 1, 2, 3]


# ================================================================
# KeyframeManifest
# ================================================================


def test_manifest_only_records_frame_indices():
    """红线（§18.6）：manifest 不含图像/embedding，只记帧索引。"""
    spans = derive_shot_spans(_cuts(0, 100), total_frames=200)
    m = build_keyframe_manifest(spans, asset_id="a1")
    assert isinstance(m, KeyframeManifest)
    assert m.asset_id == "a1"
    # entries 只含 frame + shot_index，无图像字段
    for e in m.entries:
        assert isinstance(e, KeyframeEntry)
        assert hasattr(e, "frame")
        assert hasattr(e, "shot_index")
        assert not hasattr(e, "image")
        assert not hasattr(e, "embedding")
    d = m.to_dict()
    assert "frame_count" in d
    assert all("image" not in e and "embedding" not in e for e in d["entries"])


def test_manifest_frames_from_two_shots():
    """2 个 shot × 3 关键帧 = 6 帧。"""
    spans = derive_shot_spans(_cuts(0, 100), total_frames=200)
    m = build_keyframe_manifest(spans, asset_id="a1")
    assert len(m.frames) == 6
    # 第一个 shot 的帧在 [0, 100)，第二个在 [100, 200)
    assert all(0 <= f < 100 for f in m.frames[:3])
    assert all(100 <= f < 200 for f in m.frames[3:])


def test_manifest_config_hash_stable_across_same_strategy():
    spans = derive_shot_spans(_cuts(0, 100), total_frames=200)
    m1 = build_keyframe_manifest(spans, asset_id="a1")
    m2 = build_keyframe_manifest(spans, asset_id="a2")  # 不同 asset
    # 同策略 → 同 hash（与 asset 无关）
    assert m1.config_hash == m2.config_hash


def test_manifest_to_dict_shape():
    spans = derive_shot_spans(_cuts(0), total_frames=100)
    m = build_keyframe_manifest(spans, asset_id="a1")
    d = m.to_dict()
    assert d["asset_id"] == "a1"
    assert isinstance(d["entries"], list)
    assert isinstance(d["config_hash"], str)
    assert d["frame_count"] == len(d["entries"])


def test_build_manifest_unsupported_strategy():
    spans = derive_shot_spans(_cuts(0), total_frames=100)
    with pytest.raises(ValueError):
        build_keyframe_manifest(spans, asset_id="a1", strategy="even")


# ================================================================
# StubFrameSampler
# ================================================================


def test_stub_sampler_returns_ndarray_and_records_frames():
    import numpy as np

    stub = StubFrameSampler()
    arr = stub.sample(42)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (2, 2, 3)  # 默认 HxWx3 BGR
    # 记录采样帧（测试断言用）
    stub.sample(7)
    assert stub.sampled_frames == (42, 7)


def test_stub_sampler_deterministic_by_frame():
    """同帧返回同 ndarray（确定性）。"""
    stub = StubFrameSampler()
    a1 = stub.sample(5)
    a2 = stub.sample(5)
    assert (a1 == a2).all()
    # 不同帧不同值
    a3 = stub.sample(99)
    assert not (a1 == a3).all()
