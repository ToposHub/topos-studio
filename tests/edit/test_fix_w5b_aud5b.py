"""AUD-16600 回归：adjustment_layers 生产方/消费方三元组形状契约。

生产方 ``timeline_preview_renderer``（T-P1-22）按三元组
``(ClipLayerInfo, effects_tuple, preview_grade)`` 下发调整剪辑；消费方
``_build_scoped_composite_work`` 曾按二元组解包，含调整剪辑的 GPU 帧必然
``ValueError: too many values to unpack``，且 scoped 合成的两处
``_apply_adjustment_clips`` 调用点只回传二元组、grade 被静默丢弃。

本文件锁定（无 GL，纯数据 + 桩 self）：

1. 生产方三元组形状不再抛 ValueError，旧二元组仍兼容（grade=None）；
2. work item 携带 grade，z 序（FX-C02 scope）不因三元组改变；
3. ``_composite_with_adjustment_scope`` 把三元组（含 grade）原样透传给
   ``_apply_adjustment_clips``（首个 work item 是调整项、以及循环内调整项
   两条路径）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer

pytestmark = pytest.mark.edit_core


def _adj_layer(track_index: int, opacity: float = 1.0) -> MagicMock:
    """模拟 ClipLayerInfo：scoped 合成只读 track_index/opacity/transform。"""
    layer = MagicMock()
    layer.track_index = track_index
    layer.opacity = opacity
    layer.transform = None  # identity → 走混合分支（不触发 transform 快路径）
    return layer


def _kinds(work: list) -> list:
    return [item[2] for item in work]


# ================================================================
# 1/2. _build_scoped_composite_work：三元组形状 + grade 携带 + z 序
# ================================================================


class TestScopedWorkListThreeTuple:
    def test_producer_three_tuple_no_value_error(self) -> None:
        """生产方形状（timeline_preview_renderer 下发的三元组）必须可解包。"""
        grade = {"exposure": 0.3}
        layer = _adj_layer(1)
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[0],
            adjustment_layers=[(layer, (MagicMock(),), grade)],
        )
        assert _kinds(work) == ["layer", "adjustment"]

    def test_three_tuple_with_none_grade(self) -> None:
        layer = _adj_layer(0)
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[2],
            adjustment_layers=[(layer, (), None)],
        )
        adj = work[0]
        assert adj[2] == "adjustment"
        assert adj[5] is None

    def test_legacy_two_tuple_still_supported(self) -> None:
        """旧二元组（FX-C02 时期形状）兼容：grade 按 None。"""
        layer = _adj_layer(1)
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[0],
            adjustment_layers=[(layer, (MagicMock(),))],
        )
        adj = work[1]
        assert adj[2] == "adjustment"
        assert adj[5] is None

    def test_work_item_carries_grade(self) -> None:
        grade = {"lift": (0.1, 0.0, 0.0)}
        layer = _adj_layer(3)
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[],
            track_texture_indices=[],
            adjustment_layers=[(layer, (), grade)],
        )
        assert len(work) == 1
        adj_idx, tie, kind, adj_layer, effects, carried = work[0]
        assert (adj_idx, tie, kind) == (3, 1, "adjustment")
        assert adj_layer is layer
        assert effects == ()
        assert carried is grade

    def test_scope_order_preserved_with_three_tuples(self) -> None:
        """z 序（FX-C02 scope）：lower(0)/adj(1)/upper(2) → 调整只见下层。"""
        work = EditGpuPreviewRenderer._build_scoped_composite_work(
            track_textures=[(MagicMock(), 1.0), (MagicMock(), 1.0)],
            track_texture_indices=[0, 2],
            adjustment_layers=[(_adj_layer(1), (MagicMock(),), {"exposure": 0.0})],
        )
        assert _kinds(work) == ["layer", "adjustment", "layer"]


# ================================================================
# 3. _composite_with_adjustment_scope：grade 透传到 _apply_adjustment_clips
# ================================================================


class TestScopedCompositeForwardsGrade:
    """桩 self（MagicMock）驱动 scoped 合成主循环，无需 GL 上下文。"""

    def _run_scoped(
        self,
        adjustment_layers: list,
        *,
        track_textures: list,
        track_texture_indices: list,
    ):
        fake = MagicMock()
        # work 列表必须来自真实的纯数据 helper（scope 契约的唯一事实源）。
        fake._build_scoped_composite_work = (
            EditGpuPreviewRenderer._build_scoped_composite_work
        )
        scratch_a, scratch_b = MagicMock(), MagicMock()
        scratch_a_fbo, scratch_b_fbo = MagicMock(), MagicMock()
        fake._ensure_composite_scratch.return_value = (
            scratch_a, scratch_b, scratch_a_fbo, scratch_b_fbo,
        )
        result = EditGpuPreviewRenderer._composite_with_adjustment_scope(
            fake,
            track_textures=track_textures,
            track_texture_indices=track_texture_indices,
            adjustment_layers=adjustment_layers,
            w=4,
            h=4,
            frame=7,
        )
        return fake, result

    def _forwarded_entries(self, fake) -> list:
        entries: list = []
        for call in fake._apply_adjustment_clips.call_args_list:
            entries.extend(call.args[1])
        return entries

    def test_loop_adjustment_forwards_grade(self) -> None:
        """首个 work item 是 layer、调整项在循环内：三元组整体透传。"""
        grade = {"exposure": 0.25}
        layer = _adj_layer(1)
        effects = (MagicMock(),)
        fake, result = self._run_scoped(
            [(layer, effects, grade)],
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[0],
        )
        assert result is fake._composite_tex
        entries = self._forwarded_entries(fake)
        assert entries == [(layer, effects, grade)]

    def test_first_item_adjustment_forwards_grade(self) -> None:
        """首个 work item 是调整项（其下无层）：grade-only 也要透传。"""
        grade = {"contrast": 0.1}
        layer = _adj_layer(0)
        fake, _ = self._run_scoped(
            [(layer, (), grade)],
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[2],
        )
        entries = self._forwarded_entries(fake)
        assert entries == [(layer, (), grade)]

    def test_legacy_two_tuple_forwards_none_grade(self) -> None:
        layer = _adj_layer(1)
        effects = (MagicMock(),)
        fake, _ = self._run_scoped(
            [(layer, effects)],
            track_textures=[(MagicMock(), 1.0)],
            track_texture_indices=[0],
        )
        entries = self._forwarded_entries(fake)
        assert entries == [(layer, effects, None)]
