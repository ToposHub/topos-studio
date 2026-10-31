"""Regression tests for REMEDIATION_PLAN batch-D group wd4-g28.

Covers 16 Major entries in the composite timeline-shortcut / viewer tool
domain:

- AUD-11106  timeline_shortcuts: handler exceptions logged, not silent
- AUD-11110  timeline_time_display: invalid Enter keeps edit mode even
             though Qt fires editingFinished right after returnPressed
- AUD-11111  timeline_time_display: invalid-state QSS uses the dynamic
             property selector, not objectName "#invalid"
- AUD-11115  timeline_view_model: select_keyframe maintains the
             "selected keyframe ⇒ its row selected" invariant
- AUD-11200  viewer3d_settings_section: combo sync falls back instead of
             showing stale items (hdri / msaa / quad_view_quality)
- AUD-11201  viewer3d_settings_section: merged settings validate/coerce
             external (project-file) values
- AUD-11204  viewer_handle_drawing: corner handles drawn as squares, not
             thin edge bars
- AUD-11300  viewer_handles: edge/corner mask scale anchors the opposite
             edge/corner (linear center compensation)
- AUD-11301  viewer_handles: feather handle normals rotate with the same
             matrix as the shape corners
- AUD-11302  viewer_handles: mask rotate drag is relative to the drag
             start (no snap-back to ~0°)
- AUD-11303  viewer_handles: mask rotate direction matches the transform
             convention (clockwise mouse → shape follows)
- AUD-11304  viewer_handles: feather T/L outward drag increases feather
- AUD-11305  viewer_handles: roto opacity semantics (down = decrease)
             documented consistently with the locked behavior
- AUD-11306  viewer_handles: mask/roto delta functions accept Qt modifier
             enums without TypeError; Shift uniform works
- AUD-11400  viewer_tool_system: reset()/start_drag() clear preview_params
- AUD-11401  viewer_tool_system: compute_constrained_delta uses the
             current "aspect locked by default, Shift frees" convention
"""

from __future__ import annotations

import logging
import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt

from src.features.composite.ui import (
    timeline_shortcuts as ts,
    timeline_time_display as ttd,
    timeline_view_model as tvm,
    viewer_handle_drawing as vhd,
    viewer_tool_system as vts,
)
from src.features.composite.ui.viewer3d_settings_section import (
    DEFAULT_VIEWER3D_SCENE_SETTINGS,
    Viewer3DSettingsWidget,
    merged_viewer3d_scene_settings,
)
from src.features.composite.ui.viewer_handles import (
    MaskHandleType,
    RotoHandleType,
    _normalize_modifiers,
    compute_mask_delta_params,
    compute_mask_handles,
    compute_roto_delta_params,
)

pytestmark = pytest.mark.composite_core


# ── AUD-11106: dispatcher must not swallow handler errors silently ──────────


class TestAud11106:
    def test_handler_exception_is_logged_and_dispatch_survives(self, caplog, qapp):
        # 前序套件可能留下持有焦点的 QLineEdit，触发 handle_key 的
        # "文本输入聚焦时抑制字符快捷键"规则——本用例只测异常隔离，
        # 与焦点无关，先清焦点保证顺序无关。
        focus = qapp.focusWidget()
        if focus is not None:
            focus.clearFocus()
        d = ts.ShortcutDispatcher()
        boom = [0]

        def bad_handler() -> None:
            boom[0] += 1
            raise RuntimeError("domain error in handler")

        def good_handler() -> None:
            boom[0] += 100

        d.bind(ts.CMD_NAV_NEXT_KEYFRAME, bad_handler)
        d.bind(ts.CMD_NAV_PREV_KEYFRAME, good_handler)
        with caplog.at_level(logging.ERROR, logger="src.features.composite.ui.timeline_shortcuts"):
            fired_bad = d.handle_key("K")
            fired_good = d.handle_key("J")
        # The dispatcher still resolves/fires both keys.
        assert fired_bad == ts.CMD_NAV_NEXT_KEYFRAME
        assert fired_good == ts.CMD_NAV_PREV_KEYFRAME
        assert boom[0] == 101
        # The failure is visible in the log: command name in the message,
        # traceback attached via exc_info.
        matching = [r for r in caplog.records
                    if "nav.next_keyframe" in r.message
                    and r.levelno == logging.ERROR]
        assert matching, [r.message for r in caplog.records]
        record = matching[0]
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError


# ── AUD-11110 / AUD-11111: invalid Enter keeps edit mode + real red border ──


class TestAud11110:
    def test_invalid_commit_survives_trailing_editing_finished(self, qapp):
        """Enter on garbage: returnPressed → editingFinished must NOT exit."""
        w = ttd.TimelineTimeDisplay(fps=24.0)
        received: list[int] = []
        w.frame_committed.connect(received.append)
        w.set_current_frame(10)
        w._enter_edit()
        w._edit.setText("garbage")
        # Real keyboard path: commit first, then the trailing cancel.
        w._on_edit_commit()
        w._on_edit_cancel()
        assert received == []
        assert w._editing is True, "invalid input must stay in edit mode"
        assert w.current_frame == 10
        assert w._edit.property("invalid") is True

    def test_valid_commit_exit_survives_trailing_editing_finished(self, qapp):
        w = ttd.TimelineTimeDisplay(fps=24.0)
        received: list[int] = []
        w.frame_committed.connect(received.append)
        w.set_current_frame(10)
        w._enter_edit()
        w._edit.setText("49")
        w._on_edit_commit()
        # Trailing editingFinished (from the hide() focus-out) is a no-op.
        w._on_edit_cancel()
        assert received == [49]
        assert w._editing is False
        assert w.current_frame == 49

    def test_real_focus_loss_still_cancels(self, qapp):
        """An editingFinished without a preceding commit (focus loss) cancels."""
        w = ttd.TimelineTimeDisplay(fps=24.0)
        received: list[int] = []
        w.frame_committed.connect(received.append)
        w.set_current_frame(10)
        w._enter_edit()
        w._edit.setText("49")
        w._on_edit_cancel()
        assert received == []
        assert w._editing is False
        assert w.current_frame == 10


class TestAud11111:
    def test_stylesheet_uses_property_selector(self, qapp):
        w = ttd.TimelineTimeDisplay(fps=24.0)
        sheet = w._stylesheet()
        assert 'QLineEdit[invalid="true"]' in sheet
        assert "QLineEdit#invalid" not in sheet

    def test_widget_level_stylesheet_not_cleared_on_invalid(self, qapp):
        """The old setStyleSheet("") hack is gone; polish re-evals only."""
        w = ttd.TimelineTimeDisplay(fps=24.0)
        w.set_current_frame(10)
        w._enter_edit()
        w._edit.setText("garbage")
        w._on_edit_commit()
        assert w._edit.styleSheet() == ""  # never touched by the commit path


# ── AUD-11115: select_keyframe keeps the sync_from_model invariant ──────────


class TestAud11115:
    def test_replace_clears_stale_rows_and_selects_new_row(self):
        sm = tvm.AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 1)
        sm.select_row("node:other", additive=True)  # stale non-primary row
        sm.select_keyframe("param:n:q", 5)          # replace
        assert sm.selection.keyframes == {("param:n:q", 5)}
        # Old rows gone, new keyframe's row selected — matches sync_from_model.
        assert set(sm.selected_rows()) == {"param:n:q"}

    def test_additive_selects_new_row(self):
        sm = tvm.AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 1)
        sm.select_keyframe("param:n:q", 5, additive=True)
        assert set(sm.selected_rows()) == {"param:n:p", "param:n:q"}

    def test_round_trip_with_apply_to_model(self):
        """apply_to_model must highlight exactly the rows implied by the keys."""
        sm = tvm.AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 1)
        sm.select_keyframe("param:n:q", 5)  # replace

        class _Row:
            def __init__(self, row_id: str) -> None:
                self.row_id = row_id
                self.selected = False

        class _KF:
            def __init__(self, frame: int) -> None:
                self.frame = frame
                self.selected = False

        class _Track:
            def __init__(self, frames: list[int]) -> None:
                self.keyframes = [_KF(f) for f in frames]

        rows = [_Row("param:n:p"), _Row("param:n:q")]
        tracks = {"param:n:p": _Track([1]), "param:n:q": _Track([5])}

        class _Model:
            def __init__(self, rows, tracks) -> None:
                self.rows = rows
                self._tracks = tracks

            def track_for(self, row_id: str):
                return self._tracks.get(row_id)

        model = _Model(rows, tracks)
        sm.apply_to_model(model)
        assert rows[0].selected is False   # old primary row no longer highlighted
        assert rows[1].selected is True    # new keyframe's row highlighted
        assert tracks["param:n:q"].keyframes[0].selected is True
        assert tracks["param:n:p"].keyframes[0].selected is False


# ── AUD-11201: external settings are validated/coerced ───────────────────────


class TestAud11201:
    def test_malformed_values_fall_back_to_defaults(self):
        merged = merged_viewer3d_scene_settings({
            "cam_fov": None,
            "msaa": "4x",
            "hdri_preset": 123,           # non-str
            "use_ibl": "yes",             # non-bool
            "quad_view_quality": None,
            "nav_restore_delay_ms": float("nan"),
            "env_intensity": float("inf"),
        })
        assert merged["cam_fov"] == DEFAULT_VIEWER3D_SCENE_SETTINGS["cam_fov"]
        assert merged["msaa"] == DEFAULT_VIEWER3D_SCENE_SETTINGS["msaa"]
        assert merged["hdri_preset"] == ""  # default is ""
        assert merged["use_ibl"] is True
        assert merged["quad_view_quality"] == "all_textured"
        assert merged["nav_restore_delay_ms"] == 150
        assert merged["env_intensity"] == 1.0

    def test_coercible_values_pass_through(self):
        merged = merged_viewer3d_scene_settings({
            "cam_fov": "75.5",       # numeric string
            "msaa": 8,
            "grid_opacity": 1,       # int into float slot
            "hdri_preset": "/hdris/warehouse.hdr",
            "bg_opaque": False,
        })
        assert merged["cam_fov"] == 75.5
        assert merged["msaa"] == 8
        assert merged["grid_opacity"] == 1.0
        assert merged["hdri_preset"] == "/hdris/warehouse.hdr"
        assert merged["bg_opaque"] is False

    def test_unknown_keys_dropped(self):
        merged = merged_viewer3d_scene_settings({"evil_key": object()})
        assert "evil_key" not in merged
        assert set(merged) == set(DEFAULT_VIEWER3D_SCENE_SETTINGS)

    def test_load_from_graph_survives_corrupt_project(self, qapp):
        """load_from_graph → _sync_widgets must not raise on garbage."""
        w = Viewer3DSettingsWidget()

        class _Graph:
            _viewer_state = {"default_3d_scene": {
                "cam_fov": None, "msaa": "4x", "hdri_preset": 17,
                "env_intensity": "abc", "use_ibl": 3,
            }}

        w.load_from_graph(_Graph())  # must not raise
        assert w._settings["cam_fov"] == DEFAULT_VIEWER3D_SCENE_SETTINGS["cam_fov"]
        assert w._settings["hdri_preset"] == ""


# ── AUD-11200: combo sync shows the real value / falls back ─────────────────


class TestAud11200:
    def test_custom_hdri_path_gets_dynamic_row(self, qapp):
        w = Viewer3DSettingsWidget()
        custom = "/Volumes/hdris/warehouse_4k.hdr"

        class _Graph:
            _viewer_state = {"default_3d_scene": {"hdri_preset": custom}}

        w.load_from_graph(_Graph())
        combo = w._hdri_preset
        idx = combo.currentIndex()
        assert idx == combo.count() - 1           # dynamic row selected
        assert str(combo.itemData(idx)) == custom
        assert combo.currentText() == custom      # user sees the real value

    def test_custom_hdri_row_not_duplicated_across_syncs(self, qapp):
        w = Viewer3DSettingsWidget()
        custom = "/hdris/a.hdr"

        class _Graph:
            _viewer_state = {"default_3d_scene": {"hdri_preset": custom}}

        base = w._hdri_preset.count()
        w.load_from_graph(_Graph())
        w.load_from_graph(_Graph())               # second sync
        assert w._hdri_preset.count() == base + 1

    def test_builtin_hdri_still_matches_and_dynamic_row_removed(self, qapp):
        w = Viewer3DSettingsWidget()

        class _GraphCustom:
            _viewer_state = {"default_3d_scene": {"hdri_preset": "/hdris/a.hdr"}}

        class _GraphBuiltin:
            _viewer_state = {"default_3d_scene": {"hdri_preset": "__sky__"}}

        base = w._hdri_preset.count()
        w.load_from_graph(_GraphCustom())
        w.load_from_graph(_GraphBuiltin())
        assert w._hdri_preset.count() == base     # dynamic row removed again
        assert str(w._hdri_preset.currentData()) == "__sky__"

    def test_msaa_out_of_set_falls_back_to_default(self, qapp):
        w = Viewer3DSettingsWidget()

        class _Graph:
            _viewer_state = {"default_3d_scene": {"msaa": 16}}

        w.load_from_graph(_Graph())
        assert int(w._msaa.currentData()) == \
            int(DEFAULT_VIEWER3D_SCENE_SETTINGS["msaa"])

    def test_quad_view_quality_invalid_falls_back(self, qapp):
        w = Viewer3DSettingsWidget()

        class _Graph:
            _viewer_state = {"default_3d_scene": {"quad_view_quality": "turbo"}}

        w.load_from_graph(_Graph())
        assert str(w._quad_view_quality.currentData()) == "all_textured"


# ── AUD-11204: corner handles are drawn as squares ──────────────────────────


class TestAud11204:
    def _dispatch_calls(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        calls = {"square": 0, "edge": 0, "cross": 0}

        def fake_square(*_a, **_k):
            calls["square"] += 1

        def fake_edge(_gl, _pos, handle_id, *_a, **_k):
            calls["edge"] += 1
            # Mirror the real bar-orientation logic to prove corners can no
            # longer land here.
            assert not handle_id.endswith(
                ("_top_left", "_top_right", "_bottom_left", "_bottom_right"))

        def fake_cross(*_a, **_k):
            calls["cross"] += 1

        monkeypatch.setattr(vhd, "_draw_handle_square", fake_square)
        monkeypatch.setattr(vhd, "_draw_handle_edge", fake_edge)
        monkeypatch.setattr(vhd, "_draw_handle_cross", fake_cross)
        return calls

    def _fake_gl(self) -> object:
        class _GL:  # records nothing; draw helpers are monkeypatched out
            def __getattr__(self, name: str):
                return lambda *a, **k: None
        return _GL()

    def test_all_crop_handles_dispatched_correctly(self, monkeypatch):
        calls = self._dispatch_calls(monkeypatch)
        params = {"x": 10, "y": 10, "width": 512, "height": 512}
        handles = vhd.draw_crop_reformat_handles(
            self._fake_gl(), 1, 2, 3, "crop", params,
            1000.0, 1000.0, (-1.0, -1.0, 1.0, 1.0),
            800.0, 600.0, 1.0, None, None,
        )
        assert set(handles) == {
            "crop_top_left", "crop_top_right", "crop_bottom_left",
            "crop_bottom_right", "crop_top", "crop_bottom", "crop_left",
            "crop_right", "crop_center",
        }
        # 4 corners + 0 → square; 4 edges → edge; 1 center → cross.
        assert calls["square"] == 4
        assert calls["edge"] == 4
        assert calls["cross"] == 1


# ── AUD-11300: opposite-edge/corner anchoring is linear ──────────────────────


def _mask_params(**over) -> dict:
    base = {"center_x": 0.5, "center_y": 0.5, "width": 1.0, "height": 1.0,
            "rotation": 0.0, "feather": 0.0}
    base.update(over)
    return base


class TestAud11300:
    def test_width_l_keeps_right_edge_fixed(self):
        # audit scenario: image_width=1000, orig_w=1.0, drag left by 100px
        out = compute_mask_delta_params(
            "mask_width_l", (400.0, 500.0), (300.0, 500.0),
            _mask_params(), 1000, 1000)
        new_w = out["width"]
        right = out["center_x"] + new_w / 2.0
        assert right == pytest.approx(1.0, abs=1e-9)  # right edge stays at 1.0
        assert out["center_x"] == pytest.approx(0.4, abs=1e-9)

    def test_width_r_keeps_left_edge_fixed(self):
        out = compute_mask_delta_params(
            "mask_width_r", (600.0, 500.0), (700.0, 500.0),
            _mask_params(), 1000, 1000)
        left = out["center_x"] - out["width"] / 2.0
        assert left == pytest.approx(0.0, abs=1e-9)

    def test_height_t_keeps_bottom_edge_fixed(self):
        out = compute_mask_delta_params(
            "mask_height_t", (500.0, 400.0), (500.0, 300.0),
            _mask_params(), 1000, 1000)
        bottom = out["center_y"] + out["height"] / 2.0
        assert bottom == pytest.approx(1.0, abs=1e-9)

    def test_height_b_keeps_top_edge_fixed(self):
        out = compute_mask_delta_params(
            "mask_height_b", (500.0, 600.0), (500.0, 700.0),
            _mask_params(), 1000, 1000)
        top = out["center_y"] - out["height"] / 2.0
        assert top == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("handle", [
        "mask_corner_tl", "mask_corner_tr", "mask_corner_bl", "mask_corner_br"])
    def test_corners_anchor_opposite_corner(self, handle: str):
        # Drag each corner outward; the opposite corner must not move.
        # anchor = the OPPOSITE corner in image-normalized coords (x, y),
        # Y-down (top = 0): tl↔br=(1,1), tr↔bl=(0,1), bl↔tr=(1,0), br↔tl=(0,0).
        drags = {
            "mask_corner_tl": ((400.0, 400.0), (300.0, 300.0), (1.0, 1.0)),
            "mask_corner_tr": ((600.0, 400.0), (700.0, 300.0), (0.0, 1.0)),
            "mask_corner_bl": ((400.0, 600.0), (300.0, 700.0), (1.0, 0.0)),
            "mask_corner_br": ((600.0, 600.0), (700.0, 700.0), (0.0, 0.0)),
        }
        start, cur, anchor = drags[handle]
        out = compute_mask_delta_params(
            handle, start, cur, _mask_params(), 1000, 1000)
        ax = out["center_x"] + (anchor[0] - 0.5) * out["width"]
        ay = out["center_y"] + (anchor[1] - 0.5) * out["height"]
        assert ax == pytest.approx(anchor[0], abs=1e-9)
        assert ay == pytest.approx(anchor[1], abs=1e-9)


# ── AUD-11301: feather normals rotate with the shape ────────────────────────


class TestAud11301:
    def test_feather_handles_follow_rotation(self):
        data = compute_mask_handles(
            mask_type="rectangle", center_x=0.5, center_y=0.5,
            width_norm=0.5, height_norm=0.5, rotation=90.0, feather=0.1,
            image_width=1000, image_height=1000,
            image_ndc_rect=(-1.0, -1.0, 1.0, 1.0))
        # θ=90° CCW: the top edge center moves to the LEFT side of the shape
        # (corners use R(+θ)); the top feather handle must sit outside it,
        # i.e. further left than the top edge midpoint (NDC, Y-up).
        top_edge_x = (data.corner_tl[0] + data.corner_tr[0]) / 2.0
        assert data.feather_t[0] < top_edge_x
        # Before the fix the normal used R(−θ), placing the handle on the
        # mirrored (right) side — inside/behind the shape.
        assert data.feather_t[0] < data.center[0]

    def test_unrotated_normals_unchanged(self):
        data0 = compute_mask_handles(
            mask_type="rectangle", center_x=0.5, center_y=0.5,
            width_norm=0.5, height_norm=0.5, rotation=0.0, feather=0.1,
            image_width=1000, image_height=1000,
            image_ndc_rect=(-1.0, -1.0, 1.0, 1.0))
        assert data0.feather_t[1] > data0.center[1]  # above
        assert data0.feather_b[1] < data0.center[1]  # below
        assert data0.feather_l[0] < data0.center[0]  # left
        assert data0.feather_r[0] > data0.center[0]  # right


# ── AUD-11302 / AUD-11303: relative rotation, follows the mouse ──────────────


class TestAud11302:
    def test_no_jump_back_for_rotated_shape(self):
        # A 90°-rotated mask: the rotate handle sits above the bbox. Press it
        # and jiggle — rotation must stay ~90°, not snap to ~0°.
        params = _mask_params(rotation=90.0)
        center = (500.0, 500.0)
        out = compute_mask_delta_params(
            "mask_rotate", center, center, params, 1000, 1000)
        assert out["rotation"] == pytest.approx(90.0)
        # Tiny move from the press point keeps the value near 90°.
        out2 = compute_mask_delta_params(
            "mask_rotate", (500.0, 300.0), (505.0, 295.0), params, 1000, 1000)
        assert abs(out2["rotation"] - 90.0) < 5.0

    def test_rotation_is_relative_to_drag_start(self):
        # Start above the center, drag 90° clockwise (to the right); the
        # value changes by exactly -90° (mask convention: CW mouse → CCW
        # positive store), magnitude 90 from any baseline.
        params = _mask_params(rotation=10.0)
        out = compute_mask_delta_params(
            "mask_rotate", (500.0, 200.0), (800.0, 500.0), params, 1000, 1000)
        assert out["rotation"] == pytest.approx(10.0 - 90.0)
        # Sanity: clockwise-from-up angle of start is 0°, of end is 90°.

    def test_clockwise_drag_decreases_stored_rotation(self):
        """AUD-11303: mask handles are CCW-positive; a clockwise mouse delta
        must decrease the stored value so the shape follows the cursor."""
        params = _mask_params(rotation=0.0)
        out = compute_mask_delta_params(
            "mask_rotate", (500.0, 200.0), (650.0, 350.0), params, 1000, 1000)
        assert out["rotation"] < 0.0


# ── AUD-11304: feather T/L outward drag increases feather ────────────────────


class TestAud11304:
    @pytest.mark.parametrize("handle,start,cur", [
        ("mask_feather_t", (500.0, 400.0), (500.0, 300.0)),  # drag up = outward
        ("mask_feather_b", (500.0, 600.0), (500.0, 700.0)),  # drag down = outward
        ("mask_feather_l", (400.0, 500.0), (300.0, 500.0)),  # drag left = outward
        ("mask_feather_r", (600.0, 500.0), (700.0, 500.0)),  # drag right = outward
    ])
    def test_outward_drag_increases_feather(self, handle, start, cur):
        out = compute_mask_delta_params(
            handle, start, cur, _mask_params(feather=0.1), 1000, 1000)
        assert out["feather"] > 0.1

    @pytest.mark.parametrize("handle,start,cur", [
        ("mask_feather_t", (500.0, 400.0), (500.0, 500.0)),
        ("mask_feather_l", (400.0, 500.0), (500.0, 500.0)),
    ])
    def test_inward_drag_decreases_feather(self, handle, start, cur):
        out = compute_mask_delta_params(
            handle, start, cur, _mask_params(feather=0.2), 1000, 1000)
        assert out["feather"] < 0.2


# ── AUD-11305: roto opacity semantics documented + locked ────────────────────


class TestAud11305:
    def test_down_decreases_up_increases(self):
        params = {"opacity": 0.5, "center_x": 0.5, "center_y": 0.5,
                  "width": 0.5, "height": 0.5, "rotation": 0.0, "feather": 0.0}
        down = compute_roto_delta_params(
            "roto_opacity", (500.0, 400.0), (500.0, 500.0), params, 1000, 1000)
        up = compute_roto_delta_params(
            "roto_opacity", (500.0, 500.0), (500.0, 400.0), params, 1000, 1000)
        assert down["opacity"] < 0.5
        assert up["opacity"] > 0.5


# ── AUD-11306: modifier normalization for mask/roto delta params ─────────────


class _FakeQtShiftModifier:
    """Stand-in for Qt.KeyboardModifier.ShiftModifier (enum, rejects int&)."""
    value = 0x2000000


class TestAud11306:
    def test_normalize_helper(self):
        assert _normalize_modifiers(0) == 0
        assert _normalize_modifiers(0x2000000) == 0x2000000
        assert _normalize_modifiers(_FakeQtShiftModifier()) == 0x2000000
        assert _normalize_modifiers(object()) == 0

    def test_real_qt_enum_accepted(self):
        # Exact production failure mode: Qt flag object & int → TypeError.
        shift = Qt.KeyboardModifier.ShiftModifier
        with pytest.raises(TypeError):
            _ = shift & 0x2000000  # noqa: B016  (the bug being guarded against)
        assert _normalize_modifiers(shift) == 0x2000000

    def test_mask_corner_accepts_qt_enum_and_plain_drag_is_uniform(self):
        shift = Qt.KeyboardModifier.ShiftModifier
        # AUD-11310（ef-06 批）：与 Transform 工具契约统一——普通角点拖拽等比
        # 缩放，Shift 释放轴向做独立 x/y 缩放（旧 mask 约定相反，见 ef-06
        # 报告披露）。本测试保留 AUD-11306 的本意：Qt 枚举可安全传入。
        plain = compute_mask_delta_params(
            "mask_corner_br", (600.0, 600.0), (800.0, 600.0),
            _mask_params(), 1000, 1000, modifiers=0)
        assert plain["width"] == pytest.approx(plain["height"])  # plain = uniform

        shifted = compute_mask_delta_params(
            "mask_corner_br", (600.0, 600.0), (800.0, 600.0),
            _mask_params(), 1000, 1000, modifiers=shift)
        assert shifted["width"] > shifted["height"]  # shift frees axes

    def test_mask_corner_qt_enum_still_accepted(self):
        # Qt 枚举不再触发 TypeError（AUD-11306 归一化守卫继续有效）
        out = compute_mask_delta_params(
            "mask_corner_br", (600.0, 600.0), (800.0, 600.0),
            _mask_params(), 1000, 1000,
            modifiers=Qt.KeyboardModifier.ShiftModifier)
        assert {"width", "height"} <= set(out)

    def test_roto_delegation_accepts_qt_enum(self):
        # AUD-11310 后普通角点拖拽即等比；roto 委托路径同样适用
        out = compute_roto_delta_params(
            "roto_corner_br", (600.0, 600.0), (800.0, 600.0),
            {"center_x": 0.5, "center_y": 0.5, "width": 0.5, "height": 0.5,
             "rotation": 0.0, "feather": 0.0, "opacity": 1.0},
            1000, 1000, modifiers=0)
        assert out["width"] == pytest.approx(out["height"])


# ── AUD-11400: preview_params cleared on reset/start_drag ────────────────────


class TestAud11400:
    def test_start_drag_clears_stale_preview(self):
        st = vts.ViewerInteractionState()
        st.preview_params = {"delta_x": 99.0, "delta_y": -5.0}
        st.start_drag(
            tool=vts.ViewerToolMode.TRANSFORM, node_id="n", handle="move",
            view_pos=QPointF(0, 0), ndc_pos=QPointF(0, 0),
            image_pos=QPointF(10, 10), format_pos=QPointF(0, 0),
            original_params={"translate_x": 1.0}, modifiers=0)
        assert st.preview_params == {}
        assert st.is_dragging is True

    def test_reset_clears_preview(self):
        st = vts.ViewerInteractionState()
        st.preview_params = {"delta_x": 42.0}
        st.reset()
        assert st.preview_params == {}
        assert st.is_dragging is False


# ── AUD-11401: unified constraint entry matches the live convention ─────────


class TestAud11401:
    def _scale_delta(self, modifiers, size=(100.0, 50.0)):
        delta, config, snapped = vts.compute_constrained_delta(
            QPointF(30.0, 3.0), modifiers,
            original_size=size, is_scale=True)
        assert snapped is None
        return config

    def test_no_modifier_locks_aspect(self):
        cfg = self._scale_delta(0)
        assert cfg.lock_aspect_ratio is True

    def test_shift_frees_aspect(self):
        cfg = self._scale_delta(0x2000000)  # ShiftModifier
        assert cfg.lock_aspect_ratio is False

    def test_shift_enum_input_accepted(self):
        cfg = self._scale_delta(Qt.KeyboardModifier.ShiftModifier)
        assert cfg.lock_aspect_ratio is False

    def test_rotate_shift_still_snaps(self):
        delta = QPointF(10.0, 10.0)
        _, config, snapped = vts.compute_constrained_delta(
            delta, 0x2000000, is_rotate=True)
        assert config.angle_snap_enabled is True
        assert snapped is not None

    def test_matches_live_pipeline_config(self):
        for mods in (0, 0x2000000, Qt.KeyboardModifier.NoModifier,
                     Qt.KeyboardModifier.ShiftModifier):
            unified = self._scale_delta(mods)
            live = vts.get_constraint_config_from_modifiers(
                mods, is_scale=True)
            assert unified.lock_aspect_ratio == live.lock_aspect_ratio, \
                f"convention mismatch for modifiers={mods!r}"

    def test_rotate_angle_unchanged_for_shift_and_snap_enabled(self):
        # No Shift on rotate: no snapping.
        _, config, snapped = vts.compute_constrained_delta(
            QPointF(10.0, 10.0), 0, is_rotate=True)
        assert config.angle_snap_enabled is False
        assert snapped is None
