"""Run a GPU golden check with fresh OpenGL platform and error state."""

import inspect
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


_WORKER = """
import runpy
import sys
import pytest

try:
    runpy.run_path(sys.argv[1])[sys.argv[2]](*sys.argv[3:])
except pytest.skip.Exception as exc:
    print(exc)
    sys.exit(77)
"""


def run_standalone_gpu(check: Callable[..., None], *args: str) -> None:
    source = inspect.getsourcefile(check)
    assert source is not None
    root = Path(__file__).resolve().parents[2]
    process = subprocess.run(
        [sys.executable, "-c", _WORKER, source, check.__name__, *args],
        cwd=root, timeout=45, text=True, capture_output=True,
        env={**os.environ, "TOPOS_HEADLESS_GL": "1"},
    )
    if process.returncode == 77:
        pytest.skip(process.stdout.strip())
    assert process.returncode == 0, process.stdout + process.stderr
