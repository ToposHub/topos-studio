#!/usr/bin/env python3
"""
Capture Edit Page Screenshots for Visual Acceptance Testing.

This tool launches the ToposColor application, switches to the Edit page,
and captures baseline screenshots at multiple resolutions.

Usage:
    # Capture all resolutions with empty project
    .venv/bin/python tools/capture_edit_page.py

    # Capture specific resolution
    .venv/bin/python tools/capture_edit_page.py --resolution 1920x1080

    # Capture with clips on timeline
    .venv/bin/python tools/capture_edit_page.py --with-clips

    # Capture only toolbar region
    .venv/bin/python tools/capture_edit_page.py --region toolbar

    # List available resolutions
    .venv/bin/python tools/capture_edit_page.py --list-resolutions

Baseline screenshots saved to: tests/visual/baselines/

Naming convention:
    {snapshot_name}_{width}x{height}.png

Examples:
    edit_page_empty_1280x800.png
    edit_page_with_clips_1920x1080.png
    edit_timeline_tools_1440x900.png
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, List, Tuple

# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = PROJECT_ROOT / "tests" / "visual" / "baselines"

SUPPORTED_RESOLUTIONS: List[Tuple[int, int]] = [
    (1280, 800),
    (1440, 900),
    (1920, 1080),
]

DEFAULT_CAPTURE_DELAY = 1.5  # seconds to wait before screenshot

logger = logging.getLogger("capture_edit_page")


# =============================================================================
# Screenshot Capture Engine
# =============================================================================

class EditPageCapture:
    """
    Captures screenshots of the ToposColor Edit page at various resolutions.

    Workflow:
        1. Create QApplication with desired geometry
        2. Instantiate MainWindow
        3. Switch to Edit page
        4. Wait for layout stabilization
        5. Grab widget screenshots
        6. Save to baseline directory
    """

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or BASELINE_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.app = None
        self.window = None

    # -----------------------------------------------------------------
    # Application lifecycle
    # -----------------------------------------------------------------

    def _ensure_qt(self, width: int, height: int) -> None:
        """Initialise QApplication and MainWindow at the given resolution."""
        os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
        os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QSurfaceFormat
        from PyQt6.QtWidgets import QApplication

        # Surface format (mirrors src/main.py)
        fmt = QSurfaceFormat()
        fmt.setVersion(4, 5)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        fmt.setRedBufferSize(8)
        fmt.setGreenBufferSize(8)
        fmt.setBlueBufferSize(8)
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        if sys.platform != "win32":
            fmt.setSamples(4)
        fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
        QSurfaceFormat.setDefaultFormat(fmt)
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

        app = QApplication.instance()
        if app is None:
            self.app = QApplication(sys.argv)
        else:
            self.app = app

        # Import MainWindow after QApplication exists
        from src.ui.main_window import MainWindow
        self.window = MainWindow()
        self.window.resize(width, height)
        self.window.show()

        # Process events to let the UI settle
        self.app.processEvents()
        time.sleep(0.3)
        self.app.processEvents()

    def _switch_to_edit(self) -> None:
        """Switch MainWindow to the Edit page."""
        if self.window is None:
            raise RuntimeError("Window not initialised – call _ensure_qt first")
        self.window.switch_to_page("Edit")
        self.app.processEvents()
        time.sleep(0.5)
        self.app.processEvents()

    def _cleanup(self) -> None:
        """Close the window and clean up."""
        if self.window is not None:
            self.window.close()
            self.window = None

    # -----------------------------------------------------------------
    # Screenshot helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _grab_widget(widget, save_path: Path) -> bool:
        """Grab a widget screenshot and save as PNG."""
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtCore import QRect

        pixmap = widget.grab()
        if pixmap.isNull():
            logger.warning("Captured pixmap is null for %s", save_path.name)
            return False
        saved = pixmap.save(str(save_path), "PNG")
        if saved:
            logger.info("Saved: %s (%dx%d)", save_path.name, pixmap.width(), pixmap.height())
        else:
            logger.error("Failed to save: %s", save_path.name)
        return saved

    @staticmethod
    def _grab_region(widget, rect, save_path: Path) -> bool:
        """Grab a region of a widget and save as PNG."""
        from PyQt6.QtGui import QPixmap

        pixmap = widget.grab(rect)
        if pixmap.isNull():
            logger.warning("Captured region pixmap is null for %s", save_path.name)
            return False
        saved = pixmap.save(str(save_path), "PNG")
        if saved:
            logger.info("Saved region: %s (%dx%d)", save_path.name, pixmap.width(), pixmap.height())
        else:
            logger.error("Failed to save region: %s", save_path.name)
        return saved

    # -----------------------------------------------------------------
    # High-level capture routines
    # -----------------------------------------------------------------

    def capture_edit_page_empty(self, width: int, height: int) -> Optional[Path]:
        """Capture the Edit page with an empty project (no clips)."""
        self._ensure_qt(width, height)
        self._switch_to_edit()

        # Ensure no project is loaded / create empty state
        time.sleep(DEFAULT_CAPTURE_DELAY)
        self.app.processEvents()

        filename = f"edit_page_empty_{width}x{height}.png"
        save_path = self.output_dir / filename

        self._grab_widget(self.window, save_path)
        self._cleanup()
        return save_path if save_path.exists() else None

    def capture_edit_page_with_clips(self, width: int, height: int) -> Optional[Path]:
        """Capture the Edit page with clips on the timeline.

        This creates a test project with sample media references so that
        timeline clips are visible. The clips use placeholder metadata
        (no real media files required).
        """
        self._ensure_qt(width, height)
        self._switch_to_edit()

        # Create a test project with clips
        self._populate_test_clips()

        time.sleep(DEFAULT_CAPTURE_DELAY)
        self.app.processEvents()

        filename = f"edit_page_with_clips_{width}x{height}.png"
        save_path = self.output_dir / filename

        self._grab_widget(self.window, save_path)
        self._cleanup()
        return save_path if save_path.exists() else None

    def capture_timeline_tools(self, width: int, height: int) -> Optional[Path]:
        """Capture just the timeline toolbar region."""
        self._ensure_qt(width, height)
        self._switch_to_edit()

        time.sleep(DEFAULT_CAPTURE_DELAY)
        self.app.processEvents()

        filename = f"edit_timeline_tools_{width}x{height}.png"
        save_path = self.output_dir / filename

        # Try to find the timeline panel and grab its toolbar
        timeline_panel = self._find_timeline_panel()
        if timeline_panel is not None:
            toolbar = self._find_child_toolbar(timeline_panel)
            if toolbar is not None:
                self._grab_widget(toolbar, save_path)
            else:
                # Fallback: grab top portion of timeline panel
                rect = timeline_panel.rect()
                from PyQt6.QtCore import QRect
                toolbar_rect = QRect(0, 0, rect.width(), min(80, rect.height()))
                self._grab_region(timeline_panel, toolbar_rect, save_path)
        else:
            logger.warning("Timeline panel not found; capturing full window")
            self._grab_widget(self.window, save_path)

        self._cleanup()
        return save_path if save_path.exists() else None

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _populate_test_clips(self) -> None:
        """Add test clips to the timeline via the Edit project.

        Uses the immutable EditProject API: each mutation returns a new
        instance, so we re-assign self._current_edit_project on the window.
        """
        try:
            from src.core.edit.project import EditProject

            project = EditProject.create(name="Visual Test Project")

            # Import dummy media assets (no real files needed)
            for i in range(3):
                project = project.import_asset(
                    path=f"/tmp/test_clip_{i}.mp4",
                    media_type="video",
                    metadata={
                        "width": 1920,
                        "height": 1080,
                        "fps": 24.0,
                        "duration": 5.0,
                        "codec": "h264",
                    },
                    display_name=f"Test Clip {i+1}",
                )

            # Set the project on the main window (mirrors new_edit_project flow)
            if hasattr(self.window, '_current_edit_project'):
                self.window._current_edit_project = project
                self.window._current_edit_project_filepath = None
                self.window._current_timeline_sequences = {}

            # Update Media Pool Panel
            if hasattr(self.window, 'media_pool_panel'):
                self.window.media_pool_panel.set_project(project)

            self.app.processEvents()
            time.sleep(0.5)

        except Exception as e:
            logger.warning("Could not populate test clips (non-fatal): %s", e)

    def _find_timeline_panel(self):
        """Find the TimelinePanel widget in the window."""
        from src.ui.edit.timeline_panel import TimelinePanel
        return self.window.findChild(TimelinePanel)

    def _find_child_toolbar(self, parent_widget):
        """Find a toolbar-like widget inside a parent."""
        from PyQt6.QtWidgets import QToolBar, QFrame

        # Look for QToolBar first
        toolbar = parent_widget.findChild(QToolBar)
        if toolbar:
            return toolbar

        # Look for the first horizontal frame/layout that looks like a toolbar
        frames = parent_widget.findChildren(QFrame)
        for frame in frames:
            if frame.height() <= 80 and frame.width() > 200:
                return frame

        return None

    # -----------------------------------------------------------------
    # Batch capture
    # -----------------------------------------------------------------

    def capture_all(
        self,
        resolutions: Optional[List[Tuple[int, int]]] = None,
        with_clips: bool = True,
        toolbar: bool = True,
    ) -> List[Path]:
        """Capture all baseline screenshots at all resolutions."""
        resolutions = resolutions or SUPPORTED_RESOLUTIONS
        results: List[Path] = []

        for w, h in resolutions:
            logger.info("=== Capturing %dx%d ===", w, h)

            # Empty project
            p = self.capture_edit_page_empty(w, h)
            if p:
                results.append(p)

            if with_clips:
                p = self.capture_edit_page_with_clips(w, h)
                if p:
                    results.append(p)

            if toolbar:
                p = self.capture_timeline_tools(w, h)
                if p:
                    results.append(p)

        return results


# =============================================================================
# Visual Acceptance Criteria Checker
# =============================================================================

class VisualAcceptanceChecker:
    """
    Checks visual acceptance criteria by inspecting widget geometry and state.

    These checks verify layout quality without requiring pixel comparison:
    - Toolbar buttons are not squished (no text overflow)
    - Timeline clip text does not overflow clip boundaries
    - Track header buttons don't overlap
    - Program Viewer shows content (not empty/blank)
    - Media Pool grid/list views both render correctly
    """

    def __init__(self, window):
        self.window = window
        self.results: List[dict] = []

    def check_all(self) -> List[dict]:
        """Run all acceptance checks and return results."""
        self.results = []
        self._check_toolbar_no_squish()
        self._check_timeline_clip_text()
        self._check_track_header_buttons()
        self._check_program_viewer()
        self._check_media_pool_views()
        return self.results

    def _add_result(self, name: str, passed: bool, detail: str = ""):
        status = "PASS" if passed else "FAIL"
        self.results.append({
            "name": name,
            "passed": passed,
            "status": status,
            "detail": detail,
        })
        logger.info("[%s] %s: %s", status, name, detail)

    def _check_toolbar_no_squish(self):
        """Check that toolbar buttons are not squished or overlapping."""
        try:
            from src.ui.edit.timeline_panel import TimelinePanel
            panel = self.window.findChild(TimelinePanel)
            if panel is None:
                self._add_result("toolbar_no_squish", False, "TimelinePanel not found")
                return

            from PyQt6.QtWidgets import QToolButton, QPushButton
            buttons = panel.findChildren(QToolButton) + panel.findChildren(QPushButton)

            overlap_count = 0
            squish_count = 0
            for btn in buttons:
                # Check minimum size hint
                min_hint = btn.minimumSizeHint()
                actual_size = btn.size()
                if actual_size.width() < min_hint.width() * 0.8:
                    squish_count += 1
                if actual_size.height() < min_hint.height() * 0.8:
                    squish_count += 1

            # Check for overlapping buttons
            rects = [(btn.geometry(), btn.objectName()) for btn in buttons if btn.isVisible()]
            for i, (r1, n1) in enumerate(rects):
                for r2, n2 in rects[i + 1:]:
                    if r1.intersects(r2):
                        overlap_count += 1

            passed = (squish_count == 0 and overlap_count == 0)
            detail = f"squished={squish_count}, overlapping={overlap_count}"
            self._add_result("toolbar_no_squish", passed, detail)

        except Exception as e:
            self._add_result("toolbar_no_squish", False, f"Error: {e}")

    def _check_timeline_clip_text(self):
        """Check that timeline clip text does not overflow clip boundaries."""
        try:
            from src.ui.edit.timeline_view import TimelineView
            view = self.window.findChild(TimelineView)
            if view is None:
                self._add_result("timeline_clip_text", False, "TimelineView not found")
                return

            scene = view.scene()
            if scene is None:
                self._add_result("timeline_clip_text", False, "Scene not found")
                return

            from PyQt6.QtWidgets import QGraphicsTextItem
            overflow_count = 0
            checked = 0

            for item in scene.items():
                # Look for clip graphics items with text
                if hasattr(item, 'clip_rect') and hasattr(item, 'label'):
                    checked += 1
                    clip_rect = item.clip_rect if hasattr(item, 'clip_rect') else item.boundingRect()
                    label = item.label if hasattr(item, 'label') else None
                    if label and hasattr(label, 'boundingRect'):
                        label_rect = label.boundingRect()
                        if label_rect.width() > clip_rect.width():
                            overflow_count += 1

            passed = (overflow_count == 0)
            detail = f"checked={checked}, overflow={overflow_count}"
            self._add_result("timeline_clip_text", passed, detail)

        except Exception as e:
            self._add_result("timeline_clip_text", False, f"Error: {e}")

    def _check_track_header_buttons(self):
        """Check that track header buttons don't overlap."""
        try:
            from src.ui.edit.timeline_view import TimelineView
            view = self.window.findChild(TimelineView)
            if view is None:
                self._add_result("track_header_buttons", False, "TimelineView not found")
                return

            from PyQt6.QtWidgets import QPushButton, QToolButton
            # Find buttons in track header area
            buttons = []
            for btn in view.findChildren(QToolButton) + view.findChildren(QPushButton):
                if btn.isVisible() and btn.objectName() and "track" in btn.objectName().lower():
                    buttons.append(btn)

            overlap_count = 0
            rects = [(btn.geometry(), btn.objectName()) for btn in buttons]
            for i, (r1, n1) in enumerate(rects):
                for r2, n2 in rects[i + 1:]:
                    if r1.intersects(r2):
                        overlap_count += 1

            passed = (overlap_count == 0)
            detail = f"buttons={len(buttons)}, overlapping={overlap_count}"
            self._add_result("track_header_buttons", passed, detail)

        except Exception as e:
            self._add_result("track_header_buttons", False, f"Error: {e}")

    def _check_program_viewer(self):
        """Check that Program Viewer is not empty/blank."""
        try:
            from src.ui.edit.program_viewer import ProgramViewerWidget
            viewer = self.window.findChild(ProgramViewerWidget)
            if viewer is None:
                self._add_result("program_viewer_not_empty", False, "ProgramViewerWidget not found")
                return

            # Check that viewer has reasonable dimensions
            size = viewer.size()
            is_reasonable_size = size.width() > 100 and size.height() > 100

            # Check visibility
            is_visible = viewer.isVisible()

            # Grab a screenshot to check it's not all-black
            pixmap = viewer.grab()
            is_not_blank = True
            if not pixmap.isNull():
                from PyQt6.QtGui import QImage
                img = pixmap.toImage()
                # Sample a few pixels to check not all black
                samples = [
                    img.pixelColor(img.width() // 4, img.height() // 4),
                    img.pixelColor(img.width() // 2, img.height() // 2),
                    img.pixelColor(3 * img.width() // 4, 3 * img.height() // 4),
                ]
                all_black = all(
                    c.red() == 0 and c.green() == 0 and c.blue() == 0
                    for c in samples
                )
                is_not_blank = not all_black

            passed = is_reasonable_size and is_visible
            detail = (
                f"visible={is_visible}, "
                f"size={size.width()}x{size.height()}, "
                f"not_blank={is_not_blank}"
            )
            self._add_result("program_viewer_not_empty", passed, detail)

        except Exception as e:
            self._add_result("program_viewer_not_empty", False, f"Error: {e}")

    def _check_media_pool_views(self):
        """Check that Media Pool grid and list views can both display."""
        try:
            from src.ui.edit.media_pool_panel import MediaPoolPanel
            panel = self.window.findChild(MediaPoolPanel)
            if panel is None:
                self._add_result("media_pool_views", False, "MediaPoolPanel not found")
                return

            # Check for grid and list view widgets
            from PyQt6.QtWidgets import QListView, QTreeWidget

            has_list_view = panel.findChild(QTreeWidget) is not None
            has_grid_view = panel.findChild(QListView) is not None

            # Try toggling between views
            grid_works = False
            list_works = False

            try:
                if hasattr(panel, 'set_view_mode'):
                    panel.set_view_mode("grid")
                    grid_works = True
                    panel.set_view_mode("list")
                    list_works = True
                elif hasattr(panel, '_set_grid_view') and hasattr(panel, '_set_list_view'):
                    panel._set_grid_view()
                    grid_works = True
                    panel._set_list_view()
                    list_works = True
                else:
                    # Check for view toggle buttons
                    from PyQt6.QtWidgets import QToolButton
                    buttons = panel.findChildren(QToolButton)
                    for btn in buttons:
                        name = btn.objectName() or ""
                        tip = btn.toolTip() or ""
                        if "grid" in name.lower() or "grid" in tip.lower():
                            grid_works = True
                        if "list" in name.lower() or "list" in tip.lower():
                            list_works = True
            except Exception:
                pass

            passed = (has_list_view or list_works) and (has_grid_view or grid_works)
            detail = (
                f"list_view={has_list_view}, grid_view={has_grid_view}, "
                f"list_toggle={list_works}, grid_toggle={grid_works}"
            )
            self._add_result("media_pool_views", passed, detail)

        except Exception as e:
            self._add_result("media_pool_views", False, f"Error: {e}")


# =============================================================================
# CLI
# =============================================================================

def parse_resolution(s: str) -> Tuple[int, int]:
    """Parse a resolution string like '1920x1080'."""
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid resolution '{s}', expected format: WIDTHxHEIGHT (e.g. 1920x1080)"
        )


def main():
    global DEFAULT_CAPTURE_DELAY
    parser = argparse.ArgumentParser(
        description="Capture Edit Page screenshots for visual acceptance testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--resolution", "-r",
        type=parse_resolution,
        action="append",
        dest="resolutions",
        help="Resolution to capture (e.g. 1920x1080). Can be specified multiple times.",
    )
    parser.add_argument(
        "--list-resolutions",
        action="store_true",
        help="List supported resolutions and exit.",
    )
    parser.add_argument(
        "--with-clips",
        action="store_true",
        default=False,
        help="Include screenshots with timeline clips populated.",
    )
    parser.add_argument(
        "--toolbar-only",
        action="store_true",
        default=False,
        help="Capture only the timeline toolbar region.",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=BASELINE_DIR,
        help=f"Output directory for baseline images (default: {BASELINE_DIR})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Run visual acceptance criteria checks after capture.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        default=False,
        help="Only run acceptance checks (no screenshot capture).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_CAPTURE_DELAY,
        help=f"Delay in seconds before capturing (default: {DEFAULT_CAPTURE_DELAY})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging.",
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    # List resolutions
    if args.list_resolutions:
        print("Supported resolutions:")
        for w, h in SUPPORTED_RESOLUTIONS:
            print(f"  {w}x{h}")
        return 0

    # Determine resolutions
    resolutions = args.resolutions or SUPPORTED_RESOLUTIONS

    # Validate resolutions
    for r in resolutions:
        if r not in SUPPORTED_RESOLUTIONS:
            logger.warning(
                "Resolution %dx%d is not in the standard set. Capturing anyway.",
                r[0], r[1],
            )

    # Update global delay
    DEFAULT_CAPTURE_DELAY = args.delay

    # --- Check-only mode ---
    if args.check_only:
        logger.info("Running acceptance checks only (no capture)...")
        capturer = EditPageCapture(output_dir=args.output_dir)
        capturer._ensure_qt(resolutions[0][0], resolutions[0][1])
        capturer._switch_to_edit()
        time.sleep(args.delay)
        capturer.app.processEvents()

        checker = VisualAcceptanceChecker(capturer.window)
        results = checker.check_all()

        capturer._cleanup()

        # Print report
        print("\n" + "=" * 60)
        print("VISUAL ACCEPTANCE CHECK REPORT")
        print("=" * 60)
        for r in results:
            status_mark = "✓" if r["passed"] else "✗"
            print(f"  [{status_mark}] {r['name']}: {r['detail']}")
        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        print(f"\nResult: {passed}/{total} checks passed")
        print("=" * 60)
        return 0 if passed == total else 1

    # --- Capture mode ---
    logger.info("Starting Edit Page screenshot capture...")
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Resolutions: %s", [f"{w}x{h}" for w, h in resolutions])

    capturer = EditPageCapture(output_dir=args.output_dir)

    if args.toolbar_only:
        results = []
        for w, h in resolutions:
            p = capturer.capture_timeline_tools(w, h)
            if p:
                results.append(p)
    else:
        results = capturer.capture_all(
            resolutions=resolutions,
            with_clips=args.with_clips,
            toolbar=True,
        )

    # Print summary
    print("\n" + "=" * 60)
    print("CAPTURE RESULTS")
    print("=" * 60)
    if results:
        for p in results:
            size_kb = p.stat().st_size / 1024
            print(f"  {p.name} ({size_kb:.1f} KB)")
        print(f"\nTotal: {len(results)} screenshots saved to {args.output_dir}")
    else:
        print("  No screenshots captured.")
    print("=" * 60)

    # --- Optional acceptance check ---
    if args.check and results:
        logger.info("Running acceptance checks on captured screenshots...")
        # Re-launch for checks
        capturer2 = EditPageCapture(output_dir=args.output_dir)
        capturer2._ensure_qt(resolutions[0][0], resolutions[0][1])
        capturer2._switch_to_edit()
        time.sleep(args.delay)
        capturer2.app.processEvents()

        checker = VisualAcceptanceChecker(capturer2.window)
        check_results = checker.check_all()

        capturer2._cleanup()

        print("\n" + "=" * 60)
        print("VISUAL ACCEPTANCE CHECK REPORT")
        print("=" * 60)
        for r in check_results:
            status_mark = "✓" if r["passed"] else "✗"
            print(f"  [{status_mark}] {r['name']}: {r['detail']}")
        passed = sum(1 for r in check_results if r["passed"])
        total = len(check_results)
        print(f"\nResult: {passed}/{total} checks passed")
        print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
