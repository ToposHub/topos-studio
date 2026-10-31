"""
F11 格式能力矩阵 + 可插拔 loader 接口测试。

覆盖：
- FormatCapability：能力查询（supports/capability/summary）
- FORMAT_CAPABILITIES：矩阵完整性（每种格式注册）
- detect_format / supported_extensions / importable_extensions / can_import
- ModelLoaderBackend：ABC 接口 + LOADER_REGISTRY 注册
- SceneUnitConverter：单位/轴向/帧率转换

纯 Python 逻辑测试，无 GPU 依赖。
"""

import numpy as np
import pytest

from src.features.composite.render3d.format_capabilities import (
    ALL_CAPABILITY_FIELDS,
    AXIS_AUTO,
    AXIS_Y_UP,
    AXIS_Z_UP,
    CAP_FULL,
    CAP_NONE,
    CAP_PARTIAL,
    CAM_ANIMATION,
    CAP_ANIMATION,
    CAP_CAMERA,
    CAP_HIERARCHY,
    CAP_INSTANCING,
    CAP_LIGHT,
    CAP_MATERIAL,
    CAP_MESH,
    CAP_MORPH,
    CAP_PBR,
    CAP_SKELETON,
    CAP_SKINNING,
    CAP_TEXTURE,
    CAP_UV,
    CAP_UV2,
    AssetPathResolution,
    FORMAT_CAPABILITIES,
    FormatImportProbe,
    LOADER_REGISTRY,
    AXIS_Z_UP as _Z,
    FormatCapability,
    ModelLoaderBackend,
    SceneUnitConverter,
    can_import,
    detect_format,
    find_backend_for_path,
    importable_extensions,
    ensure_builtin_loader_backends,
    probe_import,
    register_loader_backend,
    resolve_asset_path,
    runtime_importable_extensions,
    supported_extensions,
    unit_to_meter,
)
from src.features.composite.render3d.model_import_report import (
    LOADER_BACKEND_TRIMESH,
    LOADER_BACKEND_UNKNOWN,
)


# ──────────────────────────────────────────────────────────────────────
# FormatCapability
# ──────────────────────────────────────────────────────────────────────


class TestFormatCapability:
    def _make(self, **kw):
        defaults = dict(
            format_id="test", extensions=frozenset({".test"}),
            backend="test_backend", mesh=CAP_FULL,
        )
        defaults.update(kw)
        return FormatCapability(**defaults)

    def test_defaults_all_none(self):
        fc = FormatCapability(
            format_id="x", extensions=frozenset({".x"}), backend="b")
        # 除 mesh 外，默认全 NONE（mesh 也 NONE 因未传）
        assert fc.mesh == CAP_NONE
        assert fc.animation == CAP_NONE
        assert fc.skeleton == CAP_NONE

    def test_capability_known_field(self):
        fc = self._make(mesh=CAP_FULL, animation=CAP_PARTIAL)
        assert fc.capability("mesh") == CAP_FULL
        assert fc.capability("animation") == CAP_PARTIAL

    def test_capability_unknown_field(self):
        fc = self._make()
        assert fc.capability("nonexistent") == CAP_NONE

    def test_supports_full(self):
        fc = self._make(mesh=CAP_FULL)
        assert fc.supports("mesh") is True
        assert fc.supports("mesh", CAP_FULL) is True

    def test_supports_partial_meets_min_partial(self):
        fc = self._make(mesh=CAP_PARTIAL)
        assert fc.supports("mesh") is True  # 默认 min=PARTIAL
        assert fc.supports("mesh", CAP_FULL) is False  # 达不到 FULL

    def test_supports_none(self):
        fc = self._make(mesh=CAP_NONE)
        assert fc.supports("mesh") is False

    def test_summary_all_fields(self):
        fc = self._make(mesh=CAP_FULL)
        s = fc.summary()
        assert isinstance(s, dict)
        for f in ALL_CAPABILITY_FIELDS:
            assert f in s

    def test_frozen(self):
        """FormatCapability 是 frozen dataclass（不可变）。"""
        fc = self._make()
        with pytest.raises((AttributeError, Exception)):
            fc.mesh = CAP_NONE  # type: ignore


# ──────────────────────────────────────────────────────────────────────
# FORMAT_CAPABILITIES 矩阵
# ──────────────────────────────────────────────────────────────────────


class TestFormatCapabilitiesMatrix:
    def test_all_formats_registered(self):
        expected = {"gltf", "glb", "fbx", "obj", "stl", "ply", "dae",
                    "3ds", "blend", "usd", "abc"}
        assert set(FORMAT_CAPABILITIES.keys()) == expected

    def test_format_ids_match_keys(self):
        for key, fc in FORMAT_CAPABILITIES.items():
            assert fc.format_id == key

    def test_no_extension_overlap(self):
        """不同格式的扩展名不应重叠。"""
        all_exts = []
        for fc in FORMAT_CAPABILITIES.values():
            all_exts.extend(fc.extensions)
        # 无重复
        assert len(all_exts) == len(set(all_exts))

    def test_gltf_full_capabilities(self):
        fc = FORMAT_CAPABILITIES["gltf"]
        assert fc.supports(CAP_MESH, CAP_FULL)
        assert fc.supports(CAP_PBR, CAP_FULL)
        assert fc.supports(CAP_SKELETON, CAP_FULL)
        assert fc.supports(CAP_ANIMATION, CAP_FULL)

    def test_obj_no_skeleton(self):
        fc = FORMAT_CAPABILITIES["obj"]
        assert fc.supports(CAP_MESH, CAP_FULL)
        assert not fc.supports(CAP_SKELETON)
        assert not fc.supports(CAP_ANIMATION)
        assert not fc.supports(CAP_PBR)

    def test_usd_is_planned(self):
        """USD 标记为 planned（backend=UNKNOWN）。"""
        fc = FORMAT_CAPABILITIES["usd"]
        assert fc.backend == LOADER_BACKEND_UNKNOWN
        assert not fc.supports(CAP_MESH)

    def test_alembic_is_planned(self):
        fc = FORMAT_CAPABILITIES["abc"]
        assert fc.backend == LOADER_BACKEND_UNKNOWN

    def test_fbx_partial_skeleton(self):
        """FBX 骨骼能力是 PARTIAL（pyassimp 不稳定）。"""
        fc = FORMAT_CAPABILITIES["fbx"]
        assert fc.skeleton == CAP_PARTIAL
        assert fc.backend != LOADER_BACKEND_UNKNOWN


# ──────────────────────────────────────────────────────────────────────
# detect_format / supported_extensions / can_import
# ──────────────────────────────────────────────────────────────────────


class TestDetectFormat:
    def test_gltf(self):
        fc = detect_format("model.gltf")
        assert fc is not None
        assert fc.format_id == "gltf"

    def test_glb(self):
        fc = detect_format("model.GLB")  # 大写
        assert fc is not None
        assert fc.format_id == "glb"

    def test_fbx(self):
        assert detect_format("a.fbx").format_id == "fbx"

    def test_usd_variants(self):
        for ext in (".usd", ".usda", ".usdc", ".usdz"):
            assert detect_format(f"a{ext}").format_id == "usd"

    def test_unknown_extension(self):
        assert detect_format("a.xyz") is None

    def test_no_extension(self):
        assert detect_format("noext") is None

    def test_empty_path(self):
        assert detect_format("") is None


class TestSupportedExtensions:
    def test_contains_common(self):
        exts = supported_extensions()
        for e in (".gltf", ".glb", ".fbx", ".obj", ".usd", ".abc"):
            assert e in exts

    def test_importable_excludes_planned(self):
        """importable 不含 USD/Alembic（planned）。"""
        imp = importable_extensions()
        assert ".gltf" in imp
        assert ".fbx" in imp
        assert ".usd" not in imp
        assert ".abc" not in imp

    def test_importable_subset_of_supported(self):
        assert importable_extensions() <= supported_extensions()


class TestCanImport:
    def test_probe_summary_is_json_safe(self, tmp_path):
        path = tmp_path / "asset.usd"
        path.write_bytes(b"placeholder")
        probe = probe_import(str(path))
        payload = probe.to_dict()
        assert payload["format_id"] == "usd"
        assert payload["reason_code"] == "format.planned"
        assert payload["capability"][CAP_MESH] == CAP_NONE
        assert payload["capability_notes"]

    def test_gltf_importable(self):
        assert can_import("a.gltf") is True

    def test_usd_not_importable(self):
        assert can_import("a.usd") is False

    def test_unknown_not_importable(self):
        assert can_import("a.xyz") is False

    def test_runtime_query_requires_a_real_file(self, tmp_path):
        assert can_import(str(tmp_path / "a.gltf"), require_exists=True) is False


class TestImportProbe:
    def test_unknown_format(self):
        result = probe_import("asset.xyz")
        assert isinstance(result, FormatImportProbe)
        assert result.reason_code == "format.unknown"
        assert result.can_import is False

    def test_planned_format(self, tmp_path):
        path = tmp_path / "asset.usd"
        path.write_bytes(b"not parsed")
        result = probe_import(str(path))
        assert result.format_id == "usd"
        assert result.is_planned is True
        assert result.reason_code == "format.planned"
        assert result.can_import is False

    def test_missing_file_is_reported_before_backend_failure(self, tmp_path):
        result = probe_import(str(tmp_path / "asset.gltf"))
        assert result.file_exists is False
        assert result.reason_code == "asset.missing"
        assert result.can_import is False

    def test_backend_unavailable(self, tmp_path, monkeypatch):
        from src.features.composite.render3d import model_loader

        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: False)
        result = probe_import(str(path))
        assert result.file_exists is True
        assert result.backend_available is False
        assert result.reason_code == "backend.unavailable"
        assert result.ready is False

    def test_ready_when_backend_is_available(self, tmp_path, monkeypatch):
        from src.features.composite.render3d import model_loader

        path = tmp_path / "asset.gltf"
        path.write_bytes(b"placeholder")
        monkeypatch.setattr(model_loader, "trimesh_available", lambda: True)
        result = probe_import(str(path))
        assert result.format_id == "gltf"
        assert result.backend == LOADER_BACKEND_TRIMESH
        assert result.reason_code == "ready"
        assert result.ready is True
        assert result.capability is FORMAT_CAPABILITIES["gltf"]

    def test_runtime_extensions_are_subset_of_static_matrix(self):
        assert runtime_importable_extensions() <= importable_extensions()


class TestBuiltinBackends:
    def test_builtin_backends_are_registered_lazily(self):
        ensure_builtin_loader_backends()
        assert LOADER_BACKEND_TRIMESH in LOADER_REGISTRY
        assert ".gltf" in LOADER_REGISTRY[LOADER_BACKEND_TRIMESH].extensions()


class TestAssetPathResolution:
    def test_project_relative_path(self, tmp_path):
        project = tmp_path / "project"
        media = project / "media"
        media.mkdir(parents=True)
        asset = media / "shot.glb"
        asset.write_bytes(b"asset")

        result = resolve_asset_path("media/shot.glb", project_dir=str(project))
        assert isinstance(result, AssetPathResolution)
        assert result.status == "found"
        assert result.is_resolved is True
        assert result.resolved_path == str(asset)

    def test_relink_by_basename_after_absolute_path_moved(self, tmp_path):
        moved_root = tmp_path / "new_assets"
        moved_root.mkdir()
        asset = moved_root / "shot.glb"
        asset.write_bytes(b"asset")

        result = resolve_asset_path(
            "/old_project/media/shot.glb",
            search_roots=(str(moved_root),),
        )
        assert result.status == "found"
        assert result.resolved_path == str(asset)

    def test_missing_and_empty_paths(self, tmp_path):
        empty = resolve_asset_path("")
        missing = resolve_asset_path("missing.glb", project_dir=str(tmp_path))
        assert empty.status == "missing"
        assert empty.candidates == ()
        assert missing.status == "missing"
        assert missing.is_resolved is False


# ──────────────────────────────────────────────────────────────────────
# ModelLoaderBackend ABC + 注册表
# ──────────────────────────────────────────────────────────────────────


class _DummyBackend(ModelLoaderBackend):
    """测试用 dummy backend。"""

    def __init__(self, exts, available=True):
        self._exts = set(exts)
        self._available = available

    @property
    def backend_id(self):
        return "dummy"

    def extensions(self):
        return self._exts

    def is_available(self):
        return self._available

    def load(self, path, normalize=False):
        return f"loaded:{path}"


class TestModelLoaderBackend:
    def test_default_backend_id(self):
        b = ModelLoaderBackend()
        assert b.backend_id == LOADER_BACKEND_UNKNOWN

    def test_can_handle_match(self):
        b = _DummyBackend({".test"}, available=True)
        assert b.can_handle("a.test") is True

    def test_can_handle_wrong_ext(self):
        b = _DummyBackend({".test"})
        assert b.can_handle("a.fbx") is False

    def test_can_handle_unavailable(self):
        b = _DummyBackend({".test"}, available=False)
        assert b.can_handle("a.test") is False

    def test_abstract_methods_raise(self):
        b = ModelLoaderBackend()
        with pytest.raises(NotImplementedError):
            b.extensions()
        with pytest.raises(NotImplementedError):
            b.is_available()
        with pytest.raises(NotImplementedError):
            b.load("a.txt")


class TestLoaderRegistry:
    def test_register_and_find(self):
        b = _DummyBackend({".regtest"})
        register_loader_backend(b)
        assert "dummy" in LOADER_REGISTRY
        found = find_backend_for_path("a.regtest")
        assert found is b

    def test_find_none_when_no_match(self):
        found = find_backend_for_path("a.nonexistent_ext")
        assert found is None

    def test_find_none_when_unavailable(self):
        b = _DummyBackend({".unavail"}, available=False)
        register_loader_backend(b)
        assert find_backend_for_path("a.unavail") is None

    def teardown_method(self, method):
        """清理注册表（避免测试间污染）。"""
        LOADER_REGISTRY.pop("dummy", None)


# ──────────────────────────────────────────────────────────────────────
# SceneUnitConverter
# ──────────────────────────────────────────────────────────────────────


class TestUnitToMeter:
    def test_meter(self):
        assert unit_to_meter("m") == 1.0
        assert unit_to_meter("meter") == 1.0

    def test_centimeter(self):
        assert abs(unit_to_meter("cm") - 0.01) < 1e-9

    def test_millimeter(self):
        assert abs(unit_to_meter("mm") - 0.001) < 1e-9

    def test_unknown_returns_one(self):
        assert unit_to_meter("lightyear") == 1.0


class TestSceneUnitConverter:
    def test_defaults(self):
        c = SceneUnitConverter()
        assert c.source_unit == "meter"
        assert c.source_axis == AXIS_AUTO
        assert c.unit_scale == 1.0
        assert c.needs_axis_flip is False

    def test_unit_scale_cm(self):
        c = SceneUnitConverter(source_unit="cm")
        assert abs(c.unit_scale - 0.01) < 1e-9

    def test_needs_axis_flip_z_up(self):
        c = SceneUnitConverter(source_axis=AXIS_Z_UP)
        assert c.needs_axis_flip is True

    def test_frame_to_seconds_with_fps(self):
        c = SceneUnitConverter(source_fps=30.0)
        assert abs(c.frame_to_seconds(30) - 1.0) < 1e-9

    def test_frame_to_seconds_zero_fps(self):
        """source_fps<=0 时 frame 视为秒。"""
        c = SceneUnitConverter(source_fps=0.0)
        assert c.frame_to_seconds(5.0) == 5.0

    def test_convert_frame(self):
        """源 30fps 的第 30 帧 → 目标 24fps 的第 24 帧（都是 1 秒）。"""
        c = SceneUnitConverter(source_fps=30.0, target_fps=24.0)
        assert abs(c.convert_frame(30) - 24.0) < 1e-6

    def test_convert_vertices_unit_only(self):
        """cm → m：顶点坐标除 100。"""
        c = SceneUnitConverter(source_unit="cm")
        v = np.array([[100.0, 200.0, 300.0]], dtype=np.float32)
        out = c.convert_vertices(v)
        np.testing.assert_array_almost_equal(out, [[1.0, 2.0, 3.0]])

    def test_convert_vertices_axis_flip(self):
        """Z up → Y up：绕 X 轴 -90° 旋转（x,y,z → x,z,-y），非镜像。

        AUD-88010：旧实现是纯 swap（x,y,z→x,z,y），行列式 -1 的镜像——
        前后向颠倒、三角形绕序反转。已按修复条目更正为旋转语义。"""
        c = SceneUnitConverter(source_axis=AXIS_Z_UP)
        v = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
        out = c.convert_vertices(v)
        # x,y,z → x,z,-y → [1, 3, -2]
        np.testing.assert_array_almost_equal(out[0], [1.0, 3.0, -2.0])

    def test_convert_vertices_no_op(self):
        """Y up + meter：无转换。"""
        c = SceneUnitConverter(source_unit="m", source_axis=AXIS_Y_UP)
        v = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
        out = c.convert_vertices(v)
        np.testing.assert_array_almost_equal(out, [[1.0, 2.0, 3.0]])

    def test_convert_vertices_returns_copy(self):
        """不修改输入数组。"""
        c = SceneUnitConverter(source_unit="cm")
        v = np.array([[100.0, 0, 0]], dtype=np.float32)
        original = v.copy()
        c.convert_vertices(v)
        np.testing.assert_array_equal(v, original)

    def test_signature_stability(self):
        c1 = SceneUnitConverter(source_unit="cm", source_fps=30)
        c2 = SceneUnitConverter(source_unit="cm", source_fps=30)
        assert c1.signature() == c2.signature()

    def test_signature_differs(self):
        c1 = SceneUnitConverter(source_unit="cm")
        c2 = SceneUnitConverter(source_unit="mm")
        assert c1.signature() != c2.signature()
