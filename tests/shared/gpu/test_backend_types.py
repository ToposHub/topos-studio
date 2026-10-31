"""P2-01/P2-02: four-dim backend types and probe-backed capabilities."""
from __future__ import annotations

import pytest

from src.shared.gpu.backend_types import (
    BackendSelection,
    ComputeBackendKind,
    DecodeBackendKind,
    GraphicsBackendKind,
    InteropBackendKind,
    all_dimension_values,
    parse_dimension,
)
from src.shared.gpu.capabilities import (
    detect_backend_capabilities,
    detect_compute_backend,
    detect_decode_backend,
    detect_graphics_backend,
    detect_interop_backend,
)


# ---------------------------------------------------------------------------
# P2-01 types + serialization
# ---------------------------------------------------------------------------

def test_defaults_are_auto_and_round_trip() -> None:
    sel = BackendSelection()
    assert sel.graphics is GraphicsBackendKind.AUTO
    assert sel.compute is ComputeBackendKind.AUTO

    restored = BackendSelection.from_dict(sel.as_dict())
    assert restored == sel


def test_from_dict_validates_unknown_values() -> None:
    with pytest.raises(ValueError, match="invalid graphics backend"):
        BackendSelection.from_dict({"graphics": "vulkan"})
    with pytest.raises(ValueError, match="invalid decode backend"):
        BackendSelection.from_dict({"decode": "quicktime"})


def test_force_prefix_is_parsed_not_silently_dropped() -> None:
    kind, forced = parse_dimension(GraphicsBackendKind, "force_metal", dimension="graphics")
    assert kind is GraphicsBackendKind.METAL
    assert forced is True

    sel = BackendSelection.from_dict({"graphics": "force_metal"})
    assert sel.graphics is GraphicsBackendKind.METAL
    assert sel.graphics_forced is True
    assert sel.as_dict()["forced"] == {
        "graphics": True, "compute": False, "decode": False, "interop": False,
    }


def test_from_env_reads_dimension_variables() -> None:
    sel = BackendSelection.from_env({
        "TOPOS_BACKEND_GRAPHICS": "opengl",
        "TOPOS_BACKEND_COMPUTE": "force_glsl_fragment",
        "TOPOS_BACKEND_DECODE": "videotoolbox",
        "TOPOS_BACKEND_INTEROP": "cvmetal",
    })
    assert sel.graphics is GraphicsBackendKind.OPENGL
    assert sel.compute is ComputeBackendKind.GLSL_FRAGMENT
    assert sel.compute_forced is True
    assert sel.decode is DecodeBackendKind.VIDEOTOOLBOX
    assert sel.interop is InteropBackendKind.CVMETAL


def test_value_space_is_stable_for_schema_docs() -> None:
    values = all_dimension_values()
    assert values["graphics"] == [
        "auto", "opengl", "metal", "d3d11", "d3d12"]
    assert "cuda" in values["compute"]  # CUDA is compute, never graphics


# ---------------------------------------------------------------------------
# P2-02 capabilities
# ---------------------------------------------------------------------------

def test_graphics_reports_live_driver_as_probe() -> None:
    cap = detect_graphics_backend(gl_version="4.1 ATI-6.1.13", gl_renderer="AMD")
    assert cap.available is True
    assert cap.kind is GraphicsBackendKind.OPENGL
    assert cap.evidence == "probe"


def test_graphics_without_context_probes_metal_timeline_backend() -> None:
    cap = detect_graphics_backend(system="Darwin", gl_version=None)
    from src.shared.gpu.metal import MetalComputeContext
    available = MetalComputeContext.is_supported()
    assert cap.kind is GraphicsBackendKind.METAL
    assert cap.available is available
    assert (cap.reason == "") is available


def test_compute_maps_gl41_to_fragment_with_reason() -> None:
    cap = detect_compute_backend(gl_version="4.1 ATI-6.1.13")
    assert cap.kind is ComputeBackendKind.GLSL_FRAGMENT
    assert "4.1" in cap.reason

    cap43 = detect_compute_backend(gl_version="4.3")
    assert cap43.kind is ComputeBackendKind.GLSL_COMPUTE


def test_decode_requires_media_path_for_probe() -> None:
    cap = detect_decode_backend(system="Darwin")
    assert cap.available is False
    assert cap.reason == "probe_requires_media_path_and_codec"
    assert cap.evidence == "static"


def test_interop_env_flag_never_reports_zero_copy() -> None:
    cap = detect_interop_backend(
        system="Windows", env={"TOPOS_D3D11_SHARED_MAPPED": "1"})
    assert cap.available is False
    assert cap.zero_copy is False
    assert "env_flag_unproven" in cap.reason


def test_macos_interop_reports_iosurface_probe_backed(monkeypatch) -> None:
    # 宿主无关：iosurface 探测依赖 PyObjC + 真实分配映射（AUD-35910 后
    # 可用性标志不再构成零拷贝证据），此处注入探测成功态，钉 Darwin
    # 分支的选择/序列化契约本身
    from src.shared.media import topos_surface_capability

    monkeypatch.setattr(
        topos_surface_capability, "_probe_macos_iosurface",
        lambda: (True, ""),
    )
    cap = detect_interop_backend(system="Darwin", env={})
    assert cap.kind is InteropBackendKind.IOSURFACE
    assert cap.available is True
    assert cap.zero_copy is True
    assert cap.evidence == "probe"


def test_full_snapshot_serializes_and_lists_unavailable() -> None:
    snap = detect_backend_capabilities(system="Darwin", env={})
    data = snap.as_dict()
    # W0-03: platform 词典（d3d11/d3d11va/nvdec/cuda/directml...）入快照
    assert set(data) == {"graphics", "compute", "decode", "interop",
                         "environment", "platform"}
    # macOS: GL 4.1 无 compute（未传 gl_version → 无上下文诊断）、decode 未探测
    missing = snap.unavailable_reasons()
    assert "decode" in missing
