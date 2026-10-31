"""Machine-readable macOS full-chain GPU benchmark report.

This is the M0 reporting boundary from the macOS execution plan. It keeps
machine/input fingerprints separate from runtime evidence and refuses to call
an export full-GPU unless the serialized :class:`ExportGpuTrailSnapshot`
proves all four pixel-resident stages.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .gpu_trail import ExportGpuTrailSnapshot

MACOS_GPU_REPORT_SCHEMA_VERSION = 1


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash an input without loading the complete media file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _ffprobe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        return {"available": False, "reason": "ffprobe_not_found"}
    command = [
        executable, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=20,
        )
        payload = json.loads(completed.stdout or "{}")
        return {"available": True, **payload}
    except Exception as exc:
        return {"available": False, "reason": f"ffprobe_failed:{exc}"}


def build_input_manifest(path: str | Path) -> dict[str, Any]:
    """Return the immutable input manifest required by M0/M12 reports."""
    media_path = Path(path).expanduser().resolve()
    stat = media_path.stat()
    return {
        "path": str(media_path),
        "file_name": media_path.name,
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": sha256_file(media_path),
        "ffprobe": _ffprobe(media_path),
    }


def collect_machine_manifest() -> dict[str, Any]:
    """Collect stable environment facts without claiming a GPU capability."""
    manifest: dict[str, Any] = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version,
    }
    try:
        import av

        manifest["pyav"] = getattr(av, "__version__", "unknown")
    except Exception as exc:
        manifest["pyav"] = {"available": False, "reason": str(exc)}
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            version = subprocess.run(
                [ffmpeg, "-version"], check=True, capture_output=True,
                text=True, timeout=5,
            ).stdout.splitlines()[0]
        except Exception as exc:
            version = f"probe_failed:{exc}"
    else:
        version = "not_found"
    manifest["ffmpeg"] = version
    return manifest


@dataclass
class MacOSGpuBenchmarkReport:
    """A report with the plan's required sections and fail-closed evidence."""

    environment: dict[str, Any]
    inputs: list[dict[str, Any]]
    output: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    gpu_evidence: dict[str, Any] = field(default_factory=dict)
    cpu_evidence: dict[str, Any] = field(default_factory=dict)
    correctness: dict[str, Any] = field(default_factory=dict)
    fallback: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    gpu_trail: Optional[ExportGpuTrailSnapshot] = None
    generated_at_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def full_gpu_claim_reason(self) -> str:
        if self.gpu_trail is None:
            return "gpu_trail_missing"
        return self.gpu_trail.full_gpu_claim_reason()

    @property
    def is_full_gpu(self) -> bool:
        return self.full_gpu_claim_reason == ""

    def to_dict(self) -> dict[str, Any]:
        trail = self.gpu_trail.to_dict() if self.gpu_trail is not None else None
        return {
            "schema_version": MACOS_GPU_REPORT_SCHEMA_VERSION,
            "benchmark": "macos_full_chain_gpu_export",
            "generated_at_utc": self.generated_at_utc,
            "environment": self.environment,
            "inputs": self.inputs,
            "output": self.output,
            "timings": self.timings,
            "gpu_evidence": self.gpu_evidence,
            "cpu_evidence": self.cpu_evidence,
            "correctness": self.correctness,
            "fallback": self.fallback,
            "performance": self.performance,
            "gpu_trail": trail,
            "full_gpu": self.is_full_gpu,
            "full_gpu_claim_reason": self.full_gpu_claim_reason,
        }

    def validate(self) -> None:
        """Validate report shape and ensure the claim cannot be hand-written."""
        if self.environment.get("system") != "Darwin":
            raise ValueError("macOS GPU report requires environment.system=Darwin")
        if not self.inputs:
            raise ValueError("macOS GPU report requires at least one input")
        payload = self.to_dict()
        required = (
            "environment", "inputs", "output", "timings", "gpu_evidence",
            "cpu_evidence", "correctness", "fallback", "performance",
            "gpu_trail", "full_gpu_claim_reason",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"report missing fields: {', '.join(missing)}")
        if payload["full_gpu"] and payload["full_gpu_claim_reason"]:
            raise ValueError("full_gpu report has a non-empty claim reason")

    def write_json(self, path: str | Path) -> Path:
        self.validate()
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            destination,
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
        )
        return destination

    def write_markdown(self, path: str | Path) -> Path:
        self.validate()
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        lines = [
            "# macOS Full-Chain GPU Export Report",
            "",
            f"- Generated: `{self.generated_at_utc}`",
            f"- Inputs: `{len(self.inputs)}`",
            f"- Full GPU: `{data['full_gpu']}`",
            f"- Claim reason: `{data['full_gpu_claim_reason']}`",
            "",
            "## Evidence",
            "",
            "```json",
            json.dumps({
                "gpu_evidence": data["gpu_evidence"],
                "cpu_evidence": data["cpu_evidence"],
                "gpu_trail": data["gpu_trail"],
            }, indent=2, sort_keys=True),
            "```",
            "",
            "## Inputs",
            "",
        ]
        for item in self.inputs:
            lines.append(
                f"- `{item.get('file_name', item.get('path', 'unknown'))}` "
                f"SHA-256 `{item.get('sha256', 'unknown')}`"
            )
        _atomic_write_text(destination, "\n".join(lines) + "\n")
        return destination


def _atomic_write_text(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)

