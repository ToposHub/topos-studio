"""F14：安装事务持久日志、fsync/目录同步与崩溃恢复一致性。

崩溃注入 = 真实构造"日志有 begin 无收尾"的磁盘状态（模拟进程在事务
中途死亡）；恢复必须按**磁盘事实**收尾，且中断后 current/payload/
provenance 一致。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.shared.plugins.installer import (
    InstallerError,
    LocalPackageInstaller,
    TransactionJournal,
    fsync_directory,
)

HOST = dict(host_api=1, topos_version="3.0.0", platform="darwin", arch="x86_64")


def _pkg(root: Path, version="1.0.0") -> Path:
    folder = root / f"pkg-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.crashy", "display_name": "C", "version": version,
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    (folder / "code.bin").write_bytes(os.urandom(64))
    return folder


def _installer(tmp_path: Path) -> LocalPackageInstaller:
    return LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)


# ---------------------------------------------------------------------------
# 持久日志：全程记账 + 每条 fsync
# ---------------------------------------------------------------------------

def test_journal_records_full_lifecycle(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, "1.0.0"))
    installer.install(_pkg(tmp_path, "2.0.0"))
    events = [item["event"] for item in installer.journal.entries()]
    assert events.count("begin") == 2
    assert events.count("committed") == 2
    tokens = {item["token"] for item in installer.journal.entries()
              if item["event"] == "begin"}
    assert len(tokens) == 2                       # 每事务唯一令牌
    assert installer.journal.pending_tokens() == {}


def test_journal_aborted_on_validation_failure(tmp_path):
    installer = _installer(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "topos.plugin.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(InstallerError):
        installer.install(bad)
    events = [item["event"] for item in installer.journal.entries()]
    assert "aborted" in events or "begin" not in events   # 失败即收尾
    assert installer.journal.pending_tokens() == {}


def test_journal_tail_truncated_by_crash_is_tolerated(tmp_path):
    journal = TransactionJournal(tmp_path / "installed")
    journal.begin("t1", "files.x", "")
    # 崩溃截断：半行写入
    log = tmp_path / "installed" / ".transactions.log"
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "commi')
    assert journal.pending_tokens() == {"t1": journal.pending_tokens()["t1"]}


# ---------------------------------------------------------------------------
# 崩溃恢复：按磁盘事实收尾
# ---------------------------------------------------------------------------

def test_recovery_after_swap_crash_rolls_forward(tmp_path):
    """崩溃于交换后、committed 前：current 有效且摘要一致 → 前滚记账。"""
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, "1.0.0"))
    # 模拟：安装 2.0.0 完整落盘（指针已换）后进程死亡（无 committed）
    installer.install(_pkg(tmp_path, "2.0.0"))
    last_begin = [item for item in installer.journal.entries()
                  if item["event"] == "begin"][-1]
    # 人为抹掉收尾行（模拟崩溃）：重写日志只保留到 begin
    log = tmp_path / "installed" / ".transactions.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    kept = lines[: max(i for i, line in enumerate(lines)
                       if json.loads(line)["token"] == last_begin["token"]
                       and json.loads(line)["event"] == "begin") + 1]
    log.write_text(chr(10).join(kept) + chr(10), encoding="utf-8")
    assert installer.journal.pending_tokens()           # 崩溃态成立

    fresh = _installer(tmp_path)                        # 下一个事务启动
    outcome = fresh.install(_pkg(tmp_path, "3.0.0"))
    assert outcome.outcome == "updated"
    recovered = [item for item in fresh.journal.entries()
                 if item["event"] == "recovered"]
    assert recovered and recovered[-1]["outcome"].startswith("committed:2.0.0")
    # 一致性：current/payload/provenance 校验通过
    assert fresh.current_version("files.crashy") == "3.0.0"


def test_recovery_before_swap_crash_aborts_cleanly(tmp_path):
    """崩溃于交换前：无指针动作 → 恢复记账 aborted，安装根干净。"""
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, "1.0.0"))
    installer.journal.begin("ghost-token", "files.crashy", "1.0.0")
    fresh = _installer(tmp_path)
    fresh.install(_pkg(tmp_path, "1.5.0"))
    recovered = [item for item in fresh.journal.entries()
                 if item["event"] == "recovered"
                 and item["token"] == "ghost-token"]
    assert recovered and recovered[0]["outcome"] == "aborted:before-swap"
    assert fresh.current_version("files.crashy") == "1.5.0"


def test_recovery_with_corrupted_payload_rolls_back(tmp_path):
    """崩溃后 payload 被换坏（摘要不符）：回指 previous_version。"""
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, "1.0.0"))
    installer.install(_pkg(tmp_path, "2.0.0"))
    # 破坏当前 payload（模拟交换后崩溃 + 位腐）并抹掉收尾
    payload = (tmp_path / "installed" / "files.crashy" / "versions" / "2.0.0"
               / "code.bin")
    payload.write_bytes(b"corrupted")
    journal = TransactionJournal(tmp_path / "installed")
    last_begin = [item for item in journal.entries()
                  if item["event"] == "begin"][-1]
    log = tmp_path / "installed" / ".transactions.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    kept = lines[: max(i for i, line in enumerate(lines)
                       if json.loads(line)["token"] == last_begin["token"]
                       and json.loads(line)["event"] == "begin") + 1]
    log.write_text(chr(10).join(kept) + chr(10), encoding="utf-8")

    fresh = _installer(tmp_path)
    fresh.install(_pkg(tmp_path, "3.0.0"))             # 触发恢复
    recovered = [item for item in fresh.journal.entries()
                 if item["event"] == "recovered"]
    assert any(item["outcome"].startswith("rolled_back:1.0.0")
               for item in recovered)
    # 回滚后 1.0.0 完整可用，最终安装 3.0.0 成功
    versions = tmp_path / "installed" / "files.crashy" / "versions"
    assert (versions / "1.0.0" / "code.bin").is_file()
    assert fresh.current_version("files.crashy") == "3.0.0"


def test_recovery_with_no_previous_and_bad_pointer_quarantines(tmp_path):
    """首装崩溃 + 指针坏（无可回退）：坏 payload 隔离、指针摘除。"""
    installer = _installer(tmp_path)
    installer.journal.begin("first-crash", "files.crashy", "")
    root = tmp_path / "installed" / "files.crashy"
    (root / "versions" / "0.9.0").mkdir(parents=True)
    (root / "versions" / "0.9.0" / "junk").write_text("junk", encoding="utf-8")
    os.symlink("versions/0.9.0", root / "current")     # 无 provenance 的坏安装

    fresh = _installer(tmp_path)
    fresh.install(_pkg(tmp_path, "1.0.0"))
    recovered = [item for item in fresh.journal.entries()
                 if item["event"] == "recovered"
                 and item["token"] == "first-crash"]
    outcome = recovered[0]["outcome"]
    assert outcome.startswith("rolled_back:no-previous") or outcome.startswith("aborted")
    quarantine = list(root.glob(".quarantined-first-crash"))
    if quarantine:
        assert (quarantine[0] / "junk").is_file()      # 坏 payload 隔离可查
    assert fresh.current_version("files.crashy") == "1.0.0"


# ---------------------------------------------------------------------------
# fsync/目录同步
# ---------------------------------------------------------------------------

def test_fsync_directory_posix_no_error(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX")
    fsync_directory(tmp_path)                          # 不抛错即通过
    fsync_directory(tmp_path / "not-exist")            # 缺失目录静默（诊断靠日志）


def test_committed_implies_directory_sync_survives(tmp_path):
    """committed 后指针/目录已同步（行为级：重读实例即见一致状态）。"""
    first = _installer(tmp_path)
    first.install(_pkg(tmp_path, "1.0.0"))
    second = _installer(tmp_path)                      # 新实例=新进程视角
    assert second.current_version("files.crashy") == "1.0.0"
    manifest = second.installed_manifest("files.crashy")
    assert manifest is not None and manifest.version == "1.0.0"
