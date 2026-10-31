"""Regression tests for the Composite/Color shared Scopes panel."""

import numpy as np


def test_composite_scope_panel_is_the_color_scope_panel(qapp):
    from src.features.color.ui.scope_panel import MODE_ORDER, ScopePanel
    from src.features.composite.ui.composite_scope_widgets import (
        CompositeScopePanel,
        SCOPE_MODE_ORDER,
        configure_composite_scope_panel,
    )

    # An alias, rather than a second implementation, is what guarantees that
    # controls, mode order, styling and i18n cannot drift between pages.
    assert CompositeScopePanel is ScopePanel
    assert tuple(MODE_ORDER) == SCOPE_MODE_ORDER

    panel = configure_composite_scope_panel(CompositeScopePanel())
    panel.resize(600, 300)
    panel.show()

    assert [panel.mode_combo.itemData(i) for i in range(panel.mode_combo.count())] == list(MODE_ORDER)
    # Composite scopes run the same full-GPU pipeline as Color: the shared
    # panel stays on its default GPU page (the CPU page is only the explicit
    # diagnostic fallback / source-reference path).
    assert panel.display_stack.currentIndex() == 1
    assert panel._gpu_display_active
    assert panel._cpu_analysis_max_width == 768
    assert panel._cpu_analysis_max_height == 512
    assert panel.selector_container is not None
    assert panel.settings_btn is not None
    assert panel.source_label is not None
    assert panel.source_label.text().endswith("Graded")
    panel.close()


def test_composite_scope_panel_processes_all_color_scope_modes(qapp):
    from src.features.composite.ui.composite_scope_widgets import (
        CompositeScopePanel,
        MODE_ORDER,
        configure_composite_scope_panel,
    )

    panel = configure_composite_scope_panel(CompositeScopePanel())
    panel.resize(600, 300)
    panel.show()
    image = np.random.default_rng(7).random((48, 64, 3), dtype=np.float32)

    for mode in MODE_ORDER:
        panel.set_mode(mode)
        panel.update_scope(image)
        panel._process_pending_image()
        assert panel.current_mode == mode
        assert panel._last_image is image

    assert panel.stack.count() == len(MODE_ORDER)
    assert panel.parade_widget is not None
    assert panel.luma_widget is not None
    assert panel.histogram_widget is not None
    assert panel.cie_widget is not None
    assert panel.falsecolor_widget is not None
    panel.close()


def test_composite_scope_panel_bounds_full_size_readback_for_cpu_analysis(qapp):
    from src.features.composite.ui.composite_scope_widgets import (
        CompositeScopePanel,
        configure_composite_scope_panel,
    )

    panel = configure_composite_scope_panel(CompositeScopePanel())
    panel.resize(600, 300)
    panel.show()
    image = np.zeros((1024, 1536, 4), dtype=np.float32)
    image[:, :, 3] = 1.0

    panel.update_scope(image)
    panel._process_pending_image()

    assert panel._last_image.shape == (512, 768, 3)
    panel.close()
