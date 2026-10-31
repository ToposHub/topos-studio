"""批 A（Critical）媒体层修复回归测试。

覆盖四个审查发现的修复（全部使用 fake 帧/会话构造分支逻辑，不做真实解码）：

- AUD-34703: planar proxy 回退不得落到 packed RGB 后再按平面切片
- AUD-34900: 硬解顺序读必须消费 seek 落地帧并回同步会话
- AUD-35205: ``_native_pointer`` 不得以 hash() 伪造原生指针
- AUD-35900: alpha 移位不得原地替换池条目的 ``entry.arrays[3]``（UAF）
"""

from __future__ import annotations

import ctypes
import sys
import types
from typing import Any, Optional

import numpy as np
import pytest

from src.shared.media.source import MediaDecodeError
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational, VideoMetadata

PACKED_PREFIXES = ("rgb", "bgr", "argb", "abgr")


# ============================================================================
# AUD-34703: ffmpeg_source planar 回退路径
# ============================================================================


class _FakeFormat:
    """PyAV VideoFormat 近似。"""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakePlane(bytes):
    """PyAV VideoPlane 近似：bytes 载荷 + line_size/height/width 属性。"""

    def __new__(
        cls, data: bytes, line_size: int, height: int, width: int
    ) -> "_FakePlane":
        obj = super().__new__(cls, data)
        obj.line_size = line_size
        obj.height = height
        obj.width = width
        return obj


class _FakeFrame:
    """PyAV VideoFrame 近似；reformat 按 allow/deny 集合模拟 swscale。"""

    def __init__(
        self,
        name: str,
        width: int,
        height: int,
        planes: list[_FakePlane],
        *,
        deny: Optional[set[str]] = None,
        pts: int = 7,
    ) -> None:
        self.format = _FakeFormat(name)
        self.width = width
        self.height = height
        self.planes = planes
        self.pts = pts
        self._deny = deny or set()
        self.reformat_calls: list[str] = []

    def reformat(
        self,
        width: Optional[int] = None,
        height: Optional[int] = None,
        format: Optional[str] = None,
    ) -> "_FakeFrame":
        fmt = str(format)
        self.reformat_calls.append(fmt)
        if fmt in self._deny:
            raise RuntimeError(f"fake swscale cannot convert to {fmt}")
        return build_planar_frame(fmt, int(width), int(height))


def _itemsize_for(fmt: str) -> int:
    return 2 if any(tag in fmt for tag in ("10", "12", "14", "16")) else 1


def _plane_geometry(fmt: str, w: int, h: int) -> list[tuple[int, int]]:
    """按 ffmpeg 命名约定给出各平面 (width, height)。"""
    full = fmt.startswith("yuva")
    if "444" in fmt:
        geoms = [(w, h)] * 3
    elif "420" in fmt:
        geoms = [(w, h), ((w + 1) // 2, (h + 1) // 2), ((w + 1) // 2, (h + 1) // 2)]
    else:  # 422
        geoms = [(w, h), ((w + 1) // 2, h), ((w + 1) // 2, h)]
    if full:
        geoms.append((w, h))
    return geoms


def build_planar_frame(fmt: str, w: int, h: int) -> _FakeFrame:
    itemsize = _itemsize_for(fmt)
    planes = []
    for pw, ph in _plane_geometry(fmt, w, h):
        line_size = pw * itemsize
        planes.append(_FakePlane(bytes(line_size * ph), line_size, ph, pw))
    return _FakeFrame(fmt, w, h, planes)


def _make_ffmpeg_source(
    pixel_format: str, target_size: Optional[tuple[int, int]]
) -> Any:
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    source = FFmpegMediaSource("/fake/path.mov", target_size=target_size)
    source._metadata = VideoMetadata(
        width=8,
        height=6,
        fps=Rational(24, 1),
        total_frames=100,
        codec_name="prores",
        codec_profile=None,
        pixel_format=pixel_format,
        bit_depth=12,
        has_alpha=True,
    )
    source._video_stream = None
    return source


def test_aud34703_unmapped_format_falls_back_to_planar_not_packed() -> None:
    """未映射格式（ProRes 4444 XQ 的 yuva444p12le）回退必须落在 planar 格式。"""
    source = _make_ffmpeg_source("yuva444p12le", target_size=(4, 2))
    frame = build_planar_frame("yuva444p12le", 8, 6)

    decoded = source._convert_frame_planar(frame)

    # 布局标志与实际切片一致：绝不出现 packed rgb48le/rgba64le
    assert not decoded.pixel_format.startswith(PACKED_PREFIXES)
    assert decoded.pixel_format == "yuva444p"
    assert decoded.is_planar
    # 4 个全宽平面（8-bit yuva444p），而非单平面 1/3 截断数据
    assert len(decoded.planes) == 4
    assert decoded.planes[0].shape == (2, 4)
    assert decoded.planes[1].shape == (2, 4)
    assert decoded.planes[3].shape == (2, 4)
    assert decoded.planes[0].dtype == np.uint8
    assert decoded.extra is not None
    assert decoded.extra["proxy_reformat_fallback"] is True
    # 回退只请求过中间 planar 格式，从未请求 packed 终点格式
    assert "rgb48le" not in frame.reformat_calls
    assert "rgba64le" not in frame.reformat_calls


def test_aud34703_reformat_failure_uses_planar_chain_candidates() -> None:
    """映射格式的 planar reformat 失败时，逐候选尝试 planar 中间格式。"""
    source = _make_ffmpeg_source("yuv420p10le", target_size=(4, 2))
    frame = build_planar_frame("yuv420p10le", 8, 6)
    # 主目标与第一个候选（8-bit）都失败 → 命中 16-bit planar 候选
    frame._deny = {"yuv420p10le", "yuv420p"}

    decoded = source._convert_frame_planar(frame)

    assert decoded.pixel_format == "yuv420p16le"
    assert len(decoded.planes) == 3
    assert decoded.planes[0].shape == (2, 4)
    assert decoded.planes[0].dtype == np.uint16
    assert decoded.planes[1].shape == (1, 2)  # 420 色度半几何
    assert decoded.extra is not None
    assert decoded.extra["proxy_reformat_fallback"] is True
    assert "rgb48le" not in frame.reformat_calls


def test_aud34703_all_fallbacks_fail_preserves_source_planar_layout() -> None:
    """全部 planar 候选失败：保持源 planar 帧（放弃缩放），不得切 packed。"""
    source = _make_ffmpeg_source("yuv420p10le", target_size=(4, 2))
    frame = build_planar_frame("yuv420p10le", 8, 6)
    frame._deny = {
        "yuv420p10le", "yuv420p", "yuv420p16le", "yuv444p", "yuva444p",
    }

    decoded = source._convert_frame_planar(frame)

    assert decoded.pixel_format == "yuv420p10le"
    assert len(decoded.planes) == 3
    assert decoded.planes[0].shape == (6, 8)  # 未缩放的源几何
    assert decoded.planes[1].shape == (3, 4)


def test_aud34703_packed_frame_raises_instead_of_planar_slicing() -> None:
    """布局防御：planar 合约下 packed 帧必须显式失败而非错乱切片。"""
    source = _make_ffmpeg_source("rgb48le", target_size=None)
    packed = _FakeFrame("rgb48le", 8, 6, [_FakePlane(bytes(48 * 6), 48, 6, 8)])

    with pytest.raises(MediaDecodeError, match="Planar output not available"):
        source._convert_frame_planar(packed)


def test_aud34703_planar_fallback_helper_candidates_are_all_planar() -> None:
    """_reformat_to_planar_fallback 只会返回回退链的中间 planar 格式。"""
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    for source_fmt in (
        "yuva444p12le",
        "yuv420p16le",
        "yuv422p16be",
        "yuv444p12le",
    ):
        source = _make_ffmpeg_source(source_fmt, target_size=(4, 2))
        frame = build_planar_frame(source_fmt, 8, 6)
        converted, fmt = source._reformat_to_planar_fallback(
            frame, source_fmt, 4, 2
        )
        chain = FFmpegMediaSource._get_reformat_fallback_chain(
            source_fmt, "rgb48le"
        )
        assert fmt in {inter for inter, _ in chain}
        assert not fmt.startswith(PACKED_PREFIXES)
        assert converted is not frame
        assert converted.format.name == fmt


# ============================================================================
# AUD-34900: hwaccel 顺序读的 seek 落地契约
# ============================================================================


def _make_hwaccel_source() -> Any:
    from src.shared.media.hwaccel import FFmpegHwaccelSource, HardwareDecodeConfig

    source = FFmpegHwaccelSource("/fake/path.mov", HardwareDecodeConfig())
    source._is_open = True
    source._using_hwaccel = True
    source._container = types.SimpleNamespace()
    source._video_stream = types.SimpleNamespace()
    return source


def _stub_decoded(frame_index: int) -> DecodedFrame:
    return DecodedFrame(
        frame_index=frame_index,
        pts=0,
        time_seconds=0.0,
        planes=(),
        pixel_format="rgb24",
        dtype=np.dtype(np.uint8),
        width=4,
        height=2,
    )


def _install_converter(source: Any) -> list[Any]:
    """替身 _convert_hwaccel_frame：记录被转换的原始帧。"""
    converted: list[Any] = []

    def convert(frame: Any) -> DecodedFrame:
        converted.append(frame)
        return _stub_decoded(source._current_frame)

    source._convert_hwaccel_frame = convert  # type: ignore[method-assign]
    return converted


def test_aud34900_pending_seek_frame_is_consumed_first() -> None:
    """seek 落地帧（_pending_av_frame）必须被下一次顺序读消费，而非跳过。"""
    source = _make_hwaccel_source()
    converted = _install_converter(source)
    pending = types.SimpleNamespace(role="pending")
    main_frame = types.SimpleNamespace(role="main")
    source._pending_av_frame = pending
    source._current_frame = 7
    source._frame_iterator = iter([main_frame])

    result = source.read_next_frame()

    # 旧缺陷：返回的是 main_frame（错位 N+1 帧）且 pending 永久滞留
    assert converted == [pending]
    assert result.frame_index == 7
    assert source._pending_av_frame is None
    assert source._current_frame == 8
    # 主迭代器未被消费（下一帧仍在迭代器里）
    assert next(source._frame_iterator) is main_frame


def test_aud34900_seek_session_is_read_before_main_iterator() -> None:
    """SLICE seek 会话续读优先于主会话迭代器重建。"""
    source = _make_hwaccel_source()
    converted = _install_converter(source)
    session_frame = types.SimpleNamespace(role="session")

    events: list[str] = []

    def pull() -> Optional[Any]:
        events.append("pull")
        return session_frame

    source._pull_next_frame_from_seek_session = pull  # type: ignore[method-assign]
    source._current_frame = 7

    result = source.read_next_frame()

    assert converted == [session_frame]
    assert result.frame_index == 7
    assert source._current_frame == 8
    assert events == ["pull"]
    assert source._frame_iterator is None  # 未建主迭代器


def test_aud34900_main_iterator_resyncs_main_session_first() -> None:
    """建主迭代器前必须回同步主会话（_main_resync_pts），且顺序在 decode 之前。"""
    source = _make_hwaccel_source()
    converted = _install_converter(source)
    main_frame = types.SimpleNamespace(role="main")
    events: list[str] = []

    source._pull_next_frame_from_seek_session = (  # type: ignore[method-assign]
        lambda: events.append("pull") or None
    )

    def resync() -> None:
        events.append("resync")

    source._resync_main_session_if_needed = resync  # type: ignore[method-assign]

    def decode(stream: Any) -> Any:
        events.append("decode")
        return iter([main_frame])

    source._container = types.SimpleNamespace(decode=decode)
    source._main_resync_pts = 1234
    source._current_frame = 7

    result = source.read_next_frame()

    assert events == ["pull", "resync", "decode"]
    assert converted == [main_frame]
    assert result.frame_index == 7
    assert source._current_frame == 8


def test_aud34900_cpu_branch_still_delegates_to_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_using_hwaccel=False 时仍走父类路径（回归保护）。"""
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    source = _make_hwaccel_source()
    source._using_hwaccel = False

    def spy(self: Any) -> DecodedFrame:
        return _stub_decoded(42)

    monkeypatch.setattr(FFmpegMediaSource, "read_next_frame", spy)
    result = source.read_next_frame()
    assert result.frame_index == 42


# ============================================================================
# AUD-35205: vt_compression._native_pointer 不得伪造指针
# ============================================================================


@pytest.mark.skipif(sys.platform != "darwin", reason="VideoToolbox 仅 macOS")
class TestAud35205NativePointer:
    def _import(self) -> Any:
        from src.shared.media.macos_vt.vt_compression import (
            VideoToolboxCompressionError,
            _native_pointer,
        )

        return _native_pointer, VideoToolboxCompressionError

    def test_int_and_void_p_accepted(self) -> None:
        _native_pointer, _Error = self._import()
        assert _native_pointer(0x1000).value == 0x1000
        assert _native_pointer(ctypes.c_void_p(0x2000)).value == 0x2000

    def test_none_rejected(self) -> None:
        _native_pointer, Error = self._import()
        with pytest.raises(Error, match="handle_missing"):
            _native_pointer(None)

    def test_negative_pointer_rejected(self) -> None:
        """负整数经 c_void_p 会回绕成非法地址，必须按无效指针拒绝。"""
        _native_pointer, Error = self._import()
        with pytest.raises(Error, match="handle_null"):
            _native_pointer(-5)

    def test_non_pointer_like_raises_not_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 PyObjC 可用且 handle 非 int/c_void_p 时必须 fail-loud。

        旧缺陷：此处以 hash(value) 兜底伪造指针送入 VideoToolbox（进程崩溃）。
        """

        def _raise(value: Any) -> int:
            raise RuntimeError("pyobjc unavailable")

        _native_pointer, Error = self._import()
        monkeypatch.setitem(
            sys.modules, "objc", types.SimpleNamespace(pyobjc_id=_raise)
        )
        sentinel = object()
        with pytest.raises(Error, match="not_pointer_like"):
            _native_pointer(sentinel)
        # 不产生任何指针值：错误即错误，结果绝不来自 hash()
        with pytest.raises(Error):
            _native_pointer([1, 2, 3])

    def test_pyobjc_path_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _native_pointer, _Error = self._import()
        monkeypatch.setitem(
            sys.modules, "objc", types.SimpleNamespace(pyobjc_id=lambda v: 555)
        )
        assert _native_pointer(object()).value == 555


# ============================================================================
# AUD-35900: topos_source alpha 移位的池条目 UAF
# ============================================================================


def _make_topos_source() -> Any:
    from src.shared.codec.topos_binding import ToposMovieSummary
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource("/fake/topos.mov")
    source._summary = ToposMovieSummary(
        width=8,
        height=4,
        profile=0,
        pixel_format=0,
        bit_depth=10,
        qmatrix_id=0,
        qp_base=0,
        qp_delta_luma=0,
        qp_delta_chroma=0,
        alpha_mode=1,
        alpha_bit_depth=10,
        alpha_premultiplied=False,
        color_range=1,
        color_primaries=1,
        color_transfer=1,
        color_matrix=1,
        chroma_siting=0,
        sar_num=1,
        sar_den=1,
        timescale=24,
        sample_count=10,
        faststart=False,
        index_bytes=0,
        bitstream_major=7,
        tier_id=0,
    )
    source._metadata = VideoMetadata(
        width=8,
        height=4,
        fps=Rational(24, 1),
        total_frames=10,
        codec_name="topos",
        codec_profile=None,
        pixel_format="topos_yuva422p10a10",
        bit_depth=10,
        has_alpha=True,
    )
    source._color_semantics = None
    source._alpha_budget = None
    source._trwm = None
    return source


def _topos_frame_info() -> Any:
    from src.shared.codec.topos_binding import ToposFrameInfo

    return ToposFrameInfo(
        visible_width=8,
        visible_height=4,
        plane_count=4,
        bit_depth=10,
        profile=0,
        pixel_format=0,
        alpha_mode=1,
        concealed_slices=0,
        slice_count=1,
        slice_status=(),
    )


class _FakeViews:
    """记录平面裸地址的 fake decode views（模拟 topos_binding.make_views）。"""

    def __init__(self, arrays: list[np.ndarray]) -> None:
        self.pixels = [int(arr.ctypes.data) for arr in arrays]


def _build_entry() -> Any:
    from src.shared.media.topos_source import _ToposPlaneEntry, _plane_geoms

    geoms = _plane_geoms(0, 4, 8, 4)  # Y 8x4, U/V 4x4, A 8x4
    arrays: list[np.ndarray] = []
    for w, h in geoms:
        arr = np.zeros((h, w), dtype="<u2")
        arr.flags.writeable = True
        arrays.append(arr)
    # 满刻度 alpha（16-bit 容器；a10 移位 6 后 = 1023）
    arrays[3].fill(65535)
    for arr in arrays:
        arr.flags.writeable = False
    return _ToposPlaneEntry((8, 4, 4, 0, 1), arrays, _FakeViews(arrays))


def test_aud35900_assemble_does_not_mutate_caller_planes_list() -> None:
    """根因断言：装配尾部不得原地改写调用方的 planes 列表。"""
    source = _make_topos_source()
    info = _topos_frame_info()
    entry = _build_entry()
    alpha_raw = entry.arrays[3]
    originals = list(entry.arrays)

    # 最坏情形：调用方直接传 entry.arrays（read_next_frame 旧代码的用法）
    frame = source._assemble_decoded_frame(0, info, entry.arrays, 5, pool_entry=entry)

    # 列表未被改写：原始 alpha 平面仍被 entry.arrays 持有
    assert entry.arrays[3] is alpha_raw
    assert all(
        a is b for a, b in zip(entry.arrays, originals)
    ) and len(entry.arrays) == len(originals)
    # 交付的是移位后的 scratch，数值与只读契约正确
    assert frame.planes[3] is entry.alpha_scratch
    assert frame.planes[3] is not alpha_raw
    assert int(frame.planes[3].max()) == 1023  # 65535 >> 6
    assert not frame.planes[3].flags.writeable
    assert frame.plane_infos is not None
    assert frame.plane_infos[3].bit_depth == 10
    # Y/U/V 平面保持池内原始缓冲
    assert frame.planes[0] is originals[0]


def test_aud35900_pool_reuse_keeps_views_buffer_alive() -> None:
    """UAF 机制断言：views 绑定的 alpha 裸地址在装配/释放/复用周期内始终有效。"""
    from src.shared.media.topos_source import _entry_free

    source = _make_topos_source()
    info = _topos_frame_info()
    entry = _build_entry()
    view_address = entry.views.pixels[3]
    assert view_address == entry.arrays[3].ctypes.data

    frame = source._assemble_decoded_frame(0, info, entry.arrays, 5, pool_entry=entry)

    # 消费方持有移位帧期间，条目不可回收（scratch 参与判定）
    assert not _entry_free(entry)
    # 原始 alpha 缓冲仍存活且地址不变——views[3] 不是悬垂指针
    assert entry.views.pixels[3] == view_address
    assert entry.arrays[3].ctypes.data == view_address

    del frame
    # 释放后条目空闲：可安全交给下一次原生解码复用
    assert _entry_free(entry)

    # 复用同一 entry 再装配一帧：scratch 复用、原始缓冲地址仍稳定
    frame2 = source._assemble_decoded_frame(1, info, entry.arrays, 6, pool_entry=entry)
    assert frame2.planes[3] is entry.alpha_scratch
    assert entry.views.pixels[3] == view_address
    assert entry.arrays[3].ctypes.data == view_address


def test_aud35900_legacy_path_without_pool_does_not_mutate() -> None:
    """无池（legacy）路径：表达式移位同样不得原地替换调用方列表。"""
    source = _make_topos_source()
    info = _topos_frame_info()
    entry = _build_entry()
    planes = list(entry.arrays)
    alpha_raw = planes[3]

    frame = source._assemble_decoded_frame(0, info, planes, 5)

    assert planes[3] is alpha_raw  # 调用方列表未被改写
    assert frame.planes[3] is not alpha_raw  # 交付移位副本
    assert int(frame.planes[3].max()) == 1023
