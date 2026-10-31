"""wd6-g34 回归测试 — 批 D（composite nodes 域）16 条 Major 中白名单内 9 条。

覆盖（对照 REMEDIATION_PLAN.md 行 1573-1591）：
- AUD-64003 scene payload 收集不再原地改写共享 payload.node_id，宽泛 except 收窄
- AUD-64010 MergeNode 显式 BYPASS_MAPPING（禁用 → 输出 B）
- AUD-64013 blur 半径上限钳制（ROI / tile padding / 求值）
- AUD-64015 cam_fov 读取钳制 [1,170]（投影矩阵无 inf）
- AUD-6504 Constant width/height 上限按 PARAM_DEFS 截断
- AUD-6605 Deflicker 历史缓冲按字节预算封顶（窗口帧永不逐出）
- AUD-6607 Deformer3D GL context 身份守卫（换 ctx 弃缓存 + 重建 program）
- AUD-6609 Denoise quality 档位经共享 tier 契约生效（marker/ROI/padding）
- AUD-6705 FaceTrack rerun_on_evaluate 静默写 landmarks（无 dirty/revision/state 副作用）

白名单外 7 条（AUD-6710/6711、AUD-6800/6801、AUD-6804、AUD-6809、AUD-6900）
目标文件不在本批白名单内，见 docs/plans/2026-09-28-multi-machine-program/fixes/reports/wd6-g34.md。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dataclasses import dataclass

import numpy as np
import pytest

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.format import BBox
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.blur_node import BlurNode, _RADIUS_MAX, _RADIUS_XY_MAX
from src.features.composite.nodes.camera3d_node import Camera3DNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.nodes.deflicker_node import (
    DeflickerNode, _BUFFER_MAX_BYTES,
)
from src.features.composite.nodes.deformer3d_node import Deformer3DNode
from src.features.composite.nodes.denoise_node import DenoiseNode
from src.features.composite.nodes.face_track_node import FaceTrackNode
from src.features.composite.nodes.blend_node import MergeNode
from src.features.composite.nodes.node_state import NodeState
from src.features.composite.render3d.scene_payload import CameraPayload


def _tex(texture_id: int = 1, w: int = 8, h: int = 8) -> TextureResult:
    return TextureResult(texture_id=texture_id, width=w, height=h)


# ── AUD-64003: scene payload node_id 注入不改写共享对象 ───────────────────

class TestAud64003ScenePayloadInjection:

    def _ctx_two_cameras(self) -> CompositeEvalContext:
        # 与生产一致：Camera3D 发射的 payload 不带 node_id（默认 ""），
        # 由收集方注入。
        def cam() -> CameraPayload:
            return CameraPayload(proj=np.eye(4), view=np.eye(4),
                                 eye=(0, 0, 5), target=(0, 0, 0))

        return CompositeEvalContext(gl="fake", scene_payloads={
            "cam1": {"display": cam()},
            "cam2": {"display": cam()},
        })

    def test_collect_by_type_returns_copy_shared_payload_untouched(self):
        ctx = self._ctx_two_cameras()
        stored = ctx.scene_payloads["cam1"]["display"]
        agg = NODE_REGISTRY["scene3d"]("agg")

        collected = agg._collect_scene_payload_by_type(ctx, CameraPayload)

        assert isinstance(collected, CameraPayload)
        assert collected is not stored
        assert collected.node_id == "cam1"
        # AUD-64003 核心：共享 payload 不被原地改写。
        assert stored.node_id == ""
        assert ctx.scene_payloads["cam2"]["display"].node_id == ""

    def test_repeated_collection_is_stable(self):
        """两个聚合器先后收集同一上游：后者不再被前者污染（幂等）。"""
        ctx = self._ctx_two_cameras()
        agg = NODE_REGISTRY["scene3d"]("agg")
        first = agg._collect_scene_payload_by_type(ctx, CameraPayload)
        second = agg._collect_scene_payload_by_type(ctx, CameraPayload)
        assert first.node_id == "cam1"
        assert second.node_id == "cam1"

    def test_connected_collect_injects_on_copy(self):
        ctx = self._ctx_two_cameras()
        ctx.scene_connections = {"agg": {"in": ["cam1"]}}
        stored = ctx.scene_payloads["cam1"]["display"]
        agg = NODE_REGISTRY["scene3d"]("agg")

        result = agg._collect_connected_scene_payloads(ctx, CameraPayload)

        assert len(result) == 1
        assert result[0].node_id == "cam1"
        assert result[0] is not stored
        assert stored.node_id == ""

    def test_matching_node_id_returns_same_object(self):
        """node_id 已正确时无需复制（幂等捷径）。"""
        payload = CameraPayload(proj=np.eye(4), view=np.eye(4),
                                eye=(0, 0, 5), target=(0, 0, 0),
                                node_id="cam1")
        out = Camera3DNode("c")._inject_payload_node_id(payload, "cam1")
        assert out is payload

    def test_plain_class_payload_copied_via_shallow_copy(self):
        """非 dataclass payload：浅拷贝路径注入，原对象不动。"""
        class PlainPayload:
            def __init__(self) -> None:
                self.node_id = ""

        original = PlainPayload()
        out = Camera3DNode("c")._inject_payload_node_id(original, "n1")
        assert out is not original
        assert out.node_id == "n1"
        assert original.node_id == ""

    def test_readonly_node_id_returns_original_no_crash(self):
        """node_id 为只读属性（旧 except Exception: pass 场景）：返回原对象。"""
        class ReadOnlyPayload:
            @property
            def node_id(self) -> str:
                return ""

        original = ReadOnlyPayload()
        out = Camera3DNode("c")._inject_payload_node_id(original, "n1")
        assert out is original

    def test_frozen_dataclass_payload_replaced_not_mutated(self):
        """frozen dataclass：dataclasses.replace 路径（原写入必然失败的类型）。"""
        @dataclass(frozen=True)
        class FrozenPayload:
            value: int = 1
            node_id: str = ""

        original = FrozenPayload()
        out = Camera3DNode("c")._inject_payload_node_id(original, "n1")
        assert out is not original
        assert out.node_id == "n1"
        assert original.node_id == ""


# ── AUD-64010: MergeNode BYPASS_MAPPING ──────────────────────────────────

class TestAud64010MergeBypass:

    def test_bypass_mapping_defined_points_to_b(self):
        assert MergeNode.BYPASS_MAPPING == {"result": "B"}

    def test_disabled_merge_outputs_b_input(self):
        node = MergeNode("m")
        node.disabled = True
        tex_a = _tex(11)
        tex_b = _tex(22)
        out = node.evaluate({"A": tex_a, "B": tex_b}, None, None)
        assert out == {"result": tex_b}

    def test_disabled_merge_missing_b_falls_back_empty(self):
        """B 未连接时不输出 A（显式语义：禁用 merge 输出背景）。"""
        node = MergeNode("m")
        node.disabled = True
        out = node.evaluate({"A": _tex(11)}, None, None)
        assert out == {}


# ── AUD-64013: blur 半径上限钳制 ─────────────────────────────────────────

class TestAud64013BlurRadiusClamp:

    def test_caps_mirror_paramdef_maxima(self):
        assert _RADIUS_XY_MAX == 200.0
        assert _RADIUS_MAX == 100.0

    def test_effective_radius_clamps_xy(self):
        node = BlurNode("b")
        rx, ry = node._get_effective_radius(
            {"blur_type": "gaussian", "radius_x": 1e9, "radius_y": -5.0})
        assert rx == 200.0
        assert ry == 0.0

    def test_effective_radius_clamps_scalar(self):
        node = BlurNode("b")
        r = node._get_effective_radius(
            {"blur_type": "bokeh", "radius": 1e12})
        assert r == (100.0, 100.0)

    def test_effective_radius_garbage_falls_back(self):
        node = BlurNode("b")
        rx, ry = node._get_effective_radius(
            {"blur_type": "gaussian", "radius_x": "garbage",
             "radius_y": float("nan")})
        assert rx == 5.0
        assert ry == 5.0

    def test_compute_input_roi_padding_bounded(self):
        node = BlurNode("b")
        roi = BBox.full_frame(1920, 1080)
        out = node.compute_input_roi(
            roi, {"blur_type": "gaussian", "radius_x": 10 ** 9,
                  "radius_y": 0.0})
        # padding 钳制到 200：宽度最多 1920 + 2*200。
        assert out["image"].width == 1920 + 2 * 200
        assert out["image"].height == 1080 + 2 * 200

    def test_tile_padding_bounded(self):
        node = BlurNode("b")
        assert node._tile_padding(
            {"blur_type": "motion", "radius": 10 ** 12}) == 100

    def test_evaluate_reads_clamped_radius(self):
        """_do_evaluate 的 radius 读取同样走上限钳制（不经过 ROI 路径）。"""
        node = BlurNode("b")
        node.parameters["radius_x"] = 1e9
        node.parameters["radius_y"] = 1e9
        marker = node._do_evaluate({"image": _tex()}, None, None,
                                   render_mode="full")
        assert marker["_effect_params"]["radius_x"] == 200.0
        assert marker["_effect_params"]["radius_y"] == 200.0


# ── AUD-64015: cam_fov 钳制 ──────────────────────────────────────────────

class TestAud64015CamFovClamp:

    def _payload_for(self, fov: float) -> CameraPayload:
        ctx = CompositeEvalContext(gl="fake", scene_payloads={})
        cam = Camera3DNode("cam")
        cam.parameters["cam_fov"] = fov
        cam.evaluate({}, ctx, None)
        return ctx.scene_payloads["cam"]["display"]

    @pytest.mark.parametrize("bad_fov", [0.0, 360.0, 1e9, -50.0])
    def test_out_of_range_fov_produces_finite_projection(self, bad_fov):
        payload = self._payload_for(bad_fov)
        assert np.isfinite(np.asarray(payload.proj)).all()
        assert np.isfinite(np.asarray(payload.view)).all()
        assert 1.0 <= payload.fov <= 170.0

    def test_valid_fov_unchanged(self):
        payload = self._payload_for(60.0)
        assert payload.fov == 60.0
        assert np.isfinite(np.asarray(payload.proj)).all()


# ── AUD-6504: Constant 尺寸上限 ──────────────────────────────────────────

class _FakeTex:
    def __init__(self) -> None:
        self.glo = 1
        self.filter = None

    def release(self) -> None:
        pass


class _FakeGL:
    def __init__(self) -> None:
        self.texture_sizes = []

    def texture(self, size, components=4, data=None, dtype="f4"):
        self.texture_sizes.append((size, components, dtype))
        return _FakeTex()


class TestAud6504ConstantDimensionCap:

    def test_dimension_max_derived_from_paramdefs(self):
        defs = {p.name: p for p in ConstantNode.PARAM_DEFS}
        assert ConstantNode._DIMENSION_MAX == defs["width"].max_val
        assert ConstantNode._DIMENSION_MAX == defs["height"].max_val

    def test_evaluate_clamps_oversized_dimensions(self, monkeypatch):
        """100000×100000 的外部值 → GL 纹理按 16384 上限分配。"""
        # 不真的分配 16384² 的像素数组——只验证请求的分配尺寸被截断。
        monkeypatch.setattr(
            ConstantNode, "_make_solid",
            staticmethod(lambda w, h, r, g, b, a: np.zeros((2, 2, 4), np.float32)))
        gl = _FakeGL()
        node = ConstantNode("c")
        node.parameters["width"] = 100000
        node.parameters["height"] = 99999
        out = node._do_evaluate({}, gl, None)
        assert gl.texture_sizes == [((16384, 16384), 4, "f4")]
        assert out["result"].format.width == 16384
        assert out["result"].format.height == 16384
        assert out["result"].bbox.width == 16384

    def test_evaluate_keeps_small_dimensions(self, monkeypatch):
        monkeypatch.setattr(
            ConstantNode, "_make_solid",
            staticmethod(lambda w, h, r, g, b, a: np.zeros((2, 2, 4), np.float32)))
        gl = _FakeGL()
        node = ConstantNode("c")
        node.parameters["width"] = 64
        node.parameters["height"] = 32
        out = node._do_evaluate({}, gl, None)
        assert gl.texture_sizes == [((64, 32), 4, "f4")]
        assert out["result"].format.width == 64


# ── AUD-6605: Deflicker 缓冲字节预算 ────────────────────────────────────

class TestAud6605DeflickerBufferBudget:

    def test_small_history_not_evicted(self):
        node = DeflickerNode("d")
        node.parameters["window"] = "5"
        plane = np.zeros((64, 64), dtype=np.float32)  # 16KB
        for frame in range(50):
            node._store_luma(frame, plane)
        assert len(node._luma_buffer) == 50

    def test_large_planes_evict_history_keep_window(self):
        node = DeflickerNode("d")
        node.parameters["window"] = "3"  # trailing window: frames 8,9,10
        big = np.zeros((4096, 4096), dtype=np.float32)  # 64MB each
        plane_bytes = big.nbytes
        for frame in range(11):
            node._store_luma(frame, big)
        # 窗口帧（8,9,10）永不逐出。
        assert set(range(8, 11)) <= set(node._luma_buffer)
        # 窗口外的历史 ≤ 256MB（8 帧×64MB=512MB → 只保留 4 帧历史）。
        history = [f for f in node._luma_buffer if f < 8]
        assert plane_bytes * len(history) <= _BUFFER_MAX_BYTES
        # 窗口 3 帧 + 历史 4 帧 = 7 帧。
        assert len(node._luma_buffer) == 7

    def test_rgb_buffer_evicted_together(self):
        node = DeflickerNode("d")
        node.parameters["window"] = "3"
        luma = np.zeros((4096, 4096), dtype=np.float32)       # 64MB
        rgb = np.zeros((4096, 4096, 3), dtype=np.float32)     # 192MB
        for frame in range(11):
            node._store_luma(frame, luma, rgb)
        # 每帧 luma+rgb = 256MB → 预算内最多 1 帧历史；窗口 8,9,10 保留。
        assert set(range(8, 11)) <= set(node._rgb_buffer)
        assert set(node._rgb_buffer) == set(node._luma_buffer)
        assert len([f for f in node._rgb_buffer if f < 8]) == 1

    def test_entry_cap_still_applies_for_tiny_planes(self):
        node = DeflickerNode("d")
        node.parameters["window"] = "5"
        plane = np.zeros((8, 8), dtype=np.float32)
        for frame in range(200):
            node._store_luma(frame, plane)
            # 条目上限：任意一次 store 之后都不超过 96 条。
            assert len(node._luma_buffer) <= 96
        assert max(node._luma_buffer) == 199


# ── AUD-6607: Deformer3D GL context 身份守卫 ─────────────────────────────

class _FakeDeformerEvalCtx:
    def __init__(self, gl) -> None:
        self.gl = gl
        self.scene_payloads: dict = {}
        self.scene_connections = None


class TestAud6607DeformerCtxGuard:

    def test_context_switch_drops_cache_and_programs(self, monkeypatch):
        import src.features.composite.core.eval_context as ec
        monkeypatch.setattr(ec, "CompositeEvalContext", _FakeDeformerEvalCtx)

        node = Deformer3DNode("def")
        node._last_gl_ctx = object()
        node._mesh_prog = object()
        node._mesh_tangent_prog = object()
        node._mesh_skinned_prog = object()
        node._deform_sig = ("stale",)
        node._deformed_meshes = [{
            "vao": object(), "vbo_hash": "h",
            "is_skinned": False, "has_tangent": False,
        }]

        fake = _FakeDeformerEvalCtx(gl=object())
        out = node._do_evaluate({}, fake, None)

        assert out == {}
        # 缓存的 VAO/mesh 与 program 全部弃置（属主是已死 ctx）。
        assert node._deformed_meshes == []
        assert node._deform_sig == ()
        assert node._mesh_prog is None
        assert node._mesh_tangent_prog is None
        assert node._mesh_skinned_prog is None
        assert node._last_gl_ctx is fake.gl

    def test_same_context_keeps_state(self, monkeypatch):
        import src.features.composite.core.eval_context as ec
        monkeypatch.setattr(ec, "CompositeEvalContext", _FakeDeformerEvalCtx)

        node = Deformer3DNode("def")
        fake = _FakeDeformerEvalCtx(gl=object())
        node._do_evaluate({}, fake, None)
        sentinel_prog = object()
        sentinel_sig = ("sig",)
        node._mesh_prog = sentinel_prog
        node._deform_sig = sentinel_sig

        node._do_evaluate({}, fake, None)

        assert node._mesh_prog is sentinel_prog
        assert node._deform_sig == sentinel_sig

    def test_first_evaluation_sets_ctx_without_dropping(self, monkeypatch):
        import src.features.composite.core.eval_context as ec
        monkeypatch.setattr(ec, "CompositeEvalContext", _FakeDeformerEvalCtx)

        node = Deformer3DNode("def")
        prog = object()
        node._mesh_prog = prog  # 首次求值前不应被清（_last_gl_ctx 还是 None）
        fake = _FakeDeformerEvalCtx(gl=object())
        node._do_evaluate({}, fake, None)
        assert node._mesh_prog is prog
        assert node._last_gl_ctx is fake.gl


# ── AUD-6609: Denoise quality 档位 ───────────────────────────────────────

class TestAud6609DenoiseQuality:

    def _marker(self, quality, radius=3, render_mode="full"):
        node = DenoiseNode("n")
        node.parameters["quality"] = quality
        node.parameters["radius"] = radius
        node.parameters["strength"] = 0.5
        return node.evaluate({"image": _tex()}, None, None,
                             render_mode=render_mode)

    def test_high_quality_doubles_radius(self):
        params = self._marker("high")["_effect_params"]
        assert params["radius"] == 6
        assert params["quality"] == "high"

    def test_fast_quality_caps_radius(self):
        params = self._marker("fast")["_effect_params"]
        assert params["radius"] == 2

    def test_normal_quality_unchanged(self):
        params = self._marker("normal")["_effect_params"]
        assert params["radius"] == 3

    def test_bogus_quality_falls_back_to_normal(self):
        params = self._marker("bogus")["_effect_params"]
        assert params["radius"] == 3
        assert params["quality"] == "normal"

    def test_preview_reduction_applies_after_tier(self):
        # radius 8 → high: 10 → preview: 5（先档位后预览折减）。
        params = self._marker("high", radius=8, render_mode="preview")["_effect_params"]
        assert params["radius"] == 5

    def test_roi_and_padding_use_effective_radius(self):
        node = DenoiseNode("n")
        roi = BBox.full_frame(100, 100)
        out = node.compute_input_roi(roi, {"radius": 3, "quality": "high"})
        assert out["image"].width == 100 + 2 * 6
        assert node._tile_padding({"radius": 3, "quality": "high"}) == 6
        # 默认 normal：原半径。
        assert node._tile_padding({"radius": 3}) == 3

    def test_effective_radius_matches_shared_contract(self):
        from src.features.shared.effects.effect_tiers import (
            effective_denoise_radius,
        )
        node = DenoiseNode("n")
        for quality in ("fast", "normal", "high"):
            assert node._effective_radius(4, quality) == \
                effective_denoise_radius(4, quality)


# ── AUD-6705: FaceTrack 静默写 landmarks ─────────────────────────────────

class TestAud6705FaceTrackSilentWrite:

    def _detector(self):
        def detect(frame, image_array):
            return ([(1.0, 2.0), (3.0, 4.0)], 0.9)
        return detect

    def test_rerun_on_evaluate_writes_without_side_effects(self, monkeypatch):
        import src.features.composite.nodes.face_track_node as ftmod
        monkeypatch.setattr(ftmod, "_DETECTOR", self._detector())

        node = FaceTrackNode("ft")
        node.parameters["rerun_on_evaluate"] = True
        # 从"已求值干净"状态出发（节点构造默认 dirty/DIRTY）。
        node._dirty = False
        node.state = NodeState.CACHED
        states: list = []
        node.set_state_callback(lambda nid, st: states.append((nid, st)))
        revision_before = node.param_revision
        port_rev_before = node.get_port_revision("result")

        ctx = types.SimpleNamespace(frame_number=7)
        out = node._do_evaluate({"image": _tex()}, ctx, None)

        assert out["result"].texture_id == 1
        assert node.detection_state == "OK"
        # 数据确实落库（可序列化持久化）。
        assert node.get_landmarks()[7] == [(1.0, 2.0), (3.0, 4.0)]
        # AUD-6705 核心：无自我置脏 / 修订 / 状态回调。
        assert node.param_revision == revision_before
        assert node.get_port_revision("result") == port_rev_before
        assert node._dirty is False
        assert states == []

    def test_manual_detect_keeps_full_set_parameter_semantics(self, monkeypatch):
        import src.features.composite.nodes.face_track_node as ftmod
        monkeypatch.setattr(ftmod, "_DETECTOR", self._detector())

        node = FaceTrackNode("ft")
        node._dirty = False
        node.state = NodeState.CACHED
        states: list = []
        node.set_state_callback(lambda nid, st: states.append((nid, st)))
        revision_before = node.param_revision

        assert node.detect_at(3) is True

        assert node.get_landmarks()[3] == [(1.0, 2.0), (3.0, 4.0)]
        assert node.param_revision == revision_before + 1
        assert node._dirty is True
        assert states  # 状态回调照常触发（UI 路径）

    def test_set_landmarks_silent_flag_controls_channel(self):
        node = FaceTrackNode("ft")
        node._dirty = False
        node.set_landmarks({5: [(1.0, 1.0)]}, silent=True)
        assert node.parameters["landmarks"] != "{}"
        assert node._dirty is False
        assert node.param_revision == 1
