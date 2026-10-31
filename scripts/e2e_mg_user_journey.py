#!/usr/bin/env python
"""End-to-end real-user simulation of the Motion Graphic editor (WP-0..WP-8).

Drives the *real* editor window the way a user does: real Qt widgets, real
keyboard events, real menu actions, a real Qt WebEngine page with real DOM
clicks / in-place text editing / pointer-draw gestures.  Nothing is monkey-
patched; every verification reads the same document / DOM the user sees.

Run on a desktop session (windows will appear briefly):

    .venv/bin/python scripts/e2e_mg_user_journey.py

Pass ``--offscreen`` to run only the non-browser journeys headlessly.
"""

from __future__ import annotations

import sys
import time
import traceback
from dataclasses import dataclass, replace

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

# The real app imports QtWebEngineWidgets before QApplication (src/main.py);
# mirror that startup order so the interactive preview can create Chromium.
try:  # pragma: no cover - optional dependency
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    pass

from src.features.teaching_animation.core.builtin_templates import TITLE_CARD
from src.features.teaching_animation.core.design_tokens import read_scene_tokens
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.hierarchy import read_hierarchy
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.core.validator import validate_scene
from src.features.teaching_animation.render.evaluator import evaluate
from src.features.teaching_animation.render.html_native_overlay import (
    native_animation_css, native_overlay_css_variables, native_overlay_markup,
)
from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

@dataclass
class StepResult:
    journey: str
    name: str
    ok: bool | None  # None = skipped
    detail: str = ""
    seconds: float = 0.0


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Report:
    def __init__(self) -> None:
        self.results: list[StepResult] = []

    def step(self, journey: str, name: str, fn) -> None:  # type: ignore[no-untyped-def]
        start = time.monotonic()
        try:
            detail = fn()
            self.results.append(StepResult(journey, name, True, str(detail or ""), time.monotonic() - start))
            print(f"  ✅ {name}")
        except Exception as exc:  # noqa: BLE001 - record every failure and continue
            trace = traceback.format_exc(limit=4)
            self.results.append(StepResult(journey, name, False, f"{exc}\n{trace}", time.monotonic() - start))
            print(f"  ❌ {name}: {exc}")

    def skip(self, journey: str, name: str, why: str) -> None:
        self.results.append(StepResult(journey, name, None, why, 0.0))
        print(f"  ⏭️  {name} — skipped: {why}")

    def summary(self) -> int:
        passed = sum(1 for r in self.results if r.ok is True)
        failed = sum(1 for r in self.results if r.ok is False)
        skipped = sum(1 for r in self.results if r.ok is None)
        print("\n" + "=" * 72)
        print(f"RESULT: {passed} passed · {failed} failed · {skipped} skipped "
              f"({len(self.results)} steps)")
        for r in self.results:
            if r.ok is False:
                print(f"\n--- FAILURE: [{r.journey}] {r.name} ---\n{r.detail}")
        print("=" * 72)
        return 1 if failed else 0


# --------------------------------------------------------------------------- #
# Qt helpers (real event pumping, no mocks)
# --------------------------------------------------------------------------- #

def pump(app: QApplication, seconds: float = 0.3) -> None:
    """Process Qt events (including queued WebChannel signals) for a while."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def wait_until(app: QApplication, condition, timeout: float = 15.0, what: str = "condition") -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


def run_js(page, script: str, timeout_ms: int = 8000):  # type: ignore[no-untyped-def]
    """Evaluate JS in the live page and block for the callback result."""
    results: list[object] = []
    loop = QEventLoop()

    def _done(value: object) -> None:
        results.append(value)
        loop.quit()

    page.runJavaScript(script, _done)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    if not results:
        raise TimeoutError("JavaScript evaluation timed out")
    return results[0]


def undo_depth(window: MotionGraphicEditorWindow) -> int:
    return len(window.editor_controller._stack._undo_stack)


def scene_markup(window: MotionGraphicEditorWindow) -> str:
    scene = window.editor_controller.scene
    tokens = read_scene_tokens(scene.metadata)
    return native_overlay_markup(scene.elements, read_hierarchy(scene.metadata).parents, tokens)


def contains_element(markup: str, outer_id: str, inner_id: str) -> bool:
    """True when inner's node renders inside outer's node in the overlay DOM."""
    import re
    outer_attr = markup.find(f'data-topos-native-element="{outer_id}"')
    inner_attr = markup.find(f'data-topos-native-element="{inner_id}"')
    if outer_attr < 0 or inner_attr < 0 or inner_attr < outer_attr:
        return False
    tag_start = markup.rfind("<div", 0, outer_attr)
    if tag_start < 0:
        return False
    depth = 0
    for match in re.finditer(r"<div\b|</div>", markup[tag_start:]):
        pos = tag_start + match.start()
        if markup[pos:pos + 5] == "</div":
            depth -= 1
            if depth == 0:
                return tag_start < inner_attr < pos
        else:
            depth += 1
    return False


def close_silently(window: MotionGraphicEditorWindow) -> None:
    """Make the window closable without the unsaved-changes modal."""
    window.editor_controller.mark_clean()
    window.html_source_editor.discard_pending_draft()
    window.html_devtools.set_has_unapplied_changes(False)
    window.close()


# --------------------------------------------------------------------------- #
# Journey A — vector scene editing (real Qt widget interaction)
# --------------------------------------------------------------------------- #

def journey_a(report: Report, app: QApplication) -> MotionGraphicEditorWindow:
    print("\n[Journey A] 矢量场景：建元素 → 属性 → 排版 → 令牌 → 动画 → 撤销 → 导出")
    scene = replace(
        TeachingSceneDocument.create_default("journey-vector", duration_frames=120),
        elements=(),  # start from an empty canvas like a new Motion Graphic
    )
    window = MotionGraphicEditorWindow(scene)
    window.resize(1440, 900)
    window.show()
    pump(app, 0.5)
    state: dict[str, object] = {}

    def a1_launch() -> str:
        require(window.isVisible(), "editor window did not show")
        require(window.scene_view is not None and window.layer_tree is not None
                and window.timeline is not None and window.inspector is not None,
                "core panels missing")
        require(window.editor_controller.scene.elements == (), "fresh scene should be empty")
        require(undo_depth(window) == 0, "undo stack should start empty")
        return "window + panels up, empty scene"

    report.step("A", "A1 启动编辑器（真实窗口显示）", a1_launch)

    def a2_add_text() -> str:
        window._create_layer("text")
        pump(app, 0.1)
        scene_now = window.editor_controller.scene
        require(len(scene_now.elements) == 1, "text element not created")
        element = scene_now.elements[0]
        require(element.type == "text", f"expected text, got {element.type}")
        require(window._selected_elements == {element.element_id}, "new element not selected")
        require(undo_depth(window) == 1, "create should push one undo entry")
        state["text_id"] = element.element_id
        return f"text element {element.element_id[:14]}… created + auto-selected"

    report.step("A", "A2 图层面板新建 Text 图层", a2_add_text)

    def a3_add_shape_and_frame() -> str:
        window._create_layer("rect")
        pump(app, 0.1)
        window._create_layer("frame")
        pump(app, 0.1)
        scene_now = window.editor_controller.scene
        require(len(scene_now.elements) == 3, f"expected 3 elements, got {len(scene_now.elements)}")
        shape = next(e for e in scene_now.elements if e.type == "shape")
        frame = next(e for e in scene_now.elements if e.type == "frame")
        require(frame.layout and frame.layout.get("mode") == "flex-column",
                f"frame layout default wrong: {frame.layout}")
        state["shape_id"], state["frame_id"] = shape.element_id, frame.element_id
        return "rect shape + flex-column frame created with layout defaults"

    report.step("A", "A3 新建 Rect 形状与 Frame（Flex 容器）", a3_add_shape_and_frame)

    def a4_type_text() -> str:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        text_id = str(state["text_id"])
        window._set_selection({text_id})
        pump(app, 0.1)
        edit = window.inspector.content_edit
        edit.clear()
        edit.setFocus()
        QTest.keyClicks(edit, "Particle Glow Title")
        QTest.keyClick(edit, Qt.Key.Key_Return)  # commits editingFinished like a real user
        pump(app, 0.2)
        element = window.editor_controller.scene.element_by_id(text_id)
        require(element is not None and element.content == "Particle Glow Title",
                f"typed text not committed: {element.content if element else None!r}")
        return "keyboard typing committed via editingFinished"

    report.step("A", "A4 键盘输入标题文本（真实键盘事件）", a4_type_text)

    def a5_style_edits() -> str:
        from PyQt6.QtGui import QColor
        text_id = str(state["text_id"])
        window._set_selection({text_id})
        pump(app, 0.1)
        inspector = window.inspector
        before = undo_depth(window)
        inspector.font_size.slider.setValue(72)
        inspector.font_size.interactionEnded.emit()  # mouse-release gesture commit
        weight_index = inspector.font_weight.findData("700")
        require(weight_index >= 0, "font_weight combo has no 700 option")
        inspector.font_weight.setCurrentIndex(weight_index)
        inspector.color_edit._commit_color(QColor("#ffcc00"))  # the picker's real commit path
        pump(app, 0.2)
        element = window.editor_controller.scene.element_by_id(text_id)
        style = element.style if element else {}
        require(style.get("font_size") == 72.0, f"font_size not applied: {style.get('font_size')}")
        require(str(style.get("font_weight")) == "700", f"font_weight not applied: {style.get('font_weight')}")
        require(str(style.get("color", "")).lower() == "#ffcc00", f"color not applied: {style.get('color')}")
        depth = undo_depth(window) - before
        require(depth == 3, f"expected 3 undo gestures (font size, weight, color), got {depth}")
        return "font 72px / weight 700 / color #ffcc00 applied as 3 undoable gestures"

    report.step("A", "A5 属性面板改字号/字重/颜色（滑条手势 + 组合框）", a5_style_edits)

    def a6_markup_reflects_style() -> str:
        markup = scene_markup(window)
        require("Particle Glow Title" in markup, "typed text missing from native markup")
        require("font-size:72" in markup, f"font-size missing from markup: {markup[:300]}")
        require("font-weight:700" in markup,
                f"font-weight missing: {markup[:400]}")
        require("#ffcc00" in markup.lower(), "color missing from markup")
        return "native overlay markup carries text + all edited styles"

    report.step("A", "A6 画布原生 DOM 标记反映样式", a6_markup_reflects_style)

    def a7_frame_layout() -> str:
        frame_id = str(state["frame_id"])
        window._set_selection({frame_id})
        pump(app, 0.1)
        inspector = window.inspector
        inspector.layout_mode.setCurrentIndex(inspector.layout_mode.findData("flex-row"))
        inspector.layout_gap.slider.setValue(24)
        inspector.layout_gap.interactionEnded.emit()
        inspector.layout_padding.slider.setValue(32)
        inspector.layout_padding.interactionEnded.emit()
        pump(app, 0.2)
        frame = window.editor_controller.scene.element_by_id(frame_id)
        layout = frame.layout if frame else {}
        require(layout.get("mode") == "flex-row", f"layout mode: {layout.get('mode')}")
        require(layout.get("gap") == 24.0, f"gap: {layout.get('gap')}")
        require(layout.get("padding") == 32.0, f"padding: {layout.get('padding')}")
        markup = scene_markup(window)
        require("display:flex" in markup, "flex container CSS missing")
        require("flex-direction:row" in markup, "flex-direction:row missing")
        require("gap:24" in markup, "gap:24px missing")
        require("padding:32" in markup, "padding:32px missing")
        return "flex-row + gap 24 + padding 32 live in document and CSS"

    report.step("A", "A7 Frame 切换 Flex-Row 并调 gap/padding", a7_frame_layout)

    def a8_nest_children() -> str:
        text_id, shape_id, frame_id = str(state["text_id"]), str(state["shape_id"]), str(state["frame_id"])
        window._reparent_element(text_id, frame_id)   # the Layers-panel "nest" action
        window._reparent_element(shape_id, frame_id)
        pump(app, 0.2)
        scene_now = window.editor_controller.scene
        parents = read_hierarchy(scene_now.metadata).parents
        require(parents.get(text_id) == frame_id and parents.get(shape_id) == frame_id,
                f"hierarchy not stored: {parents}")
        markup = scene_markup(window)
        require(contains_element(markup, frame_id, text_id),
                "text element not nested inside the frame's DOM node")
        require(contains_element(markup, frame_id, shape_id),
                "shape element not nested inside the frame's DOM node")
        return "two children nested under frame in hierarchy + DOM"

    report.step("A", "A8 图层面板嵌套子元素进 Frame", a8_nest_children)

    def a9_tokens_and_theme() -> str:
        text_id = str(state["text_id"])
        # Bind the text color to the $palette.accent token, like a user picking
        # "accent" in the inspector's color-token combo.
        window._set_selection({text_id})
        pump(app, 0.1)
        inspector = window.inspector
        index = inspector.color_token.findData("accent")
        require(index >= 0, "color_token combo has no 'accent' entry")
        inspector.color_token.setCurrentIndex(index)
        pump(app, 0.2)
        element = window.editor_controller.scene.element_by_id(text_id)
        require(element is not None and element.style.get("color") == "$palette.accent",
                f"token binding missing: {element.style if element else None}")
        markup = scene_markup(window)
        require("#f59e0b" in markup.lower(), "accent token value not resolved into markup")
        css_vars = native_overlay_css_variables(read_scene_tokens(window.editor_controller.scene.metadata))
        require("--topos-color-accent" in css_vars, f"CSS variables missing accent: {css_vars[:200]}")
        # Author a brand theme into the scene (as a branded template would),
        # then switch it through the real Theme menu action.
        scene_now = window.editor_controller.scene
        metadata = dict(scene_now.metadata)
        themes = dict(metadata.get("brand_themes") or {})
        themes["journey-dark"] = {"palette": {"accent": "#22d3ee"}, "source": "journey"}
        metadata["brand_themes"] = themes
        from src.features.teaching_animation.core.commands import SetSceneMetadataCommand
        window.editor_controller.execute(SetSceneMetadataCommand(metadata))
        pump(app, 0.2)
        window._rebuild_theme_menu()
        actions = {a.text(): a for a in window._theme_menu.actions()}
        require("journey-dark" in actions, f"theme not listed: {list(actions)}")
        actions["journey-dark"].trigger()   # real menu action
        pump(app, 0.3)
        scene_now = window.editor_controller.scene
        require(read_scene_tokens(scene_now.metadata).palette.get("accent") == "#22d3ee",
                "active theme did not overlay the accent token")
        markup = scene_markup(window)
        require("#22d3ee" in markup.lower(), "themed accent not reflected in markup")
        return "token binding + brand-theme switch resolved end-to-end"

    report.step("A", "A9 设计令牌绑定 + 品牌主题切换", a9_tokens_and_theme)

    def a10_keyframes() -> str:
        from PyQt6.QtCore import Qt
        text_id = str(state["text_id"])
        window._set_selection({text_id})
        inspector = window.inspector
        window.timeline.slider.setValue(0)   # real playhead drag path
        pump(app, 0.1)
        inspector.opacity.slider.setValue(20)     # 0.2 in percent
        inspector.opacity.interactionEnded.emit()
        inspector.keyframe_button.click()         # real keyframe button
        window.timeline.slider.setValue(48)
        pump(app, 0.1)
        inspector.opacity.slider.setValue(100)
        inspector.opacity.interactionEnded.emit()
        inspector.keyframe_button.click()
        pump(app, 0.3)
        scene_now = window.editor_controller.scene
        track = next((a for a in scene_now.animations
                      if a.element_id == text_id and a.property == "opacity"), None)
        require(track is not None, "opacity track missing after keyframe clicks")
        require(track is not None and len(track.track.keyframes) == 2,
                f"expected 2 keyframes, got {len(track.track.keyframes) if track else None}")
        values = sorted(k.value for k in track.track.keyframes) if track else []
        require(values == [0.2, 1.0], f"keyframe values should be [0.2, 1.0], got {values}")
        css = native_animation_css(scene_now)
        require("@keyframes" in css, "animation CSS missing")
        mid = evaluate(scene_now, 24)
        sampled = next((e for e in mid.elements if e.source.element_id == text_id), None)
        require(sampled is not None and abs(sampled.opacity - 0.6) < 0.05,
                f"frame-24 opacity should interpolate to ~0.6, got {sampled.opacity if sampled else None}")
        return "2 opacity keyframes → CSS keyframes + correct frame-24 interpolation (0.6)"

    report.step("A", "A10 打关键帧并验证插值/动画CSS", a10_keyframes)

    def a11_undo_redo() -> str:
        scene_now = window.editor_controller.scene
        element_now = scene_now.element_by_id(str(state["text_id"]))
        content_now = element_now.content if element_now else ""
        depth = undo_depth(window)
        for _ in range(3):
            window._undo_action.trigger()
            pump(app, 0.1)
        require(undo_depth(window) == depth - 3, f"undo depth {undo_depth(window)} != {depth - 3}")
        for _ in range(3):
            window._redo_action.trigger()
            pump(app, 0.1)
        restored = window.editor_controller.scene.element_by_id(str(state["text_id"]))
        require(restored is not None and restored.content == content_now,
                "redo did not restore the typed content")
        require(undo_depth(window) == depth, "redo depth mismatch")
        animations = window.editor_controller.scene.animations
        track = next((a for a in animations if a.property == "opacity"), None)
        require(track is not None and len(track.track.keyframes) == 2,
                "undo/redo round-trip lost keyframes (track updates must be undoable)")
        return f"3× undo + 3× redo restored document exactly (depth {depth}, 2 keyframes intact)"

    report.step("A", "A11 菜单撤销×3 / 重做×3", a11_undo_redo)

    def a12_copy_paste_style() -> str:
        shape_id, text_id = str(state["shape_id"]), str(state["text_id"])
        window._set_selection({shape_id})
        window._copy_style_action.trigger()
        window._set_selection({text_id})
        before = undo_depth(window)
        window._paste_style_action.trigger()
        pump(app, 0.2)
        scene_now = window.editor_controller.scene
        shape = scene_now.element_by_id(shape_id)
        text = scene_now.element_by_id(text_id)
        require(shape is not None and text is not None, "elements vanished")
        require(dict(text.style) == dict(shape.style),
                f"paste style mismatch:\n text={text.style}\n shape={shape.style}")
        require(undo_depth(window) == before + 1, "paste style should be exactly one undo step")
        return "copy/paste style via Edit menu, one undoable step"

    report.step("A", "A12 复制/粘贴样式（形状→文字）", a12_copy_paste_style)

    def a13_cache_quality() -> str:
        from src.features.teaching_animation.cache_quality import get_cache_quality
        window._set_cache_quality("high_quality")
        pump(app, 0.1)
        require(get_cache_quality() == "high_quality", "process-wide tier not updated")
        checked = {key: action.isChecked() for key, action in window._cache_quality_actions.items()}
        require(checked.get("high_quality") is True, f"menu check state wrong: {checked}")
        window._set_cache_quality("balanced")
        require(get_cache_quality() == "balanced", "tier not restored")
        return "cache-quality menu updates tier + check state"

    report.step("A", "A13 缓存质量菜单切换", a13_cache_quality)

    def a14_render_frames() -> str:
        from src.features.teaching_animation.host_adapters import render_teaching_scene_frame
        scene_now = window.editor_controller.scene
        scenes = {scene_now.scene_id: scene_now}
        frame0 = render_teaching_scene_frame(scene_now.scene_id, 0, scenes=scenes)
        frame24 = render_teaching_scene_frame(scene_now.scene_id, 24, scenes=scenes)
        require(frame0.width == scene_now.width and frame0.height == scene_now.height,
                f"render size {frame0.width}x{frame0.height} != scene {scene_now.width}x{scene_now.height}")
        require(len(frame0.data) == frame0.width * frame0.height * 4, "rgba buffer size mismatch")
        require(any(frame0.data), "frame 0 rendered fully transparent")
        require(frame0.data != frame24.data,
                "frames 0 and 24 are identical — the opacity animation did not animate the export path")
        return (f"rendered 2 real RGBA frames ({frame0.width}×{frame0.height}), "
                "frame 24 differs from frame 0 (animation live in export)")

    report.step("A", "A14 真实渲染导出两帧并验证动画生效", a14_render_frames)

    def a15_round_trip() -> str:
        from src.features.teaching_animation.core.model import SCENE_SCHEMA_VERSION
        scene_now = window.editor_controller.scene
        data = scene_now.to_dict()
        restored = TeachingSceneDocument.from_dict(data)
        require(restored.schema_version == SCENE_SCHEMA_VERSION == 3,
                f"schema version: {restored.schema_version}")
        validation = validate_scene(restored)
        require(validation.is_valid, f"restored scene invalid: {[str(i) for i in validation.issues]}")
        parents = read_hierarchy(restored.metadata).parents
        require(parents.get(str(state["text_id"])) == str(state["frame_id"]),
                "hierarchy lost across save/load")
        restored_frame = restored.element_by_id(str(state["frame_id"]))
        require(restored_frame is not None and restored_frame.layout is not None
                and restored_frame.layout.get("mode") == "flex-row",
                "frame layout lost across save/load")
        require(read_scene_tokens(restored.metadata).palette.get("accent") == "#22d3ee",
                "active brand theme lost across save/load")
        return "to_dict → from_dict round-trip keeps schema v3, hierarchy, layout, theme"

    report.step("A", "A15 保存/加载往返 + 校验器", a15_round_trip)
    return window


# --------------------------------------------------------------------------- #
# Journey B — HTML scene in the real browser (real Chromium DOM interaction)
# --------------------------------------------------------------------------- #

def _html_scene() -> TeachingSceneDocument:
    from src.features.teaching_animation.core.html_layer import HtmlEditSlot
    base = TeachingSceneDocument.create_default("journey-html", duration_frames=240)
    html = (
        '<section data-topos-page="1"><h1 data-topos-node-id="headline">Page One</h1></section>'
        '<section data-topos-page="2"><h1 data-topos-node-id="headline2">Page Two</h1></section>'
    )
    layer = build_html_layer(
        layer_id="journey_html", sanitized_html=html, authored_source=html,
        viewport_width=1920, viewport_height=1080, fps_num=24, fps_den=1,
        duration_frames=240,
        editable_slots=(
            HtmlEditSlot(node_id="headline", kind="text", label="Headline"),
            HtmlEditSlot(node_id="headline2", kind="text", label="Headline 2"),
        ),
    )
    from src.features.teaching_animation.core.model import SceneElement, SceneTransform
    text = SceneElement(
        element_id="journey_text", type="text", z_index=1,
        transform=SceneTransform(x=400.0, y=400.0, width=1000.0, height=96.0),
        style={"color": "#f9fafb", "font_size": 64.0},
        content="Browser Editable",
    )
    return replace(base, elements=(text,), html_layer=layer)


def journey_b(report: Report, app: QApplication) -> MotionGraphicEditorWindow | None:
    print("\n[Journey B] HTML 场景：真实 Chromium 页面交互（点击选择/就地编辑/拖画/时间轴联动）")
    if QApplication.platformName().lower() == "offscreen":
        report.skip("B", "B* 真实浏览器交互", "offscreen platform has no interactive WebEngine surface")
        return None

    window = MotionGraphicEditorWindow(_html_scene())
    window.resize(1440, 900)
    window.show()
    preview = window.html_browser_preview

    def b1_browser_ready() -> str:
        wait_until(app, lambda: preview.ready, 45.0, "WebEngine page load")
        require(preview.view is not None, "no WebEngine view")
        require(window._html_browser_active, "browser did not become the active preview")
        return "real Chromium page loaded, browser preview active"

    report.step("B", "B1 打开编辑器并加载真实网页", b1_browser_ready)
    browser_ok = report.results[-1].ok is True
    if not browser_ok:
        state = (f"available={preview.available} view={preview.view is not None} "
                 f"active={window._html_browser_active} "
                 f"placeholder='{preview._placeholder.text()}'")
        report.results[-1].detail += f"\n[browser state] {state}"
        print(f"      browser state: {state}")

    def bstep(name: str, fn) -> None:  # type: ignore[no-untyped-def]
        if browser_ok:
            report.step("B", name, fn)
        else:
            report.skip("B", name, "browser did not load (see B1)")

    def b2_dom_materialized() -> str:
        page = preview.view.page()
        count = run_js(page, "document.querySelectorAll('[data-topos-native-element]').length")
        require(count == 1, f"expected 1 native element in DOM, got {count}")
        overlay = run_js(page, "!!document.getElementById('topos-native-vector-overlay')")
        require(overlay is True, "native overlay container missing")
        guard = run_js(page, "window.__toposApplyingNativeOverlay === undefined")
        # The guard flag exists but must not be stuck on (it's only true during applies).
        stuck = run_js(page, "window.__toposApplyingNativeOverlay === true")
        require(stuck is False, "__toposApplyingNativeOverlay stuck true after apply")
        return "native vector layer materialized as real DOM nodes"

    bstep( "B2 原生矢量层真实物化为 DOM", b2_dom_materialized)

    def b3_timeline_scrubs_css_time() -> str:
        window.timeline.slider.setValue(60)   # real playhead drag; 60/24 = 2.5 s
        pump(app, 0.4)
        page = preview.view.page()
        value = run_js(page, (
            "getComputedStyle(document.getElementById('topos-native-vector-overlay'))"
            ".getPropertyValue('--topos-time')"
        ))
        require(str(value).strip().startswith("2.5"), f"--topos-time = {value!r}, expected 2.5000s")
        return "timeline playhead drives --topos-time in the live page (no reload)"

    bstep( "B3 时间轴拖动驱动 CSS --topos-time", b3_timeline_scrubs_css_time)

    def b4_click_selects() -> str:
        page = preview.view.page()
        run_js(page, "document.querySelector('[data-topos-native-element]').click()")
        pump(app, 0.8)  # WebChannel signal is delivered through the event loop
        require("journey_text" in window._selected_elements,
                f"DOM click did not select the element: {window._selected_elements}")
        highlighted = run_js(page, "document.querySelectorAll('.topos-native-selected').length")
        require(highlighted == 1, f"selection highlight missing in DOM: {highlighted}")
        return "JS click → bridge → Qt selection → DOM highlight (full loop)"

    bstep( "B4 页面内点击元素完成选择回环", b4_click_selects)

    def b5_inplace_text_edit() -> str:
        page = preview.view.page()
        before_undo = undo_depth(window)
        result = run_js(page, """
            (() => {
              const el = document.querySelector('div[data-topos-native-element]');
              if (!el) return 'no element';
              el.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
              if (!el.isContentEditable) return 'dblclick did not start editing';
              el.textContent = 'Edited In Browser';
              el.blur();
              // A synthetic session may not carry real focus; nudge the blur
              // handler the same way a focus loss would.
              if (el.isContentEditable) el.dispatchEvent(new FocusEvent('blur'));
              return el.isContentEditable ? 'commit handler did not run' : 'committed';
            })()
        """)
        require(result == "committed", f"in-place edit session failed: {result}")
        pump(app, 0.8)
        element = window.editor_controller.scene.element_by_id("journey_text")
        require(element is not None and element.content == "Edited In Browser",
                f"browser edit did not reach the document: {element.content if element else None!r}")
        require(undo_depth(window) == before_undo + 1,
                f"browser edit should push one undo entry, depth delta = {undo_depth(window) - before_undo}")
        markup = scene_markup(window)
        require("Edited In Browser" in markup, "edited text not reflected in overlay markup")
        return "dblclick → contenteditable → blur → undoable document edit"

    bstep( "B5 页面内双击就地编辑文本", b5_inplace_text_edit)

    def b6_draw_tool_shape() -> str:
        window._set_shape_draw_mode("rect")   # real toolbar action
        pump(app, 0.5)
        before = len(window.editor_controller.scene.elements)
        page = preview.view.page()
        result = run_js(page, """
            (() => {
              const s = document.getElementById('topos-native-draw-surface');
              if (!s) return 'no surface';
              if (s.style.pointerEvents !== 'auto') return 'surface not active: ' + s.style.pointerEvents;
              const r = s.getBoundingClientRect();
              const mk = (type, x, y) => new PointerEvent(type, {
                bubbles: true, clientX: r.left + x, clientY: r.top + y,
                pointerId: 7, button: 0, buttons: 1, isPrimary: true,
              });
              s.dispatchEvent(mk('pointerdown', 200, 200));
              s.dispatchEvent(mk('pointermove', 500, 360));
              s.dispatchEvent(mk('pointerup', 500, 360));
              return 'drawn';
            })()
        """)
        require(result == "drawn", f"pointer draw failed: {result}")
        pump(app, 0.8)
        after = window.editor_controller.scene.elements
        require(len(after) == before + 1,
                f"draw gesture created {len(after) - before} elements, expected 1")
        shape = after[-1]
        require(shape.type == "shape", f"drawn element type: {shape.type}")
        window._set_shape_draw_mode(None)  # back to Select like a user would
        return f"browser pointer-drag created shape {shape.element_id[:14]}… (undoable)"

    bstep( "B6 浏览器画布拖拽绘制形状", b6_draw_tool_shape)
    return window


# --------------------------------------------------------------------------- #
# Journey C — templates (builtin generator compile + save/apply round trip)
# --------------------------------------------------------------------------- #

def journey_c(report: Report, app: QApplication,
              html_window: MotionGraphicEditorWindow | None) -> MotionGraphicEditorWindow | None:
    print("\n[Journey C] 模板：内置生成器编译 → 打开 → 另存为模板 → 重新应用")
    window_c: MotionGraphicEditorWindow | None = None

    def c1_builtin_template() -> str:
        nonlocal window_c
        instance = TITLE_CARD.instantiate()
        scene = TITLE_CARD.compile(instance)
        validation = validate_scene(scene)
        require(validation.is_valid, f"compiled template invalid: {[str(i) for i in validation.issues]}")
        require(len(scene.elements) > 0, "compiled template has no elements")
        tokens = read_scene_tokens(scene.metadata)
        require(tokens.palette.get("primary"), "compiled template missing design tokens")
        window_c = MotionGraphicEditorWindow(scene)
        window_c.resize(1280, 800)
        window_c.show()
        pump(app, 0.4)
        require(window_c.isVisible(), "template window did not show")
        require(len(window_c.editor_controller.scene.elements) == len(scene.elements),
                "editor did not load the compiled template scene")
        return f"TITLE_CARD compiled ({len(scene.elements)} elements) and opened in a real editor"

    report.step("C", "C1 内置 TITLE_CARD 模板编译并在编辑器打开", c1_builtin_template)

    def c2_save_and_apply_template() -> str:
        if html_window is None:
            raise AssertionError("needs the HTML-scene window from Journey B")
        template = html_window._save_as_template("Journey Saved Template")
        require(template is not None, "save-as-template returned None")
        require(any(t.template_id == template.template_id
                    for t in html_window._html_templates.list_templates()),
                "saved template not registered")
        require(len(template.params_schema) > 0, "saved template exposes no parameters")
        before = undo_depth(html_window)
        ok = html_window._apply_template(template.template_id)
        require(ok, "apply_template returned False")
        pump(app, 0.3)
        require(undo_depth(html_window) == before + 1, "template apply should be one undo step")
        layer = html_window.editor_controller.scene.html_layer
        require(layer is not None and layer.layer_id != "journey_html",
                "html layer was not replaced by the template application")
        return (f"saved template '{template.template_id}' ({len(template.params_schema)} params), "
                "re-applied undoably")

    if html_window is not None:
        report.step("C", "C2 另存为模板并重新应用（可撤销）", c2_save_and_apply_template)
    else:
        report.skip("C", "C2 另存为模板并重新应用", "Journey B skipped (offscreen)")
    return window_c


def main() -> int:
    offscreen = "--offscreen" in sys.argv
    import os
    if offscreen:
        # Must be set before QApplication is constructed.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    app = QApplication.instance() or QApplication(sys.argv)
    print(f"Motion Graphic editor — real-user E2E journey  (platform: {app.platformName()})")
    report = Report()

    window_a = journey_a(report, app)
    window_b = journey_b(report, app)
    window_c = journey_c(report, app, window_b)

    for window in (window_c, window_b, window_a):
        if window is not None:
            close_silently(window)
    pump(app, 0.8)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
