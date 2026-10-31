"""W1 presets + helpers: loop/ping_pong/wiggle/count_up/typewriter + registry."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.motion import (
    MotionPreset, MotionPresetRegistry, count_up, create_default_preset_registry,
    loop, ping_pong, reveal_progress, stagger_index, typewriter, wiggle,
)


class TestHelpers:
    def test_loop_wraps(self) -> None:
        assert loop(0, 5) == 0
        assert loop(5, 5) == 0
        assert loop(7, 5) == 2

    def test_ping_pong_bounces(self) -> None:
        assert ping_pong(0, 5) == 0
        assert ping_pong(5, 5) == 5
        assert ping_pong(7, 5) == 3   # going back: 10 - 7
        assert ping_pong(10, 5) == 0

    def test_loop_invalid_period(self) -> None:
        with pytest.raises(ValueError):
            loop(0, 0)

    def test_wiggle_is_deterministic(self) -> None:
        a = wiggle(13, seed=42, frequency=0.7, amplitude=5.0)
        b = wiggle(13, seed=42, frequency=0.7, amplitude=5.0)
        assert a == b

    def test_wiggle_within_amplitude(self) -> None:
        for f in range(0, 100):
            v = wiggle(f, seed=1, frequency=0.5, amplitude=10.0)
            assert -10.0 <= v <= 10.0

    def test_wiggle_seed_changes_output(self) -> None:
        a = wiggle(13, seed=1)
        b = wiggle(13, seed=2)
        assert a != b

    def test_count_up(self) -> None:
        assert count_up(0, start_frame=0, end_frame=10, from_=0.0, to=100.0) == 0.0
        assert count_up(5, start_frame=0, end_frame=10, from_=0.0, to=100.0) == 50.0
        assert count_up(10, start_frame=0, end_frame=10, from_=0.0, to=100.0) == 100.0

    def test_typewriter_progressive(self) -> None:
        text = "hello"
        # frame 0 -> 0 chars visible; frame 1 -> 1 char; frame 5 capped at full text.
        assert typewriter(0, text, start_frame=0, chars_per_frame=1.0) == ""
        assert typewriter(1, text, start_frame=0, chars_per_frame=1.0) == "h"
        assert typewriter(5, text, start_frame=0, chars_per_frame=1.0) == "hello"
        # 2 frames * 0.5 chars/frame = 1.0 -> first char only
        assert typewriter(2, text, start_frame=0, chars_per_frame=0.5) == "h"

    def test_typewriter_before_start_empty(self) -> None:
        assert typewriter(2, "hi", start_frame=5, chars_per_frame=1.0) == ""

    def test_reveal_progress_clamped(self) -> None:
        assert reveal_progress(0, start=5, duration=10) == 0.0
        assert reveal_progress(10, start=5, duration=10) == 0.5
        assert reveal_progress(20, start=5, duration=10) == 1.0
        assert reveal_progress(2, start=5, duration=10) == 0.0

    def test_stagger_index(self) -> None:
        assert stagger_index(0, interval=6, delay=2) == 2
        assert stagger_index(1, interval=6, delay=2) == 8
        assert stagger_index(3, interval=6, delay=2) == 20


class TestRegistry:
    def test_default_registry_has_core_presets(self) -> None:
        reg = create_default_preset_registry()
        for required in ("fade", "slide", "pop", "scale", "overshoot", "reveal", "draw", "word_highlight"):
            assert reg.get(required) is not None

    def test_build_uses_defaults_and_overrides(self) -> None:
        reg = create_default_preset_registry()
        fn = reg.build("fade", start=0, duration=12)
        assert fn(0, 24.0) == pytest.approx(0.0)
        assert fn(12, 24.0) == pytest.approx(1.0)

    def test_build_unknown_raises(self) -> None:
        reg = create_default_preset_registry()
        with pytest.raises(KeyError):
            reg.build("nope")

    def test_register_rejects_lower_version_without_replace(self) -> None:
        reg = MotionPresetRegistry()
        reg.register(MotionPreset("x", 2, "d", build=lambda **k: (lambda f, fps: 0.0)))
        # lower version without replace -> rejected
        assert reg.register(MotionPreset("x", 1, "d", build=lambda **k: (lambda f, fps: 0.0))) is False
        assert reg.get("x").version == 2
        # replace allows downgrade
        assert reg.register(MotionPreset("x", 1, "d", build=lambda **k: (lambda f, fps: 0.0)), replace=True) is True
        assert reg.get("x").version == 1

    def test_pop_preset_overshoots_then_settles(self) -> None:
        reg = create_default_preset_registry()
        fn = reg.build("pop", start=0)
        # starts at 0
        assert fn(0, 24.0) == pytest.approx(0.0)
        # eventually settles near 1
        assert fn(60, 24.0) == pytest.approx(1.0, abs=1e-3)

    def test_preset_is_deterministic(self) -> None:
        reg = create_default_preset_registry()
        fn = reg.build("slide", start=0, duration=12, distance=100.0, direction="up")
        a = [fn(f, 24.0) for f in range(0, 24)]
        b = [fn(f, 24.0) for f in range(0, 24)]
        assert a == b
