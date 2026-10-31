# Visual Test Baselines

This directory contains baseline screenshots for visual acceptance testing.

## Naming Convention

```
{snapshot_name}_{width}x{height}.png
```

## Standard Baselines

| File | Description |
|------|-------------|
| `edit_page_empty_1280x800.png` | Empty Edit page at 1280x800 |
| `edit_page_empty_1440x900.png` | Empty Edit page at 1440x900 |
| `edit_page_empty_1920x1080.png` | Empty Edit page at 1920x1080 |
| `edit_page_with_clips_1280x800.png` | Edit page with timeline clips at 1280x800 |
| `edit_page_with_clips_1440x900.png` | Edit page with timeline clips at 1440x900 |
| `edit_page_with_clips_1920x1080.png` | Edit page with timeline clips at 1920x1080 |
| `edit_timeline_tools_1280x800.png` | Timeline toolbar at 1280x800 |
| `edit_timeline_tools_1440x900.png` | Timeline toolbar at 1440x900 |
| `edit_timeline_tools_1920x1080.png` | Timeline toolbar at 1920x1080 |

## How to Generate Baselines

```bash
# Generate all baselines at all resolutions
.venv/bin/python tools/capture_edit_page.py

# Generate at a specific resolution
.venv/bin/python tools/capture_edit_page.py --resolution 1920x1080

# Generate with timeline clips
.venv/bin/python tools/capture_edit_page.py --with-clips

# Generate only toolbar screenshots
.venv/bin/python tools/capture_edit_page.py --toolbar-only
```

## How to Run Acceptance Checks

```bash
# CLI tool
.venv/bin/python tools/capture_edit_page.py --check-only

# Pytest
.venv/bin/python -m pytest tests/visual/ -v
```
