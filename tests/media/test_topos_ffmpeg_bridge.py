"""FFmpeg external-process bridge roundtrip gate for GBR10 `.toos`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.media_core

REPO = Path(__file__).resolve().parents[2]
LIB = next(
    (REPO / "native/topos_codec/build/release" / name
     for name in ("topos_codec.dylib", "libtopos_codec.dylib",
                  "libtopos_codec.so", "topos_codec.dll")
     if (REPO / "native/topos_codec/build/release" / name).exists()),
    REPO / "native/topos_codec/build/release/topos_codec.dylib",
)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg/ffprobe 未安装")
@pytest.mark.skipif(not LIB.exists(), reason="libtopos_codec 未构建")
def test_ffmpeg_bridge_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    toos = tmp_path / "roundtrip.toos"
    restored = tmp_path / "restored.png"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=1",
        "-frames:v", "1", "-y", str(source),
    ], check=True)
    bridge = REPO / "scripts/topos_ffmpeg_bridge.py"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    subprocess.run([
        sys.executable, str(bridge), "encode", str(source), str(toos),
        "--library", str(LIB),
    ], check=True, cwd=REPO, env=env)
    subprocess.run([
        sys.executable, str(bridge), "decode", str(toos), str(restored),
        "--library", str(LIB),
    ], check=True, cwd=REPO, env=env)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,pix_fmt", "-of", "json",
        str(restored),
    ], capture_output=True, text=True, check=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (64, 48)
    assert restored.stat().st_size > 0
