"""Tests for the per-effect collapsible sections in EditPropertiesPanel.

Covers the Phase 2 refactor:
* Each clip effect becomes its own EditSectionWidget (not a single
  "Effect Stack" section with a list).
* Sections honour the ``collapsed`` initial state.
* ``effect_mode`` adds enable/delete buttons that re-emit the existing
  effect_enabled_changed / effect_remove_requested signals.
* Native param sections (Transform/Crop/Opacity/Speed) collapse by
  default when the clip has effects.
* AddEffectCommand.create(effect_id=...) plants a pre-generated id so the
  panel can scroll to the new section before apply returns.

These tests run against a real QApplication (via the ``qapp`` fixture from
conftest) but use mock clips — they verify widget structure and signal
wiring, not actual GPU rendering.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QPushButton

from src.features.edit.core.commands import AddEffectCommand
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.clip_params import CropParams
from src.features.edit.ui.edit_properties_panel import (
    EditPropertiesPanel,
    EditSectionWidget,
)


# ──────────────────────────────────────────────────────────────────────────
# Mock helpers
# ──────────────────────────────────────────────────────────────────────────

def _make_mock_clip(effects: tuple | None = None) -> MagicMock:
    """Build a minimal mock clip that drives set_clip to render sections.

    ``effects`` defaults to empty — pass EffectInstance objects to exercise
    the per-effect section code path.
    """
    clip = MagicMock()
    clip.clip_id = "test-clip"
    clip.transform = MagicMock(
        translate_x=0.0, translate_y=0.0,
        scale_x=1.0, scale_y=1.0, rotation=0.0,
        pivot_mode="center", pivot_x=0.0, pivot_y=0.0,
    )
    clip.crop = MagicMock(
        left=0.0, right=0.0, top=0.0, bottom=0.0, feather=0.0,
    )
    clip.opacity = 1.0
    speed = MagicMock()
    speed.speed = 1.0
    speed.reverse = False
    speed.method = "linear"
    # ``isinstance(clip.speed, SpeedParams)`` check in _build_speed_section
    # needs this to be a real SpeedParams to take the happy path; the mock
    # otherwise falls through to the float() branch which also works.
    clip.speed = speed
    clip.effects = effects if effects is not None else ()
    clip.asset_id = "asset-1"
    clip.name = "Test"
    clip.timeline_start = 0
    clip.timeline_end = 100
    return clip


def _make_blur_effect(effect_id: str = "eff-blur-1") -> EffectInstance:
    """Build a real blur EffectInstance via the registry (covers migration
    and the full create_instance pipeline)."""
    from src.features.edit.core.effect_registry import get_effect_registry
    reg = get_effect_registry()
    blur_def = reg.get_definition("blur")
    inst = blur_def.create_instance(order=0)
    return inst.with_effect_id(effect_id)


# ──────────────────────────────────────────────────────────────────────────
# EffectInstance.with_effect_id
# ──────────────────────────────────────────────────────────────────────────

class TestWithEffectId:
    def test_with_effect_id_returns_new_id(self):
        inst = _make_blur_effect("original")
        new = inst.with_effect_id("replacement")
        assert new.effect_id == "replacement"
        assert inst.effect_id == "original"  # frozen, original unchanged

    def test_with_effect_id_preserves_other_fields(self):
        inst = _make_blur_effect("orig")
        inst = inst.with_param("radius", 15.0)
        new = inst.with_effect_id("new-id")
        assert new.effect_type == inst.effect_type
        assert new.params["radius"] == 15.0
        assert new.is_enabled == inst.is_enabled
        assert new.order == inst.order


# ──────────────────────────────────────────────────────────────────────────
# AddEffectCommand.create(effect_id=...)
# ──────────────────────────────────────────────────────────────────────────

class TestAddEffectCommandPreGeneratedId:
    def test_create_accepts_effect_id(self):
        cmd = AddEffectCommand.create(
            track_id="t1", clip_id="c1", effect_type="blur",
            effect_id="pre-generated",
        )
        assert cmd._effect_id == "pre-generated"

    def test_create_without_effect_id_leaves_none(self):
        cmd = AddEffectCommand.create(
            track_id="t1", clip_id="c1", effect_type="blur",
        )
        # Default is None → apply() auto-generates inside create_instance.
        assert cmd._effect_id is None


# ──────────────────────────────────────────────────────────────────────────
# EditSectionWidget: collapsed + effect_mode
# ──────────────────────────────────────────────────────────────────────────

class TestEditSectionWidgetCollapsed:
    def test_default_is_expanded(self, qapp):
        section = EditSectionWidget("sid", "Title")
        assert section._is_collapsed is False
        # isVisible() reports False until the widget is in a shown window,
        # so we check isHidden() instead (the explicit user-facing flag).
        # isHidden() == False means "not explicitly hidden by the collapse
        # logic", which is the contract we care about.
        assert section._content_widget.isHidden() is False
        assert section._collapse_btn.text() == "▼"

    def test_collapsed_true_hides_content(self, qapp):
        section = EditSectionWidget("sid", "Title", collapsed=True)
        assert section._is_collapsed is True
        # setVisible on a widget without a parent may still report False
        # regardless — the contract we care about is the internal flag and
        # the button label that the user actually sees.
        assert section._collapse_btn.text() == "▶"

    def test_toggle_collapse_flips_state(self, qapp):
        section = EditSectionWidget("sid", "Title", collapsed=True)
        section.toggle_collapse()
        assert section._is_collapsed is False
        assert section._collapse_btn.text() == "▼"


class TestEditSectionWidgetEffectMode:
    def test_effect_mode_adds_enable_and_delete_buttons(self, qapp):
        section = EditSectionWidget("sid", "Blur", effect_mode=True)
        assert section._effect_enable_btn is not None
        assert section._effect_delete_btn is not None

    def test_non_effect_mode_has_no_effect_buttons(self, qapp):
        section = EditSectionWidget("sid", "Transform")
        assert section._effect_enable_btn is None
        assert section._effect_delete_btn is None

    def test_set_effect_enabled_updates_button_without_emitting(self, qapp):
        section = EditSectionWidget("sid", "Blur", effect_mode=True)
        captured = []
        section.effect_enable_toggled.connect(
            lambda sid, en: captured.append((sid, en))
        )
        # Programmatic state change should NOT fire the signal (otherwise
        # rebuilding a panel would emit a flood of spurious commands).
        section.set_effect_enabled(False)
        assert section._effect_enabled is False
        assert section._effect_enable_btn.isChecked() is False
        assert captured == []  # no signal emitted

    def test_enable_button_click_emits_signal(self, qapp):
        section = EditSectionWidget("clip:c1:effect:e1", "Blur", effect_mode=True)
        captured = []
        section.effect_enable_toggled.connect(
            lambda sid, en: captured.append((sid, en))
        )
        # User clicks → button toggles → signal fires with section_id.
        section._effect_enable_btn.setChecked(False)
        section._on_effect_enable_clicked()
        assert captured == [("clip:c1:effect:e1", False)]

    def test_delete_button_click_emits_signal(self, qapp):
        section = EditSectionWidget("clip:c1:effect:e1", "Blur", effect_mode=True)
        captured = []
        section.effect_delete_requested.connect(lambda sid: captured.append(sid))
        section._on_effect_delete_clicked()
        assert captured == ["clip:c1:effect:e1"]


# ──────────────────────────────────────────────────────────────────────────
# EditPropertiesPanel: per-effect section structure
# ──────────────────────────────────────────────────────────────────────────

class TestPerEffectSections:
    def test_clip_without_effects_has_no_effect_sections(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=())
        panel.set_clip(clip)
        effect_sections = [
            sid for sid in panel._sections if ":effect:" in sid
        ]
        assert effect_sections == []

    def test_one_effect_creates_one_effect_section(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)
        effect_sections = [
            sid for sid in panel._sections if ":effect:" in sid
        ]
        assert len(effect_sections) == 1
        assert effect_sections[0] == "clip:test-clip:effect:e1"

    def test_two_effects_create_two_sections(self, qapp):
        panel = EditPropertiesPanel()
        blur1 = _make_blur_effect("e1")
        blur2 = _make_blur_effect("e2")
        clip = _make_mock_clip(effects=(blur1, blur2))
        panel.set_clip(clip)
        effect_sections = sorted(
            sid for sid in panel._sections if ":effect:" in sid
        )
        assert effect_sections == [
            "clip:test-clip:effect:e1",
            "clip:test-clip:effect:e2",
        ]

    def test_adjustment_clip_keeps_transform_and_opacity(self, qapp):
        """Adjustment geometry/compositing controls act on lower layers."""
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))

        panel.set_clip(clip, media_type="adjustment")

        assert "clip:test-clip:effect:e1" in panel._sections
        assert "clip:test-clip:transform" in panel._sections
        assert "clip:test-clip:crop" not in panel._sections
        assert "clip:test-clip:speed" not in panel._sections
        assert "clip:test-clip:opacity" in panel._sections

    def test_effect_section_is_in_effect_mode(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)
        section = panel._sections["clip:test-clip:effect:e1"]
        assert section._effect_mode is True
        # enable + delete buttons should be present
        assert section._effect_enable_btn is not None
        assert section._effect_delete_btn is not None

    def test_effect_section_title_uses_display_name(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)
        section = panel._sections["clip:test-clip:effect:e1"]
        # Display name comes from EffectDefinition.name, which for the
        # catalog blur spec is "Blur".
        assert section.title == "Blur"


class TestNativeSectionsDefaultExpanded:
    def test_transform_expanded_without_effects(self, qapp):
        """Native sections are immediately editable for an ordinary clip."""
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=())
        panel.set_clip(clip)
        transform = panel._sections.get("clip:test-clip:transform")
        assert transform is not None
        assert transform._is_collapsed is False

    def test_native_sections_expanded_with_effects(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)
        for name in ("transform", "crop", "opacity", "speed"):
            sid = f"clip:test-clip:{name}"
            section = panel._sections.get(sid)
            assert section is not None, f"missing section {name}"
            assert section._is_collapsed is False, (
                f"{name} should be expanded when clip has effects"
            )


class TestNativeSectionReset:
    def test_crop_reset_emits_defaults_and_updates_widgets(self, qapp):
        """N13/T3-05：Crop Reset 一次动作一个原子批（此前逐参数 5 次发
        射 5 个宏，模型只剩最后一项默认值）。批载荷为整组默认
        CropParams，控件显示同步归位。"""
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=())
        clip.crop = CropParams(
            left=0.2, right=0.15, top=0.1, bottom=0.05, feather=12.0,
        )
        panel.set_clip(clip)

        batches = []
        panel.clips_param_batch_changed.connect(
            lambda batch: batches.append(batch)
        )
        singles = []
        panel.clip_param_changed.connect(
            lambda *args: singles.append(args)
        )
        panel._on_section_reset("clip:test-clip:crop")

        assert len(batches) == 1, "reset must be one atomic batch"
        assert not singles, "reset must not replay per-param signals"
        batch = batches[0]
        assert len(batch) == 1
        clip_id, group, _path, value = batch[0]
        assert (clip_id, group) == ("test-clip", "crop")
        assert value == CropParams(), "all fields must reset to defaults"

        section = panel._sections["clip:test-clip:crop"]
        assert all(
            info["widget"].value() == 0.0
            for info in section._param_widgets.values()
        )


# ──────────────────────────────────────────────────────────────────────────
# Effect section signals → panel signals
# ──────────────────────────────────────────────────────────────────────────

class TestEffectSectionSignalRouting:
    def test_enable_toggled_routes_to_panel_signal(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)

        captured = []
        panel.effect_enabled_changed.connect(
            lambda clip_id, effect_id, enabled: captured.append(
                (clip_id, effect_id, enabled)
            )
        )

        section = panel._sections["clip:test-clip:effect:e1"]
        section._effect_enable_btn.setChecked(False)
        section._on_effect_enable_clicked()

        assert captured == [("test-clip", "e1", False)]

    def test_delete_routes_to_panel_signal(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)

        captured = []
        panel.effect_remove_requested.connect(
            lambda clip_id, effect_id: captured.append((clip_id, effect_id))
        )

        section = panel._sections["clip:test-clip:effect:e1"]
        section._on_effect_delete_clicked()

        assert captured == [("test-clip", "e1")]


# ──────────────────────────────────────────────────────────────────────────
# scroll_to_effect
# ──────────────────────────────────────────────────────────────────────────

class TestScrollToEffect:
    def test_scroll_to_existing_effect_expands_it(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(
            _make_blur_effect("e1"),
            _make_blur_effect("e2"),
        ))
        panel.set_clip(clip)
        # Both start collapsed (no _selected_effect_id).
        s1 = panel._sections["clip:test-clip:effect:e1"]
        s2 = panel._sections["clip:test-clip:effect:e2"]
        assert s1._is_collapsed is True
        assert s2._is_collapsed is True

        panel.scroll_to_effect("e2")
        # Target expands; the other stays collapsed.
        assert s1._is_collapsed is True
        assert s2._is_collapsed is False
        # Selection state updated so a subsequent set_clip keeps e2 expanded.
        assert panel._selected_effect_id == "e2"

    def test_scroll_to_unknown_effect_is_noop(self, qapp):
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=(_make_blur_effect("e1"),))
        panel.set_clip(clip)
        # Must not raise on unknown id (e.g. race with command stack).
        panel.scroll_to_effect("does-not-exist")


# ──────────────────────────────────────────────────────────────────────────
# Phase 3 / round 2: per-effect keyframe (◆) button
# ──────────────────────────────────────────────────────────────────────────

class TestEffectKeyframeButton:
    """The ◆ button on animatable effect params."""

    def test_animatable_float_param_gets_keyframe_button(self, qapp):
        """A float ParamDef (e.g. blur.radius) gets a ◆ button registered
        under the composite key 'effect:<eid>:<param>'."""
        panel = EditPropertiesPanel()
        effect = _make_blur_effect("e1")
        clip = _make_mock_clip(effects=(effect,))
        panel.set_clip(clip)

        # blur has several float params; pick 'radius' as representative.
        key = f"effect:e1:radius"
        assert key in panel._keyframe_buttons


    def test_non_animatable_param_no_keyframe_button(self, qapp):
        """Non-numeric params (option/bool) don't get a ◆ button."""
        panel = EditPropertiesPanel()
        effect = _make_blur_effect("e1")
        clip = _make_mock_clip(effects=(effect,))
        panel.set_clip(clip)

        # 'blur_type' is an option param — not animatable.
        assert "effect:e1:blur_type" not in panel._keyframe_buttons

    def test_button_emits_effect_keyframe_toggled(self, qapp):
        """Clicking the ◆ button emits effect_keyframe_toggled with the
        clip_id, effect_id, param_name, frame, value payload."""
        panel = EditPropertiesPanel()
        effect = _make_blur_effect("e1")
        # Plant a param value so the button captures it.
        effect = effect.with_param("radius", 7.0)
        clip = _make_mock_clip(effects=(effect,))
        panel.set_clip(clip)
        panel.set_current_frame(12)

        received: list[tuple] = []
        panel.effect_keyframe_toggled.connect(
            lambda *a: received.append(a)
        )

        btn = panel._keyframe_buttons["effect:e1:radius"]
        btn.click()

        assert len(received) == 1
        clip_id, eid, pname, frame, value = received[0]
        assert clip_id == "test-clip"
        assert eid == "e1"
        assert pname == "radius"
        assert frame == 12

    def test_update_keyframe_buttons_routes_effect_query(self, qapp):
        """_update_keyframe_buttons queries the EffectInstance (not the
        clip) for keys with the 'effect:' prefix."""
        from src.shared.keyframe import InterpolationType
        panel = EditPropertiesPanel()
        effect = _make_blur_effect("e1")
        # Add a real keyframe so is_animated / has_keyframe_at are True.
        effect = effect.with_keyframe_added(
            "radius", frame=5, value=4.0,
            interpolation=InterpolationType.LINEAR,
            min_value=0.0, max_value=100.0,
        )
        clip = _make_mock_clip(effects=(effect,))
        # Make the mock clip's get_effect_by_id return our real effect so
        # the panel can query it.
        clip.get_effect_by_id = lambda eid: effect if eid == "e1" else None
        panel.set_clip(clip)
        panel.set_current_frame(5)

        btn = panel._keyframe_buttons["effect:e1:radius"]
        # After set_current_frame → _update_keyframe_buttons runs.
        # At frame 5 the effect has a keyframe, so the button should be
        # in the "has keyframe at frame" state.
        assert btn._has_keyframe_at_frame is True
        assert btn._is_animated is True


class TestBlurChannelRow:
    def test_blur_channels_are_compact_rgb_buttons_without_alpha(self, qapp):
        panel = EditPropertiesPanel()
        effect = _make_blur_effect("e1")
        clip = _make_mock_clip(effects=(effect,))
        panel.set_clip(clip)

        section = panel._sections["clip:test-clip:effect:e1"]
        buttons = {
            button.text(): button
            for button in section.findChildren(QPushButton)
            if button.text() in {"R", "G", "B", "A"}
        }
        assert set(buttons) == {"R", "G", "B"}
        assert all(buttons[channel].isChecked() for channel in ("R", "G", "B"))


# ──────────────────────────────────────────────────────────────────────────
# Review fix: update_current_clip — swap a fresher clip without rebuilding
# ──────────────────────────────────────────────────────────────────────────

class TestUpdateCurrentClip:
    """update_current_clip lets the controller sync a mutated clip into
    the panel after a command (e.g. AddKeyframeCommand) so the ◆ button
    refreshes without a full set_clip rebuild."""

    def test_replaces_existing_clip_reference(self, qapp):
        """The panel's _current_clips should hold the new clip object
        after update_current_clip (same clip_id, new identity)."""
        panel = EditPropertiesPanel()
        clip = _make_mock_clip(effects=())
        clip.clip_id = "test-clip"
        panel.set_clip(clip)
        assert panel._current_clips[0] is clip

        # Simulate a command producing a new clip object with same id.
        new_clip = _make_mock_clip(effects=())
        new_clip.clip_id = "test-clip"
        panel.update_current_clip(new_clip)
        assert panel._current_clips[0] is new_clip

    def test_unknown_clip_id_prepended(self, qapp):
        """If the clip_id isn't in _current_clips, it's inserted rather
        than dropped (defensive — shouldn't normally happen)."""
        panel = EditPropertiesPanel()
        panel.set_clip(_make_mock_clip(effects=()))  # clip_id="test-clip"
        other = _make_mock_clip(effects=())
        other.clip_id = "other-clip"
        panel.update_current_clip(other)
        assert any(getattr(c, "clip_id", None) == "other-clip"
                   for c in panel._current_clips)

    def test_refreshes_keyframe_buttons(self, qapp):
        """After update_current_clip, the ◆ button state is recomputed
        against the new clip — so a freshly-added keyframe shows up
        without a full panel rebuild."""
        from src.shared.keyframe import InterpolationType
        panel = EditPropertiesPanel()
        effect = _make_blur_effect("e1")
        clip = _make_mock_clip(effects=(effect,))
        # Wire get_effect_by_id to return the real effect so the panel's
        # _update_keyframe_buttons queries real keyframe state (otherwise
        # MagicMock auto-returns a truthy mock that breaks assertions).
        clip.get_effect_by_id = lambda eid: effect if eid == "e1" else None
        panel.set_clip(clip)
        panel.set_current_frame(5)
        btn = panel._keyframe_buttons["effect:e1:radius"]
        assert btn._has_keyframe_at_frame is False  # no keyframe yet

        # Simulate the command adding a keyframe: produce a new effect
        # with a keyframe, wrap it in a new clip mock.
        new_effect = effect.with_keyframe_added(
            "radius", frame=5, value=4.0,
            interpolation=InterpolationType.LINEAR,
            min_value=0.0, max_value=100.0,
        )
        new_clip = _make_mock_clip(effects=(new_effect,))
        new_clip.get_effect_by_id = lambda eid: new_effect if eid == "e1" else None
        panel.update_current_clip(new_clip)

        # Button should now reflect the keyframe without a set_clip call.
        assert btn._has_keyframe_at_frame is True
