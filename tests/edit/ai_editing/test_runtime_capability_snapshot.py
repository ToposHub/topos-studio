"""T-P0-00 RuntimeCapabilitySnapshot 聚焦测试。

覆盖总计划任务卡的验证要求：
1. 四种环境（正确 release dylib / 不兼容 dylib / 未设置 dylib / GPU 不可用）
   均断言四态状态与稳定 reason_code；
2. snapshot_id 稳定性与敏感性（检测时间不参与）；
3. CapabilitySnapshotStore 变化事件（ID 变化才通知）；
4. build_manifest 注入：runtime 块 + topos 不可用时 export codecs fail-closed；
5. toposctl 离线路径与快照同源（runtime.snapshot_id 可见）；
6. Deliver preflight：注入快照后 topos 交付目标在 codec 不可用时 BLOCKING；
7. 模块级不依赖 Qt/GL（离线 CLI 可用）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.features.edit.ai_editing.capability_status import _LOCAL_PACK_IDS
from src.features.edit.ai_editing.runtime_capability_snapshot import (
    CapabilitySnapshotStore,
    RuntimeCapabilityEntry,
    RuntimeCapabilityState,
    RuntimeCapabilitySnapshot,
    build_runtime_capability_snapshot,
)

RELEASE_DYLIB = (
    Path(__file__).resolve().parents[3]
    / "native/topos_codec/build/release/topos_codec.0.1.0.dylib"
)


def _codec_entry(snapshot: RuntimeCapabilitySnapshot):
    entry = snapshot.entry("codec.topos")
    assert entry is not None
    return entry


def _gpu_unavailable_prober():
    def prober():
        return {
            "status": "UNAVAILABLE",
            "reason_code": "GPU_INFO_SYSTEM_PROFILER_FAILED",
            "detail": "simulated: no GPU info",
            "devices": [],
        }
    return prober


# ---------------------------------------------------------------------------
# 四种 dylib 环境
# ---------------------------------------------------------------------------


class TestToposCodecEnvironments:
    def test_release_dylib_available(self) -> None:
        if not RELEASE_DYLIB.is_file():
            pytest.skip("release dylib 未构建")
        snapshot = build_runtime_capability_snapshot(
            topos_lib_path=str(RELEASE_DYLIB))
        entry = _codec_entry(snapshot)
        assert entry.state is RuntimeCapabilityState.AVAILABLE
        assert entry.reason_code is None
        assert entry.info["abi_version"] >= 1
        assert entry.info["library_path"] == str(RELEASE_DYLIB)
        # 档位条目跟随 codec 状态，不第四份手抄
        profiles = snapshot.entry("codec.topos.profiles")
        assert profiles is not None
        assert profiles.state is RuntimeCapabilityState.AVAILABLE

    def test_incompatible_dylib(self, tmp_path: Path) -> None:
        bogus = tmp_path / "not_a_dylib.dylib"
        bogus.write_bytes(b"this is not a mach-o image")
        snapshot = build_runtime_capability_snapshot(topos_lib_path=str(bogus))
        entry = _codec_entry(snapshot)
        assert entry.state is RuntimeCapabilityState.INCOMPATIBLE
        assert entry.reason_code == "TOPOS_CODEC_INCOMPATIBLE"
        # fail-closed 传导到档位
        profiles = snapshot.entry("codec.topos.profiles")
        assert profiles.state is RuntimeCapabilityState.INCOMPATIBLE
        assert profiles.reason_code == "TOPOS_CODEC_INCOMPATIBLE"

    def test_missing_dylib_env(self, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOPOS_CODEC_LIB", str(tmp_path / "nope.dylib"))
        snapshot = build_runtime_capability_snapshot()
        entry = _codec_entry(snapshot)
        assert entry.state is RuntimeCapabilityState.UNAVAILABLE
        assert entry.reason_code == "TOPOS_CODEC_LIB_NOT_FOUND"

    def test_no_dylib_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.shared.codec import topos_binding

        monkeypatch.delenv("TOPOS_CODEC_LIB", raising=False)
        monkeypatch.setattr(topos_binding, "candidate_library_paths",
                            lambda: [])
        snapshot = build_runtime_capability_snapshot()
        entry = _codec_entry(snapshot)
        assert entry.state is RuntimeCapabilityState.UNAVAILABLE
        assert entry.reason_code == "TOPOS_CODEC_LIB_NOT_FOUND"


# ---------------------------------------------------------------------------
# GPU 不可用环境
# ---------------------------------------------------------------------------


class TestGpuUnavailable:
    def test_gpu_and_metal_unavailable_with_reason(self) -> None:
        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober())
        gpu = snapshot.entry("gpu.display")
        assert gpu.state is RuntimeCapabilityState.UNAVAILABLE
        assert gpu.reason_code == "GPU_INFO_SYSTEM_PROFILER_FAILED"
        metal = snapshot.entry("gpu.metal")
        assert metal.state is RuntimeCapabilityState.UNAVAILABLE
        assert metal.reason_code == "METAL_INFO_REQUIRES_GPU_INFO"
        # GL 默认诚实记录"探测推迟"，不得伪装可用
        gl = snapshot.entry("render.gl")
        assert gl.state is RuntimeCapabilityState.FALLBACK
        assert gl.reason_code == "GL_PROBE_DEFERRED_TO_RUNTIME"

    def test_injected_gl_prober(self) -> None:
        def gl_prober():
            return {"status": "AVAILABLE", "reason_code": None,
                    "detail": None, "gl_version": "4.1",
                    "renderer": "AMD Radeon"}
        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(), gl_prober=gl_prober)
        gl = snapshot.entry("render.gl")
        assert gl.state is RuntimeCapabilityState.AVAILABLE
        assert gl.info["gl_version"] == "4.1"


# ---------------------------------------------------------------------------
# snapshot_id 与模型包
# ---------------------------------------------------------------------------


class TestSnapshotIdentity:
    def test_id_stable_and_time_insensitive(self) -> None:
        s1 = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(), now="2026-09-17T10:00:00+0000")
        s2 = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(), now="2026-09-17T11:00:00+0000")
        assert s1.snapshot_id == s2.snapshot_id
        assert s1.detected_at != s2.detected_at

    def test_id_sensitive_to_state_change(self) -> None:
        s_ok = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober())
        s_bad = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(), topos_lib_path="/no/such.dylib")
        assert s_ok.snapshot_id != s_bad.snapshot_id

    def test_to_dict_json_safe(self) -> None:
        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober())
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        assert "snapshot_id" in payload

    def test_model_packs_without_manager_fail_closed(self) -> None:
        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober())
        for pack_id in sorted(_LOCAL_PACK_IDS):
            entry = snapshot.entry(f"models.{pack_id}")
            assert entry is not None
            assert entry.state is RuntimeCapabilityState.UNAVAILABLE
            assert entry.reason_code == "MODEL_MANAGER_NOT_PROVIDED"

    def test_model_packs_with_demo_manager(self) -> None:
        class _Status:
            def __init__(self, usable, is_demo):
                self.usable = usable
                self.is_demo = is_demo

        class _Manager:
            def get_status(self, pack_id):
                if pack_id == "qwen3_asr":
                    return _Status(True, False)
                if pack_id == "qwen3_align":
                    return _Status(True, True)
                return _Status(False, False)

        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(),
            model_pack_manager=_Manager())
        assert snapshot.entry("models.qwen3_asr").state is (
            RuntimeCapabilityState.AVAILABLE)
        assert snapshot.entry("models.qwen3_align").state is (
            RuntimeCapabilityState.FALLBACK)
        assert snapshot.entry("models.qwen3_align").reason_code == (
            "MODEL_PACK_DEMO")
        assert snapshot.entry("models.nemo_sortformer").state is (
            RuntimeCapabilityState.UNAVAILABLE)


# ---------------------------------------------------------------------------
# Store 事件
# ---------------------------------------------------------------------------


class TestSnapshotStore:
    @staticmethod
    def _snapshot(good: bool) -> RuntimeCapabilitySnapshot:
        state = (RuntimeCapabilityState.AVAILABLE if good
                 else RuntimeCapabilityState.UNAVAILABLE)
        entry = RuntimeCapabilityEntry(
            component_id="codec.topos", state=state,
            reason_code=None if good else "TOPOS_CODEC_LIB_NOT_FOUND")
        return RuntimeCapabilitySnapshot(
            entries=(entry,), platform={"os": "test"},
            git_revision="deadbeef", detected_at="t")

    def test_event_fires_only_on_id_change(self) -> None:
        store = CapabilitySnapshotStore()
        events: list[str] = []
        store.subscribe(lambda s: events.append(s.snapshot_id[:8]))
        s1 = self._snapshot(True)
        assert store.set(s1) is True
        assert store.set(s1) is False  # 同一 snapshot：无事件
        assert events == [s1.snapshot_id[:8]]
        s2 = self._snapshot(False)
        assert store.set(s2) is True
        assert len(events) == 2

    def test_listener_failure_does_not_block(self) -> None:
        store = CapabilitySnapshotStore()

        def boom(_s):
            raise RuntimeError("listener bug")

        store.subscribe(boom)
        assert store.set(self._snapshot(True)) is True
        assert store.get() is not None


# ---------------------------------------------------------------------------
# build_manifest 注入
# ---------------------------------------------------------------------------


class TestManifestInjection:
    def test_manifest_without_snapshot_keeps_legacy_shape(self) -> None:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        manifest = build_manifest().to_dict()
        assert manifest["runtime"] == {}
        assert any(c.startswith("topos") for c in manifest["export"]["codecs"])

    def test_manifest_with_snapshot_embeds_runtime(self) -> None:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober())
        data = build_manifest(runtime_snapshot=snapshot).to_dict()
        assert data["runtime"]["snapshot_id"] == snapshot.snapshot_id
        entry_ids = {e["component_id"] for e in data["runtime"]["entries"]}
        assert {"codec.topos", "gpu.display", "gpu.metal", "render.gl",
                "media.ffprobe.cli"} <= entry_ids

    def test_manifest_gates_topos_codecs_when_unavailable(self) -> None:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(),
            topos_lib_path="/no/such/topos.dylib")
        data = build_manifest(runtime_snapshot=snapshot).to_dict()
        codecs = data["export"]["codecs"]
        assert not any(c.startswith("topos") for c in codecs)
        assert data["export"]["topos_removed_reason_code"] == (
            "TOPOS_CODEC_LIB_NOT_FOUND")
        assert "h264" in codecs  # 其余交付目标不受影响

    def test_manifest_keeps_topos_when_available(self) -> None:
        from src.features.edit.ai_editing.capability_manifest import build_manifest

        if not RELEASE_DYLIB.is_file():
            pytest.skip("release dylib 未构建")
        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober(),
            topos_lib_path=str(RELEASE_DYLIB))
        data = build_manifest(runtime_snapshot=snapshot).to_dict()
        assert any(c.startswith("topos") for c in data["export"]["codecs"])
        assert "topos_removed_reason_code" not in data["export"]


# ---------------------------------------------------------------------------
# toposctl 离线路径
# ---------------------------------------------------------------------------


class TestToposctlOfflineInjection:
    def test_offline_capabilities_include_runtime(self) -> None:
        from src.features.edit.ai_editing.toposctl import run_cli

        result = run_cli(["capabilities"])
        assert result.exit_code == 0
        runtime = result.result.get("runtime") or {}
        assert runtime, "离线 capabilities 必须携带 runtime 快照"
        entry_ids = {e["component_id"] for e in runtime["entries"]}
        assert "codec.topos" in entry_ids
        # 快照真实探测了 dylib：环境里有 release dylib 时必须是 AVAILABLE
        topos = next(e for e in runtime["entries"]
                     if e["component_id"] == "codec.topos")
        if RELEASE_DYLIB.is_file():
            assert topos["state"] == "AVAILABLE"

    def test_offline_matches_connected_manifest_source(self) -> None:
        """离线路径与 build_manifest(runtime_snapshot=…) 同源：operations 一致。"""
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        from src.features.edit.ai_editing.runtime_capability_snapshot import (
            build_runtime_capability_snapshot as build,
        )
        from src.features.edit.ai_editing.toposctl import run_cli

        m = build_manifest(runtime_snapshot=build(
            gpu_prober=_gpu_unavailable_prober()))
        result = run_cli(["capabilities"])
        assert set(result.result["operations"]) == set(m.operations)


# ---------------------------------------------------------------------------
# Deliver preflight 门禁
# ---------------------------------------------------------------------------


class _Entry:
    def __init__(self, state: str, reason: str | None):
        class _S:
            def __init__(self, v):
                self.value = v
        self.state = _S(state)
        self.reason_code = reason
        self.detail = "unit-test detail"


class _SnapshotStub:
    def __init__(self, state: str, reason: str | None):
        self._entry = _Entry(state, reason)

    def entry(self, component_id):
        if component_id == "codec.topos":
            return self._entry
        return None


class TestPreflightRuntimeGate:
    def _job(self, codec: str, container: str = "mov"):
        from src.features.deliver.core.video_export_job import (
            VideoConfig,
            VideoExportJob,
        )

        return VideoExportJob(
            timeline_id="t1",
            output_path="/tmp/x.mov",
            video_config=VideoConfig(codec=codec, container=container),
        )

    def test_topos_blocked_when_codec_unavailable(self) -> None:
        from src.features.deliver.core.video_export_preflight import (
            PreflightContext,
            run_video_export_preflight,
        )

        job = self._job("topos_proxy")
        ctx = PreflightContext(
            runtime_snapshot=_SnapshotStub("UNAVAILABLE",
                                           "TOPOS_CODEC_LIB_NOT_FOUND"))
        result = run_video_export_preflight(job, ctx)
        assert any(i.code == "runtime.TOPOS_CODEC_LIB_NOT_FOUND"
                   for i in result.blocking)

    def test_h264_unaffected_by_topos_state(self) -> None:
        from src.features.deliver.core.video_export_preflight import (
            PreflightContext,
            run_video_export_preflight,
        )

        job = self._job("h264", container="mp4")
        ctx = PreflightContext(
            runtime_snapshot=_SnapshotStub("UNAVAILABLE",
                                           "TOPOS_CODEC_LIB_NOT_FOUND"))
        result = run_video_export_preflight(job, ctx)
        codes = [i.code for i in result.issues]
        assert not any(c.startswith("runtime.") for c in codes)

    def test_no_snapshot_keeps_legacy_behavior(self) -> None:
        from src.features.deliver.core.video_export_preflight import (
            PreflightContext,
            run_video_export_preflight,
        )

        job = self._job("topos_proxy")
        result = run_video_export_preflight(job, PreflightContext())
        codes = [i.code for i in result.issues]
        assert not any(c.startswith("runtime.") for c in codes)


# ---------------------------------------------------------------------------
# 模块级无 Qt/GL 依赖
# ---------------------------------------------------------------------------


class TestNoQtGlImport:
    def test_module_level_no_qt(self) -> None:
        import inspect

        import src.features.edit.ai_editing.runtime_capability_snapshot as mod

        for forbidden in ("QApplication", "QWidget", "moderngl"):
            assert not hasattr(mod, forbidden)
        for line in inspect.getsource(mod).splitlines():
            stripped = line.strip()
            if stripped.startswith(("import PyQt6", "from PyQt6",
                                    "import moderngl", "from moderngl",
                                    "import OpenGL", "from OpenGL")):
                pytest.fail(f"快照模块顶层依赖 Qt/GL: {stripped}")

    def test_snapshot_buildable_headless(self) -> None:
        """无 QApplication 环境下完整构建一次（冒烟）。"""
        snapshot = build_runtime_capability_snapshot(
            gpu_prober=_gpu_unavailable_prober())
        assert snapshot.entries
        states = {e.state for e in snapshot.entries}
        assert states <= set(RuntimeCapabilityState)
