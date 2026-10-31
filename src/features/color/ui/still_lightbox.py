"""M09.3 · HDR still Lightbox 比较对话框。

复用现有参考帧模型（``load_still_best`` + M09.3 alpha 声明）：参考 vs
当前并排 / 差异模式；精度（exr_float32/jpeg8）与 alpha 语义
（straight/premultiplied/none/undeclared）显式徽标——**undeclared 的
四通道不做差异比较**（语义靠猜即透明边缘像素错误），状态栏明示。
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

_VALID_SEMANTICS = ("none", "straight", "premultiplied", "undeclared")


def _to_display_u8(img: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """float/uint 图 → 显示用 uint8（HDR 值 clip 到 [0,1]，仅显示域）。"""
    if img is None or img.size == 0:
        return None
    arr = np.asarray(img, dtype=np.float32)
    if arr.ndim != 3:
        return None
    if arr.shape[2] == 4:
        arr = arr[..., :3]
    return (np.clip(arr, 0.0, 1.0) * 255.0).round().astype(np.uint8)


def _qimage_from_bgr_u8(bgr: Optional[np.ndarray]) -> Optional[QImage]:
    """BGR uint8 → QImage（列序 BGR→RGB 交换；连续内存）。"""
    if bgr is None:
        return None
    rgb = np.ascontiguousarray(bgr[..., ::-1])
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def difference_display(ref: Optional[np.ndarray],
                       cur: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """差异视图数据：|ref−cur| 每通道（显示域 0-1，差异放大 4×）。

    None 输入或形状不一致返回 None（调用方明示，不猜对齐）。
    """
    if ref is None or cur is None:
        return None
    a = np.asarray(ref, dtype=np.float32)
    b = np.asarray(cur, dtype=np.float32)
    if a.shape != b.shape:
        return None
    diff = np.abs(a - b)
    if diff.ndim == 3 and diff.shape[2] == 4:
        diff = diff[..., :3]
    return np.clip(diff * 4.0, 0.0, 1.0)


class StillLightboxDialog(QDialog):
    """参考 vs 当前 still 比较（并排/差异 + 精度/alpha 声明徽标）。"""

    def __init__(
        self,
        ref_img: Optional[np.ndarray],
        cur_img: Optional[np.ndarray],
        *,
        ref_precision: str = "exr_float32",
        cur_precision: str = "exr_float32",
        ref_alpha: str = "undeclared",
        cur_alpha: str = "undeclared",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ref = ref_img
        self._cur = cur_img
        self._ref_precision = str(ref_precision)
        self._cur_precision = str(cur_precision)
        self._ref_alpha = (ref_alpha if ref_alpha in _VALID_SEMANTICS
                           else "undeclared")
        self._cur_alpha = (cur_alpha if cur_alpha in _VALID_SEMANTICS
                           else "undeclared")
        self._difference_mode = False
        self.last_refusal: str = ""

        self.setWindowTitle(I18N.tr(
            "still_lightbox.title", "Still Lightbox——参考比较"))
        layout = QVBoxLayout(self)

        self._badges = QLabel(self._badge_text())
        self._badges.setWordWrap(True)
        layout.addWidget(self._badges)

        self._status = QLabel()
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._ref_label = QLabel()
        self._cur_label = QLabel()
        row.addWidget(self._ref_label)
        row.addWidget(self._cur_label)
        layout.addLayout(row, 1)

        self._diff_btn = QPushButton(I18N.tr(
            "still_lightbox.difference_mode", "差异模式"))
        self._diff_btn.clicked.connect(self._on_toggle_difference)
        layout.addWidget(self._diff_btn)

        close_btn = QPushButton(I18N.tr("still_lightbox.close", "关闭"))
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self._refresh()

    # ── 呈现 ─────────────────────────────────────────────────────────

    def _badge_text(self) -> str:
        return (f"参考：{self._ref_precision}｜alpha {self._ref_alpha}"
                f"　当前：{self._cur_precision}｜alpha {self._cur_alpha}")

    def _has_alpha(self, img) -> bool:
        return (img is not None and getattr(img, "ndim", 0) == 3
                and img.shape[2] == 4)

    def _semantic_refusal(self) -> str:
        """undeclared 四通道 → 差异比较拒绝（不猜语义）。"""
        for role, img, sem in (("参考", self._ref, self._ref_alpha),
                               ("当前", self._cur, self._cur_alpha)):
            if self._has_alpha(img) and sem == "undeclared":
                return f"{role}侧 alpha 语义未声明——差异比较拒绝（猜语义即透明边缘像素错误）"
        return ""

    def _on_toggle_difference(self) -> None:
        if not self._difference_mode:
            refusal = self._semantic_refusal()
            if refusal:
                self.last_refusal = refusal
                self._status.setText(refusal)
                return
        self._difference_mode = not self._difference_mode
        self._diff_btn.setText(
            "并排模式" if self._difference_mode else "差异模式")
        self._refresh()

    def _set_image(self, label: QLabel, img: Optional[np.ndarray]) -> None:
        qimg = _qimage_from_bgr_u8(_to_display_u8(img))
        if qimg is None:
            label.clear()
            return
        label.setPixmap(QPixmap.fromImage(qimg))

    def _refresh(self) -> None:
        if self._difference_mode:
            diff = difference_display(self._ref, self._cur)
            if diff is None:
                self._status.setText(I18N.tr(
                    "still_lightbox.diff_unavailable",
                    "差异不可用（图像缺失或形状不一致）"))
                self._set_image(self._ref_label, None)
                self._set_image(self._cur_label, None)
                return
            self._status.setText(I18N.tr(
                "still_lightbox.diff_view", "差异视图：|参考−当前|×4（显示域）"))
            self._set_image(self._ref_label, diff)
            self._cur_label.clear()
        else:
            self._status.setText(I18N.tr(
                "still_lightbox.side_by_side",
                "并排视图（显示域，HDR 值已 clip——精度徽标为准）"))
            self._set_image(self._ref_label, self._ref)
            self._set_image(self._cur_label, self._cur)


__all__ = ["StillLightboxDialog", "difference_display"]
