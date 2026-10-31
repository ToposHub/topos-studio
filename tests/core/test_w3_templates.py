"""W3 ExecutableTemplate + builtin templates + AI command/brand commands.

Covers TM-01 (scene compiler + migration), TM-04 (first templates), TM-06
(AI schema export), TM-02 (apply_template / set_brand_theme / replace_asset_slot),
and the SG-03 layout-adapts-on-canvas-change contract (16:9 → 9:16).
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core import (
    ApplyTemplateCommand, BrandTheme, ExecutableTemplate, ExecutableTemplateRegistry,
    ParamSpec, ReplaceAssetSlotCommand, SetBrandThemeCommand, TemplateBuildContext,
    TITLE_CARD, create_builtin_template_registry, default_brand_tokens,
)
from src.features.teaching_animation.core.commands import SceneCommandStack
from src.features.teaching_animation.core.model import (
    SCENE_SCHEMA_VERSION, SceneAssetRef, TeachingSceneDocument,
)


def _blank_scene(width: int = 1920, height: int = 1080) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="seed", schema_version=SCENE_SCHEMA_VERSION, name="seed",
        width=width, height=height, fps_num=24, fps_den=1, duration_frames=240,
        background={"color": "#000000"}, elements=(), animations=(),
    )


class TestExecutableTemplateCore:
    def test_compile_is_deterministic(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(canvas_width=1920, canvas_height=1080, duration_frames=120)
        inst1 = reg.instantiate("title_card", {"title": "X"}, context=ctx)
        inst2 = reg.instantiate("title_card", {"title": "X"}, context=ctx)
        s1 = reg.compile(inst1)
        s2 = reg.compile(inst2)
        # same params + context -> same scene (except auto-generated scene_id; compare elements)
        assert [e.to_dict() for e in s1.elements] == [e.to_dict() for e in s2.elements]

    def test_param_validation_rejects_bad_types(self) -> None:
        errors = TITLE_CARD.validate_params({"title": 123})
        assert any("title" in e for e in errors)

    def test_unknown_param_reported(self) -> None:
        errors = TITLE_CARD.validate_params({"title": "T", "bogus": 1})
        assert any("unknown" in e for e in errors)

    def test_required_missing(self) -> None:
        errors = TITLE_CARD.validate_params({"subtitle": "x"})  # title required
        assert any("required" in e for e in errors)

    def test_instance_round_trip(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(canvas_width=960, canvas_height=540, duration_frames=60)
        inst = reg.instantiate("section_divider", {"label": "Intro"}, context=ctx)
        restored = type(inst).from_dict(inst.to_dict())
        assert restored.template_id == inst.template_id
        assert restored.params == inst.params
        assert restored.context.canvas_width == 960


class TestBuiltinTemplates:
    @pytest.mark.parametrize("template_id,params", [
        ("title_card", {"title": "Hello", "subtitle": "World"}),
        ("section_divider", {"index": "2", "label": "Methods"}),
        ("steps", {"title": "Flow", "steps": "A, B, C"}),
        ("lower_third", {"name": "Ada", "role": "Engineer"}),
    ])
    def test_each_template_compiles_valid_scene(self, template_id: str, params: dict) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(canvas_width=1920, canvas_height=1080, duration_frames=120)
        scene = reg.compile(reg.instantiate(template_id, params, context=ctx))
        assert scene.width == 1920 and scene.height == 1080
        assert len(scene.elements) > 0
        assert scene.schema_version == SCENE_SCHEMA_VERSION

    def test_steps_grid_responsive_columns(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(canvas_width=1920, canvas_height=1080)
        three = reg.compile(reg.instantiate("steps", {"steps": "A,B,C"}, context=ctx))
        four = reg.compile(reg.instantiate("steps", {"steps": "A,B,C,D"}, context=ctx))
        # three cards + title + number/label sub-elements each
        assert any(e.element_id == "card_3" for e in three.elements)
        assert any(e.element_id == "card_4" for e in four.elements)

    def test_long_cjk_text_does_not_crash(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(canvas_width=1920, canvas_height=1080)
        scene = reg.compile(reg.instantiate("title_card", {"title": "人工智能代理四步骤", "subtitle": "中文副标题"}, context=ctx))
        assert scene.elements[1].content == "人工智能代理四步骤"


class TestLayoutAdaptsToCanvas:
    def test_16x9_vs_9x16_keep_content_in_safe_area(self) -> None:
        reg = create_builtin_template_registry()
        wide = reg.compile(reg.instantiate("title_card", {"title": "T"}, context=TemplateBuildContext(1920, 1080)))
        tall = reg.compile(reg.instantiate("title_card", {"title": "T"}, context=TemplateBuildContext(1080, 1920)))
        # title element stays within safe area (40px token safe-area) on both.
        safe = 40.0
        for scene in (wide, tall):
            title = next(e for e in scene.elements if e.element_id == "title")
            assert title.transform.x >= safe - 1.0
            assert title.transform.y >= safe - 1.0
            assert title.transform.x + title.transform.width <= scene.width - safe + 1.0
        assert tall.height == 1920


class TestAIContextExport:  # TM-06
    def test_schema_export_lists_params(self) -> None:
        reg = create_builtin_template_registry()
        schema = reg.schema_for_ai("steps")
        names = {p["name"] for p in schema["params"]}
        assert {"title", "steps", "accent_color"}.issubset(names)
        assert schema["capabilities"]["preview"] is True

    def test_unknown_template_schema_raises(self) -> None:
        reg = create_builtin_template_registry()
        with pytest.raises(KeyError):
            reg.schema_for_ai("nope")


class TestTemplateCommands:  # TM-02
    def test_apply_template_records_instance_and_undo(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(1920, 1080, duration_frames=120)
        stack = SceneCommandStack()
        seed = _blank_scene()
        applied = stack.execute(ApplyTemplateCommand("steps", {"steps": "A,B"}, registry=reg, context=ctx), seed)
        assert "template_instance" in applied.metadata
        assert len(applied.elements) > 0
        restored = stack.undo(applied)
        assert restored is not None
        assert restored.elements == ()

    def test_brand_theme_recompiles_template_scene(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(1920, 1080, duration_frames=120)
        stack = SceneCommandStack()
        scene = stack.execute(ApplyTemplateCommand("title_card", {"title": "T"}, registry=reg, context=ctx), _blank_scene())
        before = next(e for e in scene.elements if e.element_id == "accent_bar").style["fill"]
        theme = BrandTheme(theme_id="brand", palette={"primary": "#2563eb"})
        themed = stack.execute(SetBrandThemeCommand(theme, base_tokens=default_brand_tokens(), registry=reg), scene)
        after = next(e for e in themed.elements if e.element_id == "accent_bar").style["fill"]
        assert before != after
        assert after == "#2563eb"
        restored = stack.undo(themed)
        assert restored is not None
        assert next(e for e in restored.elements if e.element_id == "accent_bar").style["fill"] == before

    def test_brand_theme_reports_unresolved_refs(self) -> None:
        theme = BrandTheme(theme_id="b", palette={"primary": "$palette.missing"})
        cmd = SetBrandThemeCommand(theme, base_tokens=default_brand_tokens())
        cmd.apply(_blank_scene())  # noqa: builds conflict metadata
        assert any("primary" in t for t in cmd.unresolved_tokens)

    def test_replace_asset_slot_registers_and_swaps(self) -> None:
        reg = create_builtin_template_registry()
        ctx = TemplateBuildContext(1920, 1080)
        stack = SceneCommandStack()
        scene = stack.execute(ApplyTemplateCommand("title_card", {"title": "T"}, registry=reg, context=ctx), _blank_scene())
        # make a card image element to host an asset
        asset = SceneAssetRef(asset_id="logo_1", kind="image", path="assets/logo.png")
        from src.features.teaching_animation.core.model import SceneElement, SceneTransform
        scene = scene.with_element_added(SceneElement(
            element_id="logo", type="image", transform=SceneTransform(x=10, y=10, width=100, height=100),
            style={}, content="", asset_ref=None,
        ))
        replaced = stack.execute(ReplaceAssetSlotCommand("logo", asset), scene)
        logo = replaced.element_by_id("logo")
        assert logo.asset_ref == "logo_1"
        assert any(ref.asset_id == "logo_1" for ref in replaced.asset_refs)
        restored = stack.undo(replaced)
        assert restored.element_by_id("logo").asset_ref is None
        assert not any(ref.asset_id == "logo_1" for ref in restored.asset_refs)
