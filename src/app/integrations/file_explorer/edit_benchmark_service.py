"""Qt-side host bridge for the Edit decode/render benchmark.

The file-explorer plugin runs in the same Qt process as the editor, but its
Flask routes are served by a worker thread.  This service is therefore the
small, explicit boundary between the plugin window and the Edit page:

* the plugin only asks for a session and reads JSON-safe snapshots;
* playback is started/stopped on the Qt thread which owns the Edit widgets;
* frame counts come from ``TimelinePreviewRenderer.frame_ready`` (the actual
  async render/present completion signal), with an explicit engine-signal
  fallback only for older hosts;
* timing and source-path details come from the renderer's existing telemetry.

No OpenGL object is created or touched here.  The renderer remains the sole
owner of its GL context and GPU resources.
"""

from __future__ import annotations

import time
import uuid
import weakref
from typing import Any, Optional

from PyQt6.QtCore import QCoreApplication, QObject, QThread, QTimer, pyqtSignal


def _json_safe(value: Any) -> Any:
    """Convert Qt/numeric containers into values safe for plugin JSON/UI use."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    # Rational and other lightweight model values are represented by their
    # textual form rather than leaking a project object into the plugin.
    return str(value)


class EditBenchmarkHostService(QObject):
    """Expose the live Edit playback pipeline as a safe benchmark service."""

    snapshot_changed = pyqtSignal(object)
    completed = pyqtSignal(object)

    def __init__(self, main_window: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent or main_window)
        self._main_window_ref = weakref.ref(main_window)
        self._session: Optional[dict[str, Any]] = None
        self._renderer: Any = None
        self._engine: Any = None
        self._panel: Any = None
        self._renderer_connected = False
        self._engine_connected = False
        self._panel_connected = False
        self._trace_collector: Any = None
        self._finishing = False
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------------
    # Host discovery / read-only snapshot
    # ------------------------------------------------------------------

    def _host(self) -> Any:
        try:
            return self._main_window_ref()
        except RuntimeError:
            return None

    def _edit_objects(self) -> tuple[Any, Any, Any]:
        host = self._host()
        renderer = getattr(host, "timeline_preview_renderer", None) if host else None
        engine = getattr(renderer, "realtime_engine", None) if renderer else None
        panel = getattr(host, "timeline_panel", None) if host else None
        return renderer, engine, panel

    def _on_qt_thread(self) -> bool:
        """Guard the host boundary against accidental Flask-thread calls."""

        try:
            return QThread.currentThread() == self.thread()
        except RuntimeError:
            return False

    def is_available(self) -> bool:
        """Return whether an active Edit timeline and realtime engine exist."""

        renderer, engine, panel = self._edit_objects()
        return renderer is not None and engine is not None and panel is not None

    @staticmethod
    def _fps_value(value: Any, default: float = 24.0) -> float:
        try:
            converter = getattr(value, "to_float", None)
            if callable(converter):
                return float(converter())
            return float(value)
        except (TypeError, ValueError):
            return default

    def _timeline_snapshot(self) -> dict[str, Any]:
        host = self._host()
        edit_page = getattr(host, "edit_page", None) if host else None
        timeline = getattr(edit_page, "_current_timeline", None) if edit_page else None
        panel = getattr(host, "timeline_panel", None) if host else None
        if timeline is None:
            view = getattr(panel, "timeline_view", None) if panel else None
            timeline = getattr(view, "_timeline", None) if view else None

        width = int(getattr(timeline, "width", 0) or 0)
        height = int(getattr(timeline, "height", 0) or 0)
        fps = self._fps_value(getattr(timeline, "fps", getattr(panel, "_fps", 24.0)))
        duration = int(getattr(timeline, "duration_frames", 0) or 0)
        if duration <= 0:
            duration = int(getattr(panel, "_total_frames", 0) or 0)
        return {
            "id": str(getattr(timeline, "timeline_id", "") or ""),
            "name": str(getattr(timeline, "name", "") or ""),
            "width": width,
            "height": height,
            "fps": fps,
            "duration_frames": duration,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe live view of Edit telemetry and session state."""

        if not self._on_qt_thread():
            return {"available": False, "error": "Edit host service 只能在 Qt 主线程读取"}
        renderer, engine, panel = self._edit_objects()
        if renderer is None or engine is None or panel is None:
            return {"available": False, "error": "Edit 页面或实时渲染管线尚未就绪"}

        try:
            engine_stats = engine.stats() if callable(getattr(engine, "stats", None)) else {}
        except Exception as exc:  # pragma: no cover - defensive host boundary
            engine_stats = {"error": str(exc)}
        telemetry = engine_stats.get("telemetry", {}) if isinstance(engine_stats, dict) else {}
        if not telemetry:
            try:
                telemetry = engine.telemetry.summary()
            except Exception:
                telemetry = {}
        try:
            gpu_stats = renderer.gpu_pipeline_stats()
        except Exception:
            gpu_stats = {}
        trace_summary: dict[str, Any] = {}
        if self._session is not None:
            cached_trace = self._session.get("trace_summary")
            if isinstance(cached_trace, dict):
                trace_summary = cached_trace
        if self._trace_collector is not None:
            try:
                trace_summary = self._trace_collector.get_summary()
            except Exception:
                trace_summary = {}
        try:
            controller = panel.get_playback_controller()
            playing = bool(getattr(controller, "is_playing", False))
            current_frame = int(getattr(controller, "current_frame", 0) or 0)
            speed = float(getattr(controller, "playback_speed", 1.0) or 1.0)
            direction = getattr(getattr(controller, "playback_direction", None), "value", 1)
        except Exception:
            playing, current_frame, speed, direction = False, 0, 1.0, 1

        session = dict(self._session or {})
        if session and isinstance(gpu_stats, dict):
            compositor = gpu_stats.get("compositor")
            baseline_gpu = session.get("gpu_baseline")
            if isinstance(compositor, dict) and isinstance(baseline_gpu, dict):
                for key in ("gpu_composite_frames", "cpu_qimage_frames", "submitted"):
                    current = int(compositor.get(key, 0) or 0)
                    baseline = int(baseline_gpu.get(key, 0) or 0)
                    session[f"{key}_delta"] = max(0, current - baseline)
        if session:
            started_at = float(session.get("started_at") or 0.0)
            if started_at:
                session["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000.0, 2)
            session["playing"] = playing
            session["current_frame"] = current_frame
            baseline = session.get("engine_baseline", {})
            if isinstance(engine_stats, dict):
                for key in ("total_requests", "total_presented", "total_misses", "total_holds"):
                    session[f"{key[6:]}_delta"] = max(
                        0,
                        int(engine_stats.get(key, 0) or 0) - int(baseline.get(key, 0) or 0),
                    )
            measured = int(session.get("presented_frames", 0) or 0)
            elapsed_s = max(float(session.get("elapsed_ms", 0.0) or 0.0) / 1000.0, 1e-9)
            session["presented_fps"] = round(measured / elapsed_s, 2) if measured else 0.0

        invariants_ok, invariants_message = True, "not checked"
        try:
            invariants_ok, invariants_message = engine.telemetry.assert_realtime_invariants()
        except Exception:
            pass

        snapshot = {
            "available": True,
            "timeline": self._timeline_snapshot(),
            "playback": {
                "playing": playing,
                "current_frame": current_frame,
                "speed": speed,
                "direction": direction,
            },
            "engine": {
                key: engine_stats.get(key)
                for key in (
                    "state",
                    "mode",
                    "generation",
                    "total_requests",
                    "total_presented",
                    "total_misses",
                    "total_holds",
                    "consecutive_misses",
                )
                if isinstance(engine_stats, dict) and key in engine_stats
            },
            "telemetry": _json_safe(telemetry),
            "gpu": _json_safe(gpu_stats),
            "frame_trace": _json_safe(trace_summary),
            "invariants": {"ok": bool(invariants_ok), "message": str(invariants_message)},
            "session": _json_safe(session),
        }
        return snapshot

    # ------------------------------------------------------------------
    # Session lifecycle (always called on the Qt thread)
    # ------------------------------------------------------------------

    def _disconnect_signals(self) -> None:
        if self._renderer is not None and self._renderer_connected:
            try:
                self._renderer.frame_ready.disconnect(self._on_frame_ready)
            except (TypeError, RuntimeError):
                pass
        if self._engine is not None and self._engine_connected:
            try:
                self._engine.frame_presented.disconnect(self._on_frame_presented)
            except (TypeError, RuntimeError):
                pass
        if self._panel is not None and self._panel_connected:
            controller = None
            try:
                controller = self._panel.get_playback_controller()
            except Exception:
                pass
            if controller is not None:
                try:
                    controller.state_changed.disconnect(self._on_playback_state_changed)
                except (TypeError, RuntimeError):
                    pass
        self._renderer_connected = False
        self._engine_connected = False
        self._panel_connected = False
        self._renderer = None
        self._engine = None
        self._panel = None
        self._trace_collector = None

    def start_session(self, frame_count: int = 120, warmup_frames: int = 8) -> dict[str, Any]:
        """Start a measured forward playback session on the active timeline."""

        if not self._on_qt_thread():
            return {"ok": False, "error": "Edit host service 只能在 Qt 主线程启动"}
        if self._session is not None and self._session.get("phase") not in {"completed", "cancelled"}:
            return {"ok": False, "error": "已有 Edit 跑分正在进行"}
        renderer, engine, panel = self._edit_objects()
        if renderer is None or engine is None or panel is None:
            return {"ok": False, "error": "Edit 页面或实时渲染管线尚未就绪"}
        try:
            controller = panel.get_playback_controller()
        except Exception:
            controller = None
        if controller is None:
            return {"ok": False, "error": "无法取得时间线播放控制器"}
        if bool(getattr(controller, "is_playing", False)):
            return {"ok": False, "error": "请先暂停时间线，再开始 Edit 跑分"}
        timeline_info = self._timeline_snapshot()
        if int(timeline_info.get("duration_frames", 0) or 0) <= 0:
            return {"ok": False, "error": "当前时间线没有可播放的视频帧"}

        requested = max(1, min(int(frame_count), 10000))
        warmup = max(0, min(int(warmup_frames), 240))
        try:
            baseline = engine.stats()
            engine.telemetry.reset()
        except Exception:
            baseline = {}
        try:
            gpu_stats = renderer.gpu_pipeline_stats()
            compositor_stats = gpu_stats.get("compositor", {}) if isinstance(gpu_stats, dict) else {}
        except Exception:
            compositor_stats = {}
        try:
            from src.shared.video.playback_frame_trace import get_trace_collector

            trace_collector = get_trace_collector()
            trace_collector.start_session(page="edit_benchmark")
        except Exception:
            trace_collector = None
        self._session = {
            "id": uuid.uuid4().hex,
            "phase": "warmup" if warmup else "measuring",
            "requested_frames": requested,
            "warmup_frames": warmup,
            "warmup_presented": 0,
            "presented_frames": 0,
            "started_at": None if warmup else time.perf_counter(),
            "elapsed_ms": 0.0,
            "engine_baseline": baseline if isinstance(baseline, dict) else {},
            "gpu_baseline": compositor_stats if isinstance(compositor_stats, dict) else {},
            "current_frame": int(getattr(controller, "current_frame", 0) or 0),
            "timeline": timeline_info,
        }
        self._renderer, self._engine, self._panel = renderer, engine, panel
        self._trace_collector = trace_collector
        frame_signal_source = "renderer.frame_ready"
        try:
            renderer.frame_ready.connect(self._on_frame_ready)
            self._renderer_connected = True
        except (AttributeError, TypeError):
            # Compatibility for a host renderer predating frame_ready.  This
            # path is explicitly labelled in the report because the engine
            # signal means "submitted" on those older builds.
            try:
                engine.frame_presented.connect(self._on_frame_presented)
                self._engine_connected = True
                frame_signal_source = "engine.frame_presented_fallback"
            except (AttributeError, TypeError):
                if trace_collector is not None:
                    try:
                        trace_collector.end_session()
                    except Exception:
                        pass
                self._session = None
                return {"ok": False, "error": "实时渲染器不支持 frame_ready 呈现信号"}
        self._session["frame_signal_source"] = frame_signal_source
        try:
            controller.state_changed.connect(self._on_playback_state_changed)
            self._panel_connected = True
        except (AttributeError, TypeError):
            self._panel_connected = False
        QTimer.singleShot(0, lambda: panel.start_playback(direction=1, speed=1.0))
        return {"ok": True, "snapshot": self.snapshot()}

    def _on_rendered_frame(self, _timeline_frame: int) -> None:
        session = self._session
        if not session or self._finishing:
            return
        phase = str(session.get("phase", ""))
        if phase == "warmup":
            session["warmup_presented"] = int(session.get("warmup_presented", 0)) + 1
            if session["warmup_presented"] >= int(session.get("warmup_frames", 0)):
                try:
                    self._engine.telemetry.reset()
                except Exception:
                    pass
                session["phase"] = "measuring"
                session["started_at"] = time.perf_counter()
        elif phase == "measuring":
            session["presented_frames"] = int(session.get("presented_frames", 0)) + 1
            if session["presented_frames"] >= int(session.get("requested_frames", 1)):
                self._finish_session(cancelled=False)
        # The plugin polls at 100 ms.  Avoid building a full telemetry/GL
        # statistics snapshot for every frame because that would perturb the
        # playback timing being measured.

    def _on_frame_ready(self, timeline_frame: int, _result: object) -> None:
        """Count asynchronous renderer output, not queue submission."""

        self._on_rendered_frame(timeline_frame)

    def _on_frame_presented(self, timeline_frame: int) -> None:
        """Compatibility counter for old renderers without ``frame_ready``."""

        self._on_rendered_frame(timeline_frame)

    def _on_playback_state_changed(self, state: Any) -> None:
        if self._finishing or not self._session:
            return
        if self._session.get("phase") not in {"warmup", "measuring"}:
            return
        state_value = str(getattr(state, "value", state)).lower()
        if state_value in {"paused", "stopped", "idle", "shutdown"}:
            # A short timeline may reach its end before requested_frames. Keep
            # the partial result instead of leaving the plugin stuck forever.
            self._finish_session(cancelled=False)

    def _finish_session(self, cancelled: bool) -> None:
        if self._finishing or self._session is None:
            return
        self._finishing = True
        session = self._session
        session["phase"] = "cancelled" if cancelled else "draining"
        try:
            if self._panel is not None:
                self._panel.pause_playback()
        except Exception:
            pass
        # Render worker trace enrichment can arrive just after frame_presented;
        # let the in-flight GPU jobs finish before taking the final snapshot.
        QTimer.singleShot(200, self._finalize_session)

    def _finalize_session(self) -> None:
        if self._session is None:
            self._finishing = False
            return
        session = self._session
        if session.get("started_at"):
            session["elapsed_ms"] = round(
                (time.perf_counter() - float(session["started_at"])) * 1000.0, 2
            )
        session["phase"] = "cancelled" if session.get("phase") == "cancelled" else "completed"
        if self._trace_collector is not None:
            try:
                self._trace_collector.end_session()
                trace_summary = self._trace_collector.get_summary()
                session["trace_summary"] = trace_summary
            except Exception:
                pass
        self._disconnect_signals()
        self._finishing = False
        snapshot = self.snapshot()
        self.snapshot_changed.emit(snapshot)
        self.completed.emit(snapshot)

    def stop_session(self) -> dict[str, Any]:
        """Stop an active session and return its final/partial snapshot."""

        if not self._on_qt_thread():
            return {"ok": False, "error": "Edit host service 只能在 Qt 主线程停止"}
        if self._session is None:
            return {"ok": True, "snapshot": self.snapshot()}
        if not self._finishing:
            self._finish_session(cancelled=True)
        return {"ok": True, "snapshot": self.snapshot()}

    def shutdown(self) -> None:
        """Stop playback and release signal connections during app shutdown."""

        if self._session is not None:
            try:
                if self._panel is not None:
                    self._panel.pause_playback()
            except Exception:
                pass
        if self._trace_collector is not None:
            try:
                self._trace_collector.end_session()
            except Exception:
                pass
        self._disconnect_signals()
        self._session = None
        self._finishing = False


__all__ = ["EditBenchmarkHostService"]
