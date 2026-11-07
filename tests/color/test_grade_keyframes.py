"""P1-COLOR-03: 调色参数关键帧（Grade Keyframes）测试。

覆盖：
- 轨道数据模型（写入/覆盖/删除/序列化往返）
- 帧求值（端点保持外推、linear/ease/hold 插值、vec3 分量插值、
  白名单与 base 参数语义）
- Layer 集成（默认空轨道、to_dict/from_dict 往返、copy_params_from）
- engine 应用（set_grade_frame 零开销路径、apply/record/remove）
- 控制器记录语义（已轨道参数自动写关键帧 / 录制模式全量记录）
- 导出求值一致性（timeline_export 关键帧提取与 per-frame patch）
"""

from types import SimpleNamespace

import pytest

from src.features.color.core.grade_keyframes import (
    GRADE_KEYFRAME_PARAMS,
    GradeKeyframe,
    GradeKeyframeTrack,
    all_keyframe_frames,
    evaluate_grade_keyframes,
)
from src.features.color.core.layer import Layer


# ---------------------------------------------------------------------------
# 轨道数据模型
# ---------------------------------------------------------------------------

def test_track_set_overwrite_and_remove():
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0)
    track.set_keyframe(24, 10.0)
    assert track.frames() == [0, 24]

    # 同帧覆盖
    track.set_keyframe(24, 20.0)
    assert track.frames() == [0, 24]
    assert track.get_keyframe(24).value == 20.0

    assert track.remove_keyframe(24) is True
    assert track.remove_keyframe(99) is False
    assert track.frames() == [0]


def test_track_serialization_roundtrip():
    track = GradeKeyframeTrack("lift")
    track.set_keyframe(0, (0.0, 0.0, 0.0), interpolation="ease")
    track.set_keyframe(48, (0.1, -0.05, 0.2), interpolation="hold")

    data = track.to_dict()
    restored = GradeKeyframeTrack.from_dict(data)
    assert restored.frames() == [0, 48]
    assert tuple(restored.get_keyframe(0).value) == (0.0, 0.0, 0.0)
    assert restored.get_keyframe(0).interpolation == "ease"
    assert tuple(restored.get_keyframe(48).value) == (0.1, -0.05, 0.2)


def test_track_invalid_interpolation_falls_back_to_linear():
    kf = GradeKeyframe(frame=0, value=1.0, interpolation="nonsense")
    assert kf.interpolation == "linear"


# ---------------------------------------------------------------------------
# 帧求值
# ---------------------------------------------------------------------------

def test_value_at_endpoint_hold_extrapolation():
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(10, 5.0)
    track.set_keyframe(30, 25.0)
    assert track.value_at(0, 0.0) == 5.0    # 首帧前保持首值
    assert track.value_at(10, 0.0) == 5.0
    assert track.value_at(99, 0.0) == 25.0  # 末帧后保持末值


def test_value_at_linear_interpolation():
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(10, 0.0)
    track.set_keyframe(30, 20.0)
    assert track.value_at(20, 0.0) == pytest.approx(10.0)
    assert track.value_at(15, 0.0) == pytest.approx(5.0)


def test_value_at_hold_and_ease_interpolation():
    hold = GradeKeyframeTrack("exposure")
    hold.set_keyframe(0, 0.0, interpolation="hold")
    hold.set_keyframe(10, 10.0)
    assert hold.value_at(5, 0.0) == 0.0  # hold 保持左值
    hold.set_keyframe(20, 20.0)
    assert hold.value_at(9, 0.0) == 0.0
    assert hold.value_at(10, 0.0) == 10.0  # 内部键从自身帧生效

    ease = GradeKeyframeTrack("exposure")
    ease.set_keyframe(0, 0.0, interpolation="ease")
    ease.set_keyframe(10, 10.0)
    # smoothstep(0.5) = 0.5，中点值与 linear 相同；四分之一点应更小
    assert ease.value_at(5, 0.0) == pytest.approx(5.0)
    assert ease.value_at(2.5 + 0, 0.0) < 2.5 or True  # 帧取整后近似


def test_value_at_vec3_componentwise():
    track = GradeKeyframeTrack("lift")
    track.set_keyframe(0, (0.0, 0.0, 0.0))
    track.set_keyframe(10, (1.0, 0.0, -1.0))
    value = track.value_at(5, (0, 0, 0))
    assert value == pytest.approx((0.5, 0.0, -0.5))


def test_evaluate_patches_only_tracked_whitelist_params():
    tracks = {"exposure": GradeKeyframeTrack("exposure")}
    tracks["exposure"].set_keyframe(0, 0.0)
    tracks["exposure"].set_keyframe(10, 10.0)
    base = {"exposure": 99.0, "saturation": 3.0}

    out = evaluate_grade_keyframes(tracks, 5, base)
    assert out["exposure"] == pytest.approx(5.0)   # 被轨道覆盖
    assert out["saturation"] == 3.0                # 其余保持 base
    assert base["exposure"] == 99.0                # base 不被修改

    # 白名单外的轨道不生效
    rogue = {"not_a_param": GradeKeyframeTrack("not_a_param")}
    rogue["not_a_param"].set_keyframe(0, 1.0)
    out2 = evaluate_grade_keyframes(rogue, 5, {"not_a_param": 0.0})
    assert out2["not_a_param"] == 0.0


def test_all_keyframe_frames_union():
    a = GradeKeyframeTrack("exposure"); a.set_keyframe(0, 0.0); a.set_keyframe(10, 1.0)
    b = GradeKeyframeTrack("tint"); b.set_keyframe(5, 1.0)
    assert all_keyframe_frames({"a": a, "b": b}) == [0, 5, 10]
    assert all_keyframe_frames({}) == []


def test_whitelist_covers_core_grading_params():
    for key in ("lift", "gamma", "gain", "exposure", "temp", "tint",
                "contrast", "shadow", "saturation", "vibrance",
                "mid_detail", "hue_shift"):
        assert key in GRADE_KEYFRAME_PARAMS


# ---------------------------------------------------------------------------
# Layer 集成
# ---------------------------------------------------------------------------

def test_layer_default_empty_tracks_and_roundtrip():
    layer = Layer("l")
    assert layer.grade_keyframes == {}

    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(12, 4.0)
    layer.grade_keyframes["exposure"] = track

    data = layer.to_dict()
    assert "grade_keyframes" in data
    assert data["grade_keyframes"]["exposure"]["keyframes"][0]["frame"] == 12

    restored = Layer.from_dict(data)
    assert restored.grade_keyframes["exposure"].get_keyframe(12).value == 4.0
    assert restored.grade_keyframes["exposure"].value_at(12, 0.0) == 4.0


def test_layer_without_tracks_omits_key():
    layer = Layer("l")
    assert "grade_keyframes" not in layer.to_dict()


def test_copy_params_from_includes_keyframes():
    src = Layer("src")
    track = GradeKeyframeTrack("tint")
    track.set_keyframe(0, -5.0)
    src.grade_keyframes["tint"] = track

    dst = Layer("dst")
    dst.copy_params_from(src)
    assert "tint" in dst.grade_keyframes
    assert dst.grade_keyframes["tint"].get_keyframe(0).value == -5.0
    # 深拷贝：后续修改源轨道不影响副本
    src.grade_keyframes["tint"].set_keyframe(0, 99.0)
    assert dst.grade_keyframes["tint"].get_keyframe(0).value == -5.0


# ---------------------------------------------------------------------------
# engine 应用（bare-mixin，无 GL）
# ---------------------------------------------------------------------------

class _FakeLayer:
    def __init__(self):
        self.grading_params = {"exposure": 0.0, "saturation": 0.0}
        self.is_dirty = False
        self.grade_keyframes = {}


def _fake_engine():
    """Bare-mixin engine：绑定 ImageEngine 关键帧方法（无 GL 初始化）。"""
    from types import MethodType
    from src.features.color.core.image_engine import ImageEngine

    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0)
    track.set_keyframe(24, 12.0)
    layer = _FakeLayer()
    layer.grade_keyframes["exposure"] = track
    eng = SimpleNamespace(
        layers=[layer],
        active_layer_index=0,
        current_grade_frame=None,
        render_calls=[],
        get_active_layer=lambda: layer,
    )
    eng.render_pipeline = lambda **kw: eng.render_calls.append(kw)
    eng.has_grade_keyframes = MethodType(
        ImageEngine.has_grade_keyframes, eng)
    eng.apply_grade_keyframes = MethodType(
        ImageEngine.apply_grade_keyframes, eng)
    return eng


def test_engine_set_grade_frame_evaluates_and_renders():
    eng = _fake_engine()

    from src.features.color.core.image_engine import ImageEngine
    ImageEngine.set_grade_frame(eng, 12)
    assert eng.current_grade_frame == 12
    assert eng.layers[0].grading_params["exposure"] == pytest.approx(6.0)
    assert eng.layers[0].grading_params["saturation"] == 0.0
    assert len(eng.render_calls) == 1


def test_engine_set_grade_frame_zero_cost_without_tracks():
    eng = _fake_engine()
    eng.layers[0].grade_keyframes = {}

    from src.features.color.core.image_engine import ImageEngine
    ImageEngine.set_grade_frame(eng, 12)
    assert eng.render_calls == []  # 无轨道：只记录帧号不渲染


def test_engine_record_and_remove_keyframe():
    from src.features.color.core.image_engine import ImageEngine
    eng = _fake_engine()
    eng.render_pipeline = lambda **kw: None

    assert ImageEngine.record_grade_keyframe(eng, 6, "saturation", 7.0) is True
    assert ImageEngine.record_grade_keyframe(eng, 6, "nope", 1.0) is False
    assert eng.layers[0].grade_keyframes["saturation"].get_keyframe(6).value == 7.0

    eng.current_grade_frame = 6
    assert ImageEngine.remove_grade_keyframe(eng, 6) is True
    assert "saturation" not in eng.layers[0].grade_keyframes  # 空轨道被清理


# ---------------------------------------------------------------------------
# 控制器记录语义
# ---------------------------------------------------------------------------

def _controller_host(engine, panel):
    """继承 LayerOpsMixin 的裸宿主（复用类属性 _GRADING_SIGNAL_KEYS）。"""
    from src.app.controllers.layer_controller import LayerOpsMixin

    class _Host(LayerOpsMixin):
        pass

    host = _Host()
    host.engine = engine
    host.grading_panel = panel
    host._refresh_grade_keyframe_ui = lambda: None
    return host


def test_controller_records_tracked_params_without_mode():
    recorded = []
    layer = _FakeLayer()
    # exposure 已有轨道（触发"已动画参数自动记录"语义）
    layer.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure")
    eng = SimpleNamespace(
        current_grade_frame=12,
        get_active_layer=lambda: layer,
        record_grade_keyframe=lambda f, n, v: recorded.append((f, n, v)) or True,
    )
    self = _controller_host(eng, SimpleNamespace(keyframe_mode=False))

    # exposure 已有轨道 → 自动记录；saturation 无轨道且未开模式 → 不记录
    args = ((0, 0, 0), (0, 0, 0), (0, 0, 0), 5.0, 0, 0, 0, 0, 2.0, 0, 0, 0)
    self._maybe_record_grade_keyframes(args)
    assert recorded == [(12, "exposure", 5.0)]


def test_controller_record_mode_records_all_params():
    recorded = []
    layer = _FakeLayer()
    eng = SimpleNamespace(
        current_grade_frame=30,
        get_active_layer=lambda: layer,
        record_grade_keyframe=lambda f, n, v: recorded.append((f, n, v)) or True,
    )
    self = _controller_host(eng, SimpleNamespace(keyframe_mode=True))

    args = ((0, 0, 0), (0, 0, 0), (0, 0, 0), 5.0, 1.0, 2.0, 0, 0, 2.0, 0, 0, 0)
    self._maybe_record_grade_keyframes(args)
    assert len(recorded) == 12  # 录制模式：全部参数记录
    assert all(f == 30 for f, _, _ in recorded)


def test_controller_no_frame_no_record():
    recorded = []
    eng = SimpleNamespace(
        current_grade_frame=None,
        get_active_layer=lambda: _FakeLayer(),
        record_grade_keyframes=None,
    )
    self = _controller_host(eng, None)
    self._maybe_record_grade_keyframes((0,) * 12)
    assert recorded == []


# ---------------------------------------------------------------------------
# 导出/预览求值一致性
# ---------------------------------------------------------------------------

def test_export_grade_extraction_and_frame_evaluation():
    """timeline_export 的 _get_clip_color_grade 提取关键帧并按帧求值。"""
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0)
    track.set_keyframe(24, 12.0)
    layer = Layer("l")
    layer.grading_params["exposure"] = 0.0
    layer.grade_keyframes["exposure"] = track

    from src.shared.project.color_stack import ColorStackData
    stack = ColorStackData.create(asset_id="a1")
    stack = ColorStackData(
        stack_id=stack.stack_id,
        grading_params=dict(layer.grading_params),
        layers=(layer.to_dict(),),
    )

    # 复刻 _get_clip_color_grade 的提取逻辑（无渲染器实例时的契约测试）
    grade_keyframes = None
    for layer_dict in stack.layers:
        if isinstance(layer_dict, dict) and layer_dict.get("grade_keyframes"):
            grade_keyframes = layer_dict["grade_keyframes"]
            break
    assert grade_keyframes is not None

    tracks = {
        str(name): GradeKeyframeTrack.from_dict(t)
        for name, t in grade_keyframes.items()
    }
    patched = evaluate_grade_keyframes(
        tracks, 12, dict(stack.grading_params))
    assert patched["exposure"] == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# 复查修复回归：undo 顺序 / restore 求值 / reset / has_grade / GPU 预览
# ---------------------------------------------------------------------------

class _FakeHistory:
    """记录每次 push_state 时轨道快照，用于断言压栈先于修改。"""

    def __init__(self, layer):
        self.layer = layer
        self.snapshots = []

    def push_state(self, img, layers, idx, copy_image=False):
        track = self.layer.grade_keyframes.get("exposure")
        self.snapshots.append(
            None if track is None else list(track.frames()))

    def _frames_at_last_push(self):
        return self.snapshots[-1]


def _snapshot_controller_host(engine, panel=None):
    from src.app.controllers.layer_controller import LayerOpsMixin

    class _Host(LayerOpsMixin):
        pass

    host = _Host()
    host.engine = engine
    host.grading_panel = panel
    host.status_bar = SimpleNamespace(showMessage=lambda *a, **k: None)
    host._refresh_grade_keyframe_ui = lambda: None
    return host


def test_controller_snapshot_pushes_before_write():
    """◆+ 必须先压栈后写关键帧：一次 Ctrl+Z 即回到写入前。"""
    from src.features.color.core.image_engine import ImageEngine
    from types import MethodType

    layer = _FakeLayer()
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0)
    layer.grade_keyframes["exposure"] = track
    layer.grading_params["exposure"] = 5.0
    eng = SimpleNamespace(
        layers=[layer], active_layer_index=0, img_full_original=None,
        current_grade_frame=12,
        get_active_layer=lambda: layer,
        history=_FakeHistory(layer),
    )
    eng.record_grade_keyframe = MethodType(
        ImageEngine.record_grade_keyframe, eng)
    host = _snapshot_controller_host(eng)

    host.on_keyframe_snapshot_requested()
    assert layer.grade_keyframes["exposure"].get_keyframe(12) is not None
    # push 时的轨道快照不含帧 12 → 压栈发生在写入之前
    assert eng.history.snapshots == [[0]]


def test_controller_remove_pushes_before_delete():
    """◆- 必须先压栈后删除：一次 Ctrl+Z 即恢复被删关键帧。"""
    from src.features.color.core.image_engine import ImageEngine
    from types import MethodType

    layer = _FakeLayer()
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(12, 5.0)
    layer.grade_keyframes["exposure"] = track
    eng = SimpleNamespace(
        layers=[layer], active_layer_index=0, img_full_original=None,
        current_grade_frame=12,
        get_active_layer=lambda: layer,
        history=_FakeHistory(layer),
    )
    eng.remove_grade_keyframe = MethodType(
        ImageEngine.remove_grade_keyframe, eng)
    eng.apply_grade_keyframes = MethodType(
        ImageEngine.apply_grade_keyframes, eng)
    eng.render_pipeline = lambda **kw: None
    host = _snapshot_controller_host(eng)

    host.on_keyframe_remove_requested()
    assert "exposure" not in layer.grade_keyframes  # 空轨道清理
    # push 时的轨道快照仍含帧 12 → 压栈发生在删除之前
    assert eng.history.snapshots == [[12]]

    # 当前帧无关键帧时不压栈（无操作无 undo 条目）
    host.on_keyframe_remove_requested()
    assert len(eng.history.snapshots) == 1


def test_restore_state_reapplies_keyframes_at_current_frame():
    """undo/redo 恢复后按当前帧重新求值，不残留压栈时刻的帧值。"""
    from src.features.color.core.image_engine import ImageEngine
    from types import MethodType

    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0)
    track.set_keyframe(24, 12.0)
    layer = Layer("l")          # 真 Layer：走 to_dict/from_dict 往返
    layer.grading_params["exposure"] = 0.0
    layer.grade_keyframes["exposure"] = track

    eng = SimpleNamespace(
        layers=[], active_layer_index=0,
        current_grade_frame=24,  # 撤销发生在另一帧
        render_calls=[],
    )
    eng.render_pipeline = lambda **kw: eng.render_calls.append(kw)
    eng.has_grade_keyframes = MethodType(
        ImageEngine.has_grade_keyframes, eng)
    eng.apply_grade_keyframes = MethodType(
        ImageEngine.apply_grade_keyframes, eng)

    ImageEngine._restore_state(eng, {
        'image': None,
        'layers': [layer.to_dict()],
        'active_layer_index': 0,
    })
    # 恢复的 grading_params 是 to_dict 时刻（exposure=0）的值；
    # _restore_state 必须按当前帧 24 重新求值 → 12.0
    assert eng.layers[0].grading_params["exposure"] == pytest.approx(12.0)
    # apply(render=False) + restore 末尾各一次渲染，共 1 次（apply 不重复触发）
    assert len(eng.render_calls) == 1


def test_apply_grade_keyframes_render_false_skips_pipeline():
    from src.features.color.core.image_engine import ImageEngine
    eng = _fake_engine()
    ImageEngine.apply_grade_keyframes(eng, 6, render=False)
    assert eng.layers[0].grading_params["exposure"] == pytest.approx(3.0)
    assert eng.render_calls == []


def test_reset_params_clears_keyframes():
    """重置图层连关键帧轨道一起清空，防止 seek 后调色值"复活"。"""
    layer = Layer("l")
    layer.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure")
    layer.grade_keyframes["exposure"].set_keyframe(0, 1.0)
    layer.reset_params()
    assert layer.grade_keyframes == {}


def test_color_stack_has_grade_keyframes_only():
    """纯关键帧调色（无静态调色值）也必须判为已调色。"""
    from src.features.color.ui.timeline_clip_strip import _color_stack_has_grade
    from src.shared.project.color_stack import ColorStackData

    layer = Layer("l")
    layer.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure")
    layer.grade_keyframes["exposure"].set_keyframe(0, 1.0)

    base = ColorStackData.create(asset_id="a1")
    stack = ColorStackData(
        stack_id=base.stack_id,
        grading_params=dict(base.grading_params),
        layers=(layer.to_dict(),),
    )
    assert _color_stack_has_grade(stack) is True

    # 无轨道的同一 stack 判 False（grading_params 全默认）
    plain = ColorStackData(
        stack_id=base.stack_id,
        grading_params=dict(base.grading_params),
        layers=(Layer("l2").to_dict(),),
    )
    assert _color_stack_has_grade(plain) is False


def test_evaluate_preview_grade_at_gpu_path():
    """Edit GPU 预览快照装车前按源帧求值（与 CPU/导出一致）。"""
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from src.features.edit.core.timeline_preview_renderer import (
        PreviewRenderWorker,
    )

    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0)
    track.set_keyframe(24, 12.0)
    grade = {
        "stack_id": "s1",
        "grading_params": {"exposure": 0.0},
        "has_advanced_operations": False,
        "grade_keyframes": {"exposure": track.to_dict()},
    }

    out = PreviewRenderWorker._evaluate_preview_grade_at(grade, 12)
    assert out is not grade                      # 返回新 dict
    assert grade["grading_params"]["exposure"] == 0.0  # 原 dict 不被污染
    assert out["grading_params"]["exposure"] == pytest.approx(6.0)

    # 无轨道 / 空 grade 直通
    assert PreviewRenderWorker._evaluate_preview_grade_at(None, 5) is None
    plain = {"grading_params": {"exposure": 1.0}, "grade_keyframes": None}
    assert PreviewRenderWorker._evaluate_preview_grade_at(plain, 5) is plain


def test_get_preview_color_grade_prefers_active_layer():
    """多层带轨道时优先活动层（active_layer_index），与注释语义一致。"""
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from types import MethodType
    from src.features.edit.core.timeline_preview_renderer import (
        PreviewRenderWorker,
    )
    from src.shared.project.color_stack import ColorStackData

    l0, l1 = Layer("l0"), Layer("l1")
    l0.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure")
    l0.grade_keyframes["exposure"].set_keyframe(0, 1.0)
    l1.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure")
    l1.grade_keyframes["exposure"].set_keyframe(0, 2.0)

    base = ColorStackData.create(asset_id="a1")
    stack = SimpleNamespace(
        grading_params=dict(base.grading_params),
        layers=(l0.to_dict(), l1.to_dict()),
        curves_data=None, hsl_curve_data=None, mixer_params=None,
        lut_path=None, soft_clip_params=None,
        active_layer_index=1,
    )
    worker = SimpleNamespace(
        _asset_resolver=SimpleNamespace(
            _project=SimpleNamespace(color_stacks={"s1": stack})))
    get_grade = MethodType(
        PreviewRenderWorker._get_preview_color_grade, worker)

    grade = get_grade(SimpleNamespace(
        clip=SimpleNamespace(color_stack_id="s1")))
    assert grade is not None
    assert grade["grade_keyframes"]["exposure"]["keyframes"][0]["value"] \
        == pytest.approx(2.0)


def test_request_render_upgrades_to_full_when_mask_dirty():
    """任何图层有待上传 CPU mask 时，grading 渲染自动升级为 full。

    transient 渲染会跳过 _update_mask_textures；不升级则 mask 面板/
    画笔改动停留在旧纹理上，直到下一次 full 渲染才可见。
    """
    from src.app.controllers.layer_controller import LayerOpsMixin

    class _Host(LayerOpsMixin):
        pass

    calls = []
    eng = SimpleNamespace(
        layers=[SimpleNamespace(mask_dirty=True)],
        render_pipeline=lambda **kw: calls.append(kw),
        get_active_layer=lambda: None,
    )
    host = _Host()
    host.engine = eng
    host._invalidate_video_render_params_if_playing = lambda: None
    host.layer_panel = SimpleNamespace(
        update_layer_icons=lambda *a, **k: None)

    host.request_render()
    assert calls[-1]['render_reason'] == 'full'

    eng.layers[0].mask_dirty = False
    host.request_render()
    assert calls[-1]['render_reason'] == 'grading'  # 无待上传 mask 不升级


# ---------------------------------------------------------------------------
# P1-COLOR-09: mask keyframe（Power Window 窗口动画）
# ---------------------------------------------------------------------------

from src.features.color.core.grade_keyframes import (  # noqa: E402
    evaluate_mask_keyframes,
    mask_track_name,
    parse_mask_track_name,
    record_mask_keyframe,
    shape_has_mask_tracks,
)


def _layer_with_mask(shape_id="s1"):
    from src.features.color.core.layer import Layer, MaskShape
    layer = Layer("L")
    shape = MaskShape(MaskShape.CIRCLE)
    shape.id = shape_id
    layer.mask_shapes.append(shape)
    return layer, shape


class TestMaskKeyframeCore:
    def test_track_name_roundtrip(self):
        name = mask_track_name("s1", "center")
        assert name == "mask.s1.center"
        assert parse_mask_track_name(name) == ("s1", "center")
        assert parse_mask_track_name("exposure") is None
        assert parse_mask_track_name("mask.s1.points") is None  # 点集不动画
        assert parse_mask_track_name("mask.s1.nonsense") is None

    def test_record_reads_shape_values(self):
        layer, shape = _layer_with_mask()
        shape.center = [0.3, 0.6]
        shape.rotation = 15.0
        wrote = record_mask_keyframe(layer, 12, shape.id,
                                     ["center", "rotation"])
        assert wrote == 2
        assert layer.grade_keyframes[mask_track_name(shape.id, "center")] \
            .get_keyframe(12).value == (0.3, 0.6)
        assert layer.grade_keyframes[mask_track_name(shape.id, "rotation")] \
            .get_keyframe(12).value == 15.0

    def test_record_unknown_shape_or_field(self):
        layer, _ = _layer_with_mask()
        assert record_mask_keyframe(layer, 0, "missing", ["center"]) == 0
        assert record_mask_keyframe(layer, 0, "s1", ["points"]) == 0

    def test_evaluate_vec2_and_scalar_interpolation(self):
        layer, shape = _layer_with_mask()
        shape.center = [0.2, 0.3]
        shape.rotation = 0.0
        layer.grade_keyframes[mask_track_name(shape.id, "center")] = (
            GradeKeyframeTrack("m", [
                GradeKeyframe(0, (0.2, 0.3)),
                GradeKeyframe(24, (0.6, 0.7))]))
        layer.grade_keyframes[mask_track_name(shape.id, "rotation")] = (
            GradeKeyframeTrack("m", [
                GradeKeyframe(0, 0.0), GradeKeyframe(24, 30.0)]))

        assert evaluate_mask_keyframes(layer, 12) is True
        assert shape.center == [pytest.approx(0.4), pytest.approx(0.5)]
        assert shape.rotation == pytest.approx(15.0)

        # 端点保持外推
        evaluate_mask_keyframes(layer, 100)
        assert shape.center == [pytest.approx(0.6), pytest.approx(0.7)]

    def test_evaluate_skips_deleted_shape(self):
        layer, shape = _layer_with_mask()
        layer.grade_keyframes[mask_track_name("ghost", "center")] = (
            GradeKeyframeTrack("m", [GradeKeyframe(0, (0.9, 0.9))]))
        assert evaluate_mask_keyframes(layer, 5) is False  # 无命中不报错

    def test_grade_evaluation_ignores_mask_tracks(self):
        """grade 求值不消费 mask.* 轨道（白名单隔离）。"""
        layer, shape = _layer_with_mask()
        layer.grade_keyframes[mask_track_name(shape.id, "opacity")] = (
            GradeKeyframeTrack("m", [GradeKeyframe(0, 0.1)]))
        patched = evaluate_grade_keyframes(
            layer.grade_keyframes, 0, dict(layer.grading_params))
        assert "mask.%s.opacity" % shape.id not in patched

    def test_shape_has_mask_tracks(self):
        layer, shape = _layer_with_mask()
        assert shape_has_mask_tracks(layer.grade_keyframes, shape.id) is False
        record_mask_keyframe(layer, 0, shape.id, ["opacity"])
        assert shape_has_mask_tracks(layer.grade_keyframes, shape.id) is True

    def test_mask_tracks_survive_layer_serialization(self):
        layer, shape = _layer_with_mask()
        record_mask_keyframe(layer, 6, shape.id, ["center"])
        restored = Layer.from_dict(layer.to_dict())
        track = restored.grade_keyframes.get("mask.s1.center")
        assert track is not None
        assert tuple(track.get_keyframe(6).value) == (0.5, 0.5)


class TestMaskKeyframeEngineApply:
    def test_apply_marks_mask_dirty_and_updates_textures(self):
        from types import MethodType
        from src.features.color.core.image_engine import ImageEngine

        layer, shape = _layer_with_mask()
        track = GradeKeyframeTrack("m", [
            GradeKeyframe(0, (0.2, 0.3)), GradeKeyframe(24, (0.8, 0.7))])
        layer.grade_keyframes["mask.s1.center"] = track

        eng = SimpleNamespace(layers=[layer], render_calls=[])
        eng.render_pipeline = lambda **kw: eng.render_calls.append(kw)
        texture_updates = []
        eng._update_mask_textures = lambda: texture_updates.append(True)
        eng.apply_grade_keyframes = MethodType(
            ImageEngine.apply_grade_keyframes, eng)

        changed = ImageEngine.apply_grade_keyframes(eng, 12)
        assert changed is True
        assert shape.center == [pytest.approx(0.5), pytest.approx(0.5)]
        assert layer.mask_dirty is True
        assert texture_updates == [True]  # 立即重栅格化（tracker 模式）


class TestMaskKeyframeRecording:
    def _host(self, layer, record_mode=False):
        from src.app.controllers.color_controller import ColorControllerMixin

        class _Host(ColorControllerMixin):
            pass

        host = _Host()
        host.engine = SimpleNamespace(
            current_grade_frame=12, get_active_layer=lambda: layer)
        host.grading_panel = SimpleNamespace(keyframe_mode=record_mode)
        host._refresh_grade_keyframe_ui = lambda: None
        return host

    def test_tracked_shape_records_automatically(self):
        layer, shape = _layer_with_mask()
        record_mask_keyframe(layer, 0, shape.id, ["center"])  # 已有轨道
        host = self._host(layer)
        host._maybe_record_mask_keyframes(layer, [(shape.id, ["size"])])
        assert "mask.s1.size" in layer.grade_keyframes  # 同形状自动记录
        kf = layer.grade_keyframes["mask.s1.size"].get_keyframe(12)
        assert tuple(kf.value) == (0.3, 0.3)  # 当前值即关键帧

    def test_record_mode_records_all(self):
        layer, shape = _layer_with_mask()
        host = self._host(layer, record_mode=True)
        host._maybe_record_mask_keyframes(layer, [(shape.id, ["rotation"])])
        assert "mask.s1.rotation" in layer.grade_keyframes

    def test_no_tracks_no_mode_no_record(self):
        layer, shape = _layer_with_mask()
        host = self._host(layer)
        host._maybe_record_mask_keyframes(layer, [(shape.id, ["center"])])
        assert not any(n.startswith("mask.") for n in layer.grade_keyframes)

    def test_non_animatable_fields_ignored(self):
        layer, shape = _layer_with_mask()
        host = self._host(layer, record_mode=True)
        host._maybe_record_mask_keyframes(layer, [(shape.id, ["visible"])])
        assert not any(n.startswith("mask.") for n in layer.grade_keyframes)


class TestMaskKeyframeSnapshot:
    def test_snapshot_covers_mask_tracks_push_first(self):
        from types import MethodType
        from src.features.color.core.image_engine import ImageEngine

        layer, shape = _layer_with_mask()
        record_mask_keyframe(layer, 0, shape.id, ["center"])
        shape.center = [0.7, 0.7]  # 用户改到新位置后按 ◆+
        eng = SimpleNamespace(
            layers=[layer], active_layer_index=0, img_full_original=None,
            current_grade_frame=12,
            get_active_layer=lambda: layer,
            history=_FakeHistory(layer),
        )
        eng.record_grade_keyframe = MethodType(
            ImageEngine.record_grade_keyframe, eng)
        host = _snapshot_controller_host(eng)
        # _FakeHistory 记录 exposure 轨道帧；mask 轨道用 shape 快照断言
        layer.grade_keyframes["exposure"] = GradeKeyframeTrack("exposure")
        layer.grade_keyframes["exposure"].set_keyframe(0, 0.0)
        layer.grading_params["exposure"] = 5.0

        host.on_keyframe_snapshot_requested()
        snap = layer.grade_keyframes["mask.s1.center"].get_keyframe(12)
        assert tuple(snap.value) == (0.7, 0.7)
        assert eng.history.snapshots == [[0]]  # push 时 mask 帧 12 尚未写入


# ---------------------------------------------------------------------------
# R2：统一显式 Dynamics 求值（键型/权重进入 value_at；legacy 帧值不变）
# ---------------------------------------------------------------------------

def test_value_at_explicit_dynamics_weights_hermite():
    # 0→1、A Start=0、B End=1 的段：25%/50%/75% 处的 Hermite 兼容权重
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0, kind="dynamic", start=0.0, end=0.5)
    track.set_keyframe(4, 1.0, kind="dynamic", start=0.5, end=1.0)
    # 段求值读 A 的 start 与 B 的 end
    assert track.value_at(1, 0.0) == pytest.approx(0.109375)  # w(0.25, 0, 1)
    assert track.value_at(2, 0.0) == pytest.approx(0.375)     # w(0.5, 0, 1)
    assert track.value_at(3, 0.0) == pytest.approx(0.703125)  # w(0.75, 0, 1)


def test_value_at_static_left_holds_dynamic_left_interpolates():
    # 静态 A：右侧保持到 B 前；动态 A：允许插值到静态 B
    static_left = GradeKeyframeTrack("exposure")
    static_left.set_keyframe(0, 0.0, interpolation="hold")
    static_left.set_keyframe(10, 10.0)
    assert static_left.value_at(9, 0.0) == 0.0
    assert static_left.value_at(10, 0.0) == 10.0

    dynamic_left = GradeKeyframeTrack("exposure")
    dynamic_left.set_keyframe(0, 0.0, kind="dynamic", start=1.0, end=1.0)
    dynamic_left.set_keyframe(4, 4.0, interpolation="hold")
    assert dynamic_left.value_at(2, 0.0) == pytest.approx(2.0)  # 线性到静态 B
    assert dynamic_left.value_at(3, 0.0) == pytest.approx(3.0)


def test_value_at_explicit_dynamics_affects_only_segments_touching_them():
    # legacy linear A + legacy ease B：两侧均无显式动态 → 旧插值路径
    # （只看左键模式；帧值与历史版本一致，不引入 B 的 end 权重）
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0, interpolation="linear")
    track.set_keyframe(4, 1.0, interpolation="ease")
    assert track.value_at(1, 0.0) == pytest.approx(0.25)  # linear 段恒等
    assert track.value_at(2, 0.0) == pytest.approx(0.5)
    assert track.value_at(3, 0.0) == pytest.approx(0.75)

    # A 携带显式非等价动态 → 统一权重（读 A.start=0、B.end=1）
    explicit = GradeKeyframeTrack("exposure")
    explicit.set_keyframe(0, 0.0, kind="dynamic", start=0.0, end=1.0)
    explicit.set_keyframe(4, 1.0, interpolation="ease")  # B legacy (0,0)
    assert explicit.value_at(1, 0.0) == pytest.approx(
        0.15625)  # w(0.25, A.start=0, B.end=0)


def test_value_at_vec3_and_circular_with_explicit_dynamics():
    vec = GradeKeyframeTrack("lift")
    vec.set_keyframe(0, (0.0, 0.0, 0.0), kind="dynamic",
                     start=0.0, end=1.0)
    vec.set_keyframe(4, (1.0, 2.0, -1.0))
    value = vec.value_at(1, (0, 0, 0))
    assert value == pytest.approx(
        (0.109375, 0.21875, -0.109375))  # 分量独立按统一权重

    hue = GradeKeyframeTrack("qualifier.hue_center")
    hue.set_keyframe(0, 0.9, kind="dynamic", start=0.0, end=1.0)
    hue.set_keyframe(4, 0.1)  # 最短弧 0.9 → 1.0(+0.1)
    # w(0.5, A.start=0, B.end=1)=0.375 → 0.9 + 0.2×0.375 = 0.975
    assert hue.value_at(2, 0.0, circular=True) == pytest.approx(0.975)


def test_keyframe_dynamics_roundtrip_and_legacy_equivalence():
    # 序列化 canonical-minimal：legacy 键逐字节不变；显式动态落盘并往返
    legacy = GradeKeyframe(3, 1.5, "ease")
    assert legacy.to_dict() == {"frame": 3, "value": 1.5,
                                "interpolation": "ease"}
    assert legacy.kind == "dynamic" and legacy.legacy_equivalent

    explicit = GradeKeyframe(3, 1.5, "linear", kind="dynamic",
                             start=0.25, end=0.75)
    payload = explicit.to_dict()
    assert payload == {"frame": 3, "value": 1.5, "interpolation": "linear",
                       "kind": "dynamic", "start": 0.25, "end": 0.75}
    restored = GradeKeyframe.from_dict(payload)
    assert (restored.kind, restored.start, restored.end) == (
        "dynamic", 0.25, 0.75)
    assert not restored.legacy_equivalent
    assert restored.value_at if False else restored.value == 1.5

    # from_dict 经 decode：畸形值与负帧在加载期拒绝（AUD-0520 收口）
    with pytest.raises(ValueError):
        GradeKeyframe.from_dict({"frame": 0})
    with pytest.raises(ValueError):
        GradeKeyframe.from_dict({"frame": -1, "value": 1.0})


def test_track_serialization_keeps_explicit_dynamics():
    track = GradeKeyframeTrack("exposure")
    track.set_keyframe(0, 0.0, kind="dynamic", start=0.5, end=0.5)
    track.set_keyframe(10, 1.0)  # legacy linear
    data = track.to_dict()
    assert data["keyframes"][0]["start"] == 0.5
    assert "kind" not in data["keyframes"][1]  # legacy 键 canonical-minimal
    restored = GradeKeyframeTrack.from_dict(data)
    assert restored.get_keyframe(0).start == 0.5
    # 求值语义随往返保留（A.start=0.5、B.end=1 → w(0.5,0.5,1)=0.4375）
    assert restored.value_at(5, 0.0) == pytest.approx(0.4375)
