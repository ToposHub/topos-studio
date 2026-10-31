"""Release-package style smoke test for the public image ABI wrapper."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "native/topos_codec/tests/conformance/golden_image_v1_gbr10.bin"
SCRIPT = REPO / "scripts/topos_abi_smoke.py"


def test_public_abi_smoke_on_golden() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT), str(GOLDEN)],
                          cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["input"]["plane_count"] == 3
    assert payload["capabilities"]["tile_roi"] is False
    assert payload["decoded_plane_sizes"] == [[64, 48]] * 3
