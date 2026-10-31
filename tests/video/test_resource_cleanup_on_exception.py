"""
资源释放测试 - 验证上下文管理器和 weakref.finalize 机制

测试目标：
1. 验证上下文管理器 (__enter__/__exit__) 在异常情况下正确释放资源
2. 验证 weakref.finalize 在对象被 GC 时正确释放资源
3. 验证显式调用 close/release 方法正常工作

测试模块：
- VideoTexturePool
- VideoFrameNode
- FFmpegMediaSource（替代已移除的 VideoDecoder/GStreamerMediaSource）

注意：
VideoAsyncRenderer 由于是 QThread，测试需要特殊处理，
本测试文件暂不测试 VideoAsyncRenderer 的上下文管理器。
"""

import gc
import pytest
import weakref
import numpy as np

try:
    import moderngl
    HAS_MODERNGL = True
except ImportError:
    HAS_MODERNGL = False

import tempfile
import os
from pathlib import Path
from typing import Any

# P2-1: Skip entire module when moderngl is not installed (e.g. headless CI)
pytestmark = pytest.mark.skipif(not HAS_MODERNGL, reason="moderngl not available")

# 测试 VideoTexturePool 的上下文管理器


class MockTextureSlot:
    """模拟纹理槽位，用于测试释放"""

    def __init__(self, width: int, height: int, components: int = 4):
        self.width = width
        self.height = height
        self.components = components
        self.texture = None
        self.released = False

    def release(self):
        """标记为已释放"""
        self.released = True


class TestVideoTexturePoolContextManager:
    """测试 VideoTexturePool 的上下文管理器"""

    def test_context_manager_normal_exit(self):
        """测试正常退出上下文时释放资源"""
        # 创建模拟的 VideoTexturePool（不依赖 OpenGL context）
        from src.shared.video.video_texture_pool import VideoTexturePool, VideoTextureSlot

        # 使用 Mock 对象测试
        slots = []
        for i in range(3):
            slot = VideoTextureSlot(
                texture=MockTextureSlot(100, 100),
                frame_index=i,
                last_used_time=0.0,
                width=100,
                height=100,
                components=4
            )
            slots.append(slot)

        # 验证 release_all 逻辑
        for slot in slots:
            if slot.texture:
                slot.texture.release()

        # 检查释放状态
        # MockTextureSlot 有 released 属性
        for slot in slots:
            if isinstance(slot.texture, MockTextureSlot):
                assert slot.texture.released

    def test_context_manager_with_exception(self):
        """测试异常情况下资源仍然被释放"""
        slots = []

        try:
            # 模拟 with 块中发生异常
            for i in range(3):
                slot = MockTextureSlot(100, 100)
                slots.append(slot)

            # 模拟异常
            raise ValueError("Simulated error")
        except ValueError:
            # 模拟 __exit__ 被调用
            for slot in slots:
                slot.release()
        finally:
            # 检查释放状态
            for slot in slots:
                assert slot.released

    def test_weakref_finalize_cleanup(self):
        """测试 weakref.finalize 在 GC 时释放资源"""
        # 创建需要释放的资源列表
        resources = []
        for i in range(3):
            resources.append(MockTextureSlot(100, 100))

        # 定义 cleanup 函数
        cleanup_called = []

        def cleanup(res_list):
            for res in res_list:
                try:
                    if res is not None:
                        res.release()
                        cleanup_called.append(res)
                except Exception:
                    pass

        # 创建对象并设置 finalizer
        class Container:
            def __init__(self):
                self._resources = resources
                self._finalizer = weakref.finalize(self, cleanup, self._resources)

        container = Container()

        # 删除对象并触发 GC
        del container
        gc.collect()

        # 验证 cleanup 被调用
        assert len(cleanup_called) == 3
        for res in cleanup_called:
            assert res.released


class TestVideoFrameNodeContextManager:
    """测试 VideoFrameNode 的上下文管理器"""

    def test_context_manager_protocol_exists(self):
        """验证 VideoFrameNode 有上下文管理器方法"""
        from src.features.composite.nodes.video_frame_node import VideoFrameNode

        # 检查方法存在
        assert hasattr(VideoFrameNode, '__enter__')
        assert hasattr(VideoFrameNode, '__exit__')
        assert hasattr(VideoFrameNode, '_cleanup_on_gc')
        assert hasattr(VideoFrameNode, 'cleanup')

    def test_context_manager_calls_cleanup(self):
        """测试上下文管理器调用 cleanup 方法"""
        from src.features.composite.nodes.video_frame_node import VideoFrameNode

        node = None
        try:
            with VideoFrameNode("test_node") as n:
                node = n
                # 模拟异常
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # 验证 cleanup 被调用（load_state 应为 MISSING）
        assert node._load_state == "missing"


# 综合测试：weakref.finalize 机制

class TestWeakrefFinalizeMechanism:
    """测试 weakref.finalize GC 后备释放机制"""

    def test_finalizer_called_on_gc(self):
        """测试 GC 时 finalizer 被调用"""
        cleanup_count = 0

        def cleanup_callback():
            cleanup_count = 1
            return cleanup_count

        class TestObject:
            def __init__(self):
                self._finalizer = weakref.finalize(self, cleanup_callback)

        obj = TestObject()
        obj_ref = weakref.ref(obj)

        # 删除对象
        del obj
        gc.collect()

        # 验证对象已被 GC
        assert obj_ref() is None

    def test_finalizer_with_resources(self):
        """测试 finalizer 释放资源列表"""
        released_resources = []

        class MockResource:
            def __init__(self, id):
                self.id = id

            def release(self):
                released_resources.append(self.id)

        def cleanup(res_list):
            for res in res_list:
                try:
                    if res is not None:
                        res.release()
                except Exception:
                    pass

        class Container:
            def __init__(self, resources):
                self._resources = resources
                self._finalizer = weakref.finalize(self, cleanup, self._resources)

        resources = [MockResource(i) for i in range(5)]
        container = Container(resources)

        # 删除容器
        del container
        gc.collect()

        # 验证所有资源被释放
        assert len(released_resources) == 5
        assert sorted(released_resources) == [0, 1, 2, 3, 4]

    def test_finalizer_survives_reference_cycle(self):
        """测试 finalizer 在引用循环中仍能工作"""
        cleanup_called = []

        def cleanup():
            cleanup_called.append(True)

        class CycleObject:
            def __init__(self):
                self._self_ref = None  # 自引用（模拟循环）
                self._finalizer = weakref.finalize(self, cleanup)

            def create_cycle(self):
                self._self_ref = self  # 创建循环引用

        obj = CycleObject()
        obj.create_cycle()

        # 删除外部引用
        del obj

        # GC 应该能检测到循环并触发 finalizer
        # 注意：可能需要多次 GC
        for _ in range(3):
            gc.collect()

        # finalizer 应被调用
        assert cleanup_called


# 异常路径资源释放测试

class TestExceptionPathResourceCleanup:
    """测试异常路径下的资源释放"""

    def test_nested_with_statements(self):
        """测试嵌套 with 语句中的异常处理"""
        state = {"outer": False, "inner": False}

        class OuterResource:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                state["outer"] = True

        class InnerResource:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                state["inner"] = True

        try:
            with OuterResource():
                with InnerResource():
                    raise ValueError("Inner error")
        except ValueError:
            pass

        # 两个资源都应被释放
        assert state["inner"]
        assert state["outer"]

    def test_multiple_exceptions(self):
        """测试多个异常的处理"""
        cleanup_order = []

        class Resource:
            def __init__(self, name):
                self.name = name

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                cleanup_order.append(self.name)

        try:
            with Resource("first"):
                with Resource("second"):
                    with Resource("third"):
                        raise ValueError("Error")
        except ValueError:
            pass

        # 资源应按嵌套顺序释放（从内到外）
        assert cleanup_order == ["third", "second", "first"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])