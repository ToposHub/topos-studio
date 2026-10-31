"""Phase 4 tests: export CPU effects + after_clip_effects provider branch.

Covers:
* ``_apply_clip_effects_cpu`` runs the clip's effect chain on a BGR uint8
  frame using numpy/cv2 (blur / sharpen / glow / denoise / defocus).
* CPU handlers are no-ops when their driving parameter is below threshold.
* Unsupported effects passthrough (logged, not crashed).
* ``CompositeRuntimeContext.clip_effects`` field round-trips.
* ``TimelineClipFrameProvider`` routes ``after_clip_effects`` to the
  effect runner and falls back to the base texture when there are no
  effects.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_export import (
    TimelineExporter,
    _CPU_EFFECT_HANDLERS,
    _cpu_blur,
    _cpu_sharpen,
    _cpu_glow,
    _cpu_edge_detect,
    _cpu_denoise,
    _cpu_defocus,
)
from src.features.composite.core.runtime_context import CompositeRuntimeContext


# ──────────────────────────────────────────────────────────────────────────
# CPU effect handlers
# ──────────────────────────────────────────────────────────────────────────

def _white_pixel_frame() -> np.ndarray:
    """16×16 BGR frame with a single white pixel in the middle."""
    f = np.zeros((16, 16, 3), dtype=np.uint8)
    f[8, 8] = [255, 255, 255]
    return f


def _edge_frame() -> np.ndarray:
    """32×32 BGR frame with a sharp vertical edge (left half black,
    right half white) — gives sharpen/glow a real discontinuity to
    act on (linear ramps have zero second derivative, so unsharp is
    a no-op there)."""
    f = np.zeros((32, 32, 3), dtype=np.uint8)
    f[:, 16:] = 255
    return f


class TestCpuEffectHandlers:
    def test_registry_has_current_cpu_export_effects(self):
        assert set(_CPU_EFFECT_HANDLERS) >= {
            "blur", "sharpen", "glow", "erode_dilate", "edge_detect",
            "denoise",
            "defocus",
        }

    def test_blur_spreads_pixels(self):
        frame = _white_pixel_frame()
        out = _cpu_blur(frame, {"radius": 3.0, "mix": 1.0})
        # Neighbours of the original white pixel should now be non-zero.
        assert int(out[8, 7].max()) > 0 or int(out[7, 8].max()) > 0

    def test_blur_noop_when_radius_tiny(self):
        frame = _white_pixel_frame()
        out = _cpu_blur(frame, {"radius": 0.0})
        assert np.array_equal(out, frame)

    def test_blur_mix_blends(self):
        frame = _white_pixel_frame()
        full = _cpu_blur(frame, {"radius": 3.0, "mix": 1.0})
        half = _cpu_blur(frame, {"radius": 3.0, "mix": 0.5})
        # half-blend should be brighter than full at the original pixel
        # (mix pulls toward the sharper original which is all-or-nothing).
        assert int(half[8, 8].max()) >= int(full[8, 8].max())

    def test_sharpen_noop_when_amount_tiny(self):
        frame = _white_pixel_frame()
        out = _cpu_sharpen(frame, {"amount": 0.0})
        assert np.array_equal(out, frame)

    def test_sharpen_changes_frame(self):
        # Sharpen acts on high-frequency texture detail, not on hard
        # edges (those saturate under unsharp). A noise frame has the
        # per-pixel variation sharpen amplifies.
        rng = np.random.default_rng(42)
        frame = rng.integers(0, 256, size=(32, 32, 3), dtype=np.uint8)
        out = _cpu_sharpen(frame, {"amount": 1.0, "radius": 1.0})
        assert not np.array_equal(out, frame)

    def test_glow_noop_when_radius_tiny(self):
        frame = _white_pixel_frame()
        out = _cpu_glow(frame, {"radius": 0.0})
        assert np.array_equal(out, frame)

    def test_glow_brightens(self):
        frame = _white_pixel_frame()
        out = _cpu_glow(frame, {
            "threshold": 0.5, "radius": 3.0, "intensity": 1.0, "mix": 1.0,
        })
        # Glow should not darken anything; the white pixel stays near 255.
        assert int(out[8, 8].max()) >= 250

    def test_edge_detect_finds_vertical_sobel_edge(self):
        frame = _edge_frame()
        out = _cpu_edge_detect(frame, {
            "method": "sobel", "threshold": 0.1,
            "kernel_size": 3, "channel": "luminance",
        })
        assert np.all(out[:, 15:17] == 255)
        assert np.all(out[:, :14] == 0)
        assert np.all(out[:, 18:] == 0)

    def test_edge_detect_supports_direction_and_invert(self):
        frame = _edge_frame()
        edge = _cpu_edge_detect(frame, {
            "direction": "horizontal", "threshold": 0.1,
        })
        assert np.all(edge[:, 15:17] == 255)  # gx detects vertical edges
        assert np.all(edge[:, :14] == 0)
        inverted = _cpu_edge_detect(frame, {
            "direction": "horizontal", "threshold": 0.1,
            "invert": True,
        })
        assert np.all(inverted[:, 15:17] == 0)
        assert np.all(inverted[:, :14] == 255)

    def test_denoise_filters_alpha_in_full_mode_and_preserves_it_in_luma_mode(self):
        rng = np.random.default_rng(371)
        frame = rng.integers(0, 256, (23, 31, 4), dtype=np.uint8)
        params = {
            "strength": 0.65, "radius": 2,
            "luminance_only": False, "chroma_only": False,
        }
        result = _cpu_denoise(frame, params)
        assert result.shape == frame.shape
        assert result.dtype == np.uint8
        assert not np.array_equal(result[:, :, 3], frame[:, :, 3])
        assert not np.array_equal(result[:, :, :3], frame[:, :, :3])
        luma_only = _cpu_denoise(frame, {
            **params, "luminance_only": True,
        })
        assert np.array_equal(luma_only[:, :, 3], frame[:, :, 3])

    def test_denoise_luminance_only_keeps_chroma(self):
        frame = _edge_frame()
        frame[:, :, 0] //= 2
        result = _cpu_denoise(frame, {
            "strength": 0.8, "radius": 2, "luminance_only": True,
        })
        assert result.dtype == frame.dtype
        assert result.shape == frame.shape
        coeff = np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32)
        input_rgb = frame[:, :, ::-1].astype(np.float32) / 255.0
        output_rgb = result[:, :, ::-1].astype(np.float32) / 255.0
        input_luma = np.sum(input_rgb * coeff, axis=2, keepdims=True)
        output_luma = np.sum(output_rgb * coeff, axis=2, keepdims=True)
        chroma_difference = np.abs(
            (input_rgb - input_luma) - (output_rgb - output_luma)) * 255.0
        assert float(chroma_difference.max()) <= 0.75

    @pytest.mark.parametrize("edge_policy", ("clamp", "wrap", "mirror"))
    def test_defocus_separable_applies_focal_blur_with_edge_policy(self, edge_policy):
        frame = np.zeros((19, 27, 3), dtype=np.uint8)
        frame[1, 1] = (255, 180, 90)
        result = _cpu_defocus(frame, {
            "mode": "separable", "focal_x": 0.5, "focal_y": 0.5,
            "focal_size": 0.1, "max_radius": 12.0,
            "edge_policy": edge_policy, "process_alpha": True,
        })
        assert result.dtype == frame.dtype
        assert result.shape == frame.shape
        assert not np.array_equal(result, frame)
        assert np.count_nonzero(result[1, :, 0]) > 1

    def test_defocus_keeps_alpha_when_process_alpha_is_false(self):
        frame = np.zeros((13, 17, 4), dtype=np.uint8)
        frame[2, 2] = (255, 150, 40, 90)
        result = _cpu_defocus(frame, {
            "mode": "separable", "focal_x": 0.5, "focal_y": 0.5,
            "focal_size": 0.1, "max_radius": 8.0,
            "process_alpha": False,
        })
        assert not np.array_equal(result[:, :, :3], frame[:, :, :3])
        assert np.array_equal(result[:, :, 3], frame[:, :, 3])

    def test_defocus_mipmap_mode_uses_fractional_pyramid_lod(self):
        rng = np.random.default_rng(8041)
        frame = rng.integers(0, 256, (23, 31, 3), dtype=np.uint8)
        result = _cpu_defocus(frame, {
            "mode": "mipmap", "focal_x": 0.5, "focal_y": 0.5,
            "focal_size": 0.1, "max_radius": 15.0,
        })
        assert result.shape == frame.shape
        assert result.dtype == frame.dtype
        assert not np.array_equal(result, frame)

    def test_handlers_return_uint8(self):
        frame = _white_pixel_frame()
        for handler in (_cpu_blur, _cpu_sharpen, _cpu_glow,
                        _cpu_edge_detect, _cpu_denoise, _cpu_defocus):
            params = {"radius": 3.0, "amount": 1.0, "intensity": 1.0,
                      "threshold": 0.5, "mix": 1.0}
            out = handler(frame, params)
            assert out.dtype == np.uint8
            assert out.shape == frame.shape


# ──────────────────────────────────────────────────────────────────────────
# _apply_clip_effects_cpu (end-to-end through the exporter)
# ──────────────────────────────────────────────────────────────────────────

class TestApplyClipEffectsCpu:
    def _make_layer(self, effects: tuple) -> MagicMock:
        clip = MagicMock()
        clip.effects = effects
        layer = MagicMock()
        layer.clip = clip
        return layer

    def _make_effect(self, effect_type: str, params: dict,
                     enabled: bool = True, order: int = 0) -> MagicMock:
        eff = MagicMock()
        eff.effect_type = effect_type
        eff.is_enabled = enabled
        eff.order = order
        eff.params = params
        eff.get_param_at_frame = lambda name, frame: params.get(name)
        return eff

    def test_no_effects_passthrough(self):
        exporter = TimelineExporter.__new__(TimelineExporter)
        layer = self._make_layer(())
        frame = _white_pixel_frame()
        out = exporter._apply_clip_effects_cpu(frame, layer, 0)
        assert out is frame  # unchanged, same object

    def test_disabled_effect_skipped(self):
        exporter = TimelineExporter.__new__(TimelineExporter)
        eff = self._make_effect("blur", {"radius": 5.0}, enabled=False)
        layer = self._make_layer((eff,))
        frame = _white_pixel_frame()
        out = exporter._apply_clip_effects_cpu(frame, layer, 0)
        assert np.array_equal(out, frame)  # disabled → no-op

    def test_blur_applied(self):
        exporter = TimelineExporter.__new__(TimelineExporter)
        eff = self._make_effect("blur", {"radius": 5.0, "mix": 1.0})
        layer = self._make_layer((eff,))
        frame = _white_pixel_frame()
        out = exporter._apply_clip_effects_cpu(frame, layer, 0)
        assert not np.array_equal(out, frame)

    def test_unknown_effect_passthrough(self):
        """Effects without a CPU handler are skipped, not crashed."""
        exporter = TimelineExporter.__new__(TimelineExporter)
        eff = self._make_effect("exotic_filter", {"x": 1.0})
        layer = self._make_layer((eff,))
        frame = _white_pixel_frame()
        out = exporter._apply_clip_effects_cpu(frame, layer, 0)
        assert np.array_equal(out, frame)

    def test_multiple_effects_applied_in_order(self):
        exporter = TimelineExporter.__new__(TimelineExporter)
        blur = self._make_effect("blur", {"radius": 3.0, "mix": 1.0}, order=0)
        sharp = self._make_effect("sharpen", {"amount": 1.0, "radius": 1.0}, order=1)
        layer = self._make_layer((sharp, blur))  # unsorted; order field drives it
        frame = _white_pixel_frame()
        out = exporter._apply_clip_effects_cpu(frame, layer, 0)
        assert out.dtype == np.uint8
        # Both effects ran → result differs from a blur-only pass.
        blur_only = _cpu_blur(frame, {"radius": 3.0, "mix": 1.0})
        assert not np.array_equal(out, blur_only)

    def test_sample_effect_params_uses_get_param_at_frame(self):
        """When the effect has get_param_at_frame, params are sampled at
        the requested frame (keyframe-aware)."""
        exporter = TimelineExporter.__new__(TimelineExporter)
        eff = MagicMock()
        eff.params = {"radius": 5.0}
        calls = []
        eff.get_param_at_frame = lambda name, frame: calls.append((name, frame)) or 7.0
        out = TimelineExporter._sample_effect_params(eff, frame_idx=42)
        assert out == {"radius": 7.0}
        assert calls == [("radius", 42)]


# ──────────────────────────────────────────────────────────────────────────
# CompositeRuntimeContext.clip_effects
# ──────────────────────────────────────────────────────────────────────────

class TestRuntimeContextClipEffects:
    def test_default_is_empty_tuple(self):
        ctx = CompositeRuntimeContext.for_timeline_clip(
            project_id="p", graph_id="g", timeline_id="t", clip_id="c",
            timeline_frame=0, clip_local_frame=0, source_frame=0,
        )
        assert ctx.clip_effects == ()

    def test_round_trips_supplied_effects(self):
        effects = ("eff1", "eff2")
        ctx = CompositeRuntimeContext.for_timeline_clip(
            project_id="p", graph_id="g", timeline_id="t", clip_id="c",
            timeline_frame=0, clip_local_frame=0, source_frame=0,
            input_stage="after_clip_effects",
            clip_effects=effects,
        )
        assert ctx.input_stage == "after_clip_effects"
        assert ctx.clip_effects == effects


# ──────────────────────────────────────────────────────────────────────────
# TimelineClipFrameProvider — after_clip_effects routing
# ──────────────────────────────────────────────────────────────────────────

class TestProviderAfterClipEffectsRouting:
    def test_after_clip_effects_with_no_effects_returns_base(self):
        """When clip_effects is empty, after_clip_effects returns the
        after_color texture unchanged (no runner needed)."""
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
        )
        provider = TimelineClipFrameProvider()
        provider._effect_runner = None  # ensure no runner
        provider._gl_context = None

        base_tex = MagicMock()
        base_tex.texture_id = 42
        provider._try_color_output_texture = lambda ctx: base_tex

        ctx = CompositeRuntimeContext.for_timeline_clip(
            project_id="p", graph_id="g", timeline_id="t", clip_id="c",
            timeline_frame=0, clip_local_frame=0, source_frame=0,
            input_stage="after_clip_effects",
            clip_effects=(),  # no effects
        )
        result = provider.get_timeline_clip_texture(ctx)
        assert result is base_tex

    def test_after_clip_effects_with_effects_calls_runner(self):
        """When clip_effects is non-empty, the provider runs them through
        EffectPassRunner and returns the processed texture."""
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
        )
        provider = TimelineClipFrameProvider()
        provider._gl_context = MagicMock()  # truthy so runner can init

        base_tex = MagicMock()
        base_tex.texture_id = 42
        base_tex.format = ("rgba16f", 4)
        base_tex.alpha_premultiplied = True
        base_tex.color_space = None
        base_tex.bbox = None
        provider._try_color_output_texture = lambda ctx: base_tex

        # Mock the runner so no real GL runs.
        fake_runner = MagicMock()
        fake_runner.run.return_value = 99  # new glo
        provider._get_effect_runner = lambda: fake_runner

        # Mock the catalog so the effect resolves.
        fake_spec = MagicMock()
        fake_catalog = MagicMock()
        fake_catalog.get_spec.return_value = fake_spec

        eff = MagicMock()
        eff.effect_type = "blur"
        eff.is_enabled = True
        eff.order = 0
        eff.params = {"radius": 5.0}
        eff.get_param_at_frame = lambda name, frame: 5.0

        ctx = CompositeRuntimeContext.for_timeline_clip(
            project_id="p", graph_id="g", timeline_id="t", clip_id="c",
            timeline_frame=0, clip_local_frame=0, source_frame=0,
            input_stage="after_clip_effects",
            clip_effects=(eff,),
        )

        with patch(
            "src.features.shared.effects.get_effect_catalog",
            return_value=fake_catalog,
        ):
            result = provider.get_timeline_clip_texture(ctx)

        # Runner was called with the base texture's glo.
        fake_runner.run.assert_called_once()
        assert fake_runner.run.call_args[0][1] == 42  # input_glo
        # Result carries the new glo (99).
        assert result.texture_id == 99

    def test_after_clip_effects_samples_keyframes_at_timeline_frame(self):
        """Composite's effect stage follows the Edit timeline-time contract.

        A trimmed/retimed clip can have a source frame that differs from the
        global playhead.  Effects are animated at the playhead, so this path
        must never pass ``source_frame`` to ``get_param_at_frame``.
        """
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
        )

        provider = TimelineClipFrameProvider()
        provider._gl_context = MagicMock()
        fake_runner = MagicMock()
        fake_runner.run.return_value = 99
        provider._get_effect_runner = lambda: fake_runner

        base_tex = MagicMock()
        base_tex.texture_id = 42
        base_tex.format = ("rgba16f", 4)
        base_tex.alpha_premultiplied = True
        base_tex.color_space = None
        base_tex.bbox = None

        fake_catalog = MagicMock()
        fake_catalog.get_spec.return_value = MagicMock()
        sampled_frames = []
        eff = MagicMock()
        eff.effect_type = "blur"
        eff.is_enabled = True
        eff.order = 0
        eff.params = {"radius": 5.0}
        eff.get_param_at_frame = (
            lambda name, frame: sampled_frames.append((name, frame)) or 5.0
        )

        context = CompositeRuntimeContext.for_timeline_clip(
            project_id="p", graph_id="g", timeline_id="t", clip_id="c",
            timeline_frame=105, clip_local_frame=5, source_frame=17,
            input_stage="after_clip_effects", clip_effects=(eff,),
        )

        with patch(
            "src.features.shared.effects.get_effect_catalog",
            return_value=fake_catalog,
        ):
            result = provider._apply_after_clip_effects(
                base_tex, (eff,), 64, 64, context,
            )

        assert result.texture_id == 99
        assert sampled_frames == [("radius", 105)]
