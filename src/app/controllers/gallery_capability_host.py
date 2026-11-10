"""G01：Gallery 能力宿主——把领域能力注册表接到真实窗口事实。

镜像 ``color_viewport_capability_host`` 的宿主形态（计划 §5 G01）：

- ``install(window)``：绑定 ``window.presets_panel``（chrome／list_widget
  缺席时返回 False，不假装可用）；面板存宿主引用供右键菜单门控；
  chrome ``more_menu.aboutToShow`` 上再挂一道门（chrome 自己的重建连接
  在前，宿主门在后，看到的总是重建后的 action 集）。
- ``refresh()``：采集真实窗口事实（媒体在场／引擎在场／当前 album／
  选中项，含选中项 grade 单独探测）→ 纯 ``evaluate_gallery_capabilities``
  → revision 复核后应用。评估期间上下文再变（换 album／换选择）由
  revision 守卫丢弃陈旧结果。
- 菜单打开时同步刷新再门控——媒体状态变化不需要额外信号线；选区／
  album 变化即时刷新。
- ``mark_shutdown()``：断开连接、解除面板引用，之后全部 no-op。

五个旧信号与 LayerController handler 保持原样；本宿主只做可用性回填，
不改模型、不重复执行动作。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject

from src.features.color.core.gallery_contracts import (
    GalleryCapability,
    GalleryCapabilityContext,
    evaluate_gallery_capabilities,
    gallery_capability_by_action,
)
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class GalleryCapabilityHost(QObject):
    """Gallery 面板动作能力的安装／刷新／守卫宿主。"""

    def __init__(self, window) -> None:
        super().__init__()
        self._window = window
        self._panel = None
        self._repository = None
        self._revision = 0
        self._shutdown = False
        self._connections: list = []
        # 诊断快照（最后一次成功应用的评估结果）。
        self.last_context: Optional[GalleryCapabilityContext] = None
        self.last_capabilities: Dict[str, GalleryCapability] = {}
        self.last_revision: int = -1

    # ── 安装／关闭 ─────────────────────────────────────────────────────

    def install(self) -> bool:
        if self._shutdown or self._panel is not None:
            return False   # 已安装／已关闭：不重复接线
        panel = getattr(self._window, "presets_panel", None)
        if panel is None or getattr(panel, "list_widget", None) is None:
            return False
        chrome = getattr(panel, "_gallery", None)
        more_menu = getattr(chrome, "more_menu", None)
        folder_combo = getattr(panel, "folder_combo", None)
        if more_menu is None or folder_combo is None:
            return False

        self._panel = panel
        repository = getattr(panel, "_repository", None)
        if repository is None:
            from src.features.color.core.gallery_repository import (
                GalleryFolderRepository,
            )
            repository = GalleryFolderRepository(panel.presets_root)
        self._repository = repository
        panel.set_gallery_capability_host(self)

        # chrome 的 aboutToShow→_rebuild_more_menu 连接在构造时建立；
        # 宿主随后连接 → 同一次 aboutToShow 里先重建后门控。
        self._connect(more_menu.aboutToShow, self._gate_more_menu)
        self._connect(folder_combo.currentIndexChanged,
                      self._on_album_changed)
        self._connect(panel.list_widget.itemSelectionChanged, self.refresh)
        self.refresh()
        return True

    def mark_shutdown(self) -> None:
        # 先置关闭标志再脱离面板——面板据此把动作门切到"关闭态拒绝"
        # （区别于"宿主未安装"的旧恒启用行为，BR05）。
        self._shutdown = True
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections.clear()
        if self._panel is not None:
            try:
                self._panel.set_gallery_capability_host(None)
            except RuntimeError:
                pass  # 面板已销毁
        self._panel = None
        self._repository = None

    @property
    def shutdown(self) -> bool:
        return self._shutdown

    def _connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    # ── revision 守卫 ──────────────────────────────────────────────────

    def bump_context_revision(self) -> int:
        """上下文变化（换 album／换工程）→ 陈旧评估作废。"""
        self._revision += 1
        return self._revision

    def _on_album_changed(self, *_args) -> None:
        self.bump_context_revision()
        self.refresh()

    # ── 刷新 ───────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._shutdown or self._panel is None:
            return
        revision = self._revision
        context = self._gather_context()
        caps = gallery_capability_by_action(
            evaluate_gallery_capabilities(context))
        if self._revision != revision:
            return  # 评估期间上下文又变：丢弃陈旧结果
        self.last_context = context
        self.last_capabilities = caps
        self.last_revision = revision

    def action_context(self) -> GalleryCapabilityContext:
        """当前权威上下文快照（执行门 BR05 用；随取随新）。"""
        return self._gather_context()

    def evaluate_targeted(self, action_key: str,
                          topos_path: Optional[str]) -> GalleryCapability:
        """按**被操作对象**评估动作能力（BR05）。

        右击未选中项时作用于该对象本身——把目标 still 代入评估上下文
        （grade 探测对目标做，而不是碰巧的选中项）；同一坏包／无媒体／
        关闭态下所有入口（菜单/双击/Enter/中键）结论一致。
        """
        from src.features.color.core.gallery_contracts import GalleryCapability
        if self._shutdown:
            return GalleryCapability(action=action_key, supported=False,
                                     reason="host shutdown", checked=True)
        import dataclasses
        context = self._gather_context()
        still = None
        if self._repository is not None and self._panel is not None \
                and isinstance(topos_path, str):
            still = self._repository.still_at(
                self._panel.get_current_folder(), topos_path,
                probe_grade=True)
        targeted = dataclasses.replace(
            context, selection=still,
            selection_count=1 if still is not None else 0,
            selection_grade_loadable=(still.grade_loadable
                                      if still is not None else None))
        caps = gallery_capability_by_action(
            evaluate_gallery_capabilities(targeted))
        cap = caps.get(action_key)
        if cap is None:
            return GalleryCapability(action=action_key, supported=False,
                                     reason="unknown action", checked=True)
        return cap

    def _gather_context(self) -> GalleryCapabilityContext:
        window = self._window
        panel = self._panel
        has_media = bool(getattr(window, "current_file_path", None))
        media_is_video = bool(getattr(window, "current_file_is_video", False))
        engine = getattr(window, "engine", None)

        album = None
        selection = None
        selection_grade: Optional[bool] = None
        selection_count = 0
        if self._repository is not None:
            album = self._repository.album_for_folder(panel.get_current_folder())
            selected = panel.list_widget.selectedItems()
            selection_count = len(selected)
            if selected:
                topos_path = selected[0].data(0x0100)  # Qt.ItemDataRole.UserRole
                if isinstance(topos_path, str):
                    selection = self._repository.still_at(
                        panel.get_current_folder(), topos_path,
                        probe_grade=True)
                    if selection is not None:
                        selection_grade = selection.grade_loadable

        # BR05：服务位反映面板真实接线（传输/Label 服务在场即真）。
        return GalleryCapabilityContext(
            has_media=has_media,
            media_is_video=media_is_video,
            engine_available=engine is not None,
            album=album,
            selection=selection,
            selection_count=selection_count,
            selection_grade_loadable=selection_grade,
            import_service_available=bool(
                getattr(panel, "_transfer_service", None)),
            label_service_available=bool(
                getattr(panel, "_label_service", None)),
        )

    # ── chrome more-menu 门控 ──────────────────────────────────────────

    def _gate_more_menu(self) -> None:
        """more 菜单打开：同步刷新后按文本门控（菜单刚被 chrome 重建）。

        以 I18N key 的当前译文匹配（与 chrome 重建用同一 key，译文变化
        两侧同步）；未匹配的 action（chrome 后续新增项）保持 chrome 自身
        的启用状态，不做破坏性修改。
        """
        if self._shutdown or self._panel is None:
            return
        self.refresh()
        chrome = getattr(self._panel, "_gallery", None)
        menu = getattr(chrome, "more_menu", None)
        if menu is None:
            return
        gates = [
            (I18N.tr("presets.capture_still", "Capture Still"), "capture.still"),
            (I18N.tr("presets.new_folder", "New Folder"), "album.create"),
            (I18N.tr("gallery.image_wipe", "Image Wipe"), "compare.wipe"),
            (I18N.tr("gallery.split_screen", "Split Screen"), "compare.split"),
            (I18N.tr("presets.delete", "Delete"), "delete.bundle"),
            (I18N.tr("gallery.import", "Import Still"), "import.bundle"),
        ]
        gates_by_label = {
            label.rstrip("…").strip(): key for label, key in gates}
        for action in menu.actions():
            text = action.text().rstrip("…").strip()
            key = gates_by_label.get(text)
            if key is None:
                continue
            cap = self.last_capabilities.get(key)
            if cap is None:
                continue
            action.setEnabled(cap.supported)
            if not cap.supported and cap.reason:
                action.setToolTip(cap.reason)

    # ── 诊断 ───────────────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        context = self.last_context
        return {
            "host_shutdown": self._shutdown,
            "revision": self.last_revision,
            "has_media": bool(context and context.has_media),
            "album_id": context.album.album_id if context and context.album
                        else None,
            "selection_id": context.selection.still_id
                            if context and context.selection else None,
            "capabilities": {
                key: cap.to_dict()
                for key, cap in self.last_capabilities.items()},
        }


def install_gallery_capability_host(window) -> Optional[GalleryCapabilityHost]:
    """在主窗口上安装 Gallery 能力宿主（面板缺席时安静返回 None）。"""
    host = GalleryCapabilityHost(window)
    if not host.install():
        return None
    setattr(window, "_gallery_capability_host", host)
    return host


def refresh_gallery_capabilities(window) -> None:
    """getattr 防护的刷新入口（媒体加载/关闭路径可安全调用）。"""
    host = getattr(window, "_gallery_capability_host", None)
    if host is not None:
        host.refresh()


def bump_gallery_context(window) -> None:
    """getattr 防护的上下文 bump（工程打开/关闭路径调用）。"""
    host = getattr(window, "_gallery_capability_host", None)
    if host is not None:
        host.bump_context_revision()
        host.refresh()
