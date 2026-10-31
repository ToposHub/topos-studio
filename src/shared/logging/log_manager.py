"""
LogManager - File logging management for Topos Color.

Provides centralized logging initialization with:
- Console and rotating file handlers
- Config-driven log level, file path, and max file size
- Runtime reconfiguration (e.g. after Preferences changes)
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional


class LogManager:
    """Manages application-wide logging configuration.

    Singleton pattern — use LogManager.initialize() once at startup,
    then LogManager.instance() to access.
    """

    _instance: Optional['LogManager'] = None

    def __init__(self, config_manager=None) -> None:
        self._config_manager = config_manager
        self._file_handler: Optional[RotatingFileHandler] = None
        self._console_handler: Optional[logging.StreamHandler] = None
        self._log_level: int = logging.WARNING

    @classmethod
    def initialize(cls, config_manager=None) -> 'LogManager':
        """Create the singleton and configure logging."""
        cls._instance = cls(config_manager)
        cls._instance._setup()
        return cls._instance

    @classmethod
    def instance(cls) -> Optional['LogManager']:
        return cls._instance

    # ── Setup ─────────────────────────────────────────────────────

    def _setup(self) -> None:
        """Initial logging setup from config."""
        cfg = self._config_manager

        # Resolve log level
        level_map = {
            "debug": logging.DEBUG, "info": logging.INFO,
            "warning": logging.WARNING, "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        level_str = (cfg.get("log_level") if cfg else None) or "warning"
        self._log_level = level_map.get(level_str, logging.WARNING)

        # Configure root logger — detach any existing handlers first, then
        # close them (AUD-34203: the previous wholesale list clear threw away
        # the old handler objects without ``close()``, so every repeated
        # initialize()/reconfigure() leaked the RotatingFileHandler's file
        # descriptor. Detachment replaces the whole handler list in one
        # atomic attribute assignment, so a concurrent ``emit`` iterating the
        # handlers either walks the old list to completion or the new one —
        # no half-cleared window. Closing happens only after detachment, so
        # no concurrent emit can touch a closed handler.)
        root = logging.getLogger()
        root.setLevel(self._log_level)
        stale_handlers = list(root.handlers)
        root.handlers = []
        for stale in stale_handlers:
            try:
                stale.close()
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning(
                    "closing stale log handler %r failed: %s", stale, exc)

        # Console handler
        self._console_handler = logging.StreamHandler()
        self._console_handler.setLevel(self._log_level)
        self._console_handler.setFormatter(
            logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(self._console_handler)

        # File handler (if enabled)
        if cfg and cfg.get("enable_file_logging"):
            self._add_file_handler()

    def _add_file_handler(self) -> None:
        """Create and attach a RotatingFileHandler.

        log_file_path is user-configurable external input (default "" in
        config). A bare relative filename made os.path.dirname return ""
        and os.makedirs("") raise FileNotFoundError, and unwritable
        paths / disk-full raised OSError from makedirs or the handler
        constructor — all uncaught, crashing startup and Preferences
        reconfiguration with logging left half-initialized. The whole
        file-handler establishment is guarded: on OSError, degrade to
        console-only logging instead of propagating (AUD-34201).
        """
        cfg = self._config_manager
        log_path = ""
        try:
            log_path = self._resolve_log_path()

            # Normalize (bare filename resolves against CWD so dirname is
            # never ""; relative paths become predictable absolute ones).
            log_path = os.path.abspath(log_path)
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)

            max_size = (cfg.get("max_log_file_size_mb") if cfg else None) or 10
            max_bytes = max_size * 1024 * 1024  # MB → bytes

            handler = RotatingFileHandler(
                log_path, maxBytes=max_bytes, backupCount=3, encoding="utf-8"
            )
        except OSError as exc:
            self._file_handler = None
            logging.getLogger(__name__).warning(
                "File logging disabled — cannot establish log file "
                "%r (degrading to console-only): %s", log_path, exc)
            return

        handler.setLevel(self._log_level)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
        self._file_handler = handler

    def _resolve_log_path(self) -> str:
        """Resolve the configured log path or the platform default.

        Single source of truth for _add_file_handler and
        get_log_file_path so both agree on the default location.
        """
        cfg = self._config_manager
        log_path = (cfg.get("log_file_path") if cfg else None) or ""
        if not log_path:
            from src.shared.config.config_manager import ConfigManager  # [R2-5] 平台规范目录
            log_path = os.path.join(ConfigManager.get_platform_data_dir(), "logs", "topos.log")
        return log_path

    def _remove_file_handler(self) -> None:
        """Detach and close the file handler."""
        if self._file_handler:
            logging.getLogger().removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None

    # ── Public API ────────────────────────────────────────────────

    def reconfigure(self) -> None:
        """Re-apply logging config (call after Preferences changes)."""
        self._remove_file_handler()
        self._setup()

    def get_log_file_path(self) -> str:
        """Return the resolved log file path (for UI display)."""
        return self._resolve_log_path()
