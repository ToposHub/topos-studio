"""Source-space stabilization agrees in CPU preview/export and real HDR GL."""
from __future__ import annotations
from types import SimpleNamespace
import numpy as np
import pytest
from src.shared.tracking.stabilization_state import validate_stabilization_state, stabilization_matrix
from src.features.shared.effects.video_stabilization import apply_stabilization_cpu, apply_stabilization_gpu
from src.shared.project.color_stack import ColorStackData


def state(matrix=None):
    m=np.eye(3) if matrix is None else np.array(matrix,dtype=float)
    if matrix is None:m[:2,2]=[4.,-3.]
    return {'schema_version':1,'params':{'mode':'similarity'},'source_width':64,'source_height':48,
            'transforms':{'10':np.eye(3).ravel().tolist()},'corrections':{'10':m.ravel().tolist()}}


def test_versioned_state_deep_freeze_unknowns_and_legacy_roundtrip():
    payload=state();payload['future']={'value':3}
    stack=ColorStackData.create().with_video_stabilization(payload)
    payload['corrections']['10'][2]=99
    assert stack.video_stabilization['corrections']['10'][2]==4.
    restored=ColorStackData.from_dict(stack.to_dict())
    assert restored.video_stabilization==stack.video_stabilization
    assert restored.video_stabilization['future']=={'value':3}
    assert ColorStackData.from_dict({'stack_id':'legacy'}).video_stabilization=={}
    assert stabilization_matrix(stack.video_stabilization,11) is None
    bypass=state();bypass['params']['bypass']=True
    assert stabilization_matrix(bypass,10) is None


@pytest.mark.parametrize('change',[{'schema_version':2},{'source_width':0},
 {'corrections':{'10':[float('nan')]*9}},{'corrections':{'10':[0.]*9}},
 {'params':{'smooth':float('inf')}}])
def test_corrupt_states_are_diagnosed(change):
    with pytest.raises(ValueError):validate_stabilization_state({**state(),**change})


def test_clear_and_undo_project_command_and_command_serialization():
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.commands.color_commands import UpdateColorStackCommand
    stack=ColorStackData.create().with_video_stabilization(state())
    project=EditProject.create('stabilizer')
    from dataclasses import replace
    project=replace(project,color_stacks={stack.stack_id:stack})
    command=UpdateColorStackCommand.create(stack.stack_id,video_stabilization={})
    updated=command.apply(project)
    assert updated.color_stacks[stack.stack_id].video_stabilization=={}
    loaded=UpdateColorStackCommand.from_dict(command.to_dict())
    undone=loaded.undo(updated)
    assert undone.color_stacks[stack.stack_id].video_stabilization==stack.video_stabilization


def test_actual_cpu_timeline_preview_and_export_use_source_frame():
    from src.features.edit.core.timeline_export import TimelineExporter
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
    stack=ColorStackData.create().with_video_stabilization(state())
    project=SimpleNamespace(color_stacks={stack.stack_id:stack})
    layer=SimpleNamespace(clip=SimpleNamespace(color_stack_id=stack.stack_id),source_frame=10)
    image=np.zeros((48,64,3),np.float32);image[12:18,20:25]=2.
    exporter=TimelineExporter.__new__(TimelineExporter);exporter._project=project
    exporter._apply_color_grade_unstabilized=lambda frame,layer,**_kw:frame
    expected=apply_stabilization_cpu(image,state(),10)
    np.testing.assert_array_equal(exporter._apply_color_grade(image,layer),expected)
    worker=PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver=SimpleNamespace(_project=project)
    worker._apply_preview_color_grade_unstabilized=lambda frame,layer,**_kw:frame
    np.testing.assert_array_equal(worker._apply_preview_color_grade(image,layer),expected)
    layer.source_frame=11
    np.testing.assert_array_equal(exporter._apply_color_grade(image,layer),image)


@pytest.mark.parametrize('matrix',[None,[[1.,.02,-1.],[.01,1.,2.],[.0001,.0002,1.]]])
def test_real_gl_direction_hdr_and_cpu_parity(gl_context,matrix):
    import moderngl
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner
    image=np.zeros((48,64,4),np.float32);image[...,3]=1.
    image[8:20,12:30,0]=3.;image[30:38,40:52,1]=2.
    source=gl_context.texture((64,48),4,np.flipud(image).copy().tobytes(),dtype='f4')
    source.filter=(moderngl.LINEAR,moderngl.LINEAR)
    runner=EffectPassRunner(gl_context)
    try:
        output=apply_stabilization_gpu(runner,source.glo,state(matrix),10,64,48,dtype='f4')
        assert output!=source.glo
        texture=runner.output_texture()
        actual=np.flipud(np.frombuffer(texture.read(),np.float32).reshape(48,64,4))
        expected=apply_stabilization_cpu(image,state(matrix),10)
        assert actual[...,0].max()>2.5
        assert np.abs(actual-expected).mean()<.015
        if matrix is None:np.testing.assert_allclose(actual,expected,atol=1e-5)
    finally:
        runner.release();source.release()


def test_real_gl_subpixel_black_border_does_not_wrap_hdr_edges(gl_context):
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner
    image = np.ones((48, 64, 4), np.float32)
    image[..., :3] = [3., 2., .5]
    matrix = np.array([[1., 0., .5], [0., 1., -.5], [0., 0., 1.]])
    # A homography and its negative describe the same 2D mapping.
    payload = state(-matrix)
    source = gl_context.texture((64, 48), 4, np.flipud(image).copy().tobytes(), dtype='f4')
    runner = EffectPassRunner(gl_context)
    try:
        apply_stabilization_gpu(runner, source.glo, payload, 10, 64, 48, dtype='f4')
        actual = np.flipud(np.frombuffer(runner.output_texture().read(), np.float32).reshape(48, 64, 4))
        expected = apply_stabilization_cpu(image, payload, 10)
        np.testing.assert_allclose(actual, expected, atol=1e-5)
    finally:
        runner.release()
        source.release()


def test_real_gl_source_scaled_proxy_and_viewport_roi(gl_context):
    from src.features.shared.effects.effect_pass_runner import EffectPassRunner
    image = np.ones((48, 64, 4), np.float32)
    image[..., 0] = np.arange(64)[None, :] / 16.
    image[..., 1] = np.arange(48)[:, None] / 12.
    payload = state([[1.,0.,8.], [0.,1.,-6.], [0.,0.,1.]])
    payload.update(source_width=128, source_height=96)
    source = gl_context.texture((64, 48), 4, np.flipud(image).copy().tobytes(), dtype='f4')
    runner = EffectPassRunner(gl_context)
    try:
        apply_stabilization_gpu(runner, source.glo, payload, 10, 32, 24, dtype='f4',
                                output_rect=(32.,24.,64.,48.))
        actual = np.flipud(np.frombuffer(runner.output_texture().read(), np.float32).reshape(24,32,4))
        expected = apply_stabilization_cpu(image, payload, 10)[12:36,16:48]
        np.testing.assert_allclose(actual, expected, atol=1e-5)
    finally:
        runner.release()
        source.release()
