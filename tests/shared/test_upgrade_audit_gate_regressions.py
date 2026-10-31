"""升级复核：探针缺口不能被进程零退出码隐藏，采样线程能显式停止。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        Path(relative_path).stem, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("issue_present", [True, False])
def test_probe_exit_zero_still_checks_findings(issue_present: bool) -> None:
    gate = _load("scripts/m18_machine_b_recheck.py")
    payload = json.dumps([{"id": "S01", "issue_present": issue_present}])
    result = gate._run([sys.executable, "-c", f"print({payload!r})"], probe=True)
    assert result["exit_code"] == 0
    assert result["passed"] is (not issue_present)


@pytest.mark.parametrize("payload", ["[]", "{}", "not json",
                                     '[{"id":"S01","issue_present":"false"}]'])
def test_invalid_probe_result_fails_closed(payload: str) -> None:
    gate = _load("scripts/m18_machine_b_recheck.py")
    result = gate._run([sys.executable, "-c", f"print({payload!r})"], probe=True)
    assert result["passed"] is False
    assert result["probe_error"]


def test_soak_sampler_stops_and_closes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PyQt6")
    soak = _load("tests/edit/test_stress_soak_w08.py")
    monkeypatch.setattr(soak._MetricsSampler, "_vram_mib", lambda self: None)
    sampler = soak._MetricsSampler(tmp_path / "samples.jsonl", interval=0.01)
    sampler.start()
    samples = sampler.stop()
    assert samples
    assert not sampler.is_alive()
    assert sampler._out.closed
