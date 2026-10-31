"""O2 quality metric and profile contract tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

from scripts.measure_topos_image_quality import measure
from scripts.validate_topos_image_profiles import validate

REPO = Path(__file__).resolve().parents[2]


def test_bit_exact_quality_report_is_lossless() -> None:
    source = np.zeros((4, 5, 4), dtype=np.uint16)
    source[..., :3] = [100, 200, 300]
    source[..., 3] = np.arange(20, dtype=np.uint16).reshape(4, 5)
    result = measure(source, source.copy())
    assert result["bit_exact"] is True
    assert result["quality_class"] == "lossless"
    assert result["psnr_db"] is None
    assert result["alpha"]["bit_exact"] is True


def test_quality_report_detects_rgb_and_alpha_error() -> None:
    reference = np.full((4, 5, 4), 32768, dtype=np.uint16)
    candidate = reference.copy()
    candidate[..., 0] += 256
    candidate[..., 3] -= 128
    result = measure(reference, candidate)
    assert result["bit_exact"] is False
    assert result["rgb_max_abs_error"] > 0.0
    assert result["alpha"]["max_abs_error"] > 0.0


def test_quality_cli_writes_report(tmp_path: Path) -> None:
    ref = np.full((3, 4, 3), 128, dtype=np.uint8)
    candidate = ref.copy()
    candidate[0, 0, 0] = 127
    ref_path, got_path = tmp_path / "ref.png", tmp_path / "got.png"
    cv2.imwrite(str(ref_path), ref[..., ::-1])
    cv2.imwrite(str(got_path), candidate[..., ::-1])
    output = tmp_path / "quality.json"
    proc = subprocess.run([sys.executable, str(REPO / "scripts/measure_topos_image_quality.py"),
                           str(ref_path), str(got_path), "--output", str(output)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["metrics"]["bit_exact"] is False


def test_manifest_profile_contract() -> None:
    validate(REPO / "docs/image/capability_manifest.json")
