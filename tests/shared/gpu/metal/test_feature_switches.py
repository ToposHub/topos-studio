"""One switch per Metal capability, and a test for each fallback (plan 8.5/8.6).

``TOPOS_METAL_DISABLE`` turns a capability off whatever its own opt-in says.
These tests are the reproducible fallback the plan asks for: set the switch,
watch the capability refuse, with the reason telling a deliberate switch-off
from a failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.shared.gpu.metal.feature_switches import (
    ENV,
    OPT_IN_ENV,
    ROLLOUT,
    MetalFeature,
    disabled_features,
    feature_disabled,
    opt_in_enabled,
    parse_disabled,
    rollout_of,
    switch_refusal,
)


class TestParsing:
    def test_empty_and_blank_switch_nothing_off(self) -> None:
        assert parse_disabled("") == ((), ())
        assert parse_disabled(" , ;") == ((), ())
        assert disabled_features() == frozenset()

    def test_names_are_split_trimmed_and_lower_case(self) -> None:
        disabled, unknown = parse_disabled(" Native_Viewport ; edit_metal_preview,")
        assert disabled == ("native_viewport", "edit_metal_preview")
        assert unknown == ()

    def test_a_name_listed_twice_is_off_once(self) -> None:
        assert parse_disabled("native_viewport,native_viewport")[0] == ("native_viewport",)

    def test_unknown_names_are_reported_and_ignore_nothing_else(self) -> None:
        disabled, unknown = parse_disabled("native_viewport,colour,colour,typo")
        assert disabled == ("native_viewport",)
        assert unknown == ("colour", "typo")


class TestSwitch:
    def test_a_switched_off_feature_refuses_with_its_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "native_viewport")
        assert feature_disabled(MetalFeature.NATIVE_VIEWPORT)
        assert switch_refusal(MetalFeature.NATIVE_VIEWPORT) == "disabled_by_switch:native_viewport"

    def test_other_features_keep_their_own_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "native_viewport")
        assert switch_refusal(MetalFeature.EDIT_PREVIEW) is None
        assert switch_refusal(MetalFeature.EDIT_COMPOSITE) is None

    def test_an_unknown_name_disables_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV, "everything")
        assert not any(switch_refusal(feature) for feature in MetalFeature)


class TestLedger:
    def test_every_capability_has_exactly_one_entry(self) -> None:
        features = [entry.feature for entry in ROLLOUT]
        assert sorted(feature.value for feature in features) == sorted(
            feature.value for feature in MetalFeature)
        assert len(features) == len(set(features))

    @pytest.mark.parametrize("feature", list(MetalFeature), ids=lambda f: f.value)
    def test_an_entry_says_how_to_enable_and_what_it_takes_to_change(self, feature) -> None:
        entry = rollout_of(feature)
        assert entry.default in ("opt-in", "default")
        assert entry.opt_in.strip() and entry.since.strip() and entry.condition.strip()

    def test_what_is_on_by_default_is_stated_explicitly(self) -> None:
        """Only the export fast path is on by default; the rest waits for its
        compatibility condition (plan 8.5: explicit first, default later)."""
        by_default = [entry.feature.value for entry in ROLLOUT if entry.default == "default"]
        assert by_default == [
            MetalFeature.EDIT_PREVIEW.value,
            MetalFeature.EDIT_COMPOSITE.value,
            MetalFeature.NATIVE_VIEWPORT.value,
            MetalFeature.EXPORT_NATIVE_VT.value,
        ]


# ---------------------------------------------------------------------------
# Each capability honours the switch (the reproducible fallback of plan 8.6)
# ---------------------------------------------------------------------------


def test_the_edit_preview_switch_overrides_the_graphics_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import metal_timeline_preview

    monkeypatch.setattr(metal_timeline_preview.sys, "platform", "darwin")
    monkeypatch.setenv("TOPOS_BACKEND_GRAPHICS", "metal")
    monkeypatch.setenv(ENV, "edit_metal_preview")
    assert metal_timeline_preview.metal_preview_mode() == "off"
    monkeypatch.setenv(ENV, "")
    assert metal_timeline_preview.metal_preview_mode() == "all"


def test_the_composite_switch_keeps_composites_on_gl_but_leaves_the_single_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer, EditRenderTask

    monkeypatch.setenv(ENV, "edit_metal_composite")
    renderer = EditGpuPreviewRenderer(main_context=None)
    renderer._metal_preview_mode = "all"
    source = type("Source", (), {
        "pixel_buffer": object(), "pixel_format": "nv12", "bit_depth": 8, "width": 64,
        "height": 32, "color_matrix": "bt709", "color_range": "limited",
        "color_primaries": "bt709", "color_transfer": "bt709"})()
    dissolve = EditRenderTask(7, 1, [
        {"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": source,
         "track_info": {"opacity": 0.4}},
        {"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": source,
         "track_info": {"opacity": 0.6}},
    ])
    untouched = EditRenderTask(7, 1, [
        {"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": source, "track_info": {}}])

    assert renderer._metal_composite_plan(dissolve) is None
    # The switch is per capability: the single-clip preview is not touched.
    assert renderer._metal_preview_plan(untouched) is not None
    assert renderer._metal_composite_refusals == {"disabled_by_switch:edit_metal_composite": 1}


def test_the_viewport_switch_wins_over_the_session_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PyQt6.QtWidgets")
    from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
    from src.features.edit.core import metal_timeline_preview

    monkeypatch.setattr(metal_timeline_preview, "metal_preview_mode", lambda: "lut")
    monkeypatch.setenv("TOPOS_EDIT_METAL_VIEWPORT", "1")
    monkeypatch.setenv(ENV, "native_viewport")
    assert EditGpuPreviewRenderer(main_context=None).native_viewport_opted_in() is False
    monkeypatch.setenv(ENV, "")
    assert EditGpuPreviewRenderer(main_context=None).native_viewport_opted_in() is True


def test_the_export_switch_keeps_the_native_export_family_on_the_regular_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both native VideoToolbox export entries: the default-on Metal H.264
    fast path and the opt-in regular bridge (plan 3.1/3.2, 8.6)."""
    pytest.importorskip("PyQt6.QtWidgets")
    from src.features.edit.core.timeline_export import (
        TimelineExporter,
        native_export_switched_off,
    )

    monkeypatch.setenv("TOPOS_EXPORT_NATIVE_VT", "1")
    assert native_export_switched_off() is False
    monkeypatch.setenv("TOPOS_METAL_DISABLE", "export_native_vt")
    assert native_export_switched_off() is True
    # A bare exporter: the switch is read before anything of the export is
    # touched, so neither entry gets as far as needing exporter state.
    exporter = TimelineExporter.__new__(TimelineExporter)
    assert exporter._try_export_metal_h264_lut(object(), object(), 0, 1) is None
    exporter._maybe_create_default_native_gpu_bridge("/unused/out.mp4", object())
    assert not hasattr(exporter, "_gpu_encode_surface_provider")


def test_the_composite_graph_switch_wins_over_its_own_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.composite.render.composite_graph_render_service import (
        composite_metal_enabled,
    )

    monkeypatch.setenv("TOPOS_COMPOSITE_METAL", "1")
    monkeypatch.setenv(ENV, "composite_metal")
    assert composite_metal_enabled() is False
    monkeypatch.setenv(ENV, "")
    assert composite_metal_enabled() is True


class TestOptIn:
    """A capability asked for by its own variable, and not switched off."""

    @pytest.fixture(autouse=True)
    def asking_for_nothing(self, monkeypatch: pytest.MonkeyPatch):
        for variable in OPT_IN_ENV.values():
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.delenv(ENV, raising=False)

    def test_off_unless_asked_for(self) -> None:
        assert opt_in_enabled(MetalFeature.COLOR_GRAPH_TEXTURE) == (False, "opt_in_missing")

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_on_when_asked_for(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TOPOS_COLOR_METAL", value)
        assert opt_in_enabled(MetalFeature.COLOR_GRAPH_TEXTURE) == (True, "")

    def test_the_kill_switch_wins_over_the_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.setenv(ENV, "color_graph_metal")
        assert opt_in_enabled(MetalFeature.COLOR_GRAPH_TEXTURE) == (
            False, "disabled_by_switch:color_graph_metal")

    def test_another_capability_in_the_kill_switch_does_not_matter(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TOPOS_COLOR_METAL", "1")
        monkeypatch.setenv(ENV, "native_viewport")
        assert opt_in_enabled(MetalFeature.COLOR_GRAPH_TEXTURE) == (True, "")

    def test_a_capability_without_a_variable_of_its_own_is_not_guessed(self) -> None:
        with pytest.raises(KeyError):
            opt_in_enabled(MetalFeature.EDIT_PREVIEW)

    def test_every_variable_in_the_table_is_named_in_its_ledger_entry(self) -> None:
        for feature, variable in OPT_IN_ENV.items():
            assert f"{variable}=1" in rollout_of(feature).opt_in
