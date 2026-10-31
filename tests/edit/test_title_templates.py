"""C1: 标题模板库测试。

覆盖：
- style_preset category 字段与每类内置预设（title/callout/credit/social/lower_third）
- 用户标题模板存储：保存/读取/查重/删除/坏 JSON 容错（Presets/User/title_templates）
- cue 级动画枚举与 ease-out 包络数值表
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.captions.graphics import (  # noqa: E402
    CUE_ANIMATIONS,
    cue_animation_envelope,
)
from src.features.edit.core.captions.model import CaptionStyle  # noqa: E402
from src.features.edit.core.captions.style_preset import (  # noqa: E402
    BUILTIN_PRESETS,
)


class TestBuiltinCategories:
    def test_all_required_categories_present(self) -> None:
        categories = {p.category for p in BUILTIN_PRESETS}
        assert {"title", "lower_third", "callout", "credit", "social"} <= categories

    def test_title_category_has_multiple_presets(self) -> None:
        titles = [p for p in BUILTIN_PRESETS if p.category == "title"]
        assert len(titles) >= 3

    def test_category_presets_are_builtin(self) -> None:
        for preset in BUILTIN_PRESETS:
            if preset.category in ("title", "callout", "credit"):
                assert preset.is_builtin


class TestUserTitleTemplates:
    @pytest.fixture(autouse=True)
    def template_store(self, tmp_path: Path, monkeypatch) -> Path:
        monkeypatch.chdir(tmp_path)
        return Path(
            __import__(
                "src.features.edit.core.title_templates",
                fromlist=["title_templates_dir"],
            ).title_templates_dir()
        )

    def test_save_list_roundtrip(self, template_store: Path) -> None:
        from src.features.edit.core.title_templates import (
            list_user_title_templates,
            save_title_template,
        )

        style = CaptionStyle(font_family="Arial", font_size=200, font_weight="bold")
        saved = save_title_template("My Title", style, category="title")
        assert saved.preset_id.startswith("user_")
        listed = list_user_title_templates()
        assert [p.label for p in listed] == ["My Title"]
        assert listed[0].style.font_size == 200
        assert listed[0].category == "title"
        # JSON 标记自定义
        data = json.loads(
            (template_store / f"{saved.preset_id}.json").read_text(encoding="utf-8"))
        assert data["is_custom"] is True

    def test_duplicate_name_rejected(self, template_store: Path) -> None:
        from src.features.edit.core.title_templates import save_title_template

        save_title_template("Dup", CaptionStyle())
        with pytest.raises(ValueError, match="duplicate"):
            save_title_template("Dup", CaptionStyle())

    def test_bad_json_skipped(self, template_store: Path) -> None:
        from src.features.edit.core.title_templates import (
            list_user_title_templates,
            save_title_template,
        )

        save_title_template("Good", CaptionStyle(font_size=120))
        (template_store / "broken.json").write_text("{oops", encoding="utf-8")
        listed = list_user_title_templates()
        assert [p.label for p in listed] == ["Good"]

    def test_delete(self, template_store: Path) -> None:
        from src.features.edit.core.title_templates import (
            delete_title_template,
            list_user_title_templates,
            save_title_template,
        )

        saved = save_title_template("Doomed", CaptionStyle())
        assert delete_title_template(saved.preset_id) is True
        assert list_user_title_templates() == []


class TestCueAnimations:
    def test_enum_covers_planned_set(self) -> None:
        assert {"none", "fade", "slide_up", "slide_left",
                "scale_pop", "typewriter"} <= CUE_ANIMATIONS

    def test_fade_envelope_monotonic_ease_out(self) -> None:
        prev = -1.0
        for frame in range(0, 12):
            opacity, _tx, _ty, _scale = cue_animation_envelope(
                frame, 24, in_animation="fade", in_frames=12)
            assert opacity >= prev
            prev = opacity
        # ease-out 起步快于线性（1/12 处 > 1/12）
        opacity, _tx, _ty, _scale = cue_animation_envelope(
            1, 24, in_animation="fade", in_frames=12)
        assert opacity > 1.0 / 12

    def test_slide_up_translates(self) -> None:
        # AUD-15900: Y-down 惯例——slide_up 从下方进入（正 y 位移）
        opacity, tx, ty, _scale = cue_animation_envelope(
            0, 24, in_animation="slide_up", in_frames=12)
        assert opacity < 1.0 and ty > 0 and tx == 0.0

    def test_slide_axes_not_merged(self) -> None:
        # AUD-15900: in=slide_left + out=slide_up 异轴组合不得叠加成单轴
        # （local=2 处 in/out 窗口重叠，两轴同时非零）
        opacity, tx, ty, _scale = cue_animation_envelope(
            2, 8, in_animation="slide_left", out_animation="slide_up",
            in_frames=8, out_frames=8)
        assert tx < 0 and ty < 0  # 进入向左 + 退出向上，两轴独立

    def test_scale_pop_scales(self) -> None:
        _o, _tx, _ty, scale = cue_animation_envelope(
            0, 24, in_animation="scale_pop", in_frames=12)
        assert 0.8 <= scale < 1.0

    def test_none_is_constant(self) -> None:
        assert cue_animation_envelope(0, 24) == (1.0, 0.0, 0.0, 1.0)
        assert cue_animation_envelope(23, 24) == (1.0, 0.0, 0.0, 1.0)
