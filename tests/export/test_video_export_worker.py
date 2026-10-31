"""P5-T6：video_export_worker 专测。

VideoExportWorker / VideoExporter 的公共 API 覆盖。run() 内的
TimelineExporter 经 monkeypatch 替换为 fake（真实管线需媒体/编码器
硬件，不在本组测试范围）：

- run() 三态结果：success / cancelled / failure（error_code+cause）
- post_export 质量门：bool / tuple / 异常 / stage_telemetry 签名探测
- 预置控制请求：cancel（清 pause 标记）/ pause 在 run 前到达
- progress 转发（percent ×100）
- VideoExporter：线程化 round-trip（finished/error 回调 + is_exporting
  复位）、导出中拒绝二次 export、cancel/pause/resume 转发、cleanup

线程化用例经 qapp（tests/conftest 会话级 QApplication）+
processEvents 泵驱动，等待上限 5s，不依赖真实媒体与编码器。
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # tests/ui 同款约定

import pytest

from src.shared.export.video_export_worker import (
    VideoExporter,
    VideoExportWorker,
)


class FakeOutcome:
    """ExportOutcome（timeline_export.py:716）的替身。"""

    def __init__(self, success: bool = True, cancelled: bool = False,
                 error_code: str = "", cause: str = "",
                 audio_omitted: bool = False, hw_fallback_reason: str = "",
                 stage_telemetry=None) -> None:
        self.success = success
        self.cancelled = cancelled
        self.error_code = error_code
        self.cause = cause
        self.audio_omitted = audio_omitted
        self.hw_fallback_reason = hw_fallback_reason
        self.stage_telemetry = stage_telemetry


class FakeExporter:
    """TimelineExporter 替身：记录控制调用，可回放进度回调。"""

    def __init__(self, outcome: FakeOutcome, progress_callback=None) -> None:
        self._outcome = outcome
        self._progress_callback = progress_callback
        self.export_calls = []
        self.cancel_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0

    def export_timeline(self, config):
        self.export_calls.append(config)
        return self._outcome

    def cancel_export(self) -> None:
        self.cancel_calls += 1

    def pause_export(self) -> None:
        self.pause_calls += 1

    def resume_export(self) -> None:
        self.resume_calls += 1


def make_config(output_path: str = "/tmp/out.topos") -> SimpleNamespace:
    return SimpleNamespace(output_path=output_path,
                           codec=SimpleNamespace(value="topos"))


@pytest.fixture
def install_fake_exporter(monkeypatch):
    """把 run() 延迟导入的工厂替换为 FakeExporter 工厂。

    用法：``install_fake_exporter.set_outcome(FakeOutcome(...))`` 预置
    本次 run() 的导出结果；``install_fake_exporter.created`` 检视已
    创建的 fake exporter。
    """
    import src.features.edit.core.timeline_export as tl_mod

    state = {"outcome": FakeOutcome()}
    created: list = []

    def factory(project, sequence, progress_callback=None,
                timeline_sequences=None):
        fake = FakeExporter(state["outcome"],
                            progress_callback=progress_callback)
        created.append(fake)
        return fake

    monkeypatch.setattr(tl_mod, "create_timeline_exporter", factory)

    class Handle:
        pass

    handle = Handle()
    handle.created = created
    handle.set_outcome = lambda outcome: state.__setitem__("outcome", outcome)
    return handle


class TestWorkerRun:
    def test_success_emits_finished_and_keeps_outcome(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(
            FakeOutcome(success=True, stage_telemetry={"schema": "x"}))
        worker = VideoExportWorker()
        finished, errors = [], []
        worker.finished.connect(finished.append)
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config("/o/a.topos"))

        assert finished == ["/o/a.topos"]
        assert errors == []
        assert worker.last_outcome.success is True
        # finally 清理导出器引用
        assert worker._exporter is None

    def test_cancelled_emits_export_cancelled(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(
            FakeOutcome(success=False, cancelled=True))
        worker = VideoExportWorker()
        finished, errors = [], []
        worker.finished.connect(finished.append)
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert finished == []
        assert errors == ["EXPORT_CANCELLED"]

    def test_failure_emits_error_code_with_cause(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(
            success=False, error_code="EXPORT_ENCODE_FAILED",
            cause="disk full"))
        worker = VideoExportWorker()
        errors = []
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert errors == ["EXPORT_ENCODE_FAILED: disk full"]

    def test_failure_without_cause_uses_code_alone(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(
            FakeOutcome(success=False, error_code="EXPORT_FAILED"))
        worker = VideoExportWorker()
        errors = []
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert errors == ["EXPORT_FAILED"]

    def test_factory_crash_emits_error_message(
            self, qapp, monkeypatch) -> None:
        import src.features.edit.core.timeline_export as tl_mod

        def exploding_factory(*args, **kwargs):
            raise RuntimeError("pipeline exploded")

        monkeypatch.setattr(tl_mod, "create_timeline_exporter",
                            exploding_factory)
        worker = VideoExportWorker()
        finished, errors = [], []
        worker.finished.connect(finished.append)
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert finished == []
        assert errors == ["pipeline exploded"]

    def test_progress_callback_forwarded_times_100(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        progress_events = []
        worker.progress.connect(
            lambda current, total, percent: progress_events.append(
                (current, total, percent)))

        worker.run(object(), object(), make_config())
        fake = install_fake_exporter.created[0]
        assert fake._progress_callback is not None
        fake._progress_callback(5, 10, 0.5)

        assert progress_events == [(5, 10, 50.0)]


class TestWorkerPostExportGate:
    def test_accepted_bool_emits_finished(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        worker._post_export = lambda path: True
        finished, errors = [], []
        worker.finished.connect(finished.append)
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config("/o/b.topos"))

        assert finished == ["/o/b.topos"]
        assert errors == []

    def test_rejected_bool_emits_postcheck_failed(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        worker._post_export = lambda path: False
        finished, errors = [], []
        worker.finished.connect(finished.append)
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert finished == []
        assert errors == ["EXPORT_POSTCHECK_FAILED"]

    def test_rejected_tuple_carries_message(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        worker._post_export = lambda path: (False, "file size mismatch")
        errors = []
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert errors == ["file size mismatch"]

    def test_post_callback_crash_becomes_postcheck_failed(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))

        def broken(path):
            raise OSError("cannot stat")

        worker = VideoExportWorker()
        worker._post_export = broken
        errors = []
        worker.error.connect(errors.append)

        worker.run(object(), object(), make_config())

        assert errors == ["EXPORT_POSTCHECK_FAILED: OSError"]

    def test_telemetry_aware_signature_receives_stage_telemetry(
            self, qapp, install_fake_exporter) -> None:
        telemetry = {"schema": "topos-export-stage-telemetry/1"}
        install_fake_exporter.set_outcome(FakeOutcome(
            success=True, stage_telemetry=telemetry))
        received = {}

        def gate_with_telemetry(path, stage_telemetry=None):
            received["path"] = path
            received["telemetry"] = stage_telemetry
            return True

        def gate_without_telemetry(path):
            received["plain_called"] = True
            return True

        worker = VideoExportWorker()
        worker._post_export = gate_with_telemetry
        worker.finished.connect(lambda _path: None)
        worker.run(object(), object(), make_config("/o/c.topos"))
        assert received["path"] == "/o/c.topos"
        assert received["telemetry"] is telemetry

        # 无 stage_telemetry 形参的旧签名仍按位置调用
        worker2 = VideoExportWorker()
        worker2._post_export = gate_without_telemetry
        worker2.finished.connect(lambda _path: None)
        worker2.run(object(), object(), make_config())
        assert received.get("plain_called") is True


class TestWorkerControlRequests:
    def test_pre_run_cancel_applied_to_created_exporter(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        worker.request_cancel()  # 早于 run() 到达
        assert worker._cancel_requested is True

        worker.run(object(), object(), make_config())
        fake = install_fake_exporter.created[0]
        assert fake.cancel_calls == 1
        assert fake.pause_calls == 0

    def test_pre_run_pause_applied_to_created_exporter(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        worker.request_pause()
        assert worker._pause_requested is True

        worker.run(object(), object(), make_config())
        fake = install_fake_exporter.created[0]
        assert fake.pause_calls == 1
        assert fake.cancel_calls == 0

    def test_cancel_clears_pending_pause_request(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        worker = VideoExportWorker()
        worker.request_pause()
        worker.request_cancel()  # 取消优先，清除暂停标记

        worker.run(object(), object(), make_config())
        fake = install_fake_exporter.created[0]
        assert fake.cancel_calls == 1
        assert fake.pause_calls == 0

    def test_resume_clears_pause_flag_without_exporter(self, qapp) -> None:
        worker = VideoExportWorker()
        worker.request_pause()
        worker.request_resume()  # 无 exporter 在场：仅清标记，不抛
        assert worker._pause_requested is False

    def test_cancel_without_exporter_is_safe(self, qapp) -> None:
        worker = VideoExportWorker()
        worker.request_cancel()  # 不应抛异常
        assert worker._cancel_requested is True


def _pump_until(qapp, flag: threading.Event, timeout: float = 5.0) -> bool:
    """驱动主线程事件循环直到 flag 置位或超时。"""
    deadline = time.time() + timeout
    while not flag.is_set() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return flag.is_set()


class TestVideoExporterRoundTrip:
    def test_round_trip_success_and_is_exporting_reset(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        done = threading.Event()
        received = {}

        def on_finished(path: str) -> None:
            received["path"] = path
            done.set()

        exporter = VideoExporter()
        try:
            assert exporter.is_exporting is False
            exporter.export(project=object(), sequence=object(),
                            config=make_config("/o/rt.topos"),
                            on_finished=on_finished)
            assert exporter.is_exporting is True

            assert _pump_until(qapp, done), "finished 未在 5s 内回调"
            assert received["path"] == "/o/rt.topos"
            assert exporter.is_exporting is False
        finally:
            exporter.cleanup()

    def test_round_trip_error_invokes_on_error(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(
            success=False, error_code="EXPORT_RENDER_FAILED",
            cause="shader compile"))
        done = threading.Event()
        received = {}

        def on_error(msg: str) -> None:
            received["msg"] = msg
            done.set()

        exporter = VideoExporter()
        try:
            exporter.export(project=object(), sequence=object(),
                            config=make_config(),
                            on_error=on_error)
            assert _pump_until(qapp, done), "error 未在 5s 内回调"
            assert received["msg"] == "EXPORT_RENDER_FAILED: shader compile"
            assert exporter.is_exporting is False
        finally:
            exporter.cleanup()

    def test_second_export_while_running_is_ignored(
            self, qapp, install_fake_exporter) -> None:
        install_fake_exporter.set_outcome(FakeOutcome(success=True))
        exporter = VideoExporter()
        try:
            exporter._is_exporting = True  # 模拟导出进行中
            exporter.export(project=object(), sequence=object(),
                            config=make_config("/o/x.topos"))
            # 工厂未被调用、状态未被覆盖
            assert install_fake_exporter.created == []
            assert exporter._is_exporting is True
        finally:
            exporter._is_exporting = False
            exporter.cleanup()

    def test_cancel_pause_resume_forward_to_worker(self, qapp) -> None:
        exporter = VideoExporter()
        try:
            exporter.pause()
            assert exporter._worker._pause_requested is True
            exporter.cancel()
            assert exporter._worker._cancel_requested is True
            assert exporter._worker._pause_requested is False
            exporter.resume()
            assert exporter._worker._pause_requested is False
        finally:
            exporter.cleanup()

    def test_cleanup_is_idempotent(self, qapp) -> None:
        exporter = VideoExporter()
        exporter.cleanup()
        exporter.cleanup()  # 二次调用不应抛异常
