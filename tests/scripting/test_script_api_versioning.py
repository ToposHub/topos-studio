"""P1-COLOR-24: Script API 版本化 + 镜头遍历 + dry-run diff 测试。

覆盖：
- API_VERSION 常量与脚本头声明解析（有/无声明、前 40 行限制、非法格式）
- 版本兼容性判定（主版本不匹配拒绝 + 迁移提示、次版本高警告、兼容/未声明）
- diff_snapshots / ToposAPI.diff_grade 变更清单
- ToposAPI.grade_snapshot / list_clips / clip_grade（fake bridge 主线程直通）
- ScriptEngine.execute 的版本拒绝路径（不启动线程）
- MainWindow._script_timeline_context（fake project/sequence，bare-mixin）
"""

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# 版本常量与声明解析
# ---------------------------------------------------------------------------

def test_api_version_constants():
    from src.features.scripting.api import API_VERSION, API_VERSION_TUPLE
    assert API_VERSION == "1.0"
    assert API_VERSION_TUPLE == (1, 0)


def test_parse_api_requirement_found():
    from src.features.scripting.api import parse_api_requirement
    assert parse_api_requirement("# topos-api: 1.0\nprint(1)") == (1, 0)
    assert parse_api_requirement("#topos-api: 2.5") == (2, 5)
    # 头部注释块内（前 40 行）
    code = "# Title\n# Author\n# topos-api: 1.2\ntopos.open('x')"
    assert parse_api_requirement(code) == (1, 2)


def test_parse_api_requirement_not_found():
    from src.features.scripting.api import parse_api_requirement
    assert parse_api_requirement("print('no declaration')") is None
    assert parse_api_requirement("") is None
    # 声明在第 41 行 = 超出扫描窗口
    code = "\n".join(["# pad"] * 40 + ["# topos-api: 9.9"])
    assert parse_api_requirement(code) is None
    # 非法格式不匹配
    assert parse_api_requirement("# topos-api 1.0") is None
    assert parse_api_requirement("# topos-api: abc") is None


def test_check_api_compatibility_major_mismatch_rejected():
    from src.features.scripting.api import check_api_compatibility
    ok, message = check_api_compatibility((2, 0), (1, 0))
    assert ok is False
    assert "主版本不兼容" in message
    assert "迁移" in message


def test_check_api_compatibility_minor_higher_warns():
    from src.features.scripting.api import check_api_compatibility
    ok, message = check_api_compatibility((1, 5), (1, 0))
    assert ok is True
    assert message  # 警告文案


def test_check_api_compatibility_compatible_and_none():
    from src.features.scripting.api import check_api_compatibility
    assert check_api_compatibility(None) == (True, "")
    assert check_api_compatibility((1, 0)) == (True, "")
    assert check_api_compatibility((1, 0), (1, 3)) == (True, "")


# ---------------------------------------------------------------------------
# dry-run diff
# ---------------------------------------------------------------------------

def test_diff_snapshots_detects_changes():
    from src.features.scripting.api import diff_snapshots
    before = {"exposure": 0.0, "tint": 5, "lift": (0, 0, 0)}
    after = {"exposure": 0.5, "tint": 5, "lift": (0.1, 0, 0), "lut": "a.cube"}
    changes = diff_snapshots(before, after)
    text = "\n".join(changes)
    assert "exposure" in text and "0.0" in text and "0.5" in text
    assert "lift" in text
    assert "added" in text and "lut" in text
    assert "tint" not in text  # 未变化不出现


def test_diff_snapshots_removed():
    from src.features.scripting.api import diff_snapshots
    changes = diff_snapshots({"a": 1}, {})
    assert changes == ["a: removed 1"]


def test_diff_grade_static_method():
    from src.features.scripting.api import ToposAPI
    changes = ToposAPI.diff_grade({"x": 1}, {"x": 2})
    assert changes == ["x: 1 -> 2"]
    assert ToposAPI.diff_grade(None, {}) == []
    assert ToposAPI.diff_grade({}, None) == []


# ---------------------------------------------------------------------------
# ToposAPI 快照与遍历（fake bridge 直通主线程）
# ---------------------------------------------------------------------------

class _FakeBridge:
    """测试用 bridge：直接在当前线程执行（模拟主线程直通）。"""

    def call_sync(self, func, timeout=60000):
        return func()


def _make_api(engine, provider=None):
    from src.features.scripting.api import ToposAPI
    api = ToposAPI(engine, _FakeBridge())
    api._timeline_provider = provider
    return api


def test_api_version_method():
    api = _make_api(SimpleNamespace())
    assert api.api_version() == "1.0"


def test_grade_snapshot_reads_active_layer():
    layer = SimpleNamespace(
        grading_params={"exposure": 1.5},
        active_lut_path="lut.cube",
        lut_intensity=0.7,
        qualifier_params={"enabled": True},
    )
    engine = SimpleNamespace(get_active_layer=lambda: layer)
    api = _make_api(engine)

    snap = api.grade_snapshot()
    assert snap["grading_params"]["exposure"] == 1.5
    assert snap["lut_path"] == "lut.cube"
    assert snap["lut_intensity"] == 0.7
    assert snap["qualifier_enabled"] is True

    # 快照是深拷贝：后续修改不影响已取快照
    layer.grading_params["exposure"] = 9.0
    assert snap["grading_params"]["exposure"] == 1.5


def test_grade_snapshot_without_layer():
    engine = SimpleNamespace(get_active_layer=lambda: None)
    api = _make_api(engine)
    assert api.grade_snapshot() is None


def test_list_clips_via_provider_and_empty_paths():
    clips = [{"clip_id": "c1", "timeline_start": 0}]
    api = _make_api(SimpleNamespace(), provider=lambda what, cid=None: clips)
    assert api.list_clips() == clips

    # 无 provider → 空清单；clip_grade → None
    api_none = _make_api(SimpleNamespace())
    assert api_none.list_clips() == []
    assert api_none.clip_grade("c1") is None


def test_clip_grade_provider_lookup():
    grades = {"c1": {"grading_params": {"exposure": 2.0}}}
    provider = lambda what, cid=None: grades.get(cid) if what == "clip_grade" else []
    api = _make_api(SimpleNamespace(), provider=provider)
    assert api.clip_grade("c1")["grading_params"]["exposure"] == 2.0
    assert api.clip_grade("missing") is None


def test_list_clips_swallows_provider_errors():
    def bad_provider(what, cid=None):
        raise RuntimeError("boom")
    api = _make_api(SimpleNamespace(), provider=bad_provider)
    assert api.list_clips() == []
    assert api.clip_grade("x") is None


# ---------------------------------------------------------------------------
# ScriptEngine 版本拒绝路径
# ---------------------------------------------------------------------------

def test_engine_execute_rejects_major_mismatch(qapp):
    from src.features.scripting.engine import ScriptEngine
    engine = ScriptEngine(SimpleNamespace())

    finished, errors, outputs = [], [], []
    engine.script_finished.connect(lambda ok, msg: finished.append((ok, msg)))
    engine.script_error.connect(lambda msg, line: errors.append((msg, line)))
    engine.output_ready.connect(lambda t: outputs.append(t))

    engine.execute("# topos-api: 2.0\ntopos.open('x')")

    assert finished and finished[0][0] is False
    assert errors and "主版本不兼容" in errors[0][0]
    assert not engine.is_running()  # 未启动线程


def test_engine_execute_accepts_matching_version(qapp):
    from src.features.scripting.engine import ScriptEngine
    engine = ScriptEngine(SimpleNamespace())
    finished = []
    engine.script_finished.connect(lambda ok, msg: finished.append((ok, msg)))

    engine.execute("# topos-api: 1.0\nprint('hi')")
    # 异步线程执行；轮询等待完成
    import time
    deadline = time.monotonic() + 10
    while not finished and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert finished and finished[0][0] is True


# ---------------------------------------------------------------------------
# MainWindow._script_timeline_context（bare-mixin）
# ---------------------------------------------------------------------------

def _fake_timeline_host(sequence, project):
    from src.app.main_window import MainWindow
    host = SimpleNamespace(
        edit_page=SimpleNamespace(_current_timeline=sequence),
        _current_edit_project=project,
    )
    return MainWindow._script_timeline_context.__get__(host)


def test_script_timeline_context_lists_clips_sorted():
    from src.features.color.core.layer import Layer
    from src.shared.project.color_stack import ColorStackData

    graded = ColorStackData.create(asset_id="a1")
    graded = ColorStackData(
        stack_id=graded.stack_id,
        grading_params={"exposure": 1.0},
    )
    project = SimpleNamespace(color_stacks={graded.stack_id: graded})
    sequence = SimpleNamespace(clips=[
        SimpleNamespace(clip_id="c2", asset_id="a", track_id="V1",
                        timeline_start=24, timeline_duration=12,
                        source_start=0, source_duration=12,
                        color_stack_id=None),
        SimpleNamespace(clip_id="c1", asset_id="a", track_id="V1",
                        timeline_start=0, timeline_duration=24,
                        source_start=0, source_duration=24,
                        color_stack_id=graded.stack_id),
    ])
    ctx = _fake_timeline_host(sequence, project)

    clips = ctx("list_clips")
    assert [c["clip_id"] for c in clips] == ["c1", "c2"]  # 按起点排序
    assert clips[0]["has_grade"] is True
    assert clips[1]["has_grade"] is False
    assert clips[0]["timeline_duration"] == 24


def test_script_timeline_context_clip_grade_lookup():
    from src.shared.project.color_stack import ColorStackData
    stack = ColorStackData.create(asset_id="a1")
    stack = ColorStackData(
        stack_id=stack.stack_id,
        grading_params={"contrast": 5.0},
        lut_path="look.cube",
    )
    project = SimpleNamespace(color_stacks={stack.stack_id: stack})
    sequence = SimpleNamespace(clips=[
        SimpleNamespace(clip_id="c1", asset_id="a", track_id="V1",
                        timeline_start=0, timeline_duration=10,
                        source_start=0, source_duration=10,
                        color_stack_id=stack.stack_id),
    ])
    ctx = _fake_timeline_host(sequence, project)

    grade = ctx("clip_grade", "c1")
    assert grade["grading_params"]["contrast"] == 5.0
    assert grade["lut_path"] == "look.cube"

    assert ctx("clip_grade", "missing") is None
    assert ctx("unknown_what") is None


def test_script_timeline_context_empty_without_timeline():
    ctx = _fake_timeline_host(None, None)
    assert ctx("list_clips") == []
    assert ctx("clip_grade", "c1") is None
