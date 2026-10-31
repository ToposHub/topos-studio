"""TAEP M10 — Tool-call 审计日志（§4.2 step 1 / §11.2 完成检查 / §12.1 安全测试矩阵）。

MCP / toposctl / LLM tool-calling loop 三路都经过 ``ToolFacade.invoke()``，本模块统一
记录每次工具调用，满足：

- §4.2 step 1："每个 tool 有……审计与 JSON serializable 测试"。
- §11.2 完成检查："相同请求经 UI/CLI/MCP 得到相同 diagnostics"——可追溯验证。
- §12.1 安全测试矩阵："越权 tool、伪造 grant、重放"——日志是取证依据。

设计（遵循 .clinerules / §0 / §1.2）：

- **不记 secret / 绝对路径**（§0 红线第四条）。``args_summary`` / ``result_summary``
  是脱敏摘要（截断 + 不含 key/path），不是原始 args/result。
- **JSON serializable**（§4.2 step 1）：``to_dict`` 后可 ``json.dumps``。
- **append-only**：``ToolCallLog`` 只追加，不修改历史（审计不可篡改）。
- **client 维度**：记录调用来源（``mcp`` / ``cli`` / ``tool_calling_loop`` / ``ui``），
  便于区分外部客户端与内部循环。
- **approval 可追溯**：记录是否消耗了 ``ApprovalGrant``（write 类工具）。

与既有 ``AuditStore`` 的关系：``AuditStore`` 是 per-plan apply 事件（粒度=一次提交）；
本模块是 per-tool-call（粒度=单次 invoke，含 search/validate 这类非 apply 调用）。两者
互补，互不替代。``ToolFacade.__init__`` 的 ``audit_sink`` callback 可路由到本日志。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from src.features.edit.ai_editing.privacy_guard import redact_text


# ---------------------------------------------------------------------- #
# ToolCallRecord —— 单次工具调用记录
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolCallRecord:
    """单次 ``ToolFacade.invoke`` 的审计记录。

    红线：``args_summary`` / ``result_summary`` 是**脱敏摘要**（截断 + 不含 secret/
    绝对路径），不是原始 payload。``approval_grant_id`` 仅记 grant id（不记 token 值）。
    """

    call_id: str                       # 唯一 id（uuid 或序列）
    timestamp: float                   # epoch seconds
    tool_name: str                     # 如 "topos.search_assets"
    client: str = "ui"                 # mcp / cli / tool_calling_loop / ui
    client_session_id: str = ""        # MCP session / CLI invocation / loop run id
    skill_id: str = ""                 # 触发此调用的 Skill（可空）
    args_summary: str = ""             # 脱敏参数摘要（截断，≤200 字符）
    result_summary: str = ""           # 脱敏结果摘要（截断，≤200 字符）
    succeeded: bool = True             # invoke 是否成功（无 error diagnostic）
    approval_grant_id: str = ""        # 消耗的 ApprovalGrant id（write 类；空=未用）
    diagnostics_count: int = 0         # 返回的诊断数（error/warning/info 不区分）
    error_codes: tuple[str, ...] = ()  # TAEP 诊断码（如 TAEP_PERMISSION_DENIED）
    duration_ms: int = 0               # 调用耗时（毫秒）

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "timestamp": self.timestamp,
            "tool_name": self.tool_name,
            "client": self.client,
            "client_session_id": self.client_session_id,
            "skill_id": self.skill_id,
            "args_summary": self.args_summary,
            "result_summary": self.result_summary,
            "succeeded": self.succeeded,
            "approval_grant_id": self.approval_grant_id,
            "diagnostics_count": self.diagnostics_count,
            "error_codes": list(self.error_codes),
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------- #
# 摘要工具（脱敏 + 截断）
# ---------------------------------------------------------------------- #


_SUMMARY_MAX = 200


# 顶层 + 嵌套结构里出现即视为敏感的键（小写匹配）。含路径/凭证/密钥/网络端点。
_SENSITIVE = {
    "api_key", "apikey", "token", "authorization", "auth", "secret", "password",
    "passwd", "credential", "credential_ref", "credentials",
    "path", "output_path", "file_path", "filepath", "filename",
    "src", "dst", "source", "destination",
    "url", "endpoint", "uri", "host",
}

# AUD-14709：值级脱敏补充——privacy_guard.redact_text 覆盖 secret 形状与 unix
# 绝对路径（/Users、/home、/private、/tmp），这里补 Windows 盘符绝对路径。
_WINDOWS_ABS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[a-z]:\\[^\s,;\"']+")


def _scrub_text(text: str) -> str:
    """字符串值级脱敏：secret 形状 + unix/windows 绝对路径 → 占位符。

    复用同包 ``privacy_guard.redact_text``（与 release_evidence 的 AUD-14205 修复
    同一来源），避免两套正则漂移。
    """
    return _WINDOWS_ABS_PATH.sub("[PATH_REDACTED]", redact_text(text))


def _redact(value: Any) -> Any:
    """递归脱敏（镜像 ``tool_facade.sanitize_untrusted`` 的 isinstance 分发）。

    dict：敏感 key 的值替换为 ``<redacted>``，其余 key 递归其值；
    list/tuple：逐元素递归；字符串叶子做值级脱敏（AUD-14709——键名不在黑名单
    时，值中内嵌的绝对路径/secret 也不得进入审计日志）；其余标量原样返回。
    保留结构形态（哪些 key），只替换敏感叶子值——审计摘要需要看出 args 的形状，
    但不能带 secret/path。
    """
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if str(k).lower() in _SENSITIVE else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v) for v in value)
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def summarize_args(args: dict[str, Any] | None) -> str:
    """把 args 脱敏成短摘要（截断，递归去掉 key/path/secret 字段）。

    红线（§0 第四条）：不记 secret / 绝对路径。``api_key`` / ``token`` / ``path`` /
    ``output_path`` / ``file_path`` / ``url`` 等键（含嵌套层，如
    ``{"request": {"output_path": ...}}``）的值替换为 ``<redacted>``。
    """
    if not args:
        return ""
    safe = _redact(args)
    s = repr(safe)
    return s[:_SUMMARY_MAX] + ("…" if len(s) > _SUMMARY_MAX else "")


def summarize_result(result: dict[str, Any] | None) -> str:
    """把 result 脱敏成短摘要（截断，只取关键标志）。

    红线（§0 第四条 / AUD-14709）：``error``/``note`` 等字符串值先经值级脱敏
    ——facade 的 ``error`` 常来自异常消息（如 FileNotFoundError），内嵌绝对路径。
    """
    if not result:
        return ""
    # 只记 ok / 顶层键 / segments 数 / artifact 是否非空，不展开内容
    bits: list[str] = []
    if "ok" in result:
        bits.append(f"ok={result['ok']}")
    for k in ("new_revision", "artifact_id", "note", "error"):
        if k in result and result[k] is not None:
            val = _scrub_text(str(result[k]))
            bits.append(f"{k}={val[:60]}")
    if "segments" in result and isinstance(result["segments"], list):
        bits.append(f"segments={len(result['segments'])}")
    if "diagnostics" in result and isinstance(result["diagnostics"], list):
        bits.append(f"diagnostics={len(result['diagnostics'])}")
    s = ", ".join(bits) if bits else _scrub_text(repr(result))
    return s[:_SUMMARY_MAX] + ("…" if len(s) > _SUMMARY_MAX else "")


def extract_error_codes(result: dict[str, Any] | None) -> tuple[str, ...]:
    """从 result 的 diagnostics 提取 error 级 TAEP 诊断码。"""
    if not result:
        return ()
    diags = result.get("diagnostics") or []
    codes: list[str] = []
    for d in diags:
        if isinstance(d, dict) and d.get("severity") == "error" and d.get("code"):
            codes.append(str(d["code"]))
    return tuple(codes)


# ---------------------------------------------------------------------- #
# ToolCallLog —— append-only 日志
# ---------------------------------------------------------------------- #


class ToolCallLog:
    """append-only 工具调用日志（§4.2 step 1 审计）。

    线程安全（AUD-14710）：内部一把 ``threading.Lock`` 保护 counter 与记录
    列表——``next_call_id`` 单调唯一（不重复）、``record`` 的 append+FIFO 淘汰
    原子（不丢帧）。MCP 网关请求线程 / agent loop / UI 并发写入时无需调用方
    在 invoke 外自行串行化。``records``/查询方法返回锁内快照。
    """

    def __init__(self, *, max_entries: int = 10_000) -> None:
        # AUD-14711：FIFO 淘汰改用 deque(maxlen=...)——饱和后每条记录 O(1)
        # 淘汰最旧（deque 从另一端弹出），取代 list.pop(0) 的 O(n) 整体搬移
        #（万条上限下每次写入曾搬移 ~万条）。锁语义（AUD-14710）不变。
        self._records: deque[ToolCallRecord] = deque(maxlen=max(1, int(max_entries)))
        self._max = max_entries
        self._counter = 0
        self._lock = threading.Lock()

    def record(self, rec: ToolCallRecord) -> ToolCallRecord:
        """追加一条记录。超过 ``max_entries`` 时丢弃最旧的（FIFO）。"""
        with self._lock:
            self._records.append(rec)
        return rec

    def next_call_id(self) -> str:
        """生成下一个 call_id（单调递增且唯一，便于排序）。"""
        with self._lock:
            self._counter += 1
            return f"tc-{self._counter:08d}"

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def records(self) -> tuple[ToolCallRecord, ...]:
        """全部记录（只读快照）。"""
        with self._lock:
            return tuple(self._records)

    def clear(self) -> None:
        """清空（仅测试/重置用）。"""
        with self._lock:
            self._records.clear()
            self._counter = 0

    # ---------------- 查询 ---------------- #

    def by_client(self, client: str) -> tuple[ToolCallRecord, ...]:
        return tuple(r for r in self.records() if r.client == client)

    def by_tool(self, tool_name: str) -> tuple[ToolCallRecord, ...]:
        return tuple(r for r in self.records() if r.tool_name == tool_name)

    def by_skill(self, skill_id: str) -> tuple[ToolCallRecord, ...]:
        return tuple(r for r in self.records() if r.skill_id == skill_id)

    def errors(self) -> tuple[ToolCallRecord, ...]:
        """失败的调用（取证用）。"""
        return tuple(r for r in self.records() if not r.succeeded)

    def to_dict(self) -> dict[str, Any]:
        records = self.records()
        return {
            "count": len(records),
            "records": [r.to_dict() for r in records],
        }


# ---------------------------------------------------------------------- #
# record_invoke helper —— 把 ToolFacade.invoke 包成审计记录
# ---------------------------------------------------------------------- #


def record_invoke(
    log: ToolCallLog,
    invoke_fn: Callable[..., dict[str, Any]],
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    client: str = "ui",
    client_session_id: str = "",
    skill_id: str = "",
    approval_grant_id: str = "",
    **invoke_kwargs: Any,
) -> tuple[dict[str, Any], ToolCallRecord]:
    """调用 ``invoke_fn``（通常是 ``ToolFacade.invoke``）并自动记审计。

    返回 ``(result, record)``。即使 invoke 抛异常也记一条 failed 记录（不吞异常，
    重新 raise）。脱敏摘要由 ``summarize_args`` / ``summarize_result`` 保证。
    """
    call_id = log.next_call_id()
    start = time.monotonic()
    succeeded = True
    result: dict[str, Any] = {}
    try:
        result = invoke_fn(tool_name, args, **invoke_kwargs)
        return result, _finalize(
            log, call_id, tool_name, args, result, client, client_session_id,
            skill_id, approval_grant_id, succeeded, start,
        )
    except Exception:
        succeeded = False
        _finalize(
            log, call_id, tool_name, args, {"ok": False, "error": "invoke raised"},
            client, client_session_id, skill_id, approval_grant_id, succeeded, start,
        )
        raise


def _finalize(
    log: ToolCallLog, call_id: str, tool_name: str,
    args: dict[str, Any] | None, result: dict[str, Any],
    client: str, client_session_id: str, skill_id: str,
    approval_grant_id: str, succeeded: bool, start: float,
) -> ToolCallRecord:
    duration_ms = int((time.monotonic() - start) * 1000)
    diags = result.get("diagnostics") if isinstance(result, dict) else None
    diag_count = len(diags) if isinstance(diags, list) else 0
    rec = ToolCallRecord(
        call_id=call_id,
        timestamp=time.time(),
        tool_name=tool_name,
        client=client,
        client_session_id=client_session_id,
        skill_id=skill_id,
        args_summary=summarize_args(args),
        result_summary=summarize_result(result),
        succeeded=succeeded and result.get("ok", True) is not False,
        approval_grant_id=approval_grant_id,
        diagnostics_count=diag_count,
        error_codes=extract_error_codes(result),
        duration_ms=duration_ms,
    )
    log.record(rec)
    return rec


__all__ = [
    "ToolCallRecord",
    "ToolCallLog",
    "summarize_args",
    "summarize_result",
    "extract_error_codes",
    "record_invoke",
]
