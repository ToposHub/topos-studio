"""FX-H01 / FX-M03 regression: effect capability & diagnostics.

FX-H01: every attachable effect must declare which backends render it,
so 'this effect silently vanishes from the export' becomes a visible
status instead of a silent drop. The capability resolver computes this
from the EFFECTIVE backend coverage (the actual dispatch tables), not a
hand-maintained table — so adding a handler updates the UI automatically.

FX-M03: when a shader fails to compile or a param can't be sampled, the
user must see a per-effect diagnostic state (active / degraded / error)
instead of an unexplained 'effect invalid'.

These tests pin both contracts in pure data space (no GL/Qt), matching
the audit's testability constraint (§7).
"""

from __future__ import annotations

import pytest

from src.features.shared.effects import (
    CPU_PREVIEW_EFFECT_TYPES,
    EXPORT_EFFECT_TYPES,
    GPU_EXPORT_EFFECT_TYPES,
    EffectCapability,
    EffectDiagnostic,
    EffectDiagnosticState,
    check_backend_support,
    classify_for_export,
    compute_effect_status,
    decide_export_policy,
    decide_preview_policy,
    get_effect_catalog,
    parse_backend_declaration,
    resolve_capability,
)

pytestmark = pytest.mark.edit_core


@pytest.fixture(scope="module")
def catalog():
    return get_effect_catalog()


# ── FX-H01: capability resolution matches effective backend coverage ─

class TestCapabilityResolution:
    @pytest.mark.parametrize("effect_type", [
        "blur", "sharpen", "denoise", "edge_detect",
        "erode_dilate", "defocus", "glow",
    ])
    def test_capability_matches_real_backends(self, catalog, effect_type) -> None:
        """The resolver must agree with the actual dispatch tables.

        GPU preview == catalog-known; CPU preview == the preview handler
        set; export == the export handler set. This is the single source
        of truth the panel badges from — if it drifts from the renderers,
        the user is misled.
        """
        cap = resolve_capability(effect_type, catalog_known=catalog.is_known(effect_type))
        assert cap.gpu_preview is True, f"{effect_type} should be GPU-previewable"
        assert cap.cpu_preview == (effect_type in CPU_PREVIEW_EFFECT_TYPES)
        assert cap.cpu_export == (effect_type in EXPORT_EFFECT_TYPES)
        assert cap.gpu_export == (effect_type in GPU_EXPORT_EFFECT_TYPES)

    def test_unknown_effect_has_no_backend(self) -> None:
        """A non-catalog effect (e.g. a FX-C06 ghost) has no backend at all."""
        cap = resolve_capability("transform", catalog_known=False)
        # P7-06：无后端 = 后端轴为空集（不是默认 opengl——默认值仅用于
        # 已知效果/已声明插件，未知效果必须整体不可用）
        assert cap == EffectCapability(
            gpu_preview=False, cpu_preview=False, gpu_export=False, cpu_export=False,
            graphics_backends=frozenset(), compute_backends=frozenset(),
        )
        assert cap.previewable is False
        assert cap.exportable is False

    def test_gpu_only_effect_flags_export_blocked(self, catalog) -> None:
        """A preview-only plugin remains blocked from timeline export."""
        effect_type = "plugin:Color_Space_Transform"
        cap = resolve_capability(
            effect_type, catalog_known=catalog.is_known(effect_type))
        assert cap.previewable is True
        assert cap.exportable is False, (
            "plugin without an export handler must be flagged for the UI"
        )

    def test_full_coverage_effect_is_fully_capable(self, catalog) -> None:
        """blur has GPU preview + CPU preview + export — the gold standard."""
        cap = resolve_capability("blur", catalog_known=True)
        assert cap.gpu_preview and cap.cpu_preview and cap.cpu_export
        assert cap.exportable and cap.previewable


# ── FX-H01: export preflight (audit P4-step-4) ───────────────────────

class TestExportPreflight:
    def test_previewable_but_not_exportable_is_blocked(self, catalog) -> None:
        """glow is previewable (GPU) and has an export handler → not blocked.

        the plugin is previewable but has NO export handler → blocked. This is
        the audit's headline 'silently disappears from the deliverable'
        case made visible."""
        # glow: has export handler → not blocked
        glow = resolve_capability("glow", catalog_known=True)
        assert classify_for_export("glow", glow) is None
        # Color Space Transform plugin: preview-only → blocked
        plugin = resolve_capability("plugin:Color_Space_Transform", catalog_known=True)
        assert classify_for_export(
            "plugin:Color_Space_Transform", plugin,
        ) == EffectDiagnosticState.EXPORT_BLOCKED

    def test_unsupported_in_preview_is_unsupported(self) -> None:
        cap = resolve_capability("vignette", catalog_known=False)
        assert classify_for_export("vignette", cap) == EffectDiagnosticState.UNSUPPORTED

    def test_fully_capable_passes_preflight(self, catalog) -> None:
        cap = resolve_capability("blur", catalog_known=True)
        assert classify_for_export("blur", cap) is None


# ── FX-M03: diagnostic state ─────────────────────────────────────────

class TestDiagnosticState:
    def test_active_and_degraded_are_ok(self) -> None:
        assert EffectDiagnostic("e1", "blur", EffectDiagnosticState.ACTIVE).ok
        assert EffectDiagnostic("e2", "blur", EffectDiagnosticState.PREVIEW_DEGRADED).ok

    def test_error_states_are_not_ok(self) -> None:
        for bad in (EffectDiagnosticState.BYPASSED, EffectDiagnosticState.UNSUPPORTED,
                    EffectDiagnosticState.COMPILE_ERROR, EffectDiagnosticState.PARAM_ERROR,
                    EffectDiagnosticState.EXPORT_BLOCKED):
            assert not EffectDiagnostic("e", "blur", bad).ok

    def test_state_enum_serialises_to_string(self) -> None:
        """Values are strings so they serialise without the enum import."""
        assert EffectDiagnosticState.EXPORT_BLOCKED == "export_blocked"
        assert EffectDiagnosticState.ACTIVE.value == "active"


# ── Capability ledger stays in sync with renderers (FX-H01 guard) ────

class TestCapabilityLedgerSync:
    """Guard against the capability table drifting from the renderers.

    These sets ARE the renderer dispatch tables re-declared for the
    capability resolver. If someone adds a CPU/export handler, this test
    forces them to update the capability sets too (or vice-versa)."""

    def test_cpu_preview_set_matches_known_handlers(self) -> None:
        # timeline_preview_renderer._apply_clip_effects_cpu dispatches blur/sharpen
        assert CPU_PREVIEW_EFFECT_TYPES == frozenset({"blur", "sharpen"})

    def test_export_set_matches_known_handlers(self) -> None:
        # timeline_export._CPU_EFFECT_HANDLERS dispatches these effects.
        assert EXPORT_EFFECT_TYPES == frozenset({
            "blur", "sharpen", "glow", "erode_dilate", "edge_detect",
            "denoise", "defocus",
        })

    def test_gpu_export_set_matches_native_metal_effect_handlers(self) -> None:
        assert GPU_EXPORT_EFFECT_TYPES == frozenset({
            "blur", "sharpen", "glow", "erode_dilate", "edge_detect",
            "denoise", "defocus",
        })


# ── FX-M05: unified failure policy ───────────────────────────────────

class TestPreviewFailurePolicy:
    """Preview may degrade but must badge — never silently change picture."""

    def test_full_coverage_on_gpu_is_active(self) -> None:
        cap = EffectCapability(gpu_preview=True, cpu_preview=True,
                               gpu_export=False, cpu_export=True)
        assert decide_preview_policy(cap, gpu_available=True) == EffectDiagnosticState.ACTIVE

    def test_gpu_only_on_cpu_machine_is_unsupported(self) -> None:
        """A GPU-only effect on a GPU-less machine → UNSUPPORTED,
        not a silent no-op. The panel must badge it."""
        cap = EffectCapability(gpu_preview=True, cpu_preview=False,
                               gpu_export=False, cpu_export=False)
        assert decide_preview_policy(cap, gpu_available=False) == EffectDiagnosticState.UNSUPPORTED

    def test_gpu_effect_with_cpu_fallback_degrades(self) -> None:
        """blur has a CPU handler, so a GPU-less machine DEGRADES (keeps
        editing) rather than dropping the effect silently."""
        cap = EffectCapability(gpu_preview=True, cpu_preview=True,
                               gpu_export=False, cpu_export=True)
        assert decide_preview_policy(cap, gpu_available=False) == EffectDiagnosticState.PREVIEW_DEGRADED


class TestExportFailurePolicy:
    """Export must not silently degrade — block or badge, never omit."""

    def test_exportable_effect_is_active(self) -> None:
        cap = EffectCapability(gpu_preview=True, cpu_preview=True,
                               gpu_export=False, cpu_export=True)
        assert decide_export_policy(cap) == EffectDiagnosticState.ACTIVE

    def test_preview_only_effect_is_blocked(self) -> None:
        """an effect with no export handler → EXPORT_BLOCKED, never silently
        omitted from the deliverable."""
        cap = EffectCapability(gpu_preview=True, cpu_preview=False,
                               gpu_export=False, cpu_export=False)
        assert decide_export_policy(cap) == EffectDiagnosticState.EXPORT_BLOCKED


# ── FX-M03: per-effect UI status badge ───────────────────────────────

class TestComputeEffectStatus:
    """The panel badges each effect from compute_effect_status. Pins the
    static-capability status so a silently-dropped effect is visible."""

    def test_full_coverage_is_active(self) -> None:
        cap = EffectCapability(gpu_preview=True, cpu_preview=True,
                               gpu_export=False, cpu_export=True)
        d = compute_effect_status("blur", cap)
        assert d.state == EffectDiagnosticState.ACTIVE

    def test_gpu_only_no_export_is_export_blocked(self) -> None:
        """A preview-only plugin gets an EXPORT_BLOCKED badge."""
        cap = EffectCapability(gpu_preview=True, cpu_preview=False,
                               gpu_export=False, cpu_export=False)
        d = compute_effect_status("plugin:Color_Space_Transform", cap)
        assert d.state == EffectDiagnosticState.EXPORT_BLOCKED
        assert d.detail  # non-empty explanation

    def test_gpu_and_export_but_no_cpu_is_preview_degraded(self) -> None:
        """glow: GPU + export, no CPU fallback → PREVIEW_DEGRADED badge
        (CPU preview machine would lose it)."""
        cap = EffectCapability(gpu_preview=True, cpu_preview=False,
                               gpu_export=False, cpu_export=True)
        d = compute_effect_status("glow", cap)
        assert d.state == EffectDiagnosticState.PREVIEW_DEGRADED

    def test_unrenderable_is_unsupported(self) -> None:
        """No backend at all → UNSUPPORTED."""
        cap = EffectCapability(gpu_preview=False, cpu_preview=False,
                               gpu_export=False, cpu_export=False)
        d = compute_effect_status("ghost", cap)
        assert d.state == EffectDiagnosticState.UNSUPPORTED

    @pytest.mark.parametrize("effect_type", [
        "blur", "sharpen", "denoise", "edge_detect",
        "erode_dilate", "defocus", "glow",
    ])
    def test_real_catalog_effects_get_a_status(self, catalog, effect_type) -> None:
        """Every catalog effect resolves to a defined status (never silent)."""
        cap = resolve_capability(effect_type, catalog_known=catalog.is_known(effect_type))
        d = compute_effect_status(effect_type, cap)
        assert d.state in (
            EffectDiagnosticState.ACTIVE,
            EffectDiagnosticState.EXPORT_BLOCKED,
            EffectDiagnosticState.PREVIEW_DEGRADED,
        )


# =========================================================================
# Phase 7（P7-06/07/08）：后端轴 + manifest 声明 + 会话后端诊断
# =========================================================================


class TestBackendAxes:
    """P7-06：EffectCapability 的 graphics/compute 后端轴。"""

    def test_defaults_are_opengl_only(self):
        """旧调用方零改动兼容：默认轴 = opengl / glsl_fragment。"""
        cap = EffectCapability(
            gpu_preview=True, cpu_preview=False, gpu_export=False, cpu_export=True,
        )
        assert cap.graphics_backends == frozenset({"opengl"})
        assert cap.compute_backends == frozenset({"glsl_fragment"})
        assert cap.supports_graphics("opengl")
        assert not cap.supports_graphics("metal")
        assert cap.supports_compute("glsl_fragment")

    def test_explicit_backend_declaration(self):
        cap = EffectCapability(
            gpu_preview=True, cpu_preview=False, gpu_export=False, cpu_export=False,
            graphics_backends=frozenset({"opengl", "metal"}),
            compute_backends=frozenset({"metal_compute"}),
        )
        assert cap.supports_graphics("metal")
        assert cap.supports_compute("metal_compute")
        assert not cap.supports_graphics("opengl") is False  # 两者皆可
        assert cap.supports_graphics("opengl")
        assert not cap.supports_compute("cuda")

    def test_fragment_fallback_for_glsl_compute(self):
        """compute 效果声明 glsl_compute 时，fragment 自动回退视为可用。"""
        cap = EffectCapability(
            gpu_preview=True, cpu_preview=False, gpu_export=False, cpu_export=False,
            compute_backends=frozenset({"glsl_compute"}),
        )
        assert cap.supports_compute("glsl_fragment")  # §0.4 既有回退通道
        assert not cap.supports_compute("metal_compute")

    def test_cpu_is_always_valid_compute(self):
        """CPU 片段回退（§4.3 层级末位）一律可用。"""
        cap = EffectCapability(
            gpu_preview=True, cpu_preview=False, gpu_export=False, cpu_export=False,
        )
        assert cap.supports_compute("cpu")

    def test_resolve_capability_empty_for_unknown(self):
        cap = resolve_capability("nonexistent", catalog_known=False)
        assert cap.graphics_backends == frozenset()
        assert cap.compute_backends == frozenset()

    def test_resolve_capability_carries_axes(self):
        cap = resolve_capability("blur", catalog_known=True)
        assert "opengl" in cap.graphics_backends
        assert "metal" in cap.graphics_backends
        assert "glsl_fragment" in cap.compute_backends
        assert "metal_compute" in cap.compute_backends
        assert check_backend_support(
            "blur", cap, graphics="metal", compute="metal_compute",
        ) is None

    def test_metal_is_not_declared_for_unimplemented_effect_export(self):
        cap = resolve_capability("plugin:Color_Space_Transform", catalog_known=True)
        assert not cap.supports_graphics("metal")
        assert not cap.supports_compute("metal_compute")


class TestBackendDeclarationParsing:
    """P7-07：.tglsl manifest 的 @graphics_backends / @compute_backends。"""

    def test_parse_simple(self):
        assert parse_backend_declaration("opengl,metal", kind="graphics") == (
            frozenset({"opengl", "metal"})
        )
        assert parse_backend_declaration(" metal_compute ", kind="compute") == (
            frozenset({"metal_compute"})
        )

    def test_parse_empty_is_empty_set(self):
        assert parse_backend_declaration("", kind="graphics") == frozenset()
        assert parse_backend_declaration(None, kind="compute") == frozenset()

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError, match="vulkan"):
            parse_backend_declaration("opengl,vulkan", kind="graphics")

    def test_auto_is_not_a_valid_declaration(self):
        with pytest.raises(ValueError, match="auto"):
            parse_backend_declaration("auto", kind="graphics")

    def test_manifest_defaults_applied(self):
        """未声明后端的旧插件 → 解析产物按 opengl/glsl_fragment 兜底。"""
        from src.features.color.core.plugin_manager import PluginManager

        pm = PluginManager.__new__(PluginManager)  # 纯解析，不走 init
        caps = pm._parse_capabilities("// @precision float16\n// @param slider x \"X\" 0 0 1\n")
        assert caps["graphics_backends"] == "opengl"
        assert caps["compute_backends"] == "glsl_fragment"

    def test_manifest_explicit_declaration_parsed(self):
        from src.features.color.core.plugin_manager import PluginManager

        pm = PluginManager.__new__(PluginManager)
        caps = pm._parse_capabilities(
            "// @graphics_backends opengl,metal\n"
            "// @compute_backends metal_compute,glsl_fragment\n"
        )
        assert caps["graphics_backends"] == "opengl,metal"
        assert caps["compute_backends"] == "metal_compute,glsl_fragment"
        g = parse_backend_declaration(caps["graphics_backends"], kind="graphics")
        c = parse_backend_declaration(caps["compute_backends"], kind="compute")
        assert g == frozenset({"opengl", "metal"})
        assert c == frozenset({"metal_compute", "glsl_fragment"})


class TestSessionBackendDiagnostics:
    """P7-08：按会话后端检查——不可用必须显式诊断，绝不静默丢效果。"""

    def _cap(self, graphics=("opengl",), compute=("glsl_fragment",)):
        return EffectCapability(
            gpu_preview=True, cpu_preview=False, gpu_export=False, cpu_export=False,
            graphics_backends=frozenset(graphics),
            compute_backends=frozenset(compute),
        )

    def test_available_returns_none(self):
        assert check_backend_support(
            "blur", self._cap(), graphics="opengl", compute="glsl_fragment",
        ) is None

    def test_unsupported_graphics_gets_diagnostic(self):
        d = check_backend_support("myeffect", self._cap(), graphics="metal")
        assert d is not None
        assert d.state == EffectDiagnosticState.UNSUPPORTED
        assert "metal" in d.detail
        assert "opengl" in d.detail  # 诊断列出已声明后端

    def test_unsupported_compute_gets_diagnostic(self):
        cap = self._cap(graphics=("opengl", "metal"), compute=("glsl_fragment",))
        d = check_backend_support("myeffect", cap, graphics="metal", compute="metal_compute")
        assert d is not None
        assert "metal_compute" in d.detail

    def test_no_backend_at_all(self):
        cap = self._cap(graphics=(), compute=())
        d = check_backend_support("myeffect", cap, graphics="opengl")
        assert d is not None
        assert "no render backend declared" in d.detail
