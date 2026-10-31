"""AUD-34519：降级路径 open 失败时已创建的 MediaSource 必须被关闭。

``MediaSourceFactory.create`` 成功而 ``open`` 抛错时，FallbackStrategy 的
三个降级尝试（backend 轮换 / 代理分辨率 / 最小兼容模式）都不应丢弃已
创建 source 持有的原生解码器句柄——历史上 except 分支直接 continue，
每轮降级尝试泄漏一个句柄。
"""

from __future__ import annotations

from typing import Any, List

import pytest

import src.shared.media.error_handling as eh


class _LeakProbeSource:
    """create 成功、open 必失败、记录 close 是否被调用的探针。"""

    def __init__(self) -> None:
        self.closed: bool = False

    def open(self) -> None:
        raise RuntimeError("probe: open failed")

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def probing_factory(monkeypatch: pytest.MonkeyPatch) -> List[_LeakProbeSource]:
    created: List[_LeakProbeSource] = []

    class _ProbingFactory:
        @staticmethod
        def _get_backend_order(backend: str, path: str) -> List[str]:
            return ["ffmpeg"]

        @staticmethod
        def create(path: str, **kwargs: Any) -> _LeakProbeSource:
            source = _LeakProbeSource()
            created.append(source)
            return source

    monkeypatch.setattr(eh, "MediaSourceFactory", _ProbingFactory)
    return created


def _assert_all_closed(created: List[_LeakProbeSource]) -> None:
    assert created, "factory.create 应至少被尝试一次"
    assert all(source.closed for source in created), (
        "AUD-34519：open 失败后已创建的 source 必须被显式关闭，"
        f"泄漏 {sum(1 for s in created if not s.closed)} 个"
    )


def test_backend_fallback_closes_source_when_open_fails(
        probing_factory: List[_LeakProbeSource]) -> None:
    result = eh.FallbackStrategy()._try_backend_switch("/bad.mov")
    assert not result.success
    _assert_all_closed(probing_factory)


def test_proxy_resolution_closes_source_when_open_fails(
        probing_factory: List[_LeakProbeSource]) -> None:
    result = eh.FallbackStrategy()._try_proxy_resolution("/bad.mov")
    assert not result.success
    _assert_all_closed(probing_factory)


def test_minimal_mode_closes_source_when_open_fails(
        probing_factory: List[_LeakProbeSource]) -> None:
    result = eh.FallbackStrategy()._try_minimal_mode("/bad.mov")
    assert not result.success
    _assert_all_closed(probing_factory)
