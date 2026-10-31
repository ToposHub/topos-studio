"""O2 regression: distinguish mathematical lossless from reserved profile 3."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "native/topos_codec/build/debug/toos"
pytestmark = pytest.mark.skipif(not CLI.exists(), reason="toos CLI 未构建")


def _encode(tmp_path: Path, output: Path, profile: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run([
        str(CLI), "encode", str(tmp_path / "input.raw"), "-o", str(output),
        "--width", "16", "--height", "16", "--pixel-format", "2",
        "--bit-depth", "10", "--alpha-mode", "0", "--image-profile", str(profile),
        "--qp", "28", "--overwrite",
    ], capture_output=True, text=True)


def test_explicit_qp28_is_bit_exact_but_lossless_profile_is_reserved(tmp_path: Path) -> None:
    source = np.random.default_rng(42).integers(0, 1024, (3, 16, 16), dtype=np.uint16)
    source.tofile(tmp_path / "input.raw")
    lossless = tmp_path / "qp28.toos"
    proc = _encode(tmp_path, lossless, 1)
    assert proc.returncode == 0, proc.stderr
    decoded = tmp_path / "decoded.raw"
    proc = subprocess.run([str(CLI), "decode", str(lossless), "-o", str(decoded)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert np.array_equal(source.ravel(), np.fromfile(decoded, dtype="<u2"))

    # TRAW 拍板（2026-09-13）：IDSC 值 3 改派 Image RAW（Lossless 档移除，
    # 无损语义由 RAW Linear 模式承载）。GBR 载荷写 image_profile=3 仍必须
    # 拒绝——现在是组合冲突（RAW ⇔ CFA/profile7）而非 reserved。
    reserved = _encode(tmp_path, tmp_path / "reserved.toos", 3)
    assert reserved.returncode != 0
    assert not (tmp_path / "reserved.toos").exists()
