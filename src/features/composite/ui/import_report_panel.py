"""
ImportReportPanel — 模型导入报告面板（F12 专业 UI）。

显示 ModelImportReport 的结构化内容：
- 基本信息：backend / 单位 / 轴向
- 资产计数：mesh / 三角面 / 顶点 / 材质 / 纹理 / 嵌入贴图
- 骨骼动画：skin / 骨骼数 / 蒙皮顶点 / 动画片段
- 相机灯光：camera_count / light_count
- 警告与丢失：warnings / lost_features（红色高亮）

设计：
- 纯展示面板（只读），接收 ModelImportReport 对象刷新内容。
- 暗色主题（与 PerfPanel 一致）。
- 折叠分组（计数/警告）。
- 当 report 为 None 时显示「无导入报告」（向后兼容）。
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from src.features.composite.render3d.model_import_report import ModelImportReport
from src.shared.i18n.i18n import I18N


# ──────────────────────────────────────────────────────────────────────
# backend 显示名映射（中文友好）
# ──────────────────────────────────────────────────────────────────────

_BACKEND_DISPLAY = {
    "trimesh": "trimesh（glTF/OBJ）",
    "pyassimp": "pyassimp（FBX/3DS）",
    "assimp_cli_glb": "assimp CLI → GLB",
    "unknown": "未知",
}


def _backend_display_name(backend: str) -> str:
    return _BACKEND_DISPLAY.get(backend, backend)


def _format_bytes(value: object) -> str:
    """Format a byte count without exposing locale-dependent UI state."""
    try:
        amount = max(0, float(value))
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KiB", "MiB", "GiB")
    unit_index = 0
    while amount >= 1024.0 and unit_index < len(units) - 1:
        amount /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(amount):,} {units[unit_index]}"
    return f"{amount:.1f} {units[unit_index]}"


# ──────────────────────────────────────────────────────────────────────
# ImportReportPanel
# ──────────────────────────────────────────────────────────────────────


class ImportReportPanel(QWidget):
    """模型导入报告面板（只读展示 ModelImportReport）。

    用法：
        panel = ImportReportPanel()
        panel.update_report(report)  # report: ModelImportReport | None
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._report: Optional[ModelImportReport] = None
        self._runtime_error: str = ""
        self._runtime_node_id: str = ""
        self._import_status: str = ""
        self._import_phase: str = ""
        self._import_progress: float = 0.0
        self._runtime_diagnostics: tuple[dict, ...] = ()
        self._init_ui()

    # ── UI 构建 ──────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # 标题
        title = QLabel(I18N.tr("panel.import_report", "导入报告"))
        title.setStyleSheet(
            "color: #ddd; font-weight: bold; font-size: 12px;"
        )
        layout.addWidget(title)

        # P2-03：导入进度与报告共用一个面板。进度条只显示 worker 的
        # 标量快照，不持有 Future 或任何 render/GL 对象。
        self._import_state_label = QLabel(I18N.tr("import_report.not_started", "未开始"))
        self._import_state_label.setStyleSheet(
            "color: #aaa; font-size: 11px; margin-top: 2px;"
        )
        layout.addWidget(self._import_state_label)
        self._import_progress_bar = QProgressBar()
        self._import_progress_bar.setRange(0, 1000)
        self._import_progress_bar.setTextVisible(True)
        self._import_progress_bar.setFormat("%p%")
        self._import_progress_bar.setMaximumHeight(12)
        self._import_progress_bar.setStyleSheet(
            "QProgressBar { color: #ccc; background: #252525; border: 0; "
            "text-align: center; } QProgressBar::chunk { background: #6b8fd3; }"
        )
        self._import_progress_bar.hide()
        layout.addWidget(self._import_progress_bar)
        self._resource_label = self._make_row(layout, "资源预估", "—")
        self._format_label = self._make_row(layout, "格式能力", "—")

        # 基本信息区
        self._backend_label = self._make_row(layout, "Backend", "—")
        self._unit_label = self._make_row(layout, "单位 / 轴向", "—")
        self._path_label = self._make_row(layout, "路径", "—")
        self._node_label = self._make_row(layout, "节点", "—")

        # 资产计数区
        layout.addWidget(self._make_section_label("资产"))
        self._mesh_label = self._make_row(layout, "  Mesh", "0")
        self._tri_label = self._make_row(layout, "  三角面", "0")
        self._vert_label = self._make_row(layout, "  顶点", "0")
        self._mat_label = self._make_row(layout, "  材质", "0")
        self._tex_label = self._make_row(layout, "  纹理", "0")
        self._morph_label = self._make_row(layout, "  Morph", "0")
        self._embed_label = self._make_row(layout, "  嵌入贴图", "0")

        # 骨骼动画区
        layout.addWidget(self._make_section_label("骨骼 / 动画"))
        self._skin_label = self._make_row(layout, "  蒙皮", "0")
        self._bone_label = self._make_row(layout, "  骨骼", "0")
        self._weight_label = self._make_row(layout, "  蒙皮顶点", "0")
        self._anim_label = self._make_row(layout, "  动画片段", "0")

        # 相机灯光区
        layout.addWidget(self._make_section_label("相机 / 灯光"))
        self._cam_label = self._make_row(layout, "  相机", "0")
        self._light_label = self._make_row(layout, "  灯光", "0")

        # 警告与丢失
        self._warn_section = self._make_section_label("⚠ 警告 / 丢失")
        self._warn_section.setStyleSheet(
            "color: #e8a848; font-size: 11px; font-weight: bold; margin-top: 6px;"
        )
        layout.addWidget(self._warn_section)
        self._warn_label = QLabel("—")
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet(
            "color: #e88; font-size: 10px; padding: 2px 0 2px 8px;"
        )
        layout.addWidget(self._warn_label)

        layout.addStretch()

    def _make_section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #999; font-size: 11px; font-weight: bold; margin-top: 6px;"
        )
        return lbl

    def _make_row(self, parent_layout: QVBoxLayout, label: str, value: str) -> QLabel:
        """构建一行「label: value」，返回 value 的 QLabel（便于后续更新）。"""
        row = QHBoxLayout()
        key_lbl = QLabel(label)
        key_lbl.setStyleSheet("color: #888; font-size: 11px;")
        key_lbl.setMinimumWidth(80)
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet("color: #ccc; font-size: 11px;")
        row.addWidget(key_lbl)
        row.addWidget(val_lbl)
        row.addStretch()
        # 包装进 QFrame 便于样式控制
        frame = QFrame()
        frame.setLayout(row)
        parent_layout.addWidget(frame)
        return val_lbl

    # ── 数据刷新 ────────────────────────────────────────────────────

    def update_import_state(
            self, status: str, progress: float = 0.0, phase: str = "") -> None:
        """显示后台导入的阶段化状态。

        此方法只接受渲染诊断中的基础类型，故可由 Qt 主线程直接调用。
        """
        self._import_status = str(status or "").strip().lower()
        self._import_phase = str(phase or "").strip().lower()
        try:
            self._import_progress = max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            self._import_progress = 0.0

        phase_names = {
            "queued": "排队",
            "probe": "预检格式",
            "parse": "解析模型",
            "handoff": "交接渲染线程",
            "ready": "已就绪",
            "done": "解析完成",
            "failed": "导入失败",
            "cancelled": "已取消",
        }
        status_names = {
            "loading": "加载中",
            "cancelling": "取消中",
            "handoff": "准备渲染",
            "ready": "已就绪",
            "error": "导入失败",
            "cancelled": "已取消",
        }
        if not self._import_status:
            text = I18N.tr("import_report.not_started", "未开始")
        else:
            detail = phase_names.get(self._import_phase, self._import_phase)
            text = status_names.get(self._import_status, self._import_status)
            if detail and detail not in text:
                text = f"{text} · {detail}"
        self._import_state_label.setText(text)
        self._import_progress_bar.setValue(round(self._import_progress * 1000.0))
        self._import_progress_bar.setVisible(
            self._import_status in {"loading", "cancelling", "handoff"})

    def update_resource_estimate(self, estimate: Optional[dict]) -> None:
        """显示导入前资源预估及显式预算的结果。"""
        if not isinstance(estimate, dict) or not estimate:
            self._resource_label.setText("—")
            self._resource_label.setStyleSheet("color: #ccc; font-size: 11px;")
            return
        total = _format_bytes(estimate.get("total_bytes", 0))
        budget = estimate.get("budget_bytes")
        over_budget = bool(estimate.get("over_budget", False))
        text = f"约 {total}"
        if budget is not None:
            text += f" / 预算 {_format_bytes(budget)}"
        if over_budget:
            text += "（超预算）"
        self._resource_label.setText(text)
        self._resource_label.setToolTip(
            f"CPU: {_format_bytes(estimate.get('cpu_bytes', 0))}; "
            f"GPU: {_format_bytes(estimate.get('gpu_bytes', 0))}; "
            f"置信度: {estimate.get('confidence', 'unknown')}"
        )
        color = "#e88" if over_budget else "#ccc"
        self._resource_label.setStyleSheet(
            f"color: {color}; font-size: 11px;")

    def update_format_probe(self, probe: Optional[dict]) -> None:
        """显示格式矩阵与当前运行时后端的一致性结论。"""
        if not isinstance(probe, dict) or not probe:
            self._format_label.setText("—")
            self._format_label.setToolTip("")
            self._format_label.setStyleSheet("color: #ccc; font-size: 11px;")
            return
        format_id = str(probe.get("format_id") or "unknown").upper()
        backend = str(probe.get("backend") or "unknown")
        reason_code = str(probe.get("reason_code") or "")
        if bool(probe.get("can_import")):
            text = f"{format_id} · {_backend_display_name(backend)}"
            color = "#9cdb9c"
        elif reason_code == "format.planned":
            text = f"{format_id} · 未接入后端"
            color = "#e8a848"
        elif reason_code == "backend.unavailable":
            text = f"{format_id} · 后端不可用"
            color = "#e88"
        else:
            text = f"{format_id} · {reason_code or '不可导入'}"
            color = "#e88"
        self._format_label.setText(text)
        self._format_label.setToolTip(
            str(probe.get("capability_notes") or probe.get("reason") or ""))
        self._format_label.setStyleSheet(
            f"color: {color}; font-size: 11px;")

    def update_diagnostics(self, records: object) -> None:
        """Replace the panel's detached runtime diagnostic snapshot."""
        if not isinstance(records, (list, tuple)):
            self._runtime_diagnostics = ()
            return
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                continue
            code = str(record.get("code") or "").strip()
            message = str(record.get("message") or "").strip()
            if not code or not message:
                continue
            normalized.append({
                "code": code,
                "severity": str(record.get("severity") or "warning"),
                "message": message,
                "suggestion": str(record.get("suggestion") or ""),
            })
        self._runtime_diagnostics = tuple(normalized)

    def _diagnostic_lines(self) -> list[str]:
        """Render normalized runtime records for the warning area."""
        markers = {"error": "✗", "warning": "⚠", "info": "ℹ"}
        lines = []
        for record in self._runtime_diagnostics:
            marker = markers.get(record["severity"], "⚠")
            line = f"{marker} [{record['code']}] {record['message']}"
            suggestion = record.get("suggestion", "")
            if suggestion:
                line += f" → {suggestion}"
            lines.append(line)
        return lines

    def update_report(
            self,
            report: Optional[ModelImportReport],
            *,
            runtime_error: str = "",
            runtime_node_id: str = "",
    ) -> None:
        """用新的 ModelImportReport 刷新面板内容。

        Args:
            report: 导入报告；None 显示「无报告」占位。
        """
        self._report = report
        self._runtime_error = str(runtime_error or "").strip()
        self._runtime_node_id = str(runtime_node_id or "").strip()
        if report is None:
            self._show_no_report()
            return

        self._backend_label.setText(_backend_display_name(report.loader_backend))
        self._unit_label.setText(
            f"{report.unit_scale}m / {report.axis_mode}")
        # 路径只显示文件名（完整路径太长）
        path = report.source_path
        self._path_label.setText(os.path.basename(path) if path else "—")
        self._path_label.setToolTip(path)
        self._node_label.setText(self._runtime_node_id or "—")

        self._mesh_label.setText(str(report.mesh_count))
        self._tri_label.setText(f"{report.triangle_count:,}")
        self._vert_label.setText(f"{report.vertex_count:,}")
        self._mat_label.setText(str(report.material_count))
        self._tex_label.setText(str(report.texture_count))
        self._morph_label.setText(str(report.morph_target_count))
        self._embed_label.setText(str(report.embedded_textures_extracted))

        self._skin_label.setText(str(report.skeleton_count))
        self._bone_label.setText(str(report.bone_count))
        self._weight_label.setText(f"{report.skin_weight_count:,}")
        self._anim_label.setText(str(report.animation_clip_count))

        self._cam_label.setText(str(report.camera_count))
        self._light_label.setText(str(report.light_count))

        # 警告与丢失
        lines = self._diagnostic_lines()
        record_messages = {record["message"] for record in self._runtime_diagnostics}
        if self._runtime_error and self._runtime_error not in record_messages:
            lines.append(f"✗ 错误: {self._runtime_error}")
        for w in report.warnings:
            lines.append(f"⚠ {w}")
        for lf in report.lost_features:
            lines.append(f"✗ 丢失: {lf}")
        if lines:
            self._warn_label.setText("\n".join(lines))
            self._warn_section.show()
            self._warn_label.show()
        else:
            self._warn_label.setText(I18N.tr("import_report.none", "无"))
            self._warn_section.show()
            self._warn_label.show()

    def update_summary(self, summary: Optional[dict]) -> None:
        """Update from a render-thread-safe report summary dictionary."""
        if not isinstance(summary, dict):
            self.update_import_state("")
            self.update_resource_estimate(None)
            self.update_format_probe(None)
            self.update_diagnostics(())
            self.update_report(None)
            return
        # A cached report emitted by a selection refresh may not contain an
        # import-state block. Preserve the latest progress in that case;
        # otherwise selecting a node would make an active load look idle.
        state_keys = {
            "model_import_status", "model_import_phase",
            "model_import_progress",
        }
        if state_keys.intersection(summary):
            self.update_import_state(
                str(summary.get("model_import_status") or ""),
                summary.get("model_import_progress", 0.0),
                str(summary.get("model_import_phase") or ""),
            )
        if "model_import_resource_estimate" in summary:
            self.update_resource_estimate(
                summary.get("model_import_resource_estimate"))
        if "model_import_probe" in summary:
            self.update_format_probe(summary.get("model_import_probe"))
        if "diagnostic_records" in summary:
            self.update_diagnostics(summary.get("diagnostic_records"))
        self.update_report(
            ModelImportReport.from_summary_dict(summary),
            runtime_error=str(summary.get("model_error") or ""),
            runtime_node_id=str(summary.get("model_node_id") or ""),
        )

    def _show_no_report(self) -> None:
        """无报告时的占位显示。"""
        self._backend_label.setText(
            I18N.tr("import_report.no_report", "—（无导入报告）"))
        self._unit_label.setText("—")
        self._path_label.setText("—")
        self._path_label.setToolTip("")
        self._node_label.setText(self._runtime_node_id or "—")
        for lbl in (self._mesh_label, self._tri_label, self._vert_label,
                    self._mat_label, self._tex_label, self._morph_label,
                    self._embed_label,
                    self._skin_label, self._bone_label, self._weight_label,
                    self._anim_label, self._cam_label, self._light_label):
            lbl.setText("—")
        lines = self._diagnostic_lines()
        if self._runtime_error and self._runtime_error not in {
                record["message"] for record in self._runtime_diagnostics}:
            lines.insert(0, f"✗ 错误: {self._runtime_error}")
        self._warn_label.setText("\n".join(lines) if lines else "—")
