"""F5.4 片1 · 通用图像序列导入——扫描/缺帧诊断/指纹/显式选择。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.shared.media.generic_image_sequence import (
    classify_directory,
    parse_generic_frame_name,
)


def _make_seq(tmp_path, prefix="shot_", frames=range(1001, 1021),
              ext=".png", skip=()):
    for f in frames:
        if f in skip:
            continue
        (tmp_path / f"{prefix}{f}{ext}").write_bytes(b"x")


class TestScan:
    def test_shot_1001_series_is_one_asset_candidate(self, tmp_path):
        _make_seq(tmp_path)
        result = classify_directory(str(tmp_path))
        assert len(result.candidates) == 1
        cand = result.candidates[0]
        assert cand.prefix == "shot_" and cand.suffix == ".png"
        assert cand.first_frame == 1001 and cand.last_frame == 1020
        assert len(cand.frames) == 20
        assert cand.holes == ()
        assert result.single_frames == ()

    def test_missing_1007_diagnosed_not_faked(self, tmp_path):
        _make_seq(tmp_path, skip=(1007,))
        result = classify_directory(str(tmp_path))
        cand = result.candidates[0]
        assert cand.holes == (1007,)
        diag = "\n".join(result.diagnostics())
        assert "1007" in diag and "缺帧" in diag

    def test_single_frame_not_forced_into_sequence(self, tmp_path):
        (tmp_path / "photo_8001.png").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("ignore")
        result = classify_directory(str(tmp_path))
        assert result.candidates == ()
        assert len(result.single_frames) == 1

    def test_separate_prefixes_separate_assets(self, tmp_path):
        _make_seq(tmp_path, prefix="a_")
        _make_seq(tmp_path, prefix="b_", frames=range(1, 6), ext=".tif")
        result = classify_directory(str(tmp_path))
        assert {c.prefix for c in result.candidates} == {"a_", "b_"}

    def test_fingerprint_stable_across_move(self, tmp_path):
        _make_seq(tmp_path)
        first = classify_directory(str(tmp_path)).candidates[0].fingerprint()
        # 同内容重扫（模拟重开）——指纹一致
        second = classify_directory(str(tmp_path)).candidates[0].fingerprint()
        assert first == second
        # 内容变化（增帧）——指纹变化
        (tmp_path / "shot_999.png").write_bytes(b"x")
        third = classify_directory(str(tmp_path)).candidates[0].fingerprint()
        assert third != first

    def test_parse_frame_name(self):
        assert parse_generic_frame_name("shot_1001.png") == ("shot_", 1001, ".png")
        # 非贪婪前缀下 "frame-12" 解析为 frame=-12（负号并入帧号——与
        # topos 序列冻结语法的负帧支持一致；避免歧义用无连字符命名）
        assert parse_generic_frame_name("frame-12.TIF") == ("frame", -12, ".TIF")
        assert parse_generic_frame_name("take12.TIF") == ("take", 12, ".TIF")
        assert parse_generic_frame_name("no_number.png") is None
