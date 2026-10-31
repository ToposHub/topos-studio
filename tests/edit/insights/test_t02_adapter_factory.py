"""T02 · 注入式适配器与惰性 factory 测试。"""

from __future__ import annotations

import pytest

from tests.edit.insights import synthetic as syn
from tests.edit.insights.fake_host import FakeHost

from src.features.edit.insights.factory import (
    current_window,
    open_insights_window,
    shutdown_insights_window,
)
from src.features.edit.insights.host_adapter import InjectedHostAdapter
from src.features.edit.insights.host_protocol import (
    CAPTURE_FAILED,
    CAPTURE_INCONSISTENT,
    CAPTURE_NO_PROJECT,
    REASON_NOT_SUPPORTED,
    THUMB_UNAVAILABLE,
)


pytestmark = [pytest.mark.edit_core, pytest.mark.edit_ui]


# ── InjectedHostAdapter ─────────────────────────────────────────


def test_adapter_capture_ok():
    st = syn.build_synthetic_state()
    adapter = InjectedHostAdapter(capture_state_fn=lambda: st.state)
    result = adapter.capture_readonly_state()
    assert result.ok and result.snapshot is not None
    assert len(result.snapshot.sequences) == 3


def test_adapter_no_project():
    adapter = InjectedHostAdapter(capture_state_fn=lambda: None)
    result = adapter.capture_readonly_state()
    assert not result.ok and result.error_code == CAPTURE_NO_PROJECT


def test_adapter_provider_exception_structured():
    def boom():
        raise RuntimeError("host read failed")

    adapter = InjectedHostAdapter(capture_state_fn=boom)
    result = adapter.capture_readonly_state()
    assert not result.ok and result.error_code == CAPTURE_FAILED
    assert "host read failed" in result.message


def test_adapter_malformed_state_inconsistent():
    class Broken:
        project = object()          # 有 project
        sequences = None            # 缺 sequences dict → 结构错误

    adapter = InjectedHostAdapter(capture_state_fn=lambda: Broken())
    result = adapter.capture_readonly_state()
    assert not result.ok and result.error_code == CAPTURE_INCONSISTENT


def test_adapter_unwired_navigation_and_thumbnail():
    adapter = InjectedHostAdapter(capture_state_fn=lambda: None)
    from src.features.edit.insights.host_protocol import NavigateRequest, ThumbnailRequest

    nav = adapter.navigate_to(
        NavigateRequest(
            capture_id="c", project_id="p", sequence_id="s", target_frame=0
        )
    )
    assert not nav.ok and nav.reason == REASON_NOT_SUPPORTED
    thumb = adapter.request_thumbnail(
        ThumbnailRequest(capture_id="c", asset_id="a")
    )
    assert not thumb.ok and thumb.quality == THUMB_UNAVAILABLE


def test_adapter_satisfies_protocol():
    from src.features.edit.insights.host_protocol import InsightsHostProtocol

    st = syn.build_synthetic_state()
    adapter = InjectedHostAdapter(capture_state_fn=lambda: st.state)
    assert isinstance(adapter, InsightsHostProtocol)


# ── factory：重复打开 / 关闭注销 / worker 不泄漏 ────────────────


def test_factory_reuses_open_window(qtbot):
    host = FakeHost(state_provider=lambda: syn.build_synthetic_state().state)
    try:
        first = open_insights_window(host)
        qtbot.addWidget(first)
        second = open_insights_window(host)
        assert second is first  # 重复打开=激活同一实例，不新建窗口/worker
    finally:
        shutdown_insights_window()


def test_factory_recreates_after_close(qtbot):
    host = FakeHost(state_provider=lambda: syn.build_synthetic_state().state)
    first = open_insights_window(host)
    qtbot.addWidget(first)
    worker_threads = list(first._worker._threads)
    shutdown_insights_window()
    assert all(not t.is_alive() for t in worker_threads)  # 旧 worker 已停
    assert current_window() is None
    second = open_insights_window(host)
    qtbot.addWidget(second)
    assert second is not first
    shutdown_insights_window()


def test_factory_shutdown_without_window_is_noop():
    shutdown_insights_window()  # 未打开时静默
    assert current_window() is None
