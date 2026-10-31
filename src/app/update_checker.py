import urllib.request
import urllib.error
import json
import os
import re
import logging
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# 单一事实源是 pyproject.toml 的 [project].version（发布注释同步维护）。
# 解析顺序：pip 安装/打包环境的发行元数据（构建后端从 pyproject 同步）
# → 源码运行直接解析 pyproject.toml → 字面量兜底（与 pyproject 人工同步，
# 使用时记 warning 留痕，避免冻结环境里悄悄拿旧版本号做比较 —— AUD-42002）。
_FALLBACK_VERSION = "3.0.0"

# 发行名候选：PyPI/sdist 与某些打包环境注册的名称可能不同，逐个尝试，
# 全部落空才走 pyproject/字面量兜底。
_DIST_NAMES = ("topos", "topos-color")


def _version_from_metadata() -> str | None:
    """Best-effort installed-distribution version, or None."""
    import importlib.metadata as _md
    for dist in _DIST_NAMES:
        try:
            return _md.version(dist)
        except _md.PackageNotFoundError:
            continue
    return None


def _resolve_version() -> str:
    version = _version_from_metadata()
    if version is not None:
        return version
    try:
        import tomllib
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception as e:
        logger.debug("version resolve fallback: %s", e)
        # AUD-42002: the literal is hand-synced with pyproject; in a
        # frozen/packaged environment neither metadata nor pyproject may be
        # reachable, so make the stale-literal situation loudly observable.
        logger.warning(
            "Using hard-coded fallback version %s — keep it in sync with "
            "pyproject.toml [project].version", _FALLBACK_VERSION,
        )
        return _FALLBACK_VERSION


CURRENT_VERSION = _resolve_version()

# Gitee API v5 endpoint for releases (the project's release channel is Gitee)
RELEASES_URL = "https://gitee.com/api/v5/repos/xiaoduoshare/topos/releases/latest"


def parse_version(version_str: str) -> tuple:
    """Parse a version string like 'v1.2.3' into (1, 2, 3)."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return (0, 0, 0)


def is_newer(remote: str, local: str) -> bool:
    """Return True if remote version is newer than local version."""
    return parse_version(remote) > parse_version(local)


class UpdateChecker(QThread):
    """Async update checker – queries the Gitee Releases API (Gitee API v5)."""

    update_available = pyqtSignal(str, str, str)  # version, url, changelog
    no_update = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, parent=None, url: str = RELEASES_URL):
        super().__init__(parent)
        self._url = url

    def run(self):
        # Skip network access during tests to avoid thread leaks and segfaults
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TOPOS_TEST_MODE"):
            self.no_update.emit()
            return
        try:
            req = urllib.request.Request(
                self._url,
                headers={"User-Agent": "Topos-Color-Update-Checker"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            tag = data.get("tag_name", "")
            html_url = data.get("html_url", "")
            changelog = data.get("body", "")

            if tag and is_newer(tag, CURRENT_VERSION):
                self.update_available.emit(tag, html_url, changelog)
            else:
                self.no_update.emit()

        except urllib.error.HTTPError as e:
            # 404 means no releases yet – not an error
            if e.code == 404:
                self.no_update.emit()
            else:
                self.error.emit(f"HTTP {e.code}")
        except Exception as e:
            logger.debug("Update check failed: %s", e)
            self.error.emit(str(e))
