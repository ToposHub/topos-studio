"""B2 Stem 分轨导出对话框（AM-10 阶段 3.5 生产入口）。

从当前时间线规划 stem（Master / 单轨 / Bus / D-M-E 分组），用户勾选后经
QThread worker 调用 :func:`src.features.edit.core.stem_export.export_all_stems`
写出 WAV（48kHz/24bit）。禁止 UI 线程直跑渲染。

入口：
- Edit→File→Export Stems...（file_controller.export_stem_tracks）
- Deliver 页 "Export Stems…" 按钮（deliver_page）

取消语义：worker 在渲染块和回读块边界轮询取消。当前批次仅在全部
文件通过后发布；失败/取消清理暂存，已有交付文件保持不变。
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
import tempfile
from typing import Any, Callable, List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.core.stem_export import (
    StemSpec,
    StemType,
    export_all_stems,
    plan_stems_from_sequence,
)
from src.features.edit.core.nested_sequence import NestedAudioExpansionError
from src.features.edit.core.delivery_profile import (
    DeliveryProfile, StemRule, publish_delivery_atomic, DeliveryPublicationCancelled,
)
from src.features.edit.core.delivery_preset import FULL_STEM_SET
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

#: 输出能力（write_stem_wav 现状，勿超范围）
STEM_SAMPLE_RATE = 48000
STEM_CHANNELS = 2
STEM_BIT_DEPTH = 24

_STEM_TYPE_LABELS = {
    StemType.MASTER: "Master 混音",
    StemType.TRACK: "单轨",
    StemType.BUS: "Bus",
    StemType.GROUP: "分组",
}


class StemExportWorker(QThread):
    """QThread 包装 export_all_stems：进度、取消、完成信号。

    解码器上下文（AudioDecoder 句柄）只能在本线程内创建/释放，因此
    decode factory 的 close 在 run() 收尾处调用，而不是 UI 线程。
    """

    progress_changed = pyqtSignal(int, int, str)  # current, total, stem_name
    finished_ok = pyqtSignal(object)  # StemExportResult
    failed = pyqtSignal(str)  # error message

    def __init__(
        self,
        sequence: Any,
        project: Any,
        output_dir: str,
        specs: Tuple[StemSpec, ...],
        prefix: str = "",
        parent: Optional[QWidget] = None,
        *,
        sequence_resolver: Optional[Callable] = None,
        delivery_profile: Optional[DeliveryProfile] = None,
    ) -> None:
        super().__init__(parent)
        self._sequence = sequence
        self._project = project
        self._sequence_resolver = sequence_resolver
        self._asset_lookup = dict(getattr(project, "assets", {}) or {}).get
        self.failure_diagnostics: tuple = ()
        self._output_dir = output_dir
        self._specs = specs
        # Normalize before assigning final spec filenames, avoiding duplicate separators.
        self._prefix = (prefix or "").strip().rstrip("_- ")
        # Default is an evidence profile, not a customer loudness target. It
        # verifies encoded WAV integrity and records measurements without
        # normalizing audio or imposing an unsolicited LUFS/peak requirement.
        self._delivery_profile = delivery_profile or DeliveryProfile(
            profile_id="topos-stem-technical-v1", name="Stem WAV technical readback",
            file_format="wav", target_lufs=None, max_true_peak_dbtp=None,
            max_sample_peak_dbfs=None,
            stems=tuple(StemRule(stem, allow_silence=True) for stem in FULL_STEM_SET),
        )
        self._cancel_requested = False

    def cancel(self) -> None:
        """请求取消（下个渲染/回读块边界生效）。"""
        self._cancel_requested = True

    def run(self) -> None:
        try:
            from src.features.edit.core.stem_export import (
                create_clip_decode_fn, create_pre_render_base_fn,
            )

            decode_fn, close_fn = create_clip_decode_fn(
                self._project,
                float(getattr(self._sequence, "fps", 24.0) or 24.0),
                STEM_SAMPLE_RATE,
            )
            # §3.3：预渲染处理栈（STFT 类步骤）的缓存基底。
            pre_render_base_fn = create_pre_render_base_fn(
                self._project, self._sequence)
            decoders_closed = False
            try:
                output = Path(self._output_dir)
                output.mkdir(parents=True, exist_ok=True)
                safe_prefix = "".join(
                    c if c.isalnum() or c in "-_" else "_" for c in self._prefix)
                specs = [replace(spec, filename_override=(
                    f"{safe_prefix}_{spec.filename}" if safe_prefix else spec.filename))
                    for spec in self._specs]
                # Render and inspect in a task-owned directory; no failed or
                # cancelled job can overwrite a user's existing delivery.
                with tempfile.TemporaryDirectory(prefix=".stem-job-", dir=output) as staging:
                    result = export_all_stems(
                        self._sequence, staging,
                        decode_clip_fn=decode_fn,
                        pre_render_base_fn=pre_render_base_fn,
                        sample_rate=STEM_SAMPLE_RATE,
                        channels=STEM_CHANNELS,
                        bit_depth=STEM_BIT_DEPTH,
                        specs=specs,
                        cancel_check=lambda: self._cancel_requested,
                        progress_callback=self._emit_progress,
                        sequence_resolver=self._sequence_resolver,
                        asset_lookup=self._asset_lookup,
                        qc_profile=self._delivery_profile,
                    )
                    if self._cancel_requested:
                        result.cancelled = True
                    # Resource cleanup is part of job success. Do it before
                    # publication so a cleanup exception cannot report failure
                    # after approved files have already been overwritten.
                    close_fn()
                    decoders_closed = True
                    if self._cancel_requested:
                        result.cancelled = True
                    if not result.cancelled:
                        if not result.all_succeeded:
                            raise ValueError("Stem render/readback failed: " + "; ".join(
                                result.qc_errors or [name for name, ok in result.stems if not ok]))
                        paths = list(result.output_files)
                        paths += [result.routing_manifest_path, result.delivery_manifest_path]
                        files = {str(index): Path(path) for index, path in enumerate(paths) if path}
                        checksums = {
                            key: result.measurements[path.name].file_sha256
                            for key, path in files.items() if path.suffix.lower() == ".wav"
                        }
                        try:
                            publish_delivery_atomic(
                                files, output, expected_checksums=checksums,
                                cancel_check=lambda: self._cancel_requested)
                        except DeliveryPublicationCancelled:
                            result.cancelled = True
                    if not result.cancelled:
                        result.output_files = [str(output / Path(path).name)
                                               for path in result.output_files]
                        if result.routing_manifest_path:
                            result.routing_manifest_path = str(output / "routing.json")
                        if result.delivery_manifest_path:
                            result.delivery_manifest_path = str(output / "delivery.json")
                    else:
                        result.stems = [(name, False) for name, _ok in result.stems]
                        result.output_files = []
                        result.routing_manifest_path = ""
                        result.delivery_manifest_path = ""
                    result.output_dir = self._output_dir
            finally:
                if not decoders_closed:
                    close_fn()

            self.progress_changed.emit(len(result.stems), len(result.stems), "")
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001 — worker 边界，错误回 UI
            self.failure_diagnostics = tuple(getattr(exc, "diagnostics", ()))
            logger.exception("Stem export worker failed")
            self.failed.emit(str(exc))

    def _emit_progress(self, current: int, total: int, name: str) -> None:
        self.progress_changed.emit(current, total, name)

class StemExportDialog(QDialog):
    """Stem 规划勾选 + 输出配置 + worker 进度/结果摘要。

    空 sequence / 无音频轨时进入空态：说明文案 + 只读关闭，不崩溃。
    """

    def __init__(
        self,
        sequence: Any,
        project: Any = None,
        parent: Optional[QWidget] = None,
        *,
        sequence_resolver: Optional[Callable] = None,
    ) -> None:
        super().__init__(parent)
        self._sequence = sequence
        self._project = project
        self._sequence_resolver = sequence_resolver
        self.failure_diagnostics: tuple = ()
        self._planning_error = ""
        self._worker: Optional[StemExportWorker] = None
        self._spec_boxes: List[Tuple[StemSpec, QCheckBox]] = []

        self.setWindowTitle(I18N.tr("edit.stem_export.title", "导出 Stem 分轨"))
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)

        # 1. Stem 规划列表
        plan_group = QGroupBox(I18N.tr("edit.stem_export.plan", "Stem 分组"))
        plan_layout = QVBoxLayout(plan_group)
        specs: List[StemSpec] = []
        if sequence is not None:
            try:
                from src.features.edit.core.timeline_export import expand_sequence_audio_for_render

                planning_sequence = expand_sequence_audio_for_render(
                    sequence, sequence_resolver, project, strict=True,
                )
                specs = plan_stems_from_sequence(planning_sequence)
            except NestedAudioExpansionError as exc:
                self.failure_diagnostics = exc.diagnostics
                self._planning_error = I18N.tr(
                    "edit.stem_export.plan_failed", "Stem 规划失败：{msg}", msg=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 — UI boundary; explicit failure
                self._planning_error = I18N.tr(
                    "edit.stem_export.plan_failed", "Stem 规划失败：{msg}",
                    msg=str(exc) or type(exc).__name__,
                )
                logger.warning("plan_stems_from_sequence failed", exc_info=True)
        for spec in specs:
            box = QCheckBox(
                I18N.tr(
                    "edit.stem_export.plan_row",
                    "[{kind}] {name} — {n} 轨",
                    kind=_STEM_TYPE_LABELS.get(spec.stem_type, spec.stem_type.value),
                    name=spec.name,
                    n=len(spec.track_ids),
                )
            )
            box.setChecked(True)
            self._spec_boxes.append((spec, box))
            plan_layout.addWidget(box)
        if not self._spec_boxes and not self._planning_error:
            plan_layout.addWidget(QLabel(
                I18N.tr(
                    "edit.stem_export.empty",
                    "当前时间线没有音频轨，无 stem 可导出。",
                )
            ))
        layout.addWidget(plan_group)

        # 2. 输出配置
        out_group = QGroupBox(I18N.tr("edit.stem_export.output", "输出"))
        out_form = QFormLayout(out_group)

        dir_row = QHBoxLayout()
        self._dir_input = QLineEdit()
        self._dir_input.setPlaceholderText(
            I18N.tr("edit.stem_export.dir_placeholder", "选择输出目录…")
        )
        dir_row.addWidget(self._dir_input, 1)
        browse_btn = QPushButton(I18N.tr("edit.stem_export.browse", "浏览…"))
        browse_btn.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(browse_btn)
        out_form.addRow(
            I18N.tr("edit.stem_export.dir_label", "输出目录:"), dir_row
        )

        self._prefix_input = QLineEdit()
        self._prefix_input.setPlaceholderText(
            I18N.tr("edit.stem_export.prefix_placeholder", "可选，如 MyFilm_")
        )
        out_form.addRow(
            I18N.tr("edit.stem_export.prefix_label", "文件名前缀:"),
            self._prefix_input,
        )

        format_label = QLabel(
            I18N.tr("edit.stem_export.format_wav_stereo", "WAV · 48 kHz · 24 bit · 立体声"))
        out_form.addRow(I18N.tr("edit.stem_export.format_label", "格式:"), format_label)
        layout.addWidget(out_group)

        # 3. 进度
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

        self._status_label = QLabel(self._planning_error)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # 4. 按钮
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
        )
        self._export_btn = self._button_box.addButton(
            I18N.tr("edit.stem_export.export", "导出"),
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self._export_btn.setEnabled(bool(self._spec_boxes) and not self._planning_error)
        self._cancel_btn = self._button_box.addButton(
            I18N.tr("edit.stem_export.cancel", "取消导出"),
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self._cancel_btn.setVisible(False)
        self._button_box.rejected.connect(self.reject)
        self._export_btn.clicked.connect(self._start_export)
        self._cancel_btn.clicked.connect(self._request_cancel)
        layout.addWidget(self._button_box)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _browse_output_dir(self) -> None:
        target = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("edit.stem_export.dir_picker", "选择 stem 输出目录"),
            self._dir_input.text() or os.path.expanduser("~"),
        )
        if target:
            self._dir_input.setText(target)

    def _selected_specs(self) -> List[StemSpec]:
        return [
            spec for spec, box in self._spec_boxes if box.isChecked()
        ]

    def _start_export(self) -> None:
        if self._planning_error:
            self._status_label.setText(self._planning_error)
            self._export_btn.setEnabled(False)
            return
        if self._worker is not None and self._worker.isRunning():
            return
        output_dir = self._dir_input.text().strip()
        if not output_dir:
            QMessageBox.information(
                self,
                I18N.tr("edit.stem_export.title", "导出 Stem 分轨"),
                I18N.tr("edit.stem_export.need_dir", "请先选择输出目录。"),
            )
            return
        specs = self._selected_specs()
        if not specs:
            QMessageBox.information(
                self,
                I18N.tr("edit.stem_export.title", "导出 Stem 分轨"),
                I18N.tr("edit.stem_export.none_selected", "请至少勾选一个 stem。"),
            )
            return

        self._switch_running(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(0)
        self._status_label.setText(
            I18N.tr("edit.stem_export.running", "正在导出 stem…")
        )
        self._worker = StemExportWorker(
            self._sequence,
            self._project,
            output_dir,
            tuple(specs),
            prefix=self._prefix_input.text(),
            parent=self,
            sequence_resolver=self._sequence_resolver,
        )
        self._worker.progress_changed.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._cancel_btn.setEnabled(False)
            self._status_label.setText(
                I18N.tr("edit.stem_export.cancelling_block", "正在取消…")
            )

    def _switch_running(self, running: bool) -> None:
        self._export_btn.setEnabled(
            not running and bool(self._spec_boxes) and not self._planning_error)
        self._cancel_btn.setVisible(running)
        self._cancel_btn.setEnabled(running)
        close_btn = self._button_box.button(QDialogButtonBox.StandardButton.Close)
        # AUD-20910: worker 未退出期间 Close 保持禁用；结束（成功/失败）后恢复。
        if close_btn is not None:
            close_btn.setEnabled(not running and not self._worker_running())
        for _spec, box in self._spec_boxes:
            box.setEnabled(not running)
        self._dir_input.setEnabled(not running)
        self._prefix_input.setEnabled(not running)

    # ------------------------------------------------------------------
    # Worker 回调（UI 线程）
    # ------------------------------------------------------------------

    def _on_progress(self, current: int, total: int, name: str) -> None:
        if total > 0:
            self._progress_bar.setValue(int(current * 100 / total))
        if name:
            self._status_label.setText(
                I18N.tr(
                    "edit.stem_export.progress",
                    "正在渲染: {name}（{current}/{total}）",
                    name=name, current=current + 1, total=total,
                )
            )

    def _on_finished(self, result: Any) -> None:
        self._switch_running(False)
        self._progress_bar.setValue(100)
        lines: List[str] = []
        if getattr(result, "cancelled", False):
            lines.append(I18N.tr(
                "edit.stem_export.cancelled_unpublished",
                "已取消：此批次文件未发布，已有交付文件保持不变。",
            ))
        if result.all_succeeded and result.stems:
            lines.append(I18N.tr(
                "edit.stem_export.done_all",
                "已写入 {n} 个 stem 到:\n{dir}",
                n=len(result.stems), dir=result.output_dir,
            ))
        else:
            ok_count = sum(1 for _name, ok in result.stems if ok)
            lines.append(I18N.tr(
                "edit.stem_export.done_partial",
                "导出完成：{ok}/{n} 成功（{dir}）",
                ok=ok_count, n=len(result.stems), dir=result.output_dir,
            ))
        if result.all_succeeded and getattr(result, "output_files", None):
            lines.extend(f"  ✔ {Path(path).name}" for path in result.output_files)
        else:
            for name, ok in result.stems:
                mark = "✔" if ok else "✘"
                lines.append(f"  {mark} {name}.wav")
        self._status_label.setText("\n".join(lines))
        self._worker = None

    def _on_failed(self, message: str) -> None:
        self.failure_diagnostics = tuple(
            getattr(self._worker, "failure_diagnostics", ()))
        self._switch_running(False)
        self._progress_bar.setVisible(False)
        self._status_label.setText(
            I18N.tr("edit.stem_export.failed", "导出失败：{msg}", msg=message)
        )
        self._worker = None

    # ------------------------------------------------------------------

    def _worker_running(self) -> bool:
        """worker 是否仍在运行（含 join 超时后的后台残留）。"""
        worker = self._worker
        return worker is not None and worker.isRunning()

    def _cancel_and_join_worker(self, timeout_ms: int = 3000) -> bool:
        """请求取消并等待 worker 退出。

        AUD-20903 系列（AUD-20910）：返回 worker 是否在超时内退出。取消
        只在 stem 边界生效，单个 stem（如 Master 混音）渲染常超 3 秒；
        调用方必须检查返回值，超时后禁止关闭对话框（见 reject/closeEvent），
        否则 worker（parent=对话框）在对话框销毁时触发
        "QThread: Destroyed while thread is still running"。
        """
        if self._worker_running():
            self._worker.cancel()
            # QThread.wait(ms) 返回 True 表示线程已在超时内结束
            return bool(self._worker.wait(timeout_ms))
        return True

    def _enter_wait_worker_exit_state(self) -> None:
        """AUD-20910: join 超时后进入"等待后台完成"状态。

        保持对话框存活（隐藏后的 UI 仍能收到 worker 信号），禁用所有
        可触发关闭/再导出的入口；finished_ok/failed 回调恢复正常状态。
        """
        self._switch_running(True)
        self._cancel_btn.setEnabled(False)
        self._status_label.setText(
            I18N.tr(
                "edit.stem_export.waiting_background",
                "导出线程仍在后台收尾（当前 stem 完成后停止）…完成后可关闭窗口。",
            )
        )

    def reject(self) -> None:  # noqa: N802 — Qt 命名
        # Esc / Close 按钮走 reject()→done()→hide()，不经过 closeEvent；
        # 不在此收口的话导出线程会在对话框隐藏后继续在后台跑，
        # 对话框销毁时触发 "QThread: Destroyed while thread is still running"。
        if not self._cancel_and_join_worker():
            # AUD-20910: join 超时 —— worker 仍在运行，拒绝关闭并进入
            # 等待状态，由 finished_ok/failed 恢复 Close。
            self._enter_wait_worker_exit_state()
            return
        super().reject()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 — Qt 命名
        """关闭时请求取消并等待 worker 退出，避免线程悬空。"""
        if not self._cancel_and_join_worker():
            # AUD-20910: 同 reject() —— worker 未退出前不允许销毁对话框。
            self._enter_wait_worker_exit_state()
            event.ignore()
            return
        super().closeEvent(event)


__all__ = [
    "STEM_BIT_DEPTH",
    "STEM_CHANNELS",
    "STEM_SAMPLE_RATE",
    "StemExportDialog",
    "StemExportWorker",
]
