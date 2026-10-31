"""Regression tests for audit batch-A fixes (w3-g8).

Covers three Critical findings:

- AUD-20900: paintGL must not alias the render-thread pool texture
  (OutputSlotPool) into ``_current_tex_id`` — ``close_source`` ->
  ``clear_texture`` would otherwise delete a texture owned by the pool.
- AUD-20901: the CPU fallback upload paths must never write into (or
  redefine) the shared pool texture, even when dimensions match.
- AUD-13500: ``complete_host_export`` referenced the undefined name
  ``subtitle_duration_frames`` and raised NameError on every call.

The GL entry points are monkeypatched on the module-level ``_gl`` wrapper so
the ownership logic can be exercised headlessly without a GL context.
"""
from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtGui import QColor, QImage

import src.features.edit.ui.source_viewer as sv
from src.features.edit.ui.source_viewer import SourceViewerWidget

POOL_TEX_ID = 101  # simulates a texture owned by the render-thread OutputSlotPool
NEW_TEX_ID = 777   # texture the viewer allocates for itself


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _GLRecorder:
    """Records delete/bind/upload calls issued through the module _gl wrapper."""

    def __init__(self, monkeypatch) -> None:
        self.deleted: list[int] = []
        self.bound: list[int] = []
        self.sub_uploaded: list[int] = []
        self.tex_imaged: list[int] = []
        self.generated: list[int] = []

        monkeypatch.setattr(sv._gl, "glDeleteTextures", self._delete)
        monkeypatch.setattr(sv._gl, "glBindTexture", self._bind)
        monkeypatch.setattr(sv._gl, "glTexSubImage2D", self._sub_image)
        monkeypatch.setattr(sv._gl, "glTexImage2D", self._tex_image)
        monkeypatch.setattr(sv._gl, "glGenTextures", self._gen_textures)
        monkeypatch.setattr(sv._gl, "glTexParameteri", lambda *a, **k: None)
        monkeypatch.setattr(sv._gl, "glClear", lambda *a, **k: None)

    def _delete(self, ids) -> None:
        self.deleted.extend(ids)

    def _bind(self, target, tex_id) -> None:
        self.bound.append(tex_id)

    def _sub_image(self, *args, **kwargs) -> None:
        # args: target, level, x, y, w, h, format, type, pixels — bound via
        # the recorded glBindTexture call that precedes this one.
        self.sub_uploaded.append(self.bound[-1] if self.bound else None)

    def _tex_image(self, *args, **kwargs) -> None:
        self.tex_imaged.append(self.bound[-1] if self.bound else None)

    def _gen_textures(self, n: int):
        ids = [NEW_TEX_ID + len(self.generated) + i for i in range(n)]
        self.generated.extend(ids)
        return ids[0] if n == 1 else ids


def _display_gpu_frame(widget: SourceViewerWidget, tex_id: int = POOL_TEX_ID) -> None:
    """Simulate an accepted GPU present frame (shared pool texture on display)."""
    gen = widget._gpu_generation
    widget._gpu_expected_frame = 49
    assert widget.set_gpu_frame(tex_id, 320, 180, frame=49, generation=gen, slot_id=0) is True
    assert widget.viewer._shared_tex_id == tex_id


def _make_qimage(width: int = 320, height: int = 180) -> QImage:
    img = QImage(width, height, QImage.Format.Format_RGB888)
    img.fill(QColor(30, 40, 50))
    return img


# --------------------------------------------------------------------------- #
# AUD-20900 — pool texture must never be aliased into _current_tex_id
# --------------------------------------------------------------------------- #


def test_paintgl_does_not_alias_pool_texture_into_owned_field(qapp, monkeypatch) -> None:
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)

    _display_gpu_frame(widget)
    viewer._gl_initialized = True
    viewer.paintGL()

    # Display dimensions propagate, but the pool texture ID is referenced
    # read-only: _current_tex_id stays reserved for viewer-owned textures.
    assert viewer._tex_width == 320 and viewer._tex_height == 180
    assert viewer._current_tex_id is None
    assert viewer._display_tex_id() == POOL_TEX_ID


def test_close_source_does_not_delete_render_pool_texture(qapp, monkeypatch) -> None:
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)

    acked: list[int] = []
    widget.set_gpu_slot_ack_callback(acked.append)
    _display_gpu_frame(widget)

    viewer._gl_initialized = True
    viewer.paintGL()

    widget._current_asset = object()
    widget.close_source()

    # The pool texture is released via the slot-ack protocol, never via
    # glDeleteTextures from the UI thread.
    assert recorder.deleted == []
    assert POOL_TEX_ID not in recorder.deleted
    assert acked == [0]  # displayed slot released back to the pool
    assert viewer._shared_tex_id is None


def test_clear_texture_deletes_only_viewer_owned_texture(qapp, monkeypatch) -> None:
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)

    # CPU path allocates a viewer-owned texture.
    viewer._gl_initialized = True
    viewer.set_texture_from_qimage_with_display_size(_make_qimage(), 320, 180)
    assert viewer._current_tex_id == NEW_TEX_ID

    viewer.clear_texture()
    assert recorder.deleted == [NEW_TEX_ID]
    assert viewer._current_tex_id is None


def test_clear_texture_skips_delete_if_id_still_aliases_shared(qapp, monkeypatch) -> None:
    """Defensive guard: even with a stale alias, clear_texture must not delete it."""
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)

    _display_gpu_frame(widget)
    # Simulate the pre-fix alias state directly to exercise the guard.
    viewer._current_tex_id = POOL_TEX_ID

    viewer.clear_texture()
    assert recorder.deleted == []


# --------------------------------------------------------------------------- #
# AUD-20901 — CPU fallback must upload into viewer-owned textures only
# --------------------------------------------------------------------------- #


def test_cpu_fallback_upload_never_writes_pool_texture(qapp, monkeypatch) -> None:
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)

    _display_gpu_frame(widget)
    viewer._gl_initialized = True
    viewer.paintGL()
    assert viewer._tex_width == 320 and viewer._tex_height == 180

    # GPU watchdog timeout -> CPU fallback decodes 320x180 (same size as the
    # pool texture: the exact size-match trap from the audit).
    viewer.set_texture_from_qimage_with_display_size(_make_qimage(), 320, 180)

    assert POOL_TEX_ID not in recorder.bound
    assert recorder.sub_uploaded == []
    assert recorder.tex_imaged == [NEW_TEX_ID]
    assert viewer._current_tex_id == NEW_TEX_ID
    assert viewer._current_tex_id != viewer._shared_tex_id


def test_set_texture_from_qimage_redefines_owned_not_pool(qapp, monkeypatch) -> None:
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)

    _display_gpu_frame(widget)
    viewer._gl_initialized = True
    # Simulate the pre-fix alias state directly to exercise the guard in
    # set_texture_from_qimage (glTexImage2D redefine path).
    viewer._current_tex_id = POOL_TEX_ID

    viewer.set_texture_from_qimage(_make_qimage())

    assert POOL_TEX_ID not in recorder.bound
    assert recorder.tex_imaged == [NEW_TEX_ID]
    assert viewer._current_tex_id == NEW_TEX_ID


def test_cpu_upload_reuses_viewer_owned_texture_on_size_match(qapp, monkeypatch) -> None:
    """Legit reuse still works: same viewer-owned texture, glTexSubImage2D."""
    widget = SourceViewerWidget()
    viewer = widget.viewer
    recorder = _GLRecorder(monkeypatch)
    viewer._gl_initialized = True

    viewer.set_texture_from_qimage_with_display_size(_make_qimage(), 320, 180)
    assert viewer._current_tex_id == NEW_TEX_ID
    first_generated = list(recorder.generated)

    # Second frame, same size, no shared texture involved -> in-place update.
    viewer.set_texture_from_qimage_with_display_size(_make_qimage(), 320, 180)
    assert recorder.generated == first_generated  # no new allocation
    assert recorder.sub_uploaded == [NEW_TEX_ID]
    assert viewer._current_tex_id == NEW_TEX_ID


# --------------------------------------------------------------------------- #
# AUD-13500 — complete_host_export NameError
# --------------------------------------------------------------------------- #


def test_complete_host_export_forwards_durations_without_name_error(monkeypatch) -> None:
    """The brand timeline duration must flow from the method's own parameter.

    Pre-fix, evaluating the finalize kwargs raised
    ``NameError: subtitle_duration_frames`` on every call.
    """
    import src.features.edit.ai_editing.export_service as es

    service = es.ExportService()
    job = service.jobs.submit(es.JobKind.EXPORT, "test-export")

    artifacts = SimpleNamespace(
        succeeded=True,
        manifest_path="/tmp/m.json",
        thumbnail_path="/tmp/t.png",
        error_message="",
        error_code="",
    )
    monkeypatch.setattr(es, "materialize_export_artifacts", lambda *a, **k: artifacts)

    recorded: dict = {}

    def fake_finalize(j, manifest, probe, **kwargs) -> bool:
        recorded.update(kwargs)
        return True

    monkeypatch.setattr(service, "finalize_export_with_probe", fake_finalize)

    accepted, msg = service.complete_host_export(
        job,
        "/tmp/out.mov",
        probe=object(),
        subtitle_timeline_duration_frames=250,
        brand_timeline_duration_frames=240,
    )

    assert accepted is True
    assert msg == ""
    assert recorded["brand_timeline_duration_frames"] == 240
    assert recorded["subtitle_timeline_duration_frames"] == 250


def test_complete_host_export_end_to_end_reaches_quality_gate(monkeypatch) -> None:
    """Full path (real finalize_export_with_probe) with a failing probe stub.

    Pre-fix this raised NameError before the quality gate could ever run.
    """
    import src.features.edit.ai_editing.export_service as es

    service = es.ExportService()
    job = service.jobs.submit(es.JobKind.EXPORT, "test-export")

    artifacts = SimpleNamespace(
        succeeded=True,
        manifest_path="/tmp/m.json",
        thumbnail_path="/tmp/t.png",
        error_message="",
        error_code="",
    )
    monkeypatch.setattr(es, "materialize_export_artifacts", lambda *a, **k: artifacts)

    class _FailingProbe:
        @staticmethod
        def probe_and_evaluate(output_path, **kwargs):
            result = SimpleNamespace(succeeded=False, error_code="PROBE_XX")
            return result, None

    accepted, msg = service.complete_host_export(
        job,
        "/tmp/out.mov",
        probe=_FailingProbe(),
        brand_timeline_duration_frames=240,
        subtitle_timeline_duration_frames=240,
    )

    assert accepted is False
    assert msg  # job error message surfaced
    assert job.status.value == "failed"
