from __future__ import annotations

import json

from src.shared.media.macos_vt.metal_capability_report import (
    METAL_CAPABILITY_REPORT_SCHEMA_VERSION,
    build_report,
    render_markdown,
    write_report,
)


def test_report_is_fail_closed_and_json_safe(tmp_path) -> None:
    report = build_report(
        status="partial",
        device={"name": "Test Metal", "argument_buffers_tier": 2},
        compute={"status": "supported"},
        format_matrix=[
            {"label": "NV12 8-bit", "status": "supported"},
            {"label": "422 10-bit", "status": "unsupported", "reason": "cv_return_unsupported:-6661"},
        ],
        failure_reasons=["pyobjc_corevideo_route_unusable"],
    )

    assert report["schema_version"] == METAL_CAPABILITY_REPORT_SCHEMA_VERSION
    assert report["full_chain_gpu_claim"] is False
    assert report["full_chain_gpu_claim_reason"]
    assert "422 10-bit" in render_markdown(report)

    write_report(report, json_path=tmp_path / "probe.json", markdown_path=tmp_path / "probe.md")
    payload = json.loads((tmp_path / "probe.json").read_text())
    assert payload == report
    assert "Full-chain GPU claim: `false`" in (tmp_path / "probe.md").read_text()


def test_report_writer_accepts_json_only(tmp_path) -> None:
    report = build_report(status="unavailable", error="no pyobjc")
    write_report(report, json_path=tmp_path / "probe.json")
    assert json.loads((tmp_path / "probe.json").read_text())["status"] == "unavailable"
