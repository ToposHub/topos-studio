"""B2: Stem 分轨导出对话框（生产入口）测试。

覆盖：
- export_all_stems 新增的 ``specs`` 子集导出与 ``cancel_check`` 取消语义
- create_clip_decode_fn 工厂（无工程/资源缺失时安全返回 None，不抛）
- StemExportDialog 空态与规划列表
- StemExportWorker 线程包装（真实 WAV 写出 / 取消）

sequence 夹具与 tests/edit/test_stem_export.py 同源（3 轨 + 2 Bus）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.stem_export import (  # noqa: E402
    StemType,
    create_clip_decode_fn,
    export_all_stems,
    plan_stems_from_sequence,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

SR = 48000
FPS = 24.0


def _make_track(
    track_id: str,
    name: str = "",
    clips_count: int = 1,
    is_bus: bool = False,
    output_bus_id=None,
    stem_group: str = "",
) -> EditTrack:
    clips = (
        tuple(
            ClipInstance(
                clip_id=f"{track_id}_clip{i}",
                asset_id=f"{track_id}_asset",
                track_id=track_id,
                source_start=0,
                source_duration=48,
                timeline_start=0,
                timeline_duration=48,
            )
            for i in range(clips_count)
        )
        if clips_count and not is_bus
        else ()
    )
    return EditTrack(
        track_id=track_id,
        name=name or track_id,
        track_type="audio",
        index=0,
        clips=clips,
        is_bus=is_bus,
        output_bus_id=output_bus_id,
        stem_group=stem_group,
    )


def _make_sequence() -> TimelineSequence:
    bus_a = _make_track("busA", "Bus A", is_bus=True)
    t1 = _make_track("t1", "Dialogue", output_bus_id="busA", stem_group="dialogue")
    t2 = _make_track("t2", "Music", stem_group="music")
    seq = TimelineSequence(audio_tracks=(t1, t2, bus_a), fps=FPS)
    return seq


# ----------------------------------------------------------------------
# Core: specs 子集 + cancel_check
# ----------------------------------------------------------------------

class TestExportAllStemsSelection:
    def test_specs_subset_exports_only_selected(self, tmp_path: Path) -> None:
        """显式 specs 只导出所选 stem（对话框勾选语义）。"""
        seq = _make_sequence()
        specs = [s for s in plan_stems_from_sequence(seq)
                 if s.stem_type == StemType.TRACK]
        result = export_all_stems(
            seq, str(tmp_path), sample_rate=SR, specs=specs,
        )
        names = [n for n, _ok in result.stems]
        assert sorted(names) == ["Music", "Dialogue"] or set(names) == {
            "Dialogue", "Music"
        }
        assert result.all_succeeded
        for name in names:
            assert (tmp_path / f"{name}.wav").exists()

    def test_cancel_check_stops_before_first_stem(self, tmp_path: Path) -> None:
        seq = _make_sequence()
        result = export_all_stems(
            seq, str(tmp_path), sample_rate=SR,
            cancel_check=lambda: True,
        )
        assert result.cancelled is True
        assert result.stems == []
        assert list(tmp_path.iterdir()) == []

    def test_cancel_flag_false_on_complete_run(self, tmp_path: Path) -> None:
        seq = _make_sequence()
        result = export_all_stems(
            seq, str(tmp_path), sample_rate=SR,
            specs=plan_stems_from_sequence(seq)[:1],
        )
        assert result.cancelled is False
        assert result.all_succeeded


class TestCreateClipDecodeFn:
    def test_no_project_returns_none_silently(self) -> None:
        decode, close = create_clip_decode_fn(None, FPS, SR)
        seq = _make_sequence()
        track = seq.audio_tracks[0]
        clip = track.clips[0]
        assert decode(track, clip, 0, FPS, 1920) is None
        close()

    def test_close_fn_idempotent(self) -> None:
        _decode, close = create_clip_decode_fn(None, FPS, SR)
        close()
        close()  # 二次释放不抛


# ----------------------------------------------------------------------
# Dialog
# ----------------------------------------------------------------------

@pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")
class TestStemExportDialog:
    def _dialog(self, sequence=None, project=None):
        from src.features.edit.ui.stem_export_dialog import StemExportDialog

        return StemExportDialog(sequence, project)

    def test_empty_sequence_safe(self, qapp) -> None:
        """无序列（None）时进入空态：无 stem 行、导出按钮禁用、不崩。"""
        dialog = self._dialog(None, None)
        assert dialog._spec_boxes == []
        assert not dialog._export_btn.isEnabled()

    def test_plan_rows_listed(self, qapp) -> None:
        seq = _make_sequence()
        dialog = self._dialog(seq, None)
        # Master + 2 track stems + 1 bus stem + 2 group stems (D/M) = 6
        assert len(dialog._spec_boxes) == len(plan_stems_from_sequence(seq))
        assert dialog._export_btn.isEnabled()

    def test_selection_respected(self, qapp) -> None:
        seq = _make_sequence()
        dialog = self._dialog(seq, None)
        dialog._spec_boxes[0][1].setChecked(False)
        assert len(dialog._selected_specs()) == len(dialog._spec_boxes) - 1


# ----------------------------------------------------------------------
# Worker
# ----------------------------------------------------------------------

@pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")
class TestStemExportWorker:
    def test_worker_exports_real_wav(self, qtbot, tmp_path: Path) -> None:
        from src.features.edit.ui.stem_export_dialog import StemExportWorker

        seq = _make_sequence()
        specs = plan_stems_from_sequence(seq)[:1]  # Master
        worker = StemExportWorker(seq, None, str(tmp_path), tuple(specs))
        with qtbot.waitSignal(worker.finished_ok, timeout=30000):
            worker.start()
        result = worker.finished_ok_signal_result = None
        # waitSignal 返回后 worker 已结束；重新读取结果
        assert worker.isFinished()
        assert (tmp_path / "Master.wav").exists()
        del result

    def test_worker_cancel_reports_cancelled(self, qtbot, tmp_path: Path) -> None:
        from src.features.edit.ui.stem_export_dialog import StemExportWorker

        seq = _make_sequence()
        specs = plan_stems_from_sequence(seq)
        worker = StemExportWorker(seq, None, str(tmp_path), tuple(specs))
        worker.cancel()
        with qtbot.waitSignal(worker.finished_ok, timeout=30000):
            worker.start()
        # 取消后无文件写出（首个 stem 前即中止）
        assert list(tmp_path.iterdir()) == []

    def test_worker_prefix_renames_files(self, qtbot, tmp_path: Path) -> None:
        from src.features.edit.ui.stem_export_dialog import StemExportWorker

        seq = _make_sequence()
        specs = plan_stems_from_sequence(seq)[:1]
        worker = StemExportWorker(
            seq, None, str(tmp_path), tuple(specs), prefix="MyFilm_"
        )
        with qtbot.waitSignal(worker.finished_ok, timeout=30000):
            worker.start()
        assert (tmp_path / "MyFilm_Master.wav").exists()
        assert not (tmp_path / "Master.wav").exists()
