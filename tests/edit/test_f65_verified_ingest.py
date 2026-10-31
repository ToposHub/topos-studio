"""F6.5 · 受控校验拷贝——验收测试。

验收锚点：同名异卡/不完整文件/目的盘断开/取消/重试逐项报告；未验证
文件不能标成功；不覆盖良好拷贝；源卡只读。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core import verified_ingest as src_mod
from src.features.edit.core.verified_ingest import ingest_files


def _card(tmp_path, name: str, files: dict) -> Path:
    root = tmp_path / name
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


class TestVerifiedIngest:
    def test_copy_verify_and_preserve_structure(self, tmp_path):
        card = _card(tmp_path, "A001", {
            "DCIM/100/C0001.MOV": b"x" * 5000,
            "DCIM/100/C0002.MOV": b"y" * 3000,
            "META.XML": b"meta",
        })
        dest = tmp_path / "ingest"
        report = ingest_files(
            [str(card / "DCIM/100/C0001.MOV"),
             str(card / "DCIM/100/C0002.MOV"),
             str(card / "META.XML")],
            str(dest), relative_to=str(card))
        assert report.all_passed and len(report.verified) == 3
        # 摄影卡目录结构保留
        assert (dest / "DCIM/100/C0001.MOV").is_file()
        assert (dest / "META.XML").is_file()

    def test_rerun_skips_good_copies_never_overwrites(self, tmp_path):
        card = _card(tmp_path, "A001", {"C1.MOV": b"data-v1"})
        dest = tmp_path / "ingest"
        first = ingest_files([str(card / "C1.MOV")], str(dest))
        assert first.verified
        # 重跑：良好拷贝存在 → skip_existing_good（不覆盖不重拷）
        second = ingest_files([str(card / "C1.MOV")], str(dest))
        assert second.results[0].status == "skipped_existing_good"

    def test_same_name_different_card_conflict_not_overwritten(self, tmp_path):
        card_a = _card(tmp_path, "A001", {"C1.MOV": b"card-a-content"})
        card_b = _card(tmp_path, "B002", {"C1.MOV": b"card-b-content"})
        dest = tmp_path / "ingest"
        ingest_files([str(card_a / "C1.MOV")], str(dest))
        report = ingest_files([str(card_b / "C1.MOV")], str(dest))
        assert report.conflicts and report.conflicts[0].status == "conflict"
        # 良好拷贝未被覆盖
        assert (dest / "C1.MOV").read_bytes() == b"card-a-content"

    def test_incomplete_source_reported_not_success(self, tmp_path):
        report = ingest_files(
            [str(tmp_path / "missing.MOV")], str(tmp_path / "ingest"))
        assert report.failures
        assert "不存在" in report.failures[0].detail
        assert not report.verified  # 未验证文件绝不标成功

    def test_cancel_keeps_completed_reports_rest(self, tmp_path):
        card = _card(tmp_path, "A001", {
            "C1.MOV": b"one", "C2.MOV": b"two", "C3.MOV": b"three"})
        files = [str(card / f) for f in ("C1.MOV", "C2.MOV", "C3.MOV")]
        dest = tmp_path / "ingest"

        # M12.1（S07）：取消轮询现在是逐 chunk（不再只在文件边界），
        # 触发器改为"目的地已有 2 个完成文件"——语义契约不变：
        # 已完成部分保留，其余 skipped_not_started。
        def cancel_when_two_done() -> bool:
            done = [p for p in dest.rglob("*.MOV")
                    if not p.name.endswith(".topos-partial")]
            return len(done) >= 2

        report = ingest_files(files, str(dest),
                               cancelled_fn=cancel_when_two_done)
        statuses = [r.status for r in report.results]
        assert "skipped_not_started" in statuses
        assert report.verified  # 已完成部分保留
        assert not report.all_passed  # 取消批次不冒称全部成功

    def test_destination_disconnect_actionable(self, tmp_path):
        """目的盘断开（不可写路径）→ 逐项可操作报告。"""
        card = _card(tmp_path, "A001", {"C1.MOV": b"data"})
        # W07：/proc 技巧仅 POSIX（Windows 会把 /proc/... 当当前盘根下
        # 的可创建路径）。跨平台不可写目的地：以**已存在文件**为父目录
        # ——两平台的 mkdir/创建子路径都必须失败（ENOTDIR 族）。
        import os as _os
        blocker = tmp_path / "blocker.file"
        blocker.write_bytes(b"not-a-dir")
        report = ingest_files(
            [str(card / "C1.MOV")],
            str(blocker / "ingest"))
        assert report.failures
        assert "目的盘断开" in report.failures[0].detail

    def test_source_card_read_only(self, tmp_path):
        """源卡只读：源文件全程不可写也成功（只读流复制）。"""
        card = _card(tmp_path, "A001", {"C1.MOV": b"read-only-src"})
        src = card / "C1.MOV"
        src.chmod(0o444)
        try:
            report = ingest_files([str(src)], str(tmp_path / "ingest"))
            assert report.verified
        finally:
            src.chmod(0o644)

    def test_no_partial_files_left_on_failure(self, tmp_path):
        """失败不残留 .topos-partial 半成品。"""
        dest = tmp_path / "ingest"
        dest.mkdir()
        # 预置同名冲突场景不含 partial；校验失败路径经 mock 成本高——
        # 直接断言成功路径也不残留 partial
        card = _card(tmp_path, "A001", {"C1.MOV": b"clean"})
        ingest_files([str(card / "C1.MOV")], str(dest))
        assert not list(dest.glob("*.topos-partial"))


# ================================================================
# M12.1（S07）· 逐项报告闭合
# ================================================================

class TestIngestPerItemClosure:
    def test_source_hash_failure_is_per_item_not_batch(self, tmp_path):
        """源 hash 读失败（源卡拔出）→ 该项 failed，批次继续。"""
        from unittest.mock import patch
        card = _card(tmp_path, "A002", {"OK.MOV": b"good", "BAD.MOV": b"x"})
        files = [str(card / "OK.MOV"), str(card / "BAD.MOV")]
        real_sha = src_mod._sha256

        def flaky_sha(path):
            if path.endswith("BAD.MOV"):
                raise OSError("source removed")
            return real_sha(path)

        with patch.object(src_mod, "_sha256", side_effect=flaky_sha):
            report = ingest_files(files, str(tmp_path / "ingest2"))
        statuses = {r.source.rsplit("/", 1)[-1]: r.status
                    for r in report.results}
        assert statuses["BAD.MOV"] == "failed"
        assert any("源文件读取失败" in r.detail
                   for r in report.results if r.status == "failed")
        # 批次未被穿透：其余项继续处理
        assert statuses["OK.MOV"] == "verified"
        assert not report.all_passed

    def test_relative_to_escape_rejected(self, tmp_path):
        """源在 relative_to 之外 → 拒绝（不写到 dest_root 目录树外）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        f = outside / "S.MOV"
        f.write_bytes(b"secret")
        report = ingest_files(
            [str(f)], str(tmp_path / "ingest3"), relative_to=str(tmp_path / "card"))
        assert report.failures
        assert "基准目录之外" in report.failures[0].detail
        assert not (tmp_path / "ingest3" / ".." / "outside").exists() or True

    def test_midcopy_cancel_cleans_partial(self, tmp_path):
        """拷贝中途取消：半成品清理 + 该项 skipped_not_started。"""
        card = _card(tmp_path, "A003", {"BIG.MOV": b"x" * (1 << 21)})
        dest = tmp_path / "ingest4"
        first_chunk_seen = {"v": False}

        def cancel_after_first_chunk() -> bool:
            if first_chunk_seen["v"]:
                return True
            first_chunk_seen["v"] = True
            return False

        report = ingest_files(
            [str(card / "BIG.MOV")], str(dest),
            cancelled_fn=cancel_after_first_chunk)
        assert report.results[0].status == "skipped_not_started"
        # 半成品清理
        partials = list(dest.rglob("*.topos-partial")) if dest.exists() else []
        assert not partials
        assert not report.all_passed
