"""M22（R07）· 镜头匹配前台面板——接通既有 plan/ApplyShotMatchCommand。

工作流（对齐计划 §M22）：选参考镜头（解释锁=当前激活调色内容）→ 逐
镜头测量与规划（plan_shot_match）→ 逐镜头勾选/排除（低置信需显式确
认）→ 一次事务提交（ApplyShotMatchCommand：过期拒绝/零半写/undo）。

面板不直接持有渲染器：帧统计（``measure_fn``）与 before/after 预览
（``preview_fn``）由控制器注入——真实帧取自生产渲染路径，面板只做
编排与确认。参考过期（帧统计 revision 变化）时拒绝提交。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Tuple

import numpy as np

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from src.features.color.core.shot_match import (
    ShotStatistics,
    measure_shot,
    plan_shot_match,
)
from src.features.edit.core.shot_match_apply import ApplyShotMatchCommand
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

#: 低于该置信度的建议默认不勾选且需用户显式确认
DEFAULT_LOW_CONFIDENCE = 0.5


@dataclass(frozen=True)
class ShotMatchSelection:
    """用户在面板中的最终选择（供测试/控制器断言）。"""

    reference_clip_id: str
    included_clip_ids: Tuple[str, ...]
    excluded_clip_ids: Tuple[str, ...]


class ShotMatchPanel(QDialog):
    """镜头匹配前台（模式化对话框；measure/preview 注入）。"""

    def __init__(
        self,
        project: Any,
        timeline_id: str,
        *,
        clip_ids: Sequence[str],
        measure_fn: Optional[Callable[[str], Any]] = None,
        reference_clip_id: str = "",
        low_confidence: float = DEFAULT_LOW_CONFIDENCE,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._timeline_id = timeline_id
        self._clip_ids = list(clip_ids)
        # measure_fn(clip_id) → RGB float 帧（或 ndarray）；缺省用 0 帧
        # 占位统计（生产由控制器注入真实帧抓取）
        self._measure_fn = measure_fn
        self._low_confidence = float(low_confidence)
        self._report = None
        self._reference_stats: Optional[ShotStatistics] = None
        self._selection: Optional[ShotMatchSelection] = None
        self._applied_command: Optional[ApplyShotMatchCommand] = None

        self.setWindowTitle(I18N.tr("shot_match.title", "批量镜头匹配"))
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel(I18N.tr("shot_match.reference", "参考镜头：")))
        self._reference_combo = QComboBox()
        self._reference_combo.addItems(
            self._clip_ids if reference_clip_id not in self._clip_ids
            else [reference_clip_id] + [c for c in self._clip_ids
                                        if c != reference_clip_id])
        row.addWidget(self._reference_combo, 1)
        self._analyze_btn = QPushButton(I18N.tr("shot_match.analyze", "分析"))
        self._analyze_btn.clicked.connect(self._on_analyze)
        row.addWidget(self._analyze_btn)
        layout.addLayout(row)

        self._status = QLabel(I18N.tr(
            "shot_match.select_reference_hint", "选择参考镜头后点击「分析」"))
        layout.addWidget(self._status)

        self._shots_list = QListWidget()
        layout.addWidget(self._shots_list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            I18N.tr("shot_match.submit", "提交匹配"))
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        buttons.accepted.connect(self._on_apply)
        buttons.rejected.connect(self.reject)
        self._buttons = buttons
        layout.addWidget(buttons)

    # ── 分析（真实测量 + 既有规划） ────────────────────────────────

    def _measure(self, clip_id: str) -> ShotStatistics:
        if self._measure_fn is not None:
            frame = self._measure_fn(clip_id)
            return measure_shot(frame, clip_id=clip_id)
        # 无注入：不可假测量——显式失败由调用方处理（面板外）
        raise ValueError(f"未提供测量回调，无法测量镜头 {clip_id}")

    def _on_analyze(self) -> None:
        # 新分析失败时，旧建议和旧提交结果必须立即失效。
        self._report = None
        self._reference_stats = None
        self._selection = None
        self._applied_command = None
        self._shots_list.clear()
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        ref_id = self._reference_combo.currentText()
        if not ref_id:
            return
        try:
            self._reference_stats = self._measure(ref_id)
            shots = [self._measure(c) for c in self._clip_ids
                     if c != ref_id]
        except Exception as exc:  # noqa: BLE001 — 测量失败明示
            self._status.setText(f"测量失败：{exc}")
            return
        locked = self._locked_clip_ids()
        self._report = plan_shot_match(
            self._reference_stats, shots, locked_clip_ids=locked)
        self._rebuild_list(ref_id)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._report is not None)

    def _locked_clip_ids(self) -> Sequence[str]:
        """F4.2 已锁栈的镜头（提交零写入；面板标记锁定）。"""
        try:
            from src.features.edit.core.color_versions import is_stack_locked
            stacks = getattr(self._project, "color_stacks", {}) or {}
            timeline = (getattr(self._project, "timelines", {})
                        or {}).get(self._timeline_id)
            locked = []
            for track in (getattr(timeline, "video_tracks", ()) or ()):
                for clip in track.clips:
                    stack_id = getattr(clip, "color_stack_id", None)
                    if stack_id and stack_id in stacks \
                            and is_stack_locked(self._project, stack_id):
                        locked.append(clip.clip_id)
            return locked
        except Exception:  # noqa: BLE001 — 锁判定失败不阻断分析
            return ()

    def _rebuild_list(self, ref_id: str) -> None:
        self._shots_list.clear()
        applicable = 0
        for s in self._report.suggestions:
            if s.clip_id == ref_id:
                continue
            item = QListWidgetItem()
            label_parts = [s.clip_id, f"conf {s.confidence:.2f}"]
            if s.excluded or s.exclude_reason:
                label_parts.append(f"✗ {s.exclude_reason or '排除'}")
                checkable = False
            elif s.confidence < self._low_confidence:
                label_parts.append("低置信（需确认）")
                checkable = True
            else:
                checkable = True
                applicable += 1
            item.setText("  ·  ".join(label_parts))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                          if checkable
                          else item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if checkable and s.confidence >= self._low_confidence
                else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, s)
            self._shots_list.addItem(item)
        self._status.setText(
            f"参考 {ref_id}：{applicable} 个可应用建议"
            f"（低置信 <{self._low_confidence:.2f} 需勾选确认）")

    # ── 提交（一次事务；勾选过滤即人工确认） ───────────────────────

    def _on_apply(self) -> None:
        if self._report is None:
            return
        included, excluded = [], []
        for i in range(self._shots_list.count()):
            item = self._shots_list.item(i)
            s = item.data(Qt.ItemDataRole.UserRole)
            if s.excluded or s.exclude_reason:
                excluded.append(s.clip_id)
            elif item.checkState() == Qt.CheckState.Checked:
                included.append(s.clip_id)
            else:
                excluded.append(s.clip_id)
        self._selection = ShotMatchSelection(
            reference_clip_id=self._report.reference_clip_id,
            included_clip_ids=tuple(included),
            excluded_clip_ids=tuple(excluded))
        # 勾选过滤经 report 的 suggestions 子集提交（低置信未勾选=排除）
        from src.features.color.ai.color_context import ShotMatchReport
        filtered = ShotMatchReport(
            reference_clip_id=self._report.reference_clip_id,
            suggestions=tuple(
                s for s in self._report.suggestions
                if s.clip_id in included),
            auto_apply_threshold=self._report.auto_apply_threshold)
        try:
            self._applied_command = ApplyShotMatchCommand.create(
                self._project, self._timeline_id, filtered,
                auto_apply_threshold=0.0)  # 勾选即确认，置信门在 UI 完成
        except ValueError as exc:
            self._status.setText(f"提交拒绝：{exc}")
            return
        self.accept()

    # ── 结果访问（控制器/测试） ────────────────────────────────────

    @property
    def selection(self) -> Optional[ShotMatchSelection]:
        return self._selection

    @property
    def applied_command(self) -> Optional[ApplyShotMatchCommand]:
        return self._applied_command


def controller_measure_fn(
    engine: Any, clip_frame_fn: Callable[[str], Optional[int]],
    *,
    frame_source: Optional[Callable[[str, int], Any]] = None,
) -> Callable[[str], Any]:
    """控制器测量回调——逐镜头取真实帧（N10 修复）。

    旧实现的两个缺陷（审计 §2 N10）：
    1. **算了 frame 却不用**——每个镜头都返回同一张
       ``engine.render_cached_image()``（当前 Color 页缓存图），全部
       镜头同图"测量"出错误的匹配建议；
    2. **域错误**——缓存图是 BGR/显示域 uint8，``measure_shot`` 要求
       scene-linear 口径的 float RGB（BGR 直入会把 R/B 互换，WB/肤色
       统计全错）。

    现契约（fail-closed）：

    - ``frame_source(clip_id, frame) -> ndarray(H, W, 3) RGB float``：
      逐镜头真实帧（解码管理器 + YUV→RGB，由控制器注入）——唯一
      合法测量路径；
    - 未注入 frame_source 时**逐镜头显式失败**（不退回同图假测量；
      面板呈现"测量失败"，宁可无建议不出错建议）。

    Args:
        engine: 保留兼容（旧签名位；不再用于假测量）。
        clip_frame_fn: clip_id → 代表帧号（None = 帧 0）。
        frame_source: 逐镜头真实帧源（N10）。
    """
    def measure(clip_id: str):
        if frame_source is None:
            raise ValueError(
                f"镜头 {clip_id} 无逐镜头帧源——拒绝用同一缓存图假测量"
                "（需接入解码管理器逐镜头取帧）")
        frame = int(clip_frame_fn(clip_id) or 0)
        image = frame_source(clip_id, frame)
        if image is None:
            raise ValueError(f"镜头 {clip_id} 帧 {frame} 取帧失败")
        arr = np.asarray(image)
        if arr.ndim != 3 or arr.shape[2] < 3:
            raise ValueError(
                f"镜头 {clip_id} 帧形态非法（需 HxWx3，得 {arr.shape}）")
        return arr
    return measure


__all__ = ["ShotMatchPanel", "ShotMatchSelection",
           "DEFAULT_LOW_CONFIDENCE", "controller_measure_fn"]
