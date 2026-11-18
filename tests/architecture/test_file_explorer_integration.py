"""Native Files must not import or boot the retired Files runtime."""
from pathlib import Path
import ast
import os
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]


def test_native_host_source_has_no_browser_server_or_legacy_imports():
    for relative in ("src/app/ui/files_panel.py", "src/app/ui/library_panel.py",
                     "src/app/ui/preferences_dialog.py", "src/app/main_window.py"):
        source = (REPO / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(name and ("file_explorer" in name or "QtWebEngine" in name)
                       for name in imports), relative
    source = (REPO / "src/app/ui/files_panel.py").read_text()
    assert "start_server" not in source and "runJavaScript" not in source


def test_files_host_import_succeeds_when_old_runtime_imports_are_forbidden(tmp_path):
    code = r"""
import importlib.abc
import sys
class RejectOldRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'file_explorer' or fullname.startswith(('file_explorer.', 'external.file_explorer')):
            raise AssertionError('retired runtime imported: ' + fullname)
sys.meta_path.insert(0, RejectOldRuntime())
from src.app.ui.files_panel import FilesPanel
from src.app.ui.library_panel import LibraryPanel
from src.features.files.builtin_plugins import NativePluginCatalog
assert len(NativePluginCatalog().snapshot()) == 10
assert hasattr(FilesPanel, 'set_root_path')
assert hasattr(LibraryPanel, 'refresh_file_rating')
"""
    env = dict(os.environ, PYTHONPATH=str(REPO), TOPOS_CONFIG_PATH=str(tmp_path / "config.json"))
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
