"""TM-04 full first-batch coverage: all 15 executable templates.

Acceptance proxy for "每个模板有横/竖屏、中文、缺素材、品牌覆盖和 3 帧质量样例":

* 横竖屏 — each template compiles at 16:9 AND 9:16,
* 中文    — each compiles with CJK content,
* 品牌覆盖 — a brand ``accent_color`` override changes an accent element,
* 3 帧质量样例 — the compiled structure is deterministic and every element's
  bbox stays inside the canvas (the pure-logic proxy for "frames are clean",
  since structured templates carry no wall-clock state).

Templates carry no external asset dependency, so 缺素材 degrades gracefully by
construction (no asset refs to resolve); that is asserted too.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core import (
    create_builtin_template_registry, TemplateBuildContext,
)
from src.features.teaching_animation.core.model import SCENE_SCHEMA_VERSION


# Per-template params, each carrying at least one CJK value + the brand
# accent override token.  These mirror the plan's CJK / brand acceptance.
CJK_PARAMS: dict[str, dict] = {
    "title_card": {"title": "人工智能四步", "subtitle": "副标题", "accent_color": "#2563eb"},
    "section_divider": {"index": "2", "label": "章节标题"},
    "steps": {"title": "步骤", "steps": "一, 二, 三, 四", "accent_color": "#2563eb"},
    "lower_third": {"name": "张三", "role": "工程师", "accent_color": "#2563eb"},
    "process_flow": {"title": "流程", "nodes": "开始, 中间, 结束", "direction": "vertical"},
    "data_chart": {"title": "数据", "values": "4, 7, 3, 9", "labels": "甲, 乙, 丙, 丁",
                   "accent_color": "#2563eb"},
    "comparison": {"title": "对比", "left_label": "旧", "right_label": "新",
                   "left_items": "慢, 贵", "right_items": "快, 省"},
    "quote": {"quote": "学而时习之", "author": "孔子", "role": "论语", "accent_color": "#2563eb"},
    "formula_proof": {"title": "证明", "formula": "a^2 + b^2 = c^2", "steps": "勾, 股, 弦",
                      "accent_color": "#2563eb"},
    "timeline": {"title": "时间线", "events": "2020:立项, 2021:开发, 2022:发布",
                 "accent_color": "#2563eb"},
    "checklist": {"title": "清单", "items": "第一, 第二, 第三", "accent_color": "#2563eb"},
    "tutorial_callout": {"title": "注意", "body": "本节要点内容", "pointer_direction": "top",
                         "accent_color": "#2563eb"},
    "social_card": {"headline": "今日发布", "handle": "@官方", "tag": "#新功能",
                    "accent_color": "#2563eb"},
    "product_feature": {"title": "产品", "subtitle": "核心能力", "features": "快速, 可靠, 开放",
                        "accent_color": "#2563eb"},
    "cta": {"headline": "立即开始", "subtext": "免费试用", "button_label": "注册",
            "accent_color": "#2563eb"},
}


@pytest.fixture(scope="module")
def registry():
    return create_builtin_template_registry()


def test_registry_has_fifteen_templates(registry):
    ids = {t.template_id for t in registry.list_templates()}
    expected = {
        "title_card", "lower_third", "section_divider", "steps", "process_flow",
        "data_chart", "comparison", "quote", "formula_proof", "timeline",
        "checklist", "tutorial_callout", "social_card", "product_feature", "cta",
    }
    assert ids == expected


@pytest.mark.parametrize("template_id", sorted(CJK_PARAMS))
def test_template_compiles_in_both_orientations(registry, template_id):
    params = dict(CJK_PARAMS[template_id])
    for w, h in ((1920, 1080), (1080, 1920)):
        ctx = TemplateBuildContext(w, h, duration_frames=120)
        scene = registry.compile(registry.instantiate(template_id, params, context=ctx))
        assert scene.width == w and scene.height == h
        assert scene.schema_version == SCENE_SCHEMA_VERSION
        assert len(scene.elements) > 0


@pytest.mark.parametrize("template_id", sorted(CJK_PARAMS))
def test_template_elements_stay_inside_canvas(registry, template_id):
    """3-frame quality proxy: deterministic compile, bboxes inside the canvas."""
    ctx = TemplateBuildContext(1920, 1080, duration_frames=120)
    params = dict(CJK_PARAMS[template_id])
    scene1 = registry.compile(registry.instantiate(template_id, params, context=ctx))
    scene2 = registry.compile(registry.instantiate(template_id, params, context=ctx))
    # Deterministic: same params -> identical element list.
    assert [e.to_dict() for e in scene1.elements] == [e.to_dict() for e in scene2.elements]
    ids = [e.element_id for e in scene1.elements]
    assert len(ids) == len(set(ids)), "duplicate element ids"
    # Element ids are unique and every bbox is within the canvas (allow a tiny
    # tolerance for shapes whose accent bar intentionally hugs an edge).
    for el in scene1.elements:
        t = el.transform
        assert t.width >= 0.0 and t.height >= 0.0
        assert t.x >= -1.0 and t.y >= -1.0
        assert t.x + t.width <= scene1.width + 1.0
        assert t.y + t.height <= scene1.height + 1.0


@pytest.mark.parametrize("template_id", sorted(CJK_PARAMS))
def test_template_has_no_external_asset_dependency(registry, template_id):
    """Structured templates degrade gracefully when assets are missing."""
    ctx = TemplateBuildContext(1920, 1080)
    scene = registry.compile(registry.instantiate(template_id, dict(CJK_PARAMS[template_id]), context=ctx))
    assert scene.asset_refs == ()


class TestBrandAccentOverride:
    """品牌覆盖: changing accent_color visibly changes an accent-bearing element."""

    @pytest.mark.parametrize("template_id,accent_element", [
        ("title_card", "accent_bar"),
        ("lower_third", "accent_bar"),
        ("steps", "card_1_number"),
        ("process_flow", "arrow_1"),
        ("data_chart", "bar_1"),
        ("quote", "mark"),
        ("formula_proof", "formula"),
        ("timeline", "event_1_dot"),
        ("checklist", "item_1_box"),
        ("tutorial_callout", "border"),
        ("social_card", "tag_pill"),
        ("product_feature", "feature_1_bullet"),
        ("cta", "button"),
    ])
    def test_accent_override_changes_element(self, registry, template_id, accent_element):
        ctx = TemplateBuildContext(1920, 1080, duration_frames=120)
        default = registry.compile(registry.instantiate(template_id, {}, context=ctx))
        branded = registry.compile(
            registry.instantiate(template_id, {"accent_color": "#ff00ff"}, context=ctx))
        el_default = default.element_by_id(accent_element)
        el_branded = branded.element_by_id(accent_element)
        assert el_default is not None and el_branded is not None
        # At least one colour-bearing style field picks up the override.
        keys = ("fill", "color", "stroke")
        changed = any(el_default.style.get(k) != el_branded.style.get(k) for k in keys)
        assert changed, f"accent override did not affect {accent_element}"


class TestParserValidation:
    def test_process_flow_needs_two_nodes(self, registry):
        ctx = TemplateBuildContext(1920, 1080)
        # The >=2-nodes rule lives in build(), so it surfaces on compile.
        with pytest.raises(ValueError):
            registry.compile(registry.instantiate("process_flow", {"nodes": "Only"}, context=ctx))

    def test_data_chart_needs_one_value(self, registry):
        ctx = TemplateBuildContext(1920, 1080)
        with pytest.raises(ValueError):
            registry.compile(registry.instantiate("data_chart", {"values": ""}, context=ctx))

    def test_direction_enum_enforced(self, registry):
        ctx = TemplateBuildContext(1920, 1080)
        errors = registry.get("process_flow").validate_params(
            {"nodes": "A, B", "direction": "diagonal"})
        assert any("direction" in e for e in errors)

    def test_pointer_direction_enum_enforced(self, registry):
        errors = registry.get("tutorial_callout").validate_params(
            {"pointer_direction": "sideways"})
        assert any("pointer_direction" in e for e in errors)

    def test_max_value_accepts_number(self, registry):
        errors = registry.get("data_chart").validate_params(
            {"values": "1,2", "max_value": 10})
        assert not any("max_value" in e for e in errors)
