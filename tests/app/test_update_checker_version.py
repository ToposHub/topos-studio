"""R0-4 回归测试：应用版本必须以 pyproject.toml 为单一事实源。

历史缺陷：update_checker.CURRENT_VERSION 硬编码 "1.0.0"，与 pyproject 的
"3.0.0" 严重不符——欢迎页/Project Manager 显示 v1.0.0，远端版本比较基线错误。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from src.app.update_checker import CURRENT_VERSION, _FALLBACK_VERSION, parse_version

ROOT = Path(__file__).resolve().parents[2]


def test_current_version_matches_pyproject():
    with open(ROOT / "pyproject.toml", "rb") as f:
        pyproject_version = tomllib.load(f)["project"]["version"]
    assert CURRENT_VERSION == pyproject_version, (
        "update_checker.CURRENT_VERSION 与 pyproject.toml 版本漂移，"
        "更新比较基线与 UI 显示将失真"
    )
    # 字面量兜底也必须与 pyproject 同步
    assert _FALLBACK_VERSION == pyproject_version


def test_parse_version_handles_v_prefix():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("3.0.0") == (3, 0, 0)
    assert parse_version("garbage") == (0, 0, 0)
