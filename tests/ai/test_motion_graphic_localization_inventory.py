"""R-08: localization inventory for teaching_animation/module.py.

module.py mixes four kinds of Chinese text on purpose:

  * AI intent keywords     — input parsing for Chinese user commands; these
    MUST stay Chinese regardless of UI locale;
  * model-facing prompts   — the production plan / rewrite prompts target the
    LLM and stay Chinese by design;
  * developer comments/docstrings — never user-visible;
  * user-visible messages  — chat/status text shown in the assistant dock;
    these must go through I18N.tr.

This test classifies the file and freezes the user-visible baseline: every
such message that is not yet migrated is counted, and the count may never
grow.  Migrated messages must resolve in BOTH locales.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "src/features/teaching_animation/module.py"
LOCALES = MODULE.parent / "locales"
# After the 2026-08 R-08 batch: 28 message sites (plus 3 status texts) were
# migrated to I18N.tr.  The remaining baseline below may only shrink.
MAX_UNMIGRATED_USER_MESSAGES = 37

_SINKS = ("_append_assistant_message", "showMessage", "_append_system_message")
_ZH = re.compile(r"[\u4e00-\u9fff]")


def _user_visible_chinese_sites() -> list[int]:
    lines = MODULE.read_text(encoding="utf-8").splitlines()
    sites: list[int] = []
    for i, line in enumerate(lines):
        if any(sink in line for sink in _SINKS):
            window = "\n".join(lines[i:i + 6])
            if _ZH.search(window) and "I18N.tr" not in window:
                sites.append(i + 1)
    return sites


def test_user_visible_chinese_messages_stay_within_baseline():
    sites = _user_visible_chinese_sites()
    assert len(sites) <= MAX_UNMIGRATED_USER_MESSAGES, (
        f"{len(sites)} user-visible Chinese message sites exceed the frozen "
        f"baseline {MAX_UNMIGRATED_USER_MESSAGES}: new user-facing messages "
        "must go through I18N.tr (see motion_graphic.ai.* keys); lines: "
        f"{sites[:10]}")


def test_migrated_ai_keys_exist_in_both_locales():
    used = set(re.findall(r'I18N\.tr\("motion_graphic\.ai\.([a-z_]+)"', MODULE.read_text(
        encoding="utf-8")))
    assert used, "expected migrated motion_graphic.ai.* keys in module.py"
    for locale in ("en_US", "zh_CN"):
        data = json.loads((LOCALES / f"{locale}.json").read_text(encoding="utf-8"))
        table = data.get("motion_graphic", {}).get("ai", {})
        missing = used - set(table)
        assert not missing, f"{locale} locale missing motion_graphic.ai keys: {sorted(missing)}"


def test_english_mode_renders_english_for_migrated_keys():
    import sys
    sys.path.insert(0, str(MODULE.parents[2] / "src"))
    from src.shared.i18n.i18n import I18N
    # load_locale() resets the table, so the feature translations must be
    # (re-)merged after every locale switch — same as the runtime module init.
    assert I18N.load_locale("en_US")
    I18N.load_module_translations(str(LOCALES))
    text = I18N.tr("motion_graphic.ai.plan_cancelled", "fallback")
    assert text == ("The Motion Graphic edit plan was cancelled; "
                    "the scene was not modified.")
    assert not re.search(r"[\u4e00-\u9fff]", text)
    assert I18N.load_locale("zh_CN")
    I18N.load_module_translations(str(LOCALES))
    assert "已取消" in I18N.tr("motion_graphic.ai.plan_cancelled", "fallback")
    assert I18N.load_locale("en_US")  # leave the process back on en_US
