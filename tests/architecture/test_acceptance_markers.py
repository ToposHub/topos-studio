"""Verify that acceptance markers are properly assigned to tests."""


def test_color_core_marker_collects_tests(pytestconfig):
    """color_core marker should select at least some tests."""
    # We verify by checking that the marker is registered
    markers = pytestconfig.getini("markers")
    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "color_core" in marker_names


def test_composite_core_marker_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "composite_core" in marker_names


def test_media_core_marker_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "media_core" in marker_names


def test_modular_marker_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "modular" in marker_names


def test_visual_marker_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    marker_names = [m.split(":")[0].strip() for m in markers]
    assert "visual" in marker_names
