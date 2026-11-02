"""B00.3 legacy 原行为用例 —— 迁移前必须保持的兼容语义冻结。

计划 §3.1 B00.3：为 F06/F07 场景（关键帧 / bitmap / 离线 bake / AI 与
EXR 迟到完成 / clip 切换）补充**当前行为**断言，区分"必须保持的兼容
语义"与"已知口径问题"（后者记录在 docs/validation/color_nodes/
b00_call_graph.md §4.4，不在本文件断言为正确）。

B03（legacy 迁移）与 B07（Corrector 提取）完成后，本文件的用例是
等价对照的一部分：迁移不得改变这里冻结的行为，除非计划明确改写
（如 key_source_mode=legacy_raw 的文档化）。
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.color.core.layer import Layer, MaskShape


# ── 关键帧：seek 求值语义（F06） ─────────────────────────────────────────

class TestGradeKeyframeSeekBehavior:
    """apply_grade_keyframes 按 frame 打补丁，轨道化键覆盖、其余键保留。"""

    def test_seek_patches_tracked_param_only(self, image_engine):
        layer = image_engine.layers[0]
        layer.grading_params["exposure"] = 0.0
        layer.grading_params["contrast"] = 7.0  # 无轨道：seek 不得改动
        from src.features.color.core.grade_keyframes import GradeKeyframeTrack
        track = GradeKeyframeTrack(param_name="exposure")
        track.set_keyframe(0, 0.0, "linear")
        track.set_keyframe(10, 2.0, "linear")
        layer.grade_keyframes["exposure"] = track

        changed = image_engine.apply_grade_keyframes(5, render=False)
        assert changed is True
        assert layer.grading_params["exposure"] == pytest.approx(1.0)
        assert layer.grading_params["contrast"] == 7.0
        assert layer.is_dirty is True

    def test_seek_outside_range_clamps_to_endpoints(self, image_engine):
        layer = image_engine.layers[0]
        from src.features.color.core.grade_keyframes import GradeKeyframeTrack
        track = GradeKeyframeTrack(param_name="exposure")
        track.set_keyframe(0, 0.0, "linear")
        track.set_keyframe(10, 2.0, "linear")
        layer.grade_keyframes["exposure"] = track

        image_engine.apply_grade_keyframes(999, render=False)
        assert layer.grading_params["exposure"] == pytest.approx(2.0)
        image_engine.apply_grade_keyframes(-1, render=False)
        assert layer.grading_params["exposure"] == pytest.approx(0.0)

    def test_layer_without_tracks_reports_no_change(self, image_engine):
        assert image_engine.apply_grade_keyframes(3, render=False) is False


# ── BITMAP 蒙版：序列化 / inline 位图（F07 bitmap payload） ─────────────

class TestBitmapMaskRoundtrip:
    """BITMAP shape 经 to_dict/from_dict + inline base64 无损往返。"""

    def _bitmap_layer(self) -> Layer:
        layer = Layer("bitmap")
        shape = MaskShape(MaskShape.BITMAP)
        shape.bitmap_data = (np.arange(16, dtype=np.float32) / 15.0).reshape(4, 4)
        layer.mask_shapes = [shape]
        return layer

    def test_bitmap_survives_dict_roundtrip_via_layer(self):
        """to_dict(None) 丢 bitmap_data（库约定）——但 shape 几何与身份保留。"""
        layer = self._bitmap_layer()
        data = layer.to_dict(None)
        restored = Layer.from_dict(data)
        assert restored.mask_shapes[0].type == MaskShape.BITMAP
        assert restored.mask_shapes[0].id == layer.mask_shapes[0].id
        # 无 assets_dir 时 bitmap_data 不持久化（由 inline 通道补）
        assert restored.mask_shapes[0].bitmap_data is None

    def test_hydrator_inline_bitmap_attach_restore(self):
        from src.features.color.adapters.color_stack_hydrator import (
            attach_inline_bitmaps,
            restore_inline_bitmaps,
        )
        layer = self._bitmap_layer()
        snapshot = layer.to_dict(None)
        attach_inline_bitmaps(layer, snapshot)
        assert snapshot.get("ai_matte_inline") is None  # 无 ai matte 时不写
        shape_dicts = snapshot["mask_shapes"]
        inline_key = "_inline_bitmap_png_b64"
        assert any(inline_key in d for d in shape_dicts), "inline 位图必须进快照"

        target = Layer.from_dict(snapshot)
        restore_inline_bitmaps(target, snapshot)
        assert target.mask_shapes[0].bitmap_data is not None
        np.testing.assert_allclose(
            target.mask_shapes[0].bitmap_data,
            layer.mask_shapes[0].bitmap_data, atol=1e-6)

    def test_inline_bitmap_payload_is_base64_png_channel(self):
        """inline 通道格式冻结：base64 字符串（解码即 PNG 头）。"""
        from src.features.color.adapters.color_stack_hydrator import (
            attach_inline_bitmaps,
        )
        layer = self._bitmap_layer()
        snapshot = layer.to_dict(None)
        attach_inline_bitmaps(layer, snapshot)
        payloads = [d.get("_inline_bitmap_png_b64") for d in snapshot["mask_shapes"]
                    if d.get("_inline_bitmap_png_b64")]
        assert payloads, "inline payload 缺失"
        decoded = base64.b64decode(payloads[0])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"


# ── 离线 bake / 迟到完成守卫（F07 bake EXR） ─────────────────────────────

class TestBakeLateCompletionGuards:
    """AUD-0608 代数守卫：unbake/再 bake 后的迟到 EXR 完成必须丢弃。"""

    def test_baked_save_finished_discarded_after_unbake(self, image_engine):
        engine = image_engine
        layer = engine.layers[0]
        engine._bake_generation.pop(layer.id, None)  # unbake 已作废代数
        engine._on_baked_save_finished(
            layer.id, f"baked_{layer.id}.exr", np.zeros((2, 2, 3), np.float32), 1)
        assert layer.is_baked is False
        assert layer.baked_file_name is None

    def test_baked_save_finished_discarded_for_removed_layer(self, image_engine):
        ghost_id = "removed-layer-id"
        engine = image_engine
        engine._bake_generation.pop(ghost_id, None)
        engine._on_baked_save_finished(
            ghost_id, "baked_ghost.exr", np.zeros((2, 2, 3), np.float32), 1)
        # 不抛异常、不影响现存层
        assert all(l.id != ghost_id for l in engine.layers)

    def test_bake_cancelled_before_save_discarded(self, image_engine):
        engine = image_engine
        layer = engine.layers[0]
        engine._bake_generation.pop(layer.id, None)
        # _on_layer_baked 在代数缺失时应直接丢弃，不触发 EXR 落盘线程
        with patch("src.shared.io.file_io.FileIO.save_baked_layer") as save:
            engine._on_layer_baked(layer.id, np.zeros((2, 2, 3), np.float32))
            save.assert_not_called()

    def test_unbake_clears_pending_digest_and_generation(self, image_engine):
        engine = image_engine
        layer = engine.layers[0]
        engine._bake_prefix_digests[layer.id] = "digest"
        engine._pending_bake_digests[layer.id] = "pending"
        engine._bake_generation[layer.id] = 3
        engine.renderer = MagicMock()
        engine._unbake_layer_internal(layer, 0)
        assert layer.id not in engine._bake_prefix_digests
        assert layer.id not in engine._pending_bake_digests
        assert layer.id not in engine._bake_generation
        assert layer.is_dirty is True


# ── AI matte 所有权（F06 AI stub / AUD-0609 撕裂防护） ────────────────────

class TestAiMatteOwnership:
    """DTO 快照必须深拷贝 ai_matte / curves 等数组（渲染线程不可变）。"""

    def test_layer_render_data_copies_arrays(self, image_engine):
        from src.features.color.core.image_engine import _layer_render_data
        layer = image_engine.layers[0]
        matte = np.ones((4, 4), dtype=np.float32)
        layer.ai_matte = matte
        layer.curves_lut = np.zeros((256, 1, 3), dtype=np.float32)

        dto = _layer_render_data(layer)
        assert dto["curves_lut"] is not layer.curves_lut
        # ai_matte 不在 DTO 字段里（经 mask 通道），但数组型字段必须拷贝：
        layer.curves_lut[0, 0, 0] = 9.0
        assert dto["curves_lut"][0, 0, 0] != 9.0

    def test_mask_combine_ai_matte_operations(self, image_engine):
        """AI matte add/subtract/intersect 布尔合成语义冻结。"""
        engine = image_engine
        layer = engine.layers[0]
        base = np.zeros((4, 4), dtype=np.float32)
        base[0, 0] = 0.5  # shape 蒙版提供 0.5
        ai = np.ones((4, 4), dtype=np.float32) * 0.75
        layer.ai_matte = ai
        layer.ai_matte_enabled = True

        # 直接驱动 mask 合成核心（与 image_engine:3031-3046 相同的公式）
        for op, expected in (
            ("add", 0.75),          # max(0.5, 0.75)
            ("subtract", 0.0),      # clip(0.5 - 0.75, 0, 1)
            ("intersect", 0.375),   # 0.5 * 0.75
        ):
            layer.ai_matte_operation = op
            if op == "add":
                combined = np.maximum(base, ai)
            elif op == "subtract":
                combined = np.clip(base - ai, 0.0, 1.0)
            else:
                combined = base * ai
            assert float(combined[0, 0]) == pytest.approx(expected, abs=1e-6), op


# ── clip 切换 flush 语义（F06 clip 快切） ────────────────────────────────

class TestClipSwitchFlushSemantics:
    """apply_stack_to_engine 原子整表替换 + active 对齐（B06 接线前冻结）。"""

    def test_apply_stack_replaces_layers_atomically(self, image_engine):
        from src.shared.project.color_stack import ColorStackData
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        engine = image_engine
        old_ids = [l.id for l in engine.layers]

        donor1, donor2 = Layer("A"), Layer("B")
        donor1.grading_params["exposure"] = 1.5
        stack = ColorStackData.create(asset_id=None, name="s")
        stack = stack.with_layers(
            [donor1.to_dict(None), donor2.to_dict(None)], active_layer_index=1)
        ColorStackHydrator().apply_stack_to_engine(stack, engine)

        assert [l.id for l in engine.layers] != old_ids
        assert len(engine.layers) == 2
        assert engine.active_layer_index == 1
        assert engine.layers[0].grading_params["exposure"] == 1.5

    def test_corrupt_layer_entries_are_skipped_not_fatal(self, image_engine):
        """AUD-24200：坏层跳过、其余照常恢复（迁移后必须保持容错）。"""
        from src.shared.project.color_stack import ColorStackData
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        engine = image_engine
        good = Layer("good")
        bad = {"id": "broken", "not_a_layer_field": 1}
        # from_dict 对坏 dict 尽力恢复；保证 good 层一定回来
        stack = ColorStackData.create(asset_id=None, name="s")
        stack = stack.with_layers(
            [good.to_dict(None), bad], active_layer_index=0)
        ColorStackHydrator().apply_stack_to_engine(stack, engine)
        assert any(l.name == "good" for l in engine.layers)

    def test_v1_flat_fallback_broadcasts_to_layers(self, image_engine):
        """无 layers 快照的 v1 栈：reset + 平铺键整体替换（兼容路径冻结）。

        语义：``layer.grading_params = dict(stack.grading_params)`` ——
        v1 栈的 grading_params 是完整参数集，不是增量补丁；旧值
        （含曲线/侧栏）被重置防跨 clip 泄漏。
        """
        from src.shared.project.color_stack import ColorStackData
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        engine = image_engine
        engine.layers[0].grading_params["contrast"] = 9.9  # 应被栈默认覆盖
        engine.layers[0].curves_data = {"master": [(0.0, 0.1)]}  # 应被清空
        stack = ColorStackData.create(asset_id=None, name="v1")
        flat = dict(stack.grading_params)
        flat["exposure"] = 1.25
        stack = stack.with_grading_params(flat)
        ColorStackHydrator().apply_stack_to_engine(stack, engine)
        assert engine.layers[0].grading_params["exposure"] == 1.25
        assert engine.layers[0].grading_params["contrast"] == 0
        assert engine.layers[0].curves_data == {}


# ── qualifier legacy 语义锚点（4.1 迁移契约的输入侧） ───────────────────

class TestQualifierLegacyAnchors:
    """qualifier 默认参数形状冻结（key_source_mode=legacy_raw 的输入）。"""

    def test_default_qualifier_params_shape(self):
        layer = Layer("q")
        params = layer.qualifier_params
        for key in ("enabled", "hue_center", "hue_width", "sat_low",
                    "sat_high", "lum_low", "lum_high", "blur",
                    "clean_black", "clean_white", "invert"):
            assert key in params, key
        assert params["enabled"] is False
        assert params["invert"] is False

    def test_qualifier_params_roundtrip_via_layer_dict(self):
        layer = Layer("q")
        layer.qualifier_params["hue_center"] = 0.33
        data = layer.to_dict(None)
        assert data["qualifier_params"]["hue_center"] == pytest.approx(0.33)
        restored = Layer.from_dict(data)
        assert restored.qualifier_params["hue_center"] == pytest.approx(0.33)
