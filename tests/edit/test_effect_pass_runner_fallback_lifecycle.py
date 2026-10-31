"""FX-M07 regression: last-known-good programs must remain live."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.features.shared.effects.effect_pass_runner import EffectPassRunner
from src.features.shared.effects.effect_spec import ShaderKind


def _runner() -> EffectPassRunner:
    runner = EffectPassRunner.__new__(EffectPassRunner)
    runner._max_cache_size = 1
    runner._program_cache = {}
    runner._program_order = []
    runner._fallback_programs = {}
    runner._compile_failures = set()
    runner._vao_cache = {}
    runner._lock = threading.RLock()
    return runner


def _spec(source: str, effect_type: str = "plugin:test") -> SimpleNamespace:
    return SimpleNamespace(
        effect_type=effect_type,
        shader_sources=(source,),
    )


def test_compile_failure_returns_live_last_known_good_program() -> None:
    runner = _runner()
    old_program = MagicMock(name="old_program")
    latest_program = MagicMock(name="latest_program")

    with patch.object(
        runner,
        "_compile_spec",
        side_effect=[old_program, latest_program, None],
    ):
        assert runner._get_program(_spec("v1"), ()) is old_program
        assert runner._get_program(_spec("v2"), ()) is latest_program
        fallback = runner._get_program(_spec("broken-v3"), ())

    old_program.release.assert_called_once()
    latest_program.release.assert_not_called()
    assert fallback is latest_program
    assert runner.get_compile_failures() == frozenset({"plugin:test"})


def test_fallback_entries_do_not_bypass_cache_size_limit() -> None:
    runner = _runner()
    first_program = MagicMock(name="first_program")
    second_program = MagicMock(name="second_program")

    with patch.object(
        runner,
        "_compile_spec",
        side_effect=[first_program, second_program],
    ):
        runner._get_program(_spec("v1", "plugin:first"), ())
        runner._get_program(_spec("v1", "plugin:second"), ())

    assert len(runner._program_cache) == runner._max_cache_size == 1
    assert "plugin:first" not in runner._fallback_programs
    first_program.release.assert_called_once()
    second_program.release.assert_not_called()


def test_separable_second_compile_failure_releases_first_program() -> None:
    runner = _runner()
    first_program = MagicMock(name="horizontal_program")
    runner._ctx = MagicMock()
    runner._ctx.program.side_effect = [first_program, RuntimeError("vertical failed")]
    spec = SimpleNamespace(
        effect_type="builtin:separable",
        shader_kind=ShaderKind.SEPARABLE_PASSES,
        shader_sources=("horizontal", "vertical"),
    )

    assert runner._compile_spec(spec) is None
    first_program.release.assert_called_once()
