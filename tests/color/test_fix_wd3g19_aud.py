"""wd3-g19 批 D（color core 杂项域）修复回归测试。

覆盖 REMEDIATION_PLAN 批 D 的 17 条 Major 条目中的可 headless 验证项：
AUD-0610/0611（image_engine 烘焙语义与源 epoch）、AUD-0700/0701/0702
（layer 缓存键与反序列化）、AUD-0710/0711/0712/0713（lut_exporter）、
AUD-0800（插件注册表线程安全）、AUD-0830/0831/0832（shader 缓存键/
容量/改写防护）、AUD-0840/0910（GLSL+MSL 双侧同步，另有
tests/color/test_metal_composition_* 的 G2 真机对拍门禁）、
AUD-0900/0903（denoise 表面缓存与 stats 快照）。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.features.color.core.layer import (
    Layer,
    MaskGroup,
    MaskShape,
    build_curves_lut_from_data,
    build_hsl_curve_lut_from_data,
)
from src.features.color.core.shader_nodes import (
    BLEND_MODES_SNIPPET,
    GRADING_SNIPPET,
    CURVES_SNIPPET,
)
from src.features.color.core.shader_nodes_msl import (
    MSL_BLEND_MODES,
    MSL_GRADING,
    MSL_CURVES,
)


# ---------------------------------------------------------------------------
# AUD-0910 / AUD-0840 —— GLSL 与 MSL 双侧同步（源文本 + numpy 语义镜像）
# ---------------------------------------------------------------------------


class TestAud0910ColorBlendMode:
    def test_glsl_mode14_takes_base_value(self):
        line = next(ln for ln in BLEND_MODES_SNIPPET.splitlines()
                    if "mode == 14" in ln)
        assert "a_hsv.x, a_hsv.y, b_hsv.z" in line
        assert "a_hsv.z" not in line

    def test_msl_mode14_matches_glsl(self):
        glsl = next(ln for ln in BLEND_MODES_SNIPPET.splitlines()
                    if "mode == 14" in ln)
        msl = next(ln for ln in MSL_BLEND_MODES.splitlines()
                   if "mode == 14" in ln)
        assert "a_hsv.x, a_hsv.y, b_hsv.z" in msl
        # 两侧分量选取逐字一致（HSV 分量三元组相同）
        assert glsl.split("vec3(")[1].split(")")[0] == \
            msl.split("float3(")[1].split(")")[0]

    def test_color_blend_semantics_preserves_base_value(self):
        """Color 混合 = 上层 H/S + 下层 V：亮度必须来自 base。"""
        base = np.array([0.9, 0.45, 0.1])    # V=0.9
        blend = np.array([0.1, 0.3, 0.6])    # V=0.6
        b_hsv = _rgb_to_hsv_np(base)
        a_hsv = _rgb_to_hsv_np(blend)
        result = _hsv_to_rgb_np(np.array([a_hsv[0], a_hsv[1], b_hsv[2]]))
        # 与 Normal（= blend_layer）明确区分
        assert not np.allclose(result, blend, atol=1e-3)
        # 结果的 HSV 亮度等于 base 的 V（保留 base 亮度）
        assert np.isclose(_rgb_to_hsv_np(result)[2], b_hsv[2], atol=1e-5)


class TestAud0840NegativeHsvProtection:
    def test_glsl_hue_shift_guarded(self):
        assert "rgb2hsv(abs(color))" in GRADING_SNIPPET
        assert "sign_c * hsv2rgb(hsv)" in GRADING_SNIPPET

    def test_msl_hue_shift_guarded(self):
        assert "msl_rgb2hsv(abs(color))" in MSL_GRADING
        assert "sign_c * msl_hsv2rgb(hsv)" in MSL_GRADING

    def test_glsl_hsl_curves_guarded(self):
        assert "vec3 hsv = rgb2hsv(abs_color);" in CURVES_SNIPPET
        assert "sign_c * hsv2rgb(hsv)" in CURVES_SNIPPET
        assert "mix(gray, abs_color, final_sat_mult)" in CURVES_SNIPPET

    def test_msl_hsl_curves_guarded(self):
        assert "float3 hsv = msl_rgb2hsv(abs_color);" in MSL_CURVES
        assert "sign_c * msl_hsv2rgb(hsv)" in MSL_CURVES
        assert "mix(gray, abs_color, final_sat_mult)" in MSL_CURVES

    def test_mixed_sign_negative_preserves_sign_pattern(self):
        """带符号防护：lift/gamma 之后可达的混合符号 HDR 颜色。

        旧实现直接 rgb2hsv(含负分量)：d/(q.x+e) 对负/混合 max 产生
        错误饱和度与色相，输出整体符号翻转（RGB 全体反相）——语义
        完全不可预测。防护后逐通道符号保持。
        """
        color = np.array([[0.49938124, -0.36175418, 0.17019545]])
        guarded = _mirror_hue_shift(color, 120.0)
        legacy = _mirror_hue_shift_legacy(color, 120.0)

        assert np.all(np.sign(guarded) == np.sign(color))  # 符号逐通道保留
        assert np.ptp(np.abs(guarded)) > 1e-3              # 色度未被压平
        # 旧路径：输出符号模式翻转（负变正、正变负）
        assert np.any(np.sign(legacy) != np.sign(color))
        assert not np.allclose(guarded, legacy, atol=1e-3)

    def test_nonnegative_input_unchanged_by_guard(self):
        """非负输入 abs 恒等：防护语义与旧公式逐点一致（不回归 SDR）。"""
        rng = np.random.default_rng(7)
        color = rng.uniform(0.0, 1.0, size=(64, 3))
        assert np.allclose(_mirror_hue_shift(color, 90.0),
                           _mirror_hue_shift_legacy(color, 90.0), atol=1e-6)


# ---------------------------------------------------------------------------
# AUD-0610 / AUD-0611 —— image_engine
# ---------------------------------------------------------------------------


class _FakeRenderer:
    def __init__(self):
        self.commands = []

    def submit(self, cmd, data=None, **kwargs):
        self.commands.append((cmd, data))


class TestAud0610BakeSessionSemantics:
    def test_commit_baked_source_preserves_session(self, image_engine):
        from src.features.color.core.image_engine import RenderCommand

        engine = image_engine
        engine.renderer = _FakeRenderer()
        engine.current_file_path = "/tmp/keep_me.raw"
        engine.ai_plugin_result_cache = {"ai": object()}
        engine.is_current_image_srgb = False
        engine.current_raw_params = {"enabled": True, "gamma": "sRGB"}
        engine.crop_params = {"rect": (0.1, 0.1, 0.5, 0.5), "rotation": 12.0}
        engine._flip_h = True
        engine._source_epoch = 3
        full = np.full((8, 8, 3), 200, dtype=np.uint8)
        high = np.full((4, 4, 3), 0.8, dtype=np.float32)
        low = np.full((2, 2, 3), 0.8, dtype=np.float32)

        engine._commit_baked_source(full, high, low)

        # 会话状态保留（旧 set_image(is_new_file=True) 会全部重置/清空）
        assert engine.current_file_path == "/tmp/keep_me.raw"
        assert engine.ai_plugin_result_cache == {"ai": object()} or \
            len(engine.ai_plugin_result_cache) == 1
        assert engine.current_raw_params == {"enabled": True, "gamma": "sRGB"}
        assert engine.is_current_image_srgb is False
        # 源图替换 + 几何基准回到恒等（烘焙像素已含翻转/裁剪）
        assert engine.img_full_original.shape == (8, 8, 3)
        assert engine.img_source_full.shape == (8, 8, 3)
        assert engine._flip_h is False and engine._flip_v is False
        assert engine.crop_params == {"rect": (0.0, 0.0, 1.0, 1.0),
                                      "rotation": 0.0}
        # 源替换语义仍生效：epoch 递增 + GPU 重上传 + 蒙版置脏
        assert engine._source_epoch == 4
        assert any(c[0] == RenderCommand.UPLOAD_IMAGE
                   for c in engine.renderer.commands)
        assert all(layer.mask_dirty for layer in engine.layers)

    def test_apply_changes_routes_through_bake_path(self, image_engine):
        engine = image_engine
        engine.renderer = _FakeRenderer()
        engine.img_full_original = np.full((4, 4, 3), 64, dtype=np.uint8)
        result = np.full((4, 4, 3), 128, dtype=np.uint8)
        engine.render_full_resolution = MagicMock(return_value=result)
        engine.current_file_path = "/tmp/x.png"
        called = {}
        engine._commit_baked_source = lambda f, h, l: called.update(
            shape=f.shape)

        engine.apply_changes()

        assert called.get("shape") == (4, 4, 3)
        assert engine.current_file_path == "/tmp/x.png"


class TestAud0611RawRedecodeEpoch:
    def test_raw_update_increments_source_epoch(self, image_engine):
        engine = image_engine
        engine.renderer = _FakeRenderer()
        engine._source_epoch = 11
        full = np.full((8, 8, 3), 90, dtype=np.uint8)
        high = np.full((4, 4, 3), 0.35, dtype=np.float32)
        low = np.full((2, 2, 3), 0.35, dtype=np.float32)

        engine._on_raw_update_finished(full, high, low,
                                       {"enabled": True, "user_wb": [1, 1, 1, 1]})

        assert engine._source_epoch == 12  # bake 前缀摘要盐已失效
        assert engine.img_full_original.shape == (8, 8, 3)
        assert engine._last_applied_raw_params["enabled"] is True
        assert all(layer.mask_dirty for layer in engine.layers)


# ---------------------------------------------------------------------------
# AUD-0700 / 0701 / 0702 —— layer.py
# ---------------------------------------------------------------------------


class TestAud0700BitmapCacheKey:
    def test_inplace_modification_changes_key(self):
        shape = MaskShape(MaskShape.BITMAP)
        shape.bitmap_data = np.zeros((4, 4), dtype=np.float32)
        k1 = shape._get_render_cache_key(8, 8, 16)
        shape.bitmap_data[0, 0] = 1.0  # 原位修改：id/shape 都不变
        k2 = shape._get_render_cache_key(8, 8, 16)
        assert k1 != k2

    def test_same_content_different_object_same_key(self):
        a, b = MaskShape(MaskShape.BITMAP), MaskShape(MaskShape.BITMAP)
        a.bitmap_data = np.full((4, 4), 0.5, dtype=np.float32)
        b.bitmap_data = np.full((4, 4), 0.5, dtype=np.float32)  # 新对象
        assert (a._get_render_cache_key(8, 8, 16) ==
                b._get_render_cache_key(8, 8, 16))

    def test_shape_cache_key_also_uses_fingerprint(self):
        shape = MaskShape(MaskShape.BITMAP)
        shape.bitmap_data = np.zeros((4, 4), dtype=np.float32)
        k1 = shape._get_shape_cache_key(8, 8, 16)
        shape.bitmap_data += 0.25
        assert shape._get_shape_cache_key(8, 8, 16) != k1


class TestAud0701FromDictGuards:
    def test_blend_mode_none_raises_clean_value_error(self):
        """层级结构损坏保持抛错契约（上层按项隔离丢弃，AUD-24200
        锁定），但由裸 AttributeError 升级为带诊断信息的 ValueError。"""
        with pytest.raises(ValueError, match="blend_mode"):
            Layer.from_dict({"name": "x", "blend_mode": None})

    def test_blend_mode_non_string_raises_clean_value_error(self):
        with pytest.raises(ValueError, match="blend_mode"):
            Layer.from_dict({"name": "x", "blend_mode": 14})

    def test_mask_shape_from_dict_missing_type_raises_value_error(self):
        with pytest.raises(ValueError, match="type"):
            MaskShape.from_dict({"id": "x", "feather": 10})

    def test_mask_shape_from_dict_non_dict_raises(self):
        with pytest.raises(ValueError, match="dict"):
            MaskShape.from_dict(["not", "a", "dict"])

    def test_corrupt_mask_items_isolated(self):
        data = {
            "name": "x",
            "mask_shapes": [
                {"type": "circle"},                  # 有效
                {"missing_type": 1},                 # 坏项 → 跳过
                "not-a-dict",                        # 坏项 → 跳过
                {"type": MaskGroup.TYPE, "children": [
                    {"type": "rect"},                # 组内有效
                    {"no_type": True},               # 组内坏项 → 跳过
                ]},
            ],
        }
        layer = Layer.from_dict(data)
        assert len(layer.mask_shapes) == 2
        assert isinstance(layer.mask_shapes[0], MaskShape)
        group = layer.mask_shapes[1]
        assert isinstance(group, MaskGroup) and len(group.children) == 1

    def test_corrupt_containers_degrade_locally(self):
        layer = Layer.from_dict({
            "name": "x",
            "grading_params": ["not", "a", "dict"],
            "curves_data": "garbage",
            "hsl_curve_data": 42,
            "mask_shapes": "also-garbage",
        })
        # 全部坏容器只局部降级，图层本身可用
        assert layer.grading_params.get("exposure", 0) == 0
        assert not layer.curves_data
        assert not layer.hsl_curve_data
        assert layer.mask_shapes == []

    def test_curve_points_survive_bad_points(self):
        lut = build_curves_lut_from_data({
            "M": [(0, 255), (128, 8), "bad", None, (7,)],  # 混入坏点
        })
        assert lut.shape == (256, 1, 3)
        assert np.isfinite(lut).all()


class TestAud0702PchipDuplicateX:
    def test_duplicate_x_no_value_error(self):
        # 重复 x 控制点：scipy 环境下旧实现抛
        # "x must be strictly increasing"
        lut = build_curves_lut_from_data(
            {"M": [(0, 255), (128, 10), (128, 200), (255, 0)]})
        assert lut.shape == (256, 1, 3)
        assert np.isfinite(lut).all()

    def test_unsorted_input_sorted_before_interp(self):
        unsorted_pts = [(255, 0), (64, 210), (0, 255), (192, 60)]
        sorted_pts = sorted(unsorted_pts)
        a = build_curves_lut_from_data({"M": unsorted_pts})
        b = build_curves_lut_from_data({"M": sorted_pts})
        assert np.allclose(a, b, atol=1e-6)

    def test_hsl_curve_duplicate_x_no_value_error(self):
        lut = build_hsl_curve_lut_from_data(
            {"HueSat": [(0, 128), (100, 20), (100, 90), (255, 128)]})
        assert lut.shape == (6, 256)
        assert np.isfinite(lut).all()

    def test_all_duplicate_x_degrades_to_default(self):
        # 全部同 x：无有效曲线 → 该通道按默认处理（不抛异常）
        lut = build_hsl_curve_lut_from_data({"HueHue": [(10, 5), (10, 9)]})
        assert np.allclose(lut[1], 0.0)  # HueHue 默认 0


# ---------------------------------------------------------------------------
# AUD-0710 / 0711 / 0712 / 0713 —— lut_exporter
# ---------------------------------------------------------------------------


class TestAud0710HsvHelpers:
    def test_hsv_roundtrip_all_sectors(self):
        from src.features.color.core.lut_exporter import LutExportWorker

        worker = LutExportWorker()
        rng = np.random.default_rng(3)
        rgb = rng.uniform(0, 1, size=(64, 3))
        hsv = worker._rgb_to_hsv(rgb)   # 旧实现在这里不会抛（问题在反向）
        back = worker._hsv_to_rgb(hsv)  # 旧实现：np.stack 混标量必抛
        assert back.shape == rgb.shape
        assert np.isfinite(back).all()

    def test_hue_shift_grading_path_no_exception(self):
        from src.features.color.core.lut_exporter import LutExportWorker

        worker = LutExportWorker()
        cube = np.stack(np.meshgrid(
            np.linspace(0, 1, 4), np.linspace(0, 1, 4),
            np.linspace(0, 1, 4), indexing="ij"), axis=-1)
        out = worker._apply_basic_grading(cube, {"hue_shift": 90.0})
        assert out.shape == cube.shape and np.isfinite(out).all()

    def test_mixer_path_no_exception(self):
        from src.features.color.core.lut_exporter import LutExportWorker

        worker = LutExportWorker()
        cube = np.stack(np.meshgrid(
            np.linspace(0, 1, 4), np.linspace(0, 1, 4),
            np.linspace(0, 1, 4), indexing="ij"), axis=-1)
        out = worker._apply_mixer_cpu(
            cube, {"red": [0.2, 0.0, 0.0], "blue": [0.0, -0.1, 0.05]})
        assert out.shape == cube.shape and np.isfinite(out).all()


class _FakeExportLayer:
    visible = True
    opacity = 1.0
    blend_mode = "Normal"
    curves_lut = None
    hsl_curve_lut = None
    mixer_params = {"red": [0.1, 0.0, 0.0]}
    soft_clip_params = {}
    lut_intensity = 1.0
    active_lut_path = None

    def __init__(self):
        self.grading_params = {"exposure": 10.0, "contrast": 5.0}


class TestAud0711AsyncExport:
    def test_generation_runs_on_worker_thread(self, qapp, tmp_path, monkeypatch):
        from PyQt6.QtCore import QThread

        from src.features.color.core.lut_exporter import (
            LutExportWorker,
            LutExporter,
        )

        threads: list[str] = []
        original = LutExportWorker.generate_lut_cpu

        def spy(self, *args, **kwargs):
            # QThread.currentThread：Qt 原生线程名（threading 模块无法
            # 识别 Qt 创建的线程）
            threads.append(QThread.currentThread().objectName())
            return original(self, *args, **kwargs)

        monkeypatch.setattr(LutExportWorker, "generate_lut_cpu", spy)

        exporter = LutExporter()
        try:
            assert exporter.worker.thread() is exporter.worker_thread

            out = tmp_path / "lut.cube"
            done = []
            exporter.export_lut(
                [_FakeExportLayer()], str(out), size=17, format_type="cube",
                on_finished=lambda _p: done.append(True))

            # 投递后 UI 线程立即返回；在事件循环上等待 worker 完成
            deadline = time.monotonic() + 60.0
            while not done and time.monotonic() < deadline:
                qapp.processEvents()
                QThread.msleep(20)

            assert done, "LUT export did not finish within timeout"
            assert out.exists() and out.stat().st_size > 0
            assert threads == [exporter.worker_thread.objectName()]
        finally:
            exporter.cleanup()

    def test_extract_params_deep_copies_nested_lists(self):
        from src.features.color.core.lut_exporter import LutExporter

        exporter = object.__new__(LutExporter)
        layer = _FakeExportLayer()
        params = exporter._extract_exportable_params(layer)
        assert params["mixer_params"]["red"] is not layer.mixer_params["red"]
        assert params["mixer_params"] is not layer.mixer_params


class TestAud0712ThreeDlRoundtrip:
    def test_3dl_header_roundtrips_through_own_loader(self, tmp_path):
        from src.features.color.core.lut_exporter import LutExportWorker
        from src.shared.color.lut_loader import load_lut

        worker = LutExportWorker()
        size = 17
        lut = worker._generate_identity_cube(size)
        path = tmp_path / "identity.3dl"
        worker._write_3dl_file(lut, size, str(path), "RT")

        first_code_line = next(
            ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#"))
        assert first_code_line == f"{size} {size} {size}"
        assert "TITLE" not in first_code_line  # .cube 方言不入首行

        loaded = load_lut(str(path))
        assert loaded.size == size
        assert loaded.table.shape == (size, size, size, 3)
        # loader 的表布局是 [b,g,r,c]（R 变化最快；lut_exporter._load_lut_file
        # 消费侧以 transpose(2,1,0,3) 转回 [r,g,b,c]）——按该约定比对
        assert np.allclose(loaded.table, lut.transpose(2, 1, 0, 3),
                           atol=1.0 / 255.0)


class TestAud0713LutLoadErrors:
    def test_missing_file_reported_not_swallowed(self, tmp_path, caplog):
        from src.features.color.core.lut_exporter import LutExportWorker

        with caplog.at_level(logging.WARNING, logger="src.features.color.core.lut_exporter"):
            arr, size = LutExportWorker()._load_lut_file(
                str(tmp_path / "missing.cube"))
        assert arr is None and size == 0
        assert any("missing" in r.message for r in caplog.records)

    def test_corrupt_file_reported_not_swallowed(self, tmp_path, caplog):
        from src.features.color.core.lut_exporter import LutExportWorker

        bad = tmp_path / "bad.cube"
        bad.write_text("LUT_3D_SIZE 3\n0.0 0.0\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="src.features.color.core.lut_exporter"):
            arr, size = LutExportWorker()._load_lut_file(str(bad))
        assert arr is None and size == 0
        assert any("failed to load" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# AUD-0800 —— plugin_manager 注册表线程安全
# ---------------------------------------------------------------------------

_TGLSL = """// @name {name}
// @author T
// @param slider strength "Strength" 0.5 0.0 1.0
vec3 process(vec3 color, vec2 uv) {{
    return color * strength;
}}
"""


def _fresh_manager():
    from src.features.color.core.plugin_manager import PluginManager

    pm = object.__new__(PluginManager)
    pm._initialized = False
    pm.__init__()
    return pm


class TestAud0800RegistryLocking:
    def test_scan_snapshot_then_merge(self, tmp_path):
        (tmp_path / "a.tglsl").write_text(_TGLSL.format(name="A"),
                                          encoding="utf-8")
        pm = _fresh_manager()
        result = pm.scan_plugins(str(tmp_path))
        assert "a" in result and pm.get_plugin("a")["name"] == "A"
        assert pm.get_all_plugins() is pm.plugins

    def test_reload_replaces_registry_atomically(self, tmp_path):
        (tmp_path / "a.tglsl").write_text(_TGLSL.format(name="A"),
                                          encoding="utf-8")
        pm = _fresh_manager()
        pm.scan_plugins(str(tmp_path))
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        pm.reload_plugins(str(empty_dir))
        assert "a" not in pm.get_all_plugins()
        assert pm.get_plugin("a") is None

    def test_scan_merge_semantics_preserved(self, tmp_path):
        d1 = tmp_path / "one"
        d2 = tmp_path / "two"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.tglsl").write_text(_TGLSL.format(name="A"), encoding="utf-8")
        (d2 / "b.tglsl").write_text(_TGLSL.format(name="B"), encoding="utf-8")
        pm = _fresh_manager()
        pm.scan_plugins(str(d1))
        pm.scan_plugins(str(d2))
        assert {"a", "b"} <= set(pm.get_all_plugins())


# ---------------------------------------------------------------------------
# AUD-0830 / 0831 / 0832 —— shader_compiler
# ---------------------------------------------------------------------------

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")


def _plugin_binding(pid: str, src: str, content_hash: str, index: int = 0):
    from src.features.composite.nodes.base_node import ParamDef
    from src.features.shared.effects.effect_spec import (
        EffectSpec,
        PluginBinding,
        ShaderKind,
    )

    spec = EffectSpec(
        effect_type=f"plugin:{pid}",
        name=pid,
        category="plugin",
        param_defs=(ParamDef(name="strength", ptype="float", default=0.5),),
        shader_kind=ShaderKind.DCTL_PROCESS,
        shader_sources=(src,),
        dispatch_key=f"plugin:{pid}",
        param_to_uniform={},
        plugin_id=f"plugin:{pid}",
        plugin_content_hash=content_hash,
    )
    return PluginBinding(spec=spec, values={"strength": 0.5}, index=index)


_PLUGIN_SRC = """vec3 process(vec3 color, vec2 uv) {
    float strength = u_PLACEHOLDER;  // replaced below
    return color * strength;
}
""".replace("u_PLACEHOLDER", "strength")


class TestAud0830ContentHashKey:
    def test_content_change_creates_new_cache_entry(self):
        from src.features.color.core.shader_compiler import ShaderPipeline

        ctx = moderngl.create_standalone_context(require=330)
        try:
            pipeline = ShaderPipeline(ctx)
            b1 = _plugin_binding("p", _PLUGIN_SRC, "hash-1")
            b2 = _plugin_binding("p", _PLUGIN_SRC.replace("0.5", "0.6"),
                                 "hash-2")
            assert pipeline.build_composition_shader(plugins=[b1]) is not None
            assert pipeline.build_composition_shader(plugins=[b2]) is not None
            # 结构键相同但内容不同 → 两个缓存条目（不再命中旧 program）
            assert len(pipeline.comp_cache) == 2
            pipeline.release()
        finally:
            ctx.release()

    def test_same_content_hits_cache(self):
        from src.features.color.core.shader_compiler import ShaderPipeline

        ctx = moderngl.create_standalone_context(require=330)
        try:
            pipeline = ShaderPipeline(ctx)
            b1 = _plugin_binding("p", _PLUGIN_SRC, "hash-1")
            p1 = pipeline.build_composition_shader(plugins=[b1])
            p2 = pipeline.build_composition_shader(plugins=[b1])
            assert p1 is p2 and len(pipeline.comp_cache) == 1
            pipeline.release()
        finally:
            ctx.release()


class TestAud0831CacheBounded:
    def test_lru_eviction_caps_cache(self, monkeypatch):
        from src.features.color.core.shader_compiler import ShaderPipeline

        monkeypatch.setattr(ShaderPipeline, "MAX_COMP_CACHE_PROGRAMS", 2)
        ctx = moderngl.create_standalone_context(require=330)
        try:
            pipeline = ShaderPipeline(ctx)
            assert pipeline.build_composition_shader(has_curves=True) is not None
            assert pipeline.build_composition_shader(
                has_hsl_curve=True) is not None
            assert len(pipeline.comp_cache) == 2
            first_key = next(iter(pipeline.comp_cache))
            mixer_prog = pipeline.build_composition_shader(has_mixer=True)
            assert mixer_prog is not None
            assert len(pipeline.comp_cache) == 2          # 容量封顶
            assert first_key not in pipeline.comp_cache   # 最旧被淘汰
            # 命中刷新 LRU 顺序：最近命中的 program 不被下一轮淘汰
            assert pipeline.build_composition_shader(
                has_mixer=True) is mixer_prog
            assert pipeline.build_composition_shader(
                has_lut3d=True) is not None
            assert len(pipeline.comp_cache) == 2
            assert any(p is mixer_prog for p in pipeline.comp_cache.values())
            pipeline.release()
        finally:
            ctx.release()


class TestAud0832ReservedParamNames:
    def test_builtin_named_param_rejected_and_clean_compiles(self):
        from src.features.composite.nodes.base_node import ParamDef
        from src.features.shared.effects.effect_spec import (
            EffectSpec,
            PluginBinding,
            ShaderKind,
        )
        from src.features.color.core.shader_compiler import ShaderPipeline

        ctx = moderngl.create_standalone_context(require=330)
        try:
            pipeline = ShaderPipeline(ctx)
            src = ("vec3 process(vec3 color, vec2 uv) { "
                   "return mix(color, vec3(1.0), strength); }")
            spec = EffectSpec(
                effect_type="plugin:bad", name="bad", category="plugin",
                param_defs=(ParamDef(name="mix", ptype="float", default=0.5),),
                shader_kind=ShaderKind.DCTL_PROCESS,
                shader_sources=(src,),
                dispatch_key="plugin:bad", param_to_uniform={},
                plugin_id="plugin:bad", plugin_content_hash="h")
            prog = pipeline.build_composition_shader(
                plugins=[PluginBinding(spec=spec, values={"mix": 0.5})])
            assert prog is None  # 显式拒绝而非产出被破坏的源
            assert not pipeline.comp_cache
        finally:
            ctx.release()

    def test_signature_param_name_rejected(self):
        from src.features.composite.nodes.base_node import ParamDef
        from src.features.shared.effects.effect_spec import (
            EffectSpec,
            PluginBinding,
            ShaderKind,
        )
        from src.features.color.core.shader_compiler import ShaderPipeline

        ctx = moderngl.create_standalone_context(require=330)
        try:
            pipeline = ShaderPipeline(ctx)
            src = ("vec3 process(vec3 color, vec2 uv) { "
                   "return color + vec3(color_param); }")
            spec_ok = EffectSpec(
                effect_type="plugin:ok3", name="ok3", category="plugin",
                param_defs=(ParamDef(name="color_param", ptype="float",
                                     default=0.5),),
                shader_kind=ShaderKind.DCTL_PROCESS,
                shader_sources=(src,),
                dispatch_key="plugin:ok3", param_to_uniform={},
                plugin_id="plugin:ok3", plugin_content_hash="h")
            # @param 与签名形参 color 撞名：改写会重命名形参 → 拒绝
            spec_bad = EffectSpec(
                effect_type="plugin:bad3", name="bad3", category="plugin",
                param_defs=(ParamDef(name="color", ptype="float",
                                     default=0.5),),
                shader_kind=ShaderKind.DCTL_PROCESS,
                shader_sources=(src.replace("color_param", "color"),),
                dispatch_key="plugin:bad3", param_to_uniform={},
                plugin_id="plugin:bad3", plugin_content_hash="h")
            assert pipeline.build_composition_shader(
                plugins=[PluginBinding(spec=spec_ok, values={})]) is not None
            assert pipeline.build_composition_shader(
                plugins=[PluginBinding(spec=spec_bad, values={})]) is None
        finally:
            ctx.release()

    def test_clean_plugin_still_compiles(self):
        from src.features.color.core.shader_compiler import ShaderPipeline

        ctx = moderngl.create_standalone_context(require=330)
        try:
            pipeline = ShaderPipeline(ctx)
            prog = pipeline.build_composition_shader(
                plugins=[_plugin_binding("ok", _PLUGIN_SRC, "h")])
            assert prog is not None
            assert "u_P0_strength" in prog  # 参数已命名空间化为 uniform
        finally:
            ctx.release()


# ---------------------------------------------------------------------------
# AUD-0900 / AUD-0903 —— denoise
# ---------------------------------------------------------------------------


class TestAud0900SurfaceCacheNoUseAfterRelease:
    def test_failed_recreate_drops_released_surface(self, monkeypatch):
        import src.features.color.core.video_denoise_gl as vdg

        attempts: list[tuple[int, int]] = []

        class FakeSurface:
            def __init__(self, ctx, w, h, comp=4, dtype="f2"):
                attempts.append((w, h))
                self.size = (w, h)
                self.released = False

            def release(self):
                self.released = True

        class FailBigSurface(FakeSurface):
            def __init__(self, ctx, w, h, comp=4, dtype="f2"):
                if (w, h) == (8, 8):
                    raise RuntimeError("simulated surface OOM")
                super().__init__(ctx, w, h, comp, dtype)

        monkeypatch.setattr(vdg, "RenderSurface", FailBigSurface)
        cache = vdg.RenderSurfaceCache(ctx=object())

        s4 = cache.get("k", 4, 4)
        assert s4 is not None and s4.size == (4, 4)
        # 尺寸变化：旧表面 release 后新表面创建失败
        assert cache.get("k", 8, 8) is None
        assert s4.released
        # 旧尺寸再次请求：不得命中已释放表面（旧实现返回 s4）
        assert cache.peek("k") is None
        again = cache.get("k", 4, 4)
        assert again is not s4 and again is not None


class TestAud0903StatsSnapshot:
    def test_stats_predefines_window_frames(self):
        import inspect

        import src.features.color.core.video_denoise_pipeline as vdp

        src = inspect.getsource(vdp.VideoDenoisePipeline)
        assert '"window_frames": 0' in src           # __init__ 预置全部键
        assert '.get("window_frames", 0)' not in src  # 运行期不再插入新键

    def test_process_window_increments_in_place(self, monkeypatch):
        import src.features.color.core.video_denoise_pipeline as vdp

        monkeypatch.setattr(vdp, "QuadRenderer", MagicMock())
        monkeypatch.setattr(vdp, "RenderSurfaceCache", MagicMock())
        monkeypatch.setattr(vdp, "VideoSpatialDenoiser", MagicMock())
        monkeypatch.setattr(vdp, "VideoTemporalDenoiser", MagicMock())

        pipeline = vdp.VideoDenoisePipeline.__new__(vdp.VideoDenoisePipeline)
        pipeline._temporal = MagicMock()
        src_tex = object()
        pipeline._temporal.process_window.return_value = src_tex
        pipeline._debug_overlay_flag = False
        pipeline._spatial_flag = False
        pipeline._enabled_flag = True
        pipeline._temporal_flag = True
        pipeline._quality_mode = "preview"

        temporal = MagicMock()
        temporal.is_active.return_value = True
        spatial = MagicMock()
        spatial.is_active.return_value = False
        quality = MagicMock()
        quality.temporal = temporal
        quality.spatial = spatial
        params = MagicMock()
        params.for_quality.return_value = quality
        pipeline._params = params

        pipeline.stats = {
            "frames": 0,
            "window_frames": 0,
            "temporal_ms": 0.0, "spatial_ms": 0.0, "total_ms": 0.0,
            "last_temporal_ms": 0.0, "last_spatial_ms": 0.0,
            "last_total_ms": 0.0,
        }

        # 渲染线程更新 + 另一线程快照读取并发不抛 dict-changed-size
        snapshot_errors: list[Exception] = []
        stop = threading.Event()

        def snapshot_reader():
            while not stop.is_set():
                try:
                    dict(pipeline.stats)
                except RuntimeError as e:  # pragma: no cover - 旧缺陷路径
                    snapshot_errors.append(e)

        reader = threading.Thread(target=snapshot_reader)
        reader.start()
        try:
            for _ in range(200):
                pipeline.process_window(src_tex, [])
        finally:
            stop.set()
            reader.join()

        assert pipeline.stats["window_frames"] == 200
        assert snapshot_errors == []


# ---------------------------------------------------------------------------
# numpy HSV 镜像（标准公式，用于 AUD-0840/0910 的语义性质验证）
# ---------------------------------------------------------------------------


def _rgb_to_hsv_np(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    max_c = rgb.max(axis=-1)
    min_c = rgb.min(axis=-1)
    diff = max_c - min_c
    v = max_c
    s = np.where(max_c > 1e-10, diff / np.maximum(max_c, 1e-10), 0.0)
    h = np.zeros_like(max_c)
    mask_r = (rgb[..., 0] == max_c) & (diff > 1e-10)
    mask_g = (rgb[..., 1] == max_c) & (diff > 1e-10) & ~mask_r
    mask_b = (rgb[..., 2] == max_c) & (diff > 1e-10) & ~mask_r & ~mask_g
    h[mask_r] = ((rgb[..., 1] - rgb[..., 2])[mask_r] / diff[mask_r]) % 6.0
    h[mask_g] = (rgb[..., 2] - rgb[..., 0])[mask_g] / diff[mask_g] + 2.0
    h[mask_b] = (rgb[..., 0] - rgb[..., 1])[mask_b] / diff[mask_b] + 4.0
    return np.stack([h / 6.0, s, v], axis=-1)


def _hsv_to_rgb_np(hsv: np.ndarray) -> np.ndarray:
    hsv = np.asarray(hsv, dtype=np.float64)
    h, s, v = hsv[..., 0] * 6.0, hsv[..., 1], hsv[..., 2]
    i = np.floor(h).astype(int) % 6
    f = h - np.floor(h)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [v, q, p, p, t, v])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [t, v, v, q, p, p])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def _glsl_rgb2hsv_exact(c: np.ndarray) -> np.ndarray:
    """逐行镜像 COMMON_DEFINITIONS.rgb2hsv 的 K 向量公式（标量循环）。

    负值输入下这是"旧语义"的忠实复刻（会产生负饱和度/失真色相）。
    mix(a, b, t) 按 GLSL 语义：t>=1 取 b；swizzle 逐位对应。
    """
    out = np.zeros_like(c, dtype=np.float64)
    for i, (r, g, b) in enumerate(np.asarray(c, dtype=np.float64)):
        # p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g))
        if g >= b:
            p = (g, b, 0.0, -1.0 / 3.0)
        else:
            p = (b, g, -1.0, 2.0 / 3.0)
        # q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r))
        if r >= p[0]:
            q = (r, p[1], p[2], p[0])
        else:
            q = (p[0], p[1], p[3], r)
        d = q[0] - min(q[3], q[1])
        e = 1.0e-10
        out[i] = (abs(q[2] + (q[3] - q[1]) / (6.0 * d + e)),
                  d / (q[0] + e), q[0])
    return out


def _glsl_hsv2rgb_exact(c: np.ndarray) -> np.ndarray:
    """逐行镜像 COMMON_DEFINITIONS.hsv2rgb（标量循环）。"""
    out = np.zeros_like(c, dtype=np.float64)
    k = (1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0)
    for i, (h, s, v) in enumerate(np.asarray(c, dtype=np.float64)):
        chan = []
        for kj in k[:3]:
            f = h + kj
            f -= np.floor(f)
            p = abs(f * 6.0 - 3.0)
            mixed = (1.0 - s) + s * min(max(p - 1.0, 0.0), 1.0)
            chan.append(v * mixed)
        out[i] = chan
    return out


def _shift_hue(hsv: np.ndarray, shift_deg: float) -> np.ndarray:
    hsv = hsv.copy()
    hsv[..., 0] = (hsv[..., 0] + shift_deg / 360.0) % 1.0
    return hsv


def _mirror_hue_shift(color: np.ndarray, shift_deg: float) -> np.ndarray:
    """修复后的 hue_shift 语义镜像（AUD-0840 的 abs + sign 还原）。"""
    color = np.asarray(color, dtype=np.float64)
    sign_c = np.sign(color)
    return sign_c * _hsv_to_rgb_np(_shift_hue(_rgb_to_hsv_np(np.abs(color)),
                                              shift_deg))


def _mirror_hue_shift_legacy(color: np.ndarray, shift_deg: float) -> np.ndarray:
    """修复前的 hue_shift：GLSL 公式对可能为负的 color 直接转换。"""
    return _glsl_hsv2rgb_exact(
        _shift_hue(_glsl_rgb2hsv_exact(color), shift_deg))
