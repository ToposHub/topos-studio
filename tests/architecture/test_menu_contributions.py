"""Test menu action contribution system."""
import json
import os
import pytest
from src.shared.actions import MenuActionDescriptor, ActionRegistry
from src.shared.i18n.i18n import I18N


class TestActionRegistry:
    def test_register_and_get(self):
        registry = ActionRegistry()
        desc = MenuActionDescriptor(
            action_id="edit.project.new",
            menu_path="File/Edit Project",
            label="New Edit Project",
            callback_name="new_edit_project",
            order=200,
            feature_id="edit",
        )
        registry.register(desc)
        assert registry.get_action("edit.project.new") is desc

    def test_duplicate_registration_ignored(self):
        registry = ActionRegistry()
        desc = MenuActionDescriptor(
            action_id="test.action",
            menu_path="File/Test",
            label="Test",
            callback_name="test",
        )
        registry.register(desc)
        registry.register(desc)  # duplicate
        assert len(registry.get_actions()) == 1

    def test_filter_by_enabled_features(self):
        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="edit.project.new",
            menu_path="File/Edit Project",
            label="New Edit Project",
            callback_name="new_edit_project",
            feature_id="edit",
        ))
        registry.register(MenuActionDescriptor(
            action_id="color.reset",
            menu_path="Color",
            label="Reset Grade",
            callback_name="reset_grade",
            feature_id="color",
        ))

        # Only color enabled
        actions = registry.get_actions(enabled_features={"color"})
        assert len(actions) == 1
        assert actions[0].action_id == "color.reset"

        # All enabled
        actions = registry.get_actions(enabled_features={"color", "edit"})
        assert len(actions) == 2

    def test_app_level_actions_always_present(self):
        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="app.preferences",
            menu_path="File",
            label="Preferences",
            callback_name="open_preferences",
            feature_id=None,  # app-level
        ))

        # Even with no features enabled, app-level actions show
        actions = registry.get_actions(enabled_features=set())
        assert len(actions) == 1

    def test_get_menu_paths(self):
        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="a", menu_path="File", label="A", callback_name="a",
        ))
        registry.register(MenuActionDescriptor(
            action_id="b", menu_path="Edit", label="B", callback_name="b",
        ))

        paths = registry.get_menu_paths()
        assert paths == ["Edit", "File"]

    def test_ordering(self):
        registry = ActionRegistry()
        registry.register(MenuActionDescriptor(
            action_id="b", menu_path="File", label="B", callback_name="b", order=2,
        ))
        registry.register(MenuActionDescriptor(
            action_id="a", menu_path="File", label="A", callback_name="a", order=1,
        ))

        actions = registry.get_actions()
        assert actions[0].action_id == "a"
        assert actions[1].action_id == "b"


class TestPreferencesRegistry:
    """Test preference page filtering by enabled features."""

    def test_preferences_filter_by_features(self):
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        registry.register(PreferencePageDescriptor(
            page_id="edit.timeline",
            title="Timeline",
            category="Edit",
            factory=lambda ctx: None,
            feature_id="edit",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="color.processing",
            title="Color & Processing",
            category="Color",
            factory=lambda ctx: None,
            feature_id="color",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="app.general",
            title="General",
            category="General",
            factory=lambda ctx: None,
            feature_id=None,
        ))

        # Color-only mode
        pages = registry.get_pages(enabled_features={"color"})
        assert len(pages) == 2
        page_ids = [p.page_id for p in pages]
        assert "color.processing" in page_ids
        assert "app.general" in page_ids
        assert "edit.timeline" not in page_ids

        # Studio mode
        pages = registry.get_pages(enabled_features={"color", "edit"})
        assert len(pages) == 3

    def test_preferences_categories_filtered(self):
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        registry.register(PreferencePageDescriptor(
            page_id="edit.timeline",
            title="Timeline",
            category="Edit",
            factory=lambda ctx: None,
            feature_id="edit",
        ))
        registry.register(PreferencePageDescriptor(
            page_id="app.general",
            title="General",
            category="General",
            factory=lambda ctx: None,
        ))

        # Edit disabled — should only see General
        cats = registry.get_categories(enabled_features={"color"})
        assert cats == ["General"]

    def test_duplicate_page_registration_ignored(self):
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        desc = PreferencePageDescriptor(
            page_id="test.page",
            title="Test",
            category="Test",
            factory=lambda ctx: None,
        )
        registry.register(desc)
        registry.register(desc)
        assert len(registry.get_pages()) == 1

    def test_preferences_order_uses_order_before_category(self):
        from src.shared.preferences import PreferencePageDescriptor, PreferencesRegistry

        registry = PreferencesRegistry()
        registry.register(PreferencePageDescriptor(
            page_id="app.advanced",
            title="Advanced",
            category="Advanced",
            factory=lambda ctx: None,
            order=800,
        ))
        registry.register(PreferencePageDescriptor(
            page_id="app.general",
            title="General",
            category="General",
            factory=lambda ctx: None,
            order=100,
        ))
        registry.register(PreferencePageDescriptor(
            page_id="color.processing",
            title="Color & Processing",
            category="Color & Processing",
            factory=lambda ctx: None,
            order=300,
            feature_id="color",
        ))

        pages = registry.get_pages(enabled_features={"color"})
        assert [p.page_id for p in pages] == [
            "app.general",
            "color.processing",
            "app.advanced",
        ]


class TestShortcutFeatureFiltering:
    """Test shortcut filtering by enabled features."""

    def test_shortcut_action_has_feature_id(self):
        from src.shared.ui.shortcut_manager import ShortcutAction
        action = ShortcutAction(
            id="edit.timeline.cut",
            label="Cut",
            category="Edit",
            default_shortcut="Ctrl+X",
            feature_id="edit",
        )
        assert action.feature_id == "edit"

    def test_shortcut_action_default_feature_id_is_none(self):
        from src.shared.ui.shortcut_manager import ShortcutAction
        action = ShortcutAction(id="test", label="Test", category="Test")
        assert action.feature_id is None

    def test_filter_by_enabled_features(self):
        from src.shared.ui.shortcut_manager import ShortcutAction
        # Can't instantiate ShortcutManager without Qt app, test filtering logic directly
        actions = [
            ShortcutAction(id="edit.cut", label="Cut", category="Edit", feature_id="edit"),
            ShortcutAction(id="color.reset", label="Reset", category="Color", feature_id="color"),
            ShortcutAction(id="app.save", label="Save", category="File", feature_id=None),
        ]

        enabled = {"color"}
        filtered = [a for a in actions if a.feature_id is None or a.feature_id in enabled]
        assert len(filtered) == 2
        assert filtered[0].id == "color.reset"
        assert filtered[1].id == "app.save"


class TestModuleMenuActionRegistration:
    """Test that modules register the expected menu actions and filtering works."""

    def _make_registry_with_modules(self, available_features=None):
        """Helper: create an ActionRegistry populated by all modules.

        Args:
            available_features: set of feature_ids whose module should be
                marked as available.  If None, all are available.
        """
        from src.shared.actions import ActionRegistry
        from src.features.edit.module import EditModule
        from src.features.composite.module import CompositeModule
        from src.features.deliver.module import DeliverModule
        from src.features.color.module import ColorModule

        registry = ActionRegistry()

        # Instantiate modules and override _available where needed
        modules = []

        edit = EditModule()
        if available_features is not None:
            edit._available = "edit" in available_features
        modules.append(edit)

        composite = CompositeModule()
        if available_features is not None:
            composite._available = "composite" in available_features
        modules.append(composite)

        deliver = DeliverModule()
        if available_features is not None:
            deliver._available = "deliver" in available_features
        modules.append(deliver)

        # Color is always available
        modules.append(ColorModule())

        for mod in modules:
            if hasattr(mod, 'register_menu_actions'):
                mod.register_menu_actions(registry)

        return registry

    def test_edit_actions_not_returned_when_disabled(self):
        """Edit menu actions should be filtered out when edit is disabled."""
        registry = self._make_registry_with_modules(
            available_features={"color", "composite", "deliver"}
        )
        actions = registry.get_actions(enabled_features={"color", "composite", "deliver"})
        action_ids = {a.action_id for a in actions}
        # Edit actions must be absent
        assert not any(aid.startswith("edit.") for aid in action_ids)
        # But other module actions should be present
        assert any(aid.startswith("color.") for aid in action_ids)
        assert any(aid.startswith("composite.") for aid in action_ids)
        assert any(aid.startswith("deliver.") for aid in action_ids)

    def test_composite_actions_not_returned_when_disabled(self):
        """Composite menu actions should be filtered out when composite is disabled."""
        registry = self._make_registry_with_modules(
            available_features={"color", "edit", "deliver"}
        )
        actions = registry.get_actions(enabled_features={"color", "edit", "deliver"})
        action_ids = {a.action_id for a in actions}
        # Composite actions must be absent
        assert not any(aid.startswith("composite.") for aid in action_ids)
        # But edit and deliver should be present
        assert any(aid.startswith("edit.") for aid in action_ids)
        assert any(aid.startswith("deliver.") for aid in action_ids)

    def test_deliver_actions_not_returned_when_disabled(self):
        """Deliver menu actions should be filtered out when deliver is disabled."""
        registry = self._make_registry_with_modules(
            available_features={"color", "edit", "composite"}
        )
        actions = registry.get_actions(enabled_features={"color", "edit", "composite"})
        action_ids = {a.action_id for a in actions}
        # Deliver actions must be absent
        assert not any(aid.startswith("deliver.") for aid in action_ids)
        # But edit and composite should be present
        assert any(aid.startswith("edit.") for aid in action_ids)
        assert any(aid.startswith("composite.") for aid in action_ids)

    def test_no_duplicate_action_ids(self):
        """All action IDs across modules must be unique."""
        registry = self._make_registry_with_modules()  # all available
        all_actions = registry.get_actions()
        ids = [a.action_id for a in all_actions]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {[x for x in ids if ids.count(x) > 1]}"

    def test_edit_timeline_actions_registered(self):
        """Edit module should register timeline cut/copy/paste/ripple_delete actions."""
        registry = self._make_registry_with_modules(
            available_features={"edit", "color"}
        )
        actions = registry.get_actions(enabled_features={"edit", "color"})
        action_ids = {a.action_id for a in actions}
        assert "edit.timeline.cut" in action_ids
        assert "edit.timeline.copy" in action_ids
        assert "edit.timeline.paste" in action_ids
        assert "edit.timeline.ripple_delete" in action_ids

    def test_composite_actions_registered(self):
        """Composite module should register node add, bypass, and viewer render actions."""
        registry = self._make_registry_with_modules(
            available_features={"composite", "color"}
        )
        actions = registry.get_actions(enabled_features={"composite", "color"})
        action_ids = {a.action_id for a in actions}
        assert "composite.add_node" in action_ids
        assert "composite.node.bypass" in action_ids
        assert "composite.viewer.render" in action_ids

    def test_deliver_actions_registered(self):
        """Deliver module should register export and settings actions."""
        registry = self._make_registry_with_modules(
            available_features={"deliver", "color"}
        )
        actions = registry.get_actions(enabled_features={"deliver", "color"})
        action_ids = {a.action_id for a in actions}
        assert "deliver.export" in action_ids
        assert "deliver.export.current_source" in action_ids
        assert "deliver.export.settings" in action_ids

    def test_action_id_format_follows_convention(self):
        """All action IDs must follow the {module}.{category}.{action} pattern."""
        registry = self._make_registry_with_modules()
        all_actions = registry.get_actions()
        for action in all_actions:
            parts = action.action_id.split(".")
            # At minimum: module.action or module.category.action
            assert len(parts) >= 2, f"Invalid action_id format: {action.action_id}"
            # First part should match feature_id
            if action.feature_id:
                assert parts[0] == action.feature_id, (
                    f"action_id prefix mismatch: {action.action_id} vs feature_id={action.feature_id}"
                )

    def test_edit_module_does_not_register_duplicate_project_lifecycle(self):
        """Project lifecycle belongs to the app-level unified File menu."""
        registry = self._make_registry_with_modules(
            available_features={"edit", "color"}
        )
        actions = registry.get_actions(enabled_features={"edit", "color"})
        action_ids = {a.action_id for a in actions}
        assert not any(
            action_id in action_ids
            for action_id in {
                "edit.new_project",
                "edit.open_project",
                "edit.save_project",
                "edit.save_project_as",
                "edit.close_project",
            }
        )


class TestModuleShortcutRegistration:
    """Test that module shortcut registrations carry correct feature_id."""

    def test_edit_shortcuts_have_feature_id(self):
        """Edit module shortcuts should carry feature_id='edit'."""
        from src.shared.ui.shortcut_manager import ShortcutAction
        from src.features.edit.module import EditModule

        # Collect registered shortcuts by inspecting register calls
        registered = {}

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                sa = ShortcutAction(
                    id=kwargs.get("id") or args[0],
                    label=kwargs.get("label") or args[1],
                    category=kwargs.get("category") or args[2],
                    default_shortcut=kwargs.get("default_shortcut") or args[3],
                    feature_id=kwargs.get("feature_id"),
                )
                registered[sa.id] = sa

        edit = EditModule()
        edit._available = True
        edit.register_shortcuts(FakeShortcutManager())

        for sid, sa in registered.items():
            assert sa.feature_id == "edit", f"Shortcut {sid} has feature_id={sa.feature_id}"
        # Check specific shortcuts exist
        assert "edit.timeline.cut" in registered
        assert "edit.timeline.copy" in registered
        assert "edit.timeline.paste" in registered
        assert "edit.timeline.ripple_delete" in registered

    def test_composite_shortcuts_have_feature_id(self):
        """Composite module shortcuts should carry feature_id='composite'."""
        from src.shared.ui.shortcut_manager import ShortcutAction
        from src.features.composite.module import CompositeModule

        registered = {}

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                sa = ShortcutAction(
                    id=kwargs.get("id") or args[0],
                    label=kwargs.get("label") or args[1],
                    category=kwargs.get("category") or args[2],
                    default_shortcut=kwargs.get("default_shortcut") or args[3],
                    feature_id=kwargs.get("feature_id"),
                )
                registered[sa.id] = sa

        composite = CompositeModule()
        composite._available = True
        composite.register_shortcuts(FakeShortcutManager())

        for sid, sa in registered.items():
            assert sa.feature_id == "composite", f"Shortcut {sid} has feature_id={sa.feature_id}"
        assert "composite.node.bypass" in registered
        assert "composite.render" in registered

    def test_deliver_shortcuts_have_feature_id(self):
        """Deliver module shortcuts should carry feature_id='deliver'."""
        from src.shared.ui.shortcut_manager import ShortcutAction
        from src.features.deliver.module import DeliverModule

        registered = {}

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                sa = ShortcutAction(
                    id=kwargs.get("id") or args[0],
                    label=kwargs.get("label") or args[1],
                    category=kwargs.get("category") or args[2],
                    default_shortcut=kwargs.get("default_shortcut") or args[3],
                    feature_id=kwargs.get("feature_id"),
                )
                registered[sa.id] = sa

        deliver = DeliverModule()
        deliver._available = True
        deliver.register_shortcuts(FakeShortcutManager())

        for sid, sa in registered.items():
            assert sa.feature_id == "deliver", f"Shortcut {sid} has feature_id={sa.feature_id}"
        assert "deliver.export.current_source" in registered

    def test_color_shortcuts_have_feature_id(self):
        """Color module shortcuts should carry feature_id='color'."""
        from src.shared.ui.shortcut_manager import ShortcutAction
        from src.features.color.module import ColorModule

        registered = {}

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                sa = ShortcutAction(
                    id=kwargs.get("id") or args[0],
                    label=kwargs.get("label") or args[1],
                    category=kwargs.get("category") or args[2],
                    default_shortcut=kwargs.get("default_shortcut") or args[3],
                    feature_id=kwargs.get("feature_id"),
                )
                registered[sa.id] = sa

        color = ColorModule()
        color.register_shortcuts(FakeShortcutManager())

        for sid, sa in registered.items():
            assert sa.feature_id == "color", f"Shortcut {sid} has feature_id={sa.feature_id}"
        assert "color.capture_still" in registered
        assert "color.reset_grade" in registered

    def test_no_duplicate_shortcut_ids(self):
        """No two modules should register the same shortcut ID."""
        from src.shared.ui.shortcut_manager import ShortcutAction
        from src.features.edit.module import EditModule
        from src.features.composite.module import CompositeModule
        from src.features.deliver.module import DeliverModule
        from src.features.color.module import ColorModule

        all_ids = []

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                sid = kwargs.get("id") or args[0]
                all_ids.append(sid)

        for Mod in (EditModule, CompositeModule, DeliverModule, ColorModule):
            mod = Mod()
            if hasattr(mod, '_available'):
                mod._available = True
            mod.register_shortcuts(FakeShortcutManager())

        assert len(all_ids) == len(set(all_ids)), f"Duplicate shortcut IDs: {[x for x in all_ids if all_ids.count(x) > 1]}"

    def test_edit_shortcuts_not_registered_when_unavailable(self):
        """Edit module should not register shortcuts when _available is False."""
        from src.features.edit.module import EditModule

        registered = []

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                registered.append(kwargs.get("id") or args[0])

        edit = EditModule()
        edit._available = False
        edit.register_shortcuts(FakeShortcutManager())
        assert len(registered) == 0

    def test_composite_shortcuts_not_registered_when_unavailable(self):
        """Composite module should not register shortcuts when _available is False."""
        from src.features.composite.module import CompositeModule

        registered = []

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                registered.append(kwargs.get("id") or args[0])

        composite = CompositeModule()
        composite._available = False
        composite.register_shortcuts(FakeShortcutManager())
        assert len(registered) == 0

    def test_deliver_shortcuts_not_registered_when_unavailable(self):
        """Deliver module should not register shortcuts when _available is False."""
        from src.features.deliver.module import DeliverModule

        registered = []

        class FakeShortcutManager:
            def register(self, *args, **kwargs):
                registered.append(kwargs.get("id") or args[0])

        deliver = DeliverModule()
        deliver._available = False
        deliver.register_shortcuts(FakeShortcutManager())
        assert len(registered) == 0


class TestI18NModuleContribution:
    """Test i18n module translation contribution system."""

    def test_load_module_translations_nonexistent_dir(self):
        """Loading from non-existent directory should not crash."""
        I18N.load_module_translations("/nonexistent/path/locales")
        # Should not raise

    def test_deep_merge(self):
        """_deep_merge should recursively merge dicts, with override winning."""
        base = {"menu": {"file": "File", "edit": "Edit"}}
        override = {"menu": {"file": "Fichier"}, "color": {"reset": "Reset"}}
        result = I18N._deep_merge(base, override)
        assert result["menu"]["file"] == "Fichier"  # overridden
        assert result["menu"]["edit"] == "Edit"     # preserved
        assert result["color"]["reset"] == "Reset"   # added

    def test_deep_merge_does_not_mutate_base(self):
        """_deep_merge should not mutate the original base dict."""
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        result = I18N._deep_merge(base, override)
        assert "c" not in base["a"]  # base untouched
        assert result["a"]["c"] == 2

    def test_load_module_translations_merges(self, tmp_path):
        """Module translations should be merged into I18N."""
        locale_file = tmp_path / "en_US.json"
        locale_file.write_text('{"test_module": {"key": "value"}}', encoding="utf-8")

        original_translations = dict(I18N._translations)
        try:
            I18N.load_module_translations(str(tmp_path))
            assert I18N._translations.get("test_module", {}).get("key") == "value"
        finally:
            I18N._translations = original_translations

    def test_load_module_translations_invalid_json(self, tmp_path):
        """Invalid JSON in module locale file should not crash."""
        bad_file = tmp_path / "en_US.json"
        bad_file.write_text("not valid json{", encoding="utf-8")

        original_translations = dict(I18N._translations)
        try:
            I18N.load_module_translations(str(tmp_path))
            # Should not raise, translations unchanged
            assert I18N._translations == original_translations
        finally:
            I18N._translations = original_translations

    def test_load_module_translations_merges_fallback(self, tmp_path):
        """Module fallback translations should be merged into _fallback_translations."""
        fallback_file = tmp_path / "en_US.json"
        fallback_file.write_text('{"fb_module": {"greeting": "Hello"}}', encoding="utf-8")

        original_fallback = dict(I18N._fallback_translations)
        try:
            I18N.load_module_translations(str(tmp_path))
            assert I18N._fallback_translations.get("fb_module", {}).get("greeting") == "Hello"
        finally:
            I18N._fallback_translations = original_fallback

    def test_module_register_i18n_does_not_crash(self):
        """Each module's register_i18n() should not crash."""
        from src.features.color.module import ColorModule
        from src.features.edit.module import EditModule
        from src.features.composite.module import CompositeModule
        from src.features.deliver.module import DeliverModule

        original_translations = dict(I18N._translations)
        original_fallback = dict(I18N._fallback_translations)
        try:
            ColorModule().register_i18n()
            EditModule().register_i18n()
            CompositeModule().register_i18n()
            DeliverModule().register_i18n()
        finally:
            I18N._translations = original_translations
            I18N._fallback_translations = original_fallback
