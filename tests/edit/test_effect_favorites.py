"""
Tests for Favorites functionality in EditEffectsPanel.

P7 Completion - Favorites system tests.
"""

import pytest
from unittest.mock import MagicMock, patch
from PyQt6.QtWidgets import QApplication

pytestmark = pytest.mark.edit_core

# Import the module first to ensure it's loaded
import src.features.edit.ui.edit_effects_panel as effects_panel_module

# Ensure QApplication exists for widget tests
@pytest.fixture(scope="module")
def qapp():
    """Create QApplication for tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestFavoritesManagement:
    """Test favorites management methods."""

    def test_load_favorites_empty(self, qapp):
        """Test loading favorites when none are saved."""
        mock_config = MagicMock()
        mock_config.get.return_value = None

        # Create panel with mocked config
        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()
            favorites = panel._load_favorites()

            assert favorites == set()
            assert len(favorites) == 0

    def test_load_favorites_with_data(self, qapp):
        """Test loading favorites with saved data."""
        mock_config = MagicMock()
        mock_config.get.return_value = ["gaussian_blur", "vignette", "gain"]

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()
            favorites = panel._load_favorites()

            assert favorites == {"gaussian_blur", "vignette", "gain"}
            assert len(favorites) == 3

    def test_save_favorites(self, qapp):
        """Test saving favorites to config."""
        mock_config = MagicMock()
        mock_config.get.return_value = []

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Add some favorites
            panel._favorites.add("gaussian_blur")
            panel._favorites.add("sharpen")

            # Save
            panel._save_favorites()

            # Verify set was called with list
            mock_config.set.assert_called()
            # Check last call
            call_args = mock_config.set.call_args
            assert call_args[0][0] == "effect_favorites"
            saved_list = call_args[0][1]
            assert "gaussian_blur" in saved_list
            assert "sharpen" in saved_list

    def test_is_favorite(self, qapp):
        """Test checking if effect is favorite."""
        mock_config = MagicMock()
        mock_config.get.return_value = ["gaussian_blur"]

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            assert panel._is_favorite("gaussian_blur") == True
            assert panel._is_favorite("sharpen") == False
            assert panel._is_favorite("vignette") == False

    def test_toggle_favorite_add(self, qapp):
        """Test toggling favorite - adding new favorite."""
        mock_config = MagicMock()
        mock_config.get.return_value = []

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Toggle to add
            panel._toggle_favorite("gaussian_blur")

            assert "gaussian_blur" in panel._favorites
            assert panel._is_favorite("gaussian_blur") == True

            # Verify saved
            mock_config.set.assert_called()

    def test_toggle_favorite_remove(self, qapp):
        """Test toggling favorite - removing existing favorite."""
        mock_config = MagicMock()
        mock_config.get.return_value = ["gaussian_blur", "vignette"]

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Should have loaded favorites
            assert "gaussian_blur" in panel._favorites

            # Toggle to remove
            panel._toggle_favorite("gaussian_blur")

            assert "gaussian_blur" not in panel._favorites
            assert panel._is_favorite("gaussian_blur") == False

            # Vignette should still be favorite
            assert "vignette" in panel._favorites


class TestFavoritesTreePopulation:
    """Test Favorites tree widget population."""

    def test_populate_favorites_empty(self, qapp):
        """Test populating favorites tree when no favorites."""
        mock_config = MagicMock()
        mock_config.get.return_value = None

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Favorites tree should have placeholder
            tree = panel.favorites_tree
            top_level_count = tree.topLevelItemCount()

            assert top_level_count >= 1

            # First item should be placeholder
            item = tree.topLevelItem(0)
            text = item.text(0)
            assert "No favorites" in text or "no" in text.lower()

    def test_populate_favorites_with_effects(self, qapp):
        """Test populating favorites tree with favorited effects."""
        # 必须用注册表里存在的 effect_type（旧 id gaussian_blur/vignette/
        # gain 已不在 EffectRegistry，populated 树会因 get_definition=None 为空）
        mock_config = MagicMock()
        mock_config.get.return_value = ["blur", "sharpen", "denoise"]

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Favorites tree should have items
            tree = panel.favorites_tree

            # Should have category items for each category
            top_level_count = tree.topLevelItemCount()
            assert top_level_count >= 1


class TestAudioEffectsTreePopulation:
    """Test Audio Effects tree widget population."""

    def test_populate_audio_effects(self, qapp):
        """Test populating audio effects tree."""
        mock_config = MagicMock()
        mock_config.get.return_value = None

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Audio effects tree should have items
            tree = panel.audio_effects_tree
            top_level_count = tree.topLevelItemCount()

            assert top_level_count >= 1

            # Should have Audio category
            audio_cat = tree.topLevelItem(0)
            assert audio_cat is not None

            # Category should have children (the 8 mixer audio effects)
            child_count = audio_cat.childCount()
            assert child_count >= 8  # strip_eq, limiter, compressor, reverb, ...

    def test_audio_effects_draggable(self, qapp):
        """Test audio effects tree has drag enabled."""
        from PyQt6.QtWidgets import QAbstractItemView

        mock_config = MagicMock()
        mock_config.get.return_value = None

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            tree = panel.audio_effects_tree
            assert tree.dragEnabled() == True
            assert tree.dragDropMode() == QAbstractItemView.DragDropMode.DragOnly


class TestContextMenu:
    """Test context menu for favorites toggle."""

    def test_context_menu_action_text(self, qapp):
        """Test context menu shows correct action text."""
        mock_config = MagicMock()
        mock_config.get.return_value = []

        with patch.object(effects_panel_module, 'ConfigManager', return_value=mock_config):
            panel = effects_panel_module.EditEffectsPanel()

            # Toggle favorite to set up state
            panel._toggle_favorite("gaussian_blur")

            # Should now be favorite
            assert panel._is_favorite("gaussian_blur")

            # Toggle again to remove
            panel._toggle_favorite("gaussian_blur")

            # Should no longer be favorite
            assert not panel._is_favorite("gaussian_blur")


class TestEffectRegistryCategoryDisplayName:
    """Test category display name mapping."""

    def test_audio_category_display_name(self):
        """Test 'audio' category gets correct display name."""
        from src.features.edit.core.effect_registry import get_effect_registry

        registry = get_effect_registry()
        display_name = registry.get_category_display_name("audio")

        assert display_name == "Audio"

    def test_unknown_category_display_name(self):
        """Test unknown category returns capitalized name."""
        from src.features.edit.core.effect_registry import get_effect_registry

        registry = get_effect_registry()
        display_name = registry.get_category_display_name("unknown_category")

        # Should return capitalized version
        assert display_name == "Unknown_Category"