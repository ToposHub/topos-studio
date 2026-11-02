"""Camera compensation removes high-frequency jitter, preserving intended pans."""
from __future__ import annotations
from threading import Event
import cv2
import numpy as np
import pytest
from src.shared.tracking.camera_stabilization import compute_stabilization, compute_stabilization_plan
from src.shared.tracking.camera_motion import CameraMotionAnalyzer
from src.shared.tracking.tracker_2d import TrackRequest
from src.shared.tracking.frame_sampler import AnalysisQuality, SampledFrame
from src.shared.tracking.types import TrackTransform, TrackStatus, TrackingMode, FailureReason


def trajectory(count=80):
    values={}
    for frame in range(count):
        matrix=np.eye(3);matrix[0,2]=frame*.8+(-1)**frame*5.;matrix[1,2]=(-1)**frame*3.
        values[frame]=TrackTransform.from_matrix(frame,matrix,1.,TrackStatus.VALID)
    return values


def test_smoothing_removes_jitter_and_keeps_pan_and_strength_zero():
    path=trajectory();corrections=compute_stabilization(path,(640,360),zoom=False,smooth=.3)
    observed=np.array([t.matrix_np[0,2] for t in path.values()])
    actual=np.array([(np.array(corrections[f]).reshape(3,3)@t.matrix_np)[0,2] for f,t in path.items()])
    assert np.std(np.diff(actual)) < np.std(np.diff(observed))*.15
    assert np.polyfit(np.arange(10,70),actual[10:70],1)[0] == pytest.approx(.8,abs=.03)
    zero=compute_stabilization(path,(640,360),strength=0.)
    assert all(np.allclose(np.array(m).reshape(3,3),np.eye(3)) for m in zero.values())


def test_intentional_constant_pan_does_not_ease_in_or_out_at_clip_boundaries():
    path = {}
    for frame in range(80):
        matrix = np.eye(3)
        matrix[:2, 2] = [frame * 3., frame * -.5]
        path[frame] = TrackTransform.from_matrix(frame, matrix, 1., TrackStatus.VALID)
    corrections = compute_stabilization(path, (640, 360), zoom=False, smooth=.4)
    for matrix in corrections.values():
        np.testing.assert_allclose(np.array(matrix).reshape(3, 3), np.eye(3), atol=1e-9)


@pytest.mark.parametrize('perspective',[False,True])
def test_camera_lock_cancels_complete_matrix(perspective):
    path={}
    for f in range(10):
        angle=f*.01;c,s=np.cos(angle),np.sin(angle)
        h=np.array([[c,-s,float(f)],[s,c,-f*.5],[f*1e-5 if perspective else 0.,0.,1.]])
        path[f]=TrackTransform.from_matrix(f,h,1.,TrackStatus.VALID)
    output=compute_stabilization(path,(640,360),camera_lock=True,zoom=False)
    for f,m in output.items():
        actual=np.array(m).reshape(3,3)@path[f].matrix_np
        np.testing.assert_allclose(actual/actual[2,2],np.eye(3),atol=1e-10)


def test_autozoom_is_constant_and_covers_output_corners():
    path=trajectory();plan=compute_stabilization_plan(path,(640,360),cropping_ratio=.8)
    assert 1. < plan.zoom_factor <= 1.25
    corners=np.array([[0.,0.],[640.,0.],[640.,360.],[0.,360.]]).reshape(1,-1,2)
    for correction in plan.matrices.values():
        mapped=cv2.perspectiveTransform(corners,np.linalg.inv(np.array(correction).reshape(3,3)))[0]
        assert mapped.min() >= -1e-5
        assert mapped[:,0].max() <= 640.+1e-5 and mapped[:,1].max() <= 360.+1e-5


def test_failure_splits_smoothing_segments_and_preserves_missing_frames():
    path=trajectory(21)
    path[10]=TrackTransform.failed(10,FailureReason.OCCLUSION)
    for f in range(11,21):path[f]=TrackTransform.from_matrix(f,np.eye(3),1.,TrackStatus.VALID)
    plan=compute_stabilization_plan(path,(640,360),zoom=False,smooth=1.)
    assert plan.failed_frames==(10,)
    for f in range(10,21):np.testing.assert_allclose(np.array(plan.matrices[f]).reshape(3,3),np.eye(3))


class Sampler:
    def __init__(self):
        rng=np.random.default_rng(7);self.base=rng.integers(0,255,(240,400),np.uint8)
    def read_gray(self,frame,quality):
        image=cv2.warpAffine(self.base,np.float32([[1,0,frame],[0,1,0]]),(400,240),borderMode=cv2.BORDER_REFLECT)
        return SampledFrame(frame,image,400,240,1.,400,240)


@pytest.mark.parametrize('mode',[TrackingMode.TRANSLATION,TrackingMode.TRANSLATION_ROTATION_SCALE,TrackingMode.HOMOGRAPHY])
def test_global_analysis_known_translation_and_no_window_binding(mode):
    req=TrackRequest('memory',(0.,0.,1.,1.),(0.,0.,1.,1.),0,0,19,mode=mode,quality=AnalysisQuality.HIGH)
    result=CameraMotionAnalyzer().track(req,sampler=Sampler())
    assert set(result.transforms)==set(range(20))
    assert all(t.status==TrackStatus.VALID for t in result.transforms.values())
    for f,t in result.transforms.items():
        point=cv2.perspectiveTransform(np.array([[[200.,120.]]]),t.matrix_np)[0,0]
        np.testing.assert_allclose(point,[200+f,120],atol=.15)


def test_analysis_cancel_keeps_boundary_and_does_not_finish_whole_clip():
    token=Event();req=TrackRequest('memory',(0.,0.,1.,1.),(0.,0.,1.,1.),0,0,19)
    def progress(done,total):
        if done==4:token.set()
    result=CameraMotionAnalyzer().track(req,sampler=Sampler(),cancel_token=token,progress_cb=progress)
    assert result.cancelled and set(result.transforms)==set(range(4))


@pytest.mark.parametrize('mode', [TrackingMode.TRANSLATION,
    TrackingMode.TRANSLATION_ROTATION_SCALE, TrackingMode.HOMOGRAPHY])
def test_moving_foreground_does_not_become_the_camera_path(mode):
    class MovingForeground(Sampler):
        def read_gray(self, frame, quality):
            image = super().read_gray(frame, quality).gray.copy()
            # An independently moving subject crosses a distributed background.
            left = 120 + frame * 4
            image[70:170, left:left+80] = self.base[20:120, 20:100]
            return SampledFrame(frame, image, 400, 240, 1., 400, 240)
    request = TrackRequest('memory', (0.,0.,1.,1.), (0.,0.,1.,1.), 0, 0, 19,
                           mode=mode, quality=AnalysisQuality.HIGH)
    result = CameraMotionAnalyzer().track(request, sampler=MovingForeground())
    assert all(transform.status == TrackStatus.VALID for transform in result.transforms.values())
    point = cv2.perspectiveTransform(np.array([[[40., 40.]]]), result.transforms[19].matrix_np)[0,0]
    np.testing.assert_allclose(point, [59., 40.], atol=.5)


def test_scene_cut_is_explicit_and_analysis_resumes_to_the_last_frame():
    class Cut(Sampler):
        def read_gray(self, frame, quality):
            sample = super().read_gray(frame, quality)
            if frame == 8:
                sample.gray.fill(255)
            return sample
    request = TrackRequest('memory', (0.,0.,1.,1.), (0.,0.,1.,1.), 0, 0, 19)
    result = CameraMotionAnalyzer().track(request, sampler=Cut())
    assert set(result.transforms) == set(range(20))
    assert result.transforms[8].status == TrackStatus.FAILED
    assert result.transforms[19].status == TrackStatus.VALID
    assert not result.cancelled
