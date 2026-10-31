from __future__ import annotations

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.render.render_worker import RenderWorker


def test_svg_render_cancel_restart_soak_has_no_stale_progress() -> None:
    scene = TeachingSceneDocument.create_default(
        "cancel restart soak", scene_id="cancel-restart-soak", width=64, height=48, duration_frames=12,
    )
    context = RenderContext(fps=24, width=64, height=48)
    worker = RenderWorker(max_workers=1)
    try:
        for iteration in range(100):
            progress_jobs: list[str] = []

            def cancel_after_first(progress: object) -> bool:
                job_id = getattr(progress, "job_id", "")
                progress_jobs.append(job_id)
                return True

            cancelled = worker.submit(scene, range(scene.duration_frames), context, cancel_after_first)
            result = cancelled.result(timeout=10)
            assert result.cancelled or len(result.frames) < scene.duration_frames
            assert progress_jobs and len(set(progress_jobs)) == 1

            restarted = worker.submit(scene, (iteration % 4,), context)
            restarted_result = restarted.result(timeout=10)
            assert restarted_result.frames[0].frame == iteration % 4
            assert restarted_result.frames[0].data
    finally:
        worker.shutdown()
