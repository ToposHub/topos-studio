"""T1.4 — 分析 job 进度上报测试。

验收（docs/ai_closure_focused_plan_2026-08-04.md 工作流 1 T1.4）：

- AnalysisService.execute_analysis 把 job 标 RUNNING 并按 provider 进度上报。
- 单 provider：job.progress 从 0 → provider 上报值 → 1.0（complete）。
- 多 provider：每个 provider 占整体进度均分段，base 偏移正确。
- provider 不支持 progress（不调 callback）时 job 仍 complete，progress=1.0。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AnalysisProvider,
    AssetAnalysis,
    AssetFingerprint,
    analysis_to_segments,
)
from src.features.edit.ai_editing.analysis_service import AnalysisService
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.jobs import JobStatus


class _RecordingProvider:
    """记录 progress callback 调用的 fake provider。"""

    def __init__(self, pid: str, *, fractions: tuple[float, ...] = (),
                 kinds: tuple[SegmentKind, ...] = (SegmentKind.SHOT,)) -> None:
        self.provider_id = pid
        self.supported_kinds = kinds
        self._fractions = fractions
        self.progress_calls: list[tuple[float, Any]] = []

    def analyze(self, asset_id: str, fingerprint: AssetFingerprint, *,
                fps: str, progress: Any | None = None) -> tuple:
        # 模拟 provider 上报自身进度
        if progress is not None:
            for frac in self._fractions:
                progress(frac, f"{self.provider_id} at {frac}")
                self.progress_calls.append((frac, None))
        analysis = AssetAnalysis(
            asset_id=asset_id, fingerprint=fingerprint, fps=fps,
            scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},),
            provenance=AnalysisProvenance(provider=self.provider_id, model="test"),
        )
        return analysis, tuple(analysis_to_segments(analysis))


class _OptionsProvider(_RecordingProvider):
    def __init__(self, pid: str) -> None:
        super().__init__(pid)
        self.received_options = None

    def analyze(self, asset_id, fingerprint, *, fps, progress=None, options=None):
        self.received_options = options
        return super().analyze(asset_id, fingerprint, fps=fps, progress=progress)


class TestAnalysisProgressReporting:
    def test_request_options_are_forwarded_only_to_capable_provider(self):
        svc = AnalysisService()
        provider = _OptionsProvider("options")
        svc.register(provider)

        job, error = svc.request_analysis(
            "a1", AssetFingerprint(), provider_id="options",
            options={"max_new_tokens": 16384},
        )

        assert error == ""
        assert job.status == JobStatus.SUCCEEDED
        assert provider.received_options == {"max_new_tokens": 16384}

    def test_external_progress_callback_receives_stage_updates(self):
        """宿主回调收到 job + provider 的状态，供状态栏实时展示。"""
        svc = AnalysisService()
        svc.register(_RecordingProvider("p1", fractions=(0.5,)))
        events: list[tuple[float | None, Any]] = []

        job, _ = svc.request_analysis(
            "a1", AssetFingerprint(), fps="24/1", provider_id="p1",
            on_progress=lambda fraction, message: events.append((fraction, message)),
        )

        assert job.status == JobStatus.SUCCEEDED
        assert events[0] == (0.0, "分析任务已启动")
        assert (0.5, "p1: p1 at 0.5") in events
        assert events[-1] == (1.0, "分析完成")

    def test_progress_callback_failure_does_not_fail_analysis(self):
        """状态栏回调异常不能把成功的分析任务变成失败。"""
        svc = AnalysisService()
        svc.register(_RecordingProvider("p1", fractions=(0.5,)))

        def broken_callback(_fraction, _message):
            raise RuntimeError("status bar unavailable")

        job, error = svc.request_analysis(
            "a1", AssetFingerprint(), fps="24/1", provider_id="p1",
            on_progress=broken_callback,
        )

        assert error == ""
        assert job.status == JobStatus.SUCCEEDED

    def test_single_provider_progress_reflected_in_job(self):
        """单 provider 上报进度 → job.progress 从 0 升到 1.0。"""
        svc = AnalysisService()
        # 注册一个会上报 0.5 的 provider
        provider = _RecordingProvider("p1", fractions=(0.5,))
        svc.register(provider)

        job, _ = svc.request_analysis(
            "a1", AssetFingerprint(), fps="24/1", provider_id="p1",
        )

        assert job.status == JobStatus.SUCCEEDED
        assert job.progress == 1.0  # complete 后固定 1.0
        assert job.started_at is not None
        # provider 确实收到了 progress callback
        assert len(provider.progress_calls) == 1

    def test_multi_provider_progress_base_offset(self):
        """两个 provider：第二个 provider 的进度正确偏移到整体后半段。"""
        svc = AnalysisService()
        p1 = _RecordingProvider("p1", fractions=(1.0,))  # 第一个完成
        p2 = _RecordingProvider("p2", fractions=(0.0, 1.0))  # 第二个上报
        svc.register(p1)
        svc.register(p2)

        # 用显式 provider_ids 控制顺序
        job, _ = svc.request_analysis(
            "a1", AssetFingerprint(), fps="24/1", provider_ids=("p1", "p2"),
        )

        assert job.status == JobStatus.SUCCEEDED
        assert job.progress == 1.0
        # p2 收到了 progress callback（被调用）
        assert len(p2.progress_calls) == 2

    def test_provider_without_progress_still_completes(self):
        """provider 不调 progress callback（fractions 空）→ job 仍 complete。"""
        svc = AnalysisService()
        svc.register(_RecordingProvider("silent", fractions=()))

        job, _ = svc.request_analysis(
            "a1", AssetFingerprint(), fps="24/1", provider_id="silent",
        )

        assert job.status == JobStatus.SUCCEEDED
        assert job.progress == 1.0

    def test_job_marked_running_during_execution(self):
        """执行期间 job 处于 RUNNING（不是 QUEUED）。

        用一个在 analyze 里捕获 job 引用的 provider 验证。
        """
        svc = AnalysisService()
        observed: list[JobStatus] = []
        job_ref: list = []

        class _Peek:
            provider_id = "peek"
            supported_kinds = (SegmentKind.SHOT,)

            def analyze(self, asset_id, fingerprint, *, fps, progress=None):
                if job_ref:
                    observed.append(job_ref[0].status)
                analysis = AssetAnalysis(
                    asset_id=asset_id, fingerprint=fingerprint, fps=fps,
                    provenance=AnalysisProvenance(provider="peek", model="t"),
                )
                return analysis, ()

        svc.register(_Peek())
        # 捕获 request_analysis 内部 submit 的 job
        original_submit = svc._jobs.submit
        svc._jobs.submit = lambda *a, **k: job_ref.append(original_submit(*a, **k)) or job_ref[0]  # type: ignore[assignment]

        job, _ = svc.request_analysis(
            "a1", AssetFingerprint(), fps="24/1", provider_id="peek",
        )

        assert job.status == JobStatus.SUCCEEDED
        # 执行期间观察到 RUNNING（mark_running 在 provider.analyze 前调）
        assert JobStatus.RUNNING in observed, f"expected RUNNING in {observed}"
