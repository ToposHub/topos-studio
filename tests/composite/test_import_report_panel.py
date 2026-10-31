"""
ImportReportPanel 测试（F12 专业 UI）。

覆盖：
- 默认状态（无报告）
- 完整报告刷新（计数/警告/丢失）
- backend 显示名映射
- None 报告占位
- 数字格式化（千分位）

需要 PyQt6 offscreen 环境。
"""

import os

import pytest

# 确保 offscreen（CI 无显示器）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from src.features.composite.render3d.model_import_report import (  # noqa: E402
    LOADER_BACKEND_ASSIMP_CLI_GLB,
    LOADER_BACKEND_PYASSIMP,
    LOADER_BACKEND_TRIMESH,
    ModelImportReport,
)
from src.features.composite.ui.import_report_panel import (  # noqa: E402
    ImportReportPanel,
    _backend_display_name,
)


@pytest.fixture(scope="module")
def qapp():
    """共享一个 QApplication 实例。"""
    app = QApplication.instance() or QApplication([])
    yield app


def _make_report(**kw) -> ModelImportReport:
    defaults = dict(source_path="/tmp/model.glb", loader_backend="trimesh")
    defaults.update(kw)
    return ModelImportReport(**defaults)


# ──────────────────────────────────────────────────────────────────────
# _backend_display_name
# ──────────────────────────────────────────────────────────────────────


class TestBackendDisplayName:
    def test_trimesh(self):
        assert "trimesh" in _backend_display_name(LOADER_BACKEND_TRIMESH)

    def test_pyassimp(self):
        assert "pyassimp" in _backend_display_name(LOADER_BACKEND_PYASSIMP)

    def test_cli(self):
        assert "CLI" in _backend_display_name(LOADER_BACKEND_ASSIMP_CLI_GLB)

    def test_unknown_passthrough(self):
        assert _backend_display_name("custom_backend") == "custom_backend"


# ──────────────────────────────────────────────────────────────────────
# ImportReportPanel
# ──────────────────────────────────────────────────────────────────────


class TestImportReportPanelDefaults:
    def test_default_no_report(self, qapp):
        panel = ImportReportPanel()
        panel.update_report(None)
        assert "—" in panel._backend_label.text()

    def test_default_counts_dash(self, qapp):
        panel = ImportReportPanel()
        panel.update_report(None)
        assert panel._mesh_label.text() == "—"
        assert panel._tri_label.text() == "—"


class TestImportReportPanelUpdate:
    def test_runtime_diagnostics_show_code_and_suggestion(self, qapp):
        panel = ImportReportPanel()
        panel.update_summary({
            "diagnostic_records": [{
                "code": "format.planned",
                "severity": "error",
                "message": "USD 后端尚未接入",
                "suggestion": "改用 GLB",
            }],
        })
        text = panel._warn_label.text()
        assert "[format.planned]" in text
        assert "USD 后端尚未接入" in text
        assert "改用 GLB" in text

    def test_runtime_diagnostic_is_not_duplicated_by_runtime_error(self, qapp):
        panel = ImportReportPanel()
        panel.update_summary({
            "model_error": "asset.missing: 文件不存在",
            "diagnostic_records": [{
                "code": "asset.missing",
                "severity": "error",
                "message": "asset.missing: 文件不存在",
            }],
        })
        assert panel._warn_label.text().count("[asset.missing]") == 1

    def test_planned_format_probe_is_explicit(self, qapp):
        panel = ImportReportPanel()
        panel.update_format_probe({
            "format_id": "usd",
            "backend": "unknown",
            "can_import": False,
            "reason_code": "format.planned",
            "reason": "planned",
            "capability_notes": "USD 后端待接入",
        })
        assert panel._format_label.text() == "USD · 未接入后端"
        assert "USD 后端待接入" in panel._format_label.toolTip()

    def test_ready_format_probe_shows_backend(self, qapp):
        panel = ImportReportPanel()
        panel.update_format_probe({
            "format_id": "gltf",
            "backend": "trimesh",
            "can_import": True,
            "reason_code": "ready",
        })
        assert "GLTF" in panel._format_label.text()
        assert "trimesh" in panel._format_label.text()

    def test_resource_estimate_is_visible(self, qapp):
        panel = ImportReportPanel()
        panel.update_resource_estimate({
            "source_bytes": 1024,
            "cpu_bytes": 1024 * 1024,
            "gpu_bytes": 2 * 1024 * 1024,
            "total_bytes": 3 * 1024 * 1024,
            "budget_bytes": 2 * 1024 * 1024,
            "over_budget": True,
            "confidence": "source_size_heuristic",
        })
        assert "3.0 MiB" in panel._resource_label.text()
        assert "超预算" in panel._resource_label.text()
        assert "CPU" in panel._resource_label.toolTip()

    def test_resource_estimate_clears(self, qapp):
        panel = ImportReportPanel()
        panel.update_resource_estimate({"total_bytes": 1024})
        panel.update_resource_estimate(None)
        assert panel._resource_label.text() == "—"

    def test_import_progress_shows_phase(self, qapp):
        panel = ImportReportPanel()
        panel.update_import_state("loading", 0.15, "parse")
        assert "加载中" in panel._import_state_label.text()
        assert "解析模型" in panel._import_state_label.text()
        assert panel._import_progress_bar.value() == 150
        assert not panel._import_progress_bar.isHidden()

    def test_import_progress_hides_when_ready(self, qapp):
        panel = ImportReportPanel()
        panel.update_import_state("ready", 1.0, "ready")
        assert panel._import_state_label.text() == "已就绪"
        assert panel._import_progress_bar.value() == 1000
        assert panel._import_progress_bar.isHidden()

    def test_import_state_round_trips_through_summary(self, qapp):
        panel = ImportReportPanel()
        panel.update_summary({
            "model_import_status": "loading",
            "model_import_phase": "handoff",
            "model_import_progress": 0.85,
            "model_import_resource_estimate": {"total_bytes": 1024},
            "model_import_probe": {
                "format_id": "gltf", "backend": "trimesh",
                "can_import": True, "reason_code": "ready",
            },
        })
        assert panel._import_status == "loading"
        assert panel._import_phase == "handoff"
        assert panel._import_progress == 0.85
        assert "1.0 KiB" in panel._resource_label.text()
        assert "GLTF" in panel._format_label.text()

    def test_report_refresh_without_state_preserves_active_import(self, qapp):
        panel = ImportReportPanel()
        panel.update_import_state("loading", 0.15, "parse")
        panel.update_summary({"source_path": "/tmp/model.glb"})
        assert panel._import_status == "loading"
        assert panel._import_phase == "parse"
        assert panel._import_progress == 0.15

    def test_basic_counts(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(mesh_count=5, triangle_count=12000, vertex_count=8000)
        panel.update_report(r)
        assert panel._mesh_label.text() == "5"
        assert panel._tri_label.text() == "12,000"
        assert panel._vert_label.text() == "8,000"

    def test_skeleton_animation(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(skeleton_count=2, bone_count=45,
                         skin_weight_count=5000, animation_clip_count=3)
        panel.update_report(r)
        assert panel._skin_label.text() == "2"
        assert panel._bone_label.text() == "45"
        assert panel._weight_label.text() == "5,000"
        assert panel._anim_label.text() == "3"

    def test_camera_light(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(camera_count=1, light_count=2)
        panel.update_report(r)
        assert panel._cam_label.text() == "1"
        assert panel._light_label.text() == "2"

    def test_unit_axis(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(unit_scale=0.01, axis_mode="z_up")
        panel.update_report(r)
        assert "0.01" in panel._unit_label.text()
        assert "z_up" in panel._unit_label.text()

    def test_path_shows_basename(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(source_path="/abs/path/to/model.fbx")
        panel.update_report(r)
        assert panel._path_label.text() == "model.fbx"

    def test_path_tooltip_full(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(source_path="/abs/path/to/model.fbx")
        panel.update_report(r)
        assert panel._path_label.toolTip() == "/abs/path/to/model.fbx"

    def test_warnings_displayed(self, qapp):
        panel = ImportReportPanel()
        r = _make_report()
        r.add_warning("mesh 无 UV")
        r.add_warning("材质贴图路径为空")
        panel.update_report(r)
        text = panel._warn_label.text()
        assert "mesh 无 UV" in text
        assert "材质贴图路径为空" in text

    def test_lost_features_displayed(self, qapp):
        panel = ImportReportPanel()
        r = _make_report()
        r.add_lost_feature("骨骼动画")
        panel.update_report(r)
        assert "骨骼动画" in panel._warn_label.text()

    def test_no_warnings_shows_none(self, qapp):
        panel = ImportReportPanel()
        r = _make_report()  # 无警告
        panel.update_report(r)
        assert panel._warn_label.text() == "无"

    def test_none_after_report(self, qapp):
        """先有报告再设 None → 占位。"""
        panel = ImportReportPanel()
        r = _make_report(mesh_count=5)
        panel.update_report(r)
        assert panel._mesh_label.text() == "5"
        panel.update_report(None)
        assert panel._mesh_label.text() == "—"

    def test_large_numbers_formatted(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(triangle_count=1234567, vertex_count=9876543)
        panel.update_report(r)
        assert panel._tri_label.text() == "1,234,567"
        assert panel._vert_label.text() == "9,876,543"

    def test_embedded_textures(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(embedded_textures_extracted=12)
        panel.update_report(r)
        assert panel._embed_label.text() == "12"

    def test_backend_display(self, qapp):
        panel = ImportReportPanel()
        r = _make_report(loader_backend="pyassimp")
        panel.update_report(r)
        assert "pyassimp" in panel._backend_label.text()

    def test_runtime_error_is_tied_to_model_node(self, qapp):
        """渲染线程的模型错误应在报告面板显示并带节点定位。"""
        panel = ImportReportPanel()
        panel.update_summary({
            "source_path": "/abs/path/to/model.fbx",
            "loader_backend": "pyassimp",
            "model_node_id": "model-17",
            "model_error": "asset.missing: 模型文件不存在。",
        })

        assert panel._node_label.text() == "model-17"
        assert "asset.missing" in panel._warn_label.text()
        assert "模型文件不存在" in panel._warn_label.text()

    def test_runtime_error_clears_with_no_report(self, qapp):
        panel = ImportReportPanel()
        panel.update_summary({
            "model_node_id": "model-17",
            "model_error": "解析失败",
        })
        panel.update_summary(None)

        assert panel._node_label.text() == "—"
        assert panel._warn_label.text() == "—"
