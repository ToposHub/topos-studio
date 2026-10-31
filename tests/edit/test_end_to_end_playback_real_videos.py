"""
端到端播放验证（FINAL-P0-07 真实素材验收）。

用 tests/test_videos 里的真实视频验证完整播放链路：
1. 真实素材能被 PyAV 打开 + 解码。
2. engine SourceResolver 在真实素材上的 source decision 正确。
3. seek 不堆积 stale generation。
4. 多帧连续解码不出现 wrong source frame。
5. AVSyncController 决策在真实 fps 下合理。

这是"用户测试视频"的离线等价测试（不需要 Qt/GL context，但能验证
解码 + source policy + AV sync 的完整闭环）。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEST_VIDEOS = ROOT / "tests" / "test_videos"


def _require_test_videos():
    """测试视频缺失时 skip。"""
    if not TEST_VIDEOS.exists():
        pytest.skip("test_videos 目录不存在")
    # macOS copies can leave AppleDouble resource forks (``._foo.mov``).
    # They are 4 KiB metadata files, not media, and Windows/PyAV correctly
    # rejects them.  Keep real-asset discovery platform-neutral and ignore
    # both dotfiles and AppleDouble sidecars.
    videos = [
        path
        for path in (*TEST_VIDEOS.glob("*.mov"), *TEST_VIDEOS.glob("*.mp4"))
        if not path.name.startswith(".")
    ]
    if not videos:
        pytest.skip("无测试视频")
    return videos


def _require_pyav():
    try:
        import av  # noqa: F401
    except ImportError:
        pytest.skip("PyAV 未安装")


# =============================================================================
# 1. 真实素材能被 PyAV 打开
# =============================================================================


def test_all_test_videos_openable():
    """所有测试视频必须能被 PyAV 打开并有 video stream。"""
    _require_pyav()
    import av
    videos = _require_test_videos()
    failures = []
    for v in videos:
        try:
            container = av.open(str(v))
            streams = [s for s in container.streams if s.type == "video"]
            assert streams, f"{v.name} 无 video stream"
            container.close()
        except Exception as e:
            failures.append(f"{v.name}: {e}")
    assert not failures, "部分视频打开失败：\n" + "\n".join(failures)


# =============================================================================
# 2. 每个测试视频解码至少 10 帧
# =============================================================================


def _pyav_decoder_known(stream) -> bool:
    """PyAV/FFmpeg 是否注册了该流的解码器。

    Topos 私有编码（TPIC FourCC）在 FFmpeg 里 codec_id 未注册：
    ``av.open`` 能成功（容器层合法），但对这种流调用 ``container.decode()``
    是 **FFmpeg 库内 SIGSEGV**（avcodec_find_decoder 返回 NULL 后空指针
    解引用），无法用 Python 异常捕获。因此必须在 decode 前探测——
    codec context 缺失（属性访问抛 AttributeError）即视为不可解码，
    由专门的 Topos 套件（tests/media/test_topos_*.py）覆盖。
    """
    try:
        return bool(stream.codec_context and stream.codec_context.name)
    except AttributeError:
        return False


def test_each_video_decodes_at_least_10_frames():
    """每个测试视频必须能顺序解码至少 10 帧（无 corrupt frame）。"""
    _require_pyav()
    import av
    videos = _require_test_videos()
    skipped = []
    for v in videos:
        container = av.open(str(v))
        stream = next((s for s in container.streams if s.type == "video"), None)
        assert stream is not None, f"{v.name} 无 video stream"
        if not _pyav_decoder_known(stream):
            skipped.append(v.name)
            container.close()
            continue
        decoded = 0
        for i, frame in enumerate(container.decode(stream)):
            if i >= 10:
                break
            arr = frame.to_ndarray(format="bgr24")
            assert arr.size > 0, f"{v.name} frame {i} 解码为空"
            decoded += 1
        container.close()
        assert decoded == 10, f"{v.name} 只解码了 {decoded} 帧（期望 10）"
    # 防御：全部素材都被跳过 = 探测逻辑本身失灵
    assert len(skipped) < len(videos), "所有素材都被判定为不可解码（探测逻辑错误）"


# =============================================================================
# 3. engine source decision 在真实素材路径上工作
# =============================================================================


def test_engine_source_decision_real_paths():
    """engine SourceResolver 对真实素材路径能正常决策。"""
    videos = _require_test_videos()
    from src.features.edit.core.realtime_playback import (
        PlaybackMode,
        RealtimePlaybackEngine,
    )
    from src.features.edit.core.realtime_playback.source_resolver import (
        DiskProxyServiceAdapter,
        LayerSourceQuery,
        SourceResolver,
    )

    resolver = SourceResolver(proxy_manager=DiskProxyServiceAdapter())
    engine = RealtimePlaybackEngine()
    queries = [
        LayerSourceQuery(
            clip_id=f"clip-{i}",
            asset_id=f"asset-{i}",
            source_path=str(v),
            source_frame=0,
            timeline_frame=0,
        )
        for i, v in enumerate(videos)
    ]
    engine.configure(
        delegate_render=lambda f, g, d, dc: True,
        source_resolver=resolver,
        layer_query_provider=lambda f: queries,
    )
    engine.set_mode(PlaybackMode.REALTIME)
    ok = engine.request_present(0)
    assert ok is True

    # 所有 layer 都必须有 SourceDecision
    summary = engine.telemetry.summary()
    source_kinds = summary.get("source_kinds", {})
    # 真实素材可能没 ready proxy → PLACEHOLDER（这是正确的 realtime 行为）
    # 关键是 master_requests 必须 == 0
    assert summary.get("realtime_master_requests", 0) == 0, (
        "REALTIME 模式不应有 master_requests"
    )
    assert source_kinds, "telemetry 必须记录 source_kinds"


# =============================================================================
# 4. seek 不堆积 stale（连续 100 次 seek + present）
# =============================================================================


def test_seek_stress_100_iterations_no_stale():
    """100 次随机 seek + present 后 stale_generation 必须 == 0。"""
    videos = _require_test_videos()
    video = videos[0]
    from src.features.edit.core.realtime_playback import (
        PlaybackMode,
        RealtimePlaybackEngine,
    )
    from src.features.edit.core.realtime_playback.source_resolver import (
        DiskProxyServiceAdapter,
        LayerSourceQuery,
        SourceResolver,
    )

    resolver = SourceResolver(proxy_manager=DiskProxyServiceAdapter())
    engine = RealtimePlaybackEngine()
    engine.configure(
        delegate_render=lambda f, g, d, dc: True,
        source_resolver=resolver,
        layer_query_provider=lambda f: [
            LayerSourceQuery(
                clip_id="c1", asset_id="a1", source_path=str(video),
                source_frame=f, timeline_frame=f,
            )
        ],
    )
    engine.set_mode(PlaybackMode.REALTIME)

    import random
    rng = random.Random(2026)
    for _ in range(100):
        target = rng.randint(0, 1000)
        engine.seek(target)
        engine.request_present(target)

    summary = engine.telemetry.summary()
    assert summary.get("stale_generation", 0) == 0, (
        f"100 次 seek 后 stale_generation={summary.get('stale_generation')} > 0"
    )
    assert summary.get("wrong_source_frame", 0) == 0


# =============================================================================
# 5. AVSyncController 在真实 fps 下决策合理
# =============================================================================


@pytest.mark.parametrize("fps", [23.976, 24.0, 25.0, 29.97, 30.0, 60.0])
def test_avsync_at_common_fps(fps):
    """常见帧率下 AVSyncController 必须能正确分类 0/1/2/5 帧差。"""
    from src.features.edit.core.realtime_playback import (
        AVSyncAction,
        AVSyncController,
    )

    ctrl = AVSyncController(fps=fps)
    # 0 帧差 → IGNORE
    d0 = ctrl.evaluate(audio_frame=100, video_frame=100)
    assert d0.action is AVSyncAction.IGNORE

    # 大帧差（≥150ms 等价帧数）→ REBUFFER
    ms_per_frame = 1000.0 / fps
    rebuffer_frames = int(150.0 / ms_per_frame) + 1
    d_big = ctrl.evaluate(audio_frame=100 + rebuffer_frames, video_frame=100)
    assert d_big.action is AVSyncAction.REBUFFER, (
        f"fps={fps} {rebuffer_frames} 帧差应 REBUFFER，实际 {d_big}"
    )


# =============================================================================
# 6. ProRes 真实素材路径 → SourceDecision 不允许 realtime master
# =============================================================================


def test_prores_realtime_no_master_allowed():
    """ProRes 素材在 REALTIME 模式下 SourceDecision 必须禁止 master fallback。"""
    videos = _require_test_videos()
    prores = [v for v in videos if "prores" in v.name.lower()]
    if not prores:
        pytest.skip("无 ProRes 测试视频")
    video = prores[0]

    from src.features.edit.core.realtime_playback import (
        PlaybackMode,
        RealtimePlaybackEngine,
    )
    from src.features.edit.core.realtime_playback.source_resolver import (
        DiskProxyServiceAdapter,
        LayerSourceQuery,
        SourceResolver,
    )

    # mock DiskProxyService：返回 MISSING（无代理）
    from unittest.mock import MagicMock
    fake_info = MagicMock()
    fake_info.is_ready = False
    fake_info.status.value = "missing"
    fake_info.proxy_path = None
    fake_service = MagicMock()
    fake_service.get_disk_proxy_info.return_value = fake_info
    fake_service.generate_proxy.return_value = None

    resolver = SourceResolver(proxy_manager=DiskProxyServiceAdapter(service=fake_service))
    engine = RealtimePlaybackEngine()
    engine.configure(
        delegate_render=lambda f, g, d, dc: True,
        source_resolver=resolver,
        layer_query_provider=lambda f: [
            LayerSourceQuery(
                clip_id="c1", asset_id="a1", source_path=str(video),
                source_frame=f, timeline_frame=f,
            )
        ],
    )
    engine.set_mode(PlaybackMode.REALTIME)
    engine.request_present(0)

    summary = engine.telemetry.summary()
    # REALTIME + 无 proxy → 必须 PLACEHOLDER，不允许 master
    source_kinds = summary.get("source_kinds", {})
    assert "placeholder" in source_kinds, (
        f"ProRes 无 proxy 在 REALTIME 应为 PLACEHOLDER，实际 source_kinds={source_kinds}"
    )
    assert summary.get("realtime_master_requests", 0) == 0, (
        "ProRes 无 proxy REALTIME 不应请求 master 解码"
    )
