#!/usr/bin/env python
"""R6: particle-flow HTML background — real end-to-end journey.

The first benchmark sample "particle HTML background + SVG layers + text +
animation → cache/export" now has full real-chain evidence:

  P1  the particle-flow-background package loads from disk (manifest + HTML + GIF)
  P2  a real editor window opens with the template as its HTML layer and the
      real Qt WebEngine/Chromium page renders the particle canvas
  P3  the canvas is *alive*: two pixel samples taken ~700 ms apart differ
  P4  adding text/rect/circle layers + keyframes through real commands never
      reloads the page (a JS marker survives) and the native overlay
      materializes as DOM driven by --topos-time
  P5  export ≥3 s of 1920×1080 H.264 through the real export service
      (Playwright Chromium capture + FFmpeg encode), then extract and compare
      the first/mid/last frames
  P6  save → close → reopen: elements, HTML layer and particle animation all
      survive the round-trip

Everything runs against real widgets and a real Chromium; nothing is
monkey-patched.  Browser steps report an explicit skip (never a silent pass)
when WebEngine/Chromium/FFmpeg are unavailable.

Readiness gate (R-02): a trivial instrumented page load runs before every
browser step.  When the environment cannot run WebEngine the dependent
steps become one root cause + explicit blocked skips.  Default mode
--require-browser fails the run (CI); --allow-skip-browser accepts the
environment and reports skips (developer diagnostics).

Usage:
  .venv/bin/python scripts/e2e_mg_particle_flow.py [--offscreen]
      [--require-browser | --allow-skip-browser]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mg_webengine_readiness as readiness  # noqa: E402

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

try:  # pragma: no cover - optional dependency, mirrors src/main.py order
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    pass

from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.motion_graphic_template_library import (
    default_motion_graphic_template_root,
    discover_motion_graphic_template_packages,
)
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

W, H, FPS, DURATION = 1920, 1080, 24, 72  # 3 seconds


@dataclass
class StepResult:
    name: str
    ok: bool | None
    detail: str = ""


class Report:
    def __init__(self) -> None:
        self.results: list[StepResult] = []

    def step(self, journey: str, name: str, fn) -> None:  # type: ignore[no-untyped-def]
        started = time.monotonic()
        try:
            detail = str(fn())
            self.results.append(StepResult(name, True, detail))
            print(f"  [PASS] {name}: {detail}")
        except Exception as exc:  # noqa: BLE001 — E2E reports, never crashes
            self.results.append(StepResult(name, False, str(exc)))
            print(f"  [FAIL] {name}: {exc}")
        del started

    def skip(self, journey: str, name: str, reason: str) -> None:
        self.results.append(StepResult(name, None, reason))
        print(f"  [SKIP] {name}: {reason}")

    def fail(self, journey: str, name: str, reason: str) -> None:
        self.results.append(StepResult(name, False, reason))
        print(f"  [FAIL] {name}: {reason}")

    def summary(self) -> int:
        failed = [r for r in self.results if r.ok is False]
        passed = sum(1 for r in self.results if r.ok is True)
        skipped = sum(1 for r in self.results if r.ok is None)
        print(f"\nParticle-flow E2E: {passed} passed, {len(failed)} failed, {skipped} skipped")
        for result in failed:
            print(f"  FAILED {result.name}: {result.detail}")
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
    loop = QEventLoop()
    holder: dict[str, object] = {}

    def done(result: object) -> None:
        holder["value"] = result
        loop.quit()

    page.runJavaScript(script, done)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    if "value" not in holder:
        raise TimeoutError(f"runJavaScript timed out: {script[:80]}…")
    return holder["value"]


CANVAS_SUMMARY_JS = """
(() => {
  const canvas = document.getElementById('particle-flow');
  if (!canvas) return null;
  const ctx = canvas.getContext('2d');
  const {data} = ctx.getImageData(0, 0, canvas.width, canvas.height);
  let lit = 0, sum = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i] + data[i + 1] + data[i + 2] > 45) lit++;
    sum += data[i] + data[i + 1] + data[i + 2];
  }
  return {lit, sum, w: canvas.width, h: canvas.height};
})()
"""


def _particle_scene() -> TeachingSceneDocument:
    """Instantiate the template exactly the way an Effects-panel drop does."""
    from src.features.teaching_animation.render.html_layer.template_promote import apply_html_template
    packages = discover_motion_graphic_template_packages(default_motion_graphic_template_root())
    package = next(p for p in packages if p.template.template_id == "particle-flow-background")
    template = replace(package.template, viewport_width=W, viewport_height=H, duration_frames=DURATION)
    layer = apply_html_template(template, template.default_params(), layer_id="particle-layer")
    scene = replace(
        TeachingSceneDocument.create_default("particle-e2e", width=W, height=H, duration_frames=DURATION),
        elements=(),
    )
    return replace(scene, html_layer=layer)


def main() -> int:
    if "--offscreen" in sys.argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    app = QApplication(sys.argv[:1] or ["e2e_mg_particle_flow"])
    report = Report()
    print(f"\n[Particle Flow E2E] real window · real WebEngine · {W}x{H} · {DURATION / FPS:g}s")

    state: dict[str, object] = {}

    def p1_package_loads() -> str:
        packages = discover_motion_graphic_template_packages(default_motion_graphic_template_root())
        package = next((p for p in packages if p.template.template_id == "particle-flow-background"), None)
        require(package is not None, "particle-flow-background package not discovered")
        assert package is not None
        require(package.template.html_template, "template HTML empty")
        require(package.preview_path is not None and package.preview_path.is_file(),
                "preview.gif missing from the package")
        names = [spec.name for spec in package.template.params_schema]
        require({"accent", "particle_density", "motion_speed"} <= set(names),
                f"params_schema missing keys: {names}")
        state["package"] = package
        return (f"manifest + template.html + preview.gif + {len(names)} params "
                f"({', '.join(names)})")

    report.step("P1", "P1 粒子模板包从磁盘完整加载", p1_package_loads)

    # R-02: readiness preflight — one instrumented trivial page load before
    # any browser-dependent step, so an unavailable environment produces ONE
    # root cause + blocked skips instead of cascading NoneType failures.
    browser_mode = readiness.parse_browser_mode(sys.argv)
    probe = readiness.probe_webengine()
    if not probe.ok:
        readiness.print_diagnostics(probe)
    probe_ok = probe.ok

    window: MotionGraphicEditorWindow | None = None
    try:
        def p2_browser_renders_particles() -> str:
            nonlocal window
            window = MotionGraphicEditorWindow(_particle_scene())
            window.resize(1440, 900)
            window.show()
            pump(app, 0.5)
            preview = window.html_browser_preview
            wait_until(app, lambda: preview.ready, 60.0, "WebEngine page load")
            require(preview.view is not None, "no WebEngine view")
            page = preview.view.page()
            canvas = run_js(page, "!!document.getElementById('particle-flow')")
            require(canvas is True, "particle canvas missing from the live page")
            first = run_js(page, CANVAS_SUMMARY_JS)
            require(isinstance(first, dict) and first.get("w", 0) > 0,
                    f"canvas has no pixels: {first}")
            require(int(first.get("lit", 0)) > 50,
                    f"canvas shows no lit particles: {first}")
            state["first_summary"] = first
            return (f"canvas {first['w']}x{first['h']} with {first['lit']} lit pixels "
                    "rendering in real Chromium")

        if probe_ok:
            report.step("P2", "P2 真实 Chromium 页面渲染粒子画布", p2_browser_renders_particles)
            browser_ok = report.results[-1].ok is True
        elif browser_mode == "allow-skip":
            report.skip("P2", "P2 真实 Chromium 页面渲染粒子画布",
                        f"SKIP (blocked by browser readiness): {probe.reason}")
            browser_ok = False
        else:
            report.fail("P2", "P2 真实 Chromium 页面渲染粒子画布", probe.summary)
            browser_ok = False

        def p3_particles_animate() -> str:
            preview = window.html_browser_preview
            page = preview.view.page()
            run_js(page, "window.__toposParticleMarker = 0x5eed")
            first = run_js(page, CANVAS_SUMMARY_JS)
            time.sleep(0.7)
            pump(app, 0.2)
            second = run_js(page, CANVAS_SUMMARY_JS)
            require(isinstance(first, dict) and isinstance(second, dict), "canvas sampling failed")
            require(first["sum"] != second["sum"] or first["lit"] != second["lit"],
                    f"canvas is frozen: {first} vs {second}")
            return (f"alive: lit {first['lit']} → {second['lit']}, "
                    f"sum {first['sum']} → {second['sum']}")

        if browser_ok:
            report.step("P3", "P3 粒子持续运动（两时点像素采样不同）", p3_particles_animate)
        else:
            report.skip("P3", "P3 粒子持续运动", "SKIP (blocked by browser readiness — see P2/diagnostics)")

        def p4_layers_and_keyframes_without_reload() -> str:
            from src.features.teaching_animation.core.commands import InsertSceneFragmentCommand
            from src.features.teaching_animation.presets.fragment import prepare_insert
            elements = (
                SceneElement("pf-title", "text",
                             transform=SceneTransform(x=560, y=420, width=800, height=120),
                             style={"font_size": 96.0, "color": "#ffffff"},
                             content={"text": "Particle Flow"},
                             local_animation_tracks=(
                                 KeyframeTrack("opacity", (Keyframe(0, 0.0), Keyframe(23, 1.0))),
                             )),
                SceneElement("pf-rect", "shape",
                             transform=SceneTransform(x=180, y=760, width=420, height=90),
                             style={"fill": "#22d9ff"},
                             content={"kind": "rounded_rect"}),
                SceneElement("pf-dot", "shape",
                             transform=SceneTransform(x=1400, y=770, width=80, height=80),
                             style={"fill": "#ffd166"},
                             content={"kind": "ellipse"}),
            )
            host = window.editor_controller.scene
            fragment_elements, parents = prepare_insert(
                _fragment_scene(elements), host, target_x=host.width / 2, target_y=host.height / 2)
            window.editor_controller.execute(
                InsertSceneFragmentCommand(tuple(fragment_elements), parents))
            pump(app, 0.6)
            page = window.html_browser_preview.view.page()
            marker = run_js(page, "window.__toposParticleMarker")
            require(marker == 0x5eed, "page reloaded during edits (marker lost)")
            count = run_js(page, "document.querySelectorAll('[data-topos-native-element]').length")
            require(count == 3, f"expected 3 native elements in DOM, got {count}")
            window.timeline.slider.setValue(23)  # 23/24 ≈ 0.958 s
            pump(app, 0.4)
            value = run_js(page, (
                "getComputedStyle(document.getElementById('topos-native-vector-overlay'))"
                ".getPropertyValue('--topos-time')"))
            require(str(value).strip().startswith("0.95"), f"--topos-time = {value!r}")
            canvas = run_js(page, "!!document.getElementById('particle-flow')")
            require(canvas is True, "particle canvas lost after adding layers")
            return ("3 native layers live (no reload), --topos-time scrubbed, "
                    "particles still mounted")

        if browser_ok:
            report.step("P4", "P4 加文字/矩形/圆形+关键帧且整页不重载", p4_layers_and_keyframes_without_reload)
        else:
            report.skip("P4", "P4 加图层与关键帧", "SKIP (blocked by browser readiness — see P2/diagnostics)")

        export_available = (
            browser_ok
            and shutil.which("ffmpeg") is not None
            and shutil.which("ffprobe") is not None
            and _playwright_available()
        )

        def p5_export_three_seconds() -> str:
            outdir = Path(tempfile.mkdtemp(prefix="mg_particle_export_"))
            out = outdir / "particle.mp4"
            from src.features.teaching_animation.export_service import (
                ExportOptions,
                TeachingAnimationExportService,
            )
            service = TeachingAnimationExportService()
            try:
                result = service.export(
                    window.editor_controller.scene, out,
                    options=ExportOptions(format="mp4", fps=float(FPS), width=W, height=H))
            finally:
                service.close()
            require(result.frame_count == DURATION,
                    f"exported {result.frame_count} frames, expected {DURATION}")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height,nb_frames",
                 "-of", "default=noprint_wrappers=1", str(out)],
                capture_output=True, text=True, check=True)
            info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
            require(info.get("codec_name") == "h264", f"codec: {info}")
            require(info.get("width") == str(W) and info.get("height") == str(H),
                    f"dims: {info}")
            frames_dir = outdir / "frames"
            frames_dir.mkdir()
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(out),
                 "-vf", f"select='eq(n\\,0)+eq(n\\,{DURATION // 2})+eq(n\\,{DURATION - 1})'",
                 "-vsync", "0", str(frames_dir / "f_%02d.png")],
                check=True, capture_output=True)
            sampled = sorted(frames_dir.glob("f_*.png"))
            require(len(sampled) == 3, f"expected 3 extracted frames, got {len(sampled)}")
            from PIL import Image
            sums = []
            for frame in sampled:
                with Image.open(frame) as image:
                    pixels = image.convert("L").resize((96, 54))
                    sums.append(sum(pixels.getdata()))
            require(len(set(sums)) == 3, f"exported frames are identical: {sums}")
            state["export_dir"] = str(outdir)
            return (f"h264 {W}x{H} {DURATION} frames; first/mid/last luma sums "
                    f"{sums[0]}/{sums[1]}/{sums[2]} all distinct; kept in {outdir}")

        if export_available:
            report.step("P5", "P5 导出 3 秒 1080p H.264 并抽帧对比", p5_export_three_seconds)
        else:
            report.skip("P5", "P5 导出验收",
                        "playwright chromium / ffmpeg unavailable — real-browser export pending")

        def p6_save_close_reopen() -> str:
            scene = window.editor_controller.scene
            payload = scene.to_json()
            require("pf-title" in payload and "particle-flow" in payload,
                    "saved scene lost elements or the HTML layer")
            window.editor_controller.mark_clean()
            window.close()
            pump(app, 0.4)
            reopened_doc = TeachingSceneDocument.from_json(payload)
            require(len(reopened_doc.elements) == 3, "reopened scene lost elements")
            require(reopened_doc.html_layer is not None, "reopened scene lost the HTML layer")
            window2 = MotionGraphicEditorWindow(reopened_doc)
            window2.resize(1440, 900)
            window2.show()
            pump(app, 0.6)
            state["window2"] = window2
            preview = window2.html_browser_preview
            wait_until(app, lambda: preview.ready, 60.0, "reopened WebEngine page load")
            page = preview.view.page()
            canvas = run_js(page, "!!document.getElementById('particle-flow')")
            require(canvas is True, "particle canvas missing after reopen")
            first = run_js(page, CANVAS_SUMMARY_JS)
            time.sleep(0.7)
            pump(app, 0.2)
            second = run_js(page, CANVAS_SUMMARY_JS)
            require(first["sum"] != second["sum"] or first["lit"] != second["lit"],
                    f"reopened canvas frozen: {first} vs {second}")
            window2.editor_controller.mark_clean()
            window2.close()
            return ("3 elements + HTML layer survive JSON round-trip; particles "
                    f"still animate after reopen (lit {first['lit']} → {second['lit']})")

        if browser_ok:
            report.step("P6", "P6 保存→关闭→重开，粒子仍运动", p6_save_close_reopen)
        else:
            report.skip("P6", "P6 保存重开", "SKIP (blocked by browser readiness — see P2/diagnostics)")

    finally:
        if window is not None:
            try:
                window.editor_controller.mark_clean()
                window.close()
            except Exception:  # noqa: BLE001 — teardown must not mask results
                pass
    return report.summary()


def _fragment_scene(elements) -> TeachingSceneDocument:  # type: ignore[no-untyped-def]
    return replace(
        TeachingSceneDocument.create_default("particle-fragment", duration_frames=DURATION),
        elements=tuple(elements),
    )


def _playwright_available() -> bool:
    try:
        from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
            PlaywrightChromiumBackend,
        )
        return bool(PlaywrightChromiumBackend().is_available())
    except Exception:  # noqa: BLE001 — optional browser
        return False


if __name__ == "__main__":
    raise SystemExit(main())
