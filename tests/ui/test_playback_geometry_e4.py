"""E4（FUP-07/FR-19/20）：几何/时基/色彩一致性——旋转、SAR、奇数尺寸矩阵。

* 纯函数口径：DISPLAYMATRIX → 90° 象限（镜像/任意角不支持且不假装支持）、
  旋转感知等比拟合（框转置）、RGB 顺时针旋转；
* 集成口径（ffmpeg 合成素材，临时目录，不依赖仓库测试视频）：真
  PyAV 会话探取旋转并在 RGB 路径落地（90° 内容恰为源帧顺时针旋转）；
  BT.2020 PQ 色彩元数据在 YUV 路径原样保留；
* 时基矩阵 24/25（30000÷1001 已有）与奇数尺寸等比补齐。
"""
from __future__ import annotations

import shutil
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

pytest.importorskip("av")

from src.shared.playback.frame_geometry import (  # noqa: E402
    display_rotation_quarters, fit_aspect_size, rotate_rgb_quarters,
)
from src.shared.playback.libav_player import _LibavSession  # noqa: E402

FFMPEG = shutil.which("ffmpeg")

# 实测口径：ffmpeg -display_rotation W 写入 → 首帧 DISPLAYMATRIX（16.16 定点）
_M_IDENTITY = [65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824]
_M_90 = [0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824]
_M_180 = [-65536, 0, 0, 0, -65536, 0, 0, 0, 1073741824]
_M_270 = [0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824]


# ---------------------------------------------------------------------------
# 纯函数：旋转象限 / 旋转感知拟合 / RGB 旋转
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("matrix,expected", [
    (_M_IDENTITY, 0), (_M_90, 1), (_M_180, 2), (_M_270, 3),
])
def test_display_rotation_quarters_from_written_matrices(matrix, expected):
    assert display_rotation_quarters(matrix) == expected


def test_display_rotation_rejects_mirror_and_arbitrary_angle():
    # 镜像（det<0）：不支持，不得伪装成 180° 旋转
    mirrored = [-65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824]
    assert display_rotation_quarters(mirrored) == 0
    # 任意角 45°：不支持，不凑 90° 倍数
    c = int(65536 * 0.70710678)
    angled = [c, -c, 0, c, c, 0, 0, 0, 1073741824]
    assert display_rotation_quarters(angled) == 0
    assert display_rotation_quarters("junk") == 0
    assert display_rotation_quarters([1, 2]) == 0


def test_fit_aspect_transposes_box_for_quarter_turns():
    frame = SimpleNamespace(width=320, height=180)
    # 无旋转：320×180 装进 400×200 → 356×200（宽约束）
    w0, h0 = fit_aspect_size(frame, 400, 200)
    assert w0 == 356 and h0 == 200
    # 90° 显示旋转：显示宽高互换 → 等价先把框转置（200×400）再拟合
    w1, h1 = fit_aspect_size(frame, 400, 200, rotation_quarters=1)
    assert (w1, h1) == (200, 112)      # 解码尺寸；旋转后得 112×200 竖幅
    # 180°：显示宽高不变，与无旋转同参
    w2, h2 = fit_aspect_size(frame, 400, 200, rotation_quarters=2)
    assert (w2, h2) == (w0, h0)


def test_rotate_rgb_quarters_clockwise():
    grid = np.array([[1, 2], [3, 4]], dtype=np.uint8)[:, :, None]
    grid = np.repeat(grid, 3, axis=2)
    assert np.array_equal(rotate_rgb_quarters(grid, 0), grid)
    clockwise = rotate_rgb_quarters(grid, 1)
    assert clockwise.shape[:2] == (2, 2)
    assert clockwise[0, 0, 0] == 3 and clockwise[0, 1, 0] == 1   # [[3,1],[4,2]]
    assert clockwise[1, 0, 0] == 4 and clockwise[1, 1, 0] == 2
    tall = np.zeros((4, 6, 3), np.uint8)
    assert rotate_rgb_quarters(tall, 1).shape == (6, 4, 3)     # 90°：宽高互换
    assert rotate_rgb_quarters(tall, 2).shape == (4, 6, 3)
    back = rotate_rgb_quarters(rotate_rgb_quarters(tall, 1), 3)
    assert np.array_equal(back, tall) and back.flags["C_CONTIGUOUS"]


def test_fit_aspect_odd_source_keeps_ratio_with_even_output():
    frame = SimpleNamespace(width=853, height=477)
    w, h = fit_aspect_size(frame, 400, 400)
    assert w % 2 == 0 and h % 2 == 0
    assert abs(w / h - 853 / 477) < 0.02      # 奇数源不拉伸（偶对齐容差内）


def test_fit_aspect_sar_still_applies_with_rotation():
    frame = SimpleNamespace(width=320, height=180)
    # SAR 2:1 的 320×180 显示为 640×180（32:9）；装 400×200 → 400×112
    w, h = fit_aspect_size(frame, 400, 200, sar=Fraction(2, 1))
    assert (w, h) == (400, 112)
    # 旋转 + SAR 同时存在：框转置后 SAR 仍作用于源宽（显示比例主导）
    w2, h2 = fit_aspect_size(frame, 400, 200, sar=Fraction(2, 1),
                             rotation_quarters=1)
    assert w2 % 2 == 0 and h2 % 2 == 0
    assert abs(w2 / h2 - 320 * 2 / 180) < 0.02


# ---------------------------------------------------------------------------
# 集成（ffmpeg 合成素材）：真实 PyAV 会话的旋转探取与落地
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rotated_clips(tmp_path_factory):
    if FFMPEG is None:
        pytest.skip("本机无 ffmpeg CLI，跳过旋转合成素材")
    import subprocess

    workdir = tmp_path_factory.mktemp("e4_rot")
    base = workdir / "base.mp4"
    subprocess.run(
        [FFMPEG, '-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'testsrc2=size=320x180:duration=0.5:rate=10',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(base)],
        check=True)
    clips = {"base": base}
    for deg in (90, 180, 270):
        out = workdir / f"dm{deg}.mov"
        subprocess.run(
            [FFMPEG, '-y', '-loglevel', 'error',
             '-display_rotation', str(deg), '-i', str(base),
             '-c', 'copy', str(out)],
            check=True)
        clips[deg] = out
    return clips


@pytest.mark.parametrize("deg,quarters", [(90, 1), (180, 2), (270, 3)])
def test_libav_session_extracts_display_rotation(rotated_clips, deg, quarters):
    session = _LibavSession(str(rotated_clips[deg]))
    try:
        assert session.rotation_quarters == quarters
        assert session.frame_count > 0
    finally:
        session.close()


def test_libav_session_without_rotation_is_zero(rotated_clips):
    session = _LibavSession(str(rotated_clips["base"]))
    try:
        assert session.rotation_quarters == 0
    finally:
        session.close()


def test_libav_rgb_path_applies_rotation_fit_and_pixels(rotated_clips):
    base = _LibavSession(str(rotated_clips["base"]))
    rotated = _LibavSession(str(rotated_clips[90]))
    try:
        # 拟合口径：400×200 框 + 90° 旋转 → 解码 200×112 → 旋转后竖幅 112×200
        rgb = rotated.frame_rgb(0, size=(400, 200))
        assert rgb.shape == (200, 112, 3)
        # 像素口径：旋转素材的全尺寸帧 == 源帧顺时针转 90°（同一解码器，逐像素）
        rotated_full = rotated.frame_rgb(0)
        base_full = base.frame_rgb(0)
        assert rotated_full.shape == (320, 180, 3)   # 旋转后 H/W 互换
        assert np.array_equal(rotated_full, np.rot90(base_full, k=-1))
    finally:
        base.close()
        rotated.close()


def test_libav_bt2020_pq_metadata_flows_to_yuv(tmp_path):
    """BT.2020+PQ 色彩元数据在 YUV 路径原样保留（H.273 码值不丢）。"""
    if FFMPEG is None:
        pytest.skip("本机无 ffmpeg CLI，跳过色彩合成素材")
    import subprocess

    clip = tmp_path / "pq.mp4"
    subprocess.run(
        [FFMPEG, '-y', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'testsrc2=size=320x180:duration=0.5:rate=10',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
         '-colorspace', 'bt2020nc', '-color_primaries', 'bt2020',
         '-color_trc', 'smpte2084', str(clip)],
        check=True)
    session = _LibavSession(str(clip))
    try:
        frame = session.frame_yuv(0)
        assert frame.matrix == 9          # BT2020_NCL（H.273）
        assert frame.subsampling == '420'
        # RGB 路径同源可用（色彩/HDR 展示口径一致，不做色调映射）
        rgb = session.frame_rgb(0)
        assert rgb.shape[2] == 3 and rgb.shape[0] > 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 时基矩阵：24/25 精确帧率（30000÷1001 已在 FR-19 既有用例）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample_count,ticks,timescale,expected_fps", [
    (24, 1, 24, 24.0),        # 1 秒 24 帧：timescale 即每秒 tick
    (25, 1, 25, 25.0),
    (50, 2, 50, 25.0),        # ticks≠1 的 25fps
])
def test_fr19_exact_rates_matrix(sample_count, ticks, timescale, expected_fps):
    from tests.ui.test_playback_semantics_regressions import (
        _FakeMovie, _close_session, _session_with_movie,
    )
    movie = _FakeMovie(sample_count=sample_count, ticks_per_frame=ticks,
                       timescale=timescale)
    session = _session_with_movie(movie)
    try:
        assert session.fps == pytest.approx(expected_fps)
        assert session.duration_sec == pytest.approx(
            sample_count / expected_fps)
    finally:
        _close_session(session)


def test_topos_session_has_no_rotation_contract():
    from tests.ui.test_playback_semantics_regressions import (
        _FakeMovie, _close_session, _session_with_movie,
    )
    movie = _FakeMovie(sample_count=4, ticks_per_frame=1, timescale=4)
    session = _session_with_movie(movie)
    try:
        assert session.rotation_quarters == 0   # Topos 容器无旋转概念
    finally:
        _close_session(session)


def test_libav_player_avoids_gpu_path_when_rotated(qtbot):
    """[E4] YUV/GPU 路径不支持旋转：有显示旋转时播放器必须走 RGB。"""
    from src.shared.playback.libav_player import LibavPlayer

    player = LibavPlayer()
    qtbot.addWidget(player)
    calls = {"yuv": 0, "rgb": 0}

    class _Canvas:
        gpu_active = True
        def display_size(self):
            return (400, 200)

    class _RotatedSession:
        rotation_quarters = 1
        def frame_yuv(self, index, size=None):
            calls["yuv"] += 1
            raise AssertionError("旋转素材不得走 YUV/GPU 路径")
        def frame_rgb(self, index, size=None):
            calls["rgb"] += 1
            return np.zeros((4, 6, 3), np.uint8)

    player.canvas = _Canvas()
    result = player._decode_frame(_RotatedSession(), 0)
    assert calls == {"yuv": 0, "rgb": 1}
    assert result is not None

    class _PlainSession(_RotatedSession):
        rotation_quarters = 0
        def frame_yuv(self, index, size=None):
            calls["yuv"] += 1
            return object()   # YuvFrame 占位（不再深入 GPU 上传）
        def frame_rgb(self, index, size=None):
            calls["rgb"] += 1
            return np.zeros((4, 6, 3), np.uint8)

    player._decode_frame(_PlainSession(), 0)
    assert calls["yuv"] == 1      # 未旋转素材照常优先 GPU YUV
