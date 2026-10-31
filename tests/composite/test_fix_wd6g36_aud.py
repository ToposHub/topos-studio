"""wd6-g36 批次修复回归测试（composite nodes 域，Major 级 16 条中的可单测项）。

覆盖条目：
- AUD-7301  scene3d id-write/outline VAO 自建 VBO/IBO 释放记账
- AUD-7302  scene3d _lod_vao_cache 释放 + MeshVBOPool 引用计数回退
- AUD-7303  scene3d depth/id/shadow/catcher vao_key 含 id(mesh)
- AUD-7304  scene3d 深度 pass near/far 跟随本帧相机
- AUD-7305  scene3d id/outline/shadow/catcher 按本帧动画采样
- AUD-7306  scene3d Geo3D 进入 GPU ID pass / outline pass（真实几何 VAO）
- AUD-7404  switch compute_output_bbox 将 which 归一为 int
- AUD-7407  timeline_clip_input provider 异常按 render_mode 区分（export 抛出）
- AUD-7411  text 栅格化复用调用方已测得的 bbox（不再二次全量测量）
- AUD-7412  text PIL 回退颜色 clamp 到 0-255 字节域
- AUD-7501  tracker decode_track_data 容忍非数值键
- AUD-7505  transform 调度器侧 bbox/ROI 共用同一参考系 + 契约
- AUD-7602  video_frame _clear_cache 按 (prog, vao) 元组形态分派释放
- AUD-7603  video_frame mid 纹理 / flip program/VBO/IBO / 换尺寸路径释放
- AUD-7604  video_frame _upload_frame_gpu_vt 异常路径归还桥接租约

全部 headless：GL 对象用 fake/mock 替代，不依赖 QApplication。
"""

import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

from src.features.composite.core.format import BBox

pytestmark = pytest.mark.composite_core


# ──────────────────────────────────────────────────────────────────────────
# 共用 fake
# ──────────────────────────────────────────────────────────────────────────

class FakeUniform:
    def __init__(self) -> None:
        self.value: Any = None

    def __repr__(self) -> str:
        return f"FakeUniform(value={self.value!r})"


class FakeProgram:
    """支持 ``prog['name'].value = x`` 的假 program。"""

    def __init__(self) -> None:
        self.uniforms: Dict[str, FakeUniform] = {}

    def __getitem__(self, name: str) -> FakeUniform:
        if name not in self.uniforms:
            self.uniforms[name] = FakeUniform()
        return self.uniforms[name]

    def value_of(self, name: str) -> Any:
        uniform = self.uniforms.get(name)
        return None if uniform is None else uniform.value


class FakeFbo:
    def __init__(self) -> None:
        self.used: int = 0

    def use(self) -> None:
        self.used += 1


class FakeStateCtx:
    """渲染 pass 早段需要的 ctx 状态面（viewport/clear/enable/disable）。"""

    def __init__(self) -> None:
        self.viewport: Tuple[int, int, int, int] = (0, 0, 0, 0)
        self.calls: List[str] = []

    def clear(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("clear")

    def enable(self, flag: Any) -> None:
        self.calls.append("enable")

    def disable(self, flag: Any) -> None:
        self.calls.append("disable")

    def texture(self, size: Tuple[int, int], components: int,
                 dtype: str = "f4", data: Any = None) -> MagicMock:
        tex = MagicMock()
        tex.size = size
        return tex

    def framebuffer(self, *args: Any, **kwargs: Any) -> MagicMock:
        fbo = MagicMock()
        fbo.color_attachments = [MagicMock()]
        return fbo

    def buffer(self, data: Any = b"") -> MagicMock:
        buf = MagicMock()
        return buf

    def program(self, vertex_shader: str = "", fragment_shader: str = "") -> FakeProgram:
        return FakeProgram()

    def vertex_array(self, *args: Any, **kwargs: Any) -> MagicMock:
        vao = MagicMock()
        return vao


def _eye4() -> np.ndarray:
    return np.eye(4, dtype=np.float32)


# ──────────────────────────────────────────────────────────────────────────
# AUD-7404 — SwitchNode.compute_output_bbox
# ──────────────────────────────────────────────────────────────────────────

class TestAud7404SwitchWhichNormalization:
    def test_float_which_selects_matching_port(self) -> None:
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        b0 = BBox(x=0, y=0, width=100, height=100)
        b1 = BBox(x=0, y=0, width=640, height=480)
        out = node.compute_output_bbox(
            {"input0": b0, "input1": b1}, {"which": 1.0, "input_count": 2})
        assert out == b1

    def test_keyframe_interpolated_fraction_truncates(self) -> None:
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        b0 = BBox(x=0, y=0, width=100, height=100)
        b1 = BBox(x=0, y=0, width=640, height=480)
        # 关键帧插值在 0..1 之间时解析出 0.4 → int 后选 input0
        out = node.compute_output_bbox(
            {"input0": b0, "input1": b1}, {"which": 0.4, "input_count": 2})
        assert out == b0

    def test_out_of_range_clamps_to_last_port(self) -> None:
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        b0 = BBox(x=0, y=0, width=100, height=100)
        b1 = BBox(x=0, y=0, width=640, height=480)
        out = node.compute_output_bbox(
            {"input0": b0, "input1": b1}, {"which": 9, "input_count": 2})
        assert out == b1

    def test_non_numeric_which_falls_back_to_first(self) -> None:
        from src.features.composite.nodes.switch_node import SwitchNode
        node = SwitchNode()
        b0 = BBox(x=0, y=0, width=100, height=100)
        b1 = BBox(x=0, y=0, width=640, height=480)
        out = node.compute_output_bbox(
            {"input0": b0, "input1": b1}, {"which": "oops", "input_count": 2})
        assert out == b0


# ──────────────────────────────────────────────────────────────────────────
# AUD-7501 — decode_track_data 容忍损坏
# ──────────────────────────────────────────────────────────────────────────

class TestAud7501TrackerDecodeTolerance:
    def test_non_numeric_keys_skipped_not_raised(self) -> None:
        from src.features.composite.nodes.tracker_node import decode_track_data
        raw = ('{"transforms": {"abc": {"tx": 1.0}, "nan": {"tx": 2.0}, '
               '"inf": {"tx": 3.0}, "12.5": {"tx": 4.0}, "010": {"tx": 5.0}, '
               '"not-a-dict": 42}}')
        payload = decode_track_data(raw)
        assert payload is not None
        assert payload["transforms"] == {"12": {"tx": 4.0}, "10": {"tx": 5.0}}

    def test_dict_input_not_mutated(self) -> None:
        from src.features.composite.nodes.tracker_node import decode_track_data
        raw = {"transforms": {"1.0": {"tx": 1.0}, "zz": {"tx": 2.0}}}
        payload = decode_track_data(raw)
        assert payload["transforms"] == {"1": {"tx": 1.0}}
        # 原始 dict 的键不被改写（规范化写副本）
        assert set(raw["transforms"].keys()) == {"1.0", "zz"}

    def test_plain_json_still_round_trips(self) -> None:
        from src.features.composite.nodes.tracker_node import (
            decode_track_data, encode_track_data)
        data = {"version": 1, "transforms": {"7": {"tx": 0.5}}}
        payload = decode_track_data(encode_track_data(data))
        assert payload["transforms"] == {"7": {"tx": 0.5}}


# ──────────────────────────────────────────────────────────────────────────
# AUD-7407 — TimelineClipInputNode provider 异常按 render_mode 区分
# ──────────────────────────────────────────────────────────────────────────

class TestAud7407TimelineClipProviderFailure:
    @staticmethod
    def _make_node_and_ctx() -> Tuple[Any, Any]:
        from src.features.composite.nodes.timeline_clip_input_node import (
            TimelineClipInputNode)
        node = TimelineClipInputNode(node_id="tcin")

        def _boom(runtime: Any) -> Any:
            raise RuntimeError("provider exploded")

        runtime = SimpleNamespace(
            timeline_id="tl", clip_id="c", asset_id="a",
            source_frame=3, input_stage="source_raw")
        provider = SimpleNamespace(get_timeline_clip_texture=_boom)
        ctx = SimpleNamespace(
            runtime_context=runtime, timeline_clip_gpu_provider=provider)
        return node, ctx

    def test_preview_swallows_and_records_error(self) -> None:
        node, ctx = self._make_node_and_ctx()
        out = node._do_evaluate({}, ctx, None, render_mode="preview")
        assert out == {}
        assert node.error_message == "provider exploded"

    def test_export_raises(self) -> None:
        node, ctx = self._make_node_and_ctx()
        with pytest.raises(RuntimeError, match="provider exploded"):
            node._do_evaluate({}, ctx, None, render_mode="export")

    def test_full_raises(self) -> None:
        node, ctx = self._make_node_and_ctx()
        with pytest.raises(RuntimeError, match="provider exploded"):
            node._do_evaluate({}, ctx, None, render_mode="full")

    def test_stage_override_failure_still_serves_frame(self) -> None:
        from src.features.composite.nodes.timeline_clip_input_node import (
            TimelineClipInputNode)
        node = TimelineClipInputNode(node_id="tcin2")
        node.set_parameter("input_stage", "after_color")

        tex = SimpleNamespace(color_space="bt2020")

        class _NoReplaceRuntime:
            # 非 dataclass：replace() 会抛 TypeError → 走 debug 日志分支
            timeline_id = "tl"
            clip_id = "c"
            asset_id = "a"
            source_frame = 1
            input_stage = "source_raw"

        provider = SimpleNamespace(
            get_timeline_clip_texture=lambda runtime: "raw")
        ctx = SimpleNamespace(
            runtime_context=_NoReplaceRuntime(),
            timeline_clip_gpu_provider=provider)
        # _to_texture_result 是私有跨模块导入——这里让 provider 直接抛，
        # 验证 stage override 失败不再被纯 pass 吞掉后仍走 preview 降级。
        with patch(
            "src.features.edit.core.timeline_clip_frame_provider."
            "_to_texture_result", return_value=tex, create=True):
            out = node._do_evaluate({}, ctx, None, render_mode="preview")
        assert out == {"output": tex}
        assert node._last_source_color_tag == "bt2020"


# ──────────────────────────────────────────────────────────────────────────
# AUD-7411 — text 栅格化复用已测 bbox
# ──────────────────────────────────────────────────────────────────────────

class TestAud7411TextRasterReusesMeasurement:
    def test_bbox_origin_passed_and_single_measurement(self) -> None:
        from src.features.composite.nodes.text_node import TextNode
        node = TextNode(node_id="txt")
        measure = MagicMock(return_value=(12.0, 34.0, 100.0, 50.0))
        raster = MagicMock(return_value=np.zeros((50, 100, 4), dtype=np.float32))
        tex = MagicMock()
        tex.glo = 11
        fake_ctx = SimpleNamespace(
            texture=lambda size, comps, data=None, dtype=None: tex)

        with patch.object(node, "_measure_text_bbox", measure), \
                patch.object(node, "_render_text_pixels", raster), \
                patch.object(node, "_content_hash",
                             return_value="sig-wd6g36"), \
                patch.object(node, "_resolve_caption_input_text",
                             return_value=None):
            node._do_evaluate_with_caption(
                {}, fake_ctx, None, render_mode="preview")

        # 全量测量只执行一次（旧代码在 _render_text_pixels 内部还会再测）
        assert measure.call_count == 1
        # 栅格函数收到调用方测得的 bbox 原点，不再自行测量
        raster.assert_called_once()
        assert raster.call_args.kwargs.get("bbox_origin") == (12.0, 34.0)

    def test_bbox_origin_none_keeps_legacy_fallback(self) -> None:
        from src.features.composite.nodes.text_node import TextNode
        node = TextNode(node_id="txt2")
        node.set_parameter("text", "H")
        handle = MagicMock(return_value=(5.0, 6.0, 30.0, 20.0))
        with patch("PyQt6.QtWidgets.QApplication.instance",
                   return_value=object()), \
                patch.object(node, "text_handle_bbox", handle), \
                patch.object(node, "_build_font", return_value=MagicMock()), \
                patch.object(node, "_rasterize_mask",
                             side_effect=lambda w, h, draw: np.zeros(
                                 (h, w, 4), dtype=np.float32)), \
                patch.object(node, "_draw_text_mask"):
            out = node._render_text_pixels(20, 30, bbox_origin=None)
        assert out.shape == (30, 20, 4)
        handle.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────
# AUD-7412 — PIL 回退 HDR 颜色 clamp
# ──────────────────────────────────────────────────────────────────────────

class TestAud7412PilColorClamp:
    def test_hdr_fill_clamped_to_byte_domain(self) -> None:
        pytest.importorskip("PIL")
        from src.features.composite.nodes.text_node import TextNode
        node = TextNode(node_id="txt3")
        node.set_parameter("text", "H")
        node.set_parameter("fill_r", 4.0)
        node.set_parameter("fill_g", 2.0)
        node.set_parameter("fill_b", -1.0)   # 负值同样 clamp 到 0
        node.set_parameter("fill_a", 1.0)
        arr = node._render_text_pixels_pil(32, 32)
        # 旧代码 int(4.0*255)=1020 越界污染相邻通道；修复后所有分量 ∈ [0,1]
        assert arr.min() >= 0.0 and arr.max() <= 1.0
        # fill 分量不超过显示域上限（alpha 覆盖处 RGB 达到 255/255）
        assert float(arr[..., 3].max()) <= 1.0


# ──────────────────────────────────────────────────────────────────────────
# AUD-7505 — Transform 调度器侧参考系统一
# ──────────────────────────────────────────────────────────────────────────

_ROT_PARAMS: Dict[str, Any] = {
    "translate_x": 30.0, "translate_y": -20.0,
    "scale_x": 1.3, "scale_y": 0.9,
    "rotation": 25.0, "pivot_mode": "center", "filter": "linear",
}


class TestAud7505TransformReferenceFrame:
    def test_full_frame_estimate_matches_evaluator_formula(self) -> None:
        from src.features.composite.nodes.transform_node import TransformNode
        node = TransformNode()
        bbox = BBox(x=0, y=0, width=640, height=480)
        est = node.compute_output_bbox({"image": bbox}, _ROT_PARAMS)
        # 求值器公式（_do_evaluate）：纹理 640x480 == 数据窗口 → 参考一致
        fwd = node._build_transform_matrix(_ROT_PARAMS, 640, 480)
        expect = node._transform_bbox(bbox, fwd, 480)
        assert est == expect

    def test_packed_output_estimate_matches_evaluator_formula(self) -> None:
        from src.features.composite.nodes.transform_node import TransformNode
        node = TransformNode()
        # 上游 Transform 的打包输出：偏移 bbox，纹理尺寸 == bbox 尺寸
        bbox = BBox(x=420, y=-140, width=1500, height=800)
        est = node.compute_output_bbox({"image": bbox}, _ROT_PARAMS)
        fwd = node._build_transform_matrix(_ROT_PARAMS, 1500, 800)
        expect = node._transform_bbox(bbox, fwd, 800)
        assert est == expect

    def test_scheduler_methods_share_reference(self) -> None:
        from src.features.composite.nodes.transform_node import TransformNode
        node = TransformNode()
        ref = node._scheduler_reference_size(
            {"image": BBox(x=7, y=9, width=120, height=80)}, None)
        assert ref == (120, 80)
        # 无 input_bboxes 时回退到 output ROI 尺寸
        ref2 = node._scheduler_reference_size(
            None, BBox(x=0, y=0, width=64, height=32))
        assert ref2 == (64, 32)

    def test_forward_then_inverse_round_trip_with_rotation(self) -> None:
        from src.features.composite.nodes.transform_node import TransformNode
        node = TransformNode()
        bbox = BBox(x=0, y=0, width=200, height=120)
        out = node.compute_output_bbox({"image": bbox}, _ROT_PARAMS)
        assert out is not None
        roi = node.compute_input_roi(
            out, _ROT_PARAMS, {"image": bbox})["image"]
        pad = node._tile_padding(_ROT_PARAMS)
        # 逆映射收回的 ROI 应覆盖原输入 bbox（含 ≤pad 的滤波余量）
        inner = roi if pad == 0 else roi.expanded(-pad)
        for cx in (bbox.x, bbox.x + bbox.width - 1):
            for cy in (bbox.y, bbox.y + bbox.height - 1):
                assert inner.contains(cx, cy), (
                    f"corner ({cx},{cy}) outside inverse ROI {inner}")


# ──────────────────────────────────────────────────────────────────────────
# AUD-7301 / AUD-7302 — scene3d 释放记账
# ──────────────────────────────────────────────────────────────────────────

class TestAud7301IdOutlineBufferAccounting:
    def test_get_id_write_vao_records_owned_buffers(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        ctx = FakeStateCtx()
        made_buffers: List[MagicMock] = []

        def _buffer(data: Any = b"") -> MagicMock:
            buf = MagicMock()
            made_buffers.append(buf)
            return buf

        ctx.buffer = _buffer  # type: ignore[assignment]
        prog = FakeProgram()

        mesh = SimpleNamespace(
            is_skinned=False,
            interleaved=lambda: np.zeros((1, 8), dtype=np.float32),
            indices=np.zeros(3, dtype=np.uint32),
        )
        vao = node._get_id_write_vao(ctx, mesh, "m1", program=prog)
        assert vao is not None
        assert node._id_write_buffers["m1"] == made_buffers

    def test_release_render_target_releases_id_outline_buffers(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        vao, vao2 = MagicMock(), MagicMock()
        b1, b2, b3, b4 = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        node._id_write_vaos = {"k": vao}
        node._id_write_buffers = {"k": [b1, b2]}
        node._outline_vaos = {"o": vao2}
        node._outline_buffers = {"o": [b3, b4]}
        node._release_render_target()
        vao.release.assert_called_once()
        vao2.release.assert_called_once()
        for buf in (b1, b2, b3, b4):
            buf.release.assert_called_once()
        assert node._id_write_vaos == {}
        assert node._id_write_buffers == {}
        assert node._outline_vaos == {}
        assert node._outline_buffers == {}


class TestAud7302LodVaoCacheRelease:
    def test_release_releases_vaos_and_pool_refs(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        vao = MagicMock()
        node._lod_vao_cache = {"hash-a": (vao, 12)}
        pool = MagicMock()
        fake_ctx = object()
        node._last_gl_ctx = fake_ctx
        with patch(
            "src.features.composite.render3d.mesh_vbo_pool.MeshVBOPool."
            "for_context", return_value=pool):
            node._release_lod_vao_cache()
        vao.release.assert_called_once()
        pool.release.assert_called_once_with("hash-a", False)
        assert node._lod_vao_cache == {}

    def test_release_render_target_flushes_lod_cache(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        node._lod_vao_cache = {"h": (MagicMock(), 1)}
        node._release_render_target()
        assert node._lod_vao_cache == {}

    def test_release_without_ctx_skips_pool(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        vao = MagicMock()
        node._lod_vao_cache = {"h2": (vao, 4)}
        node._last_gl_ctx = None
        node._release_lod_vao_cache()  # 不应抛
        vao.release.assert_called_once()
        assert node._lod_vao_cache == {}


# ──────────────────────────────────────────────────────────────────────────
# AUD-7303 / AUD-7305 / AUD-7306 — scene3d ID pass 键控 / 动画 / Geo3D
# ──────────────────────────────────────────────────────────────────────────

def _make_scene3d_for_id_pass() -> Tuple[Any, FakeStateCtx]:
    from src.features.composite.nodes.scene3d_node import Scene3DNode
    node = Scene3DNode()
    ctx = FakeStateCtx()
    node._id_texture = SimpleNamespace(glo=7)
    node._id_fbo = FakeFbo()
    node._ensure_id_write_program = lambda c: FakeProgram()  # type: ignore[assignment]
    node._ensure_id_write_skinned_program = lambda c: FakeProgram()  # type: ignore[assignment]
    return node, ctx


def _mesh_desc(name: str, nid: str) -> Dict[str, Any]:
    mesh = SimpleNamespace(
        is_skinned=False,
        interleaved=lambda: np.zeros((1, 8), dtype=np.float32),
        interleaved_skinned=lambda: np.zeros((1, 12), dtype=np.float32),
        indices=np.zeros(3, dtype=np.uint32),
        vertices=np.zeros((3, 3), dtype=np.float32),
    )
    return {
        "node_id": nid, "node_name": name, "mesh": mesh,
        "world_matrix": _eye4(),
    }


class TestAud7303VaoKeyIncludesMeshIdentity:
    def test_id_pass_key_contains_id_mesh(self) -> None:
        node, ctx = _make_scene3d_for_id_pass()
        md = _mesh_desc("Cube", "n1")
        get_vao = MagicMock(return_value=None)
        with patch.object(node, "_get_id_write_vao", get_vao), \
                patch.object(node, "_sample_animation_worlds",
                             return_value={}):
            node._render_id_pass(
                ctx, 8, 8, _eye4(), _eye4(), _eye4(),
                True, False, 1, [md], None, [], [])
        key = get_vao.call_args_list[0].args[2]
        assert key == f"Cube:{id(md['mesh'])}"

    def test_duplicate_names_get_distinct_keys(self) -> None:
        node, ctx = _make_scene3d_for_id_pass()
        md1 = _mesh_desc("Cube", "n1")
        md2 = _mesh_desc("Cube", "n2")
        get_vao = MagicMock(return_value=None)
        with patch.object(node, "_get_id_write_vao", get_vao), \
                patch.object(node, "_sample_animation_worlds",
                             return_value={}):
            node._render_id_pass(
                ctx, 8, 8, _eye4(), _eye4(), _eye4(),
                True, False, 1, [md1, md2], None, [], [])
        keys = [c.args[2] for c in get_vao.call_args_list]
        assert len(keys) == 2 and keys[0] != keys[1]


class TestAud7305AnimationSampling:
    def test_id_pass_samples_animation_when_src_missing(self) -> None:
        node, ctx = _make_scene3d_for_id_pass()
        sampler = MagicMock(return_value={})
        md = _mesh_desc("Cube", "n1")
        with patch.object(node, "_sample_animation_worlds", sampler), \
                patch.object(node, "_get_id_write_vao",
                             return_value=None):
            node._render_id_pass(
                ctx, 8, 8, _eye4(), _eye4(), _eye4(),
                True, False, 1, [md], None, [], [],
                anim_enabled=True, anim_time=2.5, anim_speed=2.0)
        sampler.assert_called_once_with(True, 2.5, 2.0)

    def test_shadow_map_samples_animation_when_src_missing(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        ctx = FakeStateCtx()
        sampler = MagicMock(return_value={})
        with patch.object(node, "_sample_animation_worlds", sampler):
            with patch.object(node, "_ensure_shadow_targets", return_value=False):
                node._render_shadow_map(
                    ctx, _eye4(), _eye4(), (0, 0, 0), (0.0, 1.0, 0.0),
                    False, None, None, None, None,
                    anim_enabled=True, anim_time=1.0, anim_speed=1.0)
        # 早退（无 shadow target）不采样；给 target 后采样
        assert sampler.call_count == 0
        fbo = FakeFbo()
        with patch.object(node, "_ensure_shadow_targets", return_value=True), \
                patch.object(node, "_shadow_fbo", fbo), \
                patch.object(node, "_shadow_map_size", 256), \
                patch.object(node, "_shadow_texture", MagicMock()), \
                patch.object(node, "_sample_animation_worlds", sampler):
            node._render_shadow_map(
                ctx, _eye4(), _eye4(), (0, 0, 0), (0.0, 1.0, 0.0),
                False, None, None, None, None,
                anim_enabled=True, anim_time=1.0, anim_speed=1.0)
        sampler.assert_called_once_with(True, 1.0, 1.0)


class TestAud7306Geo3dEntersIdAndOutlinePasses:
    def test_id_pass_uses_payload_mesh_not_procedural(self) -> None:
        node, ctx = _make_scene3d_for_id_pass()
        gp_mesh = SimpleNamespace(
            is_skinned=False,
            interleaved=lambda: np.zeros((1, 8), dtype=np.float32),
            indices=np.zeros(3, dtype=np.uint32),
            vertices=np.zeros((3, 3), dtype=np.float32),
        )
        gp = SimpleNamespace(
            node_id="geo1", mesh=gp_mesh, model_matrix=_eye4(),
            instance_enabled=False, is_lines=False, vao=None)
        get_vao = MagicMock(return_value=None)
        with patch.object(node, "_get_id_write_vao", get_vao):
            node._render_id_pass(
                ctx, 8, 8, _eye4(), _eye4(), _eye4(),
                False, False, 1, None, None, [], [gp])
        keys = [c.args[2] for c in get_vao.call_args_list]
        # 不再退化为 '__procedural__'，而是 geo:<index>:<id(mesh)>
        assert keys and all(k != "__procedural__" for k in keys)
        assert keys[0] == f"geo:0:{id(gp_mesh)}"
        # mesh 参数是 payload 的真实几何
        assert get_vao.call_args_list[0].args[1] is gp_mesh

    def test_outline_pass_uses_payload_mesh_not_procedural(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        ctx = FakeStateCtx()
        node._render_fbo = FakeFbo()
        gp_mesh = SimpleNamespace(
            is_skinned=False,
            interleaved=lambda: np.zeros((1, 8), dtype=np.float32),
            indices=np.zeros(3, dtype=np.uint32),
        )
        gp = SimpleNamespace(
            node_id="geo1", mesh=gp_mesh, model_matrix=_eye4(),
            instance_enabled=False, is_lines=False, vao=None)
        get_vao = MagicMock(return_value=None)
        prog = FakeProgram()
        with patch.object(node, "_ensure_outline_program", return_value=prog), \
                patch.object(node, "_get_outline_vao", get_vao):
            node._render_outline_pass(
                ctx, 8, 8, _eye4(), _eye4(), _eye4(),
                False, False, 1, None, None, [], [gp], "geo1")
        keys = [c.args[2] for c in get_vao.call_args_list]
        assert keys == [f"geo:0:{id(gp_mesh)}"]
        assert get_vao.call_args_list[0].args[1] is gp_mesh


# ──────────────────────────────────────────────────────────────────────────
# AUD-7304 — 深度 pass near/far
# ──────────────────────────────────────────────────────────────────────────

class TestAud7304DepthPassNearFar:
    def test_camera_clip_planes_reach_shader(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        ctx = FakeStateCtx()
        node._depth_texture = SimpleNamespace(glo=3)
        node._depth_fbo = FakeFbo()
        prog = FakeProgram()
        node._ensure_depth_write_program = lambda c: prog  # type: ignore[assignment]
        node._render_depth_pass(
            ctx, 8, 8, _eye4(), _eye4(), _eye4(), (0.0, 0.0, 5.0),
            False, False, 1, False, 0.0, 1.0,
            cam_near=0.25, cam_far=512.0)
        assert prog.value_of("u_near") == 0.25
        assert prog.value_of("u_far") == 512.0

    def test_defaults_match_builtin_constants(self) -> None:
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        ctx = FakeStateCtx()
        node._depth_texture = SimpleNamespace(glo=3)
        node._depth_fbo = FakeFbo()
        prog = FakeProgram()
        node._ensure_depth_write_program = lambda c: prog  # type: ignore[assignment]
        node._render_depth_pass(
            ctx, 8, 8, _eye4(), _eye4(), _eye4(), (0.0, 0.0, 5.0),
            False, False, 1, False, 0.0, 1.0)
        assert prog.value_of("u_near") == 0.1
        assert prog.value_of("u_far") == 1000.0


# ──────────────────────────────────────────────────────────────────────────
# AUD-7602 / AUD-7603 — video_frame 清理路径
# ──────────────────────────────────────────────────────────────────────────

def _make_video_node():
    from src.features.composite.nodes.video_frame_node import VideoFrameNode
    return VideoFrameNode(node_id="vfn")


class TestAud7602VtProgramRelease:
    def test_tuple_entries_released_element_wise(self) -> None:
        node = _make_video_node()
        ibo = MagicMock()
        prog = MagicMock()
        vao = MagicMock()
        node._vt_programs = {"_ibo": ibo, "2d": (prog, vao)}
        node._clear_cache()
        ibo.release.assert_called_once()
        prog.release.assert_called_once()
        vao.release.assert_called_once()
        assert node._vt_programs == {}


class TestAud7603FlipAndMidResources:
    def test_clear_cache_releases_flip_prog_vbo_ibo(self) -> None:
        node = _make_video_node()
        prog, vbo, ibo, vao = (MagicMock(), MagicMock(), MagicMock(),
                               MagicMock())
        node._flip_prog, node._flip_vbo = prog, vbo
        node._flip_ibo, node._flip_vao = ibo, vao
        node._clear_cache()
        prog.release.assert_called_once()
        vbo.release.assert_called_once()
        ibo.release.assert_called_once()
        vao.release.assert_called_once()
        assert node._flip_prog is None and node._flip_ibo is None

    def test_clear_cache_releases_mid_texture(self) -> None:
        node = _make_video_node()
        fbo, tex = MagicMock(), MagicMock()
        node._gpu_mid_fbo, node._gpu_mid_tex = fbo, tex
        node._clear_cache()
        fbo.release.assert_called_once()
        tex.release.assert_called_once()
        assert node._gpu_mid_tex is None

    def test_resize_releases_old_fbos_and_mid_texture(self) -> None:
        node = _make_video_node()
        ctx = FakeStateCtx()
        old_fbo, old_mid_fbo, old_mid_tex = (MagicMock(), MagicMock(),
                                             MagicMock())
        old_tex = MagicMock()
        node._gpu_texture = old_tex
        node._gpu_fbo = old_fbo
        node._gpu_mid_fbo = old_mid_fbo
        node._gpu_mid_tex = old_mid_tex
        node._width, node._height = 64, 48
        assert node._ensure_gpu_target(ctx, 128, 96) is True
        old_tex.release.assert_called_once()
        old_fbo.release.assert_called_once()
        old_mid_fbo.release.assert_called_once()
        old_mid_tex.release.assert_called_once()
        # 新 mid 纹理被显式持有
        assert node._gpu_mid_tex is not None
        assert node._width == 128 and node._height == 96


# ──────────────────────────────────────────────────────────────────────────
# AUD-7604 — VT 上传异常路径归还桥接租约
# ──────────────────────────────────────────────────────────────────────────

class TestAud7604VtLeaseReleasedOnException:
    def test_post_bind_exception_releases_lease(self) -> None:
        node = _make_video_node()
        lease = MagicMock()
        planes = [SimpleNamespace(internal_format="GL_R8", target="2d"),
                  SimpleNamespace(internal_format="GL_RG8", target="2d")]
        bridge = MagicMock()
        bridge.bind_pixel_buffer.return_value = (planes, lease)
        node._vt_bridge = bridge
        # 同一 ctx 对象：避免走真实 IOSurfaceGLBridge 重建分支（native 崩溃）
        ctx = object()
        node._vt_bridge_ctx = ctx

        def _boom(ctx: Any, use_rect: bool) -> Any:
            raise KeyError("u_color_range")  # 模拟 uniform 缺失

        node._ensure_vt_program = _boom  # type: ignore[assignment]
        node._ensure_gpu_target = lambda ctx, w, h: True  # type: ignore[assignment]

        gpu_frame = SimpleNamespace(
            deferred_vt_frame=SimpleNamespace(
                pixel_buffer=None, frame_index=0, pixel_format=None,
                bit_depth=8),
            width=64, height=48)
        entry = SimpleNamespace(gpu_frame=gpu_frame, color_range=0,
                                yuv_matrix=0)

        ok = node._upload_frame_gpu_vt(ctx, entry)
        assert ok is False
        # bind 成功后的异常路径必须归还桥接租约（ring 槽位不泄漏）
        lease.release.assert_called_once()
        assert node._vt_pinned is None  # 未 pin，不残留半移交状态


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
