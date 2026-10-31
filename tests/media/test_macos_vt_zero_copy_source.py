from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.shared.media.macos_vt.source import MacOSVtZeroCopySource


_darwin_only_w01 = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS 专属逻辑（VT/Metal）；W01 复跑确认在 Windows 为测试缺平台门"
)

class _FakeDemuxer:
    def __init__(self) -> None:
        self.seeked_to: int | None = None

    def seek_to_frame(self, frame_index: int) -> None:
        self.seeked_to = frame_index


def test_read_frame_decodes_forward_after_keyframe_seek(monkeypatch):
    source = MacOSVtZeroCopySource()
    source._is_open = True
    source._demuxer = _FakeDemuxer()
    released = []
    source._vt_session = SimpleNamespace(
        flush=lambda: None,
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )
    source._current_frame = 10

    frames = iter([
        SimpleNamespace(
            frame_index=143, pts=143, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=144, pts=144, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=145, pts=145, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=146, pts=146, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=147, pts=147, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
    ])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))

    frame = source.read_frame_pixel_buffer(147)

    assert source._demuxer.seeked_to == 147
    assert frame.frame_index == 147
    frame.release()
    assert 147 in released


def test_interactive_deadline_returns_nearby_buffered_frame(monkeypatch):
    source = MacOSVtZeroCopySource()
    source._is_open = True
    released = []
    source._vt_session = SimpleNamespace(
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )

    frames = iter([
        SimpleNamespace(
            frame_index=105, pts=105, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
    ])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))
    perf_values = iter([0.0, 2.0])
    monkeypatch.setattr(
        "src.shared.media.macos_vt.source.time.perf_counter",
        lambda: next(perf_values, 2.0),
    )

    frame = source._decode_until_present_frame(
        100,
        deadline_at=1.0,
        deadline_nearest_max_distance=12,
    )

    assert frame is not None
    assert frame.frame_index == 105
    frame.release()
    assert released == [105]


def test_interactive_deadline_returns_nearby_frame_before_target(monkeypatch):
    source = MacOSVtZeroCopySource()
    source._is_open = True
    released = []
    source._vt_session = SimpleNamespace(
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )

    frames = iter([
        SimpleNamespace(
            frame_index=95, pts=95, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
    ])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))
    perf_values = iter([0.0, 2.0])
    monkeypatch.setattr(
        "src.shared.media.macos_vt.source.time.perf_counter",
        lambda: next(perf_values, 2.0),
    )

    frame = source._decode_until_present_frame(
        100,
        deadline_at=1.0,
        deadline_nearest_max_distance=12,
    )

    assert frame is not None
    assert frame.frame_index == 95
    frame.release()
    assert released == [95]


def test_interactive_deadline_rejects_far_buffered_frame(monkeypatch):
    source = MacOSVtZeroCopySource()
    source._is_open = True
    source._vt_session = SimpleNamespace(release_decoded_frame=lambda _decoded: None)

    frames = iter([
        SimpleNamespace(
            frame_index=140, pts=140, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
    ])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))
    perf_values = iter([0.0, 2.0])
    monkeypatch.setattr(
        "src.shared.media.macos_vt.source.time.perf_counter",
        lambda: next(perf_values, 2.0),
    )

    frame = source._decode_until_present_frame(
        100,
        deadline_at=1.0,
        deadline_nearest_max_distance=12,
    )

    assert frame is None


def _real_h265_path() -> Path:
    return Path(__file__).resolve().parents[1] / "test_videos" / "4K_H265.mov"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS VT test requires macOS")
def test_h265_b_frames_are_returned_in_presentation_order(monkeypatch):
    import src.shared.media.macos_vt.capability as capability

    monkeypatch.setattr(capability, "is_zero_copy_available", lambda **_kwargs: True)
    monkeypatch.setattr(
        capability,
        "get_zero_copy_capability",
        lambda: SimpleNamespace(available=True, supported_codecs={'h264', 'hevc', 'h265'}),
    )
    path = _real_h265_path()
    if not path.exists():
        pytest.skip(f"missing test video: {path}")

    source = MacOSVtZeroCopySource()
    assert source.open(str(path))
    frames = []
    try:
        for _ in range(12):
            frame = source.read_next_pixel_buffer()
            assert frame is not None
            frames.append(frame.frame_index)
            frame.release()
    finally:
        source.close()

    assert frames == list(range(12))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS VT test requires macOS")
def test_h265_random_reads_wait_for_exact_reordered_frame(monkeypatch):
    import src.shared.media.macos_vt.capability as capability

    monkeypatch.setattr(capability, "is_zero_copy_available", lambda **_kwargs: True)
    monkeypatch.setattr(
        capability,
        "get_zero_copy_capability",
        lambda: SimpleNamespace(available=True, supported_codecs={'h264', 'hevc', 'h265'}),
    )
    path = _real_h265_path()
    if not path.exists():
        pytest.skip(f"missing test video: {path}")

    source = MacOSVtZeroCopySource()
    assert source.open(str(path))
    try:
        for target in (10, 30):
            frame = source.read_frame_pixel_buffer(target)
            assert frame is not None
            try:
                assert frame.frame_index == target
            finally:
                frame.release()
    finally:
        source.close()


def test_interactive_coarse_returns_first_keyframe_frame(monkeypatch):
    """Coarse fast-drag scrub decodes one keyframe-anchored frame, no walk."""
    source = MacOSVtZeroCopySource()
    source._is_open = True
    source._demuxer = _FakeDemuxer()
    released = []
    source._vt_session = SimpleNamespace(
        flush=lambda: None,
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )
    source._current_frame = 10

    # After seeking to 147 the demuxer restarts at the keyframe; exactly one
    # decode must happen regardless of how far the target is beyond it.
    frames = iter([
        SimpleNamespace(
            frame_index=125, pts=125, time_seconds=0.0, width=3840, height=2160,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=126, pts=126, time_seconds=0.0, width=3840, height=2160,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
    ])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))

    frame = source.read_frame_pixel_buffer_interactive(147, coarse=True)

    assert source._demuxer.seeked_to == 147
    assert frame is not None
    assert frame.frame_index == 125  # keyframe, not the exact target
    assert source._current_frame == 126
    frame.release()
    assert released == [125]


def test_interactive_coarse_nearby_gap_stays_sequential(monkeypatch):
    """Coarse mode still decodes sequentially when the cursor is on target."""
    source = MacOSVtZeroCopySource()
    source._is_open = True
    source._demuxer = _FakeDemuxer()
    source._vt_session = SimpleNamespace(
        flush=lambda: None,
        release_decoded_frame=lambda decoded: None,
    )
    source._current_frame = 100

    frames = iter([
        SimpleNamespace(
            frame_index=100, pts=100, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=101, pts=101, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
        SimpleNamespace(
            frame_index=102, pts=102, time_seconds=0.0, width=1920, height=1080,
            pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
        ),
    ])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))

    frame = source.read_frame_pixel_buffer_interactive(102, coarse=True)

    assert source._demuxer.seeked_to is None  # gap <= 2: no seek
    assert frame is not None and frame.frame_index == 102
    frame.release()


def test_vt_session_rejects_prores_without_known_tag():
    """ProRes sessions require a known container fourcc tag."""
    from src.shared.media.macos_vt.vt_session import VtSession

    sess = VtSession()
    assert sess.create(width=3840, height=2160, codec="prores",
                       pixel_format="p010", extradata=None, codec_tag="zzzz") is False
    assert sess.create(width=3840, height=2160, codec="dnxhd",
                       pixel_format="p010", extradata=b"\x00" * 24) is False


@_darwin_only_w01
def test_vt_session_prores_format_description_uses_fourcc(monkeypatch):
    """ProRes format descriptions come from the fourcc, not extradata."""
    import sys
    import types

    from src.shared.media.macos_vt.vt_session import VtSession

    sess = VtSession()
    sess._codec_tag = "apch"
    created = {}

    def fake_cmd(allocator, fourcc, width, height, extensions, out):
        created["fourcc"] = fourcc
        created["size"] = (width, height)
        return 0, object()

    fake_cm = types.ModuleType("CoreMedia")
    fake_cm.CMVideoFormatDescriptionCreate = fake_cmd
    monkeypatch.setitem(sys.modules, "CoreMedia", fake_cm)

    fd = sess._create_format_description("prores", None, 3840, 2160)
    assert fd is not None
    assert created["fourcc"] == 0x61706368  # 'apch' -> ProRes 422 HQ
    assert created["size"] == (3840, 2160)


def test_capability_defaults_include_prores():
    """The zero-copy capability advertises ProRes support by default."""
    from src.shared.media.macos_vt.capability import (
        MacOSZeroCopyCapability,
        _PERSISTENT_CACHE_SCHEMA_VERSION,
    )

    cap = MacOSZeroCopyCapability()
    assert "prores" in cap.supported_codecs
    # Schema bump invalidates pre-ProRes persistent caches.
    assert _PERSISTENT_CACHE_SCHEMA_VERSION >= 3


def test_prores_routes_to_native_vt():
    """ProRes uses native VT decode even for smaller preview targets."""
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager()
    mgr._probe_codec = lambda path: "prores"
    mgr._probe_resolution = lambda path: (3840, 2160)
    assert mgr._should_use_vt_planar_source("x.mov", (1920, 1080)) is True
    assert mgr._should_use_vt_planar_source("x.mov", None) is True


# ---------------------------------------------------------------------------
# reorder buffer 淘汰机制（修复"持续向前拖拽越拖越慢"的 IOSurface 滞留）
# ---------------------------------------------------------------------------

def _make_frame(source: MacOSVtZeroCopySource, idx: int) -> "VtPixelBufferFrame":
    return source._make_pixel_buffer_frame(SimpleNamespace(
        frame_index=idx, pts=idx, time_seconds=0.0, width=1920, height=1080,
        pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
    ))


def test_reorder_prune_keeps_nearest_candidates_only():
    """低于 floor（target-2）的死条目被释放，±2 邻近候选保留。"""
    source = MacOSVtZeroCopySource()
    released = []
    source._vt_session = SimpleNamespace(
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )
    # 模拟 deadline 中止后留下的近邻候选 + 旧残留（直接入 buffer，
    # 与追帧循环里 deadline 候选分支的写入方式一致）
    for idx in (90, 98, 99, 100, 101):
        source._reorder_buffer[idx] = _make_frame(source, idx)

    source._prune_reorder_buffer(target_frame=100)

    # floor = 100-2 = 98：90 被释放；98~101 是当前 walk 的可用候选
    assert sorted(source._reorder_buffer) == [98, 99, 100, 101]
    assert released == [90]


def test_reorder_prune_next_walk_releases_previous_residue():
    """下一次追帧会释放上一轮 deadline 中止留下的全部残留。"""
    source = MacOSVtZeroCopySource()
    released = []
    source._vt_session = SimpleNamespace(
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )
    for idx in (99, 100, 101):
        source._reorder_buffer[idx] = _make_frame(source, idx)

    # 用户继续向前拖到 130：旧残留（< 128）全部过期
    source._prune_reorder_buffer(target_frame=130)

    assert source._reorder_buffer == {}
    assert sorted(released) == [99, 100, 101]


def test_reorder_hard_cap_evicts_oldest_future_frames():
    """异常 GOP 结构下硬上限生效，按最旧帧号淘汰。"""
    source = MacOSVtZeroCopySource()
    released = []
    source._vt_session = SimpleNamespace(
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )

    # 一次性向 buffer 塞入超过上限的未来帧（构造深重排窗口场景）
    for idx in range(200, 220):
        source._reorder_buffer[idx] = _make_frame(source, idx)
    source._prune_reorder_buffer(target_frame=150)

    assert len(source._reorder_buffer) == MacOSVtZeroCopySource._REORDER_BUFFER_MAX
    # 淘汰最旧的 4 帧
    assert sorted(released) == [200, 201, 202, 203]
    assert sorted(source._reorder_buffer) == list(range(204, 220))


def test_forward_walk_prunes_stale_entries_from_deadline_aborts(monkeypatch):
    """端到端：deadline 中止留下的残留不会被下一次顺序追帧无限滞留。"""
    source = MacOSVtZeroCopySource()
    source._is_open = True
    released = []
    source._vt_session = SimpleNamespace(
        release_decoded_frame=lambda decoded: released.append(decoded.frame_index),
    )

    # 第一轮：目标 100，deadline 立即到达，只解出 99（T-1 候选）
    frames = iter([SimpleNamespace(
        frame_index=99, pts=99, time_seconds=0.0, width=1920, height=1080,
        pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
    )])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames, None))
    perf_values = iter([0.0, 2.0])
    monkeypatch.setattr(
        "src.shared.media.macos_vt.source.time.perf_counter",
        lambda: next(perf_values, 2.0),
    )
    frame = source._decode_until_present_frame(
        100, deadline_at=1.0, deadline_nearest_max_distance=2)
    assert frame is not None and frame.frame_index == 99
    frame.release()
    # deadline 弹走 99 后 buffer 应为空（无残留）
    assert source._reorder_buffer == {}

    # 第二轮：目标 130，解出 131（超前的未来帧入 buffer），随后 EOF
    frames2 = iter([SimpleNamespace(
        frame_index=131, pts=131, time_seconds=0.0, width=1920, height=1080,
        pixel_format="nv12", bit_depth=8, pixel_buffer=object(),
    )])
    monkeypatch.setattr(source, "_read_next_decoded_pixel_buffer", lambda: next(frames2, None))
    perf_values2 = iter([0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(
        "src.shared.media.macos_vt.source.time.perf_counter",
        lambda: next(perf_values2, 2.0),
    )
    frame2 = source._decode_until_present_frame(
        130, deadline_at=None, deadline_nearest_max_distance=2)
    # 无 deadline：EOF 后 nearest>=target 兜底返回 131
    assert frame2 is not None and frame2.frame_index == 131
    frame2.release()
    assert source._reorder_buffer == {}


def test_vt_session_pending_decode_refs_drain_releases_after_threshold(monkeypatch):
    """refs 超阈值时等待异步帧完成后批量 CFRelease，阈值以下零开销。"""
    import ctypes

    from src.shared.media.macos_vt import vt_session as vt_module

    released = []
    monkeypatch.setattr(vt_module, "_CF_LIB", SimpleNamespace(
        CFRelease=lambda p: released.append(p)))
    monkeypatch.setattr(vt_module, "_VT_LIB", SimpleNamespace(
        VTDecompressionSessionWaitForAsynchronousFrames=lambda s: None))

    session = vt_module.VtSession()
    session._destroyed = False
    session._ctypes_session = ctypes.c_void_p(0x1)

    for i in range(200):
        session._pending_decode_refs.append(ctypes.c_void_p(i))

    session._drain_pending_decode_refs(threshold=128)

    assert session._pending_decode_refs == []
    assert len(released) == 200

    # 阈值以下不触发等待/释放
    session._pending_decode_refs.append(ctypes.c_void_p(999))
    session._drain_pending_decode_refs(threshold=128)
    assert len(session._pending_decode_refs) == 1
    assert 999 not in released
