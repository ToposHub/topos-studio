"""Persisted graphics backend preference selects the timeline export path."""

from __future__ import annotations

from types import SimpleNamespace

from src.features.edit.core.timeline_export import TimelineExporter


class _ConfigStub:
    def __init__(self, backend: str):
        self._backend = backend

    def get_backend_selection(self):
        return SimpleNamespace(graphics=SimpleNamespace(value=self._backend))


def test_auto_retains_existing_opengl_export_route(monkeypatch):
    monkeypatch.delenv("TOPOS_BACKEND_GRAPHICS", raising=False)
    from src.shared.config import config_manager

    monkeypatch.setattr(config_manager, "ConfigManager", lambda: _ConfigStub("auto"))
    exporter = object.__new__(TimelineExporter)
    assert exporter._select_timeline_graphics_backend() == "opengl"


def test_explicit_graphics_choice_is_used_for_timeline_export(monkeypatch):
    monkeypatch.delenv("TOPOS_BACKEND_GRAPHICS", raising=False)
    from src.shared.config import config_manager

    for value in ("metal", "opengl"):
        monkeypatch.setattr(
            config_manager, "ConfigManager", lambda _value=value: _ConfigStub(_value))
        exporter = object.__new__(TimelineExporter)
        assert exporter._select_timeline_graphics_backend() == value


def test_backend_environment_override_takes_precedence(monkeypatch):
    from src.shared.config import config_manager

    monkeypatch.setattr(config_manager, "ConfigManager", lambda: _ConfigStub("metal"))
    monkeypatch.setenv("TOPOS_BACKEND_GRAPHICS", "opengl")
    exporter = object.__new__(TimelineExporter)
    assert exporter._select_timeline_graphics_backend() == "opengl"


def test_explicit_d3d11_is_selected_only_when_complete_gate_passes(monkeypatch):
    monkeypatch.delenv("TOPOS_BACKEND_GRAPHICS", raising=False)
    from src.shared.config import config_manager
    from src.shared.media.windows_hw import d3d11_export_capability

    monkeypatch.setattr(config_manager, "ConfigManager", lambda: _ConfigStub("d3d11"))
    monkeypatch.setattr(
        d3d11_export_capability,
        "probe_d3d11_export_capability",
        lambda: SimpleNamespace(ready=True, reason=""),
    )
    exporter = object.__new__(TimelineExporter)
    assert exporter._select_timeline_graphics_backend() == "d3d11"


def test_d3d11_compositor_does_not_silently_use_opengl() -> None:
    exporter = object.__new__(TimelineExporter)
    exporter._gpu_render_disabled = False
    exporter._export_has_alpha = False
    exporter._timeline_graphics_backend = "d3d11"
    assert exporter._gpu_render_composite([], (), 16, 16) is None
    assert exporter._timeline_graphics_backend_reason == (
        "not_yet_implemented:d3d11_timeline_composite_integration"
    )
