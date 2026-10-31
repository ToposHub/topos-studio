"""MenuBuilder — converts MenuActionDescriptor registrations into actual
QMenu / QAction entries.

This replaces hand-written menu construction for feature-specific items.
App-level items (Undo/Redo, Image operations, Layer operations, Effects,
Window, Help) remain hand-coded in setup_menus().

Usage::

    builder = MenuBuilder(
        menu_bar=self.menuBar(),
        action_registry=self._action_registry,
        callback_resolver=self._resolve_menu_callback,
        shortcut_manager=self.shortcut_manager,
    )
    builder.register_existing_menu("File", self.menu_file)
    self._module_actions = builder.build(enabled_features)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Set

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QMenuBar

from src.shared.actions import ActionRegistry, MenuActionDescriptor
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class MenuBuilder:
    """Builds QMenu / QAction entries from registered MenuActionDescriptors.

    For each descriptor the builder:
    1. Resolves the *menu_path* to an existing QMenu (registered via
       :meth:`register_existing_menu`) or creates intermediate sub-menus.
    2. Creates a ``QAction`` from *label* (AUD-3603: labels arrive
       pre-translated — feature modules translate them when registering the
       descriptor; the builder itself does not re-translate action labels,
       though auto-created top-level/sub-menu titles do go through I18N —
       see AUD-3600/AUD-3601).
    3. Looks up the shortcut in :class:`ShortcutManager` when
       *shortcut_id* is set and applies it.
    4. Resolves the *callback_name* through the *callback_resolver* and
       connects ``triggered``.
    5. Registers the shortcut with :class:`ShortcutManager` so it appears
       in the Keyboard Shortcuts dialog.
    """

    def __init__(
        self,
        menu_bar: QMenuBar,
        action_registry: ActionRegistry,
        callback_resolver: Callable[[str], Optional[Callable]],
        shortcut_manager=None,
    ) -> None:
        self._menu_bar = menu_bar
        self._action_registry = action_registry
        self._callback_resolver = callback_resolver
        self._shortcut_manager = shortcut_manager
        self._menus: Dict[str, QMenu] = {}  # path -> QMenu
        self._actions: Dict[str, QAction] = {}  # action_id -> QAction

    # ── public API ──────────────────────────────────────────────────

    def register_existing_menu(self, name: str, menu: QMenu) -> None:
        """Register a top-level or sub-menu so descriptors can add items."""
        self._menus[name] = menu

    def build(
        self,
        enabled_features: Optional[Set[str]] = None,
        insert_before: Optional[Dict[str, QAction]] = None,
    ) -> Dict[str, QAction]:
        """Build all menus from registered descriptors.

        Args:
            enabled_features: Set of enabled feature IDs. ``None`` includes
                all features (no filtering).
            insert_before: Optional mapping of *menu_path* to a ``QAction``
                before which new actions should be inserted.  When not
                provided actions are appended to the end of the menu.

        Returns:
            Dict mapping *action_id* -> ``QAction``.
        """
        descriptors = self._action_registry.get_actions(enabled_features)

        # Group descriptors by menu_path for ordered insertion.
        by_path: Dict[str, List[MenuActionDescriptor]] = defaultdict(list)
        for desc in descriptors:
            by_path[desc.menu_path].append(desc)

        for menu_path, descs in by_path.items():
            # Resolve the target menu.
            menu = self._resolve_menu(menu_path)
            if menu is None:
                # [AUD-3600] 此前仅 DEBUG 级静默跳过——View/Timeline 的 5 个
                # action 因此从未出现且生产日志不可见。_resolve_menu 现会在
                # 持有 menu_bar 时自动补建顶层菜单，走到这里说明连 menu_bar
                # 都没有（如测试替身），至少以 warning 让配置错误可见。
                logger.warning(
                    "MenuBuilder: no menu registered for path %r, skipping %d actions",
                    menu_path,
                    len(descs),
                )
                continue

            # Determine anchor action for insert-before semantics.
            before_action: Optional[QAction] = None
            if insert_before and menu_path in insert_before:
                before_action = insert_before[menu_path]

            for desc in descs:
                self._build_action(desc, menu, before_action)

        return dict(self._actions)

    def clear(self) -> None:
        """Remove all actions created by this builder."""
        for action in self._actions.values():
            parent = action.parent()
            if isinstance(parent, QMenu):
                parent.removeAction(action)
            elif action.associatedWidgets():
                for w in action.associatedWidgets():
                    if isinstance(w, QMenu):
                        w.removeAction(action)
                        break
        self._actions.clear()

    def get_action(self, action_id: str) -> Optional[QAction]:
        """Return a previously built ``QAction`` by *action_id*."""
        return self._actions.get(action_id)

    # ── internal helpers ────────────────────────────────────────────

    def _resolve_menu(self, menu_path: str) -> Optional[QMenu]:
        """Resolve a ``menu_path`` to a ``QMenu``.

        The path format is ``"TopMenu/SubMenu"``. The top-level menu must
        have been registered via :meth:`register_existing_menu` — or, as of
        [AUD-3600], is auto-created on the builder's ``menu_bar`` when the
        caller never registered it (the "View/Timeline" group of edit
        actions was silently dropped this way). Sub-menus are created
        on-the-fly when needed.
        """
        # Direct lookup first (handles both "File" and "File/Edit Project").
        if menu_path in self._menus:
            return self._menus[menu_path]

        # str.split("/") always yields at least one element; the only real
        # empty case is an empty path itself.
        if not menu_path:
            return None

        # First part = top-level menu name.
        parts = menu_path.split("/")
        top_name = parts[0]
        top_menu = self._menus.get(top_name)
        if top_menu is None:
            # [AUD-3600] 顶层菜单未注册：持有 menu_bar 时自动补建（标题走
            # i18n，追加在菜单栏末尾），让未注册路径下的 action 不再被
            # 整组丢弃；无 menu_bar（测试替身）时返回 None 交由调用方
            # 以 warning 记录。
            if self._menu_bar is None:
                return None
            top_menu = self._menu_bar.addMenu(
                I18N.tr(f"menu.{top_name.lower()}", top_name))
            self._menus[top_name] = top_menu
            logger.info(
                "MenuBuilder: top-level menu %r not registered; auto-created",
                top_name,
            )

        current = top_menu
        full_path = top_name
        for sub in parts[1:]:
            full_path = full_path + "/" + sub
            if full_path in self._menus:
                current = self._menus[full_path]
                continue
            # Create intermediate sub-menu.
            # [AUD-3601] 中间子菜单标题必须经 i18n——此前直接用英文段
            # （如 "Edit/Timeline" 的 "Timeline"），中文界面显示英文。
            sub_menu = current.addMenu(self._translated_segment(sub))
            self._menus[full_path] = sub_menu
            current = sub_menu

        return current

    @staticmethod
    def _translated_segment(segment: str) -> str:
        """[AUD-3601] 中间子菜单标题的 i18n 查找链。

        ``menu.<segment>`` 为规范键；历史键 ``panel.<segment>``（如
        panel.timeline，全部 10 个 locale 均有条目）作为二级查找；
        均未命中时回退英文原文段（与旧行为一致，等待词条补齐）。
        locale 键一律小写（menu.view / panel.timeline），段名需归一。"""
        seg = segment.lower()
        for key in (f"menu.{seg}", f"panel.{seg}"):
            translated = I18N.tr_or_none(key)
            if translated:
                return translated
        return segment

    def _build_action(
        self,
        desc: MenuActionDescriptor,
        menu: QMenu,
        before_action: Optional[QAction] = None,
    ) -> Optional[QAction]:
        """Create and wire a single ``QAction`` from *desc*.

        If ``desc.callback_name`` is set but the callback cannot be resolved,
        this method returns ``None`` and the action is **not** created. This
        prevents empty-shell menu items (clickable but no-op) and orphan
        shortcut bindings from polluting the menu and the ShortcutManager.
        """
        # 先校验 callback 是否存在 —— 不存在就跳过整个 action
        # （避免产生"点了没反应"的空壳菜单项 + 空壳快捷键）
        # 注：feature module 经常会提前注册 MenuActionDescriptor（含菜单标签、
        # 快捷键）但对应的 handler 还在开发中。这种"占位但未实现"是预期情况，
        # 用 debug 级别即可，避免启动日志被大量良性告警淹没。
        callback = None
        if desc.callback_name:
            callback = self._callback_resolver(desc.callback_name)
            if callback is None:
                logger.debug(
                    "MenuBuilder: skipping action %r — callback %r not found "
                    "(not yet implemented)",
                    desc.action_id,
                    desc.callback_name,
                )
                return None

        action = QAction(desc.label, menu.parent())

        # Checkable toggles (e.g. View overlays) pass the new checked state
        # to the callback via triggered(bool).
        if desc.checkable:
            action.setCheckable(True)

        # Bind shortcut via ShortcutManager.
        if desc.shortcut_id and self._shortcut_manager:
            self._apply_shortcut(desc, action)

        # Connect callback.
        if callback:
            action.triggered.connect(callback)

        # Insert into menu.
        if before_action and before_action in menu.actions():
            menu.insertAction(before_action, action)
        else:
            menu.addAction(action)

        self._actions[desc.action_id] = action
        return action

    def _apply_shortcut(self, desc: MenuActionDescriptor, action: QAction) -> None:
        """Apply shortcut to *action* and register with ShortcutManager.

        If the shortcut_id is already registered in the ShortcutManager we
        apply its current shortcut. Otherwise we derive the default shortcut
        from the descriptor and register a new entry.
        """
        sm = self._shortcut_manager
        existing = sm.get_action(desc.shortcut_id)
        if existing is not None:
            # Already registered — apply and update the QAction reference.
            sm.update_action_reference(desc.shortcut_id, action)
        else:
            # Not yet registered — derive a sensible default shortcut.
            default_shortcut = self._derive_default_shortcut(desc)
            sm.register(
                desc.shortcut_id,
                desc.label.replace("&", ""),
                self._category_for_path(desc.menu_path),
                default_shortcut,
                action,
                feature_id=desc.feature_id,
            )

    # ── shortcut helpers ────────────────────────────────────────────

    @staticmethod
    def _derive_default_shortcut(desc: MenuActionDescriptor) -> str:
        """Derive a default shortcut string for a descriptor.

        Uses known mappings for module actions; app-level project shortcuts
        are registered directly by the main File menu.
        """
        _DEFAULTS = {
            "deliver.export": "Ctrl+E",
        }
        return _DEFAULTS.get(desc.shortcut_id, "")

    @staticmethod
    def _category_for_path(menu_path: str) -> str:
        """Map a menu_path to a ShortcutManager category."""
        return menu_path.split("/")[0]
