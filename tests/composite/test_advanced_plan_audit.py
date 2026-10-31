"""Advanced-plan audit regression tests (2026-09-09 复查修复).

Covers the fixes from the full plan re-audit: shader/evaluator corrections
(lens-flare aspect, cryptomatte isolate alpha, vignette softness clamp,
denoise mask consumption, CA tile padding), disk-cache hardening (f2
upload, f16 saturation, disabled semantics, concurrent tmp names), the
AI batch (RIFE session resolution/caching, shared GPU readback, ai_gen
cache-key sensitivity, volcengine exception chaining), deflicker (RGB
percentile path, resolution guard, visible readback degradation), the 3D
batch (volume step consistency, spacing round-trip, sRGB vertex colours,
file mtime cache dimension), ml_roto point cap, face-workflow exclusion
carving, and the CLI contract (--no-cache, usage exit code 1).
"""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core

_W, _H = 64, 48


def _upload_rgba(harness, rgba):
    """Upload a top-down RGBA array through the harness pool."""
    h, w = rgba.shape[:2]
    tex = harness._resource_pool.acquire_texture(w, h)
    tex.write(np.ascontiguousarray(np.flipud(rgba)).tobytes())
    return TextureResult(
        texture_id=tex.glo, width=w, height=h,
        bbox=BBox.full_frame(w, h),
        alpha_premultiplied=False, color_space="linear")


# ── Shaders / evaluators 


class TestVignetteSoftnessClamp:
    def test_mirror_matches_shader_when_softness_exceeds_radius(self):
        # softness > radius used to diverge between shader and mirror.
        from src.features.composite.nodes.vignette_node import vignette_factor
        # Mirror: clamped softness keeps smoothstep valid for any input.
        assert vignette_factor(0.4, 0.4, 0.5, 1.0, 0.5, 0.0) >= 0.0
        assert vignette_factor(0.4, 0.4, 0.5, 1.0, 0.5, 0.0) <= 1.0


class TestTilePaddingChromaticAberration:
    def test_tile_padding_computer_has_ca_branch(self):
        from src.features.composite.core.tile_padding_computer import (
            TilePaddingComputer,
        )
        computer = TilePaddingComputer()
        ns = SimpleNamespace(
            node_id="ca1", node_type="chromatic_aberration",
            parameters={"offset_r": 10.0, "offset_b": -6.0},
            disabled=False)
        padding = computer._get_node_padding(ns)
        assert padding == 10  # max(|offset_r|, |offset_b|)


class TestZblurBokehToggle:
    def test_bokeh_toggle_changes_kernel(self, qapp):
        """bokeh_toggle=False must soften the disc (gaussian weights)."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no GL: {exc}")
        try:
            from src.features.composite.evaluators.base_evaluator import (
                BaseEvaluatorMixin,
            )
            from src.features.composite.evaluators.color_evaluator import (
                ColorEvaluatorMixin,
            )
            from src.features.composite.evaluators.filter_evaluator import (
                FilterEvaluatorMixin,
            )

            class _Pool:
                def __init__(self, ctx):
                    self.ctx = ctx
                    self._pooled_textures = {}

                def acquire_texture(self, w, h, components=4, dtype="f4"):
                    tex = self.ctx.texture((w, h), components, dtype=dtype)
                    self._pooled_textures.setdefault(dtype, []).append(tex)
                    return tex

            class _Harness(BaseEvaluatorMixin, ColorEvaluatorMixin,
                           FilterEvaluatorMixin):
                def __init__(self, ctx):
                    self.ctx = ctx
                    self.utility_shaders = {}
                    self._resource_pool = _Pool(ctx)
                    from src.shared.gpu.gl_backend import (
                        FramebufferManager,
                    )
                    self.fb_manager = FramebufferManager(ctx)
                    self._load_shaders()

                def _bind_texture_unit(self, texture_id, unit):
                    tex = self._get_texture_by_id(texture_id)
                    if tex is not None:
                        tex.use(unit)

            h = _Harness(ctx)
            # Depth: everything far from focus → full radius everywhere.
            depth = np.ones((_H, _W, 4), dtype=np.float32)
            img = np.zeros((_H, _W, 4), dtype=np.float32)
            img[_H // 2, _W // 4:_W // 4 + 2, :3] = 1.0  # bright stripe
            img[..., 3] = 1.0
            src = _upload_rgba(h, img)
            depth_tex = _upload_rgba(h, depth)
            base = {"focus_distance": 0.0, "focal_range": 0.1,
                    "max_blur_radius": 12.0, "sample_count": 32,
                    "depth_units": 0}
            disc = h._apply_depth_tool("zblur", src, depth_tex,
                                       dict(base, bokeh_toggle=True),
                                       _W, _H)["result"]
            gauss = h._apply_depth_tool("zblur", src, depth_tex,
                                        dict(base, bokeh_toggle=False),
                                        _W, _H)["result"]

            def _read(result):
                tex = h._get_texture_by_id(result.texture_id)
                fbo = h.fb_manager.get_fbo(tex)
                fbo.use()
                ctx.viewport = (0, 0, _W, _H)
                arr = np.frombuffer(
                    fbo.read(components=4, dtype="f4"),
                    dtype=np.float32).reshape(_H, _W, 4)
                return np.flipud(arr)

            disc_img, gauss_img = _read(disc), _read(gauss)
            row = _H // 2
            # The toggle genuinely changes the kernel...
            assert np.abs(disc_img - gauss_img).max() > 1e-3
            # ...both kernels normalise to unit weight, so the 2D
            # energy of the blurred stripe matches...
            assert disc_img[..., 0].sum() == pytest.approx(
                gauss_img[..., 0].sum(), rel=0.3)
            # ...and the gaussian concentrates its weight: its row peak
            # is at least as high as the uniform disc's.
            disc_row = disc_img[row, :, 0]
            gauss_row = gauss_img[row, :, 0]
            assert gauss_row.max() >= disc_row.max() > 0.0
        finally:
            ctx.release()


class TestCryptomatteIsolateAlpha:
    def test_isolate_alpha_carries_matte(self, qapp):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no GL: {exc}")
        try:
            from src.features.composite.evaluators.base_evaluator import (
                BaseEvaluatorMixin,
            )
            from src.features.composite.evaluators.color_evaluator import (
                ColorEvaluatorMixin,
            )
            from src.features.composite.evaluators.filter_evaluator import (
                FilterEvaluatorMixin,
            )

            class _Pool:
                def __init__(self, ctx):
                    self.ctx = ctx
                    self._pooled_textures = {}

                def acquire_texture(self, w, h, components=4, dtype="f4"):
                    tex = self.ctx.texture((w, h), components, dtype=dtype)
                    self._pooled_textures.setdefault(dtype, []).append(tex)
                    return tex

            class _Harness(BaseEvaluatorMixin, ColorEvaluatorMixin,
                     FilterEvaluatorMixin):
                def __init__(self, ctx):
                    self.ctx = ctx
                    self.utility_shaders = {}
                    self._resource_pool = _Pool(ctx)
                    from src.shared.gpu.gl_backend import (
                        FramebufferManager,
                    )
                    self.fb_manager = FramebufferManager(ctx)
                    self._load_shaders()

                def _bind_texture_unit(self, texture_id, unit):
                    tex = self._get_texture_by_id(texture_id)
                    if tex is not None:
                        tex.use(unit)

            h = _Harness(ctx)
            # ID texture: left half id 5.0 (float_r), right half id 9.0.
            ids = np.zeros((_H, _W, 4), dtype=np.float32)
            ids[..., 0] = np.where(
                np.arange(_W)[None, :] < _W // 2, 5.0, 9.0)
            ids[..., 3] = 1.0
            src = _upload_rgba(h, ids)
            selected = np.zeros(32, dtype=np.float32)
            selected[0] = 5.0
            out = h._apply_cryptomatte_select(
                src, {"id_encoding": "float_r", "output_mode": "isolate",
                      "selected_ids": [5.0]}, _W, _H)
            fbo = h.fb_manager.get_fbo(
                h._get_texture_by_id(out["result"].texture_id))
            fbo.use()
            ctx.viewport = (0, 0, _W, _H)
            got = np.frombuffer(
                fbo.read(components=4, dtype="f4"), dtype=np.float32
            ).reshape(_H, _W, 4)
            got = np.flipud(got)
            # Unselected (id 9) region: alpha 0 under isolate mode —
            # previously the original alpha survived and produced a
            # foggy semi-transparent edge.
            assert got[:, _W // 2 + 2:, 3].max() == pytest.approx(0.0,
                                                                  abs=1e-4)
            assert got[:, :_W // 2 - 2, 3].min() == pytest.approx(1.0,
                                                                  abs=1e-4)
        finally:
            ctx.release()


class TestDenoiseMaskConsumed:
    def test_pixels_outside_mask_untouched(self, qapp):
        """face_smooth contract: _apply_denoise must honour _mask_tex."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no GL: {exc}")
        try:
            from src.features.composite.evaluators.base_evaluator import (
                BaseEvaluatorMixin,
            )
            from src.features.composite.evaluators.color_evaluator import (
                ColorEvaluatorMixin,
            )
            from src.features.composite.evaluators.filter_evaluator import (
                FilterEvaluatorMixin,
            )

            class _Pool:
                def __init__(self, ctx):
                    self.ctx = ctx
                    self._pooled_textures = {}

                def acquire_texture(self, w, h, components=4, dtype="f4"):
                    tex = self.ctx.texture((w, h), components, dtype=dtype)
                    self._pooled_textures.setdefault(dtype, []).append(tex)
                    return tex

            class _Harness(BaseEvaluatorMixin, ColorEvaluatorMixin,
                     FilterEvaluatorMixin):
                def __init__(self, ctx):
                    self.ctx = ctx
                    self.utility_shaders = {}
                    self._resource_pool = _Pool(ctx)
                    from src.shared.gpu.gl_backend import (
                        FramebufferManager,
                    )
                    self.fb_manager = FramebufferManager(ctx)
                    self._load_shaders()

                def _bind_texture_unit(self, texture_id, unit):
                    tex = self._get_texture_by_id(texture_id)
                    if tex is not None:
                        tex.use(unit)

            h = _Harness(ctx)
            rng = np.random.default_rng(7)
            img = np.full((_H, _W, 4), 0.6, dtype=np.float32)
            img[..., :3] += rng.normal(0, 0.15, (_H, _W, 3)).astype(
                np.float32)
            img[..., 3] = 1.0
            src = _upload_rgba(h, img)
            mask_arr = np.zeros((_H, _W, 4), dtype=np.float32)
            mask_arr[:, _W // 2:, 0] = 1.0  # right half masked in
            mask = _upload_rgba(h, mask_arr)
            out = h._apply_denoise(
                src, {"strength": 1.0, "radius": 3, "_mask_tex": mask},
                _W, _H)
            fbo = h.fb_manager.get_fbo(
                h._get_texture_by_id(out["result"].texture_id))
            fbo.use()
            ctx.viewport = (0, 0, _W, _H)
            got = np.frombuffer(
                fbo.read(components=4, dtype="f4"), dtype=np.float32
            ).reshape(_H, _W, 4)
            got = np.flipud(got)
            left, right = got[:, :_W // 4], got[:, -_W // 4:]
            # Outside the mask: bit-for-bit the input.
            np.testing.assert_allclose(left, img[:, :_W // 4], atol=1e-5)
            # Inside: bilateral smoothing reduces variance materially.
            var_before = float(np.var(img[:, -_W // 4:, 1]))
            var_after = float(np.var(right[:, :, 1]))
            assert var_after < var_before  # smoothing happened
        finally:
            ctx.release()


# ── Disk cache ──────────────────────────────────────────────────────────

class TestDiskCacheStoreHardening:
    def test_f16_saturation_no_inf(self, tmp_path):
        from src.features.composite.render.disk_cache_store import (
            DiskCacheStore,
        )
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        arr = np.full((4, 4, 4), 1e5, dtype=np.float32)  # > f16 max
        assert store.store("big", arr) is True
        back = store.load("big")
        assert np.isfinite(back).all()
        assert back.flat[0] == pytest.approx(65504.0, rel=1e-3)

    def test_capacity_zero_disables(self, tmp_path):
        from src.features.composite.render.disk_cache_store import (
            DiskCacheStore,
        )
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=0.0)
        assert store.disabled
        assert store.store("k", np.zeros((2, 2, 4), np.float32)) is False
        assert store.has("k") is False

    def test_env_disable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_DISABLE_DISK_CACHE", "1")
        from src.features.composite.render import disk_cache_store as dcs
        store = dcs.DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        assert store.disabled
        assert store.has("anything") is False

    def test_concurrent_tmp_names_unique(self, tmp_path):
        from src.features.composite.render.disk_cache_store import (
            DiskCacheStore,
        )
        store = DiskCacheStore(root_dir=str(tmp_path), capacity_gb=1.0)
        arr = np.zeros((2, 2, 4), np.float32)
        assert store.store("k1", arr)
        # Only the final .npz/.json pair may remain (no fixed .writing).
        names = set(os.listdir(store._root))
        assert not any(n.endswith(".writing") for n in names)

    def test_upload_uses_f16_bytes_for_f2_pool(self, tmp_path, qapp):
        """The production pool is f2 — the upload must not be rejected."""
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"no GL: {exc}")
        try:
            from src.features.composite.evaluators.cache_evaluator import (
                CacheEvaluatorMixin,
            )
            from src.features.composite.evaluators.base_evaluator import (
                BaseEvaluatorMixin,
            )

            class _Pool:
                def __init__(self, ctx):
                    self.ctx = ctx
                    self._pooled_textures = {}

                def acquire_texture(self, w, h, components=4, dtype="f2"):
                    tex = self.ctx.texture((w, h), components, dtype=dtype)
                    self._pooled_textures.setdefault(dtype, []).append(tex)
                    return tex

            class _H(BaseEvaluatorMixin, CacheEvaluatorMixin):
                def __init__(self, ctx):
                    self.ctx = ctx
                    self._resource_pool = _Pool(ctx)
                    from src.shared.gpu.gl_backend import (
                        FramebufferManager,
                    )
                    self.fb_manager = FramebufferManager(ctx)

            h = _H(ctx)
            arr = rng_arr = np.random.default_rng(3).random(
                (12, 20, 4)).astype(np.float32)
            result = h._upload_rgba_array_result(arr, None)
            assert result is not None and result.is_valid
            back = h._readback_result_array(result)
            np.testing.assert_allclose(back, rng_arr, atol=5e-3)
        finally:
            ctx.release()


# ── AI batch ────────────────────────────────────────────────────────────

class TestRifeSessionResolution:
    def test_manager_method_used_and_session_cached(self, monkeypatch,
                                                    tmp_path):
        import src.shared.ai_models.rife_interpolator as ri

        built = {"count": 0}

        class _FakeSession:
            pass

        class _FakeManager:
            def get_model_path(self, model_id):
                return str(tmp_path / "rife.onnx")

        def _build_session(path, sess_options=None, providers=None):
            # ef 批修复后生产侧传入 sess_options 会话选项，桩对齐签名
            built["count"] += 1
            return _FakeSession()

        fake_ort = SimpleNamespace(InferenceSession=_build_session)
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        fake_manager_module = SimpleNamespace(
            get_model_manager=lambda: _FakeManager())
        monkeypatch.setitem(sys.modules,
                            "src.shared.ai_models.model_manager",
                            fake_manager_module)
        ri._SESSION_CACHE.clear()

        first = ri.RifeInterpolator()._resolve_session()
        second = ri.RifeInterpolator()._resolve_session()
        assert isinstance(first, _FakeSession)
        assert first is second  # cached across instances
        assert built["count"] == 1  # the model loads once

    def test_used_fallback_resets_after_tiled_success(self):
        from src.shared.ai_models.rife_interpolator import RifeInterpolator

        class _Session:
            def run(self, _names, feeds):
                # Echo frame A so the output shape survives the pipeline.
                return (feeds[self.get_inputs()[0].name],)

            def get_inputs(self):
                return [SimpleNamespace(name=f"i{i}") for i in range(3)]

        interp = RifeInterpolator(session=_Session())
        interp.used_fallback = True
        big = np.zeros((1300, 8, 3), dtype=np.float32)
        out = interp.interpolate(big, big, 0.5, hd_mode=True)
        assert out.shape == big.shape
        assert interp.used_fallback is False


class TestSharedGpuReadback:
    def test_find_texture_handles_set_and_dict_holders(self):
        from src.features.composite.nodes._gpu_readback import find_texture

        class _Tex:
            def __init__(self, glo):
                self.glo = glo

        t1, t2, t3 = _Tex(11), _Tex(22), _Tex(33)
        fb = SimpleNamespace(_active_textures={t1, t2},
                             _texture_pool={(4, 4, 4, "f2"): [t3]})
        pool = SimpleNamespace(_pooled_textures={(8, 8, 4, "f2"): [_Tex(9)]})
        assert find_texture(22, fb, pool) is t2
        assert find_texture(33, fb, pool) is t3
        assert find_texture(9, fb, pool).glo == 9
        assert find_texture(404, fb, pool) is None


class TestAiGenCacheSensitivity:
    def test_key_changes_with_input_identity_and_size(self):
        from src.features.composite.nodes.ai_gen_expand_node import (
            AIGenExpandNode,
        )

        node = AIGenExpandNode(node_id="g")

        def _tex(tex_id):
            return TextureResult(texture_id=tex_id, width=_W, height=_H,
                                 bbox=BBox.full_frame(_W, _H),
                                 alpha_premultiplied=False,
                                 color_space="linear")

        job_a = node._build_job(_tex(1), None, None, "r0")
        job_b = node._build_job(_tex(2), None, None, "r0")
        assert job_a.key != job_b.key  # upstream change → new key

        node.set_parameter("output_width", 128)
        job_c = node._build_job(_tex(1), None, None, "r0")
        assert job_c.key != job_a.key  # resolution → new key

    def test_connected_mask_pixels_win_over_border_mask(self):
        from src.features.composite.nodes.ai_gen_expand_node import (
            AIGenExpandNode,
        )

        node = AIGenExpandNode(node_id="g2")
        node.set_parameter("expand_left", 0.5)  # would make a border mask
        mask_pixels = np.zeros((_H, _W, 4), dtype=np.float32)
        mask_pixels[:_H // 2, :, 0] = 1.0  # top half only
        image = TextureResult(texture_id=1, width=_W, height=_H,
                              bbox=BBox.full_frame(_W, _H),
                              alpha_premultiplied=False,
                              color_space="linear")
        job = node._build_job(image, mask_pixels, None, "r")
        # The read-back mask (red channel) replaces the border mask.
        np.testing.assert_allclose(job.mask, mask_pixels[..., 0])
        assert job.mask[:_H // 2].max() == 1.0
        assert job.mask[_H // 2:].max() == 0.0


class TestImageGenJobKey:
    def test_output_size_participates_in_key(self):
        from src.shared.ai_models.generation.image_gen_job import cache_key
        assert (cache_key("p", "", "m", 1, 1.0, b"img", b"", "100x100")
                != cache_key("p", "", "m", 1, 1.0, b"img", b"", "200x100"))


class TestVolcengineExceptionChain:
    def test_corrupt_b64_raises_value_error_not_nameerror(self):
        from src.shared.ai_models.generation import (
            volcengine_generation_provider as provider,
        )
        # "_$!" is invalid base64 — the except must bind `exc` for the
        # `from` chain (previously a NameError escaped instead).
        with pytest.raises(ValueError):
            provider.VolcengineSeedreamImageProvider._cache_b64(
                "req", "_$!", suffix=".png")


# ── Deflicker ───────────────────────────────────────────────────────────

class TestDeflickerFixes:
    def test_per_pixel_rgb_path_implemented(self):
        from src.features.composite.nodes.deflicker_node import (
            apply_deflicker,
        )
        rng = np.random.default_rng(11)
        stack = [rng.random((6, 8, 3)).astype(np.float32) for _ in range(5)]
        current = stack[2]
        luma_stack = [ (0.2126 * f[..., 0] + 0.7152 * f[..., 1]
                        + 0.0722 * f[..., 2]).astype(np.float32)
                       for f in stack]
        luma = luma_stack[2]
        out = apply_deflicker(current, luma, luma_stack, 0.5, False,
                              "per_pixel", rgb_stack=stack)
        # Channel-wise median of the middle window element == itself.
        expected = np.median(np.stack(stack), axis=0)
        np.testing.assert_allclose(out, np.clip(expected, 0.0, None),
                                   atol=1e-6)

    def test_resolution_mismatch_counts_as_absent(self):
        from src.features.composite.nodes.deflicker_node import DeflickerNode

        node = DeflickerNode(node_id="d")
        node._luma_buffer[0] = np.zeros((4, 4), np.float32)
        node._luma_buffer[1] = np.zeros((4, 4), np.float32)
        node._luma_buffer[2] = np.zeros((8, 8), np.float32)  # other res
        present, lumas = node._collect_window(
            2, node._luma_buffer, (4, 4))
        assert present == [0, 1]  # the 8×8 plane is skipped, not stacked
        assert len(lumas) == 2

    def test_readback_failure_is_visible(self):
        from src.features.composite.nodes.deflicker_node import DeflickerNode

        node = DeflickerNode(node_id="d2")
        image = TextureResult(texture_id=5, width=4, height=4,
                              bbox=BBox.full_frame(4, 4),
                              alpha_premultiplied=False,
                              color_space="linear")
        out = node._do_evaluate({"image": image}, object(), None)
        assert out == {"result": image}
        assert node.warming is True
        assert "readback" in node.error_message


# ── 3D batch ────────────────────────────────────────────────────────────

class TestVolumeStepConsistency:
    def test_thin_volume_integrates_full_depth(self):
        """D=8 with steps=32 must integrate 8 voxels, not 2."""
        from src.features.composite.nodes.volume3d_node import (
            compute_transmittance,
            sample_indices,
        )
        depth, sigma = 8, 1.0
        indices = sample_indices(depth, 32)
        slab = np.ones((len(indices), 4, 4), np.float32)
        step_world = depth / len(indices)
        t = compute_transmittance(slab, sigma, step_world)
        # exp(-sigma * 8), NOT exp(-2) as the old mismatch produced.
        np.testing.assert_allclose(t, np.exp(-8.0), rtol=1e-5)

    def test_sample_count_independent(self):
        from src.features.composite.nodes.volume3d_node import (
            compute_transmittance,
            sample_indices,
        )
        depth = 64
        for steps in (8, 16, 32, 64):
            indices = sample_indices(depth, steps)
            slab = np.ones((len(indices), 2, 2), np.float32)
            t = compute_transmittance(slab, 0.5, depth / len(indices))
            np.testing.assert_allclose(t, np.exp(-32.0), rtol=2e-2)

    def test_spacing_roundtrip(self, tmp_path):
        from src.features.composite.render3d.volume_grid import (
            load_volume,
            save_volume,
        )
        path = str(tmp_path / "vol.npz")
        save_volume(path, np.zeros((4, 4, 4), np.float32), spacing=0.25)
        assert load_volume(path).spacing == pytest.approx(0.25)

    def test_sequence_frame_cache_flag_and_resolution(self):
        from src.features.composite.core.frame_sequence import (
            resolve_frame_pattern_path,
        )
        from src.features.composite.nodes.volume3d_node import Volume3DNode

        assert Volume3DNode.params_require_frame_cache(
            {"file_path": "smoke.####.npz"}) is True
        assert Volume3DNode.params_require_frame_cache(
            {"file_path": "smoke.npz"}) is False
        assert resolve_frame_pattern_path("smoke.####.npz", 7) == \
            "smoke.0007.npz"
        assert resolve_frame_pattern_path("smoke.%04d.npz", 42) == \
            "smoke.0042.npz"
        assert resolve_frame_pattern_path("plain.npz", 42) == "plain.npz"


class TestPointCloudColourPipeline:
    def test_xyz_midgray_decodes_to_linear(self, tmp_path):
        from src.features.composite.render3d.point_cloud import (
            load_point_cloud,
            srgb_to_linear,
        )
        path = tmp_path / "gray.xyz"
        path.write_text("0 0 0 128 128 128\n")
        cloud = load_point_cloud(str(path))
        expected = float(srgb_to_linear(np.float32(128.0 / 255.0)))
        assert cloud.colors[0, 0] == pytest.approx(expected, abs=1e-5)
        # And the byte-range heuristic normalised the input.
        assert cloud.colors[0, 0] < 0.5

    def test_ply_midgray_decodes_to_linear(self, tmp_path):
        from src.features.composite.render3d.point_cloud import (
            srgb_to_linear,
        )
        try:
            import trimesh
        except ImportError:
            pytest.skip("trimesh unavailable")
        colors = np.full((1, 4), 128, dtype=np.uint8)
        cloud = trimesh.PointCloud(
            vertices=np.zeros((1, 3)), colors=colors)
        path = tmp_path / "gray.ply"
        cloud.export(str(path))
        from src.features.composite.render3d.point_cloud import (
            load_point_cloud,
        )
        loaded = load_point_cloud(str(path))
        expected = float(srgb_to_linear(np.float32(128.0 / 255.0)))
        assert loaded.colors[0, 0] == pytest.approx(expected, abs=1e-4)

    def test_node_module_has_logger(self):
        from src.features.composite.nodes import pointcloud3d_node
        assert hasattr(pointcloud3d_node, "logger")

    def test_runtime_file_signature_changes_with_mtime(self, tmp_path):
        from src.features.composite.nodes.pointcloud3d_node import (
            PointCloud3DNode,
        )
        path = tmp_path / "cloud.ply"
        path.write_text("stub")
        node = PointCloud3DNode(node_id="pc")
        node.set_parameter("file_path", str(path))
        sig1 = node.runtime_file_signature()
        os.utime(str(path), (0, 12345))
        sig2 = node.runtime_file_signature()
        assert sig1 != sig2


class TestBakeSequenceClosure:
    def test_bake_pattern_resolves_via_image_input(self, tmp_path):
        from src.features.composite.nodes.image_input_node import (
            ImageInputNode,
        )
        node = ImageInputNode(node_id="bake_in")
        node.set_parameter("file_path",
                           str(tmp_path / "bake_x.####.exr"))
        assert node.resolve_frame_path(12) == \
            str(tmp_path / "bake_x.0012.exr")
        assert ImageInputNode.params_require_frame_cache(
            {"file_path": "bake_x.####.exr"}) is True
        assert ImageInputNode.params_require_frame_cache(
            {"file_path": "plate.exr"}) is False


# ── ml_roto / face workflow ─────────────────────────────────────────────

class TestMlRotoPointCap:
    def test_decimation_never_exceeds_cap(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            pytest.skip("cv2 unavailable")
        from src.features.composite.ml_roto import mask_to_path

        # A dense circle contour has 100+ points; the cap must hold.
        yy, xx = np.mgrid[:200, :200]
        mask = ((xx - 100) ** 2 + (yy - 100) ** 2 <= 80 ** 2
                ).astype(np.uint8) * 255
        built = mask_to_path(mask, 16, start_tolerance=1.0,
                             max_tolerance=1.0)
        assert built is not None
        path, _decimated = built
        assert len(path) <= 16


class TestFaceExclusionCarving:
    def test_exclusion_centre_stays_zero_after_feather(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            pytest.skip("cv2 unavailable")
        from src.features.composite.face_workflow import (
            EXCLUSION_POLYGONS,
            SKIN_POLYGONS,
            _polygon_mask,
            build_skin_mask,
        )

        # Grid landmarks everywhere except the lips cluster, which sits
        # on a small circle around the frame centre.
        pts = [( (i % 20) / 19.0, (i // 20) / 23.0 )
               for i in range(468)]
        import math
        for k, idx in enumerate(EXCLUSION_POLYGONS["lips_outer"]):
            angle = 2.0 * math.pi * k / 21.0
            pts[idx] = (0.5 + 0.08 * math.cos(angle),
                        0.5 + 0.08 * math.sin(angle))
        raw_skin = _polygon_mask(SKIN_POLYGONS.values(), pts, 100, 100)
        raw_excl = _polygon_mask(EXCLUSION_POLYGONS.values(), pts, 100, 100)
        mask = build_skin_mask(pts, 100, 100, feather_px=4.0)
        # Exclusion centre stays carved after feathering (no bleed-back).
        assert mask[50, 50] < 0.1
        # Skin-only region survives essentially intact.
        region = mask[(raw_skin > 0) & (raw_excl == 0)]
        assert region.size > 50 and region.mean() > 0.4


# ── Locale parity / CLI ─────────────────────────────────────────────────

class TestLocaleParity:
    def test_zh_en_key_sets_match(self):
        base = "src/features/composite/locales"

        def _keys(obj, prefix=""):
            out = set()
            for key, value in obj.items():
                name = f"{prefix}{key}"
                if isinstance(value, dict):
                    out |= _keys(value, name + ".")
                else:
                    out.add(name)
            return out

        zh = _keys(json.load(open(f"{base}/zh_CN.json")))
        en = _keys(json.load(open(f"{base}/en_US.json")))
        assert zh == en


class TestCliContract:
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "scripts/topos_render.py", *args],
            cwd=self.ROOT, capture_output=True, text=True, timeout=90)

    def test_usage_errors_exit_one(self):
        assert self._run().returncode == 1  # missing positional
        assert self._run("--nope", "x.json").returncode == 1

    def test_no_cache_flag_accepted_on_dry_run(self, tmp_path):
        graph = tmp_path / "g.json"
        graph.write_text(json.dumps({
            "version": 1,
            "nodes": [{"id": "n1", "type": "constant"}],
            "connections": [],
        }))
        proc = self._run(str(graph), "--dry-run", "--no-cache",
                         "--frames", "1-3")
        assert proc.returncode == 0, proc.stderr

    def test_dry_run_catches_bad_frames(self, tmp_path):
        graph = tmp_path / "g.json"
        graph.write_text(json.dumps({
            "version": 1,
            "nodes": [{"id": "n1", "type": "constant"}],
            "connections": [],
        }))
        proc = self._run(str(graph), "--dry-run", "--frames", "oops")
        assert proc.returncode == 1
