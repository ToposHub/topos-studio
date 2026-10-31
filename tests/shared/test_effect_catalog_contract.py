"""DESK-06B · 效果/转场目录共享契约测试。

锚点：
- 入库目录 = 注册表当前产物（文件新鲜度）；
- 目录 id 唯一、params 值域自洽（min ≤ default ≤ max）；
- 04B 分级所查的注册表与目录一致（capability 的"未识别"判定不会因
  目录/注册表分叉而误判）；
- 目录摘要双端核对锚点。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.shared.contracts import effect_catalog as ec

pytestmark = pytest.mark.edit_core

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _pristine_catalog() -> dict:
    """在纯净子进程重建目录——测试收集期的注册表污染不进入比对。"""
    result = subprocess.run(
        [sys.executable, "-c",
         "import json,sys;sys.path.insert(0, '.')\n"
         "from src.shared.contracts.effect_catalog import build_catalog\n"
         "print(json.dumps(build_catalog()))"],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestCatalogFreshness:
    def test_committed_file_matches_pristyne_registry(self) -> None:
        assert ec.load_catalog_file() == _pristine_catalog()

    def test_digest_deterministic_and_pinned(self) -> None:
        digest = ec.catalog_digest()
        assert digest == ec.catalog_digest(ec.load_catalog_file())
        # 2026-10-25 冻结值（plugin:* 动态插件不入冻结目录）；目录
        # 演进（追加条目）时同步更新
        assert digest.startswith("214ca242c9b82c1a")


class TestCatalogIntegrity:
    def test_unique_ids(self) -> None:
        catalog = ec.load_catalog_file()
        effect_ids = [e["effect_type"] for e in catalog["effects"]]
        transition_ids = [t["transition_type"] for t in catalog["transitions"]]
        assert len(effect_ids) == len(set(effect_ids))
        assert len(transition_ids) == len(set(transition_ids))

    def test_param_ranges_self_consistent(self) -> None:
        catalog = ec.load_catalog_file()
        for effect in catalog["effects"]:
            for name, param in effect["params"].items():
                lo, hi, default = param["min"], param["max"], param["default"]
                if isinstance(default, (int, float)) and \
                        isinstance(lo, (int, float)) and lo is not None:
                    assert default >= lo, (effect["effect_type"], name)
                if isinstance(default, (int, float)) and \
                        isinstance(hi, (int, float)) and hi is not None:
                    assert default <= hi, (effect["effect_type"], name)

    def test_effect_ids_are_stable_identifiers(self) -> None:
        """effect_type 是包内引用的跨端 id：ASCII、无空白。"""
        for effect in ec.load_catalog_file()["effects"]:
            tid = effect["effect_type"]
            assert tid and tid == tid.strip()
            assert all(ord(c) < 128 for c in tid), tid

    def test_builtin_transitions_only(self) -> None:
        catalog = ec.load_catalog_file()
        assert all("source" not in t for t in catalog["transitions"])


class TestCatalogCoversProductionLookups:
    def test_builtin_definitions_all_in_catalog(self) -> None:
        catalog_types = {
            e["effect_type"] for e in ec.load_catalog_file()["effects"]}
        for entry in _pristine_catalog()["effects"]:
            assert entry["effect_type"] in catalog_types, entry["effect_type"]
        # 目录与纯净注册表完全一致（含内容）
        pristine = {e["effect_type"]: e
                    for e in _pristine_catalog()["effects"]}
        committed = {e["effect_type"]: e
                     for e in ec.load_catalog_file()["effects"]}
        assert committed == pristine

    def test_capability_check_uses_same_universe(self) -> None:
        """04B 的'未识别效果'判定与目录同一事实来源。"""
        catalog_types = {
            e["effect_type"] for e in ec.load_catalog_file()["effects"]}
        from src.features.edit.core.effect_registry import get_effect_definition

        for effect_type in catalog_types:
            assert get_effect_definition(effect_type) is not None, effect_type
