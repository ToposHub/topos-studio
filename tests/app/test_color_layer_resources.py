"""Image resources mutate real Layer/history without graph or MediaPool UI."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QPushButton, QWidget

from src.app.controllers.color_layer_resource_controller import ColorLayerResourceController
from src.app.controllers.layer_controller import LayerOpsMixin
from src.features.color.core.history import HistoryManager
from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.layer import Layer
from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
from src.features.color.ui.layer_panel import LayerPanel
from src.features.color.ui.lut_browser_panel import ColorLutBrowser
from src.shared.color.lut_resource_registry import LutResourceSession


SCHEMA = [{"var_name": "amount", "type": "slider", "default": 0.2, "min": 0.0, "max": 1.0}]


class Catalog(QObject):
    catalog_changed = pyqtSignal()

    def entries(self) -> list[dict]:
        return [
            {"id": "grain", "name": "Grain", "kind": "effect", "source": "dctl", "available": True, "parameters": SCHEMA},
            {"id": "effect:blur", "name": "Graph blur", "kind": "effect", "source": "builtin", "available": True},
            {"id": "offline-ai", "kind": "effect", "source": "ai", "available": True},
            {"id": "missing", "kind": "effect", "source": "dctl", "available": True},
        ]


class Engine:
    apply_lut = ImageEngine.apply_lut
    reset_lut = ImageEngine.reset_lut
    set_lut_intensity = ImageEngine.set_lut_intensity
    remove_plugin = ImageEngine.remove_plugin

    def __init__(self) -> None:
        self.layers = [Layer("First layer"), Layer("Second layer")]
        self.active_layer_index = 0
        self.img_full_original = np.zeros((8, 8, 3), dtype=np.uint8)
        self.img_low = np.zeros((8, 8, 3), dtype=np.float32)
        self._source_epoch = 1
        self.renderer = None
        self.history = HistoryManager()
        self.ai_plugin_result_cache = {}
        self.render_pipeline = Mock()

    def get_active_layer(self) -> Layer | None:
        return self.layers[self.active_layer_index] if self.active_layer_index >= 0 else None


@pytest.fixture
def rig(qtbot, tmp_path, monkeypatch):
    metadata = {"grain": {"type": "dctl", "shader_code": "vec3 process(vec3 c) { return c; }", "params": SCHEMA},
                "offline-ai": {"type": "ai", "params": []}}
    manager = SimpleNamespace(get_plugin=lambda name: metadata.get(name))
    monkeypatch.setattr("src.features.color.core.plugin_manager.get_plugin_manager", lambda: manager)
    session = LutResourceSession(str(tmp_path / "lut-stage"))
    monkeypatch.setattr("src.shared.color.lut_resource_registry.get_lut_resource_session", lambda: session)
    host = QWidget()
    qtbot.addWidget(host)
    host.engine = Engine()
    host.current_file_path = str(tmp_path / "image.png")
    host._current_edit_project = SimpleNamespace(project_id="image-project")
    host.edit_project_dirty = False
    host.layer_panel = LayerPanel(host)
    host.color_effects_browser = ColorEffectsBrowser(host)
    host.color_lut_browser = ColorLutBrowser(host)
    host.request_render = Mock()
    host._trigger_autosave = Mock()
    host._push_layer_history = lambda: LayerOpsMixin._push_layer_history(host)
    catalog = Catalog()
    adapter = ColorLayerResourceController(host, catalog_provider=catalog)
    rejections = []
    adapter.resource_rejected.connect(rejections.append)
    yield SimpleNamespace(host=host, engine=host.engine, adapter=adapter, session=session,
                          catalog=catalog, rejections=rejections, root=tmp_path)
    adapter.shutdown()


def _lut(rig) -> str:
    path = rig.root / "identity.cube"
    path.write_text("LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n")
    result = rig.session.import_files([str(path)])[0]
    assert result.entry is not None
    rig.adapter.refresh_catalog()
    return result.entry.uri


def _add(rig) -> dict:
    rig.adapter.activate_effect({"id": "grain", "kind": "effect"})
    return rig.engine.layers[0].plugins[-1]


def test_layer_header_offers_reset_all(rig, qtbot):
    panel = rig.host.layer_panel
    header = panel.findChild(QWidget, "colorLayersHeader")
    button = header.findChild(QPushButton, "colorLayersResetAllButton")
    assert button.toolTip() == button.accessibleName()
    assert not button.icon().isNull()
    with qtbot.waitSignal(panel.reset_all_requested, timeout=500):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

def test_real_layer_effect_add_preserves_source_and_undo(rig):
    assert not hasattr(rig.host, "media_pool_panel") and not hasattr(rig.host, "color_graph_host")
    first, second = rig.engine.layers
    plugin = _add(rig)
    assert plugin["params"] == {"amount": 0.2}
    assert not second.plugins
    assert rig.host.edit_project_dirty
    assert len(rig.engine.history.undo_stack) == 1
    previous = rig.engine.history.undo_stack[-1]["layers"]
    assert previous[0]["plugins"] == []
    assert rig.host._trigger_autosave.call_count == 1


def test_gesture_commits_once_and_cancel_changes_nothing(rig):
    plugin = _add(rig)
    before = len(rig.engine.history.undo_stack)
    args = {"instance_id": plugin["instance_id"], "name": "amount"}
    rig.adapter.edit("set_parameter", dict(args, value=0.2, phase="start"))
    for value in (0.3, 0.4, 0.7):
        rig.adapter.edit("set_parameter", dict(args, value=value, phase="change"))
    assert plugin["params"]["amount"] == 0.2
    assert len(rig.engine.history.undo_stack) == before
    rig.adapter.edit("set_parameter", dict(args, value=0.7, phase="commit"))
    assert plugin["params"]["amount"] == 0.7
    assert len(rig.engine.history.undo_stack) == before + 1
    rig.adapter.edit("set_parameter", dict(args, value=0.7, phase="start"))
    rig.adapter.edit("set_parameter", dict(args, value=0.9, phase="change"))
    rig.adapter.edit("set_parameter", dict(args, phase="cancel"))
    assert plugin["params"]["amount"] == 0.7
    assert len(rig.engine.history.undo_stack) == before + 1


def test_layer_switch_rejects_old_gesture_and_async_token(rig):
    plugin = _add(rig)
    token = rig.adapter.issue_async_token(plugin["instance_id"])
    rig.adapter.edit("set_parameter", {"instance_id": plugin["instance_id"], "name": "amount", "value": 0.2, "phase": "start"})
    rig.engine.active_layer_index = 1
    rig.adapter.refresh_target()
    assert not rig.adapter.verify_async_token(token)
    rig.adapter.edit("set_parameter", {"instance_id": plugin["instance_id"], "name": "amount", "value": 0.9, "phase": "commit"})
    assert plugin["params"]["amount"] == 0.2
    assert not rig.engine.layers[1].plugins
    assert rig.rejections[-1]["code"] == "instance_unknown"


@pytest.mark.parametrize("value", [2.0, -1.0, float("nan"), True, "bad"])
def test_invalid_parameter_never_enters_history(rig, value):
    plugin = _add(rig)
    before = len(rig.engine.history.undo_stack)
    rig.adapter.edit("set_parameter", {"instance_id": plugin["instance_id"], "name": "amount", "value": value})
    assert len(rig.engine.history.undo_stack) == before
    assert plugin["params"] == {"amount": 0.2}
    assert rig.rejections


@pytest.mark.parametrize("resource", ["effect:blur", "offline-ai", "missing", "unknown"])
def test_unwired_or_unknown_backend_is_not_added(rig, resource):
    rig.adapter.activate_effect({"id": resource, "available": True})
    assert not rig.engine.layers[0].plugins
    assert not rig.engine.history.undo_stack
    assert rig.rejections[-1]["code"] == "resource_unavailable"


def test_registered_lut_apply_strength_remove_and_serialization(rig):
    from src.shared.color.lut_loader import resolve_lut_path
    uri = _lut(rig)
    rig.adapter.activate_resource({"kind": "lut", "id": uri})
    layer = rig.engine.layers[0]
    assert layer.active_lut_path == uri
    assert Path(resolve_lut_path(uri)).is_file()
    assert Layer.from_dict(layer.to_dict(None)).active_lut_path == uri
    rig.adapter.edit("set_lut_strength", {"strength": 0.5})
    assert layer.lut_intensity == 0.5
    rig.adapter.edit("remove_lut", {})
    assert layer.active_lut_path is None
    assert len(rig.engine.history.undo_stack) == 3
    resources, files = rig.session.save_payload()
    assert resources and files


def test_lut_noop_and_unknown_uri_do_not_create_history(rig):
    uri = _lut(rig)
    rig.adapter.activate_resource({"kind": "lut", "id": uri})
    count = len(rig.engine.history.undo_stack)
    rig.adapter.activate_resource({"kind": "lut", "id": uri})
    rig.adapter.activate_resource({"kind": "lut", "id": "topos-res://missing"})
    assert len(rig.engine.history.undo_stack) == count
    assert rig.rejections[-1]["code"] == "resource_unavailable"


def test_enable_reset_remove_use_layer_and_engine_services(rig):
    plugin = _add(rig)
    args = {"instance_id": plugin["instance_id"]}
    rig.adapter.edit("set_enabled", dict(args, enabled=False))
    assert not plugin["enabled"]
    rig.adapter.edit("set_parameter", dict(args, name="amount", value=0.8))
    rig.adapter.edit("reset_instance", args)
    assert plugin["params"] == {"amount": 0.2}
    rig.engine.ai_plugin_result_cache[(rig.engine.layers[0].id, plugin["instance_id"])] = "old"
    rig.adapter.edit("remove_instance", args)
    assert not rig.engine.layers[0].plugins
    assert not rig.engine.ai_plugin_result_cache


def test_source_epoch_change_invalidates_async_result(rig):
    plugin = _add(rig)
    token = rig.adapter.issue_async_token(plugin["instance_id"])
    assert rig.adapter.verify_async_token(token)
    rig.engine._source_epoch += 1
    assert not rig.adapter.verify_async_token(token)


def test_baked_layer_disables_resource_editing(rig):
    layer = rig.engine.layers[0]
    layer.is_baked = True
    rig.adapter.refresh_target()
    rig.adapter.activate_effect({"id": "grain"})
    assert not layer.plugins
    assert rig.rejections[-1]["code"] == "layer_baked"


def test_import_is_catalog_only_and_never_applies_to_changed_layer(rig, qtbot):
    import threading
    path = rig.root / "import.cube"
    path.write_text("fixture")
    entered, release = threading.Event(), threading.Event()
    def import_files(paths, *, cancel_check):
        entered.set()
        release.wait(2)
        assert cancel_check()
        return []
    rig.session.import_files = import_files
    messages = []
    rig.adapter.import_feedback.connect(messages.append)
    rig.adapter.import_luts([str(path)])
    qtbot.waitUntil(entered.is_set)
    rig.engine.active_layer_index = 1
    rig.adapter.refresh_target()
    release.set()
    qtbot.waitUntil(lambda: any(message.get("status") == "finished" for message in messages))
    assert all(layer.active_lut_path is None for layer in rig.engine.layers)
    assert not rig.engine.history.undo_stack


def test_finish_transaction_commits_one_buffered_change(rig):
    plugin = _add(rig)
    args = {"instance_id": plugin["instance_id"], "name": "amount"}
    rig.adapter.edit("set_parameter", dict(args, value=0.2, phase="start"))
    rig.adapter.edit("set_parameter", dict(args, value=0.6, phase="change"))
    rig.adapter.finish_transaction()
    assert plugin["params"]["amount"] == 0.6
    assert len(rig.engine.history.undo_stack) == 2


def test_changed_plugin_schema_cannot_use_old_catalog_defaults(rig):
    metadata = rig.adapter._plugin_manager().get_plugin("grain")
    metadata["params"] = [{"var_name": "new_amount", "type": "slider", "default": 0.5}]
    rig.adapter.activate_effect({"id": "grain"})
    assert not rig.engine.layers[0].plugins
    assert not rig.engine.history.undo_stack
    assert rig.rejections[-1]["code"] == "resource_unavailable"


def test_shutdown_cancels_pending_gesture_without_mutation(rig):
    plugin = _add(rig)
    args = {"instance_id": plugin["instance_id"], "name": "amount"}
    rig.adapter.edit("set_parameter", dict(args, value=0.2, phase="start"))
    rig.adapter.edit("set_parameter", dict(args, value=0.9, phase="change"))
    rig.adapter.shutdown()
    assert plugin["params"]["amount"] == 0.2
    assert len(rig.engine.history.undo_stack) == 1


def test_late_slider_release_cannot_overwrite_restored_layer(rig):
    plugin = _add(rig)
    args = {"instance_id": plugin["instance_id"], "name": "amount"}
    rig.adapter.edit("set_parameter", dict(args, value=0.2, phase="start"))
    restored = Layer.from_dict(rig.engine.layers[0].to_dict(None))
    rig.engine.layers[0] = restored
    rig.adapter.refresh_target()
    rig.adapter.edit("set_parameter", dict(args, value=0.9, phase="commit"))
    assert restored.plugins[0]["params"]["amount"] == 0.2
    assert len(rig.engine.history.undo_stack) == 1
    assert rig.rejections[-1]["code"] == "gesture_target_changed"


def test_missing_registered_lut_is_rejected_before_history(rig):
    uri = _lut(rig)
    path = rig.session.staged_path(uri)
    assert path is not None
    Path(path).unlink()
    assert rig.session.status_of(uri) == "registered"
    rig.adapter.activate_resource({"kind": "lut", "id": uri})
    assert rig.engine.layers[0].active_lut_path is None
    assert not rig.engine.history.undo_stack
    assert rig.rejections[-1]["code"] == "resource_unavailable"
    assert rig.host._trigger_autosave.call_count == 0


def test_failed_lut_import_reports_failure_details(rig, qtbot):
    bad = rig.root / "bad.cube"
    bad.write_text("This is not a LUT")
    events = []
    rig.adapter.import_feedback.connect(events.append)
    rig.adapter.import_luts([str(bad)])
    qtbot.waitUntil(lambda: any(event.get("status") == "failed" for event in events))
    qtbot.waitUntil(lambda: bool(rig.rejections))
    assert rig.rejections[-1]["action"] == "import_luts"
    assert rig.rejections[-1]["code"] == "lut_import_failed"
    assert "bad.cube" in rig.host.color_lut_browser.status_label.text()
    assert "failed 1" in rig.host.color_lut_browser.status_label.text()
    assert not rig.engine.history.undo_stack


def test_partial_lut_import_retains_success_and_displays_failures(rig, qtbot):
    _lut(rig)
    good = rig.root / "identity.cube"
    bad = rig.root / "bad.cube"
    bad.write_text("bad LUT")
    events = []
    rig.adapter.import_feedback.connect(events.append)
    rig.adapter.import_luts([str(good), str(bad)])
    qtbot.waitUntil(lambda: any(event.get("status") == "finished" for event in events))
    assert len(rig.session.lut_entries()) == 1
    assert rig.engine.layers[0].active_lut_path is None
    assert "duplicate 1" in rig.host.color_lut_browser.status_label.text()
    assert "failed 1" in rig.host.color_lut_browser.status_label.text()
    assert rig.rejections[-1]["code"] == "lut_import_failed"
