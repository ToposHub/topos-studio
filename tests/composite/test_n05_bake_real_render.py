"""N05 余项 · Bake 真实渲染回调接线测试。

审计句：菜单传 graph=None、恒 False render stub、固定范围、未 start。
修复契约：预建全帧 NodeEngine snapshot（UI 线程）→ render_fn 走
CompositeExportProvider.render_full_res 写 EXR；帧域=时间线区间；经
宿主打开并真正 start()；改写回 UI 线程。
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

pytestmark = pytest.mark.composite_core


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _host():
    """绑定 MenuToolbarMixin 的 N05 方法到假宿主（QWidget，宿主需要）。"""
    from PyQt6.QtWidgets import QWidget
    from src.app.controllers.menu_toolbar_controller import (
        MenuToolbarMixin,
    )

    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self.unavailable = []

            def record(message):
                self.unavailable.append(message)

            self._upgrade_panel_unavailable = record

    host = _Host()
    for name, fn in inspect.getmembers(
            MenuToolbarMixin, predicate=inspect.isfunction):
        if name in ("on_open_bake_dialog", "_build_bake_context",
                    "_upgrade_panel_context", "_open_upgrade_panel",
                    "_panel_host", "_selected_tracker_node",
                    "_composite_host"):
            setattr(host, name, fn.__get__(host))
    return host


def _fake_engine(threads):
    class _NG:
        def snapshot(self, **kw):
            threads.append(threading.current_thread().ident)
            payload = dict(kw)
            payload["frame"] = kw.get("frame_number")
            return NS(**payload)
    return _NG()


def _timeline(total=3):
    clip = NS(timeline_start=0, timeline_duration=total)
    track = NS(clips=(clip,))
    return NS(timeline_id="tl1", fps=24.0, width=64, height=48,
              video_tracks=(track,), audio_tracks=(), subtitle_tracks=())


class TestBakeContext:
    def _setup(self, monkeypatch, threads):
        host = _host()
        ng = _fake_engine(threads)
        timeline = _timeline(3)
        host._upgrade_panel_context = lambda: {
            "project": NS(revision=1), "timeline": timeline}
        host._selected_tracker_node = lambda: NS(node_id="node-9")
        host._composite_host = lambda: NS(node_graph=ng)
        return host, ng

    def test_snapshots_prebuilt_on_ui_thread_full_range(
            self, qapp, monkeypatch):
        threads = []
        host, ng = self._setup(monkeypatch, threads)
        context = host._build_bake_context()
        assert context is not None, host.unavailable
        assert (context["frame_start"], context["frame_end"]) == (0, 2)
        # 快照全部在 UI（当前主）线程构建，覆盖每帧
        assert threads and all(
            t == threading.current_thread().ident for t in threads)

    def test_render_fn_uses_provider_and_writes_image(
            self, qapp, monkeypatch, tmp_path):
        threads = []
        host, ng = self._setup(monkeypatch, threads)
        import numpy as np
        seen = []

        class _Provider:
            def render_full_res(self, snapshot=None, **kw):
                seen.append(getattr(snapshot, "frame", None))
                return NS(image=np.full((4, 4, 3), 0.5, np.float32))

        monkeypatch.setattr(
            "src.features.composite.export_provider."
            "CompositeExportProvider", _Provider)
        context = host._build_bake_context()
        out = tmp_path / "f0.exr"
        assert context["render_fn"]("node-9", 1, str(out)) is True
        assert seen == [1]  # 预建快照按帧取用

    def test_missing_snapshot_or_bad_render_fails(
            self, qapp, monkeypatch, tmp_path):
        threads = []
        host, ng = self._setup(monkeypatch, threads)
        context = host._build_bake_context()
        # 越界帧无快照 → False（诚实失败）
        assert context["render_fn"]("node-9", 99,
                                    str(tmp_path / "x.exr")) is False

        class _BadProvider:
            def render_full_res(self, snapshot=None, **kw):
                return NS(image=None)

        monkeypatch.setattr(
            "src.features.composite.export_provider."
            "CompositeExportProvider", _BadProvider)
        assert context["render_fn"]("node-9", 0,
                                    str(tmp_path / "y.exr")) is False

    def test_prerequisites_refuse_honestly(self, qapp, monkeypatch):
        host = _host()
        host._selected_tracker_node = lambda: None
        host._upgrade_panel_context = lambda: {"project": None,
                                               "timeline": None}
        host._composite_host = lambda: NS(node_graph=None)
        assert host._build_bake_context() is None
        assert any("选中" in m for m in host.unavailable)
        host._selected_tracker_node = lambda: NS(node_id="n")
        assert host._build_bake_context() is None
        assert any("Composite 图不可用" in m for m in host.unavailable)


class TestMenuEndToEnd:
    def test_opens_via_host_and_starts_worker(self, qapp, monkeypatch):
        threads = []
        host = _host()
        ng = _fake_engine(threads)
        host._upgrade_panel_context = lambda: {
            "project": NS(revision=1), "timeline": _timeline(2)}
        host._selected_tracker_node = lambda: NS(node_id="node-9")
        host._composite_host = lambda: NS(node_graph=ng)
        from src.app.controllers.upgrade_panel_host import (
            UpgradePanelHost,
        )
        host._panel_host = UpgradePanelHost(host)
        import numpy as np

        class _Provider:
            def render_full_res(self, snapshot=None, **kw):
                return NS(image=np.full((4, 4, 3), 0.5, np.float32))

        monkeypatch.setattr(
            "src.features.composite.export_provider."
            "CompositeExportProvider", _Provider)
        # execute_bake 打桩：消费 render_fn 全帧 + ok 结果（改写经 plan）
        import src.features.composite.ui.bake_progress_dialog as bpd
        rewrites = []  # 真实改写路径（UI 线程 apply_bake_rewrite）记录

        class _Plan:
            pass

        def fake_execute_bake(**kwargs):
            for frame in range(kwargs["frame_start"],
                               kwargs["frame_end"] + 1):
                out = f"{kwargs['output_dir']}/f{frame}.exr"
                if not kwargs["render_fn"](kwargs["target_id"], frame, out):
                    return NS(ok=False, plan=None, published=[],
                              written=[], error="render failed",
                              stage="render")
            assert kwargs["apply_rewrite"] is False  # 改写在 UI 线程
            return NS(ok=True, plan=_Plan(), published=["a"],
                      written=[], error="", stage="")

        monkeypatch.setattr(bpd, "execute_bake", fake_execute_bake)

        monkeypatch.setattr(
            "src.features.composite.bake_service.apply_bake_rewrite",
            lambda graph, plan: rewrites.append(
                threading.current_thread().ident))
        host.on_open_bake_dialog()
        dialog = host._panel_host.active_panel("composite.bake")
        assert dialog is not None
        assert dialog._worker is not None
        assert dialog._worker.wait(5000)
        from PyQt6.QtWidgets import QApplication
        for _ in range(10):
            QApplication.processEvents()
        assert dialog.result is not None and dialog.result.ok
        assert dialog.rewrite_applied is True  # UI 线程改写已执行
        assert rewrites and rewrites[0] == threading.current_thread().ident
