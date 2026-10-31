"""
Final-6 (FINAL-P1-12): Proxy/cache product loop。

验收点：
1. MediaPoolPanel._maybe_auto_ensure_disk_proxy 对 video 类型素材触发
   DiskProxyService.generate_proxy。
2. audio/image/offline/已 ready/已 generating 的素材跳过。
3. SourceResolver.ensure_proxy 在 proxy 缺失时触发后台生成（已在 Final-1 覆盖，
   这里做端到端验证）。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


def _make_asset(media_type: str = "video", *, offline: bool = False, path: str = "/fake/a.mov"):
    asset = MagicMock()
    asset.media_type = media_type
    asset.is_offline = offline
    asset.path = path
    asset.asset_id = "asset-1"
    # 真实尺寸（P2-02：策略对未知/非法尺寸显式 skip dimensions_unknown；
    # MagicMock 的隐式 int() 会把 width 变成 1，曾让该用例假性失败）。
    asset.width = 3840
    asset.height = 2160
    asset.metadata = {"codec": "prores"}
    return asset


# =============================================================================
# 1. _maybe_auto_ensure_disk_proxy 触发生成
# =============================================================================


def test_auto_ensure_proxy_triggers_for_video():
    """video 类型素材必须触发 generate_proxy。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("video")
    fake_info = MagicMock()
    fake_info.is_ready = False
    fake_info.status.value = "missing"

    fake_service = MagicMock()
    fake_service.get_disk_proxy_info.return_value = fake_info
    fake_service.generate_proxy.return_value = None

    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    # QWidget 子类未调 __init__ 时读类上不存在的属性会抛 RuntimeError
    # （sip 保护）并被 _maybe_auto_ensure_disk_proxy 的 try 吞掉——
    # 显式补项目属性（None = 默认策略），让被测路径真正可达。
    panel._project = None
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=fake_service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    fake_service.get_disk_proxy_info.assert_called_once_with("/fake/a.mov")
    fake_service.generate_proxy.assert_called_once()
    call_kwargs = fake_service.generate_proxy.call_args
    assert call_kwargs.kwargs["source_path"] == "/fake/a.mov"
    assert call_kwargs.kwargs["asset_id"] == "asset-1"


def test_auto_ensure_proxy_skips_audio():
    """audio 类型素材不应触发 proxy 生成。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("audio")
    fake_service = MagicMock()
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=fake_service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    fake_service.generate_proxy.assert_not_called()


def test_auto_ensure_proxy_skips_image():
    """image 类型素材不应触发 proxy 生成。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("image")
    fake_service = MagicMock()
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=fake_service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    fake_service.generate_proxy.assert_not_called()


def test_auto_ensure_proxy_skips_offline():
    """offline 素材不应触发 proxy 生成。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("video", offline=True)
    fake_service = MagicMock()
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=fake_service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    fake_service.generate_proxy.assert_not_called()


def test_auto_ensure_proxy_skips_when_already_ready():
    """proxy 已 ready 时不应重复触发。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("video")
    fake_info = MagicMock()
    fake_info.is_ready = True
    fake_info.status.value = "ready"

    fake_service = MagicMock()
    fake_service.get_disk_proxy_info.return_value = fake_info
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=fake_service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    fake_service.generate_proxy.assert_not_called()


def test_auto_ensure_proxy_skips_when_generating():
    """proxy 已 generating 时不应重复触发。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("video")
    fake_info = MagicMock()
    fake_info.is_ready = False
    fake_info.status.value = "generating"

    fake_service = MagicMock()
    fake_service.get_disk_proxy_info.return_value = fake_info
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        return_value=fake_service,
    ):
        panel._maybe_auto_ensure_disk_proxy(asset)

    fake_service.generate_proxy.assert_not_called()


def test_auto_ensure_proxy_swallows_exceptions():
    """触发失败不应抛异常（不阻塞素材添加流程）。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    asset = _make_asset("video")
    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    with patch(
        "src.shared.media.disk_proxy_service.get_disk_proxy_service",
        side_effect=RuntimeError("service unavailable"),
    ):
        # 不应抛异常
        panel._maybe_auto_ensure_disk_proxy(asset)


# =============================================================================
# 2. SourceResolver.ensure_proxy 端到端（Final-1 + Final-6 联动）
# =============================================================================


def test_realtime_engine_does_not_start_proxy_transcode_on_placeholder():
    """实时播放只决策来源，不能在 tick 中启动代理转码。"""
    from src.features.edit.core.realtime_playback import (
        PlaybackMode,
        RealtimePlaybackEngine,
    )
    from src.features.edit.core.realtime_playback.source_resolver import (
        DiskProxyServiceAdapter,
        LayerSourceQuery,
        SourceResolver,
    )

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
                clip_id="c1", asset_id="a1", source_path="/x.mov",
                source_frame=f, timeline_frame=f,
            )
        ],
    )
    engine.set_mode(PlaybackMode.REALTIME)
    engine.request_present(0)

    fake_service.generate_proxy.assert_not_called()
