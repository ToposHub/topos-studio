"""R-02: WebEngine readiness preflight shared by the MG browser E2E scripts.

A page-load timeout in an E2E used to cascade into misleading follow-up
failures (``NoneType has no attribute page``) with no way to tell "this
machine cannot run WebEngine" from "the product broke".  Worse, on hostile
platforms (offscreen) merely creating a WebEngine view can segfault the
whole process.  This module therefore runs the probe in a *subprocess*:

  * the child imports QtWebEngine, loads a trivial data: URL in a
    standalone view (one controlled retry, bounded waits) and prints one
    JSON line with the verdict and diagnostics — Qt/QtWebEngine versions,
    platform, Chromium flags, load result, last console messages, and
    whether the render process terminated;
  * the parent turns a crash, hang (bounded by timeout) or import failure
    into a clean "environment unavailable" verdict — the E2E host process
    is never taken down by the probe itself;
  * scripts use the verdict to mark dependent steps ``SKIP (blocked by
    browser readiness)`` and exit per their --require-browser /
    --allow-skip-browser mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
_PROBE_URL = "data:text/html,<!doctype html><html><body>topos-webengine-readiness</body></html>"
# Bounded waits: readiness must answer fast, never hang the E2E.
_LOAD_TIMEOUT_S = 20.0
_SUBPROCESS_TIMEOUT_S = 90.0
_MARKER = "READINESS_JSON:"


@dataclass
class ReadinessResult:
    ok: bool
    reason: str
    diagnostics: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.ok:
            return "WebEngine ready"
        return f"browser environment unavailable: {self.reason}"


# ── child side ───────────────────────────────────────────────────────────────

_CHILD_CODE = r"""
import json, sys
sys.path.insert(0, {scripts_dir!r})
import mg_webengine_readiness as readiness

result = readiness._probe_inprocess(timeout_s={timeout!r})
print(readiness._MARKER + json.dumps({
    "ok": result.ok, "reason": result.reason, "diagnostics": result.diagnostics,
}))
"""


def _base_diagnostics() -> dict[str, str]:
    from PyQt6.QtCore import qVersion
    diag: dict[str, str] = {
        "qt_runtime": str(qVersion()),
        "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "chromium_flags": os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""),
        "python": sys.version.split()[0],
        "probe_url": _PROBE_URL,
    }
    try:
        from PyQt6.QtWebEngine import PYQT_WEBENGINE_VERSION  # type: ignore[attr-defined]
        diag["qt_webengine"] = str(PYQT_WEBENGINE_VERSION)
    except Exception:  # noqa: BLE001 — optional constant
        diag["qt_webengine"] = "unknown"
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        diag["platform_note"] = (
            "offscreen: editor-embedded browsers refuse this platform; "
            "standalone views sometimes still work")
    return diag


def _probe_inprocess(timeout_s: float = _LOAD_TIMEOUT_S) -> ReadinessResult:
    """Load a trivial page once (one retry) inside THIS process.

    Only called from the child subprocess — on hostile platforms it can
    crash, which is exactly why it never runs in the E2E host.
    """
    diag = _base_diagnostics()
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        try:  # QWebEnginePage lives in QtWebEngineCore on PyQt6 wheels
            from PyQt6.QtWebEngineCore import QWebEnginePage
        except ImportError:
            from PyQt6.QtWebEngineWidgets import QWebEnginePage  # type: ignore[no-redef]
    except ImportError as exc:
        return ReadinessResult(False, f"QtWebEngineWidgets not importable: {exc}", diag)

    from PyQt6.QtCore import QUrl
    from PyQt6.QtWidgets import QApplication

    class _ProbePage(QWebEnginePage):
        """Captures console messages (Qt6 pages expose no console signal)."""

        def __init__(self) -> None:  # noqa: D107
            super().__init__()
            self.console: list[str] = []

        def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: ANN001
            self.console.append(f"{message} ({source}:{line})")

    app = QApplication.instance() or QApplication(["mg_readiness_probe"])
    history = ""
    for attempt in (1, 2):  # one controlled retry, then give up loudly
        view = QWebEngineView()
        page = _ProbePage()
        view.setPage(page)
        events: dict[str, object] = {"loaded": False, "render_terminated": None}

        def on_loaded(ok: bool) -> None:  # noqa: ANN001 — Qt signal
            events["loaded"] = ok

        def on_render_terminated(status, exit_code) -> None:  # noqa: ANN001
            events["render_terminated"] = f"{status} (exit {exit_code})"

        page.loadFinished.connect(on_loaded)
        page.renderProcessTerminated.connect(on_render_terminated)
        diag["attempt"] = str(attempt)

        view.load(QUrl(_PROBE_URL))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not events["loaded"]:
            app.processEvents()
            time.sleep(0.05)
        end = time.monotonic() + 0.2  # let console messages settle
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        diag["last_console"] = " | ".join(page.console[-3:]) or "(none)"
        diag["load_result"] = "ok" if events["loaded"] else "no loadFinished"
        diag["render_process"] = str(events.get("render_terminated") or "running")
        ok = bool(events["loaded"])
        page.deleteLater()
        view.deleteLater()
        if ok:
            return ReadinessResult(True, "trivial page loaded", diag)
        history += f"attempt {attempt}: not loaded in {timeout_s:.0f}s; "
    diag["failure_history"] = history
    return ReadinessResult(
        False,
        "page never emitted a successful loadFinished (see last_console / "
        "load_result / render_process in diagnostics)",
        diag,
    )


# ── parent side ──────────────────────────────────────────────────────────────

def probe_webengine(timeout_s: float = _SUBPROCESS_TIMEOUT_S) -> ReadinessResult:
    """Run the readiness probe in a child process; never crash the caller."""
    code = (_CHILD_CODE.replace("{scripts_dir!r}", repr(str(SCRIPTS_DIR)))
                       .replace("{timeout!r}", repr(_LOAD_TIMEOUT_S)))
    env = dict(os.environ)
    env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=timeout_s, env=env, cwd=str(SCRIPTS_DIR))
    except subprocess.TimeoutExpired:
        return ReadinessResult(
            False,
            f"probe subprocess did not answer within {timeout_s:.0f}s "
            "(WebEngine startup hang)",
            {"qt_qpa_platform": env.get("QT_QPA_PLATFORM", ""),
             "chromium_flags": env.get("QTWEBENGINE_CHROMIUM_FLAGS", "")})
    for line in completed.stdout.splitlines():
        if line.startswith(_MARKER):
            payload = json.loads(line[len(_MARKER):])
            return ReadinessResult(bool(payload["ok"]), str(payload["reason"]),
                                   {str(k): str(v) for k, v in payload["diagnostics"].items()})
    stderr_tail = (completed.stderr or "").strip().splitlines()[-3:]
    return ReadinessResult(
        False,
        f"probe subprocess exited {completed.returncode} before reporting "
        "(WebEngine crashed or failed to start)",
        {"qt_qpa_platform": env.get("QT_QPA_PLATFORM", ""),
         "chromium_flags": env.get("QTWEBENGINE_CHROMIUM_FLAGS", ""),
         "stderr_tail": " | ".join(stderr_tail) or "(none)"})


def print_diagnostics(result: ReadinessResult) -> None:
    print("\n  WebEngine readiness diagnostics " + "-" * 40)
    for key, value in result.diagnostics.items():
        print(f"    {key}: {value}")
    print("  " + "-" * 59)


def parse_browser_mode(argv: list[str]) -> str:
    """Return 'require' (default) or 'allow-skip' from the CLI flags."""
    if "--allow-skip-browser" in argv:
        return "allow-skip"
    return "require"
