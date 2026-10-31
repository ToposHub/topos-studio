"""wd16-g87 批 D 修复回归测试（shared/gpu 域 + i18n 杂项）。

覆盖 REMEDIATION_PLAN 批 D 的 18 条 Major 条目中可在无 GPU/无窗口环境
验证的行为契约（真机 GL 行为由既有真机层测试与代码审查覆盖）：

- AUD-1207  wait_fence 的 glWaitSync/glClientWaitSync 语义
- AUD-1210  GLContextManager.initialize 的 pyqt_context 守卫
- AUD-1211  create_shared_context 走 share=True 且要求已初始化
- AUD-1214  GLContextHub 探测不再发 GL 调用（identity / Qt / fail-closed）
- AUD-1215  GLContextHub 单例与 dict 的并发防护
- AUD-1300  release_fn 失败条目回插台账并可重试
- AUD-1401  dispatch 丢弃链后 submit 显式报错（一次即清）
- AUD-1414  kernels_dec 不再发未声明的 status=93u
- AUD-1510  yuv convert 位深只接受 8/10
- AUD-1520  drain 跳过 submit() 预留的 None 槽
- AUD-1521  on_complete 异常被记录而非逃逸
- AUD-1601  TextureLease 状态转移与 release_all 无"复活"竞态
- AUD-1602  GpuPresentFrame.extra 切断引用共享
- AUD-1603  pool.discard 回收丢弃帧 slot
- AUD-1604  register_slot 校验 + 重复注册回收 fence
- AUD-33800 tr() 占位符格式化捕获 KeyError/IndexError/ValueError
- AUD-33801 翻译 JSON 非 dict 根按坏文件降级
"""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from src.shared.gpu import present_frame as pf
from src.shared.gpu.gl_context_hub import GLContextHub
from src.shared.gpu.gl_resource_registry import (
    GLResourceRegistry,
    GROUP_UPLOAD,
)
from src.shared.gpu.metal.context import MetalComputeContext, MetalComputeError
from src.shared.gpu.metal import kernels_dec
from src.shared.gpu.metal.kernels_video import build_yuv_convert_params
from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler
from src.shared.i18n.i18n import I18N


# ---------------------------------------------------------------------------
# AUD-1207 — wait_fence timeout 语义
# ---------------------------------------------------------------------------


class _FakeGL:
    """替身 OpenGL.GL：记录调用，不触真 GL。"""

    GL_TIMEOUT_IGNORED = 0xFFFFFFFFFFFFFFFF

    def __init__(self) -> None:
        self.wait_sync_calls: list[tuple] = []
        self.client_wait_sync_calls: list[tuple] = []

    def glWaitSync(self, sync, flags, timeout) -> None:
        self.wait_sync_calls.append((sync, flags, timeout))

    def glClientWaitSync(self, sync, flags, timeout) -> int:
        self.client_wait_sync_calls.append((sync, flags, timeout))
        return 0  # ALREADY_SIGNALED


@pytest.fixture()
def fake_gl(monkeypatch):
    from src.shared.gpu import composite_gpu_frame_handle as h

    gl_pkg = types.ModuleType("OpenGL")
    gl_mod = _FakeGL()
    fake_gl_module = types.ModuleType("OpenGL.GL")
    for name in ("GL_TIMEOUT_IGNORED", "glWaitSync", "glClientWaitSync"):
        setattr(fake_gl_module, name, getattr(gl_mod, name))
    gl_pkg.GL = fake_gl_module
    monkeypatch.setitem(sys.modules, "OpenGL", gl_pkg)
    monkeypatch.setitem(sys.modules, "OpenGL.GL", fake_gl_module)
    return gl_mod


def test_wait_fence_infinite_uses_timeout_ignored(fake_gl) -> None:
    from src.shared.gpu.composite_gpu_frame_handle import wait_fence

    assert wait_fence(0x1234) is True
    assert len(fake_gl.wait_sync_calls) == 1
    sync, flags, timeout = fake_gl.wait_sync_calls[0]
    assert timeout == fake_gl.GL_TIMEOUT_IGNORED  # AUD-1207：永不为其它值
    assert fake_gl.client_wait_sync_calls == []


def test_wait_fence_bounded_timeout_uses_client_wait(fake_gl) -> None:
    from src.shared.gpu.composite_gpu_frame_handle import wait_fence

    assert wait_fence(0x1234, timeout_ns=1_500_000) is True
    # 限时路径必须走 glClientWaitSync——glWaitSync 的 timeout 只接受
    # GL_TIMEOUT_IGNORED，传其它值是 GL_INVALID_VALUE 且依然无限等待。
    assert len(fake_gl.client_wait_sync_calls) == 1
    _, flags, timeout = fake_gl.client_wait_sync_calls[0]
    assert timeout == 1_500_000
    assert fake_gl.wait_sync_calls == []


def test_wait_fence_invalid_handle_short_circuits(fake_gl) -> None:
    from src.shared.gpu.composite_gpu_frame_handle import wait_fence

    assert wait_fence(0) is False
    assert fake_gl.wait_sync_calls == []
    assert fake_gl.client_wait_sync_calls == []


# ---------------------------------------------------------------------------
# AUD-1210 / AUD-1211 — GLContextManager
# ---------------------------------------------------------------------------


class TestGLContextManager:
    def _manager(self):
        from src.shared.gpu.gl_backend import GLContextManager

        return GLContextManager()

    def test_initialize_rejects_non_current_pyqt_context(self, monkeypatch) -> None:
        from src.shared.gpu import gl_backend as gb

        mgr = self._manager()
        monkeypatch.setattr(
            gb.GLContextManager,
            "_is_current_on_calling_thread",
            staticmethod(lambda ctx: False),
        )
        created: list = []
        monkeypatch.setattr(
            gb.moderngl, "create_context", lambda **kw: created.append(kw)
        )
        with pytest.raises(RuntimeError, match="not current"):
            mgr.initialize(object())
        assert created == []  # 未误绑当前线程上下文
        assert mgr.initialized is False

    def test_initialize_adopts_context_and_records_owner_thread(self, monkeypatch) -> None:
        from src.shared.gpu import gl_backend as gb

        mgr = self._manager()
        monkeypatch.setattr(
            gb.GLContextManager,
            "_is_current_on_calling_thread",
            staticmethod(lambda ctx: True),
        )
        sentinel = object()
        monkeypatch.setattr(gb.moderngl, "create_context", lambda **kw: sentinel)
        mgr.initialize(object())
        assert mgr.ctx is sentinel
        assert mgr.initialized is True
        assert mgr._owner_thread_id == threading.get_ident()

    def test_shared_context_requires_init_and_uses_share_mode(self, monkeypatch) -> None:
        from src.shared.gpu import gl_backend as gb

        mgr = self._manager()
        with pytest.raises(RuntimeError, match="no context"):
            mgr.create_shared_context()

        mgr.ctx = object()
        mgr._owner_thread_id = threading.get_ident()
        captured: dict = {}

        def fake_create_context(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(gb.moderngl, "create_context", fake_create_context)
        mgr.create_shared_context()
        # AUD-1211：必须显式 share=True；普通 create_context() 只会拿回
        # 已附着的上下文，不产生共享对象命名空间。
        assert captured.get("share") is True


# ---------------------------------------------------------------------------
# AUD-1214 / AUD-1215 — GLContextHub
# ---------------------------------------------------------------------------


class TestGLContextHub:
    @pytest.fixture(autouse=True)
    def _fresh_hub(self, monkeypatch):
        monkeypatch.setattr(GLContextHub, "_instance", None)
        yield
        GLContextHub.instance().reset()

    def test_singleton_unique_under_concurrency(self) -> None:
        seen: list[GLContextHub] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            seen.append(GLContextHub.instance())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(h) for h in seen}) == 1  # AUD-1215

    def test_concurrent_register_and_probe_is_consistent(self) -> None:
        hub = GLContextHub()
        stop = threading.Event()
        errors: list[Exception] = []

        def writer() -> None:
            i = 0
            while not stop.is_set():
                hub.register_context(f"ctx{i % 3}", object())
                i += 1

        def reader() -> None:
            try:
                while not stop.is_set():
                    hub.probe_sharing("ctx0", "ctx1")
            except Exception as exc:  # noqa: BLE001 — 测试内收集
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        stop.set()
        for t in threads:
            t.join()
        assert errors == []  # AUD-1215：无撕裂视图/迭代崩溃

    def test_probe_same_object_is_trivially_shared(self) -> None:
        hub = GLContextHub()
        ctx = object()
        hub.register_context("a", ctx)
        hub.register_context("b", ctx)
        assert hub.probe_sharing("a", "b") is True

    def test_probe_unknown_names_fail_closed(self) -> None:
        hub = GLContextHub()
        hub.register_context("a", object())
        assert hub.probe_sharing("a", "missing") is False
        assert hub.probe_sharing("a", "missing") is False  # 负缓存命中

    def test_probe_non_qt_contexts_fail_closed_without_gl(self) -> None:
        # AUD-1214：moderngl 之类的对象无法在任意调用线程上安全探测，
        # 必须 fail-closed 且绝不发出作用于"当前上下文"的 GL 调用。
        hub = GLContextHub()
        hub.register_context("renderer", types.SimpleNamespace())
        hub.register_context("viewer", types.SimpleNamespace())
        assert hub.probe_sharing("renderer", "viewer") is False

    def test_probe_qt_contexts_use_aresharing(self, monkeypatch) -> None:
        calls: list = []

        class _StubQOpenGLContext:
            @staticmethod
            def areSharing(first, second) -> bool:
                calls.append((first, second))
                return True

        stub_pkg = types.ModuleType("PyQt6")
        stub_gui = types.ModuleType("PyQt6.QtGui")
        stub_gui.QOpenGLContext = _StubQOpenGLContext
        stub_pkg.QtGui = stub_gui
        monkeypatch.setitem(sys.modules, "PyQt6", stub_pkg)
        monkeypatch.setitem(sys.modules, "PyQt6.QtGui", stub_gui)

        hub = GLContextHub()
        ctx_a = _StubQOpenGLContext()
        ctx_b = _StubQOpenGLContext()
        hub.register_context("renderer", ctx_a)
        hub.register_context("viewer", ctx_b)
        assert hub.probe_sharing("renderer", "viewer") is True
        assert calls == [(ctx_a, ctx_b)]


# ---------------------------------------------------------------------------
# AUD-1300 — GLResourceRegistry
# ---------------------------------------------------------------------------


class TestRegistryReinsert:
    def test_failed_release_keeps_record_and_retry(self) -> None:
        registry = GLResourceRegistry(owner="fix")
        failed = {"count": 0}

        def flaky() -> None:
            failed["count"] += 1
            if failed["count"] == 1:
                raise RuntimeError("ctx lost")

        registry.register(GROUP_UPLOAD, "tex", release_fn=flaky)

        assert registry.release_group(GROUP_UPLOAD) == []
        # AUD-1300：条目不因回调失败而从台账消失
        assert registry.get(GROUP_UPLOAD, "tex") is not None
        assert registry.snapshot() == {GROUP_UPLOAD: ["tex"]}

        assert registry.release_group(GROUP_UPLOAD) == ["tex"]  # 重试成功
        assert registry.record_count() == 0


# ---------------------------------------------------------------------------
# AUD-1401 — Metal dispatch 丢弃链信号
# ---------------------------------------------------------------------------


class _FakeEnc:
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    def setComputePipelineState_(self, pso) -> None:
        pass

    def setBuffer_offset_atIndex_(self, buf, offset, index) -> None:
        if self._fail:
            raise RuntimeError("bind failed")

    def setTexture_atIndex_(self, tex, index) -> None:
        pass

    def dispatchThreads_threadsPerThreadgroup_(self, size, tg) -> None:
        pass

    def endEncoding(self) -> None:
        pass


class _FakeCB:
    def __init__(self, fail_next_encoding: bool = False) -> None:
        self._fail_next = fail_next_encoding
        self.committed = False

    def computeCommandEncoder(self):
        enc = _FakeEnc(self._fail_next)
        self._fail_next = False
        return enc

    def commit(self) -> None:
        self.committed = True

    def waitUntilCompleted(self) -> None:
        pass

    def error(self):
        return None

    def gpuStartTime(self) -> float:
        return 10.0

    def gpuEndTime(self) -> float:
        return 10.0025


class _FailFirstEncodingQueue:
    """第 1 个 command buffer 的 encoder 绑定 buffer 即抛错。"""

    def __init__(self) -> None:
        self.buffers: list[_FakeCB] = []

    def commandBuffer(self) -> _FakeCB:
        cb = _FakeCB(fail_next_encoding=(len(self.buffers) == 0))
        self.buffers.append(cb)
        return cb


class _StubDevice:
    def __init__(self) -> None:
        self.queue = _FailFirstEncodingQueue()

    def newCommandQueue(self):
        return self.queue


@pytest.fixture()
def stub_metal(monkeypatch):
    stub = MagicMock()
    stub.MTLResourceStorageModeShared = 1
    stub.MTLSizeMake = lambda w, h, d: (w, h, d)
    monkeypatch.setitem(sys.modules, "Metal", stub)
    return stub


class TestDroppedWorkSignal:
    def _ctx(self, stub_metal) -> MetalComputeContext:
        return MetalComputeContext(device=_StubDevice())

    def test_submit_raises_once_after_drop_and_recovers(self, stub_metal) -> None:
        from src.shared.gpu.metal.context import MetalKernel

        ctx = self._ctx(stub_metal)
        kernel = MetalKernel("k", types.SimpleNamespace(
            maxTotalThreadsPerThreadgroup=lambda: 512))

        with pytest.raises(RuntimeError, match="bind failed"):
            # 第 1 个 cb：kernel 绑定 buffer 即失败 → 整链丢弃
            ctx.dispatch(kernel, buffers={0: None}, threads=(4, 1, 1))

        assert ctx.has_dropped_work is True
        with pytest.raises(MetalComputeError, match="丢弃"):
            ctx.finish()  # 不能静默返回 None 冒充“无 pending 工作”
        assert ctx.has_dropped_work is False  # 一次即清

        # 调用方重编整条链后恢复正常提交
        ctx.dispatch(kernel, threads=(4, 1, 1))
        ctx.finish()
        assert ctx.has_dropped_work is False

    def test_successful_chain_submission_clears_flag(self, stub_metal) -> None:
        from src.shared.gpu.metal.context import MetalKernel

        ctx = self._ctx(stub_metal)
        kernel = MetalKernel("k", types.SimpleNamespace(
            maxTotalThreadsPerThreadgroup=lambda: 512))
        ctx.dispatch(kernel, threads=(4, 1, 1))
        ctx.finish()
        assert ctx.has_dropped_work is False
        assert ctx._lock is not None  # 常规路径不受影响


# ---------------------------------------------------------------------------
# AUD-1414 — kernels_dec 状态契约
# ---------------------------------------------------------------------------


def test_kernels_dec_no_undeclared_status_codes() -> None:
    # 声明集 {0,4,9,11}；93u 是 slice 版 zigzag 越界分支的笔误
    assert "93u" not in kernels_dec.METAL_RANS2_DECODE_MSL
    assert "if (idx > 63u) { status[sid] = 9u; return; }" in (
        kernels_dec.METAL_RANS2_DECODE_MSL)


# ---------------------------------------------------------------------------
# AUD-1510 — yuv convert 位深支持集
# ---------------------------------------------------------------------------


class TestYuvBitDepth:
    @pytest.mark.parametrize("bd", [8, 10])
    def test_supported_depths(self, bd: int) -> None:
        params = build_yuv_convert_params(bd, "limited", "bt709")
        assert params["max_code"][0] == float((1 << bd) - 1)

    @pytest.mark.parametrize("bd", [6, 7, 11, 12, 16])
    def test_unsupported_depths_rejected(self, bd: int) -> None:
        with pytest.raises(ValueError, match="位深"):
            build_yuv_convert_params(bd, "limited", "bt709")


# ---------------------------------------------------------------------------
# AUD-1520 / AUD-1521 — MetalSubmissionScheduler
# ---------------------------------------------------------------------------


class _SchedulerCB:
    def __init__(self) -> None:
        self.wait_calls = 0

    def error(self):
        return None

    def waitUntilCompleted(self) -> None:
        self.wait_calls += 1


class _PlainContext:
    def __init__(self) -> None:
        self.callbacks = []
        self.command_buffers: list[_SchedulerCB] = []

    def submit(self, *, completion, wait):
        assert wait is False
        cb = _SchedulerCB()
        self.callbacks.append(completion)
        self.command_buffers.append(cb)
        return cb


class _BlockingSubmitContext(_PlainContext):
    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def submit(self, *, completion, wait):
        self.gate.wait(5.0)  # 模拟 commit 在途：槽位已预留为 None
        return super().submit(completion=completion, wait=wait)


class TestScheduler:
    def test_drain_skips_reserved_none_slot(self) -> None:
        # AUD-1520：drain 快照到 submit() 的 None 预留槽时不得解引用
        # .command_buffer（关机路径 AttributeError）。
        context = _BlockingSubmitContext()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        results: dict = {}

        def do_submit() -> None:
            results["sub"] = scheduler.submit()

        thread = threading.Thread(target=do_submit)
        thread.start()
        time.sleep(0.1)  # 槽位已预留（None），context.submit 尚未返回

        t0 = time.monotonic()
        scheduler.drain(timeout_s=0.2)  # 不得抛 AttributeError
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0

        context.gate.set()
        thread.join(5.0)
        assert results.get("sub") is not None  # submit 侧不受 drain 影响

    def test_drain_waits_resolved_submissions(self) -> None:
        context = _PlainContext()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        scheduler.submit()
        scheduler.drain()
        assert context.command_buffers[0].wait_calls == 1

    def test_on_complete_failure_recorded_not_escaped(self) -> None:
        # AUD-1521：on_complete 在 completion 线程抛异常不得逃逸，
        # 且要进入 error() 供 poll 侧发现。
        context = _PlainContext()
        scheduler = MetalSubmissionScheduler(context, max_in_flight=2)
        released: list[int] = []

        def broken_on_complete(submission, error) -> None:
            raise RuntimeError("viewer gone")

        scheduler.submit(
            release_callbacks=(lambda: released.append(1),),
            on_complete=broken_on_complete,
        )
        context.callbacks[0](context.command_buffers[0])  # 模拟驱动回调

        assert released == [1]  # 前置 release 回调不受影响
        err = scheduler.error(0)
        assert err is not None and "on_complete" in err
        assert scheduler.in_flight == 0


# ---------------------------------------------------------------------------
# AUD-1601–1604 — present_frame
# ---------------------------------------------------------------------------


def _pool_with_slot(slot_id: int = 0) -> tuple[pf.OutputSlotPool, pf.TextureLease]:
    pool = pf.OutputSlotPool(size=1)
    lease = pool.register_slot(slot_id, texture_glo=101, width=16, height=16)
    return pool, lease


class TestTextureLeaseLocking:
    def test_release_all_prevents_state_revival(self) -> None:
        # AUD-1601：shutdown 的 release_all 与渲染线程的（锁外）
        # mark_ready 竞态——最终状态必须一致，不允许 FREE 被复活成 READY。
        for _ in range(200):
            pool, lease = _pool_with_slot()
            acquired = pool.acquire_for_writing(generation_id=1, frame_index=1)
            assert acquired is lease
            cleanups: list[int] = []
            errors: list[Exception] = []

            def renderer() -> None:
                try:
                    lease.mark_ready(
                        0xABCD, lambda f: cleanups.append(f))
                except RuntimeError as exc:  # 已被 release_all 回收：拒绝复活
                    errors.append(exc)

            t_render = threading.Thread(target=renderer)
            t_render.start()
            pool.release_all()
            t_render.join(5.0)

            assert lease.state is pf.SlotState.FREE  # 无僵尸 READY
            assert len(cleanups) <= 1  # fence 至多清理一次
            if not errors:
                assert cleanups == [0xABCD]

    def test_plain_lifecycle_transitions_survive(self) -> None:
        pool, lease = _pool_with_slot()
        lease.begin_writing(1, 5)
        lease.mark_ready(0x1)
        present = lease.make_present_frame(frame=5, generation_id=1)
        assert present.fence_ns == 0x1
        pool.mark_in_viewer(lease.slot_id)
        pool.ack(lease.slot_id)
        assert lease.state is pf.SlotState.RELEASABLE


class TestPresentFrameExtra:
    def test_extra_is_copied_not_shared(self) -> None:
        # AUD-1602：emit 后调用方改写原 dict 不得影响 descriptor。
        _, lease = _pool_with_slot()
        lease.begin_writing(1, 5)
        lease.mark_ready(0)
        payload = {"note": "a"}
        present = lease.make_present_frame(
            frame=5, generation_id=1, extra=payload)
        payload["note"] = "b"
        payload["extra2"] = True
        assert present.extra == {"note": "a"}


class TestPoolDiscard:
    def test_discard_recycles_ready_slot_with_fence(self) -> None:
        pool, lease = _pool_with_slot()
        cleaned: list[int] = []
        lease.begin_writing(1, 5)
        lease.mark_ready(0x7E, lambda f: cleaned.append(f))

        assert pool.discard(lease.slot_id) is True
        assert cleaned == [0x7E]  # fence 随 discard 回收
        assert lease.state is pf.SlotState.RELEASABLE
        # 回收后的 slot 可再次 acquire（画面不再被冻结）
        re_acquired = pool.acquire_for_writing(generation_id=1, frame_index=6)
        assert re_acquired is lease

    def test_discard_rejects_inert_states(self) -> None:
        pool, lease = _pool_with_slot()
        assert pool.discard(lease.slot_id) is False  # FREE：无事可回收
        assert pool.discard(999) is False  # 不存在的 slot


class TestRegisterSlotValidation:
    def test_rejects_non_positive_arguments(self) -> None:
        pool = pf.OutputSlotPool(size=1)
        with pytest.raises(ValueError, match="texture_glo"):
            pool.register_slot(0, texture_glo=0, width=16, height=16)
        with pytest.raises(ValueError, match="width/height"):
            pool.register_slot(0, texture_glo=1, width=0, height=16)
        with pytest.raises(ValueError, match="width/height"):
            pool.register_slot(0, texture_glo=1, width=16, height=-4)

    def test_duplicate_registration_recovers_pending_fence(self) -> None:
        # AUD-1604：重复 slot_id 不得静默覆盖未清理的 fence（GLsync 泄漏）。
        pool = pf.OutputSlotPool(size=1)
        old = pool.register_slot(0, texture_glo=101, width=8, height=8)
        old.begin_writing(1, 3)
        cleaned: list[int] = []
        old.mark_ready(0xFE, lambda f: cleaned.append(f))

        new = pool.register_slot(0, texture_glo=202, width=8, height=8)
        assert cleaned == [0xFE]  # 旧 lease 的 fence 已被回收
        assert new is not old
        assert pool.get_slot(0) is new


# ---------------------------------------------------------------------------
# AUD-33800 / AUD-33801 — i18n
# ---------------------------------------------------------------------------


class _I18NStateGuard:
    """保存/恢复 I18N 类级状态，避免测试间污染。"""

    def __enter__(self):
        self.saved = (
            I18N._translations,
            I18N._fallback_translations,
            I18N._current_locale,
            I18N._locales_dir,
            list(I18N._module_locales_dirs),
        )
        return self

    def __exit__(self, *exc) -> None:
        (I18N._translations,
         I18N._fallback_translations,
         I18N._current_locale,
         I18N._locales_dir,
         I18N._module_locales_dirs) = self.saved
        I18N._module_locales_dirs = list(I18N._module_locales_dirs)


class TestTrFormatErrors:
    def test_main_path_degrades_on_malformed_template(self) -> None:
        with _I18NStateGuard():
            I18N._translations = {
                "bad": {"brace": "100% {", "positional": "{0}"},
                "missing": {"arg": "{name}"},
                "ok": "hello {name}",
            }
            I18N._fallback_translations = {}
            # ValueError（孤立 '{'）不逃逸（AUD-33800）
            assert I18N.tr("bad.brace", name=1) == "100% {"
            # IndexError（位置占位符）不逃逸
            assert I18N.tr("bad.positional", name=1) == "{0}"
            # KeyError（缺参数）维持原降级
            assert I18N.tr("missing.arg") == "{name}"
            assert I18N.tr("ok", name="x") == "hello x"

    def test_fallback_path_degrades_on_malformed_template(self) -> None:
        with _I18NStateGuard():
            # 非空词典缺键才会走 fallback 导航
            I18N._translations = {"unrelated": "x"}
            I18N._fallback_translations = {"bad": {"brace": "}"}}
            assert I18N.tr("bad.brace", name=1) == "}"


class TestTranslationRootValidation:
    def test_load_locale_rejects_non_dict_root(self, tmp_path) -> None:
        with _I18NStateGuard():
            locales = tmp_path / "locales"
            locales.mkdir()
            (locales / "en_US.json").write_text('["menu", "file"]', encoding="utf-8")
            I18N.initialize(str(locales))

            assert I18N.load_locale("en_US") is False
            assert I18N._translations == {}

    def test_module_merge_survives_non_dict_root(self, tmp_path) -> None:
        # AUD-33801：模块激活路径不得因畸形翻译文件抛未捕获异常。
        with _I18NStateGuard():
            I18N._translations = {"menu": {"file": "File"}}
            module_dir = tmp_path / "feature_locales"
            module_dir.mkdir()
            (module_dir / "en_US.json").write_text(
                json.dumps(["not", "a", "dict"]), encoding="utf-8")

            I18N.load_module_translations(str(module_dir))  # 不得抛异常
            assert I18N._translations == {"menu": {"file": "File"}}

    def test_module_merge_applies_valid_root(self, tmp_path) -> None:
        with _I18NStateGuard():
            I18N._translations = {"menu": {"file": "File"}}
            module_dir = tmp_path / "feature_locales"
            module_dir.mkdir()
            (module_dir / "en_US.json").write_text(
                json.dumps({"menu": {"edit": "Edit"}}), encoding="utf-8")

            I18N.load_module_translations(str(module_dir))
            assert I18N.tr("menu.edit") == "Edit"
