"""src/shared/preferences 页面贡献注册表专测（P5-T4b）。

layout 域已有 tests/architecture/test_layout_store.py 68 例覆盖（T4a 部分
核实为已有覆盖）；本文件补齐 PreferencesRegistry / PreferencePageDescriptor
的公共 API 正/负路径。
"""

from __future__ import annotations

from src.shared.preferences import (
    PreferencePageDescriptor,
    PreferencesRegistry,
    get_config_manager,
)


class _PageObj:
    """最小 ModularPreferencePage 假件。"""

    def __init__(self):
        self.created_with = None

    def create_widget(self, parent=None):
        self.created_with = parent
        return f"widget-for({parent})"


def _d(page_id, title="T", category="General", order=100, feature_id=None,
       factory=None, page_object=None):
    return PreferencePageDescriptor(
        page_id=page_id, title=title, category=category, order=order,
        feature_id=feature_id, factory=factory, page_object=page_object,
    )


def test_register_and_get_page():
    reg = PreferencesRegistry()
    reg.register(_d("color.processing", title="Processing", category="Color"))
    got = reg.get_page("color.processing")
    assert got is not None and got.title == "Processing"
    assert reg.get_page("missing") is None


def test_duplicate_registration_ignored_first_wins():
    reg = PreferencesRegistry()
    reg.register(_d("a", title="First"))
    reg.register(_d("a", title="Second"))
    assert reg.get_page("a").title == "First"


def test_get_pages_sorted_by_order_category_id():
    reg = PreferencesRegistry()
    reg.register(_d("b2", order=20, category="Z"))
    reg.register(_d("a1", order=10, category="Z"))
    reg.register(_d("a0", order=10, category="A"))
    ids = [p.page_id for p in reg.get_pages()]
    assert ids == ["a0", "a1", "b2"]  # (order, category, page_id)


def test_feature_filtering_keeps_app_level_pages():
    reg = PreferencesRegistry()
    reg.register(_d("app.general", feature_id=None))
    reg.register(_d("edit.timeline", feature_id="edit"))
    reg.register(_d("composite.nodes", feature_id="composite"))

    visible = [p.page_id for p in reg.get_pages(enabled_features={"edit"})]
    assert visible == ["app.general", "edit.timeline"]

    all_ids = [p.page_id for p in reg.get_pages(enabled_features=None)]
    assert len(all_ids) == 3


def test_get_categories_unique_and_sorted():
    reg = PreferencesRegistry()
    reg.register(_d("x1", category="Color"))
    reg.register(_d("x2", category="Color"))
    reg.register(_d("x3", category="General"))
    assert reg.get_categories() == ["Color", "General"]


def test_page_object_builds_lazy_factory():
    obj = _PageObj()
    d = _d("p.obj", page_object=obj)
    assert d.factory is not None
    assert obj.created_with is None  # 惰性：注册时不创建
    w = d.factory("parent-1")
    assert w == "widget-for(parent-1)"
    assert obj.created_with == "parent-1"


def test_explicit_factory_not_overridden_by_page_object():
    sentinel = lambda parent=None: "explicit"  # noqa: E731
    d = _d("p.both", factory=sentinel, page_object=_PageObj())
    assert d.factory(None) == "explicit"


def test_get_config_manager_returns_shared_instance():
    m1 = get_config_manager()
    m2 = get_config_manager()
    assert m1 is m2
