"""
Visual Acceptance Testing Package for ToposColor.

This package provides tools and tests for visual regression testing
of the Edit page UI layout and rendering.

Structure:
    baselines/       - Baseline screenshot images (PNG)
    __init__.py      - Package init
    conftest.py      - Pytest fixtures for visual testing

Usage:
    # Capture baseline screenshots
    .venv/bin/python tools/capture_edit_page.py

    # Run visual acceptance checks
    .venv/bin/python tools/capture_edit_page.py --check-only

    # Run visual tests via pytest
    .venv/bin/python -m pytest tests/visual/ -v
"""
