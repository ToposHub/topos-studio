"""Contract checks for the optional OIIO adapter."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_oiio_plugin_source_and_syntax_gate_exist() -> None:
    source = REPO / "plugins/oiio_topos/topos_imageio.cpp"
    syntax = REPO / "plugins/oiio_topos/syntax_check.sh"
    cmake = REPO / "native/topos_codec/cmake/oiio_plugin.cmake"
    assert source.is_file()
    assert syntax.is_file() and syntax.stat().st_mode & 0o111
    assert "../../plugins/oiio_topos/topos_imageio.cpp" in cmake.read_text(
        encoding="utf-8",
    )


def test_oiio_stub_syntax_gate() -> None:
    proc = subprocess.run(["sh", str(REPO / "plugins/oiio_topos/syntax_check.sh")],
                          cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "syntax OK" in proc.stdout
