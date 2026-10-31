"""批 A Critical 修复回归测试（AUD-88050 / AUD-8901 / AUD-9200）。

1. AUD-88050 — 辐照度卷积 sin(theta) 立体角权重（渲染数学正确性）：
   - 源码级：_IRRADIANCE_CONVOLVE_FS 的累积项必须含 ``cos_theta * sin_theta``
     （dω = sinθ·dθ·dφ，Lambert 余弦 × 立体角面积元）。
   - 数值级：numpy 向量化镜像该 GLSL Riemann 求和（含切线空间方向构造），
     对解析可积环境贴图做收敛/对称性检查——
       * 均匀环境 L≡1 → 存储值（E/π，learnopengl 约定）= 1.0，任意法线一致；
       * 天空场 L(ω)=max(0,ω_y)：N=+Y → 2/3；N=−Y / N=+X → 0（反对称）；
       * 旧权重（缺 sinθ）对均匀环境给出 ≈2.0（能量翻倍），证明权重 load-bearing。
   消费端（core/shaders.py: diffuse_ibl = irradiance * albedo）按 E/π 约定取值，
   故 π/N 归一化保持不变，只修权重。
2. AUD-8901 — material_override_to_dict 保留 list/tuple：
   材质面板（material_panel.py:348/371）用普通 Python list 写 base_color，
   序列化不得静默丢弃；encode→decode 往返后 base_color 仍在且可直接应用。
3. AUD-9200 — ModelPayload.signature 纳入 material_params：
   材质控制参数（model3d_node.py 填充的 6 个键）任一变化必须改变签名，
   否则 Scene3D 缓存命中时回传陈旧纹理。
"""
import json
import math
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pytest

from src.features.composite.render3d.hdri import _IRRADIANCE_CONVOLVE_FS
from src.features.composite.render3d.material_model import (
    MaterialEditModel,
    MaterialOverride,
    apply_override,
    apply_override_to_material_data,
    decode_material_overrides,
    encode_material_overrides,
    material_override_from_dict,
    material_override_to_dict,
)
from src.features.composite.render3d.scene_payload import ModelPayload


# ── AUD-88050：辐照度卷积 sin(theta) 立体角权重 ──────────────────────────

# 与 _IRRADIANCE_CONVOLVE_FS 中 phi/theta 循环步长一致
_IRRADIANCE_STEP = 0.025


def _irradiance_shader_mirror(radiance, normal, solid_angle_weight=True):
    """numpy 镜像 _IRRADIANCE_CONVOLVE_FS 的 Riemann 求和（含方向构造）。

    与 GLSL 逐行对应：为法线 ``normal`` 建切线空间 (T,B,N)，在 (φ,θ) 网格上
    构造采样方向 ``dir = t.x*T + t.y*B + t.z*N``，累积
    ``Σ L(dir)·cosθ·(sinθ)``，最后乘 π/N。

    radiance: callable(dirs) -> (n,3)，dirs 为 (n,3) 采样方向数组
    （等价于对 equirect 环境贴图的纹理采样，此处用解析场代替）。
    返回 (3,) irradiance——shader 写入 cubemap 的值（= E/π，learnopengl 约定，
    主 shader 用 ``irradiance * albedo`` 重建 Lambert 漫反射）。

    solid_angle_weight=False 复现 AUD-88050 修复前的旧权重（仅 cosθ）。
    """
    n = np.asarray(normal, dtype=np.float64)
    n = n / np.linalg.norm(n)
    up = np.array([0.0, 1.0, 0.0]) if abs(n[1]) < 0.999 else np.array([0.0, 0.0, 1.0])
    t = np.cross(up, n)
    t = t / np.linalg.norm(t)
    b = np.cross(n, t)

    phis = np.arange(0.0, 2.0 * math.pi, _IRRADIANCE_STEP)
    thetas = np.arange(0.0, 0.5 * math.pi, _IRRADIANCE_STEP)
    phi_grid, theta_grid = np.meshgrid(phis, thetas, indexing="ij")
    cos_theta = np.cos(theta_grid).ravel()
    sin_theta = np.sin(theta_grid).ravel()
    # 切线空间方向 → 世界（对应 shader 的 sample_dir）
    dirs = (np.outer(sin_theta * np.cos(phi_grid.ravel()), t)
            + np.outer(sin_theta * np.sin(phi_grid.ravel()), b)
            + np.outer(cos_theta, n))
    weight = cos_theta * (sin_theta if solid_angle_weight else 1.0)
    total = radiance(dirs).T @ weight
    count = float(dirs.shape[0])
    return math.pi * total / max(count, 1.0)


class TestIrradianceSolidAngleWeight:
    def test_shader_accumulation_includes_solid_angle_weight(self) -> None:
        """累积项必须同时含 cos_theta（Lambert）与 sin_theta（dω 面积元）。"""
        match = re.search(r"irradiance \+= ([^;]+);", _IRRADIANCE_CONVOLVE_FS)
        assert match is not None, "irradiance 累积语句缺失"
        expr = match.group(1)
        assert "cos_theta" in expr, f"累积权重缺 cos_theta: {expr!r}"
        assert "sin_theta" in expr, f"累积权重缺 sin_theta（立体角）: {expr!r}"

    def test_uniform_environment_converges_to_one_for_any_normal(self) -> None:
        """均匀环境 L≡1：E = ∫ cosθ dω = π → 存储值 E/π = 1（方向对称）。"""
        radiance = lambda dirs: np.ones_like(dirs)  # noqa: E731
        for normal in ([0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, -1.0, 0.0],
                       [0.577, 0.577, 0.577]):
            value = _irradiance_shader_mirror(radiance, normal)
            assert value == pytest.approx(1.0, rel=0.02), (
                f"均匀环境在 normal={normal} 应收敛到 1.0（E/π），得到 {value}")

    def test_cosine_sky_matches_analytic_per_normal(self) -> None:
        """L(ω)=max(0,ω_y)（仅上半球天空）对三个法线方向的解析值：

        - N=+Y：E = ∫_{ω_y>0} ω_y·ω_y dω = 2π/3 → 存储值 E/π = 2/3；
        - N=−Y：半球内 ω_y≤0 → L≡0 → 0；
        - N=+X：E = ∫_{ω_x>0,ω_y>0} ω_y·ω_x dω = 2·(1/3) = 2/3 → 2/(3π)。
        """
        sky = lambda dirs: np.clip(dirs[:, 1:2], 0.0, None)  # noqa: E731
        up = _irradiance_shader_mirror(sky, [0.0, 1.0, 0.0])
        assert up == pytest.approx(2.0 / 3.0, rel=0.02), (
            f"N=+Y 应为 2/3（E=2π/3），得到 {up}")
        down = _irradiance_shader_mirror(sky, [0.0, -1.0, 0.0])
        assert down == pytest.approx(0.0, abs=0.01), f"N=−Y 应为 0，得到 {down}"
        side = _irradiance_shader_mirror(sky, [1.0, 0.0, 0.0])
        assert side == pytest.approx(2.0 / (3.0 * math.pi), rel=0.02), (
            f"N=+X 应为 2/(3π)≈0.2111，得到 {side}")

    def test_missing_sin_theta_diverges_from_analytic(self) -> None:
        """旧权重（缺 sinθ）对均匀环境给出 ≈2.0（能量翻倍），证明权重必要。"""
        radiance = lambda dirs: np.ones_like(dirs)  # noqa: E731
        buggy = _irradiance_shader_mirror(radiance, [0.0, 1.0, 0.0],
                                          solid_angle_weight=False)
        assert buggy == pytest.approx(2.0, rel=0.02), (
            f"旧权重应给出 ≈2.0（极点超权），得到 {buggy}")
        assert buggy != pytest.approx(1.0, rel=0.1)


# ── AUD-8901：material_override_to_dict 保留 list/tuple ───────────────────

class TestMaterialOverrideListPersistence:
    def test_list_base_color_survives_to_dict(self) -> None:
        """材质面板（material_panel.py:348）以普通 Python list 写 base_color。"""
        override = MaterialOverride(target_material_id="body")
        override.set_scalar("base_color", [1.0, 0.5, 0.25])
        data = material_override_to_dict(override)
        assert data["overrides"].get("base_color") == [1.0, 0.5, 0.25], (
            "base_color list 覆盖不得在序列化时被静默丢弃")

    def test_tuple_with_numpy_elements_normalized_to_json_safe(self) -> None:
        override = MaterialOverride(target_material_id="body")
        override.set_scalar(
            "base_color", tuple(np.float32(x) for x in (0.1, 0.2, 0.3)))
        data = material_override_to_dict(override)
        value = data["overrides"]["base_color"]
        # float32→float64 展宽允许近似（np.float32(0.1).item() ≈ 0.1+1.5e-9）
        assert np.allclose(value, [0.1, 0.2, 0.3])
        json.dumps(data)  # numpy 标量必须已规约，否则 json.dumps 抛 TypeError

    def test_encode_decode_roundtrip_restores_base_color(self) -> None:
        """保存/重载往返：base_color 仍在，且能应用到材质（反序列化侧对齐）。"""
        from src.features.composite.render3d import scene_graph as sg

        override = MaterialOverride(
            target_material_id="body", target_material_name="Body")
        override.set_scalar("base_color", [1.0, 0.0, 0.25])
        override.set_scalar("metallic", 0.9)
        encoded = encode_material_overrides({"body": override})

        decoded, diagnostics = decode_material_overrides(encoded)
        assert diagnostics == ()
        assert decoded["body"].overrides["base_color"] == [1.0, 0.0, 0.25]

        # 编辑模型侧应用
        result = apply_override(MaterialEditModel(), decoded["body"])
        assert np.allclose(result.base_color, [1.0, 0.0, 0.25])
        # 渲染桥侧应用（MaterialData 副本）
        effective = apply_override_to_material_data(
            sg.MaterialData(), decoded["body"])
        assert np.allclose(effective.base_color, [1.0, 0.0, 0.25])

    def test_from_dict_still_accepts_list_directly(self) -> None:
        """反序列化侧本就透传 list（读写对称由序列化侧补齐）。"""
        restored = material_override_from_dict({
            "target_material_id": "body",
            "overrides": {"base_color": [0.2, 0.4, 0.6]},
        })
        assert restored.overrides["base_color"] == [0.2, 0.4, 0.6]

    def test_array_and_scalar_overrides_still_serialize(self) -> None:
        """回归：ndarray / numpy 标量 / str 原有分支不受影响。"""
        override = MaterialOverride(target_material_id="body")
        override.set_scalar("emissive", np.array([0.2, 0.4, 0.6], dtype=np.float32))
        override.set_scalar("roughness", np.float64(0.3))
        override.set_scalar("alpha_mode", "blend")
        data = material_override_to_dict(override)
        assert np.allclose(data["overrides"]["emissive"], [0.2, 0.4, 0.6])
        assert data["overrides"]["roughness"] == 0.3
        assert data["overrides"]["alpha_mode"] == "blend"


# ── AUD-9200：ModelPayload.signature 纳入 material_params ─────────────────

# model3d_node.py 填充 material_params 的全部键（material_mode / normal_format /
# orm_layout / texture_filter / texture_wrap / emissive_intensity）
_MATERIAL_PARAM_CHANGES = [
    ("material_mode", "override"),
    ("normal_format", "DirectX"),
    ("orm_layout", "unreal"),
    ("texture_filter", "nearest"),
    ("texture_wrap", "repeat"),
    ("emissive_intensity", 3.0),
]


def _model_payload(**params) -> ModelPayload:
    base = {
        "material_mode": "imported",
        "normal_format": "OpenGL",
        "orm_layout": "glTF ORM",
        "texture_filter": "linear",
        "texture_wrap": "clamp",
        "emissive_intensity": 1.0,
    }
    base.update(params)
    return ModelPayload(has_model=True, model_path="/m.glb",
                        material_params=base)


class TestModelPayloadSignatureMaterialParams:
    @pytest.mark.parametrize("key,value", _MATERIAL_PARAM_CHANGES)
    def test_each_control_param_changes_signature(self, key, value) -> None:
        assert _model_payload().signature() != _model_payload(**{key: value}).signature(), (
            f"material_params[{key!r}] 变化未反映到签名（陈旧渲染风险）")

    def test_identical_params_same_signature(self) -> None:
        assert _model_payload().signature() == _model_payload().signature()

    def test_none_and_empty_params_are_equivalent(self) -> None:
        assert (ModelPayload().signature()
                == ModelPayload(material_params={}).signature())

    def test_params_present_differs_from_absent(self) -> None:
        assert ModelPayload().signature() != _model_payload().signature()

    def test_signature_remains_hashable(self) -> None:
        sig = _model_payload().signature()
        assert isinstance(sig, tuple)
        assert isinstance(hash(sig), int)
