"""wd3-g18 批 D 修复验证（REMEDIATION_PLAN 16 条，image_engine/history/grade 域）。

覆盖条目：AUD-0405 / 0411 / 0412 / 0502（已修复核）/ 0510 / 0511 /
0520 / 0601 ~ 0609。GPU 真实路径全部 mock（renderer / GLContextManager /
FileIO），与 test_image_engine_coverage.py 互补，不重复既有断言。
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.color.core.color_stack_render_service import ColorStackRenderService
from src.features.color.core.grade_clipboard import GradeClipboard
from src.features.color.core.grade_keyframes import (
    GradeKeyframe,
    GradeKeyframeTrack,
    evaluate_grade_keyframes,
)
from src.features.color.core.image_engine import (
    ImageEngine,
    _copy_array_like,
    _layer_render_data,
)
from src.features.color.core.layer import Layer, MaskShape
from src.shared.video.render_commands import RenderCommand


# ---------------------------------------------------------------------------
# AUD-0405：embedded 模式透传 lut_path / lut_intensity
# ---------------------------------------------------------------------------

class TestAud0405EmbeddedLutPassthrough:
    def test_embedded_returns_lut_keys(self) -> None:
        snapshot: Dict[str, Any] = {
            "grading_params": {"exposure": 0.5},
            "curves_data": {}, "hsl_curve_data": {},
            "mixer_params": {}, "soft_clip_params": {},
            "lut_path": "/luts/look.cube", "lut_intensity": 0.7,
        }
        resolved = ColorStackRenderService().resolve_grading_params(
            "embedded", embedded_stack=snapshot)
        assert resolved["lut_path"] == "/luts/look.cube"
        assert resolved["lut_intensity"] == 0.7

    def test_embedded_lut_neutral_defaults_when_missing(self) -> None:
        resolved = ColorStackRenderService().resolve_grading_params(
            "embedded", embedded_stack={"grading_params": {}})
        assert resolved["lut_path"] is None
        assert resolved["lut_intensity"] == 1.0

    def test_key_set_parity_with_clip_stack(self) -> None:
        """embedded 与 clip_stack 两个分支的返回键集合一致。"""
        stack = SimpleNamespace_Stack(
            grading_params={}, curves_data={}, hsl_curve_data={},
            mixer_params={}, soft_clip_params={},
            lut_path="/a.cube", lut_intensity=0.5)
        project = SimpleNamespace_Project(color_stacks={"s1": stack})
        clip_resolved = ColorStackRenderService().resolve_grading_params(
            "clip_stack", color_stack_id="s1", project=project)
        embedded_resolved = ColorStackRenderService().resolve_grading_params(
            "embedded", embedded_stack=stack.__dict__)
        assert set(clip_resolved) == set(embedded_resolved)


class SimpleNamespace_Stack:  # noqa: N801（测试替身）
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class SimpleNamespace_Project:  # noqa: N801（测试替身）
    def __init__(self, color_stacks: Dict[str, Any]) -> None:
        self.color_stacks = color_stacks


# ---------------------------------------------------------------------------
# AUD-0411：DenoiseWorker 会话缓存 LRU + 双引擎不互清
# ---------------------------------------------------------------------------

class TestAud0411SessionCache:
    @pytest.fixture(autouse=True)
    def _reset_cache(self) -> None:
        from src.features.color.core.denoise_engine import DenoiseWorker
        with DenoiseWorker._session_cache_lock:
            DenoiseWorker._session_cache.clear()
        yield
        with DenoiseWorker._session_cache_lock:
            DenoiseWorker._session_cache.clear()

    def test_second_model_does_not_clear_first(self) -> None:
        """denoise/deblur 交替（不同 model_path）不再互清缓存。"""
        from src.features.color.core.denoise_engine import DenoiseWorker
        s1, s2 = object(), object()
        DenoiseWorker._cache_session("/models/denoise.onnx", s1)
        DenoiseWorker._cache_session("/models/deblur.onnx", s2)
        with DenoiseWorker._session_cache_lock:
            assert DenoiseWorker._session_cache["/models/denoise.onnx"] is s1
            assert DenoiseWorker._session_cache["/models/deblur.onnx"] is s2

    def test_lru_evicts_oldest_beyond_capacity(self) -> None:
        from src.features.color.core.denoise_engine import DenoiseWorker
        DenoiseWorker._cache_session("m1", object())
        DenoiseWorker._cache_session("m2", object())
        DenoiseWorker._cache_session("m3", object())
        with DenoiseWorker._session_cache_lock:
            keys = list(DenoiseWorker._session_cache.keys())
        assert "m1" not in keys
        assert keys == ["m2", "m3"]

    def test_rehit_refreshes_lru_order(self) -> None:
        from src.features.color.core.denoise_engine import DenoiseWorker
        DenoiseWorker._cache_session("m1", object())
        DenoiseWorker._cache_session("m2", object())
        # m1 命中后应比 m2 更"新"，淘汰的是 m2
        DenoiseWorker._cache_session("m1", object())
        DenoiseWorker._cache_session("m3", object())
        with DenoiseWorker._session_cache_lock:
            keys = list(DenoiseWorker._session_cache.keys())
        assert keys == ["m1", "m3"]


# ---------------------------------------------------------------------------
# AUD-0412：AI 降噪路径 HDR 保留（max-scaling）
# ---------------------------------------------------------------------------

class TestAud0412HdrAiPath:
    @pytest.fixture(autouse=True)
    def _worker(self, qapp, tmp_path, monkeypatch):
        from src.features.color.core.denoise_engine import DenoiseWorker
        model = tmp_path / "model.onnx"
        model.write_bytes(b"fake-onnx")
        img = np.zeros((2, 2, 3), dtype=np.float32)
        img[0, 0] = 2.0  # HDR 高光 > 1.0
        worker = DenoiseWorker(img, "AI", {
            "model_path": str(model), "blend": 1.0, "use_tiling": False})
        monkeypatch.setattr(worker, "_load_onnx_session", lambda _path: object())
        self.captured_inputs: List[np.ndarray] = []

        def _fake_single(inp: np.ndarray, _session: Any) -> np.ndarray:
            self.captured_inputs.append(inp.copy())
            return inp.copy()  # identity 模型

        monkeypatch.setattr(worker, "_process_single_onnx", _fake_single)
        yield worker

    def test_model_input_max_scaled_to_unit(self, _worker) -> None:
        out = _worker._run_ai()
        assert out is not None
        assert self.captured_inputs[0].max() == pytest.approx(1.0)

    def test_hdr_highlights_survive_identity_model(self, _worker) -> None:
        out = _worker._run_ai()
        assert out.max() == pytest.approx(2.0, abs=1e-6)

    def test_blend_uses_unclipped_original(self, _worker, monkeypatch) -> None:
        """blend=0.5 且模型输出为 0 时，结果保留原帧一半的 HDR 值。"""
        monkeypatch.setattr(
            _worker, "_process_single_onnx",
            lambda inp, sess: np.zeros_like(inp))
        _worker.params["blend"] = 0.5
        out = _worker._run_ai()
        # 0.5 * 0(模型) + 0.5 * 2.0(原帧) = 1.0；旧行为会先 clip 到 0.5
        assert out.max() == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# AUD-0502：flip 位 undo/redo 往返（BUG-A2-01 已修复，此处锁定行为）
# ---------------------------------------------------------------------------

class TestAud0502FlipRoundTrip:
    def test_undo_redo_round_trip_restores_flip(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        engine.img_full_original = img
        engine.img_source_full = img.copy()
        engine._flip_h, engine._flip_v = True, False
        engine.history.push_state(img, engine.layers, 0,
                                  flip_h=True, flip_v=False)

        engine._flip_h = False  # 后续操作翻转态变化
        assert engine.undo() is True
        assert engine._flip_h is True  # 快照记录的翻转位被回读恢复

        assert engine.redo() is True
        assert engine._flip_h is False  # 对侧栈记录的"当前"翻转位

    def test_engine_undo_passes_current_flip_to_redo_stack(self,
                                                            image_engine: ImageEngine) -> None:
        engine = image_engine
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        engine.img_full_original = img
        engine.img_source_full = img.copy()
        engine._flip_h = True
        engine.history.push_state(img, engine.layers, 0)
        engine._flip_h = False
        engine.undo()
        # redo 栈中的状态必须含翻转位（undo 时的当前值）
        assert engine.history.redo_stack[0]["flip_h"] is False


# ---------------------------------------------------------------------------
# AUD-0510：粘贴 patch 深拷贝隔离
# ---------------------------------------------------------------------------

def _curve_layer(name: str) -> Layer:
    layer = Layer(name)
    shape = MaskShape(MaskShape.CURVE)
    shape.points = [{"pos": (0.1, 0.1)}, {"pos": (0.2, 0.2)}]
    layer.mask_shapes = [shape]
    return layer


class TestAud0510PasteIsolation:
    def test_broadcast_targets_do_not_share_nested_lists(self) -> None:
        clip = GradeClipboard()
        clip.copy_grade([_curve_layer("src")])
        t1, t2 = _curve_layer("t1"), _curve_layer("t2")
        t1.mask_shapes, t2.mask_shapes = [], []
        clip.paste_onto_layers([t1, t2])

        p1 = t1.mask_shapes[0].points
        p2 = t2.mask_shapes[0].points
        assert p1 is not p2
        p1[0]["pos"] = (0.9, 0.9)
        assert p2[0]["pos"] == (0.1, 0.1)  # 目标间互不影响
        # 剪贴板快照不被目标上的就地编辑污染
        assert clip.layer_patches[0]["mask_shapes"][0]["points"][0]["pos"] == (0.1, 0.1)

    def test_repeat_paste_not_corrupted_by_prior_target_edit(self) -> None:
        clip = GradeClipboard()
        clip.copy_grade([_curve_layer("src")])
        t1 = _curve_layer("t1"); t1.mask_shapes = []
        clip.paste_onto_layers([t1])
        t1.mask_shapes[0].points[0]["pos"] = (0.9, 0.9)  # 就地编辑目标

        t2 = _curve_layer("t2"); t2.mask_shapes = []
        clip.paste_onto_layers([t2])
        assert t2.mask_shapes[0].points[0]["pos"] == (0.1, 0.1)


# ---------------------------------------------------------------------------
# AUD-0511：BITMAP 蒙版位图跨剪贴板携带
# ---------------------------------------------------------------------------

class TestAud0511BitmapCarry:
    def _bitmap_layer(self) -> Layer:
        layer = Layer("src")
        shape = MaskShape(MaskShape.BITMAP)
        shape.bitmap_data = np.linspace(0.0, 1.0, 16, dtype=np.float32).reshape(4, 4)
        layer.mask_shapes = [shape]
        return layer

    def test_bitmap_data_restored_after_paste(self) -> None:
        clip = GradeClipboard()
        clip.copy_grade([self._bitmap_layer()], categories={"windows"})
        dst = Layer("dst")
        clip.paste_onto_layers([dst], categories={"windows"})

        restored = dst.mask_shapes[0]
        assert restored.type == MaskShape.BITMAP
        assert restored.bitmap_data is not None
        np.testing.assert_allclose(restored.bitmap_data, np.linspace(
            0.0, 1.0, 16, dtype=np.float32).reshape(4, 4))

    def test_each_target_gets_independent_bitmap_copy(self) -> None:
        clip = GradeClipboard()
        src = self._bitmap_layer()
        clip.copy_grade([src], categories={"windows"})
        t1, t2 = Layer("t1"), Layer("t2")
        clip.paste_onto_layers([t1, t2], categories={"windows"})

        b1 = t1.mask_shapes[0].bitmap_data
        b2 = t2.mask_shapes[0].bitmap_data
        assert b1 is not b2
        assert b1 is not src.mask_shapes[0].bitmap_data
        b1[0, 0] = 0.123
        assert b2[0, 0] != 0.123

    def test_non_bitmap_windows_paste_unaffected(self) -> None:
        clip = GradeClipboard()
        clip.copy_grade([_curve_layer("src")], categories={"windows"})
        dst = Layer("dst")
        assert clip.paste_onto_layers([dst], categories={"windows"}) == 1
        assert len(dst.mask_shapes) == 1


# ---------------------------------------------------------------------------
# AUD-0520：GradeKeyframe.from_dict 值形态校验
# ---------------------------------------------------------------------------

class TestAud0520KeyframeValidation:
    def test_from_dict_rejects_malformed_scalars(self) -> None:
        for bad in ("abc", {"x": 1}, None, True, [1, "b"]):
            with pytest.raises((TypeError, ValueError)):
                GradeKeyframe.from_dict({"frame": 0, "value": bad})

    def test_from_dict_accepts_valid_values(self) -> None:
        assert GradeKeyframe.from_dict({"frame": 3, "value": 0.5}).value == 0.5
        assert GradeKeyframe.from_dict({"frame": 3, "value": 2}).value == 2.0
        vec = GradeKeyframe.from_dict({"frame": 3, "value": [0.1, 0.2, 0.3]})
        assert vec.value == (0.1, 0.2, 0.3)

    def test_track_drops_wrong_component_count(self) -> None:
        track = GradeKeyframeTrack.from_dict({
            "param_name": "lift",  # vec3
            "keyframes": [
                {"frame": 0, "value": (0.1, 0.2)},        # 2 分量 → 丢弃
                {"frame": 10, "value": (0.1, 0.2, 0.3)},  # 3 分量 → 保留
            ],
        })
        assert [kf.frame for kf in track.keyframes] == [10]

    def test_scalar_track_drops_vector_value(self) -> None:
        track = GradeKeyframeTrack.from_dict({
            "param_name": "exposure",
            "keyframes": [{"frame": 0, "value": (1.0, 2.0)}],
        })
        assert track.keyframes == []

    def test_mask_vec2_track_count_enforced(self) -> None:
        good = GradeKeyframeTrack.from_dict({
            "param_name": "mask.shape1.center",
            "keyframes": [{"frame": 0, "value": (0.5, 0.5)}],
        })
        bad = GradeKeyframeTrack.from_dict({
            "param_name": "mask.shape1.center",
            "keyframes": [{"frame": 0, "value": (0.5, 0.5, 0.5)}],
        })
        assert len(good.keyframes) == 1
        assert bad.keyframes == []

    def test_unknown_param_lenient(self) -> None:
        """未注册参数不校验分量数（向前兼容），但值仍须可 float 化。"""
        track = GradeKeyframeTrack.from_dict({
            "param_name": "future_param",
            "keyframes": [{"frame": 0, "value": (1.0, 2.0, 3.0, 4.0, 5.0)}],
        })
        assert len(track.keyframes) == 1

    def test_malformed_loaded_track_evaluates_without_crash(self) -> None:
        track = GradeKeyframeTrack.from_dict({
            "param_name": "saturation",
            "keyframes": [{"frame": 0, "value": "not-a-number"}],
        })
        result = evaluate_grade_keyframes({"saturation": track}, 5, {"saturation": 0.2})
        assert result["saturation"] == 0.2  # 坏帧被丢弃，回落 base 值


# ---------------------------------------------------------------------------
# AUD-0601：resize_image 同步 img_full_original
# ---------------------------------------------------------------------------

class TestAud0601ResizeUpdatesFullOriginal:
    def test_resize_updates_full_original(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.img_full_original = np.zeros((8, 8, 3), dtype=np.uint8)
        engine.img_source_full = engine.img_full_original.copy()
        engine.resize_image(6, 4)  # (width, height)
        assert engine.img_full_original.shape[:2] == (4, 6)


# ---------------------------------------------------------------------------
# AUD-0602：flip 重建重放 crop_params（裁剪不再被翻转丢弃）
# ---------------------------------------------------------------------------

class TestAud0602CropSurvivesFlip:
    def _prepare(self, engine: ImageEngine) -> None:
        src = np.zeros((10, 10, 3), dtype=np.uint8)
        src[:] = 128
        engine.img_source_full = src
        engine.img_full_original = src.copy()

    def test_crop_displayed_but_source_anchored(self, image_engine: ImageEngine) -> None:
        """裁剪更新显示图；img_source_full 保持原图锚点（连续裁剪为替换语义）。"""
        engine = image_engine
        self._prepare(engine)
        engine.apply_crop({"center": (0.5, 0.5), "size": (0.5, 0.5),
                           "rotation": 0.0})
        assert engine.img_full_original.shape[:2] == (5, 5)
        assert engine.img_source_full.shape[:2] == (10, 10)

    def test_flip_after_crop_keeps_crop(self, image_engine: ImageEngine) -> None:
        """翻转从原图重建时重放 crop_params，裁剪结果不丢失。"""
        import cv2
        engine = image_engine
        self._prepare(engine)
        engine.img_source_full[:] = 0
        engine.img_source_full[:, :5] = 255  # 左白右黑
        engine.img_full_original = engine.img_source_full.copy()
        engine.apply_crop({"center": (0.5, 0.5), "size": (0.5, 0.5),
                           "rotation": 0.0})
        pre_flip = engine.img_full_original.copy()
        engine._flip_h = False
        engine.flip_image(horizontal=True)
        # 裁剪保留（旧行为：从未裁剪 10x10 原图重建）
        assert engine.img_full_original.shape[:2] == (5, 5)
        # 翻转内容正确：等于对裁剪结果做水平镜像
        np.testing.assert_array_equal(
            engine.img_full_original, cv2.flip(pre_flip, 1))

    def test_reset_flip_after_crop_keeps_crop(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        self._prepare(engine)
        engine.apply_crop({"center": (0.5, 0.5), "size": (0.4, 0.6),
                           "rotation": 0.0})
        engine._flip_h = True
        engine.reset_flip()
        assert engine.img_full_original.shape[:2] == (6, 4)  # 裁剪保留

    def test_undo_crop_restores_params_and_display(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        self._prepare(engine)
        engine.apply_crop({"center": (0.5, 0.5), "size": (0.5, 0.5),
                           "rotation": 0.0})
        assert engine.undo() is True
        assert engine.img_full_original.shape[:2] == (10, 10)
        # crop_params 一并回滚到裁剪前（全幅默认），随后的 flip 不会
        # 用已撤销的裁剪参数重建
        assert engine.crop_params.get("rect") == (0.0, 0.0, 1.0, 1.0)
        engine.flip_image(horizontal=True)
        assert engine.img_full_original.shape[:2] == (10, 10)


# ---------------------------------------------------------------------------
# AUD-0603 / AUD-0604：render_cached_image finally 恢复 + HDR clip
# ---------------------------------------------------------------------------

def _headless_engine(monkeypatch, render_impl) -> ImageEngine:
    engine = ImageEngine.__new__(ImageEngine)
    monkeypatch.setattr(
        "src.features.color.core.image_engine.QOpenGLContext",
        MagicMock(**{"currentContext.return_value": MagicMock()}))
    monkeypatch.setattr(engine, "render_full_resolution", render_impl)
    return engine


class TestAud0603CachedRenderRestore:
    def test_full_original_restored_after_render_exception(self, monkeypatch) -> None:
        orig = np.zeros((4, 4, 3), dtype=np.uint8)

        def boom() -> None:
            raise RuntimeError("render failed")

        engine = _headless_engine(monkeypatch, boom)
        engine.img_full_original = orig
        engine.img_high = np.ones((2, 2, 3), dtype=np.float32)

        assert engine.render_cached_image() is None
        assert engine.img_full_original is orig  # 不滞留低分辨率代理

    def test_full_original_restored_on_success(self, monkeypatch) -> None:
        orig = np.zeros((4, 4, 3), dtype=np.uint8)
        engine = _headless_engine(
            monkeypatch, lambda: np.zeros((2, 2, 3), dtype=np.uint8))
        engine.img_full_original = orig
        engine.img_high = np.ones((2, 2, 3), dtype=np.float32)
        assert engine.render_cached_image() is not None
        assert engine.img_full_original is orig


class TestAud0604HdrQuantization:
    def test_hdr_clipped_not_wrapped(self, monkeypatch) -> None:
        captured: Dict[str, np.ndarray] = {}

        def capture() -> np.ndarray:
            captured["swapped"] = engine.img_full_original
            return np.zeros((2, 2, 3), dtype=np.uint8)

        engine = _headless_engine(monkeypatch, capture)
        engine.img_full_original = np.zeros((4, 4, 3), dtype=np.uint8)
        engine.img_high = np.full((2, 2, 3), 2.0, dtype=np.float32)  # 2.0*255=510
        engine.render_cached_image()
        swapped = captured["swapped"]
        assert swapped.dtype == np.uint8
        # 旧行为 510 % 256 = 254（回绕伪影）；clip 后应为 255
        assert swapped.max() == 255


# ---------------------------------------------------------------------------
# AUD-0605：bake_layer 全分辨率数据按 dtype 归一化
# ---------------------------------------------------------------------------

class TestAud0605BakeNormalization:
    def _bake_payload(self, engine: ImageEngine) -> Dict[str, Any]:
        calls = [c.args[1] for c in engine.renderer.submit.call_args_list
                 if c.args and c.args[0] == RenderCommand.BAKE_LAYER]
        assert calls, "BAKE_LAYER 未提交"
        return calls[0]

    def test_uint16_normalized_by_65535(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.full((2, 2, 3), 65535, dtype=np.uint16)
        engine.bake_layer(0)
        full = self._bake_payload(engine)["full_res_image"]
        assert full.dtype == np.float32
        assert full.max() == pytest.approx(1.0)  # 旧行为 ≈257（过曝）

    def test_uint8_normalized_by_255(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.full((2, 2, 3), 255, dtype=np.uint8)
        engine.bake_layer(0)
        full = self._bake_payload(engine)["full_res_image"]
        assert full.max() == pytest.approx(1.0)

    def test_float_hdr_passed_through(self, image_engine: ImageEngine) -> None:
        engine = image_engine
        engine.renderer = MagicMock()
        engine.img_full_original = np.full((2, 2, 3), 2.0, dtype=np.float32)
        engine.bake_layer(0)
        full = self._bake_payload(engine)["full_res_image"]
        assert full.max() == pytest.approx(2.0)  # 不除以 255


# ---------------------------------------------------------------------------
# AUD-0606：request_sam_decode 发射快照副本
# ---------------------------------------------------------------------------

class TestAud0606SamDecodeSnapshot:
    def test_emits_copy_not_live_list(self, image_engine: ImageEngine) -> None:
        emitted: List[tuple] = []
        image_engine.sig_process_sam_decode.connect(
            lambda pts, labels: emitted.append((pts, labels)))
        image_engine.request_sam_decode(0.1, 0.2, 1)
        image_engine.request_sam_decode(0.3, 0.4, 0)

        pts0, _ = emitted[0]
        assert len(pts0) == 1  # 发射时快照，不随后续 append 增长
        pts0.append(("polluted",))
        assert len(image_engine._sam_points) == 2  # 引擎状态不受接收方污染


# ---------------------------------------------------------------------------
# AUD-0607：_do_render_full_res 异常路径资源登记（结构性断言）
# ---------------------------------------------------------------------------

class TestAud0607FullResCleanupStructure:
    def test_textures_registered_immediately(self) -> None:
        src = inspect.getsource(ImageEngine._do_render_full_res)
        # 禁止中途整体重绑非空 garbage 清单（会丢弃此前登记项）；
        # 顶部的空清单初始化 `texture_garbage = []` 是允许的
        assert re.search(r"texture_garbage[ \t]*=[ \t]*\[[^\]]", src) is None
        # 关键资源创建后立即登记
        assert "texture_garbage.append(input_tex)" in src
        assert "texture_garbage.append(raw_dev_tex)" in src
        assert "texture_garbage.append(ping_tex)" in src
        assert "texture_garbage.append(dummy_matte)" in src
        # 本地 program 统一登记并在 finally 释放
        assert "program_garbage.append(prog_raw)" in src
        assert "program_garbage.append(prog_pass)" in src
        assert "for prog in program_garbage" in src

    def test_no_bare_except_in_cleanup(self) -> None:
        src = inspect.getsource(ImageEngine._do_render_full_res)
        assert "except:" not in src

    def test_mask_release_in_finally(self) -> None:
        src = inspect.getsource(ImageEngine._do_render_full_res)
        assert "finally:" in src
        assert "self.fb_manager.release_temp_texture(mask_tex)" in src


# ---------------------------------------------------------------------------
# AUD-0608：bake 代数守卫（unbake 后迟到完成信号不复活）
# ---------------------------------------------------------------------------

class TestAud0608BakeGeneration:
    def _bake(self, engine: ImageEngine):
        engine.renderer = MagicMock()
        engine.img_full_original = np.zeros((2, 2, 3), dtype=np.uint8)
        engine.bake_layer(0)
        layer = engine.layers[0]
        gen = engine._bake_generation[layer.id]
        return layer, gen

    def test_unbake_cancels_pending_bake(self, image_engine: ImageEngine,
                                         monkeypatch) -> None:
        deleted: List[str] = []
        monkeypatch.setattr(
            "src.shared.io.file_io.FileIO.delete_baked_layer",
            staticmethod(lambda name: deleted.append(name)))
        layer, gen = self._bake(image_engine)

        assert image_engine.unbake_layer(0) is True
        assert layer.is_baked is False
        assert layer.id not in image_engine._bake_generation
        assert layer.id not in image_engine._pending_bake_digests

        # EXR 异步保存完成后迟到信号到达（携带旧代数）
        image_engine._on_baked_save_finished(
            layer.id, "baked_stale.exr", np.zeros((2, 2, 3), np.float32), gen)
        assert layer.is_baked is False            # 不复活
        assert layer.baked_file_name is None
        assert layer.id not in image_engine._bake_prefix_digests  # 无台账复活
        assert "baked_stale.exr" in deleted       # 孤儿 EXR 被清理

    def test_normal_completion_registers_digest(self, image_engine: ImageEngine) -> None:
        layer, gen = self._bake(image_engine)
        digest = image_engine._pending_bake_digests[layer.id]

        image_engine._on_baked_save_finished(
            layer.id, f"baked_{layer.id}.exr",
            np.zeros((2, 2, 3), np.float32), gen)
        assert layer.is_baked is True
        assert image_engine._bake_prefix_digests[layer.id] == digest
        assert layer.id not in image_engine._bake_generation

    def test_superseded_generation_discarded(self, image_engine: ImageEngine) -> None:
        layer, gen1 = self._bake(image_engine)
        image_engine.bake_layer(1 % len(image_engine.layers))  # 同层再 bake → gen2
        gen2 = image_engine._bake_generation[layer.id]
        assert gen2 == gen1 + 1

        # 旧代数的完成信号作废
        image_engine._on_baked_save_finished(
            layer.id, "baked_old.exr", np.zeros((2, 2, 3), np.float32), gen1)
        assert layer.id not in image_engine._bake_prefix_digests


# ---------------------------------------------------------------------------
# AUD-0609：渲染线程 DTO ndarray 隔离
# ---------------------------------------------------------------------------

class TestAud0609DtoArrayIsolation:
    def _layer(self) -> Layer:
        layer = Layer("t")
        layer.curves_lut = np.zeros((256, 3), dtype=np.float32)
        layer.hsl_curve_lut = np.zeros((6, 256), dtype=np.float32)
        layer.warper_params = np.zeros((4, 4, 2), dtype=np.float32)
        layer.ai_matte = np.zeros((4, 4), dtype=np.float32)
        return layer

    def test_copy_array_like_helper(self) -> None:
        arr = np.zeros(3, dtype=np.float32)
        assert _copy_array_like(arr) is not arr
        lst = [1.0, 2.0]
        assert _copy_array_like(lst) is not lst
        assert _copy_array_like(None) is None
        assert _copy_array_like("x") == "x"

    def test_layer_render_data_copies_ndarrays(self) -> None:
        layer = self._layer()
        dto = _layer_render_data(layer)
        for key in ("curves_lut", "hsl_curve_lut", "warper_params"):
            value = dto[key]
            assert value is not getattr(layer, key)
            if isinstance(value, np.ndarray):
                value[0, 0] = 9.0
        assert layer.curves_lut[0, 0] == 0.0
        assert layer.warper_params[0, 0, 0] == 0.0

    def test_create_layer_snapshot_copies_ai_matte(self) -> None:
        engine = ImageEngine.__new__(ImageEngine)
        layer = self._layer()
        snapshot = engine._create_layer_snapshot(layer)
        assert snapshot["ai_matte"] is not layer.ai_matte
        snapshot["ai_matte"][0, 0] = 9.0
        assert layer.ai_matte[0, 0] == 0.0
        assert snapshot["curves_lut"] is not layer.curves_lut
        assert snapshot["warper_params"] is not layer.warper_params
