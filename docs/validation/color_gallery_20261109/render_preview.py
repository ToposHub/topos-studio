"""Render real Gallery widgets using disposable sample stills, without a media engine."""
from pathlib import Path
import os
import sys
import tempfile
import zipfile
import json

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage
from PyQt6.QtCore import Qt
from src.features.color.ui import presets_panel
from src.features.color.ui.gallery_compare import GalleryCompareDialog
from src.shared.ui.style_sheet import DARK_THEME
from src.shared.i18n.i18n import I18N


def main() -> None:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(DARK_THEME)
    I18N.load_locale('en_US')
    output = Path(__file__).resolve().parent
    original = presets_panel._resolve_presets_root
    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix='topos-gallery-ui-') as temporary:
        os.chdir(temporary)
        presets_panel._resolve_presets_root = lambda: temporary
        panel = presets_panel.PresetsPanel()
        presets_panel._resolve_presets_root = original
        import cv2
        source = ROOT / 'tests/test_videos/2K_prores422.mov'
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise RuntimeError(f'Cannot decode Gallery QA media: {source}')
        for number in range(1, 9):
            source_frame = (number - 1) * 25
            capture.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f'Cannot decode frame {source_frame}: {source}')
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width = rgb.shape[:2]
            thumbnail = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
            destination = Path(panel.get_current_folder()) / f'Still_{number:02d}.topos'
            with zipfile.ZipFile(destination, 'w') as package:
                package.writestr('project.json', json.dumps({'layers': []}))
            thumbnail.scaled(512, 288, Qt.AspectRatioMode.KeepAspectRatio).save(str(destination.with_suffix('.thumb.jpg')))
            from src.features.color.core.preset_meta import PresetMeta, save_preset_meta
            save_preset_meta(str(destination), PresetMeta(display_name=source.name, source_frame=source_frame))
        capture.release()
        panel.scan_presets()
        for width, height, sidebar, mode, search in (
            (504, 440, False, 'grid', False),
            (700, 500, True, 'grid', False),
            (700, 500, True, 'list', False),
            (504, 440, False, 'grid', True),
            (280, 360, False, 'grid', False),
        ):
            panel.resize(width, height)
            panel._gallery.sidebar_button.setChecked(sidebar)
            panel._gallery.set_view_mode(mode)
            panel._gallery.set_search_visible(search)
            panel.show()
            for _ in range(5):
                app.processEvents()
            panel.list_widget.setCurrentRow(0)
            name = f'gallery_{width}x{height}_{mode}' + ('_albums' if sidebar else '') + ('_search' if search else '')
            assert panel.width() == width
            assert panel.grab().save(str(output / (name + '.png')))
            print(name)
        entries = [(panel.list_widget.item(i).text(), panel.list_widget.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(3)]
        for mode in ('wipe', 'split'):
            dialog = GalleryCompareDialog(entries, [entries[0][1]], mode, panel)
            dialog.show()
            app.processEvents()
            assert dialog.grab().save(str(output / f'comparison_{mode}.png'))
            dialog.close()
        presets_panel._resolve_presets_root = lambda: os.path.join(temporary, 'empty-gallery')
        empty = presets_panel.PresetsPanel()
        presets_panel._resolve_presets_root = original
        empty.resize(504, 440)
        empty.show()
        for _ in range(10):
            app.processEvents()
        assert empty.list_widget.count() == 0
        assert not empty.list_widget.verticalScrollBar().isVisible()
        assert empty.grab().save(str(output / 'gallery_empty_504x440.png'))
        empty.close()
        panel.close()
        os.chdir(previous_cwd)
    presets_panel._resolve_presets_root = original


if __name__ == '__main__':
    main()
