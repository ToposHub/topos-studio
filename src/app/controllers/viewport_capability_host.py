"""I03（复查计划）：Viewer capability 宿主——真实上下文 → 注册表 → 菜单回填。

职责：

- 收集**真实宿主状态**（时间线在场、Source 素材在场、Deliver 页、GPU
  可用性）构造 ``ViewerCapabilityContext``，经
  ``evaluate_viewer_capabilities`` 评估后把 enabled/reason 回填到
  ``EditViewport`` 的能力动作（单一事实源；无 handler 的动作保持禁用）；
- 触发时机：菜单即将展开（viewport 的 ``capabilities_context_requested``）
  与 I01 协调器权威快照变化（素材/页面/播放状态变化即时回填）；
- 执行有真实 handler 的动作：

  - ``timeline.match_frame``（Timeline→Source）：复用 B06 的
    ``resolve_match_frame_at_playhead``（选中优先，覆盖播放头映射），成
    功后走既有 ``EditPage._on_clip_open_in_source`` 打开链；拒绝回状态栏
    原因，零写入；
  - ``source.match_frame``（Source→Timeline）：``find_source_frame_uses``
    列候选——唯一使用直接跳播放头；多处使用弹出候选菜单（精确到帧，
    经 ``timeline_to_source_frame`` 反查，retimed/freeze 片段也准确），
    取消不改变播放头。

无服务项（Proxy 切换/bypass/Source Tape/markers）保持禁用并带注册表
原因——本宿主不为它们注册 handler。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import QObject
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QMenu

from src.features.edit.core.match_frame import (
    describe_match_frame_reason,
    find_source_frame_uses,
    resolve_match_frame_at_playhead,
)
from src.features.edit.core.viewer_capabilities import (
    ViewerCapabilityContext,
    capability_by_action,
    evaluate_viewer_capabilities,
)

logger = logging.getLogger(__name__)


class ViewportCapabilityHost(QObject):
    """把注册表接到真实宿主对象上（MainWindow 适配）。"""

    def __init__(self, window: Any, parent: Optional[QObject] = None) -> None:
        # 窗口不要求是 QObject（测试宿主可用 SimpleNamespace）；只有显式
        # parent 才作为 Qt 父对象，生命周期由宿主持有的引用管理。
        super().__init__(parent)
        self._window = window
        self._candidate_menu: Optional[QMenu] = None
        viewport = window.edit_viewport
        viewport.capability_action_requested.connect(self._on_action)
        viewport.capabilities_context_requested.connect(self.refresh)
        coordinator = getattr(window, "_viewport_playback_coordinator", None)
        if coordinator is not None:
            # I01 快照变化（素材/页面/播放）→ 即时回填菜单状态
            coordinator.snapshot_changed.connect(lambda _snap: self.refresh())
        self.refresh()

    # ── 状态收集与回填 ──────────────────────────────────────────────

    def _context(self) -> ViewerCapabilityContext:
        window = self._window
        sequence = self._timeline_sequence()
        source = getattr(window, "source_viewer_widget", None)
        in_deliver = str(getattr(window, "current_page", "") or "") == "Deliver"
        # GPU 可用性：目前无全局探针；Source 侧 CPU 回退是临时路径而非
        # 永久能力缺失，注册表的 gpu 门（fullscreen）保持默认可用，等
        # B02 计数接入宿主后由真实信号驱动。
        return ViewerCapabilityContext(
            has_timeline=sequence is not None,
            source_asset_open=getattr(source, "current_asset", None) is not None,
            gpu_available=True,
            in_deliver=in_deliver)

    def _timeline_sequence(self) -> Any:
        window = self._window
        panel = getattr(window, "timeline_panel", None)
        view = getattr(panel, "timeline_view", None)
        get_timeline = getattr(view, "get_timeline", None)
        return get_timeline() if callable(get_timeline) else None

    def refresh(self) -> None:
        caps = capability_by_action(evaluate_viewer_capabilities(
            self._context()))
        self._window.edit_viewport.refresh_capabilities(caps)

    # ── 动作执行 ────────────────────────────────────────────────────

    def _on_action(self, key: str) -> None:
        if key == "timeline.match_frame":
            self._match_timeline_to_source()
        elif key == "source.match_frame":
            self._match_source_to_timeline()
        else:
            logger.debug("capability host: no handler for %s", key)

    def _match_timeline_to_source(self) -> None:
        """Timeline→Source：播放头处解析 → 既有打开链。"""
        window = self._window
        panel = getattr(window, "timeline_panel", None)
        view = getattr(panel, "timeline_view", None)
        sequence = self._timeline_sequence()
        if sequence is None or view is None:
            self._status("No timeline is open for Match Frame.")
            return
        playhead = int(getattr(view, "_playhead_frame", 0) or 0)
        selection = getattr(view, "_selection", None)
        selected = list(getattr(selection, "selected_clip_ids", ()) or ())
        result = resolve_match_frame_at_playhead(sequence, playhead, selected)
        if not result.ok:
            self._status(describe_match_frame_reason(result.reason or ""))
            return
        edit_page = getattr(window, "edit_page", None)
        opener = getattr(edit_page, "_on_clip_open_in_source", None)
        if not callable(opener) or result.clip_id is None:
            self._status("Match Frame: source open path unavailable.")
            return
        opener(result.clip_id, int(result.source_frame or 0))

    def _match_source_to_timeline(self) -> None:
        """Source→Timeline：列出同素材使用；唯一即跳，多处出候选。"""
        window = self._window
        source = getattr(window, "source_viewer_widget", None)
        asset = getattr(source, "current_asset", None)
        sequence = self._timeline_sequence()
        if asset is None or sequence is None:
            self._status("Match Frame needs an open source and a timeline.")
            return
        frame = int(getattr(source, "current_frame", 0) or 0)
        uses = find_source_frame_uses(sequence, getattr(asset, "asset_id", ""),
                                      frame)
        if not uses:
            self._status("This source frame is not used in the timeline.")
            return
        if len(uses) == 1:
            self._jump_to_use(uses[0], frame)
            return
        # 多处使用：候选菜单（默认第一处会撒谎——用户必须能选）
        self._candidate_menu = menu = QMenu()
        for use in uses:
            exact = self._locate_timeline_frame(use, frame)
            label_frame = exact if exact is not None else use.timeline_start
            action = menu.addAction(
                f"{use.track_id} · frame {label_frame}")
            action.setData(label_frame)
        menu.triggered.connect(self._on_candidate_selected)
        menu.popup(QCursor.pos())

    def _on_candidate_selected(self, action: Any) -> None:
        frame = action.data()
        panel = getattr(self._window, "timeline_panel", None)
        view = getattr(panel, "timeline_view", None)
        if frame is not None and view is not None:
            view.set_playhead(int(frame))
        if self._candidate_menu is not None:
            self._candidate_menu.deleteLater()
            self._candidate_menu = None

    def _jump_to_use(self, use: Any, source_frame: int) -> None:
        frame = self._locate_timeline_frame(use, source_frame)
        panel = getattr(self._window, "timeline_panel", None)
        view = getattr(panel, "timeline_view", None)
        if view is None:
            return
        view.set_playhead(int(frame if frame is not None
                              else use.timeline_start))

    def _locate_timeline_frame(self, use: Any,
                               source_frame: int) -> Optional[int]:
        """源帧 → 精确时间线帧（经 ``timeline_to_source_frame`` 反查）。

        线性扫描片段跨度：retimed/freeze/反向片段也准确（朴素公式
        ``start + (frame − source_start)`` 对变速片段错帧）。找不到（如
        源帧只被部分覆盖的边界）返回 None，调用方回落片段起点。
        """
        sequence = self._timeline_sequence()
        if sequence is None:
            return None
        clip = sequence.get_clip_by_id(use.clip_id)
        if clip is None:
            return None
        start = int(getattr(clip, "timeline_start", 0) or 0)
        duration = int(getattr(clip, "timeline_duration", 0) or 0)
        mapper = getattr(clip, "timeline_to_source_frame", None)
        if not callable(mapper):
            return None
        target = int(source_frame)
        for offset in range(max(0, duration)):
            try:
                if int(mapper(start + offset)) == target:
                    return start + offset
            except Exception:  # noqa: BLE001 — 片段映射异常按未命中处理
                return None
        return None

    def _status(self, message: str) -> None:
        logger.debug("capability host: %s", message)
        status_bar = getattr(self._window, "statusBar", None)
        if callable(status_bar):
            bar = status_bar()
            if bar is not None:
                bar.showMessage(message, 4000)


def install_viewport_capability_host(window: Any) -> ViewportCapabilityHost:
    """单入口：宿主在 EditViewport 创建后安装一次。"""
    return ViewportCapabilityHost(window)
