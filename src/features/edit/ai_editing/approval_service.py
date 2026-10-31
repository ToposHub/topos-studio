"""TAEP ApprovalService（M0 安全边界）。

把"批准"从"任意非空字符串"升级为**受信任宿主签发的不透明、一次性、绑定作用域
的 grant**（TAEP §3.3 / §5.6 / §11.6 步骤 11）。

核心问题（审查 M0.1）：

- 旧实现里 ``TransactionService.commit(approval_token=str)`` 只检查 ``is None``；
  ``ToolFacade._apply_plan`` 还会优先读取模型在 ``args`` 里塞入的
  ``approval_token``。任何客户端都能伪造任意 token 触发真实写入。

本模块的不可变约束：

1. ``ApprovalGrant`` 由 ``issue_after_user_confirmation`` 产生——该入口只能由
   宿主（主窗口确认对话框 / 受信任自动化策略）调用；模型 / 脚本 / MCP 客户端拿不到。
2. grant 内含**随机不透明** ``grant_id``（``secrets.token_urlsafe``），不携带任何
   可被模型复用的语义；模型侧永远看不到 grant 本身，只看到"已被宿主附带授权"。
3. ``consume`` 必须原子校验：存在、未过期、未使用、且 ``ApprovalScope`` 的
   action/project/timeline/base_revision/plan_hash/output_grant 完全匹配；成功后
   立即标为 used，**一次性**。
4. grant 不保存 secret / 原始模型文本 / 绝对路径；宿主关闭确认框、切换工程、
   revision 变化、plan hash 改变、超过 TTL 后均 revoke。
5. ``single_use=True`` 是默认；预览 / 导出等可配置 ``single_use=False`` 但仍受
   expiry + scope 约束。

本模块是纯内存状态（进程级）；不落盘、不持久化（持久化由宿主可选，且只持久化
grant_id/scope/expiry/used，绝不持久化 secret）。线程安全由 ``RLock`` 保证。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal


# ---------------------------------------------------------------------- #
# 作用域与 grant
# ---------------------------------------------------------------------- #


ApprovalAction = Literal["commit_plan", "render_preview", "export_file"]

#: 默认 TTL（秒）。commit 类授权默认较短，防止"批准一次后无限期可用"。
DEFAULT_COMMIT_TTL_SECONDS: float = 300.0
DEFAULT_PREVIEW_TTL_SECONDS: float = 600.0
DEFAULT_EXPORT_TTL_SECONDS: float = 600.0


@dataclass(frozen=True)
class ApprovalScope:
    """批准的作用域——必须与将要执行的动作逐字段匹配。

    所有字段都是"绑定对象"（TAEP 总体不变量 §1.3：批准必须绑定 project /
    timeline / base revision / plan hash / 权限范围 / 过期时间）。

    Attributes:
        action: 被批准的动作类别。
        project_id: 工程绑定（跨工程 grant 无效）。
        timeline_id: 时间线绑定（None 表示未约束；commit 路径强烈建议非 None）。
        base_revision: 乐观锁基线（plan 基于 revision）。
        plan_hash: plan 内容 hash（``canonical_json`` 的 sha256）；改变即作废。
        diff_hash: TimelineDiff 内容 hash（``diff.to_dict()`` 的 sha256）。
            绑定"用户看到的那一份 diff"与"将要提交的那一份 diff"（总体不变量 §1.4：
            预览即提交）。None 表示未约束（向后兼容旧 grant）。
        output_grant_id: 导出动作绑定的 PathGrant id（仅 export 动作）。
    """

    action: ApprovalAction
    project_id: str
    timeline_id: str | None = None
    base_revision: int | None = None
    plan_hash: str | None = None
    diff_hash: str | None = None
    output_grant_id: str | None = None

    def matches(self, other: "ApprovalScope") -> bool:
        """逐字段匹配（AUD-12909：签发侧绑定的强约束必须被 expected 复现）。

        ``self`` 是签发侧（grant 携带）的 scope，``other`` 是消费方构造的
        expected scope。规则：

        - ``action`` / ``project_id`` 永远必须相等；
        - 其余字段若**签发侧**非 None，则 expected 必须携带同一值——expected
          缺省（None）视为不匹配。否则 grant 以 ``diff_hash=X`` 签发后，消费方
          只要在 expected 里省略该字段即可绕过绑定，grant 可被用于一份从未
          预览过的 diff（违反总体不变量 §1.4 "预览即提交"）；
        - 签发侧为 None 的字段不约束（向后兼容旧 grant：expected 携带值仍匹配）。
        """
        if self.action != other.action:
            return False
        if self.project_id != other.project_id:
            return False
        for field_name in (
            "timeline_id", "base_revision", "plan_hash", "diff_hash", "output_grant_id",
        ):
            mine = getattr(self, field_name)
            theirs = getattr(other, field_name)
            if mine is None:
                continue
            if mine != theirs:
                return False
        return True


@dataclass(frozen=True)
class ApprovalGrant:
    """受信任宿主签发的不透明、一次性授权 token。

    - ``grant_id``：随机不透明（``secrets.token_urlsafe``）；模型永远看不到它，
      只由宿主在内部传递给 commit 入口。
    - ``single_use``：默认 True；consume 成功后立即失效，不可重放。
    - 不携带 secret / 原始模型文本 / 绝对路径。
    """

    grant_id: str
    scope: ApprovalScope
    expires_at: float
    single_use: bool = True
    issued_by: str = "host"  # 签发来源标识（不含 secret）

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def is_expired_at(self, now: float) -> bool:
        """Return whether this grant has expired at a caller-supplied time.

        ``ApprovalService`` accepts an injectable clock so that TTL handling is
        deterministic in tests and in hosts that use a controlled clock.  Its
        internal checks must use that same clock instead of silently reading the
        system wall clock.
        """
        return now >= self.expires_at


class ApprovalError(Exception):
    """grant 校验失败的基类。携带稳定诊断码（见 ``code``）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GrantNotFound(ApprovalError):
    def __init__(self, grant_id: str) -> None:
        super().__init__(
            "TAEP_PERMISSION_DENIED",
            f"approval grant {grant_id!r} not found or already consumed",
        )


class GrantExpired(ApprovalError):
    def __init__(self, grant_id: str) -> None:
        super().__init__(
            "TAEP_PERMISSION_DENIED",
            f"approval grant {grant_id!r} expired",
        )


class GrantScopeMismatch(ApprovalError):
    def __init__(self, grant_id: str, detail: str) -> None:
        super().__init__(
            "TAEP_PERMISSION_DENIED",
            f"approval grant {grant_id!r} scope mismatch: {detail}",
        )


# ---------------------------------------------------------------------- #
# ApprovalService
# ---------------------------------------------------------------------- #


@dataclass
class _GrantRecord:
    """内部 grant 状态（可变：used 标记）。"""

    grant: ApprovalGrant
    used: bool = False


class ApprovalService:
    """受信任宿主持有的授权签发与校验服务。

    生命周期：

    - 宿主在用户确认后调 ``issue_after_user_confirmation`` 签发 grant；
    - commit / preview / export 入口调 ``consume`` 原子校验并消费；
    - 工程切换 / 关闭时宿主调 ``revoke_all`` 清空。
    """

    def __init__(self, *, time_fn: Any = None) -> None:
        # time_fn 仅用于测试注入时钟；生产用 time.time。
        self._time_fn = time_fn or time.time
        self._records: dict[str, _GrantRecord] = {}
        self._lock = RLock()

    # ---------------- 签发（仅宿主）---------------- #

    def issue_after_user_confirmation(
        self,
        scope: ApprovalScope,
        *,
        ttl_seconds: float | None = None,
        single_use: bool = True,
        issued_by: str = "host",
    ) -> ApprovalGrant:
        """签发一个不透明、一次性 grant。

        **此方法只能由宿主在用户确认对话框/受信任自动化策略后调用。** 模型 /
        MCP 客户端 / 脚本不应持有 ``ApprovalService`` 引用——它们只能通过
        ``EditSession`` / ``ToolFacade`` 走"宿主附带授权"的内部路径。

        Args:
            scope: 绑定作用域（必须含 project_id）。
            ttl_seconds: 有效期；None 时按 action 取默认（commit 300s）。
            single_use: True 时 consume 后立即失效（默认）。
            issued_by: 签发来源标识（不含 secret）。
        """
        if not scope.project_id:
            raise ValueError("ApprovalScope.project_id is required")
        if ttl_seconds is None:
            ttl_seconds = {
                "commit_plan": DEFAULT_COMMIT_TTL_SECONDS,
                "render_preview": DEFAULT_PREVIEW_TTL_SECONDS,
                "export_file": DEFAULT_EXPORT_TTL_SECONDS,
            }.get(scope.action, DEFAULT_COMMIT_TTL_SECONDS)

        grant_id = secrets.token_urlsafe(32)
        grant = ApprovalGrant(
            grant_id=grant_id,
            scope=scope,
            expires_at=self._time_fn() + ttl_seconds,
            single_use=single_use,
            issued_by=issued_by,
        )
        with self._lock:
            self._records[grant_id] = _GrantRecord(grant=grant)
        return grant

    # ---------------- 消费（commit / preview / export 入口）---------------- #

    def consume(self, grant_id: str, expected_scope: ApprovalScope) -> ApprovalGrant:
        """原子校验并消费一个 grant。

        校验顺序：存在 → 未过期 → 作用域匹配 → single_use 时标 used。
        任一失败抛 ``ApprovalError``（携带 ``TAEP_PERMISSION_DENIED`` 码），
        **且绝不标记 used**（重放仍会因原失败原因被拒）。

        成功后：若 ``single_use``，立即标 used（后续 consume 该 id 一律
        ``GrantNotFound``）。
        """
        with self._lock:
            rec = self._records.get(grant_id)
            if rec is None or (rec.grant.single_use and rec.used):
                raise GrantNotFound(grant_id)
            if rec.grant.is_expired_at(self._time_fn()):
                # 过期也清理
                self._records.pop(grant_id, None)
                raise GrantExpired(grant_id)
            if not rec.grant.scope.matches(expected_scope):
                raise GrantScopeMismatch(
                    grant_id,
                    f"expected action={expected_scope.action!r} "
                    f"project={expected_scope.project_id!r} "
                    f"timeline={expected_scope.timeline_id!r} "
                    f"base_revision={expected_scope.base_revision!r} "
                    f"plan_hash={expected_scope.plan_hash!r}",
                )
            if rec.grant.single_use:
                rec.used = True
            return rec.grant

    def validate(self, grant_id: str, expected_scope: ApprovalScope) -> bool:
        """非消费式校验（用于"是否仍有效"的预检，不标 used）。"""
        try:
            with self._lock:
                rec = self._records.get(grant_id)
                if rec is None or (rec.grant.single_use and rec.used):
                    return False
                if rec.grant.is_expired_at(self._time_fn()):
                    return False
                return rec.grant.scope.matches(expected_scope)
        except Exception:
            return False

    # ---------------- 撤销 / 生命周期 ---------------- #

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            return self._records.pop(grant_id, None) is not None

    def revoke_for_project(self, project_id: str) -> int:
        """撤销某工程下的所有 grant（工程切换/关闭时调用）。返回撤销数。"""
        n = 0
        with self._lock:
            for gid in list(self._records):
                if self._records[gid].grant.scope.project_id == project_id:
                    self._records.pop(gid, None)
                    n += 1
        return n

    def revoke_all(self) -> int:
        with self._lock:
            n = len(self._records)
            self._records.clear()
        return n

    # ---------------- 内省（测试 / 审计用）---------------- #

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for r in self._records.values()
                if not r.grant.is_expired_at(self._time_fn())
                and not (r.grant.single_use and r.used)
            )


# ---------------------------------------------------------------------- #
# plan hash 工具（供 commit 入口构造 expected scope）
# ---------------------------------------------------------------------- #


def compute_plan_hash(canonical_json: bytes | str) -> str:
    """对 plan 的 canonical JSON 计算 sha256 hex。

    用于把 grant 绑定到"被用户看到的那一份 plan"（TAEP 总体不变量 §1.4：
    预览即提交——用户看到的 diff 必须由将要提交的同一 plan 得到）。
    """
    import hashlib

    if isinstance(canonical_json, str):
        canonical_json = canonical_json.encode("utf-8")
    return hashlib.sha256(canonical_json).hexdigest()


def compute_diff_hash(diff: Any) -> str:
    """对 TimelineDiff 计算 sha256 hex（绑定"用户看到的 diff == 提交的 diff"）。

    总体不变量 §1.4 要求 Preview 绑定 ``diff_hash``：用户批准的是某一份可见
    diff，提交必须复现同一份 diff。单独 hash diff（而非仅靠 ``plan_hash``）
    可以在 plan 文本相同但底层 timeline 已变化（例如他人在预览与提交之间手工
    编辑）时也判定 stale。

    Args:
        diff: 任意实现了 ``to_dict()`` 的对象（``TimelineDiff``），或一个
            ``Mapping``。序列化为 ``sort_keys`` JSON 后取 sha256，保证同一份
            diff 总产生同一 hash。

    Note:
        本函数故意放在 ``approval_service``（不 import ``TimelineDiff``），避免
        ``transaction_service`` ↔ ``approval_service`` 循环 import。运行时只
        鸭子类型地调用 ``diff.to_dict()``。
    """
    import hashlib
    import json

    if hasattr(diff, "to_dict"):
        payload = diff.to_dict()
    else:
        payload = diff
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
