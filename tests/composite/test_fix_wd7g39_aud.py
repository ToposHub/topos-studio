"""wd7-g39 批次修复回归测试（REMEDIATION_PLAN 行 1655-1670 + AUD-7502）。

覆盖条目：
- AUD-8401 / AUD-8402：AsyncPBOReadback.get_latest 的 fence 超时判定与非阻塞交付
- AUD-8408：PrefetchScheduler velocity 单位统一（units/ms）
- AUD-8412：RenderScheduler.plan 的 viewer_node_id ROI 种子（预览分支 lod）
- AUD-7502：params_hash 按 (node_id, param_revision) 记忆化
- AUD-8501：encode_toos_payload_float 异常路径 abort()
- AUD-8601：AnimationState.update finished 判定的 end_frame 裁剪
- AUD-8605：gate_fit fill/overscan Maya 语义
- AUD-8702：Taper/Stretch 法线 inverse-transpose 变换
- AUD-8703：未知 deformer_type 显式 ValueError
- AUD-8706：DeformerStack.evaluate 如实上报 recomputed_normals=False
- AUD-8707：selection_set_id 限定 mask 作用域
- AUD-88010：Z-up→Y-up 旋转（非镜像）
- AUD-88051：load_equirect 灰度全景图
- AUD-8905：MaterialStateKey.has_tangent
- AUD-8907：_apply_texture_wrap 纹理单元保存/恢复
- AUD-8908：贴图 mtime 淘汰旧 GL 纹理

纯 numpy / mock，无 GL/PyQt 依赖（GL 路径用 stub 模块验证状态机）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import hashlib
import json
import types

import numpy as np
import pytest

from src.features.composite.render.pbo_readback import (
    AsyncPBOReadback, ReadbackResult, _PBOEntry,
)
from src.features.composite.render.prefetch_scheduler import PrefetchScheduler
from src.features.composite.render import render_scheduler as rs_mod
from src.features.composite.render.render_scheduler import RenderScheduler
from src.features.composite.core.graph_snapshot import (
    GraphSnapshot, NodeSnapshot, ConnectionSnapshot,
)
from src.features.composite.core.format import BBox
from src.features.composite.render3d.animation import (
    AnimationState, PLAY_MODE_ONCE,
)
from src.features.composite.render3d.camera_model import (
    filmback_spec, sensor_height_for_gate_fit, fov_from_focal_and_filmback,
)
from src.features.composite.render3d.deformers import (
    DeformerSpec, DeformerStack,
)
from src.features.composite.render3d.deformers.base import (
    DEFORMER_BULGE, DEFORMER_TAPER, DEFORMER_STRETCH, DEFORMER_SHEAR,
)
from src.features.composite.render3d.deformers.cpu import evaluate_deformer
from src.features.composite.render3d import primitives as prim
from src.features.composite.render3d.format_capabilities import (
    SceneUnitConverter, AXIS_Z_UP,
)
from src.features.composite.render3d.material_state import (
    MaterialStateKey, MaterialStateCache,
)


# ── AUD-8401 / AUD-8402：get_latest ─────────────────────────────────

class TestGetLatestFenceAndDelivery:
    def _pbo_with_pending(self, monkeypatch, wait_result=True):
        pbo = AsyncPBOReadback(None, ring_size=2, dtype="f4")
        pbo._pbos = [11, 12]
        entry = _PBOEntry(request_id=5, width=2, height=2, data=None,
                          fence=object())
        pbo._ring[0] = entry
        monkeypatch.setattr(pbo, "_wait_entry_fence",
                            lambda e, timeout_ns=0: wait_result)
        mapped = []

        def fake_map(e, pbo_id):
            e.data = np.zeros((2, 2, 4), dtype=np.float32)
            mapped.append(e.request_id)
            return True

        monkeypatch.setattr(pbo, "_map_entry", fake_map)
        deleted = []
        monkeypatch.setattr(pbo, "_delete_fence", lambda f: deleted.append(f))
        return pbo, entry, mapped, deleted

    def test_wait_newest_timeout_returns_none_keeps_fence(self, monkeypatch):
        """AUD-8401：fence 超时按未就绪处理——不映射、不删 fence、留在 ring。"""
        pbo, entry, mapped, deleted = self._pbo_with_pending(
            monkeypatch, wait_result=False)
        assert pbo.get_latest(wait_newest=True) is None
        assert mapped == []            # 未映射在途 PBO
        assert deleted == []           # fence 保留供后续非阻塞 poll
        assert entry.fence is not None
        assert pbo._ring[0] is entry   # 条目仍在 ring

    def test_wait_newest_success_maps_and_deletes_fence(self, monkeypatch):
        pbo, entry, mapped, deleted = self._pbo_with_pending(
            monkeypatch, wait_result=True)
        r = pbo.get_latest(wait_newest=True)
        assert r is not None and r.request_id == 5
        assert mapped == [5]
        assert len(deleted) == 1
        assert entry.fence is None

    def test_nonblocking_delivers_just_completed_frame(self, monkeypatch):
        """AUD-8402：最新帧恰在本次调用时完成 → 直接交付而非丢弃返回 None。"""
        pbo = AsyncPBOReadback(None, ring_size=2, dtype="f4")
        entry = _PBOEntry(request_id=5, width=2, height=2, data=None,
                          fence=object())
        pbo._ring[0] = entry

        def fake_poll():
            # 模拟 poll_ready：完成映射并把 ring 槽位清空（真实行为）。
            pbo._ring[0] = None
            return [ReadbackResult(request_id=5, width=2, height=2,
                                   data=np.zeros((2, 2, 4), np.float32))]

        monkeypatch.setattr(pbo, "poll_ready", fake_poll)
        r = pbo.get_latest(wait_newest=False)
        assert r is not None
        assert r.request_id == 5

    def test_nonblocking_nothing_ready_returns_none(self, monkeypatch):
        pbo = AsyncPBOReadback(None, ring_size=2, dtype="f4")
        entry = _PBOEntry(request_id=5, width=2, height=2, data=None,
                          fence=object())
        pbo._ring[0] = entry
        monkeypatch.setattr(pbo, "poll_ready", lambda: [])
        assert pbo.get_latest(wait_newest=False) is None


# ── AUD-8408：velocity 单位 ──────────────────────────────────────────

class TestPrefetchVelocityUnits:
    def test_velocity_is_units_per_ms(self):
        sched = PrefetchScheduler()
        # 0.1s（100ms）间隔内变化 0.1 → 1.0 unit/s = 0.001 unit/ms。
        sched.record_param_change("n1", "radius", 0.0, timestamp=10.0)
        sched.record_param_change("n1", "radius", 0.1, timestamp=10.1)
        history = sched._param_histories[("n1", "radius")]
        assert history.velocity == pytest.approx(0.001, rel=1e-3)

    def test_normal_drag_speed_stays_on_trend_path(self):
        """旧代码 velocity=units/s 与 0.01 units/ms 阈值比较，正常拖拽速度
        （1 unit/s）也超阈值 → 趋势路径不可达；修复后走趋势预测。"""
        sched = PrefetchScheduler()
        t = 100.0
        for v in (0.10, 0.20, 0.30):
            sched.record_param_change("n2", "amount", v, timestamp=t)
            t += 0.1
        pred = sched.predict_next_value("n2", "amount", 0.30,
                                        param_bounds=(0.0, 1.0))
        assert pred is not None
        assert pred.based_on_trend is True


# ── AUD-8412：ROI 传播的 viewer 种子 ────────────────────────────────

def _roi_snapshot() -> GraphSnapshot:
    nodes = {
        "c1": NodeSnapshot(node_id="c1", node_type="color_input",
                           parameters={}, disabled=False,
                           input_ports=[], output_ports=[("image", "image")]),
        "b1": NodeSnapshot(node_id="b1", node_type="blur",
                           parameters={"radius": 5.0}, disabled=False,
                           input_ports=[("image", "image")],
                           output_ports=[("result", "image")]),
        "o1": NodeSnapshot(node_id="o1", node_type="output",
                           parameters={}, disabled=False,
                           input_ports=[("image", "image")],
                           output_ports=[("display", "image")]),
        "b2": NodeSnapshot(node_id="b2", node_type="blur",
                           parameters={"radius": 5.0}, disabled=False,
                           input_ports=[("image", "image")],
                           output_ports=[("result", "image")]),
    }
    conns = [
        ConnectionSnapshot("c1", "image", "b1", "image"),
        ConnectionSnapshot("b1", "result", "o1", "image"),
    ]
    return GraphSnapshot(generation=1, nodes=nodes, connections=conns,
                         eval_order=["c1", "b1", "o1", "b2"],
                         color_width=1920, color_height=1080)


class TestViewerNodeRoiSeed:
    def test_preview_branch_without_seed_gets_lod2(self):
        sched = RenderScheduler()
        plan = sched.plan(_roi_snapshot(), "preview",
                          viewer_roi=BBox(0, 0, 200, 200))
        # b2 不在 Output 上游：旧行为（也是无 viewer_node_id 时的行为）。
        assert plan.node_plans["b2"].lod_level == 2

    def test_preview_branch_with_seed_gets_lod0(self):
        sched = RenderScheduler()
        plan = sched.plan(_roi_snapshot(), "preview",
                          viewer_roi=BBox(0, 0, 200, 200),
                          viewer_node_id="b2")
        np_b2 = plan.node_plans["b2"]
        assert np_b2.output_roi is not None
        assert np_b2.output_roi.intersects(BBox(0, 0, 200, 200))
        assert np_b2.lod_level == 0


# ── AUD-7502：params_hash 记忆化 ────────────────────────────────────

def _hash_snapshot(rev_a=1, rev_b=1, generation=1) -> GraphSnapshot:
    nodes = {
        "n1": NodeSnapshot(node_id="n1", node_type="blur",
                           parameters={"radius": 5.0, "big": "x" * 64},
                           disabled=False,
                           input_ports=[("image", "image")],
                           output_ports=[("result", "image")],
                           param_revision=rev_a),
        "n2": NodeSnapshot(node_id="n2", node_type="blur",
                           parameters={"radius": 1.0}, disabled=False,
                           input_ports=[("image", "image")],
                           output_ports=[("result", "image")],
                           param_revision=rev_b),
    }
    return GraphSnapshot(generation=generation, nodes=nodes,
                         connections=[], eval_order=["n1", "n2"])


class TestParamsHashMemo:
    def test_hash_values_unchanged_and_memo_populated(self):
        sched = RenderScheduler()
        snap = _hash_snapshot()
        plan = sched.plan(snap, "preview")
        for nid, ns in snap.nodes.items():
            expect = hashlib.md5(json.dumps(
                ns.parameters, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
            assert plan.node_plans[nid].params_hash == expect
        assert set(sched._params_hash_memo) == {"n1", "n2"}

    def test_second_plan_skips_reserialization(self, monkeypatch):
        sched = RenderScheduler()
        sched.plan(_hash_snapshot(), "preview")
        calls = []
        real_dumps = json.dumps

        def counting_dumps(*a, **k):
            calls.append(1)
            return real_dumps(*a, **k)

        monkeypatch.setattr(json, "dumps", counting_dumps)
        sched.plan(_hash_snapshot(), "preview")
        assert calls == []  # revision 未变 → 全部命中 memo

    def test_revision_change_recomputes_only_that_node(self, monkeypatch):
        sched = RenderScheduler()
        sched.plan(_hash_snapshot(), "preview")
        calls = []
        real_dumps = json.dumps

        def counting_dumps(*a, **k):
            calls.append(1)
            return real_dumps(*a, **k)

        monkeypatch.setattr(json, "dumps", counting_dumps)
        sched.plan(_hash_snapshot(rev_a=2), "preview")
        assert len(calls) == 1  # 仅 n1 重算

    def test_generation_decrease_clears_memo(self, monkeypatch):
        """新 graph 实例（from_dict 重建，generation 重置）→ memo 失效。"""
        sched = RenderScheduler()
        sched.plan(_hash_snapshot(generation=10), "preview")
        assert sched._params_hash_memo
        calls = []
        real_dumps = json.dumps

        def counting_dumps(*a, **k):
            calls.append(1)
            return real_dumps(*a, **k)

        monkeypatch.setattr(json, "dumps", counting_dumps)
        sched.plan(_hash_snapshot(generation=3), "preview")
        assert len(calls) == 2  # generation 回退 → 全部重算

    def test_reset_dirty_tracking_clears_memo(self):
        sched = RenderScheduler()
        sched.plan(_hash_snapshot(), "preview")
        assert sched._params_hash_memo
        sched.reset_dirty_tracking()
        assert sched._params_hash_memo == {}
        assert sched._params_hash_memo_generation == -1


# ── AUD-8501：HALF 编码异常路径 abort ────────────────────────────────

class _StubEncoder:
    instances = []

    def __init__(self, prefix, cfg):
        self.prefix = prefix
        self.opened = False
        self.aborted = False
        self.closed = False
        self.fail_encode = False
        _StubEncoder.instances.append(self)

    def open(self):
        self.opened = True

    def encode_frame(self, frame):
        if self.fail_encode:
            raise RuntimeError("encode boom")
        return 0

    def close(self):
        self.closed = True
        # 成功路径：产出帧文件（函数随后 move 到目标路径）。
        out_dir = os.path.join(os.path.dirname(self.prefix), "frame")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "frame_0001.toos"), "wb") as fh:
            fh.write(b"toos")

    def abort(self):
        self.aborted = True


class _StubConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestHalfEncodeAbort:
    def _patch(self, monkeypatch):
        import src.shared.export.topos_encoder as enc_mod
        import src.shared.export.video_encoder as ve_mod
        _StubEncoder.instances = []
        monkeypatch.setattr(enc_mod, "ToposImageSequenceEncoder", _StubEncoder)
        monkeypatch.setattr(ve_mod, "FFmpegEncoderConfig", _StubConfig)
        return _StubEncoder.instances

    def test_encode_failure_aborts_and_reraises(self, monkeypatch, tmp_path):
        instances = self._patch(monkeypatch)
        from src.features.composite.render.topos_cache_codec import (
            encode_toos_payload_float,
        )
        rgba = np.zeros((2, 2, 4), dtype=np.float32)

        def failing_encode(frame):
            raise RuntimeError("encode boom")

        real_init = _StubEncoder.__init__

        def init_fail_encode(self, prefix, cfg):
            real_init(self, prefix, cfg)
            self.encode_frame = failing_encode

        monkeypatch.setattr(_StubEncoder, "__init__", init_fail_encode)
        with pytest.raises(RuntimeError, match="encode boom"):
            encode_toos_payload_float(str(tmp_path / "out.toos"), rgba)
        enc = instances[0]
        assert enc.opened
        assert enc.aborted          # AUD-8501：native 上下文显式丢弃
        assert not enc.closed

    def test_success_path_closes_without_abort(self, monkeypatch, tmp_path):
        instances = self._patch(monkeypatch)
        from src.features.composite.render.topos_cache_codec import (
            encode_toos_payload_float,
        )
        rgba = np.full((2, 2, 4), 0.5, dtype=np.float32)
        info = encode_toos_payload_float(str(tmp_path / "out.toos"), rgba)
        enc = instances[0]
        assert enc.closed and not enc.aborted
        assert info["format"]  # 正常返回 manifest
        assert os.path.isfile(str(tmp_path / "out.toos"))


# ── AUD-8601：finished 判定裁剪 ─────────────────────────────────────

class TestAnimationFinishedClamp:
    def test_once_end_frame_clamp_not_finished(self):
        st = AnimationState(play_mode=PLAY_MODE_ONCE, end_frame=48.0)
        t = st.update(frame=200.0, fps=24.0, clip_duration=5.0)
        assert t == pytest.approx(2.0)      # remap 钳到 48 帧 → 2.0s
        assert st.finished is False         # 旧代码：raw=8.33 ≥ 5 → True（错）

    def test_once_without_end_frame_finishes(self):
        st = AnimationState(play_mode=PLAY_MODE_ONCE)
        t = st.update(frame=200.0, fps=24.0, clip_duration=5.0)
        assert t == pytest.approx(5.0)
        assert st.finished is True

    def test_once_end_frame_at_clip_tail_finishes(self):
        st = AnimationState(play_mode=PLAY_MODE_ONCE, end_frame=120.0)
        st.update(frame=200.0, fps=24.0, clip_duration=5.0)
        # 钳后 raw = 120/24 = 5.0 ≥ duration → finished。
        assert st.finished is True


# ── AUD-8605：gate_fit 语义 ─────────────────────────────────────────

class TestGateFitSemantics:
    def test_fill_overscan_maya_convention(self):
        ff = filmback_spec("full_frame")  # 36×24；16:9 → horizontal 20.25mm
        # Maya Fill：resolution gate 装进 film gate → 取较小（20.25）。
        assert sensor_height_for_gate_fit(
            ff, 16.0 / 9.0, "fill") == pytest.approx(20.25)
        # Maya Overscan：film gate 装进 resolution gate → 取较大（24.0）。
        assert sensor_height_for_gate_fit(
            ff, 16.0 / 9.0, "overscan") == pytest.approx(24.0)

    def test_fill_fov_narrower_than_overscan(self):
        from math import atan, degrees
        ff = filmback_spec("full_frame")
        fov_fill = fov_from_focal_and_filmback(50.0, ff, 16.0 / 9.0, "fill")
        fov_over = fov_from_focal_and_filmback(
            50.0, ff, 16.0 / 9.0, "overscan")
        # 50mm 下：fill（20.25mm 有效高度）≈ 22.9°，overscan（24mm）≈ 27.0°。
        assert fov_fill == pytest.approx(
            degrees(2.0 * atan(20.25 / 100.0)), abs=1e-6)
        assert fov_over == pytest.approx(
            degrees(2.0 * atan(24.0 / 100.0)), abs=1e-6)
        assert fov_fill < fov_over


# ── AUD-8702：Taper/Stretch 法线 ────────────────────────────────────

class TestDeformerNormals:
    def test_taper_normals_inverse_transpose(self):
        s2 = 0.5 * np.sqrt(2.0)
        verts = np.array([[0.0, 1.0, 0.0]], dtype=np.float32)
        norms = np.array([[s2, s2, 0.0]], dtype=np.float32)
        spec = DeformerSpec(deformer_type=DEFORMER_TAPER, strength=1.0,
                            axis="y", params={"amount": 2.0})
        pts, nrm = evaluate_deformer(spec, verts, norms)
        # t=1（顶）→ scale=2.0：法线 ∝ (n_x/2, n_y, n_z) 再归一化。
        expect = np.array([[s2 / 2.0, s2, 0.0]])
        expect = expect / np.linalg.norm(expect)
        assert np.allclose(nrm, expect, atol=1e-5)
        assert not np.allclose(nrm, norms, atol=1e-3)  # 旧代码原样返回
        assert np.allclose(np.linalg.norm(nrm, axis=1), 1.0, atol=1e-5)

    def test_stretch_normals_inverse_transpose(self):
        s2 = 0.5 * np.sqrt(2.0)
        verts = np.array([[0.0, 1.0, 0.0]], dtype=np.float32)
        norms = np.array([[s2, s2, 0.0]], dtype=np.float32)
        spec = DeformerSpec(deformer_type=DEFORMER_STRETCH, strength=1.0,
                            axis="y", params={"amount": 2.0})
        pts, nrm = evaluate_deformer(spec, verts, norms)
        assert pts[0, 1] == pytest.approx(2.0)  # axis × stretch_f=2
        # 法线 ∝ (n_x·√2, n_y/2, n_z)（axis 分量 × 1/stretch_f）。
        expect = np.array([[s2 * np.sqrt(2.0), s2 / 2.0, 0.0]])
        expect = expect / np.linalg.norm(expect)
        assert np.allclose(nrm, expect, atol=1e-5)
        assert not np.allclose(nrm, norms, atol=1e-3)


# ── AUD-8703：未知类型显式报错 ──────────────────────────────────────

class TestUnknownDeformerType:
    @pytest.mark.parametrize("bad_type", ["Bend", "shear", "spherify", ""])
    def test_unknown_type_raises_valueerror(self, bad_type):
        spec = DeformerSpec(deformer_type=bad_type)
        verts = np.zeros((2, 3), dtype=np.float32)
        norms = np.zeros((2, 3), dtype=np.float32)
        with pytest.raises(ValueError, match="deformer_type"):
            evaluate_deformer(spec, verts, norms)


# ── AUD-8706：recomputed_normals 如实上报 ───────────────────────────

class TestRecomputedNormalsFlag:
    def test_nonempty_stack_reports_false(self):
        cube = prim.cube(1.0)
        stack = DeformerStack(deformers=[DeformerSpec(
            deformer_type=DEFORMER_BULGE, strength=0.5)])
        result = stack.evaluate(cube)
        assert result.recomputed_normals is False  # 旧代码恒 True


# ── AUD-8707：selection_set_id 作用域 ───────────────────────────────

class TestSelectionSetScoping:
    def test_unselected_deformer_affects_all_vertices(self):
        cube = prim.cube(1.0)
        mask = np.zeros(cube.vertices.shape[0], dtype=bool)
        mask[:8] = True
        stack = DeformerStack(deformers=[
            DeformerSpec(deformer_type=DEFORMER_BULGE, strength=0.3,
                         selection_set_id="selA"),
            DeformerSpec(deformer_type=DEFORMER_BULGE, strength=0.3),
        ])
        result = stack.evaluate(cube, selection_mask=mask)
        # 旧代码：mask 一刀切 → 未选顶点完全不动。
        # 修复后：无 selection_set_id 的变形器作用于全部顶点。
        assert not np.allclose(result.vertices[~mask],
                               cube.vertices[~mask], atol=1e-6)
        # 带 selection_set_id 的变形器仍只影响选中顶点（选中顶点位移更大）。
        d_sel = np.linalg.norm(result.vertices[mask] - cube.vertices[mask],
                               axis=1)
        d_unsel = np.linalg.norm(result.vertices[~mask] - cube.vertices[~mask],
                                 axis=1)
        assert d_sel.mean() > d_unsel.mean() > 0.0

    def test_no_selection_set_ids_behaves_like_before(self):
        cube = prim.cube(1.0)
        mask = np.zeros(cube.vertices.shape[0], dtype=bool)
        mask[:8] = True
        stack = DeformerStack(deformers=[DeformerSpec(
            deformer_type=DEFORMER_BULGE, strength=0.3,
            selection_set_id="selA")])
        result = stack.evaluate(cube, selection_mask=mask)
        assert np.allclose(result.vertices[~mask], cube.vertices[~mask],
                           atol=1e-6)


# ── AUD-88010：Z-up→Y-up 旋转 ──────────────────────────────────────

class TestAxisFlipRotation:
    def test_known_vectors(self):
        conv = SceneUnitConverter(source_unit="m", source_axis=AXIS_Z_UP)
        v = np.array([[0.0, 1.0, 0.0],    # Z-up 前向 → 目标 -Z
                      [0.0, 0.0, 1.0],    # Z-up 上 → 目标 +Y
                      [1.0, 0.0, 0.0]], dtype=np.float32)
        out = conv.convert_vertices(v)
        assert np.allclose(out[0], [0.0, 0.0, -1.0], atol=1e-6)
        assert np.allclose(out[1], [0.0, 1.0, 0.0], atol=1e-6)
        assert np.allclose(out[2], [1.0, 0.0, 0.0], atol=1e-6)

    def test_rotation_not_mirror(self):
        conv = SceneUnitConverter(source_unit="m", source_axis=AXIS_Z_UP)
        basis = np.eye(3, dtype=np.float32)
        m = conv.convert_vertices(basis)
        assert np.linalg.det(m) == pytest.approx(1.0, abs=1e-5)  # det=+1
        assert np.allclose(m.T @ m, np.eye(3), atol=1e-5)        # 正交

    def test_unit_scale_still_applied(self):
        conv = SceneUnitConverter(source_unit="cm", source_axis=AXIS_Z_UP)
        out = conv.convert_vertices(
            np.array([[100.0, 0.0, 0.0]], dtype=np.float32))
        assert np.allclose(out, [[1.0, 0.0, 0.0]], atol=1e-6)


# ── AUD-88051：灰度全景图 ──────────────────────────────────────────

class TestLoadEquirectGrayscale:
    def _stub_cv2(self, monkeypatch, img):
        stub = types.ModuleType("cv2")
        stub.IMREAD_ANYDEPTH = 2
        stub.IMREAD_ANYCOLOR = 4
        stub.imread = lambda path, flags: img
        monkeypatch.setitem(sys.modules, "cv2", stub)

    def test_2d_grayscale_becomes_three_channel(self, monkeypatch):
        from src.features.composite.render3d.hdri import load_equirect
        self._stub_cv2(monkeypatch, np.full((4, 6), 128, np.uint8))
        out = load_equirect("/fake/gray.png")
        assert out.shape == (4, 6, 3)
        assert out.dtype == np.float32
        # 函数按实际 max（128）归一化 → 全部通道 = (128/128)^2.2 = 1.0；
        # 修复点是不再抛 IndexError 且三通道等值复制。
        assert np.allclose(out, 1.0, atol=1e-6)
        # 三通道等值（灰度复制）。
        assert np.allclose(out[..., 0], out[..., 1])
        assert np.allclose(out[..., 1], out[..., 2])

    def test_hw1_grayscale_becomes_three_channel(self, monkeypatch):
        from src.features.composite.render3d.hdri import load_equirect
        self._stub_cv2(monkeypatch, np.full((4, 6, 1), 1.0, np.float32))
        out = load_equirect("/fake/grayf.exr")
        assert out.shape == (4, 6, 3)   # HDR float 路径不做归一化
        assert np.allclose(out, 1.0)


# ── AUD-8905：MaterialStateKey.has_tangent ──────────────────────────

class TestMaterialStateKeyTangent:
    def test_has_tangent_differentiates_keys(self):
        k_t = MaterialStateKey.from_material(None, {}, has_tangent=True)
        k_f = MaterialStateKey.from_material(None, {}, has_tangent=False)
        assert k_t != k_f

    def test_cache_no_longer_matches_across_tangent_state(self):
        cache = MaterialStateCache()
        cache.update(MaterialStateKey.from_material(None, {}, has_tangent=True))
        # 下一 mesh 同材质但 tangent 不可用 → key 不同 → 必须重绑。
        assert cache.matches(
            MaterialStateKey.from_material(None, {}, has_tangent=False)) is False
        assert cache.matches(
            MaterialStateKey.from_material(None, {}, has_tangent=True)) is True

    def test_default_keeps_legacy_equality(self):
        a = MaterialStateKey.from_material(None, {"texture_filter": "linear"})
        b = MaterialStateKey.from_material(None, {"texture_filter": "linear"})
        assert a == b


# ── AUD-8907：_apply_texture_wrap 纹理单元安全 ──────────────────────

class TestApplyTextureWrapUnitSafety:
    def _stub_gl(self, monkeypatch):
        gl = types.ModuleType("OpenGL.GL")
        gl.GL_TEXTURE_2D = 0x0DE1
        gl.GL_ACTIVE_TEXTURE = 0x84E0
        gl.GL_TEXTURE_BINDING_2D = 0x8069
        gl.GL_TEXTURE_WRAP_S = 0x2802
        gl.GL_TEXTURE_WRAP_T = 0x2803
        gl.GL_REPEAT = 0x2901
        gl.GL_CLAMP_TO_EDGE = 0x812F
        gl.GL_MIRRORED_REPEAT = 0x8370
        state = {"active": gl.GL_ACTIVE_TEXTURE + 3,
                 "binding": {gl.GL_ACTIVE_TEXTURE + 3: 42}}
        calls = []

        gl.glGetIntegerv = lambda pname: (
            state["active"] if pname == gl.GL_ACTIVE_TEXTURE
            else state["binding"][state["active"]])

        def bind(target, tex):
            assert target == gl.GL_TEXTURE_2D
            state["binding"][state["active"]] = tex
            calls.append(("bind", tex))

        def active_texture(unit):
            state["active"] = unit
            calls.append(("active", unit))

        def tex_param(target, pname, value):
            calls.append(("param", pname, value))

        gl.glBindTexture = bind
        gl.glActiveTexture = active_texture
        gl.glTexParameteri = tex_param
        pkg = types.ModuleType("OpenGL")
        pkg.GL = gl
        monkeypatch.setitem(sys.modules, "OpenGL", pkg)
        monkeypatch.setitem(sys.modules, "OpenGL.GL", gl)
        return gl, state, calls

    def test_wrap_restores_unit_and_binding(self, monkeypatch):
        from src.features.composite.render3d.material_texture_loader import (
            _apply_texture_wrap,
        )
        gl, state, calls = self._stub_gl(monkeypatch)

        class Tex:
            glo = 7

        tex = Tex()
        unit3 = gl.GL_ACTIVE_TEXTURE + 3
        _apply_texture_wrap(tex, "mirror")
        # 绑定序列：目标纹理 → 恢复旧绑定；激活单元恢复原值。
        assert [c for c in calls if c[0] == "bind"] == \
            [("bind", 7), ("bind", 42)]
        assert ("active", unit3) in calls
        assert state["active"] == unit3
        assert state["binding"][unit3] == 42  # 未被串位覆盖
        assert ("param", gl.GL_TEXTURE_WRAP_S, gl.GL_MIRRORED_REPEAT) in calls
        assert ("param", gl.GL_TEXTURE_WRAP_T, gl.GL_MIRRORED_REPEAT) in calls


# ── AUD-8908：mtime 淘汰 ────────────────────────────────────────────

class TestTextureCacheMtimeEviction:
    def test_stale_mtime_entry_released(self, monkeypatch, tmp_path):
        from src.features.composite.render3d.material_texture_loader import (
            MaterialTextureLoader,
        )
        tex_path = tmp_path / "base.png"
        tex_path.write_bytes(b"fake")
        counter = {"n": 0}
        released = []

        class FakeTex:
            def __init__(self, n):
                self.n = n
                self.glo = n

            def release(self):
                released.append(self.n)

        def fake_upload(ctx, path, role):
            counter["n"] += 1
            return FakeTex(counter["n"])

        monkeypatch.setattr(MaterialTextureLoader, "_upload",
                            staticmethod(fake_upload))

        class Desc:
            path = str(tex_path)
            role = "base_color"

        loader = MaterialTextureLoader()
        os.utime(str(tex_path), (100, 100))
        t1 = loader.get_or_upload(None, Desc())
        assert t1 is not None and t1.n == 1
        assert len(loader._cache) == 1

        # 文件热更新（mtime 变化）→ 旧条目释放，不残留。
        os.utime(str(tex_path), (200, 200))
        t2 = loader.get_or_upload(None, Desc())
        assert t2 is not None and t2.n == 2
        assert released == [1]
        assert len(loader._cache) == 1
        key = next(iter(loader._cache))
        assert key[1] == pytest.approx(200.0)
