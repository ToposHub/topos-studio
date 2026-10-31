"""P1-COLOR-23: 工程依赖报告测试。

覆盖：
- 依赖收集（LUT 路径 / 启用的 DCTL 插件，去重、忽略禁用与非 DCTL）
- 报告构建（文件存在 ok+sha256、缺失 missing、相对路径解析、插件注册
  表 lookup、AI 模型段）
- 序列化往返
- compare_reports（缺失检测、同名不同内容检测、新增依赖、一致=空）
- summary 文案
"""

import json
import os

import pytest

from src.features.color.core.layer import Layer
from src.features.color.core.project_dependencies import (
    KIND_LUT, KIND_PLUGIN, STATUS_MISSING, STATUS_OK,
    DependencyReport, build_dependency_report, collect_lut_paths,
    collect_plugin_ids, compare_reports,
)


# ---------------------------------------------------------------------------
# 工程数据收集
# ---------------------------------------------------------------------------

def _project_with_stacks(tmp_path, stacks):
    return type("P", (), {"color_stacks": {s.stack_id: s for s in stacks}})()


def _stack_with_layer(layer_lut=None, plugins=(), stack_lut=None):
    from src.shared.project.color_stack import ColorStackData
    layer = Layer("l")
    if layer_lut:
        layer.active_lut_path = layer_lut
    for pid, enabled, ptype in plugins:
        layer.plugins.append({
            "instance_id": pid, "plugin_id": pid,
            "params": {}, "enabled": enabled, "type": ptype,
        })
    base = ColorStackData.create(asset_id="a1")
    return ColorStackData(
        stack_id=base.stack_id,
        lut_path=stack_lut,
        grading_params={"exposure": 1.0},
        layers=(layer.to_dict(),),
    )


def test_collect_lut_paths_dedupes_layer_and_stack_level():
    stack = _stack_with_layer(
        layer_lut="look1.cube", stack_lut="look2.cube")
    other = _stack_with_layer(layer_lut="look1.cube")
    project = _project_with_stacks(None, [stack, other])
    paths = collect_lut_paths(project)
    assert sorted(paths) == ["look1.cube", "look2.cube"]


def test_collect_plugin_ids_only_enabled_dctl():
    stack = _stack_with_layer(plugins=[
        ("fx_a", True, "dctl"),
        ("fx_b", False, "dctl"),   # 禁用不收集
        ("ai_x", True, "ai"),      # AI 插件不收集（模型段另管）
    ])
    project = _project_with_stacks(None, [stack])
    assert collect_plugin_ids(project) == ["fx_a"]


def test_collect_from_project_without_stacks():
    project = type("P", (), {"color_stacks": {}})()
    assert collect_lut_paths(project) == []
    assert collect_plugin_ids(project) == []


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

class _FakePluginManager:
    def __init__(self, plugins):
        self.plugins = plugins

    def get_plugin(self, pid):
        return self.plugins.get(pid)


def test_build_report_lut_ok_and_missing(tmp_path):
    existing = tmp_path / "a.cube"
    existing.write_bytes(b"LUTDATA")
    stack = _stack_with_layer(layer_lut=str(existing))
    missing_stack = _stack_with_layer(layer_lut=str(tmp_path / "gone.cube"))
    project = _project_with_stacks(None, [stack, missing_stack])

    report = build_dependency_report(project, plugin_manager=False)
    luts = report.by_kind(KIND_LUT)
    assert len(luts) == 2
    assert report.ok_count == 1
    assert len(report.missing) == 1
    assert report.missing[0].key.endswith("gone.cube")

    ok_entry = next(e for e in luts if e.status == STATUS_OK)
    assert ok_entry.size_bytes == len(b"LUTDATA")
    assert ok_entry.sha256 and len(ok_entry.sha256) == 64


def test_build_report_resolves_relative_paths(tmp_path):
    lut_dir = tmp_path / "luts"
    lut_dir.mkdir()
    (lut_dir / "rel.cube").write_bytes(b"REL")
    stack = _stack_with_layer(layer_lut="luts/rel.cube")
    project = _project_with_stacks(None, [stack])

    report = build_dependency_report(
        project, base_dir=str(tmp_path), plugin_manager=False)
    entry = report.by_kind(KIND_LUT)[0]
    assert entry.status == STATUS_OK
    assert entry.path == str(lut_dir / "rel.cube")


def test_build_report_plugin_registered_and_missing(tmp_path):
    plugin_file = tmp_path / "fx_a.tglsl"
    plugin_file.write_text("// @name FX A\nvoid main(){}")
    manager = _FakePluginManager({
        "fx_a": {"source_uri": str(plugin_file),
                 "content_hash": "abc123def456", "version": "1.2"},
    })
    stack = _stack_with_layer(plugins=[("fx_a", True, "dctl"),
                                       ("fx_ghost", True, "dctl")])
    project = _project_with_stacks(None, [stack])

    report = build_dependency_report(project, plugin_manager=manager)
    plugins = report.by_kind(KIND_PLUGIN)
    assert len(plugins) == 2

    ok = next(e for e in plugins if e.key == "fx_a")
    assert ok.status == STATUS_OK
    assert ok.version == "1.2"
    assert "abc123def456"[:12] in ok.detail
    assert ok.sha256  # 文件真实哈希

    ghost = next(e for e in plugins if e.key == "fx_ghost")
    assert ghost.status == STATUS_MISSING
    assert "未注册" in ghost.detail


def test_build_report_ai_model_section(tmp_path):
    model_file = tmp_path / "model.onnx"
    model_file.write_bytes(b"MODEL")
    registry = {
        "birefnet": type("M", (), {
            "local_path": str(model_file), "version": "2"})(),
        "sam": type("M", (), {"local_path": None, "version": None})(),
    }
    manager = type("MM", (), {"_registry": registry})()
    project = _project_with_stacks(None, [])

    report = build_dependency_report(
        project, plugin_manager=False, model_manager=manager)
    models = report.by_kind("ai_model")
    assert len(models) == 2
    by_key = {e.key: e for e in models}
    assert by_key["birefnet"].status == STATUS_OK
    assert by_key["birefnet"].version == "2"
    assert by_key["sam"].status == STATUS_MISSING


def test_report_summary_lists_missing():
    report = DependencyReport(entries=[])
    assert report.summary() == "无外部依赖"

    from src.features.color.core.project_dependencies import DependencyEntry
    report = DependencyReport(entries=[
        DependencyEntry(kind=KIND_LUT, key="ok.cube", status=STATUS_OK,
                        sha256="a" * 64),
        DependencyEntry(kind=KIND_LUT, key="gone.cube",
                        status=STATUS_MISSING, detail="文件不存在"),
    ])
    text = report.summary()
    assert "lut: 1/2 可用" in text
    assert "[缺失] gone.cube" in text


# ---------------------------------------------------------------------------
# 序列化与对比
# ---------------------------------------------------------------------------

def test_report_serialization_roundtrip():
    from src.features.color.core.project_dependencies import DependencyEntry
    report = DependencyReport(entries=[
        DependencyEntry(kind=KIND_LUT, key="a.cube", status=STATUS_OK,
                        path="/x/a.cube", size_bytes=10,
                        sha256="f" * 64, version=None, detail=""),
        DependencyEntry(kind=KIND_PLUGIN, key="fx", status=STATUS_MISSING,
                        detail="未注册"),
    ])
    data = json.loads(json.dumps(report.to_dict()))
    restored = DependencyReport.from_dict(data)
    assert restored.entries == report.entries


def test_compare_reports_detects_missing():
    from src.features.color.core.project_dependencies import DependencyEntry
    baseline = DependencyReport(entries=[
        DependencyEntry(kind=KIND_LUT, key="a.cube", status=STATUS_OK,
                        sha256="1" * 64),
    ])
    current = DependencyReport(entries=[
        DependencyEntry(kind=KIND_LUT, key="a.cube", status=STATUS_MISSING,
                        detail="文件不存在"),
    ])
    diffs = compare_reports(baseline, current)
    assert len(diffs) == 1
    assert "缺失" in diffs[0]


def test_compare_reports_detects_same_name_different_content():
    from src.features.color.core.project_dependencies import DependencyEntry
    baseline = DependencyReport(entries=[
        DependencyEntry(kind=KIND_LUT, key="a.cube", status=STATUS_OK,
                        sha256="1" * 64),
    ])
    current = DependencyReport(entries=[
        DependencyEntry(kind=KIND_LUT, key="a.cube", status=STATUS_OK,
                        sha256="2" * 64),
    ])
    diffs = compare_reports(baseline, current)
    assert len(diffs) == 1
    assert "内容不同" in diffs[0]
    assert "静默" in diffs[0]


def test_compare_reports_identical_and_new_dep():
    from src.features.color.core.project_dependencies import DependencyEntry
    entry = DependencyEntry(kind=KIND_LUT, key="a.cube", status=STATUS_OK,
                            sha256="1" * 64)
    assert compare_reports(
        DependencyReport(entries=[entry]),
        DependencyReport(entries=[entry]),
    ) == []

    baseline = DependencyReport(entries=[])
    current = DependencyReport(entries=[entry])
    diffs = compare_reports(baseline, current)
    assert len(diffs) == 1 and "新增依赖" in diffs[0]


# ---------------------------------------------------------------------------
# Dialog（冒烟）
# ---------------------------------------------------------------------------

def test_dependency_dialog_builds_table(qapp, tmp_path):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QTableWidget
    from src.features.color.ui.dependency_report_dialog import (
        DependencyReportDialog,
    )

    lut = tmp_path / "x.cube"
    lut.write_bytes(b"DATA")
    report = build_dependency_report(
        _project_with_stacks(None, [_stack_with_layer(layer_lut=str(lut))]),
        plugin_manager=False)

    dialog = DependencyReportDialog(None, report)
    tables = dialog.findChildren(QTableWidget)
    assert len(tables) == 1
    assert tables[0].rowCount() == len(report.entries)
    assert tables[0].columnCount() == 5
    dialog.deleteLater()


def test_dependency_dialog_export_baseline_writes_json(qapp, tmp_path, monkeypatch):
    from src.features.color.ui.dependency_report_dialog import (
        DependencyReportDialog,
    )
    from PyQt6.QtWidgets import QFileDialog, QMessageBox

    report = build_dependency_report(
        _project_with_stacks(None, []), plugin_manager=False)
    dialog = DependencyReportDialog(None, report)

    out = tmp_path / "baseline.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(out), "")))
    # QMessageBox.information 是模态框，测试中替换为 no-op
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: None))

    dialog._export_baseline()

    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "entries" in data
    dialog.deleteLater()
