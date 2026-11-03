"""R01: Effects 资源目录权威投影（EffectsResourceCatalog）。

验收（计划 R01）：
- 权威 = PluginManager 注册表 + EffectCatalog（内存读，投影不扫描磁盘）；
- 身份：插件 id = plugin_id 跨会话稳定；同名不同内容确定性消歧
  ``id@hash8``；同内容多路径去重；分类/名称翻译不改 ID；
- 能力状态：AI 模型缺失 / DCTL 缺 shader / 内建未接 Color 后端 →
  available=False + disabled_reason 码，缺失条目不能发真实添加命令；
- 热刷新：更新/删除插件后投影同步，未变条目身份不漂移；
- 未知参数类型原样保留；搜索/刷新不触发目录扫描。
"""
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_resource_catalog import EffectsResourceCatalog
from src.app.controllers.color_resource_preview import ColorResourcePreview
from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
from src.features.color.ui.lut_browser_panel import ColorLutBrowser
from src.features.color.ui.node_editor_panel import ColorNodePanel


class FakePluginManager:
    """内存注册表替身：只提供 get_all_plugins，绝不触碰磁盘。"""

    def __init__(self, plugins: dict[str, dict[str, Any]] | None = None) -> None:
        self.plugins = dict(plugins or {})

    def get_all_plugins(self) -> dict[str, dict[str, Any]]:
        return dict(self.plugins)


class FakeEffectCatalog:
    def __init__(self, specs: list[Any] | None = None, *, built: bool = True) -> None:
        self._specs = list(specs or [])
        self._built = built

    def is_built(self) -> bool:
        return self._built

    def get_all_specs(self) -> list[Any]:
        self._built = True
        return list(self._specs)


class FakeSpec:
    """最小 EffectSpec 面（effect_type/name/category/param_defs/哈希）。"""

    def __init__(self, effect_type: str, name: str = "", category: str = "",
                 param_defs: tuple = ()) -> None:
        self.effect_type = effect_type
        self.name = name or effect_type
        self.category = category or "filter"
        self.param_defs = param_defs
        self.description = f"builtin {effect_type}"
        self.plugin_content_hash = "b" * 64
        self.plugin_parameter_schema_hash = "s" * 64


def _dctl(plugin_id: str, name: str = "", *, shader: str = "//code",
          params: list | None = None, content_hash: str = "") -> dict[str, Any]:
    return {"plugin_id": plugin_id, "name": name or plugin_id, "author": "Topos",
            "type": "dctl", "params": params or [], "shader_code": shader,
            "version": "1.0", "content_hash": content_hash or ("h" + plugin_id),
            "source_uri": f"/plugins/{plugin_id}.tglsl",
            "capabilities": {"graphics_backends": "opengl"}, "description": ""}


def _ai(plugin_id: str, model: str) -> dict[str, Any]:
    return {"plugin_id": plugin_id, "name": plugin_id, "author": "Topos",
            "type": "ai", "category": "upscale", "model_path": model,
            "params": [], "shader_code": None, "version": "",
            "content_hash": "ai-" + plugin_id, "source_uri": f"/plugins/{plugin_id}.tai"}


def _catalog(pm: FakePluginManager | None = None, ec: FakeEffectCatalog | None = None,
             probe: Any = None) -> EffectsResourceCatalog:
    return EffectsResourceCatalog(
        plugin_manager=pm or FakePluginManager(),
        effect_catalog=ec if ec is not None else FakeEffectCatalog(built=False),
        ai_model_probe=probe or (lambda path: True))


# ═══ 身份与去重 ═══════════════════════════════════════════════════════

def test_duplicate_display_names_keep_distinct_ids() -> None:
    pm = FakePluginManager({"grain_a": _dctl("grain_a", "Film Grain"),
                            "grain_b": _dctl("grain_b", "Film Grain")})
    entries = _catalog(pm).entries()
    names = [e for e in entries if e["name"] == "Film Grain"]
    assert len(names) == 2
    assert len({e["id"] for e in names}) == 2


def test_same_id_different_content_is_disambiguated() -> None:
    # 两个来源共用 plugin_id 但内容不同（多目录/多注册表场景）：
    # dict 键不同、载荷 plugin_id 相同 → 确定性消歧为 id@hash8。
    pm = FakePluginManager({
        "k1": dict(_dctl("dup"), content_hash="aaaa1111", source_uri="/a/dup.tglsl"),
        "k2": dict(_dctl("dup"), content_hash="bbbb2222", source_uri="/b/dup.tglsl"),
    })
    entries = _catalog(pm).entries()
    same = [e for e in entries if e["id"].startswith("dup")]
    assert {e["id"] for e in same} == {"dup@aaaa1111", "dup@bbbb2222"}


def test_same_content_multiple_paths_deduplicated() -> None:
    pm = FakePluginManager({
        "k1": dict(_dctl("grain"), source_uri="/a/grain.tglsl"),
        "k2": dict(_dctl("grain"), source_uri="/b/grain.tglsl"),
    })
    entries = _catalog(pm).entries()
    assert [e["id"] for e in entries if e["id"] == "grain"] == ["grain"]


def test_unicode_names_do_not_leak_into_ids() -> None:
    pm = FakePluginManager({"中文插件": _dctl("中文插件", "电影颗粒 🎞")})
    entry = _catalog(pm).entries()[0]
    assert entry["name"] == "电影颗粒 🎞"
    assert entry["id"] == "中文插件"
    assert entry["schema_hash"]  # 非 ASCII 也能稳定哈希


def test_category_and_name_changes_never_change_ids() -> None:
    pm = FakePluginManager({"grain": _dctl("grain", "Old Name")})
    catalog = _catalog(pm)
    first = {e["id"]: e for e in catalog.entries()}
    pm.plugins["grain"] = _dctl("grain", "New Name")
    pm.plugins["grain"]["category"] = "Renamed Category"
    catalog.refresh()
    second = {e["id"]: e for e in catalog.entries()}
    assert set(first) == set(second) == {"grain"}
    assert second["grain"]["name"] == "New Name"


# ═══ 能力状态 ════════════════════════════════════════════════════════

def test_ai_model_missing_disables_entry_with_reason() -> None:
    pm = FakePluginManager({"up": _ai("up", "models/missing.onnx")})
    probe_results = {"models/missing.onnx": False, "models/ok.onnx": True}
    catalog = _catalog(pm, probe=probe_results.get)
    entries = {e["id"]: e for e in catalog.entries()}
    assert entries["up"]["available"] is False
    assert entries["up"]["disabled_reason"] == "ai_model_missing"
    assert entries["up"]["bake_capable"] is True
    assert entries["up"]["realtime"] is False
    pm.plugins["up"] = _ai("up", "models/ok.onnx")
    catalog.refresh()
    entries = {e["id"]: e for e in catalog.entries()}
    assert entries["up"]["available"] is True
    assert entries["up"]["disabled_reason"] == ""


def test_dctl_without_shader_is_disabled() -> None:
    pm = FakePluginManager({"broken": _dctl("broken", shader="")})
    entry = {e["id"]: e for e in _catalog(pm).entries()}["broken"]
    assert entry["available"] is False
    assert entry["disabled_reason"] == "plugin_shader_missing"


def test_builtins_projected_only_when_catalog_built_and_disabled() -> None:
    pm = FakePluginManager()
    cold = _catalog(pm, FakeEffectCatalog(built=False))
    assert not [e for e in cold.entries() if e["source"] == "builtin"]
    warm = _catalog(pm, FakeEffectCatalog([FakeSpec("blur")], built=True))
    builtin = {e["id"]: e for e in warm.entries()}["effect:blur"]
    assert builtin["available"] is False
    assert builtin["disabled_reason"] == "color_backend_not_wired"
    # DCTL 插件 spec 不从 builtin 路径重复投影
    dup = FakeEffectCatalog([FakeSpec("plugin:grain")], built=True)
    ids = {e["id"] for e in _catalog(FakePluginManager({"grain": _dctl("grain")}), dup).entries()}
    assert "grain" in ids and "effect:plugin:grain" not in ids


# ═══ 热刷新与身份稳定 ════════════════════════════════════════════════

def test_hot_refresh_update_delete_and_identity_stability() -> None:
    pm = FakePluginManager({
        "grain": _dctl("grain", params=[{"var_name": "amount", "type": "slider",
                                         "default": 0.2, "min": 0.0, "max": 1.0}]),
    })
    catalog = _catalog(pm)
    first = {e["id"]: e for e in catalog.entries()}["grain"]
    # 未变化的热刷新：身份（id + schema_hash）不漂移
    catalog.refresh()
    second = {e["id"]: e for e in catalog.entries()}["grain"]
    assert (second["id"], second["schema_hash"]) == (first["id"], first["schema_hash"])
    # 内容更新：id 稳定，content_hash 变化（收藏身份不丢，缓存可失效）
    pm.plugins["grain"]["content_hash"] = "updated-hash"
    catalog.refresh()
    third = {e["id"]: e for e in catalog.entries()}["grain"]
    assert third["id"] == "grain"
    assert third["content_hash"] == "updated-hash"
    # 删除：条目消失
    del pm.plugins["grain"]
    catalog.refresh()
    assert "grain" not in {e["id"] for e in catalog.entries()}


def test_unknown_param_type_is_preserved_verbatim() -> None:
    params = [{"var_name": "magic", "type": "future_widget", "default": "raw"}]
    pm = FakePluginManager({"exotic": _dctl("exotic", params=params)})
    entry = {e["id"]: e for e in _catalog(pm).entries()}["exotic"]
    assert entry["parameters"] == params


# ═══ 不扫描契约 ═════════════════════════════════════════════════════

def test_refresh_and_entries_never_touch_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("catalog projection must not scan directories")

    monkeypatch.setattr("os.listdir", boom)
    monkeypatch.setattr("os.scandir", boom)
    catalog = _catalog(FakePluginManager({"grain": _dctl("grain")}),
                       probe=lambda _p: True)
    catalog.refresh()
    assert catalog.entries()


def test_cached_entries_do_not_reproject() -> None:
    calls: list[int] = []
    catalog = _catalog(FakePluginManager({"grain": _dctl("grain")}))
    original = catalog._project

    def counting() -> list[dict[str, Any]]:
        calls.append(1)
        return original()

    catalog._project = counting  # type: ignore[method-assign]
    catalog.entries()
    catalog.entries()
    catalog.entries()
    assert len(calls) == 1  # 冷投影一次，后续命中缓存


def test_browser_search_and_refresh_do_not_scan(monkeypatch: pytest.MonkeyPatch,
                                                qapp: Any) -> None:
    from src.features.color.core import plugin_manager as pm_module

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("search must not trigger plugin scans")

    monkeypatch.setattr(pm_module.PluginManager, "scan_plugins", boom)
    monkeypatch.setattr(pm_module.PluginManager, "reload_plugins", boom)
    browser = ColorEffectsBrowser()
    entries = _catalog(FakePluginManager({
        "grain": _dctl("grain", "Film Grain"),
        "halation": _dctl("halation", "Halation")})).entries()
    browser.set_resources(entries)
    browser.search_edit.setText("film")
    browser.set_resources(entries)
    browser.search_edit.setText("hal")
    assert browser.resource_list.count() >= 1


# ═══ 缺失条目不能发真实添加命令 ═════════════════════════════════════

def test_unavailable_entry_cannot_be_added_to_draft(qtbot: Any) -> None:
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QWidget

    host = QWidget()
    qtbot.addWidget(host)
    host.color_node_panel = ColorNodePanel(host)
    host.color_effects_browser = ColorEffectsBrowser(host)
    host.color_lut_browser = ColorLutBrowser(host)
    host.color_graph_host = SimpleNamespace(
        _coordinator=SimpleNamespace(session_id="s", generation=0, scope_owner_id="o"),
        _bound_stack_id="stack")
    resources = [{'id': 'missing_ai', 'kind': 'effect', 'name': 'Missing AI',
                  'category': 'AI', 'available': False,
                  'disabled_reason': 'ai_model_missing', 'parameters': []}]
    presenter = ColorResourcePreview(host, resources=resources)
    presenter.activate(resources[0])
    assert presenter._current_draft()["instances"] == []
    intents: list[Any] = []
    presenter.resource_intent.connect(lambda payload: intents.append(payload))
    presenter.activate(resources[0])
    assert intents == []
