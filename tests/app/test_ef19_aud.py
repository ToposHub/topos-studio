"""批 EF-19 修复回归：24 条 Suggestion 级审计发现的逐条验证。

每条 ID 的测试在该条修复被回退时必须失败（回退敏感性由开发流程中的
抽检保证，本文件断言均针对修复后的行为/结构特征）。

覆盖:
- src/app(根): AUD-3200 / AUD-3507 / AUD-3513 / AUD-42002 / AUD-42004
- src/features/composite/evaluators: AUD-57001 / AUD-57004 / AUD-5923 /
  AUD-6010 / AUD-6012
- src/features/teaching_animation/ui: AUD-30802 / AUD-30905 / AUD-30906 /
  AUD-31002 / AUD-31003
- src/shared/project: AUD-36809 / AUD-36817 / AUD-36922 / AUD-37010 /
  AUD-37012
- src/app/ui: AUD-03908 / AUD-03912 / AUD-4103 / AUD-4112
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import queue
import sys
import threading
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.app


# ══════════════════════════════ src/app（根） ══════════════════════════════


class TestAud3200FeatureFlagsCache:
    """AUD-3200: 兼容包装层零调用 + 每次调用重复重建 flags → lru_cache。"""

    def setup_method(self) -> None:
        from src.app import feature_flags as ff

        ff.get_enabled_features.cache_clear()
        ff.is_feature_enabled.cache_clear()

    def teardown_method(self) -> None:
        from src.app import feature_flags as ff

        ff.get_enabled_features.cache_clear()
        ff.is_feature_enabled.cache_clear()

    def test_get_enabled_features_reuses_loaded_flags(self, monkeypatch) -> None:
        from src.app import feature_flags as ff

        loader = Mock(return_value=SimpleNamespace(is_enabled=lambda f: True))
        monkeypatch.setattr(ff, "load_feature_flags", loader)

        first = ff.get_enabled_features()
        second = ff.get_enabled_features()

        assert first == second
        assert loader.call_count == 1  # 旧实现每次调用都重建 → 2 次

    def test_is_feature_enabled_uses_shared_cache(self, monkeypatch) -> None:
        from src.app import feature_flags as ff

        loader = Mock(return_value=SimpleNamespace(is_enabled=lambda f: f == "color"))
        monkeypatch.setattr(ff, "load_feature_flags", loader)

        assert ff.is_feature_enabled("color") is True
        assert ff.is_feature_enabled("edit") is False

    def test_legacy_alias_and_defaults_kept(self) -> None:
        from src.app import feature_flags as ff
        from src.shared.feature_flags import DEFAULT_FEATURE_FLAGS

        assert ff.DEFAULT_FEATURES is DEFAULT_FEATURE_FLAGS
        assert "color" in ff.get_enabled_features()


class TestAud3507StartupAbort:
    """AUD-3507: GPU/引擎/面板初始化失败无降级 → 明确报错后安全退出。"""

    def test_engine_init_failure_raises_typed_error_without_dialog(
            self, qapp, monkeypatch) -> None:
        from src.app import main_window as mw_mod
        from PyQt6.QtWidgets import QMessageBox

        critical = Mock()
        monkeypatch.setattr(QMessageBox, "critical", critical)
        monkeypatch.setattr(
            mw_mod, "Database",
            Mock(side_effect=OSError("gpu/db init failed")))

        with pytest.raises(RuntimeError, match="engine initialization"):
            mw_mod.MainWindow(test_mode=True)
        # test_mode 下跳过对话框，但失败必须以 RuntimeError 报告（旧实现
        # 直接把裸 OSError 沿 __init__ 抛出）
        critical.assert_not_called()

    def test_abort_startup_reports_and_rethrows(self, main_window, monkeypatch,
                                                caplog) -> None:
        from PyQt6.QtWidgets import QMessageBox

        mw = main_window
        critical = Mock()
        monkeypatch.setattr(QMessageBox, "critical", critical)
        monkeypatch.setattr(mw, "_test_mode", False)

        original = RuntimeError("panel blew up")
        with caplog.at_level("ERROR", logger="src.app.main_window"):
            with pytest.raises(RuntimeError, match="panels initialization"):
                mw._abort_startup("panels", original)

        critical.assert_called_once()
        assert any("panels" in rec.getMessage() or "Startup failed"
                   in rec.getMessage() for rec in caplog.records)


@pytest.fixture(scope="module")
def main_window(qapp):
    """模块级复用的轻量 MainWindow（test_mode 不建面板/不碰 GL）。"""
    from PyQt6.QtTest import QTest
    from src.app.main_window import MainWindow

    window = MainWindow(test_mode=True)
    yield window
    try:
        window.close()
    except Exception:
        pass
    window.deleteLater()
    QTest.qWait(50)


class TestAud3513AudioDiagnosticsOffUiThread:
    """AUD-3513: 音频后端诊断移入后台线程，经信号回 UI 线程。"""

    def _patch_probe(self, monkeypatch, caps: Any = None,
                     error: Exception | None = None):
        import src.features.edit.core.audio_capabilities as caps_mod

        monkeypatch.setattr(caps_mod, "reset_audio_capabilities_cache", Mock())
        probe_threads: list[str] = []

        def fake_probe(check_output_device: bool = False):
            probe_threads.append(threading.current_thread().name)
            if error is not None:
                raise error
            return caps

        monkeypatch.setattr(caps_mod, "get_audio_capabilities", fake_probe)
        return probe_threads

    def _wait_until(self, cond, timeout_ms: float = 5000.0) -> bool:
        from PyQt6.QtTest import QTest
        import time

        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            QTest.qWait(20)
            if cond():
                return True
        return False

    def test_probe_runs_off_main_thread_and_applies_on_ui_thread(
            self, main_window, monkeypatch) -> None:
        from PyQt6.QtTest import QTest

        caps = SimpleNamespace(disabled_components=lambda: [])
        probe_threads = self._patch_probe(monkeypatch, caps=caps)
        mw = main_window
        mw.__dict__.pop("_audio_capabilities", None)
        mw.status_bar = MagicMock(name="status_bar")

        mw._run_audio_backend_diagnostics()

        assert self._wait_until(
            lambda: getattr(mw, "_audio_capabilities", None) is caps)

        assert probe_threads, "probe never ran"
        assert all(name != "MainThread" for name in probe_threads)
        # 旧实现在调用返回前同步完成；新实现探测在后台线程进行
        QTest.qWait(50)
        mw.status_bar.addPermanentWidget.assert_not_called()

    def test_degraded_snapshot_shows_warning_on_ui_thread(
            self, main_window, monkeypatch) -> None:
        caps = SimpleNamespace(disabled_components=lambda: ["pyav"])
        probe_threads = self._patch_probe(monkeypatch, caps=caps)
        mw = main_window
        mw.status_bar = MagicMock(name="status_bar")

        mw._run_audio_backend_diagnostics()

        assert self._wait_until(
            lambda: mw.status_bar.addPermanentWidget.call_count == 1)
        assert all(name != "MainThread" for name in probe_threads)
        label = mw.status_bar.addPermanentWidget.call_args[0][0]
        assert mw._audio_backend_warning_label is label
        label.deleteLater()
        mw.__dict__.pop("_audio_backend_warning_label", None)
        mw.__dict__.pop("_audio_capabilities", None)

    def test_probe_failure_never_blocks_or_applies(
            self, main_window, monkeypatch) -> None:
        from PyQt6.QtTest import QTest

        probe_threads = self._patch_probe(
            monkeypatch, error=RuntimeError("device gone"))
        mw = main_window
        mw.__dict__.pop("_audio_capabilities", None)
        mw.status_bar = MagicMock(name="status_bar")

        mw._run_audio_backend_diagnostics()  # 不得抛出
        QTest.qWait(120)

        assert all(name != "MainThread" for name in probe_threads)
        assert getattr(mw, "_audio_capabilities", None) is None
        mw.status_bar.addPermanentWidget.assert_not_called()
        mw.__dict__.pop("_audio_capabilities", None)


class TestAud42002VersionFallback:
    """AUD-42002: 版本兜底字面量 → 多发行名元数据 + 字面量兜底告警留痕。"""

    def test_metadata_falls_through_to_second_dist_name(self, monkeypatch) -> None:
        import importlib.metadata as md
        from src.app import update_checker as uc

        def fake_version(dist: str) -> str:
            if dist == "topos":
                raise md.PackageNotFoundError(dist)
            if dist == "topos-color":
                return "7.7.7"
            raise md.PackageNotFoundError(dist)

        monkeypatch.setattr(md, "version", fake_version)
        assert uc._version_from_metadata() == "7.7.7"

    def test_metadata_none_when_all_names_missing(self, monkeypatch) -> None:
        import importlib.metadata as md
        from src.app import update_checker as uc

        monkeypatch.setattr(
            md, "version",
            Mock(side_effect=md.PackageNotFoundError("topos")))
        assert uc._version_from_metadata() is None

    def test_literal_fallback_logs_warning(self, monkeypatch, caplog) -> None:
        import importlib.metadata as md
        from src.app import update_checker as uc

        monkeypatch.setattr(
            md, "version",
            Mock(side_effect=md.PackageNotFoundError("topos")))

        class _FakePath:
            def __init__(self, *parts: str) -> None:
                self._parts = parts

            def resolve(self) -> "_FakePath":
                return self

            def __truediv__(self, other: str) -> "_FakePath":
                return _FakePath(*(self._parts + (other,)))

            @property
            def parents(self) -> tuple:
                return (_FakePath("a"), _FakePath("b"), _FakePath("c"))

        monkeypatch.setattr(uc, "Path", _FakePath)

        with caplog.at_level("WARNING", logger="src.app.update_checker"):
            version = uc._resolve_version()

        assert version == uc._FALLBACK_VERSION
        assert any("fallback version" in rec.getMessage()
                   for rec in caplog.records)  # 旧实现静默返回 → 无记录


class TestAud42004GiteeEndpointDocs:
    """AUD-42004: 注释/文档写 GitHub，实际端点为 Gitee。"""

    def test_release_comment_no_longer_claims_github(self) -> None:
        import pathlib

        path = pathlib.Path(
            importlib.import_module("src.app.update_checker").__file__)
        source = path.read_text(encoding="utf-8")
        assert "GitHub API endpoint" not in source
        assert "Gitee API v5" in source

    def test_update_checker_docstring_mentions_gitee(self) -> None:
        from src.app.update_checker import RELEASES_URL, UpdateChecker

        assert "Gitee" in (UpdateChecker.__doc__ or "")
        assert RELEASES_URL.startswith("https://gitee.com/")


# ═══════════════════ src/features/composite/evaluators ═══════════════════


class TestAud57001and57004DocPaths:
    """AUD-57001/57004: docstring 引用不存在的 src/composite/ 路径。"""

    def test_package_docstring_points_at_real_mixin_file(self) -> None:
        from src.features.composite import evaluators

        doc = (evaluators.__doc__ or "")
        assert "src/composite/graph_evaluator_mixin.py" not in doc
        assert ("src/features/composite/evaluators/graph_evaluator_mixin.py"
                in doc)
        # 文档指向的文件真实存在
        import pathlib
        assert pathlib.Path("src/features/composite/evaluators/"
                            "graph_evaluator_mixin.py").exists()

    def test_ai_evaluator_docstring_points_at_real_manager_file(self) -> None:
        from src.features.composite.evaluators.ai_evaluator import AIEvaluatorMixin

        doc = (AIEvaluatorMixin.__doc__ or "")
        assert "src/composite/ai_compute_manager.py" not in doc
        assert "src/features/composite/render/ai_compute_manager.py" in doc
        import pathlib
        assert pathlib.Path(
            "src/features/composite/render/ai_compute_manager.py").exists()


from src.features.composite.evaluators.cache_evaluator import CacheEvaluatorMixin


class _DiskCacheHost(CacheEvaluatorMixin):
    """CacheEvaluatorMixin 的最小宿主：只提供 readback 桩。"""

    def __init__(self, rgba: Any) -> None:
        self._rgba = rgba

    def _readback_result_array(self, tex_result: Any) -> Any:
        return self._rgba


class TestAud5923AsyncDiskCacheStore:
    """AUD-5923: 磁盘缓存 npz 压缩/写盘不再阻塞渲染线程。"""

    def test_store_runs_on_background_writer_thread(self, monkeypatch) -> None:
        import numpy as np
        import src.features.composite.evaluators.cache_evaluator as ce
        from src.features.composite.nodes.base_node import TextureResult

        store_calls: list[tuple[str, threading.Thread]] = []

        class _Store:
            def store(self, key: str, rgba: Any, metadata: Any) -> bool:
                store_calls.append(
                    (key, threading.current_thread()))
                return True

        monkeypatch.setattr(
            "src.features.composite.render.disk_cache_store."
            "get_disk_cache_store", lambda: _Store())

        host = _DiskCacheHost(np.zeros((4, 4, 4), dtype=np.float32))
        ns = SimpleNamespace(node_id="n1", node_type="blur")
        outputs = {"result": TextureResult(texture_id=1)}

        host._store_disk_cache_outputs(ns, "key1", outputs)

        deadline = threading.Event()
        for _ in range(250):  # ≤5s
            if store_calls:
                break
            deadline.wait(0.02)
        assert store_calls, "store never ran"
        key, thread = store_calls[0]
        assert key == "nkey1"  # _disk_cache_key 前缀
        assert thread.name != "MainThread"
        assert thread.name == "disk-cache-store-writer"

    def test_full_queue_skips_store_without_error(self, monkeypatch) -> None:
        import numpy as np
        import src.features.composite.evaluators.cache_evaluator as ce

        monkeypatch.setattr(ce, "_disk_store_queue",
                            queue.Queue(maxsize=1))
        monkeypatch.setattr(ce, "_ensure_disk_store_worker", Mock())
        store = Mock()
        monkeypatch.setattr(
            "src.features.composite.render.disk_cache_store."
            "get_disk_cache_store", lambda: store)

        ce._disk_store_queue.put(("pending", None, {}))
        from src.features.composite.nodes.base_node import TextureResult
        host = _DiskCacheHost(np.zeros((2, 2, 4), dtype=np.float32))
        ns = SimpleNamespace(node_id="n2", node_type="blur")

        host._store_disk_cache_outputs(
            ns, "k", {"r": TextureResult(texture_id=3)})

        store.assert_not_called()  # 写手忙 → 跳过而非阻塞/堆积
        ce._disk_store_queue.get_nowait()  # 清理

    def test_queue_is_bounded(self) -> None:
        import src.features.composite.evaluators.cache_evaluator as ce

        assert ce._DISK_STORE_QUEUE_MAXSIZE == 2
        assert ce._disk_store_queue.maxsize == ce._DISK_STORE_QUEUE_MAXSIZE


def _make_histogram_manager() -> Any:
    """HistogramManager 稀疏实例：只挂 `_downsample` 直建路径需要的属性。"""
    from src.features.composite.evaluators.histogram_manager import HistogramManager

    mgr = HistogramManager.__new__(HistogramManager)
    mgr._ctx = MagicMock(name="ctx")
    mgr._downsample_prog = MagicMock(name="downsample_prog")
    mgr._get_quad_vao = Mock(return_value=MagicMock(name="quad_vao"))
    return mgr


class TestAud6010DirectPathGpuLeaks:
    """AUD-6010: 无 resource_pool/fb_manager 直建路径泄漏纹理与 FBO。"""

    def test_direct_downsample_releases_local_fbo_not_texture(self) -> None:
        mgr = _make_histogram_manager()
        ds_tex = mgr._ctx.texture.return_value

        result = mgr._downsample(
            Mock(name="input"), 64, 64, 32, 32,
            fb_manager=None, resource_pool=None)

        assert result is ds_tex
        fbo = mgr._ctx.framebuffer.return_value
        fbo.release.assert_called_once()  # 直建 FBO 用毕就地释放
        ds_tex.release.assert_not_called()  # 纹理归调用方释放

    def test_pooled_fbo_not_released_here(self) -> None:
        mgr = _make_histogram_manager()
        fb_manager = MagicMock(name="fb_manager")

        mgr._downsample(
            Mock(name="input"), 64, 64, 32, 32,
            fb_manager=fb_manager, resource_pool=None)

        mgr._ctx.framebuffer.assert_not_called()
        fb_manager.get_fbo.return_value.release.assert_not_called()

    def test_gl_compute_path_releases_direct_texture(self) -> None:
        import numpy as np
        from src.features.composite.evaluators.histogram_manager import (
            HISTOGRAM_TOTAL_BINS, HistogramManager)

        mgr = HistogramManager.__new__(HistogramManager)
        import threading
        mgr._lock = threading.RLock()
        mgr._ensure_programs = Mock()
        mgr._ensure_buffers = Mock()
        mgr._available = True
        mgr._metal_path = None
        mgr._compute_prog = MagicMock(name="compute_prog")
        mgr._histogram_buffer = MagicMock(name="hist_buf")
        mgr._histogram_buffer.read.return_value = bytes(HISTOGRAM_TOTAL_BINS * 4)
        mgr._counter_buffer = MagicMock(name="counter_buf")
        mgr._counter_buffer.read.return_value = b"\x00\x00\x00\x00"
        mgr._clear_buffers = Mock()
        direct_tex = MagicMock(name="direct_ds_tex")
        mgr._downsample = Mock(return_value=direct_tex)
        mgr._compute_cdf = Mock(return_value=np.zeros((4, 256), dtype=np.float32))

        result = mgr.compute_histogram(
            Mock(name="input", glo=7, revision=0), 4096, 4096,
            channel_mask=15, fb_manager=None, resource_pool=None)

        assert result.total_pixels == 0
        direct_tex.release.assert_called_once()  # 旧实现跳过 → 泄漏

    def test_metal_path_releases_direct_texture(self) -> None:
        import numpy as np
        from src.features.composite.evaluators.histogram_manager import (
            HistogramManager)

        mgr = HistogramManager.__new__(HistogramManager)
        metal = MagicMock(name="metal_path")
        metal.compute.return_value = (
            np.zeros(1024, dtype="<u4"),
            np.zeros((4, 256), dtype="<f4"),
            4, np.zeros(8, dtype="<u4"),
        )
        mgr._metal_path = metal
        direct_tex = MagicMock(name="direct_ds_tex")
        # 4096 → 降采样到 512×512 RGBA16F
        direct_tex.read.return_value = bytes(512 * 512 * 4 * 2)
        mgr._downsample = Mock(return_value=direct_tex)

        mgr._compute_histogram_metal(
            Mock(name="input"), 4096, 4096, 15, None, None, 0.0, 1.0, 0)

        direct_tex.release.assert_called_once()


class TestAud6012QuadVboAndStats:
    """AUD-6012: release() 未释放 quad VBO；get_stats 无锁读缓存内部。"""

    def _mgr_with_quad(self) -> Any:
        from src.features.composite.evaluators.histogram_manager import (
            HistogramCache, HistogramManager)

        mgr = HistogramManager.__new__(HistogramManager)
        mgr._ctx = MagicMock(name="ctx")
        mgr._downsample_prog = MagicMock(name="prog")
        return mgr

    def test_quad_vbo_tracked_and_released(self) -> None:
        from src.features.composite.evaluators.histogram_manager import (
            HistogramCache, HistogramManager)
        import threading

        mgr = self._mgr_with_quad()
        vao = mgr._get_quad_vao()
        vbo = mgr._quad_vbo
        assert vbo is mgr._ctx.buffer.return_value

        mgr._histogram_buffer = None
        mgr._counter_buffer = None
        mgr._compute_prog = None
        mgr._downsample_prog = None
        mgr._cache = HistogramCache()
        mgr._lock = threading.RLock()
        mgr.release()

        vao.release.assert_called_once()
        vbo.release.assert_called_once()  # 旧实现泄漏 VBO
        assert mgr._quad_vbo is None

    def test_histogram_cache_len_is_lock_respecting(self) -> None:
        from src.features.composite.evaluators.histogram_manager import (
            HistogramCache, HistogramResult)

        cache = HistogramCache()
        for i in range(3):
            cache.put(f"k{i}", HistogramResult(
                histogram=None, cdf=None, total_pixels=i,
                channel_mask=15, texture_revision=0))
        assert len(cache) == 3
        cache.clear()
        assert len(cache) == 0

    def test_get_stats_uses_public_len_not_private_dict(self) -> None:
        from src.features.composite.evaluators.histogram_manager import (
            HistogramManager)
        import threading

        mgr = HistogramManager.__new__(HistogramManager)
        mgr._available = True
        mgr._metal_path = None
        mgr.is_available = Mock(return_value=True)
        mgr.get_diagnostic = Mock(return_value="")

        class _LenOnlyCache:
            """只暴露 __len__：穿透私有 _cache 的旧实现会 AttributeError。"""

            def __len__(self) -> int:
                return 5

        mgr._cache = _LenOnlyCache()
        mgr._lock = threading.RLock()

        stats = mgr.get_stats()
        assert stats["cache_entries"] == 5


# ═══════════════════ src/features/teaching_animation/ui ═══════════════════


def _mg_scene(duration: int = 48) -> Any:
    from src.features.teaching_animation.core.model import (
        ScenePage, TeachingSceneDocument)

    scene = replace(
        TeachingSceneDocument.create_default("ef19", duration_frames=duration),
        elements=(),
        pages=(ScenePage("p1", duration_frames=duration // 2),
               ScenePage("p2", duration_frames=duration - duration // 2)),
    )
    return scene


class TestAud30802RowIndexMap:
    """AUD-30802: 行索引/行查找循环内线性扫描 → O(1) row_id 索引。"""

    def _panel_with_fake_rows(self, qapp, monkeypatch, row_ids: list[str]):
        from src.features.teaching_animation.ui.mg_keyframes_panel import (
            MgKeyframesPanel)

        rows = [
            SimpleNamespace(row_id=rid, kind="property", points=(),
                            element_id="e1", editable=True, selected=False)
            for rid in row_ids
        ]
        monkeypatch.setattr(
            "src.features.teaching_animation.ui.mg_keyframes_panel."
            "build_keyframe_rows", lambda *a, **k: rows)
        panel = MgKeyframesPanel()
        panel.set_scene(_mg_scene())
        return panel, rows

    def test_rebuild_builds_row_index_map(self, qapp, monkeypatch) -> None:
        panel, rows = self._panel_with_fake_rows(
            qapp, monkeypatch, ["r0", "r1", "r2"])
        try:
            assert panel._row_index_by_id == {"r0": 0, "r1": 1, "r2": 2}
        finally:
            panel.deleteLater()

    def test_row_by_id_is_constant_time(self, qapp, monkeypatch) -> None:
        class _CountingList(list):
            iter_count = 0

            def __iter__(self):
                _CountingList.iter_count += 1
                return super().__iter__()

        panel, rows = self._panel_with_fake_rows(
            qapp, monkeypatch, ["a", "b", "c"])
        try:
            counting = _CountingList(rows)
            panel._rows = counting
            _CountingList.iter_count = 0
            for _ in range(3):
                assert panel._row_by_id("b") is rows[1]
            assert _CountingList.iter_count == 0  # 旧实现逐次线性扫 → 3
        finally:
            panel.deleteLater()

    def test_row_by_id_unknown_returns_none(self, qapp, monkeypatch) -> None:
        panel, _rows = self._panel_with_fake_rows(qapp, monkeypatch, ["a"])
        try:
            assert panel._row_by_id("missing") is None
        finally:
            panel.deleteLater()

    def test_hit_keyframe_computes_row_center_once(self, qapp, monkeypatch) -> None:
        points = [SimpleNamespace(frame=f, selected=False) for f in (0, 10, 20)]
        panel, rows = self._panel_with_fake_rows(
            qapp, monkeypatch, ["only"])
        try:
            row = rows[0]
            row.points = points
            panel._mapper = SimpleNamespace(frame_to_x=lambda f: 0.0)
            tracks = panel._tracks
            center_calls = []
            original_center = tracks._row_center

            def counting_center(r):
                center_calls.append(r)
                return original_center(r)

            monkeypatch.setattr(tracks, "_row_center", counting_center)
            y = original_center(row)
            hit = tracks._hit_keyframe(row, 0.0, y)
            assert hit == 0  # 命中第一个关键帧
            assert len(center_calls) == 1  # 旧实现每点重算 → 3
        finally:
            panel.deleteLater()


class TestAud30905PendingRequestCleared:
    """AUD-30905: 直启新请求前不清 pending → 陈旧请求被重复启动。"""

    def _window(self):
        from src.features.teaching_animation.ui.motion_graphic_editor_window import (
            MotionGraphicEditorWindow)

        w = MotionGraphicEditorWindow.__new__(MotionGraphicEditorWindow)
        w._html_preview_generation = 1
        w._html_preview_pending = ("stale", 0, 0)
        w._html_preview_thread = SimpleNamespace(
            isRunning=lambda: False, deleteLater=Mock())
        w._start_html_preview = Mock()
        return w

    def test_stale_pending_cleared_before_direct_start(self) -> None:
        w = self._window()
        scene = SimpleNamespace(html_layer=object())

        w._request_html_preview(scene, 3)

        w._start_html_preview.assert_called_once_with(scene, 3, 2)
        assert w._html_preview_pending is None  # 旧实现保留陈旧 pending

    def test_running_thread_branch_still_parks_new_request(self) -> None:
        w = self._window()
        w._html_preview_thread = SimpleNamespace(
            isRunning=lambda: True,
            requestInterruption=Mock(),
            deleteLater=Mock())
        w.statusBar = Mock()
        scene = SimpleNamespace(html_layer=object())

        w._request_html_preview(scene, 5)

        w._start_html_preview.assert_not_called()
        assert w._html_preview_pending == (scene, 5, 2)


class TestAud30906StopWaitBounded:
    """AUD-30906: _stop_html_preview 在 UI 线程无限期 wait()。"""

    def _window(self, wait_return: bool):
        from src.features.teaching_animation.ui.motion_graphic_editor_window import (
            MotionGraphicEditorWindow)

        w = MotionGraphicEditorWindow.__new__(MotionGraphicEditorWindow)
        w._html_preview_generation = 1
        w._html_preview_closing = False
        w._html_preview_pending = ("s", 0, 0)
        thread = SimpleNamespace(
            isRunning=lambda: True,
            requestInterruption=Mock(),
            wait=Mock(return_value=wait_return),
            deleteLater=Mock())
        w._html_preview_thread = thread
        return w, thread

    def test_wait_gets_timeout_and_timeout_is_logged(self, caplog) -> None:
        from src.features.teaching_animation.ui.motion_graphic_editor_window import (
            _HTML_PREVIEW_STOP_TIMEOUT_MS)

        w, thread = self._window(wait_return=False)
        with caplog.at_level("WARNING",
                             logger="src.features.teaching_animation.ui."
                                    "motion_graphic_editor_window"):
            w._stop_html_preview()

        thread.wait.assert_called_once()
        assert thread.wait.call_args.args == (_HTML_PREVIEW_STOP_TIMEOUT_MS,)
        assert thread.wait.call_args.args[0] > 0
        assert any("still running" in rec.getMessage()
                   for rec in caplog.records)
        thread.deleteLater.assert_called_once()

    def test_clean_stop_logs_nothing(self, caplog) -> None:
        w, thread = self._window(wait_return=True)
        with caplog.at_level("WARNING"):
            w._stop_html_preview()
        assert thread.wait.call_args.args[0] > 0
        assert not any("still running" in rec.getMessage()
                       for rec in caplog.records)


class TestAud31002DeadUpdatingFlag:
    """AUD-31002: `_updating` 状态锁只写不读 → 删除并注释原因。"""

    def test_flag_removed_and_set_frame_still_works(self, qapp) -> None:
        from src.features.teaching_animation.ui.page_navigation import (
            PageNavigationBar)

        bar = PageNavigationBar()
        scene = _mg_scene()
        bar.set_scene(scene, 0)
        try:
            assert "_updating" not in bar.__dict__
            assert not hasattr(bar, "_updating")  # 旧实现存在该属性

            frame_spies: list[int] = []
            bar.frameRequested.connect(frame_spies.append)

            bar.set_frame(30)  # 落在第二页
            states = [b.isChecked() for b in bar._buttons]
            assert states == [False, True]
            assert frame_spies == []  # set_frame 不回触发页面跳转
        finally:
            bar.deleteLater()

    def test_set_frame_out_of_range_buttons_unchecked(self, qapp) -> None:
        from src.features.teaching_animation.ui.page_navigation import (
            PageNavigationBar)

        bar = PageNavigationBar()
        bar.set_scene(_mg_scene(), 0)
        try:
            bar.set_frame(999)  # 尾帧由最后一页吸收（page_index_for_frame 语义）
            assert [b.isChecked() for b in bar._buttons] == [False, True]
        finally:
            bar.deleteLater()


class TestAud31003LinkSliderBlockSignals:
    """AUD-31003: _link_slider 双向互连 → blockSignals 成对包裹防回声。"""

    def _linked(self, qapp):
        from PyQt6.QtCore import Qt
        from src.features.teaching_animation.ui.page_timing_panel import (
            PageTimingPanel)
        from src.shared.ui.widgets.drag_value import DragValue
        from src.shared.ui.widgets.topos_controls import ToposClickableSlider

        slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 1000)
        value = DragValue("", 0.0, 100.0, 0.0)
        slider_spies: list[int] = []
        value_spies: list[float] = []
        slider.valueChanged.connect(slider_spies.append)
        value.valueChanged.connect(value_spies.append)
        PageTimingPanel._link_slider(slider, value, 10.0)
        return slider, value, slider_spies, value_spies

    def test_slider_drive_cuts_echo_at_source(self, qapp) -> None:
        slider, value, slider_spies, value_spies = self._linked(qapp)

        slider.setValue(500)

        assert value.value() == pytest.approx(50.0)
        assert slider.value() == 500
        # 旧实现：value.valueChanged 回声外泄 1 次；新实现信号在源头被屏蔽
        assert value_spies == []
        assert slider_spies == [500]
        assert not slider.signalsBlocked()
        assert not value.signalsBlocked()

    def test_value_drive_cuts_echo_at_source(self, qapp) -> None:
        slider, value, slider_spies, value_spies = self._linked(qapp)

        value.setValue(7.0)

        assert slider.value() == 70
        # 旧实现：slider.valueChanged 回声外泄 1 次
        assert slider_spies == []
        assert value_spies == [pytest.approx(7.0)]
        assert not slider.signalsBlocked()
        assert not value.signalsBlocked()

    def test_repeated_drives_terminate_without_pong_pong(self, qapp) -> None:
        slider, value, slider_spies, value_spies = self._linked(qapp)
        for pos in (100, 200, 300):
            slider.setValue(pos)
            assert len(slider_spies) <= 1  # 每次用户驱动最多一条自身信号
            slider_spies.clear()
            value_spies.clear()


# ═══════════════════════════ src/shared/project ═══════════════════════════


class TestAud36809RestoreBackupTempLeak:
    """AUD-36809: restore_project_backup 仅捕获 OSError → BadZipFile 泄漏临时文件。"""

    def _make_backup(self, backup_path) -> None:
        with zipfile.ZipFile(backup_path, "w") as zf:
            zf.writestr("manifest.json", '{"project_id": "p1"}')

    def test_bad_zip_leaves_no_temp_file(self, tmp_path, monkeypatch,
                                         caplog) -> None:
        from src.shared.project import library

        project = tmp_path / "my.topos"
        project.write_bytes(b"old project")
        backup = tmp_path / "backups" / "my_20260101.topos"
        backup.parent.mkdir()
        self._make_backup(backup)

        def corrupt_rewrite(backup_path, temporary_name, name=None):
            raise zipfile.BadZipFile("truncated after precheck")

        monkeypatch.setattr(library, "_rewrite_project_archive",
                            corrupt_rewrite)
        with caplog.at_level("ERROR", logger="src.shared.project.library"):
            ok = library.restore_project_backup(project, backup)

        assert ok is False
        leftovers = list(tmp_path.glob(".my.restore.*"))
        assert leftovers == []  # 旧实现 BadZipFile 穿透 + 临时文件残留
        assert any("Could not restore project backup" in rec.getMessage()
                   for rec in caplog.records)

    def test_oserror_path_still_cleans_up(self, tmp_path, monkeypatch) -> None:
        from src.shared.project import library

        project = tmp_path / "my.topos"
        project.write_bytes(b"old")
        backup = tmp_path / "b.topos"
        self._make_backup(backup)

        monkeypatch.setattr(
            library, "_rewrite_project_archive",
            Mock(side_effect=OSError("disk full")))
        assert library.restore_project_backup(project, backup) is False
        assert list(tmp_path.glob(".my.restore.*")) == []

    def test_successful_restore_replaces_project(self, tmp_path,
                                                 monkeypatch) -> None:
        import shutil
        from src.shared.project import library

        project = tmp_path / "my.topos"
        project.write_bytes(b"old")
        backup = tmp_path / "b.topos"
        self._make_backup(backup)

        monkeypatch.setattr(
            library, "_rewrite_project_archive",
            lambda b, t, name=None: shutil.copyfile(b, t))
        assert library.restore_project_backup(project, backup) is True
        assert project.read_bytes() != b"old"


class TestAud36817AutoSearchBudget:
    """AUD-36817: auto_search_for_missing 家目录递归扫描 → 预算 + 取消点。"""

    def _patch_assets(self, monkeypatch, search_root, paths: list[str]) -> None:
        from src.shared.project.offline_asset_manager import OfflineAssetManager

        assets = [SimpleNamespace(path=p, asset_id=f"a{i}")
                  for i, p in enumerate(paths)]
        monkeypatch.setattr(OfflineAssetManager, "get_offline_asset_list",
                            staticmethod(lambda project: assets))
        monkeypatch.setattr(
            OfflineAssetManager, "_suggest_search_dirs",
            staticmethod(lambda paths_: [str(search_root)]))

    def test_budget_zero_stops_deep_scan(self, tmp_path, monkeypatch) -> None:
        deep = tmp_path / "lvl1" / "lvl2"
        deep.mkdir(parents=True)
        (deep / "missing.mp4").write_bytes(b"x")
        self._patch_assets(monkeypatch, tmp_path, [str(deep / "missing.mp4")])

        from src.shared.project.offline_asset_manager import OfflineAssetManager

        found = OfflineAssetManager.auto_search_for_missing(
            object(), budget_seconds=0.0)
        assert found == {}  # 预算 0 → 不深扫

        found_full = OfflineAssetManager.auto_search_for_missing(object())
        assert found_full == {"a0": str(deep / "missing.mp4")}  # 无预算照常找到

    def test_budget_still_finds_direct_hits(self, tmp_path,
                                            monkeypatch) -> None:
        (tmp_path / "top.mp4").write_bytes(b"x")
        self._patch_assets(monkeypatch, tmp_path, [str(tmp_path / "top.mp4")])

        from src.shared.project.offline_asset_manager import OfflineAssetManager

        found = OfflineAssetManager.auto_search_for_missing(
            object(), budget_seconds=0.0)
        # 顶层直接命中在递归（及预算检查）前完成，不受影响
        assert found == {"a0": str(tmp_path / "top.mp4")}

    def test_cancelled_callback_stops_search(self, tmp_path,
                                             monkeypatch) -> None:
        (tmp_path / "a.mp4").write_bytes(b"x")
        self._patch_assets(monkeypatch, tmp_path, [str(tmp_path / "a.mp4")])

        from src.shared.project.offline_asset_manager import OfflineAssetManager

        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return True

        found = OfflineAssetManager.auto_search_for_missing(
            object(), cancelled=cancel)
        assert found == {}
        assert calls["n"] >= 1

    def test_no_budget_kwargs_keeps_old_behavior(self, tmp_path,
                                                 monkeypatch) -> None:
        (tmp_path / "f.mp4").write_bytes(b"x")
        self._patch_assets(monkeypatch, tmp_path, [str(tmp_path / "f.mp4")])

        from src.shared.project.offline_asset_manager import OfflineAssetManager

        # 不传新参数时与旧签名完全兼容
        assert OfflineAssetManager.auto_search_for_missing(object()) == {
            "a0": str(tmp_path / "f.mp4")}


class TestAud36922BackupProjectDeadCode:
    """AUD-36922: backup_project 恒假条件 + 重复路径解析。"""

    def _router(self, tmp_path) -> Any:
        from src.shared.project.save_router import SaveRouter

        real_path = tmp_path / "real.topos"
        real_path.write_bytes(b"project")
        missing_path = tmp_path / "missing.topos"
        router = SaveRouter.__new__(SaveRouter)
        router._real_path = str(real_path)
        router._missing_path = str(missing_path)
        router._callback_counts = {"get_current_file_path": 0}

        def call_callback(name, *args, **kwargs):
            if name == "get_current_file_path":
                router._callback_counts[name] += 1
                return str(missing_path)
            return None

        router._call_callback = call_callback
        # 默认 context 解析为空 → _resolve 走 current_file_path 推导
        router._get_project_path_for_context = Mock(return_value="")
        router._get_project_path_for_file = (
            lambda p: str(missing_path) if p else None)
        router._resolve_backup_source_path = (
            SaveRouter._resolve_backup_source_path.__get__(router))
        router.save = Mock(return_value=True)
        router._write_backup_copy = Mock(return_value=True)
        return router

    def test_duplicate_path_resolution_removed(self, tmp_path) -> None:
        router = self._router(tmp_path)
        # context 直接给出已存在的工程路径 → 全程无需兜底
        router._get_project_path_for_context = Mock(
            return_value=router._real_path)

        assert router.backup_project() is True
        router._write_backup_copy.assert_called_once_with(
            router._real_path, "", 5)
        # 旧实现函数体顶部又直接调用一次 get_current_file_path → 2 次
        assert router._callback_counts["get_current_file_path"] == 1
        router.save.assert_not_called()

    def test_no_path_returns_false_without_save(self, tmp_path) -> None:
        router = self._router(tmp_path)
        router._get_project_path_for_file = lambda p: None

        assert router.backup_project() is False
        router.save.assert_not_called()
        router._write_backup_copy.assert_not_called()

    def test_save_fallback_resolves_current_path_once_more(self, tmp_path) -> None:
        router = self._router(tmp_path)
        # 兜底分支内按需补读一次 current_file_path；context 第二次返回真实路径
        router._get_project_path_for_context = Mock(
            side_effect=["", router._real_path])

        assert router.backup_project() is True
        router.save.assert_called_once()
        # 新实现共 2 次（_resolve 内部 1 次 + 兜底分支 1 次）；旧实现 3 次
        assert router._callback_counts["get_current_file_path"] == 2
        router._write_backup_copy.assert_called_once_with(
            router._real_path, "", 5)


class TestAud37010AutoSaveDoubleSave:
    """AUD-37010: auto_save_triggered 与 save() 不再双发。"""

    def _controller(self, qapp, save_result: bool = True):
        from src.shared.project.topos_project_controller import PageType
        from src.shared.project.topos_project_controller import (
            ToposProjectController)

        controller = ToposProjectController()
        callback = Mock(return_value=save_result)
        controller.set_save_callback(callback)
        controller.mark_dirty(PageType.COLOR, "ef19")
        return controller, callback

    def test_no_consumer_falls_back_to_direct_save(self, qapp) -> None:
        controller, callback = self._controller(qapp)

        controller._on_auto_save_timer()

        callback.assert_called_once()  # 无人监听信号 → 兜底保存不失效

    def test_consumer_connected_means_signal_only(self, qapp) -> None:
        controller, callback = self._controller(qapp)
        handler = Mock()
        controller.auto_save_triggered.connect(handler)

        controller._on_auto_save_timer()

        handler.assert_called_once()
        callback.assert_not_called()  # 旧实现：信号 + save() 双重保存

    def test_not_dirty_skips_entirely(self, qapp) -> None:
        controller, callback = self._controller(qapp)
        controller.clear_all_dirty(reason="test")

        controller._on_auto_save_timer()

        callback.assert_not_called()


class TestAud37012SavePageDeadCode:
    """AUD-37012: save_page 成功分支空操作死代码 + 文档语义不符。"""

    def test_no_pass_statement_in_save_page(self) -> None:
        from src.shared.project.topos_project_controller import (
            ToposProjectController)

        source = inspect.getsource(ToposProjectController.save_page)
        tree = ast.parse("class _C:\n" + inspect.getsource(
            ToposProjectController.save_page))
        pass_nodes = [node for node in ast.walk(tree)
                      if isinstance(node, ast.Pass)]
        assert pass_nodes == []  # 旧实现 `if success: pass` 死代码
        assert "success = self.save()" not in source

    def test_docstring_states_full_save_contract(self) -> None:
        from src.shared.project.topos_project_controller import (
            ToposProjectController)

        doc = ToposProjectController.save_page.__doc__ or ""
        assert "AUD-37012" in doc
        # 旧文档承诺"清除该页面的 dirty 状态"，与实现（全量清除）不符
        assert "清除该页面的 dirty 状态" not in doc

    def test_save_page_short_circuit_and_full_clear(self, qapp) -> None:
        from src.shared.project.topos_project_controller import PageType
        from src.shared.project.topos_project_controller import (
            ToposProjectController)

        controller = ToposProjectController()
        callback = Mock(return_value=True)
        controller.set_save_callback(callback)

        # 页面不 dirty → 短路，不触发保存回调
        assert controller.save_page(PageType.COLOR) is True
        callback.assert_not_called()

        # dirty 页面保存成功后清除*所有*页面 dirty（全量保存契约）
        controller.mark_dirty(PageType.COLOR, "a")
        controller.mark_dirty(PageType.EDIT, "b")
        assert controller.save_page(PageType.COLOR) is True
        assert controller.get_dirty_pages() == []


# ══════════════════════════════ src/app/ui ════════════════════════════════


class _PrefsStubConfig:
    """PreferencesDialog 的 ConfigManager 替身（不落盘）。"""

    def __init__(self, initial: dict | None = None) -> None:
        from src.shared.config.config_manager import ConfigManager
        self.DEFAULT_CONFIG = ConfigManager.DEFAULT_CONFIG
        self.config = {**self.DEFAULT_CONFIG, **(initial or {})}

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value, auto_save=True):
        self.config[key] = value

    def save_config(self):
        pass


@pytest.fixture(scope="module")
def prefs_dialog(qapp):
    """模块级复用的 PreferencesDialog（真实页面树，无模块页）。"""
    import time
    from PyQt6.QtTest import QTest

    dialog = PreferencesDialog(_PrefsStubConfig())
    # 构造期 _load_current_settings 已触发一次真实缓存目录扫描；等它结束，
    # 避免其 in-flight 标志吞掉用例自己触发的扫描。
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and dialog._cache_scan_in_progress:
        QTest.qWait(50)
    yield dialog
    dialog.deleteLater()
    qapp.processEvents()


from src.app.ui.preferences_dialog import PreferencesDialog  # noqa: E402


class TestAud03908CacheSizeScan:
    """AUD-03908: 缓存体积统计移入后台线程 + os.walk 递归统计。"""

    def test_compute_cache_bytes_is_recursive(self, tmp_path, monkeypatch) -> None:
        import src.shared.io.file_io as fio_mod

        baked = tmp_path / "baked"
        imgs = tmp_path / "imgs"
        (baked / "sub").mkdir(parents=True)
        imgs.mkdir()
        (baked / "top.bin").write_bytes(b"a" * 100)
        (baked / "sub" / "nested.bin").write_bytes(b"b" * 50)  # 旧实现漏统计
        (imgs / "img.dat").write_bytes(b"c" * 25)

        monkeypatch.setattr(fio_mod.FileIO, "BAKED_CACHE_DIR", str(baked))
        monkeypatch.setattr(fio_mod.FileIO, "CACHE_DIR", str(imgs))

        total = PreferencesDialog._compute_cache_bytes()
        assert total == 175  # 旧实现顶层 listdir 只算 125

    def test_label_updated_from_worker_thread(self, prefs_dialog,
                                              monkeypatch) -> None:
        probe_threads: list[str] = []

        def fake_compute() -> int:
            probe_threads.append(threading.current_thread().name)
            return 20480

        gate = threading.Event()
        started = threading.Event()

        def gated_compute() -> int:
            started.set()
            gate.wait(3.0)  # 挡住 worker，制造可观察的"异步窗口"
            probe_threads.append(threading.current_thread().name)
            return 20480

        monkeypatch.setattr(prefs_dialog, "_compute_cache_bytes", gated_compute)
        label = prefs_dialog._widgets.get("cache_size_label")
        assert label is not None
        prefs_dialog._cache_scan_in_progress = False  # 排空构造期的那次扫描

        prefs_dialog._update_cache_size_label()
        assert started.wait(3.0), "worker never started"
        assert probe_threads == []  # worker 仍被 gate 挡住 → 未在 UI 线程同步扫盘
        gate.set()

        from PyQt6.QtTest import QTest
        import time
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            QTest.qWait(20)
            if probe_threads:
                break
        assert probe_threads, "scan never ran"
        assert all(t != "MainThread" for t in probe_threads)
        assert "20.0 KB" in label.text()

    def test_overlapping_scan_is_skipped(self, prefs_dialog,
                                         monkeypatch) -> None:
        gate = threading.Event()
        compute = Mock(side_effect=lambda: (gate.wait(5.0), 1)[1])
        monkeypatch.setattr(prefs_dialog, "_compute_cache_bytes", compute)
        prefs_dialog._cache_scan_in_progress = False  # 排空构造期的那次扫描

        prefs_dialog._update_cache_size_label()
        prefs_dialog._update_cache_size_label()  # 扫描进行中 → 复用，不叠加
        gate.set()

        from PyQt6.QtTest import QTest
        import time
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            QTest.qWait(20)
            if compute.call_count == 1 and not prefs_dialog._cache_scan_in_progress:
                break
        assert compute.call_count == 1  # 旧实现同步调用两次 → 2
        assert prefs_dialog._cache_scan_in_progress is False


class TestAud03912DiagnosticsExportOffUiThread:
    """AUD-03912: 诊断导出的 torch/onnxruntime 导入移入后台线程。"""

    def test_export_probes_and_writes_off_main_thread(
            self, prefs_dialog, monkeypatch, tmp_path) -> None:
        import types

        fake_ort = types.SimpleNamespace(
            __version__="9.9.9",
            get_available_providers=lambda: ["CPUExecutionProvider"])
        fake_torch = types.SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: False, device_count=lambda: 0))
        monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        target = tmp_path / "diag.json"
        monkeypatch.setattr(
            "src.app.ui.preferences_dialog.QFileDialog.getSaveFileName",
            staticmethod(lambda *a, **k: (str(target), "")))

        infos, warnings = [], []
        monkeypatch.setattr(
            "src.app.ui.preferences_dialog.QMessageBox.information",
            lambda *a, **k: infos.append(a))
        monkeypatch.setattr(
            "src.app.ui.preferences_dialog.QMessageBox.warning",
            lambda *a, **k: warnings.append(a))

        probe_threads: list[str] = []
        original_probe = prefs_dialog._probe_runtime_diagnostics

        def recording_probe():
            probe_threads.append(threading.current_thread().name)
            return original_probe()

        monkeypatch.setattr(prefs_dialog, "_probe_runtime_diagnostics",
                            recording_probe)

        prefs_dialog._export_diagnostics()

        import time
        from PyQt6.QtTest import QTest
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            QTest.qWait(20)
            if target.exists() and infos:
                break
        assert target.exists(), "diagnostics file never written"
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["onnx_runtime"]["version"] == "9.9.9"
        assert payload["cuda"]["available"] is False
        assert probe_threads and probe_threads[0] != "MainThread"
        assert len(infos) == 1  # 保存成功提示
        assert warnings == []

    def test_write_failure_reports_warning(self, prefs_dialog, monkeypatch,
                                           tmp_path) -> None:
        bad_probe = Mock(side_effect=OSError("probe blew up"))
        monkeypatch.setattr(prefs_dialog, "_probe_runtime_diagnostics",
                            bad_probe)
        target = tmp_path / "diag2.json"
        infos, warnings = [], []
        monkeypatch.setattr(
            "src.app.ui.preferences_dialog.QMessageBox.information",
            lambda *a, **k: infos.append(a))
        monkeypatch.setattr(
            "src.app.ui.preferences_dialog.QMessageBox.warning",
            lambda *a, **k: warnings.append(a))

        prefs_dialog._write_diagnostics_report({}, str(target))

        from PyQt6.QtTest import QTest
        QTest.qWait(120)
        assert len(warnings) == 1
        assert infos == []


class _ShortcutStubConfig:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, auto_save=True):
        self.data[key] = value

    def save_config(self):
        return None


class TestAud4103DeadTableFlag:
    """AUD-4103: `_updating_table` 只写不读的死标志 → 删除。"""

    def _dialog(self, qapp):
        from src.app.ui.shortcut_dialog import ShortcutDialog
        from src.shared.ui.shortcut_manager import ShortcutManager

        cfg = _ShortcutStubConfig({"custom_shortcuts": {}})
        manager = ShortcutManager(cfg)
        manager.register("test.a1", "Action 1", "Test", "Ctrl+1")
        manager.register("test.a2", "Action 2", "Test", "Ctrl+2")
        return ShortcutDialog(manager)

    def test_dead_flag_removed_but_table_still_fills(self, qapp) -> None:
        dialog = self._dialog(qapp)
        try:
            assert "_updating_table" not in dialog.__dict__
            assert not hasattr(dialog, "_updating_table")  # 旧实现存在

            actions = dialog._manager.get_actions_by_feature(
                dialog._enabled_features)
            dialog._populate_table(actions)
            assert dialog._table.rowCount() == len(actions) == 2
            assert dialog._table.item(0, dialog.COL_ACTION) is not None
        finally:
            dialog.deleteLater()


class _WelcomeStubConfig:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, auto_save=True):
        self.data[key] = value

    def save_config(self):
        pass


class TestAud4112SilentExceptBlocks:
    """AUD-4112: welcome_dialog 三处宽泛 except 吞错 → 收窄留痕 + 提示。"""

    @staticmethod
    def _patch_work_mode(monkeypatch, fail_from: int = 2) -> dict:
        """_setup_ui 里有一次不受 try 保护的 get_work_mode 调用（:127）。

        首次放行返回 "video"，从第 fail_from 次起抛错 —— 精确命中被测的
        except 分支而不炸构造。
        """
        import src.shared.feature_flags as ff

        calls = {"n": 0}

        def fake(config):
            calls["n"] += 1
            if calls["n"] >= fail_from:
                raise RuntimeError("mode detect failed")
            return "video"

        monkeypatch.setattr(ff, "get_work_mode", fake)
        return calls

    def test_recent_filter_failure_logs_warning(self, qapp, monkeypatch,
                                                caplog) -> None:
        from src.app.ui.welcome_dialog import WelcomeDialog

        self._patch_work_mode(monkeypatch, fail_from=2)
        cfg = _WelcomeStubConfig({
            "recent_files": ["/a/b.png"],
            "recent_projects": [],
        })
        with caplog.at_level("WARNING", logger="src.app.ui.welcome_dialog"):
            dialog = WelcomeDialog(cfg)
        try:
            assert dialog.recent_list.count() == 1  # 降级不过滤，但列表正常
            assert any("recent-files work-mode filter" in rec.getMessage()
                       for rec in caplog.records)  # 旧实现静默
        finally:
            dialog.deleteLater()

    def test_project_mode_detection_failure_logs_warning(
            self, qapp, monkeypatch, caplog) -> None:
        from PyQt6.QtTest import QTest
        from src.app.ui.welcome_dialog import WelcomeDialog

        monkeypatch.setattr("src.shared.feature_flags.get_product_mode",
                            lambda: "studio")
        self._patch_work_mode(monkeypatch, fail_from=2)
        cfg = _WelcomeStubConfig({"recent_projects": []})
        with caplog.at_level("WARNING", logger="src.app.ui.welcome_dialog"):
            dialog = WelcomeDialog(cfg)
        try:
            assert any("work-mode detection" in rec.getMessage()
                       for rec in caplog.records)
        finally:
            QTest.qWait(80)  # 让分类线程池先跑完（projects 为空，立即结束）
            dialog.deleteLater()
            QTest.qWait(20)

    def test_restart_save_failure_warns_instead_of_silence(
            self, qapp, monkeypatch, caplog) -> None:
        from PyQt6.QtWidgets import QMessageBox
        from src.app.ui.welcome_dialog import WelcomeDialog

        dialog = WelcomeDialog(_WelcomeStubConfig())
        try:
            monkeypatch.setattr(
                QMessageBox, "question",
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
            warnings: list = []
            monkeypatch.setattr(
                QMessageBox, "warning",
                staticmethod(lambda *a, **k: warnings.append(a)))
            monkeypatch.setattr(
                dialog._config, "save_config",
                Mock(side_effect=OSError("config file locked")))
            restart = Mock()
            monkeypatch.setattr(
                "src.shared.app_restart.restart_application", restart)

            with caplog.at_level("ERROR", logger="src.app.ui.welcome_dialog"):
                dialog._on_mode_clicked("image")

            assert len(warnings) == 1  # 旧实现静默重启，无提示
            assert any("Config save before restart failed" in rec.getMessage()
                       for rec in caplog.records)
            restart.assert_called_once()
        finally:
            dialog.deleteLater()
            qapp.processEvents()
