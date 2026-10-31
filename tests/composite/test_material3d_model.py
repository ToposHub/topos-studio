"""F4 专业材质编辑系统单测（master_plan §7 / §21.7）。

覆盖：
1. MaterialEditModel：完整 PBR schema 字段、signature 稳定性。
2. TextureSlot / UVTransform：贴图槽描述、UV 变换矩阵。
3. edit_model_from_material_data：从导入材质派生编辑模型（非破坏）。
4. MaterialOverride：覆盖应用、空覆盖、序列化签名。
5. 材质预设库。

纯 Python / numpy，无 GL/PyQt 依赖。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render3d import scene_graph as sg
from src.features.composite.render3d.material_model import (
    MaterialEditModel, TextureSlot, UVTransform, MaterialOverride,
    edit_model_from_material_data, apply_override,
    MaterialPreset, default_material_presets,
    COLOR_SPACE_SRGB, COLOR_SPACE_LINEAR,
    ALPHA_MODE_OPAQUE, ALPHA_MODE_BLEND, ALPHA_MODE_MASK,
    CULL_MODE_BACK, CULL_MODE_NONE,
    NORMAL_FORMAT_OPENGL, NORMAL_FORMAT_DIRECTX,
    material_override_to_dict, material_override_from_dict,
    decode_material_overrides, encode_material_overrides,
    material_summary, apply_override_to_material_data,
)
from src.features.composite.nodes.scene3d_node import Scene3DNode

pytestmark = pytest.mark.composite_core


# ── MaterialEditModel ────────────────────────────────────────────────

class TestMaterialEditModel:
    def test_defaults(self):
        m = MaterialEditModel()
        assert m.metallic == 0.0
        assert m.roughness == 0.5
        assert m.ior == 1.5
        assert m.alpha_mode == ALPHA_MODE_OPAQUE
        assert m.cull_mode == CULL_MODE_BACK
        assert m.normal_format == NORMAL_FORMAT_OPENGL
        assert m.opacity == 1.0

    def test_has_any_texture_empty(self):
        m = MaterialEditModel()
        assert not m.has_any_texture()

    def test_has_any_texture_with_basecolor(self):
        m = MaterialEditModel()
        m.base_color_slot.path = "/a.png"
        assert m.has_any_texture()

    def test_all_slots_count(self):
        m = MaterialEditModel()
        assert len(m.all_slots()) == 9

    def test_signature_stable(self):
        m1 = MaterialEditModel(metallic=0.3, roughness=0.7)
        m2 = MaterialEditModel(metallic=0.3, roughness=0.7)
        assert m1.signature() == m2.signature()

    def test_signature_differs(self):
        m1 = MaterialEditModel(metallic=0.3)
        m2 = MaterialEditModel(metallic=0.8)
        assert m1.signature() != m2.signature()

    def test_base_color_normalized(self):
        m = MaterialEditModel(base_color=[1, 0.5, 0.2])
        assert m.base_color.shape == (3,)
        assert m.base_color.dtype == np.float32


# ── TextureSlot / UVTransform ────────────────────────────────────────

class TestTextureSlot:
    def test_empty_slot(self):
        s = TextureSlot()
        assert s.is_empty()
        assert s.path == ""

    def test_color_space_defaults(self):
        s = TextureSlot()
        assert s.color_space == COLOR_SPACE_LINEAR
        s2 = TextureSlot(color_space=COLOR_SPACE_SRGB)
        assert s2.color_space == COLOR_SPACE_SRGB

    def test_signature_with_path(self):
        s1 = TextureSlot(path="/a.png")
        s2 = TextureSlot(path="/b.png")
        assert s1.signature() != s2.signature()


class TestUVTransform:
    def test_identity_matrix(self):
        t = UVTransform()
        m = t.to_matrix()
        assert np.allclose(m, np.eye(3, dtype=np.float32), atol=1e-5)

    def test_offset_in_matrix(self):
        t = UVTransform(offset_u=0.5, offset_v=0.25)
        m = t.to_matrix()
        # 变换 (0,0) → (0.5, 0.25)
        uv = np.array([0, 0, 1], dtype=np.float32)
        result = m @ uv
        assert np.allclose(result[:2], [0.5, 0.25], atol=1e-5)

    def test_scale_in_matrix(self):
        t = UVTransform(scale_u=2.0, scale_v=0.5)
        m = t.to_matrix()
        uv = np.array([1, 1, 1], dtype=np.float32)
        result = m @ uv
        assert np.allclose(result[:2], [2.0, 0.5], atol=1e-5)

    def test_nonuniform_scale_plus_rotation_trs(self):
        """非均匀缩放 + 旋转的 T·R·S 正确性（回归 MODERATE）。

        之前矩阵是 T·S·R（scale 乘到旋转行），旋转后方向错误。
        正确的 T·R·S：先 scale，再 rotate。
        su=2, sv=1, rotation=90° → (1,0) 先 scale→(2,0) 再 rotate90→(0,2)
        """
        import numpy as np
        t = UVTransform(scale_u=2.0, scale_v=1.0, rotation=np.pi / 2)
        m = t.to_matrix()
        uv = np.array([1, 0, 1], dtype=np.float32)
        result = m @ uv
        # scale: (1,0)→(2,0)；rotate 90°: (2,0)→(0,2)
        assert np.allclose(result[:2], [0.0, 2.0], atol=1e-5)

    def test_signature_stable(self):
        t1 = UVTransform(offset_u=0.1, rotation=0.5)
        t2 = UVTransform(offset_u=0.1, rotation=0.5)
        assert t1.signature() == t2.signature()


# ── edit_model_from_material_data ────────────────────────────────────

class TestEditModelFromMaterialData:
    def test_scalar_fields_copied(self):
        mat = sg.MaterialData(
            base_color=[0.5, 0.3, 0.1],
            metallic=0.8,
            roughness=0.2,
        )
        model = edit_model_from_material_data(mat, name="test")
        assert model.name == "test"
        assert np.allclose(model.base_color, [0.5, 0.3, 0.1])
        assert model.metallic == 0.8
        assert model.roughness == 0.2

    def test_texture_paths_copied(self):
        mat = sg.MaterialData()
        mat.base_color_tex.path = "/bc.png"
        mat.normal_tex.path = "/n.png"
        model = edit_model_from_material_data(mat)
        assert model.base_color_slot.path == "/bc.png"
        assert model.normal_slot.path == "/n.png"
        # 颜色空间推断
        assert model.base_color_slot.color_space == COLOR_SPACE_SRGB
        assert model.normal_slot.color_space == COLOR_SPACE_LINEAR

    def test_non_destructive(self):
        """派生不修改原 MaterialData。"""
        mat = sg.MaterialData(metallic=0.5)
        orig_metallic = mat.metallic
        model = edit_model_from_material_data(mat)
        model.metallic = 0.9
        assert mat.metallic == orig_metallic  # 原材质不变


# ── MaterialOverride ─────────────────────────────────────────────────

class TestMaterialOverride:
    def test_identity_override(self):
        o = MaterialOverride()
        assert o.is_identity()

    def test_set_scalar(self):
        o = MaterialOverride(target_material_id="m1")
        o.set_scalar("metallic", 0.9)
        o.set_scalar("roughness", 0.1)
        assert not o.is_identity()
        assert o.overrides["metallic"] == 0.9

    def test_apply_override_scalar(self):
        model = MaterialEditModel(metallic=0.1, roughness=0.8)
        override = MaterialOverride(target_material_id="m1")
        override.set_scalar("metallic", 0.9)
        result = apply_override(model, override)
        assert result.metallic == 0.9  # 覆盖生效
        assert result.roughness == 0.8  # 未覆盖保留
        assert model.metallic == 0.1  # 原模型不变（非破坏）

    def test_apply_override_color(self):
        model = MaterialEditModel()
        override = MaterialOverride()
        override.set_scalar("base_color", [1.0, 0.0, 0.0])
        result = apply_override(model, override)
        assert np.allclose(result.base_color, [1, 0, 0])

    def test_apply_override_slot(self):
        model = MaterialEditModel()
        override = MaterialOverride()
        new_slot = TextureSlot(path="/new.png")
        override.set_slot("base_color_slot", new_slot)
        result = apply_override(model, override)
        assert result.base_color_slot.path == "/new.png"

    def test_override_signature(self):
        o1 = MaterialOverride(target_material_id="m1")
        o1.set_scalar("metallic", 0.5)
        o2 = MaterialOverride(target_material_id="m1")
        o2.set_scalar("metallic", 0.5)
        o3 = MaterialOverride(target_material_id="m1")
        o3.set_scalar("metallic", 0.8)
        assert o1.signature() == o2.signature()
        assert o1.signature() != o3.signature()

    def test_json_roundtrip_keeps_scalar_and_slot(self):
        override = MaterialOverride(
            target_material_id="body",
            target_material_name="Body",
        )
        override.set_scalar("base_color", [1.0, 0.0, 0.25])
        override.set_scalar("metallic", 0.9)
        override.set_slot(
            "base_color_slot",
            TextureSlot(path="textures/body.png", color_space=COLOR_SPACE_SRGB),
        )
        restored = material_override_from_dict(material_override_to_dict(override))
        assert restored.target_material_id == "body"
        assert restored.overrides["metallic"] == 0.9
        assert restored.slot_overrides["base_color_slot"].path == "textures/body.png"
        assert restored.slot_overrides["base_color_slot"].color_space == COLOR_SPACE_SRGB

    def test_override_document_is_deterministic(self):
        override = MaterialOverride(target_material_id="body")
        override.set_scalar("roughness", 0.2)
        encoded = encode_material_overrides({"body": override})
        decoded, diagnostics = decode_material_overrides(encoded)
        assert diagnostics == ()
        assert list(decoded) == ["body"]
        assert decoded["body"].overrides["roughness"] == 0.2
        assert encode_material_overrides(decoded) == encoded

    def test_invalid_override_document_is_diagnostic_not_exception(self):
        overrides, diagnostics = decode_material_overrides("{not-json")
        assert overrides == {}
        assert diagnostics == ("material.overrides_invalid_json",)

    def test_material_summary_reports_slots_and_missing_files(self, tmp_path):
        mat = sg.MaterialData()
        mat.material_id = "body"
        mat.name = "Body"
        mat.base_color_tex.path = "missing.png"
        summary = material_summary(mat, model_dir=str(tmp_path))
        assert summary["material_id"] == "body"
        assert summary["slots"]["base_color"]["color_space"] == COLOR_SPACE_SRGB
        assert summary["slots"]["base_color"]["missing"] is True

    def test_override_maps_to_render_material_copy(self):
        mat = sg.MaterialData(metallic=0.1, roughness=0.8)
        override = MaterialOverride(target_material_id="body")
        override.set_scalar("metallic", 0.95)
        override.set_scalar("opacity", 0.4)
        override.set_slot("normal_slot", TextureSlot(path="normal.png"))
        result = apply_override_to_material_data(mat, override)
        assert result.metallic == 0.95
        assert result.alpha == 0.4
        assert result.normal_tex.path == "normal.png"
        assert mat.metallic == 0.1

    def test_scene_bridge_resolves_override_by_material_slot_id(self):
        mat = sg.MaterialData(metallic=0.1, roughness=0.8, material_id="body")
        override = MaterialOverride(target_material_id="body")
        override.set_scalar("roughness", 0.15)
        effective = Scene3DNode._material_with_override(
            mat, {"material_id": "body", "node_id": "mesh"}, {"body": override})
        assert effective.roughness == 0.15
        assert mat.roughness == 0.8


# ── 材质预设库 ───────────────────────────────────────────────────────

class TestMaterialPresets:
    def test_default_presets_not_empty(self):
        presets = default_material_presets()
        assert len(presets) >= 5
        names = {p.name for p in presets}
        assert "Glass" in names
        assert "Polished Metal" in names

    def test_glass_preset_transparent(self):
        presets = default_material_presets()
        glass = next(p for p in presets if p.name == "Glass")
        assert glass.model.alpha_mode == ALPHA_MODE_BLEND
        assert glass.model.opacity < 1.0
        assert glass.model.transmission > 0.5

    def test_preset_to_dict(self):
        m = MaterialEditModel(name="x", metallic=0.5)
        p = MaterialPreset("x", "basic", m)
        d = p.to_dict()
        assert d["name"] == "x"
        assert d["metallic"] == 0.5
        assert "base_color" in d
