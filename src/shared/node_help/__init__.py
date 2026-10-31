"""Node usage help system — bilingual per-node documentation.

Every node can ship a usage document that the node-section header exposes
through a "?" button. All documents live in this package's ``docs/`` folder,
one Markdown file per node per language:

    docs/<node_type>.zh_CN.md   — 简体中文
    docs/<node_type>.en_US.md   — English (fallback language)

Adding help for a new node is a docs-only change: drop the two Markdown files
into ``docs/`` (either language alone is fine; English is the fallback) and
the header button picks them up automatically — no code changes required.

Language selection follows the app-wide I18N locale: a zh_* locale reads the
zh_CN document, everything else reads en_US. Documents are read fresh from
disk on every call so edits show up without restarting the app.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Optional

# Canonical fallback language for node help documents.
FALLBACK_LANG = "en_US"

logger = logging.getLogger(__name__)


def get_docs_dir() -> str:
    """Return the absolute path of the node help docs folder.

    Works both in a source checkout (``docs/`` beside this file) and in a
    PyInstaller build (datas are unpacked under ``sys._MEIPASS``).
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            frozen_dir = os.path.join(
                meipass, "src", "shared", "node_help", "docs")
            if os.path.isdir(frozen_dir):
                return frozen_dir
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


def _lang_for_locale(locale_code: str) -> str:
    """Map an app locale code to a help-document language code."""
    if locale_code and locale_code.lower().startswith("zh"):
        return "zh_CN"
    return FALLBACK_LANG


# AUD-36404：node_type 直接拼入 docs 目录下的文件路径——只接受标识符
# 字符（字母/数字/下划线/连字符），拒绝路径分隔与穿越成分（../、/、\、
# 盘符冒号等），防止 help 查询越出 docs 目录读取任意 .md 文件。
_SAFE_NODE_TYPE_RE = re.compile(r"[A-Za-z0-9_\-]+")


def _is_safe_node_type(node_type: str) -> bool:
    return bool(_SAFE_NODE_TYPE_RE.fullmatch(node_type))


def _current_app_locale() -> str:
    """Best-effort current app locale without hard-importing the I18N module.

    Deferred import keeps this loader usable in contexts where the i18n
    package is not initialized yet (and in pure unit tests).
    """
    try:
        from src.shared.i18n.i18n import I18N
        return I18N.get_current_locale()
    except Exception:
        return FALLBACK_LANG


def get_node_help(node_type: str,
                  locale_code: Optional[str] = None) -> Optional[str]:
    """Return the usage document for ``node_type``, or None if it has none.

    Args:
        node_type: Node type key (e.g. "cornerpin", "transform").
        locale_code: Explicit locale override; defaults to the app locale.

    Resolution order:
        1. docs/<node_type>.<lang>.md        (language of the current locale)
        2. docs/<node_type>.en_US.md         (English fallback)
    """
    if not node_type:
        return None
    if not _is_safe_node_type(node_type):
        logger.warning("node help lookup rejected unsafe node_type: %r",
                       node_type)
        return None
    lang = _lang_for_locale(locale_code or _current_app_locale())
    docs_dir = get_docs_dir()

    candidates = [os.path.join(docs_dir, f"{node_type}.{lang}.md")]
    if lang != FALLBACK_LANG:
        candidates.append(os.path.join(docs_dir, f"{node_type}.{FALLBACK_LANG}.md"))

    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError as exc:
                # AUD-36401: a preferred-language document that exists but
                # cannot be read (permissions, race with an editor save,
                # decode error surfaced as OSError) must not abandon the
                # documented resolution order — continue to the en_US
                # fallback instead of returning None right away.
                logger.warning(
                    "node help %r unreadable (%s); trying fallback language",
                    path, exc)
                continue
    return None


def available_langs(node_type: str) -> list[str]:
    """Language codes that have a document for ``node_type``.

    The app locale language comes first, then the fallback language, then
    any other ``<node_type>.<lang>.md`` files found in the docs folder.
    """
    if not node_type:
        return []
    if not _is_safe_node_type(node_type):
        return []
    docs_dir = get_docs_dir()
    prefix = f"{node_type}."
    suffix = ".md"
    found: list[str] = []
    try:
        names = os.listdir(docs_dir)
    except OSError:
        return []
    for name in names:
        if name.startswith(prefix) and name.endswith(suffix):
            found.append(name[len(prefix):-len(suffix)])

    preferred = _lang_for_locale(_current_app_locale())
    order = [preferred] + [FALLBACK_LANG] + sorted(set(found))
    return [lang for lang in dict.fromkeys(order) if lang in found]


def has_node_help(node_type: str,
                  locale_code: Optional[str] = None) -> bool:
    """True when a usage document exists for ``node_type`` (any language).

    AUD-36403: answers via a pure existence probe over the candidate paths —
    it neither opens nor decodes any Markdown file (the previous
    implementation read the whole document through :func:`get_node_help`
    just to return a boolean).
    """
    if not node_type:
        return False
    if not _is_safe_node_type(node_type):
        return False
    lang = _lang_for_locale(locale_code or _current_app_locale())
    docs_dir = get_docs_dir()

    candidates = [os.path.join(docs_dir, f"{node_type}.{lang}.md")]
    if lang != FALLBACK_LANG:
        candidates.append(os.path.join(docs_dir, f"{node_type}.{FALLBACK_LANG}.md"))
    return any(os.path.isfile(path) for path in candidates)
