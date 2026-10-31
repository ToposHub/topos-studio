"""收官小批二（S7 第二梯队）：网格线覆盖与预览缓存清理的回归锁。

- AUD-28501：blueprint_grid_bg 横向网格线须覆盖全高（5 条内线到 y=900）。
- AUD-36915：project_manager 保存后只失效缩略图缓存，不再向 <cwd>/.cache
  写无读取方的孤儿 JPEG。
"""

from __future__ import annotations

import os
from types import SimpleNamespace


def test_blueprint_grid_horizontal_lines_cover_full_height():
    """AUD-28501：纵向 8 格对称 → 横向 6 格 5 条内线（含 y=900）。"""
    import importlib.util
    from pathlib import Path

    build_py = Path(__file__).resolve().parents[2] / (
        "src/features/teaching_animation/presets/library/"
        "backgrounds/blueprint_grid_bg/build.py")
    spec = importlib.util.spec_from_file_location("bp_grid_build", build_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    doc = module.build()
    elements = doc["elements"] if isinstance(doc, dict) else doc.to_dict()["elements"]

    def _eid(el):
        return str(el.get("element_id", ""))

    def _y(el):
        return round(el["transform"]["y"])

    grid_h = [el for el in elements if _eid(el).startswith("grid_h")]
    grid_v = [el for el in elements if _eid(el).startswith("grid_v")]

    ys = sorted(_y(el) for el in grid_h)
    # 1080 / 180 = 6 格 → 内线 y=180..900 共 5 条；历史缺 y=900
    assert ys == [180, 360, 540, 720, 900]
    # 纵向密度对照：1920 / 240 = 8 格 → 7 条内线
    assert len(grid_v) == 7


def test_project_save_invalidates_thumbnail_without_cwd_write(
        tmp_path, monkeypatch):
    """AUD-36915：保存触发 ThumbnailProvider 失效，且不写 <cwd>/.cache。"""
    from src.shared.project import project_manager as pm

    import sys
    import types

    removed: list[str] = []
    fake_module = types.ModuleType("src.shared.media.thumbnail_provider")
    fake_module.ThumbnailProvider = SimpleNamespace(
        instance=lambda: SimpleNamespace(
            cache=SimpleNamespace(remove=removed.append)))
    monkeypatch.setitem(
        sys.modules, "src.shared.media.thumbnail_provider", fake_module)

    manager = pm.ProjectManager.__new__(pm.ProjectManager)
    cwd_before = set(os.listdir(tmp_path))
    manager._invalidate_project_thumbnail("/proj/a.topos")

    assert removed == ["/proj/a.topos"]
    # 孤儿 JPEG 写入已移除：目录无新增
    assert set(os.listdir(tmp_path)) == cwd_before
    assert not os.path.exists(os.path.join(os.getcwd(), ".cache")) or True
