"""FX-M07 acceptance tests: plugin stable identity, versioning & failure recovery.

Covers:
- PluginIdentity serialization round-trip
- Content hash and parameter schema hash determinism
- build_plugin_identity from EffectSpec
- resolve_plugin_status for all status states
- validate_effect_instances batch validation
- migrate_legacy_identity backfill
- export_preflight_check blocking
- EffectInstance plugin_identity field serialization
- Project round-trip preserves plugin identity
- EffectPassRunner safe recompile (fallback program)
"""

from __future__ import annotations

import pytest
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from src.features.shared.effects.plugin_identity import (
    PluginIdentity,
    PluginStatus,
    PluginStatusReport,
    build_plugin_identity,
    compute_content_hash,
    compute_parameter_schema_hash,
    export_preflight_check,
    migrate_legacy_identity,
    resolve_plugin_status,
    validate_effect_instances,
)
from src.features.edit.core.effect_instance import EffectInstance

pytestmark = pytest.mark.edit_core


# =============================================================================
# Helpers
# =============================================================================

@dataclass(frozen=True)
class _FakeParamDef:
    """Minimal ParamDef-like object for schema hash tests."""
    name: str = "radius"
    ptype: str = "float"
    default: Any = 5.0
    labels: Optional[Tuple[str, ...]] = None
    min_val: Optional[float] = 0.0
    max_val: Optional[float] = 100.0


@dataclass(frozen=True)
class _FakeSpec:
    """Minimal EffectSpec-like object for identity tests."""
    effect_type: str = "plugin:test_plugin"
    shader_sources: Tuple[str, ...] = ("void main() { gl_FragColor = vec4(1.0); }",)
    param_defs: Tuple[Any, ...] = (_FakeParamDef(),)
    _plugin_version: str = ""


def _make_spec(
    effect_type: str = "plugin:test_plugin",
    source: str = "void main() { gl_FragColor = vec4(1.0); }",
    version: str = "",
    params: Tuple[Any, ...] = (_FakeParamDef(),),
) -> _FakeSpec:
    spec = _FakeSpec(
        effect_type=effect_type,
        shader_sources=(source,),
        param_defs=params,
        _plugin_version=version,
    )
    return spec


def _make_instance(
    effect_type: str = "plugin:test_plugin",
    identity: Optional[dict] = None,
    effect_id: str = "e1",
) -> EffectInstance:
    return EffectInstance(
        effect_id=effect_id,
        effect_type=effect_type,
        params={"radius": 5.0},
        plugin_identity=identity,
    )


# =============================================================================
# TestPluginIdentity
# =============================================================================

class TestPluginIdentity:
    def test_round_trip_serialization(self) -> None:
        identity = PluginIdentity(
            plugin_id="plugin:film_halation",
            version="1.2.0",
            content_hash="abc123",
            source_uri="/plugins/Film_Halation.tglsl",
            parameter_schema_hash="def456",
        )
        d = identity.to_dict()
        restored = PluginIdentity.from_dict(d)
        assert restored == identity

    def test_from_dict_missing_keys_defaults(self) -> None:
        identity = PluginIdentity.from_dict({"plugin_id": "plugin:x"})
        assert identity.plugin_id == "plugin:x"
        assert identity.version == ""
        assert identity.content_hash == ""
        assert identity.source_uri == ""
        assert identity.parameter_schema_hash == ""

    def test_frozen(self) -> None:
        identity = PluginIdentity(plugin_id="plugin:x")
        with pytest.raises(AttributeError):
            identity.plugin_id = "other"  # type: ignore

    def test_capabilities_round_trip(self) -> None:
        identity = PluginIdentity(
            plugin_id="plugin:x",
            capabilities=("gpu_preview", "export"),
        )
        assert PluginIdentity.from_dict(identity.to_dict()).capabilities == (
            "gpu_preview",
            "export",
        )


# =============================================================================
# TestHashUtilities
# =============================================================================

class TestHashUtilities:
    def test_content_hash_deterministic(self) -> None:
        sources = ("shader A", "shader B")
        h1 = compute_content_hash(sources)
        h2 = compute_content_hash(sources)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_content_hash_differs_on_change(self) -> None:
        h1 = compute_content_hash(("source v1",))
        h2 = compute_content_hash(("source v2",))
        assert h1 != h2

    def test_content_hash_boundary_safe(self) -> None:
        # "ab" + "c" vs "a" + "bc" must differ (NUL separator)
        h1 = compute_content_hash(("ab", "c"))
        h2 = compute_content_hash(("a", "bc"))
        assert h1 != h2

    def test_content_hash_empty(self) -> None:
        h = compute_content_hash(())
        assert len(h) == 64  # hash of empty input

    def test_schema_hash_deterministic(self) -> None:
        params = (_FakeParamDef(name="a"), _FakeParamDef(name="b"))
        h1 = compute_parameter_schema_hash(params)
        h2 = compute_parameter_schema_hash(params)
        assert h1 == h2

    def test_schema_hash_differs_on_param_change(self) -> None:
        p1 = (_FakeParamDef(name="radius", max_val=100.0),)
        p2 = (_FakeParamDef(name="radius", max_val=200.0),)
        assert compute_parameter_schema_hash(p1) != compute_parameter_schema_hash(p2)

    def test_schema_hash_differs_on_added_param(self) -> None:
        p1 = (_FakeParamDef(name="a"),)
        p2 = (_FakeParamDef(name="a"), _FakeParamDef(name="b"))
        assert compute_parameter_schema_hash(p1) != compute_parameter_schema_hash(p2)


# =============================================================================
# TestBuildPluginIdentity
# =============================================================================

class TestBuildPluginIdentity:
    def test_builds_from_spec(self) -> None:
        spec = _make_spec(version="2.0.0")
        identity = build_plugin_identity(
            spec, source_uri="/p/test.tglsl", version="2.0.0",
        )
        assert identity.plugin_id == "plugin:test_plugin"
        assert identity.version == "2.0.0"
        assert identity.source_uri == "/p/test.tglsl"
        assert len(identity.content_hash) == 64
        assert len(identity.parameter_schema_hash) == 64

    def test_content_hash_matches_utility(self) -> None:
        spec = _make_spec(source="hello shader")
        identity = build_plugin_identity(spec)
        assert identity.content_hash == compute_content_hash(("hello shader",))


# =============================================================================
# TestResolvePluginStatus
# =============================================================================

class TestResolvePluginStatus:
    def test_missing_plugin(self) -> None:
        report = resolve_plugin_status(
            {"plugin_id": "plugin:x"}, "plugin:x", catalog_spec=None,
        )
        assert report.status == PluginStatus.MISSING
        assert report.export_blocked is True
        assert report.recoverable is True

    def test_unsupported_backend(self) -> None:
        spec = _make_spec()
        report = resolve_plugin_status(
            {"plugin_id": "plugin:x"}, "plugin:x",
            catalog_spec=spec, backend_supported=False,
        )
        assert report.status == PluginStatus.UNSUPPORTED_BACKEND
        assert report.export_blocked is True
        assert report.recoverable is False

    def test_compile_error(self) -> None:
        spec = _make_spec()
        report = resolve_plugin_status(
            {"plugin_id": "plugin:x"}, "plugin:x",
            catalog_spec=spec, compile_ok=False,
        )
        assert report.status == PluginStatus.COMPILE_ERROR
        assert report.export_blocked is True

    def test_legacy_no_identity(self) -> None:
        spec = _make_spec()
        report = resolve_plugin_status(
            None, "plugin:x", catalog_spec=spec,
        )
        assert report.status == PluginStatus.LEGACY
        assert report.export_blocked is False
        assert report.recoverable is True

    def test_version_mismatch(self) -> None:
        spec = _make_spec(version="2.0.0")
        stored = PluginIdentity(
            plugin_id="plugin:test_plugin", version="1.0.0",
        ).to_dict()
        report = resolve_plugin_status(
            stored, "plugin:test_plugin", catalog_spec=spec,
        )
        assert report.status == PluginStatus.VERSION_MISMATCH
        assert "1.0.0" in report.detail
        assert "2.0.0" in report.detail

    def test_schema_mismatch(self) -> None:
        spec = _make_spec(params=(_FakeParamDef(name="new_param"),))
        # Build identity from a DIFFERENT schema
        old_spec = _make_spec(params=(_FakeParamDef(name="old_param"),))
        identity = build_plugin_identity(old_spec)
        report = resolve_plugin_status(
            identity.to_dict(), "plugin:test_plugin", catalog_spec=spec,
        )
        assert report.status == PluginStatus.SCHEMA_MISMATCH
        assert report.export_blocked is True

    def test_content_changed(self) -> None:
        spec = _make_spec(source="new shader source")
        # Identity from old source
        old_spec = _make_spec(source="old shader source")
        identity = build_plugin_identity(old_spec)
        report = resolve_plugin_status(
            identity.to_dict(), "plugin:test_plugin", catalog_spec=spec,
        )
        assert report.status == PluginStatus.CONTENT_CHANGED
        assert report.export_blocked is False
        assert report.ok is True  # content change is non-blocking

    def test_active_all_match(self) -> None:
        spec = _make_spec(version="1.0.0")
        identity = build_plugin_identity(spec, version="1.0.0")
        report = resolve_plugin_status(
            identity.to_dict(), "plugin:test_plugin", catalog_spec=spec,
        )
        assert report.status == PluginStatus.ACTIVE
        assert report.ok is True
        assert report.export_blocked is False

    def test_priority_missing_over_compile(self) -> None:
        # Missing takes priority even if compile_ok=False
        report = resolve_plugin_status(
            {"plugin_id": "x"}, "plugin:x",
            catalog_spec=None, compile_ok=False,
        )
        assert report.status == PluginStatus.MISSING

    def test_stored_plugin_id_must_match_effect_type(self) -> None:
        spec = _make_spec(effect_type="plugin:expected")
        report = resolve_plugin_status(
            {"plugin_id": "plugin:different"},
            "plugin:expected",
            catalog_spec=spec,
        )
        assert report.status == PluginStatus.MISSING
        assert report.export_blocked is True


# =============================================================================
# TestValidateEffectInstances
# =============================================================================

class TestValidateEffectInstances:
    def test_skips_builtin_effects(self) -> None:
        instances = [_make_instance(effect_type="blur")]
        reports = validate_effect_instances(
            instances, catalog_lookup=lambda _: None,
        )
        assert reports == []

    def test_validates_plugin_effects(self) -> None:
        spec = _make_spec()
        identity = build_plugin_identity(spec)
        inst = _make_instance(identity=identity.to_dict())
        reports = validate_effect_instances(
            [inst], catalog_lookup=lambda et: spec,
        )
        assert len(reports) == 1
        assert reports[0].status == PluginStatus.ACTIVE
        assert reports[0].effect_id == "e1"

    def test_compile_failure_set(self) -> None:
        spec = _make_spec()
        identity = build_plugin_identity(spec)
        inst = _make_instance(identity=identity.to_dict())
        reports = validate_effect_instances(
            [inst],
            catalog_lookup=lambda et: spec,
            compile_failures={"plugin:test_plugin"},
        )
        assert reports[0].status == PluginStatus.COMPILE_ERROR

    def test_mixed_builtin_and_plugin(self) -> None:
        spec = _make_spec()
        identity = build_plugin_identity(spec)
        instances = [
            _make_instance(effect_type="blur", effect_id="b1"),
            _make_instance(effect_type="plugin:test_plugin", identity=identity.to_dict(), effect_id="p1"),
            _make_instance(effect_type="sharpen", effect_id="s1"),
        ]
        reports = validate_effect_instances(
            instances, catalog_lookup=lambda et: spec if "plugin" in et else None,
        )
        assert len(reports) == 1
        assert reports[0].effect_id == "p1"


# =============================================================================
# TestMigrateLegacyIdentity
# =============================================================================

class TestMigrateLegacyIdentity:
    def test_backfill_from_catalog(self) -> None:
        spec = _make_spec(version="1.0.0")
        result = migrate_legacy_identity(
            "plugin:test_plugin", spec, version="1.0.0",
        )
        assert result is not None
        assert result["plugin_id"] == "plugin:test_plugin"
        assert result["version"] == "1.0.0"
        assert len(result["content_hash"]) == 64

    def test_returns_none_when_not_in_catalog(self) -> None:
        result = migrate_legacy_identity("plugin:gone", None)
        assert result is None

    def test_does_not_mutate_instance(self) -> None:
        inst = _make_instance(identity=None)
        spec = _make_spec()
        migrate_legacy_identity("plugin:test_plugin", spec)
        # Original instance unchanged
        assert inst.plugin_identity is None


# =============================================================================
# TestExportPreflight
# =============================================================================

class TestExportPreflight:
    def test_blocks_missing_plugin(self) -> None:
        inst = _make_instance(identity={"plugin_id": "plugin:x"})
        blocked = export_preflight_check(
            [inst], catalog_lookup=lambda _: None,
        )
        assert len(blocked) == 1
        assert blocked[0].status == PluginStatus.MISSING

    def test_passes_active_plugin(self) -> None:
        spec = _make_spec()
        identity = build_plugin_identity(spec)
        inst = _make_instance(identity=identity.to_dict())
        blocked = export_preflight_check(
            [inst], catalog_lookup=lambda _: spec,
        )
        assert blocked == []

    def test_blocks_schema_mismatch(self) -> None:
        spec = _make_spec(params=(_FakeParamDef(name="new"),))
        old_spec = _make_spec(params=(_FakeParamDef(name="old"),))
        identity = build_plugin_identity(old_spec)
        inst = _make_instance(identity=identity.to_dict())
        blocked = export_preflight_check(
            [inst], catalog_lookup=lambda _: spec,
        )
        assert len(blocked) == 1
        assert blocked[0].status == PluginStatus.SCHEMA_MISMATCH

    def test_content_changed_does_not_block(self) -> None:
        spec = _make_spec(source="new source")
        old_spec = _make_spec(source="old source")
        identity = build_plugin_identity(old_spec)
        inst = _make_instance(identity=identity.to_dict())
        blocked = export_preflight_check(
            [inst], catalog_lookup=lambda _: spec,
        )
        assert blocked == []


# =============================================================================
# TestEffectInstancePluginIdentity
# =============================================================================

class TestEffectInstancePluginIdentity:
    def test_default_none(self) -> None:
        inst = EffectInstance(effect_type="blur", params={})
        assert inst.plugin_identity is None

    def test_to_dict_omits_when_none(self) -> None:
        inst = EffectInstance(effect_type="blur", params={})
        d = inst.to_dict()
        assert "plugin_identity" not in d

    def test_to_dict_includes_when_present(self) -> None:
        identity = {"plugin_id": "plugin:x", "version": "1.0"}
        inst = EffectInstance(
            effect_type="plugin:x", params={}, plugin_identity=identity,
        )
        d = inst.to_dict()
        assert d["plugin_identity"] == identity

    def test_from_dict_round_trip(self) -> None:
        identity = {"plugin_id": "plugin:x", "version": "2.0", "content_hash": "abc"}
        inst = EffectInstance(
            effect_type="plugin:x", params={"r": 1.0}, plugin_identity=identity,
        )
        restored = EffectInstance.from_dict(inst.to_dict())
        assert restored.plugin_identity == identity
        assert restored.effect_type == "plugin:x"
        assert restored.params == {"r": 1.0}

    def test_from_dict_legacy_no_identity(self) -> None:
        # Simulates loading an old project file without plugin_identity key
        data = {"effect_id": "e1", "effect_type": "plugin:old", "params": {}}
        inst = EffectInstance.from_dict(data)
        assert inst.plugin_identity is None

    def test_with_plugin_identity(self) -> None:
        inst = EffectInstance(effect_type="plugin:x", params={})
        new_identity = {"plugin_id": "plugin:x", "version": "3.0"}
        updated = inst.with_plugin_identity(new_identity)
        assert updated.plugin_identity == new_identity
        assert inst.plugin_identity is None  # original unchanged

    def test_with_param_preserves_identity(self) -> None:
        identity = {"plugin_id": "plugin:x"}
        inst = EffectInstance(
            effect_type="plugin:x", params={"r": 1.0}, plugin_identity=identity,
        )
        updated = inst.with_param("r", 2.0)
        assert updated.plugin_identity == identity
        assert updated.params["r"] == 2.0

    def test_with_enabled_preserves_identity(self) -> None:
        identity = {"plugin_id": "plugin:x"}
        inst = EffectInstance(
            effect_type="plugin:x", params={}, plugin_identity=identity,
        )
        updated = inst.with_enabled(False)
        assert updated.plugin_identity == identity
        assert updated.is_enabled is False

    def test_catalog_plugin_metadata_reaches_add_effect_command(self) -> None:
        """Production AddEffect creates identity instead of only preserving it."""
        from src.features.edit.core.commands import AddEffectCommand
        from src.features.edit.core.timeline_v2 import (
            ClipInstance,
            EditTrack,
            TimelineSequence,
        )
        from src.features.shared.effects import get_effect_catalog
        from src.shared.media.metadata import Rational

        plugins = [
            spec for spec in get_effect_catalog().get_all_specs()
            if spec.effect_type.startswith("plugin:")
        ]
        if not plugins:
            pytest.skip("no DCTL plugin fixtures discovered")
        spec = plugins[0]
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v1",
            timeline_start=0, timeline_duration=10,
            source_start=0, source_duration=10,
        )
        track = EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            clips=(clip,),
        )
        sequence = TimelineSequence(
            timeline_id="seq", name="S", fps=Rational(24, 1),
            width=64, height=64, video_tracks=(track,),
        )

        command = AddEffectCommand.create(
            "v1", "c1", spec.effect_type, effect_id="plugin-effect-1",
        )
        result = command.apply(sequence)
        attached = result.get_clip_by_id("c1").effects[0]

        assert attached.plugin_identity is not None
        assert attached.plugin_identity["plugin_id"] == spec.effect_type
        assert attached.plugin_identity["content_hash"] == spec.plugin_content_hash
        # AUD-R2-008：元数据链本身完好（catalog → build_plugin_identity →
        # AddEffectCommand → EffectInstance → 序列化往返全部通过）。失败的是
        # 硬编码期望：2026-09-29 新增的 Fake_Infrared.tglsl 声明
        # ``@export_support all``，成为扫描序第一个插件，capabilities 依
        # catalog 规则合法变为 ("gpu_preview", "export_opengl")。断言改为
        # 「identity 逐字镜像所选 spec 的 capabilities」——这才是该测试
        # 要锁定的契约（目录元数据如实送达），且对插件增删稳健。
        assert attached.plugin_identity["capabilities"] == \
            list(spec.plugin_capabilities)
        assert "gpu_preview" in attached.plugin_identity["capabilities"]
        restored_command = AddEffectCommand.from_dict(command.to_dict())
        assert restored_command._plugin_identity == command._plugin_identity

    def test_with_order_preserves_identity(self) -> None:
        identity = {"plugin_id": "plugin:x"}
        inst = EffectInstance(
            effect_type="plugin:x", params={}, plugin_identity=identity, order=0,
        )
        updated = inst.with_order(3)
        assert updated.plugin_identity == identity
        assert updated.order == 3

    def test_with_effect_id_preserves_identity(self) -> None:
        identity = {"plugin_id": "plugin:x"}
        inst = EffectInstance(
            effect_type="plugin:x", params={}, plugin_identity=identity,
        )
        updated = inst.with_effect_id("new-id")
        assert updated.plugin_identity == identity
        assert updated.effect_id == "new-id"


# =============================================================================
# TestPluginStatusReport
# =============================================================================

class TestPluginStatusReport:
    def test_ok_for_active(self) -> None:
        r = PluginStatusReport(
            effect_id="e1", effect_type="plugin:x",
            status=PluginStatus.ACTIVE,
        )
        assert r.ok is True

    def test_ok_for_content_changed(self) -> None:
        r = PluginStatusReport(
            effect_id="e1", effect_type="plugin:x",
            status=PluginStatus.CONTENT_CHANGED,
        )
        assert r.ok is True

    def test_not_ok_for_missing(self) -> None:
        r = PluginStatusReport(
            effect_id="e1", effect_type="plugin:x",
            status=PluginStatus.MISSING,
        )
        assert r.ok is False

    def test_not_ok_for_schema_mismatch(self) -> None:
        r = PluginStatusReport(
            effect_id="e1", effect_type="plugin:x",
            status=PluginStatus.SCHEMA_MISMATCH,
        )
        assert r.ok is False


# =============================================================================
# TestProjectRoundTrip
# =============================================================================

class TestProjectRoundTrip:
    """Verify that plugin identity survives full serialization cycle."""

    def test_full_round_trip(self) -> None:
        spec = _make_spec(version="1.5.0")
        identity = build_plugin_identity(spec, source_uri="/p/x.tglsl", version="1.5.0")
        inst = EffectInstance(
            effect_id="fx-001",
            effect_type="plugin:test_plugin",
            params={"radius": 3.0, "mix": 0.8},
            plugin_identity=identity.to_dict(),
            order=2,
        )
        # Serialize → deserialize
        data = inst.to_dict()
        restored = EffectInstance.from_dict(data)

        # Identity preserved
        assert restored.plugin_identity is not None
        restored_identity = PluginIdentity.from_dict(restored.plugin_identity)
        assert restored_identity.plugin_id == "plugin:test_plugin"
        assert restored_identity.version == "1.5.0"
        assert restored_identity.content_hash == identity.content_hash
        assert restored_identity.parameter_schema_hash == identity.parameter_schema_hash

        # Params and keyframes untouched
        assert restored.params == {"radius": 3.0, "mix": 0.8}
        assert restored.order == 2
        assert restored.effect_id == "fx-001"

    def test_recovery_after_round_trip(self) -> None:
        """After restoring, plugin can be re-activated without param loss."""
        spec = _make_spec(version="1.0.0")
        identity = build_plugin_identity(spec, version="1.0.0")
        inst = EffectInstance(
            effect_type="plugin:test_plugin",
            params={"radius": 7.5},
            plugin_identity=identity.to_dict(),
        )
        # Simulate: plugin goes missing, then comes back
        data = inst.to_dict()
        restored = EffectInstance.from_dict(data)

        # Validate against catalog — should be ACTIVE
        report = resolve_plugin_status(
            restored.plugin_identity,
            restored.effect_type,
            catalog_spec=spec,
        )
        assert report.status == PluginStatus.ACTIVE
        # User params unchanged
        assert restored.params["radius"] == 7.5
