"""修复验证：批 wd16-g89（计划表批 D，shared/media 上半，16 条 Major）。

覆盖：
- AUD-34300：VideoDecoderAdapter planar（YUV/GBR）帧真实转换，不再依赖
  对 planar 恒返回 None 的 DecodedFrame.to_rgb()。
- AUD-34301：total_frames/fps/width/height 只捕获领域异常并记录 warning，
  探测失败缓存且 fps 不再伪造 30.0。
- AUD-34309：_pending_generates 去重 guard 在 entry 缺失时也返回。
- AUD-34310：GENERATING/pending 孤儿状态自愈；_mark_failed 先清 pending；
  服务侧取消终态广播 MISSING 并回调挂起调用方。
- AUD-34311：_sync_from_disk_proxy_info 从 proxy_resolution 取尺寸；
  open_proxy_source 尺寸未知时不伪造 1920x1080。
- AUD-34401：limited/full 色度 range offset 用对称下界 c_min（中性 128 →
  ≈127.5，与 _apply_range_conversion 的 224 全幅基准一致）。
- AUD-34411：_get_8bit_format 链式降级到不动点（'yuv422p10le'→'yuv420p'）。
- AUD-34421：transfer 三套映射表补 TRAW_LOG0/ST2084 条目，枚举往返不丢。
- AUD-34500：seek/stop 代数（epoch）使在途旧帧入队前被丢弃。
- AUD-34501：request_frame 去重返回队列中既有句柄，cancel_request 有效。
- AUD-34507：READY 缓存命中复验源签名，源修改降级 STALE；update_config
  清 READY 缓存。
- AUD-34508：磁盘发现路径从源元数据探测分辨率，失败置 None（不虚构 4K）。
- AUD-34509：GENERATING 分支回调挂按 source_path 注册表，终态消费清理，
  不再泄漏进全局列表 / 跨文件串扰 / 丢弃 on_complete。
- AUD-34600：probe 完整回退路径异常时 finally 关闭已创建源。
- AUD-34601：create(auto_open=True) 打开失败后 best-effort close 再切换
  下一后端。
- AUD-34602：类级缓存/失败记录容量上限 + LRU 淘汰。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.shared.color.types import ColorTransfer
from src.shared.media.adapter import VideoDecoderAdapter
from src.shared.media.asset_proxy_manager import (
    AssetProxyManager,
    _GENERATING_STALE_TIMEOUT_S,
)
from src.shared.media.color_decode import ColorConverter
from src.shared.media.color_metadata import (
    color_transfer_to_ffmpeg,
    ffmpeg_to_color_transfer,
    get_standard_name,
)
from src.shared.media.decode_policy import DecodePolicyManager, DecodeTarget
from src.shared.media.decode_queue import DecodeQueue, DecodedFrameEntry, FrameRequest
from src.shared.media.disk_proxy_service import DiskProxyService
from src.shared.media.factory import MediaSourceFactory
from src.shared.media.frame import DecodedFrame, PlaneInfo
from src.shared.media.proxy import DiskProxyInfo, ProxyStatus
from src.shared.media.proxy_generator import ProxyGenerationResult, ProxyProgress
from src.shared.media.source import MediaOpenError
from src.shared.media.proxy_manifest import ProxyManifestEntry


# =============================================================================
# AUD-34300 / AUD-34301：adapter
# =============================================================================

def _make_adapter_stub() -> VideoDecoderAdapter:
    """构造不走 __init__（不打开文件）的适配器实例。"""
    adapter = VideoDecoderAdapter.__new__(VideoDecoderAdapter)
    adapter._source = None  # 避免 __del__ 对半构造对象产生噪音（AUD-34306 范围外）
    adapter._preserve_depth = False
    adapter._last_frame_depth = 8
    adapter._probe_failed = False
    adapter._path = "/stub"
    adapter._backend = "auto"
    return adapter


def _make_yuv422_frame(y: int = 126, uv: int = 128, bit_depth: int = 8) -> DecodedFrame:
    """构造 8x4 的 planar YUV 4:2:2 帧（limited range，BT.709）。"""
    dtype = np.uint8 if bit_depth == 8 else np.uint16
    h, w = 4, 8
    planes = (
        np.full((h, w), y, dtype=dtype),
        np.full((h, w // 2), uv, dtype=dtype),
        np.full((h, w // 2), uv, dtype=dtype),
    )
    infos = (
        PlaneInfo.create(0, w, h, "Y", dtype, bit_depth),
        PlaneInfo.create(1, w // 2, h, "U", dtype, bit_depth),
        PlaneInfo.create(2, w // 2, h, "V", dtype, bit_depth),
    )
    fmt = "yuv422p" if bit_depth == 8 else "yuv422p10le"
    return DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0, data=None, planes=planes,
        pixel_format=fmt, dtype=np.dtype(dtype), width=w, height=h,
        metadata=SimpleNamespace(color_matrix="bt709", color_range="limited"),
        plane_infos=infos,
    )


def _make_gbr_frame(bit_depth: int = 10) -> DecodedFrame:
    """构造 planar GBR 帧（平面序 G,B,R）：G=满刻度、B=0、R=半刻度。"""
    full = (1 << bit_depth) - 1
    h, w = 4, 8
    planes = (
        np.full((h, w), full, dtype=np.uint16),      # G
        np.zeros((h, w), dtype=np.uint16),           # B
        np.full((h, w), full // 2, dtype=np.uint16),  # R
    )
    infos = tuple(
        PlaneInfo.create(i, w, h, c, np.uint16, bit_depth)
        for i, c in enumerate(("G", "B", "R"))
    )
    return DecodedFrame(
        frame_index=0, pts=0, time_seconds=0.0, data=None, planes=planes,
        pixel_format="gbrp10le", dtype=np.dtype(np.uint16), width=w, height=h,
        metadata=None, plane_infos=infos,
    )


class TestAud34300PlanarConversion:
    def test_planar_yuv_gray_to_bgr8(self) -> None:
        adapter = _make_adapter_stub()
        frame = _make_yuv422_frame()
        bgr = adapter._convert_to_bgr8(frame)
        assert bgr is not None
        assert bgr.dtype == np.uint8
        assert bgr.shape == (4, 8, 3)
        # limited 8-bit：Y=126 → y_norm=(126-16)/219≈0.502 → ≈128 中性灰
        assert int(bgr[0, 0, 0]) == pytest.approx(128, abs=2)
        assert int(bgr[0, 0, 1]) == pytest.approx(128, abs=2)
        assert int(bgr[0, 0, 2]) == pytest.approx(128, abs=2)

    def test_planar_gbr_to_bgr8(self) -> None:
        adapter = _make_adapter_stub()
        bgr = adapter._convert_to_bgr8(_make_gbr_frame())
        assert bgr.dtype == np.uint8
        # RGB=(512,1023,0)/1023 ≈ (0.5,1.0,0) → BGR=(0,255,127)
        assert int(bgr[0, 0, 0]) == pytest.approx(0, abs=2)
        assert int(bgr[0, 0, 1]) == pytest.approx(255, abs=2)
        assert int(bgr[0, 0, 2]) == pytest.approx(127, abs=2)

    def test_planar_yuv_preserve_depth(self) -> None:
        adapter = _make_adapter_stub()
        # 10-bit limited 中性：Y=512、U=V=512（c_offset）
        frame = _make_yuv422_frame(y=512, uv=512, bit_depth=10)
        out = adapter._convert_preserving_depth(frame)
        assert out.dtype == np.uint16
        # y_norm=(512-64)/876≈0.511 → ×1023 ≈ 523
        assert int(out[0, 0, 0]) == pytest.approx(523, abs=4)
        assert adapter._last_frame_depth == 10

    def test_cfa_raises_domain_error(self) -> None:
        adapter = _make_adapter_stub()
        frame = _make_yuv422_frame()
        object.__setattr__(frame, "pixel_format", "topos_cfa12le")
        with pytest.raises(ValueError, match="CFA"):
            adapter._convert_to_bgr8(frame)

    def test_insufficient_planes_raises(self) -> None:
        adapter = _make_adapter_stub()
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(np.zeros((2, 2), dtype=np.uint8),),
            pixel_format="yuv420p", dtype=np.dtype(np.uint8),
        )
        with pytest.raises(ValueError, match="平面数不足"):
            adapter._convert_to_bgr8(frame)


class TestAud34301Properties:
    def _make_failing_adapter(self) -> tuple[VideoDecoderAdapter, MagicMock]:
        source = MagicMock()
        source.probe.side_effect = MediaOpenError("probe failed")
        adapter = VideoDecoderAdapter.__new__(VideoDecoderAdapter)
        adapter._source = source
        adapter._path = "/x.mov"
        adapter._probe_failed = False
        adapter._metadata_cache = None
        return adapter, source

    def test_fps_failure_returns_zero_not_30(self) -> None:
        adapter, _ = self._make_failing_adapter()
        assert adapter.fps == 0.0

    def test_dimensions_failure_return_zero_with_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter, _ = self._make_failing_adapter()
        with caplog.at_level("WARNING"):
            assert adapter.width == 0
            assert adapter.height == 0
            assert adapter.total_frames == 0
        assert any("探测失败" in r.message for r in caplog.records)

    def test_probe_failure_cached(self) -> None:
        adapter, source = self._make_failing_adapter()
        adapter.fps
        adapter.width
        adapter.height
        adapter.total_frames
        assert source.probe.call_count == 1  # 失败后不再重复探测

    def test_non_domain_exception_propagates(self) -> None:
        source = MagicMock()
        source.probe.side_effect = RuntimeError("bug")  # 非领域异常必须逃逸
        adapter = VideoDecoderAdapter.__new__(VideoDecoderAdapter)
        adapter._source = source
        adapter._path = "/x.mov"
        adapter._probe_failed = False
        with pytest.raises(RuntimeError):
            _ = adapter.width


# =============================================================================
# AUD-34309 / AUD-34310 / AUD-34311：AssetProxyManager
# =============================================================================

@pytest.fixture
def proxy_mgr(tmp_path: Path) -> AssetProxyManager:
    src = tmp_path / "src.mov"
    src.write_bytes(b"\x00" * 32)
    mgr = AssetProxyManager()
    mgr.configure(cache_dir=tmp_path / "cache", app_version="1")
    return mgr


class TestAud34309DedupGuard:
    def test_guard_returns_even_without_entry(self, proxy_mgr: AssetProxyManager) -> None:
        """竞态窗口：pending 已置位但 entry 尚未写入——guard 也必须返回。"""
        # 用一个真实存在的源文件（manifest key 需要 mtime/size）
        src_file = proxy_mgr._cache_dir.parent / "src.mov"
        assert src_file.exists()
        source_path = str(src_file)

        mock_service = MagicMock()
        proxy_mgr._disk_proxy_service = mock_service
        # 模拟竞态：pending 已置位、manifest 尚无 entry
        proxy_mgr._pending_generates[source_path] = time.time()

        entry = proxy_mgr.ensure_proxy_async(source_path=source_path, asset_id="a2")
        assert entry.is_generating
        # 关键断言：不得重复派发生成
        assert mock_service.generate_proxy.call_count == 0

    def test_pending_generates_store_timestamp(self, proxy_mgr: AssetProxyManager) -> None:
        src_file = proxy_mgr._cache_dir.parent / "src.mov"
        mock_service = MagicMock()
        proxy_mgr._disk_proxy_service = mock_service
        proxy_mgr.ensure_proxy_async(source_path=str(src_file), asset_id="a1")
        pending = proxy_mgr._pending_generates.get(str(src_file))
        assert isinstance(pending, float)
        assert pending > 0.0


class TestAud34310StaleSelfHeal:
    def test_stale_generating_entry_regenerates(
        self, proxy_mgr: AssetProxyManager,
    ) -> None:
        src_file = proxy_mgr._cache_dir.parent / "src.mov"
        source_path = str(src_file)
        # 构造滞留 GENERATING entry（updated_at 超时）
        from src.shared.media.proxy_manifest import build_manifest_key

        key = build_manifest_key(source_path, proxy_mgr._config, proxy_mgr._app_version)
        assert key is not None
        stale = ProxyManifestEntry(
            key=key, status=ProxyStatus.GENERATING, proxy_path=None,
            width=0, height=0, codec="", pixel_format="",
            created_at=time.time() - (_GENERATING_STALE_TIMEOUT_S + 60),
            updated_at=time.time() - (_GENERATING_STALE_TIMEOUT_S + 60),
        )
        proxy_mgr._manifest.upsert_entry(stale)
        proxy_mgr._pending_generates[source_path] = (
            time.time() - (_GENERATING_STALE_TIMEOUT_S + 60))

        mock_service = MagicMock()
        proxy_mgr._disk_proxy_service = mock_service
        entry = proxy_mgr.ensure_proxy_async(source_path=source_path, asset_id="a1")
        # 自愈：重新派发生成
        assert mock_service.generate_proxy.call_count == 1
        assert entry.is_generating

    def test_mark_failed_clears_pending_without_entry(
        self, proxy_mgr: AssetProxyManager,
    ) -> None:
        proxy_mgr._pending_generates["/gone.mov"] = time.time()
        proxy_mgr._mark_failed("/gone.mov", "boom")  # 无 entry，不得抛错
        assert "/gone.mov" not in proxy_mgr._pending_generates

    def test_service_cancel_broadcasts_missing(self) -> None:
        """取消终态经 status 回调广播（manager manifest 不再滞留 GENERATING）。"""
        service = DiskProxyService()
        received: list[tuple[str, DiskProxyInfo]] = []
        service.register_status_callback(lambda p, i: received.append((p, i)))
        service._info_cache["/a.mov"] = DiskProxyInfo(status=ProxyStatus.GENERATING)

        result = ProxyGenerationResult(
            success=False, cancelled=True, source_path="/a.mov")
        service._on_generation_complete("/a.mov", result, None)
        assert received, "取消终态必须广播状态"
        assert received[0][1].status == ProxyStatus.MISSING


# AUD-34509（服务侧取消路径消费挂起回调）在 TestAud34509 中覆盖


class TestAud34311SyncDimensions:
    def test_sync_uses_proxy_resolution(self, proxy_mgr: AssetProxyManager) -> None:
        src_file = proxy_mgr._cache_dir.parent / "src.mov"
        source_path = str(src_file)
        info = DiskProxyInfo(
            status=ProxyStatus.READY, proxy_path="/cache/p.mov",
            proxy_resolution=(607, 1080))
        proxy_mgr._sync_from_disk_proxy_info(source_path, info)
        entry = proxy_mgr._manifest.get_entry(source_path)
        assert entry is not None
        assert (entry.width, entry.height) == (607, 1080)

    def test_open_proxy_source_no_fabricated_size(
        self, proxy_mgr: AssetProxyManager,
    ) -> None:
        src_file = proxy_mgr._cache_dir.parent / "src.mov"
        from src.shared.media.proxy_manifest import build_manifest_key

        key = build_manifest_key(str(src_file), proxy_mgr._config, proxy_mgr._app_version)
        assert key is not None
        entry = ProxyManifestEntry(
            key=key, status=ProxyStatus.READY, proxy_path="/cache/p.mov",
            width=0, height=0, codec="prores", pixel_format="",
            created_at=time.time(), updated_at=time.time(),
        )
        fake_source = object()
        with patch("src.shared.media.factory.MediaSourceFactory") as mock_factory:
            mock_factory.create.return_value = fake_source
            src = proxy_mgr.open_proxy_source(entry)
        assert src is fake_source
        # 尺寸未知：不传 target_size（不伪造 1920x1080）
        _, kwargs = mock_factory.create.call_args
        assert "target_size" not in kwargs or kwargs.get("target_size") is None

    def test_open_proxy_source_known_size_passed(
        self, proxy_mgr: AssetProxyManager,
    ) -> None:
        src_file = proxy_mgr._cache_dir.parent / "src.mov"
        from src.shared.media.proxy_manifest import build_manifest_key

        key = build_manifest_key(str(src_file), proxy_mgr._config, proxy_mgr._app_version)
        assert key is not None
        entry = ProxyManifestEntry(
            key=key, status=ProxyStatus.READY, proxy_path="/cache/p.mov",
            width=607, height=1080, codec="prores", pixel_format="",
            created_at=time.time(), updated_at=time.time(),
        )
        with patch("src.shared.media.factory.MediaSourceFactory") as mock_factory:
            mock_factory.create.return_value = object()
            proxy_mgr.open_proxy_source(entry)
        _, kwargs = mock_factory.create.call_args
        assert kwargs.get("target_size") == (607, 1080)


# =============================================================================
# AUD-34401：color_decode range offset
# =============================================================================

class TestAud34401ChromaRangeOffset:
    def test_limited_to_full_chroma_neutral(self) -> None:
        conv = ColorConverter()
        out = conv.limited_to_full(
            np.array([128], dtype=np.uint8), is_luma=False, bit_depth=8)
        # 对称下界 c_min=16：128 → 127.5（此前错误公式得 ≈131.9）
        assert 127 <= int(out[0]) <= 128

    def test_chroma_endpoints(self) -> None:
        conv = ColorConverter()
        out = conv.limited_to_full(
            np.array([16, 240], dtype=np.uint8), is_luma=False, bit_depth=8)
        assert int(out[0]) == 0
        assert int(out[1]) == 255

    def test_chroma_roundtrip(self) -> None:
        conv = ColorConverter()
        codes = np.arange(16, 241, dtype=np.uint8)
        full = conv.limited_to_full(codes, is_luma=False, bit_depth=8)
        back = conv.full_to_limited(full, is_luma=False, bit_depth=8)
        assert np.all(np.abs(back.astype(np.int32) - codes.astype(np.int32)) <= 1)

    def test_consistent_with_apply_range_conversion(self) -> None:
        """色度 offset 与 _apply_range_conversion 的 224 全幅基准一致。"""
        conv = ColorConverter()
        neutral = 128.0
        norm = conv._apply_range_conversion(
            np.float32(neutral), np.float32(neutral), np.float32(neutral),
            __import__("src.shared.color.types", fromlist=["ColorRange"]).ColorRange.LIMITED,
            8,
        )
        assert float(norm[1]) == pytest.approx(0.0, abs=1e-6)
        full = conv.limited_to_full(
            np.array([neutral], dtype=np.float32), is_luma=False, bit_depth=8)
        assert float(full[0]) / 255.0 - 0.5 == pytest.approx(float(norm[1]), abs=0.01)

    def test_10bit_chroma_neutral(self) -> None:
        conv = ColorConverter()
        out = conv.limited_to_full(
            np.array([512], dtype=np.uint16), is_luma=False, bit_depth=10)
        # c_min = 2*512-960 = 64；512 → (512-64)*1023/896 ≈ 511.5
        assert 511 <= int(out[0]) <= 512

    def test_luma_unchanged(self) -> None:
        conv = ColorConverter()
        out = conv.limited_to_full(
            np.array([16, 235], dtype=np.uint8), is_luma=True, bit_depth=8)
        assert int(out[0]) == 0
        assert int(out[1]) == 255


# =============================================================================
# AUD-34411：decode_policy 链式降级
# =============================================================================

class TestAud34411ChainedDowngrade:
    def test_422_10le_reaches_420p(self) -> None:
        assert DecodePolicyManager._get_8bit_format("yuv422p10le") == "yuv420p"

    def test_444_12le_reaches_420p(self) -> None:
        assert DecodePolicyManager._get_8bit_format("yuv444p12le") == "yuv420p"

    def test_420_10le_stays_420p(self) -> None:
        assert DecodePolicyManager._get_8bit_format("yuv420p10le") == "yuv420p"

    def test_already_8bit_fixed_point(self) -> None:
        assert DecodePolicyManager._get_8bit_format("yuv420p") == "yuv420p"

    def test_unknown_format_passthrough(self) -> None:
        assert DecodePolicyManager._get_8bit_format("gray16le") == "gray16le"

    def test_module_docstring_contract(self) -> None:
        """模块头示例：get_output_format('yuv422p10le', PREVIEW) == 'yuv420p'。"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
        assert DecodePolicyManager.get_output_format("yuv422p10le", policy) == "yuv420p"


# =============================================================================
# AUD-34421：color_metadata transfer 映射
# =============================================================================

class TestAud34421TransferMaps:
    def test_ffmpeg_20_maps_to_traw_log0(self) -> None:
        assert ffmpeg_to_color_transfer(20) is ColorTransfer.TRAW_LOG0

    def test_traw_log0_roundtrip(self) -> None:
        assert color_transfer_to_ffmpeg(ColorTransfer.TRAW_LOG0) == 20
        assert ffmpeg_to_color_transfer(
            color_transfer_to_ffmpeg(ColorTransfer.TRAW_LOG0)) is ColorTransfer.TRAW_LOG0

    def test_st2084_alias_maps_to_16(self) -> None:
        assert color_transfer_to_ffmpeg(ColorTransfer.ST2084) == 16
        assert color_transfer_to_ffmpeg(ColorTransfer.ST2084) == \
            color_transfer_to_ffmpeg(ColorTransfer.PQ)

    def test_standard_name_traw(self) -> None:
        assert get_standard_name(ColorTransfer.TRAW_LOG0) == "traw-log0"

    def test_all_known_transfers_have_ffmpeg_value(self) -> None:
        for transfer in ColorTransfer:
            if transfer is ColorTransfer.UNKNOWN:
                continue
            value: Optional[int] = color_transfer_to_ffmpeg(transfer)
            assert value is not None, f"{transfer} 缺 ffmpeg 反向映射"
            assert ffmpeg_to_color_transfer(value) is not None or transfer is ColorTransfer.UNKNOWN


# =============================================================================
# AUD-34500 / AUD-34501：DecodeQueue
# =============================================================================

class _FakeSource:
    """probe 返回 50 帧；read_frame 立即返回占位帧。"""

    def __init__(self) -> None:
        self.total_frames = 50

    def probe(self) -> SimpleNamespace:
        return SimpleNamespace(total_frames=self.total_frames)

    def read_frame(self, idx: int) -> SimpleNamespace:
        return SimpleNamespace(frame_index=idx)


class _GatedFakeSource(_FakeSource):
    """read_frame 阻塞在门闩上——用于复现 seek 与在途解码的竞态。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.decode_started = threading.Event()

    def read_frame(self, idx: int) -> SimpleNamespace:
        self.decode_started.set()
        assert self.gate.wait(timeout=5.0), "测试门闩超时"
        return SimpleNamespace(frame_index=idx)


class TestAud34501RequestHandle:
    def test_duplicate_request_returns_queued_handle(self) -> None:
        dq = DecodeQueue(_FakeSource(), window_size=0, max_queue_size=8)
        r1 = dq.request_frame(5)
        r2 = dq.request_frame(5)
        assert r1 is r2, "重复请求必须返回队列中既有句柄"

    def test_cancel_on_returned_handle_is_effective(self) -> None:
        dq = DecodeQueue(_FakeSource(), window_size=0, max_queue_size=8)
        handle = dq.request_frame(5)
        dq.cancel_request(handle)
        popped, _ = dq._pop_next_request()
        assert popped is None, "取消返回句柄必须取消队列中的真实请求"

    def test_priority_upgrade_on_duplicate(self) -> None:
        dq = DecodeQueue(_FakeSource(), window_size=0, max_queue_size=8)
        r1 = dq.request_frame(5, is_master=False, priority=3)
        r2 = dq.request_frame(5, is_master=True, priority=0)
        assert r1 is r2
        assert r1.priority == 0


class TestAud34500SeekEpoch:
    def test_seek_and_stop_increment_epoch(self) -> None:
        dq = DecodeQueue(_FakeSource(), window_size=0, max_queue_size=8)
        assert dq._decode_epoch == 0
        dq.seek(10)
        assert dq._decode_epoch == 1
        dq.stop(timeout=0.5)
        assert dq._decode_epoch == 2

    def test_stale_entry_dropped_by_getter(self) -> None:
        dq = DecodeQueue(_FakeSource(), window_size=0, max_queue_size=8)
        dq.seek(10)  # epoch → 1
        stale = DecodedFrameEntry(
            frame=SimpleNamespace(frame_index=0), frame_index=0,
            is_master=False, decode_epoch=0)
        dq._decoded_queue.put(stale)
        assert dq.get_frame(timeout=0.1) is None
        assert dq._decoded_queue.qsize() == 0

    def test_inflight_frame_dropped_after_seek(self) -> None:
        """复现审计竞态：seek 清队后在途旧帧不得重新入队。"""
        src = _GatedFakeSource()
        dq = DecodeQueue(src, window_size=0, max_queue_size=16)
        dq.start()
        try:
            dq.request_frame(7, is_master=True)
            assert src.decode_started.wait(timeout=5.0)
            dq.seek(30)          # 清队 + epoch 自增 + 新请求
            src.gate.set()       # 放行在途的旧帧解码
            # 等待新位置帧产出；期间任何可见条目都必须是新代的
            deadline = time.monotonic() + 5.0
            got_30 = False
            while time.monotonic() < deadline:
                entry = dq.get_frame_entry(timeout=0.1)
                if entry is None:
                    continue
                assert entry.frame_index == 30, \
                    f"过期帧 {entry.frame_index} 泄漏进解码队列"
                got_30 = True
                break
            assert got_30, "seek 后应产出新位置帧"
        finally:
            dq.stop(timeout=2.0)


# =============================================================================
# AUD-34507 / AUD-34508 / AUD-34509：DiskProxyService
# =============================================================================

def _make_service_with_proxy(tmp_path: Path) -> tuple[DiskProxyService, Path]:
    """构造带 mock manager 的服务：manager 报告给定代理路径有效。"""
    src = tmp_path / "source.mov"
    src.write_bytes(b"\x00" * 100)
    proxy = tmp_path / "proxy.mov"
    proxy.write_bytes(b"\x01" * 50)

    service = DiskProxyService()
    service._manager = MagicMock()
    service._manager.get_valid_proxy_path.return_value = str(proxy)
    return service, src


class TestAud34507StaleDetection:
    def test_source_modified_downgrades_to_stale(self, tmp_path: Path) -> None:
        service, src = _make_service_with_proxy(tmp_path)
        with patch.object(MediaSourceFactory, "probe_video_metadata_cached") as probe:
            probe.return_value = SimpleNamespace(metadata=None)
            info1 = service.get_disk_proxy_info(str(src))
            assert info1.status == ProxyStatus.READY
            # 源文件被外部修改（尺寸变化 → 签名失配）
            src.write_bytes(b"\x00" * 200)
            time.sleep(0.01)
            src.touch()
            info2 = service.get_disk_proxy_info(str(src))
        assert info2.status == ProxyStatus.STALE

    def test_unmodified_source_stays_ready(self, tmp_path: Path) -> None:
        service, src = _make_service_with_proxy(tmp_path)
        with patch.object(MediaSourceFactory, "probe_video_metadata_cached") as probe:
            probe.return_value = SimpleNamespace(metadata=None)
            assert service.get_disk_proxy_info(str(src)).status == ProxyStatus.READY
            assert service.get_disk_proxy_info(str(src)).status == ProxyStatus.READY

    def test_update_config_clears_ready_cache(self, tmp_path: Path) -> None:
        from src.shared.media.proxy import DEFAULT_DISK_PROXY_CONFIG

        service, src = _make_service_with_proxy(tmp_path)
        with patch.object(MediaSourceFactory, "probe_video_metadata_cached") as probe:
            probe.return_value = SimpleNamespace(metadata=None)
            assert service.get_disk_proxy_info(str(src)).status == ProxyStatus.READY
            service.update_config(DEFAULT_DISK_PROXY_CONFIG)
            assert str(src) not in service._info_cache


class TestAud34508ResolutionDiscovery:
    def test_resolution_from_source_metadata(self, tmp_path: Path) -> None:
        service, src = _make_service_with_proxy(tmp_path)
        md = SimpleNamespace(width=1920, height=1080)
        with patch.object(MediaSourceFactory, "probe_video_metadata_cached") as probe:
            probe.return_value = SimpleNamespace(metadata=md)
            info = service.get_disk_proxy_info(str(src))
        # HALF(1080p) = (960, 540)；此前硬编码 4K 基准错误产出 (1920, 1080)
        assert info.proxy_resolution == (960, 540)

    def test_probe_failure_yields_none_not_fabricated(self, tmp_path: Path) -> None:
        service, src = _make_service_with_proxy(tmp_path)
        with patch.object(MediaSourceFactory, "probe_video_metadata_cached") as probe:
            probe.return_value = SimpleNamespace(metadata=None)
            info = service.get_disk_proxy_info(str(src))
        assert info.status == ProxyStatus.READY
        assert info.proxy_resolution is None


class TestAud34509GeneratingCallbacks:
    def _generating_service(self) -> DiskProxyService:
        service = DiskProxyService()
        service._info_cache["/a.mov"] = DiskProxyInfo(status=ProxyStatus.GENERATING)
        return service

    def test_no_global_leak_and_registry_used(self) -> None:
        service = self._generating_service()
        calls: list[str] = []
        service.generate_proxy(
            "/a.mov", "asset-1",
            on_complete=lambda p, i: calls.append("complete"),
            on_progress=lambda p, pr: calls.append("progress"),
            on_error=lambda p, e: calls.append("error"),
        )
        # 不再追加进全局列表（无泄漏 / 无跨文件串扰）
        assert service._progress_callbacks == []
        assert "/a.mov" in service._pending_caller_callbacks

    def test_pending_progress_dispatched_per_path(self) -> None:
        service = self._generating_service()
        progress_hits: list[str] = []
        service.generate_proxy(
            "/a.mov", "asset-1",
            on_progress=lambda p, pr: progress_hits.append(p))
        other_hits: list[str] = []
        service._progress_callbacks.append(lambda p, pr: other_hits.append(p))

        progress = ProxyProgress(percent=42.0)
        service._on_generation_progress("/b.mov", progress, None)
        assert progress_hits == []  # 只发给同 source_path 的挂起回调

        service._on_generation_progress("/a.mov", progress, None)
        assert progress_hits == ["/a.mov"]
        # 全局回调仍按原有语义分发（对每个进度事件广播）
        assert "/a.mov" in other_hits

    def test_pending_complete_fired_and_drained_on_success(self) -> None:
        service = self._generating_service()
        got: list[DiskProxyInfo] = []
        service.generate_proxy(
            "/a.mov", "asset-1", on_complete=lambda p, i: got.append(i))
        result = ProxyGenerationResult(
            success=True, proxy_path="/cache/a.mov",
            proxy_resolution=(960, 540), proxy_size_bytes=10,
            elapsed_seconds=0.1)
        service._on_generation_complete("/a.mov", result, None)
        assert len(got) == 1
        assert got[0].is_ready
        assert "/a.mov" not in service._pending_caller_callbacks

    def test_pending_error_fired_and_drained(self) -> None:
        service = self._generating_service()
        errors: list[str] = []
        service.generate_proxy(
            "/a.mov", "asset-1", on_error=lambda p, e: errors.append(e))
        service._on_generation_error("/a.mov", "boom", None)
        assert errors == ["boom"]
        assert "/a.mov" not in service._pending_caller_callbacks

    def test_pending_complete_fired_on_cancel(self) -> None:
        service = self._generating_service()
        got: list[DiskProxyInfo] = []
        service.generate_proxy(
            "/a.mov", "asset-1", on_complete=lambda p, i: got.append(i))
        result = ProxyGenerationResult(success=False, cancelled=True)
        service._on_generation_complete("/a.mov", result, None)
        assert len(got) == 1
        assert got[0].status == ProxyStatus.MISSING
        assert "/a.mov" not in service._pending_caller_callbacks


# =============================================================================
# AUD-34600 / AUD-34601 / AUD-34602：factory
# =============================================================================

class TestAud34600ProbeFallbackClose:
    def test_source_closed_when_open_raises(self, tmp_path: Path) -> None:
        media = tmp_path / "bad.mov"
        media.write_bytes(b"garbage")

        fake = MagicMock()
        fake.open.side_effect = MediaOpenError("open failed")

        with patch.object(MediaSourceFactory, "_probe_video_metadata_ffmpeg",
                          return_value=None), \
             patch.object(MediaSourceFactory, "create", return_value=fake):
            MediaSourceFactory.clear_media_probe_cache()
            result = MediaSourceFactory._probe_video_metadata_uncached(
                str(media), None)
        assert result.reason_code == "FULL_PROBE_FAILED"
        assert fake.close.call_count == 1, "异常路径必须关闭已创建的源"

    def test_source_closed_when_probe_raises(self, tmp_path: Path) -> None:
        media = tmp_path / "bad2.mov"
        media.write_bytes(b"garbage")

        fake = MagicMock()
        fake.probe.side_effect = RuntimeError("probe boom")

        with patch.object(MediaSourceFactory, "_probe_video_metadata_ffmpeg",
                          return_value=None), \
             patch.object(MediaSourceFactory, "create", return_value=fake):
            MediaSourceFactory.clear_media_probe_cache()
            result = MediaSourceFactory._probe_video_metadata_uncached(
                str(media), None)
        assert result.reason_code == "FULL_PROBE_FAILED"
        assert fake.close.call_count == 1


class TestAud34601AutoOpenCleanup:
    def test_failed_open_closes_source_before_next_backend(
        self, tmp_path: Path,
    ) -> None:
        media = tmp_path / "video.mov"
        media.write_bytes(b"garbage")

        bad, good = MagicMock(), MagicMock()
        bad.open.side_effect = MediaOpenError("open failed")

        def fake_create_with_backend(backend_name, path, target_size, preserve_planes=False, no_deblock=False):
            return bad if backend_name == "bad_backend" else good

        with patch.object(MediaSourceFactory, "_get_backend_order",
                          return_value=["bad_backend", "good_backend"]), \
             patch.object(MediaSourceFactory, "_create_with_backend",
                          side_effect=fake_create_with_backend):
            source = MediaSourceFactory.create(str(media), auto_open=True)
        assert source is good
        assert bad.close.call_count == 1, "open 失败后必须 best-effort close"
        good.close.assert_not_called()

    def test_all_backends_fail_closes_each(self, tmp_path: Path) -> None:
        from src.shared.media.source import MediaOpenError as _MOE

        media = tmp_path / "video2.mov"
        media.write_bytes(b"garbage")
        bad1, bad2 = MagicMock(), MagicMock()
        bad1.open.side_effect = _MOE("open failed 1")
        bad2.open.side_effect = _MOE("open failed 2")

        def fake_create_with_backend(backend_name, path, target_size, preserve_planes=False, no_deblock=False):
            return bad1 if backend_name == "b1" else bad2

        with patch.object(MediaSourceFactory, "_get_backend_order",
                          return_value=["b1", "b2"]), \
             patch.object(MediaSourceFactory, "_create_with_backend",
                          side_effect=fake_create_with_backend):
            with pytest.raises(_MOE):
                MediaSourceFactory.create(str(media), auto_open=True)
        assert bad1.close.call_count == 1
        assert bad2.close.call_count == 1


class TestAud34602BoundedCaches:
    def test_cache_put_evicts_oldest(self) -> None:
        cache: dict[str, int] = {}
        old_max = MediaSourceFactory._CACHE_MAX_ENTRIES
        MediaSourceFactory._CACHE_MAX_ENTRIES = 4
        try:
            for i in range(10):
                MediaSourceFactory._cache_put(cache, f"p{i}", i)
            assert len(cache) == 4
            assert "p0" not in cache and "p5" not in cache
            assert set(cache) == {"p6", "p7", "p8", "p9"}
        finally:
            MediaSourceFactory._CACHE_MAX_ENTRIES = old_max

    def test_probe_cache_lru_eviction(self) -> None:
        old_max = MediaSourceFactory._CACHE_MAX_ENTRIES
        MediaSourceFactory._CACHE_MAX_ENTRIES = 3
        MediaSourceFactory.clear_media_probe_cache()
        calls: list[str] = []

        def fake_uncached(path: str, sig):
            calls.append(path)
            from src.shared.media.factory import MediaProbeResult
            return MediaProbeResult(
                path=path, signature=sig, metadata=None, backend="none")

        # 每个路径的签名保持稳定（签名变化的失效语义由别的路径覆盖，
        # 此处专测 LRU 淘汰）
        def fake_sig(path: str):
            return (1, abs(hash(path)) % 100000)

        try:
            with patch.object(MediaSourceFactory, "_probe_video_metadata_uncached",
                              side_effect=fake_uncached), \
                 patch("src.shared.media.factory._file_signature",
                       side_effect=fake_sig):
                for p in ["p1", "p2", "p3"]:
                    MediaSourceFactory.probe_video_metadata_cached(p)
                MediaSourceFactory.probe_video_metadata_cached("p1")  # 刷新 p1
                MediaSourceFactory.probe_video_metadata_cached("p4")  # 淘汰 p2
                # p1 命中缓存（不重新探测），p2 被淘汰（重新探测）
                MediaSourceFactory.probe_video_metadata_cached("p1")
                MediaSourceFactory.probe_video_metadata_cached("p2")
            assert calls.count("p1") == 1
            assert calls.count("p2") == 2
            assert len(MediaSourceFactory._PROBE_RESULT_CACHE) == 3
        finally:
            MediaSourceFactory._CACHE_MAX_ENTRIES = old_max
            MediaSourceFactory.clear_media_probe_cache()

    def test_native_failure_log_bounded(self) -> None:
        old_max = MediaSourceFactory._CACHE_MAX_ENTRIES
        MediaSourceFactory._CACHE_MAX_ENTRIES = 4
        MediaSourceFactory.clear_media_probe_cache()
        try:
            with patch.object(MediaSourceFactory, "_probe_video_metadata_ffmpeg",
                              return_value=None):
                for i in range(10):
                    MediaSourceFactory._topos_native_failure(
                        f"/p{i}", (1, i), "TOPOS_FILE_OPEN_FAILED", "d")
            assert len(MediaSourceFactory._NATIVE_FAILURE_LOGGED) == 4
            assert isinstance(MediaSourceFactory._NATIVE_FAILURE_LOGGED, dict)
        finally:
            MediaSourceFactory._CACHE_MAX_ENTRIES = old_max
            MediaSourceFactory.clear_media_probe_cache()
