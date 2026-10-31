"""出厂默认布局（factory default layout）契约测试。

验收：
- 仓库根的 ``default_layout.json`` 必须存在且结构合法（CI 守护：防止提交损坏的出厂布局）。
- 视频/图片两种工作模式按需分桶存储，缺失模式回退 "all" 桶，再回退旧版扁平结构。
- 路径解析支持 ``TOPOS_DEFAULT_LAYOUT_PATH`` 覆盖（测试与维护流程用）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QByteArray

from src.app.controllers.dock_layout_controller import DockLayoutMixin

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FACTORY_PATH = PROJECT_ROOT / "default_layout.json"

# DockLayoutMixin 无自定义 __init__，出厂布局方法只依赖文件系统与传入参数，
# 可直接实例化调用，无需构造完整 MainWindow。
_stub = DockLayoutMixin()


def _load(work_mode=None):
    return _stub._load_factory_layout(work_mode=work_mode)


# ----------------------------------------------------------------------
# 仓库内出厂文件契约
# ----------------------------------------------------------------------

def test_committed_factory_file_is_valid():
    """提交到仓库的出厂布局文件必须可解析且结构完整。"""
    assert FACTORY_PATH.is_file(), (
        "default_layout.json 缺失：出厂默认布局必须随仓库提交，"
        "否则 Reset to Default 只能退回硬编码布局"
    )
    data = json.loads(FACTORY_PATH.read_text(encoding="utf-8"))
    assert data.get("schema") == 1
    modes = data.get("modes")
    assert isinstance(modes, dict) and modes, "modes 分桶不能为空"
    assert any(m in modes for m in ("video", "image", "all")), (
        "至少要有 video/image/all 中的一个捕获"
    )
    for mode, bucket in modes.items():
        assert isinstance(bucket.get("geometry"), str) and bucket["geometry"]
        assert isinstance(bucket.get("state"), str) and bucket["state"]
        # state 必须是合法 hex（QByteArray.fromHex 可解码）
        QByteArray.fromHex(bucket["state"].encode())
        panels = bucket.get("panel_states")
        if panels is not None:
            assert isinstance(panels, dict)
            for page, state_hex in panels.items():
                assert isinstance(state_hex, str)
                QByteArray.fromHex(state_hex.encode())


def test_load_factory_layout_reads_committed_file():
    """默认路径能读到已提交的出厂布局（按当前默认模式 video）。"""
    geom, state, panels = _load(work_mode="video")
    assert geom and state
    assert panels is None or isinstance(panels, dict)


# ----------------------------------------------------------------------
# 模式分桶与回退
# ----------------------------------------------------------------------

def test_mode_fallback_all_then_legacy(tmp_path, monkeypatch):
    path = tmp_path / "default_layout.json"
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(path))

    # 无文件 → None
    assert _load(work_mode="image") == (None, None, None)

    # 只有 video 捕获：image 请求拿不到
    path.write_text(json.dumps({
        "schema": 1,
        "modes": {"video": {"geometry": "01", "state": "02", "panel_states": {}}},
    }), encoding="utf-8")
    assert _load(work_mode="image") == (None, None, None)
    assert _load(work_mode="video")[0] == "01"

    # 增加 all 桶：image 回退到 all
    data = json.loads(path.read_text(encoding="utf-8"))
    data["modes"]["all"] = {"geometry": "03", "state": "04"}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _load(work_mode="image")[:2] == ("03", "04")

    # 旧版扁平结构视为 all
    path.write_text(json.dumps({"geometry": "05", "state": "06"}), encoding="utf-8")
    assert _load(work_mode="image")[:2] == ("05", "06")
    assert _load(work_mode="video")[:2] == ("05", "06")


def test_corrupt_file_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "default_layout.json"
    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", str(path))

    path.write_text("{ not json", encoding="utf-8")
    assert _load(work_mode="video") == (None, None, None)

    path.write_text(json.dumps({"schema": 1, "modes": {"video": {"geometry": ""}}}), encoding="utf-8")
    assert _load(work_mode="video") == (None, None, None)


def test_path_env_override_and_source_default(monkeypatch):
    monkeypatch.delenv("TOPOS_DEFAULT_LAYOUT_PATH", raising=False)
    resolved = _stub._get_factory_layout_path()
    assert resolved == str(PROJECT_ROOT / "default_layout.json")

    monkeypatch.setenv("TOPOS_DEFAULT_LAYOUT_PATH", "/tmp/custom.json")
    assert _stub._get_factory_layout_path() == "/tmp/custom.json"
