"""GLResourceRegistry 语义测试（计划 P3-03）。

覆盖：登记/覆盖/移除、快照、release_group LIFO 释放、纯台账条目跳过、
release_all 汇总、异常回调不中断同组释放、clear 不触发回调。
"""

from __future__ import annotations

import pytest

from src.shared.gpu.gl_resource_registry import (
    GROUP_DENOISE,
    GROUP_SHADERS,
    GROUP_UPLOAD,
    GLResourceRegistry,
)


class TestRegisterAndSnapshot:
    def test_register_and_snapshot(self):
        registry = GLResourceRegistry(owner="async_renderer")
        registry.register(GROUP_UPLOAD, "video_texture_pool")
        registry.register(GROUP_UPLOAD, "yuv_gpu_uploader")
        registry.register(GROUP_SHADERS, "utility_shaders")

        snapshot = registry.snapshot()
        assert snapshot == {
            GROUP_UPLOAD: ["video_texture_pool", "yuv_gpu_uploader"],
            GROUP_SHADERS: ["utility_shaders"],
        }
        assert registry.owner == "async_renderer"
        assert registry.record_count() == 3

    def test_register_same_name_replaces(self):
        registry = GLResourceRegistry(owner="r")
        first = object()
        second = object()
        registry.register(GROUP_UPLOAD, "pool", first)
        registry.register(GROUP_UPLOAD, "pool", second)

        assert registry.record_count() == 1
        assert registry.get(GROUP_UPLOAD, "pool") is not None
        # 覆盖登记后 detail 取最新资源类型名
        assert registry.get(GROUP_UPLOAD, "pool").detail == "object"

    def test_register_requires_group_and_name(self):
        registry = GLResourceRegistry(owner="r")
        with pytest.raises(ValueError):
            registry.register("", "name")
        with pytest.raises(ValueError):
            registry.register("group", "")

    def test_unregister_removes_record_and_empty_group(self):
        registry = GLResourceRegistry(owner="r")
        registry.register(GROUP_UPLOAD, "pool")
        assert registry.unregister(GROUP_UPLOAD, "pool") is True
        assert registry.unregister(GROUP_UPLOAD, "pool") is False
        assert registry.has_group(GROUP_UPLOAD) is False

    def test_get_missing_returns_none(self):
        registry = GLResourceRegistry(owner="r")
        assert registry.get("nope", "nothing") is None


class TestReleaseGroup:
    def test_release_group_calls_release_fn_lifo(self):
        order = []
        registry = GLResourceRegistry(owner="r")
        registry.register(GROUP_UPLOAD, "a", release_fn=lambda: order.append("a"))
        registry.register(GROUP_UPLOAD, "b", release_fn=lambda: order.append("b"))

        released = registry.release_group(GROUP_UPLOAD)

        assert released == ["b", "a"]  # LIFO
        assert order == ["b", "a"]
        assert registry.has_group(GROUP_UPLOAD) is False

    def test_release_group_skips_ledger_only_records(self):
        released_flag = []
        registry = GLResourceRegistry(owner="r")
        registry.register(GROUP_UPLOAD, "ledger_only")  # release_fn=None
        registry.register(
            GROUP_UPLOAD, "real", release_fn=lambda: released_flag.append(1)
        )

        released = registry.release_group(GROUP_UPLOAD)

        assert released == ["real"]
        assert released_flag == [1]
        assert registry.record_count() == 0

    def test_release_group_swallows_callback_errors(self):
        def boom():
            raise RuntimeError("already released")

        registry = GLResourceRegistry(owner="r")
        registry.register(GROUP_UPLOAD, "bad", release_fn=boom)
        registry.register(GROUP_UPLOAD, "good", release_fn=lambda: None)

        released = registry.release_group(GROUP_UPLOAD)

        # bad 回调失败只记日志，good 仍被释放；AUD-1300 修复后失败的条目
        # 回插台账（资源在 GL 层未真正释放，登记必须保留以便重试/诊断）
        assert released == ["good"]
        assert registry.record_count() == 1
        assert registry.get(GROUP_UPLOAD, "bad") is not None
        assert registry.snapshot() == {GROUP_UPLOAD: ["bad"]}

        # 回调恢复后重试即可清账
        registry.register(GROUP_UPLOAD, "bad", release_fn=lambda: None)
        assert registry.release_group(GROUP_UPLOAD) == ["bad"]
        assert registry.record_count() == 0

    def test_release_group_missing_group_returns_empty(self):
        registry = GLResourceRegistry(owner="r")
        assert registry.release_group("nope") == []


class TestReleaseAllAndClear:
    def test_release_all_returns_per_group_counts(self):
        registry = GLResourceRegistry(owner="r")
        registry.register(GROUP_UPLOAD, "a", release_fn=lambda: None)
        registry.register(GROUP_UPLOAD, "b", release_fn=lambda: None)
        registry.register(GROUP_DENOISE, "c", release_fn=lambda: None)
        registry.register(GROUP_SHADERS, "d")  # 纯台账

        counts = registry.release_all()

        assert counts == {GROUP_UPLOAD: 2, GROUP_DENOISE: 1, GROUP_SHADERS: 0}
        assert registry.record_count() == 0

    def test_clear_does_not_invoke_release_fns(self):
        calls = []
        registry = GLResourceRegistry(owner="r")
        registry.register(GROUP_UPLOAD, "a", release_fn=lambda: calls.append(1))

        registry.clear()

        assert calls == []
        assert registry.record_count() == 0
