"""M08.2b · Bake 进度对话框（execute_bake 协调器的 UI 接线件）。

呈现 M08.2 六阶段（freeze→render→verify→source_changed→publish→
rewrite）的进度与失败原因；render_fn 由控制器注入（线程安全的帧渲
染回调），取消经 is_cancelled 传给协调器（产物完整前原图继续有效）。

N05 修复（审计 2026-10-25 §2 N05）：

- **进度线程安全**：render 回调在 worker 线程执行，原先直接写
  ``QProgressBar.setValue``——worker 线程触碰 Qt 控件。改为
  ``frame_rendered`` 信号，UI 线程槽内更新；
- **关闭/取消生命周期**：关闭或 Esc（reject）在任务在途时先请求取消
  并等待当前帧完成（有限等待）；等待超时则拒绝关闭保活对话框，
  防止"QThread 销毁时仍在跑"的崩溃路径；结果到达后允许关闭；
- **图改写回主线程**：``apply_rewrite=False`` 执行事务，发布成功后
  经 ``rewrite_fn(result)`` 在 UI 线程改写（由调用方接命令栈/Undo）；
  未提供 rewrite_fn 时如实提示"产物已发布，图未改写"，不冒充完成。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from src.features.composite.bake_service import execute_bake
from src.shared.i18n.i18n import I18N

#: 关闭等待在途 worker 的上限（毫秒）——超时拒绝关闭（保活防崩溃）
_CLOSE_WAIT_MS = 10000


class _BakeWorker(QThread):
    finished_with_result = pyqtSignal(object)
    frame_rendered = pyqtSignal(int)

    def __init__(self, kwargs: dict, parent=None) -> None:
        super().__init__(parent)
        self._kwargs = kwargs
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # pragma: no cover — 线程体
        kwargs = dict(self._kwargs)
        kwargs["is_cancelled"] = self.is_cancelled
        try:
            result = execute_bake(**kwargs)
        except Exception as exc:  # noqa: BLE001 — 失败明示
            result = type("BakeFailure", (), {
                "ok": False, "error": str(exc), "stage": "exception",
                "published": [], "written": []})()
        self.finished_with_result.emit(result)


class BakeProgressDialog(QDialog):
    """Bake 事务进度（阶段文案+帧进度+取消）。"""

    STAGE_LABELS = {
        "freeze": "冻结源签名",
        "render": "渲染帧序列（独占临时目录）",
        "verify": "逐帧校验 + manifest 指纹",
        "source_changed": "发布前源签名复核",
        "publish": "发布（旧产物备份→就位→提交标记）",
        "rewrite": "图改写（一次 Undo）",
    }

    def __init__(
        self,
        graph: Any,
        target_id: str,
        output_dir: str,
        frame_start: int,
        frame_end: int,
        render_fn: Callable[[str, int, str], bool],
        *,
        base_name: Optional[str] = None,
        on_done: Optional[Callable[[Any], None]] = None,
        rewrite_fn: Optional[Callable[[Any], bool]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._on_done = on_done
        self._rewrite_fn = rewrite_fn
        self._result = None
        self._rewrite_applied = False
        self._finished = False
        self._close_requested = False
        self.setWindowTitle(f"烘焙 {target_id}")
        layout = QVBoxLayout(self)
        self._stage_label = QLabel(I18N.tr("bake.preparing", "准备…"))
        layout.addWidget(self._stage_label)
        self._bar = QProgressBar()
        total = max(1, frame_end - frame_start + 1)
        self._bar.setRange(0, total)
        self._bar.setValue(0)  # Qt 初值 -1=未定义态；显式归零
        layout.addWidget(self._bar)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._cancel_btn = QPushButton(I18N.tr("bake.cancel_keep", "取消（保留旧产物）"))
        self._cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(self._cancel_btn)

        self._worker = _BakeWorker({
            "graph": graph, "target_id": target_id,
            "output_dir": output_dir,
            "frame_start": frame_start, "frame_end": frame_end,
            # N05：事务本身不改图——发布成功后由 rewrite_fn 在 UI 线程
            # 改写（接命令栈/Undo），worker 线程不得变更图。
            "render_fn": self._wrap_render(render_fn),
            "base_name": base_name,
            "apply_rewrite": False,
        }, self)
        self._worker.finished_with_result.connect(self._on_result)
        self._worker.frame_rendered.connect(self._on_frame_rendered)

    def _wrap_render(self, render_fn):
        def wrapped(node_id, frame, out_path):
            ok = bool(render_fn(node_id, frame, out_path))
            # N05：worker 线程只发信号，不触碰控件
            self._worker.frame_rendered.emit(1)
            return ok
        return wrapped

    def start(self) -> None:
        self._stage_label.setText(self.STAGE_LABELS["render"])
        self._worker.start()

    def _on_frame_rendered(self, _delta: int) -> None:
        self._bar.setValue(self._bar.value() + 1)

    def _on_cancel(self) -> None:
        if self._finished:
            self.accept()
            return
        self._cancel_btn.setEnabled(False)
        self._detail.setText(
            I18N.tr("bake.cancelling", "正在取消（当前帧完成后中止，产物不发布）…"))
        self._worker.cancel()

    def _on_result(self, result) -> None:
        self._finished = True
        self._result = result
        if result.ok:
            # N05：改写在 UI 线程（本槽即 UI 线程）执行
            if self._rewrite_fn is not None:
                try:
                    self._rewrite_applied = bool(self._rewrite_fn(result))
                except Exception:  # noqa: BLE001 — 改写失败明示不崩
                    logger = __import__("logging").getLogger(__name__)
                    logger.exception("bake rewrite failed")
                    self._rewrite_applied = False
            if self._rewrite_applied:
                self._stage_label.setText(I18N.tr("bake.done_rewritten", "烘焙完成（图已改写）"))
            else:
                self._stage_label.setText(
                    I18N.tr("bake.done_published", "烘焙完成（产物已发布，图未改写）"))
            self._detail.setText(
                f"已发布 {len(result.published)} 帧；manifest 就位")
        else:
            stage = self.STAGE_LABELS.get(
                getattr(result, "stage", ""), getattr(result, "stage", ""))
            self._stage_label.setText(f"失败（{stage}）")
            self._detail.setText(str(getattr(result, "error", "")))
        self._cancel_btn.setText(I18N.tr("bake.close", "关闭"))
        self._cancel_btn.setEnabled(True)
        try:
            self._cancel_btn.clicked.disconnect()
        except TypeError:  # 无连接
            pass
        self._cancel_btn.clicked.connect(self._on_cancel)
        if self._close_requested:
            self.accept()
        if self._on_done is not None:
            # 结果对象原样交付；改写是否已应用经 ``rewrite_applied`` 属性
            # 另行暴露（dataclass 不接收外来字段，不克隆加字段）。
            self._on_done(self._result)

    # ── N05：关闭/拒绝 = 取消 + 有限等待 + 保活 ──

    def _request_close(self) -> bool:
        """取消在途任务并等待；超时返回 False（拒绝关闭，保活）。"""
        if self._finished:
            return True
        self._close_requested = True
        self._worker.cancel()
        self._detail.setText(I18N.tr("bake.cancelling_wait", "正在取消并等待当前帧完成…"))
        if self._worker.wait(_CLOSE_WAIT_MS):
            self._finished = True
            return True
        return False

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt 约定
        if self._request_close():
            event.accept()
        else:
            event.ignore()
            self._detail.setText(
                I18N.tr(
                    "bake.still_running",
                    "任务仍在途（取消等待超时），完成后再关闭——防半途销毁"))

    def reject(self) -> None:  # noqa: N802 — Qt 约定
        if self._request_close():
            super().reject()

    @property
    def result(self):
        return self._result

    @property
    def rewrite_applied(self) -> bool:
        return self._rewrite_applied

    @property
    def worker_running(self) -> bool:
        return self._worker.isRunning()


__all__ = ["BakeProgressDialog"]
