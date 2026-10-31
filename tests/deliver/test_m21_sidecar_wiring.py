"""M21.1b+ · 编码器 sidecar 产出接线回归。

SIDECAR 模式的导出任务：编码开始前 .srt 渲染到临时文件（失败即零
发布），成片发布成功后 sidecar 经 publish_file_group 成组就位（同一
成功身份；旧 sidecar 备份→成功后清理）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from types import SimpleNamespace as NS

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication

from src.features.deliver.core.export_queue import ExportJob
from src.features.deliver.core.video_export_job import (
    SubtitleConfig,
    SubtitleMode,
    VideoExportJob,
)
from src.features.deliver.core.video_job_runner import JobContext, VideoJobRunner
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.shared.media.metadata import Rational


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _FakeExporter(QObject):
    """替身 worker：进入事件循环后回调成功/失败/取消。"""

    def __init__(self, outcome, payload, parent=None):
        super().__init__(parent)
        self._outcome = outcome
        self._payload = payload

    def export(self, project, sequence, config, on_finished, on_error,
               on_progress, timeline_sequences=None):
        if self._outcome == "ok":
            QTimer.singleShot(0, lambda: on_finished(self._payload))
        else:
            QTimer.singleShot(0, lambda: on_error(self._payload))

    def cleanup(self):
        pass


def _sequence_with_subtitle() -> TimelineSequence:
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(SubtitleClip(
            clip_id="sub-1", track_id="s1", timeline_start=0,
            timeline_duration=24, text="你好字幕"),),
    )
    return TimelineSequence(
        timeline_id="t1", name="Sub TL", fps=Rational(24, 1),
        subtitle_tracks=(track,),
    )


def _run(monkeypatch, tmp_path, *, subtitle_mode, outcome="ok",
         sequence=None):
    out = tmp_path / "movie.mp4"
    job = VideoExportJob(
        project_id="p1", timeline_id="t1", timeline_name="Sub TL",
        output_path=str(out),
        subtitle_config=SubtitleConfig(mode=subtitle_mode),
    )
    legacy = ExportJob(
        job_id=job.job_id, name="job", config={"video_job": job.to_dict()},
        output_path=str(out),
    )
    runner = VideoJobRunner()
    runner.set_context_provider(lambda _job: JobContext(
        project=object(), sequence=sequence or _sequence_with_subtitle()))
    monkeypatch.setattr(
        VideoJobRunner, "_create_worker",
        lambda self, project, seq, cfg: (
            _FakeExporter(outcome, str(out) if outcome == "ok"
                          else "EXPORT_CANCELLED"), None))
    return runner, legacy, out


def test_sidecar_published_with_media(qapp, monkeypatch, tmp_path):
    """SIDECAR + 编码成功 → .srt 与成片同身份就位，无临时/备份残留。"""
    runner, legacy, out = _run(monkeypatch, tmp_path,
                               subtitle_mode=SubtitleMode.SIDECAR)
    assert runner.run_job(legacy, lambda _p: None) is True
    srt = out.with_suffix(".srt")
    assert srt.exists()
    content = srt.read_text(encoding="utf-8-sig")
    assert "你好字幕" in content and "00:00:00,000 -->" in content
    assert not os.path.exists(str(srt) + ".topos-tmp")
    assert not os.path.exists(str(srt) + ".topos-old")


def test_sidcar_off_unchanged_path(qapp, monkeypatch, tmp_path):
    """OFF 模式：不产生 sidecar（既有路径零扰动）。"""
    runner, legacy, out = _run(monkeypatch, tmp_path,
                               subtitle_mode=SubtitleMode.OFF)
    assert runner.run_job(legacy, lambda _p: None) is True
    assert not out.with_suffix(".srt").exists()


def test_sidecar_render_failure_zero_publish(qapp, monkeypatch, tmp_path):
    """sidecar 渲染失败 → 任务失败，零发布（编码不启动），临时文件清理。"""
    runner, legacy, out = _run(monkeypatch, tmp_path,
                               subtitle_mode=SubtitleMode.SIDECAR)
    from src.features.deliver import srt_exporter as srt_mod
    monkeypatch.setattr(srt_mod, "export_srt", lambda seq, path: False)
    with pytest.raises(RuntimeError, match="sidecar"):
        runner.run_job(legacy, lambda _p: None)
    srt = out.with_suffix(".srt")
    assert not srt.exists()
    assert not os.path.exists(str(srt) + ".topos-tmp")


def test_cancel_discards_sidecar_temp(qapp, monkeypatch, tmp_path):
    """编码取消 → sidecar 临时文件丢弃，final 不出现。"""
    runner, legacy, out = _run(monkeypatch, tmp_path,
                               subtitle_mode=SubtitleMode.SIDECAR,
                               outcome="cancelled")
    assert runner.run_job(legacy, lambda _p: None) is False
    srt = out.with_suffix(".srt")
    assert not srt.exists()
    assert not os.path.exists(str(srt) + ".topos-tmp")


def test_old_sidecar_replaced_backup_cleaned(qapp, monkeypatch, tmp_path):
    """旧 sidecar 存在 → 成组替换（备份→成功清理），新内容就位。"""
    runner, legacy, out = _run(monkeypatch, tmp_path,
                               subtitle_mode=SubtitleMode.SIDECAR)
    srt = out.with_suffix(".srt")
    srt.write_text("旧字幕", encoding="utf-8")
    assert runner.run_job(legacy, lambda _p: None) is True
    assert "你好字幕" in srt.read_text(encoding="utf-8-sig")
    assert not os.path.exists(str(srt) + ".topos-old")


def test_qc_panel_untested_subtitle_not_faked(qapp):
    """批队列入口只测 LUFS——字幕列显示"未测"（不冒充 0/通过）。"""
    from src.features.deliver.ui.m21_multi_version_qc_panel import (
        MultiVersionQcPanel,
    )
    lufs = {"/out/v1.mp4": {
        "input": NS(integrated_lufs=-14.0, audio_present=True),
        "error": "", "integrated_lufs": -14.0}}
    panel = MultiVersionQcPanel(lufs, subtitle_summary={})
    assert "字幕段未测" in panel._summary_label.text()
    assert panel._table.item(0, 4).text() == "未测"
    assert panel._table.item(0, 5).text() == "未测"


def test_batch_drain_emits_multi_version_qc(qapp, monkeypatch, tmp_path):
    """M21.2c/N12 接线：排空后本轮成功产物异步 LUFS 测量并发信号。"""
    from src.features.deliver.core.export_queue import ExportJob, JobStatus
    from src.features.deliver.core.video_batch_export_controller import (
        VideoBatchExportController,
    )
    media = tmp_path / "v.mp4"
    media.write_bytes(b"x")
    ctrl = VideoBatchExportController()
    job = ExportJob(config={"output_path": str(media)},
                    output_path=str(media), status=JobStatus.DONE)
    monkeypatch.setattr(
        type(ctrl.queue), "jobs", property(lambda self: [job]),
        raising=False)
    fired = []
    ctrl.multi_version_qc_ready.connect(lambda r: fired.append(r))
    monkeypatch.setattr(
        "src.features.edit.ai_editing.delivery_qc_batch."
        "measure_version_lufs",
        lambda paths: {p: {"input": None, "error": "测量失败"} for p in paths})
    ctrl._qc_batch_ids.add(str(job.job_id))  # N12：本轮批内登记
    ctrl._on_queue_drained()
    assert ctrl._qc_worker is not None  # N12：异步 worker
    assert ctrl._qc_worker.wait(5000)
    for _ in range(10):
        qapp.processEvents()
    assert len(fired) == 1
    report = fired[0]
    assert str(media) in report["lufs"]
    assert report["lufs"][str(media)]["error"]  # 失败显式≠通过
    assert report["subtitle"] == {}
    assert ctrl.last_multi_version_qc == report


def test_workspace_stashes_report_to_main_window(qapp):
    """宿主回填：报告 → 主窗口 _last_multi_version_qc（面板数据源）。"""
    from types import SimpleNamespace as NS2
    from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
    mw = NS2()
    ws = DeliverWorkspace.__new__(DeliverWorkspace)
    ws._main_window = mw
    ws._controller = NS2()
    DeliverWorkspace._on_multi_version_qc_ready(
        ws, {"lufs": {"/a.mp4": {}}, "subtitle": {}})
    assert mw._last_multi_version_qc["lufs"] == {"/a.mp4": {}}
