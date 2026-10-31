"""W06（Windows 计划项）：可选 CUDA extra 声明 / 打包收集 / 能力诊断。

L1 单元（无 GPU 依赖、无 PyInstaller 依赖）：

1. pyproject 声明：``cuda-windows`` extra 含 cuda-bindings 约束（核心
   dependencies 不含 CUDA——不把所有用户强制装 CUDA）。
2. ``resident_capability_report``：结构完整、JSON 可序列化、分层语义
   （平台门/驱动 DLL/绑定/opt-in/门闩/设备身份）。
3. ``_collect_cuda_bindings``（base_spec，import 安全）：cuda 在场 →
   hiddenimports 含 driver（延迟导入显式登记）+ 二进制按原层级收集；
   cuda 缺席 → 空（可选能力缺席不阻塞构建，运行探针 fail-closed 回退）。

真机干净 venv 两态验收（无 extra→下载 planar / 有 extra→驻留+md5 对拍）
由 tools/w06_capability_check.py 执行，结果登记于
docs/validation/upgrade_2026-10-24/W06。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestExtraDeclaration:
    def test_cuda_windows_extra_declared_and_core_excludes_cuda(self):
        data = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        extras = data["project"]["optional-dependencies"]
        assert "cuda-windows" in extras, "cuda-windows extra 未声明"
        specs = " ".join(extras["cuda-windows"])
        assert "cuda-bindings" in specs
        # 核心依赖不得强制 CUDA（可选能力缺席必须可安装可运行）
        core = " ".join(data["project"]["dependencies"])
        assert "cuda" not in core.lower(), (
            f"核心 dependencies 出现 cuda：{core}")


class TestCapabilityReport:
    def test_report_structure_and_json_serializable(self):
        from src.shared.media.windows_hw.cuda_resident import (
            resident_capability_report,
        )

        r = resident_capability_report()
        for key in (
            "probe", "platform_ok", "nvcuda_ok", "cuda_bindings_ok",
            "cuda_bindings_version", "opt_in_env", "hw_kill_switch",
            "import_latch_tripped", "route_available", "reason",
        ):
            assert key in r, f"缺字段 {key}"
        json.dumps(r)  # 必须可序列化（安装包记录用）
        assert r["platform_ok"] == (sys.platform == "win32")

    def test_report_reason_chain_fail_closed(self, monkeypatch):
        """绑定缺席层：reason 必须如实带原因（不冒充 ok）。"""
        from src.shared.media import windows_hw

        cr = windows_hw.cuda_resident
        if sys.platform != "win32":
            monkeypatch.setattr(sys, "platform", "win32")

        # nvcuda 缺席 → nvcuda_missing（层短路，不往下探测）
        def _no_dll(name):
            if name == "nvcuda.dll":
                raise OSError("no dll")
            return cr.ctypes.WinDLL(name)

        monkeypatch.setattr(cr.ctypes, "WinDLL", _no_dll, raising=False)
        r = cr.resident_capability_report()
        assert r["nvcuda_ok"] is False
        assert r["reason"].startswith("nvcuda_missing")
        assert r["route_available"] is False

        # bindings 缺席 → cuda_bindings_unavailable
        import builtins

        real_import = builtins.__import__

        def _no_cuda_import(name, *a, **k):
            if name.startswith("cuda"):
                raise ImportError("cuda bindings not installed")
            return real_import(name, *a, **k)

        monkeypatch.setattr(cr.ctypes, "WinDLL", lambda n: object())
        monkeypatch.setattr(builtins, "__import__", _no_cuda_import)
        r2 = cr.resident_capability_report()
        assert r2["cuda_bindings_ok"] is False
        assert r2["reason"].startswith("cuda_bindings_unavailable")


class TestPyInstallerCollection:
    """base_spec._collect_cuda_bindings（无需安装 PyInstaller）。"""

    @pytest.fixture()
    def base_spec(self):
        spec = importlib.util.spec_from_file_location(
            "_w06_base_spec",
            PROJECT_ROOT / "packaging" / "pyinstaller" / "base_spec.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_cuda_present_collects_driver_hidden_and_binaries(self, base_spec):
        if importlib.util.find_spec("cuda") is None:
            pytest.skip("cuda bindings not installed (dev venv has it)")
        hidden, binaries = base_spec._collect_cuda_bindings()
        assert "cuda.bindings.driver" in hidden  # 延迟导入显式登记
        assert binaries, "wheel 内二进制扩展未收集"
        for src, dst in binaries:
            Path(src).suffix in (".pyd", ".so")
            assert dst.replace("\\", "/").startswith("cuda/"), (
                f"层级必须保持 cuda/ 原布局: {dst}")

    def test_cuda_absent_collects_nothing(self, base_spec, monkeypatch):
        real = importlib.util.find_spec

        def _no_cuda(name, *a, **k):
            if name.split(".")[0] == "cuda":
                return None
            return real(name, *a, **k)

        monkeypatch.setattr(importlib.util, "find_spec", _no_cuda)
        # 函数内部 from importlib import util → 同一对象，patch 生效
        hidden, binaries = base_spec._collect_cuda_bindings()
        assert hidden == [] and binaries == []
