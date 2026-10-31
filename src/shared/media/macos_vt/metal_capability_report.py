"""Structured output for the macOS Metal capability probe.

The probe is deliberately separate from the runtime capability gate.  A
successful Metal API probe proves that a format can be imported on this
machine; it does not prove that the application has a full GPU decode/render/
encode path.  Keeping that distinction in the report prevents a capability
probe from being mistaken for an export benchmark.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

METAL_CAPABILITY_REPORT_SCHEMA_VERSION = 1

# Stable, machine-readable reasons.  Callers may add a more specific detail
# after a colon, but the prefix remains suitable for dashboards and gates.
FAILURE_REASON_CODES = frozenset({
    "pyobjc_unavailable",
    "metal_device_unavailable",
    "msl_compile_failed",
    "compute_pipeline_failed",
    "compute_dispatch_failed",
    "corevideo_cache_create_failed",
    "cv_return_unsupported",
    "pyobjc_corevideo_route_unusable",
    "probe_output_write_failed",
})


def build_report(
    *,
    status: str,
    device: Mapping[str, Any] | None = None,
    compute: Mapping[str, Any] | None = None,
    format_matrix: list[Mapping[str, Any]] | None = None,
    pyobjc_route: Mapping[str, Any] | None = None,
    failure_reasons: list[str] | None = None,
    transcript: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """Build a stable JSON-safe report payload from probe observations."""
    reasons = list(failure_reasons or [])
    payload: dict[str, Any] = {
        "schema_version": METAL_CAPABILITY_REPORT_SCHEMA_VERSION,
        "probe": "macos_metal_capability",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "device": dict(device or {}),
        "compute": dict(compute or {}),
        "format_matrix": [dict(item) for item in (format_matrix or [])],
        "pyobjc_route": dict(pyobjc_route or {}),
        "failure_reasons": reasons,
        "transcript": transcript,
        "full_chain_gpu_claim": False,
        "full_chain_gpu_claim_reason": (
            "capability_probe_only; export_gpu_trail_required"
        ),
    }
    if error:
        payload["error"] = error
    return payload


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a compact human-readable view without hiding unsupported cases."""
    lines = [
        "# macOS Metal Capability Probe",
        "",
        f"- Status: `{report.get('status', 'unknown')}`",
        f"- Generated: `{report.get('generated_at_utc', 'unknown')}`",
        "- Full-chain GPU claim: `false`",
        f"- Claim reason: `{report.get('full_chain_gpu_claim_reason', 'unknown')}`",
        "",
        "## Device",
        "",
    ]
    device = report.get("device") or {}
    if device:
        for key in ("name", "has_unified_memory", "max_threads_per_group", "argument_buffers_tier", "max_buffer_length_bytes"):
            if key in device:
                lines.append(f"- `{key}`: `{device[key]}`")
    else:
        lines.append("- No Metal device was available.")

    lines.extend(["", "## Format matrix", "", "| Format | Status | Detail |", "|---|---|---|"])
    matrix = report.get("format_matrix") or []
    if not matrix:
        lines.append("| — | unknown | no format result |")
    else:
        for item in matrix:
            label = item.get("label", "unknown")
            state = item.get("status", "unknown")
            detail = item.get("detail", item.get("reason", ""))
            lines.append(f"| `{label}` | `{state}` | `{detail}` |")

    lines.extend(["", "## Failure reasons", ""])
    reasons = report.get("failure_reasons") or []
    if reasons:
        lines.extend(f"- `{reason}`" for reason in reasons)
    else:
        lines.append("- none")
    lines.extend([
        "",
        "## Evidence boundary",
        "",
        "This report proves capability-probe observations only. Full-chain GPU "
        "status must come from an ExportGpuTrail with decode, render, "
        "yuv_conversion, and encode_submit GPU timestamps and zero CPU pixel "
        "upload/download bytes.",
        "",
    ])
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)


def write_report(
    report: Mapping[str, Any],
    *,
    json_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> None:
    """Write JSON and/or Markdown atomically, preserving the probe result."""
    if json_path is None and markdown_path is None:
        return
    try:
        if json_path is not None:
            _atomic_write_text(
                Path(json_path).expanduser(),
                json.dumps(dict(report), indent=2, sort_keys=True) + "\n",
            )
        if markdown_path is not None:
            _atomic_write_text(
                Path(markdown_path).expanduser(), render_markdown(report),
            )
    except Exception as exc:
        raise OSError(f"probe_output_write_failed: {exc}") from exc
