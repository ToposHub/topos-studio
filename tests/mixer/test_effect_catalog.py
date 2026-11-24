"""UI catalog, parameter-window ownership, and rack interaction regressions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtWidgets import QApplication, QComboBox, QScrollArea, QScrollBar, QWidget

from src.features.mixer.effects import catalog as mixer_catalog
from src.features.mixer.effects import (
    EFFECT_CATALOG, EffectBrowserWidget, EffectInstance, EffectRackWidget,
    EffectWindowManager, create_panel, get_effect, stop_panel_timers,
)
from src.shared.ui.style_sheet import COMBO_DROPDOWN_VIEW_STYLE
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


@pytest.mark.parametrize("descriptor", EFFECT_CATALOG, ids=lambda entry: entry.effect_id)
def test_every_registered_panel_constructs_without_backend(qapp: QApplication, descriptor: Any) -> None:
    assert descriptor.backend_status == "not_connected"
    assert descriptor.supports_preview is False
    assert descriptor.supports_export is False
    assert descriptor.parameter_schema["type"] == "object"
    panel = create_panel(descriptor.effect_id, "测试轨道")
    assert isinstance(panel, QWidget)
    for combo in panel.findChildren(QComboBox):
        assert isinstance(combo, ChevronComboBox)
        assert combo.view().styleSheet() == COMBO_DROPDOWN_VIEW_STYLE
    stop_panel_timers(panel)
    assert all(not timer.isActive() for timer in panel.findChildren(QTimer))
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_catalog_has_unique_stable_ids_and_clip_tool_scope() -> None:
    assert len(EFFECT_CATALOG) == 24
    assert len({entry.effect_id for entry in EFFECT_CATALOG}) == len(EFFECT_CATALOG)
    assert get_effect("clip_editor").scope == ("clip",)
    with pytest.raises(KeyError):
        create_panel("../unsafe_module")


def test_catalog_manifest_loads_without_utf8_locale_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """清单含中文：不得依赖平台默认编码（Windows 为区域代码页，如 GBK），否则启动即崩溃。"""
    original_read_text = Path.read_text

    def read_text_as_on_cp936_windows(self: Path, *args: Any, **kwargs: Any) -> str:
        # 未指定 encoding 时，非 UTF-8 模式下 io.text_encoding(None) 解析为 ANSI 代码页
        encoding = kwargs.get("encoding", args[0] if args else None)
        if encoding is None:
            if args:
                args = ("gbk", *args[1:])
            else:
                kwargs["encoding"] = "gbk"
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_as_on_cp936_windows)
    entries = mixer_catalog._load_catalog()
    assert [entry.name_zh for entry in entries] == [entry.name_zh for entry in EFFECT_CATALOG]


@pytest.mark.parametrize("effect_id", ["declick_dehum", "pitch_correction", "exciter", "subharmonic", "doubler"])
def test_design_only_effect_window_fits_whole_panel(qapp: QApplication, effect_id: str) -> None:
    parent = QWidget()
    manager = EffectWindowManager(parent)
    window = manager.open(EffectInstance.create(effect_id, "owner"), "Speech")
    try:
        window.resize(1, 1)
        assert not window.findChildren(QScrollArea)
        assert all(not bar.isVisible() for bar in window.findChildren(QScrollBar))
        assert window.rect().contains(window.panel.geometry())
        assert window.panel.width() >= window.panel.layout().minimumSize().width()
        assert window.panel.height() >= window.panel.layout().minimumSize().height()
    finally:
        manager.dispose_all()
        parent.deleteLater()


def test_window_close_and_reopen_preserve_parameters_and_pause_timers(qapp: QApplication) -> None:
    parent = QWidget()
    manager = EffectWindowManager(parent)
    instance = EffectInstance.create("eq", "dx-1")
    window = manager.open(instance, "对白")
    state = window.panel.get_state()
    state["master_gain"] = -7.25
    window.panel.set_state(state)
    window.close()
    assert instance.parameters["master_gain"] == -7.25
    assert all(not timer.isActive() for timer in window.panel.findChildren(QTimer))
    reopened = manager.open(instance, "对白")
    assert reopened is window
    assert reopened.panel.get_state()["master_gain"] == -7.25
    manager.pause_all()
    assert not window.isVisible()
    manager.dispose_all()
    assert manager.window_for(instance) is None
    parent.deleteLater()


def test_same_effect_and_instance_name_are_isolated_by_owner(qapp: QApplication) -> None:
    parent = QWidget()
    manager = EffectWindowManager(parent)
    first = EffectInstance("same-local-id", "eq", "dx-1")
    second = EffectInstance("same-local-id", "eq", "mx-1")
    first_window = manager.open(first, "对白")
    second_window = manager.open(second, "音乐")
    assert first_window is not second_window
    state = first_window.panel.get_state()
    state["master_gain"] = -9.0
    first_window.panel.set_state(state)
    assert second_window.panel.get_state()["master_gain"] != -9.0
    manager.close_instance(first.instance_id, first.owner_id)
    assert manager.window_for(first) is None
    assert manager.window_for(second) is second_window
    manager.dispose_all()
    parent.deleteLater()


def test_rack_insert_bypass_reorder_remove_and_owner_switch(qapp: QApplication) -> None:
    parent = QWidget()
    manager = EffectWindowManager(parent)
    rack = EffectRackWidget(manager, parent)
    notifications: list[tuple[str, list[EffectInstance]]] = []
    rack.changed.connect(lambda owner, instances: notifications.append((owner, instances)))
    rack.set_owner("dx-1", "对白", [])
    first = rack.insert_effect("eq")
    second = rack.insert_effect("deesser")
    rack.toggle_selected()
    assert not second.enabled
    rack.move_selected(-1)
    assert [item.instance_id for item in rack.instances()] == [second.instance_id, first.instance_id]
    rack.open_selected()
    assert manager.window_for(second) is not None
    rack.remove_selected()
    assert manager.window_for(second) is None
    assert rack.instances() == [first]
    assert notifications[-1][0] == "dx-1"
    rack.set_owner("mx-1", "音乐", [])
    assert not rack.instances()
    assert first.owner_id == "dx-1"
    with pytest.raises(ValueError):
        rack.insert_effect("clip_editor")
    manager.dispose_all()
    parent.deleteLater()


def test_browser_filters_and_emits_catalog_identity(qapp: QApplication) -> None:
    browser = EffectBrowserWidget()
    browser.set_scope("track")
    assert browser.list_widget.count() == 23
    browser.search.setText("de-esser")
    assert browser.list_widget.count() == 1
    selected: list[str] = []
    browser.effectChosen.connect(selected.append)
    browser.add_button.click()
    assert selected == ["deesser"]
    browser.search.clear()
    browser.set_scope("clip")
    assert browser.list_widget.count() == 24
    browser.deleteLater()


def test_rack_bypass_and_panel_power_remain_consistent(qapp: QApplication) -> None:
    parent = QWidget()
    manager = EffectWindowManager(parent)
    rack = EffectRackWidget(manager, parent)
    rack.set_owner("dx-1", "对白", [])
    instance = rack.insert_effect("eq")
    rack.toggle_selected()
    window = manager.open(instance, "对白")
    assert window.panel.get_state()["enabled"] is False
    rack.toggle_selected()
    assert window.panel.get_state()["enabled"] is True
    state = window.panel.get_state()
    state["enabled"] = False
    window.panel.stateChanged.emit(state)
    assert instance.enabled is False
    assert rack.list_widget.currentItem().text().startswith("○")
    manager.dispose_all()
    parent.deleteLater()
