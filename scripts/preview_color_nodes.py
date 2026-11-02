#!/usr/bin/env python3
"""Launch the Color Nodes interaction design without loading any backend.

    .venv/bin/python scripts/preview_color_nodes.py
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/preview_color_nodes.py \
        --screenshot /tmp/color-nodes.png --scenario branches

The optional fixture graph is presentation data, not an image-processing demo.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow

from src.features.color.ui.node_editor_panel import ColorNodePanel
from src.features.color.ui.node_editor_state import NodeKind
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import DARK_THEME


def build_preview(scenario: str = "serial", locale: str = "en_US") -> QMainWindow:
    I18N.load_locale(locale)
    I18N.load_module_translations(str(ROOT / "src/features/color/locales"))
    window = QMainWindow()
    window.setObjectName("ColorNodeDesignPreview")
    window.setWindowTitle("Topos — Color Nodes · Interaction Design")
    panel = ColorNodePanel(window)
    window.setCentralWidget(panel)
    window.resize(1000, 520)
    panel.set_group_available(True)
    state = panel.view.state
    if scenario == "branches":
        first = state.graph.active_id
        state.set_node_fields({first}, label="Balance")
        second = state.add_serial()
        state.set_node_fields({second}, label="Skin")
        branch = state.add_branch(NodeKind.PARALLEL_MIXER)
        state.set_node_fields({branch}, label="Background")
        mixer = next(node.id for node in state.graph.nodes.values() if node.kind == NodeKind.PARALLEL_MIXER)
        state.select({mixer}, mixer)
        look = state.add_serial()
        state.set_node_fields({look}, label="Look")
        state.auto_layout()
        # Fixture loading is not a user operation and should start with clean history.
        panel.load_graph(state.snapshot())
    elif scenario == "compound":
        first = state.graph.active_id
        second = state.add_serial()
        compound = state.make_compound({first, second})
        state.set_node_fields({compound}, label="Look")
        panel.load_graph(state.snapshot())
    panel.view.rebuild()
    window._color_node_panel = panel  # Keep an explicit preview inspection handle.
    return window


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("serial", "branches", "compound"), default="serial")
    parser.add_argument("--locale", choices=("en_US", "zh_CN"), default="en_US")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Topos Node Design")
    app.setStyleSheet(DARK_THEME)
    window = build_preview(args.scenario, args.locale)
    window.show()
    if args.screenshot is not None:
        def capture() -> None:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.centralWidget().grab().save(str(args.screenshot)):
                raise OSError(f"could not save preview: {args.screenshot}")
            app.quit()
        QTimer.singleShot(120, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
