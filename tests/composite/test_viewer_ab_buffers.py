"""B2-2: A/B snapshot buffers — two frozen frames, wipe against live.

Slot bookkeeping is tested by stubbing the GPU snapshot blit; the texture
copies themselves need a real GL context (covered by manual QA).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.composite_core


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    yield viewer
    viewer.close()


def _fake_snapshot_factory(viewer, calls):
    """Replace the GL blit with bookkeeping that hands out fake tex ids."""

    def fake_snapshot(slot: str) -> bool:
        calls.append(slot)
        tex_id = 111 if slot == "A" else 222
        if slot == "B":
            viewer._owned_secondary_b_tex_id = tex_id
            viewer._owned_secondary_b_tex_size = (64, 64)
        else:
            viewer._owned_secondary_tex_id = tex_id
            viewer._owned_secondary_tex_size = (64, 64)
        viewer._ab_active_slot = slot
        viewer._secondary_tex_id = viewer._ab_owned_tex_id(slot)
        return True

    return fake_snapshot


class TestAbBuffers:
    def test_store_arms_split_and_switches_source(self, qapp):
        viewer = next(_make_viewer(qapp))
        calls = []
        viewer._snapshot_display_texture_to_slot = _fake_snapshot_factory(
            viewer, calls)

        assert viewer.store_ab_snapshot("A") is True
        assert viewer._split_active is True
        assert viewer._ab_active_slot == "A"
        assert viewer._secondary_tex_id == 111
        assert viewer._toolbar.split_btn.isChecked()

        viewer.store_ab_snapshot("B")
        assert viewer._ab_active_slot == "B"
        assert viewer._secondary_tex_id == 222
        assert calls == ["A", "B"]

    def test_compare_source_switch_and_empty_slot_guard(self, qapp):
        viewer = next(_make_viewer(qapp))
        calls = []
        viewer._snapshot_display_texture_to_slot = _fake_snapshot_factory(
            viewer, calls)
        viewer.store_ab_snapshot("A")

        # Slot B has nothing stored yet — switching to it must be refused.
        viewer.set_ab_compare_slot("B")
        assert viewer._ab_active_slot == "A"
        assert viewer._secondary_tex_id == 111

        viewer.store_ab_snapshot("B")
        viewer.set_ab_compare_slot("A")
        assert viewer._ab_active_slot == "A"
        assert viewer._secondary_tex_id == 111
        assert viewer._toolbar._ab_slot_actions["A"].isChecked()

    def test_clear_drops_both_slots_and_resets_source(self, qapp):
        viewer = next(_make_viewer(qapp))
        calls = []
        viewer._snapshot_display_texture_to_slot = _fake_snapshot_factory(
            viewer, calls)
        # Real deleters would call glDeleteTextures on the fake ids (no GL
        # context offscreen) — emulate their bookkeeping contract instead.
        def _fake_delete_a(*a, **k):
            viewer._owned_secondary_tex_id = 0
            viewer._owned_secondary_tex_size = None
            viewer._secondary_tex_id = 0

        def _fake_delete_b(*a, **k):
            viewer._owned_secondary_b_tex_id = 0
            viewer._owned_secondary_b_tex_size = None
            if viewer._ab_active_slot == "B":
                viewer._secondary_tex_id = 0

        viewer._delete_owned_secondary_texture = _fake_delete_a
        viewer._delete_owned_secondary_b_texture = _fake_delete_b
        viewer.store_ab_snapshot("A")
        viewer.store_ab_snapshot("B")

        viewer.clear_split_snapshot()
        assert viewer._split_active is False
        assert viewer._owned_secondary_tex_id == 0
        assert viewer._owned_secondary_b_tex_id == 0
        assert viewer._secondary_tex_id == 0
        assert viewer._ab_active_slot == "A"
        assert not viewer._toolbar.split_btn.isChecked()

    def test_split_toggle_snapshots_into_active_slot(self, qapp):
        viewer = next(_make_viewer(qapp))
        calls = []
        viewer._snapshot_display_texture_to_slot = _fake_snapshot_factory(
            viewer, calls)
        viewer.store_ab_snapshot("B")
        viewer.set_split_view(False)
        calls.clear()

        viewer.set_split_view(True)
        # The toolbar split toggle re-freezes into the user's chosen slot.
        assert calls == ["B"]

    def test_store_failure_keeps_split_state(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer._snapshot_display_texture_to_slot = lambda slot: False
        assert viewer.store_ab_snapshot("A") is False
        assert viewer._split_active is False

    def test_keyboard_store_shortcuts(self, qapp):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        viewer = next(_make_viewer(qapp))
        calls = []
        viewer._snapshot_display_texture_to_slot = _fake_snapshot_factory(
            viewer, calls)

        mods = (Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier)
        viewer.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_A, mods, "A"))
        viewer.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_B, mods, "B"))
        assert calls == ["A", "B"]
        assert viewer._ab_active_slot == "B"
