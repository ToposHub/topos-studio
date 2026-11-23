""".tglsl 插件的 Metal 能力判定（阶段 6.3 / 6.4）与后端回退规划（阶段 6.5）。

纯 Python，不触达 GPU。钉住：
- 「声明 ≠ 能力」：旧插件未声明 metal 时即使结构可译也按 OpenGL 解析；声明了但
  转译失败的插件生效后端不含 metal，并留下可定位的诊断；
- 仓库里声明 metal 的插件集合 == 真实对拍验证通过的集合（21 个）；噪声类不声明；
- 预览 / 导出分别预检：只有兼容链路完整才回退，原因定位到效果实例；
- 保存 → 重开：插件 ID、参数、未知字段与身份往返不变；禁用 / 缺失 / 版本或
  schema 不匹配都能定位到具体实例。
"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.effect_instance import EffectInstance
from src.features.shared.effects import (
    check_backend_support,
    get_effect_catalog,
    resolve_capability,
)
from src.features.shared.effects import effect_catalog as EC
from src.features.shared.effects import msl_effect_registry as registry
from src.features.shared.effects import msl_plugin_verification as verification
from src.features.shared.effects.effect_backend_fallback import (
    FALLBACK_CHAINS,
    fallback_reasons,
    plan_backend_fallback,
    stage_backend_support,
)
from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind
from src.features.shared.effects.plugin_identity import (
    PluginStatus,
    build_plugin_identity,
    resolve_plugin_status,
    validate_effect_instances,
)
from src.features.shared.effects.tglsl_metal_support import (
    TglslMetalSupport,
    analyze_tglsl_metal,
    clear_metal_support_cache,
    plugin_metal_support,
)

_OK_SOURCE = "vec3 process(vec3 color, vec2 uv) {\n    return color;\n}\n"
_LOOP_SOURCE = ("vec3 process(vec3 color, vec2 uv) {\n"
                "    for (int i = 0; i < 2; i++) { color += 0.1; }\n    return color;\n}\n")
_DUAL = (("opengl", "metal"), ("glsl_fragment", "metal_compute"))


def _spec(source=_OK_SOURCE, backends=((), ()), plugin_id="plugin:synthetic", version="1.0"):
    graphics, compute = backends
    return EffectSpec(
        effect_type=plugin_id, name="synthetic", category="plugin", param_defs=(),
        shader_kind=ShaderKind.DCTL_PROCESS, shader_sources=(source,), dispatch_key=plugin_id,
        plugin_id=plugin_id, plugin_version=version, plugin_source_glsl=source,
        graphics_backends=graphics, compute_backends=compute)


# ── 声明 ≠ 能力 ─────────────────────────────────────────────────────────


def test_undeclared_plugin_stays_opengl_only_even_when_transpilable():
    support = plugin_metal_support(_spec())                      # 结构可译，但没有声明 metal
    assert isinstance(support, TglslMetalSupport)
    assert support.declared_metal is False and support.transpilable is None
    assert support.effective_graphics == frozenset({"opengl"})
    assert support.effective_compute == frozenset({"glsl_fragment"})
    assert support.supports_metal is False and support.diagnostic is None


def test_declared_and_transpilable_plugin_gets_metal():
    support = plugin_metal_support(_spec(backends=_DUAL))
    assert (support.declared_metal, support.transpilable, support.diagnostic) == (True, True, None)
    assert support.effective_graphics == frozenset({"opengl", "metal"})
    assert support.effective_compute == frozenset({"glsl_fragment", "metal_compute"})
    assert support.msl_fingerprint and support.to_dict()["transpilable"] is True


def test_declared_but_untranspilable_plugin_keeps_opengl_and_a_diagnostic():
    support = plugin_metal_support(_spec(_LOOP_SOURCE, _DUAL))
    assert (support.declared_metal, support.transpilable) == (True, False)
    assert support.effective_graphics == frozenset({"opengl"})
    assert support.effective_compute == frozenset({"glsl_fragment"})
    diagnostic = support.diagnostic
    assert diagnostic.code == "unsupported_construct:loop" and diagnostic.stage == "parse"
    assert diagnostic.plugin_id == "synthetic" and diagnostic.plugin_version == "1.0"
    assert diagnostic.backend == "metal" and diagnostic.snippet


def test_metal_only_declaration_that_fails_leaves_no_backend():
    """只声明 metal 却转译失败：没有任何可用后端（由 check_backend_support 明确报告）。"""
    support = plugin_metal_support(_spec(_LOOP_SOURCE, (("metal",), ("metal_compute",))))
    assert support.effective_graphics == frozenset() and support.effective_compute == frozenset()
    metal_only_ok = plugin_metal_support(_spec(_OK_SOURCE, (("metal",), ("metal_compute",))))
    assert metal_only_ok.effective_graphics == frozenset({"metal"})      # 不含 opengl：不会凭空获得


def test_non_plugin_spec_has_no_plugin_support():
    builtin = EffectSpec(effect_type="blur", name="Blur", category="filter", param_defs=(),
                         shader_kind=ShaderKind.SINGLE_PASS, shader_sources=("x",), dispatch_key="blur")
    assert plugin_metal_support(builtin) is None
    plugin_without_source = EffectSpec(
        effect_type="plugin:x", name="x", category="plugin", param_defs=(),
        shader_kind=ShaderKind.SINGLE_PASS, shader_sources=("x",), dispatch_key="plugin:x",
        plugin_id="plugin:x")
    assert plugin_metal_support(plugin_without_source) is None


def test_analysis_is_cached_by_content_and_can_be_cleared():
    spec = _spec(backends=_DUAL)
    assert plugin_metal_support(spec) is plugin_metal_support(spec)
    clear_metal_support_cache()
    assert plugin_metal_support(spec) is not None
    changed = _spec(_OK_SOURCE.replace("return color;", "return color * 2.0;"), _DUAL)
    assert plugin_metal_support(changed).msl_fingerprint != plugin_metal_support(spec).msl_fingerprint


def test_analyze_tglsl_metal_direct_entry():
    support = analyze_tglsl_metal("p", "0.1", _OK_SOURCE, (), declared_graphics=frozenset({"opengl", "metal"}))
    assert support.transpilable is True and support.supports_metal


# ── 仓库插件：声明集合 == 验证通过的集合 ───────────────────────────────


def _plugin_specs():
    return {s.plugin_id[7:]: s for s in get_effect_catalog().get_all_specs()
            if s.effect_type.startswith("plugin:")}


def test_declared_metal_plugins_are_exactly_the_verified_ones():
    specs = _plugin_specs()
    declared = {name for name, s in specs.items() if "metal" in s.graphics_backends}
    records = verification.PLUGIN_VERIFICATION_RECORDS
    verified = {name for name, r in records.items() if r.status == registry.STATUS_VERIFIED}
    assert len(specs) == 40
    assert declared == verified, (sorted(declared ^ verified))
    assert len(declared) == 21 and "warm_tint" in declared and "Channel_Mixer" in declared
    assert get_msl_registry_verified() == tuple(sorted(verified))


def get_msl_registry_verified():
    return registry.get_msl_effect_registry().verified_plugins()


@pytest.mark.parametrize("name", ["Film_Grain", "Glitch_Blocks", "HighPass_Retouch"])
def test_noise_gated_plugins_are_transpilable_but_not_declared(name):
    spec = _plugin_specs()[name]
    support = plugin_metal_support(spec)
    record = verification.PLUGIN_VERIFICATION_RECORDS[name]
    assert record.status == registry.STATUS_NOISE_GATED
    assert "metal" not in spec.graphics_backends
    assert support.declared_metal is False and support.effective_graphics == {"opengl"}


def test_every_verified_plugin_carries_measured_evidence():
    for name, record in verification.PLUGIN_VERIFICATION_RECORDS.items():
        assert record.device and record.date == verification.VERIFICATION_DATE
        assert record.evidence == verification.EVIDENCE_FILE
        assert record.max_abs_diff >= 0 and record.max_fp16_ulp >= 0 and record.note


@pytest.mark.parametrize("name", sorted(verification.PLUGIN_VERIFICATION_RECORDS))
def test_real_plugin_support_matches_its_record(name):
    spec = _plugin_specs()[name]
    support = plugin_metal_support(spec)
    record = verification.PLUGIN_VERIFICATION_RECORDS[name]
    capability = resolve_capability(spec.effect_type, catalog_known=True)
    if record.status == registry.STATUS_VERIFIED:
        assert support.transpilable is True and support.diagnostic is None
        assert {"opengl", "metal"} <= capability.graphics_backends
        assert "metal_compute" in capability.compute_backends
        assert check_backend_support(spec.effect_type, capability, graphics="metal",
                                     compute="metal_compute") is None
        assert check_backend_support(spec.effect_type, capability, graphics="opengl",
                                     compute="glsl_fragment") is None          # 兼容路径仍在
    else:
        assert not capability.supports_graphics("metal")


def test_legacy_plugin_without_declaration_resolves_to_opengl_capability():
    """计划 6.1：旧 .tglsl 无后端声明时继续按 OpenGL 能力解析。"""
    spec = _plugin_specs()["Color_Space_Transform"]
    assert "metal" not in spec.graphics_backends
    capability = resolve_capability(spec.effect_type, catalog_known=True)
    assert capability.graphics_backends == frozenset({"opengl"})
    assert capability.compute_backends == frozenset({"glsl_fragment"})
    diagnostic = check_backend_support(spec.effect_type, capability, graphics="metal")
    assert diagnostic is not None and "declares: opengl" in diagnostic.detail


def test_catalog_specs_carry_raw_source_and_declarations():
    for spec in _plugin_specs().values():
        assert spec.plugin_source_glsl.strip(), spec.plugin_id
        assert spec.graphics_backends and spec.compute_backends          # 默认值 = OpenGL-only
        assert spec.graphics_backends == tuple(sorted(spec.graphics_backends))
    assert _plugin_specs()["warm_tint"].graphics_backends == ("metal", "opengl")


def test_invalid_backend_declaration_is_diagnosed_not_ignored():
    graphics, compute = EC._plugin_backend_declarations(
        "broken", {"capabilities": {"graphics_backends": "opengl,vulkan",
                                    "compute_backends": "glsl_fragment"}})
    assert graphics == () and compute == ("glsl_fragment",)
    errors = EC.backend_declaration_errors()
    assert any(plugin == "broken" and raw == "opengl,vulkan" and "vulkan" in message
               for plugin, raw, message in errors)


def test_valid_declarations_are_sorted_and_default_equals_explicit_opengl():
    assert EC._plugin_backend_declarations("p", {"capabilities": {
        "graphics_backends": "metal,opengl", "compute_backends": "metal_compute,glsl_fragment"}}) == (
        ("metal", "opengl"), ("glsl_fragment", "metal_compute"))
    assert EC._plugin_backend_declarations("p", {"capabilities": {
        "graphics_backends": "opengl", "compute_backends": "glsl_fragment"}}) == (
        ("opengl",), ("glsl_fragment",))


# ── 后端回退规划（6.5）──────────────────────────────────────────────────


def test_every_effect_runs_on_metal_when_supported():
    effects = [("blur", "b1"), ("plugin:warm_tint", "w1"), ("plugin:Film_Halation", "h1")]
    for stage in ("preview", "export"):
        decisions = plan_backend_fallback(effects, stage=stage)
        if stage == "preview":
            assert [(d.selected_backend, d.fell_back, d.compatible) for d in decisions] == [
                ("metal", False, True)] * 3
        else:
            assert decisions[0].selected_backend == "metal"            # blur 有原生 Metal 导出
            assert decisions[1].selected_backend == "opengl"           # warm_tint：无 Metal 导出链路 → 回退
            assert decisions[2].compatible is False                    # 仅预览：没有兼容回退
    assert FALLBACK_CHAINS == {"metal": ("opengl", "cpu"), "opengl": ("cpu",), "cpu": ()}


def test_preview_and_export_are_planned_separately():
    plugin = [("plugin:Color_Space_Transform", "cst")]
    preview = plan_backend_fallback(plugin, stage="preview")[0]
    export = plan_backend_fallback(plugin, stage="export")[0]
    assert (preview.selected_backend, preview.fell_back, preview.compatible) == ("opengl", True, True)
    assert (export.selected_backend, export.compatible, export.reason_code) == (
        None, False, "no_compatible_fallback")
    assert "export" in export.detail and "没有完整的兼容回退链路" in export.detail
    assert preview.detail == "graphics backend 'metal' not supported (declares: opengl)"


def test_reasons_are_located_on_effect_instances():
    effects = [("blur", "b1"), ("plugin:Color_Space_Transform", "cst-7"), ("plugin:Film_Halation", "h-2")]
    decisions = plan_backend_fallback(effects, stage="preview")
    assert fallback_reasons(decisions) == {
        "cst-7": "requested_backend_unavailable: graphics backend 'metal' not supported (declares: opengl)"}
    assert [d.effect_id for d in decisions] == ["b1", "cst-7", "h-2"]
    export = plan_backend_fallback(effects, stage="export")
    reasons = fallback_reasons(export)
    assert set(reasons) == {"cst-7", "h-2"}
    assert reasons["h-2"].startswith("no_compatible_fallback: 无 metal 导出实现")


def test_runtime_metal_failure_falls_back_only_through_a_complete_chain():
    failure = {("plugin:warm_tint", "metal"): "msl_compile_failed: program_source:10:9: no member named 'x'"}
    decision = plan_backend_fallback([("plugin:warm_tint", "w")], stage="preview",
                                     backend_failures=failure)[0]
    assert (decision.selected_backend, decision.fell_back, decision.reason_code) == (
        "opengl", True, "runtime_backend_failure")
    assert decision.detail.startswith("msl_compile_failed: program_source:10:9")
    blocked = plan_backend_fallback([("plugin:Film_Halation", "h")], stage="export",
                                    backend_failures=failure)[0]
    assert blocked.compatible is False and blocked.reason_code == "no_compatible_fallback"


def test_disabled_effects_are_reported_not_planned():
    decision = plan_backend_fallback([("blur", "b", False)], stage="preview")[0]
    assert (decision.selected_backend, decision.compatible, decision.reason_code) == (None, False, "disabled")


def test_planner_accepts_effect_instances_and_mappings():
    instance = EffectInstance(effect_id="inst-1", effect_type="blur", params={})
    assert plan_backend_fallback([instance], stage="preview")[0].effect_id == "inst-1"
    assert plan_backend_fallback([{"effect_type": "blur", "effect_id": "m"}], stage="preview")[0].effect_id == "m"


def test_planner_rejects_unknown_stage_or_backend():
    with pytest.raises(ValueError):
        plan_backend_fallback([("blur", "b")], stage="render")
    with pytest.raises(ValueError):
        plan_backend_fallback([("blur", "b")], stage="preview", requested_backend="vulkan")
    with pytest.raises(ValueError):
        stage_backend_support("blur", stage="render", backend="metal")


def test_stage_support_explains_missing_stage_backend():
    assert stage_backend_support("blur", stage="export", backend="metal") is None
    assert stage_backend_support("blur", stage="preview", backend="cpu") is None       # blur 有 CPU 预览
    missing = stage_backend_support("plugin:Color_Space_Transform", stage="export", backend="opengl")
    assert missing.detail == "无 opengl 导出实现"
    assert stage_backend_support("plugin:Color_Space_Transform", stage="preview", backend="cpu").detail == \
        "无 CPU 预览实现"


# ── 保存 → 重开（6.5）───────────────────────────────────────────────────


def _saved_plugin_instance(**extra):
    spec = get_effect_catalog().get_spec("plugin:warm_tint")
    instance = EffectInstance(
        effect_id="fx-warm-1", effect_type="plugin:warm_tint",
        params={"strength": 0.8, "future_param": {"keep": 1}},
        plugin_identity=build_plugin_identity(spec).to_dict(), order=2, is_enabled=True)
    data = instance.to_dict()
    data.update(extra)
    return json.loads(json.dumps(data))                              # 一次真实的 JSON 往返


def test_saved_plugin_reopens_with_identical_id_params_and_unknown_fields():
    saved = _saved_plugin_instance(future_top_level={"also": "kept"})
    reopened = EffectInstance.from_dict(saved)
    assert reopened.to_dict() == saved                              # 再保存一次仍是同一份
    assert reopened.effect_type == "plugin:warm_tint" and reopened.effect_id == "fx-warm-1"
    assert reopened.params == {"strength": 0.8, "future_param": {"keep": 1}}
    assert reopened.to_dict()["future_top_level"] == {"also": "kept"}
    identity = reopened.plugin_identity
    assert identity["plugin_id"] == "plugin:warm_tint" and identity["version"] == "1.1"
    assert identity["content_hash"] == get_effect_catalog().get_spec("plugin:warm_tint").plugin_content_hash


def test_reopened_instance_keeps_its_metal_capability_and_is_active():
    reopened = EffectInstance.from_dict(_saved_plugin_instance())
    report = resolve_plugin_status(reopened.plugin_identity, reopened.effect_type,
                                   catalog_spec=get_effect_catalog().get_spec(reopened.effect_type))
    assert report.status == PluginStatus.ACTIVE and report.export_blocked is False
    capability = resolve_capability(reopened.effect_type, catalog_known=True)
    assert capability.supports_graphics("metal")
    decision = plan_backend_fallback([reopened], stage="preview")[0]
    assert decision.selected_backend == "metal" and decision.compatible


def test_legacy_instance_without_identity_still_resolves_but_is_flagged():
    legacy = EffectInstance.from_dict({"effect_id": "old", "effect_type": "plugin:warm_tint",
                                       "params": {"strength": 0.35}})
    assert legacy.plugin_identity is None
    report = resolve_plugin_status(None, legacy.effect_type,
                                   catalog_spec=get_effect_catalog().get_spec(legacy.effect_type))
    assert report.status == PluginStatus.LEGACY and report.export_blocked is False


@pytest.mark.parametrize(("mutate", "status", "blocked"), [
    (lambda d: d.update(version="0.9"), PluginStatus.VERSION_MISMATCH, False),
    (lambda d: d.update(parameter_schema_hash="0" * 64), PluginStatus.SCHEMA_MISMATCH, True),
    (lambda d: d.update(content_hash="1" * 64), PluginStatus.CONTENT_CHANGED, False),
])
def test_changed_disabled_or_corrupted_plugin_is_located(mutate, status, blocked):
    saved = _saved_plugin_instance()
    mutate(saved["plugin_identity"])
    reopened = EffectInstance.from_dict(saved)
    catalog = get_effect_catalog()
    reports = validate_effect_instances([reopened], catalog_lookup=catalog.get_spec)
    assert [(r.effect_id, r.effect_type, r.status, r.export_blocked) for r in reports] == [
        ("fx-warm-1", "plugin:warm_tint", status, blocked)]


def test_missing_plugin_is_located_by_effect_id():
    instance = EffectInstance(effect_id="fx-gone", effect_type="plugin:Was_Never_Here", params={},
                              plugin_identity={"plugin_id": "plugin:Was_Never_Here", "version": "1"})
    report = validate_effect_instances([instance], catalog_lookup=get_effect_catalog().get_spec)[0]
    assert (report.effect_id, report.status, report.export_blocked) == ("fx-gone", PluginStatus.MISSING, True)
    assert "plugin:Was_Never_Here" in report.detail


def test_disabled_plugin_instance_is_not_blocked_but_is_reported():
    saved = _saved_plugin_instance()
    saved["is_enabled"] = False
    reopened = EffectInstance.from_dict(saved)
    decision = plan_backend_fallback([reopened], stage="export")[0]
    assert decision.reason_code == "disabled" and decision.effect_id == "fx-warm-1"
