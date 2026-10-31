"""受限诊断测试 — RenderDiagnosticSink + topos.get_render_diagnostics 工具。

覆盖：
* sink 环形缓冲：record/recent/clear/capacity/来源过滤/去重/截断/计数。
* ``record_compile_failure`` 永不抛异常（渲染热路径安全）。
* 工具：注册可见、空/非空返回、来源过滤、JSON 可序列化、**不含路径/源码**。
* 并发写入线程安全。
"""

from __future__ import annotations

import json
import threading

import pytest

pytestmark = pytest.mark.edit_core

from src.shared.render_diagnostics import (
    CompileDiagnostic,
    CODE_SHADER_COMPILE_FAILED,
    CODE_SHADER_UNDECLARED_UNIFORM,
    RenderDiagnosticSink,
    SOURCE_COLOR_PIPELINE,
    SOURCE_COMPOSITION,
    SOURCE_EFFECT_PASS,
    get_render_diagnostic_sink,
    record_compile_failure,
    set_render_diagnostic_sink,
)
from src.features.edit.ai_editing.tool_facade import (
    ToolFacade,
    ToolPermission,
    list_tool_definitions,
)


# ================================================================
# Sink 单元测试（直接实例化，不走单例）
# ================================================================


class TestSink:
    def test_record_and_recent_returns_newest_first(self):
        sink = RenderDiagnosticSink(capacity=10)
        sink.record(source=SOURCE_COLOR_PIPELINE, message="err1")
        sink.record(source=SOURCE_COLOR_PIPELINE, message="err2")
        sink.record(source=SOURCE_COLOR_PIPELINE, message="err3")
        out = sink.recent()
        assert [d.message for d in out] == ["err3", "err2", "err1"]

    def test_recent_limit(self):
        sink = RenderDiagnosticSink(capacity=10)
        for i in range(5):
            sink.record(source=SOURCE_COLOR_PIPELINE, message=f"m{i}")
        out = sink.recent(limit=2)
        assert [d.message for d in out] == ["m4", "m3"]

    def test_source_filter(self):
        sink = RenderDiagnosticSink(capacity=10)
        sink.record(source=SOURCE_COLOR_PIPELINE, message="a")
        sink.record(source=SOURCE_EFFECT_PASS, message="b")
        sink.record(source=SOURCE_COMPOSITION, message="c")
        out = sink.recent(source=SOURCE_EFFECT_PASS)
        assert [d.message for d in out] == ["b"]
        assert all(d.source == SOURCE_EFFECT_PASS for d in out)

    def test_clear(self):
        sink = RenderDiagnosticSink()
        sink.record(source=SOURCE_COLOR_PIPELINE, message="x")
        assert sink.count() == 1
        sink.clear()
        assert sink.count() == 0
        assert sink.recent() == []

    def test_capacity_drops_oldest(self):
        sink = RenderDiagnosticSink(capacity=3)
        for i in range(5):
            sink.record(source=SOURCE_COLOR_PIPELINE, message=f"m{i}")
        # 只保留最近 3 条（m2, m3, m4），最新在前
        out = sink.recent(limit=10)
        assert [d.message for d in out] == ["m4", "m3", "m2"]
        assert sink.count() == 3

    def test_capacity_minimum_one(self):
        sink = RenderDiagnosticSink(capacity=0)
        assert sink.capacity == 1
        sink.record(source=SOURCE_COLOR_PIPELINE, message="x")
        assert sink.count() == 1

    def test_subjects_dedup_and_coerce(self):
        sink = RenderDiagnosticSink()
        sink.record(
            source=SOURCE_COLOR_PIPELINE,
            message="e",
            subjects=["Film_Halation", "Film_Halation", 42, ""],
        )
        out = sink.recent()
        assert out[0].subjects == ("Film_Halation", "42")
        assert out[0].to_dict()["subjects"] == ["Film_Halation", "42"]
        assert out[0].code == CODE_SHADER_COMPILE_FAILED

    def test_message_truncated(self):
        sink = RenderDiagnosticSink()
        huge = "X" * 100_000
        sink.record(source=SOURCE_COLOR_PIPELINE, message=huge)
        out = sink.recent()
        assert len(out[0].message) <= 4000

    def test_context_ids_are_normalized_and_serialized(self):
        sink = RenderDiagnosticSink()
        sink.record(
            source=SOURCE_COMPOSITION,
            message="compile failed",
            code=CODE_SHADER_UNDECLARED_UNIFORM,
            request_id="render/0007",
            layer_id="layer 01",
            plugin_ids=["plugin:film/grain", "plugin:film/grain", ""],
        )
        entry = sink.recent()[0]
        assert entry.request_id == "render_0007"
        assert entry.layer_id == "layer_01"
        assert entry.plugin_ids == ("plugin:film_grain",)
        assert entry.code == CODE_SHADER_UNDECLARED_UNIFORM
        assert entry.to_dict()["plugin_ids"] == ["plugin:film_grain"]

    def test_context_single_values_are_not_split_into_characters(self):
        sink = RenderDiagnosticSink()
        entry = sink.record(
            source=SOURCE_COMPOSITION,
            message="compile failed",
            subjects="single-plugin",
            plugin_ids="single-plugin",
        )
        assert entry.subjects == ("single-plugin",)
        assert entry.plugin_ids == ("single-plugin",)

    def test_count_by_source(self):
        sink = RenderDiagnosticSink()
        sink.record(source=SOURCE_COLOR_PIPELINE, message="a")
        sink.record(source=SOURCE_COLOR_PIPELINE, message="b")
        sink.record(source=SOURCE_EFFECT_PASS, message="c")
        assert sink.count() == 3
        assert sink.count(source=SOURCE_COLOR_PIPELINE) == 2
        assert sink.count(source=SOURCE_EFFECT_PASS) == 1
        assert sink.count(source=SOURCE_COMPOSITION) == 0

    def test_recent_limit_zero_returns_empty(self):
        sink = RenderDiagnosticSink()
        sink.record(source=SOURCE_COLOR_PIPELINE, message="a")
        assert sink.recent(limit=0) == []


# ================================================================
# record_compile_failure：渲染热路径安全（永不抛异常）
# ================================================================


class TestRecordCompileFailure:
    def test_records_into_singleton(self):
        fresh = RenderDiagnosticSink()
        set_render_diagnostic_sink(fresh)
        try:
            record_compile_failure(
                source=SOURCE_COLOR_PIPELINE,
                subjects=["MyPlugin"],
                message="ERROR: 0:42 undeclared identifier",
                code=CODE_SHADER_UNDECLARED_UNIFORM,
                request_id="render-1",
                layer_id="layer-1",
                plugin_ids=["MyPlugin"],
            )
            out = fresh.recent()
            assert len(out) == 1
            assert out[0].source == SOURCE_COLOR_PIPELINE
            assert out[0].subjects == ("MyPlugin",)
            assert out[0].code == CODE_SHADER_UNDECLARED_UNIFORM
            assert out[0].request_id == "render-1"
            assert out[0].layer_id == "layer-1"
            assert out[0].plugin_ids == ("MyPlugin",)
            assert "undeclared" in out[0].message
        finally:
            set_render_diagnostic_sink(None)

    def test_never_raises_on_bad_args(self):
        # 即便 subjects 是非法类型 / source 怪异，也不应抛异常
        set_render_diagnostic_sink(RenderDiagnosticSink())
        try:
            record_compile_failure(source=None, message=None, subjects=123)  # type: ignore[arg-type]
            record_compile_failure(source=SOURCE_EFFECT_PASS, message="ok")
        finally:
            set_render_diagnostic_sink(None)

    def test_singleton_is_stable(self):
        set_render_diagnostic_sink(None)
        a = get_render_diagnostic_sink()
        b = get_render_diagnostic_sink()
        assert a is b


# ================================================================
# 并发安全
# ================================================================


def test_concurrent_records_are_all_counted():
    sink = RenderDiagnosticSink(capacity=2000)
    n_threads = 8
    per_thread = 50

    def worker(tid: int) -> None:
        for i in range(per_thread):
            sink.record(
                source=SOURCE_EFFECT_PASS,
                subjects=(f"fx_{tid}",),
                message=f"t{tid}-{i}",
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sink.count() == n_threads * per_thread


# ================================================================
# topos.get_render_diagnostics 工具（经 ToolFacade）
# ================================================================


@pytest.fixture
def diagnostics_facade():
    """绑定一个隔离的诊断单例 + 真实 ToolFacade。"""
    # 复用 edit_api 的 FakeHost 模式构造一个最小可用的 session。
    from src.features.edit.ai_editing.edit_api import EditSession
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.shared.media.metadata import Rational
    from src.features.edit.core.project import (
        EditProject, MediaAsset, TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    class _FakeHost:
        def __init__(self, state):
            self._state = state

        def get_state(self):
            return self._state

        def get_manifest(self):
            from src.features.edit.ai_editing.capability_manifest import build_manifest
            return build_manifest()

        def on_committed(self, result):
            pass

        def request_user_approval(self, plan, diff):
            return None

        def approval_service(self):
            return None

    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/secret/clip.mp4", media_type="video",
        display_name="clip", metadata={},
    )
    ref = TimelineSequenceRef(timeline_id="t1", name="Main", fps=24.0,
                              width=1920, height=1080, sample_rate=48000)
    project = EditProject(project_id="p1", name="P", assets={"a1": asset},
                          timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1")
    EditSession.unbind()
    host = _FakeHost(ProjectSequences(project=project, sequences={"t1": seq}))
    session = EditSession.bind(host)

    fresh = RenderDiagnosticSink()
    set_render_diagnostic_sink(fresh)
    facade = ToolFacade(session)
    try:
        yield facade, fresh
    finally:
        EditSession.unbind()
        set_render_diagnostic_sink(None)


class TestGetRenderDiagnosticsTool:
    def test_tool_registered_and_read_permission(self):
        tools = {t.name: t for t in list_tool_definitions()}
        assert "topos.get_render_diagnostics" in tools
        assert tools["topos.get_render_diagnostics"].permission == ToolPermission.READ

    def test_empty_returns_note(self, diagnostics_facade):
        f, _ = diagnostics_facade
        r = f.invoke("topos.get_render_diagnostics", {})
        assert r["ok"] is True
        assert r["count"] == 0
        assert r["diagnostics"] == []
        assert "no recent" in r["note"]

    def test_returns_recorded_entries(self, diagnostics_facade):
        f, sink = diagnostics_facade
        sink.record(
            source=SOURCE_COLOR_PIPELINE,
            subjects=["Film_Halation"],
            message="ERROR: 0:42 'foo' : undeclared identifier",
        )
        sink.record(
            source=SOURCE_EFFECT_PASS,
            subjects=("glow",),
            message="syntax error",
            request_id="render-2",
            layer_id="layer-2",
            plugin_ids=("glow",),
        )
        r = f.invoke("topos.get_render_diagnostics", {"limit": 10})
        assert r["ok"] is True
        assert r["count"] == 2
        # 最新在前：effect_pass 在后写入 → 排第一
        assert r["diagnostics"][0]["source"]["value"] == SOURCE_EFFECT_PASS
        assert r["diagnostics"][1]["source"]["value"] == SOURCE_COLOR_PIPELINE
        # subjects 是 list（只有字符串叶子被包裹为 {value,...}）
        assert r["diagnostics"][1]["subjects"][0]["value"] == "Film_Halation"
        assert "undeclared" in r["diagnostics"][1]["message"]["value"]
        assert r["diagnostics"][0]["request_id"]["value"] == "render-2"
        assert r["diagnostics"][0]["layer_id"]["value"] == "layer-2"
        assert r["diagnostics"][0]["plugin_ids"][0]["value"] == "glow"

    def test_source_filter(self, diagnostics_facade):
        f, sink = diagnostics_facade
        sink.record(source=SOURCE_COLOR_PIPELINE, message="a")
        sink.record(source=SOURCE_EFFECT_PASS, message="b")
        r = f.invoke("topos.get_render_diagnostics", {"source": SOURCE_EFFECT_PASS})
        assert r["count"] == 1
        assert r["diagnostics"][0]["source"]["value"] == SOURCE_EFFECT_PASS

    def test_response_is_json_serializable(self, diagnostics_facade):
        f, sink = diagnostics_facade
        sink.record(source=SOURCE_COLOR_PIPELINE, subjects=["p"], message="err")
        r = f.invoke("topos.get_render_diagnostics", {})
        # M0.3 契约：任意 tool response 可 json.dumps
        json.dumps(r)

    def test_response_carries_no_file_paths(self, diagnostics_facade):
        """红线：返回里不应泄漏绝对路径（工具不读文件）。"""
        f, sink = diagnostics_facade
        sink.record(
            source=SOURCE_COLOR_PIPELINE,
            subjects=["MyPlugin"],  # 标识符，非路径
            message="ERROR: 0:1 undeclared identifier 'x'",
        )
        r = f.invoke("topos.get_render_diagnostics", {})
        blob = json.dumps(r)
        # 不应出现典型本机路径片段
        assert "/home/" not in blob
        assert "/Users/" not in blob
        assert "C:\\" not in blob

    def test_limit_respected(self, diagnostics_facade):
        f, sink = diagnostics_facade
        for i in range(5):
            sink.record(source=SOURCE_COLOR_PIPELINE, message=f"m{i}")
        r = f.invoke("topos.get_render_diagnostics", {"limit": 2})
        assert r["count"] == 2
