"""B5: 时间线内嵌快捷键的集中注册与匹配层。

计划 B5 步骤 2+3（安全子集）：

- **盘点/注册**：``TIMELINE_KEY_ACTIONS`` 声明时间线编辑键 action 清单
  （action_id / 默认键 / 标签），``register_timeline_shortcuts`` 全量注册进
  ShortcutManager —— 用户在快捷键设置里可见、可改键、可检测冲突。
  与 scene_cut 快捷键同模式：仅注册，不安装全局 QShortcut（键的真正消费
  在 TimelineView 的 keyPressEvent 状态机里）。
- **匹配层**：``matches_key_event`` 把"键+修饰键 → action"的匹配改为查
  ShortcutManager 当前绑定，TimelineView 的工具键分支已迁移至该层；
  处理逻辑（状态机）完全不动。

未迁移（保留硬编码，组合键与系统语义耦合、双触发风险最高）：
Home/End/方向键/PageUp/Down、Space、Delete/Backspace、Ctrl+C/V/X/Z/B/D/A、
Esc —— 它们的全局语义已由 MainWindow 菜单 QAction 覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

from src.shared.ui.shortcut_manager import ShortcutManager

__all__ = [
    "TIMELINE_KEY_ACTIONS",
    "DEFAULT_KEY_BY_ACTION",
    "register_timeline_shortcuts",
    "matches_key_event",
]


@dataclass(frozen=True)
class TimelineKeyAction:
    """一个时间线内嵌键的声明。"""

    action_id: str
    default_key: str
    label: str
    category: str = "Timeline"


#: B5 盘点产出：TimelineView.keyPressEvent 的可迁移编辑键（工具/标记/IO）
TIMELINE_KEY_ACTIONS: Tuple[TimelineKeyAction, ...] = (
    TimelineKeyAction("edit.timeline.retime_controls", "Ctrl+R", "Retime Controls"),
    TimelineKeyAction("edit.timeline.linked_selection", "Ctrl+Shift+L", "Linked Selection"),
    TimelineKeyAction("edit.timeline.link_clips", "Ctrl+Alt+L", "Link Clips"),
    TimelineKeyAction("edit.timeline.tool.select", "A", "Select Tool"),
    TimelineKeyAction("edit.timeline.tool.trim", "T", "Trim Tool"),
    TimelineKeyAction("edit.timeline.tool.blade", "B", "Blade Tool"),
    TimelineKeyAction("edit.timeline.tool.ripple", "R", "Ripple Tool"),
    TimelineKeyAction("edit.timeline.tool.rate_stretch", "Shift+R", "Rate Stretch Tool"),
    TimelineKeyAction("edit.timeline.tool.roll", "W", "Roll Tool"),
    TimelineKeyAction("edit.timeline.tool.slip", "Y", "Slip Tool"),
    TimelineKeyAction("edit.timeline.tool.slide", "U", "Slide Tool"),
    TimelineKeyAction("edit.timeline.tool.hand", "H", "Hand Tool"),
    TimelineKeyAction("edit.timeline.tool.zoom", "Z", "Zoom Tool"),
    # INT-07：N 的真实 handler 是 toggle_snap（见 TimelineView 的
    # handler 映射 "edit.timeline.tool.next_enable" → toggle_snap_action）。
    # label 曾写 "Enable Next Clip" 与行为不符；action_id 保持不变以兼容
    # 已保存的用户自定义键映射。
    TimelineKeyAction("edit.timeline.tool.next_enable", "N", "Toggle Snap"),
    TimelineKeyAction("edit.timeline.tool.disable", "D", "Disable Clip"),
    # ``edit.timeline.add_marker`` is already declared by EditModule's menu
    # action and registered with ShortcutManager there.  Keeping it in this
    # local inventory would register the same action twice and makes shortcut
    # conflict detection report a false duplicate.
    TimelineKeyAction("edit.timeline.add_chapter", "Alt+M", "Add Chapter Marker"),
    TimelineKeyAction("edit.timeline.jump_prev_marker", "Ctrl+Shift+M", "Jump to Previous Marker"),
    TimelineKeyAction("edit.timeline.jump_next_marker", "Shift+M", "Jump to Next Marker"),
    TimelineKeyAction("edit.timeline.mark_in", "I", "Mark In"),
    TimelineKeyAction("edit.timeline.mark_out", "O", "Mark Out"),
)

#: action_id → 默认键（注册表缺失时的回退匹配基准）
DEFAULT_KEY_BY_ACTION = {a.action_id: a.default_key for a in TIMELINE_KEY_ACTIONS}


def register_timeline_shortcuts(shortcut_manager: ShortcutManager) -> None:
    """B5 步骤 2：全部 action 以默认键值注册（不改行为，纯注册）。"""
    # The production manager exposes ``get_action`` so registration is
    # idempotent.  Keep the helper duck-typed for lightweight integration
    # managers used by module tests and by embedders: a manager that only
    # implements ``register`` should still receive the complete declaration
    # rather than failing before the feature module can finish registering.
    get_action = getattr(shortcut_manager, "get_action", None)
    for action in TIMELINE_KEY_ACTIONS:
        if get_action is None or get_action(action.action_id) is None:
            shortcut_manager.register(
                action.action_id,
                action.label,
                action.category,
                action.default_key,
                feature_id="edit",
            )


def _normalize_modifiers(modifiers) -> int:
    """Qt.KeyboardModifier flags/enum → 规范化 int（Shift/Ctrl/Alt/Meta）。"""
    try:
        return int(modifiers)
    except TypeError:
        # PyQt6 枚举/flags 的 int() 在某些组合下需要显式 value
        return int(getattr(modifiers, "value", 0))


def matches_key_event(
    event: Any, action_id: str, default_key: str | None = None,
) -> bool:
    """B5 步骤 3：键事件是否匹配 action 的当前绑定。

    从 ShortcutManager 读取该 action 当前绑定（用户可改），解析出
    key + 修饰键后与事件比较。绑定缺失时回退 ``default_key``（保持
    未注册场景下的既有行为）；两者皆无返回 False。
    """
    from PyQt6.QtGui import QKeySequence

    binding = None
    action_registered = False
    try:
        manager = ShortcutManager.instance()
        if manager is not None:
            binding = manager.get_shortcut(action_id)
            # INT-07：action 已注册而绑定为空 = 用户显式清空——不得回退
            # 默认键（旧逻辑 if not binding 会退回 default，清空后旧键
            # 仍触发）。只有 action 完全未注册时才用 default_key 兜底。
            action_registered = (
                getattr(manager, "get_action", lambda _aid: None)(action_id)
                is not None
            )
    except Exception:
        binding = None
    if not binding and not action_registered:
        binding = default_key
    if not binding:
        return False

    try:
        sequences = QKeySequence(binding)
    except Exception:
        return False

    event_key = int(event.key())
    event_mods = _normalize_modifiers(event.modifiers())

    for index in range(sequences.count()):
        combination = sequences[index]
        expected_key = int(combination.key())
        expected_mods = _normalize_modifiers(combination.keyboardModifiers())
        if expected_key == event_key and expected_mods == event_mods:
            return True
    return False
