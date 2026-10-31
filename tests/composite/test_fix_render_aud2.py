"""批 A（AUD-7906/7907/80001/8701）修复回归测试。

覆盖：
1. AUD-7906/7907 — ai_compute_manager 的 cv2 导入：模块级守护导入 +
   _require_cv2 降级路径；_generate_preview / _prepare_outpaint_input
   在修复前引用未导入的 cv2，任何默认参数调用必然 NameError。
2. AUD-80001 — 瓦片渲染跨瓦片 SOURCE 缓存复用：clear_export_cache
   不清 SOURCE（旧缺陷机制复现），新 helper _clear_tile_scoped_cache
   必须把 SOURCE 一并逐出。
3. AUD-8701 — Stretch 变形 NaN/inf 防护：UI 允许的参数组合
   （amount∈[-5,5]、strength∈[0,5]）下输出必须全部有限，且合法参数
   的变形语义不变；出口有限性过滤兜底退化输入。

除 AI worker 用例需要 QApplication（QThread 构造）外，纯 numpy 逻辑
无 GL 依赖。
"""
import sys
import os
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render import ai_compute_manager as acm
from src.features.composite.render.cache_manager import (
    CacheCategory,
    CacheEntry,
    CacheManager,
)
from src.features.composite.render.composite_export_renderer import (
    CompositeExportRenderer,
)
from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d.deformers import (
    DEFORMER_STRETCH,
    DeformerSpec,
    evaluate_deformer,
)

pytestmark = pytest.mark.composite_core


# ── AUD-7906 / AUD-7907：cv2 未导入 ─────────────────────────────────

class TestCv2Import:
    def test_module_binds_cv2(self):
        """模块级守护导入后 cv2 必须可用（修复前为 NameError 根因）。"""
        import cv2
        assert acm.cv2 is cv2

    def test_prepare_outpaint_input_runs(self, qapp):
        """修复前 _prepare_outpaint_input 引用未导入的 cv2 → NameError。"""
        worker = acm.AIComputeWorker(
            "k", "ai_expand", {}, params={}, cache_manager=object())
        image = np.linspace(0.0, 1.0, 64 * 80 * 3,
                            dtype=np.float32).reshape(64, 80, 3)
        canvas, mask, expand_params = worker._prepare_outpaint_input(
            image,
            {"expand_left": 8, "expand_right": 8,
             "expand_top": 4, "expand_bottom": 4, "blend_radius": 4})
        assert canvas.shape == (64 + 8, 80 + 16, 3)
        assert mask.shape == (64 + 8, 80 + 16)
        assert mask.dtype == np.float32
        assert expand_params["orig_w"] == 80
        # 原图区域 mask=0（不重绘），边界 mask>0（AI 生成区）
        assert mask[4:68, 8:88].max() == 0.0

    def test_generate_preview_runs(self, qapp):
        """修复前 _generate_preview 引用未导入的 cv2 → NameError。"""
        worker = acm.AIComputeWorker(
            "k", "ai_denoise", {}, params={}, cache_manager=object())

        class _Port:
            name = "input"

        class _FakeSession:
            def get_inputs(self):
                return [_Port()]

            def get_outputs(self):
                return [_Port()]

            def run(self, names, feeds):
                # ONNX Runtime 语义：返回输出列表
                return [np.ascontiguousarray(feeds["input"])]

        image = np.linspace(0.0, 1.0, 64 * 80 * 3,
                            dtype=np.float32).reshape(64, 80, 3)
        worker._generate_preview(image, None, _FakeSession(), 0.5)
        assert worker._preview_result is not None
        assert worker._preview_result.shape[:2] == (32, 40)

    def test_require_cv2_missing_raises_actionable_error(self, monkeypatch):
        """cv2 缺失时的明确降级：可操作的 ImportError 信息。"""
        monkeypatch.setattr(acm, "cv2", None)
        with pytest.raises(ImportError, match="opencv-python"):
            acm._require_cv2()

    def test_run_fails_fast_with_clear_message_when_cv2_missing(
            self, qapp, monkeypatch):
        """run() 首行做 cv2 门禁：缺失时报错信号携带可操作信息，
        而不是在推理中途 NameError/AttributeError。"""
        monkeypatch.setattr(acm, "cv2", None)
        monkeypatch.setattr(
            acm.traceback, "print_exc", lambda *a, **k: None)
        worker = acm.AIComputeWorker(
            "k", "ai_inpaint",
            {"image": np.zeros((4, 4, 3), dtype=np.float32)},
            params={}, cache_manager=object())
        errors = []
        worker.error.connect(lambda key, msg, rid: errors.append(msg))
        worker.run()
        assert len(errors) == 1
        assert "opencv-python" in errors[0]


# ── AUD-80001：跨瓦片 SOURCE 缓存复用 ───────────────────────────────

class _DummyPool:
    """release_texture_by_id no-op（条目不带 owned id 时不会被触达）。"""

    def release_texture_by_id(self, tex_id: int) -> None:
        pass


def _make_cache_with_source_and_export() -> CacheManager:
    cm = CacheManager(resource_pool=_DummyPool(),
                      max_entries=16, max_memory_mb=64.0)
    # key 形如 "node_type:node_id:..."，类别由 node_type 决定
    cm.put("image_input:n1:sig", CacheEntry(outputs={},
                                            owned_texture_ids=set()),
           render_mode="export")
    cm.put("blur:n2:sig", CacheEntry(outputs={},
                                     owned_texture_ids=set()),
           render_mode="export")
    return cm


def _fake_snapshot(nodes):
    """最小 snapshot 替身：_clear_tile_scoped_cache 只读 nodes 的
    node_type / node_id。"""
    from types import SimpleNamespace
    return SimpleNamespace(nodes={
        node_id: SimpleNamespace(node_id=node_id, node_type=node_type)
        for node_id, node_type in nodes.items()
    })


class TestTileScopedCache:
    def test_clear_export_cache_leaves_source(self):
        """旧缺陷机制复现：瓦片间 clear_export_cache 只清 EXPORT，
        首瓦片裁剪的 SOURCE 条目原样存活，后续瓦片以相同 key 命中。"""
        cm = _make_cache_with_source_and_export()
        cm.clear_export_cache()
        remaining = list(cm._cache.keys())
        assert remaining == ["image_input:n1:sig"]
        assert cm._cache[remaining[0]].category == CacheCategory.SOURCE

    def test_clear_tile_scoped_cache_evicts_source_and_ai(self):
        """新 helper 必须逐出 SOURCE/AI 节点的瓦片裁剪条目。"""
        cm = _make_cache_with_source_and_export()
        cm.put("ai_inpaint:n3:sig", CacheEntry(outputs={},
                                               owned_texture_ids=set()),
               render_mode="export")
        renderer = object.__new__(CompositeExportRenderer)
        renderer._cache_manager = cm
        renderer._snapshot = _fake_snapshot({
            "n1": "image_input", "n2": "blur", "n3": "ai_inpaint"})
        renderer._clear_tile_scoped_cache()
        assert set(cm._cache.keys()) == {"blur:n2:sig"}

    def test_clear_tile_scoped_cache_no_manager_or_snapshot(self):
        """_cache_manager / _snapshot 缺失（未初始化）时静默 no-op。"""
        renderer = object.__new__(CompositeExportRenderer)
        renderer._cache_manager = None
        renderer._snapshot = None
        renderer._clear_tile_scoped_cache()  # must not raise
        renderer._cache_manager = _make_cache_with_source_and_export()
        renderer._snapshot = None
        renderer._clear_tile_scoped_cache()  # no snapshot → no-op
        assert len(renderer._cache_manager._cache) == 2

    def test_tile_loops_clear_source_before_evaluating(self):
        """三条瓦片循环必须在 _evaluate_snapshot 之前调用 helper，
        且循环体内不得出现 between-tile clear_export_cache（会与
        SOURCE 逐出叠加触发纹理池回收损坏，见 helper docstring）。"""
        import ast
        import inspect
        from src.features.composite.render import composite_export_renderer as cer
        tree = ast.parse(inspect.getsource(cer))
        methods = {
            n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name in ("_render_tiled_gpu_stitch",
                           "_render_tiled_with_plan", "_render_tiled")
        }
        assert len(methods) == 3
        for name, func in methods.items():
            tile_loops = [
                n for n in ast.walk(func)
                if isinstance(n, ast.For)
                and isinstance(n.target, ast.Name) and n.target.id == "tile"
            ]
            assert tile_loops, name

            def call_names(node):
                return [m.func.attr for m in ast.walk(node)
                        if isinstance(m, ast.Call)
                        and isinstance(m.func, ast.Attribute)]

            for loop in tile_loops:
                names = call_names(loop)
                assert "_clear_tile_scoped_cache" in names, name
                assert "_evaluate_snapshot" in names, name
                assert names.index("_clear_tile_scoped_cache") < \
                    names.index("_evaluate_snapshot"), name
                assert "clear_export_cache" not in names, name

    def test_gpu_stitch_tiles_render_distinct_content(self, monkeypatch):
        """审计要求的端到端回归：两瓦片内容必须不同。

        渐变 constant（SOURCE 类节点）经 GPU-stitch 渲染时，若瓦片 2
        命中瓦片 1 裁剪的 SOURCE 缓存，右半画面会复刻左半内容；修复
        后瓦片拼接必须与全帧参考一致。无 GL 环境跳过。
        """
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
            ctx.release()
        except Exception as exc:  # pragma: no cover - CI GPU availability
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
        from src.features.composite.core.node_engine import NodeGraph

        W, H = 2560, 1440
        data = {
            "nodes": [
                {"id": "bg", "type": "constant", "parameters": {
                    "width": W, "height": H, "mode": "gradient",
                    "color": [0.0, 0.0, 0.0, 1.0],
                    "color2": [1.0, 1.0, 1.0, 1.0]}},
                {"id": "out", "type": "output", "parameters": {
                    "format_mode": "custom", "width": W, "height": H}},
            ],
            "connections": [{
                "src_node_id": "bg", "src_port_name": "result",
                "dst_node_id": "out", "dst_port_name": "image"}],
        }
        snap = NodeGraph.from_dict(data).snapshot(
            color_texture_id=0, color_width=0, color_height=0,
            color_revision=0)

        renderer = CompositeExportRenderer()
        renderer.set_persistent_mode(True)
        try:
            reference = renderer.render_and_readback(
                snap, readback_output="srgb_u8")
            assert not reference.error, reference.error
            assert reference.preview_strategy == "full_frame"

            monkeypatch.setattr(CompositeExportRenderer,
                                "FULL_FRAME_TRANSIENT_FACTOR", 10 ** 9)
            tiled = renderer.render_and_readback(
                snap, readback_output="srgb_u8")
            assert not tiled.error, tiled.error
            assert tiled.preview_strategy == "tiled_gpu_stitch"
            assert tiled.tile_count >= 2
            # 左右两半必须不同（渐变横跨瓦片）
            left = tiled.image[:, :W // 2].astype(np.int32)
            right = tiled.image[:, W // 2:].astype(np.int32)
            assert abs(left.mean() - right.mean()) > 5
            # 与全帧参考逐像素一致（瓦片拼接不改变内容）
            diff = np.abs(tiled.image.astype(np.int32)
                          - reference.image.astype(np.int32))
            assert diff.max() <= 2
        finally:
            renderer.cleanup()


# ── AUD-8701：Stretch NaN/inf 防护 ──────────────────────────────────

class TestStretchFinite:
    @pytest.mark.parametrize(
        "amount,strength,label",
        [
            # 审计原始案例：stretch_f = 1 + (0.5-1)*2.5 = -0.25 → sqrt 负数
            (0.5, 2.5, "audit-negative-sqrt"),
            # stretch_f = 0 → 1/0 = inf
            (0.0, 1.0, "zero-divide"),
            # UI 极端角落：amount=-5、strength=5 → stretch_f=-29
            (-5.0, 5.0, "ui-corner-negative"),
        ])
    def test_ui_range_produces_finite_vertices(self, amount, strength, label):
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_STRETCH,
                            strength=strength, axis="y",
                            params={"amount": amount})
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with np.errstate(all="raise"):
                verts, normals = evaluate_deformer(
                    spec, cube.vertices, cube.normals)
        assert verts.dtype == np.float32 and normals.dtype == np.float32
        assert np.isfinite(verts).all(), label
        assert np.isfinite(normals).all(), label
        # 防护不应退化成恒等变形（仍执行了可用的拉伸/压缩分量）
        assert not np.allclose(verts, cube.vertices)

    def test_legal_params_semantics_unchanged(self):
        """合法参数（amount=1.5, strength=1.0）下体积守恒近似语义保持。"""
        cube = prim.cube(1.0)
        spec = DeformerSpec(deformer_type=DEFORMER_STRETCH,
                            strength=1.0, axis="y", params={"amount": 1.5})
        verts, _ = evaluate_deformer(spec, cube.vertices, cube.normals)
        assert np.ptp(verts[:, 1]) > np.ptp(cube.vertices[:, 1])
        assert np.ptp(verts[:, 0]) < np.ptp(cube.vertices[:, 0]) + 1e-6

    def test_exit_filter_sanitizes_nonfinite_input_rows(self):
        """出口有限性过滤：输入网格本身损坏（NaN/inf 行）时，
        非有限顶点回退原坐标并归零兜底，保证无非有限值上传 GPU。"""
        cube = prim.cube(1.0)
        verts = cube.vertices.copy()
        verts[3] = np.nan
        verts[7, 1] = np.inf
        spec = DeformerSpec(deformer_type=DEFORMER_STRETCH,
                            strength=2.5, params={"amount": 0.5})
        out_v, out_n = evaluate_deformer(spec, verts, cube.normals)
        assert np.isfinite(out_v).all()
        assert np.isfinite(out_n).all()
        assert np.allclose(out_v[3], 0.0)
        assert np.isfinite(out_v[7]).all()

    def test_stretch_factor_floor_constant(self):
        """epsilon 下界存在且为正（squash 上界 = 1/sqrt(eps)）。"""
        from src.features.composite.render3d.deformers import cpu
        assert cpu._STRETCH_FACTOR_EPS > 0.0
        assert 1.0 / np.sqrt(cpu._STRETCH_FACTOR_EPS) < 1e4
