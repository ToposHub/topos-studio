"""W0-A: pinned external headless-browser capture backend (Playwright/Chromium).

The Qt WebEngine offscreen path returns uniform blank frames (see ADR 0001 and
:mod:`qt_capture_backend`), which failed the W0-A gate.  This module is the
fallback the ADR named: a **pinned** external headless browser that *does*
produce real, deterministic offscreen RGBA.

It implements the same :class:`FrameCaptureBackend` contract as the Qt backend
so the orchestrator (``HtmlRendererSpike``) and the gate tests are agnostic to
which renderer is active.  Per the plan's W0-A / S-00 requirements the backend:

* reports a stable ``backend_build`` (playwright python version + the browser
  revision captured from the page's user-agent) so it participates in cache
  keys and cross-build comparisons;
* applies the CSP as a meta tag and a **deny-by-default** request interceptor
  that fulfils only bound ``topos-asset://`` resources and aborts every other
  scheme (no network / file / data / blob / javascript escape);
* injects a **trusted seek bridge** via ``page.evaluate`` (privileged, not
  subject to page CSP) that pauses every animation and seeks it to the frame's
  time, then sets ``--topos-frame/time/progress`` — so capture is a function of
  the frame, never the wall clock;
* waits for ``document.fonts.ready`` and two animation frames before capture;
* decodes the PNG screenshot to RGBA8 and rejects uniform frames as BLANK
  (defence in depth, even though this backend does not exhibit the Qt failure);
* honours the cancel token and maps playwright timeout / process termination to
  TIMEOUT / CRASH — never returning a stale or blank frame as success.

The browser is launched fresh per capture so each job owns an isolated context
(ephemeral profile, no cookies/storage residue) and a crash cannot corrupt the
next job — directly exercising the "cancel/crash/restart with no residue" gate.
"""

from __future__ import annotations

import io
import threading
import time
from importlib import metadata as _importlib_metadata
from typing import Any, Mapping

from ...core.html_page_deck import _PAGE_VIEWPORT_RECOVERY_JS
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
from .security import ResourceDecision, UrlPolicy, UrlRequest


_PLAYWRIGHT_VERSION: str = ""
try:  # pragma: no cover - depends on install
    _PLAYWRIGHT_VERSION = _importlib_metadata.version("playwright")
except _importlib_metadata.PackageNotFoundError:  # pragma: no cover
    _PLAYWRIGHT_VERSION = "unknown"

_BACKEND_ID = "playwright-chromium"
_CAPTURE_METHOD = "chromium.headless->screenshot(png)->PIL(RGBA8888)"
# Bump when capture semantics change for identical (html, frame) inputs, so
# frame caches built by an older backend are invalidated naturally.
# clock1: the virtual clock shim drives authored rAF/timers/performance.now,
# so JS-animated pages now capture at the exact timeline state.
_CLOCK_BUILD = "clock1"


# Playwright's sync API is thread-affine: a runtime + its browsers MUST be
# driven from the thread that started them.  (A second runtime on the SAME
# thread raises "sync API inside the asyncio loop"; a runtime/browser touched
# from ANOTHER thread raises TargetClosedError.)  RenderWorker runs captures on
# a pool thread while is_available()/direct V2 runs on the main thread, so the
# runtime + browser are held PER THREAD: each thread lazily starts its own
# runtime and launches its own chromium process, then reuses them across
# captures (the launch dominates per-frame cost ~0.9s vs ~0.2s for a context).
# All runtimes are stopped at process exit.
_PW_LOCAL = threading.local()
_PW_RUNTIMES: list[Any] = []
_PW_LOCK = threading.Lock()


def _thread_browser(
    headless: bool,
    allow_no_sandbox: bool,
    *,
    allow_gpu_apis: bool = False,
):
    """The calling thread's cached Chromium browser for one render profile.

    CPU and GPU Chromium processes are kept as separate profiles.  Reusing a
    CPU browser for a WebGL clip would silently disable the very APIs the
    author requested; relaunching only when the profile changes keeps ordinary
    deterministic captures cheap while making the freeform path honest.
    """
    profile = (bool(headless), bool(allow_no_sandbox), bool(allow_gpu_apis))
    browser = getattr(_PW_LOCAL, "browser", None)
    if browser is not None and getattr(_PW_LOCAL, "browser_profile", None) == profile:
        return browser
    if browser is not None:
        try:
            browser.close()
        except Exception:  # pragma: no cover - crash recovery best effort
            pass
        _PW_LOCAL.browser = None
    runtime = getattr(_PW_LOCAL, "runtime", None)
    if runtime is None:
        from playwright.sync_api import sync_playwright
        runtime = sync_playwright().start()
        _PW_LOCAL.runtime = runtime
        with _PW_LOCK:
            _PW_RUNTIMES.append(runtime)
        import atexit
        atexit.register(_shutdown_all_runtimes)
    browser = runtime.chromium.launch(
        headless=headless,
        args=_launch_args(allow_no_sandbox, allow_gpu_apis=allow_gpu_apis),
    )
    _PW_LOCAL.browser = browser
    _PW_LOCAL.browser_profile = profile
    return browser


def _drop_thread_browser() -> None:
    """Close + forget the calling thread's browser (crash recovery)."""
    browser = getattr(_PW_LOCAL, "browser", None)
    _PW_LOCAL.browser = None
    _PW_LOCAL.browser_profile = None
    if browser is not None:
        try:
            browser.close()
        except Exception:  # pragma: no cover - teardown best-effort
            pass


def _shutdown_all_runtimes() -> None:
    with _PW_LOCK:
        runtimes = list(_PW_RUNTIMES)
        _PW_RUNTIMES.clear()
    for runtime in runtimes:
        try:
            runtime.stop()
        except Exception:  # pragma: no cover - teardown best-effort
            pass


def _asset_content_type(asset_id: str) -> str:
    """MIME type for a bound asset, inferred from its topos-asset id.

    Fonts need the correct ``font/*`` type so ``@font-face`` src loads them
    cleanly (Chromium tolerates octet-stream, but a precise type is more
    robust and shows up correctly in diagnostics).  Images and other assets
    fall back to a generic stream type; Chromium content-sniffs images.
    """
    aid = asset_id.lower()
    if aid.startswith("font/"):
        if aid.endswith(".woff2"):
            return "font/woff2"
        if aid.endswith(".woff"):
            return "font/woff"
        return "font/ttf"            # the packaged DejaVu set is TTF
    if aid.endswith(".png"):
        return "image/png"
    if aid.endswith(".jpg") or aid.endswith(".jpeg"):
        return "image/jpeg"
    if aid.endswith(".svg"):
        return "image/svg+xml"
    if aid.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _launch_args(allow_no_sandbox: bool, *, allow_gpu_apis: bool = False) -> list[str]:
    """Chromium launch flags for a deterministic, sandboxed headless capture.

    * The generic profile uses ``--disable-gpu`` so deterministic HTML tests
      stay byte-stable across machines.  The Motion Graphic freeform profile
      omits that flag and explicitly enables GPU compositing so authored
      WebGL/CSS-3D/canvas effects can reach Chromium.
    * ``--disable-dev-shm-usage`` avoids /dev/shm exhaustion in containers,
      which otherwise crashes the renderer — a common reason projects reach for
      ``--no-sandbox``.  We keep the sandbox ON instead.

    ``--no-sandbox`` is appended ONLY when the caller explicitly opts in
    (CI/root containers that cannot run the setuid sandbox).  It is never the
    default because this backend renders untrusted, model-generated HTML.
    """
    args = ["--disable-dev-shm-usage"]
    if allow_gpu_apis:
        args.append("--enable-gpu")
    else:
        args.append("--disable-gpu")
    if allow_no_sandbox:
        args.append("--no-sandbox")
    return args


# Virtual clock shim.  Registered as a context init script (runs before any
# authored script) so the browser's OWN time sources drive off the timeline:
# requestAnimationFrame / setTimeout / setInterval / performance.now() /
# Date.now() all read a virtual clock that the seek bridge advances frame by
# frame.  A webpage authored the ordinary way — a rAF render loop, GSAP,
# Canvas/WebGL scenes, staggered setTimeout reveals — is therefore captured
# deterministically WITHOUT knowing anything about the host protocol
# (``topos-seek`` remains available as an optional advanced interface).
#
# Phases:
# * pre-arm (page load until the first pump): rAF registrations are held in a
#   virtual queue (authored loops do not run on the wall clock); short
#   setTimeout mirrors to native so load-time init patterns (and the host's
#   own fonts-race fallback) behave exactly like Chrome;
# * pump (per captured frame): virtual time steps from 0 to the frame's time
#   in 1/fps increments, firing due timers and one rAF tick per step so both
#   time-driven AND integrator-style (state += dt) animations land on the
#   exact frame; bounded by a step cap and a real-time budget;
# * post-pump: rAF delegates to native with frozen virtual time so layout and
#   paint settle for the screenshot (the two-rAF wait keeps working).
_CLOCK_SHIM = r"""
(() => {
  if (window.__toposClock) return;
  const nativeRaf = window.requestAnimationFrame.bind(window);
  const nativeCancelRaf = window.cancelAnimationFrame.bind(window);
  const nativeSetTimeout = window.setTimeout.bind(window);
  const nativeClearTimeout = window.clearTimeout.bind(window);
  const nativeClearInterval = window.clearInterval.bind(window);
  const nativePerfNow = performance.now.bind(performance);
  // Every capture uses a fresh ephemeral page.  A wall-clock origin sampled
  // here would therefore change from frame to frame, even when the requested
  // timeline frame advances by a perfectly uniform amount.  Keep a stable
  // epoch so Date.now() remains a useful browser-compatible time source while
  // animation phase is determined solely by the timeline clock below.
  const wall0 = 1700000000000;
  const state = { now: 0, armed: false, pumping: false };
  const rafCallbacks = new Map();
  const timers = new Map();
  let rafSeq = 1000000000;
  let timerSeq = 1000000000;

  const looksLikePageAutoplay = (callback) => {
    let source = '';
    try { source = Function.prototype.toString.call(callback); } catch (e) { return false; }
    // Motion Graphic HTML is freeform, but page boundaries belong to the
    // host.  Suppress the common authored auto-advance interval patterns in
    // capture mode; ordinary rAF/CSS/Web Animations remain untouched.
    return /(?:switchPage|agentSwitchPage|nextPage|prevPage|nextSlide|prevSlide|currentPage|totalPages|data-topos-page)/i.test(source);
  };

  performance.now = () => state.now;
  Date.now = () => wall0 + Math.floor(state.now);

  window.requestAnimationFrame = (callback) => {
    if (state.armed && !state.pumping) {
      // Post-pump live window (layout/paint settle before the screenshot):
      // delegate to the native frame with the frozen virtual timestamp.
      return nativeRaf(() => { try { callback(state.now); } catch (e) { /* authored code */ } });
    }
    // Before and during the pump, rAF ticks are virtual so the loop steps
    // through EVERY timeline frame inside the pump — integrator-style motion
    // (physics, easing chases) integrates per frame instead of jumping once.
    const id = rafSeq++;
    rafCallbacks.set(id, callback);
    return id;
  };
  window.cancelAnimationFrame = (id) => {
    if (rafCallbacks.delete(id)) return;
    try { nativeCancelRaf(id); } catch (e) { /* foreign id */ }
  };

  const clearNativeMirror = (entry) => {
    if (entry.native !== null) {
      try { nativeClearTimeout(entry.native); } catch (e) { /* already fired */ }
      entry.native = null;
    }
  };
  window.setTimeout = (callback, delay, ...args) => {
    if (typeof callback !== 'function') {
      return nativeSetTimeout(callback, delay, ...args);
    }
    const ms = Math.max(0, Number(delay) || 0);
    const id = timerSeq++;
    const entry = { callback, args, deadline: state.now + ms, interval: 0, native: null };
    timers.set(id, entry);
    if (!state.armed) {
      // Pre-arm mirror: behave like a normal browser while the page loads.
      // Once armed, the virtual queue owns the timer; the mirror no-ops so a
      // timer can never fire twice (real time AND virtual time).
      entry.native = nativeSetTimeout(() => {
        if (!state.armed && timers.get(id) === entry) {
          timers.delete(id);
          try { callback(...args); } catch (e) { /* authored code */ }
        }
      }, ms);
    }
    return id;
  };
  window.clearTimeout = (id) => {
    const entry = timers.get(id);
    if (entry) { clearNativeMirror(entry); timers.delete(id); return; }
    try { nativeClearTimeout(id); } catch (e) { /* foreign id */ }
  };
  window.setInterval = (callback, interval, ...args) => {
    const ms = Math.max(1, Number(interval) || 1);
    const id = timerSeq++;
    timers.set(id, {
      callback, args, deadline: state.now + ms, interval: ms, native: null,
      pageAutoplay: looksLikePageAutoplay(callback),
    });
    return id;
  };
  window.clearInterval = (id) => {
    const entry = timers.get(id);
    if (entry) { clearNativeMirror(entry); timers.delete(id); return; }
    try { nativeClearInterval(id); } catch (e) { /* foreign id */ }
  };

  const fireDueTimers = (upToMs) => {
    for (const [id, entry] of Array.from(timers)) {
      if (entry.deadline > upToMs) continue;
      if (entry.interval > 0) {
        // setInterval: one catch-up fire per pump step max, never loop forever
        // when the interval is shorter than the step.
        entry.deadline = Math.max(entry.deadline + entry.interval, upToMs);
        try { entry.callback(...entry.args); } catch (e) { /* authored code */ }
      } else {
        timers.delete(id);
        clearNativeMirror(entry);
        try { entry.callback(...entry.args); } catch (e) { /* authored code */ }
      }
    }
  };
  const fireRafBatch = (timestampMs) => {
    const callbacks = Array.from(rafCallbacks.values());
    rafCallbacks.clear();
    for (const callback of callbacks) {
      try { callback(timestampMs); } catch (e) { /* authored code */ }
    }
    return callbacks.length;
  };

  window.__toposClock = {
    now: () => state.now,
    suppressPageAutoplay() {
      for (const [id, entry] of Array.from(timers)) {
        if (!entry.interval || !entry.pageAutoplay) continue;
        clearNativeMirror(entry);
        timers.delete(id);
      }
    },
    // Advance the virtual clock to targetMs, stepping per timeline frame.
    pump(targetMs, stepMs) {
      state.armed = true;
      const step = Math.max(1, Number(stepMs) || 16);
      const target = Math.max(state.now, Number(targetMs) || 0);
      // Keep the simulation inside the capture timeout even for very long
      // clips or heavy canvas scenes: step cap + real-time budget.  When the
      // budget runs out the clock still jumps to the target (so the page is
      // in a defined state), but the jump is REPORTED — integration-style
      // animations would have skipped steps, so the frame is not byte-
      // deterministic and the Python side fails the capture instead of
      // silently caching a machine-dependent frame (AUD-30207).
      const maxSteps = 2400;
      const realDeadline = nativePerfNow() + 2500;
      let fired = 0;
      let steps = 0;
      let jumped = false;
      state.pumping = true;
      try {
        while (state.now < target && steps < maxSteps && nativePerfNow() < realDeadline) {
          state.now = Math.min(target, state.now + step);
          fireDueTimers(state.now);
          fired += fireRafBatch(state.now);
          steps++;
        }
        if (state.now < target) {
          jumped = true;
          state.now = target;
          fireDueTimers(target);
        }
        // Final drain: a render loop re-registers from inside its callback, so
        // the last tick's re-registration must still run at the exact target
        // time (bounded — each round adds one, four is far beyond any chain).
        for (let round = 0; round < 4 && rafCallbacks.size; round++) {
          fired += fireRafBatch(state.now);
        }
      } finally {
        state.pumping = false;
      }
      return { raf: fired, steps: steps, now: state.now, jumped: jumped };
    },
  };
})();
"""


# Trusted seek bridge.  Runs via page.evaluate (privileged context; not subject
# to page CSP) so it can pause + seek authored animations deterministically.
# Returns the count of animations it froze plus the computed time, for evidence.
_SEEK_BRIDGE = r"""
(frame, fpsNum, fpsDen) => {
  const root = document.documentElement;
  const timeMs = (frame * fpsDen / fpsNum) * 1000;
  root.style.setProperty('--topos-frame', String(frame));
  root.style.setProperty('--topos-time', String(timeMs / 1000));
  const dur = (typeof document !== 'undefined' && document.body)
    ? Math.max(1, parseFloat(getComputedStyle(document.body).getPropertyValue('--topos-duration') || '0')) : 0;
  root.style.setProperty('--topos-progress', dur > 0 ? String(Math.min(1, (timeMs/1000)/dur)) : '0');
  // Motion Graphic page decks are editor-timed.  The model's CSS remains
  // available for page-internal motion, while this bridge selects exactly one
  // page for the requested timeline frame.
  const pageMeta = document.querySelector('meta[data-topos-page-deck]');
  const pages = Array.from(document.querySelectorAll('[data-topos-page]'));
  if (window.__toposClock && window.__toposClock.suppressPageAutoplay) {
    window.__toposClock.suppressPageAutoplay();
  }
  /*__TOPOS_PAGE_VIEWPORT_RECOVERY__*/
  let pageIndex = 0;
  // Page animations are authored like ordinary browser animations.  When a
  // deck switches page, seek the active page from the beginning of its own
  // interval instead of seeking it against the clip's absolute clock; this
  // keeps entrance animations useful on page 2+ without letting the host
  // dictate their visual design.
  let activePageElapsedMs = timeMs;
  if (pages.length && pageMeta) {
    let durations = [];
    try { durations = JSON.parse(pageMeta.getAttribute('data-topos-page-durations') || '[]'); } catch (e) { durations = []; }
    const transition = String(pageMeta.getAttribute('data-topos-page-transition') || 'cut');
    const transitionSeconds = Math.max(0, Number(
      pageMeta.getAttribute('data-topos-page-transition-seconds') || '0'));
    let elapsed = timeMs / 1000;
    for (let i = 0; i < pages.length; i++) {
      const seconds = Number(durations[i] || durations[durations.length - 1] || 5);
      if (elapsed < seconds || i === pages.length - 1) { pageIndex = i; break; }
      elapsed -= seconds;
    }
    activePageElapsedMs = Math.max(0, elapsed) * 1000;
    const transitionProgress = (pageIndex > 0 && transition !== 'cut' && transitionSeconds > 0)
      ? Math.min(1, Math.max(0, elapsed / transitionSeconds)) : 1;
    const transitioning = pageIndex > 0 && transitionProgress < 1;
    pages.forEach((page, index) => {
      const active = index === pageIndex;
      // A hidden page must not retain a normal-flow box.  ``visibility``
      // would keep previous pages' height and push later responsive pages
      // below the viewport; the host recovery then had to translate them,
      // producing a partial white band in the video.
      const visibleForTransition = transitioning && index === pageIndex - 1;
      if (!active && !visibleForTransition) {
        page.setAttribute('data-topos-hidden', '1');
      } else {
        page.removeAttribute('data-topos-hidden');
      }
      page.setAttribute('data-topos-active', active ? '1' : '0');
      // Keep compatibility with common browser slide decks whose authored
      // visibility and entrance rules are written as ``.slide.active``.
      // The host still exposes the data attribute as the canonical API, but
      // mirroring the conventional class makes an authored webpage behave
      // the same in Chrome and in deterministic capture.
      page.classList.toggle('active', active);
      // Preserve authored page dimensions when they exist (for example a
      // centered 16:9 slide card), but repair a zero-sized absolute wrapper
      // so the common ``.page`` pattern still fills the capture canvas.
      if (active || visibleForTransition) {
        const rect = page.getBoundingClientRect();
        page.classList.toggle(
          'topos-page-auto-box',
          rect.width < 1 || rect.height < 1 || transitioning,
        );
      }
      page.style.setProperty('--topos-page-time', active ? String(elapsed) : '0');
      const pageSeconds = Number(durations[index] || durations[durations.length - 1] || 5);
      page.style.setProperty('--topos-page-progress', active
        ? String(Math.min(1, Math.max(0, elapsed / Math.max(0.001, pageSeconds)))) : '0');
      page.removeAttribute('data-topos-transitioning');
      page.removeAttribute('data-topos-transition-role');
      page.removeAttribute('data-topos-transition');
      page.style.removeProperty('--topos-transition-progress');
      page.style.removeProperty('--topos-transition-duration');
      if (transitioning && (index === pageIndex || index === pageIndex - 1)) {
        page.setAttribute('data-topos-transitioning', '1');
        page.setAttribute('data-topos-transition-role', active ? 'enter' : 'leave');
        page.setAttribute('data-topos-transition', transition);
        page.style.setProperty('--topos-transition-duration', String(transitionSeconds) + 's');
        page.style.setProperty('--topos-transition-progress', String(transitionProgress));
      }
    });
    root.style.setProperty('--topos-page-index', String(pageIndex));
  }
  window.__toposFrame = frame;
  window.__toposTime = timeMs / 1000;
  window.dispatchEvent(new CustomEvent('topos-seek', {
    detail: { frame: frame, time: timeMs / 1000,
      fps: fpsNum / Math.max(1, fpsDen),
      pageIndex: Number(root.style.getPropertyValue('--topos-page-index') || 0) }
  }));
  let frozen = 0;
  if (document.getAnimations) {
    const anims = document.getAnimations();
    for (const a of anims) {
      try {
        const target = a.effect && a.effect.target;
        const page = target && target.closest ? target.closest('[data-topos-page]') : null;
        const seekMs = page
          ? (page.getAttribute('data-topos-active') === '1' ? activePageElapsedMs : 0)
          : timeMs;
        a.pause(); a.currentTime = seekMs; frozen++;
      } catch (e) { /* ignore */ }
    }
  }
  // Backstop: freeze any CSS animation that is not exposed via getAnimations.
  const s = document.createElement('style');
  s.id = 'topos-freeze';
  s.textContent = '*{animation-play-state:paused !important}';
  if (!document.getElementById('topos-freeze') && document.head) document.head.appendChild(s);
  // Advance the virtual clock (rAF/timers/performance.now) to this frame so
  // ordinary authored JS animations land on the captured state.  Runs after
  // the CSS freeze and the topos-seek dispatch: CSS/WAAPI is seeked above,
  // JS-driven motion is simulated here.
  let clock = null;
  if (window.__toposClock) {
    try {
      clock = window.__toposClock.pump(timeMs, 1000 * fpsDen / Math.max(1, fpsNum));
    } catch (e) { clock = null; }
  }
  // The authored page is otherwise untouched.  If it contains visible DOM
  // but the entire composition is outside the capture viewport, apply the
  // host-owned minimal recovery after all authored animation clocks settle.
  recoverPageViewport(pages[pageIndex]);
  return { frozen: frozen, timeMs: timeMs, clock: clock };
}
"""

_SEEK_BRIDGE = _SEEK_BRIDGE.replace(
    "  /*__TOPOS_PAGE_VIEWPORT_RECOVERY__*/",
    _PAGE_VIEWPORT_RECOVERY_JS,
)

# Two animation frames of settle time (no wall-clock animation advance because
# everything is paused), so layout/paint reach the seeked state before capture.
_TWO_RAF = (
    "new Promise((resolve) => requestAnimationFrame("
    "() => requestAnimationFrame(resolve)))"
)

# Read-only diagnostics used when a capture is classified as uniform.  This
# does not inspect user data beyond small counts/geometry summaries; it makes a
# blank capture actionable (hidden DOM, no active page, or a genuinely solid
# design) instead of reporting only the sampled RGBA colour.
_DOM_DIAGNOSTICS = r"""
() => {
  const body = document.body;
  const nodes = body ? Array.from(body.querySelectorAll('*')) : [];
  const viewport = { width: window.innerWidth || 0, height: window.innerHeight || 0 };
  const intersectsViewport = (rect) =>
    rect.right > 0 && rect.bottom > 0 && rect.left < viewport.width && rect.top < viewport.height;
  const visible = nodes.filter((node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    const opacity = Number.parseFloat(style.opacity || '1');
    return rect.width > 0 && rect.height > 0 &&
      style.display !== 'none' && style.visibility !== 'hidden' &&
      opacity > 0;
  });
  const viewportVisible = visible.filter((node) => intersectsViewport(node.getBoundingClientRect()));
  const pages = Array.from(document.querySelectorAll('[data-topos-page]'));
  const activePages = pages.filter((node) =>
    node.getAttribute('data-topos-active') === '1');
  const activePageIndexes = pages.reduce((indexes, node, index) => {
    if (node.getAttribute('data-topos-active') === '1') indexes.push(index);
    return indexes;
  }, []);
  const recoveredPageIndexes = pages.reduce((indexes, node, index) => {
    if (node.getAttribute('data-topos-page-recovered') === '1') indexes.push(index);
    return indexes;
  }, []);
  const sample = visible.slice(0, 8).map((node) => {
    const rect = node.getBoundingClientRect();
    return {
      tag: String(node.tagName || '').toLowerCase(),
      id: String(node.id || ''),
      className: String(node.className || '').slice(0, 80),
      x: Math.round(rect.x), y: Math.round(rect.y),
      width: Math.round(rect.width), height: Math.round(rect.height),
    };
  });
  return {
    domNodes: nodes.length,
    visibleNodes: visible.length,
    textLength: body ? String(body.innerText || '').trim().length : 0,
    pageCount: pages.length,
    activePageCount: activePages.length,
    activePageIndexes,
    recoveredPageIndexes,
    viewport: [viewport.width, viewport.height],
    viewportVisibleNodes: viewportVisible.length,
    sample,
  };
}
"""


def _format_dom_diagnostics(value: object) -> str:
    """Format bounded browser diagnostics for a human-facing blank error."""
    if not isinstance(value, Mapping):
        return "DOM diagnostics unavailable"
    return (
        f"DOM节点={value.get('domNodes', '?')}，"
        f"可见节点={value.get('visibleNodes', '?')}，"
        f"文字长度={value.get('textLength', '?')}，"
        f"页面={value.get('pageCount', '?')}，"
        f"激活页面={value.get('activePageCount', '?')}"
        f"（{value.get('activePageIndexes', '?')}），"
        f"视口可见节点={value.get('viewportVisibleNodes', '?')}"
    )


class PlaywrightChromiumBackend(FrameCaptureBackend):
    """Pinned headless-Chromium capture backend (the W0-A fallback)."""

    backend_id = _BACKEND_ID

    def __init__(
        self,
        *,
        headless: bool = True,
        asset_resolver: "Mapping[str, bytes] | None" = None,
        allow_no_sandbox: bool = False,
    ) -> None:
        self._headless = headless
        # Optional id->bytes map so bound assets resolve to real content in W2.
        # The W0-A gate runs without one (assets fulfil to a deterministic 1x1).
        self._asset_resolver = asset_resolver
        self._availability_cache: bool | None = None
        self._captured_build: str = ""   # browser UA-derived build, filled on first capture
        # The sandbox MUST stay on by default: this backend renders untrusted,
        # model-generated HTML, so disabling the renderer sandbox is a real
        # security regression, not a convenience.  ``allow_no_sandbox`` exists
        # solely for CI/root containers that cannot run the setuid sandbox, and
        # must never be the production default.
        self._allow_no_sandbox = bool(allow_no_sandbox)
        # The chromium browser is held PER THREAD (see _thread_browser): the
        # backend itself owns no process state, so it is safe to share a pooled
        # backend across callers/threads — each calling thread's captures reuse
        # that thread's lazily-launched browser (the launch dominates per-frame
        # cost ~0.9s vs ~0.2s for a context).  Each capture still gets a FRESH
        # ephemeral context + page, so the "crash/restart with no residue"
        # invariant holds.

    # ------------------------------------------------------------------
    # Lifecycle helpers (delegating to the thread-local browser cache)
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Drop the calling thread's cached browser, if any.  Idempotent.

        Browsers live per-thread, so this only releases the browser on the
        thread that calls close (typically shutdown); runtimes on other threads
        are cleaned up by ``atexit``.
        """
        _drop_thread_browser()

    @property
    def backend_build(self) -> str:
        return (
            f"playwright-chromium/py{_PLAYWRIGHT_VERSION}/{_CLOCK_BUILD}/{self._browser_build}"
        )

    @property
    def _browser_build(self) -> str:
        # Prefer the real browser version observed from the page UA; fall back
        # to a placeholder before the first capture (is_available probes it).
        return self._captured_build or "chromium-headless-shell"

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """True only if chromium launches and returns a non-blank frame.

        Memoised per instance.  This is the honest gate: a backend that only
        produces uniform frames reports unavailable (the Qt offscreen failure
        mode), so callers skip-with-reason instead of trusting blanks.
        """
        if self._availability_cache is not None:
            return self._availability_cache
        probe = (
            "<html><head><style>html,body{margin:0;background:#1034a6}"
            "div{position:absolute;left:8px;top:8px;width:40px;height:30px;"
            "background:#ff8800}</style></head><body><div></div></body></html>"
        )
        result = self.capture(
            probe,
            CaptureSurface(96, 54, 1.0),
            frame=0, fps_num=24, fps_den=1,
            csp="default-src 'none'; style-src 'unsafe-inline'",
            url_policy=UrlPolicy(),
            timeout_ms=15000,
        )
        self._availability_cache = isinstance(result, CapturedFrame)
        return self._availability_cache

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
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
        if not html or not isinstance(html, str):
            return self._failure(CaptureFailureCode.INVALID_INPUT, "empty html", None)
        if cancel_token is not None and cancel_token.is_cancelled:
            return self._failure(CaptureFailureCode.CANCELLED, "cancelled before launch", None)
        if fps_num <= 0 or fps_den <= 0:
            return self._failure(CaptureFailureCode.INVALID_INPUT, "fps must be positive", None)
        # AUD-30206: the CSS viewport is ``round(physical / dpr)`` and
        # Chromium rasterises it back at ``device_scale_factor=dpr``.  When
        # the round-trip does not reproduce the physical surface (e.g. dpr=2
        # with an odd pixel width — Python's banker's rounding maps 1081→540
        # →1080), every capture of this resolution would decode-fail.  Reject
        # the combination up front with an actionable code instead of letting
        # the PNG decoder take the blame.
        if (round(max(1, round(surface.width / surface.dpr)) * surface.dpr) != surface.width
                or round(max(1, round(surface.height / surface.dpr)) * surface.dpr) != surface.height):
            return self._failure(
                CaptureFailureCode.INVALID_INPUT,
                f"surface {surface.width}x{surface.height} at dpr {surface.dpr} is not "
                "representable as a CSS viewport; use dimensions the dpr divides evenly",
                None,
            )

        start = time.monotonic()
        # Populated inside the capture try-block below; only read on the
        # success path (every exception path returns a failure first).
        rgba: bytes | None = None
        uniform = False
        sample: tuple[int, int, int, int] | None = None
        dom_diagnostics: Mapping[str, object] | None = None
        ua = ""
        seek_info: object = None
        try:
            from playwright.sync_api import Error as PWError
            from playwright.sync_api import TimeoutError as PWTimeout
        except Exception as exc:  # pragma: no cover - environment dependent
            return self._failure(CaptureFailureCode.UNSUPPORTED, f"playwright import failed: {exc}", None)

        document = _inject_csp_meta(html, csp)
        try:
            # Reuse the calling thread's chromium process; launch lazily on
            # first use (Playwright sync API is thread-affine).
            browser = _thread_browser(
                self._headless,
                self._allow_no_sandbox,
                allow_gpu_apis=url_policy.allow_gpu_apis,
            )
            # CSS viewport is the DESIGN grid (physical / dpr); the browser
            # layout at device_scale_factor=dpr then rasterises to the physical
            # pixel grid (surface.width x height).  Feeding physical pixels as
            # the CSS viewport (the old bug) doubled the layout at 2x DPR and
            # halved relative font sizes — CSS authors see the wrong viewport.
            css_w = max(1, round(surface.width / surface.dpr))
            css_h = max(1, round(surface.height / surface.dpr))
            # A FRESH ephemeral context per capture: no cookies/localStorage/
            # cache residue between frames, preserving the no-residue invariant
            # while reusing the browser process across captures.
            context = browser.new_context(
                viewport={"width": css_w, "height": css_h},
                device_scale_factor=float(surface.dpr),
                accept_downloads=False,
                bypass_csp=False,                  # enforce page CSP on authored content
                java_script_enabled=True,          # the trusted bridge needs JS
            )
            try:
                # Keep browser-authored randomness stable across the two
                # determinism probes and across repeated renders. This does
                # not constrain the design; it only prevents a page that uses
                # Math.random() for decorative placement from changing between
                # otherwise identical timeline frames.
                context.add_init_script(
                    """(() => {
                      let seed = 0x6d2b79f5;
                      Math.random = () => {
                        seed = (seed + 0x6d2b79f5) | 0;
                        let value = Math.imul(seed ^ (seed >>> 15), 1 | seed);
                        value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
                        return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
                      };
                    })();"""
                )
                # Virtual clock: authored rAF/timers/performance.now follow the
                # timeline instead of the wall clock (see _CLOCK_SHIM).
                context.add_init_script(_CLOCK_SHIM)
                context.set_default_timeout(timeout_ms)
                page = context.new_page()
                page.route("**/*", lambda route: self._intercept(route, url_policy))
                # A website may deliberately use public HTTPS fonts/images/CDN
                # resources.  Waiting for the browser's global ``load`` event
                # makes one slow or unreachable optional resource block the
                # entire Motion Graphic capture, even though the DOM and its
                # authored fallback CSS are already renderable.  A freeform
                # page may deliberately load a synchronous CDN script (for
                # example Tailwind's browser build), which can hold
                # DOMContentLoaded indefinitely when that CDN is slow. Start
                # after response commit instead; the bounded settle window
                # below gives resources time to paint without letting one
                # optional network request abort the entire export.
                page.set_content(document, wait_until="commit", timeout=timeout_ms)

                # In the freeform browser profile external CSS/fonts/CDN
                # scripts are part of the authored page.  DOMContentLoaded can
                # arrive before those resources have painted, which makes the
                # same page look like a bare fallback in the video even though
                # it looks correct a moment later in Chrome.  Give the browser
                # a bounded network-settle window; an unreachable optional CDN
                # still falls through to the author's fallback CSS.
                if url_policy.allow_network_resources:
                    try:
                        page.wait_for_load_state(
                            "networkidle", timeout=min(timeout_ms, 3500),
                        )
                    except PWTimeout:
                        pass

                if cancel_token is not None and cancel_token.is_cancelled:
                    return self._failure(CaptureFailureCode.CANCELLED, "cancelled after load", None)

                # Pin the browser build from the UA for cache-key stability.
                ua = page.evaluate("navigator.userAgent")
                self._captured_build = _chrome_version_from_ua(ua) or self._browser_build

                # Wait for fonts, then seek + 2 rAF settle.
                try:
                    page.evaluate(
                        """() => Promise.race([
                            (document.fonts && document.fonts.ready) || Promise.resolve(),
                            new Promise(resolve => setTimeout(resolve, 1500))
                        ])""",
                    )
                except PWError:
                    pass
                seek_info = page.evaluate(
                    f"({_SEEK_BRIDGE})({int(frame)}, {int(fps_num)}, {int(fps_den)})"
                )
                page.evaluate(_TWO_RAF)
                clock_info = seek_info.get("clock") if isinstance(seek_info, dict) else None
                if isinstance(clock_info, dict) and bool(clock_info.get("jumped")):
                    # AUD-30207: the virtual clock exhausted its step/real-time
                    # budget and teleported to the target time.  Integration-
                    # style animations would have skipped steps, so this frame
                    # is machine/load-dependent — fail it instead of letting a
                    # non-reproducible frame enter the cache (byte-determinism
                    # contract).
                    return self._failure(
                        CaptureFailureCode.TIMEOUT,
                        "virtual clock budget exhausted before the target frame; "
                        "the seeked state is not deterministic",
                        None,
                    )

                if cancel_token is not None and cancel_token.is_cancelled:
                    return self._failure(CaptureFailureCode.CANCELLED, "cancelled before capture", None)

                # omit_background follows the frame's alpha intent: a transparent
                # frame (alpha_mode != STRAIGHT) captures the page with alpha so
                # it composites over downstream layers; an opaque frame
                # composites over white as before.  AUD-30206: Playwright's
                # clip is in CSS pixels — the physical grid comes from the
                # device_scale_factor rasterisation, not from the clip.
                png = page.screenshot(
                    clip={"x": 0, "y": 0, "width": css_w, "height": css_h},
                    type="png", omit_background=transparent, timeout=timeout_ms,
                )
                # AUD-30208: classify the frame BEFORE the (expensive, full-
                # document forced-layout) DOM diagnostics, while the context is
                # still open.  Non-uniform success frames pay zero extra cost;
                # diagnostics run only for frames already classified uniform.
                rgba = _png_to_rgba8(png, surface.width, surface.height)
                if rgba is not None:
                    uniform, sample = is_uniform_frame(rgba, surface.width, surface.height)
                if uniform:
                    try:
                        raw_diagnostics = page.evaluate(_DOM_DIAGNOSTICS)
                        if isinstance(raw_diagnostics, Mapping):
                            dom_diagnostics = dict(raw_diagnostics)
                    except PWError:
                        # Diagnostics must never turn a render into a different
                        # failure; the screenshot path remains authoritative.
                        dom_diagnostics = None
            finally:
                try:
                    context.close()
                except Exception:  # pragma: no cover - teardown best-effort
                    pass
        except PWTimeout as exc:
            return self._failure(CaptureFailureCode.TIMEOUT, f"playwright timeout: {exc}", None)
        except PWError as exc:
            # The browser/context may be gone; drop this thread's browser so
            # the next capture relaunches a fresh process.  Report CRASH so the
            # service also replaces this worker (defence in depth).
            _drop_thread_browser()
            return self._failure(CaptureFailureCode.CRASH, f"playwright error: {exc}", None)

        if rgba is None:
            return self._failure(CaptureFailureCode.CRASH, "png decode failed", None)
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
                "ua": ua, "frozen_animations": seek_info.get("frozen") if isinstance(seek_info, dict) else None,
                "virtual_clock": seek_info.get("clock") if isinstance(seek_info, dict) else None,
                "transparent": bool(transparent),
                "css_viewport": [css_w, css_h],
                "dom_diagnostics": dom_diagnostics or {},
            },
        )
        uniform, sample = is_uniform_frame(rgba, surface.width, surface.height)
        if uniform and not allow_uniform:
            return self._failure(
                CaptureFailureCode.BLANK,
                f"capture returned uniform frame {sample}; "
                f"{_format_dom_diagnostics(dom_diagnostics)}",
                evidence,
            )
        if uniform:
            # A uniform frame is allowed only after an explicit scene-level
            # validation pass. Keep the evidence so export manifests can show
            # that the browser returned authored pixels rather than silently
            # substituting a fallback. CaptureEvidence is frozen, so replace
            # its notes mapping rather than mutating it in place.
            from dataclasses import replace as _replace
            evidence = _replace(
                evidence,
                notes={**dict(evidence.notes), "uniform_frame_allowed": True},
            )
        return CapturedFrame(data=rgba, surface=surface, evidence=evidence)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _intercept(self, route, url_policy: UrlPolicy) -> None:
        """Enforce the per-capture asset/HTTPS policy.

        The default policy still aborts every network request.  Motion Graphic
        freeform mode can explicitly allow HTTPS resources; those requests are
        continued inside the fresh, ephemeral Chromium context and never gain
        access to files, cookies, storage or the host application.
        """
        url = route.request.url
        scheme_sep = url.find("://")
        scheme = url[:scheme_sep].lower() if scheme_sep > 0 else ""
        if scheme == "topos-asset":
            # The asset id is the full authority+path after "://" and may itself
            # contain "/" (e.g. "font/dejavu-sans", "image/logo"); strip only a
            # trailing query/fragment.  Splitting on "/" would keep just the first
            # segment ("font", "image") — never matching the namespaced id in the
            # allowlist, so every namespaced asset aborted (fonts silently fell
            # back to system fonts, breaking the cross-machine determinism lock;
            # namespaced images rendered as the placeholder).
            asset_id = url[scheme_sep + 3:].split("?", 1)[0].split("#", 1)[0]
            bound_asset = (
                asset_id in url_policy.allowed_asset_ids
                or asset_id.split("/", 1)[0] in url_policy.allowed_asset_ids
            )
            if asset_id and bound_asset:
                payload = (self._asset_resolver or {}).get(asset_id)
                if payload is not None:
                    route.fulfill(status=200,
                                  content_type=_asset_content_type(asset_id),
                                  body=payload)
                elif asset_id.startswith("font/"):
                    # A font asset must resolve to real bytes; serving a PNG
                    # placeholder would make @font-face silently fall back to a
                    # system font and break the cross-machine determinism lock.
                    # Abort so the missing font is observable, not disguised.
                    route.abort()
                else:
                    # Bound but unresolved image/asset (W0-A gate has no real
                    # bytes): a deterministic 1x1 transparent placeholder keeps
                    # the page non-blank without inventing asset content.
                    route.fulfill(status=200, content_type="image/png",
                                  body=_DETERMINISTIC_1X1_PNG)
                return
            route.abort()
            return
        resource_type = str(getattr(route.request, "resource_type", "resource") or "resource")
        kind = {
            "document": "navigation",
            "image": "image",
            "font": "font",
            "media": "media",
            "script": "script",
            "stylesheet": "style",
            "xhr": "connect",
            "fetch": "connect",
        }.get(resource_type, "resource")
        decision = url_policy.evaluate(UrlRequest(url, kind=kind)).decision
        if decision is ResourceDecision.ALLOW:
            route.continue_()
            return
        # Everything else (http/file/script-data/about/javascript/...) denied.
        route.abort()

    def _shell(self, html: str, csp: str) -> str:
        # Kept for backwards-compat callers; the capture path injects the CSP
        # meta into the authored document instead (see _inject_csp_meta) so
        # authored <head>/<style> survive — extracting the body alone would
        # drop the stylesheet and the page would render blank.
        return _inject_csp_meta(html, csp)

    def _failure(self, code: CaptureFailureCode, message: str,
                 evidence: CaptureEvidence | None) -> CaptureFailure:
        return CaptureFailure(code=code, message=message, backend_id=self.backend_id, evidence=evidence)


# 1x1 transparent PNG (deterministic) used as a bound-but-unresolved asset
# placeholder so a capture is never blank *because of* a missing asset.
# Built with zlib so the bytes are guaranteed-valid rather than hand-hexed.
import struct as _struct
import zlib as _zlib


def _make_1x1_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (_struct.pack(">I", len(data)) + tag + data
                + _struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = _struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # 1x1, 8-bit, RGBA
    raw = b"\x00" + b"\x00\x00\x00\x00"  # filter byte + one transparent RGBA pixel
    idat = _zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


_DETERMINISTIC_1X1_PNG = _make_1x1_png()


def _png_to_rgba8(png: bytes, width: int, height: int) -> bytes | None:
    """Decode a PNG to tightly-packed RGBA8 (R,G,B,A per pixel)."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - PIL is a project dependency
        return None
    try:
        img = Image.open(io.BytesIO(png))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        if img.size != (width, height):
            # Defensive: screenshot should already match; never silently resample.
            return None
        return img.tobytes()
    except Exception:
        return None


def _chrome_version_from_ua(ua: str) -> str:
    """Extract the Chrome/<version> token from a user-agent string."""
    if not isinstance(ua, str):
        return ""
    marker = "Chrome/"
    idx = ua.find(marker)
    if idx == -1:
        return ""
    rest = ua[idx + len(marker):]
    out = ""
    for ch in rest:
        if ch.isdigit() or ch == ".":
            out += ch
        else:
            break
    return f"chrome-{out}" if out else ""


def _inject_csp_meta(html: str, csp: str) -> str:
    """Inject a Content-Security-Policy meta into the document, preserving
    the authored ``<head>``/``<style>``/``<body>``.

    The meta goes in as the *first* child of an existing ``<head>`` (CSP meta
    directives combine, so ours restricts whatever the authored page declares).
    If there is no ``<head>`` we create one right after ``<html>``.  This is the
    safe inverse of extracting the body: dropping the authored head would strip
    the stylesheet and the page would render blank.
    """
    import re

    meta = f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
    # AUD-30205: ``<head[^>]*>`` would also match an opening ``<header ...>``
    # tag; a meta injected into <body> is silently ignored by the browser, so
    # require whitespace-or-``>`` right after ``<head`` and let documents
    # without a real <head> take the explicit <html>-wrap fallback below.
    head = re.search(r"<head(?:\s[^>]*)?>", html, re.IGNORECASE)
    if head:
        return html[:head.end()] + meta + html[head.end():]
    html_tag = re.search(r"<html[^>]*>", html, re.IGNORECASE)
    if html_tag:
        return html[:html_tag.end()] + f"<head>{meta}</head>" + html[html_tag.end():]
    return f"<!DOCTYPE html><html><head>{meta}</head><body>{html}</body></html>"


__all__ = ["PlaywrightChromiumBackend"]
