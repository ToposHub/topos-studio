"""
Pytest fixtures for visual acceptance testing.

Provides:
- qapp_visual: QApplication with OpenGL surface format configured
- edit_page_capture: EditPageCapture instance for screenshot capture
- visual_checker: VisualAcceptanceChecker for layout validation
- main_window_visual: MainWindow in test_mode for lightweight testing
"""

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Disable update checker and network access in tests
os.environ.setdefault("TOPOS_TEST_MODE", "1")

# CRITICAL: QtWebEngineWidgets MUST be imported BEFORE creating QApplication.
# MainWindow imports files_panel which imports QWebEngineView at module level.
# If QWebEngineWidgets is first imported after QApplication exists, it aborts.
if sys.platform != 'win32':
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError:
        pass


@pytest.fixture(scope="session")
def qapp_visual():
    """Create a QApplication with OpenGL surface format for visual testing.

    This mirrors the configuration in src/main.py to ensure consistent
    rendering during visual tests.
    """
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
        app = QApplication(sys.argv)
    yield app


@pytest.fixture
def baseline_dir():
    """Return the path to the baseline screenshots directory."""
    d = PROJECT_ROOT / "tests" / "visual" / "baselines"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def edit_page_capture(qapp_visual, baseline_dir):
    """Provide an EditPageCapture instance configured for testing.

    The capture is initialised but no window is created until a capture
    method is called.
    """
    # Add tools/ to path so we can import capture_edit_page
    tools_dir = PROJECT_ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    from capture_edit_page import EditPageCapture
    capturer = EditPageCapture(output_dir=baseline_dir)
    yield capturer

    # Cleanup
    capturer._cleanup()


@pytest.fixture
def main_window_visual(qapp_visual):
    """Create a MainWindow for visual testing with minimal panel loading.

    Uses test_mode=True to avoid creating heavy GL/rendering panels that
    cause segfaults in test environments. Skips if no display is available.
    """
    # Check display availability
    try:
        from PyQt6.QtGui import QWindow
        test_window = QWindow()
        test_window.destroy()
    except Exception:
        pytest.skip("No display available for visual tests")

    from src.app.main_window import MainWindow

    # Use test_mode to avoid creating heavy panels
    window = MainWindow(test_mode=True)
    yield window

    # Thorough cleanup
    try:
        window.close()
    except Exception:
        pass
    try:
        window.deleteLater()
    except Exception:
        pass
    qapp_visual.processEvents()
