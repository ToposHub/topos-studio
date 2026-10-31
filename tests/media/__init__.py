"""
Video Media Test Utilities Package

This package provides tools for video pipeline testing and benchmarking.

Components:
- fixtures_manifest.json: Video test fixture catalog
- benchmark_video_pipeline.py: Performance benchmark script
"""

from pathlib import Path

# Package paths
MEDIA_DIR = Path(__file__).parent
FIXTURES_MANIFEST = MEDIA_DIR / 'fixtures_manifest.json'
RESULTS_DIR = MEDIA_DIR / 'results'