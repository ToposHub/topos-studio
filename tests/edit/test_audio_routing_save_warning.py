"""P0-02 保存路径音频路由错误显示测试。

验收（计划 P0-02）："播放、导出和保存工程都显示可定位错误"。edit_page
在路由门阻断时把可定位错误暴露在 ``audio_routing_errors``；保存前
controller 读取并弹出警告（不阻断保存）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.app.controllers.edit_controller import EditControllerMixin


def _make_controller(edit_page=None) -> EditControllerMixin:
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl.edit_page = edit_page
    return ctrl


class _FakePage:
    def __init__(self, errors):
        self._errors = errors

    @property
    def audio_routing_errors(self):
        if self._errors is None:
            raise RuntimeError("boom")
        return self._errors


class TestCollectAudioRoutingErrors:

    def test_reads_errors_from_edit_page(self):
        ctrl = _make_controller(_FakePage(["Track A1 → unknown bus 'X'"]))
        assert ctrl._collect_audio_routing_errors() == (
            "Track A1 → unknown bus 'X'",)

    def test_empty_when_no_errors(self):
        ctrl = _make_controller(_FakePage([]))
        assert ctrl._collect_audio_routing_errors() == ()

    def test_missing_edit_page_returns_empty(self):
        ctrl = _make_controller(None)
        assert ctrl._collect_audio_routing_errors() == ()

    def test_property_exception_returns_empty(self):
        ctrl = _make_controller(_FakePage(None))
        assert ctrl._collect_audio_routing_errors() == ()


class TestWarnOnSave:

    def test_warns_with_locatable_errors(self, qapp):
        ctrl = _make_controller(_FakePage(["err1", "err2"]))
        with patch("src.app.controllers.edit_controller.QMessageBox") as box:
            ctrl._warn_audio_routing_errors_on_save()
        box.warning.assert_called_once()
        body = box.warning.call_args[0][2]
        assert "err1" in body and "err2" in body

    def test_no_dialog_when_routing_valid(self, qapp):
        ctrl = _make_controller(_FakePage([]))
        with patch("src.app.controllers.edit_controller.QMessageBox") as box:
            ctrl._warn_audio_routing_errors_on_save()
        box.warning.assert_not_called()

    def test_more_than_eight_errors_truncated_with_count(self, qapp):
        errors = [f"err{i}" for i in range(10)]
        ctrl = _make_controller(_FakePage(errors))
        with patch("src.app.controllers.edit_controller.QMessageBox") as box:
            ctrl._warn_audio_routing_errors_on_save()
        body = box.warning.call_args[0][2]
        assert "err7" in body and "err8" not in body
        assert "+2" in body
