"""M-B9：v1.5 tmcd 时间码轨绑定层 + 互操作（container_spec v1.5）。

验收（plan §3 M-B9）：
- set_timecode → timecode() roundtrip（fps×DF 矩阵）+ format()；
- frame_timecode(index) DF 跳帧边界表（29.97DF：`00:00:59;29 →
  00:01:00;02`；59.94DF 跳 4：`00:01:00;04` ↔ 计数 3600，ffmpeg
  oracle 实测）；
- ffprobe format.tags.timecode 读回一致（我们写 ↔ ffmpeg 读）；
- **mov→mov copy 保留、mov→mp4 丢失**（tmcd 为 MOV 族独有，语义对照）；
- 校验 fail-fast：fps 白名单、DF 配 24、ff≥fps、DF 分钟首帧标签
  （30DF 拒 ff<2 / 60DF 拒 ff<4）、重复声明。

库未构建时整体 skip（构建方式见 native/topos_codec/README.md）。
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposCodecError,
)

_W, _H = 64, 48
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


@pytest.fixture(scope="module")
def codec() -> ToposCodec:
    import os

    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is None and not any(
        p.is_file() for p in topos_binding.candidate_library_paths()
    ):
        pytest.skip("libtopos_codec 未构建；先运行 "
                    "bash native/topos_codec/scripts/build_release_repo.sh")
    status, lib_path, lib = topos_binding.first_resolved_library(explicit)
    if status == topos_binding.LIB_RESOLUTION_NOT_FOUND:
        pytest.skip("libtopos_codec 未构建；先运行 "
                    "bash native/topos_codec/scripts/build_release_repo.sh")
    if lib is None:
        pytest.fail(f"libtopos_codec 不可加载（{status}）：{lib_path}")
    for sym in ("tc_mux_set_timecode", "tc_movie_timecode"):
        if not hasattr(lib, sym):
            pytest.fail(
                f"libtopos_codec 缺少时间码符号 {sym}——请重建：bash "
                f"native/topos_codec/scripts/build_release_repo.sh")
    return ToposCodec(lib)


def _frame_config() -> topos_binding._CFrameConfig:
    fc = topos_binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = _W
    fc.visible_height = _H
    fc.profile = 3
    fc.pixel_format = 0  # 4:2:2
    fc.bit_depth = 10
    fc.qmatrix_id = 1
    fc.qp_base = 24
    fc.color_range = 1
    fc.color_primaries = 1
    fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = 1
    fc.sar_den = 1
    return fc


def _synth_planes() -> list[bytes]:
    import struct

    cw = (_W + 1) // 2
    y = struct.pack(f"<{_W * _H}H",
                    *[((x * 7 + r * 13) % 400 + 300)
                      for r in range(_H) for x in range(_W)])
    u = struct.pack(f"<{cw * _H}H",
                    *[(480 + (x % 5) * 8) for r in range(_H) for x in range(cw)])
    v = struct.pack(f"<{cw * _H}H",
                    *[(520 + (r % 4) * 6) for r in range(_H) for x in range(cw)])
    return [y, u, v]


_PLANES: list[bytes] | None = None


def _encode_packets(codec: ToposCodec, count: int) -> list[bytes]:
    global _PLANES
    if _PLANES is None:
        _PLANES = _synth_planes()
    fc = _frame_config()
    return [codec.encode_frame(fc, _PLANES)[0] for _ in range(count)]


def _mux_with_tc(codec: ToposCodec, path: Path, fps: int, df: int,
                 hh: int, mm: int, ss: int, ff: int) -> list[bytes]:
    """10 帧视频 + tmcd 声明（plan §2.7：时码与音频声明正交、恒末轨）。"""
    pkts = _encode_packets(codec, 10)
    cfg = codec.movie_config(_W, _H)
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        for i, p in enumerate(pkts):
            mux.add_packet(p, i, 1)
        mux.set_timecode(hh, mm, ss, ff, fps, df)
        mux.finish()
    return pkts


# fps × DF 矩阵（plan §3 M-B9 验收：24/25/30/48/50/60 × NDF、30/60 × DF）；
# want = 起始帧计数（DF 计数 = wall − 跳帧数；60DF 每分钟跳 4，30DF 跳 2）
_MATRIX = [
    (24, 0, 1, 2, 3, 12, (3600 + 120 + 3) * 24 + 12),
    (25, 0, 0, 0, 10, 20, 10 * 25 + 20),
    (30, 0, 0, 0, 10, 25, 10 * 30 + 25),
    (48, 0, 0, 0, 1, 47, 1 * 48 + 47),
    (50, 0, 0, 1, 1, 49, (60 + 1) * 50 + 49),
    (60, 0, 0, 0, 1, 59, 1 * 60 + 59),
    (30, 1, 0, 0, 59, 29, 1799),
    (60, 1, 0, 1, 0, 4, 3600),  # 60DF：;00-;03 无效（SMPTE 12M 跳 4）
]


@pytest.mark.parametrize("fps,df,hh,mm,ss,ff,want", _MATRIX)
def test_timecode_matrix_roundtrip(codec, tmp_path, fps, df, hh, mm, ss, ff,
                                   want) -> None:
    """mux set_timecode → demux timecode() 全字段 roundtrip + format()。"""
    path = tmp_path / f"tc_{fps}_{df}.mov"
    _mux_with_tc(codec, path, fps, df, hh, mm, ss, ff)
    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        tc = mv.timecode()
        assert tc.start_frame_count == want
        assert (tc.fps, tc.drop_frame) == (fps, df)
        assert (tc.hh, tc.mm, tc.ss, tc.ff) == (hh, mm, ss, ff)
        sep = ";" if df else ":"
        assert tc.format() == f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"
        assert mv.frame_timecode(0) == tc.format()


def test_df_boundary_frame_timecode(codec, tmp_path) -> None:
    """DF 跳帧边界：29.97DF `00:00:59;29 → 00:01:00;02`；
    59.94DF `00:01:00;04`（计数 3600）→ 下一帧 `00:01:00;05`。"""
    p29 = tmp_path / "df29.mov"
    _mux_with_tc(codec, p29, 30, 1, 0, 0, 59, 29)
    with topos_binding.ToposMovieFile(codec, str(p29)) as mv:
        assert mv.frame_timecode(0) == "00:00:59;29"
        assert mv.frame_timecode(1) == "00:01:00;02"  # ;00 ;01 跳过
        assert mv.frame_timecode(2) == "00:01:00;03"

    p60 = tmp_path / "df60.mov"
    _mux_with_tc(codec, p60, 60, 1, 0, 1, 0, 4)
    with topos_binding.ToposMovieFile(codec, str(p60)) as mv:
        assert mv.frame_timecode(0) == "00:01:00;04"
        assert mv.frame_timecode(1) == "00:01:00;05"
    # 计数 3599 = 00:00:59;59（分钟 0 不跳）
    _mux_with_tc(codec, tmp_path / "df60b.mov", 60, 1, 0, 0, 59, 59)
    with topos_binding.ToposMovieFile(
            codec, str(tmp_path / "df60b.mov")) as mv:
        assert mv.timecode().start_frame_count == 3599
        assert mv.frame_timecode(1) == "00:01:00;04"


def test_timecode_validation(codec, tmp_path) -> None:
    """fail-fast：白名单外/DF 配 24/ff≥fps/DF 分钟首帧标签/重复声明。"""
    path = tmp_path / "tc_bad.mov"
    pkts = _encode_packets(codec, 1)
    cfg = codec.movie_config(_W, _H)
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        mux.add_packet(pkts[0], 0, 1)
        with pytest.raises(ToposCodecError, match="fps 白名单"):
            mux.set_timecode(0, 0, 0, 0, 23)
        with pytest.raises(ToposCodecError, match="仅标称 30/60"):
            mux.set_timecode(0, 0, 0, 0, 24, 1)
        with pytest.raises(ToposCodecError, match="分量越界"):
            mux.set_timecode(24, 0, 0, 0, 24)
        with pytest.raises(ToposCodecError, match="分量越界"):
            mux.set_timecode(0, 0, 0, 24, 24)
        with pytest.raises(ToposCodecError, match="DF 分钟首帧"):
            mux.set_timecode(0, 1, 0, 1, 30, 1)  # 30DF 分钟首帧拒 ff<2
        with pytest.raises(ToposCodecError, match="DF 分钟首帧"):
            mux.set_timecode(0, 1, 0, 3, 60, 1)  # 60DF 拒 ff<4
        mux.set_timecode(1, 2, 3, 12, 24, 0)
        with pytest.raises(ToposCodecError, match="至多一次"):
            mux.set_timecode(1, 2, 3, 12, 24, 0)
        mux.finish()


def test_no_timecode_file_raises(codec, tmp_path) -> None:
    """无 tmcd 文件：timecode() → ToposCodecError（C 层 TC_ERR_STATE）。"""
    path = tmp_path / "no_tc.mov"
    pkts = _encode_packets(codec, 2)
    cfg = codec.movie_config(_W, _H)
    with topos_binding.ToposMuxFile(codec, str(path), cfg) as mux:
        for i, p in enumerate(pkts):
            mux.add_packet(p, i, 1)
        mux.finish()
    with topos_binding.ToposMovieFile(codec, str(path)) as mv:
        with pytest.raises(ToposCodecError, match="tmcd"):
            mv.timecode()


def test_probe_metadata_timecode(codec, tmp_path) -> None:
    """工厂 probe：tmcd 文件 timecode/timecode_fps/timecode_drop_frame
    正确；无 tmcd 文件三者同 None（零字段变化原则）。"""
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    tc_path = tmp_path / "probe_src.mov"
    _mux_with_tc(codec, tc_path, 30, 1, 0, 0, 59, 29)
    src = ToposMediaSource(str(tc_path))
    MediaSourceFactory.open_source(src)
    try:
        md = src.probe()
        assert md.timecode == "00:00:59;29"
        assert md.timecode_fps == 30
        assert md.timecode_drop_frame is True
    finally:
        src.close()

    plain_path = tmp_path / "plain.mov"
    pkts = _encode_packets(codec, 2)
    cfg = codec.movie_config(_W, _H)
    with topos_binding.ToposMuxFile(codec, str(plain_path), cfg) as mux:
        for i, p in enumerate(pkts):
            mux.add_packet(p, i, 1)
        mux.finish()
    src = ToposMediaSource(str(plain_path))
    MediaSourceFactory.open_source(src)
    try:
        md = src.probe()
        assert md.timecode is None
        assert md.timecode_fps is None
        assert md.timecode_drop_frame is None
    finally:
        src.close()


@pytest.mark.skipif(_FFPROBE is None, reason="ffprobe 不可用")
@pytest.mark.parametrize("fps,df,tc_str", [
    (25, 0, "00:00:10:20"),
    (30, 1, "00:00:59;29"),
    (60, 1, "00:01:00;04"),
])
def test_ffprobe_timecode_tag(codec, tmp_path, fps, df, tc_str) -> None:
    """我们写 ↔ ffmpeg 读：tmcd 数据流 stream_tags.timecode 与写入值
    逐字符一致（ffmpeg demuxer 从 stsd + 首样本重建；实测 tag 挂数据流
    级而非 format 级，ffmpeg 自产文件同构）。"""
    if df:
        prefix, ff = tc_str.split(";")
        hh, mm, ss = prefix.split(":")
    else:
        hh, mm, ss, ff = tc_str.split(":")
    path = tmp_path / f"probe_{fps}_{df}.mov"
    _mux_with_tc(codec, path, fps, df, int(hh), int(mm), int(ss), int(ff))
    out = subprocess.run(
        [_FFPROBE, "-v", "error", "-select_streams", "d",
         "-show_entries", "stream_tags=timecode",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert out == tc_str, f"ffprobe 读回 {out!r} ≠ 写入 {tc_str!r}"


class _Rate:
    """Rational 替身（encoder 侧以 num/den getattr 读取）。"""

    def __init__(self, num: int, den: int) -> None:
        self.num, self.den = num, den


class _Cfg:
    width, height = 64, 48
    profile = 'standard'
    crf = 44
    entropy_mode = 'rans2'
    has_alpha = False
    alpha_premultiplied = False
    pix_fmt = 'yuv422p10le'

    def __init__(self, fps: "_Rate") -> None:
        self.fps = fps


def _synth_frame_planes(n: int) -> list:
    """确定性 10bit 4:2:2 平面（条纹 + 扰动；encoder 直喂路径）。"""
    import numpy as np

    cw = (_W + 1) // 2
    rng = np.random.default_rng(11 + n)
    y = ((np.indices((_H, _W))[1] * 5 % 900 + 60).astype(np.uint16)
         + rng.integers(0, 8, (_H, _W)).astype(np.uint16))
    u = np.full((_H, cw), 512, np.uint16)
    v = np.full((_H, cw), 512, np.uint16)
    return [np.ascontiguousarray(y), u, v]


def test_export_start_timecode_wiring(codec, tmp_path) -> None:
    """导出接线：sequence 起始时码 → ToposVideoEncoder(start_timecode=)
    → 容器 tmcd 轨（plan §3 M-B9 任务 5：有则写、无则不写）。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.frame import DecodedFrame

    def _export(path, tc_str, fps) -> None:
        enc = ToposVideoEncoder(str(path), _Cfg(fps),
                                start_timecode=tc_str)
        enc.open()
        for k in range(4):
            enc.encode_frame(DecodedFrame(
                frame_index=k, pts=k, time_seconds=k / 25.0,
                data=None, planes=_synth_frame_planes(k),
                pixel_format='yuv422p10le', width=_W, height=_H))
        enc.close()

    # NDF 25fps：写入并读回
    out = tmp_path / "exp_ndf.mov"
    _export(out, "01:02:03:12", _Rate(25, 1))
    with topos_binding.ToposMovieFile(codec, str(out)) as mv:
        tc = mv.timecode()
        assert (tc.start_frame_count, tc.fps, tc.drop_frame) == \
            (3723 * 25 + 12, 25, 0)
        assert tc.format() == "01:02:03:12"

    # DF 30fps：分号分隔 → DF 标志 + 4 跳语义
    out = tmp_path / "exp_df.mov"
    _export(out, "00:00:59;29", _Rate(30000, 1001))
    with topos_binding.ToposMovieFile(codec, str(out)) as mv:
        tc = mv.timecode()
        assert (tc.start_frame_count, tc.fps, tc.drop_frame) == (1799, 30, 1)

    # 无时码：零字节变化语义 = 无 tmcd 轨（打开正常、timecode() STATE）
    out = tmp_path / "exp_none.mov"
    _export(out, "", _Rate(25, 1))
    with topos_binding.ToposMovieFile(codec, str(out)) as mv:
        with pytest.raises(ToposCodecError, match="tmcd"):
            mv.timecode()

    # 帧率不在标称白名单域（25×1.05）→ 降级不写，导出不中断
    out = tmp_path / "exp_offrate.mov"
    _export(out, "01:02:03:12", _Rate(2625, 100))  # 26.25fps
    with topos_binding.ToposMovieFile(codec, str(out)) as mv:
        with pytest.raises(ToposCodecError, match="tmcd"):
            mv.timecode()


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg 不可用")
def test_mov_mov_keeps_mov_mp4_loses(codec, tmp_path) -> None:
    """语义对照：tmcd 为 MOV 族独有——mov→mov copy 保留、mov→mp4 丢失。

    mov→mov 需 `-write_tmcd 0`：movenc 对既有 tmcd 数据流重封装外的
    自动补轨以视频流名义帧率生成 fps（本容器视频 trak 呈现网格
    ts=24000/帧 → 超出 u8 fps 域，见 ADR-C055 已知差异 K2）。
    mov→mov 后 DF 标志被 movenc 重封装丢弃（timecode_flags 恒 0），
    帧计数值保留 → 显示为 NDF 形态 `00:00:59:29`（K3，ffmpeg 行为）。
    本库 demux 拒绝 ffmpeg 重封装产物（其视频 trak 携带 edts，超出
    M-B1 输入域）——语义对照以 ffprobe 读回为准。
    """
    src = tmp_path / "src.mov"
    _mux_with_tc(codec, src, 30, 1, 0, 0, 59, 29)

    def _tag(p: Path) -> str:
        r = subprocess.run(
            [_FFPROBE, "-v", "error", "-select_streams", "d",
             "-show_entries", "stream_tags=timecode",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, check=True).stdout.strip()
        return r

    mov_out = tmp_path / "remux.mov"
    subprocess.run([_FFMPEG, "-v", "error", "-y", "-i", str(src),
                    "-map", "0", "-c", "copy", "-write_tmcd", "0",
                    str(mov_out)], check=True)
    # 帧计数 1799 保留（DF 标志被 movenc 重封装丢弃 → NDF 显示形态）
    assert _tag(mov_out) == "00:00:59:29", "mov→mov copy 丢失时码（应保留）"

    # mov→mp4：ProRes+tmcd 对照（本库 TPIC 视频本就不能 -c copy 进 MP4）。
    # MP4 标准无 tmcd 定义；无 ffmpeg 的非标 -write_tmcd 扩展时，重封装
    # 即丢（实测 ffmpeg 8.1 开着该扩展会向 MP4 塞非标 tmcd data 轨——
    # 记录于 ADR-C055 差异表；标准 MP4 读取方不识别）。
    oracle = tmp_path / "oracle_prores.mov"
    subprocess.run(
        [_FFMPEG, "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=64x48:rate=30", "-frames:v", "4",
         "-c:v", "prores_ks", "-profile:v", "0",
         "-timecode", "00:00:59;29", "-write_tmcd", "1", str(oracle)],
        check=True)
    assert _tag(oracle) == "00:00:59;29", "oracle 自产 tmcd 应可读"
    mp4_out = tmp_path / "remux.mp4"
    subprocess.run([_FFMPEG, "-v", "error", "-y", "-i", str(oracle),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-write_tmcd", "0", "-f", "mp4", str(mp4_out)], check=True)
    assert _tag(mp4_out) == "", "mov→mp4 竟保留时码（tmcd 应不支持）"
