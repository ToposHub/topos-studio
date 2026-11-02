"""Clip-owned camera stabilization lifecycle and signal-only palette binding."""
from __future__ import annotations

from typing import Any

import numpy as np
from PyQt6.QtCore import QTimer

from src.shared.i18n.i18n import tr
from src.shared.tracking.stabilization_state import DEFAULT_PARAMS


class StabilizerControllerMixin:
    def _init_stabilizer(self) -> None:
        from src.features.color.tracking.tracking_service import TrackingService
        from src.shared.tracking.camera_motion import CameraMotionAnalyzer
        from src.features.color.tracking.stabilization_plan_service import StabilizationPlanService
        panel = self.tracker_panel.stabilizer
        self.stabilization_service = TrackingService(parent=self)
        self.stabilization_service.set_tracker_factory(CameraMotionAnalyzer)
        self.stabilization_plan_service = StabilizationPlanService(self)
        self.stabilization_plan_service.ready.connect(self._on_stabilization_plan_ready)
        self.stabilization_plan_service.failed.connect(self._on_stabilization_plan_failed)
        self._stabilizer_task: tuple[str, str] | None = None
        self._stabilizer_params_timer = QTimer(self)
        self._stabilizer_params_timer.setSingleShot(True)
        self._stabilizer_params_timer.setInterval(120)
        self._stabilizer_params_timer.timeout.connect(self._recompute_stabilizer)
        panel.analyze_requested.connect(self._start_stabilization)
        panel.cancel_requested.connect(self._cancel_stabilization)
        panel.reset_requested.connect(self._reset_stabilization)
        panel.params_changed.connect(self._on_stabilizer_params_changed)
        panel.interaction_started.connect(self._on_stabilizer_interaction_started)
        panel.interaction_ended.connect(self._on_stabilizer_interaction_ended)
        panel.graph.scrub_started.connect(self._on_tracker_graph_scrub_started)
        panel.graph.frame_clicked.connect(self._on_tracker_graph_frame_clicked)
        panel.graph.scrub_finished.connect(self._on_tracker_graph_scrub_finished)
        self.tracker_panel.palette_mode_changed.connect(self._on_stabilizer_palette_changed)
        service = self.stabilization_service
        service.task_progress.connect(self._on_stabilization_progress)
        service.task_finished.connect(self._on_stabilization_finished)
        service.task_failed.connect(self._on_stabilization_failed)
        service.task_cancelled.connect(self._on_stabilization_cancelled)

    def _stabilizer_fps(self) -> float:
        controller = getattr(self, 'video_controller', None)
        for source in (controller, getattr(controller, '_playback_clock', None),
                       getattr(controller, '_coordinator', None)):
            fps = getattr(source, 'fps', None)
            if isinstance(fps, (int, float)) and np.isfinite(fps) and fps > 0.:
                return float(fps)
        return 25.0

    def _on_stabilizer_palette_changed(self, mode: str) -> None:
        if mode == 'stabilizer':
            self.canvas.hide_tracker_overlay()
            self._sync_stabilizer_panel()
        else:
            layer = self.engine.get_active_layer()
            shape_id = getattr(self, '_tracker_shape_id', None)
            shape = layer.get_mask_item_by_id(shape_id) if layer and shape_id else None
            if shape is not None:
                self._sync_tracker_overlay(shape)

    def _sync_stabilizer_panel(self) -> None:
        panel = self.tracker_panel.stabilizer
        controller = getattr(self, 'video_controller', None)
        available = bool(controller and controller.is_video_loaded())
        panel.set_video_available(available)
        if not available:
            if getattr(self, '_stabilizer_task', None) is not None:
                self._cancel_stabilization()
            self._stabilizer_params_timer.stop()
            panel.set_params(DEFAULT_PARAMS)
            panel.set_motion_series({})
            return
        clip_id, media, current, first, last = self._tracker_frame_context()
        task = getattr(self, '_stabilizer_task', None)
        if task is not None and task != (clip_id, media):
            self.stabilization_service.invalidate()
            self.stabilization_plan_service.invalidate()
            self._stabilizer_task = None
            panel.set_running(False)
        panel.graph.set_frame_range(first, last)
        panel.graph.set_timebase(self._stabilizer_fps())
        panel.graph.set_current_frame(current)
        state = self.engine.get_video_stabilization_state()
        panel.set_params(state.get('params', DEFAULT_PARAMS))
        series: dict[str, list[tuple[int, float]]] = {key: [] for key in ('tx','ty','scale','rotation')}
        for frame, matrix in state.get('transforms', {}).items():
            m = np.asarray(matrix).reshape(3, 3)
            series['tx'].append((int(frame), float(m[0,2])))
            series['ty'].append((int(frame), float(-m[1,2])))
            series['scale'].append((int(frame), float(np.hypot(m[0,0], m[1,0]))))
            series['rotation'].append((int(frame), float(-np.arctan2(m[1,0],m[0,0]))))
        panel.set_motion_series({key: sorted(values) for key, values in series.items()})

    def _start_stabilization(self) -> None:
        from src.features.color.tracking.frame_sampler import AnalysisQuality
        from src.shared.tracking.tracker_2d import TrackDirection
        from src.shared.tracking.types import TrackingMode
        controller = getattr(self, 'video_controller', None)
        if controller is None or not controller.is_video_loaded():
            return
        controller.stop_playback()
        self._stabilizer_params_timer.stop()
        self.stabilization_plan_service.invalidate()
        clip_id, media, _current, first, last = self._tracker_frame_context()
        if not clip_id or not media or last <= first:
            self.tracker_panel.stabilizer.status_label.setText(
                tr('tracker.stabilizer.needs_video', 'At least two video frames are required'))
            self.tracker_panel.stabilizer.status_label.show()
            return
        self._stabilizer_task = (clip_id, media)
        self._stabilizer_analysis_params = self.tracker_panel.stabilizer.get_params()
        self._stabilizer_analysis_range = (first,last)
        size = self.canvas.logical_size
        video_info = controller.get_video_info() if callable(getattr(controller, 'get_video_info', None)) else None
        if isinstance(video_info, dict) and video_info.get('width') and video_info.get('height'):
            self._stabilizer_source_size = (int(video_info['width']), int(video_info['height']))
        else:
            self._stabilizer_source_size = (int(size.width()), int(size.height()))
        mode = {'perspective': TrackingMode.HOMOGRAPHY,
                'similarity': TrackingMode.TRANSLATION_ROTATION_SCALE,
                'translation': TrackingMode.TRANSLATION}[self._stabilizer_analysis_params['mode']]
        self.tracker_panel.stabilizer.set_running(True)
        try:
            self.stabilization_service.start(
                clip_id=clip_id, shape_id='__camera__', media_path=media,
                pattern_region=(0.,0.,1.,1.), search_region=(0.,0.,1.,1.),
                reference_frame=first, frame_start=first, frame_end=last, mode=mode,
                quality=AnalysisQuality.NORMAL, directions=(TrackDirection.FORWARD,),
                shape_geometry={'camera_solver': 'global_camera_v1', 'mode':mode.value})
        except (OSError, ValueError, RuntimeError) as exc:
            self._on_stabilization_failed(
                self.stabilization_service.task_key(clip_id, '__camera__'), 0, str(exc))

    def _stabilizer_task_current(self, key: str) -> bool:
        task = getattr(self, '_stabilizer_task', None)
        if not task or key != self.stabilization_service.task_key(task[0], '__camera__'):
            return False
        clip, media, *_rest = self._tracker_frame_context()
        return (clip,media) == task

    def _on_stabilization_progress(self, key: str, generation: int, done: int, total: int) -> None:
        if self._stabilizer_task_current(key):
            self.tracker_panel.stabilizer.set_progress(done,total)

    def _on_stabilization_finished(self, key: str, generation: int, result: Any) -> None:
        if not self._stabilizer_task_current(key):
            return
        panel = self.tracker_panel.stabilizer
        if result.cancelled:
            panel.set_running(False)
            return
        first,last = self._stabilizer_analysis_range
        size = self._stabilizer_source_size
        params = self._stabilizer_analysis_params
        options = self._stabilizer_plan_options(params)
        state = {'schema_version':1,'params':params,'source_width':size[0],'source_height':size[1],
                 'source_frame_start':first,'source_frame_end':last,
                 'transforms':{str(f):list(t.matrix) for f,t in result.transforms.items()}}
        self.stabilization_plan_service.submit(result.transforms, size, options, self._stabilizer_fps(),
            {'kind': 'analysis', 'context': self._stabilizer_task, 'key': key, 'state': state})

    def _on_stabilization_plan_ready(self, plan: Any, metadata: dict) -> None:
        if tuple(metadata['context']) != tuple(self._tracker_frame_context()[:2]):
            return
        if metadata['kind'] == 'analysis' and not self._stabilizer_task_current(metadata['key']):
            return
        panel = self.tracker_panel.stabilizer
        if plan.valid_frames < 2:
            self._on_stabilization_plan_failed(
                tr('tracker.stabilizer.insufficient_motion',
                   'Not enough reliable camera motion; try Similarity or Translation'), metadata)
            return
        state = metadata['state']
        state.update(failed_frames=list(plan.failed_frames),
                     corrections={str(f):list(m) for f,m in plan.matrices.items()},
                     zoom_factor=plan.zoom_factor,strength_factor=plan.strength_factor)
        if metadata.get('push_history', True):
            self._push_mask_structure_snapshot()
        self.engine.set_video_stabilization_state(state)
        if metadata['kind'] == 'analysis':
            panel.set_running(False)
            self._sync_stabilizer_panel()
        first, last = state['source_frame_start'], state['source_frame_end']
        text = tr('tracker.stabilizer.result', '{done} / {total} frames · Zoom {zoom}×',
                  done=plan.valid_frames, total=last-first+1, zoom=f'{plan.zoom_factor:.2f}')
        if plan.failed_frames:
            text += tr('tracker.stabilizer.failed_frames', ' · {count} frames uncompensated',
                       count=len(plan.failed_frames))
        if plan.strength_factor < .99:
            text += tr('tracker.stabilizer.crop_limited', ' · Compensation limited by crop')
        panel.status_label.setText(text)
        panel.status_label.show()
        self._commit_stabilizer_change()

    def _on_stabilization_plan_failed(self, message: str, metadata: dict) -> None:
        if tuple(metadata['context']) == tuple(self._tracker_frame_context()[:2]):
            panel = self.tracker_panel.stabilizer
            panel.set_running(False)
            panel.status_label.setText(message)
            panel.status_label.show()

    def _on_stabilization_failed(self, key: str, generation: int, message: str) -> None:
        if self._stabilizer_task_current(key):
            self.tracker_panel.stabilizer.set_running(False)
            self.tracker_panel.stabilizer.status_label.setText(message)
            self.tracker_panel.stabilizer.status_label.show()

    def _on_stabilization_cancelled(self, key: str, generation: int) -> None:
        if self._stabilizer_task_current(key):
            self.tracker_panel.stabilizer.set_running(False)

    def _cancel_stabilization(self) -> None:
        task = getattr(self,'_stabilizer_task',None)
        if task:
            # Invalidate queued completions too, including a just-finished worker.
            self.stabilization_service.invalidate(task[0])
        self._stabilizer_task = None
        self.stabilization_plan_service.invalidate()
        self.tracker_panel.stabilizer.set_running(False)

    def _on_stabilizer_params_changed(self, params: dict) -> None:
        state = self.engine.get_video_stabilization_state()
        if not state:
            return
        if params['mode'] != state.get('params',{}).get('mode'):
            self.tracker_panel.stabilizer.status_label.setText(
                tr('tracker.stabilizer.mode_changed', 'Mode changed; click Stabilize to analyze again'))
            self.tracker_panel.stabilizer.status_label.show()
            # Selecting a different solver needs analysis, but bypass and the
            # other controls still apply immediately to the existing trajectory.
            params = {**params, 'mode': state['params']['mode']}
        self._stabilizer_pending_params = dict(params)
        self._stabilizer_pending_context = self._tracker_frame_context()[:2]
        self._stabilizer_pending_push_history = not getattr(self, '_stabilizer_interacting', False)
        self._stabilizer_params_timer.start()

    def _on_stabilizer_interaction_started(self) -> None:
        self._stabilizer_interacting = True
        # The existing interaction lane captures the gesture's single undo state.
        self.on_interaction_started()

    def _on_stabilizer_interaction_ended(self) -> None:
        self._stabilizer_interacting = False
        if self._stabilizer_params_timer.isActive():
            self._stabilizer_params_timer.stop()
            self._recompute_stabilizer()
        self.on_interaction_ended()

    def _recompute_stabilizer(self) -> None:
        from src.shared.tracking.types import TrackStatus, TrackTransform, FailureReason
        state = self.engine.get_video_stabilization_state()
        params = getattr(self,'_stabilizer_pending_params',None)
        if (not state or params is None or
                getattr(self, '_stabilizer_pending_context', None) != self._tracker_frame_context()[:2]):
            return
        failed = set(state.get('failed_frames',[]))
        transforms = {int(f): TrackTransform.from_matrix(int(f),np.array(m).reshape(3,3),
                      0. if int(f) in failed else 1.,
                      TrackStatus.FAILED if int(f) in failed else TrackStatus.VALID,
                      FailureReason.SOLVE_FAILED if int(f) in failed else FailureReason.NONE)
                      for f,m in state.get('transforms',{}).items()}
        options = self._stabilizer_plan_options(params)
        state['params'] = params
        self.stabilization_plan_service.submit(
            transforms, (state['source_width'],state['source_height']), options, self._stabilizer_fps(),
            {'kind': 'params', 'context': self._stabilizer_pending_context, 'state': state,
             'push_history': self._stabilizer_pending_push_history})

    @staticmethod
    def _stabilizer_plan_options(params: dict) -> dict:
        options = {k: params[k] for k in
                   ('smooth', 'strength', 'camera_lock', 'zoom', 'cropping_ratio')}
        if params['camera_lock']:
            # Resolve disables the numeric controls while locking. Retain their
            # edits for ordinary smoothing; use full compensation while locked.
            options.update(strength=1., cropping_ratio=0.)
        return options

    def _reset_stabilization(self) -> None:
        self._cancel_stabilization()
        self._stabilizer_params_timer.stop()
        self._push_mask_structure_snapshot()
        self.engine.set_video_stabilization_state({})
        self.tracker_panel.stabilizer.set_params(DEFAULT_PARAMS)
        self._sync_stabilizer_panel()
        self._commit_stabilizer_change()

    def _commit_stabilizer_change(self) -> None:
        controller = getattr(self,'video_controller',None)
        if controller is not None:
            controller.invalidate_cached_render_params()
        self.request_render()
        panel = getattr(self, "grading_panel", None)
        trigger_save = getattr(panel, "trigger_save", None)
        if callable(trigger_save):
            trigger_save()
        self._trigger_autosave()
