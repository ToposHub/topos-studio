"""
Script Execution Engine for Topos Color.

Provides BlockingBridge for thread-safe main-thread calls,
and ScriptEngine for running user Python scripts in a background QThread.
"""

import sys
import io
import threading
import time
import traceback
from types import TracebackType
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QMutex, QWaitCondition, Qt, QThread


class ScriptAbortError(Exception):
    """Raised when the user requests script execution to stop."""
    pass


def _error_line_number(
    exc_tb: Optional[TracebackType], script_name: str = ""
) -> int:
    """Extract the most relevant source line from *exc_tb*.

    Prefers the innermost frame whose file matches *script_name* (the
    user script); falls back to the innermost frame overall when no
    frame matches (or *script_name* is empty).  Returns 0 when there is
    no traceback.
    """
    if exc_tb is None:
        return 0
    tb = exc_tb
    if script_name:
        last_script_tb: Optional[TracebackType] = None
        while tb is not None:
            if tb.tb_frame.f_code.co_filename == script_name:
                last_script_tb = tb
            tb = tb.tb_next
        if last_script_tb is not None:
            return last_script_tb.tb_lineno
    tb = exc_tb
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_lineno


class BlockingBridge(QObject):
    """Thread-safe bridge to execute callables on the main thread.

    Uses a queued cross-thread signal + QWaitCondition so the calling
    (script) thread blocks until the main thread finishes executing the
    callable — while the documented per-call timeout and the abort check
    remain reachable (AUD-25910: with ``BlockingQueuedConnection`` the
    ``emit`` itself blocked until the slot completed, so the wait loop
    below always broke on its first iteration and a stuck main thread
    hung the script thread forever inside ``emit``).
    """
    _execute_in_main = pyqtSignal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._result = None
        self._error = None
        self._done = False
        self._abort = False
        self._seq = 0            # monotonic call sequence
        self._result_seq = -1    # seq of last completed _do_execute
        # Thread that owns this bridge (the main/UI thread).  Captured via
        # threading.get_ident() because PyQt6 wrapper identity for QThread
        # objects is not guaranteed to be stable across calls.
        self._owner_thread_id = threading.get_ident()
        self._execute_in_main.connect(
            self._do_execute, Qt.ConnectionType.QueuedConnection
        )

    def call_sync(self, func, timeout=60000):
        """Call *func* on the main thread and block until it returns.

        Returns the function's return value.  Raises on error, abort or
        timeout.  The timeout (ms) can be overridden per-call, e.g.
        ``bridge.call_sync(some_func, timeout=120000)``.
        """
        if self._abort:
            raise ScriptAbortError("Script aborted")

        if threading.get_ident() == self._owner_thread_id:
            # Same-thread call: a queued event can never be delivered while
            # this thread is blocked inside the wait loop, so execute the
            # callable directly instead of hanging until the timeout.
            return func()

        self._mutex.lock()
        self._done = False
        self._error = None
        self._result = None
        self._seq += 1
        expected_seq = self._seq
        self._mutex.unlock()

        # AUD-25911 (defensive, required once the wait path is live): carry
        # the *initiating* seq with the payload so a late completion of a
        # previously timed-out call is never mistaken for this call's result.
        self._execute_in_main.emit(expected_seq, func)

        self._mutex.lock()
        deadline = time.monotonic() + timeout / 1000.0
        while True:
            # Accept result only from *this* call (prevents stale wakeups)
            if self._done and self._result_seq == expected_seq:
                break
            if self._abort:
                self._mutex.unlock()
                raise ScriptAbortError("Script aborted")
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                self._mutex.unlock()
                raise TimeoutError("Script operation timed out")
            if not self._condition.wait(self._mutex, remaining_ms):
                if self._done and self._result_seq == expected_seq:
                    break
                self._mutex.unlock()
                raise TimeoutError("Script operation timed out")
        if self._error:
            err = self._error
            self._mutex.unlock()
            raise err
        result = self._result
        self._mutex.unlock()
        return result

    @pyqtSlot(int, object)
    def _do_execute(self, seq, func):
        try:
            result = func()
            error = None
        except Exception as e:  # noqa: BLE001 — recorded, re-raised in caller
            result = None
            error = e
        self._mutex.lock()
        self._result = result
        self._error = error
        # Write the initiating seq (payload), not the value of ``self._seq``
        # at execution time — see call_sync above.
        self._result_seq = seq
        self._done = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def request_abort(self) -> None:
        """Signal the bridge to abort on the next call."""
        self._mutex.lock()
        self._abort = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def reset_abort(self) -> None:
        """Clear a pending abort request.

        AUD-25912: ``_abort`` is shared between the script thread (wait loop
        in :meth:`call_sync`), the main thread (``request_abort``) and
        whichever thread resets it — all access must hold ``self._mutex``,
        matching the locking discipline of the other paths.
        """
        self._mutex.lock()
        try:
            self._abort = False
        finally:
            self._mutex.unlock()

    def is_aborted(self) -> bool:
        """Return whether an abort has been requested."""
        self._mutex.lock()
        try:
            return self._abort
        finally:
            self._mutex.unlock()


class _EmittingStream(io.StringIO):
    """StringIO that buffers output and forwards it to a callback in batches.

    Emits when a newline is encountered or the buffer exceeds 4096 characters.
    """
    _FLUSH_THRESHOLD = 4096

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self._buffer = []

    def write(self, text):
        if not text:
            return 0
        self._buffer.append(text)
        combined = "".join(self._buffer)
        if "\n" in combined or len(combined) >= self._FLUSH_THRESHOLD:
            self._callback(combined)
            self._buffer.clear()
        return len(text)

    def flush(self):
        if self._buffer:
            self._callback("".join(self._buffer))
            self._buffer.clear()


class _StdoutRedirect:
    """Context manager that safely redirects sys.stdout and sys.stderr."""

    def __init__(self, stdout_stream, stderr_stream):
        self._stdout_stream = stdout_stream
        self._stderr_stream = stderr_stream
        self._old_stdout = None
        self._old_stderr = None

    def __enter__(self):
        self._old_stdout = sys.stdout
        self._old_stderr = sys.stderr
        sys.stdout = self._stdout_stream
        sys.stderr = self._stderr_stream
        return self

    def __exit__(self, *exc_info):
        sys.stdout = self._old_stdout
        sys.stderr = self._old_stderr


class ScriptEngine(QObject):
    """Executes user Python scripts in a background QThread.

    Scripts receive a ``topos`` object (ToposAPI) in their namespace.
    """
    output_ready = pyqtSignal(str)
    error_output_ready = pyqtSignal(str)       # stderr output
    script_finished = pyqtSignal(bool, str)   # success, message
    script_error = pyqtSignal(str, int)        # error_message, line_number
    progress_updated = pyqtSignal(int, int)    # current, total
    status_message = pyqtSignal(str)           # message text
    action_registered = pyqtSignal(str)        # action name

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._bridge = BlockingBridge(self)
        self._thread = None
        self._is_running = False
        self._registered_actions = {}          # name -> callback
        # P1-COLOR-24: 宿主注入的只读时间线上下文 provider（list_clips /
        # clip_grade），由 ToposAPI 在脚本线程经 bridge 调用
        self._timeline_provider = None

    @property
    def bridge(self):
        return self._bridge

    def set_timeline_provider(self, provider) -> None:
        """注入时间线上下文 provider：``provider(what, clip_id=None)``。

        what == "list_clips" → clip dict 列表；
        what == "clip_grade"  → 该 clip 的调色快照 dict 或 None。
        provider 在主线程执行（经 BlockingBridge），返回纯数据。
        """
        self._timeline_provider = provider

    def is_running(self) -> bool:
        return self._is_running

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, script_code: str, script_path: str = None):
        """Execute *script_code* in a background thread."""
        if self._is_running:
            self.output_ready.emit("[Error] A script is already running.\n")
            self.script_finished.emit(False, "A script is already running.")
            return

        # P1-COLOR-24: 脚本头 "# topos-api: X.Y" 版本声明校验。
        # 主版本不匹配 → 拒绝执行并给出迁移提示。
        from src.features.scripting.api import (
            check_api_compatibility, parse_api_requirement,
        )
        required = parse_api_requirement(script_code)
        ok, message = check_api_compatibility(required)
        if not ok:
            self.output_ready.emit(f"[API Version] {message}\n")
            self.script_error.emit(message, 0)
            self.script_finished.emit(False, message)
            return
        if message:
            self.output_ready.emit(f"[API Version Warning] {message}\n")

        self._is_running = True
        self._bridge.reset_abort()

        # Lazy import to avoid circular imports
        from src.features.scripting.api import ToposAPI
        api = ToposAPI(self._engine, self._bridge)
        api._timeline_provider = self._timeline_provider

        self._thread = QThread()
        self._thread.setObjectName("ScriptExecThread")
        self._thread.run = lambda: self._run_script(
            script_code, script_path, api
        )
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def stop(self):
        """Request the running script to stop."""
        if self._is_running:
            self._bridge.request_abort()
            self.output_ready.emit("[Abort requested...]\n")

    def register_action(self, name: str, callback) -> None:
        """Register a named action that can be triggered from the Scripts menu."""
        self._registered_actions[name] = callback
        self.action_registered.emit(name)

    def get_registered_actions(self) -> dict:
        """Return a copy of registered actions."""
        return dict(self._registered_actions)

    def run_registered_action(self, name: str):
        """Execute a registered action in a background thread."""
        if name not in self._registered_actions:
            self.output_ready.emit(f"[Error] Unknown action: {name}\n")
            self.script_finished.emit(False, f"Unknown action: {name}")
            return
        if self._is_running:
            self.output_ready.emit("[Error] A script is already running.\n")
            self.script_finished.emit(False, "A script is already running.")
            return

        callback = self._registered_actions[name]
        self._is_running = True
        self._bridge.reset_abort()

        from src.features.scripting.api import ToposAPI
        api = ToposAPI(self._engine, self._bridge)
        api._timeline_provider = self._timeline_provider

        self._thread = QThread()
        self._thread.setObjectName("ScriptExecThread")
        self._thread.run = lambda: self._run_action(callback, api)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _run_action(self, callback, api):
        stdout_stream = _EmittingStream(lambda t: self.output_ready.emit(t))
        stderr_stream = _EmittingStream(lambda t: self.error_output_ready.emit(t))

        with _StdoutRedirect(stdout_stream, stderr_stream):
            try:
                # Auto-save undo snapshot for registered actions
                api._auto_undo_snapshot()
                callback(api)
                self.script_finished.emit(True, "Action completed successfully.")
            except ScriptAbortError:
                self.script_finished.emit(False, "Action aborted by user.")
            except Exception:
                tb_text = traceback.format_exc()
                self.output_ready.emit(tb_text + "\n")
                exc_type, exc_value, exc_tb = sys.exc_info()
                self.script_error.emit(
                    str(exc_value), _error_line_number(exc_tb)
                )
                self.script_finished.emit(False, str(exc_value))
            except BaseException as e:
                # AUD-25909: SystemExit / KeyboardInterrupt and other
                # BaseExceptions are not covered by ``except Exception``.
                # They must never escape this QThread.run reimplementation
                # (PyQt6 treats an exception escaping a virtual method as
                # qFatal and aborts the whole process), and script_finished
                # must still be emitted so the UI does not stay stuck in
                # "running".  Log and swallow per Qt slot conventions.
                if isinstance(e, SystemExit) and (e.code is None or e.code == 0):
                    self.script_finished.emit(True, "Action exited via sys.exit(0).")
                else:
                    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                    self.error_output_ready.emit(traceback.format_exc() + "\n")
                    self.script_error.emit(
                        msg, _error_line_number(sys.exc_info()[2])
                    )
                    self.script_finished.emit(False, msg)
            finally:
                stdout_stream.flush()
                stderr_stream.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_script(self, code, path, api):
        stdout_stream = _EmittingStream(lambda t: self.output_ready.emit(t))
        stderr_stream = _EmittingStream(lambda t: self.error_output_ready.emit(t))

        namespace = {
            "topos": api,
            "__file__": path or "<script>",
            "__name__": "__main__",
        }

        with _StdoutRedirect(stdout_stream, stderr_stream):
            try:
                # Auto-save undo snapshot if script doesn't call begin_undo manually
                api._auto_undo_snapshot()
                compiled = compile(code, path or "<script>", "exec")
                exec(compiled, namespace)
                self.script_finished.emit(True, "Script completed successfully.")
            except ScriptAbortError:
                self.script_finished.emit(False, "Script aborted by user.")
            except Exception:
                tb_text = traceback.format_exc()
                self.output_ready.emit(tb_text + "\n")

                # Extract line number — prefer user script frame over api internals
                _exc_type, exc_value, exc_tb = sys.exc_info()
                self.script_error.emit(
                    str(exc_value), _error_line_number(exc_tb, path or "<script>")
                )
                self.script_finished.emit(False, str(exc_value))
            except BaseException as e:
                # AUD-25909: SystemExit / KeyboardInterrupt and other
                # BaseExceptions are not covered by ``except Exception``.
                # They must never escape this QThread.run reimplementation
                # (PyQt6 treats an exception escaping a virtual method as
                # qFatal and aborts the whole process), and script_finished
                # must still be emitted so the UI does not stay stuck in
                # "running".  Log and swallow per Qt slot conventions.
                if isinstance(e, SystemExit) and (e.code is None or e.code == 0):
                    self.script_finished.emit(True, "Script exited via sys.exit(0).")
                else:
                    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                    self.error_output_ready.emit(traceback.format_exc() + "\n")
                    self.script_error.emit(
                        msg, _error_line_number(sys.exc_info()[2], path or "<script>")
                    )
                    self.script_finished.emit(False, msg)
            finally:
                stdout_stream.flush()
                stderr_stream.flush()

    def _on_thread_finished(self):
        self._is_running = False
        if self._thread:
            self._thread.deleteLater()
            self._thread = None
