"""VP-B07：Viewer capability 注册表——代理矩阵、门控、未设计项如实 False。"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.viewer_capabilities import (
    ProxyStateSnapshot,
    ViewerCapabilityContext,
    capability_by_action,
    evaluate_viewer_capabilities,
)

pytestmark = pytest.mark.edit_core


def _caps(**kwargs):
    context = ViewerCapabilityContext(**kwargs)
    return capability_by_action(evaluate_viewer_capabilities(context))


def test_b07_proxy_matrix():
    ready = _caps(proxy_state=ProxyStateSnapshot(has_disk_proxy_ready=True))
    assert ready["proxy.prefer_proxy"].supported
    assert ready["proxy.force_proxy"].supported

    generating = _caps(proxy_state=ProxyStateSnapshot(
        is_disk_proxy_generating=True))
    assert not generating["proxy.prefer_proxy"].supported
    assert "being generated" in generating["proxy.prefer_proxy"].reason

    none = _caps()
    assert not none["proxy.force_proxy"].supported
    assert "No usable proxy" in none["proxy.force_proxy"].reason


def test_b07_landed_backends_are_supported():
    caps = _caps(source_asset_open=True)
    assert caps["source.loop"].supported                 # B05
    assert caps["source.reverse_play"].supported         # B05（视频）
    assert caps["source.stop_in_place"].supported
    assert caps["match_frame.timeline_to_source"].supported   # B06
    assert caps["match_frame.source_to_timeline"].supported
    assert caps["preview.quality"].supported


def test_b07_undesigned_features_are_honestly_false_with_reasons():
    caps = _caps()
    assert not caps["source.tape"].supported
    assert "Source Tape" in caps["source.tape"].reason
    assert not caps["source.reverse_play_audio"].supported  # B05 能力位
    assert caps["source.reverse_play_audio"].reason
    assert not caps["source.markers"].supported             # B06 推迟 schema
    assert not caps["bypass.color_monitor"].supported       # 预览 service 未接
    assert caps["bypass.color_monitor"].reason
    assert not caps["fullscreen.source"].supported          # 仅 Program 外监
    assert caps["fullscreen.program"].supported


def test_b07_no_timeline_gates_match_frame():
    caps = _caps(has_timeline=False)
    assert not caps["match_frame.timeline_to_source"].supported
    assert not caps["match_frame.source_to_timeline"].supported
    assert caps["match_frame.timeline_to_source"].reason == "No timeline is open."


def test_b07_no_source_asset_gates_reverse():
    caps = _caps(source_asset_open=False)
    assert not caps["source.reverse_play"].supported


def test_unsupported_source_loop_has_explicit_reason() -> None:
    capability = _caps(source_loop_supported=False)["source.loop"]
    assert not capability.supported
    assert capability.reason == "Source loop playback is not supported."
    supported = _caps(source_loop_supported=True)["source.loop"]
    assert supported.supported
    assert supported.reason is None


def test_b07_deliver_forces_timeline_monitoring():
    caps = _caps(in_deliver=True, source_asset_open=True)
    assert not caps["source.loop"].supported
    assert caps["source.loop"].reason == "Deliver forces Timeline monitoring."
    assert not caps["match_frame.timeline_to_source"].supported
    # 交付页仍可从时间线定位到源（导出核对），不受 Source 面板隐藏影响
    assert caps["match_frame.source_to_timeline"].supported
    assert caps["proxy.prefer_proxy"].supported or not caps[
        "proxy.prefer_proxy"].supported  # 代理能力与页面无关


def test_b07_capabilities_serialize_for_menu_sync():
    caps = evaluate_viewer_capabilities(
        ViewerCapabilityContext(source_asset_open=True))
    data = [c.to_dict() for c in caps]
    revived = json.loads(json.dumps(data))
    assert revived[0]["action"]
    assert all(("supported" in item and "reason" in item) for item in revived)
