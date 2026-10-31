"""N07 · 文稿删除真实入口测试。

审计句：菜单恒提示去转写（永不可达占位）；工厂未传 on_plan_ready，
计划无消费者。修复契约：词级字幕在库即真实进入；计划经编译+revision
守卫+时间线命令栈一次 Undo；工程变化拒绝旧计划。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.captions.model import (  # noqa: E402
    CaptionWordTiming)
from src.features.edit.ui.filler_removal_panel import FillerRemovalPanel  # noqa: E402


def _host():
    """绑定 MenuToolbarMixin 的 N07 方法到假宿主。"""
    from src.app.controllers.menu_toolbar_controller import (
        MenuToolbarMixin,
    )
    host = NS()
    for name, fn in inspect.getmembers(
            MenuToolbarMixin, predicate=inspect.isfunction):
        if name in ("on_open_filler_removal_panel", "_extract_word_timings",
                    "_apply_filler_plan", "_upgrade_panel_context",
                    "_open_upgrade_panel", "_panel_host",
                    "_upgrade_panel_unavailable"):
            setattr(host, name, fn.__get__(host))
    return host


def _timeline_with_words(words):
    meta = NS(word_timings=tuple(words))
    clip = NS(caption_metadata=meta)
    track = NS(clips=(clip,))
    return NS(timeline_id="t1", subtitle_tracks=(track,),
              video_tracks=(), audio_tracks=())


class TestWordTimingExtraction:
    def test_extracts_from_subtitle_tracks(self):
        words = [CaptionWordTiming("um", 10, 12, 0.99),
                 CaptionWordTiming("okay", 20, 22, 0.98)]
        host = _host()
        timings = host._extract_word_timings(_timeline_with_words(words))
        assert len(timings) == 2
        assert timings[0].text == "um"

    def test_no_subtitle_track_empty(self):
        host = _host()
        assert host._extract_word_timings(
            NS(subtitle_tracks=(), video_tracks=(), audio_tracks=())) == []


class TestEntryGate:
    def test_no_words_reports_unavailable(self):
        host = _host()
        reasons = []
        host._upgrade_panel_unavailable = reasons.append
        host._upgrade_panel_context = lambda: {
            "project": NS(revision=3), "timeline": _timeline_with_words([])}
        host.on_open_filler_removal_panel()
        assert any("词级时间戳" in r for r in reasons)

    def test_words_open_panel_with_detection_and_consumer(self):
        host = _host()
        opened = {}
        host._upgrade_panel_unavailable = lambda m: None
        host._upgrade_panel_context = lambda: {
            "project": NS(revision=3),
            "timeline": _timeline_with_words(
                [CaptionWordTiming("um", 10, 12, 0.99)])}
        host._open_upgrade_panel = (
            lambda action_id, **kw: opened.update(
                {"action": action_id, **kw}))
        host.on_open_filler_removal_panel()
        assert opened["action"] == "edit.filler_removal"
        assert opened["base_revision"] == 3
        assert callable(opened["on_plan_ready"])  # 计划消费者已接线
        assert opened["detection"].hits  # 检测真实发生


class TestPlanApply:
    def test_revision_mismatch_refuses(self):
        host = _host()
        reasons = []
        host._upgrade_panel_unavailable = reasons.append
        host._current_edit_project = NS(revision=99)
        host._apply_filler_plan(NS(), base_revision=3, timeline_id="t1")
        assert any("作废" in r for r in reasons)

    def test_apply_compiles_and_executes_macro(self, monkeypatch):
        host = _host()
        executed = []
        host._current_edit_project = NS(revision=3)
        host._upgrade_panel_unavailable = lambda m: None
        host._panel_host = NS(timeline_command_executor=lambda tid: (
            lambda cmd: executed.append((tid, cmd)) or True))

        class _Macro:
            pass

        import src.features.edit.ai_editing.plan_compiler as pc
        monkeypatch.setattr(
            pc, "compile_plan",
            lambda plan: NS(macro=_Macro(), plan_id="p", undo_label="l",
                            operation_count=1, operation_summaries={}))
        host._apply_filler_plan(
            NS(), base_revision=3, timeline_id="t1")
        assert executed and executed[0][0] == "t1"
        assert isinstance(executed[0][1], _Macro)  # 编译产物入命令栈


class TestFactoryForwardsConsumer:
    def test_factory_passes_on_plan_ready(self):
        from src.shared.ui.upgrade_panel_actions import open_panel
        sentinel = object()
        created = {}

        class _Probe:
            def __init__(self, detection, **kw):
                created.update(kw)

        import src.features.edit.ui.filler_removal_panel as frp
        original = frp.FillerRemovalPanel
        frp.FillerRemovalPanel = _Probe
        try:
            open_panel(
                "edit.filler_removal",
                detection=object(), project=object(),
                timeline=NS(timeline_id="t"), base_revision=1,
                on_plan_ready=sentinel)
        finally:
            frp.FillerRemovalPanel = original
        assert created.get("on_plan_ready") is sentinel
