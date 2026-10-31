"""
F6-6 SkeletonOverlayRenderer 测试。

覆盖：
- 空 skeleton 不崩溃
- program 懒编译
- VBO 数据正确性（线段/关节点 buffer 格式）
- VBO 每帧重建（_release_vbos）
- release 释放资源

用 mock moderngl context（验证 buffer 数据，不真正画）。
"""

import numpy as np
import pytest

from src.features.composite.render3d.skeleton import (
    SkeletonData,
    _JointNode,
    build_skeleton_from_joints,
)
from src.features.composite.render3d.skeleton_renderer import (
    SkeletonOverlayRenderer,
    _COLOR_BONE_CHILD,
    _COLOR_BONE_PARENT,
    _COLOR_JOINT,
    _COLOR_ROOT,
)


# ──────────────────────────────────────────────────────────────────────
# Mock ModernGL context
# ──────────────────────────────────────────────────────────────────────


class _MockBuffer:
    """记录写入数据的 mock buffer。"""
    def __init__(self, data: bytes):
        self.data = data
        self.released = False

    def release(self):
        self.released = True


class _MockVAO:
    def __init__(self, prog, contents, index_buffer=None):
        self.prog = prog
        self.contents = contents
        self.released = False
        self.render_mode = None
        self.render_instances = None

    def render(self, mode, instances=-1):
        self.render_mode = mode
        self.render_instances = instances

    def release(self):
        self.released = True


class _MockProgram:
    """uniform 赋值的 mock program。"""
    def __init__(self):
        self._uniforms = {}
        self.released = False

    def __setitem__(self, key, value):
        self._uniforms[key] = value

    def __getitem__(self, key):
        # ModernGL uniform proxy：__getitem__ 返回的对象有可写的 .value
        return _MockUniform(self._uniforms, key)

    def release(self):
        self.released = True


class _MockUniform:
    """模拟 ModernGL uniform 对象（支持 .value 读 + 写）。"""
    def __init__(self, store, key):
        self._store = store
        self._key = key

    @property
    def value(self):
        return self._store.get(self._key)

    @value.setter
    def value(self, val):
        self._store[self._key] = val


class _MockCtx:
    """Mock ModernGL context。"""
    def __init__(self):
        self.buffers_created = []
        self.vaos_created = []
        self.programs_created = []

    def buffer(self, data):
        buf = _MockBuffer(data)
        self.buffers_created.append(buf)
        return buf

    def vertex_array(self, prog, contents, index_buffer=None):
        vao = _MockVAO(prog, contents, index_buffer)
        self.vaos_created.append(vao)
        return vao

    def program(self, vertex_shader, fragment_shader):
        prog = _MockProgram()
        self.programs_created.append(prog)
        return prog


@pytest.fixture
def mock_ctx():
    return _MockCtx()


def _make_2bone_skeleton():
    """构建 2 骨骼（parent→child）的 skeleton。"""
    parent_local = np.eye(4, dtype=np.float32)
    parent_local[0, 3] = 1.0
    joints = [
        _JointNode(name="parent", local_matrix=parent_local),
        _JointNode(name="child", local_matrix=np.eye(4, dtype=np.float32),
                   parent_joint_index=0),
    ]
    return build_skeleton_from_joints("s", joints)


# ──────────────────────────────────────────────────────────────────────
# 基础
# ──────────────────────────────────────────────────────────────────────


class TestSkeletonRendererBasic:
    def test_empty_skeleton_no_crash(self, mock_ctx):
        """空 skeleton 不崩溃，不创建 GL 资源。"""
        r = SkeletonOverlayRenderer()
        r.render(mock_ctx, SkeletonData(),
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        assert len(mock_ctx.buffers_created) == 0

    def test_none_skeleton_no_crash(self, mock_ctx):
        r = SkeletonOverlayRenderer()
        r.render(mock_ctx, None,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))

    def test_programs_lazy_compiled(self, mock_ctx):
        """program 首次 render 时编译。"""
        r = SkeletonOverlayRenderer()
        assert r._built is False
        skel = _make_2bone_skeleton()
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        assert r._built is True
        # 2 个 program（line + point）
        assert len(mock_ctx.programs_created) == 2

    def test_programs_compiled_once(self, mock_ctx):
        """program 只编译一次（多次 render 复用）。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        for _ in range(3):
            r.render(mock_ctx, skel,
                     np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                     np.eye(4, dtype=np.float32))
        assert len(mock_ctx.programs_created) == 2  # 仍然只有 2 个


# ──────────────────────────────────────────────────────────────────────
# VBO 数据正确性
# ──────────────────────────────────────────────────────────────────────


class TestVBOData:
    def test_line_vbo_has_correct_format(self, mock_ctx):
        """线段 VBO：每顶点 6 floats（pos3 + color3）。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        # 第一个 buffer 是 line VBO（2 骨骼有 1 条线段 = 2 顶点 = 12 floats）
        line_buf = mock_ctx.buffers_created[0]
        data = np.frombuffer(line_buf.data, dtype=np.float32)
        assert data.shape[0] == 12  # 2 verts × 6 floats

    def test_line_colors_parent_blue_child_yellow(self, mock_ctx):
        """线段颜色：父端蓝、子端黄。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        line_buf = mock_ctx.buffers_created[0]
        data = np.frombuffer(line_buf.data, dtype=np.float32).reshape(-1, 6)
        # 顶点0 = 父端（蓝），顶点1 = 子端（黄）
        np.testing.assert_array_almost_equal(data[0, 3:], _COLOR_BONE_PARENT)
        np.testing.assert_array_almost_equal(data[1, 3:], _COLOR_BONE_CHILD)

    def test_point_vbo_has_correct_format(self, mock_ctx):
        """关节点 VBO：每顶点 7 floats（pos3 + size1 + color3）。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        # 第二个 buffer 是 point VBO（2 关节 = 14 floats）
        point_buf = mock_ctx.buffers_created[1]
        data = np.frombuffer(point_buf.data, dtype=np.float32)
        assert data.shape[0] == 14  # 2 verts × 7 floats

    def test_root_joint_is_green(self, mock_ctx):
        """根骨骼关节点绿色。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()  # bone 0 = root
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        point_buf = mock_ctx.buffers_created[1]
        data = np.frombuffer(point_buf.data, dtype=np.float32).reshape(-1, 7)
        # bone 0 = root → green
        np.testing.assert_array_almost_equal(data[0, 4:], _COLOR_ROOT)
        # bone 1 = child → yellow
        np.testing.assert_array_almost_equal(data[1, 4:], _COLOR_JOINT)

    def test_root_joint_larger_size(self, mock_ctx):
        """根骨骼关节点比普通大（×1.5）。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32), bone_size=10.0)
        point_buf = mock_ctx.buffers_created[1]
        data = np.frombuffer(point_buf.data, dtype=np.float32).reshape(-1, 7)
        # bone 0 = root → 10*1.5=15；bone 1 = child → 10
        assert data[0, 3] == 15.0
        assert data[1, 3] == 10.0


# ──────────────────────────────────────────────────────────────────────
# VBO 重建 + release
# ──────────────────────────────────────────────────────────────────────


class TestVBOLifecycle:
    def test_vbos_recreated_each_frame(self, mock_ctx):
        """每帧重建 VBO（骨骼动画时位置变化）。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        # 第一帧
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        first_vbo = r._line_vbo
        # 第二帧
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        second_vbo = r._line_vbo
        assert first_vbo is not second_vbo
        # 第一帧 VBO 已释放
        assert first_vbo.released is True

    def test_release_frees_everything(self, mock_ctx):
        """release 释放所有 GL 资源。"""
        r = SkeletonOverlayRenderer()
        skel = _make_2bone_skeleton()
        r.render(mock_ctx, skel,
                 np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32),
                 np.eye(4, dtype=np.float32))
        assert r._line_prog is not None
        assert r._point_prog is not None
        r.release()
        assert r._built is False
        # program 已释放
        assert mock_ctx.programs_created[0].released is True
        assert mock_ctx.programs_created[1].released is True

    def test_release_idempotent(self, mock_ctx):
        """多次 release 不崩溃。"""
        r = SkeletonOverlayRenderer()
        r.release()
        r.release()

    def test_release_without_render(self):
        """未 render 过就 release 不崩溃。"""
        r = SkeletonOverlayRenderer()
        r.release()
        assert r._built is False
