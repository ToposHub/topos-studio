"""wd7-blk 回归测试 — 批 D 白名单解锁的 9 条 Major。

覆盖（对照 REMEDIATION_PLAN.md 行 1584-1606 / wd6-g34、wd6-g35 的 BLOCKED 记录）：
- AUD-6710 file_output：仅整数编码 clip [0,1]；TIFF+32bit 浮点保留 HDR 高光
- AUD-6711 file_output：format × bit_depth × channels 前置验证 + imwrite 失败原因入消息
- AUD-6800 frame_interpolate：输出纹理持久化/池化/延迟释放（不再逐求值裸分配）
- AUD-6801 frame_interpolate：cleanup() 释放 blend program/VBO/输出纹理/executor 引用
- AUD-6804 geo3d：_geo_revision 只在内容签名变化时递增（Scene3D 缓存不再恒 miss）
- AUD-6809 group：from_dict 对 mappings/member_node_ids 结构验证（畸形工程不崩载入）
- AUD-6900 image_input：参数编辑在解码输入未变时跳过渲染线程同步磁盘重解码
- AUD-7101 base_evaluator：effect 节点收到携带 frame_number 的求值上下文
- AUD-7300 scene3d：正交视图 depth renderbuffer 记账并随 _release_ortho_views 释放
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import cv2
import pytest

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.file_output_node import FileOutputNode
from src.features.composite.nodes.frame_interpolate_node import FrameInterpolateNode
from src.features.composite.nodes.geo3d_node import Geo3DNode
from src.features.composite.nodes.group_node import GroupNode
from src.features.composite.nodes.image_input_node import ImageInputNode, ImageLoadState
from src.features.composite.nodes.noise_node import NoiseNode
from src.features.composite.nodes.scene3d_node import Scene3DNode


def _tex(texture_id: int = 1, w: int = 4, h: int = 4, **kw) -> TextureResult:
    return TextureResult(texture_id=texture_id, width=w, height=h, **kw)


# ── 共享 fake GL 对象 ─────────────────────────────────────────────────────

class FakeGLObject:
    """moderngl 对象替身：记录 release 调用。"""

    _next_glo = 100

    def __init__(self):
        FakeGLObject._next_glo += 1
        self.glo = FakeGLObject._next_glo
        self.released = False

    def release(self):
        self.released = True


class FakeTexture(FakeGLObject):
    def __init__(self, w, h):
        super().__init__()
        self.width = int(w)
        self.height = int(h)
        self.writes = []

    def write(self, data):
        self.writes.append(data)


class FakePool:
    def __init__(self, fail=False):
        self.fail = fail
        self.acquired = []
        self.released = []

    def acquire_texture(self, w, h, components=4, dtype="f4"):
        if self.fail:
            raise RuntimeError("pool exhausted")
        tex = FakeTexture(w, h)
        self.acquired.append(tex)
        return tex

    def release_texture(self, tex):
        self.released.append(tex)


class FakeGL:
    """最小 GL 上下文替身（texture/buffer/program/vertex_array）。"""

    def __init__(self):
        self.textures = []
        self.buffers = []
        self.programs = []
        self.vaos = []

    def texture(self, size, components, data=None, dtype="f4"):
        tex = FakeTexture(size[0], size[1])
        self.textures.append(tex)
        return tex

    def buffer(self, data):
        obj = FakeGLObject()
        self.buffers.append(obj)
        return obj

    def program(self, **kw):
        obj = FakeGLObject()
        self.programs.append(obj)
        return obj

    def program_positional(self, vertex_shader=None, fragment_shader=None):
        return self.program()

    def vertex_array(self, prog, attribs, index_buffer=None):
        obj = FakeGLObject()
        self.vaos.append(obj)
        return obj


# ── AUD-6710: 浮点导出保留 HDR 高光 ───────────────────────────────────────

class TestAud6710FloatClamp:

    @staticmethod
    def _hdr_src() -> TextureResult:
        # 数据 pass（aov_semantic != beauty）跳过色彩转换，纯测 clip 语义。
        return _tex(texture_id=9, aov_semantic="depth", color_space="linear",
                    alpha_premultiplied=False)

    @staticmethod
    def _rgba() -> np.ndarray:
        # 一个 1x1 HDR 像素：RGB=4.0（>1 的高光），A=1。
        return np.full((1, 1, 4), 4.0, dtype=np.float32)

    def _prepare(self, fmt, bit_depth) -> np.ndarray:
        node = FileOutputNode("fo")
        node.parameters.update({"format": fmt, "bit_depth": bit_depth,
                                "channels": "RGBA"})
        return node._prepare_for_export(self._rgba(), self._hdr_src())

    def test_tiff_32bit_float_keeps_hdr(self):
        out = self._prepare("TIFF", "32")
        assert out.dtype == np.float32
        assert out.shape[-1] == 4  # RGBA
        assert float(out.max()) == pytest.approx(4.0)

    def test_exr_keeps_hdr(self):
        out = self._prepare("EXR", "32")
        assert out.dtype == np.float32
        assert float(out.max()) == pytest.approx(4.0)

    def test_tiff_16bit_integer_still_clamps(self):
        out = self._prepare("TIFF", "16")
        assert out.dtype == np.uint16
        assert int(out.max()) == 65535  # 4.0 被压到白

    def test_png_8bit_integer_still_clamps(self):
        out = self._prepare("PNG", "8")
        assert out.dtype == np.uint8
        assert int(out.max()) == 255


# ── AUD-6711: 组合前置验证 + 失败原因可见 ─────────────────────────────────

class TestAud6711ComboValidation:

    def test_valid_combos_pass(self):
        for fmt, depth, channels in (("PNG", "8", "auto"), ("PNG", "16", "RGBA"),
                                     ("TIFF", "32", "RGB"), ("TIFF", "8", "auto"),
                                     ("EXR", "32", "RGBA"), ("EXR", "8", "auto"),
                                     ("JPEG", "8", "RGB"), ("JPEG", "8", "auto")):
            assert FileOutputNode._validate_export_settings(fmt, depth, channels) is None, \
                (fmt, depth, channels)

    @pytest.mark.parametrize("fmt,depth,channels", [
        ("JPEG", "8", "RGBA"),
        ("JPEG", "16", "RGB"),
        ("JPEG", "32", "auto"),
        ("PNG", "32", "RGB"),
        ("WEBP", "8", "RGB"),
    ])
    def test_invalid_combos_rejected(self, fmt, depth, channels):
        reason = FileOutputNode._validate_export_settings(fmt, depth, channels)
        assert reason is not None and fmt in reason

    def test_evaluate_fails_fast_before_readback(self, tmp_path):
        node = FileOutputNode("fo")
        node.parameters.update({
            "file_path": str(tmp_path / "out.jpg"),
            "format": "JPEG", "bit_depth": "16", "channels": "RGB",
        })
        # 非法组合应在 GPU readback 之前被拒：传 None ctx/fb，若走到
        # readback 会抛异常/失败消息不同。
        out = node._do_evaluate({"image": _tex()}, ctx=None, fb_manager=None,
                                 render_mode="export")
        assert node.last_write_success is False
        assert "16-bit" in node.last_write_message
        assert "display" in out  # 透传不变

    def test_save_failure_reason_surfaced(self, tmp_path, monkeypatch):
        node = FileOutputNode("fo")
        node.parameters.update({
            "file_path": str(tmp_path / "ok.png"),
            "format": "PNG", "bit_depth": "8",
        })
        monkeypatch.setattr(cv2, "imwrite", lambda *a, **k: False,
                            raising=True)
        node._save_to_disk(np.zeros((2, 2, 3), dtype=np.uint8),
                           str(tmp_path / "ok.png"))
        assert "cv2.imwrite" in node._last_save_error

    def test_save_exception_reason_surfaced(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")

        node = FileOutputNode("fo")
        node.parameters.update({"format": "PNG"})
        monkeypatch.setattr(cv2, "imwrite", boom)
        ok = node._save_to_disk(np.zeros((2, 2, 3), dtype=np.uint8),
                                str(tmp_path / "x.png"))
        assert ok is False
        assert "disk full" in node._last_save_error


# ── AUD-6800: 输出纹理持久化 / 池化 / 延迟释放 ────────────────────────────

class TestAud6800OutputTextureLifecycle:

    def test_pooled_texture_reused_across_evaluations(self):
        node = FrameInterpolateNode("fi")
        gl = FakeGL()
        pool = FakePool()
        t1 = node._ensure_output_texture(gl, 8, 6, pool)
        t2 = node._ensure_output_texture(gl, 8, 6, pool)
        assert t1 is t2
        assert len(pool.acquired) == 1
        assert len(gl.textures) == 0  # 池可用时不自建

    def test_size_change_releases_old_back_to_pool(self):
        node = FrameInterpolateNode("fi")
        gl = FakeGL()
        pool = FakePool()
        t1 = node._ensure_output_texture(gl, 8, 6, pool)
        t2 = node._ensure_output_texture(gl, 16, 6, pool)
        assert t1 is not t2
        assert pool.released == [t1]
        assert not t1.released  # 归还池而非裸释放

    def test_own_fallback_released_on_size_change(self):
        node = FrameInterpolateNode("fi")
        gl = FakeGL()
        t1 = node._ensure_output_texture(gl, 8, 6, None)
        assert t1 in gl.textures and not node._out_texture_pooled
        t2 = node._ensure_output_texture(gl, 16, 6, None)
        assert t1.released
        assert t2 is not t1

    def test_pool_failure_falls_back_to_owned(self):
        node = FrameInterpolateNode("fi")
        gl = FakeGL()
        pool = FakePool(fail=True)
        tex = node._ensure_output_texture(gl, 8, 6, pool)
        assert tex in gl.textures
        assert node._out_texture_pooled is False

    def test_ctx_switch_drops_without_release(self):
        # 死 ctx 的对象不得调用 release（AUD-6607 同款策略）
        node = FrameInterpolateNode("fi")
        gl1, gl2 = FakeGL(), FakeGL()
        pool = FakePool()
        t1 = node._ensure_output_texture(gl1, 8, 6, pool)
        t2 = node._ensure_output_texture(gl2, 8, 6, pool)
        assert t2 is not t1
        assert not t1.released and pool.released == []
        assert node._out_gl_ctx is gl2

    def test_upload_writes_into_persistent_texture(self):
        node = FrameInterpolateNode("fi")
        gl = FakeGL()
        ref = _tex(texture_id=5, w=4, h=3)
        rgb = np.zeros((3, 4, 3), dtype=np.float32)
        r1 = node._upload(rgb, ref, gl, None)
        r2 = node._upload(rgb, ref, gl, None)
        assert r1.texture_id == r2.texture_id
        assert len(gl.textures) == 1  # 不再逐帧新分配
        assert len(gl.textures[0].writes) == 2
        assert (r1.width, r1.height) == (4, 3)


# ── AUD-6801: cleanup() 与 ctx 切换释放 ──────────────────────────────────

class TestAud6801Cleanup:

    def test_cleanup_releases_everything(self):
        node = FrameInterpolateNode("fi")
        gl = FakeGL()
        pool = FakePool()
        node._ensure_output_texture(gl, 8, 6, pool)
        prog, vbo = node._ensure_blend_resources(gl)
        node._interpolator = object()
        node.cleanup()
        assert pool.released and not pool.released[0].released
        assert prog.released and vbo.released
        assert node._out_texture is None
        assert node._blend_prog is None and node._blend_vbo is None
        assert node._interpolator is None

    def test_blend_resources_recreated_on_ctx_switch(self):
        node = FrameInterpolateNode("fi")
        gl1, gl2 = FakeGL(), FakeGL()
        p1, v1 = node._ensure_blend_resources(gl1)
        p2, v2 = node._ensure_blend_resources(gl2)
        assert p2 is not p1 and v2 is not v1
        assert not p1.released and not v1.released  # 死 ctx 不 release，仅弃引用
        assert len(gl2.programs) == 1 and len(gl2.buffers) == 1

    def test_hasattr_cleanup(self):
        assert hasattr(FrameInterpolateNode, "cleanup")


# ── AUD-6804: _geo_revision 内容签名门控 ─────────────────────────────────

class _GeoFakeCtx:
    """geo3d _do_evaluate 所需：scene 旁路 + GL 工厂。"""

    def __init__(self):
        self.scene_payloads = {}
        self._gl = FakeGL()

    # FakeGL 工厂直通
    def program(self, **kw):
        return self._gl.program()

    def buffer(self, data):
        return self._gl.buffer(data)

    def vertex_array(self, prog, attribs, index_buffer=None):
        return self._gl.vertex_array(prog, attribs, index_buffer)


class TestAud6804GeoRevision:

    def _evaluate(self, node, ctx) -> int:
        node._do_evaluate({}, ctx, None)
        payload = ctx.scene_payloads[node.id]["display"]
        return int(payload.content_revision)

    def test_revision_stable_across_evaluations(self):
        node = Geo3DNode("g1")
        ctx = _GeoFakeCtx()
        rev1 = self._evaluate(node, ctx)
        rev2 = self._evaluate(node, ctx)
        rev3 = self._evaluate(node, ctx)
        assert rev1 == rev2 == rev3  # 原实现每次 +1 → Scene3D 缓存恒 miss

    def test_revision_bumps_on_param_change(self):
        node = Geo3DNode("g2")
        ctx = _GeoFakeCtx()
        rev1 = self._evaluate(node, ctx)
        node.parameters["object_pos_x"] = 3.5
        rev2 = self._evaluate(node, ctx)
        node.parameters["metallic"] = 0.8
        rev3 = self._evaluate(node, ctx)
        assert rev2 == rev1 + 1
        assert rev3 == rev2 + 1

    def test_revision_bumps_after_cleanup(self):
        node = Geo3DNode("g3")
        ctx = _GeoFakeCtx()
        rev1 = self._evaluate(node, ctx)
        node.cleanup()
        rev2 = self._evaluate(node, ctx)
        assert rev2 == rev1 + 1  # 几何重建（新 VAO 实例）→ 修订号递增

    def test_mesh_vao_reused_between_evaluations(self):
        node = Geo3DNode("g4")
        ctx = _GeoFakeCtx()
        self._evaluate(node, ctx)
        vao_count = len(ctx._gl.vaos)
        self._evaluate(node, ctx)
        assert len(ctx._gl.vaos) == vao_count  # 同参数不重建 VAO


# ── AUD-6809: group from_dict 结构验证 ───────────────────────────────────

class TestAud6809GroupFromDict:

    def test_valid_roundtrip(self):
        g = GroupNode("grp")
        g.member_node_ids = ["n1", "n2"]
        g.input_mappings = {"input_1": ("n1", "image")}
        g.output_mappings = {"output_1": ("n2", "display")}
        data = g.to_dict()
        g2 = GroupNode.from_dict(data)
        assert g2.member_node_ids == ["n1", "n2"]
        assert g2.input_mappings == {"input_1": ("n1", "image")}
        assert g2.output_mappings == {"output_1": ("n2", "display")}

    @pytest.mark.parametrize("bad", [
        None, 5, "xy", ["x", 1],  # 非 (str, str) 对
    ])
    def test_malformed_mapping_entries_dropped(self, bad):
        g = GroupNode.from_dict({
            "node_type": "group",
            "input_mappings": {"in": bad},
            "output_mappings": {"out": bad},
        })
        assert g.input_mappings == {}
        assert g.output_mappings == {}

    def test_malformed_mapping_table_not_dict(self):
        g = GroupNode.from_dict({
            "node_type": "group",
            "input_mappings": ["not", "a", "dict"],
            "output_mappings": 42,
        })
        assert g.input_mappings == {} and g.output_mappings == {}

    def test_short_tuple_entry_dropped(self):
        g = GroupNode.from_dict({
            "node_type": "group",
            "input_mappings": {"in": ["only-one"]},
        })
        assert g.input_mappings == {}

    def test_malformed_member_ids(self):
        g = GroupNode.from_dict({
            "node_type": "group",
            "member_node_ids": [7, None, "ok", ""],
        })
        assert g.member_node_ids == ["ok"]

    def test_member_ids_not_list(self):
        g = GroupNode.from_dict({
            "node_type": "group",
            "member_node_ids": "n1",
        })
        assert g.member_node_ids == []

    def test_scalar_fields_coerced(self):
        g = GroupNode.from_dict({
            "node_type": "group",
            "is_expanded": 1,
            "group_color": None,
        })
        assert g.is_expanded is True
        assert isinstance(g.group_color, str)


# ── AUD-6900: 参数编辑跳过无谓的同步重解码 ────────────────────────────────

class TestAud6900SkipRedecode:

    @staticmethod
    def _make_node(png_path) -> ImageInputNode:
        node = ImageInputNode("ii")
        node.parameters["file_path"] = str(png_path)
        tex = FakeTexture(4, 2)
        node._gpu_texture = tex
        node._width, node._height = 4, 2
        node._load_state = ImageLoadState.UPLOADED_GPU
        node._last_uploaded_mtime = (os.path.getmtime(png_path)
                                     if os.path.exists(png_path)
                                     else time.time())
        node._last_requested_exr_part = ""
        node._last_requested_exr_extra = []
        node._dirty = True
        return node

    @staticmethod
    def _png(tmp_path) -> str:
        path = tmp_path / "plate.png"
        cv2.imwrite(str(path), np.full((2, 4, 3), 128, dtype=np.uint8))
        return path

    def test_param_edit_skips_redecode_when_content_unchanged(self, tmp_path):
        node = self._make_node(self._png(tmp_path))
        calls = []
        node.upload_to_gpu = lambda ctx, frame=None: calls.append(frame)
        out = node._do_evaluate({}, types.SimpleNamespace(), None)
        assert calls == []  # 未触发同步磁盘解码
        assert node._dirty is False
        assert out["image"].texture_id == node._gpu_texture.glo

    def test_exr_slice_change_triggers_reload(self, tmp_path):
        node = self._make_node(self._png(tmp_path))
        node.parameters["exr_part"] = "part2"  # 解码相关输入变化
        calls = []
        node.upload_to_gpu = lambda ctx, frame=None: calls.append(frame)
        node._do_evaluate({}, types.SimpleNamespace(), None)
        assert calls == [None]

    def test_mtime_change_triggers_reload(self, tmp_path):
        path = self._png(tmp_path)
        node = self._make_node(path)
        later = time.time() + 10
        os.utime(path, (later, later))
        calls = []
        node.upload_to_gpu = lambda ctx, frame=None: calls.append(frame)
        node._do_evaluate({}, types.SimpleNamespace(), None)
        assert calls == [None]

    def test_missing_file_keeps_legacy_path(self, tmp_path):
        node = self._make_node(tmp_path / "gone.png")  # 不存在
        calls = []
        node.upload_to_gpu = lambda ctx, frame=None: calls.append(frame)
        node._do_evaluate({}, types.SimpleNamespace(), None)
        assert calls == [None]  # 缺文件仍走原同步路径（missing_policy 语义）

    def test_failed_state_still_retries(self, tmp_path):
        node = self._make_node(self._png(tmp_path))
        node._load_state = ImageLoadState.FAILED
        calls = []
        node.upload_to_gpu = lambda ctx, frame=None: calls.append(frame)
        node._do_evaluate({}, types.SimpleNamespace(), None)
        assert calls == [None]  # 失败态保留重试语义


# ── AUD-7101: effect 节点收到携带帧号的求值上下文 ─────────────────────────

class TestAud7101EffectEvalContext:

    @staticmethod
    def _harness(eval_ctx):
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        h = BaseEvaluatorMixin()
        h.ctx = "BARE_CTX"
        h.fb_manager = None
        h._resource_pool = None
        h._eval_ctx = eval_ctx
        return h

    @staticmethod
    def _snapshot(node_type):
        return types.SimpleNamespace(
            node_id="n1", node_type=node_type, parameters={},
            disabled=False, keyframe_tracks=None, expressions=None,
            time_range=None, output_ports=[("result", "image")],
        )

    def test_effect_node_receives_eval_ctx_with_frame(self):
        eval_ctx = CompositeEvalContext(gl="GL", frame_number=42)
        h = self._harness(eval_ctx)
        seen = {}

        class Recorder:
            def evaluate(self, inputs, ctx, fb, pool, render_mode="preview"):
                seen["ctx"] = ctx
                return {}

        h._create_ephemeral_node = lambda ns: Recorder()
        h._evaluate_node(self._snapshot("noise"), {})
        assert seen["ctx"] is eval_ctx
        assert seen["ctx"].frame_number == 42

    def test_bare_ctx_fallback_without_eval_ctx(self):
        h = self._harness(None)
        seen = {}

        class Recorder:
            def evaluate(self, inputs, ctx, fb, pool, render_mode="preview"):
                seen["ctx"] = ctx
                return {}

        h._create_ephemeral_node = lambda ns: Recorder()
        h._evaluate_node(self._snapshot("noise"), {})
        assert seen["ctx"] == "BARE_CTX"

    def test_noise_marker_gets_playhead_end_to_end(self):
        # 端到端：真实 NoiseNode 从 eval ctx 解析 time（u_time 数据源）
        node = NoiseNode("nz")
        node.parameters["animate"] = True
        eval_ctx = CompositeEvalContext(gl="GL", frame_number=7)
        out = node._do_evaluate({}, eval_ctx, None)
        assert out["_effect_params"]["time"] == pytest.approx(7.0)

    def test_noise_marker_zero_time_on_bare_ctx(self):
        node = NoiseNode("nz")
        node.parameters["animate"] = True
        out = node._do_evaluate({}, "BARE", None)
        assert out["_effect_params"]["time"] == pytest.approx(0.0)


# ── AUD-7300: 正交视图 depth renderbuffer 记账释放 ───────────────────────

class _OrthoFakeCtx:
    def __init__(self):
        self.textures = []
        self.depth_rbs = []
        self.fbos = []

    def texture(self, size, comps, dtype="f4"):
        tex = FakeTexture(*size)
        self.textures.append(tex)
        return tex

    def depth_renderbuffer(self, size):
        rb = FakeGLObject()
        self.depth_rbs.append(rb)
        return rb

    def framebuffer(self, color_attachments=None, depth_attachment=None):
        fbo = FakeGLObject()
        self.fbos.append(fbo)
        return fbo


class TestAud7300OrthoDepthRb:

    def test_depth_rbs_tracked_at_creation(self):
        node = Scene3DNode("s3d")
        ctx = _OrthoFakeCtx()
        # 全部视图 skip：只跑懒建分支（创建后 continue），渲染体不执行。
        node._render_ortho_views(
            ctx, 16, 16,
            (0.0, 0.0, 0.0), 5.0, None, None, None, None,  # cam/proj/view…
            None, [], [],                                   # prog/payloads
            None, None, None, 0,                            # lights
            None, None, None, 0.0, 0,                       # IBL
            False, 1, False, False, 0.0,                    # instance/grid
            0.0, 0.0, 0.0, 1.0,                             # bg
            0.8, 0.8, 0.8, 0.0, 0.5,                        # material
            skip_views={"top", "right", "front"},
        )
        # 3 个视图 × (tex + depth_rb + fbo)，全部入账
        assert set(node._ortho_depth_rbs.keys()) == {"top", "right", "front"}
        assert all(rb is not None for rb in node._ortho_depth_rbs.values())
        assert len(ctx.depth_rbs) == 3

    def test_release_frees_texture_fbo_and_depth_rb(self):
        node = Scene3DNode("s3d")
        tex, fbo, rb = FakeTexture(4, 4), FakeGLObject(), FakeGLObject()
        node._ortho_textures["top"] = tex
        node._ortho_fbos["top"] = fbo
        node._ortho_depth_rbs["top"] = rb
        node._release_ortho_views()
        assert tex.released and fbo.released and rb.released
        assert node._ortho_textures["top"] is None
        assert node._ortho_fbos["top"] is None
        assert node._ortho_depth_rbs["top"] is None

    def test_size_change_rebuild_releases_all_previous_objects(self):
        # 尺寸变化 → _render_ortho_views 入口先 _release_ortho_views：
        # 旧 tex/fbo/depth_rb 全释放，再创建新一套（rb 重新入账）。
        node = Scene3DNode("s3d")
        ctx = _OrthoFakeCtx()
        old_tex, old_fbo, old_rb = FakeTexture(4, 4), FakeGLObject(), FakeGLObject()
        node._ortho_textures["top"] = old_tex
        node._ortho_fbos["top"] = old_fbo
        node._ortho_depth_rbs["top"] = old_rb
        node._ortho_size = (4, 4)  # 旧尺寸；新请求 (16//2)=(8,8) → 触发重建
        node._render_ortho_views(
            ctx, 16, 16,
            (0.0, 0.0, 0.0), 5.0, None, None, None, None,
            None, [], [],
            None, None, None, 0,
            None, None, None, 0.0, 0,
            False, 1, False, False, 0.0,
            0.0, 0.0, 0.0, 1.0,
            0.8, 0.8, 0.8, 0.0, 0.5,
            skip_views={"top", "right", "front"},
        )
        assert old_tex.released and old_fbo.released and old_rb.released
        assert len(ctx.depth_rbs) == 3  # 新一套 3 视图 rb 已入账

    def test_cleanup_releases_ortho_depth(self):
        node = Scene3DNode("s3d")
        rb = FakeGLObject()
        node._ortho_depth_rbs["front"] = rb
        node._release_ortho_views()
        assert rb.released


# ── AUD-6712：readback 后恢复调用方 GL 状态 ──────────────────────────────


def test_readback_texture_restores_caller_viewport_and_fbo():
    """GL 门控：readback 改绑的 viewport / 绘制 FBO 在返回前恢复。

    历史实现 readback 后遗留自己的 FBO 绑定与视口，污染同上下文后续绘制。
    """
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover - 无 GL 的主机
        pytest.skip(f"standalone GL context unavailable: {exc}")

    from src.features.composite.nodes.base_node import TextureResult
    from src.shared.gpu.gl_backend import FramebufferManager

    try:
        node = FileOutputNode("fo-aud6712")
        # 模拟调用方状态：非默认视口 + 自有 FBO 绑定
        src_tex = ctx.texture((64, 32), 4, dtype='f4')
        src_tex.use(0)
        caller_tex = ctx.texture((16, 16), 4, dtype='f4')
        caller_fbo = ctx.framebuffer(color_attachments=[caller_tex])
        caller_fbo.use()
        ctx.viewport = (3, 7, 21, 11)

        src = TextureResult(texture_id=src_tex.glo, width=64, height=32)
        arr = node._readback_texture(src, ctx, FramebufferManager(ctx))

        assert arr is not None
        assert arr.shape == (32, 64, 4)
        # 调用方 GL 状态恢复
        assert tuple(ctx.viewport) == (3, 7, 21, 11)
        try:
            from OpenGL import GL as gl
            assert int(gl.glGetIntegerv(
                gl.GL_DRAW_FRAMEBUFFER_BINDING)) == caller_fbo.glo
        except ImportError:
            pass  # 无 PyOpenGL 的环境仅校验 viewport 恢复
    finally:
        ctx.release()
