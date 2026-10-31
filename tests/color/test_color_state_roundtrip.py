"""P0-COLOR-02/03 验收：统一 ColorState 契约的完整 round-trip。

验收标准（计划表）：核心调色、曲线、HSL、Mixer、LUT、Soft Clip、mask、
Qualifier、Warper、effects、plugin、RAW、active layer、visibility、
blend、opacity、baked 状态在「捕获 → 序列化 → 反序列化 → 恢复」后逐字段一致。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator
from src.features.color.core.layer import Layer, MaskShape
from src.shared.project.color_stack import (
    COLOR_STACK_SCHEMA_VERSION,
    ColorStackData,
)


def _make_rich_layer() -> Layer:
    """构造覆盖全部高级状态域的图层。"""
    layer = Layer("Hero Grade")
    layer.visible = False
    layer.opacity = 0.72
    layer.blend_mode = "Soft Light"
    layer.grading_params = {
        "lift": (0.01, -0.02, 0.03),
        "gamma": (-0.01, 0.02, -0.03),
        "gain": (0.05, 0.04, 0.06),
        "exposure": 0.35,
        "temp": 12.0,
        "tint": -4.0,
        "contrast": 18.0,
        "shadow": -6.0,
        "highlights": 8.0,
        "saturation": 11.0,
        "vibrance": 7.0,
        "mid_detail": 5.0,
        "sharpness": 9.0,
        "hue_shift": 3.0,
    }
    layer.curves_data = {
        "RGB": [[0.0, 0.05], [0.5, 0.55], [1.0, 0.98]],
        "R": [[0.0, 0.0], [1.0, 0.9]],
    }
    layer.hsl_curve_data = {
        "hue_vs_hue": [[0.0, 0.0], [0.5, 0.1], [1.0, 0.0]],
        "lum_vs_sat": [[0.0, 0.0], [1.0, -0.1]],
    }
    layer.mixer_params = {"red": [1.0, 0.1, -0.05]}
    layer.soft_clip_params = {
        "enabled": True, "high": 0.95, "white_point": 1.2,
        "shoulder_strength": 1.5, "low": -0.02, "black_point": -0.15,
        "toe_strength": 0.8, "exposure": 0.1,
    }
    layer.active_lut_path = "/luts/look.cube"
    layer.lut_intensity = 0.65

    # 蒙版（矢量形状，BITMAP 光栅依赖 assets_dir，契约外）
    shape = MaskShape(MaskShape.CIRCLE)
    shape.name = "Face Window"
    shape.visible = True
    shape.invert = True
    shape.operation = MaskShape.OP_ADD
    shape.feather = 42
    shape.opacity = 0.9
    shape.blur = 3.0
    shape.clean_black = 0.1
    shape.clean_white = 0.05
    shape.center = [0.31, 0.28]
    shape.size = [0.4, 0.5]
    shape.rotation = 15.0
    shape.softness = 0.25
    layer.mask_shapes = [shape]

    # 限定器（真实 schema 键名的子集；from_dict 补齐其余默认键）
    layer.qualifier_params = {
        "hue_enabled": True,
        "hue_center": 0.08, "hue_width": 0.12,
        "sat_low": 0.2, "sat_high": 0.9,
        "lum_low": 0.3, "lum_high": 0.8,
        "blur": 4.0,
    }
    # 变形器（网格位移数组，Layer 约定为 ndarray/list of floats）
    import numpy as np
    layer.warper_params = np.array(
        [[0.0, 0.0, 0.0], [0.1, 0.1, 0.12], [0.0, 1.0, 0.0]],
        dtype=np.float32,
    )
    # 效果
    layer.effects_params = {
        "enabled": True, "mode": "sharpen",
        "sharpen": 0.4, "texture": 0.2, "halo": 0.1,
        "radius": 3.0, "iterations": 2,
    }
    # 插件栈
    layer.plugins = [
        {
            "instance_id": "inst-1",
            "plugin_id": "glow.tglsl",
            "params": {"intensity": 0.5},
            "enabled": True,
            "type": "glsl",
        }
    ]
    # AI 抠像标记 + 烘焙状态
    layer.ai_matte_enabled = True
    layer.ai_matte_operation = "subtract"
    layer.is_baked = True
    layer.baked_file_name = "baked_layer0.exr"
    return layer


class _FakeEngine:
    """最小 engine 替身：hydrate/collect 只依赖这几个属性。"""

    def __init__(self, layers, active_layer_index=0, raw_params=None):
        self.layers = layers
        self.active_layer_index = active_layer_index
        self.current_raw_params = raw_params or {}
        self.tracking_store = None


class TestFullStateRoundTrip:
    """完整 Layer 快照经 ColorStack 序列化往返后逐字段一致。"""

    def test_rich_layer_survives_stack_roundtrip(self):
        layer = _make_rich_layer()
        engine = _FakeEngine(
            [layer], active_layer_index=0,
            raw_params={"enabled": True, "exposure": 0.25},
        )

        hydrator = ColorStackHydrator()
        stack = hydrator.collect_stack_from_engine_and_panels(
            "stack-1", engine, panels={}
        )
        payload = stack.to_dict()
        assert payload["schema_version"] == COLOR_STACK_SCHEMA_VERSION
        assert payload["layers"], "完整 Layer 快照必须写入 payload"

        restored_stack = ColorStackData.from_dict(payload)
        assert restored_stack.layers == stack.layers
        assert restored_stack.active_layer_index == 0
        assert restored_stack.raw_params == {"enabled": True, "exposure": 0.25}

        # 恢复到全新 engine，逐字段比对
        target = _FakeEngine([Layer("placeholder")])
        hydrator.apply_stack_to_engine(restored_stack, target)

        assert len(target.layers) == 1
        out = target.layers[0]
        assert out.name == "Hero Grade"
        assert out.visible is False
        assert out.opacity == pytest.approx(0.72)
        assert out.blend_mode == "Soft Light"
        for key, value in layer.grading_params.items():
            assert out.grading_params[key] == pytest.approx(
                list(value) if isinstance(value, tuple) else value
            ), f"grading.{key} 丢失"
        assert out.curves_data == layer.curves_data
        assert out.hsl_curve_data == layer.hsl_curve_data
        assert out.mixer_params == layer.mixer_params
        # from_dict 会补齐默认键（如 contrast），按超集比较
        assert out.soft_clip_params.items() >= layer.soft_clip_params.items()
        assert out.active_lut_path == "/luts/look.cube"
        assert out.lut_intensity == pytest.approx(0.65)

        # 蒙版
        assert len(out.mask_shapes) == 1
        m_out, m_in = out.mask_shapes[0], layer.mask_shapes[0]
        assert m_out.type == MaskShape.CIRCLE
        assert m_out.name == "Face Window"
        assert m_out.invert is True
        assert m_out.feather == 42
        assert m_out.center == [0.31, 0.28]
        assert m_out.size == [0.4, 0.5]
        assert m_out.rotation == 15.0

        # 限定器 / 变形器 / 效果 / 插件 / AI 抠像 / 烘焙
        assert out.qualifier_params.items() >= layer.qualifier_params.items()
        assert out.warper_params is not None
        assert np.allclose(out.warper_params, layer.warper_params)
        assert out.effects_params == layer.effects_params
        assert out.plugins == layer.plugins
        assert out.ai_matte_enabled is True
        assert out.ai_matte_operation == "subtract"
        assert out.is_baked is True
        assert out.baked_file_name == "baked_layer0.exr"

    def test_multi_layer_and_active_index_roundtrip(self):
        l1, l2 = _make_rich_layer(), Layer("Base")
        l2.opacity = 0.5
        engine = _FakeEngine([l1, l2], active_layer_index=1)

        hydrator = ColorStackHydrator()
        stack = hydrator.collect_stack_from_engine_and_panels(
            "stack-2", engine, panels={}
        )
        payload = stack.to_dict()
        assert payload["active_layer_index"] == 1

        restored = ColorStackData.from_dict(payload)
        target = _FakeEngine([Layer("x")])
        hydrator.apply_stack_to_engine(restored, target)

        assert [l.name for l in target.layers] == ["Hero Grade", "Base"]
        assert target.active_layer_index == 1
        assert target.layers[1].opacity == pytest.approx(0.5)
        # 平铺精简键从活动图层（Base）派生，两套视图一致
        assert stack.grading_params["exposure"] == pytest.approx(0.0)

    def test_update_color_stack_carries_layers(self):
        """EditProject.update_color_stack 的 layers 通道 + undo 旧值。"""
        from src.features.edit.core.commands import UpdateColorStackCommand
        from src.features.edit.core.project import EditProject

        layer = _make_rich_layer()
        snapshot = [layer.to_dict(None)]
        project, stack_id = EditProject.create(name="roundtrip").create_color_stack()
        cmd_kwargs_stack_id = stack_id

        cmd = UpdateColorStackCommand.create(
            stack_id=cmd_kwargs_stack_id,
            grading_params={"exposure": 0.5},
            layers=snapshot,
            active_layer_index=0,
            raw_params={"enabled": False},
        )
        project2 = cmd.apply(project)
        stack = project2.color_stacks[stack_id]
        assert stack.layers == tuple(snapshot)
        assert stack.raw_params == {"enabled": False}
        assert stack.grading_params == {"exposure": 0.5}

        project3 = cmd.undo(project2)
        assert project3.color_stacks[stack_id].layers == ()
        assert project3.color_stacks[stack_id].grading_params != {"exposure": 0.5}

    def test_legacy_v1_stack_still_broadcasts(self):
        """旧格式（无 layers 键）走平铺广播，不回归。"""
        legacy = ColorStackData.from_dict({
            "stack_id": "legacy",
            "grading_params": {"exposure": 0.4},
            "curves_data": {"RGB": [[0, 0], [1, 1]]},
            "lut_path": "/old.cube",
            "lut_intensity": 0.5,
        })
        assert legacy.layers == ()

        target = _FakeEngine([Layer("a"), Layer("b")])
        ColorStackHydrator().apply_stack_to_engine(legacy, target)
        for out in target.layers:
            assert out.grading_params["exposure"] == pytest.approx(0.4)
            assert out.curves_data == {"RGB": [[0, 0], [1, 1]]}
            assert out.active_lut_path == "/old.cube"

    def test_v1_dict_without_version_key_loads(self):
        """未知键忽略、版本键缺失容错。"""
        stack = ColorStackData.from_dict({
            "stack_id": "old",
            "unknown_future_key": {"whatever": 1},
        })
        assert stack.stack_id == "old"
        assert stack.layers == ()


# ---------------------------------------------------------------------------
# P1-COLOR-13: per-clip 视频降噪参数（防串扰 + roundtrip + 面板同步）
# ---------------------------------------------------------------------------

class TestVideoDenoiseStackState:
    VD = {"spatial": {"enabled": True, "radius": 3},
          "temporal": {"enabled": False}}

    def test_stack_roundtrip(self):
        from src.shared.project.color_stack import ColorStackData
        stack = ColorStackData.create(asset_id="a1")
        stack = stack.with_video_denoise(dict(self.VD))
        data = stack.to_dict()
        assert data.get("video_denoise") == self.VD
        restored = ColorStackData.from_dict(data)
        assert restored.video_denoise == self.VD

        # 未设置时不写键；from_dict 得 None
        plain = ColorStackData.create(asset_id="a1")
        assert "video_denoise" not in plain.to_dict()
        assert ColorStackData.from_dict(plain.to_dict()).video_denoise is None

    def test_hydrator_restores_and_resets(self):
        """核心防串扰：切 clip 时先清引擎会话，再按栈恢复。"""
        from types import SimpleNamespace
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )

        hydrator = ColorStackHydrator()
        engine = SimpleNamespace(
            video_denoise_params="STALE_FROM_PREVIOUS_CLIP",
            layers=[],
            set_video_denoise_state=None)
        calls = []
        engine.set_video_denoise_state = lambda state: calls.append(state)

        # 栈无降噪字段 → 引擎被重置（不残留上一个 clip 的参数）
        plain = SimpleNamespace(layers=(), video_denoise=None)
        hydrator.apply_stack_to_engine(plain, engine)
        assert engine.video_denoise_params is None
        assert calls == []

        # 栈带参数 → 重置后恢复
        stack = SimpleNamespace(layers=(), video_denoise=dict(self.VD))
        hydrator.apply_stack_to_engine(stack, engine)
        assert engine.video_denoise_params is None  # setter 已接管
        assert calls == [self.VD]

    def test_hydrator_syncs_panel(self):
        from types import SimpleNamespace
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        hydrator = ColorStackHydrator()
        panel_calls = []
        panel = SimpleNamespace(
            set_video_params_dict=lambda d: panel_calls.append(d))
        stack = SimpleNamespace(video_denoise=dict(self.VD))
        hydrator._apply_video_denoise_to_panel(stack, panel)
        assert panel_calls == [self.VD]
        # 无参数/无面板 → no-op
        hydrator._apply_video_denoise_to_panel(
            SimpleNamespace(video_denoise=None), panel)
        hydrator._apply_video_denoise_to_panel(stack, None)
        assert panel_calls == [self.VD]
