"""Simulate missing legacy entrypoint without moving or deleting user files."""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
original_exists = Path.exists


def without_extension(path):
    if str(path).replace("\\", "/").endswith("/external/file_explorer/main.py"):
        return False
    return original_exists(path)


try:
    with patch.object(Path, "exists", without_extension):
        import src.app.ui.files_panel  # noqa: F401
except Exception as error:
    print(type(error).__name__ + ": " + str(error))
else:
    print("IMPORT_SUCCEEDED")
