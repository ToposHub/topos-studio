#!/usr/bin/env python3
"""L01: Color bottom tool dock height cap — native regression driver.

Phases (``--phase``), sharing one config dir:

``seed``     Lift the cap, drag the bottom docks tall, persist that oversized
             blob as the saved "Color" layout (simulates a pre-fix user
             layout), and prove the running window re-clamps when the
             production cap is restored.
``restore``  Fresh process on the same config: startup must clamp the
             restored oversized layout; separator-drag extreme, float/
             re-dock and Edit→Color round-trip must respect the cap; a
             normal save persists the capped layout.
``restart``  Fresh process after the capped save: still capped on startup.
``radius``   Standalone wheel-radius measurements at multiple widths.

Every phase writes a JSON report plus native screenshots and exits non-zero
on the first violated assertion.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COLOR_TOOL_DOCKS = [
    "dockDenoise", "dockDeblur", "dockGrading", "dockCurves",
    "dockColorMapping", "dockBlurSharpen", "dockGrain", "dockMasks",
    "dockQualifier", "dockRaw", "dockAnalysis", "dockCrop", "dockTracker",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("seed", "restore", "restart", "radius"),
                        required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "docs/validation/color_resources/l01_layout_cap")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config_path = args.config_dir / "config.json"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            "locale": "en_US", "work_mode": "video", "current_page": "Color",
            "show_welcome_on_startup": False, "auto_load_last_project": False,
            "check_updates_on_startup": False, "auto_save_interval": 0}))
    os.environ["TOPOS_CONFIG_PATH"] = str(config_path)
    os.environ["TOPOS_PRODUCT_MODE"] = "studio"
    os.environ["TOPOS_TEST_MODE"] = "1"

    if args.phase == "radius":
        return _radius_phase(output)

    import src.main as app_main
    from PyQt6.QtCore import QObject, QTimer, Qt
    from PyQt6.QtWidgets import QApplication
    from src.app.main_window import MainWindow
    from src.app.controllers.dock_layout_controller import (
        _COLOR_TOOL_DOCK_MAX_HEIGHT, _UNRESTRICTED_DOCK_HEIGHT,
    )

    def safe_object_name(obj: Any) -> str:
        try:
            return obj.objectName()
        except Exception:  # noqa: BLE001 — non-QObject attrs are skipped
            return ""

    def dock_attr(host: Any, object_name: str) -> str:
        for attr in dir(host):
            try:
                if safe_object_name(getattr(host, attr)) == object_name:
                    return attr
            except Exception:  # noqa: BLE001 — property access may raise
                continue
        return ""

    def dock_for(host: Any, object_name: str) -> Any:
        attr = dock_attr(host, object_name)
        return getattr(host, attr) if attr else None

    def settle(app: Any, rounds: int = 8) -> None:
        for _ in range(rounds):
            app.processEvents()
            time.sleep(0.05)

    def dock_rows(host: Any) -> list[dict]:
        rows = []
        for attr in dir(host):
            if not attr.startswith("dock"):
                continue
            try:
                dock = getattr(host, attr)
                name = safe_object_name(dock)
                if not name or dock.isFloating is None:
                    continue
                rows.append({
                    "attr": attr, "objectName": name,
                    "area": host.dockWidgetArea(dock).value,
                    "floating": dock.isFloating(), "visible": dock.isVisible(),
                    "x": dock.x(), "y": dock.y(), "w": dock.width(),
                    "h": dock.height(), "minH": dock.minimumHeight(),
                    "maxH": dock.maximumHeight(),
                    "tabbed_with": len(host.tabifiedDockWidgets(dock)),
                })
            except Exception:  # noqa: BLE001 — non-dock attrs are skipped
                continue
        return sorted(rows, key=lambda r: (r["area"], r["y"], r["x"]))

    def capped_rows(host: Any) -> list[dict]:
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea.value
        return [r for r in dock_rows(host)
                if r["objectName"] in COLOR_TOOL_DOCKS
                and r["area"] == bottom and not r["floating"] and r["visible"]]

    def set_caps(host: Any, height: int) -> None:
        for name in COLOR_TOOL_DOCKS:
            dock = dock_for(host, name)
            if dock is not None:
                dock.setMaximumHeight(height)

    def drag_bottom_to(host: Any, size: int) -> None:
        docks = [dock_for(host, n) for n in COLOR_TOOL_DOCKS]
        docks = [d for d in docks if d is not None and d.isVisible()
                 and not d.isFloating()]
        host.resizeDocks(docks, [size] * len(docks), Qt.Orientation.Vertical)

    report: dict[str, Any] = {"phase": args.phase, "cap": _COLOR_TOOL_DOCK_MAX_HEIGHT,
                              "scale_factor": os.environ.get("QT_SCALE_FACTOR", "1.0"),
                              "checks": {}}

    class Driver(QObject):
        def __init__(self, host: Any) -> None:
            super().__init__(host)
            self.host = host
            self.step = 0
            self.started = time.monotonic()
            self.timer = QTimer(self)
            self.timer.setInterval(400)
            self.timer.timeout.connect(self.tick)
            self.timer.start()

        def finish(self, success: bool, error: str = "") -> None:
            self.timer.stop()
            report["status"] = "passed" if success else "failed"
            report["error"] = error
            (output / f"{args.phase}_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"phase": args.phase, "status": report["status"],
                              "error": error}), flush=True)
            QApplication.instance().exit(0 if success else 1)

        def tick(self) -> None:
            try:
                with open(output / "tick_trace.log", "a") as trace:
                    trace.write(f"{time.monotonic() - self.started:6.1f}s "
                                f"step={self.step} visible={self.host.isVisible()}\n")
                if time.monotonic() - self.started > 120:
                    raise TimeoutError("phase did not complete in 120 seconds")
                host, app = self.host, QApplication.instance()
                if self.step == 0:
                    if not host.isVisible():
                        return
                    host.resize(1600, 1400)
                    app.processEvents()
                    report["docks_startup"] = dock_rows(host)
                    self.step += 1
                    return
                if args.phase == "seed":
                    return self.seed_tick(app, host)
                if args.phase == "restore":
                    return self.restore_tick(app, host)
                if args.phase == "restart":
                    return self.restart_tick(app, host)
            except Exception as exc:  # noqa: BLE001 — report and exit non-zero
                import traceback
                traceback.print_exc()
                self.finish(False, f"{type(exc).__name__}: {exc}")

        def seed_tick(self, app: Any, host: Any) -> None:
            if self.step == 1:
                # Lift caps and drag the bottom tool row absurdly tall —
                # this is what a pre-fix saved layout looked like.
                set_caps(host, _UNRESTRICTED_DOCK_HEIGHT)
                app.processEvents()
                drag_bottom_to(host, 1100)
                settle(app)
                heights = {r["objectName"]: r["h"] for r in capped_rows(host)}
                report["checks"]["oversized_heights"] = heights
                if not heights or max(heights.values()) <= _COLOR_TOOL_DOCK_MAX_HEIGHT:
                    raise AssertionError("seed could not create an oversized layout")
                host.grab().save(str(output / "seed_oversized.png"))
                self.step += 1
                return
            if self.step == 2:
                # Restore the production cap: the running window must clamp
                # immediately, handing the released height to Viewer/Nodes.
                viewport_before = host.dock_viewport.height()
                nodes_before = host.dock_layers.height()
                set_caps(host, _COLOR_TOOL_DOCK_MAX_HEIGHT)
                settle(app)
                heights = {r["objectName"]: r["h"] for r in capped_rows(host)}
                report["checks"]["capped_after_sync"] = heights
                assert heights and all(h <= _COLOR_TOOL_DOCK_MAX_HEIGHT
                                       for h in heights.values()), \
                    "cap restoration left a bottom tool dock above the cap"
                report["checks"]["released_height"] = {
                    "viewport_delta": host.dock_viewport.height() - viewport_before,
                    "nodes_delta": host.dock_layers.height() - nodes_before}
                host.grab().save(str(output / "seed_capped.png"))
                # Persist an oversized blob as the saved Color layout by
                # lifting the caps once more right before saving state.
                set_caps(host, _UNRESTRICTED_DOCK_HEIGHT)
                drag_bottom_to(host, 1100)
                settle(app)
                report["checks"]["seed_blob_heights"] = {
                    r["objectName"]: r["h"] for r in capped_rows(host)}
                blob = host.saveState().toHex().data().decode()
                layouts = host.config_manager.get("page_layouts")
                layouts = dict(layouts) if isinstance(layouts, dict) else {}
                layouts["Color"] = blob
                host.config_manager.set("page_layouts", layouts, auto_save=False)
                host.config_manager.save_config()
                self.finish(True)
                return

        def restore_tick(self, app: Any, host: Any) -> None:
            if self.step == 1:
                # Startup restored the oversized blob written by the seed
                # phase; the persistent maximumHeight must clamp it.
                heights = {r["objectName"]: r["h"] for r in capped_rows(host)}
                report["checks"]["startup_clamped"] = heights
                assert heights, "no visible docked bottom tool docks found"
                assert all(h <= _COLOR_TOOL_DOCK_MAX_HEIGHT
                           for h in heights.values()), \
                    ("saved oversized layout survived startup restore: "
                     + json.dumps(heights))
                host.grab().save(str(output / "restore_startup_clamped.png"))
                self.step += 1
                return
            if self.step == 2:
                # Separator drag to the extreme must stop at the cap.
                drag_bottom_to(host, 2000)
                settle(app)
                heights = {r["objectName"]: r["h"] for r in capped_rows(host)}
                report["checks"]["drag_extreme"] = heights
                assert all(h <= _COLOR_TOOL_DOCK_MAX_HEIGHT
                           for h in heights.values()), \
                    "separator drag exceeded the cap"
                host.grab().save(str(output / "restore_drag_extreme.png"))
                self.step += 1
                return
            if self.step == 3:
                # Floating lifts the cap; re-docking at the bottom restores it.
                dock = host.dock_grading
                dock.setFloating(True)
                app.processEvents()
                dock.resize(900, 760)
                settle(app)
                report["checks"]["floating"] = {
                    "height": dock.height(), "max": dock.maximumHeight()}
                assert dock.maximumHeight() > _COLOR_TOOL_DOCK_MAX_HEIGHT, \
                    "floating did not lift the cap"
                assert dock.height() > _COLOR_TOOL_DOCK_MAX_HEIGHT, \
                    "floating window could not be resized above the cap"
                host.grab().save(str(output / "restore_floating.png"))
                dock.setFloating(False)
                host.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
                settle(app)
                report["checks"]["redocked"] = {
                    "height": dock.height(), "max": dock.maximumHeight()}
                assert dock.maximumHeight() == _COLOR_TOOL_DOCK_MAX_HEIGHT, \
                    "re-docking at the bottom did not restore the cap"
                self.step += 1
                return
            if self.step == 4:
                # Edit → Color round trip keeps the cap, then save normally.
                host.switch_to_page("Edit")
                settle(app, rounds=10)
                host.switch_to_page("Color")
                settle(app, rounds=10)
                heights = {r["objectName"]: r["h"] for r in capped_rows(host)}
                report["checks"]["after_page_roundtrip"] = heights
                assert heights and all(h <= _COLOR_TOOL_DOCK_MAX_HEIGHT
                                       for h in heights.values()), \
                    "Edit→Color round trip lost the cap"
                host.grab().save(str(output / "restore_page_roundtrip.png"))
                host._save_current_layout_to_store("Color")
                host.config_manager.save_config()
                self.finish(True)
                return

        def restart_tick(self, app: Any, host: Any) -> None:
            if self.step == 1:
                heights = {r["objectName"]: r["h"] for r in capped_rows(host)}
                report["checks"]["restart_clamped"] = heights
                assert heights and all(h <= _COLOR_TOOL_DOCK_MAX_HEIGHT
                                       for h in heights.values()), \
                    "cap was not durable across save/restart"
                host.grab().save(str(output / "restart_capped.png"))
                report["docks_final"] = dock_rows(host)
                self.finish(True)
                return

    original_init = MainWindow.__init__

    def validation_init(self: Any, *init_args: Any, **kwargs: Any) -> None:
        original_init(self, *init_args, **kwargs)
        self._layout_cap_driver = Driver(self)

    MainWindow.__init__ = validation_init
    sys.argv = [sys.argv[0], "--product", "studio", "--no-startup-dialog"]
    return app_main.main()


def _radius_phase(output: Path) -> int:
    """Standalone wheel-radius + minimum-height measurements (no project)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    report: dict[str, Any] = {"phase": "radius",
                              "scale_factor": os.environ.get("QT_SCALE_FACTOR", "1.0")}
    try:
        from src.app.controllers.dock_layout_controller import (
            _COLOR_TOOL_DOCK_MAX_HEIGHT,
        )
        from src.features.color.ui.color_wheel import GradingPanel
        # The real app applies the global dark theme (pixel-sized fonts).
        # Without it the system 13pt font inflates the numeric rows and a
        # bare probe under-measures the wheel (98px at the cap panel height).
        from src.shared.ui.style_sheet import DARK_THEME
        app.setStyleSheet(DARK_THEME)
        panel = GradingPanel()
        panel.show()
        radii: dict[str, dict[str, float]] = {}
        for width in (900, 1100, 1260, 1500):
            row: dict[str, float] = {}
            for label, height in (("at_cap", _COLOR_TOOL_DOCK_MAX_HEIGHT - 7),
                                  ("above_cap", _COLOR_TOOL_DOCK_MAX_HEIGHT + 100)):
                panel.resize(width, height)
                for _ in range(6):
                    app.processEvents()
                row[label] = float(panel.lift_wheel._wheel_canvas._geometry()[1])
            radii[str(width)] = row
        report["wheel_radii"] = radii
        report["radius_reaches_100_at_cap"] = all(
            v["at_cap"] == 100.0 for v in radii.values())
        report["radius_capped_above_cap"] = all(
            v["at_cap"] == v["above_cap"] for v in radii.values())
        assert report["radius_reaches_100_at_cap"], \
            f"wheel radius at the dock cap is not 100px: {radii}"
        assert report["radius_capped_above_cap"], \
            f"extra dock height still grows the wheel: {radii}"
        panel.close()
        panel.deleteLater()

        # Minimum operable heights of the panels that build standalone.
        panels = {"GradingPanel": GradingPanel}
        try:
            from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel
            panels["BlurSharpenPanel"] = BlurSharpenPanel
        except Exception:  # noqa: BLE001 — optional import
            pass
        try:
            from src.features.color.ui.qualifier_panel import QualifierPanel
            panels["QualifierPanel"] = QualifierPanel
        except Exception:  # noqa: BLE001 — optional import
            pass
        mins: dict[str, int] = {}
        for name, cls in panels.items():
            widget = cls()
            widget.show()
            app.processEvents()
            mins[name] = int(widget.minimumSizeHint().height())
            widget.close()
            widget.deleteLater()
        report["minimum_size_hint_heights"] = mins
        (output / "radius_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"phase": "radius", "status": "passed"}), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (output / "radius_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
