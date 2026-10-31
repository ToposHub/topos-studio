"""N23 · W09 固定路由基准假绿修复测试。

审计句：全 child no_frame/无 md5 时 `all(empty or [True])` 仍
correctness_stable=True 且退出 0；越界 seek 返回 None 未检查；
i%24 循环不能称连续吞吐。修复契约：三态正确性判定（verified/
mismatch/unverified_*）、路由命中校验、seek None 显式失败态、
失败非零退出且报告保留、环形窗口指标如实命名。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bench_ab_routes_w09_n23", ROOT / "scripts" / "bench_ab_routes_w09.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ok(route: str, md5: str = "a" * 32, frame_type: str = "planar",
        fps: float = 100.0) -> dict:
    return {"status": "ok", "route": route, "frame_type": frame_type,
            "fps_cyclic24": fps, "seek_med_ms": 5.0, "frame0_md5": md5}


def _run_main(monkeypatch: pytest.MonkeyPatch, responses) -> tuple:
    """responses: (asset, route) -> list of child dicts（循环取用）。"""
    bench = _load()
    counters: dict = {}

    def fake_child(asset, route):
        key = (asset, route)
        idx = counters.get(key, 0)
        counters[key] = idx + 1
        queue = responses(key)
        return queue[idx % len(queue)]

    monkeypatch.setattr(bench, "_run_child", fake_child)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out_path = str(Path(td) / "w09.json")
        monkeypatch.setattr(
            sys, "argv",
            ["bench", "--out", out_path, "--reps", "1"])
        rc = bench.main()
        report = json.loads(Path(out_path).read_text(encoding="utf-8"))
    return rc, report


def test_all_no_frame_fails_nonzero(monkeypatch):
    def responses(key):
        asset, route = key
        if route == "hw_download" and (
                asset, "cpu_planar", "hw_download") in [
                    (p[0], p[1], p[2]) for p in [
                        ("4K_H264.mov", "cpu_planar", "hw_download")]]:
            return [_ok("hw_download")]  # 参照系成功
        return [{"status": "no_frame", "frame_type": ""}]

    rc, report = _run_main(monkeypatch, responses)
    assert rc == 1
    assert report["passed"] is False
    assert report["failures"]
    for pair in report["pairs"]:
        for route, rep in pair["reps"].items():
            if rep["ok_count"] == 0:
                assert rep["correctness"] == "unverified_empty"
    assert all(pair["correctness_stable"] is False
               for pair in report["pairs"])


def test_all_ok_verified_passes(monkeypatch):
    def responses(key):
        asset, route = key
        if route == "cuda_resident":
            return [_ok("cuda_resident", frame_type="cuda_resident",
                        md5=None)]  # 设备驻留：md5 None
        return [_ok(route, md5="ref" * 8, frame_type="planar")]

    rc, report = _run_main(monkeypatch, responses)
    # cpu/hw 对 md5 与参照一致 → verified 稳定（证据非空且全真）；
    # cuda_resident 对不可 CPU 对拍 → unverified_external → 非稳定
    stable_flags = [pair["correctness_stable"] for pair in report["pairs"]]
    assert True in stable_flags
    resident_pairs = [p for p in report["pairs"]
                      if "cuda_resident" in p["routes"]]
    for pair in resident_pairs:
        assert pair["reps"]["cuda_resident"]["correctness"] == \
            "unverified_external"
        assert pair["correctness_stable"] is False  # 旧代码此处 True（假绿）


def test_md5_mismatch_fails_hard(monkeypatch):
    def responses(key):
        asset, route = key
        if route == "hw_download":
            return [_ok("hw_download", md5="ref" * 8)]
        return [_ok(route, md5="DIFFERENT" + "0" * 24)]

    rc, report = _run_main(monkeypatch, responses)
    assert rc == 1
    mismatched = [f for f in report["failures"] if "mismatch" in f]
    assert mismatched
    for pair in report["pairs"]:
        if pair["routes"][0] == "cpu_planar":
            assert pair["reps"]["cpu_planar"]["correctness"] == "mismatch"


def test_device_resident_is_unverified_external_not_stable(monkeypatch):
    bench = _load()
    samples = [_ok("cuda_resident", frame_type="cuda_resident", md5=None)]
    verdict, _ = bench._correctness_verdict(samples, "ref" * 8)
    assert verdict == "unverified_external"  # 旧代码此处判 True（假绿）
    samples2 = []
    verdict2, _ = bench._correctness_verdict(samples2, "ref" * 8)
    assert verdict2 == "unverified_empty"
    verdict3, _ = bench._correctness_verdict(samples, None)
    assert verdict3 == "unverified_cpu"


def test_route_miss_detected(monkeypatch):
    bench = _load()
    # cuda_resident 路由退化为 planar → env 未生效/路由退化
    samples = [_ok("cuda_resident", frame_type="planar")]
    hit, seen = bench._route_hit("cuda_resident", samples)
    assert hit is False and seen == ["planar"]
    hit2, _ = bench._route_hit(
        "cuda_resident", [_ok("cuda_resident", frame_type="cuda_resident")])
    assert hit2 is True


def test_seek_none_is_explicit_status(monkeypatch):
    rc_marker = {"seen": False}

    def responses(key):
        asset, route = key
        if route == "hw_download":
            return [_ok("hw_download", md5="ref" * 8)]
        rc_marker["seen"] = True
        return [{"status": "seek_none_at", "seek_target": 180,
                 "frame_type": "planar"}]

    rc, report = _run_main(monkeypatch, responses)
    assert rc == 1
    statuses = {s for pair in report["pairs"]
                for rep in pair["reps"].values()
                for s in rep["statuses"]}
    assert "seek_none_at" in statuses
    assert rc_marker["seen"]
