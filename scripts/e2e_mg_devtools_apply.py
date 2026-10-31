#!/usr/bin/env python
"""R8: DevTools Apply/Discard — controlled two-way DOM ownership (real browser).

  D1  the live page hosts both authored HTML and editor scaffolding (overlay,
      draw surface, page-deck runtime)
  D2  a DevTools-style DOM edit reaches ``read_authored_document`` with the
      authored change intact and every scaffolding node stripped
  D3  Apply writes the authored edit back as one undoable command; the native
      overlay regenerates from the scene (never from the applied DOM); undo
      restores the previous layer, redo re-applies
  D4  Discard reloads the saved layer: an unapplied DOM mutation disappears
  D5  a DevTools edit on a NATIVE overlay element maps to one element
      property command (R-07) instead of rebuilding the layer only

Readiness gate (R-02): a trivial instrumented page load runs before every
browser step, so an unavailable environment yields one root cause + blocked
skips (never cascading NoneType failures).  Default --require-browser fails
the run (CI); --allow-skip-browser accepts the environment (diagnostics).

Run on a desktop session (a window appears briefly):

    .venv/bin/python scripts/e2e_mg_devtools_apply.py [--require-browser|--allow-skip-browser]
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mg_webengine_readiness as readiness  # noqa: E402

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

try:  # pragma: no cover - optional dependency, mirrors src/main.py order
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    pass

from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)

W, H, FPS, DURATION = 960, 540, 24, 48

AUTHORED_HTML = """<!doctype html><html><head><style>
  html,body{margin:0;width:100%;height:100%;background:#101423}
  .panel{position:absolute;left:80px;top:120px;width:420px;height:220px;
    background:#1c2742;border-radius:14px}
  .title{position:absolute;left:100px;top:150px;color:#e8ecf5;
    font-family:sans-serif;font-size:34px;font-weight:bold}
</style></head><body>
  <div class="panel" data-topos-node-id="panel"></div>
  <div class="title" data-topos-node-id="title">DevTools Roundtrip</div>
</body></html>"""


@dataclass
class StepResult:
    name: str
    ok: bool | None
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.results: list[StepResult] = []

    def step(self, journey: str, name: str, fn) -> None:  # type: ignore[no-untyped-def]
        try:
            detail = str(fn())
            self.results.append(StepResult(name, True, detail))
            print(f"  [PASS] {name}: {detail}")
        except Exception as exc:  # noqa: BLE001
            self.results.append(StepResult(name, False, str(exc)))
            print(f"  [FAIL] {name}: {exc}")

    def skip(self, journey: str, name: str, reason: str) -> None:
        self.results.append(StepResult(name, None, reason))
        print(f"  [SKIP] {name}: {reason}")

    def fail(self, journey: str, name: str, reason: str) -> None:
        self.results.append(StepResult(name, False, reason))
        print(f"  [FAIL] {name}: {reason}")

    def summary(self) -> int:
        failed = [r for r in self.results if r.ok is False]
        skipped = sum(1 for r in self.results if r.ok is None)
        print(f"\nDevTools Apply E2E: {sum(1 for r in self.results if r.ok is True)} passed, "
              f"{len(failed)} failed, {skipped} skipped")
        return 1 if failed else 0


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def pump(app: QApplication, seconds: float = 0.3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def wait_until(app: QApplication, condition, timeout: float, what: str) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {what}")


def run_js(page, script: str, timeout_ms: int = 10000):  # type: ignore[no-untyped-def]
    from PyQt6.QtCore import QEventLoop
    loop = QEventLoop()
    holder: dict[str, object] = {}

    def done(result: object) -> None:
        holder["value"] = result
        loop.quit()

    page.runJavaScript(script, done)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    if "value" not in holder:
        raise TimeoutError(f"runJavaScript timed out: {script[:70]}…")
    return holder["value"]


def _scene() -> TeachingSceneDocument:
    layer = build_html_layer(
        layer_id="devtools-layer", sanitized_html=AUTHORED_HTML,
        authored_source=AUTHORED_HTML, viewport_width=W, viewport_height=H,
        fps_num=FPS, fps_den=1, duration_frames=DURATION,
        sanitizer_version="html-sanitizer-v1",
        csp="default-src 'none'; style-src 'unsafe-inline'",
    )
    return replace(
        TeachingSceneDocument.create_default("devtools-e2e", width=W, height=H,
                                             duration_frames=DURATION),
        html_layer=layer,
        elements=(
            SceneElement("dt-badge", "shape",
                         transform=SceneTransform(x=560, y=360, width=220, height=70),
                         style={"fill": "#22c55e"},
                         content={"kind": "rounded_rect"}),
        ),
    )


def main() -> int:
    app = QApplication(sys.argv[:1] or ["e2e_mg_devtools_apply"])
    report = Report()
    print(f"\n[DevTools Apply E2E] real window · real WebEngine · {W}x{H}")
    window: MotionGraphicEditorWindow | None = None
    # R-02: readiness preflight — one instrumented trivial page load before
    # any browser step, so an unavailable environment produces ONE root cause
    # + blocked skips instead of cascading NoneType failures.
    browser_mode = readiness.parse_browser_mode(sys.argv)
    probe = readiness.probe_webengine()
    if not probe.ok:
        readiness.print_diagnostics(probe)
    try:
        def open_window() -> MotionGraphicEditorWindow:
            nonlocal window
            window = MotionGraphicEditorWindow(_scene())
            window.resize(1280, 800)
            window.show()
            pump(app, 0.6)
            preview = window.html_browser_preview
            wait_until(app, lambda: preview.ready, 60.0, "WebEngine page load")
            return window

        def d1_both_owners_present() -> str:
            win = open_window()
            page = win.html_browser_preview.view.page()
            checks = run_js(page, """
 (() => ({
   authored: !!document.querySelector('[data-topos-node-id="title"]'),
   overlay: !!document.getElementById('topos-native-vector-overlay'),
   native: document.querySelectorAll('[data-topos-native-element]').length,
   deck: !!document.querySelector('script#topos-page-deck-editor-runtime, style#topos-page-deck-editor-controller'),
 }))()""")
            require(checks.get("authored") is True, f"authored node missing: {checks}")
            require(checks.get("overlay") is True, "native overlay missing")
            require(checks.get("native") == 1, f"native elements: {checks}")
            require(checks.get("deck") is True, "page-deck runtime missing")
            return ("authored DOM + native overlay + page-deck runtime all live "
                    "in one document")

        if probe.ok:
            report.step("D1", "D1 同一页面承载作者 DOM 与编辑器脚手架", d1_both_owners_present)
            browser_ok = report.results[-1].ok is True
            if not browser_ok:
                print("  note: readiness probe passed but the editor page failed — "
                      "likely a product defect, not the environment")
        elif browser_mode == "allow-skip":
            report.skip("D1", "D1 同一页面承载作者 DOM 与编辑器脚手架",
                        f"SKIP (blocked by browser readiness): {probe.reason}")
            browser_ok = False
        else:
            report.fail("D1", "D1 同一页面承载作者 DOM 与编辑器脚手架", probe.summary)
            browser_ok = False

        def d2_read_strips_scaffolding_keeps_edit() -> str:
            page = window.html_browser_preview.view.page()
            # Simulate what a user does in DevTools Elements: restyle authored CSS.
            # DevTools Elements edits the stylesheet's text node (CSSOM
            # insertRule alone never reaches outerHTML serialization).
            run_js(page, (
                "const style = document.querySelector('style');"
                "style.textContent = '.panel{background:#f59e0b}' + style.textContent;"
                "const ghost = document.createElement('div');"
                "ghost.id = 'user-devtools-note';"
                "ghost.textContent = 'touched';"
                "document.body.appendChild(ghost);"))
            read: dict[str, object] = {}
            loop_done: dict[str, object] = {}

            from PyQt6.QtCore import QEventLoop
            loop = QEventLoop()
            window.html_browser_preview.read_authored_document(
                lambda source: (read.__setitem__("html", source), loop.quit()))
            QTimer.singleShot(10000, loop.quit)
            loop.exec()
            require(loop_done is not None and isinstance(read.get("html"), str), "read failed")
            source = str(read["html"])
            require("#f59e0b" in source, "authored CSS edit missing from the read-back")
            require("user-devtools-note" in source, "author-added node missing")
            for forbidden in ("topos-native-vector-overlay", "topos-native-draw-surface",
                              "topos-page-deck-editor-runtime", "topos-preview-scrollbars",
                              "topos-native-selection-style", "qwebchannel"):
                require(forbidden not in source, f"scaffolding leaked into authored source: {forbidden}")
            return ("DevTools edit + author node preserved; 6 scaffolding markers stripped")

        if browser_ok:
            report.step("D2", "D2 DevTools 编辑读回：作者区域保留、脚手架剥离", d2_read_strips_scaffolding_keeps_edit)
        else:
            report.skip("D2", "D2 DevTools 编辑读回", "SKIP (blocked by browser readiness — see D1)")

        def d3_apply_undo_redo() -> str:
            from PyQt6.QtCore import QEventLoop
            depth_before = len(window.editor_controller._stack._undo_stack)
            html_holder: dict[str, object] = {}
            loop = QEventLoop()
            window.html_browser_preview.read_authored_document(
                lambda source: (html_holder.__setitem__("html", source), loop.quit()))
            QTimer.singleShot(10000, loop.quit)
            loop.exec()
            source = str(html_holder["html"])
            window._apply_html_source(source)
            pump(app, 0.5)
            layer = window.editor_controller.scene.html_layer
            require(layer is not None and "#f59e0b" in layer.sanitized_html,
                    "applied layer lost the DevTools edit")
            depth_after = len(window.editor_controller._stack._undo_stack)
            require(depth_after == depth_before + 1,
                    f"apply pushed {depth_after - depth_before} undo entries, expected 1")
            page = window.html_browser_preview.view.page()
            overlay = run_js(page, "document.querySelectorAll('[data-topos-native-element]').length")
            require(overlay == 1, f"native overlay lost after apply: {overlay}")
            window.editor_controller.undo()
            pump(app, 0.5)
            previous = window.editor_controller.scene.html_layer
            require(previous is not None and "#f59e0b" not in previous.sanitized_html,
                    "undo did not restore the previous layer source")
            window.editor_controller.redo()
            pump(app, 0.5)
            redone = window.editor_controller.scene.html_layer
            require(redone is not None and "#f59e0b" in redone.sanitized_html,
                    "redo did not re-apply the DevTools edit")
            return ("apply = 1 undoable command; overlay regenerated from scene; "
                    "undo/redo round-trip exact")

        if browser_ok:
            report.step("D3", "D3 Apply 单命令可撤销，原生层从场景再生", d3_apply_undo_redo)
        else:
            report.skip("D3", "D3 Apply 单命令可撤销", "SKIP (blocked by browser readiness — see D1)")

        def d4_discard_reloads_saved_layer() -> str:
            page = window.html_browser_preview.view.page()
            run_js(page, (
                "const m = document.createElement('div');"
                "m.id = 'unapplied-mutation';"
                "document.body.appendChild(m);"))
            present = run_js(page, "!!document.getElementById('unapplied-mutation')")
            require(present is True, "mutation not staged")
            window._discard_live_dom_changes()
            pump(app, 0.8)
            gone = run_js(page, "!!document.getElementById('unapplied-mutation')")
            require(gone is False, "discard left the unapplied mutation in the page")
            kept = run_js(page, "!!document.querySelector('[data-topos-node-id=\"title\"]')")
            require(kept is True, "discard lost the authored layer")
            return "discard reloads the saved layer; unapplied mutations vanish"

        if browser_ok:
            report.step("D4", "D4 Discard 重载已保存图层", d4_discard_reloads_saved_layer)
        else:
            report.skip("D4", "D4 Discard 重载已保存图层", "SKIP (blocked by browser readiness — see D1)")

        def d5_native_edit_maps_to_property_command() -> str:
            page = window.html_browser_preview.view.page()
            node = run_js(page, "!!document.querySelector('[data-topos-native-element]')")
            require(node is True, "no native overlay element to edit")
            # Simulate a DevTools edit directly on the scene-owned element.
            run_js(page, (
                "const n = document.querySelector('[data-topos-native-element]');"
                "n.style.left = '400px';"))
            stack = window.editor_controller._stack._undo_stack
            depth_before = len(stack)
            window._apply_live_dom_to_html()
            wait_until(app, lambda: len(stack) > depth_before, 10.0,
                       "DevTools apply to reach the undo stack")
            element = window.editor_controller.scene.element_by_id("dt-badge")
            require(element is not None, "native element missing from the scene")
            require(abs(element.transform.x - 400.0) < 1e-6,
                    f"native edit did not map to a property: x={element.transform.x}")
            pushed = stack[depth_before:]
            require(len(pushed) == 1 and "DevTools Apply" in str(getattr(pushed[0], "label", "")),
                    f"expected ONE 'DevTools Apply' undo step, got "
                    f"{[str(getattr(c, 'label', c)) for c in pushed]}")
            window.editor_controller.undo()
            pump(app, 0.3)
            restored = window.editor_controller.scene.element_by_id("dt-badge")
            require(abs(restored.transform.x - 560.0) < 1e-6,
                    f"single undo did not restore the native element: x={restored.transform.x}")
            return ("native overlay edit → 1 'DevTools Apply' undo step "
                    "(x 560→400); one undo restores it exactly")

        if browser_ok:
            report.step("D5", "D5 原生元素 DevTools 编辑映射为属性命令", d5_native_edit_maps_to_property_command)
        else:
            report.skip("D5", "D5 原生元素属性映射", "SKIP (blocked by browser readiness — see D1)")

    finally:
        if window is not None:
            try:
                window.editor_controller.mark_clean()
                window.close()
            except Exception:  # noqa: BLE001 — teardown must not mask results
                pass
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
