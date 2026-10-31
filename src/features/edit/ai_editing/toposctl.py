"""TAEP M10 — toposctl：CLI 入口（§11.1 M10 / §11.2 step 3）。

``toposctl`` 是 ToolFacade + MCP dispatcher 的 CLI 封装：一个 ``run_cli(argv) -> dict``
函数，把 read / validate / preview 工具调用经**同一 ``ToolFacade``** 路径，保证
§11.2 完成检查："相同请求经 UI/CLI/MCP 得到相同 diagnostics"。

v1 决策（用户确认）：**``run_cli`` 函数 + 复用 ToolFacade**，不做完整 argparse main /
sys.exit 那层（那是接线批次）。本模块聚焦语义等价性——CLI 与 UI/MCP 走同一 invoke
路径，结果与 diagnostics 完全一致。

红线（§11.2 step 3 / §0 / §4.2）：

- **read/validate/preview 优先**（§11.2 step 3："实现 ``toposctl`` 的 read/validate/
  preview，后续才接 connected commit"）：v1 只暴露只读/preview 级工具；write/expensive/
  file_write 由 GUI/ApprovalService 签发 grant（CLI 不自带 grant）。
- **所有写动作由 GUI/ApprovalService 签发 grant**（§11.2 step 3）：CLI 尝试 write 类
  工具 → 返回 PERMISSION_DENIED（不自带 approval；与 ToolFacade 红线一致：模型/CLI
  无法自行伪造授权）。
- **审计**（§4.2 step 1）：每次调用经 ``ToolCallLog`` 记录（``client="cli"``）。
- **不读任意路径 / 不执行 shell**（§4.2 / ToolFacade.FORBIDDEN_CAPABILITIES）。

命令映射（v1 只读/preview 子集）：

- ``toposctl capabilities`` → ``topos.get_capabilities``
- ``toposctl snapshot [--detail minimal|structure|full] [--timeline ID]`` → ``topos.get_project_snapshot``
- ``toposctl search-assets [--text T] [--kind K] [--limit N]`` → ``topos.search_assets``
- ``toposctl analysis ASSET_ID`` → ``topos.get_asset_analysis``
- ``toposctl validate-plan PLAN_JSON`` → ``topos.validate_edit_plan``
- ``toposctl preview PLAN_JSON`` → ``topos.validate_edit_plan`` (safe diff preview)
- ``toposctl apply-plan PLAN_JSON`` → ``topos.apply_edit_plan``（无 grant → PERMISSION_DENIED）
- ``toposctl help [--query Q]`` → ``topos.search_help``
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
    make_diagnostic,
)
from src.features.edit.ai_editing.tool_call_log import (
    ToolCallLog,
    ToolCallRecord,
    record_invoke,
    summarize_args,
    summarize_result,
)


# CLI 默认 max_permission（§11.2 step 3：read/validate/preview 优先）
# apply-plan 调用时若宿主注入 approval 则升级到 write；否则 PERMISSION_DENIED。
_CLI_DEFAULT_PERMISSION = "preview"


# ---------------------------------------------------------------------- #
# CliResult —— run_cli 产物
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class CliResult:
    """``run_cli`` 的产物（JSON-safe）。

    ``exit_code``：0 = 成功，1 = 工具返回 error 诊断，2 = CLI 用法错误。
    """

    exit_code: int
    tool_name: str = ""
    result: dict[str, Any] | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()
    usage_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "tool_name": self.tool_name,
            "result": self.result,
            "diagnostics": list(self.diagnostics),
            "usage_hint": self.usage_hint,
        }


# ---------------------------------------------------------------------- #
# run_cli —— 主入口
# ---------------------------------------------------------------------- #


_USAGE = """\
toposctl — Topos AI Assistant CLI (read/validate/preview first; §11.2 step3)

Usage:
  toposctl [--socket PATH] version
  toposctl [--socket PATH] capabilities
  toposctl [--socket PATH] snapshot [--detail minimal|structure|full] [--timeline ID] [--asset-limit N]
  toposctl [--socket PATH] search-assets [--text T] [--kind K] [--limit N]
  toposctl [--socket PATH] analysis ASSET_ID
  toposctl [--socket PATH] validate-plan PLAN_JSON
  toposctl [--socket PATH] preview PLAN_JSON
  toposctl [--socket PATH] apply-plan PLAN_JSON [--approval GRANT_ID]
  toposctl [--socket PATH] help [--query Q] [--limit N]

version / capabilities 不依赖工程上下文（离线可用）；其余命令需通过 ``--socket`` 连接
当前用户桌面宿主（ToolFacade/EditSession）。Writes (apply-plan) require a host-issued
approval grant; local IPC/CLI cannot self-authorize.
"""


def run_cli(
    argv: Sequence[str],
    invoke_fn: Callable[..., dict[str, Any]] | None = None,
    *,
    call_log: ToolCallLog | None = None,
    approval_grant: Any = None,
) -> CliResult:
    """执行 toposctl 命令，返回 ``CliResult``。

    Args:
        argv: 命令行参数（不含程序名，如 ``["search-assets", "--text", "产品"]``）。
        invoke_fn: ``ToolFacade.invoke``（或兼容的 callable）。``version`` /
            ``capabilities`` 不需要（离线可用，直接读 build_manifest）；其它命令
            需要连接宿主，传 None 时返回 NOT_CONNECTED 诊断。
        call_log: 审计日志（None 时不记，但生产建议记）。
        approval_grant: 宿主签发的 ``ApprovalGrant``（apply-plan 用；None → PERMISSION_DENIED）。

    红线：所有工程上下文命令经 ``invoke_fn``（ToolFacade），不绕过；apply-plan 无
    grant → 拒绝。version/capabilities 例外——它们读运行时 registry，不碰工程状态。
    """
    log = call_log or ToolCallLog()
    if not argv:
        return CliResult(exit_code=2, usage_hint=_USAGE)

    cmd = argv[0]
    rest = list(argv[1:])

    # 已知命令集合（用于区分"未知命令 exit 2" vs "未连接宿主 exit 1"）。
    _KNOWN_CMDS = {
        "version", "capabilities", "snapshot", "search-assets", "analysis",
        "validate-plan", "preview", "apply-plan", "help",
    }
    if cmd in ("--help", "-h"):
        return CliResult(exit_code=0, usage_hint=_USAGE)
    # 未知命令优先于"未连接"判断（exit 2，不是 exit 1）
    if cmd not in _KNOWN_CMDS:
        return CliResult(
            exit_code=2, usage_hint=_USAGE,
            diagnostics=(_unknown_command(cmd),),
        )

    try:
        # ---- version：纯离线，不需要工程上下文 ---- #
        if cmd == "version":
            return CliResult(exit_code=0, tool_name="topos.version",
                             result=_version_info())
        # ---- capabilities：connected 时经 ToolFacade（保证审计一致）；
        #      离线（无 invoke_fn）时直接读 build_manifest（语义同源）。---- #
        if cmd == "capabilities":
            if invoke_fn is not None:
                return _run(log, invoke_fn, "topos.get_capabilities", {})
            return _capabilities_offline(log)
        # ---- 需要工程上下文（ToolFacade）的命令 ---- #
        if invoke_fn is None:
            return CliResult(
                exit_code=1, tool_name=f"topos.{cmd}",
                diagnostics=(_not_connected(cmd),),
                usage_hint=_USAGE,
            )
        if cmd == "snapshot":
            return _run(log, invoke_fn, "topos.get_project_snapshot", _parse_snapshot(rest))
        if cmd == "search-assets":
            return _run(log, invoke_fn, "topos.search_assets", _parse_search(rest))
        if cmd == "analysis":
            return _run(log, invoke_fn, "topos.get_asset_analysis", _parse_analysis(rest))
        if cmd in ("validate-plan", "preview"):
            return _run(log, invoke_fn, "topos.validate_edit_plan", _parse_plan(rest))
        if cmd == "apply-plan":
            return _run(log, invoke_fn, "topos.apply_edit_plan", _parse_plan(rest),
                        approval=approval_grant)
        if cmd == "help":
            return _run(log, invoke_fn, "topos.search_help", _parse_help(rest))
        # 不应到达（_KNOWN_CMDS 已过滤），防御性兜底
        return CliResult(
            exit_code=2, usage_hint=_USAGE,
            diagnostics=(_unknown_command(cmd),),
        )
    except _CliUsageError as exc:
        return CliResult(exit_code=2, usage_hint=_USAGE, diagnostics=(_usage_error(str(exc)),))
    except (OSError, ConnectionError, TimeoutError, json.JSONDecodeError):
        # The optional local IPC endpoint disappears when the desktop project
        # closes.  Preserve the CLI's structured error contract and do not
        # expose a local socket path in its diagnostics.
        return CliResult(
            exit_code=1, tool_name=f"topos.{cmd}",
            diagnostics=(_not_connected(cmd),), usage_hint=_USAGE,
        )


# ---------------------------------------------------------------------- #
# 离线命令实现（不依赖 ToolFacade / EditSession / 工程）
# ---------------------------------------------------------------------- #


def _version_info() -> dict[str, Any]:
    """CLI / protocol / schema 版本信息（供 client compatibility check）。

    纯读取常量，不创建 QApplication / GL context。
    """
    from src.features.edit.ai_editing.models import TAEP_PROTOCOL_VERSION
    try:
        from src.features.edit.ai_editing.capability_manifest import (
            CAPABILITY_MANIFEST_VERSION,
        )
    except (ImportError, AttributeError):
        CAPABILITY_MANIFEST_VERSION = "1"
    return {
        "cli": "toposctl",
        "cli_version": "0.1.0",
        "protocol": TAEP_PROTOCOL_VERSION,
        "manifest_version": str(CAPABILITY_MANIFEST_VERSION),
    }


def _capabilities_offline(log: ToolCallLog) -> CliResult:
    """离线 capabilities：读 build_manifest + 运行时能力快照（不经 ToolFacade）。

    与 connected 路径语义一致（build_manifest 是 ToolFacade.get_capabilities 的
    真相源），但不需要工程/host。T-P0-00：注入 RuntimeCapabilitySnapshot，
    保证离线/connected 返回同一份四态能力事实与 snapshot_id。审计仍记录
    （client="cli"）。
    """
    try:
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        from src.features.edit.ai_editing.runtime_capability_snapshot import (
            build_runtime_capability_snapshot,
        )

        manifest = build_manifest(
            runtime_snapshot=build_runtime_capability_snapshot(),
        )
        # manifest.to_dict() 已处理 EffectCapability 等嵌套对象的序列化。
        result = manifest.to_dict()
    except Exception as exc:  # noqa: BLE001
        return CliResult(
            exit_code=1, tool_name="topos.get_capabilities",
            diagnostics=(_internal_error(str(exc)),),
        )
    # 审计记录（与 connected 路径一致，client="cli"）。
    # AUD-14807：此处原先以 ``log.record(tool_name=..., args=..., ...)`` 关键字
    # 调用 ``ToolCallLog.record(rec)``（签名不符 → TypeError 被 ``except Exception``
    # 吞掉），离线 capabilities 的审计记录从未真正落日志——改为构造
    # ``ToolCallRecord`` 后正确记录，main() 注入的持久化 sink 才能收到它。
    log.record(ToolCallRecord(
        call_id=log.next_call_id(),
        timestamp=time.time(),
        tool_name="topos.get_capabilities",
        client="cli",
        args_summary=summarize_args({}),
        result_summary=summarize_result(result),
        succeeded=result.get("ok", True) is not False,
    ))
    return CliResult(
        exit_code=0, tool_name="topos.get_capabilities", result=result,
    )


def _not_connected(cmd: str) -> dict[str, Any]:
    """需要工程上下文的命令在未连接宿主时返回的诊断。"""
    return make_diagnostic(
        DiagnosticSeverity.ERROR, TaepDiagnosticCode.PERMISSION_DENIED,
        op_kind=cmd,
        details={"reason": f"command {cmd!r} requires a connected host "
                           f"(ToolFacade/EditSession); use version/capabilities for offline"},
    ).to_dict()


def _internal_error(detail: str) -> dict[str, Any]:
    return make_diagnostic(
        DiagnosticSeverity.ERROR, TaepDiagnosticCode.INTERNAL_ERROR,
        details={"reason": detail},
    ).to_dict()


def _run(
    log: ToolCallLog,
    invoke_fn: Callable[..., dict[str, Any]],
    tool_name: str,
    args: dict[str, Any],
    **invoke_kwargs: Any,
) -> CliResult:
    """经 invoke_fn 调用 + 审计，组装 CliResult。"""
    result, _rec = record_invoke(
        log, invoke_fn, tool_name, args,
        client="cli", **invoke_kwargs,
    )
    diags = result.get("diagnostics") if isinstance(result, dict) else None
    diag_list = tuple(diags) if isinstance(diags, list) else ()
    has_error = any(
        isinstance(d, dict) and d.get("severity") == "error" for d in diag_list
    )
    exit_code = 1 if has_error else 0
    return CliResult(
        exit_code=exit_code, tool_name=tool_name,
        result=result, diagnostics=diag_list,
    )


# ---------------------------------------------------------------------- #
# 参数解析（最小 hand-rolled，避免引入 argparse 接线复杂度）
# ---------------------------------------------------------------------- #


class _CliUsageError(Exception):
    """CLI 参数解析失败。"""


def _parse_int_option(flag: str, raw: str) -> int:
    """解析整数型 CLI 选项值（AUD-14806）。

    ``int()`` 抛出的 ValueError 原先不在 ``run_cli`` 的 except 列表内，CLI 以
    traceback 崩溃。数值型选项的坏值是**用户输入错误**，契约是 exit 2（usage
    error + SCHEMA_INVALID 诊断），不是进程崩溃——这里统一转成 ``_CliUsageError``。
    """
    try:
        return int(raw)
    except ValueError as exc:
        raise _CliUsageError(f"{flag} expects an integer, got {raw!r}") from exc


def _parse_snapshot(args: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--detail" and i + 1 < len(args):
            out["detail_level"] = args[i + 1]; i += 2
        elif a == "--timeline" and i + 1 < len(args):
            out["timeline_id"] = args[i + 1]; i += 2
        elif a == "--asset-limit" and i + 1 < len(args):
            out["asset_limit"] = _parse_int_option(a, args[i + 1]); i += 2
        else:
            i += 1
    return out


def _parse_search(args: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--text" and i + 1 < len(args):
            out["text"] = args[i + 1]; i += 2
        elif a == "--kind" and i + 1 < len(args):
            out["kind"] = args[i + 1]; i += 2
        elif a == "--limit" and i + 1 < len(args):
            out["limit"] = _parse_int_option(a, args[i + 1]); i += 2
        else:
            i += 1
    return out


def _parse_analysis(args: list[str]) -> dict[str, Any]:
    if not args or args[0].startswith("-"):
        raise _CliUsageError("analysis requires ASSET_ID")
    return {"asset_id": args[0]}


def _parse_plan(args: list[str]) -> dict[str, Any]:
    """解析 validate-plan / apply-plan 的 PLAN_JSON 参数。

    PLAN_JSON 可以是内联 JSON 字符串或 ``@file``（``@`` 前缀读文件——接线批次支持，
    v1 假设内联）。返回 ``{"plan": <parsed dict>}``。
    """
    if not args:
        raise _CliUsageError("requires PLAN_JSON argument")
    raw = args[0]
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _CliUsageError(f"PLAN_JSON is not valid JSON: {exc}") from exc
    return {"plan": plan}


def _parse_help(args: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--query" and i + 1 < len(args):
            out["query"] = args[i + 1]; i += 2
        elif a == "--limit" and i + 1 < len(args):
            out["limit"] = _parse_int_option(a, args[i + 1]); i += 2
        else:
            i += 1
    return out


def _unknown_command(cmd: str) -> dict[str, Any]:
    return make_diagnostic(
        DiagnosticSeverity.ERROR, TaepDiagnosticCode.UNSUPPORTED_OPERATION,
        op_kind=cmd,
        details={"reason": f"unknown toposctl command: {cmd}"},
    ).to_dict()


def _usage_error(msg: str) -> dict[str, Any]:
    return make_diagnostic(
        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
        details={"reason": f"CLI usage error: {msg}"},
    ).to_dict()


# ---------------------------------------------------------------------- #
# 审计持久化 sink（AUD-14807）
# ---------------------------------------------------------------------- #

_AUDIT_LOG_ENV = "TOPOSCTL_AUDIT_LOG"
# 与 assistant_session_store 的 ~/.topos_cache 约定同源（用户本机轻量状态，
# 不含 secret/绝对路径——记录本身经 summarize_args/result 脱敏）。
_DEFAULT_AUDIT_LOG_PATH = Path.home() / ".topos_cache" / "toposctl_audit_log.jsonl"


class _PersistentToolCallLog(ToolCallLog):
    """``ToolCallLog`` 的 JSONL 落盘镜像（AUD-14807）。

    ``main()`` 原先不传 ``call_log``，``run_cli`` 回退到随进程退出即丢弃的
    临时内存日志——模块红线"每次调用经 ToolCallLog 记录（client='cli'）"在
    console 入口不可满足。本类把每条记录在产生时（而非进程退出前）作为一行
    JSON 追加到落盘 sink，即使后续崩溃审计也已留存；内存语义（锁、FIFO、
    call_id 唯一）继承自 ``ToolCallLog``。写失败降级为 warning 并停用 sink，
    不破坏 CLI 的 stdout 单一 JSON / 退出码契约。
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._write_lock = threading.Lock()
        self._sink_enabled = True

    def record(self, rec: ToolCallRecord) -> ToolCallRecord:
        super().record(rec)
        self._append_line(rec)
        return rec

    def _append_line(self, rec: ToolCallRecord) -> None:
        if not self._sink_enabled:
            return
        try:
            line = json.dumps(rec.to_dict(), ensure_ascii=False)
            with self._write_lock, self._path.open("a", encoding="utf-8") as sink:
                sink.write(line + "\n")
        except OSError as exc:
            self._sink_enabled = False
            logging.getLogger("toposctl").warning(
                "toposctl audit log %s unavailable (%s); audit records for "
                "this invocation will not persist", self._path, exc,
            )


def _resolve_audit_log_path(override: Path | str | None) -> Path:
    """解析审计 sink 路径：显式参数 > ``TOPOSCTL_AUDIT_LOG`` > 默认 ~/.topos_cache。"""
    configured = os.environ.get(_AUDIT_LOG_ENV, "").strip()
    if override is not None:
        return Path(override)
    if configured:
        return Path(configured)
    return _DEFAULT_AUDIT_LOG_PATH


def _make_audit_log(path: Path) -> ToolCallLog:
    """构造持久化审计日志；目录不可创建时降级为进程内日志（warning 留痕）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as exc:
        logging.getLogger("toposctl").warning(
            "toposctl audit log directory %s unavailable (%s); audit records "
            "for this invocation will not persist", path.parent, exc,
        )
        return ToolCallLog()
    return _PersistentToolCallLog(path)


# ---------------------------------------------------------------------- #
# main —— console_scripts 入口（stdout 单一 JSON，stderr 日志，sys.exit）
# ---------------------------------------------------------------------- #


def main(
    argv: Sequence[str] | None = None,
    *,
    audit_log_path: Path | str | None = None,
) -> int:
    """``toposctl`` console_scripts 入口。

    - stdout 只输出**单一 JSON**（``CliResult.to_dict()``），便于 shell/CI/MCP 解析。
    - stderr 走日志（``--log-level`` 控制级别）。
    - 进程退出码 = ``CliResult.exit_code``（0=成功，1=error 诊断，2=用法错误）。
    - 审计（AUD-14807）：构造持久化 ``ToolCallLog`` 传给 ``run_cli``，每条
      client="cli" 记录即时以 JSONL 追加到 ``audit_log_path``（参数显式指定 >
      ``TOPOSCTL_AUDIT_LOG`` 环境变量 > 默认 ``~/.topos_cache/toposctl_audit_log.jsonl``），
      不再随进程退出丢弃；sink 不可写时降级 warning，不影响 stdout/退出码。

    ``version`` / ``capabilities`` 离线可用（不创建 QApplication / GL context）。
    ``--socket PATH`` 连接正在运行的当前用户桌面 MCP host；其余工程命令经该 host
    的同一 ``ToolFacade`` 处理。未指定 socket 时保留显式 NOT_CONNECTED 诊断。
    """
    args = list(argv if argv is not None else sys.argv[1:])

    # 解析通用选项（--output / --log-level）；默认 json 输出。
    output_format = "json"
    socket_path = ""
    filtered: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--output" and i + 1 < len(args):
            output_format = args[i + 1]; i += 2; continue
        if a.startswith("--output="):
            output_format = a.split("=", 1)[1]; i += 1; continue
        if a == "--log-level" and i + 1 < len(args):
            import logging

            # AUD-14809：非法级别名（如 --log-level bogus）会让 setLevel 抛
            # ValueError 使 main() 直接崩溃。这里捕获后降级为 WARNING 并在
            # stderr 留痕，保证 stdout 单一 JSON / 退出码契约不被破坏。
            requested = args[i + 1].upper()
            try:
                logging.getLogger("toposctl").setLevel(requested)
            except ValueError:
                fallback_logger = logging.getLogger("toposctl")
                fallback_logger.setLevel(logging.WARNING)
                fallback_logger.warning(
                    "invalid --log-level %r; falling back to WARNING", args[i + 1],
                )
            i += 2; continue
        if a == "--socket" and i + 1 < len(args):
            socket_path = args[i + 1]; i += 2; continue
        if a.startswith("--socket="):
            socket_path = a.split("=", 1)[1]; i += 1; continue
        filtered.append(a); i += 1

    invoke_fn = None
    if socket_path:
        from src.features.edit.ai_editing.mcp_local_ipc import LocalMcpIpcClient
        invoke_fn = LocalMcpIpcClient(socket_path).invoke
    # AUD-14807：显式传入持久化审计日志——run_cli 不再回退到随进程丢弃的
    # 临时内存 ToolCallLog。
    audit_log = _make_audit_log(_resolve_audit_log_path(audit_log_path))
    result = run_cli(filtered, invoke_fn, call_log=audit_log)

    if output_format == "text":
        # 人读模式：exit_code + 简短摘要
        if result.usage_hint and result.exit_code == 2:
            print(result.usage_hint, file=sys.stderr)
        elif result.result is not None:
            print(json.dumps(result.result, ensure_ascii=False, indent=2))
        for d in result.diagnostics:
            print(f"[{d.get('severity', 'error')}] {d.get('code', '')}: "
                  f"{d.get('message', '')}", file=sys.stderr)
    else:
        # 默认 json：stdout 单一 JSON
        print(json.dumps(result.to_dict(), ensure_ascii=False))

    return result.exit_code


__all__ = ["CliResult", "run_cli", "main"]
