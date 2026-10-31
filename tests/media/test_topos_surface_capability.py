"""RD5-03: platform surface routing never overclaims zero-copy.

P0-06: an env flag alone can never produce ``zero_copy=True``; the mapped
backend requires a positive in-process probe (``evidence == "probe"``).
"""
from __future__ import annotations

import pytest

from src.shared.media import topos_surface_capability as cap_module
from src.shared.media.topos_surface_capability import (
    ToposSurfaceBackend,
    detect_topos_surface_capability,
)


def test_windows_defaults_to_explicit_staging_fallback() -> None:
    cap = detect_topos_surface_capability(system="Windows", env={})
    assert cap.backend is ToposSurfaceBackend.WINDOWS_D3D11_STAGING
    assert cap.zero_copy is False
    assert cap.copy_bytes is True
    assert cap.reason.startswith("d3d11_shared_mapping_unavailable:")
    assert cap.zero_copy_proven is False


def test_windows_env_flag_alone_never_claims_zero_copy() -> None:
    cap = detect_topos_surface_capability(
        system="Windows", env={"TOPOS_D3D11_SHARED_MAPPED": "1"}
    )
    assert cap.backend is ToposSurfaceBackend.WINDOWS_D3D11_STAGING
    assert cap.zero_copy is False
    assert cap.copy_bytes is True
    assert cap.reason.startswith("d3d11_shared_env_flag_unproven:")


def test_windows_zero_copy_requires_positive_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cap_module, "_probe_windows_d3d11_shared", lambda: (True, "")
    )
    cap = detect_topos_surface_capability(system="Windows", env={})
    assert cap.backend is ToposSurfaceBackend.WINDOWS_D3D11_SHARED
    assert cap.zero_copy is True
    assert cap.copy_bytes is False
    assert cap.evidence == "probe"
    assert cap.zero_copy_proven is True


def test_windows_probe_available_without_interop_method_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``available`` flag is not proof; interop method must be named."""
    monkeypatch.setattr(
        cap_module, "_probe_windows_d3d11_shared", lambda: (False, "interop_not_yet_implemented")
    )
    cap = detect_topos_surface_capability(
        system="Windows", env={"TOPOS_D3D11_SHARED_MAPPED": "1"}
    )
    assert cap.zero_copy is False
    assert "interop_not_yet_implemented" in cap.reason


def test_linux_shared_mapping_requires_positive_probe() -> None:
    fallback = detect_topos_surface_capability(system="Linux", env={})
    assert fallback.backend is ToposSurfaceBackend.LINUX_STAGING
    assert fallback.zero_copy is False

    env_only = detect_topos_surface_capability(
        system="Linux", env={"TOPOS_DMABUF_MAPPED": "1"}
    )
    assert env_only.backend is ToposSurfaceBackend.LINUX_STAGING
    assert env_only.zero_copy is False
    assert env_only.reason.startswith("dmabuf_env_flag_unproven:")


def test_linux_zero_copy_requires_positive_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cap_module, "_probe_linux_dmabuf", lambda: (True, ""))
    cap = detect_topos_surface_capability(system="Linux", env={})
    assert cap.backend is ToposSurfaceBackend.LINUX_DMABUF
    assert cap.zero_copy is True
    assert cap.copy_bytes is False
    assert cap.evidence == "probe"


def test_pbo_is_never_reported_as_zero_copy() -> None:
    cap = detect_topos_surface_capability(requested="pbo", system="Darwin", env={})
    assert cap.backend is ToposSurfaceBackend.OPENGL_PBO
    assert cap.zero_copy is False
    assert cap.reason == "pbo_staging_copy"


def test_cpu_override_is_truthful_on_any_platform() -> None:
    cap = detect_topos_surface_capability(
        requested="cpu", system="Windows", env={"TOPOS_D3D11_SHARED_MAPPED": "1"}
    )
    assert cap.backend is ToposSurfaceBackend.CPU
    assert cap.zero_copy is False
    assert cap.reason == "forced_cpu_upload"
