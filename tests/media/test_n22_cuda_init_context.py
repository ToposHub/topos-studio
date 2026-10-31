"""N22 · CUDA 初始化上下文恢复与 cuGLGetDevices 异常分类测试。

审计句：initialize 创建/SetCurrent 私有 context 后成功直接返回，未
恢复调用者 current-context；cuGLGetDevices 异常被先 int() 转 TypeError
后续安全兜底不可达。修复契约：成功/失败/异常全出口恢复调用者上下文；
API 返回码/异常先安全分类，设备数兜底可达。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.media_core

from src.shared.media.windows_hw.cuda_resident import (  # noqa: E402
    CudaGlInteropSession)
from tests.media.test_cuda_resident_lifecycle_w02 import (  # noqa: E402
    _install_fake_driver_ns)


def _make_session_ns(monkeypatch) -> tuple:
    """安装替身并返回 (session, driver 实例, 命名空间)——失败注入须
    替换命名空间函数（session 经 cuda.bindings.driver 导入闭包）。"""
    drv, ns = _install_fake_driver_ns(monkeypatch)
    session = CudaGlInteropSession(ring_size=4, max_ring_size=8)
    return session, drv, ns


class TestCallerContextRestoreN22:
    def test_success_restores_caller_context(self, monkeypatch):
        session, drv, _ns = _make_session_ns(monkeypatch)
        caller_ctx = object()
        drv.current = caller_ctx  # 调用者线程已有 current-context
        assert session.initialize() is True
        # 初始化后调用者上下文被恢复（不再被会话私有上下文占用）
        assert drv.current is caller_ctx
        # 会话自身上下文仍然存在（bind/destroy 的 prev 守卫按需切换）
        assert session._ctx is not None

    def test_no_caller_context_kept_on_session_ctx(self, monkeypatch):
        session, drv, _ns = _make_session_ns(monkeypatch)
        assert drv.current is None  # 调用者无上下文
        assert session.initialize() is True
        # 无调用者上下文可恢复：不凭空恢复，私有 ctx 保持 current
        assert drv.current is session._ctx

    def test_verify_failure_restores_and_destroys(self, monkeypatch):
        session, drv, ns = _make_session_ns(monkeypatch)
        caller_ctx = object()
        drv.current = caller_ctx
        # 核验失败：第 1 次 GetCurrent（捕获调用者）返回真身，第 2 次
        # （核验）返回错配对象 → verify 失败
        calls = []

        def flaky_get_current():
            calls.append(1)
            return (0, caller_ctx if len(calls) == 1 else object())

        ns.cuCtxGetCurrent = flaky_get_current
        assert session.initialize() is False
        assert "cuCtxGetCurrent_verify" in session.last_fallback_reason
        assert drv.current is caller_ctx  # 调用者上下文已恢复
        assert drv.destroyed_ctxs  # 私有上下文已销毁

    def test_ctx_create_failure_restores_caller(self, monkeypatch):
        session, drv, ns = _make_session_ns(monkeypatch)
        caller_ctx = object()
        drv.current = caller_ctx
        ns.cuCtxCreate = lambda cfg, flags, dev: (-1, None)
        assert session.initialize() is False
        assert "cuCtxCreate" in session.last_fallback_reason
        assert drv.current is caller_ctx

    def test_device_selection_failure_restores_caller(self, monkeypatch):
        session, drv, _ns = _make_session_ns(monkeypatch)
        caller_ctx = object()
        drv.current = caller_ctx
        drv.gl_devices = None
        drv.gl_err = 100  # GL 适配器非 CUDA → fail-closed（无 ctx 创建）
        assert session.initialize() is False
        assert drv.current is caller_ctx


class TestGlDevicesExceptionClassificationN22:
    def test_gl_exception_falls_back_to_device_count(self, monkeypatch):
        session, drv, ns = _make_session_ns(monkeypatch)

        def raising_gl(n, device_list):
            raise RuntimeError("binding layer boom")

        ns.cuGLGetDevices = raising_gl
        # 旧代码：int(exc) TypeError → cuda_init_error 兜底不可达；
        # 修复后：异常安全分类 → 单 CUDA 设备兜底可用 → 初始化成功
        assert session.initialize() is True, session.last_fallback_reason
        assert session.last_fallback_reason == ""
        assert "cuDeviceGetCount" in drv.calls

    def test_gl_error_100_still_fail_closed(self, monkeypatch):
        session, drv, _ns = _make_session_ns(monkeypatch)
        drv.gl_devices = None
        drv.gl_err = 100  # CUDA_ERROR_NO_DEVICE：GL 适配器非 CUDA
        assert session.initialize() is False
        assert "no_cuda_device_for_gl_context" in \
            session.last_fallback_reason
