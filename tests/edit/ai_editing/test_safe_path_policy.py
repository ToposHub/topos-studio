"""TAEP M0.4 — SafePathPolicy + AuditStore 路径安全测试。

验收（审查 M0.4 / 总体不变量 §1.9）：

- 集中 SafePathPolicy：resolve_within 断言目标仍在授权根内。
- 拒绝 ``..`` 逃逸、symlink 跳转、跨根。
- safe_filename：合法 ID 保留，其余 sha256。
- AuditStore 落盘经 SafePathPolicy；I/O 失败降级为 warning（不误报编辑失败）。
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.safe_path_policy import (
    PathPolicyError,
    SafePathPolicy,
    assert_within_root,
    is_path_safe,
    safe_filename,
)
from src.features.edit.ai_editing.audit_store import AuditEntry, AuditStore


# ================================================================
# safe_filename
# ================================================================


def test_safe_filename_keeps_readable_ids():
    assert safe_filename("plan-abc-123") == "plan-abc-123"
    assert safe_filename("a") == "a"


def test_safe_filename_hashes_unsafe_tokens():
    out = safe_filename("../../etc/passwd")
    # 不含路径分隔符，长度 = sha256 hex
    assert "/" not in out and ".." not in out
    assert len(out) == 64


def test_safe_filename_empty():
    out = safe_filename("")
    assert len(out) == 64


def test_safe_filename_stable():
    assert safe_filename("../x") == safe_filename("../x")


# ================================================================
# SafePathPolicy.resolve_within
# ================================================================


def test_resolve_within_accepts_relative_inside_root(tmp_path):
    policy = SafePathPolicy(tmp_path)
    resolved = policy.resolve_within("audit/abc.json")
    assert resolved == (tmp_path / "audit" / "abc.json").resolve()


def test_resolve_within_rejects_dotdot_escape(tmp_path):
    policy = SafePathPolicy(tmp_path)
    with pytest.raises(PathPolicyError):
        policy.resolve_within("audit/../../etc/passwd")


def test_resolve_within_rejects_absolute_outside_root(tmp_path):
    policy = SafePathPolicy(tmp_path)
    with pytest.raises(PathPolicyError):
        policy.resolve_within("/etc/passwd")


def test_resolve_within_rejects_symlink_escape(tmp_path):
    """symlink 指向根外 → resolve() 展开后跨根 → 拒绝。"""
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    policy = SafePathPolicy(tmp_path)
    with pytest.raises(PathPolicyError):
        policy.resolve_within("escape/secret.json")


def test_resolve_within_must_exist(tmp_path):
    policy = SafePathPolicy(tmp_path)
    (tmp_path / "exists.txt").write_text("x")
    assert policy.resolve_within("exists.txt", must_exist=True)
    with pytest.raises(PathPolicyError):
        policy.resolve_within("missing.txt", must_exist=True)


def test_join_within_normalizes_each_segment(tmp_path):
    policy = SafePathPolicy(tmp_path)
    # 即使传入带 ../ 的段，join_within 先 safe_filename 再 resolve。
    resolved = policy.join_within("audit", "plan-1")
    assert resolved == (tmp_path / "audit" / "plan-1").resolve()


# ================================================================
# 便捷函数
# ================================================================


def test_assert_within_root_returns_path(tmp_path):
    p = assert_within_root("x.json", tmp_path)
    assert p == (tmp_path / "x.json").resolve()


def test_is_path_safe_predicate(tmp_path):
    assert is_path_safe("x.json", tmp_path) is True
    assert is_path_safe("../outside", tmp_path) is False


# ================================================================
# AuditStore 经 SafePathPolicy
# ================================================================


def test_audit_store_traversal_plan_id_blocked(tmp_path):
    """M0.4：plan_id 带 ../ 不能逃出 sink_dir；落盘到 hash 文件名。"""
    store = AuditStore(sink_dir=tmp_path)
    entry = AuditEntry(
        timestamp=0.0, tool="t", plan_id="../../outside", applied=False,
        new_revision=None,
    )
    store.record(entry)
    # 不应在 sink_dir 之外产生文件
    assert not (tmp_path.parent / "outside.json").exists()
    # 应在 sink_dir 内产生一个 hash 文件名
    assert list(tmp_path.glob("*.json"))


def test_audit_store_io_failure_degrades_to_warning(tmp_path):
    """M0.4：落盘失败降级 warning，内存条目仍保留（不误报编辑失败）。"""
    # 把 sink_dir 设为一个无法创建的路径（文件而非目录）
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")  # 同名文件存在 → mkdir 失败
    store = AuditStore(sink_dir=blocker)
    entry = AuditEntry(
        timestamp=0.0, tool="t", plan_id="p1", applied=True, new_revision=1,
    )
    # 不抛
    store.record(entry)
    # 内存条目仍在
    assert len(store.list_entries()) == 1


def test_audit_store_readonly_sink_degrades(tmp_path):
    """M0.4：只读目录写入失败 → warning，不抛，内存保留。"""
    ro = tmp_path / "ro"
    ro.mkdir()
    try:
        ro.chmod(0o555)  # 只读
    except (OSError, PermissionError):
        pytest.skip("cannot make readonly on this platform")
    store = AuditStore(sink_dir=ro)
    entry = AuditEntry(
        timestamp=0.0, tool="t", plan_id="p1", applied=True, new_revision=1,
    )
    try:
        store.record(entry)  # 不抛
        assert len(store.list_entries()) == 1
    finally:
        ro.chmod(0o755)
