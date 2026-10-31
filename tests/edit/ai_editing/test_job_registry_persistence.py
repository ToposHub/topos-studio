"""JobRegistry persistence tests.

The AgentLoop/JobRecoveryManager coverage that used to live here was removed
together with those modules (2026-08-19 audit: both were fully implemented but
had zero production callers — AgentLoop was superseded by the V2
AgentOrchestrator pipeline, JobRecoveryManager never got an entry point).
"""

from __future__ import annotations

from pathlib import Path

from src.features.edit.ai_editing.jobs import (
    JobKind,
    JobRegistry,
    JobStatus,
)


def test_job_registry_persist_and_safely_terminate_active_jobs(tmp_path: Path) -> None:
    registry = JobRegistry()
    active = registry.submit(JobKind.GENERATION, label="remote generation")
    active.mark_running()
    active.logs.append("Bearer secret-token /Users/testuser/private/output.mp4")
    done = registry.submit(JobKind.EXPORT, label="done")
    done.mark_running()
    done.complete({"artifact_id": "export-1"})

    path = tmp_path / "ai" / "jobs.json"
    assert registry.persist(path)
    restored = JobRegistry()
    assert restored.restore(path)
    restored_active = restored.get(active.job_id)
    assert restored_active is not None
    assert restored_active.status == JobStatus.FAILED
    assert restored_active.error_code == "JOB_INTERRUPTED"
    assert "secret-token" not in path.read_text(encoding="utf-8")
    assert "/Users/testuser" not in path.read_text(encoding="utf-8")
    restored_done = restored.get(done.job_id)
    assert restored_done is not None
    assert restored_done.status == JobStatus.SUCCEEDED
