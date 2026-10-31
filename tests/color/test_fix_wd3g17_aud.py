"""wd3-g17 批 D 修复回归测试（AUD-0201..0205 / AUD-0303..0307 / AUD-0310..0315）。

覆盖 REMEDIATION_PLAN.md 2.D 节 16 条 Major 条目的根因修复：

- async_renderer：ColorOutput 延迟释放（0201）、YUV ring 尺寸 LRU
  （0202）、matte 缓存悬挂（0203）、scope 参数深快照（0204）、legacy
  裸帧 float HDR 不再除 255（0205）
- color_manager：LCMS 转换 uint16/float 路径（0303）、embed_icc 质量/
  元数据保持（0304）、新建画布 float 分支（0305）、单例锁（0306）、
  _get_pil_profile 三级查找（0307）
- cache_manager：CacheWarmer 死锁（0310）、正常内存不清理（0311）、
  psutil 缺失优雅降级（0312）、max_memory_mb 字节上限（0313）、
  LRUCache 字节统计（0314）、GPUTextureCache 处置回调（0315）

全部 headless：AsyncRenderer 用 __new__ 桩（无 GL 上下文），
ColorManager/CacheManager 走真实构造（无 Qt/GL 依赖）。
"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, Mock

import numpy as np
import pytest
from PIL import Image, ImageCms

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.cache_manager import (
    CacheConfig,
    CacheManager,
    CacheTier,
    CacheWarmer,
    GPUTextureCache,
    LRUCache,
    MemoryPressureMonitor,
)
from src.features.color.core.color_manager import ColorManager


# =============================================================================
# Helpers
# =============================================================================

def make_stub(**attrs: Any) -> AsyncRenderer:
    """跳过 __init__（无 GL、无 QObject）的 AsyncRenderer 轻量桩。"""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    for name, value in attrs.items():
        setattr(renderer, name, value)
    return renderer


class _FakeConfig:
    """color_manager.apply_defaults_to_new_image 所需的最小配置桩。"""

    def __init__(self, bit_depth: str) -> None:
        self._bit_depth = bit_depth

    def get(self, key: str, default: Any = None) -> Any:
        if key == "default_bit_depth":
            return self._bit_depth
        return default


# =============================================================================
# AUD-0201: ColorOutput 尺寸变化走 deferred-release
# =============================================================================

class TestAud0201ColorOutputDeferredRelease:

    def test_resize_defers_old_pair_via_garbage(self) -> None:
        old_tex, old_fbo = MagicMock(size=(4, 4)), MagicMock()
        new_tex = MagicMock()
        ctx = Mock()
        ctx.texture.return_value = new_tex
        renderer = make_stub(
            ctx=ctx,
            garbage=[],
            _color_output_tex=old_tex,
            _color_output_fbo=old_fbo,
            _color_output_dtype="f2",
        )

        tex, _fbo = renderer._ensure_color_output_fbo(8, 8, None)

        assert tex is new_tex
        # 旧对不立即释放，进入 2 秒延迟释放队列
        old_tex.release.assert_not_called()
        old_fbo.release.assert_not_called()
        assert len(renderer.garbage) == 1
        assert renderer.garbage[0][1] is old_fbo
        assert renderer.garbage[0][2] is old_tex

        # 宽限期到（或 force）后才真正释放
        renderer._process_garbage(force=True)
        old_fbo.release.assert_called_once()
        old_tex.release.assert_called_once()
        assert renderer.garbage == []

    def test_rebuild_failure_keeps_old_pair_live(self) -> None:
        old_tex, old_fbo = MagicMock(size=(4, 4)), MagicMock()
        ctx = Mock()
        ctx.texture.side_effect = RuntimeError("alloc failed")
        renderer = make_stub(
            ctx=ctx,
            garbage=[],
            _color_output_tex=old_tex,
            _color_output_fbo=old_fbo,
            _color_output_dtype="f2",
        )

        with pytest.raises(RuntimeError):
            renderer._ensure_color_output_fbo(8, 8, None)

        # 半创建失败路径不触碰旧对，也不入 garbage
        old_tex.release.assert_not_called()
        old_fbo.release.assert_not_called()
        assert renderer.garbage == []


# =============================================================================
# AUD-0202: YUV→RGB ring 按尺寸 LRU 淘汰
# =============================================================================

def _make_yuv_stub() -> AsyncRenderer:
    made: List[MagicMock] = []

    def make_tex(size: Any, components: Any, dtype: Any) -> MagicMock:
        tex = MagicMock()
        tex.size = size
        tex.glo = len(made) + 1
        made.append(tex)
        return tex

    ctx = Mock()
    ctx.texture.side_effect = make_tex
    renderer = make_stub(
        ctx=ctx,
        _temp_texture_alloc_count=0,
        _yuv_rgb_output_cache={},
        _yuv_rgb_output_ring_idx={},
        _yuv_rgb_output_ring_size=3,
        _yuv_rgb_output_size_order=[],
        _yuv_rgb_output_max_sizes=2,
        _yuv_conv_fbo=Mock(),
        _yuv_conv_fbo_tex_glo=1,
    )
    renderer.__dict__["_made_textures"] = made
    return renderer


class TestAud0202YuvRingLru:

    def test_third_size_evicts_oldest_ring(self) -> None:
        renderer = _make_yuv_stub()
        made: List[MagicMock] = renderer.__dict__["_made_textures"]

        renderer._get_yuv_rgb_output_texture(100, 100)
        renderer._get_yuv_rgb_output_texture(200, 100)
        assert set(renderer._yuv_rgb_output_cache) == {(100, 100), (200, 100)}

        renderer._get_yuv_rgb_output_texture(300, 100)

        # 只保留最近 2 个尺寸；最旧尺寸的纹理被释放
        assert set(renderer._yuv_rgb_output_cache) == {(200, 100), (300, 100)}
        assert (100, 100) not in renderer._yuv_rgb_output_ring_idx
        made[0].release.assert_called_once()
        # 缓存的 YUV 转换 FBO 包着被淘汰纹理（glo 1）→ 必须一并丢弃
        assert renderer._yuv_conv_fbo is None
        assert renderer._yuv_conv_fbo_tex_glo == 0

    def test_lru_refresh_protects_reused_size(self) -> None:
        renderer = _make_yuv_stub()
        made: List[MagicMock] = renderer.__dict__["_made_textures"]

        renderer._get_yuv_rgb_output_texture(100, 100)
        renderer._get_yuv_rgb_output_texture(200, 100)
        # 重新触碰 (100,100)：它变为最近使用
        renderer._get_yuv_rgb_output_texture(100, 100)
        renderer._get_yuv_rgb_output_texture(300, 100)

        # 淘汰的应是最久未用的 (200,100)，而不是刚用过的 (100,100)
        assert (100, 100) in renderer._yuv_rgb_output_cache
        assert (200, 100) not in renderer._yuv_rgb_output_cache
        made[1].release.assert_called_once()
        made[0].release.assert_not_called()

    def test_current_size_survives_repeated_calls(self) -> None:
        renderer = _make_yuv_stub()
        for _ in range(5):
            renderer._get_yuv_rgb_output_texture(128, 96)
        assert set(renderer._yuv_rgb_output_cache) == {(128, 96)}
        assert renderer._yuv_rgb_output_size_order == [(128, 96)]


# =============================================================================
# AUD-0203: matte 重生成异常时缓存不自洽（先归还池、后失败）
# =============================================================================

class TestAud0203MatteCacheDangling:

    def test_source_path_generation_failure_keeps_old_entry_owned(self) -> None:
        old_tex = MagicMock()
        fb_manager = Mock()
        renderer = make_stub(
            ctx=Mock(),
            fb_manager=fb_manager,
            matte_cache={"L1": {"matte": old_tex, "params_hash": 12345}},
            _mask_epoch={},
            _get_source_matte_resolution=Mock(return_value=(64, 64)),
            _get_source_tex_for_qualifier=Mock(return_value=Mock()),
            _generate_source_matte=Mock(side_effect=RuntimeError("shader missing")),
        )
        layer = {
            "id": "L1",
            "qualifier_params": {"enabled": False},
            "mask_shapes": [{"type": "circle"}],
        }

        with pytest.raises(RuntimeError):
            renderer._generate_matte(layer, Mock(), 32, 32, (0, 0, 64, 64))

        # 生成失败：旧条目未被归还池（仍被缓存独占），缓存可继续命中
        fb_manager.release_temp_texture.assert_not_called()
        assert renderer.matte_cache["L1"]["matte"] is old_tex

    def test_source_path_success_releases_old_only_after_new_exists(self) -> None:
        old_tex, new_tex = MagicMock(), MagicMock()
        fb_manager = Mock()
        renderer = make_stub(
            ctx=Mock(),
            fb_manager=fb_manager,
            matte_cache={"L1": {"matte": old_tex, "params_hash": 12345}},
            _mask_epoch={},
            _get_source_matte_resolution=Mock(return_value=(64, 64)),
            _get_source_tex_for_qualifier=Mock(return_value=Mock()),
            _generate_source_matte=Mock(return_value=new_tex),
            _sample_matte_to_viewport=Mock(side_effect=lambda t, *a, **k: t),
            _matte_viewport_textures={},
        )
        layer = {
            "id": "L1",
            "qualifier_params": {"enabled": False},
            "mask_shapes": [{"type": "circle"}],
        }

        result = renderer._generate_matte(layer, Mock(), 32, 32, (0, 0, 64, 64))

        assert result is new_tex
        fb_manager.release_temp_texture.assert_called_once_with(old_tex)
        assert renderer.matte_cache["L1"]["matte"] is new_tex

    def test_viewport_path_generation_failure_keeps_old_entry_owned(self) -> None:
        tex_a = MagicMock()
        fb_manager = Mock()
        fb_manager.get_temp_texture.return_value = tex_a
        renderer = make_stub(
            ctx=Mock(),
            fb_manager=fb_manager,
            matte_cache={},
            _mask_epoch={},
            utility_shaders={"keyer": MagicMock()},
            _render_quad=Mock(),
            _apply_matte_postprocess=Mock(side_effect=lambda t, *a, **k: t),
        )
        layer = {"id": "L1", "qualifier_params": {"enabled": True}, "mask_shapes": []}

        # 第一次生成成功，缓存被填充
        first = renderer._generate_viewport_matte(layer, Mock(), 16, 16, (0, 0, 16, 16))
        assert renderer.matte_cache["L1"]["matte"] is first
        fb_manager.release_temp_texture.assert_not_called()

        # 第二次（参数变化）生成中途抛错——验证 AUD-0203 的缓存所有权保证。
        # EF-07 AUD-0206 修复后着色器缺失不再抛 KeyError（显式降级为全白
        # matte 并正常完成，见 tests/color/test_ef07_aud.py），故改用后处理
        # 阶段抛错作为"生成中途失败"的注入手段。
        renderer._apply_matte_postprocess = Mock(
            side_effect=RuntimeError("postprocess failed"))
        layer2 = {"id": "L1", "qualifier_params": {"enabled": True, "hue_center": 0.9},
                  "mask_shapes": []}
        with pytest.raises(RuntimeError):
            renderer._generate_viewport_matte(layer2, Mock(), 16, 16, (0, 0, 16, 16))

        # 旧条目未归还池，缓存不自洽的问题不复存在
        fb_manager.release_temp_texture.assert_not_called()
        assert renderer.matte_cache["L1"]["matte"] is first


# =============================================================================
# AUD-0204: scope 参数缓存深快照
# =============================================================================

class TestAud0204ScopeParamsSnapshot:

    def test_nested_mutation_does_not_leak_into_snapshot(self) -> None:
        lut = np.arange(12, dtype=np.float32).reshape(12, 1)
        params: Dict[str, Any] = {
            "viewport_size": (8, 8),
            "layers": [{
                "id": "L1",
                "curves_lut": lut,
                "qualifier_params": {"hue": 0.5},
                "plugins": [{"params": {"amount": 1}}],
                "mask_shapes": [{"points": [0.0, 1.0]}],
            }],
            "raw_preview": {"exposure": 0.0},
        }

        snap = AsyncRenderer._snapshot_scope_params(params)

        # 原地修改 UI 侧负载
        params["layers"][0]["qualifier_params"]["hue"] = 0.9
        params["layers"][0]["curves_lut"][0] = 99.0
        params["layers"][0]["plugins"][0]["params"]["amount"] = 2
        params["layers"][0]["mask_shapes"][0]["points"].append(2.0)
        params["viewport_size"] = (4, 4)

        assert snap["layers"][0]["qualifier_params"]["hue"] == 0.5
        assert snap["layers"][0]["curves_lut"][0] == 0.0
        assert snap["layers"][0]["plugins"][0]["params"]["amount"] == 1
        assert snap["layers"][0]["mask_shapes"][0]["points"] == [0.0, 1.0]
        assert snap["viewport_size"] == (8, 8)

    def test_immutable_values_pass_through(self) -> None:
        params: Dict[str, Any] = {
            "a": 1, "b": "s", "c": b"bytes", "d": None,
            "e": (1, 2), "f": 2.5,
        }
        snap = AsyncRenderer._snapshot_scope_params(params)
        assert snap == params
        assert snap["c"] is params["c"]


# =============================================================================
# AUD-0205: legacy 裸帧 float HDR 不再被 /255
# =============================================================================

def _make_video_frame_stub() -> AsyncRenderer:
    return make_stub(
        ctx=Mock(),
        _video_generation_id=0,
        _do_upload_video_frame=Mock(return_value=Mock()),
        _do_render=Mock(),
    )


class TestAud0205LegacyFloatFrameScale:

    def test_hdr_float_values_uploaded_asis(self) -> None:
        renderer = _make_video_frame_stub()
        frame = np.full((4, 4, 3), 4.0, dtype=np.float32)  # 合法线性 HDR 高光
        renderer._do_render_video_frame({
            "frame": frame,
            "frame_index": 0,
            "render_params": {"viewport_size": (4, 4), "layers": []},
        })

        upload = renderer._do_upload_video_frame.call_args[0][0]
        assert upload["dtype"] == "f2"
        # 旧启发式会把整帧除以 255（max 变 4/255）；修复后 HDR 原值上传
        assert upload["frame_data"].max() == pytest.approx(4.0)
        assert upload["frame_data"].dtype == np.float16

    def test_explicit_frame_scale_is_honoured(self) -> None:
        renderer = _make_video_frame_stub()
        frame = np.full((4, 4, 3), 128.0, dtype=np.float32)  # 0..255 legacy 生产方
        renderer._do_render_video_frame({
            "frame": frame,
            "frame_index": 0,
            "frame_scale": 255.0,
            "render_params": {"viewport_size": (4, 4), "layers": []},
        })

        upload = renderer._do_upload_video_frame.call_args[0][0]
        assert upload["frame_data"].max() == pytest.approx(
            128.0 / 255.0, abs=2e-3)  # float16 舍入容差

    def test_uint8_fast_path_unchanged(self) -> None:
        renderer = _make_video_frame_stub()
        frame = np.full((4, 4, 3), 255, dtype=np.uint8)
        renderer._do_render_video_frame({
            "frame": frame,
            "frame_index": 0,
            "render_params": {"viewport_size": (4, 4), "layers": []},
        })

        upload = renderer._do_upload_video_frame.call_args[0][0]
        assert upload["dtype"] == "f1"
        assert upload["frame_data"].max() == 255


# =============================================================================
# AUD-0303: LCMS 转换 uint16/float 路径
# =============================================================================

class TestAud0303LcmsConversion:

    def test_uint16_input_converts_instead_of_silent_failure(self) -> None:
        cm = ColorManager()
        img = np.linspace(0, 65535, 48, dtype=np.uint16).reshape(4, 4, 3)

        result = cm._convert_via_lcms(img, "sRGB", "Rec. 709")

        # 旧版：Image.fromarray(uint16 RGB) 抛 TypeError → convert_for_export
        # 静默返回未转换图像。修复后转换确实发生。
        assert result is not img
        assert result.shape == (4, 4, 3)
        assert result.dtype == np.uint8

    def test_hdr_float_warns_and_clamps_at_1(self, caplog) -> None:
        cm = ColorManager()
        img = np.linspace(0.0, 4.0, 48, dtype=np.float32).reshape(4, 4, 3)

        with caplog.at_level("WARNING"):
            result = cm._convert_via_lcms(img, "sRGB", "Rec. 709")

        # HDR 高光仍被 8-bit LCMS 路径截断（技术约束），但必须可闻
        assert any("clamps HDR highlights" in r.message for r in caplog.records)
        assert result.dtype == np.float32
        assert result.max() == pytest.approx(1.0)

    def test_sdr_float_no_warning(self, caplog) -> None:
        cm = ColorManager()
        img = np.linspace(0.0, 1.0, 48, dtype=np.float32).reshape(4, 4, 3)

        with caplog.at_level("WARNING"):
            cm._convert_via_lcms(img, "sRGB", "Rec. 709")

        assert not any("clamps HDR highlights" in r.message for r in caplog.records)


# =============================================================================
# AUD-0304: embed_icc 保持 JPEG 质量与元数据
# =============================================================================

class TestAud0304EmbedIcc:

    def _make_jpeg(self, path, quality: int = 95) -> None:
        arr = np.tile(
            np.arange(256, dtype=np.uint8).reshape(16, 16, 1), (1, 1, 3)
        )
        img = Image.fromarray(arr, "RGB")
        exif = Image.Exif()
        exif[0x0112] = 8  # orientation
        img.save(str(path), quality=quality, exif=exif.tobytes())

    def test_jpeg_quality_and_exif_preserved(self, tmp_path) -> None:
        cm = ColorManager()
        p = tmp_path / "out.jpg"
        self._make_jpeg(p, quality=95)
        with Image.open(str(p)) as im:
            quant_before = im.quantization

        assert cm.embed_icc(str(p), "sRGB")

        with Image.open(str(p)) as im:
            assert im.info.get("icc_profile")
            assert im.getexif().get(0x0112) == 8
            quant_after = im.quantization
        # quality='keep'：量化表原样保留（旧版被 Pillow 默认 q75 重编码）
        assert quant_after == quant_before

    def test_png_icc_and_exif_preserved(self, tmp_path) -> None:
        cm = ColorManager()
        p = tmp_path / "out.png"
        arr = np.zeros((8, 8, 3), dtype=np.uint8)
        img = Image.fromarray(arr, "RGB")
        exif = Image.Exif()
        exif[0x0112] = 6
        img.save(str(p), exif=exif.tobytes())

        assert cm.embed_icc(str(p), "sRGB")

        with Image.open(str(p)) as im:
            assert im.info.get("icc_profile")
            assert im.getexif().get(0x0112) == 6


# =============================================================================
# AUD-0305: apply_defaults_to_new_image float 分支
# =============================================================================

class TestAud0305NewImageFloatDefaults:

    def _cm(self, bit_depth: str) -> ColorManager:
        return ColorManager(config_manager=_FakeConfig(bit_depth))

    def test_float16_not_divided_by_255(self) -> None:
        img = np.full((2, 2, 3), 0.5, dtype=np.float16)
        out = self._cm("32-bit float").apply_defaults_to_new_image(img)
        assert out.dtype == np.float32
        assert out.max() == pytest.approx(0.5)  # 旧版会得到 0.5/255（近黑）

    def test_float64_not_divided_by_255(self) -> None:
        img = np.full((2, 2, 3), 0.25, dtype=np.float64)
        out = self._cm("32-bit float").apply_defaults_to_new_image(img)
        assert out.dtype == np.float32
        assert out.max() == pytest.approx(0.25)

    def test_integer_scalings_unchanged(self) -> None:
        cm = self._cm("32-bit float")
        out8 = cm.apply_defaults_to_new_image(
            np.full((2, 2, 3), 255, dtype=np.uint8))
        assert out8.max() == pytest.approx(1.0)
        out16 = cm.apply_defaults_to_new_image(
            np.full((2, 2, 3), 65535, dtype=np.uint16))
        assert out16.max() == pytest.approx(1.0)

    def test_float32_passthrough(self) -> None:
        img = np.full((2, 2, 3), 0.75, dtype=np.float32)
        out = self._cm("32-bit float").apply_defaults_to_new_image(img)
        assert out.max() == pytest.approx(0.75)


# =============================================================================
# AUD-0306: ColorManager 单例并发
# =============================================================================

class TestAud0306SingletonLock:

    def test_concurrent_first_construction_yields_one_instance(
        self, monkeypatch,
    ) -> None:
        calls: List[int] = []
        original = ColorManager._init_profiles

        def counting(self: ColorManager) -> None:
            calls.append(1)
            original(self)

        monkeypatch.setattr(ColorManager, "_init_profiles", counting)
        monkeypatch.setattr(ColorManager, "_instance", None)

        results: List[ColorManager] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            results.append(ColorManager())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        assert len({id(r) for r in results}) == 1
        assert len(calls) == 1  # 昂贵初始化只执行一次
        # 恢复惰性单例状态，避免影响其他用例
        monkeypatch.setattr(ColorManager, "_instance", None)


# =============================================================================
# AUD-0307: _get_pil_profile 三级查找
# =============================================================================

class TestAud0307PilProfileLookup:

    def _fresh(self) -> ColorManager:
        cm = ColorManager.__new__(ColorManager)
        cm._srgb_profile = ImageCms.createProfile("sRGB")
        cm._system_profiles = {}
        return cm

    def _profile_bytes(self, prof: Any) -> bytes:
        """Pillow 12 的 createProfile/ImageCmsProfile 返回类型不统一，
        统一取底层 core profile 再序列化。"""
        core_prof = getattr(prof, "profile", prof)
        return ImageCms.core.profile_tobytes(core_prof)

    def test_bundled_profile_is_preferred(self, tmp_path, monkeypatch) -> None:
        cm = self._fresh()
        # 生成一个合法 ICC 文件充当 bundled Display P3 profile
        icc_bytes = self._profile_bytes(cm._srgb_profile)
        (tmp_path / "DisplayP3.icc").write_bytes(icc_bytes)
        monkeypatch.setattr(
            cm, "_bundled_profiles_dir", lambda: str(tmp_path)
        )

        prof = cm._get_pil_profile("Display P3")

        assert prof is not None
        assert self._profile_bytes(prof) == icc_bytes

    def test_fallback_to_srgb_is_loud(self, tmp_path, monkeypatch, caplog) -> None:
        cm = self._fresh()
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(
            cm, "_bundled_profiles_dir", lambda: str(empty)
        )

        with caplog.at_level("WARNING"):
            prof = cm._get_pil_profile("Display P3")

        assert prof is not None
        assert self._profile_bytes(prof) == self._profile_bytes(
            cm._srgb_profile)
        assert any(
            "falls back to sRGB" in r.message for r in caplog.records
        )


# =============================================================================
# AUD-0310: CacheWarmer 重入死锁
# =============================================================================

class TestAud0310CacheWarmerDeadlock:

    def test_restart_while_running_does_not_deadlock(self) -> None:
        mgr = CacheManager(CacheConfig(enable_warmup=True))
        warmer = CacheWarmer(mgr)
        # 模拟"预热进行中"再次 start_warmup（典型：快速 seek 连续重启）
        with warmer._lock:
            warmer._is_running = True

        release = threading.Event()
        done = threading.Event()

        def call() -> None:
            warmer.start_warmup(
                "clip", 0, lookahead=1,
                decode_callback=lambda *a: (release.wait(5.0), None)[1])
            done.set()

        t = threading.Thread(target=call, daemon=True)
        t.start()
        # 旧版在锁内调 stop_warmup（不可重入锁二次 acquire）→ 永久挂起
        assert done.wait(5.0), "start_warmup deadlocked on non-reentrant lock"
        t.join(timeout=5.0)
        assert warmer.is_running
        release.set()
        warmer.stop_warmup()
        assert not warmer.is_running

    def test_old_loop_does_not_clobber_new_running_flag(self) -> None:
        mgr = CacheManager(CacheConfig(enable_warmup=True))
        warmer = CacheWarmer(mgr)

        release_first = threading.Event()
        started = threading.Event()

        def decode_first(cid: str, frame: int, q: str) -> None:
            started.set()
            release_first.wait(5.0)
            return None

        warmer.start_warmup(
            "clip", 0, lookahead=1, decode_callback=decode_first)
        assert started.wait(2.0)

        # 旧线程仍卡在 decode_callback 时重启（start 内有界 join 旧线程）
        release_second = threading.Event()
        warmer.start_warmup(
            "clip", 10, lookahead=1,
            decode_callback=lambda *a: (release_second.wait(5.0), None)[1])
        assert warmer.is_running

        # 旧线程迟到收尾：不得把新预热的 _is_running 翻回 False
        release_first.set()
        time.sleep(0.1)
        assert warmer.is_running

        release_second.set()
        warmer.stop_warmup()
        assert not warmer.is_running


# =============================================================================
# AUD-0311/0312: 内存压力监控
# =============================================================================

class TestAud0311MonitorNoCleanupWhenNormal:

    def test_normal_memory_triggers_nothing(self, monkeypatch) -> None:
        mgr = CacheManager(CacheConfig())
        levels: List[str] = []
        monkeypatch.setattr(
            mgr, "on_memory_pressure", lambda level: levels.append(level))
        fake_psutil = types.SimpleNamespace(
            virtual_memory=lambda: types.SimpleNamespace(percent=30.0))
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        monitor = MemoryPressureMonitor(mgr, check_interval=0.02)
        t = threading.Thread(target=monitor._monitor_loop, daemon=True)
        t.start()
        time.sleep(0.1)
        monitor._stop_event.set()
        t.join(timeout=2.0)

        assert not t.is_alive()
        # 旧版"正常"分支每周期发一次 low 事件清空缩略图缓存
        assert levels == []

    def test_medium_band_still_fires(self, monkeypatch) -> None:
        mgr = CacheManager(CacheConfig())
        levels: List[str] = []
        monkeypatch.setattr(
            mgr, "on_memory_pressure", lambda level: levels.append(level))
        # 0.8*warning(0.8)=0.64 ≤ 0.72 < 0.8 → medium
        fake_psutil = types.SimpleNamespace(
            virtual_memory=lambda: types.SimpleNamespace(percent=72.0))
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        monitor = MemoryPressureMonitor(mgr, check_interval=0.02)
        t = threading.Thread(target=monitor._monitor_loop, daemon=True)
        t.start()
        time.sleep(0.1)
        monitor._stop_event.set()
        t.join(timeout=2.0)

        assert levels and set(levels) == {"medium"}


class TestAud0312PsutilMissing:

    def test_missing_psutil_degrades_gracefully(self, monkeypatch, caplog) -> None:
        mgr = CacheManager(CacheConfig())
        monitor = MemoryPressureMonitor(mgr, check_interval=0.02)
        # sys.modules 里挂 None → import psutil 抛 ImportError
        monkeypatch.setitem(sys.modules, "psutil", None)

        with caplog.at_level("WARNING"):
            t = threading.Thread(target=monitor._monitor_loop, daemon=True)
            t.start()
            t.join(timeout=2.0)

        assert not t.is_alive()  # 线程优雅退出而非崩溃
        assert any(
            "psutil not available" in r.message for r in caplog.records
        )


# =============================================================================
# AUD-0313: max_memory_mb 字节上限
# =============================================================================

class TestAud0313ByteBudget:

    def test_lru_byte_cap_evicts_oldest(self) -> None:
        cache: LRUCache[int] = LRUCache(
            100, CacheTier.DECODE_FRAME, max_bytes=1000)
        cache.put("a", 1, estimated_bytes=400)
        cache.put("b", 2, estimated_bytes=400)
        cache.put("c", 3, estimated_bytes=400)  # 总 1200 > 1000

        assert cache.size == 2
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.stats.total_bytes == 800

    def test_single_oversized_entry_is_kept(self) -> None:
        cache: LRUCache[int] = LRUCache(
            100, CacheTier.DECODE_FRAME, max_bytes=100)
        cache.put("big", 1, estimated_bytes=10_000)
        assert cache.get("big") == 1  # 不做自我淘汰

    def test_config_budget_is_wired_to_tiers(self) -> None:
        mgr = CacheManager(CacheConfig(max_memory_mb=1.0))
        total = 1.0 * 1024 * 1024
        assert mgr.decode_cache.proxy_cache.max_bytes == int(total * 0.45)
        assert mgr.decode_cache.master_cache.max_bytes == int(total * 0.15)
        assert mgr.gpu_texture_cache._cache.max_bytes == int(total * 0.20)
        assert mgr.thumbnail_cache._cache.max_bytes == int(total * 0.05)

        mgr.update_config(CacheConfig(max_memory_mb=2.0))
        assert (
            mgr.decode_cache.proxy_cache.max_bytes
            == int(2.0 * 1024 * 1024 * 0.45)
        )

    def test_end_to_end_frame_bytes_respect_budget(self) -> None:
        mgr = CacheManager(CacheConfig(max_memory_mb=1.0))
        frame = np.zeros((64, 64, 3), dtype=np.uint8)  # 12288 B
        budget = mgr.decode_cache.proxy_cache.max_bytes
        max_frames = budget // frame.nbytes + 1
        for i in range(max_frames + 5):
            mgr.decode_cache.put("clip", i, "proxy", frame)
        used = mgr.decode_cache.proxy_cache.stats.total_bytes
        assert used <= budget + frame.nbytes  # 允许最后一条入队后再收敛


# =============================================================================
# AUD-0314: LRUCache 字节统计
# =============================================================================

class TestAud0314ByteAccounting:

    def test_overwrite_deducts_old_bytes(self) -> None:
        cache: LRUCache[int] = LRUCache(10, CacheTier.THUMBNAIL)
        cache.put("k", 1, estimated_bytes=100)
        cache.put("k", 2, estimated_bytes=300)
        assert cache.stats.total_bytes == 300  # 旧版为 400（虚增）

    def test_evict_deducts_evicted_entry_bytes(self) -> None:
        cache: LRUCache[int] = LRUCache(2, CacheTier.THUMBNAIL)
        cache.put("a", 1, estimated_bytes=100)
        cache.put("b", 2, estimated_bytes=500)
        cache.put("c", 3, estimated_bytes=50)  # 淘汰 a
        assert cache.get("a") is None
        assert cache.stats.total_bytes == 550  # 旧版减 50（当前条目）得 600

    def test_remove_and_invalidate_deduct_bytes(self) -> None:
        cache: LRUCache[int] = LRUCache(10, CacheTier.THUMBNAIL)
        cache.put("a", 1, estimated_bytes=100)
        cache.put("b", 2, estimated_bytes=200)
        assert cache.remove("a") is True
        assert cache.stats.total_bytes == 200  # 旧版 remove 不扣减
        removed = cache.invalidate_range(lambda k: True)
        assert removed == 1
        assert cache.stats.total_bytes == 0  # 旧版 invalidate 不扣减

    def test_clear_resets_bytes(self) -> None:
        cache: LRUCache[int] = LRUCache(10, CacheTier.THUMBNAIL)
        cache.put("a", 1, estimated_bytes=100)
        cache.clear()
        assert cache.stats.total_bytes == 0

    def test_shrinking_max_size_evicts_with_byte_tracking(self) -> None:
        cache: LRUCache[int] = LRUCache(10, CacheTier.THUMBNAIL)
        cache.put("a", 1, estimated_bytes=100)
        cache.put("b", 2, estimated_bytes=200)
        cache.max_size = 1
        assert cache.size == 1
        assert cache.stats.total_bytes == 200
        assert cache.get("a") is None


# =============================================================================
# AUD-0315: GPUTextureCache 处置回调
# =============================================================================

class TestAud0315GpuTextureEvictHook:

    def test_capacity_eviction_calls_release_hook(self) -> None:
        released: List[Any] = []
        cache = GPUTextureCache(
            max_size=2, release_hook=released.append)

        tex_a, tex_b, tex_c = MagicMock(), MagicMock(), MagicMock()
        cache.put(64, 64, tex_a)
        cache.put(64, 64, tex_b, usage="other")
        cache.put(64, 64, tex_c, usage="third")

        assert released == [tex_a]  # 淘汰的纹理交回调处置（deferred-release）

    def test_overwrite_remove_and_clear_call_hook(self) -> None:
        released: List[Any] = []
        cache = GPUTextureCache(
            max_size=8, release_hook=released.append)
        tex_a, tex_b = MagicMock(), MagicMock()

        cache.put(64, 64, tex_a)
        cache.put(64, 64, tex_a)  # 同对象覆盖：不触发
        assert released == []

        cache.put(64, 64, tex_b)  # 覆盖不同对象：旧值处置
        assert released == [tex_a]

        assert cache.get(64, 64) is tex_b  # get 不触发处置
        assert released == [tex_a]

        cache.clear()
        assert released == [tex_a, tex_b]

    def test_failing_hook_does_not_break_cache(self) -> None:
        def bad_hook(value: Any) -> None:
            raise RuntimeError("queue closed")

        cache = GPUTextureCache(max_size=1, release_hook=bad_hook)
        cache.put(64, 64, MagicMock())
        cache.put(64, 64, MagicMock(), usage="b")  # 淘汰 → hook 抛错
        assert cache._cache.size == 1  # 缓存状态不受影响

    def test_no_hook_defaults_to_old_behaviour(self) -> None:
        cache = GPUTextureCache(max_size=2)
        cache.put(64, 64, MagicMock())
        cache.put(64, 64, MagicMock(), usage="b")
        cache.put(64, 64, MagicMock(), usage="c")
        assert cache._cache.size == 2

    def test_set_release_hook_after_construction(self) -> None:
        released: List[Any] = []
        cache = GPUTextureCache(max_size=1)
        tex_a = MagicMock()
        cache.put(64, 64, tex_a)
        cache.set_release_hook(released.append)
        cache.put(64, 64, MagicMock(), usage="b")
        assert released == [tex_a]
