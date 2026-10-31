"""G2 · 资源打包验证（wheel package-data + PyInstaller datas 收集）。

真实冻结包验收仍需打包集成者在干净机器构建后打开验证；本测试守住
配置回归：insights 自有词条必须随两种分发形态收集。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


pytestmark = [pytest.mark.edit_core]

ROOT = Path(__file__).resolve().parents[3]


def test_pyproject_declares_insights_package_data():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]
    assert package_data.get("src.features.edit.insights") == ["locales/*.json"]


def test_pyinstaller_datas_collect_insights_locales(monkeypatch):
    """离线截获 generate_analysis 的 datas（stub PyInstaller Analysis）。"""
    captured: dict = {}

    class FakeAnalysis:
        def __init__(self, scripts, pathex, binaries, datas, hiddenimports, **kw):
            captured["datas"] = datas

    pyinstaller = types.ModuleType("PyInstaller")
    pyinstaller.__path__ = []
    building = types.ModuleType("PyInstaller.building")
    building.__path__ = []
    build_main = types.ModuleType("PyInstaller.building.build_main")
    build_main.Analysis = FakeAnalysis
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.building", building)
    monkeypatch.setitem(sys.modules, "PyInstaller.building.build_main", build_main)

    sys.path.insert(0, str(ROOT / "packaging" / "pyinstaller"))
    try:
        import base_spec  # noqa: F401  已 stub，无构建副作用

        try:
            base_spec.generate_analysis("topos-edit", "apps.topos_edit.main")
        except BaseException:
            pass  # Analysis 之后的收尾可以失败；datas 已截获
    finally:
        sys.path.remove(str(ROOT / "packaging" / "pyinstaller"))
        sys.modules.pop("base_spec", None)

    dests = [d[1] for d in captured.get("datas", [])]
    assert "src/features/edit/insights/locales" in dests
    assert "src/features/edit/locales" in dests  # 顶层收集不回归


def test_insights_locale_files_exist_on_disk():
    locales = ROOT / "src/features/edit/insights/locales"
    names = {p.name for p in locales.glob("*.json")}
    assert {"zh_CN.json", "en_US.json"} <= names
