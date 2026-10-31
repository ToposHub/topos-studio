"""M10.3 · 编译输出字段消费者 + 入队快照 revision 校验。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.deliver_core


class TestSubtitleLanguageField:
    def test_from_flat_config_carries_language(self):
        from src.features.deliver.core.video_export_job import (
            SubtitleConfig,
            SubtitleMode,
            VideoExportJob,
        )
        job = VideoExportJob.from_flat_config({
            "project_id": "p", "output_path": "/out/v.mp4",
            "subtitle_language": "zh-CN",
            "subtitle_mode": SubtitleMode.SIDECAR,
        })
        assert job.subtitle_config.language == "zh-CN"
        assert job.subtitle_config.mode == SubtitleMode.SIDECAR
        # 快照 dict 往返（队列持久化链）
        restored = VideoExportJob.from_dict(job.to_dict())
        assert restored.subtitle_config.language == "zh-CN"

    def test_default_language_empty(self):
        from src.features.deliver.core.video_export_job import VideoExportJob
        job = VideoExportJob.from_flat_config({})
        assert job.subtitle_config.language == ""


class TestRevisionStaleGate:
    def _controller(self):
        from src.features.deliver.core.video_batch_export_controller import (
            VideoBatchExportController,
        )
        ctrl = VideoBatchExportController.__new__(VideoBatchExportController)
        ctrl._active_project = NS(revision=7)
        ctrl._active_project_id = "p1"
        ctrl._active_project_revision = 7
        ctrl._sequences_by_id = {"t1": NS(timeline_id="t1")}
        return ctrl

    def _job(self, revision):
        from src.features.deliver.core.video_export_job import VideoExportJob
        return VideoExportJob(project_id="p1", timeline_id="t1",
                              project_revision=revision)

    def test_stale_snapshot_refused_with_reason(self):
        ctrl = self._controller()
        ctx = ctrl._resolve_context(self._job(revision=5))
        assert ctx.project is None
        assert "快照过期" in ctx.reason
        assert "5" in ctx.reason and "7" in ctx.reason

    def test_matching_revision_resolves(self):
        ctrl = self._controller()
        ctx = ctrl._resolve_context(self._job(revision=7))
        assert ctx.is_resolvable
        assert ctx.project_revision == 7

    def test_zero_revision_legacy_not_refused(self):
        """旧队列快照（revision=0）向后兼容不拒。"""
        ctrl = self._controller()
        ctx = ctrl._resolve_context(self._job(revision=0))
        assert ctx.is_resolvable
