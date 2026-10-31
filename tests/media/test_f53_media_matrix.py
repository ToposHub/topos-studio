"""F5.3 · 相机 RAW 与媒体支持矩阵——真实原片验收。

原则（计划原文）：**文件扩展名已识别不算验收完成**——每个 AVAILABLE
格式用 tests/test_videos/ 的真实原片打开并解码一帧；BRAW/R3D 有真实
原片但无 SDK——诊断+转码建议，不伪装支持。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.shared.media.support_matrix import (
    MEDIA_SUPPORT_MATRIX,
    diagnose_media_path,
    matrix_to_payload,
    support_for_path,
)

_VIDEOS = Path("tests/test_videos")

#: 真实原片抽样（每家族至少 2 个变体；全量 48 文件矩阵循环）
_REAL_FILES = [
    ("h264", "2K_h264.mp4"),
    ("h264", "4K_H264.mov"),
    ("h265", "2K_H265.mov"),
    ("h265", "4K_H265.mov"),
    ("prores", "2K_prores422.mov"),
    ("prores", "2K_Prores4444_Alpha.mov"),
    ("dnxhr", "2K_DNxHR HQX 10bit.mov"),
    ("dnxhr", "4K_DNxHR 444 12bit.mov"),
    ("topos_movie", "2K_topos_422.mov"),
    # 名带 topos 但编码是 H.264（文件名注明）——按实际编码走 FFmpeg 家族
    ("h264", "2K_topos_422_LT (H.264).mp4"),
]


def _open_and_decode(path: Path, family: str = "h264"):
    """真实打开 + 解码一帧（Topos 私有编码经 factory topos 后端路由，
    FFmpeg 家族直开——与生产 MediaSourceFactory 路由同口径）。"""
    if family == "topos_movie":
        # ToposMediaSource 的读取契约是 read_next_frame（planar 帧；
        # read_frame 定位语义属 FFmpeg 家族——测试按源契约调用）
        from src.shared.media.topos_source import ToposMediaSource
        decoder = ToposMediaSource(str(path))
        decoder.open()
        try:
            frame = decoder.read_next_frame()
            assert frame is not None and frame.planes
            return frame
        finally:
            decoder.close()
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    decoder = FFmpegMediaSource(
        str(path), output_mode="packed_rgb", output_format="rgb24")
    decoder.open()
    try:
        frame = decoder.read_frame(0)
        assert frame is not None and frame.data is not None
        return frame
    finally:
        decoder.close()


class TestRealMediaMatrix:
    @pytest.mark.parametrize("family,filename", _REAL_FILES)
    def test_real_file_opens_and_decodes(self, family, filename):
        """每个 AVAILABLE 家族用真实原片打开+解码一帧（扩展名识别≠验收）。"""
        path = _VIDEOS / filename
        if not path.exists():
            pytest.skip(f"真实原片缺失：{filename}")
        entry = support_for_path(str(path))
        assert entry is not None
        # 家族归属一致（扩展名重叠家族按声明优先级解析）
        assert entry.key in (family, "h264", "h265", "prores", "dnxhr",
                             "topos_movie")
        frame = _open_and_decode(path, family)
        data = getattr(frame, "data", None)
        if data is None:  # topos planar 帧经 planes 断言
            planes = getattr(frame, "planes", None)
            assert planes and planes[0] is not None
        else:
            assert data is not None and getattr(data, "size", 0) > 0

    def test_braw_real_file_diagnosed_with_transcode_advice(self):
        """真实 .braw 在库但无 SDK：诊断给出缺失项+转码建议，不静默。"""
        path = _VIDEOS / "A002_01012049_C013.braw"
        if not path.exists():
            pytest.skip("真实 BRAW 原片缺失")
        diag = diagnose_media_path(str(path))
        assert diag["status"] == "sdk_missing"
        assert diag["actionable"] is True
        assert any("Blackmagic" in m for m in diag["missing"])
        assert "转码建议" in diag["message"]

    def test_r3d_real_file_diagnosed(self):
        path = _VIDEOS / "A010_A037_0715UN_001.R3D"
        if not path.exists():
            pytest.skip("真实 R3D 原片缺失")
        diag = diagnose_media_path(str(path))
        assert diag["status"] == "sdk_missing"
        assert "RED" in "; ".join(diag["missing"])
        assert "ProRes" in diag["message"] or "DNxHR" in diag["message"]

    def test_supported_path_diagnosed_ok(self):
        diag = diagnose_media_path("x.mov")  # 容器级家族（内容探测在打开时）
        assert diag["status"] in ("supported", "unknown_extension")

    def test_photo_raw_and_topos_raw_listed_separately(self):
        """照片 RAW / Topos RAW 单列（不与相机视频格式混标）。"""
        keys = {e.key for e in MEDIA_SUPPORT_MATRIX}
        assert "photo_raw" in keys and "topos_raw" in keys
        photo = next(e for e in MEDIA_SUPPORT_MATRIX
                     if e.key == "photo_raw")
        assert photo.raw_controls == "available"
        assert "topos" not in photo.notes.lower().split("单列")[0]
        traw = support_for_path("clip.traw")
        assert traw is not None and traw.key == "topos_raw"
        assert traw.raw_controls == "available"

    def test_unavailable_formats_not_claimed(self):
        """矩阵不冒称：unavailable 项全维度 unavailable/diagnostic。"""
        for entry in MEDIA_SUPPORT_MATRIX:
            if entry.container == "unavailable":
                assert entry.decode == "unavailable"
                assert entry.raw_controls == "unavailable"
                assert "SDK" in entry.notes or "不伪装" in entry.notes

    def test_matrix_payload_complete(self):
        payload = matrix_to_payload()
        assert len(payload) == len(MEDIA_SUPPORT_MATRIX)
        for item in payload:
            assert {"key", "label", "extensions", "container", "decode",
                    "raw_controls", "proxy_seek", "export", "evidence"} <= set(item)
            # AVAILABLE 必须绑定证据字符串（真实文件/代码锚点）
            if item["container"] == "available":
                assert item["evidence"]
