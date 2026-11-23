"""Measure Resolve's native primaries with untagged, full-range RGB TIFFs."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.color.lut_loader import LoadedLut, sample_lut_trilinear

WIDTH, HEIGHT, TILE = 1920, 1080, 5
GRID, HOLDOUT, GRAY, MAX_CODE = 33, 4096, 1024, 65535
COLOR_SPACE = "Rec.709 Gamma 2.4"
PROJECT_SETTINGS = {
    "colorScienceMode": "davinciYRGB",
    "separateColorSpaceAndGamma": "0",
    "colorSpaceTimeline": COLOR_SPACE,
    "colorSpaceOutput": COLOR_SPACE,
    "useColorSpaceAwareGradingTools": "0",
    "timelineResolutionWidth": str(WIDTH),
    "timelineResolutionHeight": str(HEIGHT),
    "timelineFrameRate": "24",
}
CHART_NAME = "rec709-g24-chart.tif"
TONE_CURVE = 2.4
PRIMARY = {"lift", "gamma", "gain", "offset"}
LOG = {"log_shadow", "log_midtone", "log_highlight", "log_offset"}
RGB = {"r", "g", "b"}
PRIMARY_SCALARS = {"temp": 0.0, "tint": 0.0, "contrast": 1.0, "vibrance": 0.0,
                   "shadow": 0.0, "highlights": 0.0, "saturation": 50.0, "hue_shift": 50.0}


@dataclass(frozen=True)
class Edit:
    mode: str
    control: str
    channel: str
    value: float

    @property
    def key(self) -> tuple[str, str]:
        control = "offset" if self.control == "log_offset" else self.control
        return control, self.channel

    @property
    def delta(self) -> float:
        neutral = {"gain": 1.0, "offset": 25.0}.get(self.key[0], PRIMARY_SCALARS.get(self.control, 0.0))
        return round(self.value - neutral, 8)


def parse_edits(raw: Any) -> list[Edit]:
    if not isinstance(raw, list):
        raise ValueError("Edits must be a JSON list of [mode, control, channel, value]")
    edits = []
    seen = set()
    for row in raw:
        if not isinstance(row, list) or len(row) != 4 or not all(isinstance(x, str) for x in row[:3]):
            raise ValueError(f"Invalid edit: {row!r}")
        mode, control, channel, value = row
        controls = {"wheels": PRIMARY, "bars": PRIMARY, "log": LOG,
                    "primaries": PRIMARY_SCALARS}.get(mode, set())
        channels = ({"value"} if mode == "primaries" else
                    RGB | {"y"} if control in {"lift", "gamma", "gain"} else RGB)
        if (control not in controls or channel not in channels or type(value) not in (float, int)
                or not np.isfinite(value)):
            raise ValueError(f"Invalid edit: {row!r}")
        edit = Edit(mode, control, channel, float(value))
        if edit.key in seen:
            raise ValueError(f"Repeat edit for {edit.key}")
        if not edit.delta:
            raise ValueError(f"Omit neutral controls from edits: {row!r}")
        seen.add(edit.key)
        edits.append(edit)
    return edits


def chart_codes() -> np.ndarray:
    """Return RGB cube (R fastest), half-cell validation samples and gray ramp."""
    b, g, r = np.indices((GRID,) * 3)
    cube = np.stack((r, g, b), axis=-1).reshape(-1, 3) / (GRID - 1)
    # Stride is coprime with the number of cells, so the holdout has no repeats.
    n = (np.arange(HOLDOUT) * 7919) % (GRID - 1)**3
    b, g, r = np.unravel_index(n, (GRID - 1,) * 3)
    holdout = (np.stack((r, g, b), axis=-1) + 0.5) / (GRID - 1)
    gray = np.repeat(np.linspace(0.0, 1.0, GRAY)[:, None], 3, axis=1)
    return np.rint(np.concatenate((cube, holdout, gray)) * MAX_CODE).astype(np.uint16)


def write_chart(path: Path) -> None:
    rows, cols = HEIGHT // TILE, WIDTH // TILE
    tiles = np.zeros((rows * cols, 3), dtype=np.uint16)
    codes = chart_codes()
    tiles[:len(codes)] = codes[:, ::-1]  # OpenCV writes BGR.
    image = tiles.reshape(rows, cols, 3).repeat(TILE, axis=0).repeat(TILE, axis=1)
    if path.exists():
        raise ValueError(f"Chart already exists: {path}")
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_TIFF_COMPRESSION, 1]):
        raise OSError(f"Cannot write {path}")


def read_patches(path: Path, *, flat_tolerance: int = 2) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint16 or image.shape != (HEIGHT, WIDTH, 3):
        raise ValueError(f"Expected a {WIDTH}×{HEIGHT} RGB 16-bit TIFF: {path}")
    count = GRID**3 + HOLDOUT + GRAY
    y, x = np.divmod(np.arange(count), WIDTH // TILE)
    y, x = y * TILE + TILE // 2, x * TILE + TILE // 2
    centers = image[y, x, ::-1]
    max_delta = 0
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        neighbor = image[y + dy, x + dx, ::-1].astype(np.int32)
        max_delta = max(max_delta, int(np.max(np.abs(neighbor - centers))))
    if max_delta > flat_tolerance:
        raise ValueError(f"Chart patches are not flat (delta {max_delta} codes). "
                         f"Check scaling and spatial grading: {path}")
    return centers


def _require(result: Any, action: str) -> Any:
    if not result:
        raise RuntimeError(f"Resolve could not {action}")
    return result


def _resolve() -> Any:
    try:
        api = importlib.import_module("DaVinciResolveScript")
    except ImportError as exc:
        raise RuntimeError("Set PYTHONPATH and RESOLVE_SCRIPT_LIB per Resolve's scripting README") from exc
    return _require(api.scriptapp("Resolve"), "connect to the running Resolve Studio session")


def _check_settings(project: Any) -> None:
    for key, expected in PROJECT_SETTINGS.items():
        actual = project.GetSetting(key)
        matches = (str(actual) in (expected, f"{expected}.0") if key == "timelineFrameRate"
                   else str(actual) == expected)
        if not matches:
            raise ValueError(f"Resolve project {key} is {actual!r}; expected {expected!r}")


def setup(work: Path) -> None:
    """Create a separate Resolve project containing just the chart."""
    manifest = work / "session.json"
    if manifest.exists():
        raise ValueError(f"Session already exists: {manifest}")
    chart = work / CHART_NAME
    if not np.array_equal(read_patches(chart), chart_codes()):
        raise ValueError(f"Chart values do not match the calibration chart: {chart}")
    resolve = _resolve()
    pm = _require(resolve.GetProjectManager(), "get project manager")
    previous = _require(pm.GetCurrentProject(), "identify the current project")
    original = previous.GetName()
    page = resolve.GetCurrentPage()
    name = f"Topos Primary Calibration {work.name}"
    _require(pm.SaveProject(), "save the open project before switching")
    project = _require(pm.CreateProject(name), "create the separate calibration project")
    try:
        for key, val in PROJECT_SETTINGS.items():
            if project.GetSetting(key) != val:
                _require(project.SetSetting(key, val), f"set {key} to {val}")
        _check_settings(project)
        pool = _require(project.GetMediaPool(), "get the calibration media pool")
        source = _require(pool.ImportMedia([str(chart)]), "import the TIFF chart")[0]
        if source.GetClipProperty("Data Level") != "Full":
            _require(source.SetClipProperty("Data Level", "Full"), "set chart input levels to Full")
        timeline = _require(pool.CreateTimelineFromClips("Primary calibration", [source]),
                            "create the calibration timeline")
        _require(project.SetCurrentTimeline(timeline), "select the calibration timeline")
        _require(resolve.OpenPage("color"), "open the Color page")
        _require(pm.SaveProject(), "save the calibration project")
        _write_json(manifest, {"project": name, "previous_project": original, "previous_page": page,
                               "resolve_version": resolve.GetVersionString(), "settings": PROJECT_SETTINGS})
    except (ValueError, OSError, RuntimeError):
        pm.LoadProject(original)
        if page:
            resolve.OpenPage(page)
        raise


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def capture(work: Path, case: str, edits: list[list[Any]]) -> None:
    """Render one frame of the currently applied Resolve grade to a new case directory."""
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", case) is None:
        raise ValueError("Case name must use lowercase letters, digits, underscores or hyphens")
    parsed = parse_edits(edits)
    if (case == "neutral") != (not parsed):
        raise ValueError("Use --case neutral with no edits, or name a case and specify its edits")
    session = json.loads((work / "session.json").read_text(encoding="utf-8"))
    resolve = _resolve()
    pm = _require(resolve.GetProjectManager(), "get project manager")
    project = _require(pm.GetCurrentProject(), "get the active project")
    if project.GetName() != session["project"]:
        raise ValueError(f"Open the calibration project {session['project']!r} before capturing")
    if resolve.GetVersionString() != session["resolve_version"]:
        raise ValueError("Resolve version changed. Start a new calibration session")
    _check_settings(project)
    timeline = _require(project.GetCurrentTimeline(), "get the calibration timeline")
    clips = timeline.GetItemListInTrack("video", 1)
    if len(clips) != 1 or clips[0].GetMediaPoolItem().GetName() != CHART_NAME:
        raise ValueError("The calibration timeline must contain exactly the chart clip")
    if project.IsRenderingInProgress():
        raise RuntimeError("Resolve is already rendering")
    folder = work / "captures"
    folder.mkdir(exist_ok=True)
    target = folder / case
    if target.exists():
        raise ValueError(f"Capture already exists: {target}")
    with tempfile.TemporaryDirectory(prefix=f".{case}-", dir=folder) as staging:
        _render(resolve, project, timeline, Path(staging), case, edits, session)
        Path(staging).rename(target)


def _render(resolve: Any, project: Any, timeline: Any, target: Path, case: str,
            edits: list[list[Any]], session: dict[str, Any]) -> None:
    page = resolve.GetCurrentPage()
    start = timeline.GetStartFrame()
    if timeline.GetEndFrame() <= start + 1:
        raise ValueError("Calibration timeline needs at least two frames")
    _require(project.SetCurrentRenderMode(1), "set single-clip rendering")
    _require(project.SetCurrentRenderFormatAndCodec("tif", "RGB16"), "select 16-bit RGB TIFF")
    _require(project.SetRenderSettings({
        "SelectAllFrames": False, "MarkIn": start, "MarkOut": start + 1,
        "TargetDir": str(target), "CustomName": case, "ExportVideo": True,
        "ExportAudio": False, "FormatWidth": WIDTH, "FormatHeight": HEIGHT,
        "DataLevel": "Full", "ColorSpaceTag": "Rec.709", "GammaTag": "Gamma 2.4",
    }), "configure the full-range RGB render")
    job = _require(project.AddRenderJob(), "queue the calibration TIFF")
    try:
        _require(project.StartRendering([job]), "start only the calibration render job")
        deadline = time.monotonic() + 30.0
        while project.IsRenderingInProgress():
            if time.monotonic() > deadline:
                project.StopRendering()
                raise RuntimeError("Calibration render timed out")
            time.sleep(0.1)
        status = project.GetRenderJobStatus(job)
        if status.get("JobStatus") != "Complete":
            raise RuntimeError(f"Resolve TIFF render failed: {status}")
        frames = sorted(target.glob("*.tif"))
        if not frames:
            raise OSError(f"No TIFF frames in {target}")
        # Resolve Shadows/Highlights have a small spatial component. Measure
        # the tile centers and bound the locally observed spatial variation.
        tolerance = _flat_tolerance(parse_edits(edits))
        first = read_patches(frames[0], flat_tolerance=tolerance)
        if any(not np.array_equal(read_patches(frame, flat_tolerance=tolerance), first) for frame in frames[1:]):
            raise ValueError("Calibration frames differ; check that the grade is static")
        if not edits and np.max(np.abs(first.astype(np.int32) - chart_codes())) > 2:
            raise ValueError("Neutral render changed RGB codes; check data levels and color management")
        for frame in frames[1:]:
            frame.unlink()
        _write_json(target / "capture.json", {"case": case, "frame": frames[0].name,
                                             "resolve_edits": edits,
                                             "resolve_version": session["resolve_version"]})
    finally:
        if not project.IsRenderingInProgress():
            project.DeleteRenderJob(job)
        if page:
            resolve.OpenPage(page)


def restore(work: Path) -> None:
    session = json.loads((work / "session.json").read_text(encoding="utf-8"))
    resolve = _resolve()
    pm = _require(resolve.GetProjectManager(), "get project manager")
    if pm.GetCurrentProject().GetName() != session["project"]:
        raise ValueError("The calibration project is not active")
    _require(pm.SaveProject(), "save the calibration project")
    _require(pm.LoadProject(session["previous_project"]), "reopen the previous project")
    if session["previous_page"]:
        _require(resolve.OpenPage(session["previous_page"]), "return to the previous page")


def _normalize(codes: np.ndarray) -> np.ndarray:
    return codes.astype(np.float32) / MAX_CODE


def _table(values: np.ndarray, size: int = GRID) -> LoadedLut:
    return LoadedLut(values.reshape(size, size, size, 3), size, "cube")


def _flat_tolerance(edits: list[Edit]) -> int:
    return 256 if any(edit.control in ("shadow", "highlights") for edit in edits) else 2


def _read_capture(path: Path, version: str) -> tuple[list[Edit], np.ndarray]:
    info = json.loads((path / "capture.json").read_text(encoding="utf-8"))
    if info["case"] != path.name or info["resolve_version"] != version:
        raise ValueError(f"Capture name or Resolve version conflicts with this session: {path}")
    frame = Path(info["frame"])
    if frame.name != info["frame"] or frame.suffix != ".tif":
        raise ValueError(f"Invalid captured TIFF name: {path}")
    edits = parse_edits(info["resolve_edits"])
    return edits, read_patches(path / frame, flat_tolerance=_flat_tolerance(edits))


class Calibration:
    """Coded-domain RGB response tables, calibrated only from isolated edits."""

    def __init__(self, work: Path) -> None:
        session = json.loads((work / "session.json").read_text(encoding="utf-8"))
        if session["settings"] != PROJECT_SETTINGS:
            raise ValueError("Session settings do not match the Rec.709 Gamma 2.4 chart")
        self.resolve_version = session["resolve_version"]
        captures = work / "captures"
        edits, neutral = _read_capture(captures / "neutral", self.resolve_version)
        self.neutral_code_max = int(np.max(np.abs(neutral.astype(np.int32) - chart_codes())))
        if edits or self.neutral_code_max > 2:
            raise ValueError("Neutral TIFF failed the RGB code-value round trip")
        self.cases: dict[str, tuple[list[Edit], np.ndarray]] = {}
        self.responses: dict[tuple[str, str], dict[float, LoadedLut]] = {}
        base = _normalize(neutral)[:GRID**3]
        for metadata in sorted(captures.glob("*/capture.json")):
            name = metadata.parent.name
            if name == "neutral":
                continue
            edits, codes = _read_capture(metadata.parent, self.resolve_version)
            if not edits:
                raise ValueError(f"Capture is missing its edits: {name}")
            output = _normalize(codes)
            self.cases[name] = (edits, output)
            if len(edits) == 1:
                edit = edits[0]
                samples = self.responses.setdefault(edit.key, {})
                shift = _table(output[:GRID**3] - base)
                prior = samples.get(edit.delta)
                if prior and np.max(np.abs(prior.table - shift.table)) > 2 / MAX_CODE:
                    raise ValueError(f"Conflicting repeat captures for {edit.key} at {edit.value}")
                samples[edit.delta] = shift
        if not self.responses:
            raise ValueError("Capture at least one isolated edit")

    def apply(self, encoded: np.ndarray, edits: list[Edit]) -> np.ndarray:
        result = encoded.copy()
        for edit in edits:
            samples = self.responses.get(edit.key, {})
            knots = sorted([0.0, *samples])
            if not knots[0] <= edit.delta <= knots[-1]:
                raise ValueError(f"No measured interval for {edit.key} at {edit.value}; "
                                 f"available deltas from neutral: {knots}")
            high_index = min(int(np.searchsorted(knots, edit.delta, side="right")), len(knots) - 1)
            low, high = knots[high_index - 1], knots[high_index]
            weight = (edit.delta - low) / (high - low)
            for knot, scale in ((low, 1.0 - weight), (high, weight)):
                if knot and scale:
                    result += scale * sample_lut_trilinear(samples[knot], encoded)
        return np.clip(result, 0.0, 1.0)

    def bake(self, path: Path, *, case: str | None, edits: list[Edit], size: int,
             domain: str) -> None:
        if path.suffix.lower() != ".cube":
            raise ValueError("Output file must have a .cube extension")
        b, g, r = np.indices((size,) * 3, dtype=np.float32)
        points = np.stack((r, g, b), axis=-1) / (size - 1)
        encoded = points ** (1.0 / TONE_CURVE) if domain == "linear" else points
        if case is not None:
            if case not in self.cases:
                raise ValueError(f"Unknown capture: {case}")
            output = sample_lut_trilinear(_table(self.cases[case][1][:GRID**3]), encoded)
        else:
            output = self.apply(encoded, edits)
        if domain == "linear":
            output = output**TONE_CURVE
        with path.open("x", encoding="ascii") as stream:
            stream.write(f'# Rec.709 primaries, {domain} RGB, measured in Resolve {self.resolve_version}\n')
            stream.write(f'TITLE "Measured Rec709 {domain} primary calibration"\n')
            stream.write(f"LUT_3D_SIZE {size}\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
            np.savetxt(stream, output.reshape(-1, 3), fmt="%.8f")


def _topos_response(encoded: np.ndarray, edits: list[Edit], *, calibrated: bool = False) -> np.ndarray:
    from PyQt6.QtWidgets import QApplication

    from src.features.color.core.color_graph_cpu import apply_layer_grading_cpu
    from src.features.color.core.primary_calibration import PRIMARY_CALIBRATION_VERSION_KEY
    from src.features.color.ui.color_wheel import GradingPanel

    app = QApplication.instance() or QApplication([])
    panel = GradingPanel()
    if calibrated:
        panel.set_extended_params({PRIMARY_CALIBRATION_VERSION_KEY: 1})
    bars = dict(zip(("lift", "gamma", "gain", "offset"), panel.color_bars_panel.columns()))
    fields = {"y": "_bright_drag", "r": "_r_drag", "g": "_g_drag", "b": "_b_drag"}
    for edit in edits:
        value = edit.delta if edit.key[0] == "offset" and not calibrated else edit.value
        if edit.mode == "primaries":
            scalar = panel._scalar_rows[edit.control][0]
            if edit.control == "temp" and not calibrated:
                # Resolve uses a different visible temperature scale. Compare
                # the matching numeric offset where the old Topos UI allows it.
                value = min(100.0, max(-100.0, value))
            scalar.setValue((value - scalar._offset) / scalar._scale)
        elif edit.mode == "bars":
            column = bars[edit.control]
            column._nums[edit.channel.upper()].setValue(value)
            column.flush_pending_changes()
        else:
            wheel = getattr(panel, f"{edit.control}_wheel")
            getattr(wheel, fields[edit.channel]).setValue(value)
            wheel.flush_pending_changes()
    linear, _ = apply_layer_grading_cpu(encoded**TONE_CURVE, {"grading_params": panel.get_current_params()},
                                        width=1, height=1)
    panel.close()
    # Keep the application alive through widget creation, evaluation and close.
    app.processEvents()
    return np.clip(linear, 0.0, 1.0) ** (1.0 / TONE_CURVE)


def _metrics(predicted: np.ndarray, reference: np.ndarray, source: np.ndarray) -> dict[str, float | None]:
    err = predicted - reference
    baseline = float(np.sqrt(np.mean((source - reference)**2)))
    rmse = float(np.sqrt(np.mean(err**2)))
    return {
        "relative_rmse_reduction": round(1.0 - rmse / baseline, 6) if baseline > 0.0 else None,
        "rmse_10bit": round(rmse * 1023.0, 4),
        "p95_max_channel_10bit": round(float(np.percentile(np.max(np.abs(err), axis=-1), 95)) * 1023.0, 4),
        "max_channel_10bit": round(float(np.max(np.abs(err))) * 1023.0, 4),
    }


def report(calibration: Calibration, *, include_topos: bool) -> dict[str, Any]:
    points = _normalize(chart_codes())[GRID**3:]
    report_data: dict[str, Any] = {
        "resolve_version": calibration.resolve_version,
        "encoding": COLOR_SPACE,
        "data_levels": "Full",
        "neutral_max_16bit_code_error": calibration.neutral_code_max,
        "color_holdout_count": HOLDOUT,
        "gray_holdout_count": GRAY,
        "cases": {},
    }
    groups = {"rgb": slice(0, HOLDOUT), "gray": slice(HOLDOUT, None)}
    for name, (edits, output) in calibration.cases.items():
        target = output[GRID**3:]
        predictions = {}
        if len(edits) == 1:
            predictions["measured_lut"] = sample_lut_trilinear(_table(output[:GRID**3]), points)
        else:
            predictions["interpolated_edits"] = calibration.apply(points, edits)
        if include_topos:
            predictions["topos_wheels_linear_rec709"] = _topos_response(points, edits)
            predictions["topos_measured_primaries"] = _topos_response(points, edits, calibrated=True)
        report_data["cases"][name] = {
            model: {key: _metrics(prediction[span], target[span], points[span])
                    for key, span in groups.items()}
            for model, prediction in predictions.items()
        }
    return report_data


def export_runtime_profile(sessions: list[Calibration], path: Path) -> None:
    """Package measured response knots and derived per-channel wheel responses."""
    from src.features.color.core.primary_calibration import PRIMARY_CALIBRATION_SCHEMA_VERSION

    rows: dict[tuple[str, str, float], np.ndarray] = {}
    if len({session.resolve_version for session in sessions}) != 1:
        raise ValueError("Runtime profile sessions must use the same Resolve version")
    for session in sessions:
        for (control, channel), samples in session.responses.items():
            for knot, table in samples.items():
                key = control, channel, knot
                if key in rows and not np.array_equal(rows[key], table.table):
                    raise ValueError(f"Sessions disagree at {key}")
                rows[key] = table.table
    measured = set(rows)
    points = _normalize(chart_codes())[:GRID**3].reshape(GRID, GRID, GRID, 3)
    luma = (0.2126, 0.7152, 0.0722)

    for control in ("log_shadow", "log_midtone", "log_highlight", "offset"):
        source = next(key for key in sorted(rows) if key[0] == control and key[1] in RGB)
        channel = "rgb".index(source[1])
        step = abs(source[2])
        diag = np.arange(GRID)
        gray = rows[source][diag, diag, diag]
        response = (gray[:, channel] - np.delete(gray, channel, axis=1).mean(axis=1)) / source[2]
        if control == "offset":
            response[:] = np.median(response[1:-1])
        for index, color in enumerate("rgb"):
            for knot in (-step, step):
                delta = np.zeros_like(points)
                delta[..., index] = np.interp(points[..., index], points[diag, diag, diag, index], response) * knot
                rows.setdefault((control, color, knot), np.clip(points + delta, 0.0, 1.0) - points)

    for control in ("lift", "gamma", "gain"):
        for index, channel in enumerate("rgb"):
            x = points[..., index]
            chroma = np.eye(3, dtype=np.float32)[index] - luma[index]
            for knot in (-0.1, 0.1):
                if control == "lift":
                    shift = 2.0 * knot * (1.0 - x)
                elif control == "gain":
                    shift = knot * x
                else:
                    shift = x ** (1.4 ** (-np.sign(knot))) - x
                delta = shift[..., None] * chroma
                rows.setdefault((control, channel, knot), np.clip(points + delta, 0.0, 1.0) - points)

    keys = sorted(rows)
    with path.open("xb") as stream:
        np.savez_compressed(
            stream, schema_version=np.int64(PRIMARY_CALIBRATION_SCHEMA_VERSION),
            control_names=np.array([key[0] for key in keys]),
            channel_names=np.array([key[1] for key in keys]),
            knots=np.array([key[2] for key in keys], dtype=np.float64),
            deltas=np.stack([rows[key] for key in keys]).astype(np.float16),
            measured=np.array([key in measured for key in keys]),
            resolve_version=sessions[0].resolve_version,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("chart", "setup", "capture", "restore", "report", "bake", "profile"))
    parser.add_argument("work", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--edits", type=json.loads)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--domain", choices=("linear", "encoded"), default="linear")
    parser.add_argument("--size", type=int, choices=(33, 65), default=65)
    parser.add_argument("--compare-topos", action="store_true")
    parser.add_argument("--additional-session", type=Path, action="append", default=[])
    args = parser.parse_args()
    work = args.work.resolve()
    if args.action == "chart":
        work.mkdir(parents=True, exist_ok=True)
        write_chart(work / CHART_NAME)
    elif args.action == "capture":
        if not args.case:
            parser.error("capture requires --case")
        capture(work, args.case, args.edits if args.edits is not None else [])
    elif args.action == "report":
        result = report(Calibration(work), include_topos=args.compare_topos)
        if args.output:
            _write_json(args.output, result)
        else:
            print(json.dumps(result, indent=2))
    elif args.action == "bake":
        if not args.output or (args.case is None) == (args.edits is None):
            parser.error("bake requires --output and exactly one of --case or --edits")
        Calibration(work).bake(args.output, case=args.case,
                               edits=parse_edits(args.edits) if args.edits is not None else [],
                               size=args.size, domain=args.domain)
    elif args.action == "profile":
        if not args.output:
            parser.error("profile requires --output")
        export_runtime_profile([Calibration(path) for path in (work, *args.additional_session)], args.output)
    else:
        {"setup": setup, "restore": restore}[args.action](work)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        sys.exit(str(exc))
