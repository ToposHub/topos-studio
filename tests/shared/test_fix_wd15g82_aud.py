"""wd15-g82：计划表批 D（shared/codec 域 Major）8 条条目的根因修复测试。

覆盖：AUD-32101 / AUD-32104 / AUD-32108 / AUD-32201 / AUD-32202 /
AUD-32203 / AUD-32204 / AUD-32307（docs/audit/2026-09-28/batches/
batch-321/322/323-shared-codec.md）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading
from types import SimpleNamespace

import pytest

from src.shared.codec import topos_binding
from src.shared.codec.color_metadata_config import ColorMetadataConfig
from src.shared.codec.decode_worker_budget import DecodeWorkerBudget
from src.shared.codec.slice_threads_policy import SliceThreadsPolicy
from src.shared.codec.topos_binding import ToposCodec, ToposCodecError, ToposMuxFile
from src.shared.codec.topos_meta import (
    ToposMetaError,
    decode_topos_meta,
    encode_topos_meta,
)


# ---------------------------------------------------------------------------
# AUD-32101：__post_init__ 警告与行为一致 + from_metadata 边界映射
# ---------------------------------------------------------------------------

def test_aud_32101_invalid_value_kept_with_honest_warning(caplog) -> None:
    """直接构造：无效值原样保留（不再宣称"using 'bt709'"的假回退）。"""
    with caplog.at_level(logging.WARNING, logger="src.shared.codec.color_metadata_config"):
        cfg = ColorMetadataConfig(
            primaries="typo-primaries", transfer="typo-transfer",
            matrix="typo-matrix", range="typo-range",
        )
    # 无值被改写（诚实告警：no fallback applied）
    assert cfg.primaries == "typo-primaries"
    assert cfg.transfer == "typo-transfer"
    assert cfg.matrix == "typo-matrix"
    assert cfg.range == "typo-range"
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "no 'bt709' fallback applied" in joined
    assert "using 'bt709'" not in joined  # 旧的谎言文案不复存在


def test_aud_32101_valid_values_no_warning(caplog) -> None:
    """合法值（含 'unknown' 缺失标记）零告警。"""
    with caplog.at_level(logging.WARNING, logger="src.shared.codec.color_metadata_config"):
        cfg = ColorMetadataConfig(primaries="unknown", transfer="smpte2084",
                                  matrix="bt2020nc", range="limited")
    assert cfg.primaries == "unknown"
    assert not caplog.records


def test_aud_32101_from_metadata_normalizes_out_of_list(caplog) -> None:
    """外部数据边界验证：ffmpeg 侧名单外字符串 → 'unknown'（带告警）。"""
    meta = SimpleNamespace(
        codec_name="h264", color_primaries="bt2020-weird-variant",
        color_transfer="smpte2084", color_matrix="bt2020nc",
        color_range="limited",
    )
    with caplog.at_level(logging.WARNING, logger="src.shared.codec.color_metadata_config"):
        cfg = ColorMetadataConfig.from_metadata(meta)
    assert cfg.primaries == "unknown"  # 名单外不原样下传
    assert cfg.transfer == "smpte2084"
    assert cfg.matrix == "bt2020nc"
    assert cfg.range == "limited"
    assert any("primaries" in rec.getMessage() for rec in caplog.records)


def test_aud_32101_from_metadata_passthrough_and_none() -> None:
    """合法值直通、None/空串 → 'unknown'（与旧 `or "unknown"` 语义一致）。"""
    meta = SimpleNamespace(
        codec_name="h264", color_primaries="bt2020", color_transfer=None,
        color_matrix="", color_range="full",
    )
    cfg = ColorMetadataConfig.from_metadata(meta)
    assert cfg.primaries == "bt2020"
    assert cfg.transfer == "unknown"
    assert cfg.matrix == "unknown"
    assert cfg.range == "full"


# ---------------------------------------------------------------------------
# AUD-32104：_apply_updates 世代化——后到的过期快照必须被丢弃
# ---------------------------------------------------------------------------

def test_aud_32104_stale_snapshot_dropped() -> None:
    """模拟 T1 的旧快照后到：世代落后 → 不应用（旧行为会把 a=8 覆盖回 4 均势）。"""
    budget = DecodeWorkerBudget(capacity=8)
    applied: dict[str, int] = {}
    a = budget.acquire("a", role="playback",
                       apply=lambda n: applied.__setitem__("a", n))
    stale_gen = budget._generation  # T1 计算快照时的世代（此后未及 apply）

    b = budget.acquire("b", role="export",
                       apply=lambda n: applied.__setitem__("b", n))
    assert applied == {"a": 4, "b": 4}
    newer_gen = budget._generation

    # T1 恢复后拿旧快照（{a: 8}）迟到应用——必须按世代丢弃
    budget._apply_updates(
        [(lambda n: applied.__setitem__("a", n), 8, "a")], stale_gen)
    assert applied == {"a": 4, "b": 4}  # 过期快照未生效
    # 同代/新一代快照正常应用
    budget._apply_updates(
        [(lambda n: applied.__setitem__("a", n), 8, "a")], newer_gen)
    assert applied["a"] == 8
    a.release()
    b.release()


def test_aud_32104_concurrent_acquires_end_on_latest_state() -> None:
    """并发 acquire/release 压测：最终下发值必须等于最终登记表的公平分配。"""
    budget = DecodeWorkerBudget(capacity=4)
    barrier = threading.Barrier(4)

    def churn(worker_id: int) -> None:
        barrier.wait()
        for i in range(60):
            key = f"w{worker_id}-{i}"
            lease = budget.acquire(key, apply=lambda n: None)
            lease.release()

    threads = [threading.Thread(target=churn, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    applied: dict[str, int] = {}
    final = budget.acquire("final", apply=lambda n: applied.__setitem__("final", n))
    try:
        # 表清空后单一 lease 应拿到全部容量（若过期快照可后写覆盖，
        # 最终下发值可能停留在并发期间的旧均势）
        assert final.assigned == 4
        assert applied["final"] == 4
    finally:
        final.release()


# ---------------------------------------------------------------------------
# AUD-32108：_native_set/_native_get 收窄 except + 可见告警
# ---------------------------------------------------------------------------

def test_aud_32108_native_failure_logged_and_registry_consistent(
        monkeypatch, caplog) -> None:
    """native 域内错误（OSError/ToposCodecError）→ 告警可见，登记仍一致。"""
    policy = SliceThreadsPolicy()

    class _BoomCodec:
        def __init__(self) -> None:
            raise OSError("dylib load boom")

    monkeypatch.setattr(topos_binding, "ToposCodec", _BoomCodec)
    with caplog.at_level(logging.WARNING,
                         logger="src.shared.codec.slice_threads_policy"):
        policy.register("enc", 4, role="encode")  # 不得抛出
    assert policy.effective() == 4
    assert policy.describe()["applied"] is None  # 未下发显式暴露
    assert any("下发失败" in rec.getMessage() for rec in caplog.records)
    assert any(rec.exc_info for rec in caplog.records)  # exc_info 保留
    policy.unregister("enc")


def test_aud_32108_domain_error_narrowly_caught_and_logged(
        monkeypatch, caplog) -> None:
    """ToposCodecError（领域错误）也被收窄捕获 + 告警（旧代码零日志吞掉）。"""
    policy = SliceThreadsPolicy()

    class _BoomSet:
        def __init__(self) -> None:
            pass

        def set_slice_threads(self, n: int) -> int:
            raise ToposCodecError("set_slice_threads rejected")

        def slice_threads(self) -> int:
            return 8

    monkeypatch.setattr(topos_binding, "ToposCodec", _BoomSet)
    with caplog.at_level(logging.WARNING,
                         logger="src.shared.codec.slice_threads_policy"):
        policy.register("enc", 2, role="encode")
    assert policy.describe()["applied"] is None
    assert any("下发失败" in rec.getMessage() for rec in caplog.records)
    policy.unregister("enc")


def test_aud_32108_unexpected_exception_not_swallowed(monkeypatch) -> None:
    """名单外异常（如 RuntimeError）不再被裸 except 吞掉——显式上抛。"""
    policy = SliceThreadsPolicy()

    class _RtCodec:
        def __init__(self) -> None:
            raise RuntimeError("unexpected bug")

    monkeypatch.setattr(topos_binding, "ToposCodec", _RtCodec)
    with pytest.raises(RuntimeError, match="unexpected bug"):
        policy.register("enc", 4, role="encode")


# ---------------------------------------------------------------------------
# AUD-32201：movie_config 数值域校验（防 ctypes 静默回绕）
# ---------------------------------------------------------------------------

def _movie_cfg_or_skip() -> ToposCodec:
    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit is None and not any(
        p.is_file() for p in topos_binding.candidate_library_paths()
    ):
        pytest.skip("libtopos_codec 未构建")
    return ToposCodec()


@pytest.mark.parametrize("kwargs,field", [
    ({"width": 70000}, "width"),        # c_uint16 回绕为 4464 落入合法域
    ({"height": 70000}, "height"),
    ({"width": 0}, "width"),
    ({"height": -1}, "height"),
    ({"qp": 300}, "qp"),                # c_uint8 回绕为 44 绕过 native 上限
    ({"qp": -1}, "qp"),
    ({"timescale": -24000}, "timescale"),  # c_uint32 回绕为巨大 u32
    ({"timescale": 0}, "timescale"),
])
def test_aud_32201_movie_config_rejects_out_of_domain(kwargs, field) -> None:
    codec = _movie_cfg_or_skip()
    args = dict(width=64, height=48, qp=24, timescale=24000)
    args.update(kwargs)
    with pytest.raises(ToposCodecError, match=field):
        codec.movie_config(**args)


def test_aud_32201_movie_config_accepts_valid_domain() -> None:
    codec = _movie_cfg_or_skip()
    cfg = codec.movie_config(
        width=topos_binding.TC_CODEC_MAX_DIM, height=1,
        qp=topos_binding.TOPOS_QP_MAX, timescale=0xFFFFFFFF)
    assert cfg.visible_width == topos_binding.TC_CODEC_MAX_DIM
    assert cfg.qp_base == topos_binding.TOPOS_QP_MAX
    assert cfg.timescale == 0xFFFFFFFF


# ---------------------------------------------------------------------------
# AUD-32202：ParallelSequentialDecoder worker 构造段异常必须送达主线程
# ---------------------------------------------------------------------------

class _ProbeOnlyMovie:
    """首次构造（主线程 __enter__ 探测）成功，后续（worker 路）抛错。"""
    n_created = 0

    def __init__(self, codec, path) -> None:  # type: ignore[no-untyped-def]
        type(self).n_created += 1
        if type(self).n_created > 1:
            raise OSError("worker-open boom")
        self._info = SimpleNamespace(sample_count=4)

    def info(self) -> SimpleNamespace:
        return self._info

    def close(self) -> None:
        pass


def test_aud_32202_worker_pretry_failure_delivered(monkeypatch, tmp_path) -> None:
    """worker 在 try 块之前（ToposMovieFile 构造）失败 → 异常送达而非挂死。"""
    _ProbeOnlyMovie.n_created = 0
    monkeypatch.setattr(topos_binding, "ToposMovieFile", _ProbeOnlyMovie)
    codec_stub = SimpleNamespace()
    dec = topos_binding.ParallelSequentialDecoder(
        str(tmp_path / "x.toos"), workers=2, threads=None, codec=codec_stub)
    with dec:
        with pytest.raises(OSError, match="worker-open boom"):
            list(dec)


class _CloseCountingMovie:
    n_created = 0
    n_closed = 0

    def __init__(self, codec, path) -> None:  # type: ignore[no-untyped-def]
        type(self).n_created += 1
        self._info = SimpleNamespace(sample_count=4)

    def info(self) -> SimpleNamespace:
        return self._info

    def close(self) -> None:
        type(self).n_closed += 1


def test_aud_32202_worker_closes_movie_when_decoder_ctor_fails(
        monkeypatch, tmp_path) -> None:
    """dec 构造失败时 movie 句柄仍被 finally 关闭（不泄漏 fd）。"""

    class _BoomDecoder:
        def __init__(self, codec, max_slice_workers: int = 0) -> None:  # type: ignore[no-untyped-def]
            raise ToposCodecError("tc_decoder_create OOM")

    _CloseCountingMovie.n_created = 0
    _CloseCountingMovie.n_closed = 0
    monkeypatch.setattr(topos_binding, "ToposMovieFile", _CloseCountingMovie)
    monkeypatch.setattr(topos_binding, "ToposDecoder", _BoomDecoder)
    codec_stub = SimpleNamespace()
    dec = topos_binding.ParallelSequentialDecoder(
        str(tmp_path / "x.toos"), workers=2, threads=None, codec=codec_stub)
    with dec:
        with pytest.raises(ToposCodecError, match="tc_decoder_create OOM"):
            list(dec)
    # 主线程探测句柄 + 2 个 worker 句柄全部关闭
    assert _CloseCountingMovie.n_closed == _CloseCountingMovie.n_created == 3


# ---------------------------------------------------------------------------
# AUD-32203：_write_cb 与 _seek_write_cb 锁对称
# ---------------------------------------------------------------------------

class _RecordingLock:
    """兼容 with / acquire / release 三种用法的计数互斥。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquire_count = 0

    def acquire(self) -> None:
        self._lock.acquire()
        self.acquire_count += 1

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "_RecordingLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _make_mux_with_fd(tmp_path):  # type: ignore[no-untyped-def]
    mux = object.__new__(ToposMuxFile)  # 绕过 tc_mux_create（无需 native 调用）
    mux._fd = topos_binding._open_binary(
        str(tmp_path / "mux.bin"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    return mux


def test_aud_32203_write_cb_takes_instance_lock(tmp_path) -> None:
    """顺序写回调必须在实例锁内执行（与 seek_write 对称）。"""
    mux = _make_mux_with_fd(tmp_path)
    try:
        rec = _RecordingLock()
        mux._wlock = rec
        buf = ctypes.create_string_buffer(b"hello-aud32203")
        rc = mux._write_cb(0, ctypes.addressof(buf), len(b"hello-aud32203"))
        assert rc == 0
        assert rec.acquire_count >= 1  # 旧实现此处为 0（无锁）
    finally:
        os.close(mux._fd)


def test_aud_32203_write_and_seek_write_mutually_excluded(tmp_path) -> None:
    """功能性对拍：顺序写 + 定位写经共享锁互斥后落盘内容正确。

    顺序写 #1 落在 [0:4]（fd 偏移 4）；seek_write 落在 [8:10]（不动
    顺序偏移）；顺序写 #2 落在 [4:8]（沿用顺序偏移）。
    """
    mux = _make_mux_with_fd(tmp_path)
    try:
        mux._wlock = threading.Lock()
        a = ctypes.create_string_buffer(b"AAAA")
        b = ctypes.create_string_buffer(b"BB")
        assert mux._write_cb(0, ctypes.addressof(a), 4) == 0
        assert mux._seek_write_cb(0, 8, ctypes.addressof(b), 2) == 0
        assert mux._write_cb(0, ctypes.addressof(a), 4) == 0
    finally:
        os.close(mux._fd)
    data = (tmp_path / "mux.bin").read_bytes()
    assert data == b"AAAAAAAA" + b"BB"  # [0:8]=两段顺序写，[8:10]=定位写


# ---------------------------------------------------------------------------
# AUD-32204：decode 族入口复用 C4 per-thread scratch
# ---------------------------------------------------------------------------

def test_aud_32204_tile_output_planes_grow_only_reuse() -> None:
    """TLS scratch grow-only：需求变小/不变时复用同一底层缓冲。"""
    codec_stub = object.__new__(ToposCodec)
    codec_stub._dec_tls = threading.local()
    big = codec_stub._tile_output_planes([(4, 4), (2, 4)])
    assert [len(v) for v in big] == [16, 8]
    scratch_obj = codec_stub._dec_tls.scratch
    assert codec_stub._dec_tls.elems == 24
    # 同线程再次平铺更小需求 → 复用（identity 不变、不重新分配）
    small = codec_stub._tile_output_planes([(2, 2)])
    assert codec_stub._dec_tls.scratch is scratch_obj
    assert codec_stub._dec_tls.elems == 24
    # 视图零重叠：写入互不影响
    small[0][0] = 0xBEEF
    assert big[0][0] == 0xBEEF  # small 覆盖 big 的起点（同一线程平铺约定）
    small2 = codec_stub._tile_output_planes([(1, 1)])
    assert small2[0][0] == 0xBEEF
    # 更大需求 → 扩容（重新分配）
    grown = codec_stub._tile_output_planes([(8, 8)])
    assert codec_stub._dec_tls.elems == 64
    assert len(grown[0]) == 64


def _synth_planes(w: int, h: int) -> list[bytes]:
    cw = (w + 1) // 2
    y = struct.pack(f"<{w*h}H", *[((x * 7 + r * 13) % 400 + 300)
                                  for r in range(h) for x in range(w)])
    u = struct.pack(f"<{cw*h}H", *[(480 + (x % 5) * 8)
                                   for r in range(h) for x in range(cw)])
    v = struct.pack(f"<{cw*h}H", *[(520 + (r % 4) * 6)
                                   for r in range(h) for x in range(cw)])
    return [y, u, v]


def _make_frame_config(w: int, h: int, qp_base: int = 24) -> "topos_binding._CFrameConfig":
    c = topos_binding._CFrameConfig()
    c.struct_size = ctypes.sizeof(topos_binding._CFrameConfig)
    c.abi_version = topos_binding.TOPOS_CODEC_ABI_VERSION
    c.visible_width = w
    c.visible_height = h
    c.qp_base = qp_base
    c.qmatrix_id = 1
    return c


def test_aud_32204_decode_family_scratch_reuse_roundtrip() -> None:
    """真实编解码对拍：decode/decode_batch/decode_reduced/decode_request
    改用 TLS scratch 后输出与契约一致（自有 bytes 拷贝、逐帧稳定）。"""
    codec = _movie_cfg_or_skip()
    w, h = 40, 24
    cfg = _make_frame_config(w, h)
    planes = _synth_planes(w, h)
    pkt, _ = codec.encode_frame(cfg, planes)

    f1 = codec.decode(pkt)
    f2 = codec.decode(pkt)  # 同线程复用 scratch：两次结果一致（不被残留污染）
    assert f1.planes == f2.planes
    assert len(f1.planes[0]) == w * h * 2

    # decode_batch：逐帧平铺共享同一 scratch，帧间不得互相覆盖
    if getattr(codec, "_has_decode_batch", False):
        frames = codec.decode_batch([pkt, pkt, pkt])
        assert len(frames) == 3
        for fr in frames:
            assert fr.planes == f1.planes

    # decode_reduced：低频输出几何按比例缩小、可重复
    if getattr(codec, "_has_reduced_decode", False):
        r1 = codec.decode_reduced(pkt, topos_binding.TC_DECODE_SCALE_QUARTER)
        r2 = codec.decode_reduced(pkt, topos_binding.TC_DECODE_SCALE_QUARTER)
        assert r1.planes == r2.planes
        assert len(r1.planes[0]) < len(f1.planes[0])

    # decode_request：AUTO_2K 与 decode() 全量结果一致（小图不放大）
    if getattr(codec, "_has_decode_request", False):
        req = codec.make_decode_request(topos_binding.TC_DECODE_MODE_FULL)
        q1 = codec.decode_request(pkt, req)
        q2 = codec.decode_request(pkt, req)
        assert q1.planes == q2.planes == f1.planes


# ---------------------------------------------------------------------------
# AUD-32307：decode_topos_meta 的 UTF-8 解码错误 → ToposMetaError
# ---------------------------------------------------------------------------

def test_aud_32307_invalid_utf8_raises_topos_meta_error() -> None:
    """CRC 正确但 vendor/label 含非法 UTF-8 → ToposMetaError（可降级）。"""
    good = encode_topos_meta(tier_id=2, vendor="TOPOS", label="Topos 422 HQ")
    # 篡改 vendor 首字节为非法 UTF-8 起始（0xFF），并重算 CRC 使其一致
    import zlib
    bad = bytearray(good)
    bad[12] = 0xFF
    body = bytes(bad[:-4])
    payload = body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    with pytest.raises(ToposMetaError, match="UTF-8"):
        decode_topos_meta(payload)


def test_aud_32307_valid_payload_still_decodes() -> None:
    payload = encode_topos_meta(tier_id=3, vendor="TOPOS", label="toos High 16bit")
    assert decode_topos_meta(payload) == {
        "tier_id": 3, "vendor": "TOPOS", "label": "toos High 16bit"}
