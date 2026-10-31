"""
Timeline Occlusion Culling 单元测试。

对应计划：docs/timeline_occlusion_culling_optimization_plan_2026-09-19.md §6.1
覆盖全部安全/不安全条件的纯逻辑单测（fake layer，不请求解码帧）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

import pytest

from src.features.edit.core import timeline_occlusion as oc
from src.features.edit.core.timeline_occlusion import (
    LayerOcclusionFacts,
    analyze_visible_layers,
    compute_visible_region_plan,
    crop_blocks_occlusion,
    normalize_blend_mode,
    rect_covers_canvas,
    source_alpha_known_opaque,
    transformed_coverage_rect,
)


# ── 测试替身 ──


@dataclass(frozen=True)
class FakeLayer:
    """满足 ClipLayerInfo 遮挡分析契约的最小图层替身。"""

    clip_id: str
    track_index: int
    opacity: float = 1.0
    # 8 元组：tx, ty, sx, sy, rot, pivot_mode, pivot_x, pivot_y
    transform: tuple = (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
    # 5 元组：left, right, top, bottom, feather
    crop: tuple = (0.0, 0.0, 0.0, 0.0, 0.0)
    is_in_transition: bool = False
    composite_id: Optional[str] = None
    asset_id: str = ""


#: 普通无 Alpha 视频层的事实（可作遮挡层的唯一组合）。
VIDEO_FACTS = LayerOcclusionFacts(
    asset_kind="video",
    source_alpha_known_opaque=True,
    blend_mode="normal",
    has_effects=False,
)


def facts_of_static(facts: LayerOcclusionFacts):
    def _facts_of(layer):
        return facts
    return _facts_of


def bottom_to_top(*layers: FakeLayer) -> Tuple[FakeLayer, ...]:
    """按 track_index 从小到大排列（时间线 layer list 语义）。"""
    return tuple(sorted(layers, key=lambda l: l.track_index))


CANVAS = (1920, 1080)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境变量与 telemetry。"""
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_CULL", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_RECT", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_ROI", raising=False)
    monkeypatch.delenv("TOPOS_TIMELINE_OCCLUSION_DEBUG", raising=False)
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()
    yield
    oc.reset_env_cache()
    oc.get_occlusion_telemetry().reset()


# ── §6.1 用例表 ──


class TestFullFrameCulling:
    def test_two_tracks_upper_full_opaque_culls_lower(self):
        lower = FakeLayer("v1", 0)
        upper = FakeLayer("v2", 1)
        decision = analyze_visible_layers(
            bottom_to_top(lower, upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.reason == "full_frame_opaque_occluder"
        # 只有遮挡层及其上方参与渲染；下方全部被裁。
        assert decision.render_layers == (upper,)
        assert decision.culled_layers == (lower,)
        assert decision.occluder_clip_id == "v2"

    def test_three_tracks_keeps_upper_and_occluder_only(self):
        v1 = FakeLayer("v1", 0)
        v2 = FakeLayer("v2", 1)
        v3 = FakeLayer("v3", 2)
        decision = analyze_visible_layers(
            bottom_to_top(v1, v2, v3), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.render_layers == (v3,)
        assert decision.culled_layers == (v1, v2)

    def test_single_layer_no_culling(self):
        solo = FakeLayer("v1", 0)
        decision = analyze_visible_layers(
            (solo,), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.render_layers == (solo,)
        assert decision.reason == "no_occluder_candidate"

    def test_empty_layers(self):
        decision = analyze_visible_layers(
            (), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.render_layers == ()
        assert not decision.culled


class TestFailClosedConditions:
    """计划 §3.2：任何不确定都必须保留下层。"""

    def _decide(self, upper: FakeLayer, lower=None):
        lower = lower or FakeLayer("v1", 0)
        decision = analyze_visible_layers(
            bottom_to_top(lower, upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        return decision, lower

    def test_scale_down_keeps_lower(self):
        decision, lower = self._decide(
            FakeLayer("v2", 1, transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0)),
        )
        assert decision.render_layers == (lower, decision.render_layers[-1])
        assert decision.culled_layers == ()
        assert not decision.culled

    def test_translate_keeps_lower(self):
        decision, lower = self._decide(
            FakeLayer("v2", 1, transform=(100.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)),
        )
        assert not decision.culled

    def test_rotation_keeps_lower(self):
        decision, lower = self._decide(
            FakeLayer("v2", 1, transform=(0.0, 0.0, 1.0, 1.0, 30.0, "center", 0.0, 0.0)),
        )
        assert not decision.culled

    def test_custom_pivot_keeps_lower(self):
        decision, lower = self._decide(
            FakeLayer("v2", 1, transform=(0.0, 0.0, 1.0, 1.0, 0.0, "custom", 100.0, 50.0)),
        )
        assert not decision.culled

    def test_opacity_below_one_keeps_lower(self):
        decision, _ = self._decide(FakeLayer("v2", 1, opacity=0.999))
        assert not decision.culled
        assert decision.reason == "no_occluder_found"

    def test_zero_opacity_upper_still_no_cull_but_skipped(self):
        # 零透明上层不是遮挡证据，但也无害：继续扫描，找不到遮挡层。
        decision, lower = self._decide(FakeLayer("v2", 1, opacity=0.0))
        assert decision.render_layers == (lower, FakeLayer("v2", 1, opacity=0.0))
        assert not decision.culled

    def test_nan_transform_keeps_lower(self):
        decision, _ = self._decide(
            FakeLayer("v2", 1, transform=(float("nan"), 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)),
        )
        assert not decision.culled

    def test_malformed_transform_keeps_lower(self):
        decision, _ = self._decide(FakeLayer("v2", 1, transform=(0.0, 0.0, 1.0)))
        assert not decision.culled

    def test_crop_keeps_lower(self):
        decision, _ = self._decide(
            FakeLayer("v2", 1, crop=(0.1, 0.1, 0.0, 0.0, 0.0)),
        )
        assert not decision.culled

    def test_feather_keeps_lower(self):
        decision, _ = self._decide(
            FakeLayer("v2", 1, crop=(0.0, 0.0, 0.0, 0.0, 0.25)),
        )
        assert not decision.culled

    def test_transition_layer_keeps_lower(self):
        decision, _ = self._decide(FakeLayer("v2", 1, is_in_transition=True))
        assert not decision.culled

    def test_frame_transitions_keep_all(self):
        lower = FakeLayer("v1", 0)
        upper = FakeLayer("v2", 1)
        decision = analyze_visible_layers(
            bottom_to_top(lower, upper), ("fake_transition",), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.render_layers == (lower, upper)
        assert decision.reason == "transition_present"

    def test_composite_clip_cannot_occlude(self):
        decision, _ = self._decide(FakeLayer("v2", 1, composite_id="graph-1"))
        assert not decision.culled

    def test_adjustment_asset_cannot_occlude(self):
        facts = LayerOcclusionFacts(asset_kind="adjustment", source_alpha_known_opaque=True)
        decision, _ = self._decide(FakeLayer("adj", 1), lower=FakeLayer("v1", 0))
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("adj", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(facts), record=False,
        )
        assert not decision.culled

    def test_blend_mode_multiply_keeps_lower(self):
        facts = LayerOcclusionFacts(
            asset_kind="video", source_alpha_known_opaque=True,
            blend_mode="multiply",
        )
        decision, _ = self._decide(FakeLayer("v2", 1))
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(facts), record=False,
        )
        assert not decision.culled

    def test_effects_keeps_lower(self):
        facts = LayerOcclusionFacts(
            asset_kind="video", source_alpha_known_opaque=True,
            blend_mode="normal", has_effects=True,
        )
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(facts), record=False,
        )
        assert not decision.culled

    def test_unknown_alpha_keeps_lower(self):
        facts = LayerOcclusionFacts(asset_kind="video", source_alpha_known_opaque=False)
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(facts), record=False,
        )
        assert not decision.culled

    def test_solid_asset_cannot_occlude(self):
        facts = LayerOcclusionFacts(asset_kind="solid", source_alpha_known_opaque=True)
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("solid", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(facts), record=False,
        )
        assert not decision.culled

    def test_missing_facts_keeps_lower(self):
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=lambda layer: None, record=False,
        )
        assert not decision.culled

    def test_facts_provider_exception_keeps_lower(self):
        def boom(layer):
            raise RuntimeError("boom")
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=boom, record=False,
        )
        assert not decision.culled
        assert decision.reason == "analysis_error"

    def test_invalid_opacity_type_keeps_lower(self):
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1, opacity="bad")), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert not decision.culled

    def test_partial_occluders_then_opaque_still_culls(self):
        # 中间层不能遮挡（缩小），但最上层全屏不透明仍可裁掉下面两层。
        v1 = FakeLayer("v1", 0)
        v2 = FakeLayer("v2", 1, transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0))
        v3 = FakeLayer("v3", 2)
        decision = analyze_visible_layers(
            bottom_to_top(v1, v2, v3), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.render_layers == (v3,)
        assert decision.culled_layers == (v1, v2)


class TestSwitches:
    def test_cull_disabled_keeps_all(self):
        oc._ENV_CACHE["TOPOS_TIMELINE_OCCLUSION_CULL"] = False
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert decision.render_layers == (
            FakeLayer("v1", 0), FakeLayer("v2", 1),
        )
        assert decision.reason == "cull_disabled"

    def test_explicit_enabled_overrides_env(self):
        oc._ENV_CACHE["TOPOS_TIMELINE_OCCLUSION_CULL"] = False
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False, enabled=True,
        )
        assert decision.culled

    def test_rect_disabled_by_default(self):
        assert oc.rect_coverage_enabled() is False
        decision = analyze_visible_layers(
            bottom_to_top(
                FakeLayer("v1", 0),
                FakeLayer("v2", 1, transform=(0.0, 0.0, 2.0, 2.0, 0.0, "center", 0.0, 0.0)),
            ), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert not decision.culled
        assert decision.reason == "no_occluder_found"


# ── O5：矩形覆盖分析 ──


class TestRectCoverage:
    def _zoomed_upper(self):
        return FakeLayer(
            "v2", 1,
            transform=(0.0, 0.0, 2.0, 2.0, 0.0, "center", 0.0, 0.0),
        )

    def test_zoom_still_covering_culls_when_rect_enabled(self):
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), self._zoomed_upper()), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False, allow_rect=True,
        )
        assert decision.culled
        assert decision.reason == "rect_full_cover_opaque_occluder"

    def test_zoom_offset_not_covering_keeps_lower(self):
        # 平移后的层露出画布左缘：下层必须保留。
        upper = FakeLayer(
            "v2", 1,
            transform=(100.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
        )
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False, allow_rect=True,
        )
        assert not decision.culled
        assert decision.reason == "no_occluder_found"

    def test_rot45_never_culls_even_with_rect(self):
        upper = FakeLayer(
            "v2", 1,
            transform=(0.0, 0.0, 3.0, 3.0, 45.0, "center", 0.0, 0.0),
        )
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False, allow_rect=True,
        )
        assert not decision.culled

    def test_rot90_zoom_culls_with_rect(self):
        upper = FakeLayer(
            "v2", 1,
            transform=(0.0, 0.0, 2.0, 2.0, 90.0, "center", 0.0, 0.0),
        )
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False, allow_rect=True,
        )
        assert decision.culled

    def test_odd_canvas_inflation(self):
        # 奇数尺寸 + 恰好 1:1 放大：1 像素安全膨胀要求严格超出画布。
        rect = transformed_coverage_rect(
            (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0), 1921, 1081,
        )
        assert rect == (0.0, 0.0, 1921.0, 1081.0)
        assert not rect_covers_canvas(rect, 1921, 1081)  # 等大不满足膨胀
        rect2x = transformed_coverage_rect(
            (0.0, 0.0, 2.0, 2.0, 0.0, "center", 0.0, 0.0), 1921, 1081,
        )
        assert rect_covers_canvas(rect2x, 1921, 1081)

    def test_identity_takes_full_frame_path_not_rect(self):
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False, allow_rect=True,
        )
        assert decision.reason == "full_frame_opaque_occluder"


# ── 源 Alpha 判定 ──


class TestSourceAlpha:
    @pytest.mark.parametrize("fmt,expected", [
        ("yuv420p", True),
        ("yuv422p10le", True),
        ("yuv444p10le", True),
        ("bgra", False),
        ("rgba", False),
        ("yuva444p10le", False),      # ProRes 4444（注册表缺口，名称识别）
        ("topos_yuva422p10a8", False),  # Topos 4444
        ("topos_yuva444p10a10", False),
        ("prores_4444", False),        # 未知名称 → 保守
        ("", False),
        (None, False),
    ])
    def test_alpha_classification(self, fmt, expected):
        assert source_alpha_known_opaque(fmt) is expected


# ── 归一化与几何辅助 ──


class TestHelpers:
    def test_normalize_blend_mode(self):
        assert normalize_blend_mode("NORMAL") == "normal"
        assert normalize_blend_mode(None) == "normal"

        class FakeEnum:
            value = "Screen"
        assert normalize_blend_mode(FakeEnum()) == "screen"

    def test_crop_blocks(self):
        assert not crop_blocks_occlusion((0.0, 0.0, 0.0, 0.0, 0.0))
        assert not crop_blocks_occlusion(None)
        assert crop_blocks_occlusion((0.05, 0.0, 0.0, 0.0, 0.0))
        assert crop_blocks_occlusion((0.0, 0.0, 0.0, 0.0, 0.1))
        # 非法 crop 保守视为有裁切
        assert crop_blocks_occlusion(("x", "y", "z", "w", "f"))

    def test_transform_coverage_identity(self):
        rect = transformed_coverage_rect(
            (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0), 1920, 1080,
        )
        assert rect == (0.0, 0.0, 1920.0, 1080.0)

    def test_transform_coverage_translate_shifts(self):
        rect = transformed_coverage_rect(
            (100.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0), 1920, 1080,
        )
        assert rect == (100.0, 0.0, 1920.0, 1080.0)

    def test_transform_coverage_invalid_returns_none(self):
        assert transformed_coverage_rect(None, 1920, 1080) is None
        assert transformed_coverage_rect((0.0,), 1920, 1080) is None
        assert transformed_coverage_rect(
            (0.0, 0.0, 0.0, 0.0, 0.0, "center", 0.0, 0.0), 1920, 1080,
        ) is None  # 零缩放退化


# ── O0：telemetry ──


class TestTelemetry:
    def test_record_and_snapshot(self):
        lower = FakeLayer("v1", 0)
        upper = FakeLayer("v2", 1)
        analyze_visible_layers(
            bottom_to_top(lower, upper), (), 7, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), mode="export",
        )
        analyze_visible_layers(
            bottom_to_top(lower, upper), (), 8, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), mode="preview",
        )
        snap = oc.get_occlusion_telemetry().snapshot()
        assert snap["frames"] == 2
        assert snap["total_layers"] == 4
        assert snap["render_layers"] == 2  # 每帧只剩遮挡层
        assert snap["culled_layers"] == 2
        assert snap["culled_ratio"] == 0.5
        assert snap["reasons"] == {"full_frame_opaque_occluder": 2}
        assert snap["modes"] == {"export": 1, "preview": 1}
        assert snap["analysis_ms_max"] >= 0.0

    def test_reset(self):
        analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS),
        )
        oc.get_occlusion_telemetry().reset()
        assert oc.get_occlusion_telemetry().snapshot()["frames"] == 0


# ── O6：ROI 数据契约 ──


class TestRegionPlan:
    def test_upper_opaque_lower_fully_covered(self):
        lower = FakeLayer("v1", 0)
        upper = FakeLayer("v2", 1)
        plan = compute_visible_region_plan(
            bottom_to_top(lower, upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS),
        )
        assert plan.region_for("v1").complete_cover is True
        assert plan.region_for("v2").complete_cover is False
        assert plan.region_for("v2").region is None  # 整画布

    def test_partial_cover_gives_rect_region(self):
        upper = FakeLayer(
            "v2", 1,
            transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0),
        )
        plan = compute_visible_region_plan(
            bottom_to_top(FakeLayer("v1", 0), upper), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS),
        )
        # 缩小 0.5 的上层不是"确定性不透明覆盖"（unknown kind），
        # 下层保守保持整画布。
        assert plan.region_for("v1").region is None

    def test_output_order_follows_input(self):
        v1 = FakeLayer("v1", 0)
        v2 = FakeLayer("v2", 1)
        v3 = FakeLayer("v3", 2)
        plan = compute_visible_region_plan(
            bottom_to_top(v1, v2, v3), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS),
        )
        assert [r.clip_id for r in plan.regions] == ["v1", "v2", "v3"]

    def test_transitions_make_everything_full_canvas(self):
        plan = compute_visible_region_plan(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)),
            ("t",), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS),
        )
        # 转场时 layer_coverage 全部 unknown → 无人贡献覆盖 → 整画布
        assert all(r.region is None and not r.complete_cover for r in plan.regions)


# ── O7：效果链遮挡安全能力声明 ──


class FakeEffect:
    def __init__(self, effect_type, is_enabled=True):
        self.effect_type = effect_type
        self.is_enabled = is_enabled


class TestBackgroundIndependent:
    def test_empty_chain_is_not_declared(self):
        assert oc.effects_background_independent(()) is False

    def test_builtin_effects_not_declared(self):
        # 内置效果（blur 等）默认不声明 background_independent。
        assert oc.effects_background_independent(
            (FakeEffect("blur"),)) is False

    def test_unknown_effect_type_fail_closed(self):
        assert oc.effects_background_independent(
            (FakeEffect("plugin:does_not_exist_xyz"),)) is False

    def test_declared_plugin_effect_allows(self, monkeypatch):
        from src.features.shared.effects.effect_spec import EffectSpec

        declared = EffectSpec(
            effect_type="plugin:safe_fx",
            name="safe",
            category="plugin",
            param_defs=(),
            shader_kind="single_pass",
            shader_sources=("void main(){ gl_FragColor = vec4(1.0); }",),
            dispatch_key="plugin:safe_fx",
            background_independent=True,
        )

        class _Catalog:
            def get_spec(self, effect_type):
                return declared if effect_type == "plugin:safe_fx" else None

        monkeypatch.setattr(
            "src.features.shared.effects.get_effect_catalog",
            lambda: _Catalog(),
        )
        assert oc.effects_background_independent(
            (FakeEffect("plugin:safe_fx"),)) is True

    def test_mixed_chain_fail_closed(self, monkeypatch):
        from src.features.shared.effects.effect_spec import EffectSpec

        declared = EffectSpec(
            effect_type="plugin:safe_fx",
            name="safe",
            category="plugin",
            param_defs=(),
            shader_kind="single_pass",
            shader_sources=("void main(){ gl_FragColor = vec4(1.0); }",),
            dispatch_key="plugin:safe_fx",
            background_independent=True,
        )

        class _Catalog:
            def get_spec(self, effect_type):
                return declared if effect_type == "plugin:safe_fx" else None

        monkeypatch.setattr(
            "src.features.shared.effects.get_effect_catalog",
            lambda: _Catalog(),
        )
        # 声明过的效果 + 未声明的效果混合 → fail-closed。
        assert oc.effects_background_independent(
            (FakeEffect("plugin:safe_fx"), FakeEffect("blur"))) is False

    def test_disabled_effect_ignored(self, monkeypatch):
        from src.features.shared.effects.effect_spec import EffectSpec

        declared = EffectSpec(
            effect_type="plugin:safe_fx",
            name="safe",
            category="plugin",
            param_defs=(),
            shader_kind="single_pass",
            shader_sources=("void main(){ gl_FragColor = vec4(1.0); }",),
            dispatch_key="plugin:safe_fx",
            background_independent=True,
        )

        class _Catalog:
            def get_spec(self, effect_type):
                return declared if effect_type == "plugin:safe_fx" else None

        monkeypatch.setattr(
            "src.features.shared.effects.get_effect_catalog",
            lambda: _Catalog(),
        )
        # 禁用的效果不参与判定；只剩声明过的启用效果 → True。
        assert oc.effects_background_independent((
            FakeEffect("blur", is_enabled=False),
            FakeEffect("plugin:safe_fx"),
        )) is True

    def test_phase1_effects_still_block_culling(self):
        # 第一阶段语义不变：任何挂载效果（即使声明 background_independent
        # 的 LayerOcclusionFacts）都阻止该层成为遮挡层。
        facts = LayerOcclusionFacts(
            asset_kind="video",
            source_alpha_known_opaque=True,
            has_effects=True,
            background_independent=True,
        )
        decision = analyze_visible_layers(
            bottom_to_top(FakeLayer("v1", 0), FakeLayer("v2", 1)), (), 0, CANVAS,
            facts_of=facts_of_static(facts), record=False,
        )
        assert not decision.culled


class TestEffectSpecDeclarationField:
    def test_default_is_false(self):
        from src.features.shared.effects.effect_spec import EffectSpec
        spec = EffectSpec(
            effect_type="x", name="x", category="filter", param_defs=(),
            shader_kind="single_pass", shader_sources=("s",), dispatch_key="x",
        )
        assert spec.background_independent is False


# ── 复查加固：track_index 缺失时的 fail-closed ──


class BareLayer:
    """满足遮挡契约但完全缺失 track_index 的裸层。"""

    def __init__(self, clip_id):
        self.clip_id = clip_id
        self.opacity = 1.0
        self.transform = (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
        self.crop = (0.0, 0.0, 0.0, 0.0, 0.0)
        self.is_in_transition = False
        self.composite_id = None


class TestTrackIndexHardening:
    def test_missing_track_index_keeps_all(self):
        # 鸭子类型层缺 track_index：排序即抛异常 → analysis_error 保留全部
        # （修复前 getattr 默认 0 会把它排到最底并可能错误裁掉真正的上层）。
        decision = analyze_visible_layers(
            (BareLayer("v1"), BareLayer("v2")), (), 0, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS), record=False,
        )
        assert not decision.culled
        assert decision.reason == "analysis_error"

    def test_region_plan_missing_track_index_conservative(self):
        from src.features.edit.core.timeline_occlusion import (
            compute_visible_region_plan,
        )

        plan = compute_visible_region_plan(
            (BareLayer("v1"), BareLayer("v2")), (), 5, CANVAS,
            facts_of=facts_of_static(VIDEO_FACTS),
        )
        assert plan.frame == 5
        # 异常回退：全部整画布、无人被标记完全遮挡。
        assert all(r.region is None and not r.complete_cover for r in plan.regions)
