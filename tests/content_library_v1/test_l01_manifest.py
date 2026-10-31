"""L01 manifest 契约测试：字段校验/哈希稳定性/序列化往返。"""

from __future__ import annotations

import pytest

from content_staging.library_v1.specs.manifest import (
    ComponentManifest,
    ImageContract,
    ParamSpec,
    PortSpec,
    ResourceRef,
    TimeContract,
    VerificationRef,
    capability_key,
    default_capabilities,
)


def _pilot_like_manifest(**overrides) -> ComponentManifest:
    """构造一个最小合法 manifest（试点形态），字段可覆写。"""
    base = dict(
        component_id="F16",
        kind="filter",
        slug="gradient_map",
        version="1.0.0",
        status="component_ready",
        host_min_contract=".tglsl ABI 四类参数（试点 4 停靠）",
        dedup="Duotone_Toner 仅 2 停靠（L00 台账 F16 行）",
        params=(
            ParamSpec(name="stop_0", ptype="color", default=(0.0, 0.0, 0.0)),
            ParamSpec(name="pos_0", ptype="float", default=0.0, minimum=0.0, maximum=1.0),
            ParamSpec(name="mix", ptype="float", default=1.0, minimum=0.0, maximum=1.0,
                      neutral=False, unit="ratio"),
            ParamSpec(name="mode", ptype="option", default="linear",
                      options=("linear", "smooth")),
        ),
        image=ImageContract(),
        time=TimeContract(),
        ports=(PortSpec(name="image", direction="input", data_type="image", role="image"),),
        capabilities=dict(default_capabilities()),
        resources=(),
        verification=VerificationRef(oracle="content_staging/library_v1/references/gradient_map_ref.py"),
    )
    base.update(overrides)
    return ComponentManifest(**base)


class TestValidation:
    def test_valid_manifest_passes(self) -> None:
        assert _pilot_like_manifest().validate() == []

    def test_staging_rejects_integrated_status(self) -> None:
        m = _pilot_like_manifest(status="integrated")
        errs = m.validate(staging=True)
        assert any("暂存区" in e for e in errs)

    def test_unknown_status_rejected(self) -> None:
        assert any("status" in e for e in _pilot_like_manifest(status="shipped").validate())

    def test_bad_ptype_rejected(self) -> None:
        m = _pilot_like_manifest(params=(ParamSpec("x", "matrix", 1.0),))
        assert any("ptype" in e for e in m.validate())

    def test_option_requires_two_labels_and_valid_default(self) -> None:
        m = _pilot_like_manifest(params=(ParamSpec("mode", "option", "a", options=("a",)),))
        errs = m.validate()
        assert any("至少 2 个选项" in e for e in errs)
        m2 = _pilot_like_manifest(params=(ParamSpec("mode", "option", "zzz", options=("a", "b")),))
        assert any("选项之一" in e for e in m2.validate())

    def test_numeric_default_out_of_range_rejected(self) -> None:
        m = _pilot_like_manifest(params=(ParamSpec("amount", "float", 2.0, 0.0, 1.0),))
        assert any("default" in e for e in m.validate())

    def test_dynamic_time_requires_gate(self) -> None:
        m = _pilot_like_manifest(time=TimeContract(time_domain="clip-local"))
        assert any("dynamic_gate" in e for e in m.validate())

    def test_external_resource_requires_license_and_hash(self) -> None:
        m = _pilot_like_manifest(resources=(ResourceRef(kind="external", name="bluenoise", license=""),))
        errs = m.validate()
        assert any("外部资源" in e for e in errs)
        m2 = _pilot_like_manifest(resources=(
            ResourceRef(kind="external", name="bluenoise", license="CC0", sha256="ab" * 32),))
        assert not any("外部资源" in e for e in m2.validate())

    def test_neutral_param_requires_identity_verification(self) -> None:
        m = _pilot_like_manifest(
            params=(ParamSpec("mix", "float", 1.0, 0.0, 1.0, neutral=True),),
            verification=VerificationRef(),
        )
        assert any("恒等验证" in e for e in m.validate())

    def test_capability_key_axes_validated(self) -> None:
        caps = dict(default_capabilities())
        caps["color/preview/vulkan/static"] = "supported"
        m = _pilot_like_manifest(capabilities=caps)
        assert any("轴值不合法" in e for e in m.validate())
        bad_value = dict(default_capabilities())
        bad_value[capability_key("color", "preview", "cpu", "static")] = "maybe"
        m2 = _pilot_like_manifest(capabilities=bad_value)
        assert any("能力值" in e for e in m2.validate())

    def test_duplicate_param_names_rejected(self) -> None:
        m = _pilot_like_manifest(params=(
            ParamSpec("amount", "float", 0.5), ParamSpec("amount", "float", 0.6)))
        assert any("重复" in e for e in m.validate())


class TestHashAndSerialization:
    def test_schema_hash_stable_and_sensitive(self) -> None:
        a = _pilot_like_manifest()
        b = _pilot_like_manifest()
        assert a.parameter_schema_hash() == b.parameter_schema_hash()
        c = _pilot_like_manifest(params=a.params + (ParamSpec("extra", "float", 0.1),))
        assert a.parameter_schema_hash() != c.parameter_schema_hash()
        # 默认值变化也要改变 schema hash（对齐 parameter_schema_hash 语义）
        d = _pilot_like_manifest(params=(
            ParamSpec(name="mix", ptype="float", default=0.5, minimum=0.0, maximum=1.0),))
        e = _pilot_like_manifest(params=(
            ParamSpec(name="mix", ptype="float", default=0.7, minimum=0.0, maximum=1.0),))
        assert d.parameter_schema_hash() != e.parameter_schema_hash()

    def test_json_roundtrip(self) -> None:
        m = _pilot_like_manifest()
        restored = ComponentManifest.from_json(m.to_json())
        assert restored == m
        assert restored.validate() == []

    def test_from_json_rejects_non_list_fps(self) -> None:
        m = _pilot_like_manifest()
        text = m.to_json().replace('"fps": [', '"fps": "x", "_fps": [')
        with pytest.raises(ValueError):
            ComponentManifest.from_json(text)

    def test_default_capabilities_all_untested(self) -> None:
        caps = default_capabilities()
        assert len(caps) == 3 * 3 * 3 * 2
        assert set(caps.values()) == {"untested"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
