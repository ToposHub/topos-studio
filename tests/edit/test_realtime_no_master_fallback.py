"""
Stage C2 integration tests: realtime playback master fallback prohibition.
对应规格书 §15.2 / §20.2 test_edit_realtime_no_master_fallback.py。

核心验收：播放状态下 _get_video_frame 只请求 proxy，不 fallback master。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, call

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_preview_renderer import (
    PreviewRenderWorker,
    AssetResolver,
)
from src.features.edit.core.project import EditProject, MediaAsset


# =============================================================================
# Fixtures（与 test_timeline_preview_renderer.py 对齐）
# =============================================================================


@pytest.fixture
def mock_asset():
    return MediaAsset(
        asset_id="asset-001",
        path="/path/to/video.mp4",
        media_type="video",
        display_name="Video.mp4",
        metadata={
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "total_frames": 100,
            "duration_seconds": 4.0,
        },
    )


@pytest.fixture
def mock_project(mock_asset):
    project = EditProject.create("Test Project")
    project = project.import_asset(
        path=mock_asset.path,
        media_type=mock_asset.media_type,
        metadata=mock_asset.metadata,
    )
    return project


@pytest.fixture
def make_worker():
    """Factory: create a PreviewRenderWorker with a mock provider."""
    workers = []

    def _make(provider, resolver):
        w = PreviewRenderWorker(
            frame_provider=provider,
            asset_resolver=resolver,
            video_renderer=None,
        )
        workers.append(w)
        return w

    yield _make

    for w in workers:
        try:
            w.stop()
        except Exception:
            pass


class TestRealtimeMasterFallbackProhibited:
    """规格书 §15.2：实时播放中 trace 不出现 master fallback。"""

    def test_realtime_only_requests_proxy(self, mock_project, make_worker) -> None:
        """播放状态下 proxy miss 不应触发 master 请求。"""
        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.return_value = "session-a"
        # proxy 返回 None（miss）
        provider.request_frame.return_value = None

        worker = make_worker(provider, resolver)
        # 标记为实时播放 render
        worker._current_render_realtime = True

        asset = list(mock_project.assets.values())[0]
        result = worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a")

        # proxy miss + 禁止 master → 返回 None（上层 hold/placeholder）
        assert result is None
        # 只有一次 proxy 请求，且 allow_master_fallback=False
        assert provider.request_frame.call_count == 1
        request_call = provider.request_frame.call_args
        assert request_call.kwargs.get("quality") == "proxy"
        assert request_call.kwargs.get("allow_master_fallback") is False

    def test_realtime_proxy_hit_no_master(self, mock_project, make_worker) -> None:
        """播放状态下 proxy 命中，不请求 master。"""
        from src.shared.media.frame import DecodedFrame

        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.return_value = "session-a"
        proxy_frame = DecodedFrame(
            frame_index=10,
            pts=10,
            time_seconds=10.0 / 25.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        provider.request_frame.return_value = proxy_frame

        worker = make_worker(provider, resolver)
        worker._current_render_realtime = True

        asset = list(mock_project.assets.values())[0]
        result = worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a")

        assert result is not None
        assert provider.request_frame.call_count == 1
        assert provider.request_frame.call_args.kwargs.get("allow_master_fallback") is False

    def test_non_realtime_keeps_master_fallback(self, mock_project, make_worker) -> None:
        """非播放状态（暂停/逐帧/静态预览）保留 proxy→master fallback。"""
        from src.shared.media.frame import DecodedFrame

        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.return_value = "session-a"
        # proxy miss, master hit
        master_frame = DecodedFrame(
            frame_index=10,
            pts=10,
            time_seconds=10.0 / 25.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        provider.request_frame.side_effect = [None, master_frame]

        worker = make_worker(provider, resolver)
        # 非实时（默认）
        worker._current_render_realtime = False

        asset = list(mock_project.assets.values())[0]
        result = worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a")

        # paused/exact 路径保留 master fallback
        assert result is not None
        assert provider.request_frame.call_args_list == [
            call("session-a", 10, quality="proxy"),
            call("session-a", 10, quality="master"),
        ]

    def test_realtime_rejects_wrong_frame_no_master_retry(self, mock_project, make_worker) -> None:
        """播放状态下解码错误帧（mismatch > 1）返回 None，不重试 master。"""
        from src.shared.media.frame import DecodedFrame

        resolver = AssetResolver(mock_project)
        provider = Mock()
        provider.open_clip.return_value = "session-a"
        # proxy 返回严重错帧
        wrong_proxy = DecodedFrame(
            frame_index=5,  # requested 10, mismatch=5 > 1
            pts=5,
            time_seconds=5.0 / 25.0,
            data=np.zeros((8, 8, 3), dtype=np.uint8),
            pixel_format="bgr24",
        )
        provider.request_frame.return_value = wrong_proxy

        worker = make_worker(provider, resolver)
        worker._current_render_realtime = True

        asset = list(mock_project.assets.values())[0]
        result = worker._get_video_frame(asset.path, 10, asset.asset_id, "clip-a")

        # 错帧被拒绝，不重试 master
        assert result is None
        assert provider.request_frame.call_count == 1
        assert provider.request_frame.call_args.kwargs.get("allow_master_fallback") is False
