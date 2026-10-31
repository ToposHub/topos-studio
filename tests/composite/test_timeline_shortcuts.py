"""P4 tests: AE-style shortcut registry + focus-aware dispatcher.

Pure-logic tests — no QWidget needed (the dispatcher's focus check is
overridable via ``focus_query``). Covers the three P0 baseline findings:

- §6.4: plain ``K`` must NAVIGATE (not add); ``Ctrl/Cmd+K`` must ADD.
- §6.4: ``J`` must navigate to the previous keyframe.
- §6.5: shortcuts must be suppressed while a text input has focus, but
  modifier-bearing commands (Ctrl+V etc.) must still fire.
Plus the new T40 behaviour: ``UU`` → modified filter, ``EE`` → expression
filter, with a double-tap window.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ui.timeline_shortcuts import (
    CMD_ADD_KEYFRAME,
    CMD_COPY,
    CMD_FILTER_ANIMATED,
    CMD_FILTER_EXPRESSION,
    CMD_FILTER_MODIFIED,
    CMD_FOCUS_OPACITY,
    CMD_FOCUS_POSITION,
    CMD_FOCUS_ROTATION,
    CMD_FOCUS_SCALE,
    CMD_NAV_NEXT_KEYFRAME,
    CMD_NAV_PREV_KEYFRAME,
    CMD_PASTE,
    CMD_PLAY_TOGGLE,
    CMD_SELECT_ALL,
    DEFAULT_AE_BINDINGS,
    ShortcutDispatcher,
    ShortcutRegistry,
)

pytestmark = pytest.mark.composite_core


# ── stand-ins ───────────────────────────────────────────────────────────


class _FakeLineEdit:
    """Stand-in for a focused text input.

    Declares ``is_text_input = True`` so the dispatcher's focus guard
    recognises it without importing PyQt.
    """
    is_text_input = True


class _FakeButton:
    """Stand-in for a non-text widget (no is_text_input attribute)."""


def _dispatcher_with_focus(focused):
    """A dispatcher whose focus check returns ``focused`` (or None)."""
    d = ShortcutDispatcher()
    d.focus_query = lambda: focused
    return d


def _bind_all(d):
    """Bind a recording handler to every known command."""
    fired = []
    for cmd in d.registry.known_commands():
        d.bind(cmd, (lambda c: lambda: fired.append(c))(cmd))
    return fired


# ── T37/T38: J/K navigation + Cmd+K add ────────────────────────────────


class TestAENavShortcuts:
    def test_plain_K_navigates_to_next_keyframe_not_add(self):
        """P0 §6.4: plain K was ADD; AE users expect NAV. The default AE
        binding must make K navigate."""
        d = ShortcutDispatcher()
        assert d.registry.command_for("K") == CMD_NAV_NEXT_KEYFRAME

    def test_plain_J_navigates_to_prev_keyframe(self):
        d = ShortcutDispatcher()
        assert d.registry.command_for("J") == CMD_NAV_PREV_KEYFRAME

    def test_ctrl_K_and_cmd_K_both_add(self):
        """Platform normalisation: Win/Linux Ctrl+K and macOS Cmd+K both
        add a keyframe (plan §4.2)."""
        d = ShortcutDispatcher()
        assert d.registry.command_for("Ctrl+K") == CMD_ADD_KEYFRAME
        assert d.registry.command_for("Cmd+K") == CMD_ADD_KEYFRAME

    def test_handle_key_K_fires_nav_handler(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        cmd = d.handle_key("K")
        assert cmd == CMD_NAV_NEXT_KEYFRAME
        assert fired == [CMD_NAV_NEXT_KEYFRAME]

    def test_handle_key_ctrl_K_fires_add_handler(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        cmd = d.handle_key("Ctrl+K")
        assert cmd == CMD_ADD_KEYFRAME
        assert fired == [CMD_ADD_KEYFRAME]

    def test_legacy_mode_restores_K_adds(self):
        """T06 'AE-habits' switch: turning off AE nav restores the legacy
        behaviour the 216 existing UI tests were written against (K = add)."""
        reg = ShortcutRegistry()
        reg.disable_ae_nav_conflicts()
        assert reg.command_for("K") == CMD_ADD_KEYFRAME
        assert reg.command_for("J") is None  # unmapped in legacy mode
        # Cmd/Ctrl+K still adds so the AE path remains reachable.
        assert reg.command_for("Ctrl+K") == CMD_ADD_KEYFRAME


# ── T39: P/S/R/A/T focus shortcuts ─────────────────────────────────────


class TestPropertyFocusShortcuts:
    def test_P_S_R_A_T_bound_to_focus_commands(self):
        from src.features.composite.ui.timeline_shortcuts import CMD_FOCUS_ANCHOR
        d = ShortcutDispatcher()
        assert d.registry.command_for("P") == CMD_FOCUS_POSITION
        assert d.registry.command_for("S") == CMD_FOCUS_SCALE
        assert d.registry.command_for("R") == CMD_FOCUS_ROTATION
        assert d.registry.command_for("A") == CMD_FOCUS_ANCHOR
        assert d.registry.command_for("T") == CMD_FOCUS_OPACITY


# ── T40: U / UU / E / EE filters ───────────────────────────────────────


class TestFilterShortcuts:
    def test_single_U_shows_animated(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        cmd = d.handle_key("U", now_ms=0)
        assert cmd == CMD_FILTER_ANIMATED
        assert fired == [CMD_FILTER_ANIMATED]

    def test_double_U_within_window_upgrades_to_modified(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        d.handle_key("U", now_ms=0)
        cmd = d.handle_key("U", now_ms=100)
        assert cmd == CMD_FILTER_MODIFIED
        # First tap fired ANIMATED; second tap fired MODIFIED only (not both
        # in the same second-tap call).
        assert fired == [CMD_FILTER_ANIMATED, CMD_FILTER_MODIFIED]

    def test_double_U_outside_window_stays_animated(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        d.handle_key("U", now_ms=0)
        cmd = d.handle_key("U", now_ms=10_000)  # way past 350ms window
        assert cmd == CMD_FILTER_ANIMATED
        assert fired == [CMD_FILTER_ANIMATED, CMD_FILTER_ANIMATED]

    def test_E_and_double_E(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        assert d.handle_key("E", now_ms=0) == CMD_FILTER_EXPRESSION
        assert d.handle_key("E", now_ms=100) == CMD_FILTER_EXPRESSION
        assert fired == [CMD_FILTER_EXPRESSION, CMD_FILTER_EXPRESSION]

    def test_interrupted_streak_resets(self):
        """U … K … U must NOT count as a double-tap."""
        d = ShortcutDispatcher()
        _bind_all(d)
        d.handle_key("U", now_ms=0)
        d.handle_key("K", now_ms=50)
        cmd = d.handle_key("U", now_ms=100)
        assert cmd == CMD_FILTER_ANIMATED  # not MODIFIED


# ── focus context filter (P0 §6.5) ─────────────────────────────────────


class TestFocusContextFilter:
    def test_text_input_focused_suppresses_K(self):
        d = _dispatcher_with_focus(_FakeLineEdit())
        fired = _bind_all(d)
        cmd = d.handle_key("K")
        assert cmd is None
        assert fired == []

    def test_text_input_focused_suppresses_P(self):
        d = _dispatcher_with_focus(_FakeLineEdit())
        fired = _bind_all(d)
        assert d.handle_key("P") is None
        assert fired == []

    def test_text_input_focused_does_NOT_suppress_ctrl_V(self):
        """Modifier commands must still fire while typing (paste into a
        search box, undo a typed char)."""
        d = _dispatcher_with_focus(_FakeLineEdit())
        fired = _bind_all(d)
        assert d.handle_key("Ctrl+V") == CMD_PASTE
        assert fired == [CMD_PASTE]

    def test_text_input_focused_does_NOT_suppress_ctrl_A(self):
        d = _dispatcher_with_focus(_FakeLineEdit())
        fired = _bind_all(d)
        assert d.handle_key("Ctrl+A") == CMD_SELECT_ALL
        assert fired == [CMD_SELECT_ALL]

    def test_non_text_focus_allows_K(self):
        d = _dispatcher_with_focus(_FakeButton())
        fired = _bind_all(d)
        assert d.handle_key("K") == CMD_NAV_NEXT_KEYFRAME
        assert fired == [CMD_NAV_NEXT_KEYFRAME]

    def test_no_focus_allows_K(self):
        d = _dispatcher_with_focus(None)
        fired = _bind_all(d)
        assert d.handle_key("K") == CMD_NAV_NEXT_KEYFRAME
        assert fired == [CMD_NAV_NEXT_KEYFRAME]

    def test_guard_disabled_allows_everything(self):
        d = _dispatcher_with_focus(_FakeLineEdit())
        d.text_input_guard = False
        fired = _bind_all(d)
        assert d.handle_key("K") == CMD_NAV_NEXT_KEYFRAME
        assert fired == [CMD_NAV_NEXT_KEYFRAME]


# ── robustness ─────────────────────────────────────────────────────────


class TestDispatcherRobustness:
    def test_unknown_key_returns_none(self):
        d = ShortcutDispatcher()
        _bind_all(d)
        assert d.handle_key("F13") is None
        assert d.handle_key("Ctrl+Shift+Alt+Z") is None

    def test_buggy_handler_does_not_break_dispatcher(self):
        d = ShortcutDispatcher()

        def boom():
            raise RuntimeError("handler crashed")
        d.bind(CMD_ADD_KEYFRAME, boom)
        # Must not raise.
        cmd = d.handle_key("Ctrl+K")
        assert cmd == CMD_ADD_KEYFRAME

    def test_unbind_removes_handler(self):
        d = ShortcutDispatcher()
        fired = _bind_all(d)
        d.unbind(CMD_NAV_NEXT_KEYFRAME)
        cmd = d.handle_key("K")
        # Command still resolves (registry intact), but handler doesn't fire.
        assert cmd == CMD_NAV_NEXT_KEYFRAME
        assert fired == []

    def test_sequence_whitespace_normalised(self):
        d = ShortcutDispatcher()
        assert d.registry.command_for("Ctrl + K") == CMD_ADD_KEYFRAME
        assert d.registry.command_for("  K  ") == CMD_NAV_NEXT_KEYFRAME


# ── registry mutability + capability query (T062 prep) ─────────────────


class TestRegistry:
    def test_default_bindings_cover_canonical_ae_set(self):
        reg = ShortcutRegistry()
        # Sanity: the AE essentials are all bound by default.
        for seq, cmd in [
            ("K", CMD_NAV_NEXT_KEYFRAME),
            ("J", CMD_NAV_PREV_KEYFRAME),
            ("Ctrl+K", CMD_ADD_KEYFRAME),
            ("Cmd+K", CMD_ADD_KEYFRAME),
            ("Space", CMD_PLAY_TOGGLE),
            ("U", CMD_FILTER_ANIMATED),
            ("P", CMD_FOCUS_POSITION),
            ("S", CMD_FOCUS_SCALE),
            ("R", CMD_FOCUS_ROTATION),
            ("T", CMD_FOCUS_OPACITY),
        ]:
            assert reg.command_for(seq) == cmd, f"missing default: {seq}"

    def test_custom_binding_overrides_default(self):
        reg = ShortcutRegistry()
        reg.set_binding("K", CMD_ADD_KEYFRAME)  # legacy
        assert reg.command_for("K") == CMD_ADD_KEYFRAME

    def test_clear_binding(self):
        reg = ShortcutRegistry()
        reg.clear_binding("K")
        assert reg.command_for("K") is None

    def test_sequences_for_command_lists_all_spelling_variants(self):
        reg = ShortcutRegistry()
        seqs = reg.sequences_for_command(CMD_ADD_KEYFRAME)
        # Ctrl+K and Cmd+K both add.
        assert "Ctrl+K" in seqs
        assert "Cmd+K" in seqs

    def test_known_commands_is_a_complete_set(self):
        reg = ShortcutRegistry()
        cmds = reg.known_commands()
        # Every canonical command is reachable via at least one binding.
        assert CMD_PLAY_TOGGLE in cmds
        assert CMD_FOCUS_OPACITY in cmds
