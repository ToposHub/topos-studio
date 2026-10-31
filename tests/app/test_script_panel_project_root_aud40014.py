"""AUD-40014 回归：script_panel._get_project_root 少一级 ``..``。

旧实现从 ``src/app/ui/script_panel.py`` 只回退两级，落在 ``<root>/src``——
用户 Scripts 目录被错误写进/读取源码树 ``src/Scripts``（该空目录由旧代码
自动创建），而文档口径的仓库根（``run_topos.command`` 所在处）需要三级
回退。审计报告：docs/audit/2026-09-28/batches/batch-040-app-ui.md。
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.app

from src.app.ui import script_panel


def test_get_project_root_is_repo_root():
    """_get_project_root 必须解析到 run_topos.command 所在的仓库根。"""
    root = script_panel._get_project_root()
    assert os.path.isfile(os.path.join(root, "run_topos.command")), (
        f"project root {root!r} does not contain run_topos.command"
    )
    # 仓库根必须含 src/（与 src/app/ui 相对三级回退自洽）
    assert os.path.isdir(os.path.join(root, "src"))


def test_get_project_root_matches_package_parent():
    """与包结构对账：root == <repo>/（src/app/ui 再往上两级）。"""
    panel_dir = os.path.dirname(os.path.abspath(script_panel.__file__))
    expected = os.path.normpath(
        os.path.join(panel_dir, "..", "..", ".."))
    assert script_panel._get_project_root() == expected


def test_get_scripts_dir_is_repo_root_scripts_not_src_tree():
    """用户 Scripts 目录必须落在仓库根 Scripts/，不得污染 src/ 源码树。"""
    scripts_dir = script_panel._get_scripts_dir()
    root = script_panel._get_project_root()
    assert scripts_dir == os.path.join(root, "Scripts")
    assert os.path.isdir(scripts_dir)
    # 旧缺陷痕迹：src/Scripts 不应被（重新）创建
    assert not os.path.isdir(os.path.join(root, "src", "Scripts"))
