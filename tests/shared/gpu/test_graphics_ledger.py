"""The graphics ledger counts the contexts Topos makes through the factories (plan 7.5).

The hooks wrap the moderngl factories and the Metal context constructor.
Contexts Qt makes for its own ``QOpenGLWidget`` s are not among them, so the
count is a lower bound on the GL contexts of a session.  Here the moderngl
factories are stubs, so no GL context is ever made: the tests are about
what is recorded and refused, and that the wrappers call straight through.
"""

from __future__ import annotations

import json
import os
import threading

import moderngl
import pytest

from src.shared.gpu import graphics_ledger as L


class _Stub:
    """A context the stub factories hand out."""


def _stub_factory(name):
    def create(*args, **kwargs):
        create.calls.append((args, kwargs))
        return _Stub()
    create.calls = []
    create.__name__ = name
    return create


class _DummyMetal:
    """Stands in for MetalComputeContext: records the device it was given."""

    def __init__(self, device=None) -> None:
        self.device = device


@pytest.fixture()
def hooked(monkeypatch: pytest.MonkeyPatch):
    """Hooks installed over stub factories; everything restored afterwards."""
    monkeypatch.delenv(L.FACTORY_ENV, raising=False)
    create, standalone = _stub_factory("create_context"), _stub_factory("create_standalone_context")
    monkeypatch.setattr(moderngl, "create_context", create)
    monkeypatch.setattr(moderngl, "create_standalone_context", standalone)
    L.graphics_ledger().reset()
    L.install_factory_hooks("test-session", metal_context=_DummyMetal)
    yield create, standalone
    L.uninstall_factory_hooks()
    L.graphics_ledger().reset()
    assert moderngl.create_context is create
    assert moderngl.create_standalone_context is standalone
    assert _DummyMetal.__init__ is _DummyMetal_init


_DummyMetal_init = _DummyMetal.__init__


class TestRecording:
    def test_the_factories_still_return_what_they_made(self, hooked) -> None:
        create, standalone = hooked
        assert isinstance(moderngl.create_context(require=410), _Stub)
        assert isinstance(moderngl.create_standalone_context(), _Stub)
        assert create.calls == [(((), {"require": 410}))]
        assert standalone.calls == [(((), {}))]

    def test_each_call_is_one_entry_with_its_factory(self, hooked) -> None:
        create, standalone = hooked
        moderngl.create_context()
        moderngl.create_standalone_context()
        moderngl.create_standalone_context()
        calls = L.graphics_ledger().calls()
        assert [call.factory for call in calls] == [
            "moderngl.create_context", "moderngl.create_standalone_context",
            "moderngl.create_standalone_context"]
        assert {call.api for call in calls} == {L.OPENGL}
        assert {call.session for call in calls} == {"test-session"}
        # The reason names the caller, not the wrapper.
        assert all(call.reason.startswith("test_graphics_ledger.") for call in calls), calls
        assert {call.thread for call in calls} == {threading.current_thread().name}

    def test_the_metal_context_constructor_is_counted_too(self, hooked) -> None:
        device = object()
        context = _DummyMetal(device)
        assert context.device is device                 # called straight through
        (call,) = L.graphics_ledger().calls()
        assert (call.api, call.factory) == (L.METAL, "MetalComputeContext")
        assert call.reason.endswith("test_the_metal_context_constructor_is_counted_too")

    def test_the_thread_that_made_the_context_is_named(self, hooked) -> None:
        def render() -> None:
            moderngl.create_standalone_context()

        worker = threading.Thread(target=render, name="ledger-render")
        worker.start()
        worker.join(5)
        snapshot = L.graphics_ledger().snapshot()
        assert snapshot["opengl_threads"] == ["ledger-render"]

    def test_installing_again_only_renames_the_session(self, hooked) -> None:
        create, _standalone = hooked
        moderngl.create_context()
        L.install_factory_hooks("second-session")
        assert moderngl.create_context is not create
        moderngl.create_context()
        assert [call.session for call in L.graphics_ledger().calls()] == [
            "test-session", "second-session"]
        # Still one wrapper deep: the stub was called once per call.
        assert len(create.calls) == 2

    def test_snapshot_counts_by_api_and_domain(self, hooked) -> None:
        moderngl.create_context()
        moderngl.create_context()
        _DummyMetal()
        snapshot = L.graphics_ledger().snapshot()
        assert snapshot["counts"] == {"opengl": 2, "metal": 1}
        assert [call.kind for call in L.graphics_ledger().calls()] == [L.NEW, L.NEW, L.NEW]
        assert snapshot["by_domain"] == {"tests": {"opengl": 2, "metal": 1}}
        assert snapshot["session"] == "test-session"
        assert len(snapshot["calls"]) == 3


class TestAdoption:
    """``create_context()`` with nothing but the default arguments binds the
    Qt context that is current on the calling thread.  Qt made that context;
    it is still a GL context in use, so it is counted like any other."""

    @staticmethod
    def _current(monkeypatch: pytest.MonkeyPatch, context) -> None:
        from PyQt6.QtGui import QOpenGLContext

        monkeypatch.setattr(QOpenGLContext, "currentContext", staticmethod(lambda: context))

    @pytest.mark.parametrize("call", [
        lambda: moderngl.create_context(),
        lambda: moderngl.create_context(require=330),      # a hint, not a request to make one
    ])
    def test_with_a_qt_context_current_it_is_recorded_as_adopted(
        self, hooked, monkeypatch, call,
    ) -> None:
        self._current(monkeypatch, object())

        assert isinstance(call(), _Stub)                   # called straight through all the same

        (recorded,) = L.graphics_ledger().calls()
        assert (recorded.kind, recorded.factory) == (L.ADOPT, "moderngl.create_context")
        assert recorded.thread == threading.current_thread().name

    def test_an_adopted_context_is_counted(self, hooked, monkeypatch) -> None:
        self._current(monkeypatch, object())

        moderngl.create_context()
        moderngl.create_standalone_context()

        snapshot = L.graphics_ledger().snapshot()
        assert snapshot["counts"] == {"opengl": 2, "metal": 0}
        assert snapshot["by_domain"] == {"tests": {"opengl": 2, "metal": 0}}
        assert snapshot["opengl_threads"] == [threading.current_thread().name]
        assert [call["kind"] for call in snapshot["calls"]] == [L.ADOPT, L.NEW]

    @pytest.mark.parametrize("call", [
        lambda: moderngl.create_context(share=True),
        lambda: moderngl.create_context(standalone=True),
        lambda: moderngl.create_standalone_context(),
    ])
    def test_a_context_asked_for_explicitly_is_made_by_moderngl_even_when_one_is_current(
        self, hooked, monkeypatch, call,
    ) -> None:
        self._current(monkeypatch, object())

        call()

        assert [recorded.kind for recorded in L.graphics_ledger().calls()] == [L.NEW]

    def test_without_a_current_qt_context_the_default_call_makes_one(self, hooked, monkeypatch) -> None:
        self._current(monkeypatch, None)

        moderngl.create_context()

        assert [recorded.kind for recorded in L.graphics_ledger().calls()] == [L.NEW]

    def test_a_refused_adoption_is_refused_like_any_other_call(self, hooked, monkeypatch) -> None:
        """The injection stands in for a machine without GL, where no context can be current."""
        self._current(monkeypatch, object())
        monkeypatch.setenv(L.FACTORY_ENV, "deny")

        with pytest.raises(L.GraphicsFactoryRefused):
            moderngl.create_context()
        assert L.graphics_ledger().calls() == []


class TestStandalone:
    """moderngl's ``create_standalone_context()`` *is* ``create_context(standalone=True)``:
    both wrappers see one call, and it must be one context in the ledger."""

    @staticmethod
    def _like_moderngl(monkeypatch: pytest.MonkeyPatch, factory=None) -> None:
        def create_standalone_context(**kwargs):
            return moderngl.create_context(standalone=True, **kwargs)
        if factory is not None:
            original = create_standalone_context

            def create_standalone_context(**kwargs):
                return factory(original, **kwargs)
        monkeypatch.setattr(moderngl, "create_standalone_context", L._wrapped(
            L.OPENGL, "moderngl.create_standalone_context", create_standalone_context))

    def test_one_standalone_context_is_one_record(self, hooked, monkeypatch) -> None:
        self._like_moderngl(monkeypatch)

        assert isinstance(moderngl.create_standalone_context(require=330), _Stub)

        (recorded,) = L.graphics_ledger().calls()
        assert (recorded.factory, recorded.kind) == ("moderngl.create_standalone_context", L.NEW)
        assert L.graphics_ledger().snapshot()["counts"] == {"opengl": 1, "metal": 0}

    def test_the_next_default_call_is_recorded_again_after_a_standalone_one(
        self, hooked, monkeypatch,
    ) -> None:
        self._like_moderngl(monkeypatch)
        moderngl.create_standalone_context()
        moderngl.create_context()

        assert [recorded.factory for recorded in L.graphics_ledger().calls()] == [
            "moderngl.create_standalone_context", "moderngl.create_context"]

    def test_a_standalone_context_that_fails_leaves_the_next_call_recorded(
        self, hooked, monkeypatch,
    ) -> None:
        def failing(original, **kwargs):
            raise RuntimeError("no GL here")
        self._like_moderngl(monkeypatch, failing)

        with pytest.raises(RuntimeError):
            moderngl.create_standalone_context()
        moderngl.create_context()

        assert [recorded.factory for recorded in L.graphics_ledger().calls()] == [
            "moderngl.create_standalone_context", "moderngl.create_context"]


class TestDomain:
    @pytest.mark.parametrize(("module", "domain"), [
        ("src.features.edit.core.edit_gpu_preview_renderer", "edit"),
        ("src.features.edit.ui.program_viewer", "edit"),
        ("src.features.color.core.async_renderer", "color"),
        ("src.features.composite.render.composite_renderer", "composite"),
        ("src.shared.playback.gl_render_worker", "playback"),
        ("src.shared.video.video_async_renderer", "video"),
        ("src.shared.gpu.metal.context", "shared_gpu"),
        ("src.app.main_window", "app"),
        ("src.main", "startup"),
        ("tools.bench_metal_ab_playback", "tools"),
        ("tests.edit.test_x", "tests"),
    ])
    def test_topos_modules_map_to_their_domain(self, module: str, domain: str) -> None:
        assert L.domain_of_module(module) == domain

    def test_anything_else_is_other(self) -> None:
        assert L.domain_of_module(" PyQt6.QtGui") == "other"
        assert L.domain_of_module("numpy") == "other"
        assert L.domain_of_module("") == "other"


class TestRefusal:
    """``TOPOS_GRAPHICS_FACTORY=deny``: the test injection that refuses GL."""

    @pytest.mark.parametrize("policy", ["deny", "deny:opengl", " DENY "])
    def test_gl_factories_are_refused_before_creating_anything(self, hooked, monkeypatch, policy) -> None:
        create, standalone = hooked
        monkeypatch.setenv(L.FACTORY_ENV, policy)
        with pytest.raises(L.GraphicsFactoryRefused) as refused:
            moderngl.create_context()
        with pytest.raises(L.GraphicsFactoryRefused):
            moderngl.create_standalone_context()
        assert create.calls == [] and standalone.calls == []
        error = refused.value
        assert (error.api, error.domain) == (L.OPENGL, "tests")
        assert error.factory == "moderngl.create_context"
        assert str(error) == (
            "graphics factory refused: opengl context via moderngl.create_context for tests "
            "(test_graphics_ledger.test_gl_factories_are_refused_before_creating_anything)")

    def test_a_refused_factory_leaves_no_entry(self, hooked, monkeypatch) -> None:
        monkeypatch.setenv(L.FACTORY_ENV, "deny")
        with pytest.raises(L.GraphicsFactoryRefused):
            moderngl.create_context()
        assert L.graphics_ledger().calls() == []

    def test_metal_is_never_refused_by_the_gl_switch(self, hooked, monkeypatch) -> None:
        monkeypatch.setenv(L.FACTORY_ENV, "deny")
        _DummyMetal()
        assert [call.api for call in L.graphics_ledger().calls()] == [L.METAL]

    @pytest.mark.parametrize("policy", ["", "allow", "ALLOW"])
    def test_unset_or_allow_calls_straight_through(self, hooked, monkeypatch, policy) -> None:
        monkeypatch.setenv(L.FACTORY_ENV, policy)
        assert isinstance(moderngl.create_context(), _Stub)

    def test_an_unknown_policy_is_an_error_not_a_guess(self, hooked, monkeypatch) -> None:
        monkeypatch.setenv(L.FACTORY_ENV, "deny:metal")
        with pytest.raises(ValueError, match="TOPOS_GRAPHICS_FACTORY"):
            moderngl.create_context()


class TestSessionLedger:
    def test_nothing_is_installed_unless_asked(self, monkeypatch) -> None:
        monkeypatch.delenv(L.LEDGER_ENV, raising=False)
        assert L.start_session_ledger("app") is False
        assert L.hooks_installed() is False

    def test_asking_for_it_installs_the_hooks_and_writes_the_snapshot_at_exit(
        self, monkeypatch, tmp_path,
    ) -> None:
        create = _stub_factory("create_context")
        monkeypatch.setattr(moderngl, "create_context", create)
        monkeypatch.setenv(L.LEDGER_ENV, "1")
        monkeypatch.setenv(L.LEDGER_FILE_ENV, str(tmp_path / "ledger.json"))
        try:
            assert L.start_session_ledger("/opt/Topos/topos-studio") is True
            assert L.hooks_installed() is True
            moderngl.create_context()
            L.write_snapshot(str(tmp_path / "now.json"))
        finally:
            L.uninstall_factory_hooks()
            L.graphics_ledger().reset()
        snapshot = json.loads((tmp_path / "now.json").read_text(encoding="utf-8"))
        assert snapshot["session"] == "topos-studio"
        assert snapshot["counts"]["opengl"] == 1
        assert snapshot["calls"][0]["factory"] == "moderngl.create_context"

    def test_the_snapshot_is_written_before_a_hard_exit(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The application always leaves through os._exit; the hook writes
        first and the real exit still happens."""
        create = _stub_factory("create_context")
        monkeypatch.setattr(moderngl, "create_context", create)
        monkeypatch.setenv(L.LEDGER_ENV, "1")
        monkeypatch.setenv(L.LEDGER_FILE_ENV, str(tmp_path / "ledger.json"))
        real_exit = os._exit
        try:
            assert L.start_session_ledger("app") is True
            assert os._exit is not real_exit
            moderngl.create_context()
            # Not os._exit() itself: that would end the test run.
            assert L._simulate_hard_exit(0) == 0
        finally:
            monkeypatch.setattr(os, "_exit", real_exit)
            L.uninstall_factory_hooks()
            L.graphics_ledger().reset()
        snapshot = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
        assert snapshot["counts"]["opengl"] == 1

    def test_without_a_ledger_nothing_is_hooked(self, monkeypatch) -> None:
        monkeypatch.delenv(L.LEDGER_ENV, raising=False)
        real_exit = os._exit
        assert L.start_session_ledger("app") is False
        assert os._exit is real_exit

    def test_without_a_file_the_snapshot_goes_to_stderr(self, hooked, capsys) -> None:
        moderngl.create_context()
        L.write_snapshot()
        err = capsys.readouterr().err
        assert err.startswith("graphics ledger: {")
        assert json.loads(err[len("graphics ledger: "):])["counts"]["opengl"] == 1

    def test_an_empty_ledger_reports_zero_contexts(self, hooked) -> None:
        assert L.graphics_ledger().snapshot() == {
            "session": "test-session", "counts": {"opengl": 0, "metal": 0},
            "by_domain": {}, "opengl_threads": [], "calls": []}
