"""WD17-G93 修复验证：media 尾部 + Windows D3D11 硬解前半（16 条审计发现）。

覆盖（每条含正例与反例；D3D11 路径全部 mock/monkeypatch，本机 macOS
无法实机验证 Windows 分支）：
- AUD-35712  thumbnail_provider 裸 except 收窄为具体类型（OSError 等）
- AUD-35713  _get_cache_path 取消每请求 O(变体×文件) 全列扫描
- AUD-35800  Timecode.__hash__ 与 __eq__ 同源（哈希/相等契约）
- AUD-35804  Timeline.with_track_updated 对未知 track_id 显式报错
- AUD-35805  NaN speed/opacity 被 __post_init__ 拒绝
- AUD-35806  速度帧映射浮点截断吸附（0.3 步长 off-by-one）
- AUD-35809  序列 hold 模式"文件消失保留前驱帧"分支可达
- AUD-35813  预留/未知 pixel_format(>=4) + Alpha 在 open 拒绝、不再误标
- AUD-35910  darwin 零拷贝判定由进程内 IOSurface 映射探测背书
- AUD-36000  GetImmediateContext vtable 槽位 27 → 40（SDK d3d11.h 实序）
- AUD-36001  probe 自建 D3D11Device 全部 return 路径释放
- AUD-36002  from_native 缺必需键抛 ValueError（decode_next 可捕获）
- AUD-36003  close() 状态清理先行 + close_decoder 异常防护
- AUD-36100  done-callback 异常不逃逸、run() 有界等待
- AUD-36101  render 失败但 target 已分配时释放 target lease
- AUD-36102  遥测语义修正：render_waits/encode_waits 真实排队、池超时独立
"""

from __future__ import annotations

import ctypes
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.shared.media import FPS_29_97, Rational
from src.shared.media.timecode import Timecode, TimecodeType
from src.shared.media.timeline import (
    MAX_SPEED,
    MIN_SPEED,
    Timeline,
    TimelineClip,
    TimelineTrack,
)
from src.shared.media.topos_image_source import _pixel_format_name

REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# AUD-35712 / AUD-35713：thumbnail_provider
# ---------------------------------------------------------------------------

THUMBNAIL_SOURCE = REPO / "src/shared/media/thumbnail_provider.py"


class TestAud35712NoBareExcept:
    def test_source_has_no_bare_except(self) -> None:
        text = THUMBNAIL_SOURCE.read_text(encoding="utf-8")
        bare = [
            lineno
            for lineno, line in enumerate(text.splitlines(), start=1)
            if re.match(r"^\s*except\s*:", line)
        ]
        assert bare == [], f"thumbnail_provider.py 仍存在裸 except: {bare}"

    def test_write_probe_oserror_falls_back_to_platform_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.config.config_manager import ConfigManager
        from src.shared.media.thumbnail_provider import ThumbnailRequest

        platform_dir = tmp_path / "platform_cache"
        monkeypatch.setattr(
            ConfigManager,
            "get_platform_cache_dir",
            classmethod(lambda cls: str(platform_dir)),
        )
        src_dir = tmp_path / "media"
        src_dir.mkdir()
        source = src_dir / "clip.mp4"
        source.write_bytes(b"x")

        # 探针写测试文件成功但 remove 失败（OSError）→ 必须走平台目录回退
        real_remove = __import__("os").remove

        def failing_remove(path: str) -> None:
            if str(path).endswith(".write_test"):
                raise OSError("simulated remove failure")
            real_remove(path)

        monkeypatch.setattr("os.remove", failing_remove)
        request = ThumbnailRequest(str(source), None, 64)
        result = request._get_cache_path(str(source))
        assert str(platform_dir) in result
        assert ".cache" not in result

    def test_non_oserror_from_probe_is_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """收窄后的 except OSError 不得把任意异常都当"目录不可写"。"""
        from src.shared.media.thumbnail_provider import ThumbnailRequest

        source = tmp_path / "clip.mp4"
        source.write_bytes(b"x")

        def exploding_makedirs(path, exist_ok=False):  # type: ignore[no-untyped-def]
            raise TypeError("not an OSError")

        monkeypatch.setattr("os.makedirs", exploding_makedirs)
        request = ThumbnailRequest(str(source), None, 64)
        with pytest.raises(TypeError):
            request._get_cache_path(str(source))


class TestAud35713NoPerRequestDirectoryScan:
    def _make_request(self, source: Path) -> "object":
        from src.shared.media.thumbnail_provider import ThumbnailRequest

        return ThumbnailRequest(str(source), None, 64)

    @pytest.fixture()
    def isolate_global_cache(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from src.shared.media import thumbnail_provider as tp

        monkeypatch.setattr(
            tp, "_global_thumbnail_cache_dir", lambda: str(tmp_path / "no_global")
        )

    def test_exact_name_hit_never_lists_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolate_global_cache: None
    ) -> None:
        cache_dir = tmp_path / "media" / ".cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "photo.jpg_lg.webp").write_bytes(b"thumb")
        source = tmp_path / "media" / "photo.jpg"
        source.write_bytes(b"x")

        listdir_calls: list[str] = []
        real_listdir = __import__("os").listdir

        def counting_listdir(path: str) -> list[str]:
            listdir_calls.append(path)
            return real_listdir(path)

        monkeypatch.setattr("os.listdir", counting_listdir)
        request = self._make_request(source)
        result = request._get_cache_path(str(source))
        assert result == str(cache_dir / "photo.jpg_lg.webp")
        # 快路径：精确名直接 stat 命中，零目录扫描
        assert listdir_calls == []

    def test_nfd_named_cache_file_found_via_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolate_global_cache: None
    ) -> None:
        import unicodedata

        from src.shared.media.thumbnail_provider import _nfc_index_for_dir

        cache_dir = tmp_path / "media" / ".cache"
        cache_dir.mkdir(parents=True)
        # 用 NFD 形式落盘文件名（macOS 真实场景）；APFS 大小写/规范化
        # 不敏感，_get_cache_path 的快慢路径都可能直接命中——索引本身的
        # NFC 兼容能力直接对 _nfc_index_for_dir 验证
        nfd_name = unicodedata.normalize("NFD", "café.jpg.webp")
        assert unicodedata.normalize("NFC", nfd_name) != nfd_name
        (cache_dir / nfd_name).write_bytes(b"thumb")

        index = _nfc_index_for_dir(str(cache_dir))
        assert index[unicodedata.normalize("NFC", "café.jpg.webp")] == nfd_name

        source = tmp_path / "media" / "café.jpg"
        source.write_bytes(b"x")
        request = self._make_request(source)
        result = request._get_cache_path(str(source))
        # 无论快路径（APFS 规范化不敏感直接命中）还是索引路径，都必须
        # 指向真实存在的文件
        assert Path(result).is_file()

    def test_index_reused_across_requests_for_same_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolate_global_cache: None
    ) -> None:
        source = tmp_path / "media" / "miss.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"x")

        listdir_calls: list[str] = []
        real_listdir = __import__("os").listdir

        def counting_listdir(path: str) -> list[str]:
            listdir_calls.append(path)
            return real_listdir(path)

        monkeypatch.setattr("os.listdir", counting_listdir)
        request = self._make_request(source)
        request._get_cache_path(str(source))  # 首次：建目录 + 建索引（1 次扫描）
        first_scan_count = len(listdir_calls)
        assert first_scan_count == 1
        listdir_calls.clear()
        request._get_cache_path(str(source))  # 二次：目录指纹未变 → 索引复用
        assert listdir_calls == []

    def test_index_invalidated_when_directory_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolate_global_cache: None
    ) -> None:
        source = tmp_path / "media" / "grow.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"x")
        request = self._make_request(source)
        first = request._get_cache_path(str(source))
        # 新增缓存文件后（目录 mtime 变化）必须能立刻命中
        (source.parent / ".cache" / "grow.jpg.webp").write_bytes(b"t")
        second = request._get_cache_path(str(source))
        assert first != second
        assert second.endswith("grow.jpg.webp")


# ---------------------------------------------------------------------------
# AUD-35800：Timecode 哈希/相等契约
# ---------------------------------------------------------------------------


class TestAud35800HashEqContract:
    def test_equal_timecodes_have_equal_hash(self) -> None:
        ndf = Timecode(0, 0, 0, 5, Rational(30, 1), TimecodeType.NON_DROP_FRAME)
        df = Timecode(0, 0, 0, 5, Rational(30, 1), TimecodeType.DROP_FRAME)
        # __eq__ 只按帧号+帧率（与 __lt__/__sub__ 一致）→ 哈希必须同源
        assert ndf == df
        assert hash(ndf) == hash(df)

    def test_set_lookup_consistent(self) -> None:
        a = Timecode(0, 0, 0, 5, Rational(30, 1), TimecodeType.NON_DROP_FRAME)
        b = Timecode(0, 0, 0, 5, Rational(30, 1), TimecodeType.DROP_FRAME)
        table = {a: "v"}
        assert table[b] == "v"

    def test_different_frames_differ(self) -> None:
        a = Timecode(0, 0, 0, 5, Rational(30, 1))
        b = Timecode(0, 0, 0, 6, Rational(30, 1))
        assert a != b

    def test_same_type_instances_collapse_in_set(self) -> None:
        a = Timecode.from_string("01:00:00;00", FPS_29_97)
        b = Timecode.from_frame_number(107892, FPS_29_97, TimecodeType.DROP_FRAME)
        assert a == b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1


# ---------------------------------------------------------------------------
# AUD-35804 / AUD-35805 / AUD-35806：timeline
# ---------------------------------------------------------------------------


class TestAud35804TrackUpdateExplicitError:
    def test_unknown_track_raises(self) -> None:
        timeline = Timeline(tracks=(TimelineTrack(track_id="v1"),))
        ghost = TimelineTrack(track_id="missing", name="ghost")
        with pytest.raises(ValueError, match="Track not found: missing"):
            timeline.with_track_updated(ghost)

    def test_existing_track_still_updates(self) -> None:
        track = TimelineTrack(track_id="v1", name="before")
        timeline = Timeline(tracks=(track,))
        updated = timeline.with_track_updated(
            TimelineTrack(track_id="v1", name="after")
        )
        assert updated.get_track_by_id("v1").name == "after"


class TestAud35805NanRejected:
    def test_nan_speed_rejected(self) -> None:
        with pytest.raises(ValueError, match="speed"):
            TimelineClip(
                source_start=0, source_end=9,
                timeline_start=0, timeline_end=10,
                speed=float("nan"),
            )

    def test_nan_opacity_rejected(self) -> None:
        with pytest.raises(ValueError, match="opacity"):
            TimelineTrack(track_id="t", opacity=float("nan"))

    def test_infinite_values_rejected(self) -> None:
        with pytest.raises(ValueError, match="speed"):
            TimelineClip(
                source_start=0, source_end=9,
                timeline_start=0, timeline_end=10,
                speed=float("inf"),
            )
        with pytest.raises(ValueError, match="opacity"):
            TimelineTrack(track_id="t", opacity=float("-inf"))

    def test_boundaries_still_accepted(self) -> None:
        clip = TimelineClip(
            source_start=0, source_end=9,
            timeline_start=0, timeline_end=10,
            speed=MIN_SPEED,
        )
        assert clip.speed == MIN_SPEED
        track = TimelineTrack(track_id="t", opacity=0.0)
        assert track.opacity == 0.0
        track2 = TimelineTrack(track_id="t2", opacity=1.0)
        assert track2.opacity == 1.0


class TestAud35806SpeedFloatRounding:
    def test_timeline_to_source_frame_03_step(self) -> None:
        clip = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
            speed=0.3,
        )
        # 10 * 0.3 == 2.9999999999999996（二进制浮点）→ 必须是 3 而非 2
        assert clip.timeline_to_source_frame(10) == 3
        assert clip.timeline_to_source_frame(20) == 6

    def test_source_to_timeline_frame_03_step(self) -> None:
        clip = TimelineClip(
            source_start=0, source_end=99,
            timeline_start=0, timeline_end=100,
            speed=0.3,
        )
        # 3 / 0.3 == 9.999999999999998 → 必须是 10 而非 9
        assert clip.source_to_timeline_frame(3) == 10

    def test_effective_duration_03_step(self) -> None:
        clip = TimelineClip(
            source_start=0, source_end=2,  # 3 帧
            timeline_start=0, timeline_end=10,
            speed=0.3,
        )
        assert clip.effective_duration == 10

    def test_with_speed_and_trim_consistent(self) -> None:
        clip = TimelineClip(
            source_start=0, source_end=2,
            timeline_start=0, timeline_end=10,
            speed=1.0,
        )
        assert clip.with_speed(0.3).duration_frames == 10
        assert clip.with_speed(0.3).timeline_end == 10
        trimmed = TimelineClip(
            source_start=0, source_end=11,  # 12 帧
            timeline_start=0, timeline_end=12,
            speed=1.0,
        )
        assert trimmed.with_speed(0.4).duration_frames == 30  # 12/0.4

    def test_floor_semantics_preserved_for_true_fractions(self) -> None:
        clip = TimelineClip(
            source_start=100, source_end=199,
            timeline_start=0, timeline_end=50,
            speed=2.0,
        )
        # (199-100)/2 == 49.5 → 仍按 floor 截断为 49（既有契约）
        assert clip.source_to_timeline_frame(199) == 49
        assert clip.timeline_to_source_frame(25) == 150


# ---------------------------------------------------------------------------
# AUD-35809：序列 hold 模式前驱分支可达
# ---------------------------------------------------------------------------


class _StubCodec:
    """解码桩：probe/decode 恒定返回，不需要真实 .toos 文件。"""

    def __init__(self) -> None:
        self.decoded_paths: list[str] = []

    def probe(self, path: str) -> SimpleNamespace:
        return SimpleNamespace(sample_kind=0)

    def decode(self, path: str, target_size=None):  # type: ignore[no-untyped-def]
        self.decoded_paths.append(path)
        info = SimpleNamespace(has_alpha=False, alpha_bit_depth=0)
        return [np.zeros((2, 2), dtype=np.uint16)], info, {}


def _make_sequence(paths_by_frame: dict[int, str], real_frames: set[int],
                   limit: int = 8):
    """白盒构造 ToposImageSequenceSource（绕过 open 的真实解码管线）。

    real_frames 中的帧路径真实存在（stat 指纹可用）；其余槽位在索引中
    但文件已消失（size/mtime 为扫描态残留值）。
    """
    from src.shared.media.topos_image_sequence import (
        FrameSlot,
        MissingFramePolicy,
        PlaneCache,
        SequenceOptions,
        ToposImageSequenceSource,
    )

    slots = []
    for frame, path in sorted(paths_by_frame.items()):
        if frame in real_frames:
            st = __import__("os").stat(path)
            slots.append(FrameSlot(frame, path, st.st_size, st.st_mtime_ns))
        else:
            slots.append(FrameSlot(frame, path, 1, 1))
    src = ToposImageSequenceSource.__new__(ToposImageSequenceSource)
    src._options = SequenceOptions(
        missing=MissingFramePolicy(mode="hold", max_consecutive_missing=limit)
    )
    src._by_frame = {s.frame: s for s in slots}
    src._frames_sorted = sorted(src._by_frame)
    src._cache = PlaneCache(1 << 20)
    src._codec = _StubCodec()
    src._decode_token = ("stub", "full")
    src._prefetch_lock = threading.Lock()
    src._generation = 0
    src._closed = False
    src._open = True
    return src


class TestAud35809HoldPredecessorReachable:
    def test_disappeared_file_holds_predecessor(self, tmp_path: Path) -> None:
        alive = tmp_path / "f0000.toos"
        alive.write_bytes(b"real")
        gone = tmp_path / "f0001.toos"
        src = _make_sequence(
            {0: str(alive), 1: str(gone)}, real_frames={0}
        )
        planes, slot = src.read_slot(1)
        # 文件 1 消失 → hold 保留前驱帧 0（旧实现该分支不可达，直接报错）
        assert slot.frame == 0
        assert src._codec.decoded_paths == [str(alive)]

    def test_chained_disappearance_walks_back(self, tmp_path: Path) -> None:
        alive = tmp_path / "f0000.toos"
        alive.write_bytes(b"real")
        paths = {0: str(alive)}
        for frame in (1, 2):
            paths[frame] = str(tmp_path / f"f{frame:04d}.toos")
        src = _make_sequence(paths, real_frames={0})
        planes, slot = src.read_slot(2)
        assert slot.frame == 0

    def test_hold_limit_still_enforced(self, tmp_path: Path) -> None:
        alive = tmp_path / "f0000.toos"
        alive.write_bytes(b"real")
        paths = {0: str(alive), 20: str(tmp_path / "f0020.toos")}
        src = _make_sequence(paths, real_frames={0}, limit=8)
        with pytest.raises(FileNotFoundError):
            src.read_slot(20)

    def test_first_frame_disappeared_no_predecessor(self, tmp_path: Path) -> None:
        src = _make_sequence(
            {0: str(tmp_path / "f0000.toos")}, real_frames=set()
        )
        with pytest.raises(FileNotFoundError):
            src.read_slot(0)


# ---------------------------------------------------------------------------
# AUD-35813：预留/未知 pixel_format + Alpha
# ---------------------------------------------------------------------------


class _FakeProbeInfo:
    """probe() 结果桩：覆盖 open() 读取的全部 IDSC 字段。"""

    def __init__(self, pixel_format: int) -> None:
        self.pixel_format = pixel_format
        self.bit_depth = 10
        self.color_range = 1
        self.color_primaries = 1
        self.color_transfer = 1
        self.color_matrix = 1
        self.iccp_ref = 0
        self.ocio_ref = 0
        self.has_alpha = True
        self.alpha_bit_depth = 8
        self.sample_kind = 0
        self.image_profile = 1
        self.visible_width = 8
        self.visible_height = 8
        self.pixel_aspect = (1, 1)


class _FakeToosCodec:
    def __init__(self, pixel_format: int) -> None:
        self._info = _FakeProbeInfo(pixel_format)

    def probe(self, path: str) -> _FakeProbeInfo:
        return self._info

    def read_metadata(self, path: str, chunk: int) -> bytes:
        return b""

    def read_export_meta(self, path: str) -> dict:
        return {}


class TestAud35813ReservedPixelFormatWithAlpha:
    def test_pixel_format_name_rejects_unknown_with_alpha(self) -> None:
        with pytest.raises(KeyError):
            _pixel_format_name(4, 10, 8)  # 预留值
        with pytest.raises(KeyError):
            _pixel_format_name(7, 12, 8)  # 未知值
        with pytest.raises(KeyError):
            _pixel_format_name(3, 12, 8)  # CFA 与 alpha 互斥

    def test_pixel_format_name_valid_alpha_combos_unaffected(self) -> None:
        assert _pixel_format_name(0, 10, 8) == "topos_yuva422p10a8"
        assert _pixel_format_name(1, 12, 8) == "topos_yuva444p12a8"
        assert _pixel_format_name(2, 10, 8) == "topos_gbrap10a8"

    def test_open_rejects_reserved_pf_with_alpha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media import topos_image_source as tis
        from src.shared.media.source import MediaOpenError
        from src.shared.media.topos_image_source import ToposImageSource

        # 色彩语义解析与本项无关——桩掉以聚焦 pixel_format 校验
        monkeypatch.setattr(
            tis, "resolve_topos_color_semantics", lambda *a, **k: object()
        )

        target = tmp_path / "handmade.toos"
        target.write_bytes(b"x")
        for pf in (4, 5, 99):
            source = ToposImageSource(str(target), codec=_FakeToosCodec(pf))  # type: ignore[arg-type]
            with pytest.raises(MediaOpenError, match="不支持的 .toos 组合"):
                source.open()

    def test_open_accepts_valid_pf_with_alpha(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media import topos_image_source as tis
        from src.shared.media.topos_image_source import ToposImageSource

        monkeypatch.setattr(
            tis, "resolve_topos_color_semantics", lambda *a, **k: object()
        )

        target = tmp_path / "valid.toos"
        target.write_bytes(b"x")
        source = ToposImageSource(str(target), codec=_FakeToosCodec(1))  # type: ignore[arg-type]
        source.open()
        assert source._metadata.pixel_format == "topos_yuva444p10a8"


# ---------------------------------------------------------------------------
# AUD-35910：darwin 零拷贝判定需进程内探测背书
# ---------------------------------------------------------------------------


class TestAud35910DarwinZeroCopyNeedsProbe:
    def test_negative_probe_falls_back_to_cpu(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media import topos_surface_capability as cap_module
        from src.shared.media.topos_surface_capability import ToposSurfaceBackend

        monkeypatch.setattr(
            cap_module, "_probe_macos_iosurface",
            lambda: (False, "iosurface_map_probe_error:boom"),
        )
        cap = cap_module.detect_topos_surface_capability(system="Darwin", env={})
        assert cap.backend is ToposSurfaceBackend.CPU
        assert cap.zero_copy is False
        assert cap.reason.startswith("iosurface_mapping_unavailable:")
        assert "boom" in cap.reason

    def test_positive_probe_claims_zero_copy_with_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media import topos_surface_capability as cap_module
        from src.shared.media.topos_surface_capability import ToposSurfaceBackend

        monkeypatch.setattr(
            cap_module, "_probe_macos_iosurface", lambda: (True, "")
        )
        cap = cap_module.detect_topos_surface_capability(system="Darwin", env={})
        assert cap.backend is ToposSurfaceBackend.MACOS_IOSURFACE
        assert cap.zero_copy is True
        assert cap.evidence == "probe"

    def test_module_import_failure_is_negative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media import topos_surface_capability as cap_module

        monkeypatch.setitem(
            __import__("sys").modules,
            "src.shared.media.macos_vt.topos_surface", None,
        )
        mapped, reason = cap_module._probe_macos_iosurface()
        assert mapped is False
        assert reason.startswith("iosurface_module_unavailable")

    def test_real_probe_maps_and_unmaps_iosurface(self) -> None:
        """真机集成：探测必须真实持有过映射（仅 macOS 执行）。"""
        import sys

        if sys.platform != "darwin":
            pytest.skip("仅 macOS 实机验证 IOSurface 探测")
        from src.shared.media.topos_surface_capability import (
            _probe_macos_iosurface,
        )

        mapped, reason = _probe_macos_iosurface()
        assert mapped is True, reason


# ---------------------------------------------------------------------------
# AUD-36000：GetImmediateContext vtable 槽位
# ---------------------------------------------------------------------------


class TestAud36000VtableSlotGetImmediateContext:
    def test_slot_value_matches_sdk_ordering(self) -> None:
        from src.shared.media.windows_hw.d3d11_device import (
            VTABLE_SLOT_CHECK_FORMAT_SUPPORT,
            VTABLE_SLOT_CREATE_TEXTURE2D,
            VTABLE_SLOT_GET_FEATURE_LEVEL,
            VTABLE_SLOT_GET_IMMEDIATE_CONTEXT,
        )

        # SDK d3d11.h ID3D11Device 实序：CreateTexture2D=[5]、
        # CreateDeferredContext=[27]、CheckFormatSupport=[29]、
        # GetFeatureLevel=[37]、GetImmediateContext=[40]
        assert VTABLE_SLOT_CREATE_TEXTURE2D == 5
        assert VTABLE_SLOT_CHECK_FORMAT_SUPPORT == 29
        assert VTABLE_SLOT_GET_FEATURE_LEVEL == 37
        assert VTABLE_SLOT_GET_IMMEDIATE_CONTEXT == 40  # 旧值 27 = CreateDeferredContext

    def test_vtable_fn_resolves_slot_40_not_27(self) -> None:
        """合成 vtable 验证：槽 40 取到的是 GetImmediateContext 桩。"""
        from src.shared.media.windows_hw.d3d11_device import (
            VTABLE_SLOT_GET_IMMEDIATE_CONTEXT,
            vtable_fn,
        )

        # W01：win64 下 COM 指针 > 32 位——无参 proto 会让 ctypes 把
        # 实参按 c_int 转换直接 OverflowError。proto 声明 c_void_p 入参。
        proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
        get_immediate = proto(lambda _ptr: 4040)
        create_deferred = proto(lambda _ptr: 2727)
        vtbl = (ctypes.c_void_p * 48)()
        vtbl[27] = ctypes.cast(create_deferred, ctypes.c_void_p)
        vtbl[40] = ctypes.cast(get_immediate, ctypes.c_void_p)
        com_object = ctypes.c_void_p(ctypes.addressof(vtbl))
        fn = vtable_fn(
            ctypes.addressof(com_object), VTABLE_SLOT_GET_IMMEDIATE_CONTEXT, proto
        )
        assert fn(ctypes.addressof(com_object)) == 4040  # 不是 2727


# ---------------------------------------------------------------------------
# AUD-36001：probe 自建设备释放
# ---------------------------------------------------------------------------


class _FakeProbeDevice:
    def __init__(self) -> None:
        self.identity = SimpleNamespace(adapter_luid="LUID-X", feature_level=0xB000)
        self.is_released = False
        self.release_count = 0

    def release(self) -> None:
        if self.is_released:
            raise ValueError("重复 release")
        self.is_released = True
        self.release_count += 1


def _fake_loaded(available: bool, caps: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        available=available, capabilities=caps, bridge=None,
        reason="" if available else "bridge_missing",
    )


class TestAud36001ProbeDeviceReleased:
    def _patch_windows(self, monkeypatch: pytest.MonkeyPatch) -> object:
        from src.shared.media.windows_hw import d3d11_export_capability as mod

        monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
        return mod

    def test_device_released_on_bridge_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._patch_windows(monkeypatch)
        device = _FakeProbeDevice()
        monkeypatch.setattr(mod, "create_d3d11_device", lambda: (device, ""))
        monkeypatch.setattr(
            mod, "load_windows_native_bridge",
            lambda module=None: _fake_loaded(False),
        )
        cap = mod.probe_d3d11_export_capability()
        assert cap.ready is False
        assert device.is_released is True
        assert device.release_count == 1

    def test_device_released_on_success_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._patch_windows(monkeypatch)
        device = _FakeProbeDevice()
        monkeypatch.setattr(mod, "create_d3d11_device", lambda: (device, ""))
        caps = SimpleNamespace(
            decode_codecs=("h264",), encode_kinds=("hevc",),
            d3d11_renderer=True, version="test",
        )
        monkeypatch.setattr(
            mod, "load_windows_native_bridge",
            lambda module=None: _fake_loaded(True, caps),
        )
        t0 = mod.D3D11T0Evidence(
            adapter_luid="LUID-X", frame_count=3, output_playable=True,
            decode_gpu_ms=0.1, render_gpu_ms=0.1, yuv_gpu_ms=0.1,
            encode_submit_gpu_ms=0.1,
        )
        cap = mod.probe_d3d11_export_capability(t0_evidence=t0)
        assert cap.ready is True
        assert device.is_released is True  # 成功路径同样释放
        assert device.release_count == 1

    def test_injected_device_not_released_by_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._patch_windows(monkeypatch)
        device = _FakeProbeDevice()
        monkeypatch.setattr(
            mod, "load_windows_native_bridge",
            lambda module=None: _fake_loaded(False),
        )
        mod.probe_d3d11_export_capability(device=device)
        assert device.is_released is False  # 调用方自持设备的生命周期不归探测管

    def test_device_released_when_inner_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._patch_windows(monkeypatch)
        device = _FakeProbeDevice()
        monkeypatch.setattr(mod, "create_d3d11_device", lambda: (device, ""))

        def exploding_loader(module=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("bridge exploded")

        monkeypatch.setattr(mod, "load_windows_native_bridge", exploding_loader)
        with pytest.raises(RuntimeError):
            mod.probe_d3d11_export_capability()
        assert device.is_released is True  # 异常路径也必须释放


# ---------------------------------------------------------------------------
# AUD-36002 / AUD-36003：decoder
# ---------------------------------------------------------------------------


class _FakeDecodeBridge:
    def __init__(self, next_result: object) -> None:
        self._next_result = next_result
        self.close_calls: list[object] = []
        self.close_error: Exception | None = None

    def capabilities(self) -> SimpleNamespace:
        return SimpleNamespace(decode_codecs=("h264",))

    def open_decoder(self, path: str, codec: str, device_ptr: int) -> object:
        return object()

    def decode_next(self, session: object) -> object:
        return self._next_result

    def close_decoder(self, session: object) -> None:
        self.close_calls.append(session)
        if self.close_error is not None:
            raise self.close_error


class TestAud36002FromNativeMissingKeys:
    def test_mapping_missing_required_key_raises_value_error(self) -> None:
        from src.shared.media.windows_hw.d3d11_decoder import D3D11DecodedTexture

        with pytest.raises(ValueError, match="缺少必需字段"):
            D3D11DecodedTexture.from_native(
                {"texture_handle": 1, "width": 8}  # height 缺失
            )

    def test_valid_mapping_still_accepted(self) -> None:
        from src.shared.media.windows_hw.d3d11_decoder import D3D11DecodedTexture

        tex = D3D11DecodedTexture.from_native({
            "texture_handle": 1, "width": 8, "height": 4,
            "adapter_luid": "LUID-X",
        })
        assert tex.width == 8 and tex.height == 4

    def test_decode_next_returns_none_on_missing_key(self) -> None:
        from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder

        bridge = _FakeDecodeBridge({"texture_handle": 1, "width": 8})
        decoder = D3D11Decoder(bridge=bridge)  # type: ignore[arg-type]
        decoder._session = object()
        decoder._is_open = True
        # KeyError 不允许逃逸到调用方（旧实现会炸出 KeyError）
        assert decoder.decode_next() is None
        assert "decode_contract" in decoder.last_fallback_reason


class TestAud36003CloseProtection:
    def test_close_survives_native_error_and_resets_state(self) -> None:
        from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder

        bridge = _FakeDecodeBridge(None)
        bridge.close_error = RuntimeError("native close failed")
        decoder = D3D11Decoder(bridge=bridge)  # type: ignore[arg-type]
        session = object()
        decoder._session = session
        decoder._is_open = True
        decoder.close()  # 不得抛出
        assert decoder.is_open is False
        assert decoder._session is None
        assert bridge.close_calls == [session]

    def test_open_reuse_after_failing_close(self, tmp_path: Path) -> None:
        from src.shared.media.windows_hw.d3d11_decoder import D3D11Decoder

        bridge = _FakeDecodeBridge(None)
        bridge.close_error = TypeError("garbled handle")
        decoder = D3D11Decoder(bridge=bridge)  # type: ignore[arg-type]
        decoder._session = object()
        decoder._is_open = True
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"x")
        # open() 首行调用 close()——此前会带着原生异常炸出，污染复用路径
        assert decoder.open(str(source), codec="h264") is True
        assert decoder.is_open is True


# ---------------------------------------------------------------------------
# AUD-36100 / AUD-36101 / AUD-36102：pipeline
# ---------------------------------------------------------------------------


def _make_pools():
    from src.shared.media.windows_hw.d3d11_texture_pool import D3D11TexturePool

    handles = iter(range(101, 200))
    frame_pool = D3D11TexturePool(
        depth=8, width=64, height=64, dxgi_format=103,
        texture_factory=lambda: next(handles),
        texture_releaser=lambda _ptr: None,
    )
    target_handles = iter(range(201, 300))
    target_pool = D3D11TexturePool(
        depth=8, width=64, height=64, dxgi_format=103,
        texture_factory=lambda: next(target_handles),
        texture_releaser=lambda _ptr: None,
    )
    return frame_pool, target_pool


def _make_frame(pool, index: int = 0):
    from src.shared.media.windows_hw.d3d11_frame import D3D11GpuFrame

    lease = pool.acquire()
    assert lease is not None
    return D3D11GpuFrame(
        texture_ptr=lease.texture_ptr, dxgi_format=103, width=64, height=64,
        lease=lease, adapter_luid="luid", frame_index=index,
    )


def _make_target(target_pool, adapter_luid: str = "luid"):
    from src.shared.media.windows_hw.d3d11_render_graph import D3D11RenderTarget
    from src.shared.media.windows_hw.d3d11_sync import ImmediateD3D11Fence

    lease = target_pool.acquire()
    assert lease is not None
    return D3D11RenderTarget(
        texture_ptr=lease.texture_ptr, dxgi_format=103, width=64, height=64,
        adapter_luid=adapter_luid, lease=lease,
        completion_fence=ImmediateD3D11Fence(),
    )


def _ok_encode(target):  # type: ignore[no-untyped-def]
    from src.shared.media.windows_hw.d3d11_sync import ImmediateD3D11Fence
    from src.shared.media.windows_hw.native_bridge import NativeEncodeSubmitResult

    return NativeEncodeSubmitResult(
        accepted=True, gpu_submit_ms=0.1,
        completion_fence=ImmediateD3D11Fence(),
    )


class TestAud36100CallbackEscapeAndRunTimeout:
    def test_unexpected_render_exception_completes_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineScheduler

        frame_pool, target_pool = _make_pools()

        def broken_render(frame):  # type: ignore[no-untyped-def]
            raise AttributeError("renderer is broken")  # 不在精确类型清单内

        scheduler = D3D11PipelineScheduler(broken_render, _ok_encode)
        try:
            outcome = scheduler.submit(_make_frame(frame_pool))
            assert outcome.result(timeout=5) is False  # 不悬挂
            telemetry = scheduler.telemetry()
            assert any(
                "render_callback_unexpected" in r for r in telemetry.fallback_reasons
            )
            # 信号量槽必须已归还：新 scheduler 的同型帧可正常完成
            scheduler2 = D3D11PipelineScheduler(
                lambda frame: _ok_render_result(target_pool), _ok_encode
            )
            try:
                outcome2 = scheduler2.submit(_make_frame(frame_pool))
                assert outcome2.result(timeout=5) is True
            finally:
                scheduler2.close()
        finally:
            scheduler.close()
            frame_pool.close()
            target_pool.close()

    def test_run_has_bounded_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.shared.media.windows_hw import d3d11_pipeline as pipeline_module
        from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineScheduler

        monkeypatch.setattr(pipeline_module, "_OUTCOME_TIMEOUT_S", 0.1)
        frame_pool, target_pool = _make_pools()
        release_event = threading.Event()

        def slow_render(frame):  # type: ignore[no-untyped-def]
            release_event.wait(timeout=2.0)
            return _ok_render_result(target_pool)

        class _Source:
            def read_frame(self):  # type: ignore[no-untyped-def]
                return _make_frame(frame_pool), ""

        scheduler = D3D11PipelineScheduler(slow_render, _ok_encode)
        try:
            started = time.monotonic()
            completed, telemetry = scheduler.run(_Source(), 1)
            elapsed = time.monotonic() - started
            assert completed == 0
            assert elapsed < 2.0  # 超时返回而不是永久阻塞
            assert "pipeline_future_timeout" in telemetry.fallback_reasons
        finally:
            release_event.set()
            scheduler.close()
            frame_pool.close()
            target_pool.close()


def _ok_render_result(target_pool):  # type: ignore[no-untyped-def]
    from src.shared.media.windows_hw.d3d11_render_graph import D3D11RenderResult

    return D3D11RenderResult(
        _make_target(target_pool), gpu_render_ms=0.1, gpu_yuv_ms=0.1
    )


class TestAud36101FailedRenderReleasesTargetLease:
    def test_target_lease_released_when_render_fails_with_target(
        self,
    ) -> None:
        from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineScheduler
        from src.shared.media.windows_hw.d3d11_render_graph import D3D11RenderResult

        frame_pool, target_pool = _make_pools()

        def failing_render_with_target(frame):  # type: ignore[no-untyped-def]
            # target 已分配但渲染失败（reason 非空 → ok=False）
            return D3D11RenderResult(
                _make_target(target_pool), reason="shader_compile_failed"
            )

        scheduler = D3D11PipelineScheduler(failing_render_with_target, _ok_encode)
        try:
            outcome = scheduler.submit(_make_frame(frame_pool))
            assert outcome.result(timeout=5) is False
            # 渲染目标池的槽位必须被归还（lease.released=True）
            stats = target_pool.stats()
            assert stats.in_use == 0
            assert "shader_compile_failed" in scheduler.telemetry().fallback_reasons
        finally:
            scheduler.close()
            frame_pool.close()
            target_pool.close()


class TestAud36102TelemetrySemantics:
    def test_render_and_encode_queue_waits_counted(self) -> None:
        from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineScheduler

        frame_pool, target_pool = _make_pools()
        render_gate = threading.Event()

        def slow_first_render(frame):  # type: ignore[no-untyped-def]
            if not render_gate.is_set():
                time.sleep(0.08)
            return _ok_render_result(target_pool)

        scheduler = D3D11PipelineScheduler(slow_first_render, _ok_encode)
        try:
            outcomes = [
                scheduler.submit(_make_frame(frame_pool, i)) for i in range(3)
            ]
            for outcome in outcomes:
                assert outcome.result(timeout=5) is True
            telemetry = scheduler.telemetry()
            # 单工作线程执行器：后到任务在首个 80ms 渲染期间排队
            assert telemetry.render_waits >= 1
            assert telemetry.encode_waits >= 0  # encode 很快，可能无排队
            assert telemetry.surface_pool_timeouts == 0
        finally:
            render_gate.set()
            scheduler.close()
            frame_pool.close()
            target_pool.close()

    def test_surface_pool_timeout_has_own_counter(
        self,
    ) -> None:
        from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineScheduler

        frame_pool, target_pool = _make_pools()
        release_event = threading.Event()

        def blocked_render(frame):  # type: ignore[no-untyped-def]
            release_event.wait(timeout=5.0)
            return _ok_render_result(target_pool)

        scheduler = D3D11PipelineScheduler(blocked_render, _ok_encode)
        try:
            blocked_outcomes = [
                scheduler.submit(_make_frame(frame_pool, i), timeout_s=1.0)
                for i in range(4)  # 占满 surface_depth=4
            ]
            deadline = time.monotonic() + 5.0
            while (
                scheduler.telemetry().submitted < 4 and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            overflow = scheduler.submit(
                _make_frame(frame_pool, 99), timeout_s=0.05
            )
            assert overflow.result(timeout=1.0) is False
            telemetry = scheduler.telemetry()
            # AUD-36102：池超时记在独立计数，不再冒充 encode_waits
            assert telemetry.surface_pool_timeouts == 1
            assert telemetry.encode_waits == 0
            assert "surface_pool_timeout" in telemetry.fallback_reasons
        finally:
            release_event.set()
            scheduler.close()
            frame_pool.close()
            target_pool.close()

    def test_telemetry_dict_includes_new_field(self) -> None:
        from src.shared.media.windows_hw.d3d11_pipeline import D3D11PipelineTelemetry

        data = D3D11PipelineTelemetry(
            surface_depth=4, submitted=0, completed=0, failed=0,
            peak_in_flight=0, render_waits=0, encode_waits=0,
            fallback_reasons=(), surface_pool_timeouts=2,
        ).to_dict()
        assert data["surface_pool_timeouts"] == 2
