"""F9 AOV / Render Pass 单测（master_plan §12 / §21.12）。

覆盖：
1. AOVPass enum + labels。
2. AOVConfig：启用 pass、计数、signature。
3. encode_id_to_rgba / decode_id_from_rgba：往返编解码。
4. IDAllocator：稳定分配、去重、reset。
5. ObjectRenderFlags：shadow catcher / holdout 工厂。

纯 Python，无 GL/PyQt 依赖。
"""
import json
import sys
import os
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from src.features.composite.render3d.aov import (
    AOVPass, AOVConfig, AOV_PASS_LABELS, AOV_FLOAT_PASSES, AOV_FIRST_BATCH,
    AOV_EXPORTABLE_PASSES,
    encode_id_to_rgba, decode_id_from_rgba, IDAllocator,
    ObjectRenderFlags, default_ground_shadow_catcher, default_holdout_object,
    CatcherMaterialData, MatteWorkflow, MATTE_PRESETS,
    CATCHER_TYPE_SHADOW, CATCHER_TYPE_REFLECTION, CATCHER_TYPE_CONTACT,
    default_shadow_catcher_material, default_reflection_catcher_material,
    default_contact_shadow_material,
    render_flags_from_role, render_role_from_flags,
    RENDER_ROLE_SOLID, RENDER_ROLE_HOLDOUT,
    RENDER_ROLE_SHADOW_CATCHER, RENDER_ROLE_MATTE,
    MATTE_USE_MASK, MATTE_USE_KEYER, MATTE_USE_DEPTH, MATTE_USE_ID,
)
from src.features.composite.render3d.cryptomatte import (
    CryptomatteManifest,
    build_cryptomatte_manifest,
    cryptomatte_float_id,
    murmurhash3_32,
)
from src.features.composite.nodes.scene3d_node import Scene3DNode

pytestmark = pytest.mark.composite_core


# ── AOVPass / labels ─────────────────────────────────────────────────

class TestAOVPass:
    def test_enum_values(self):
        assert AOVPass.BEAUTY.value == "beauty"
        assert AOVPass.DEPTH.value == "depth"
        assert AOVPass.OBJECT_ID.value == "object_id"
        assert AOVPass.MOTION_VECTOR.value == "motion_vector"

    def test_labels_complete(self):
        for p in AOVPass:
            assert p in AOV_PASS_LABELS

    def test_float_passes_subset(self):
        """AOV_FLOAT_PASSES 是 AOVPass 的子集。"""
        assert AOVPass.POSITION in AOV_FLOAT_PASSES
        assert AOVPass.UV in AOV_FLOAT_PASSES
        assert AOVPass.BEAUTY not in AOV_FLOAT_PASSES

    def test_first_batch_contains_essential(self):
        assert AOVPass.BEAUTY in AOV_FIRST_BATCH
        assert AOVPass.DEPTH in AOV_FIRST_BATCH
        assert AOVPass.NORMAL in AOV_FIRST_BATCH

    def test_motion_vector_is_exportable(self):
        assert AOVPass.MOTION_VECTOR in AOV_EXPORTABLE_PASSES

    def test_uv_is_exportable(self):
        assert AOVPass.UV in AOV_EXPORTABLE_PASSES

    def test_material_id_is_exportable(self):
        assert AOVPass.MATERIAL_ID in AOV_EXPORTABLE_PASSES


class TestCryptomatteContract:
    def test_murmurhash3_known_vector(self):
        assert murmurhash3_32("foo") == 0xF6A5C420

    def test_manifest_is_stable_and_json_safe(self):
        manifest = build_cryptomatte_manifest([
            {"name": "Root/Body"},
            {"node_name": "Root/Head"},
            {"name": "Root/Body"},
        ])
        assert isinstance(manifest, CryptomatteManifest)
        assert [entry.name for entry in manifest.entries] == [
            "Root/Body", "Root/Head"
        ]
        metadata = manifest.to_metadata()
        assert metadata["hash_algorithm"] == "MurmurHash3_32"
        assert metadata["object_count"] == 2
        assert json.loads(metadata["manifest"]) == manifest.manifest

    def test_float_id_preserves_hash_bits(self):
        import struct

        value = cryptomatte_float_id("Root/Body")
        bits = struct.unpack("<I", struct.pack("<f", value))[0]
        assert bits == murmurhash3_32("Root/Body")

    def test_scene3d_diagnostics_include_manifest_only_object_matte(self):
        metadata = Scene3DNode._build_cryptomatte_metadata(
            SimpleNamespace(flat_meshes=[
                {"node_name": "Root/Body", "node_id": "mesh-body"},
                {"node_name": "Root/Head", "node_id": "mesh-head"},
            ]),
            (SimpleNamespace(node_id="geo-1"),),
        )
        assert metadata["status"] == "manifest_only"
        assert metadata["aov_source"] == "object_id"
        assert metadata["object_count"] == 3
        assert "Root/Body" in json.loads(metadata["manifest"]).values()


# ── AOVConfig ────────────────────────────────────────────────────────

class TestAOVConfig:
    def test_default_only_beauty(self):
        c = AOVConfig()
        assert c.beauty is True
        assert c.depth is False
        passes = c.enabled_passes()
        assert passes == {AOVPass.BEAUTY}

    def test_enable_depth_normal(self):
        c = AOVConfig(depth=True, normal=True)
        passes = c.enabled_passes()
        assert AOVPass.BEAUTY in passes
        assert AOVPass.DEPTH in passes
        assert AOVPass.NORMAL in passes
        assert c.enabled_count() == 3

    def test_is_any_extra_enabled(self):
        assert not AOVConfig().is_any_extra_enabled()
        assert AOVConfig(depth=True).is_any_extra_enabled()

    def test_signature_stable(self):
        c1 = AOVConfig(depth=True, normal=True)
        c2 = AOVConfig(depth=True, normal=True)
        assert c1.signature() == c2.signature()

    def test_signature_differs(self):
        c1 = AOVConfig(depth=True)
        c2 = AOVConfig(normal=True)
        assert c1.signature() != c2.signature()


# ── id 编解码 ────────────────────────────────────────────────────────

class TestIDEncodeDecode:
    def test_encode_id_1(self):
        """id=1 → R=1/255, G=0, B=0。"""
        r, g, b, a = encode_id_to_rgba(1)
        assert abs(r - 1 / 255.0) < 1e-6
        assert g == 0.0
        assert b == 0.0
        assert a == 1.0

    def test_encode_id_256(self):
        """id=256 → R=0, G=1/255。"""
        r, g, b, a = encode_id_to_rgba(256)
        assert r == 0.0
        assert abs(g - 1 / 255.0) < 1e-6

    def test_roundtrip(self):
        """编解码往返：任意 id → rgba → id 一致。"""
        for oid in [0, 1, 42, 255, 256, 65535, 65536, 16777215]:
            r, g, b, a = encode_id_to_rgba(oid)
            decoded = decode_id_from_rgba(r, g, b)
            assert decoded == oid, f"roundtrip failed for id={oid}"

    def test_encode_clamps_large_id(self):
        """超过 0xFFFFFF 的 id 被 clamp。"""
        r, g, b, a = encode_id_to_rgba(0x1000000)  # 超限
        decoded = decode_id_from_rgba(r, g, b)
        assert decoded == 0xFFFFFF

    def test_encode_negative_clamps_to_zero(self):
        r, g, b, a = encode_id_to_rgba(-5)
        assert decode_id_from_rgba(r, g, b) == 0


# ── IDAllocator ──────────────────────────────────────────────────────

class TestIDAllocator:
    def test_allocate_unique(self):
        alloc = IDAllocator()
        id1 = alloc.allocate("a")
        id2 = alloc.allocate("b")
        assert id1 != id2
        assert id1 >= 1  # 从 1 开始（0 保留给背景）

    def test_allocate_stable(self):
        """同名重复分配返回相同 id。"""
        alloc = IDAllocator()
        id1 = alloc.allocate("mesh0")
        id2 = alloc.allocate("mesh0")
        assert id1 == id2

    def test_get_id_existing(self):
        alloc = IDAllocator()
        alloc.allocate("x")
        assert alloc.get_id("x") is not None

    def test_get_id_missing(self):
        alloc = IDAllocator()
        assert alloc.get_id("nonexistent") is None

    def test_reset(self):
        alloc = IDAllocator()
        alloc.allocate("a")
        alloc.reset()
        assert alloc.count() == 0
        assert alloc.get_id("a") is None

    def test_count(self):
        alloc = IDAllocator()
        alloc.allocate("a")
        alloc.allocate("b")
        alloc.allocate("a")  # 重复
        assert alloc.count() == 2  # 去重


# ── ObjectRenderFlags ────────────────────────────────────────────────

class TestObjectRenderFlags:
    def test_defaults(self):
        f = ObjectRenderFlags()
        assert f.visible_to_camera is True
        assert f.holdout is False
        assert f.shadow_catcher is False

    def test_signature_stable(self):
        f1 = ObjectRenderFlags(holdout=True)
        f2 = ObjectRenderFlags(holdout=True)
        assert f1.signature() == f2.signature()

    def test_ground_shadow_catcher_factory(self):
        f = default_ground_shadow_catcher()
        assert f.shadow_catcher is True
        assert f.visible_to_camera is False  # 自身透明

    def test_holdout_factory(self):
        f = default_holdout_object()
        assert f.holdout is True
        assert f.visible_to_camera is False

    def test_matte_object(self):
        f = ObjectRenderFlags(matte_object=True, matte_id=5)
        assert f.matte_object is True
        assert f.matte_id == 5

    @pytest.mark.parametrize("role, attr", [
        (RENDER_ROLE_SOLID, "visible_to_camera"),
        (RENDER_ROLE_HOLDOUT, "holdout"),
        (RENDER_ROLE_SHADOW_CATCHER, "shadow_catcher"),
        (RENDER_ROLE_MATTE, "matte_object"),
    ])
    def test_role_conversion(self, role, attr):
        flags = render_flags_from_role(role, matte_id=19)
        assert getattr(flags, attr) is True
        assert render_role_from_flags(flags) == role

    def test_unknown_role_is_safe_solid(self):
        flags = render_flags_from_role("future_role")
        assert flags == ObjectRenderFlags()
        assert render_role_from_flags(flags) == RENDER_ROLE_SOLID

    def test_matte_role_clamps_id(self):
        flags = render_flags_from_role(RENDER_ROLE_MATTE, matte_id=-4)
        assert flags.matte_id == 0


# ── F10-3 CatcherMaterialData ─────────────────────────────────────────

class TestCatcherMaterialData:
    def test_defaults(self):
        m = CatcherMaterialData()
        assert m.catcher_type == CATCHER_TYPE_SHADOW
        assert m.shadow_color == (0.0, 0.0, 0.0)
        assert m.shadow_intensity == 1.0
        assert m.ground_plane is True

    def test_signature_stability(self):
        m1 = CatcherMaterialData(shadow_intensity=0.8)
        m2 = CatcherMaterialData(shadow_intensity=0.8)
        assert m1.signature() == m2.signature()

    def test_signature_differs(self):
        m1 = CatcherMaterialData(catcher_type=CATCHER_TYPE_SHADOW)
        m2 = CatcherMaterialData(catcher_type=CATCHER_TYPE_REFLECTION)
        assert m1.signature() != m2.signature()

    def test_shadow_catcher_factory(self):
        m = default_shadow_catcher_material()
        assert m.catcher_type == CATCHER_TYPE_SHADOW
        assert m.ground_plane is True
        assert m.shadow_intensity == 1.0

    def test_reflection_catcher_factory(self):
        m = default_reflection_catcher_material()
        assert m.catcher_type == CATCHER_TYPE_REFLECTION
        assert m.shadow_intensity == 0.5

    def test_contact_shadow_factory(self):
        m = default_contact_shadow_material()
        assert m.catcher_type == CATCHER_TYPE_CONTACT
        assert m.ground_plane is False  # 接触阴影不限地面

    def test_intensity_in_signature(self):
        m1 = CatcherMaterialData(shadow_intensity=0.5)
        m2 = CatcherMaterialData(shadow_intensity=1.0)
        assert m1.signature() != m2.signature()

    def test_plane_params_in_signature(self):
        m1 = CatcherMaterialData(plane_height=0.0, plane_size=0.0)
        m2 = CatcherMaterialData(plane_height=1.0, plane_size=10.0)
        assert m1.signature() != m2.signature()


# ── F10-6 MatteWorkflow ──────────────────────────────────────────────

class TestMatteWorkflow:
    def test_defaults(self):
        w = MatteWorkflow()
        assert w.matte_use == MATTE_USE_MASK
        assert w.merge_operation == "multiply"

    def test_signature(self):
        w = MatteWorkflow(matte_use=MATTE_USE_ID, merge_operation="over")
        sig = w.signature()
        assert MATTE_USE_ID in sig
        assert "over" in sig

    def test_presets_exist(self):
        assert "shadow_catcher" in MATTE_PRESETS
        assert "holdout" in MATTE_PRESETS
        assert "object_id" in MATTE_PRESETS
        assert "depth_comp" in MATTE_PRESETS

    def test_shadow_catcher_preset(self):
        p = MATTE_PRESETS["shadow_catcher"]
        assert p.matte_use == MATTE_USE_MASK
        assert p.merge_operation == "multiply"

    def test_holdout_preset(self):
        p = MATTE_PRESETS["holdout"]
        assert p.merge_operation == "out"

    def test_preset_has_description(self):
        for name, preset in MATTE_PRESETS.items():
            assert preset.description, f"{name} 缺少 description"
