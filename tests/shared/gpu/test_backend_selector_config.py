"""P2-05/P2-06: session backend config migration + selection diagnostics."""
from __future__ import annotations

import json

import pytest

from src.shared.config.config_manager import ConfigManager
from src.shared.gpu.backend_types import (
    BackendSelection,
    GraphicsBackendKind,
)
from src.shared.gpu.backend_selector import resolve_backend_plan
from src.shared.gpu.capabilities import (
    DimensionCapability,
    detect_backend_capabilities,
)


# ---------------------------------------------------------------------------
# P2-05 config migration
# ---------------------------------------------------------------------------

def _write_config(path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def _load(tmp_path, payload: dict) -> ConfigManager:
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path, payload)
    mgr = ConfigManager(config_path=str(cfg_path))
    mgr.load_config()
    return mgr


def test_fresh_config_gets_backend_section_with_auto_defaults(tmp_path) -> None:
    mgr = _load(tmp_path, {"locale": "en_US"})
    backend = mgr.get("backend")
    assert backend == {
        "graphics": "auto", "compute": "auto",
        "decode": "auto", "interop": "auto",
    }
    assert (tmp_path / "config.json").exists()
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert "backend" in on_disk  # migration persisted


def test_explicit_backend_config_survives_load(tmp_path) -> None:
    payload = {"backend": {"graphics": "force_opengl", "compute": "auto",
                           "decode": "cpu", "interop": "none"}}
    mgr = _load(tmp_path, payload)
    sel = mgr.get_backend_selection()
    assert sel.graphics is GraphicsBackendKind.OPENGL
    assert sel.graphics_forced is True
    assert sel.decode.value == "cpu"


def test_invalid_backend_value_resets_to_auto_with_save(tmp_path) -> None:
    payload = {"backend": {"graphics": "vulkan", "compute": "auto",
                           "decode": "auto", "interop": "auto"}}
    mgr = _load(tmp_path, payload)
    backend = mgr.get("backend")
    assert backend["graphics"] == "auto"  # 诊断重置，不保留坏值
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["backend"]["graphics"] == "auto"


def test_backend_section_wrong_type_resets(tmp_path) -> None:
    mgr = _load(tmp_path, {"backend": "yes"})
    assert isinstance(mgr.get("backend"), dict)


def test_default_config_keeps_opengl_behavior(tmp_path) -> None:
    """默认仍是 OpenGL（auto 不改变任何运行时行为）。"""
    mgr = _load(tmp_path, {})
    sel = mgr.get_backend_selection()
    assert sel.graphics.value == "auto"


# ---------------------------------------------------------------------------
# P2-06 selector
# ---------------------------------------------------------------------------

def _caps(graphics=None, compute=None, decode=None, interop=None):
    from src.shared.gpu.backend_types import (
        ComputeBackendKind, DecodeBackendKind, GraphicsBackendKind,
        InteropBackendKind,
    )
    from src.shared.gpu.capabilities import BackendCapabilities

    def _dim(kind, available, reason="", **kw):
        return DimensionCapability(kind=kind, available=available,
                                   reason=reason, **kw)
    return BackendCapabilities(
        graphics=graphics or _dim(GraphicsBackendKind.OPENGL, True),
        compute=compute or _dim(ComputeBackendKind.GLSL_FRAGMENT, True,
                                reason="gl_compute_unavailable_version=4.1"),
        decode=decode or _dim(DecodeBackendKind.CPU, True),
        interop=interop or _dim(InteropBackendKind.NONE, False,
                                reason="no_mapped_surface"),
        environment={"platform": "Darwin"},
    )


def test_auto_on_macos_resolves_opengl_fragment_cpu() -> None:
    plan = resolve_backend_plan(BackendSelection(), _caps())
    assert plan.graphics.selected == "opengl"
    assert plan.compute.selected == "glsl_fragment"
    assert plan.decode.selected == "cpu"
    # auto 保持现有 OpenGL 默认路径。
    assert plan.graphics.fallback_reason == ""
    assert plan.graphics.fallback_trail == ()
    assert plan.compute.fallback_reason  # metal_compute 不可用的原因被记录
    assert plan.graphics.available and plan.compute.available
    assert plan.decode.available
    # interop=none 在纯 GL 播放路径是常态（不算错误）
    assert plan.interop.available is False
    assert plan.interop.selected == "none"


def test_auto_fallback_records_trail() -> None:
    caps = _caps(
        graphics=DimensionCapability(
            kind=GraphicsBackendKind.OPENGL, available=False,
            reason="no_live_gl_context"),
    )
    plan = resolve_backend_plan(BackendSelection(), caps)
    assert plan.graphics.available is False
    assert plan.graphics.fallback_trail == ("opengl", "metal")
    assert plan.diagnostics()


def test_explicit_unavailable_never_silently_switches() -> None:
    caps = _caps()  # metal 不可用
    sel = BackendSelection.from_dict({"graphics": "metal"})
    plan = resolve_backend_plan(sel, caps)
    assert plan.graphics.selected == "metal"
    assert plan.graphics.available is False
    assert plan.graphics.fallback_reason  # 诊断给出原因


def test_explicit_available_is_honored() -> None:
    caps = _caps()
    sel = BackendSelection.from_dict({"graphics": "opengl"})
    plan = resolve_backend_plan(sel, caps)
    assert plan.graphics.selected == "opengl"
    assert plan.graphics.available is True
    assert plan.graphics.fallback_reason == ""


def test_explicit_metal_resolves_while_opengl_context_is_live() -> None:
    caps = detect_backend_capabilities(
        system="Darwin", gl_version="4.1", gl_renderer="test renderer")
    if caps.environment.get("metal_graphics_available") != "true":
        pytest.skip("requires an available Metal device")
    plan = resolve_backend_plan(
        BackendSelection.from_dict({"graphics": "metal"}), caps)
    assert plan.graphics.available is True
    assert plan.graphics.selected == "metal"


def test_forced_unavailable_reports_diagnosis() -> None:
    caps = _caps()
    sel = BackendSelection.from_dict({"graphics": "force_metal"})
    plan = resolve_backend_plan(sel, caps)
    assert plan.graphics.forced is True
    assert plan.graphics.available is False
    assert "as_dict" in dir(plan)
    data = plan.as_dict()
    assert data["requested_backend"]["graphics"] == "metal"
    assert data["selected_backend"]["graphics"] == "metal"
