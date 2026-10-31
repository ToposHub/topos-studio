"""
ShortcutManager - Keyboard shortcut customization system for Topos Color.

Provides a centralized registry for all keyboard shortcuts, supporting:
- Default shortcut registration
- User customization with persistence via ConfigManager
- Conflict detection
- Dynamic re-application to QActions
- DaVinci Resolve-style categorization

Architecture:
    ShortcutManager holds a registry of ShortcutAction objects.
    Each action has a unique ID, display label, category, and default shortcut.
    Custom shortcuts are stored in config.json and override defaults at runtime.

════════════════════════════════════════════════════════════════════════
⚠ BEFORE REGISTERING A NEW SHORTCUT — scope rules (the MG Space incident)
════════════════════════════════════════════════════════════════════════

QShortcuts consume a key BEFORE any event filter runs, and on macOS a
main-window shortcut (Window- or Application-scoped) still fires while a
parented modeless Tool window — the Motion Graphic editor, its HTML source
editor, its DevTools panel — is the active window.  Combined, any
window/application-wide QShortcut silently steals keys from those editors,
and code in event filters can never recover them.  Full write-up and the
shared guard helpers: src/shared/ui/shortcut_scope.py (enforced in part by
tests/edit/test_shortcut_scope_gates.py).

Rules:
1. Panel-transport / tool keys (Space, single letters, JKL…):
   Qt.WidgetWithChildrenShortcut — they cannot leave their panel.
2. Window-wide ergonomics: Qt.WindowShortcut PLUS a
   shortcut_owner_window_active() guard in the handler.
3. UI-flow keys that must share with modeless editors (Esc, Enter):
   no QShortcut at all — match them in an event filter / keyPressEvent.
4. Qt.ApplicationShortcut only for Cmd-level app commands, with in-handler
   routing to the active modeless editor where relevant
   (see active_motion_graphic_editor / FileOpsMixin.save_file).

QAction-bound registrations here additionally follow the user's
global_shortcuts preference; forcing ApplicationShortcut via the
"force_application_shortcut" property opts an action out permanently —
reserve it for commands that must work from any dock (project save).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtCore import Qt, QObject, pyqtSignal


@dataclass
class ShortcutAction:
    """Represents a single keyboard-shortcuttable action in the application.
    
    Attributes:
        id: Unique identifier, e.g. "file.open"
        label: Display name for the action, e.g. "Open Image..."
        category: Category group, e.g. "File", "Edit", "View"
        default_shortcut: Default key sequence string, e.g. "Ctrl+O". Empty string if none.
        action: Reference to the QAction this shortcut is bound to (set during registration)
    """
    id: str
    label: str
    category: str
    default_shortcut: str = ""
    action: Optional[QAction] = field(default=None, repr=False)
    feature_id: Optional[str] = None  # feature that owns this shortcut

    @property
    def shortcut(self) -> str:
        """Get the currently active shortcut (custom override or default)."""
        manager = ShortcutManager.instance()
        if manager:
            return manager.get_shortcut(self.id)
        return self.default_shortcut


@dataclass(frozen=True)
class ShortcutPresetReport:
    """Outcome of importing a shortcut preset (P2-01)."""

    applied: tuple = ()
    unknown: tuple = ()
    conflicts: tuple = ()  # (key_sequence, [action_ids])
    errors: tuple = ()

    @property
    def ok(self) -> bool:
        return not self.errors and not self.conflicts and not self.unknown

    def to_dict(self) -> dict:
        return {
            "applied": list(self.applied),
            "unknown": list(self.unknown),
            "conflicts": [list(c) for c in self.conflicts],
            "errors": list(self.errors),
        }


class ShortcutManager(QObject):
    """Centralized keyboard shortcut management system.
    
    Singleton pattern - use ShortcutManager.instance() to access.
    
    The manager maintains:
    - A registry of all shortcut actions with their defaults
    - A map of user-customized shortcuts (persisted to config.json)
    - References to QActions for dynamic shortcut re-application
    """

    _instance: Optional['ShortcutManager'] = None

    shortcuts_changed = pyqtSignal()  # Emitted when shortcuts are modified

    def __init__(self, config_manager=None) -> None:
        super().__init__()
        self._registry: Dict[str, ShortcutAction] = {}
        self._custom_shortcuts: Dict[str, str] = {}
        self._config_manager = config_manager

        # Load custom shortcuts from config
        if self._config_manager:
            saved = self._config_manager.get("custom_shortcuts")
            if saved and isinstance(saved, dict):
                self._custom_shortcuts = saved

    @classmethod
    def initialize(cls, config_manager=None) -> 'ShortcutManager':
        """Create and set the singleton instance."""
        cls._instance = cls(config_manager)
        return cls._instance

    @classmethod
    def instance(cls) -> Optional['ShortcutManager']:
        """Get the singleton instance (returns None if not initialized)."""
        return cls._instance

    # ── Registration ──────────────────────────────────────────────

    def register(self, action_id: str, label: str, category: str,
                 default_shortcut: str, action: Optional[QAction] = None,
                 feature_id: Optional[str] = None) -> ShortcutAction:
        """Register a shortcut action.

        Args:
            action_id: Unique identifier (e.g. "file.open")
            label: Human-readable name (e.g. "Open Image...")
            category: Category for grouping (e.g. "File")
            default_shortcut: Default key sequence (e.g. "Ctrl+O"), "" for none
            action: Optional QAction to bind the shortcut to
            feature_id: Optional feature ID that owns this shortcut.
                When set, the shortcut is only active when the feature
                is enabled. None = app-level shortcut.

        Returns:
            The registered ShortcutAction
        """
        sa = ShortcutAction(
            id=action_id,
            label=label,
            category=category,
            default_shortcut=default_shortcut,
            action=action,
            feature_id=feature_id,
        )
        self._registry[action_id] = sa

        # Apply the active shortcut to the QAction
        if action:
            self._apply_shortcut_to_action(sa)

        return sa

    def register_action(self, action_id: str, label: str, category: str,
                        default_shortcut: str, action: QAction,
                        feature_id: Optional[str] = None) -> ShortcutAction:
        """Convenience method to register with a QAction.

        Same as register() but action parameter is required.
        """
        return self.register(action_id, label, category, default_shortcut, action,
                             feature_id=feature_id)

    # ── Query ─────────────────────────────────────────────────────

    def get_shortcut(self, action_id: str) -> str:
        """Get the active shortcut for an action (custom or default).
        
        Args:
            action_id: The action identifier
            
        Returns:
            The key sequence string, or "" if not found / no shortcut
        """
        custom = self._custom_shortcuts.get(action_id)
        if custom is not None:
            return custom
        
        sa = self._registry.get(action_id)
        if sa:
            return sa.default_shortcut
        return ""

    def get_action(self, action_id: str) -> Optional[ShortcutAction]:
        """Get a registered ShortcutAction by ID."""
        return self._registry.get(action_id)

    def get_categories(self) -> List[str]:
        """Get all unique categories in registration order."""
        seen: List[str] = []
        for sa in self._registry.values():
            if sa.category not in seen:
                seen.append(sa.category)
        return seen

    def get_actions_by_category(self, category: str) -> List[ShortcutAction]:
        """Get all actions in a given category."""
        return [sa for sa in self._registry.values() if sa.category == category]

    def get_all_actions(self) -> List[ShortcutAction]:
        """Get all registered actions."""
        return list(self._registry.values())

    def get_actions_by_feature(self, enabled_features: Optional[set] = None) -> List[ShortcutAction]:
        """Get actions filtered by enabled features.

        Args:
            enabled_features: Set of enabled feature IDs. None = return all.
        """
        if enabled_features is None:
            return list(self._registry.values())

        result = []
        for action in self._registry.values():
            if action.feature_id is None or action.feature_id in enabled_features:
                result.append(action)
        return result

    def find_actions(self, query: str) -> List[ShortcutAction]:
        """Search actions by label or ID (case-insensitive).
        
        Args:
            query: Search string
            
        Returns:
            List of matching ShortcutActions
        """
        query_lower = query.lower()
        return [
            sa for sa in self._registry.values()
            if query_lower in sa.label.lower() or query_lower in sa.id.lower()
        ]

    # ── Modification ──────────────────────────────────────────────

    def set_shortcut(self, action_id: str, key_sequence: str) -> bool:
        """Set a custom shortcut for an action.
        
        Args:
            action_id: The action identifier
            key_sequence: The new key sequence string (e.g. "Ctrl+K"), "" to clear
            
        Returns:
            True if the action was found and updated, False otherwise
        """
        if action_id not in self._registry:
            return False

        sa = self._registry[action_id]

        if key_sequence == sa.default_shortcut:
            # Same as default - remove custom override
            self._custom_shortcuts.pop(action_id, None)
        else:
            self._custom_shortcuts[action_id] = key_sequence

        # Apply to QAction
        if sa.action:
            self._apply_shortcut_to_action(sa)

        self._save_custom_shortcuts()
        self.shortcuts_changed.emit()
        return True

    def remove_shortcut(self, action_id: str) -> bool:
        """Remove any custom shortcut override, reverting to default.
        
        Args:
            action_id: The action identifier
            
        Returns:
            True if a custom override was removed, False otherwise
        """
        if action_id in self._custom_shortcuts:
            del self._custom_shortcuts[action_id]
            sa = self._registry.get(action_id)
            if sa and sa.action:
                self._apply_shortcut_to_action(sa)
            self._save_custom_shortcuts()
            self.shortcuts_changed.emit()
            return True
        return False

    def reset_all(self) -> None:
        """Reset all shortcuts to their defaults."""
        self._custom_shortcuts.clear()
        # Re-apply all defaults to QActions
        for sa in self._registry.values():
            if sa.action:
                self._apply_shortcut_to_action(sa)
        self._save_custom_shortcuts()
        self.shortcuts_changed.emit()

    def reset_category(self, category: str) -> None:
        """Reset all shortcuts in a category to their defaults."""
        for sa in self.get_actions_by_category(category):
            aid = sa.id
            if aid in self._custom_shortcuts:
                del self._custom_shortcuts[aid]
                if sa.action:
                    self._apply_shortcut_to_action(sa)
        self._save_custom_shortcuts()
        self.shortcuts_changed.emit()

    # ── Preset Export / Import (P2-01) ────────────────────────────

    PRESET_SCHEMA = "topos-shortcut-preset"
    PRESET_VERSION = 1

    def export_preset(self) -> dict:
        """Export the full shortcut map as a schema'd preset dict (P2-01).

        Contains every registered action's currently active shortcut
        (custom override or default), keyed by action id. Round-trips
        through :meth:`import_preset`.
        """
        return {
            "schema": self.PRESET_SCHEMA,
            "version": self.PRESET_VERSION,
            "shortcuts": {
                action_id: sa.default_shortcut if action_id not in self._custom_shortcuts
                else self._custom_shortcuts[action_id]
                for action_id, sa in self._registry.items()
            },
        }

    def import_preset(self, data: dict, mode: str = "merge") -> "ShortcutPresetReport":
        """Apply a preset produced by :meth:`export_preset` (P2-01).

        Validation is strict and total — the report lists every skipped
        entry with a reason instead of silently dropping it:

        - schema/version must match;
        - non-string action ids / key sequences are rejected (reported as
          errors, the entry skipped) — a preset file is untrusted input;
        - unknown action ids are skipped (reported);
        - duplicate key sequences WITHIN the preset are conflicts and none
          of the conflicting entries is applied;
        - valid entries apply through :meth:`set_shortcut` (entries equal
          to an action's default clear its custom override).

        Args:
            data: Parsed preset dict.
            mode: "merge" keeps overrides for actions absent from the
                preset; "replace" first resets every override so the
                preset fully determines the map.

        Returns:
            Immutable ShortcutPresetReport.
        """
        applied: list[str] = []
        unknown: list[str] = []
        conflicts: list[tuple[str, list[str]]] = []
        errors: list[str] = []

        if not isinstance(data, dict):
            return ShortcutPresetReport(
                applied=(), unknown=(), conflicts=(), errors=("preset must be a dict",)
            )
        if data.get("schema") != self.PRESET_SCHEMA:
            errors.append(
                f"unknown schema {data.get('schema')!r}; expected {self.PRESET_SCHEMA!r}"
            )
        version = data.get("version")
        if not isinstance(version, int) or version > self.PRESET_VERSION:
            errors.append(f"unsupported preset version: {version!r}")
        if errors:
            return ShortcutPresetReport(
                applied=(), unknown=(), conflicts=(), errors=tuple(errors)
            )

        entries = data.get("shortcuts")
        if not isinstance(entries, dict):
            return ShortcutPresetReport(
                applied=(), unknown=(), conflicts=(),
                errors=('"shortcuts" must be a mapping of action id -> sequence',),
            )

        # Validate every entry BEFORE anything hashes or sorts it — preset
        # files are external, untrusted input (AUD-38000):
        # a list/dict value is unhashable (TypeError in the duplicate-
        # sequence scan below) and a non-str value would be persisted by
        # set_shortcut and crash QKeySequence(seq) later.  Malformed
        # entries are skipped and reported instead of aborting the app.
        known: Dict[str, str] = {}
        unknown: List[str] = []
        for aid, seq in entries.items():
            if not isinstance(aid, str):
                errors.append(
                    f"invalid action id {aid!r}: expected string, "
                    f"got {type(aid).__name__}"
                )
                continue
            if aid not in self._registry:
                unknown.append(aid)
                continue
            if not isinstance(seq, str):
                errors.append(
                    f"invalid shortcut for {aid!r}: expected string, "
                    f"got {type(seq).__name__}"
                )
                continue
            known[aid] = seq

        # Detect duplicate sequences inside the preset (excluding "" = none).
        by_sequence: dict[str, list[str]] = {}
        for aid, seq in known.items():
            if seq:
                by_sequence.setdefault(seq, []).append(aid)
        conflicting_ids = set()
        for seq, aids in by_sequence.items():
            if len(aids) > 1:
                conflicts.append((seq, sorted(aids)))
                conflicting_ids.update(aids)

        if mode == "replace":
            self.reset_all()

        for aid, seq in sorted(known.items()):
            if aid in conflicting_ids:
                continue
            if self.set_shortcut(aid, seq):
                applied.append(aid)

        return ShortcutPresetReport(
            applied=tuple(applied),
            unknown=tuple(sorted(unknown)),
            conflicts=tuple(conflicts),
            errors=tuple(errors),
        )

    # ── Conflict Detection ────────────────────────────────────────

    def find_conflicts(self, key_sequence: str, exclude_action_id: str = "") -> List[ShortcutAction]:
        """Find all actions that use the given key sequence.
        
        Args:
            key_sequence: The key sequence to check
            exclude_action_id: An action ID to exclude from the check (e.g. the action being edited)
            
        Returns:
            List of ShortcutActions that already use this shortcut
        """
        if not key_sequence:
            return []

        conflicts: List[ShortcutAction] = []
        for sa in self._registry.values():
            if sa.id == exclude_action_id:
                continue
            if self.get_shortcut(sa.id) == key_sequence:
                conflicts.append(sa)
        return conflicts

    # ── Internal ──────────────────────────────────────────────────

    @staticmethod
    def _shortcut_context(action: QAction, global_mode: bool) -> Qt.ShortcutContext:
        """Return the context for an action, honoring explicit scope overrides."""
        if action.property("force_widget_shortcut") is True:
            return Qt.ShortcutContext.WidgetShortcut
        if action.property("force_application_shortcut") is True:
            return Qt.ShortcutContext.ApplicationShortcut
        return (Qt.ShortcutContext.ApplicationShortcut if global_mode
                else Qt.ShortcutContext.WindowShortcut)

    def _apply_shortcut_to_action(self, sa: ShortcutAction) -> None:
        """Apply the active shortcut to the QAction."""
        if sa.action is None:
            return

        shortcut_str = self.get_shortcut(sa.id)
        if shortcut_str:
            sa.action.setShortcut(QKeySequence(shortcut_str))
            # Apply global shortcut context if configured.
            # 注意：非全局模式用 WindowShortcut（窗口激活时生效），而非 WidgetShortcut
            # （仅菜单获焦时生效）。WidgetShortcut 会导致 Cmd+S 这类菜单快捷键在
            # 平时按根本不触发（用户焦点在画布/面板时菜单没获焦）。
            if self._config_manager:
                global_mode = self._config_manager.get("global_shortcuts") or False
                sa.action.setShortcutContext(
                    self._shortcut_context(sa.action, global_mode)
                )
        else:
            sa.action.setShortcut(QKeySequence())

    def set_global_mode(self, enabled: bool) -> None:
        """Set all registered shortcuts to application-wide or window-scoped context.

        Args:
            enabled: True for Qt.ApplicationShortcut, False for Qt.WindowShortcut
        """
        for sa in self._registry.values():
            if sa.action and self.get_shortcut(sa.id):
                sa.action.setShortcutContext(
                    self._shortcut_context(sa.action, enabled)
                )

    def _save_custom_shortcuts(self) -> None:
        """Persist custom shortcuts to config.json."""
        if self._config_manager:
            self._config_manager.set("custom_shortcuts", dict(self._custom_shortcuts))
            self._config_manager.save_config()

    def update_action_reference(self, action_id: str, action: QAction) -> None:
        """Update the QAction reference for a registered action.
        
        This is useful when menus are rebuilt and new QAction objects are created.
        Also immediately applies the current shortcut.
        """
        if action_id in self._registry:
            self._registry[action_id].action = action
            self._apply_shortcut_to_action(self._registry[action_id])
