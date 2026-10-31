"""Minimal bootstrap tests to verify visual test infrastructure works without segfault."""
import pytest

pytestmark = pytest.mark.visual


def test_qapp_visual_fixture_works(qapp_visual):
    """The qapp_visual fixture should provide a working QApplication."""
    from PyQt6.QtWidgets import QApplication
    assert QApplication.instance() is not None


def test_main_window_test_mode_no_crash(qapp_visual):
    """MainWindow in test_mode should not crash during construction."""
    # Check display availability first
    try:
        from PyQt6.QtGui import QWindow
        test_window = QWindow()
        test_window.destroy()
    except Exception:
        pytest.skip("No display available for visual tests")

    from src.app.main_window import MainWindow
    window = MainWindow(test_mode=True)
    assert window is not None
    try:
        window.close()
    except Exception:
        pass
    try:
        window.deleteLater()
    except Exception:
        pass
    qapp_visual.processEvents()


def test_visual_marker_is_set():
    """This file should be collected under the visual marker."""
    # The pytestmark at module level handles this
    pass
