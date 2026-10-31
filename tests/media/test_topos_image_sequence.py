"""Topos Image 阶段 6：序列解析与有界预取测试。

覆盖：
- 帧号模式解析（prefix/padding/负帧号/非序列）；
- 目录扫描（空洞/负帧号/step）与缺帧策略（skip/error/hold）；
- 有界预取：generation cancel（旧代不覆盖新帧）、缓存命中、指纹失效
  （文件被替换后重新解码）；
- 缓存字节上限（超预算单帧不缓存）。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.media_core

from src.shared.media.topos_image_sequence import (
    MissingFramePolicy,
    PlaneCache,
    SequenceOptions,
    SequencePatternError,
    frame_file_name,
    open_sequence,
    parse_frame_pattern,
    scan_sequence,
)
from src.shared.media.source import MediaSourceError
from src.shared.media.decode_queue import DecodeQueue
from src.shared.media.factory import MediaSourceFactory

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "native/topos_codec/tests/conformance/golden_image_v1_gbr10.bin"

pytest.importorskip("numpy")
_lib_ok = True
try:
    from src.shared.codec.topos_image_binding import get_image_codec
    get_image_codec()
except Exception:  # noqa: BLE001
    _lib_ok = False

needs_lib = pytest.mark.skipif(not (GOLDEN.exists() and _lib_ok),
                               reason="libtopos_codec/golden 缺失")


# ---------------- 模式解析 ----------------

class TestPatternParsing:
    def test_padding(self) -> None:
        assert parse_frame_pattern("/x/frame_0001.toos") == ("frame_", 4, ".toos")
        assert parse_frame_pattern("/x/frame_1234.toos") == ("frame_", 0, ".toos")

    def test_negative_frame(self) -> None:
        prefix, pad, suffix = parse_frame_pattern("/x/shot.-001.toos")
        assert prefix == "shot.-" or prefix == "shot."
        # 负号属于帧号：frame='-001' → prefix='shot.', padding=3
        assert pad == 3 and suffix == ".toos"

    def test_non_sequence(self) -> None:
        assert parse_frame_pattern("/x/beauty.toos") is None

    def test_roundtrip(self) -> None:
        assert frame_file_name("frame_", 4, ".toos", 7) == "frame_0007.toos"
        assert frame_file_name("frame_", 0, ".toos", 7) == "frame_7.toos"
        assert frame_file_name("shot.", 3, ".toos", -12) == "shot.-012.toos"


def _make_sequence(tmp_path: Path, frames: list[int], pad: int = 4) -> Path:
    """用 golden 字节建序列目录（同内容不同名；序列测试关注枚举/缓存语义）。

    文件名一律经 frame_file_name 生成（padding 计数字位不含符号——naive
    f"{v:0{pad}d}" 对负帧号会少一位数字，严格位宽扫描即失配）。
    """
    for f in frames:
        shutil.copy(GOLDEN, tmp_path / frame_file_name("frame_", pad, ".toos", f))
    return tmp_path


# ---------------- 扫描与缺帧 ----------------

class TestScan:
    def test_scan_sorted_with_holes(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1, 2, 4, 8])
        slots = scan_sequence(str(tmp_path), "frame_", 4, ".toos")
        assert [s.frame for s in slots] == [1, 2, 4, 8]

    def test_negative_frames(self, tmp_path: Path) -> None:
        # padding 严格作用于数字位：-0001（4 位）与 0001（4 位）同属 padding=4
        (tmp_path / "frame_-0001.toos").write_bytes(GOLDEN.read_bytes())
        (tmp_path / "frame_0001.toos").write_bytes(GOLDEN.read_bytes())
        slots = scan_sequence(str(tmp_path), "frame_", 4, ".toos")
        assert [s.frame for s in slots] == [-1, 1]

    def test_padding_strict_width(self, tmp_path: Path) -> None:
        # padding > 0：严格位宽，frame_2.toos 不混入 frame_0001 序列
        _make_sequence(tmp_path, [1], pad=4)
        (tmp_path / "frame_2.toos").write_bytes(GOLDEN.read_bytes())
        assert [s.frame for s in scan_sequence(str(tmp_path), "frame_", 4, ".toos")] == [1]
        # padding = 0：任意位宽按帧号数值匹配
        assert [s.frame for s in scan_sequence(str(tmp_path), "frame_", 0, ".toos")] == [1, 2]

    def test_step_and_range(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [0, 1, 2, 3, 4, 5])
        slots = scan_sequence(str(tmp_path), "frame_", 4, ".toos",
                              start=0, end=5, step=2)
        assert [s.frame for s in slots] == [0, 2, 4]

    def test_bad_step(self, tmp_path: Path) -> None:
        with pytest.raises(SequencePatternError):
            scan_sequence(str(tmp_path), "frame_", 4, ".toos", step=0)


# ---------------- 序列源 ----------------

@pytest.mark.skipif(not _lib_ok, reason="libtopos_codec 缺失")
class TestSequenceSource:
    def test_read_and_cache(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1, 2, 3])
        seq = open_sequence(str(tmp_path / "frame_0001.toos"))
        try:
            assert seq.frames() == [1, 2, 3]
            p1, slot = seq.read_slot(1)
            assert p1[0].dtype == np.uint16
            p1b, _ = seq.read_slot(1)
            for a, b in zip(p1, p1b):
                assert np.array_equal(a, b)  # 缓存命中逐位一致
            assert seq.metadata().total_frames == 3
            assert seq.metadata().codec_name == "topos-image-sequence"
        finally:
            seq.close()

    # ---------------- N07：read_slot 交付平面恒只读 ----------------

    def test_planes_readonly_zero_cache_budget(self, tmp_path: Path) -> None:
        """N07：容量 0（永不入缓存）——交付平面仍必须只读。

        旧缺陷：只有 PlaneCache.put 接纳帧时冻结，容量 0 直接返回可写
        平面，调用方可就地改写污染后续读取。
        """
        _make_sequence(tmp_path, [1, 2])
        opts = SequenceOptions(plane_cache_bytes=0, prefetch_depth=0)
        seq = open_sequence(str(tmp_path / "frame_0001.toos"), opts)
        try:
            planes, _ = seq.read_slot(1)
            assert planes, "至少一个平面"
            for p in planes:
                assert p is None or not p.flags.writeable
        finally:
            seq.close()

    def test_planes_readonly_frame_over_budget(self, tmp_path: Path) -> None:
        """N07：单帧超预算（PlaneCache.put 拒纳）——交付平面只读。"""
        _make_sequence(tmp_path, [1, 2])
        opts = SequenceOptions(plane_cache_bytes=64, prefetch_depth=0)
        seq = open_sequence(str(tmp_path / "frame_0001.toos"), opts)
        try:
            planes, _ = seq.read_slot(1)
            for p in planes:
                assert p is None or not p.flags.writeable
            assert seq._cache._bytes == 0  # 确认走的是"不入缓存"路径
        finally:
            seq.close()

    def test_planes_readonly_after_generation_invalidation(
            self, tmp_path: Path) -> None:
        """N07：close()/prefetch 代际失效后的交付——平面只读且不落缓存。"""
        _make_sequence(tmp_path, [1, 2])
        seq = open_sequence(str(tmp_path / "frame_0001.toos"),
                            SequenceOptions(prefetch_depth=0))
        try:
            planes, _ = seq.read_slot(1)          # 正常路径（可入缓存）
            for p in planes:
                assert p is None or not p.flags.writeable
            seq._generation += 1                   # 模拟代际翻转（close/seek）
            planes2, _ = seq._read_slot(2, gen=0)  # 旧代结果不得落缓存
            for p in planes2:
                assert p is None or not p.flags.writeable
            assert not seq._cache._map or all(
                key[0] != 2 for key in seq._cache._map)
        finally:
            seq.close()

    def test_media_source_and_decode_queue_integration(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [10, 11, 12])
        source = MediaSourceFactory.create(str(tmp_path / "frame_0010.toos"))
        assert source.__class__.__name__ == "ToposImageSequenceSource"
        queue = DecodeQueue(source, window_size=1, max_queue_size=4)
        try:
            MediaSourceFactory.open_source(source)
            metadata = source.probe()
            assert metadata.total_frames == 3
            frame = source.read_frame(2)
            assert frame.frame_index == 2
            assert frame.extra["sequence_frame"] == 12

            queue.start()
            queue.request_frame(1, is_master=True)
            received = None
            for _ in range(30):
                received = queue.get_frame(frame_index=1, timeout=0.1)
                if received is not None:
                    break
            assert received is not None
            assert received.extra["sequence_frame"] == 11
        finally:
            queue.stop()
            source.close()

    def test_missing_frame_policy_skip_default(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1, 3])  # 2 缺失
        seq = open_sequence(str(tmp_path / "frame_0001.toos"))
        try:
            planes, slot = seq.read_slot(2)  # 默认 skip：跳到后继有效帧
            assert slot.frame == 3
            with pytest.raises(FileNotFoundError):
                seq.read_slot(4)  # 无后继：不伪造成功
        finally:
            seq.close()

    def test_missing_frame_policy_error_open_detects_hole(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1, 3])  # 2 缺失
        opts = SequenceOptions(missing=MissingFramePolicy(mode="error"))
        with pytest.raises(SequencePatternError, match="空洞"):
            open_sequence(str(tmp_path / "frame_0001.toos"), opts)

    def test_missing_frame_policy_error_read_out_of_range(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1, 2])  # 无空洞
        seq = open_sequence(str(tmp_path / "frame_0001.toos"),
                            SequenceOptions(missing=MissingFramePolicy(mode="error")))
        try:
            with pytest.raises(FileNotFoundError):
                seq.read_slot(5)  # 区间外缺帧同样报错
        finally:
            seq.close()

    def test_duplicate_frame_rejected(self, tmp_path: Path) -> None:
        # padding=0 时 frame_1 与 frame_01 解析为同一帧号：显式拒绝（不静默去重）
        (tmp_path / "frame_1.toos").write_bytes(GOLDEN.read_bytes())
        (tmp_path / "frame_01.toos").write_bytes(GOLDEN.read_bytes())
        with pytest.raises(SequencePatternError, match="重复帧号"):
            open_sequence(str(tmp_path / "frame_1.toos"))

    def test_missing_frame_policy_hold(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1, 3])
        seq = open_sequence(str(tmp_path / "frame_0001.toos"),
                            SequenceOptions(missing=MissingFramePolicy(mode="hold")))
        try:
            planes, slot = seq.read_slot(2)
            assert slot.frame == 1  # hold：保留前一帧，且不伪造帧号
        finally:
            seq.close()

    def test_fingerprint_invalidate_on_replace(self, tmp_path: Path) -> None:
        target = tmp_path / "frame_0001.toos"
        shutil.copy(GOLDEN, target)
        seq = open_sequence(str(target))
        try:
            first, _ = seq.read_slot(1)
            # 替换为内容不同的文件（gbr12：位深/尺寸/内容均不同）
            other = REPO / "native/topos_codec/tests/conformance/golden_image_v1_gbr12.bin"
            time.sleep(0.01)
            shutil.copy(other, target)
            second, _ = seq.read_slot(1)
            # 指纹失效 → 重新解码（不返回旧缓存）：形状或数值必不同
            shapes_differ = len(first) != len(second) or any(
                a.shape != b.shape for a, b in zip(first, second))
            values_differ = not shapes_differ and any(
                not np.array_equal(a, b) for a, b in zip(first, second))
            assert shapes_differ or values_differ
        finally:
            seq.close()


# ---------------- 预取与缓存边界 ----------------

class TestPrefetchAndCache:
    def test_1000_frame_sequential_random_reverse_pressure_is_bounded(
        self, tmp_path: Path,
    ) -> None:
        """长序列门禁：访问顺序变化不扩大 cache，也不残留预取线程。"""
        _make_sequence(tmp_path, list(range(1000)))
        opts = SequenceOptions(
            prefetch_depth=4,
            # A 64x48 GBR10 frame is ~18 KiB; this deliberately permits only
            # one decoded frame so the test observes hard byte accounting.
            plane_cache_bytes=20_000,
        )
        seq = open_sequence(str(tmp_path / "frame_0000.toos"), opts)
        try:
            for frame in range(1000):
                planes, slot = seq.read_slot(frame)
                assert slot.frame == frame
                assert planes[0].dtype == np.uint16
            for frame in (999, 17, 503, 2, 777, 0):
                _, slot = seq.read_slot(frame)
                assert slot.frame == frame
            seq.prefetch_around(500, direction=-1)
            if seq._prefetch_thread is not None:
                seq._prefetch_thread.join(timeout=30)
            assert seq._cache._bytes <= opts.plane_cache_bytes
            assert len(seq._cache._map) <= 1
            assert seq._prefetch_thread is None or not seq._prefetch_thread.is_alive()
        finally:
            seq.close()

    def test_prefetch_generation_cancel(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, list(range(1, 9)))
        seq = open_sequence(str(tmp_path / "frame_0001.toos"))
        try:
            seq.prefetch_around(1, direction=+1)
            seq.prefetch_around(8, direction=-1)  # 新代取消旧代
            seq.close()  # close 也 +1 代并等待线程收敛
            # 线程有界：至多 1 个预取线程
            assert seq._prefetch_thread is None or not seq._prefetch_thread.is_alive()
        finally:
            if seq._prefetch_thread is not None and seq._prefetch_thread.is_alive():
                seq._prefetch_thread.join(timeout=5)

    def test_prefetch_depth_bounded(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, list(range(1, 33)))
        opts = SequenceOptions(prefetch_depth=4)
        seq = open_sequence(str(tmp_path / "frame_0001.toos"), opts)
        try:
            seq.prefetch_around(1)
            seq._prefetch_thread.join(timeout=30)
            assert len(seq._cache._map) <= 4  # 窗口有界
        finally:
            seq.close()

    def test_plane_cache_byte_budget(self) -> None:
        cache = PlaneCache(max_bytes=1024)
        small = [np.zeros((8, 8), dtype=np.uint16)]  # 128B
        big = [np.zeros((1024, 1024), dtype=np.uint16)]  # 2MiB > 上限
        cache.put(("a",), small)
        cache.put(("b",), big)  # 超预算单帧：拒绝缓存（不无界增长）
        assert cache.get(("a",)) is not None
        assert cache.get(("b",)) is None
        cache.clear()
        assert cache.get(("a",)) is None

    def test_plane_cache_duplicate_put_accounting(self) -> None:
        """同 key 重复 put（前台与预取并发解码同一帧）不得重复记账。

        回归：旧实现覆盖时不扣除旧值，_bytes 单向虚高 → 缓存被过早淘汰。
        """
        cache = PlaneCache(max_bytes=1 << 20)
        planes = [np.zeros((64, 64), dtype=np.uint16) for _ in range(3)]
        nbytes = sum(p.nbytes for p in planes)
        cache.put(("k",), planes)
        cache.put(("k",), planes)  # 并发双写的现实路径
        cached = cache._map[("k",)]
        actual = sum(p.nbytes for p in cached)
        assert cache._bytes == actual == nbytes

    def test_prefetch_pending_not_dropped(self, tmp_path: Path) -> None:
        """worker 在飞时的新预取窗口必须被自取，不允许静默丢弃。

        回归：旧实现 worker 按出生代号退出，pending 自取分支不可达——
        scrub 场景下约一半预取请求被丢弃，必须等 worker 退出重试才生效。
        """
        import threading

        _make_sequence(tmp_path, list(range(0, 40)))
        opts = SequenceOptions(prefetch_depth=4)
        seq = open_sequence(str(tmp_path / "frame_0000.toos"), opts)
        try:
            decoded: list[int] = []
            real_read = seq._read_slot

            def slow_read(frame: int, gen: int):
                decoded.append(frame)
                time.sleep(0.02)  # 保证第二请求到达时 worker 仍在飞
                return real_read(frame, gen)

            seq._read_slot = slow_read  # type: ignore[method-assign]
            seq.prefetch_around(0, direction=+1)   # 窗口 [1..4]，worker 在飞
            time.sleep(0.005)
            seq.prefetch_around(20, direction=+1)  # 新窗口 [21..24]：应被自取
            # 等待 worker 收敛（自取后处理完新窗口退出）
            deadline = time.time() + 30
            while time.time() < deadline:
                t = seq._prefetch_thread
                if t is None or not t.is_alive():
                    break
                time.sleep(0.02)
            t = seq._prefetch_thread
            if t is not None:
                t.join(timeout=5)
            got = {f for f in decoded if 21 <= f <= 24}
            assert got, (
                f"新预取窗口 [21..24] 被静默丢弃（实际解码: {decoded}）")
            # 且不叠加线程：全程只有一个预取线程对象
            assert seq._prefetch_thread is not None
        finally:
            seq.close()

    def test_step_anchor_semantics(self, tmp_path: Path) -> None:
        """step 锚点（冻结语义）：anchor = start（显式）或 0（start=None）。

        过滤结果不得依赖"目录里恰好有哪些文件"——start=None 时锚定绝对
        帧号 0，负帧号序列同样确定。
        """
        # 负帧号文件名经 frame_file_name 生成（padding 计数字位不含符号；
        # naive f"{v:04d}" 会少一位数字导致严格位宽不匹配）
        for f in (-2, -1, 0, 1, 2, 3, 4):
            name = frame_file_name("frame_", 4, ".toos", f)
            (tmp_path / name).write_bytes(GOLDEN.read_bytes())
        # start=None：锚定 0 → 保留 -2,0,2,4（-1/1/3 被排除：(±1-0)%2=1）
        slots = scan_sequence(str(tmp_path), "frame_", 4, ".toos", step=2)
        assert [s.frame for s in slots] == [-2, 0, 2, 4]
        # 显式 start：锚定 start → 保留 start, start+2, …
        slots = scan_sequence(str(tmp_path), "frame_", 4, ".toos",
                              start=1, step=2)
        assert [s.frame for s in slots] == [1, 3]


# ---------------- C07（2026-09-27）：混合格式序列明确拒绝 ----------------

def _encode_variant(directory: Path, frame_no: int, height: int | None = None,
                    primaries: int | None = None) -> Path:
    """以 golden 的格式域重编一帧（可变尺寸/色彩标签），包成 .toos。"""
    import ctypes

    from src.shared.codec.topos_binding import (
        TOPOS_CODEC_ABI_VERSION,
        ToposCodec,
        _CFrameConfig,
    )
    codec = get_image_codec()
    planes, info, _ = codec.decode(str(GOLDEN))
    h = int(info.visible_height) if height is None else height
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(fc)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = int(info.visible_width)
    fc.visible_height = h
    fc.profile = {0: 3, 1: 5, 2: 6}.get(int(info.image_profile), 5)
    fc.pixel_format = int(info.pixel_format)
    fc.bit_depth = int(info.bit_depth)
    fc.qp_base = 58
    fc.color_range = int(info.color_range)
    fc.color_primaries = (int(info.color_primaries) if primaries is None
                          else primaries)
    fc.color_transfer = int(info.color_transfer)
    fc.color_matrix = int(info.color_matrix)
    tc = ToposCodec()
    pkt, _st = tc.encode_frame(
        fc, [np.ascontiguousarray(p[:h]) for p in planes])
    out = directory / frame_file_name("frame_", 4, ".toos", frame_no)
    codec.write_packet(str(out), pkt, int(info.image_profile), durable=False)
    return out


@needs_lib
class TestMixedFormatRejected:
    """C07：几何/像素/样本域/位深/alpha/色彩标签不一致 → 明确拒绝。"""

    def test_mixed_geometry_rejected(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1])
        _encode_variant(tmp_path, 2, height=32)
        seq = open_sequence(str(tmp_path / "frame_0001.toos"))
        try:
            with pytest.raises(MediaSourceError, match="尺寸"):
                seq.read_frame(1)
        finally:
            seq.close()

    def test_mixed_color_tags_rejected(self, tmp_path: Path) -> None:
        _make_sequence(tmp_path, [1])
        _encode_variant(tmp_path, 2, primaries=6)
        seq = open_sequence(str(tmp_path / "frame_0001.toos"))
        try:
            with pytest.raises(MediaSourceError, match="color_primaries"):
                seq.read_frame(1)
        finally:
            seq.close()

    def test_same_format_reencoded_frame_passes(self, tmp_path: Path) -> None:
        """控制组：同格式重编帧（逐字段同锚）不误伤。"""
        _make_sequence(tmp_path, [1])
        _encode_variant(tmp_path, 2)
        seq = open_sequence(str(tmp_path / "frame_0001.toos"))
        try:
            frame = seq.read_frame(1)
            assert frame is not None
        finally:
            seq.close()
