"""阶段 6：ToposMediaSource 集成测试（Topos MOV 解码接入 + R-17 路由回归）。

素材由 ctypes 绑定即时生成（确定性合成帧 → encode → mux），C 库未构建时
整体 skip（构建方式见 native/topos_codec/README.md）。

覆盖计划 §12 阶段 6 门禁：
- 导入、探测、逐帧播放、随机拖拽（随机访问 == 顺序访问，bit-exact）；
- close/重开/上下文管理器（native 句柄与 fd 全路径释放）；
- 帧级损坏隔离（slice conceal 交付 / header 损坏整帧拒绝、不污染邻帧）；
- proxy 缩小（分离 box 精确性）与放大回退；
- factory auto 内容探测路由 + 显式后端选择（R-17 回归）。
"""
from __future__ import annotations

import ctypes

import numpy as np
import pytest
from pathlib import Path

pytestmark = pytest.mark.media_core

from src.shared.codec import topos_binding
from src.shared.codec.topos_binding import (
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    ToposMuxFile,
)

# 素材参数：64×40（4:2:2 色度 32×40）、5 帧、24fps（timescale 24000、dur 1000）
W, H, N = 64, 40, 5
TIMESCALE, FRAME_DUR = 24000, 1000


def _lib_available() -> bool:
    import os

    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is not None:
        return True
    return any(p.is_file() for p in topos_binding.candidate_library_paths())


@pytest.fixture(scope="module")
def codec() -> ToposCodec:
    if not _lib_available():
        pytest.skip("libtopos_codec 未构建；先运行 bash native/topos_codec/run_tests.sh")
    return ToposCodec()


def _frame_config(
    w: int,
    h: int,
    qp: int = 20,
    alpha_mode: int = 0,
    alpha_bit_depth: int = 16,
) -> topos_binding._CFrameConfig:
    """与 movie_config 默认色彩字段严格一致（tpcC 一致性由 mux 强制）。"""
    c = topos_binding._CFrameConfig()
    c.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    c.abi_version = TOPOS_CODEC_ABI_VERSION
    c.visible_width, c.visible_height = w, h
    c.qp_base = qp
    c.qmatrix_id = 1
    c.alpha_mode = alpha_mode
    c.alpha_bit_depth = alpha_bit_depth if alpha_mode else 0
    c.color_range = 1
    c.color_primaries = 1
    c.color_transfer = 1
    c.color_matrix = 1
    c.sar_num = 1
    c.sar_den = 1
    return c


def _plane_bytes(i: int, w: int, h: int, with_alpha: bool) -> list[bytes]:
    """确定性合成帧（梯度 + 帧偏移；值域 0..1023 的 10-bit 满刻度）。"""
    cw = (w + 1) // 2

    def grad(w_: int, h_: int, base: int, step: int) -> bytes:
        a = (
            (np.arange(w_)[None, :] * 7 + np.arange(h_)[:, None] * 13 + base + i * step)
            % 400
            + 300
        ).astype("<u2")
        return a.tobytes()

    planes = [grad(w, h, 300, 17), grad(cw, h, 480, 11), grad(cw, h, 520, 13)]
    if with_alpha:
        planes.append((np.full((h, w), 60000, "<u2") - i * 3000).tobytes())
    return planes


def _write_movie(
    codec: ToposCodec,
    path: str,
    *,
    w: int = W,
    h: int = H,
    n: int = N,
    qp: int = 20,
    alpha_mode: int = 0,
    alpha_bit_depth: int = 16,
    corrupt: "dict | None" = None,
) -> list[int]:
    """生成标准布局 Topos MOV；返回逐帧 packet 尺寸（供损坏偏移计算）。

    corrupt: {"frame": i, "offset": o, "xor": v} —— 在第 i 帧 packet 内
    偏移 o 处按位异或 v（标准布局首 packet 在文件偏移 36）。
    """
    fc = _frame_config(w, h, qp=qp, alpha_mode=alpha_mode, alpha_bit_depth=alpha_bit_depth)
    mc = codec.movie_config(
        w, h, qp=qp, qmatrix=1,
        alpha_mode=alpha_mode, alpha_bit_depth=alpha_bit_depth,
    )
    sizes: list[int] = []
    with ToposMuxFile(codec, path, mc) as mux:
        for i in range(n):
            pkt, _ = codec.encode_frame(fc, _plane_bytes(i, w, h, alpha_mode != 0))
            sizes.append(len(pkt))
            mux.add_packet(pkt, pts=i * FRAME_DUR, dur=FRAME_DUR)
        mux.finish()
    if corrupt is not None:
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        off = 36 + sum(sizes[: corrupt["frame"]]) + corrupt["offset"]
        data[off] ^= corrupt["xor"]
        with open(path, "wb") as fh:
            fh.write(bytes(data))
    return sizes


@pytest.fixture(scope="module")
def movie_path(codec: ToposCodec, tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("topos_src") / "std.mov")
    _write_movie(codec, path)
    return path


# —— probe / 元数据 ——


def test_probe_metadata(movie_path: str) -> None:
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.metadata import Rational
    from src.shared.media.topos_source import ToposMediaSource
    from src.shared.color.types import (
        ColorMetadataAuthority,
        ColorMatrix,
        ColorPrimaries,
        ColorRange,
        ColorTransfer,
    )

    src = ToposMediaSource(movie_path)
    MediaSourceFactory.open_source(src)
    try:
        md = src.probe()
        assert (md.width, md.height) == (W, H)
        assert md.fps == Rational(24, 1)  # 24000/1000 自动约分
        assert md.total_frames == N
        assert md.codec_name == "topos"
        assert md.codec_profile == "Topos 422"  # profile 3 外部显示名（M10-6.4 命名对齐）
        assert md.pixel_format == "yuv422p10le"  # 无 Alpha：复用注册名
        assert md.bit_depth == 10
        assert md.color_primaries == "bt709"
        assert md.color_transfer == "bt709"
        assert md.color_matrix == "bt709"
        assert md.color_range == "full"  # tpcC 1=full（非 FFmpeg 枚举）
        assert md.timebase == Rational(1, TIMESCALE)
        assert md.duration_ticks == N * FRAME_DUR
        assert md.duration_seconds == pytest.approx(N * FRAME_DUR / TIMESCALE)
        semantics = src.color_semantics
        assert semantics.display_authority is ColorMetadataAuthority.CICP
        assert semantics.color_range is ColorRange.FULL
        assert semantics.color_primaries is ColorPrimaries.BT709
        assert semantics.color_transfer is ColorTransfer.BT709
        assert semantics.color_matrix is ColorMatrix.BT709
        assert md.has_alpha is False
        assert md.field_order == "progressive"
        assert md.duration_seconds == pytest.approx(N / 24)
        # VideoSource 便捷属性 / 可选能力探测
        assert src.is_planar_mode is True
        assert src.output_mode == "planar_yuv"
        assert src.fps == pytest.approx(24.0)
        assert src.total_frames == N
        assert src.read_audio_samples(0, 100) is None  # v1.0 无音频轨（C-18）
        src.set_preserve_planes(False)  # no-op，恒 planar
        assert src.is_planar_mode is True
    finally:
        src.close()


# —— 逐帧播放 / 随机拖拽 ——


def _open(movie_path: str):
    from src.shared.media.topos_source import ToposMediaSource

    src = ToposMediaSource(movie_path)
    src.open()
    return src


def test_sequential_read_and_eof(movie_path: str) -> None:
    src = _open(movie_path)
    try:
        for i in range(N):
            f = src.read_frame(i)
            assert f.frame_index == i
            assert f.pts == i * FRAME_DUR
            assert f.time_seconds == pytest.approx(i / 24)
            assert f.is_planar
            assert len(f.planes) == 3
            assert f.planes[0].shape == (H, W)
            assert f.planes[1].shape == (H, (W + 1) // 2)  # 4:2:2 全高色度
            assert f.planes[2].shape == (H, (W + 1) // 2)
            assert f.planes[0].dtype == np.dtype("<u2")
            # 满刻度 10-bit 码值（不左移到 16-bit）
            assert int(f.planes[0].max()) < 1024
            assert f.plane_infos[0].stride == W * 2  # 应用层 stride = 行字节数
            assert f.plane_infos[1].width == (W + 1) // 2
            assert f.plane_infos[2].bit_depth == 10
            assert f.is_proxy is False
            assert f.extra is not None
            assert f.extra["topos_concealed_slices"] == 0
            assert f.extra["topos_color_metadata_authority"] == "cicp"
        assert src.current_frame == N
        with pytest.raises(StopIteration):
            src.read_next_frame()
        with pytest.raises(ValueError):
            src.read_frame(N)
        with pytest.raises(ValueError):
            src.read_frame(-1)
    finally:
        src.close()


def test_random_access_matches_sequential(movie_path: str) -> None:
    seq = _open(movie_path)
    try:
        ref = [bytes(np.asarray(f.planes[0]).tobytes()) for f in
               (seq.read_frame(i) for i in range(N))]
    finally:
        seq.close()
    rnd = _open(movie_path)
    try:
        for i in (3, 0, 4, 1, 2):  # 全 intra + O(1) 索引：乱序 bit-exact
            f = rnd.read_frame(i)
            assert bytes(np.asarray(f.planes[0]).tobytes()) == ref[i]
            assert rnd.current_frame == i + 1
    finally:
        rnd.close()


def test_seek_semantics(movie_path: str) -> None:
    src = _open(movie_path)
    try:
        src.seek_frame(2)
        assert src.current_frame == 2
        assert src.read_next_frame().frame_index == 2
        # 游标命中时 read_frame 免 seek（语义等价：仍读到该帧）
        src.seek_frame(1)
        assert src.read_frame(1).frame_index == 1
        with pytest.raises(ValueError):
            src.seek_frame(N)
        # get_frame_at_time 夹取
        f = src.get_frame_at_time(2.5 / 24)
        assert f.frame_index == 2
        assert src.get_thumbnail() is None
    finally:
        src.close()


def test_get_frame_at_time_bounds(movie_path: str) -> None:
    src = _open(movie_path)
    try:
        assert src.get_frame_at_time(-1.0).frame_index == 0
        assert src.get_frame_at_time(1e9).frame_index == N - 1
    finally:
        src.close()


# —— Alpha / FastStart / 代理 ——


def test_alpha_scratch_reuse_and_hold_safety(codec: ToposCodec, tmp_path) -> None:
    """P1-13：alpha 移位写入池内 scratch——无持帧时对象复用（零新分配）；
    持有移位帧时池不得复用其底层 scratch（内容不被后续帧覆盖）。"""
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "scratch.mov")
    _write_movie(codec, path, w=36, h=20, n=4, alpha_mode=2, alpha_bit_depth=10)
    src = ToposMediaSource(path, read_ahead=0)
    src.open()
    try:
        f0 = src.read_frame(0)
        a0 = f0.planes[3]
        assert a0.max() <= 1023 and not a0.flags.writeable  # 移位后只读契约
        # 无持帧（f0 已在 f1 读取前释放引用）：scratch 复用 → 同一对象
        f1 = src.read_frame(1)
        a1 = f1.planes[3]
        assert a1.max() <= 1023
        # 读取 f2 前先丢弃 f1（f0 仍被 a0 引用持有）
        del f1
        f2 = src.read_frame(2)
        assert f2.planes[3].max() <= 1023
        # 持帧安全：a0 持有期间后续帧不得污染其内容（若 scratch 被复用，
        # a0 的值会被帧 3 的移位覆盖 → 值变化可检测）
        before = a0.copy()
        src.read_frame(3)
        import numpy as _np
        assert _np.array_equal(a0, before), "持帧期间移位 scratch 被复用污染"
    finally:
        src.close()


def test_alpha_variant(codec: ToposCodec, tmp_path) -> None:
    from src.shared.media.frame import PIXEL_FORMATS
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "alpha.mov")
    _write_movie(codec, path, w=36, h=20, n=3, alpha_mode=2, alpha_bit_depth=10)
    src = ToposMediaSource(path)
    src.open()
    try:
        md = src.probe()
        assert md.has_alpha is True
        assert md.pixel_format == "topos_yuva422p10a10"
        assert md.pixel_format in PIXEL_FORMATS
        f = src.read_frame(0)
        assert len(f.planes) == 4
        assert f.planes[3].shape == (20, 36)  # Alpha 全分辨率
        assert f.plane_infos[3].component == "A"
        assert f.plane_infos[3].bit_depth == 10  # 逐面位深为权威
        assert f.has_alpha
    finally:
        src.close()


def test_drop_alpha_request_negotiates_or_falls_back(codec: ToposCodec, tmp_path) -> None:
    """不支持新 native 能力的 DLL 必须保留 Alpha，不能破坏旧库兼容性。"""
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "alpha-drop.mov")
    _write_movie(codec, path, w=36, h=20, n=1, alpha_mode=2, alpha_bit_depth=10)
    src = ToposMediaSource(path, drop_alpha=True)
    src.open()
    try:
        frame = src.read_frame(0)
        if src.diagnostics["native_drop_alpha"]:
            assert len(frame.planes) == 3
            assert not frame.has_alpha
        else:
            assert len(frame.planes) == 4
            assert frame.has_alpha
    finally:
        src.close()


def test_no_deblock_request_skips_output_filter() -> None:
    """带 DEBLOCK 特性位的**真实产品流**：预览关滤波时解码输出必须不同。

    这是 native 请求位 TC_DECODE_FLAG_NO_DEBLOCK 的端到端钉子——单元测试
    （native/topos_codec/tests/unit/test_codec.c）只钉"标志跳过的是内核那一层"，
    落像素与否取决于门控；本用例用产品码流（proxy 档，块界台阶明显）证明
    开关真的改变画面。语料未导入时跳过（tests/test_videos 媒体不入库）。
    """
    corpus = Path(__file__).resolve().parents[1] / "test_videos" / "2K_topos_422_Proxy.mov"
    if not corpus.is_file():
        pytest.skip("语料缺失：tests/test_videos/2K_topos_422_Proxy.mov")
    from src.shared.media.topos_source import ToposMediaSource

    src_on = ToposMediaSource(str(corpus), no_deblock=False)
    src_off = ToposMediaSource(str(corpus), no_deblock=True)
    src_on.open()
    src_off.open()
    try:
        assert src_off.diagnostics["native_no_deblock"] is True, (
            "dylib 未广播 NO_DEBLOCK 能力——请求位不会生效（旧库 fail-closed）"
        )
        assert src_on.diagnostics["native_no_deblock"] is False
        f_on = src_on.read_frame(0)
        f_off = src_off.read_frame(0)
        assert len(f_on.planes) == len(f_off.planes)
        assert not np.array_equal(np.asarray(f_on.planes[0]),
                                  np.asarray(f_off.planes[0]))
        # 静息（无请求）路径：不带开关的 source 与带滤波 source 必须一致
        plain = ToposMediaSource(str(corpus))
        plain.open()
        try:
            f_plain = plain.read_frame(0)
            assert np.array_equal(np.asarray(f_plain.planes[0]),
                                  np.asarray(f_on.planes[0]))
        finally:
            plain.close()
    finally:
        src_on.close()
        src_off.close()


def test_faststart_layout(codec: ToposCodec, movie_path: str, tmp_path) -> None:
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource, is_topos_movie

    fs_path = str(tmp_path / "fs.mov")
    codec.faststart_file(movie_path, fs_path)
    assert is_topos_movie(fs_path)  # moov-first 布局同样可探测
    src = ToposMediaSource(movie_path)
    src.open()
    ref0 = bytes(np.asarray(src.read_frame(0).planes[0]).tobytes())
    src.close()

    made = MediaSourceFactory.create(fs_path)  # auto 路由
    MediaSourceFactory.open_source(made)
    try:
        assert isinstance(made, ToposMediaSource)
        assert made.total_frames == N
        f0 = made.read_frame(0)
        assert bytes(np.asarray(f0.planes[0]).tobytes()) == ref0
        assert f0.pts == 0
        assert made.read_frame(4).pts == 4 * FRAME_DUR
    finally:
        made.close()


def test_target_size_downscale_exact(codec: ToposCodec, tmp_path) -> None:
    from src.shared.media.topos_source import ToposMediaSource
    from src.shared.codec.topos_binding import ToposMovieFile

    path = str(tmp_path / "scale.mov")
    _write_movie(codec, path, n=3)
    native = ToposMediaSource(path)
    native.open()
    proxy = ToposMediaSource(path, target_size=(W // 2, H // 2))
    proxy.open()
    try:
        fp = proxy.read_frame(2)
        assert (fp.width, fp.height) == (W // 2, H // 2)
        assert fp.planes[1].shape == (H // 2, W // 4)  # 色度同比例缩小
        assert fp.is_proxy is True
        assert proxy._native_target_decode is True
        assert proxy.diagnostics["decode_request_mode"] == \
            topos_binding.TC_DECODE_MODE_REDUCED
        assert proxy.diagnostics["decode_request_scale"] == \
            topos_binding.TC_DECODE_SCALE_HALF
        assert proxy.diagnostics["bitstream_major"] in (1, 2, 3)
        assert proxy.diagnostics["stream_compatibility"] == "legacy_v1_v6"
        assert proxy.diagnostics["legacy_reduced_fallback"] is True
        # RD1：目标几何严格匹配 1/2 时，播放器结果应与 native reduced
        # request 一致，而不是完整帧后的 Python box resize。
        with ToposMovieFile(codec, path) as movie:
            expected = codec.decode_request(
                movie.packet(2),
                codec.make_decode_request(
                    topos_binding.TC_DECODE_MODE_REDUCED,
                    scale=topos_binding.TC_DECODE_SCALE_HALF,
                ),
            )
        for plane_idx in (0, 1):
            out_p = np.asarray(fp.planes[plane_idx])
            exp = np.frombuffer(expected.planes[plane_idx], dtype="<u2").reshape(
                out_p.shape
            )
            assert bool((exp == out_p).all())
        assert fp.plane_infos[0].stride == (W // 2) * 2
    finally:
        native.close()
        proxy.close()


def test_v7b_preview_uses_movie_base_range(codec: ToposCodec, tmp_path) -> None:
    """RD4-04：V7-B MOV 预览直接取 base range，不读完整 residual sample。"""
    from src.shared.media.topos_source import ToposMediaSource

    if (not getattr(codec, "_has_scalable_encoder", False) or
            not getattr(codec, "_has_movie_packet_base", False) or
            not getattr(codec, "_has_movie_packet_base_batch", False)):
        pytest.skip("dylib 缺少 V7-B writer 或 base range API")
    w, h = 48, 24
    fc = _frame_config(w, h, qp=24)
    path = str(tmp_path / "v7b_preview.mov")
    with ToposMuxFile(codec, path, codec.movie_config(w, h, qp=24, qmatrix=1)) as mux:
        for i in range(3):
            packet = codec.encode_scalable(
                fc, _plane_bytes(i, w, h, False), base_max_dim=16
            )
            mux.add_packet(packet, pts=i * FRAME_DUR, dur=FRAME_DUR)
        mux.finish()

    src = ToposMediaSource(path, target_size=(w // 3, h // 3), read_ahead=3)
    src.open()
    try:
        frame = src.read_frame(0)
        assert (frame.width, frame.height) == (w // 3, h // 3)
        frame = src.read_frame(1)
        assert (frame.width, frame.height) == (w // 3, h // 3)
        assert src.diagnostics["decode_request_mode"] == \
            topos_binding.TC_DECODE_MODE_REDUCED
        assert src.diagnostics["read_ahead_depth"] == 3
        assert src.diagnostics["decode_path_reason"] == "ok"
        assert src.diagnostics["v7b_base_range_decode"] is True
        assert src.diagnostics["v7b_base_range_samples"] == 3
        assert src.diagnostics["read_ahead_stats"]["batch_decodes"] == 1
        assert src.diagnostics["read_ahead_stats"]["prefetched_frames"] == 1
    finally:
        src.close()


def test_legacy_stream_compatibility_diagnostics_do_not_rewrite(
    codec: ToposCodec, tmp_path
) -> None:
    """RD7-02：旧 V1/V2 MOV 可读，旧流 reduced 路径且不发生写回。"""
    from src.shared.codec.topos_binding import ToposMovieFile
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "legacy_compat.mov")
    _write_movie(codec, path, n=2)
    before = open(path, "rb").read()
    with ToposMovieFile(codec, path) as movie:
        summary = movie.info()
        assert summary.bitstream_major in (1, 2, 3)

    src = ToposMediaSource(path, target_size=(W // 2, H // 2))
    src.open()
    try:
        frame = src.read_frame(0)
        assert (frame.width, frame.height) == (W // 2, H // 2)
        assert src.diagnostics["stream_compatibility"] == "legacy_v1_v6"
        assert src.diagnostics["legacy_reduced_fallback"] is True
        assert open(path, "rb").read() == before
    finally:
        src.close()


def test_target_size_upscale_falls_back(codec: ToposCodec, tmp_path) -> None:
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "up.mov")
    _write_movie(codec, path, n=1)
    src = ToposMediaSource(path, target_size=(W * 4, H * 4))
    src.open()
    try:
        f = src.read_frame(0)
        assert (f.width, f.height) == (W, H)  # 放大回退原始分辨率
        assert f.is_proxy is False
    finally:
        src.close()


# —— close / 生命周期 ——


def test_close_reopen_and_context_manager(movie_path: str) -> None:
    from src.shared.media.topos_source import ToposMediaSource

    src = ToposMediaSource(movie_path)
    src.open()
    src.read_frame(0)
    src.close()
    src.close()  # 幂等
    assert src.is_open is False
    from src.shared.media.source import MediaSourceError

    with pytest.raises(MediaSourceError):
        src.read_frame(0)
    src.open()  # 可重开
    assert src.is_open and src.total_frames == N
    assert src.read_frame(0).frame_index == 0
    src.close()

    with ToposMediaSource(movie_path) as ctx_src:  # 上下文管理器
        assert ctx_src.is_open
    assert ctx_src.is_open is False


# —— 内容探测 / factory 路由（R-17 回归） ——


def test_sniff_structural(movie_path: str, tmp_path) -> None:
    from src.shared.media.topos_source import is_topos_movie

    assert is_topos_movie(movie_path) is True
    # 非 Topos 的 qt 容器：帧数据中随机出现 'TPIC' 字节也不得命中结构位置
    fake = bytearray(open(movie_path, "rb").read())
    fake[200:204] = b"TPIC"
    fake_path = str(tmp_path / "fake_qt.mov")
    # 让 mdat 首包 magic 失效（标准布局在 36），保留深处的假 TPIC
    fake[36:40] = b"\x00\x00\x00\x00"
    with open(fake_path, "wb") as fh:
        fh.write(bytes(fake))
    assert is_topos_movie(fake_path) is False
    # 非 ftyp 文件
    txt = str(tmp_path / "plain.txt")
    with open(txt, "wb") as fh:
        fh.write(b"TPIC" * 64)
    assert is_topos_movie(txt) is False
    assert is_topos_movie(str(tmp_path / "missing.mov")) is False


def _mov_atom(kind: bytes, body: bytes) -> bytes:
    """32 位 size 的 MOV atom 构造（测试辅助）。"""
    return (len(body) + 8).to_bytes(4, "big") + kind + body


def test_sniff_faststart_rejects_tpic_text(tmp_path) -> None:
    """P0-06 回归：FastStart 布局下 moov 内任意 'TPIC' 文本不得误判。

    旧实现 `b'TPIC' in head` 会把含该 ASCII 串的普通 QuickTime 文件
    误路由到 topos 后端；新实现只认 stsd 首个 sample entry 的 4CC。
    """
    from src.shared.media.topos_source import is_topos_movie

    ftyp = _mov_atom(b"ftyp", b"qt  " + b"\x00\x00\x02\x00" + b"qt  ")

    def moov_file(name: str, moov_body: bytes) -> str:
        p = tmp_path / name
        p.write_bytes(ftyp + _mov_atom(b"moov", moov_body))
        return str(p)

    # ① free atom 里带 TPIC 文本（无 stsd）
    p1 = moov_file("meta_tpic.mov", _mov_atom(b"mvhd", b"\x00" * 100)
                   + _mov_atom(b"free", b"TPIC magic here"))
    assert is_topos_movie(p1) is False

    # ② 完整容器链但 stsd 首个 entry 是普通编码器（'apch' = ProRes），
    #    TPIC 文本藏在 entry 内部字段
    stsd_apch = _mov_atom(
        b"stsd",
        b"\x00\x00\x00\x00" + (1).to_bytes(4, "big")
        + _mov_atom(b"apch", b"\x00" * 80 + b"TPIC" + b"\x00" * 8))
    stbl = _mov_atom(b"stbl", stsd_apch)
    minf = _mov_atom(b"minf", stbl)
    mdia = _mov_atom(b"mdia", minf)
    trak = _mov_atom(b"trak", mdia)
    moov = _mov_atom(b"mvhd", b"\x00" * 100) + trak
    p2 = moov_file("apch_with_tpic_text.mov", moov)
    assert is_topos_movie(p2) is False

    # ③ 真 TPIC sample entry：结构位置命中 → True
    stsd_tpic = _mov_atom(
        b"stsd",
        b"\x00\x00\x00\x00" + (1).to_bytes(4, "big")
        + _mov_atom(b"TPIC", b"\x00" * 88))
    stbl_t = _mov_atom(b"stbl", stsd_tpic)
    trak_t = _mov_atom(b"trak", _mov_atom(b"mdia", _mov_atom(b"minf", stbl_t)))
    p3 = moov_file("real_tpic.mov", _mov_atom(b"mvhd", b"\x00" * 100) + trak_t)
    assert is_topos_movie(p3) is True

    # ④ 截断/异常 moov size：安全 False，绝不抛异常
    truncated = ftyp + (0xFFFFFF).to_bytes(4, "big") + b"moov" + b"\x00" * 10
    p4 = tmp_path / "truncated.mov"
    p4.write_bytes(truncated)
    assert is_topos_movie(str(p4)) is False

    # ⑤ 64 位 largesize moov 形式 + 真 TPIC → True
    body = _mov_atom(b"mvhd", b"\x00" * 100) + trak_t
    moov64 = (1).to_bytes(4, "big") + b"moov" + (len(body) + 16).to_bytes(8, "big") + body
    p5 = tmp_path / "largesize.mov"
    p5.write_bytes(ftyp + moov64)
    assert is_topos_movie(str(p5)) is True


def test_factory_auto_and_explicit_routing(movie_path: str) -> None:
    from src.shared.media.factory import MediaBackend, MediaSourceFactory
    from src.shared.media.topos_source import ToposMediaSource

    assert MediaBackend.TOPOS.value == "topos"
    order = MediaSourceFactory._get_backend_order("auto", movie_path)
    assert order[0] == "topos"
    assert "ffmpeg" in order  # topos 失败可自愈回退

    src = MediaSourceFactory.create(movie_path)  # auto
    assert isinstance(src, ToposMediaSource)
    MediaSourceFactory.open_source(src)
    assert src.is_open
    src.close()

    explicit = MediaSourceFactory.create(movie_path, backend="topos")
    assert isinstance(explicit, ToposMediaSource)

    # 普通文件不进 topos 车道（无 path 时也不探测）
    assert "topos" not in MediaSourceFactory._get_backend_order("auto")


def test_factory_explicit_ffmpeg_rejected(movie_path: str) -> None:
    """强制 ffmpeg 打开 Topos MOV → PyAV 无法解释 TPIC，明确失败。

    这正是 R-17 的意义：没有内容探测路由，用户永远打不开自研格式。
    """
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.media.source import MediaOpenError

    with pytest.raises(MediaOpenError):
        src = MediaSourceFactory.create(movie_path, backend="ffmpeg")
        MediaSourceFactory.open_source(src)


def test_probe_video_metadata_via_factory(movie_path: str) -> None:
    """factory 快速探测链对 Topos 文件给出 codec_name='topos'（hwaccel 门用）。"""
    from src.shared.media.factory import MediaSourceFactory

    md = MediaSourceFactory.probe_video_metadata(movie_path)
    assert md is not None
    assert md.codec_name == "topos"
    assert md.total_frames == N


def test_fallback_strategy_backend_order(movie_path: str) -> None:
    from src.shared.media.error_handling import FallbackStrategy
    from src.shared.media.topos_source import ToposMediaSource

    strategy = FallbackStrategy()
    result = strategy._try_backend_switch(movie_path, original_backend="ffmpeg")
    assert result.success
    assert isinstance(result.source, ToposMediaSource)  # 顺序表按内容路由
    if result.source is not None:
        result.source.close()


# —— 帧级损坏隔离（容器 §7.6 + 位流 §9 conceal） ——


def test_concealed_frame_isolated(codec: ToposCodec, movie_path: str, tmp_path) -> None:
    from src.shared.media.topos_source import ToposMediaSource

    clean = _open(movie_path)
    try:
        ref = [bytes(np.asarray(f.planes[0]).tobytes()) for f in
               (clean.read_frame(i) for i in range(N))]
    finally:
        clean.close()

    path = str(tmp_path / "slice_bad.mov")
    _write_movie(codec, path, corrupt={"frame": 1, "offset": 53 + 17 + 2, "xor": 0x40})
    src = _open(path)
    try:
        f0 = src.read_frame(0)
        assert f0.extra is not None and f0.extra["topos_concealed_slices"] == 0
        assert bytes(np.asarray(f0.planes[0]).tobytes()) == ref[0]  # 邻帧不受污染
        f1 = src.read_frame(1)  # 坏 slice → conceal，帧仍交付
        assert f1 is not None
        assert f1.extra is not None
        assert f1.extra["topos_concealed_slices"] >= 1
        assert f1.planes[0].shape == (H, W)
        f2 = src.read_frame(2)
        assert bytes(np.asarray(f2.planes[0]).tobytes()) == ref[2]
    finally:
        src.close()


def test_whole_frame_corruption_rejected(codec: ToposCodec, tmp_path) -> None:
    """frame header 损坏 → 整帧拒绝（MediaDecodeError），其余帧完好。"""
    from src.shared.media.source import MediaDecodeError
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "hdr_bad.mov")
    _write_movie(codec, path, corrupt={"frame": 1, "offset": 30, "xor": 0x01})
    src = ToposMediaSource(path)
    src.open()
    try:
        assert src.read_frame(0).frame_index == 0
        with pytest.raises(MediaDecodeError):
            src.read_frame(1)
        assert src.read_frame(2).frame_index == 2
    finally:
        src.close()


def test_open_failures(tmp_path) -> None:
    from src.shared.media.source import MediaOpenError
    from src.shared.media.topos_source import ToposMediaSource

    if not _lib_available():
        pytest.skip("libtopos_codec 未构建")
    missing = str(tmp_path / "no_such.mov")
    with pytest.raises(MediaOpenError):
        ToposMediaSource(missing).open()
    foreign = str(tmp_path / "foreign.mov")
    with open(foreign, "wb") as fh:
        fh.write(b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 64)
    with pytest.raises(MediaOpenError):  # reader 拒绝非 TPIC entry（容器 §7.3）
        ToposMediaSource(foreign).open()


# —— 阶段4 兑现：顺序读前批量预取（read-ahead batch）——


class _DecodeSpy:
    """计数 decoder 批量/单帧调用（验证预取与缓存命中确实发生）。"""

    def __init__(self, src) -> None:
        dec = src._decoder_ctx
        assert dec is not None, "预取路径需 decoder context（M2）"
        self.counts = {"batch": 0, "single": 0}
        self._orig = (dec.decode_views_batch, dec.decode_views)
        orig_batch, orig_single = self._orig

        def spy_batch(*args, **kwargs):
            self.counts["batch"] += 1
            return orig_batch(*args, **kwargs)

        def spy_single(*args, **kwargs):
            self.counts["single"] += 1
            return orig_single(*args, **kwargs)

        dec.decode_views_batch = spy_batch
        dec.decode_views = spy_single

    def restore(self, src) -> None:
        dec = src._decoder_ctx
        dec.decode_views_batch, dec.decode_views = self._orig


def test_readahead_batch_bitexact_and_engagement(codec: ToposCodec, tmp_path) -> None:
    """读前批量：批量/缓存命中路径与单帧路径输出逐位一致；预取确实发生
    （批量调用计数），seek/跳读失效回单帧（不浪费批量解码）。"""
    from src.shared.media.topos_source import ToposMediaSource

    n = 9
    path = str(tmp_path / "ra.mov")
    _write_movie(codec, path, n=n)

    ref = ToposMediaSource(path, read_ahead=0)
    ra = ToposMediaSource(path, read_ahead=4)
    spy: _DecodeSpy | None = None
    try:
        ref.open()
        ra.open()
        assert ra._ra_depth == 4  # 显式深度生效
        spy = _DecodeSpy(ra)
        # 阶段4 兑现补丁：批量聚读 arena 确实被走（每次批量解码一次聚读）
        arena_calls = {"n": 0}
        orig_arena = ra._movie.packets_into_batch if ra._codec._has_movie_packet_batch else None

        def spy_arena(*args, **kwargs):
            arena_calls["n"] += 1
            return orig_arena(*args, **kwargs)

        if orig_arena is not None:
            ra._movie.packets_into_batch = spy_arena
        for i in range(n):
            f_ref = ref.read_frame(i)
            f_ra = ra.read_frame(i)
            assert f_ra.frame_index == i
            assert f_ra.pts == f_ref.pts
            assert f_ra.pixel_format == f_ref.pixel_format
            assert f_ra.plane_infos == f_ref.plane_infos
            assert f_ra.extra == f_ref.extra  # concealed/slice 计数透传
            assert len(f_ra.planes) == len(f_ref.planes)
            for p_ref, p_ra in zip(f_ref.planes, f_ra.planes):
                assert np.asarray(p_ra).tobytes() == np.asarray(p_ref).tobytes()
        # 帧数 9、深度 4：0 单解 → 1 批[1..4] → 2-4 命中 → 5 批[5..8] → 6-8 命中
        assert spy.counts == {"batch": 2, "single": 1}
        assert ra.current_frame == n
        ra_stats = ra.diagnostics["read_ahead_stats"]
        assert ra_stats["batch_decodes"] == 2
        assert ra_stats["prefetched_frames"] == 6
        assert ra_stats["cache_hits"] == 6
        assert ra_stats["batch_fallbacks"] == 0
        if orig_arena is not None:
            assert arena_calls["n"] == 2  # 每次批量解码一次聚读（零逐包物化）

        # seek 失效：重定位后首帧单解，随后顺序批量重建
        ra.seek_frame(3)
        ra.read_frame(3)
        assert spy.counts == {"batch": 2, "single": 2}
        ra.read_frame(4)
        assert spy.counts == {"batch": 3, "single": 2}

        # 跳读（重定位到末帧）不触发批量：缓存已失效，单解即达
        ra.seek_frame(8)
        ra.read_frame(8)
        assert spy.counts == {"batch": 3, "single": 3}
    finally:
        if spy is not None:
            spy.restore(ra)
        if orig_arena is not None:
            ra._movie.packets_into_batch = orig_arena
        ref.close()
        ra.close()


def test_readahead_auto_depth_and_env_override(codec: ToposCodec, tmp_path, monkeypatch) -> None:
    """播放默认关闭；导出自动使用有界深度，环境变量/参数可覆盖。"""
    from src.shared.media import topos_source as ts
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "auto.mov")
    _write_movie(codec, path)

    src = ToposMediaSource(path)  # 自动播放模式默认关闭同步批量，避免尾延迟尖峰
    src.open()
    try:
        assert src._ra_depth == 0 == ts._READAHEAD_MAX_DEPTH
    finally:
        src.close()

    monkeypatch.setenv("TOPOS_READAHEAD", "2")
    src = ToposMediaSource(path)
    src.open()
    try:
        assert src._ra_depth == 2  # 环境变量显式开启实验路径
    finally:
        src.close()

    monkeypatch.setenv("TOPOS_READAHEAD", "0")
    src = ToposMediaSource(path)
    src.open()
    try:
        assert src._ra_depth == 0  # 环境关闭
    finally:
        src.close()
    src = ToposMediaSource(path, read_ahead=3)
    src.open()
    try:
        assert src._ra_depth == 3  # 参数优先于环境
    finally:
        src.close()

    monkeypatch.delenv("TOPOS_READAHEAD", raising=False)
    src = ToposMediaSource(path, decode_role="export")
    src.open()
    try:
        # 测试素材很小，预算足以容纳完整的导出默认深度。
        assert src._ra_depth == ts._READAHEAD_EXPORT_DEPTH
    finally:
        src.close()


def test_readahead_budget_cap_and_unsafe_override(codec: ToposCodec, tmp_path, monkeypatch) -> None:
    """P1-03：显式深度同样受内存预算约束；超预算覆盖须 unsafe 开关。

    用 monkeypatch 把帧字节放大到 4K 422 量级（≈31.6 MiB，160 MiB 预算
    容 5 个池条目 → 显式深度上限 3），验证：
    - 默认自动播放保持关闭，不因预算自动启用同步批量；
    - 显式请求 32 被钳到 3 并记 warning；
    - TOPOS_UNSAFE_READAHEAD=1 时允许 32（仍钳硬上限）并记 warning；
    - 预算连池基线都放不下时自适应诚实关闭（depth=0）。
    """
    from src.shared.media import topos_source as ts
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "budget.mov")
    _write_movie(codec, path, n=3)

    fb_4k422 = 3840 * 2160 * 2 * 2  # ≈31.6 MiB（422 10-bit 单帧平面）
    entries_allow = ts._READAHEAD_BUDGET_BYTES // fb_4k422  # = 5

    def patched_frame_bytes(self):
        return fb_4k422

    monkeypatch.setattr(ToposMediaSource, "_frame_plane_bytes", patched_frame_bytes)

    src = ToposMediaSource(path)  # 自动播放模式保持关闭，即使预算容得下批量
    src.open()
    try:
        assert src._ra_depth == 0
        assert src.diagnostics["plane_pool_entries"] == 5  # 5×31.6 MiB ≤ 160 MiB
    finally:
        src.close()

    monkeypatch.delenv(ts._READAHEAD_UNSAFE_ENV, raising=False)
    src = ToposMediaSource(path, read_ahead=32)
    src.open()
    try:
        assert src._ra_depth == 3  # 显式请求被钳到预算容量
    finally:
        src.close()

    monkeypatch.setenv(ts._READAHEAD_UNSAFE_ENV, "1")
    src = ToposMediaSource(path, read_ahead=32)
    src.open()
    try:
        assert src._ra_depth == 32  # unsafe：突破预算（仍受硬上限）
    finally:
        src.close()

    # 预算连池基线（5 条目）都放不下 → 4K 444 量级 ≈47.5 MiB/帧
    monkeypatch.delenv(ts._READAHEAD_UNSAFE_ENV, raising=False)
    monkeypatch.setattr(ToposMediaSource, "_frame_plane_bytes", lambda self: 3840 * 2160 * 3 * 2)
    src = ToposMediaSource(path)
    src.open()
    try:
        assert src._ra_depth == 0  # 诚实关闭而非超预算预取
    finally:
        src.close()


def test_decode_path_diagnostics(codec: ToposCodec, tmp_path, monkeypatch, caplog) -> None:
    """P1-04：decoder context 创建失败回退必须留痕且功能可用。

    monkeypatch ToposDecoder 构造失败 → decode_path='legacy'、原因含
    异常、warning 可观测；read 仍经旧路径逐位可用；正常 open 时
    decode_path 为低拷贝路径。
    """
    import logging

    from src.shared.media import topos_source as ts
    from src.shared.media.topos_source import ToposMediaSource

    path = str(tmp_path / "diag.mov")
    _write_movie(codec, path, n=4)

    class _BrokenDecoder:
        def __init__(self, _codec, **_kwargs):
            raise ts.ToposCodecError("injected: context OOM")

    # 正常路径：context 系（按 dylib 能力 context-batch 或 context）
    src = ToposMediaSource(path)
    src.open()
    try:
        if src._codec._has_decoder_ctx:
            assert src.diagnostics["decode_path"] in ("context", "context-batch")
            assert src.diagnostics["decode_path_reason"] == "ok"
        else:
            assert src.diagnostics["decode_path"] == "legacy"
    finally:
        src.close()

    # 注入失败：回退 legacy + warning + 功能可用
    monkeypatch.setattr(ts, "ToposDecoder", _BrokenDecoder)
    with caplog.at_level(logging.WARNING, logger="src.shared.media.topos_source"):
        bad = ToposMediaSource(path)
        bad.open()
        try:
            d = bad.diagnostics
            assert d["decode_path"] == "legacy"
            assert "decoder_create_failed" in d["decode_path_reason"]
            assert "injected: context OOM" in d["decode_path_reason"]
            assert any("decoder context 创建失败" in r.message for r in caplog.records)
            f = bad.read_frame(0)  # 回退路径功能可用
            assert f.frame_index == 0
            assert len(f.planes) >= 3
        finally:
            bad.close()


def test_decode_worker_budget_rebalances_live_sources(codec: ToposCodec, tmp_path) -> None:
    """D3：两个活跃 source context 共享预算，不让后建 context 叠加满池。"""
    from src.shared.media.topos_source import ToposMediaSource

    if not getattr(codec, "_has_decoder_worker_budget", False):
        pytest.skip("dylib 缺少 decoder worker budget setter")

    path = str(tmp_path / "worker_budget.mov")
    _write_movie(codec, path, n=3)
    first = ToposMediaSource(path, decode_role="playback")
    second = ToposMediaSource(path, decode_role="export")
    first.open()
    try:
        before = first.diagnostics
        assert before["decode_workers_assigned"] >= 1
        second.open()
        try:
            after_first = first.diagnostics
            after_second = second.diagnostics
            budget = after_second["decode_worker_budget"]
            assert budget["active"] == 2
            assert budget["assigned_total"] <= budget["capacity"]
            assert after_first["decode_workers_assigned"] >= 1
            assert after_second["decode_workers_assigned"] >= 1
        finally:
            second.close()
        assert first.diagnostics["decode_workers_assigned"] == \
            first.diagnostics["decode_worker_budget"]["capacity"]
    finally:
        first.close()


def test_readahead_corruption_semantics(codec: ToposCodec, tmp_path) -> None:
    """读前批量下的损坏语义与单帧路径逐位一致：

    - payload 坏 slice：批量 conceal 交付、计数透传，邻帧逐位不受污染；
    - frame header 损坏：整批拒绝 → 回退单帧，坏帧按单帧语义报错，
      其后帧仍可读且与参考一致。
    """
    from src.shared.media.source import MediaDecodeError
    from src.shared.media.topos_source import ToposMediaSource

    n = 8
    clean_path = str(tmp_path / "clean.mov")
    _write_movie(codec, clean_path, n=n)
    ref_planes = None
    ref = ToposMediaSource(clean_path, read_ahead=0)
    try:
        ref.open()
        ref_planes = [
            tuple(np.asarray(p).tobytes() for p in ref.read_frame(i).planes)
            for i in range(n)
        ]
    finally:
        ref.close()

    # 1) 批内 payload 损坏（帧 3 首 slice payload）→ conceal 交付
    conceal_path = str(tmp_path / "conceal.mov")
    _write_movie(codec, conceal_path, n=n, corrupt={"frame": 3, "offset": 53 + 17 + 2, "xor": 0x40})
    single = ToposMediaSource(conceal_path, read_ahead=0)
    ra = ToposMediaSource(conceal_path, read_ahead=4)
    try:
        single.open()
        ra.open()
        expect = [
            tuple(np.asarray(p).tobytes() for p in single.read_frame(i).planes)
            for i in range(n)
        ]
        for i in range(n):
            f = ra.read_frame(i)
            got = tuple(np.asarray(p).tobytes() for p in f.planes)
            assert got == expect[i]  # 批量/缓存路径与单帧同文件逐位一致
            concealed = int(f.extra["topos_concealed_slices"])
            if i == 3:
                assert concealed >= 1  # 坏 slice → conceal，帧仍交付
            else:
                assert concealed == 0
                assert got == ref_planes[i]
    finally:
        single.close()
        ra.close()

    # 2) 批内 frame header 损坏（帧 5）→ 整批拒绝回退，坏帧单帧语义报错
    hdr_path = str(tmp_path / "hdr.mov")
    _write_movie(codec, hdr_path, n=n, corrupt={"frame": 5, "offset": 30, "xor": 0x01})
    ra = ToposMediaSource(hdr_path, read_ahead=4)
    try:
        ra.open()
        for i in range(5):  # 0 单解 + 1 批[1..4]，均完好
            f = ra.read_frame(i)
            assert tuple(np.asarray(p).tobytes() for p in f.planes) == ref_planes[i]
        with pytest.raises(MediaDecodeError):
            ra.read_frame(5)  # 批[5..7] 被拒 → 单帧回退 → 帧头损坏拒绝
        for i in (6, 7):  # 坏帧之后顺序重建（seek 已清缓存 → 单解起步）
            f = ra.read_frame(i)
            assert tuple(np.asarray(p).tobytes() for p in f.planes) == ref_planes[i]
    finally:
        ra.close()


# —— AUD-35906：损坏音轨头（channel_count=0）不得除零 ——


def test_read_audio_samples_zero_channel_count_returns_none() -> None:
    """channel_count=0 的 LPCM 音轨头 → frame_bytes=0，须按不可解码返回 None。

    历史实现直接进入 chunk 前缀索引（audio_chunk_size // frame_bytes），
    ZeroDivisionError 崩溃调用方。
    """
    from types import SimpleNamespace

    from src.shared.media.topos_source import ToposMediaSource

    zero_channel_ai = SimpleNamespace(
        has_audio=True,
        codec=topos_binding.TC_AUDIO_CODEC_LPCM,
        bits_per_sample=16,
        sample_count=1000,
        channel_count=0,
    )
    src = ToposMediaSource.__new__(ToposMediaSource)
    src._movie = SimpleNamespace(audio_info=lambda: zero_channel_ai)
    src._audio_chunk_prefix = None

    assert src.read_audio_samples(0, 100) is None
