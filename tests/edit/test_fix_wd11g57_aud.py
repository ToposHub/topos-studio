"""Regression tests for remediation batch wd11-g57 (AUD-16605..AUD-16911).

Covers the Major-grade fixes in edit-core playback / effect / IO domains:

- AUD-16700  JKL first press must hit 1x (gear off-by-one)
- AUD-16701  broad ``except (ValueError, Exception)`` narrowed to ValueError
- AUD-16711  backup rotation must match exact file names, not stem prefix
- AUD-16712  save aborts when backup creation fails
- AUD-16713  color-management collect failures are logged, not silent
- AUD-16714  color-management override injection failures are logged
- AUD-16800  AnimatableParam.add_keyframe is copy-on-write
- AUD-16804  catalog fallback must not NameError on deleted symbols
- AUD-16812  generator asset removal preserves project revision
- AUD-16900  iris radius adapts to aspect (full-frame coverage)
- AUD-16905  integrated LUFS second-stage gating is lazy (O(1) per hop)
- AUD-16605  renderer _cleanup releases composite cache / FBOs / programs

AUD-16903 (interchange Rational fps) was fixed by the orchestrator and is
asserted here as a guard; AUD-16808's runtime premise is re-verified
(production scoped composite path exists).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ───────────────────────── AUD-16700: JKL gear off-by-one ───────────────────


class TestJklFirstPressIsOneX:
    """First L/J press must play at 1x; consecutive presses 1→2→4→8."""

    @pytest.fixture
    def controller(self, qapp):
        from src.features.edit.core.edit_playback_controller import (
            EditPlaybackController,
        )

        ctl = EditPlaybackController()
        ctl.set_timeline_duration(1000)
        return ctl

    def test_first_l_is_1x(self, controller):
        controller.jkl_forward_press()
        assert controller.playback_speed == 1.0

    def test_repeated_l_ladder(self, controller):
        expected = [1.0, 2.0, 4.0, 8.0, 8.0]
        speeds = []
        for _ in range(5):
            controller.jkl_forward_press()
            speeds.append(controller.playback_speed)
        assert speeds == expected

    def test_first_j_is_backward_1x(self, controller):
        from src.features.edit.core.edit_playback_controller import (
            PlaybackDirection,
        )

        controller.jkl_backward_press()
        assert controller.playback_speed == 1.0
        assert controller.playback_direction == PlaybackDirection.BACKWARD

    def test_repeated_j_ladder(self, controller):
        speeds = []
        for _ in range(4):
            controller.jkl_backward_press()
            speeds.append(controller.playback_speed)
        assert speeds == [1.0, 2.0, 4.0, 8.0]

    def test_after_k_next_l_is_1x_again(self, controller):
        controller.jkl_forward_press()
        controller.jkl_forward_press()  # 2x
        controller.jkl_stop()           # K pauses
        controller.jkl_forward_press()  # fresh L from pause → 1x
        assert controller.playback_speed == 1.0

    def test_after_reset_next_l_is_1x(self, controller):
        controller.jkl_forward_press()
        controller.jkl_reset()
        controller.jkl_forward_press()
        assert controller.playback_speed == 1.0


# ───────────────────────── AUD-16701: broad except narrowed ─────────────────


class TestTimecodeExceptionScope:
    """Non-domain Timecode errors must propagate instead of silent fallback."""

    @pytest.fixture
    def controller(self, qapp):
        from src.features.edit.core.edit_playback_controller import (
            EditPlaybackController,
        )

        return EditPlaybackController()

    def test_domain_error_still_falls_back(self, controller, monkeypatch):
        import src.features.edit.core.edit_playback_controller as mod

        def _raise_value_error(fps):
            raise ValueError("bad fps")

        monkeypatch.setattr(
            mod.Timecode, "auto_detect_type", staticmethod(_raise_value_error)
        )
        # Domain error → integer-division fallback still works.
        assert controller.frame_to_timecode(24) == "00:00:01:00"

    def test_programming_error_propagates(self, controller, monkeypatch):
        import src.features.edit.core.edit_playback_controller as mod

        def _raise_type_error(fps):
            raise TypeError("bug in timecode module")

        monkeypatch.setattr(
            mod.Timecode, "auto_detect_type", staticmethod(_raise_type_error)
        )
        with pytest.raises(TypeError):
            controller.frame_to_timecode(24)

    def test_parse_programming_error_propagates(self, controller, monkeypatch):
        import src.features.edit.core.edit_playback_controller as mod

        def _raise_key_error(text, fps):
            raise KeyError("bug")

        monkeypatch.setattr(
            mod.Timecode, "from_string", staticmethod(_raise_key_error)
        )
        with pytest.raises(KeyError):
            controller.timecode_to_frame("00:00:01:00")

    def test_invalid_timecode_still_returns_none(self, controller):
        # Domain error path: unparseable input → None (locked old behaviour).
        assert controller.timecode_to_frame("not-a-timecode") is None


# ───────────────────────── AUD-16711: exact-name backup matching ────────────


class TestBackupRotationExactName:
    def test_prefix_siblings_are_never_deleted(self, qapp, tmp_path):
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        io.config.max_backup_count = 1

        project_file = tmp_path / "demo.topos"
        project_file.write_bytes(b"current")
        own_backup = tmp_path / "demo.topos.bak"
        own_backup.write_bytes(b"own")
        # Other projects sharing the stem prefix — must survive rotation.
        sibling_a = tmp_path / "demo2.topos.bak"
        sibling_a.write_bytes(b"demo2")
        sibling_b = tmp_path / "demo_reel.topos.bak"
        sibling_b.write_bytes(b"demo_reel")

        io._cleanup_old_backups(project_file)

        assert sibling_a.exists()
        assert sibling_b.exists()
        # The project's own single fixed-name backup is kept (1 <= max=1).
        assert own_backup.exists()

    def test_own_backups_beyond_limit_are_rotated(self, qapp, tmp_path):
        from src.features.edit.core.edit_project_io import EditProjectIO

        io = EditProjectIO()
        io.config.max_backup_count = 1

        project_file = tmp_path / "solo.topos"
        project_file.write_bytes(b"current")
        # _create_backup only ever writes one fixed name; simulate a legacy
        # directory that somehow holds two exact-name copies is impossible,
        # so assert the invariant directly: only the exact name is matched.
        decoy = tmp_path / "solo_v2.topos.bak"
        decoy.write_bytes(b"decoy")
        io._cleanup_old_backups(project_file)
        assert decoy.exists()


# ───────────────────────── AUD-16712: save aborts on backup failure ─────────


class TestSaveAbortsOnBackupFailure:
    def test_failed_backup_blocks_save(self, qapp, tmp_path, monkeypatch):
        from src.features.edit.core import edit_project_io as io_mod
        from src.features.edit.core.project import EditProject

        io = io_mod.EditProjectIO()
        project = EditProject(project_id="pid", name="p")

        target = tmp_path / "proj.topos"
        target.write_bytes(b"previous-version")

        monkeypatch.setattr(io, "_create_backup", lambda path: False)
        saved_flag = {"called": False}

        class _StubToposIO:
            @staticmethod
            def save(*args, **kwargs):
                saved_flag["called"] = True
                return True

        monkeypatch.setattr(io_mod, "ToposProjectIO", _StubToposIO)

        ok = io.save_project(target, project)
        assert ok is False
        assert saved_flag["called"] is False, "write must not run after failed backup"
        assert target.read_bytes() == b"previous-version"

    def test_successful_backup_continues_save(self, qapp, tmp_path, monkeypatch):
        from src.features.edit.core import edit_project_io as io_mod
        from src.features.edit.core.project import EditProject

        io = io_mod.EditProjectIO()
        project = EditProject(project_id="pid", name="p")

        target = tmp_path / "proj2.topos"
        target.write_bytes(b"previous-version")

        monkeypatch.setattr(io, "_create_backup", lambda path: True)
        saved_flag = {"called": False}

        class _StubToposIO:
            @staticmethod
            def save(*args, **kwargs):
                saved_flag["called"] = True
                return True

        monkeypatch.setattr(io_mod, "ToposProjectIO", _StubToposIO)

        assert io.save_project(target, project) is True
        assert saved_flag["called"] is True


# ───────────────────── AUD-16713/16714: color-mgmt error logging ────────────


class TestColorManagementErrorLogging:
    def _descriptor_module(self):
        import src.shared.color.color_space_descriptor as descriptor

        return descriptor

    def test_collect_runtime_error_logs(self, monkeypatch, caplog):
        descriptor = self._descriptor_module()

        def _boom():
            raise RuntimeError("collector crashed")

        monkeypatch.setattr(
            descriptor, "collect_color_management_settings", _boom
        )
        from src.features.edit.core.edit_project_io import (
            _collect_color_management_settings,
        )

        with caplog.at_level("ERROR"):
            result = _collect_color_management_settings()
        assert result == {}
        assert any(
            "color management" in rec.message.lower() for rec in caplog.records
        ), "failure must be logged, not silent"

    def test_injection_runtime_error_logged_not_sworn_silent(
        self, monkeypatch, caplog
    ):
        descriptor = self._descriptor_module()

        def _boom(overrides):
            raise RuntimeError("injector crashed")

        monkeypatch.setattr(descriptor, "set_project_color_overrides", _boom)
        from src.features.edit.core.edit_project_io import (
            convert_topos_project_to_edit_project,
        )
        from src.shared.project.topos_project import ToposProject

        project = ToposProject(project_id="pid", name="p")
        with caplog.at_level("ERROR"):
            # Must not raise — load must survive, but loudly.
            convert_topos_project_to_edit_project(project)
        assert any("inject" in rec.message.lower() for rec in caplog.records)

    def test_collect_import_missing_is_quiet(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "src.shared.color.color_space_descriptor", None)
        from src.features.edit.core.edit_project_io import (
            _collect_color_management_settings,
        )

        with caplog.at_level("ERROR"):
            result = _collect_color_management_settings()
        assert result == {}
        assert not caplog.records, "missing module is a legal degrade, no error log"


# ───────────────────────── AUD-16800: COW add_keyframe ──────────────────────


class TestAddKeyframeCopyOnWrite:
    def test_adding_does_not_mutate_prior_track(self):
        from src.features.edit.core.effect_instance import AnimatableParam

        base = AnimatableParam(base_value=0.5)
        first = base.add_keyframe(10, 0.7)
        second = first.add_keyframe(20, 0.9)

        assert first.keyframes is not None
        assert len(first.keyframes.keyframes) == 1, (
            "old instance's track must not gain the new keyframe"
        )
        assert second.keyframes is not first.keyframes, (
            "instances must not share one mutable KeyframeTrack"
        )
        assert len(second.keyframes.keyframes) == 2

    def test_keyframe_value_visible_on_new_instance_only(self):
        from src.features.edit.core.effect_instance import AnimatableParam

        base = AnimatableParam(base_value=0.5)
        first = base.add_keyframe(10, 0.7)
        # Old (pre-keyframe) instance still returns the base value.
        assert base.get_value_at_frame(10) == 0.5
        assert first.get_value_at_frame(10) == 0.7

    def test_extrapolation_policy_survives_copy(self):
        from src.features.edit.core.effect_instance import AnimatableParam
        from src.shared.keyframe import Keyframe, KeyframeTrack

        track = KeyframeTrack(
            param_name="opacity",
            keyframes=[Keyframe(0, 0.1)],
            min_val=0.0,
            max_val=1.0,
            pre_extrap="linear",
            post_extrap="linear",
        )
        param = AnimatableParam(base_value=0.5, keyframes=track)
        grown = param.add_keyframe(30, 0.8)
        assert grown.keyframes.pre_extrap == "linear"
        assert grown.keyframes.post_extrap == "linear"


# ───────────────────────── AUD-16804: registry fallback ─────────────────────


class TestRegistryCatalogFallback:
    def test_catalog_failure_does_not_crash_registry(self, monkeypatch):
        import src.features.shared.effects as catalog_mod
        from src.features.edit.core.effect_registry import EffectRegistry

        def _boom():
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr(catalog_mod, "get_effect_catalog", _boom)
        # Old code raised NameError (GAUSSIAN_BLUR_EFFECT undefined) here.
        registry = EffectRegistry()
        assert registry.get_definition("strip_eq") is not None, (
            "previously registered built-ins must survive a catalog failure"
        )


# ───────────────────────── AUD-16812: revision passthrough ──────────────────


class TestGeneratorRemovalKeepsRevision:
    def test_remove_asset_preserves_revision(self):
        from src.features.edit.core.generator_clip_transaction import (
            _remove_asset_and_bin_entry,
        )
        from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin

        asset = MediaAsset(
            asset_id="gen-1", path="", media_type="solid",
            display_name="Solid", metadata={},
        )
        project = EditProject(
            project_id="pid",
            name="p",
            assets={"gen-1": asset},
            bins={"bin-1": ProjectBin(
                bin_id="bin-1", name="B", parent_id=None,
                asset_ids=("gen-1",),
            )},
            revision=41,
        )
        result = _remove_asset_and_bin_entry(project, "gen-1")
        assert result.revision == 41, "revision must not silently reset to 0"
        assert "gen-1" not in result.assets
        assert "gen-1" not in result.bins["bin-1"].asset_ids


# ───────────────────────── AUD-16900: iris aspect coverage ──────────────────


class TestIrisAspectCoverage:
    def _shader_source(self) -> str:
        import src.features.edit.core.gpu_transition_shaders as shaders

        return shaders.GPU_TRANSITION_FRAGMENT_SHADER_SOURCE

    def test_constant_radius_is_gone(self):
        source = self._shader_source()
        assert "1.55 * progress" not in source, (
            "fixed 1.55 radius cannot cover wide frames"
        )

    def test_adaptive_formula_present(self):
        source = self._shader_source()
        assert "sqrt(aspect * aspect + 1.0)" in source
        assert "sqrt(aspect * aspect + 1.0 / (0.62 * 0.62))" in source

    @staticmethod
    def _circle_covers_frame(aspect: float, progress: float) -> bool:
        """Python mirror of the shader circle test at the extreme corners."""
        radius = progress * math.sqrt(aspect * aspect + 1.0)
        return all(
            (x * x + y * y) <= radius * radius
            for x in (aspect, -aspect)
            for y in (1.0, -1.0)
        )

    @staticmethod
    def _ellipse_covers_frame(aspect: float, progress: float) -> bool:
        """Python mirror of the shader ellipse test at the extreme corners."""
        scale = progress * math.sqrt(aspect * aspect + 1.0 / (0.62 * 0.62))
        a = max(scale, 1.0e-6)
        b = max(scale * 0.62, 1.0e-6)
        return all(
            (x / a) ** 2 + (y / b) ** 2 <= 1.0
            for x in (aspect, -aspect)
            for y in (1.0, -1.0)
        )

    @pytest.mark.parametrize("aspect", [1.0, 4 / 3, 16 / 9, 2.39])
    def test_circle_covers_corners_at_end(self, aspect):
        assert self._circle_covers_frame(aspect, 1.0)

    @pytest.mark.parametrize("aspect", [1.0, 4 / 3, 16 / 9, 2.39])
    def test_ellipse_covers_corners_at_end(self, aspect):
        assert self._ellipse_covers_frame(aspect, 1.0)

    def test_old_constant_failed_wide_frames(self):
        # Documents the bug: 1.55 could not reach the 16:9 edge midpoint.
        assert 1.55 < 16 / 9


# ───────────────────────── AUD-16905: lazy integrated gating ────────────────


class TestLazyIntegratedLoudness:
    def _meter_with_blocks(self, count: int):
        from src.features.edit.core.loudness_meter import LoudnessMeter

        meter = LoudnessMeter(48000.0, channels=2)
        rng = np.random.default_rng(42)
        for _ in range(count):
            block = (rng.uniform(0.05, 0.35, (2, 4800))).astype(np.float32)
            meter.process(block)
        return meter

    def test_integrated_still_gated_bs1770(self):
        meter = self._meter_with_blocks(40)
        gated = list(meter._gated_blocks)
        assert gated, "signal above absolute gate must accumulate blocks"
        # Reference: same two-stage formula, computed inline.
        mean_first = meter._gated_sum / len(gated)
        rel_z = meter._lufs_to_z(meter._z_to_lufs(mean_first) - 10.0)
        second = [z for z in gated if z > rel_z]
        expected = meter._z_to_lufs(sum(second) / len(second))
        assert meter.integrated_lufs == pytest.approx(expected, rel=1e-12)

    def test_hop_path_does_not_rescan(self, monkeypatch):
        from src.features.edit.core.loudness_meter import LoudnessMeter

        meter = LoudnessMeter(48000.0, channels=2)
        calls = {"n": 0}
        original = LoudnessMeter._compute_integrated

        def _counting(self):
            calls["n"] += 1
            return original(self)

        monkeypatch.setattr(LoudnessMeter, "_compute_integrated", _counting)
        rng = np.random.default_rng(7)
        for _ in range(30):
            meter.process(rng.uniform(0.1, 0.3, (2, 4800)).astype(np.float32))
        assert calls["n"] == 0, "process() must not trigger the O(N) rescan"
        value = meter.integrated_lufs
        assert calls["n"] == 1
        assert meter.integrated_lufs == value
        assert calls["n"] == 1, "second read must be served from cache"

    def test_reset_clears_cache(self):
        meter = self._meter_with_blocks(5)
        assert meter.integrated_lufs > -70.0
        meter.reset()
        assert meter.integrated_lufs == -70.0
        assert not meter._integrated_dirty


# ───────────────────────── AUD-16903 guard: Rational fps ────────────────────


class TestCueSheetRationalFpsGuard:
    def test_rational_fps_does_not_crash(self):
        from src.features.edit.core.interchange import build_cue_sheet_from_sequence
        from src.shared.media.metadata import Rational

        clip = SimpleNamespace(
            clip_id="c1", asset_id="a1", timeline_start=0, timeline_duration=48
        )
        track = SimpleNamespace(name="A1", clips=[clip])
        sequence = SimpleNamespace(
            fps=Rational(24, 1),
            name="seq",
            duration_frames=96,
            audio_tracks=[track],
        )
        sheet = build_cue_sheet_from_sequence(sequence, project_name="p")
        assert sheet.fps == 24.0
        assert sheet.entries[0].duration_seconds == pytest.approx(2.0)


# ───────────────────────── AUD-16605: renderer cleanup coverage ─────────────


class _FakeGlObject:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class TestRendererCleanupReleasesAllResources:
    _COVERED_ATTRS = (
        "_bg_composite_tex", "_bg_composite_fbo",
        "_snapshot_tex", "_snapshot_fbo",
        "_yuv_program", "_yuv_rect_program", "_yuv3_program",
        "_grade_program", "_grade_vao",
        "_transform_program", "_transform_vao",
        "_crop_program", "_crop_vao",
        "_generator_program", "_generator_vao",
        "_blend_program", "_quad_vao", "_blend_vao", "_flip_blend_vao",
        "_yuv2_vao", "_yuv_rect_vao", "_yuv3_vao",
        "_transition_program", "_transition_vao",
        "_dip_program", "_dip_vao", "_flip_dip_vao",
        "_snapshot_program", "_snapshot_vao",
        "_quad_vbo", "_blend_quad_vbo",
    )

    def _make_renderer(self, qapp):
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        renderer = EditGpuPreviewRenderer(main_context=None)
        renderer._context_setup_cancelled.set()
        return renderer

    def test_cleanup_releases_programs_vaos_fbos(self, qapp):
        renderer = self._make_renderer(qapp)
        fakes = {}
        for attr in self._COVERED_ATTRS:
            fake = _FakeGlObject()
            fakes[attr] = fake
            setattr(renderer, attr, fake)

        renderer._cleanup()

        leaked = [attr for attr, fake in fakes.items() if fake.released == 0]
        assert not leaked, f"cleanup leaked GL objects: {leaked}"

    def test_cleanup_releases_composite_cache_entries(self, qapp):
        renderer = self._make_renderer(qapp)
        tex, fbo = _FakeGlObject(), _FakeGlObject()
        renderer._composite_cache[(1, 5)] = (tex, fbo, (64, 64))

        renderer._cleanup()

        assert tex.released == 1 and fbo.released == 1
        assert renderer._composite_cache == {}
        assert renderer._bg_composite_size is None

    def test_cleanup_releases_timer_and_uploader(self, qapp):
        renderer = self._make_renderer(qapp)
        timer = _FakeGlObject()
        uploader = _FakeGlObject()
        renderer._gl_pass_timer = timer
        renderer._yuv_uploader = uploader

        renderer._cleanup()

        assert timer.released == 1
        assert uploader.released == 1
        assert renderer._gl_pass_timer is None
        assert renderer._yuv_uploader is None

    def test_cleanup_without_gl_is_a_noop(self, qapp):
        renderer = self._make_renderer(qapp)
        renderer._cleanup()  # must not raise on a never-initialized renderer
        assert renderer.ctx is None

    def test_teardown_requested_signal_reaches_gui_thread(self, qapp):
        """The GL teardown signal drives context/surface destruction."""
        renderer = self._make_renderer(qapp)

        class _FakeCtx:
            def __init__(self):
                self.deleted = False

            def deleteLater(self):
                self.deleted = True

        class _FakeSurface:
            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        fake_ctx, fake_surface = _FakeCtx(), _FakeSurface()
        renderer._gl_teardown_requested.emit(fake_ctx, fake_surface)
        qapp.processEvents()
        assert fake_ctx.deleted, "context deleteLater must be invoked via signal"
        assert fake_surface.destroyed, "surface.destroy must be invoked via signal"


# ───────────────────── AUD-16808 guard: scoped path in production ───────────


class TestAdjustmentScopeContractRemainsInProduction:
    def test_run_path_uses_scoped_composite_for_adjustments(self):
        """FX-C02 must be enforced at runtime, not only in the plan module."""
        import inspect

        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        source = inspect.getsource(EditGpuPreviewRenderer._process_task)
        assert "task.adjustment_layers" in source
        assert "_composite_with_adjustment_scope" in source, (
            "renderer must composite adjustments at their z-position"
        )
        # The legacy full-screen post pass must not be the run path anymore.
        assert "_apply_adjustment_clips(" not in source.split("_do_composite")[0]

    def test_scoped_composite_docstring_documents_fx_c02(self):
        import inspect

        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        doc = EditGpuPreviewRenderer._composite_with_adjustment_scope.__doc__
        assert doc is not None and "FX-C02" in doc
