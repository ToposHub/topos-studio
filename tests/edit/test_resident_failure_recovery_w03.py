"""W03（计划 W-I02）：运行中驻留源失败的真实恢复 —— L1 逻辑测试。

覆盖三个层面的纯逻辑（全平台可跑，无 GPU/无 Windows 依赖）：

1. 按素材降级记账（EditGpuDecodeManager.note_resident_import_failure /
   _resident_route_allowed_for）：冷却窗口、限次重试上限、显式恢复。
2. 降级关闭（_apply_resident_demotion）：只关该素材的 win_cuda_resident
   源、租约未归零推迟关闭（复用 P2-09 契约）、其它素材/其它类型不受
   影响（故障隔离）、close_all 后 no-op。
3. 渲染器上报（EditGpuPreviewRenderer._import_cuda_resident_frame）：
   失败发 cuda_resident_import_failed(asset, reason, generation)、
   会话 destroyed 时丢弃死会话引用（下次重建）、活会话保留。
4. _create_source 3.4b 路由门：被降级素材不再创建驻留源、同门下其它
   素材不受影响（monkeypatch sys.platform=win32 越过平台门，复用
   d2ce07f2e 的 L1 手法；真实 Windows 路由由 L3 验证）。

真实错误 handler→重建→重新呈现的端到端证据见
tests/edit/test_cuda_resident_failure_recovery_l3.py（机器 B）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.edit.core import edit_gpu_decode_manager as egdm


def _key(manager, asset: str, stream: str | None = None,
         policy: str = 'auto') -> tuple:
    return manager._make_source_key(
        stream or asset,
        egdm._DecodeSourceSpec(
            original_path=asset, decode_path=asset, target_size=None,
            source_kind="original", vt_policy=policy),
    )


class _FakeSource:
    """可计数的假解码源（close 恰一次断言用）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.close_count = 0
        self.closed = threading.Event()

    def close(self) -> None:
        self.close_count += 1
        self.closed.set()


class TestResidentDemotionBookkeeping:
    """按素材降级记账：冷却窗口 / 限次 / 显式恢复。"""

    def test_note_failure_bars_route_within_cooldown(self):
        m = egdm.EditGpuDecodeManager()
        allowed_before, reason_before = m._resident_route_allowed_for("a.mov")
        assert allowed_before is True and reason_before == ''

        assert m.note_resident_import_failure("a.mov", "copy_failed:x") is True

        allowed, reason = m._resident_route_allowed_for("a.mov")
        assert allowed is False
        assert reason == 'demoted_cooldown'
        # 其他素材不受影响（故障隔离）
        assert m._resident_route_allowed_for("b.mov")[0] is True

        stats = m.resident_demotion_stats()
        assert stats["a.mov"]["cycles"] == 1
        assert stats["a.mov"]["last_reason"] == "copy_failed:x"
        assert stats["a.mov"]["barred_permanently"] is False
        assert stats["a.mov"]["cooldown_remaining_s"] > 0

    def test_cooldown_expiry_allows_reprobe(self, monkeypatch):
        m = egdm.EditGpuDecodeManager()
        m.note_resident_import_failure("a.mov", "copy_failed:x")
        assert m._resident_route_allowed_for("a.mov")[1] == 'demoted_cooldown'

        real_perf = time.perf_counter
        offset = {"v": 0.0}

        def _shifted() -> float:
            return real_perf() + offset["v"]

        monkeypatch.setattr(time, "perf_counter", _shifted)
        monkeypatch.setattr(egdm.time, "perf_counter", _shifted)
        offset["v"] = egdm._RESIDENT_DEMOTE_COOLDOWN_S + 1.0
        allowed, reason = m._resident_route_allowed_for("a.mov")
        assert allowed is True and reason == ''
        # cycle 记账保留（限次上限据此累计）
        assert m.resident_demotion_stats()["a.mov"]["cycles"] == 1

    def test_max_cycles_permanent_bar_until_reset(self, monkeypatch):
        m = egdm.EditGpuDecodeManager()
        for _ in range(egdm._RESIDENT_MAX_DEMOTE_CYCLES + 1):
            m.note_resident_import_failure("a.mov", "copy_failed:x")

        allowed, reason = m._resident_route_allowed_for("a.mov")
        assert allowed is False
        assert reason == 'demoted_max_cycles'

        # 时间前进也无法自动恢复（限次重试耗尽）
        real_perf = time.perf_counter

        def _far_future() -> float:
            return real_perf() + 10**9

        monkeypatch.setattr(egdm.time, "perf_counter", _far_future)
        assert m._resident_route_allowed_for("a.mov")[0] is False

        # 显式恢复（诊断接口）后允许重新探测
        m.reset_resident_demotions("a.mov")
        assert m._resident_route_allowed_for("a.mov")[0] is True
        assert "a.mov" not in m.resident_demotion_stats()

    def test_repeat_failure_while_pending_arms_single_close(self,
                                                            monkeypatch):
        m = egdm.EditGpuDecodeManager()
        # 让降级关闭线程停在租约排水（作业在飞），dedup 窗口确定存在
        monkeypatch.setattr(egdm, "_CLOSE_LEASE_DRAIN_TIMEOUT", 10.0)
        src = _FakeSource("pending_resident")
        key = _key(m, "a.mov")
        with m._lock:
            m._sources[key] = src
            m._source_types[key] = "win_cuda_resident"
            m._source_cache[key] = (src, "win_cuda_resident")
            m._lease_source(key)

        assert m.note_resident_import_failure("a.mov", "r1") is True
        # 降级关闭仍在 pending：重复报告只记账，不再排第二个关闭作业
        assert m.note_resident_import_failure("a.mov", "r2") is False
        assert m.resident_demotion_stats()["a.mov"]["cycles"] == 2
        with m._lock:
            assert "a.mov" in m._resident_demotion_pending

        # 租约归零 → 掔一个（也是唯一一个）关闭作业完成收尾
        m._release_source(key)
        assert src.closed.wait(timeout=5.0)
        assert src.close_count == 1
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            with m._lock:
                if "a.mov" not in m._resident_demotion_pending:
                    break
            time.sleep(0.02)
        with m._lock:
            assert "a.mov" not in m._resident_demotion_pending

    def test_note_after_close_all_is_rejected(self):
        m = egdm.EditGpuDecodeManager()
        m.close_all()
        assert m.note_resident_import_failure("a.mov", "r") is False
        assert m.resident_demotion_stats() == {}


class TestResidentDemotionClose:
    """降级关闭：只关该素材驻留源 / 租约排水推迟 / 恰一次。"""

    @staticmethod
    def _install(m, asset: str, source: _FakeSource, source_type: str,
                 stream: str | None = None) -> tuple:
        key = _key(m, asset, stream)
        with m._lock:
            m._sources[key] = source
            m._source_types[key] = source_type
            m._source_cache[key] = (source, source_type)
        return key

    def test_closes_only_resident_sources_of_that_asset(self):
        m = egdm.EditGpuDecodeManager()
        bad = _FakeSource("bad_resident")
        healthy_same_asset = _FakeSource("same_asset_planar")
        other_asset = _FakeSource("other_asset_resident")
        key_bad = self._install(m, "a.mov", bad, "win_cuda_resident")
        self._install(m, "a.mov", healthy_same_asset, "vt_planar", stream="s2")
        key_other = self._install(m, "b.mov", other_asset, "win_cuda_resident")

        m._apply_resident_demotion("a.mov")

        assert bad.close_count == 1  # 恰一次
        assert healthy_same_asset.close_count == 0  # 同素材其它类型源不动
        assert other_asset.close_count == 0  # 其它素材驻留源不动（隔离）
        with m._lock:
            assert key_bad not in m._sources
            assert m._source_cache.get(key_bad) is None
            assert key_other in m._sources  # b.mov 路由不受影响

    def test_demotion_defers_close_until_leases_drain(self, monkeypatch):
        m = egdm.EditGpuDecodeManager()
        monkeypatch.setattr(egdm, "_CLOSE_LEASE_DRAIN_TIMEOUT", 0.3)
        src = _FakeSource("leased_resident")
        key = self._install(m, "a.mov", src, "win_cuda_resident")
        with m._lock:
            m._lease_source(key)  # 模拟在飞解码租约

        # 排水超时：登记推迟关闭，绝不在租约持有中 close
        m._apply_resident_demotion("a.mov")
        assert src.close_count == 0
        with m._lock:
            assert key in m._deferred_close_sources
            assert key not in m._sources

        # 最后一个租约归零 → 推迟关闭恰一次补执行
        m._release_source(key)
        assert src.close_count == 1
        with m._lock:
            assert key not in m._deferred_close_sources

    def test_demotion_is_idempotent_and_noop_without_resident(self):
        m = egdm.EditGpuDecodeManager()
        planar = _FakeSource("planar")
        self._install(m, "a.mov", planar, "vt_planar")
        m._apply_resident_demotion("a.mov")  # 无驻留源：安静 no-op
        m._apply_resident_demotion("a.mov")  # 重复调用幂等
        assert planar.close_count == 0

    def test_demotion_async_thread_closes_source(self):
        m = egdm.EditGpuDecodeManager()
        src = _FakeSource("async_resident")
        self._install(m, "a.mov", src, "win_cuda_resident")
        assert m.note_resident_import_failure("a.mov", "copy_failed") is True
        assert src.closed.wait(timeout=5.0), "降级关闭线程未在 5s 内执行"
        assert src.close_count == 1
        # pending 标记随线程收尾清除（可再排新作业）
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            with m._lock:
                if "a.mov" not in m._resident_demotion_pending:
                    break
            time.sleep(0.02)
        with m._lock:
            assert "a.mov" not in m._resident_demotion_pending


class TestCreateSourceDemotionGate:
    """_create_source 3.4b：降级素材不再创建驻留源（平台门越过后）。"""

    @pytest.fixture()
    def win32_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        asset = str(tmp_path / "clip.mov")
        Path(asset).write_bytes(b"")

        import src.shared.media.windows_hw.cuda_resident as cr

        created: list[str] = []

        class _FakeResidentSource:
            def __init__(self, path: str) -> None:
                created.append(path)

            def open(self) -> None:  # noqa: D102 - 验证首帧驻留格式的桩
                return None

            @property
            def resident_contract_format(self) -> str:
                return "cuda"

            @property
            def hw_output_format(self) -> str:
                return "cuda"

            def close(self) -> None:
                return None

        monkeypatch.setattr(cr, "resident_route_available", lambda: True)
        monkeypatch.setattr(
            cr, "probe_cuda_resident_capability",
            lambda: (True, ""))
        monkeypatch.setattr(cr, "WindowsCudaResidentSource", _FakeResidentSource)
        # 越过 factory/disk-proxy/codec 探测：固定 h264 + 非代理
        monkeypatch.setitem(sys.modules, "src.shared.media.factory", None)
        monkeypatch.setattr(
            egdm.HardwareCapability, "has_nvidia_gpu",
            staticmethod(lambda: True))
        return {"asset": asset, "created": created}

    def test_demoted_asset_falls_through_resident_branch(
            self, win32_env, monkeypatch):
        m = egdm.EditGpuDecodeManager()
        asset = win32_env["asset"]
        monkeypatch.setattr(
            m, "_probe_codec", lambda path: "h264")
        monkeypatch.setattr(m, "_is_topos_movie_path", lambda path: False)

        source, source_type = m._create_source(asset)
        assert source_type == "win_cuda_resident"
        assert win32_env["created"] == [asset]

        m.note_resident_import_failure(asset, "copy_failed:injected")
        m._apply_resident_demotion(asset)

        # 降级后：不再创建驻留源，路由落到下载/CPU 分支（桩文件必然
        # 打不开 → fail-closed None，而非冒充驻留）
        del win32_env["created"][:]
        source2, source_type2 = m._create_source(asset)
        assert source2 is None
        assert source_type2 in ("cpu", "vt_planar")
        assert win32_env["created"] == []

    def test_other_asset_still_gets_resident_route(self, win32_env,
                                                   monkeypatch):
        m = egdm.EditGpuDecodeManager()
        asset_a, asset_b = win32_env["asset"], win32_env["asset"] + "b"
        Path(asset_b).write_bytes(b"")
        monkeypatch.setattr(m, "_probe_codec", lambda path: "h264")
        monkeypatch.setattr(m, "_is_topos_movie_path", lambda path: False)

        m.note_resident_import_failure(asset_a, "copy_failed:injected")
        m._apply_resident_demotion(asset_a)

        source, source_type = m._create_source(asset_b)
        assert source_type == "win_cuda_resident"
        assert win32_env["created"] == [asset_b]
        if source is not None:
            source.close()


class TestRendererImportFailureReporting:
    """渲染器导入失败上报：信号载荷 + 会话失效丢弃。"""

    @pytest.fixture()
    def renderer(self):
        # 无 pytest-qt 的逻辑 CI 也可收集本文件：QApplication 按需内建
        # （不走 qapp fixture——插件缺席时 setup 直接 error 而非 skip）
        pytest.importorskip("PyQt6")
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        del app  # 保活引用：QThread 构造只需实例存在
        from src.features.edit.core.edit_gpu_preview_renderer import (
            EditGpuPreviewRenderer,
        )

        r = EditGpuPreviewRenderer(main_context=object())
        # 构造器排了 singleShot(0) 的共享上下文初始化（需要真 GL 主上下
        # 文）。L1 不起渲染线程：置取消标志让该回调安静早退（模块内置的
        # 取消协议），哨兵 main_context 不会被触碰。
        r._context_setup_cancelled.set()
        yield r
        # 未 start() 的 QThread：只断开信号，无需 wait
        try:
            r.disconnect()
        except Exception:
            pass

    @staticmethod
    def _fake_cu_frame() -> "_RecordingFrame":
        return _RecordingFrame()

    def test_session_unavailable_emits_with_asset_and_generation(
            self, renderer, monkeypatch):
        monkeypatch.setattr(
            renderer, "_ensure_cuda_interop_session", lambda: None)
        frame = self._fake_cu_frame()

        got: list[tuple] = []
        renderer.cuda_resident_import_failed.connect(
            lambda a, r_, g: got.append((a, r_, g)))
        out = renderer._import_cuda_resident_frame(
            frame, asset_path="x/clip.mov", generation_id=7)

        assert out is None
        assert got == [("x/clip.mov", "interop_session_unavailable", 7)]
        assert frame.release_count == 1  # 源帧恰一次释放

    def test_destroyed_session_reference_dropped(self, renderer, monkeypatch):
        session = _FakeInteropSession(destroyed=True,
                                      reason="copy_failed:uv")
        monkeypatch.setattr(
            renderer, "_ensure_cuda_interop_session",
            lambda: _adopt(renderer, session))
        frame = self._fake_cu_frame()

        got: list[tuple] = []
        renderer.cuda_resident_import_failed.connect(
            lambda a, r_, g: got.append((a, r_, g)))
        out = renderer._import_cuda_resident_frame(
            frame, asset_path="a.mov", generation_id=3)

        assert out is None
        assert got == [("a.mov", "copy_failed:uv", 3)]
        assert renderer._cuda_interop_session is None  # 死会话引用丢弃
        assert frame.release_count == 1

    def test_live_session_reference_kept(self, renderer, monkeypatch):
        session = _FakeInteropSession(destroyed=False,
                                      reason="copy_failed:y")
        monkeypatch.setattr(
            renderer, "_ensure_cuda_interop_session",
            lambda: _adopt(renderer, session))
        frame = self._fake_cu_frame()

        got: list[tuple] = []
        renderer.cuda_resident_import_failed.connect(
            lambda a, r_, g: got.append((a, r_, g)))
        out = renderer._import_cuda_resident_frame(frame, "a.mov", 1)

        assert out is None
        assert got == [("a.mov", "copy_failed:y", 1)]
        # 会话仍然存活：引用保留（下次导入继续用，不重建）
        assert renderer._cuda_interop_session is session
        assert frame.release_count == 1


class TestTimelineSlotForwardsToManager:
    """TPR 槽 _on_cuda_resident_import_failed：转发到管理器（生产接线体）。"""

    def test_slot_demotes_via_manager(self):
        from types import SimpleNamespace

        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer,
        )

        m = egdm.EditGpuDecodeManager()
        shell = SimpleNamespace(_gpu_decode_manager=m)
        TimelinePreviewRenderer._on_cuda_resident_import_failed(
            shell, "a.mov", "copy_failed:uv", generation=9)

        allowed, reason = m._resident_route_allowed_for("a.mov")
        assert allowed is False
        assert m.resident_demotion_stats()["a.mov"]["last_reason"] == \
            "copy_failed:uv"

    def test_slot_survives_manager_exception(self):
        from types import SimpleNamespace

        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer,
        )

        class _BrokenManager:
            def note_resident_import_failure(self, *a, **k):
                raise RuntimeError("boom")

        shell = SimpleNamespace(_gpu_decode_manager=_BrokenManager())
        # 恢复链失败只留痕不外抛（GL 线程信号链不能因管理器故障崩 UI）
        TimelinePreviewRenderer._on_cuda_resident_import_failed(
            shell, "a.mov", "r", 1)

    def test_slot_noop_without_manager(self):
        from types import SimpleNamespace

        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer,
        )

        shell = SimpleNamespace()
        TimelinePreviewRenderer._on_cuda_resident_import_failed(
            shell, "a.mov", "r", 1)


class _RecordingFrame:
    """假 CudaResidentFrame：release 幂等 + 计数。"""

    def __init__(self) -> None:
        self.release_count = 0

    def release(self) -> None:
        self.release_count += 1


def _adopt(renderer, session):
    """模拟真 _ensure_cuda_interop_session 的缓存语义（首访即登记）。"""
    renderer._cuda_interop_session = session
    return session


class _FakeInteropSession:
    """假 interop 会话：bind 固定失败 + stats.destroyed 可配。"""

    def __init__(self, *, destroyed: bool, reason: str) -> None:
        self.last_fallback_reason = reason
        self._stats = {"destroyed": destroyed, "bind_count": 0,
                       "fail_count": 0}

    @property
    def stats(self) -> dict:
        return self._stats

    def bind_resident_frame(self, cu_frame, decode_path: str = ""):
        return None
