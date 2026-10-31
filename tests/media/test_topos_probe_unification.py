"""T-P0-10：TOPOS native 路由与 probe 单一化的聚焦测试。

总计划任务卡验证要求：
1. 真实 ``2K_topos_422_Proxy.mov``：metadata 只来自 ``ToposMediaSource.probe()``，
   不再走必定失败的 FFmpeg/PyAV probe（无 ``'VideoStream' object has no
   attribute 'width'`` 日志）；
2. 同一文件签名的重复探测只产生一次 native probe（共享缓存）；
3. 旧/不兼容 dylib → ``TOPOS_CODEC_INCOMPATIBLE``（不得回落为"媒体损坏"）；
   未设置 dylib → ``TOPOS_CODEC_LIB_NOT_FOUND``；
4. ``resolve_topos_codec_library`` 三态：正确 release / 不兼容 / 不存在；
5. timeline export 高位深直连路径对 TOPOS 分流（不再构造 FFmpegMediaSource）；
6. ``probe_media_metadata`` 持久化安全的 media identity（无绝对 dylib 路径）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.shared.codec.topos_binding import (
    LIB_RESOLUTION_INCOMPATIBLE,
    LIB_RESOLUTION_NOT_FOUND,
    LIB_RESOLUTION_OK,
    resolve_topos_codec_library,
)
from src.shared.media.factory import MediaSourceFactory

REPO_ROOT = Path(__file__).resolve().parents[2]
from tests.real_media import canonical_video, require_video  # noqa: E402

TOPOS_ASSET = REPO_ROOT / "tests/test_videos" / canonical_video("2K_topos_proxy.mov")
H264_ASSET = REPO_ROOT / "tests/test_videos/2K_h264.mp4"
RELEASE_DYLIB = REPO_ROOT / "native/topos_codec/build/release/topos_codec.0.1.0.dylib"


@pytest.fixture(scope="module", autouse=True)
def _require_real_media():
    """C02：素材缺失默认 skip；TOPOS_REAL_MEDIA_STRICT=1 的 real-media
    作业直接 fail，不允许整模块静默跳过。"""
    require_video(TOPOS_ASSET.name)


@pytest.fixture(autouse=True)
def _clean_caches():
    MediaSourceFactory.clear_media_probe_cache()
    yield
    MediaSourceFactory.clear_media_probe_cache()


@pytest.fixture()
def topos_asset() -> str:
    return str(TOPOS_ASSET)


# ---------------------------------------------------------------------------
# native-only 探测与共享缓存
# ---------------------------------------------------------------------------


class TestNativeOnlyProbe:
    def test_metadata_from_native_without_pyav_probe(
            self, topos_asset: str, monkeypatch: pytest.MonkeyPatch,
            caplog) -> None:
        """TOPOS 文件不得再走 PyAV probe（VideoStream.width 无效日志消除）。"""
        from src.shared.media import factory as factory_mod

        def _boom(path):
            raise AssertionError(
                f"TOPOS 文件不应触发 FFmpeg/PyAV probe: {path}")

        monkeypatch.setattr(
            MediaSourceFactory, "_probe_video_metadata_ffmpeg", _boom)
        with caplog.at_level(logging.DEBUG):
            result = MediaSourceFactory.probe_video_metadata_cached(topos_asset)
        assert result.backend == "topos_native"
        assert result.reason_code is None
        meta = result.metadata
        assert meta is not None
        assert (meta.width, meta.height) == (1920, 1080)
        assert meta.bit_depth == 10
        assert meta.pixel_format == "yuv422p10le"
        from tests.real_media import expected_frames
        assert meta.total_frames == expected_frames(topos_asset)
        assert meta.fps is not None and float(meta.fps) == pytest.approx(25.0)
        assert "has no attribute 'width'" not in caplog.text

    def test_repeated_probe_single_native_open(
            self, topos_asset: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一文件签名的重复探测共享缓存：native open 只发生一次。"""
        from src.shared.media import topos_source

        open_calls = {"n": 0}
        real_open = topos_source.ToposMediaSource.open

        def counting_open(self):
            open_calls["n"] += 1
            return real_open(self)

        monkeypatch.setattr(
            topos_source.ToposMediaSource, "open", counting_open)
        r1 = MediaSourceFactory.probe_video_metadata_cached(topos_asset)
        r2 = MediaSourceFactory.probe_video_metadata_cached(topos_asset)
        assert r1.metadata is not None
        assert r2.metadata is r1.metadata
        assert open_calls["n"] == 1

    def test_signature_change_invalidates_cache(
            self, topos_asset: str, tmp_path: Path) -> None:
        """文件内容变化（签名不同）后缓存必须失效——以拷贝模拟。"""
        import shutil

        copy_path = str(tmp_path / "copy_topos.mov")
        shutil.copyfile(topos_asset, copy_path)
        r1 = MediaSourceFactory.probe_video_metadata_cached(copy_path)
        assert r1.metadata is not None
        Path(copy_path).write_bytes(
            Path(copy_path).read_bytes() + b"tail")
        r2 = MediaSourceFactory.probe_video_metadata_cached(copy_path)
        assert r1.signature != r2.signature

    def test_non_topos_still_ffmpeg_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not H264_ASSET.is_file():
            pytest.skip("H.264 素材缺失")
        calls = {"n": 0}
        real = MediaSourceFactory._probe_video_metadata_ffmpeg

        def counting(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(
            MediaSourceFactory, "_probe_video_metadata_ffmpeg", counting)
        result = MediaSourceFactory.probe_video_metadata_cached(str(H264_ASSET))
        assert result.backend == "ffmpeg"
        assert calls["n"] == 1
        assert result.metadata is not None
        assert result.metadata.codec_name == "h264"


# ---------------------------------------------------------------------------
# 路由判定
# ---------------------------------------------------------------------------


class TestBackendDetection:
    def test_topos_content_match(self, topos_asset: str) -> None:
        decision = MediaSourceFactory.detect_media_backend(topos_asset)
        assert decision.backend == "topos"
        assert decision.reason_code == "TOPOS_TPIC_CONTENT_MATCH"

    def test_generic_file_falls_back_to_ffmpeg(self) -> None:
        if not H264_ASSET.is_file():
            pytest.skip("H.264 素材缺失")
        decision = MediaSourceFactory.detect_media_backend(str(H264_ASSET))
        assert decision.backend == "ffmpeg"
        assert decision.reason_code == "FFMPEG_GENERIC_FALLBACK"

    def test_detection_decision_cached(self, topos_asset: str) -> None:
        d1 = MediaSourceFactory.detect_media_backend(topos_asset)
        d2 = MediaSourceFactory.detect_media_backend(topos_asset)
        assert d1 is d2


# ---------------------------------------------------------------------------
# dylib 三态：不兼容 / 缺失
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_codec_singleton(monkeypatch: pytest.MonkeyPatch):
    """重置 topos_source 的进程级 codec 单例，隔离 dylib 环境切换。"""
    import src.shared.media.topos_source as ts

    monkeypatch.setattr(ts, "_CODEC_INSTANCE", None)
    monkeypatch.setattr(ts, "_CODEC_UNAVAILABLE", None)
    yield


class TestDylibEnvironments:
    def test_incompatible_dylib_reason(
            self, topos_asset: str, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
            fresh_codec_singleton) -> None:
        bogus = tmp_path / "stale_topos.dylib"
        bogus.write_bytes(b"not a mach-o image (simulated stale dylib)")
        monkeypatch.setenv("TOPOS_CODEC_LIB", str(bogus))
        result = MediaSourceFactory.probe_video_metadata_cached(topos_asset)
        assert result.metadata is None
        assert result.reason_code == "TOPOS_CODEC_INCOMPATIBLE"
        assert result.backend == "none"

    def test_missing_dylib_reason(
            self, topos_asset: str, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
            fresh_codec_singleton) -> None:
        monkeypatch.setenv("TOPOS_CODEC_LIB", str(tmp_path / "nope.dylib"))
        result = MediaSourceFactory.probe_video_metadata_cached(topos_asset)
        assert result.metadata is None
        assert result.reason_code == "TOPOS_CODEC_LIB_NOT_FOUND"

    def test_failure_logged_once(
            self, topos_asset: str, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch,
            fresh_codec_singleton, caplog) -> None:
        bogus = tmp_path / "stale.dylib"
        bogus.write_bytes(b"garbage")
        monkeypatch.setenv("TOPOS_CODEC_LIB", str(bogus))
        with caplog.at_level(logging.WARNING):
            MediaSourceFactory.probe_video_metadata_cached(topos_asset)
            MediaSourceFactory.probe_video_metadata_cached(topos_asset)
        warnings = [r for r in caplog.records
                    if "TOPOS native probe unavailable" in r.message
                    or (r.args and "TOPOS native probe unavailable"
                        in str(r.msg))]
        assert len(warnings) == 1


class TestLibraryResolution:
    def test_ok_with_release_dylib(self) -> None:
        if not RELEASE_DYLIB.is_file():
            pytest.skip("release dylib 未构建")
        r = resolve_topos_codec_library(str(RELEASE_DYLIB))
        assert r.status == LIB_RESOLUTION_OK
        assert r.library_path == str(RELEASE_DYLIB)

    def test_not_found_for_missing_explicit_path(self, tmp_path: Path) -> None:
        r = resolve_topos_codec_library(str(tmp_path / "nope.dylib"))
        assert r.status == LIB_RESOLUTION_NOT_FOUND

    def test_incompatible_for_garbage_file(self, tmp_path: Path) -> None:
        bogus = tmp_path / "garbage.dylib"
        bogus.write_bytes(b"definitely not mach-o")
        r = resolve_topos_codec_library(str(bogus))
        assert r.status == LIB_RESOLUTION_INCOMPATIBLE
        assert r.detail

    def test_not_found_when_no_candidates(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.shared.codec import topos_binding

        monkeypatch.delenv("TOPOS_CODEC_LIB", raising=False)
        monkeypatch.setattr(topos_binding, "candidate_library_paths",
                            lambda: [])
        r = resolve_topos_codec_library()
        assert r.status == LIB_RESOLUTION_NOT_FOUND


# ---------------------------------------------------------------------------
# 真实素材集成场景：import(probe) → thumbnail → composite video_frame
# ---------------------------------------------------------------------------


class TestRealAssetIntegration:
    def test_probe_thumbnail_composite_share_single_native_probe(
            self, topos_asset: str, monkeypatch: pytest.MonkeyPatch,
            caplog) -> None:
        """任务卡集成场景：对同一 TOPOS 文件连续执行
        import（probe_media_metadata）、thumbnail（read_topos_frame_rgb）、
        Composite video_frame（factory create→open→probe→read_frame）：
        - 日志无 ``has no attribute 'width'``（无效 PyAV probe 消除）；
        - factory probe 缓存下只发生一次 native metadata probe；
        - metadata 恒为 1920×1080 / 25fps / 10-bit 4:2:2。
        """
        from src.shared.media import topos_source
        from src.shared.media.topos_source import read_topos_frame_rgb

        probe_opens = {"n": 0}
        real_open = topos_source.ToposMediaSource.open

        def counting_open(self):
            probe_opens["n"] += 1
            return real_open(self)

        monkeypatch.setattr(
            topos_source.ToposMediaSource, "open", counting_open)

        with caplog.at_level(logging.DEBUG):
            # 1) import 路径（EditProject probe_media_metadata）
            from src.features.edit.core.project import probe_media_metadata
            meta = probe_media_metadata(topos_asset)
            assert (meta["width"], meta["height"]) == (1920, 1080)
            assert float(meta["fps"]) == pytest.approx(25.0)
            assert meta["bit_depth"] == 10
            assert meta["pixel_format"] == "yuv422p10le"
            assert meta["media_backend"] == "topos_native"

            opens_after_import = probe_opens["n"]

            # 2) thumbnail 路径（缩略图/胶片条共用 read_topos_frame_rgb）
            frame = read_topos_frame_rgb(topos_asset, 0)
            assert frame is not None
            assert frame.shape[0] == 1080 and frame.shape[1] == 1920

            # 3) Composite video_frame 路径（factory auto → native）
            source = MediaSourceFactory.create(topos_asset)
            from src.shared.media.topos_source import ToposMediaSource
            assert isinstance(source, ToposMediaSource)
            MediaSourceFactory.open_source(source)
            try:
                probed = source.probe()
                assert (probed.width, probed.height) == (1920, 1080)
                decoded = source.read_frame(3)
                assert decoded is not None
            finally:
                source.close()

            # 第二次 import 路径 probe 必须命中缓存（无新 native open；
            # thumbnail/composite 的解码 open 是各自独立的合法实例）
            opens_before_second_probe = probe_opens["n"]
            meta2 = probe_media_metadata(topos_asset)
            assert meta2["media_backend"] == "topos_native"
            assert probe_opens["n"] == opens_before_second_probe

        assert "has no attribute 'width'" not in caplog.text

    def test_probe_media_metadata_carries_codec_profile(
            self, topos_asset: str) -> None:
        """Info 面板档位显示的数据源：codec_profile 与 Scan/Timecode/
        Rotation/Alpha 字段必须透传进 metadata dict。

        此前 probe 已产出 codec_profile='Topos 422'，但 metadata 组装时
        被丢弃——面板 Codec 行退化为粗粒度 'topos'，其余行恒 '—'。
        """
        from src.features.edit.core.project import probe_media_metadata
        from src.shared.media.metadata import format_codec_label

        metadata = probe_media_metadata(topos_asset)
        assert metadata.get("codec") == "topos"
        assert metadata.get("codec_profile") == "Topos 422 Proxy"
        assert metadata.get("field_order") == "progressive"
        assert metadata.get("rotation") == 0
        assert metadata.get("has_alpha") is False
        # 面板最终显示口径：profile 已含族名 → 不重复（'Topos 422 Proxy'）
        assert format_codec_label(
            metadata.get("codec"),
            metadata.get("codec_profile")) == "Topos 422 Proxy"

    def test_tpcd_tier_survives_probe(
            self, topos_asset: str, tmp_path: Path) -> None:
        """v1.7 tpcD：mux 声明档位 → 探测恢复精确档位显示。

        用真实素材的首帧 packet 重 mux（配置逐字段同源以过帧头一致性），
        set_movie_meta(hq) 后探测应得 codec_profile='Topos 422 HQ'；
        无 tpcD 的旧文件（topos_asset 本体）回退码流 profile 名。
        """
        from src.features.edit.core.project import probe_media_metadata
        from src.shared.codec.topos_binding import (
            ToposCodec,
            ToposMovieFile,
            ToposMuxFile,
        )
        from src.shared.codec.topos_profiles import (
            get_topos_tier,
            topos_tier_numeric_id,
        )
        from src.shared.media.metadata import format_codec_label

        MediaSourceFactory.clear_media_probe_cache()
        codec = ToposCodec()
        movie = ToposMovieFile(codec, topos_asset)
        info = movie.info()
        packet = movie.packet(0)
        movie.close()

        tier = get_topos_tier("hq")
        out_path = str(tmp_path / "meta_hq.mov")
        mux = ToposMuxFile(codec, out_path, codec.movie_config(
            info.width, info.height, qp=info.qp_base,
            qmatrix=info.qmatrix_id, profile=info.profile,
            timescale=info.timescale, bit_depth=info.bit_depth,
            pixel_format=info.pixel_format, color_range=info.color_range,
            color_primaries=info.color_primaries,
            color_transfer=info.color_transfer, color_matrix=info.color_matrix,
            alpha_mode=info.alpha_mode, alpha_bit_depth=info.alpha_bit_depth,
            alpha_premultiplied=info.alpha_premultiplied,
            chroma_siting=info.chroma_siting,
            sar_num=info.sar_num, sar_den=info.sar_den))
        mux.add_packet(packet, 0, 1)
        mux.set_movie_meta(tier_id=topos_tier_numeric_id("hq"),
                           vendor="TOPOS", label=tier.label)
        mux.finish()
        mux.close()

        metadata = probe_media_metadata(out_path)
        assert metadata.get("codec_profile") == "Topos 422 HQ"
        assert format_codec_label(metadata.get("codec"),
                                  metadata.get("codec_profile")) == "Topos 422 HQ"


class TestHighDepthBypassAndIdentity:
    def test_high_depth_direct_decode_skips_topos(
            self, topos_asset: str,
            monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core.timeline_export import TimelineExporter

        def _boom(*a, **k):
            raise AssertionError("TOPOS 不应构造 FFmpegMediaSource")

        import src.shared.media.ffmpeg_source as ff

        monkeypatch.setattr(ff, "FFmpegMediaSource", _boom)
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._export_high_depth_sources = {}
        result = exporter._request_high_depth_decoded_frame(
            topos_asset, 0, "asset-1")
        assert result is None

    def test_high_depth_direct_decode_still_works_for_non_topos(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 TOPOS 文件保持原行为（构造失败 → warning + None，不误伤）。"""
        from src.features.edit.core.timeline_export import TimelineExporter

        def _boom(*a, **k):
            raise RuntimeError("simulated ffmpeg open failure")

        import src.shared.media.ffmpeg_source as ff

        monkeypatch.setattr(ff, "FFmpegMediaSource", _boom)
        exporter = TimelineExporter.__new__(TimelineExporter)
        exporter._export_high_depth_sources = {}
        if not H264_ASSET.is_file():
            pytest.skip("H.264 素材缺失")
        result = exporter._request_high_depth_decoded_frame(
            str(H264_ASSET), 0, "asset-2")
        assert result is None  # 走原失败语义（warning + None）

    def test_probe_media_metadata_persists_identity(
            self, topos_asset: str) -> None:
        from src.features.edit.core.project import probe_media_metadata

        metadata = probe_media_metadata(topos_asset)
        assert metadata.get("media_backend") == "topos_native"
        identity = metadata.get("media_identity") or {}
        assert identity.get("fingerprint") == "size+mtime_ns"
        assert identity.get("size_bytes") == TOPOS_ASSET.stat().st_size
        # 不得持久化机器绝对 dylib 路径
        dumped = repr(metadata)
        assert "dylib" not in dumped
