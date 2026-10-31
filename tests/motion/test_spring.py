"""W1 spring: golden values, settle measurement, determinism, delay/clamp."""

from __future__ import annotations

import math
import subprocess
import sys

import pytest

from src.features.teaching_animation.motion import SpringConfig, measure_spring, spring


class TestSpringGolden:
    def test_starts_at_from(self) -> None:
        assert spring(0, 24.0, SpringConfig()) == pytest.approx(0.0)

    def test_settles_to_target(self) -> None:
        cfg = SpringConfig()
        settle = measure_spring(24.0, cfg)
        assert spring(settle + 20, 24.0, cfg) == pytest.approx(1.0, abs=1e-3)

    def test_from_to_mapping(self) -> None:
        cfg = SpringConfig()
        v0 = spring(0, 24.0, cfg, from_=10.0, to=20.0)
        settle = measure_spring(24.0, cfg)
        vend = spring(settle + 30, 24.0, cfg, from_=10.0, to=20.0)
        assert v0 == pytest.approx(10.0)
        assert vend == pytest.approx(20.0, abs=1e-3)


class TestSpringDelay:
    def test_value_is_from_before_delay(self) -> None:
        cfg = SpringConfig()
        assert spring(5, 24.0, cfg, delay=10, from_=0.0, to=1.0) == pytest.approx(0.0)
        # at delay frame, spring begins
        assert spring(10, 24.0, cfg, delay=10) == pytest.approx(0.0)


class TestOvershootClamp:
    def test_clamped_never_exceeds_one(self) -> None:
        cfg = SpringConfig(mass=1.0, damping=7.0, stiffness=170.0)  # would overshoot
        max_unclamped = max(spring(f, 24.0, cfg) for f in range(0, 40))
        max_clamped = max(spring(f, 24.0, cfg, overshoot_clamp=True) for f in range(0, 40))
        assert max_unclamped > 1.0
        assert max_clamped <= 1.0 + 1e-9


class TestMeasureSpring:
    def test_measure_positive_and_bounded(self) -> None:
        settle = measure_spring(24.0, SpringConfig())
        assert 1 <= settle <= 600

    def test_stiffer_spring_settles_not_later_than_soft(self) -> None:
        soft = measure_spring(24.0, SpringConfig(mass=1.0, damping=10.0, stiffness=40.0))
        stiff = measure_spring(24.0, SpringConfig(mass=1.0, damping=10.0, stiffness=200.0))
        assert stiff <= soft + 2  # allow tiny jitter; stiff should be about-equal or faster

    def test_invalid_fps_rejected(self) -> None:
        with pytest.raises(ValueError):
            measure_spring(0.0)
        with pytest.raises(ValueError):
            spring(5, 0.0)


class TestDeterminism:
    def test_same_inputs_same_value_in_process(self) -> None:
        cfg = SpringConfig(mass=1.1, damping=9.0, stiffness=150.0)
        a = [spring(f, 24.0, cfg) for f in range(0, 30)]
        b = [spring(f, 24.0, cfg) for f in range(0, 30)]
        assert a == b

    def test_same_inputs_same_value_across_processes(self) -> None:
        """Identical inputs must yield identical values in a fresh process.

        Spring evaluation must be reproducible across preview/export processes;
        a subprocess re-evaluates and compares to the in-process value.
        """
        cfg = SpringConfig(mass=1.3, damping=8.0, stiffness=160.0)
        in_process = [spring(f, 24.0, cfg) for f in range(0, 24)]
        script = (
            "from src.features.teaching_animation.motion import spring, SpringConfig\n"
            "cfg = SpringConfig(mass=1.3, damping=8.0, stiffness=160.0)\n"
            "print([spring(f, 24.0, cfg) for f in range(0, 24)])\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], cwd=".", capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        out_process = eval(completed.stdout.strip(), {"__builtins__": {}}, {})
        assert out_process == in_process


class TestSpringConfigValidation:
    def test_non_positive_rejected(self) -> None:
        with pytest.raises(ValueError):
            SpringConfig(mass=0.0)
        with pytest.raises(ValueError):
            SpringConfig(damping=-1.0)

    def test_overdamped_branch_runs(self) -> None:
        cfg = SpringConfig(mass=1.0, damping=40.0, stiffness=10.0)  # overdamped
        assert spring(0, 24.0, cfg) == pytest.approx(0.0)
        # overdamped: monotonic, no overshoot
        values = [spring(f, 24.0, cfg) for f in range(0, 200)]
        assert all(values[i] <= values[i + 1] + 1e-9 for i in range(len(values) - 1))

    def test_critically_damped_branch_runs(self) -> None:
        # c^2 == 4mk -> discriminant 0
        cfg = SpringConfig(mass=1.0, damping=20.0, stiffness=100.0)  # 400 - 400 = 0
        assert spring(0, 24.0, cfg) == pytest.approx(0.0)
        assert math.isfinite(spring(10, 24.0, cfg))
