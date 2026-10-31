"""W0-A spike: Qt WebEngine offscreen capture backend.

This is the concrete candidate backend for the feasibility gate.  It loads an
HTML fixture into a headless ``QWebEngineView``, applies the CSP + URL policy,
seeks the frame via a trusted bridge, and attempts an RGBA readback via
``QWebEngineView.grab()``.

EMPIRICAL FINDING (this environment, Qt 6.x offscreen platform):

    ``QWebEngineView.grab()`` on the ``QT_QPA_PLATFORM=offscreen`` platform
    returns a uniform (all-white) frame regardless of Chromium rasterizer
    flags.  WebEngine composites the page in a separate GPU/compositor process
    whose output never reaches the offscreen widget backing store, so the
    grabbed pixmap is blank.  This is precisely the failure mode the W0-A gate
    was written to detect.

The backend therefore classifies uniform captures as ``CaptureFailureCode.BLANK``
instead of returning them as valid frames, and ``is_available()`` runs a cheap
self-test so callers (and CI) skip-with-reason when the offscreen path cannot
produce pixels.  The ADR records this and the fallback decision (a pinned
external headless browser renderer) for the W2 HTML waves.
"""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from PyQt6.QtCore import QEventLoop

from .capture_backend import (
    CancelToken,
    CaptureEvidence,
    CaptureFailure,
    CaptureFailureCode,
    CaptureSurface,
    CapturedFrame,
    FrameCaptureBackend,
    is_uniform_frame,
)
from .playwright_backend import _SEEK_BRIDGE, _inject_csp_meta
from .security import (
    CspPolicy,
    PAGE_ORIGIN,
    ResourceDecision,
    UrlPolicy,
    UrlRequest,
)


# Chromium flags that maximize the chance of offscreen software compositing.
# They do NOT make grab() work on the offscreen platform (see module docstring)
# but are kept so the self-test and any future display-backed run are hermetic.
_DEFAULT_CHROMIUM_FLAGS = "--no-sandbox --disable-gpu --enable-software-rasterization"

_BACKEND_ID = "qt-webengine-grab"
_BACKEND_BUILD = "qt-webengine/6.x-offscreen-grab"
_CAPTURE_METHOD = "QWebEngineView.grab()->QImage(RGBA8888)"


def _ensure_offscreen() -> None:
    """Pin the offscreen QPA platform before any QWidget is constructed."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", _DEFAULT_CHROMIUM_FLAGS)


def _qt_request_allowed(
    url_policy: UrlPolicy,
    url: str,
    kind: str,
    *,
    is_page_origin: bool,
) -> bool:
    """Pure per-request decision shared by the network interceptor (AUD-30212).

    ``is_page_origin`` marks the host-owned initial main-frame navigation to
    ``PAGE_ORIGIN`` (``about:blank``) — the one URL this backend itself loads.
    Everything authored in the page goes through :meth:`UrlPolicy.evaluate`;
    a DENY becomes ``QWebEngineUrlRequestInfo.block``.  Kept as a pure
    function so the mapping is unit-testable without a Qt event loop.
    """
    if is_page_origin and url == PAGE_ORIGIN:
        return True
    return url_policy.evaluate(UrlRequest(url=url, kind=kind)).decision is ResourceDecision.ALLOW


class QtWebEngineGrabBackend(FrameCaptureBackend):
    """Captures HTML frames via a headless QWebEngineView.

    This backend is import-safe: PyQt6 is only imported when a capture or
    self-test actually runs, so importing the spike never forces a Qt
    application in non-WebEngine environments.

    Main-thread constraint (AUD-30217): Qt requires the ``QApplication`` and
    every ``QWidget`` (WebEngine included) to live on the process main thread.
    Unlike the playwright backend (whose captures may run on pool threads),
    :meth:`capture` must be called on the main thread; a call from any other
    thread fails closed with ``CaptureFailureCode.UNSUPPORTED`` instead of
    constructing QApplication/WebEngine off the main thread (crash or
    undefined behaviour).
    """

    backend_id = _BACKEND_ID
    backend_build = _BACKEND_BUILD

    def __init__(self) -> None:
        self._availability_cache: bool | None = None

    def is_available(self) -> bool:
        """True only if a real offscreen capture produces non-uniform pixels.

        The check is memoized per instance.  It runs a minimal capture and
        inspects whether the result is a valid (non-blank) frame; on the
        offscreen platform this returns False, which is the documented gate.
        """
        if self._availability_cache is not None:
            return self._availability_cache
        probe_html = (
            "<html><head><style>html,body{margin:0;background:#1034a6}"
            "div{position:absolute;left:8px;top:8px;width:40px;height:30px;"
            "background:#ff8800}</style></head>"
            "<body><div></div></body></html>"
        )
        result = self.capture(
            probe_html,
            CaptureSurface(96, 54, 1.0),
            frame=0,
            fps_num=24,
            fps_den=1,
            csp=CspPolicy().to_header(),
            url_policy=UrlPolicy(),
            timeout_ms=8000,
        )
        self._availability_cache = isinstance(result, CapturedFrame)
        return self._availability_cache

    def capture(
        self,
        html: str,
        surface: CaptureSurface,
        *,
        frame: int,
        fps_num: int,
        fps_den: int,
        csp: str,
        url_policy: UrlPolicy,
        font_set: Mapping[str, str] = (),
        timeout_ms: int = 10000,
        cancel_token: CancelToken | None = None,
        transparent: bool = False,
        allow_uniform: bool = False,
    ) -> CapturedFrame | CaptureFailure:
        # Qt offscreen grab is known to lose compositor pixels on this
        # platform. Keep it fail-closed even when a caller is exporting a
        # validated scene; only the pinned Playwright backend may preserve a
        # legitimate uniform authored frame.
        del allow_uniform
        if not html or not isinstance(html, str):
            return self._failure(CaptureFailureCode.INVALID_INPUT, "empty html", None)
        # AUD-30217: fail closed before any Qt import when called off the main
        # thread — constructing QApplication/WebEngine there is undefined
        # behaviour (see the class docstring). The check runs before the PyQt6
        # imports so it needs no Qt runtime to be present.
        if threading.current_thread() is not threading.main_thread():
            return self._failure(
                CaptureFailureCode.UNSUPPORTED,
                "qt webengine capture must run on the main thread (AUD-30217)",
                None)
        start = time.monotonic()
        _ensure_offscreen()
        try:
            from PyQt6.QtCore import QEventLoop, QObject, QTimer, QUrl
            from PyQt6.QtGui import QImage
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtWebEngineCore import (
                QWebEnginePage,
                QWebEngineProfile,
                QWebEngineSettings,
                QWebEngineUrlRequestInfo,
                QWebEngineUrlRequestInterceptor,
            )
            from PyQt6.QtWebEngineWidgets import QWebEngineView
        except Exception as exc:  # pragma: no cover - environment dependent
            return self._failure(CaptureFailureCode.UNSUPPORTED, f"pyqt webengine import failed: {exc}", None)

        # AUD-30212: map QWebEngine resource types onto UrlRequest kinds so
        # the interceptor evaluates the same policy vocabulary as the
        # Playwright backend (navigation/image/font/...).
        _request_info = QWebEngineUrlRequestInfo
        kind_by_type: dict[int, str] = {}
        for _member_name, _kind in (
            ("ResourceTypeMainFrame", "navigation"),
            ("ResourceTypeSubFrame", "navigation"),
            ("ResourceTypeStylesheet", "style"),
            ("ResourceTypeScript", "script"),
            ("ResourceTypeImage", "image"),
            ("ResourceTypeFontResource", "font"),
            ("ResourceTypeMedia", "media"),
            ("ResourceTypeXhr", "connect"),
            ("ResourceTypeFetch", "connect"),
            ("ResourceTypePing", "connect"),
            ("ResourceTypeServiceWorker", "script"),
            ("ResourceTypeWorker", "script"),
            ("ResourceTypeCspReport", "connect"),
            ("ResourceTypePluginResource", "resource"),
        ):
            _member = getattr(_request_info, _member_name, None)
            if _member is not None:
                kind_by_type[int(_member)] = _kind
        _main_frame_type = int(getattr(
            _request_info, "ResourceTypeMainFrame",
            getattr(_request_info, "ResourceTypeUnknown", -1),
        ))

        policy = url_policy  # captured for the QObject below

        class _UrlPolicyInterceptor(QWebEngineUrlRequestInterceptor):
            """Enforce the capture URL policy at the Chromium network layer."""

            def __init__(self, parent: QObject) -> None:
                super().__init__(parent)
                self._policy = policy

            def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:  # noqa: N802
                try:
                    url = info.requestUrl().toString()
                    resource_type = int(info.resourceType())
                    kind = kind_by_type.get(resource_type, "resource")
                    allowed = _qt_request_allowed(
                        self._policy, url, kind,
                        is_page_origin=resource_type == _main_frame_type,
                    )
                    if not allowed:
                        info.block(True)
                except Exception:
                    # Fail closed: an interceptor error must never degrade
                    # into allowing an unvalidated request through.
                    try:
                        info.block(True)
                    except Exception:  # pragma: no cover - teardown race
                        pass

        # Qt's Chromium command-line parser aborts when QApplication receives
        # an empty argv (notably in the isolated W0-A subprocess).  Supplying
        # a stable program name keeps the backend's documented fail-closed
        # self-test alive so it can report BLANK/UNSUPPORTED instead of
        # terminating the whole host process.
        app = QApplication.instance() or QApplication(["topos-qt-html-capture"])
        # AUD-30214: a NAMED QWebEngineProfile is disk-backed — it persists
        # HTTP cache/localStorage/IndexedDB under ~/.QtWebEngine even with
        # NoPersistentCookies, contradicting the backend's no-residue
        # contract.  The nameless construction is the off-the-record profile;
        # an in-memory HTTP cache removes the last disk footprint.
        profile = QWebEngineProfile()
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        try:
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        except AttributeError:  # pragma: no cover - older Qt builds
            pass
        # AUD-30212: URL-layer enforcement.  Parenting the interceptor to the
        # profile keeps it alive for the lifetime of the page.
        interceptor = _UrlPolicyInterceptor(profile)
        profile.setUrlRequestInterceptor(interceptor)
        settings = profile.settings()
        # AUD-30213: the trusted seek bridge (page.runJavaScript) needs JS in
        # the page context, mirroring the Playwright backend's
        # ``java_script_enabled=True`` stance.  Authored scripts stay blocked
        # by the injected CSP (``script-src 'none'`` in the strict profile)
        # plus the URL interceptor below — defence in depth, not JS denial.
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        for attr in (
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            QWebEngineSettings.WebAttribute.AllowRunningInsecureContent,
            QWebEngineSettings.WebAttribute.PluginsEnabled,
            QWebEngineSettings.WebAttribute.FullScreenSupportEnabled,
            QWebEngineSettings.WebAttribute.AllowWindowActivationFromJavaScript,
        ):
            settings.setAttribute(attr, False)

        page = QWebEnginePage(profile)
        # AUD-30213: inject the CSP into the *authored document* (same helper
        # as the Playwright backend) instead of rebuilding a shell around an
        # extracted <body> — dropping the authored <head>/<style> made every
        # fixture render as unstyled text and hollowed out the determinism
        # evidence.
        document = _inject_csp_meta(html, csp)
        view = QWebEngineView()
        view.setPage(page)
        view.resize(surface.width, surface.height)
        view.setHtml(document, QUrl(PAGE_ORIGIN))

        loop = QEventLoop()
        loaded_ok: dict[str, bool] = {}

        def _on_loaded(ok: bool) -> None:
            loaded_ok["ok"] = ok
            loop.quit()

        page.loadFinished.connect(_on_loaded)
        # Hard timeout guard.
        QTimer.singleShot(int(timeout_ms), loop.quit)
        # Cooperative cancel guard.
        if cancel_token is not None:
            QTimer.singleShot(50, lambda: self._poll_cancel(cancel_token, loop))

        loop.exec()

        if cancel_token is not None and cancel_token.is_cancelled:
            return self._failure(CaptureFailureCode.CANCELLED, "cancelled before capture", None)
        if not loaded_ok.get("ok", False):
            return self._failure(CaptureFailureCode.TIMEOUT, "page did not finish loading", None)

        # AUD-30213: seek the authored frame through the same trusted bridge
        # the Playwright backend uses, so CSS/WAAPI animations land on the
        # requested timeline frame instead of the wall-clock phase.  The
        # result is recorded as evidence (not merely echoing frame/fps back).
        seek_loop = QEventLoop()
        seek_result: dict[str, object] = {}

        def _on_seek_done(result: object) -> None:
            seek_result["done"] = True
            seek_result["value"] = result
            seek_loop.quit()

        page.runJavaScript(
            f"({_SEEK_BRIDGE})({int(frame)}, {int(fps_num)}, {int(fps_den)})",
            _on_seek_done,
        )
        QTimer.singleShot(min(int(timeout_ms), 5000), seek_loop.quit)
        seek_loop.exec()
        seek_value = seek_result.get("value")
        seek_notes: object = seek_value if isinstance(seek_value, dict) else "seek-timeout"

        # Two paint cycles to let the compositor settle before readback.
        for _ in range(2):
            app.processEvents()
            QTimer.singleShot(30, lambda: None)

        pixmap = view.grab()
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        ptr = image.bits()
        ptr.setsize(image.sizeInBytes())
        data = bytes(ptr)
        duration_ms = (time.monotonic() - start) * 1000.0
        evidence = CaptureEvidence(
            backend_id=self.backend_id,
            backend_build=self.backend_build,
            capture_method=_CAPTURE_METHOD,
            viewport=(surface.width, surface.height),
            dpr=surface.dpr,
            duration_ms=duration_ms,
            notes={
                "csp": csp, "frame": int(frame), "fps": f"{fps_num}/{fps_den}",
                "url_policy_enforced": True, "seek": seek_notes,
            },
        )
        # Trim/validate to the requested surface (grab() honours the widget size).
        expected = surface.width * surface.height * 4
        if len(data) < expected:
            view.setPage(QWebEnginePage(profile))  # detach
            return self._failure(CaptureFailureCode.CRASH, f"short readback {len(data)} < {expected}", evidence)
        data = data[:expected]
        uniform, sample = is_uniform_frame(data, surface.width, surface.height)
        try:
            page.deleteLater()
            view.deleteLater()
            profile.deleteLater()
        except Exception:  # pragma: no cover - teardown best-effort
            pass
        if uniform:
            # THE W0-A FINDING: offscreen grab() yields a uniform blank frame.
            return self._failure(
                CaptureFailureCode.BLANK,
                f"offscreen grab() returned uniform frame {sample}; compositor did not reach backing store",
                evidence,
            )
        return CapturedFrame(data=data, surface=surface, evidence=evidence)

    def _poll_cancel(self, token: CancelToken, loop: "QEventLoop") -> None:
        if token.is_cancelled:
            loop.quit()
        else:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(50, lambda: self._poll_cancel(token, loop))

    def _failure(self, code: CaptureFailureCode, message: str, evidence: CaptureEvidence | None) -> CaptureFailure:
        return CaptureFailure(code=code, message=message, backend_id=self.backend_id, evidence=evidence)


__all__ = ["QtWebEngineGrabBackend"]
