"""Capture raw Tangent Hub trackerball deltas for motion analysis.

This probe intentionally does not touch Topos' grading panel.  It registers a
managed Tangent application, records the parameter-change stream, and writes
one JSON object per received event so X/Y timing can be analyzed later.

Run from the repository root with::

    .venv/bin/python scripts/tangent_trackerball_probe.py

Stop it with Ctrl-C after the requested movements have been captured.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any

from PyQt6.QtCore import QCoreApplication, QObject, QTimer

# Make direct invocation from any working directory resolve the project's
# existing ``src`` package without requiring PYTHONPATH configuration.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.shared.control_surfaces.tangent import TangentHubClient


PARAMETER_NAMES: dict[int, str] = {
    0x00310021: "lift_x",
    0x00310023: "lift_y",
    0x00310024: "lift_brightness",
    0x00310031: "gamma_x",
    0x00310033: "gamma_y",
    0x00310034: "gamma_brightness",
    0x00310041: "gain_x",
    0x00310043: "gain_y",
    0x00310044: "gain_brightness",
}


class TrackerballProbe(QObject):
    """Own the Hub connection and persist raw trackerball events."""

    def __init__(
        self,
        output_path: Path,
        system_path: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.output_path.open("w", encoding="utf-8")
        self._started_ns = time.monotonic_ns()
        self._event_count = 0
        self._last_report_ns = self._started_ns
        self._report_count = 0
        self._by_parameter: dict[int, list[float]] = defaultdict(list)

        self.client = TangentHubClient(
            application_name="Topos Tangent Trackerball Probe",
            system_path=system_path,
            parent=self,
        )
        self.client.connected.connect(self._on_connected)
        self.client.disconnected.connect(self._on_disconnected)
        self.client.panels_received.connect(self._on_panels_received)
        self.client.parameter_change_received_at.connect(
            self._on_parameter_change_at
        )
        self.client.parameter_reset_received.connect(self._on_parameter_reset)
        self.client.parameter_value_requested.connect(self._on_value_requested)
        self.client.action_on_received.connect(self._on_action)
        self.client.action_off_received.connect(self._on_action)
        self.client.error.connect(self._on_error)

    def start(self) -> None:
        """Start receiving Hub events."""

        self._write_record(
            {
                "type": "probe_started",
                "utc": datetime.now(timezone.utc).isoformat(),
                "monotonic_ns": self._started_ns,
            }
        )
        print(f"[probe] recording raw events to {self.output_path}", flush=True)
        print("[probe] waiting for Tangent Hub and a connected panel…", flush=True)
        self.client.start()

    def stop(self) -> None:
        """Stop the connection and close the JSONL output safely."""

        if self._output.closed:
            return
        self.client.stop()
        self._write_record(
            {
                "type": "probe_stopped",
                "monotonic_ns": time.monotonic_ns(),
                "event_count": self._event_count,
            }
        )
        self._output.close()
        self._print_summary()

    def _on_connected(self) -> None:
        print("[probe] Hub connected; move the Ripple trackerballs when ready.", flush=True)

    def _on_disconnected(self) -> None:
        print("[probe] Hub disconnected; the client will retry automatically.", flush=True)

    def _on_panels_received(self, panels: object) -> None:
        panel_items: list[dict[str, int]] = []
        if isinstance(panels, tuple):
            for panel in panels:
                panel_type = getattr(panel, "panel_type", None)
                panel_id = getattr(panel, "panel_id", None)
                if isinstance(panel_type, int) and isinstance(panel_id, int):
                    panel_items.append(
                        {"panel_type": panel_type, "panel_id": panel_id}
                    )
        self._write_record(
            {
                "type": "panels",
                "monotonic_ns": time.monotonic_ns(),
                "panels": panel_items,
            }
        )
        print(f"[probe] panels reported by Hub: {panel_items}", flush=True)
        print("[probe] start with slow horizontal, vertical, and diagonal strokes.", flush=True)

    def _on_parameter_change_at(
        self,
        parameter_id: int,
        increment: float,
        timestamp_ns: int,
    ) -> None:
        value = float(increment)
        self._event_count += 1
        self._by_parameter[parameter_id].append(value)
        self._write_record(
            {
                "type": "parameter_change",
                "monotonic_ns": timestamp_ns,
                "parameter_id": f"0x{parameter_id:08x}",
                "parameter_name": PARAMETER_NAMES.get(parameter_id, "unknown"),
                "increment": value,
            }
        )

        if timestamp_ns - self._last_report_ns >= 1_000_000_000:
            elapsed = max(0.001, (timestamp_ns - self._started_ns) / 1_000_000_000)
            rate = self._event_count / elapsed
            self._report_count += 1
            print(
                f"[probe] events={self._event_count} rate={rate:.1f}/s "
                f"last={PARAMETER_NAMES.get(parameter_id, hex(parameter_id))} "
                f"increment={value!r}",
                flush=True,
            )
            self._last_report_ns = timestamp_ns

    def _on_parameter_reset(self, parameter_id: int) -> None:
        self._write_record(
            {
                "type": "parameter_reset",
                "monotonic_ns": time.monotonic_ns(),
                "parameter_id": f"0x{parameter_id:08x}",
            }
        )

    def _on_value_requested(self, parameter_id: int) -> None:
        self._write_record(
            {
                "type": "parameter_value_requested",
                "monotonic_ns": time.monotonic_ns(),
                "parameter_id": f"0x{parameter_id:08x}",
            }
        )

    def _on_action(self, action_id: int) -> None:
        self._write_record(
            {
                "type": "action",
                "monotonic_ns": time.monotonic_ns(),
                "action_id": f"0x{action_id:08x}",
            }
        )

    def _on_error(self, message: str) -> None:
        self._write_record(
            {
                "type": "hub_error",
                "monotonic_ns": time.monotonic_ns(),
                "message": str(message),
            }
        )
        print(f"[probe] Hub: {message}", file=sys.stderr, flush=True)

    def _write_record(self, record: dict[str, Any]) -> None:
        self._output.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._output.flush()

    def _print_summary(self) -> None:
        print(f"[probe] captured {self._event_count} parameter changes.", flush=True)
        for parameter_id in sorted(self._by_parameter):
            values = self._by_parameter[parameter_id]
            nonzero = [abs(value) for value in values if value != 0.0]
            minimum = min(nonzero) if nonzero else 0.0
            print(
                f"[probe] {PARAMETER_NAMES.get(parameter_id, hex(parameter_id))}: "
                f"n={len(values)} min_abs_nonzero={minimum!r} "
                f"min={min(values)!r} max={max(values)!r}",
                flush=True,
            )


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the short-lived diagnostic probe."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/topos_tangent_trackerball_probe.jsonl"),
        help="JSONL output path (default: /tmp/topos_tangent_trackerball_probe.jsonl)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Maximum capture duration in seconds (default: 300)",
    )
    parser.add_argument(
        "--surface-path",
        type=Path,
        default=Path("src/shared/control_surfaces/tangent/assets"),
        help="Tangent Ripple control-definition directory",
    )
    return parser.parse_args()


def main() -> int:
    """Run the probe until Ctrl-C or the duration limit."""

    args = parse_args()
    app = QCoreApplication(sys.argv)
    probe = TrackerballProbe(
        output_path=args.output,
        system_path=args.surface_path,
        parent=app,
    )
    app.aboutToQuit.connect(probe.stop)

    def request_stop(*_args: object) -> None:
        app.quit()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    QTimer.singleShot(max(1, int(args.duration * 1000.0)), app.quit)
    probe.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
