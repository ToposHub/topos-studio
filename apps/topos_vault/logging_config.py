"""vault 结构化日志配置（P2-7）。

提供两种日志格式：
    - text（默认）：人类可读，带时间戳 + 模块 + 级别
    - json：结构化 JSON（机器可读，方便 ELK/Loki 采集 + 检索）

日志文件：写 <vault_dir>/logs/vault.log，按大小轮转（默认 10MB × 5 份）。

用法：
    from apps.topos_vault.logging_config import setup_logging
    setup_logging(level="INFO", fmt="json")  # 启动时调一次

    import logging
    logging.getLogger(__name__).info("打标完成", extra={"extra": {"files": 10}})

环境变量：
    TOPOS_VAULT_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR  日志级别（默认 INFO）
    TOPOS_VAULT_LOG_FORMAT=text|json                日志格式（默认 text）
    TOPOS_VAULT_LOG_FILE=1|0                        是否写文件（默认 1）
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any

from apps.topos_vault.paths import get_vault_dir


# 已配置标记（避免重复配置）
_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器。

    输出每行一个 JSON，字段：
        ts、level、logger、msg、（可选 extra 字段）

    用法：logger.info("xxx", extra={"extra": {"files": 10}}) → extra 字段并入 JSON。
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # 异常信息
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # 业务自定义字段（通过 extra={"extra": {...}} 传入）
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in log_entry:  # 不覆盖标准字段
                    log_entry[k] = v

        # 选定字段（funcName/lineno/module）放 dedicated 字段，便于检索
        log_entry["func"] = f"{record.module}:{record.funcName}:{record.lineno}"

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人类可读文本格式（带彩色级别 + 时间戳）。"""

    _LEVEL_COLORS = {
        "DEBUG": "\033[36m",    # cyan
        "INFO": "\033[32m",     # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",    # red
        "CRITICAL": "\033[35m", # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname
        color = self._LEVEL_COLORS.get(level, "")
        reset = self._RESET if color else ""
        msg = record.getMessage()
        extra = getattr(record, "extra", None)
        extra_str = f" {json.dumps(extra, ensure_ascii=False, default=str)}" if extra else ""
        exc = ""
        if record.exc_info:
            exc = "\n" + self.formatException(record.exc_info)
        return (f"{ts} {color}{level:<7}{reset} {record.name:<30s} {msg}{extra_str}{exc}")


def setup_logging(
    level: str = "INFO",
    fmt: str = "text",
    *,
    log_file: bool = True,
    force: bool = False,
) -> logging.Logger:
    """配置 vault 的根日志。

    幂等：重复调用不重复加 handler（除非 force=True）。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）。
        fmt: 格式（text/json）。
        log_file: 是否写文件（<vault_dir>/logs/vault.log）。
        force: 强制重新配置（测试切目录后用）。

    Returns:
        vault 根 logger（apps.topos_vault）。
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return logging.getLogger("apps.topos_vault")

    # 环境变量覆盖
    level = os.environ.get("TOPOS_VAULT_LOG_LEVEL", level).upper()
    fmt = os.environ.get("TOPOS_VAULT_LOG_FORMAT", fmt).lower()
    log_file_env = os.environ.get("TOPOS_VAULT_LOG_FILE")
    if log_file_env is not None:
        log_file = log_file_env not in ("0", "false", "False", "")

    root = logging.getLogger()
    # 清掉旧 handler（force 时）
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)

    # 选 formatter
    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = TextFormatter()

    # 控制台 handler
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件 handler（轮转）
    if log_file:
        try:
            log_dir = get_vault_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_dir / "vault.log"),
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5,
                encoding="utf-8",
            )
            # 文件用 JSON 格式（便于事后检索），与控制台格式解耦
            file_handler.setFormatter(JsonFormatter())
            root.addHandler(file_handler)
        except Exception as e:
            # 日志目录不可写等 → 不阻塞，只用控制台
            root.warning("无法创建日志文件：%s", e)

    root.setLevel(level)
    _CONFIGURED = True

    vault_logger = logging.getLogger("apps.topos_vault")
    vault_logger.info("vault 日志已配置", extra={"extra": {
        "level": level, "format": fmt, "log_file": log_file,
    }})
    return vault_logger


def get_log_file_path() -> str | None:
    """当前日志文件路径（诊断用）。"""
    try:
        p = get_vault_dir() / "logs" / "vault.log"
        return str(p) if p.exists() else None
    except Exception:
        return None
