"""Run the bounded Files/layout gate with the user-provided real video corpus."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-dir", type=Path, default=Path(
        "/Volumes/Project_1/开发项目/topos-color-multitask /topos-color01/tests/test_videos"))
    args = parser.parse_args()
    import pytest
    import tests.real_media as media

    media.TEST_VIDEOS = args.video_dir
    media.STRICT = True
    selected = ["tests/files", *[str(path.relative_to(ROOT))
                                  for path in sorted((ROOT / "tests/ui").glob("test_native_files_*.py"))],
                "tests/ui/test_files_native_host.py", "tests/ui/test_files_location_regressions.py",
                "tests/ui/test_files_session_regressions.py", "tests/ui/test_files_gallery_layouts.py",
                "tests/ui/test_files_four_layouts.py", "tests/ui/test_playback_semantics_regressions.py",
                "tests/ui/test_native_player_host.py", "tests/ui/test_topos_audio.py",
                "tests/ui/test_gpu_yuv_frame.py", "tests/edit/test_external_import_compensation.py",
                "tests/architecture/test_file_explorer_integration.py"]
    os.chdir(ROOT)
    report = Path(__file__).parent / "regression.xml"
    return int(pytest.main(["-q", *selected, f"--junitxml={report}"]))


if __name__ == "__main__":
    raise SystemExit(main())
