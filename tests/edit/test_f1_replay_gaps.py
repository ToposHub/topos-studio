"""F1.1 历史缺陷复测补齐——D03 过时结果守卫 / D07 异常路径 close / U07 文案。

26 项中 20 项已有测试+修复双证据（重放批次 191 passed，见 F1.1 ledger）；
本文件补齐三项残余缺口的 focused 证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.timeline_view import TimelineView

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── D03：响度分析过时结果守卫（QPointer 判空丢弃）────────────────────

class TestD03StaleResultGuard:
    def _make_widgets(self, qapp):
        from PyQt6.QtWidgets import QDoubleSpinBox, QLabel, QWidget

        progress = QWidget()
        label = QLabel("initial")
        spin = QDoubleSpinBox()
        spin.setRange(-48.0, 12.0)
        spin.setValue(0.0)
        return progress, label, spin

    def test_live_dialog_writes_result(self, qapp) -> None:
        progress, label, spin = self._make_widgets(qapp)
        TimelineView._finalize_normalize_analysis(
            progress, None, label, spin, True, "LUFS -23.0", -4.5)
        assert label.text() == "LUFS -23.0"
        assert spin.value() == -4.5

    def test_deleted_dialog_discards_silently(self, qapp) -> None:
        from PyQt6 import sip

        progress, label, spin = self._make_widgets(qapp)
        sip.delete(progress)
        sip.delete(label)
        sip.delete(spin)
        # 不抛 RuntimeError；过时结果被丢弃
        TimelineView._finalize_normalize_analysis(
            progress, None, label, spin, True, "LUFS -23.0", -4.5)

    def test_cancelled_result_never_touches_spin(self, qapp) -> None:
        progress, label, spin = self._make_widgets(qapp)
        TimelineView._finalize_normalize_analysis(
            progress, None, label, spin, False, "分析已取消", None)
        assert spin.value() == 0.0
        assert label.text() == "分析已取消"


# ── D07：解码异常路径也关闭全部 decoder（finally 契约）────────────────

class TestD07ExceptionPathClosesDecoders:
    def test_decoder_raise_closes_all_and_skips_clip(self, tmp_path, monkeypatch) -> None:
        from tests.edit.test_fix_round6_phase1b_audio import (
            _DCDecoder,
            _audio_timeline,
            _config,
            _dc_project,
            _exporter,
            _render_master,
        )

        class _RaisingDecoder(_DCDecoder):
            def decode_samples_for_frame(self, frame, fps, num_samples):
                raise RuntimeError("boom: corrupt audio")

        _DCDecoder.instances = []
        monkeypatch.setattr(
            "src.features.edit.core.audio_playback.AudioDecoder", _RaisingDecoder)

        seq = _audio_timeline("asset-1", timeline_duration=24, source_duration=24)
        exporter = _exporter(_dc_project(), seq)
        result = _render_master(exporter, 24, _config(tmp_path))
        # 单 clip 异常被逐 clip except→continue 跳过，渲染仍完成（静音段）
        assert result is not None
        created = [d for d in _DCDecoder.instances if isinstance(d, _RaisingDecoder)]
        assert created, "应当创建过 decoder"
        unclosed = [d for d in created if not d.closed]
        assert not unclosed, f"异常路径 {len(unclosed)} 个 decoder 未关闭"


# ── Q01：容器关闭互锁时 close() 有界返回（单进程套件挂起根因修复）──

class TestQ01BoundedClose:
    @staticmethod
    def _bare_source(source_cls, container):
        src = source_cls.__new__(source_cls)
        src._gop_index_thread = None
        src._frame_iterator = None
        src._decoder_buffer = []
        src._pending_av_frame = None
        src._gop_seek_iterator = None
        src._main_resync_pts = None
        src._gop_seek_container = None
        src._gop_seek_stream = None
        src._is_open = True
        src._current_frame = 5
        src._path = "q01_hang.mov"
        src._container = container
        src._video_stream = None
        return src

    def test_close_returns_bounded_when_container_close_blocks(self) -> None:
        import threading
        import time

        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        release = threading.Event()

        class _BlockingContainer:
            def close(self) -> None:
                release.wait(30.0)  # 模拟 libav 互锁

        src = self._bare_source(FFmpegMediaSource, _BlockingContainer())
        t0 = time.monotonic()
        try:
            src.close(close_timeout=0.2)
        finally:
            release.set()  # 放行守护线程
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"close() 阻塞 {elapsed:.2f}s——Q01 互锁未修复"
        assert src._is_open is False
        assert src._container is None

    def test_close_completes_when_container_healthy(self) -> None:
        """健康容器：close() 立即返回，容器由守护线程在短时间内真正关闭。"""
        import time

        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        class _HealthyContainer:
            def __init__(self):
                self.closed = False

            def close(self) -> None:
                self.closed = True

        container = _HealthyContainer()
        src = self._bare_source(FFmpegMediaSource, container)
        t0 = time.monotonic()
        src.close(close_timeout=2.0)
        assert time.monotonic() - t0 < 1.0, "close() 必须发射后立即返回"
        assert src._is_open is False
        deadline = time.monotonic() + 3.0
        while not container.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert container.closed, "健康容器应当在守护线程内短时间内关闭"

    def test_gc_path_never_closes_container_and_defers(self) -> None:
        """__del__ 上下文：容器不关闭、停靠注册表（Q01 GIL-freeze guard）。

        转储实证 av close 互锁期间不释放 GIL，join 也会冻死——GC 路径必须
        完全跳过容器关闭。
        """
        import time

        from src.shared.media import ffmpeg_source as fs_mod
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        class _NeverContainer:
            def __init__(self):
                self.close_attempts = 0

            def close(self) -> None:
                self.close_attempts += 1
                raise AssertionError("GC 路径不得调用容器 close")

        container = _NeverContainer()
        src = self._bare_source(FFmpegMediaSource, container)
        before = len(fs_mod._DEFERRED_CONTAINERS)
        t0 = time.monotonic()
        src.__del__()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert container.close_attempts == 0
        assert len(fs_mod._DEFERRED_CONTAINERS) == before + 1
        assert fs_mod._DEFERRED_CONTAINERS[-1] is container
        # 清理停靠，避免跨用例持有
        fs_mod._DEFERRED_CONTAINERS.remove(container)


# ── U07：已可用入口的文案不含占位提示（linked/markers）───────────────

class TestU07LocaleCopyTruthful:
    PLACEHOLDERS = ("即将推出", "敬请期待", "coming soon", "Coming Soon", "Coming soon")

    @staticmethod
    def _find_values(node, key_name: str, prefix: str = ""):
        found: dict[str, str] = {}
        if isinstance(node, dict):
            for k, v in node.items():
                path = f"{prefix}.{k}" if prefix else k
                if k == key_name and isinstance(v, str):
                    found[path] = v
                found.update(TestU07LocaleCopyTruthful._find_values(v, key_name, path))
        return found

    @pytest.mark.parametrize("key_name", ["linked_selection", "show_markers"])
    def test_entries_exist_and_no_placeholder(self, key_name: str) -> None:
        locales_dir = REPO_ROOT / "src" / "shared" / "i18n" / "locales"
        zh = json.loads((locales_dir / "zh_CN.json").read_text(encoding="utf-8"))
        entries = self._find_values(zh, key_name)
        assert entries, f"zh_CN 缺少 {key_name} 条目"
        for path, value in entries.items():
            for marker in self.PLACEHOLDERS:
                assert marker not in value, f"{path} 含占位文案: {value}"
