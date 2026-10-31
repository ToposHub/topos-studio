"""Q01：同一无损原始帧确实进入两条编码和逐帧测量路径。"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

from src.shared.codec.topos_binding import candidate_library_paths
from tools import measure_topos_paired_quality
from tools.measure_topos_paired_quality import (
    _prepare_vt_12bit_input, _require_encoder_input_format,
    _validate_source_domain, run,
)


def test_two_codec_paths_share_lossless_source(tmp_path: Path) -> None:
    if not any(path.is_file() for path in candidate_library_paths()):
        pytest.skip("TOPOS native 库未构建")
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg 未安装")
    width, height = 64, 64
    source = tmp_path / "source.yuv"
    with source.open("wb") as output:
        for index in range(2):
            y = np.full((height, width), 200 + index * 50, dtype="<u2")
            y[:, 16:32] += np.arange(16, dtype="<u2")
            u = np.full((height, width // 2), 512, dtype="<u2")
            v = np.full((height, width // 2), 490, dtype="<u2")
            for plane in (y, u, v):
                output.write(plane.tobytes())
    report = run(Namespace(
        source_raw=source, source_origin="lossless-original",
        tier="proxy", width=width, height=height, frames=2, fps=25,
        threads=2, ffmpeg="ffmpeg", out_dir=tmp_path / "run",
        source_parent=None, topos_crf=None, match_prores_bitrate=False,
        prores_encoder="prores_ks", allow_sw=False,
    ))
    assert report["status"] == "measurement_only_no_release_verdict"
    assert report["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(report["per_frame"]) == 2
    assert report["topos"]["video_bytes"] > 0
    assert report["prores"]["video_bytes"] > 0
    assert all(frame["topos"]["y"]["max_abs_codes"] >= 0
               and frame["prores"]["y"]["max_abs_codes"] >= 0
               for frame in report["per_frame"])


def test_prores_ks_refuses_silent_12bit_to_10bit_conversion(monkeypatch) -> None:
    def fake_probe(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Supported pixel formats: yuv422p10le yuv444p10le\n",
            stderr="",
        )

    monkeypatch.setattr(measure_topos_paired_quality.subprocess, "run", fake_probe)
    _require_encoder_input_format("ffmpeg", "prores_ks", "yuv422p10le")
    with pytest.raises(ValueError, match="不接受原生 yuv444p12le"):
        _require_encoder_input_format("ffmpeg", "prores_ks", "yuv444p12le")


def test_source_domain_checks_every_frame(tmp_path: Path) -> None:
    source = tmp_path / "bad-second-frame.yuv"
    with source.open("wb") as output:
        for frame in range(2):
            for plane in range(3):
                values = np.full((8, 8 if plane == 0 else 4),
                                 512, dtype="<u2")
                if frame == 1 and plane == 2:
                    values[0, 0] = 4095
                output.write(values.tobytes())
    with pytest.raises(ValueError, match="源帧 1 平面 2"):
        _validate_source_domain(source, 8, 8, "yuv422p10le", 2, 10)


def test_vt_p416_intermediate_roundtrips_12bit_exactly(tmp_path: Path) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg 未安装")
    source = tmp_path / "source444.yuv"
    values = np.random.default_rng(7).integers(
        0, 4096, size=3 * 16 * 16 * 2, dtype=np.uint16,
    )
    source.write_bytes(values.astype("<u2").tobytes())
    conversion = _prepare_vt_12bit_input(
        "ffmpeg", source, tmp_path, 16, 16, 2, 25,
    )
    assert conversion["roundtrip_bit_exact"] is True
    assert (tmp_path / "prores_input_p416le.raw").is_file()
