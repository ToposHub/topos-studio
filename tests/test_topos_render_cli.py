"""A7 headless CLI (scripts/topos_render.py) tests — subprocess level.

Covers the exit-code contract, frame-spec parsing, --json output,
--dry-run, and real 3-frame EXR rendering through the production export
path.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "topos_render.py"


def _make_graph_json(path: Path, width=160, height=120):
    graph = {
        "nodes": [
            {
                "id": "src",
                "type": "color_input",
                "parameters": {"width": width, "height": height},
            },
            {
                "id": "out",
                "type": "output",
                "parameters": {},
            },
        ],
        "connections": [
            {
                "src_node_id": "src",
                "src_port_name": "output",
                "dst_node_id": "out",
                "dst_port_name": "image",
            }
        ],
    }
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def _run(*argv):
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


class TestFrameSpec:
    def test_parse(self):
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location("topos_render", CLI)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.parse_frame_spec("5") == [5]
        assert module.parse_frame_spec("1-3") == [1, 2, 3]
        assert module.parse_frame_spec("1,3,5-7") == [1, 3, 5, 6, 7]
        with pytest.raises(ValueError):
            module.parse_frame_spec("9-1")
        with pytest.raises(ValueError):
            module.parse_frame_spec("")


class TestExitCodes:
    def test_missing_input_is_validation_failure(self, tmp_path):
        proc = _run(str(tmp_path / "nope.json"), "--json")
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert "load failed" in payload["error"]

    def test_empty_graph_is_validation_failure(self, tmp_path):
        bad = tmp_path / "empty.json"
        bad.write_text(json.dumps({"nodes": [], "connections": []}))
        proc = _run(str(bad), "--json")
        assert proc.returncode == 2

    def test_dry_run_validates_without_output(self, tmp_path):
        graph = _make_graph_json(tmp_path / "g.json")
        out_dir = tmp_path / "out"
        proc = _run(str(graph), "--dry-run", "--out", str(out_dir),
                    "--frames", "1-3", "--json")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert not out_dir.exists()

    def test_bad_frame_spec_is_usage_error(self, tmp_path):
        graph = _make_graph_json(tmp_path / "g.json")
        proc = _run(str(graph), "--frames", "9-1", "--json")
        assert proc.returncode == 1

    def test_bad_format_is_usage_error(self, tmp_path):
        graph = _make_graph_json(tmp_path / "g.json")
        proc = _run(str(graph), "--format", "avi", "--json")
        assert proc.returncode == 1


class TestRealRender:
    @pytest.mark.skipif(
        os.environ.get("TOPOS_CLI_GL") != "1",
        reason="headless standalone-GL context creation is machine-state "
               "dependent on macOS (A7 spike: crashes in fresh processes "
               "while working under pytest-launched processes); run with "
               "TOPOS_CLI_GL=1 on a machine with healthy headless GL")
    def test_three_frames_exr_match_direct_api(self, tmp_path):
        """CLI output equals a direct CompositeRenderOutput call of the
        same graph — the CLI wraps the production path, so this pins the
        "identical to GUI export" contract at the byte level."""
        graph = _make_graph_json(tmp_path / "g.json")
        cli_out = tmp_path / "cli"
        proc = _run(str(graph), "--frames", "1-3", "--format", "exr",
                    "--out", str(cli_out), "--json")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["frames"] == [1, 2, 3]
        assert payload["passes"] == ["beauty"]

        cli_files = sorted(p.name for p in cli_out.glob("*.exr"))
        assert cli_files == ["g.0001.exr", "g.0002.exr", "g.0003.exr"]
        assert all((cli_out / n).stat().st_size > 0 for n in cli_files)

        # Direct API run of the same graph + settings.
        from src.features.composite.render.composite_render_output import (
            CompositeRenderOutput,
        )
        api_out = tmp_path / "api"
        api_out.mkdir()
        graph_data = json.loads(graph.read_text())
        outcome = CompositeRenderOutput().render_image_sequence(
            graph_data, str(api_out), "g", "exr", 1, 3)
        assert outcome.success
        for name in cli_files:
            cli_bytes = (cli_out / name).read_bytes()
            api_bytes = (api_out / name).read_bytes()
            assert len(cli_bytes) == len(api_bytes)
            assert cli_bytes == api_bytes
