"""W10（计划 R03/R13 · M20/W10）：跨机归档 Windows 文件系统适配测试。

覆盖计划验收的文件系统/文件名子集（Mac↔Windows 往返的 Windows 侧）：

- 大小写冲突（A.mov/a.mov）：大小写不敏感文件系统（NTFS/APFS）上
  不得静默覆盖/跨素材错连——两份各自完整存活；
- Windows 保留名（CON/PRN/COM1…带扩展名同样非法）：目标名净化后可
  落盘（不指向设备）；
- 中文/空格名：CJK 保留、空白净化；
- NFC/NFD：同一可见名的两种分解形式各自成档不互撞（净化产生稳定名）；
- 嵌套序列（目录素材）：目录聚合哈希的相对路径统一 "/"——跨平台同
  内容同哈希（分隔符差异不算内容变化）；
- 可移植索引：报告 archived_path 只含 "/"，hash_algo 固化；
- 深路径（长于 MAX_PATH 的目标树）：Windows 上收集不因路径长度失败；
- Windows 文件锁失败恢复：复制失败按素材降级为 copy_failed 记录，
  报告仍产出（AUD-17202 契约在 Windows 锁场景的验证）。

脱机重开/输出同像素与 PCM 的完整往返、LUT/字体/外录闭包由 M20 共享
归档 schema（Mac 集成者持有）与 W07 黄金工程覆盖——本文件为 Windows
平台适配与可移植性契约的证据。
"""
from __future__ import annotations

import hashlib
import os
import sys
import unicodedata
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject, MediaAsset
from features.edit.core.project_archive import (
    STATUS_COPIED,
    STATUS_COPY_FAILED,
    _sanitize_name,
    _sha256_of,
    _unique_dest_name,
    collect_project,
)


def _asset(asset_id: str, path: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=str(path),
        media_type="video",
        display_name=os.path.basename(str(path)),
        metadata={},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project(*assets: MediaAsset) -> EditProject:
    return EditProject(
        project_id="w10", name="w10", assets={a.asset_id: a for a in assets})


class TestCaseInsensitiveCollision:
    """A.mov / a.mov：大小写不敏感文件系统不得静默错连。"""

    def test_case_variant_names_both_survive(self, tmp_path):
        # 注意：源文件必须分属不同目录——大小写不敏感文件系统（NTFS/
        # APFS）上同一目录放不下 A.mov 与 a.mov（第二次写直接覆盖第一
        # 个，测试前提自毁）。真实场景正是 Mac（大小写敏感盘）归档到
        # Windows：两个不同源目录的同名异 case 文件落入同一 media/。
        d1 = tmp_path / "mac-hd"
        d2 = tmp_path / "ext-volume"
        d1.mkdir(); d2.mkdir()
        (d1 / "A.mov").write_bytes(b"AAA-content")
        (d2 / "a.mov").write_bytes(b"aaa-content")
        project = _project(
            _asset("up", d1 / "A.mov"),
            _asset("low", d2 / "a.mov"),
        )

        archived, report = collect_project(project, target_dir=str(tmp_path / "arc"))

        assert report.passed
        assert len(report.copied) == 2
        # 两份副本都存在且内容各自正确（未被第二个覆盖）
        names = {r.archived_path for r in report.copied}
        assert len(names) == 2, f"目标名未按大小写不敏感去重: {names}"
        by_asset = {r.asset_id: r for r in report.copied}
        for asset_id, content in (("up", b"AAA-content"), ("low", b"aaa-content")):
            archived_file = tmp_path / "arc" / by_asset[asset_id].archived_path
            assert archived_file.read_bytes() == content, (
                f"{asset_id} 的归档副本被另一同名异case素材覆盖/错连")
            # 重写后的工程路径确实指向该副本
        for asset in archived.assets.values():
            assert (tmp_path / "arc" / "media") in Path(asset.path).parents

    def test_unique_dest_name_casefold_dedup(self):
        used: set[str] = set()
        first = _unique_dest_name("clip.mov", used)
        second = _unique_dest_name("CLIP.mov", used)
        assert first != second
        assert first.casefold() != second.casefold()


class TestWindowsReservedNames:
    def test_sanitize_reserved_names(self):
        for bad in ("CON", "CON.mov", "prn.wav", "com1.png", "LPT9"):
            out = _sanitize_name(bad, fallback="asset_x")
            stem = out.split(".", 1)[0].upper()
            assert stem not in {
                "CON", "PRN", "AUX", "NUL"} | {
                f"COM{i}" for i in range(1, 10)} | {
                f"LPT{i}" for i in range(1, 10)}, f"{bad} -> {out} 仍非法"

    @pytest.mark.skipif(sys.platform != "win32", reason="win32 reserved names")
    def test_collect_reserved_named_asset_lands_on_disk(self, tmp_path):
        src = tmp_path / "NUL.mov"  # 源名合法是测试假设外的；用 display_name 构造
        src.write_bytes(b"reserved")
        project = _project(_asset(
            "resv", src, display_name="NUL.mov"))
        archived, report = collect_project(
            project, target_dir=str(tmp_path / "arc"))
        assert report.passed, report.warnings
        rec = report.copied[0]
        dest = tmp_path / "arc" / rec.archived_path
        assert dest.is_file()  # 真实落盘（未指向设备）
        assert dest.read_bytes() == b"reserved"

    def test_trailing_dot_space_stripped(self):
        out = _sanitize_name("clip. ", fallback="f")
        assert not out.endswith(".") and not out.endswith(" ")


class TestUnicodeNames:
    def test_cjk_kept_spaces_sanitized(self):
        out = _sanitize_name("外录 中文 素材.wav", fallback="f")
        assert "外录" in out and "素材" in out
        assert " " not in out

    def test_nfc_nfd_forms_do_not_collide(self, tmp_path):
        # 同一可见名 "café"：NFC（单码位）与 NFD（e+组合符）净化后须稳定
        # 且可区分（组合符净化为 _，两键不同 → 不互撞不覆盖）
        nfc = unicodedata.normalize("NFC", "café project.mov")
        nfd = unicodedata.normalize("NFD", "café project.mov")
        used: set[str] = set()
        a = _unique_dest_name(_sanitize_name(nfc, "f"), used)
        b = _unique_dest_name(_sanitize_name(nfd, "f"), used)
        assert a != b


class TestPortableDirectoryHash:
    def test_directory_hash_uses_forward_slash_relpath(self, tmp_path):
        seq_dir = tmp_path / "seq"
        (seq_dir / "sub").mkdir(parents=True)
        (seq_dir / "sub" / "0001.png").write_bytes(b"one")
        (seq_dir / "0000.png").write_bytes(b"zero")

        # 参照（"Mac 侧"算法）：rel 一律 "/"；顺序 = os.walk 根文件
        # 先于子目录（seq/0000.png 先于 seq/sub/0001.png）
        h = hashlib.sha256()
        for rel, content in (("0000.png", b"zero"), ("sub/0001.png", b"one")):
            h.update(rel.encode("utf-8") + b"\0")
            h.update(content)
        expected = h.hexdigest()

        assert _sha256_of(str(seq_dir)) == expected, (
            "目录聚合哈希必须与 '/' 分隔的跨平台参照一致"
            "（分隔符差异不得算成内容变化）")


class TestPortableReport:
    def test_report_paths_portable_and_hash_algo_pinned(self, tmp_path):
        (tmp_path / "m.mov").write_bytes(b"m")
        project = _project(_asset("m1", tmp_path / "m.mov"))
        _, report = collect_project(project, target_dir=str(tmp_path / "arc"))

        data = report.to_dict()
        assert data["hash_algo"] == "sha256"
        for rec in data["records"]:
            if rec["archived_path"]:
                assert "\\" not in rec["archived_path"], (
                    f"可移植索引不得携带平台分隔符: {rec['archived_path']}")


class TestLongPaths:
    @pytest.mark.skipif(sys.platform != "win32", reason="win32 long paths")
    def test_deep_target_tree_collects(self, tmp_path):
        # 目标树 ~200 字符 + 120 字符名：MAX_PATH 压力（真实 NTFS）
        deep = tmp_path
        for part in ("r" * 40, "s" * 40, "t" * 40, "u" * 40, "v" * 38):
            deep = deep / part
        long_name = "L" * 110 + ".mov"
        src = tmp_path / "src.mov"
        src.write_bytes(b"long-path")
        project = _project(_asset("lp", src, display_name=long_name))

        _, report = collect_project(project, target_dir=str(deep))
        assert report.passed, report.warnings
        assert report.copied[0].status == STATUS_COPIED


class TestWindowsFileLockRecovery:
    def test_locked_source_degrades_to_copy_failed_report_produced(
            self, tmp_path, monkeypatch):
        import features.edit.core.project_archive as pa

        src = tmp_path / "locked.mov"
        src.write_bytes(b"locked")

        real_copy2 = pa.shutil.copy2

        def _selective_copy2(src, dst, **kw):
            if os.path.basename(str(src)) == "locked.mov":
                raise PermissionError(
                    5, "The process cannot access the file (locked)")
            return real_copy2(src, dst, **kw)

        monkeypatch.setattr(pa.shutil, "copy2", _selective_copy2)
        (tmp_path / "ok.mov").write_bytes(b"ok")
        project = _project(
            _asset("lok", src), _asset("ok1", tmp_path / "ok.mov"))

        _, report = collect_project(project, target_dir=str(tmp_path / "arc"))

        by_id = {r.asset_id: r for r in report.records}
        assert by_id["lok"].status == STATUS_COPY_FAILED  # 降级不中断
        assert by_id["ok1"].status == STATUS_COPIED  # 其余素材继续
        assert not report.passed  # 失败如实阻断"发布"判定
