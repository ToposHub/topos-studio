"""W01：libtopos_codec 候选查找的平台-架构-目录三重预检回归。

旧实现 Windows 上只验"文件存在"：字母序扫描让测试构建（win-half-tests）
先于正式构建被选中；错架构 DLL / `!<arch>` 伪 DLL 只能等 CDLL 的
WinError 193，与"文件损坏"不可区分（W-I01）。本文件锁定：
1. PE machine 解析与伪 DLL 判定（纯函数，全平台可跑）；
2. win-release* 目录过滤（win32）；
3. load_library 搜索失败信息含架构原因。
素材无关：不依赖真实媒体文件（区别于 test_topos_probe_unification）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.shared.codec import topos_binding


# ---------------------------------------------------------------------------
# W01：打包/开发布局查找的平台-架构-目录三重预检（不能只验文件存在）
# ---------------------------------------------------------------------------


def _synthetic_pe(machine: int) -> bytes:
    """最小合法 PE 头（MZ + e_lfanew + PE\\0\\0 + machine），供 machine 解析。"""
    header = bytearray(0x48)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x40).to_bytes(4, "little")
    header[0x40:0x44] = b"PE\x00\x00"
    header[0x44:0x46] = machine.to_bytes(2, "little")
    return bytes(header)


class TestLoaderArchValidationW01:
    def test_pe_machine_type_roundtrip(self, tmp_path: Path) -> None:
        from src.shared.codec.topos_binding import pe_machine_type

        amd64 = tmp_path / "amd64.dll"
        amd64.write_bytes(_synthetic_pe(0x8664))
        assert pe_machine_type(amd64) == 0x8664

    def test_archive_pseudo_dll_is_not_pe(self, tmp_path: Path) -> None:
        """zig4 实案：`!<arch>` 静态归档被命名成 .dll → 非 PE 判定。"""
        from src.shared.codec.topos_binding import pe_machine_type

        pseudo = tmp_path / "topos_codec.dll"
        pseudo.write_bytes(b"!<arch>\n" + b"\x00" * 64)
        assert pe_machine_type(pseudo) is None

    def test_dll_matches_current_arch_rejects_pseudo(
            self, tmp_path: Path) -> None:
        from src.shared.codec.topos_binding import dll_matches_current_arch

        pseudo = tmp_path / "topos_codec.dll"
        pseudo.write_bytes(b"!<arch>\n" + b"\x00" * 64)
        ok, reason = dll_matches_current_arch(pseudo)
        assert not ok
        assert "not_a_pe_image" in reason

    def test_dll_matches_current_arch_rejects_wrong_machine(
            self, tmp_path: Path) -> None:
        """错架构（x86 DLL on AMD64 解释器）必须带原因拒绝，不冒充可加载。"""
        import sys as _sys

        if _sys.platform != "win32":
            pytest.skip("PE machine 对拍仅 Windows 生效")
        from src.shared.codec.topos_binding import dll_matches_current_arch

        wrong = tmp_path / "topos_codec.dll"
        wrong.write_bytes(_synthetic_pe(0x014C))  # i386
        ok, reason = dll_matches_current_arch(wrong)
        assert not ok
        assert "arch_mismatch" in reason
        assert "014C" in reason

    def test_non_dll_platforms_always_match(self, tmp_path: Path) -> None:
        from src.shared.codec.topos_binding import dll_matches_current_arch

        so = tmp_path / "topos_codec.so"
        so.write_bytes(b"ELF...")
        ok, reason = dll_matches_current_arch(so)
        assert ok and reason == ""

    def test_load_library_reports_arch_reason_for_pseudo_dll(
            self, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        from src.shared.codec import topos_binding

        pseudo = tmp_path / "win-release-zig9" / "topos_codec.dll"
        pseudo.parent.mkdir()
        pseudo.write_bytes(b"!<arch>\n" + b"\x00" * 64)
        monkeypatch.delenv("TOPOS_CODEC_LIB", raising=False)
        monkeypatch.setattr(
            topos_binding, "candidate_library_paths", lambda: [pseudo])
        with pytest.raises(topos_binding.ToposCodecError) as ei:
            topos_binding.load_library()
        assert "not_a_pe_image" in str(ei.value)


class TestCandidateDirectoryFilterW01:
    def test_windows_test_build_dirs_excluded(
            self, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """win-half-tests 等测试构建不得进入生产候选（字母序先于正式构建）。"""
        import sys as _sys

        if _sys.platform != "win32":
            pytest.skip("Windows 目录扫描过滤仅 win32 生效")
        from src.shared.codec import topos_binding

        build = tmp_path / "native" / "topos_codec" / "build"
        for sub in ("win-half-tests", "win-release-zig9"):
            d = build / sub
            d.mkdir(parents=True)
            (d / "topos_codec.dll").write_bytes(b"x")
        monkeypatch.setattr(topos_binding, "_repo_root", lambda: tmp_path)
        cands = [c for c in topos_binding.candidate_library_paths()
                 if c.is_file()]
        assert cands, "应至少发现 win-release 候选"
        assert all("win-half-tests" not in str(c) for c in cands)

    def test_win_release_dirs_kept(
            self, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        import sys as _sys

        if _sys.platform != "win32":
            pytest.skip("Windows 目录扫描过滤仅 win32 生效")
        from src.shared.codec import topos_binding

        d = tmp_path / "native" / "topos_codec" / "build" / "win-release-zig9"
        d.mkdir(parents=True)
        (d / "topos_codec.dll").write_bytes(b"x")
        monkeypatch.setattr(topos_binding, "_repo_root", lambda: tmp_path)
        cands = [str(c) for c in topos_binding.candidate_library_paths()
                 if c.is_file()]
        assert any("win-release-zig9" in c for c in cands)




class TestRequiredExportsGateW01:
    def test_missing_required_exports_reports_names(self) -> None:
        """假库对象缺必选导出 → 缺失清单非空（纯函数，全平台）。"""

        class _Full:
            pass

        full = _Full()
        for name in topos_binding._REQUIRED_EXPORTS:
            setattr(full, name, lambda: None)
        assert topos_binding._missing_required_exports(full) == []

        class _Stale:
            pass

        stale = _Stale()  # 一个导出都没有
        missing = topos_binding._missing_required_exports(stale)
        assert missing and set(missing) <= set(topos_binding._REQUIRED_EXPORTS)

    def test_load_library_skips_stale_but_loadable_dll(self, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        """可加载但缺必选导出的陈旧 DLL（win-release-zig7 实案）不得被
        悄然选中——搜索应越过它（本机存在旧构建时才跑，win32-only）。"""
        import sys as _sys

        if _sys.platform != "win32":
            pytest.skip("仅 Windows DLL 实案")
        stale = (Path(__file__).resolve().parents[2]
                 / "native/topos_codec/build/win-release-zig7/topos_codec.dll")
        if not stale.is_file():
            pytest.skip("本机无陈旧 DLL 实案（win-release-zig7）")
        import shutil

        copied = tmp_path / "win-release-zig7" / "topos_codec.dll"
        copied.parent.mkdir()
        shutil.copy2(stale, copied)
        monkeypatch.delenv("TOPOS_CODEC_LIB", raising=False)
        monkeypatch.setattr(
            topos_binding, "candidate_library_paths", lambda: [copied])
        with pytest.raises(topos_binding.ToposCodecError) as ei:
            topos_binding.load_library()
        assert "missing_exports" in str(ei.value)
