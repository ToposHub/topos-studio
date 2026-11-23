"""What graphics contexts Topos creates, counted (plan 7.5).

The acceptance of plan 7.5 is a count: over a cold start, a typical
project, an export and quitting, Topos creates no GL context and no GL
thread once a path has moved to Metal, and the compatible mode still
works where it has to.  A count needs somewhere to be kept.

:func:`install_factory_hooks` wraps the two ``moderngl`` context factories
and the Metal compute context constructor, so every context Topos creates
through them is recorded with its API, the *domain* of the caller, the
*reason* (the calling function), the thread and the session.  Nothing else
changes: the wrappers call straight through.

Not counted are the contexts Qt makes for its own widgets (every
``QOpenGLWidget``): they do not pass through the factories.  So
``counts["opengl"]`` is the number of moderngl contexts and a lower bound
on the GL contexts of a session; a viewer that is a ``QOpenGLWidget`` keeps
its context even at ``counts["opengl"] == 0``.

Two things the records say about each GL context.  ``kind`` is ``"adopt"``
when ``moderngl.create_context()`` bound the Qt context that was current on
the calling thread -- what every render thread here does (it makes a
``QOpenGLContext`` current on an offscreen surface first) and what
``GLContextManager.initialize`` does on the GUI thread -- and ``"new"``
when moderngl made the context itself (``create_standalone_context``, or
``create_context()`` with nothing current).  An adopted context was made by
Qt, but it is a GL context all the same and is counted like any other: this
is the only way a Qt-made context shows up in the ledger at all.

The same hooks are the test injection of the plan ("用测试注入拒绝 GL
工厂"): with ``TOPOS_GRAPHICS_FACTORY=deny`` a GL factory raises
:class:`GraphicsFactoryRefused` before creating anything, so a caller's
fallback can be exercised on a machine that has a perfectly working GL.

Off by default.  ``TOPOS_GRAPHICS_LEDGER=1`` starts a session ledger at
application start (:func:`start_session_ledger`) and, when the process
exits, writes the snapshot to ``TOPOS_GRAPHICS_LEDGER_FILE`` or to stderr.

Reading a snapshot: ``counts["opengl"]`` is the number of moderngl GL
contexts created; ``opengl_threads`` are the threads that created them (a
render thread shows up here the moment it makes its context);
``by_domain`` splits both APIs by the domain of the caller.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

LEDGER_ENV = "TOPOS_GRAPHICS_LEDGER"
LEDGER_FILE_ENV = "TOPOS_GRAPHICS_LEDGER_FILE"
FACTORY_ENV = "TOPOS_GRAPHICS_FACTORY"

OPENGL = "opengl"
METAL = "metal"

#: caller module path prefix -> domain, longest prefix first.
_DOMAINS = (
    ("src.features.edit.", "edit"),
    ("src.features.color.", "color"),
    ("src.features.composite.", "composite"),
    ("src.features.deliver.", "deliver"),
    ("src.features.teaching_animation.", "teaching_animation"),
    ("src.shared.playback.", "playback"),
    ("src.shared.video.", "video"),
    ("src.shared.gpu.", "shared_gpu"),
    ("src.shared.media.", "media"),
    ("src.app.", "app"),
    ("src.main", "startup"),
    ("tools.", "tools"),
    ("tests.", "tests"),
)


class GraphicsFactoryRefused(RuntimeError):
    """A GL context factory was refused by ``TOPOS_GRAPHICS_FACTORY``."""

    def __init__(self, api: str, domain: str, reason: str, factory: str = "") -> None:
        via = f" via {factory}" if factory else ""
        super().__init__(f"graphics factory refused: {api} context{via} for {domain} ({reason})")
        self.api = api
        self.domain = domain
        self.reason = reason
        self.factory = factory


#: how a GL context came to the caller
NEW = "new"            # moderngl made the context
ADOPT = "adopt"        # create_context() bound the Qt context that was already current


@dataclass(frozen=True)
class GraphicsCall:
    """One GL or Metal context in use, and how it came to the caller."""

    api: str            # "opengl" / "metal"
    domain: str         # the caller's domain
    reason: str         # the caller: "<module name>.<function>"
    thread: str
    session: str
    factory: str        # which factory was called
    kind: str = NEW


class GraphicsLedger:
    """Thread-safe record of every graphics context created."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: List[GraphicsCall] = []
        self.session = ""

    def record(
        self, api: str, domain: str, reason: str, factory: str, kind: str = NEW,
    ) -> GraphicsCall:
        call = GraphicsCall(
            api=api, domain=domain, reason=reason, factory=factory, kind=kind,
            thread=threading.current_thread().name, session=self.session)
        with self._lock:
            self._calls.append(call)
        return call

    def calls(self) -> List[GraphicsCall]:
        with self._lock:
            return list(self._calls)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()

    def snapshot(self) -> Dict[str, Any]:
        """Counts by API and by domain, and the threads that created GL contexts."""
        with self._lock:
            calls = list(self._calls)
        by_domain: Dict[str, Dict[str, int]] = {}
        for call in calls:
            by_domain.setdefault(call.domain, {"opengl": 0, "metal": 0})[call.api] += 1
        return {
            "session": self.session,
            "counts": {api: sum(call.api == api for call in calls) for api in (OPENGL, METAL)},
            "by_domain": {domain: by_domain[domain] for domain in sorted(by_domain)},
            "opengl_threads": sorted({call.thread for call in calls if call.api == OPENGL}),
            "calls": [asdict(call) for call in calls],
        }


_LEDGER = GraphicsLedger()


def graphics_ledger() -> GraphicsLedger:
    """The process-wide ledger."""
    return _LEDGER


def domain_of_module(module: str) -> str:
    """The domain a module belongs to; ``"other"`` when it is not Topos code."""
    for prefix, domain in _DOMAINS:
        if module.startswith(prefix) or module == prefix.rstrip("."):
            return domain
    return "other"


def factory_policy() -> str:
    """``"allow"`` or ``"deny"`` for GL factories, from ``TOPOS_GRAPHICS_FACTORY``.

    ``deny`` and ``deny:opengl`` refuse OpenGL context factories; ``allow``
    and an unset variable call straight through.  A value naming anything
    else is refused as unknown rather than guessed.
    """
    raw = os.environ.get(FACTORY_ENV, "").strip().lower()
    if not raw or raw == "allow":
        return "allow"
    if raw in ("deny", "deny:opengl"):
        return "deny"
    raise ValueError(f"{FACTORY_ENV}: unknown policy {raw!r} (allow, deny)")


def _caller() -> tuple[str, str]:
    """``(module, function)`` of the first frame outside the factory plumbing."""
    frame = sys._getframe(2)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not (module.startswith("moderngl") or module.startswith("glcontext")
                or module == __name__):
            # The module's own name; its domain is a field of its own, so
            # "core.edit_gpu_preview_renderer" would only add ambiguity.
            return module, f"{module.rsplit('.', 1)[-1]}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "<unknown>", "<unknown>"


def _adopts_current_context(args: tuple, kwargs: Dict[str, Any]) -> bool:
    """Whether this ``create_context()`` call binds the Qt context that is
    current instead of making one: no argument selects ``standalone`` or
    ``share``, and a Qt context is current on the calling thread."""
    if args or kwargs.get("standalone") or kwargs.get("share") or kwargs.get("context"):
        return False
    try:
        from PyQt6.QtGui import QOpenGLContext
    except ImportError:       # a host without Qt has no context to adopt
        return False
    return QOpenGLContext.currentContext() is not None


_inside_standalone = threading.local()


def _before_factory(
    api: str, factory: str, args: tuple = (), kwargs: Optional[Dict[str, Any]] = None,
) -> None:
    if factory == "moderngl.create_context" and getattr(_inside_standalone, "active", False):
        # moderngl's create_standalone_context() is create_context(standalone=True):
        # the same context would be counted twice.
        return
    module, reason = _caller()
    domain = domain_of_module(module)
    if api == OPENGL and factory_policy() == "deny":
        # Refused before anything is created: the caller sees the same
        # situation as a machine without a usable GL.
        raise GraphicsFactoryRefused(api, domain, reason, factory)
    kind = ADOPT if (api == OPENGL and factory == "moderngl.create_context"
                     and _adopts_current_context(args, kwargs or {})) else NEW
    _LEDGER.record(api, domain, reason, factory, kind)


def _wrapped(api: str, factory: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def create(*args: Any, **kwargs: Any) -> Any:
        if factory == "moderngl.create_standalone_context":
            _before_factory(api, factory, args, kwargs)
            _inside_standalone.active = True
            try:
                return original(*args, **kwargs)
            finally:
                _inside_standalone.active = False
        _before_factory(api, factory, args, kwargs)
        return original(*args, **kwargs)
    create.__name__ = getattr(original, "__name__", "create")
    create.__doc__ = getattr(original, "__doc__", None)
    create.__wrapped__ = original  # type: ignore[attr-defined]
    return create


_installed: Optional[Dict[str, Any]] = None


def install_factory_hooks(session: str = "", *, metal_context: Any = None) -> None:
    """Record (and optionally refuse) every context the factories create.

    ``metal_context`` is the Metal compute context type whose constructor is
    counted; it defaults to the real one and may be any class in tests.
    Installing twice only sets the session name.
    """
    global _installed
    import moderngl

    _LEDGER.session = session
    if _installed is not None:
        return
    if metal_context is None:
        try:
            from src.shared.gpu.metal.context import MetalComputeContext as metal_context
        except ImportError:       # pragma: no cover - not a macOS machine
            metal_context = None
    originals: Dict[str, Any] = {"moderngl": moderngl,
                                 "create_context": moderngl.create_context,
                                 "standalone": moderngl.create_standalone_context,
                                 "metal": None}
    moderngl.create_context = _wrapped(OPENGL, "moderngl.create_context", originals["create_context"])
    moderngl.create_standalone_context = _wrapped(
        OPENGL, "moderngl.create_standalone_context", originals["standalone"])
    if metal_context is not None:
        originals["metal"] = metal_context
        originals["metal_init"] = metal_context.__init__

        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            _before_factory(METAL, "MetalComputeContext")
            originals["metal_init"](self, *args, **kwargs)

        metal_context.__init__ = __init__
    _installed = originals


def uninstall_factory_hooks() -> None:
    """Put the original factories back (tests)."""
    global _installed
    if _installed is None:
        return
    saved = _installed
    _installed = None
    saved["moderngl"].create_context = saved["create_context"]
    saved["moderngl"].create_standalone_context = saved["standalone"]
    if saved["metal"] is not None:
        saved["metal"].__init__ = saved["metal_init"]


def hooks_installed() -> bool:
    return _installed is not None


def write_snapshot(target: Optional[str] = None) -> str:
    """The ledger as JSON, written to ``target`` (a file path) or to stderr."""
    text = json.dumps(_LEDGER.snapshot(), indent=1, ensure_ascii=False)
    if target:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print("graphics ledger: " + text, file=sys.stderr)
    return text


def start_session_ledger(argv0: str = "") -> bool:
    """Install the hooks when ``TOPOS_GRAPHICS_LEDGER`` asks for it.

    The snapshot is written when the process exits: on a normal exit and
    before ``os._exit``, which is how the application always leaves (its
    orderly shutdown stops the thread pools and exits hard), so an
    ``atexit`` handler alone would never run.  Nothing is installed and
    ``False`` is returned when the variable is unset, so an ordinary
    session is untouched.
    """
    if os.environ.get(LEDGER_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    target = os.environ.get(LEDGER_FILE_ENV) or None
    install_factory_hooks(session=os.path.basename(argv0) or "session")
    atexit.register(write_snapshot, target)

    def _flush(*_ignored: Any) -> None:
        write_snapshot(target)

    _hook_hard_exit(_flush)
    return True


_HARD_EXIT_HOOK: Optional[Callable[[], None]] = None


def _hook_hard_exit(flush: Callable[[], None]) -> None:
    """Write the snapshot before ``os._exit``; the real exit still happens."""
    global _HARD_EXIT_HOOK
    real_exit = os._exit
    _HARD_EXIT_HOOK = flush

    def _exit(status: int = 0) -> None:
        try:
            flush()
        finally:
            real_exit(status)

    os._exit = _exit  # type: ignore[assignment]


def _simulate_hard_exit(status: int = 0) -> int:
    """Run what the ``os._exit`` hook would do before exiting (tests)."""
    if _HARD_EXIT_HOOK is None:
        raise RuntimeError("no session ledger was started")
    _HARD_EXIT_HOOK()
    return status
