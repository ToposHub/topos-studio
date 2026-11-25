"""Generate the Rec.709 X Grade chart and compare native TIFFs with Topos delivery."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import cv2
import numpy as np

from tools import resolve_primary_calibration as chart

_ANCHORS = {
    "warm": (0.50, 0.28, 0.16),
    "green": (0.22, 0.55, 0.18),
    "blue": (0.17, 0.35, 0.60),
    "neutral": (0.48, 0.44, 0.42),
}
_BASELINE = "xgrade-neutral-enabled"


@dataclass(frozen=True)
class _Case:
    color: str
    width: float
    stops: float = 0.0
    scope_offset: tuple[float, float] | None = None
    fit: bool = True


@dataclass(frozen=True)
class _Adjustment:
    anchor_rgb: tuple[float, float, float]
    width: float
    stops: float
    scope_offset: tuple[float, float] | None = None
    scope_reference_pixels: tuple[float, float] | None = None
    fit: bool | None = None


@dataclass(frozen=True)
class _ManifestCase:
    case_id: str
    adjustment: _Adjustment
    native: Path
    native_neutral: Path


@dataclass(frozen=True)
class _Region:
    roi: tuple[int, int, int, int]
    sample_points: tuple[tuple[int, int], ...] = ()


_CASES = {
    "warm-w020-plus1": _Case("warm", 0.20, 1.0),
    "warm-wide-minus1": _Case("warm", 0.60, -1.0),
    "blue-minus075": _Case("blue", 0.20, -0.75),
    "green-w020-plus1": _Case("green", 0.20, 1.0),
    "green-w035-plus05": _Case("green", 0.35, 0.5),
    "blue-w060-minus05": _Case("blue", 0.60, -0.5),
    "blue-w045-minus05": _Case("blue", 0.45, -0.5, fit=False),
    "near-neutral-w030-plus065": _Case("neutral", 0.30, 0.65, fit=False),
    "green-scope-hue": _Case("green", 0.35, scope_offset=(49.0, -19.0)),
    "warm-scope-up90": _Case("warm", 0.20, scope_offset=(0.0, -90.0)),
    "blue-scope-right50-down33": _Case("blue", 0.20, scope_offset=(50.0, 33.0), fit=False),
}


def write_anchors_chart(path: Path) -> None:
    if path.exists():
        raise FileExistsError(path)
    with TemporaryDirectory(prefix="x-grade-chart-") as scratch:
        base = Path(scratch) / "chart.tif"
        chart.write_chart(base)
        image = cv2.imread(str(base), cv2.IMREAD_UNCHANGED)
    for i, color in enumerate(_ANCHORS.values()):
        image[640:960, i * 480:(i + 1) * 480] = np.rint(np.array(color[::-1]) * chart.MAX_CODE)
    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_TIFF_COMPRESSION, 1]):
        raise OSError(f"Cannot write {path}")


def export_topos(source: Path, case: str, directory: Path) -> tuple[Path, dict[str, Any]]:
    """Run a case from the original four-anchor chart comparison."""
    setting = _CASES.get(case)
    adjustment = _Adjustment(_ANCHORS[setting.color], setting.width, setting.stops,
                             setting.scope_offset) if setting is not None else None
    return _export_topos(source, case, directory, (chart.WIDTH, chart.HEIGHT), adjustment)


def _export_topos(source: Path, case: str, directory: Path, size: tuple[int, int],
                  adjustment: _Adjustment | None) -> tuple[Path, dict[str, Any]]:
    from src.features.color.adapters.warper_command_adapter import WarperCommandAdapter
    from src.features.color.core.layer import Layer
    from src.features.color.core.warper_chroma709 import chroma_warp_position
    from src.features.color.core.warper_commands import WarperCommandService
    from src.features.color.core.warper_state import WarperStroke, XGradeState
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import ExportCodec, TimelineExportConfig, create_timeline_exporter
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational
    from src.shared.project.color_stack import ColorStackData

    layer = Layer("X Grade")
    if adjustment is not None:
        rgb = adjustment.anchor_rgb
        point = chroma_warp_position(np.array(rgb))
        target = point
        if adjustment.scope_offset is not None:
            from src.features.color.ui.x_grade_panel import _REFERENCE_SCOPE_PIXELS, _SCOPE_INVERSE

            reference = adjustment.scope_reference_pixels or (_REFERENCE_SCOPE_PIXELS,) * 2
            target = np.clip(point + (np.asarray(adjustment.scope_offset) / reference) @ _SCOPE_INVERSE, 0.0, 1.0)
        stroke = WarperStroke(
            case, "normal", rgb, rgb, {"chroma_range": adjustment.width / 5.0, "exposure": adjustment.stops},
            (tuple(point), tuple(target)),
        )
        grade = XGradeState(strokes=(stroke,))
        adapter = WarperCommandAdapter(WarperCommandService(), lambda: layer)
        result = adapter.submit({
            "mode": "x_grade", "action": "x_grade_edit",
            "payload": {"phase": "atomic", "state": grade.to_dict()},
        })
        if not result["ok"]:
            raise RuntimeError(result)
    stack = ColorStackData(stack_id="stack", layers=(layer.to_dict(),))
    asset = MediaAsset("chart", str(source), "image", "Rec.709 chart", {})
    project = EditProject("comparison", "X Grade comparison", assets={asset.asset_id: asset},
                          color_stacks={stack.stack_id: stack}).with_color_management({
        "color.timeline_space": "rec709-g24", "color.output_space": "rec709-g24",
    })
    clip = ClipInstance("chart", asset.asset_id, "v0", 0, 1, 0, 1, color_stack_id=stack.stack_id)
    sequence = TimelineSequence(
        timeline_id="chart", name="X Grade chart", width=size[0], height=size[1],
        fps=Rational(24, 1), video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
    )
    directory.mkdir(parents=True, exist_ok=False)
    config = TimelineExportConfig(
        output_path=str(directory / case), codec=ExportCodec.TIFF_SEQUENCE, bit_depth=16,
        timeline_id=sequence.timeline_id, include_audio=False, burn_subtitles=False,
    )
    exporter = create_timeline_exporter(project, sequence)
    outcome = exporter.export_timeline(config)
    if not outcome.success:
        raise RuntimeError(f"Topos TIFF render failed for {case} ({outcome.cause})")
    return next(directory.glob("*.tif")), outcome.stage_telemetry


def _statistics(delta: np.ndarray) -> dict[str, dict[str, float]]:
    groups = {
        "rgb_grid": delta[:chart.GRID**3],
        "rgb_holdout": delta[chart.GRID**3:-chart.GRAY],
        "gray_ramp": delta[-chart.GRAY:],
    }
    return {name: {
        "rmse_10bit": float(np.sqrt(np.mean(values**2))),
        "p95_max_channel_10bit": float(np.quantile(np.abs(values).max(axis=-1), 0.95)),
        "max_10bit": float(np.abs(values).max()),
    } for name, values in groups.items()}


def _read(path: Path) -> np.ndarray:
    return chart.read_patches(path, flat_tolerance=None).astype(np.float64) / chart.MAX_CODE


def compare(work: Path, output: Path) -> None:
    def native(case: str) -> Path:
        return work / "baselight-renders" / case / f"{case}.0000.tif"

    source = work / "rec709-g24-anchors.tif"
    local_neutral, telemetry = export_topos(source, "anchors-neutral", output / "anchors-neutral")
    expected = _read(native("anchors-neutral"))
    native_baseline = _read(native(_BASELINE))
    actual = _read(local_neutral)
    report: dict[str, Any] = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "color_space": "Rec.709 Gamma 2.4 Full",
        "neutral": {
            "raw_difference": _statistics((actual - expected) * 1023),
            "baselight_enabled_vs_no_operator": _statistics((native_baseline - expected) * 1023),
            "baselight_short_viewer_gestures": {
                name: _statistics((_read(native(name)) - native_baseline) * 1023)
                for name in ("green-drag-right60", "warm-viewer-right120")
            },
            "topos_export": telemetry,
        },
        "cases": {},
    }
    for name, setting in _CASES.items():
        top, telemetry = export_topos(source, name, output / name)
        vendor = native(name)
        top_pixels = _read(top)
        native_pixels = _read(vendor)
        local_delta = top_pixels - actual
        native_delta = native_pixels - native_baseline
        report["cases"][name] = {
            "anchor_rgb": _ANCHORS[setting.color], "width": setting.width, "stops": setting.stops,
            "scope_offset_estimated_pixels": setting.scope_offset,
            "fit": setting.fit, "baselight_baseline": _BASELINE,
            "baselight_sha256": hashlib.sha256(vendor.read_bytes()).hexdigest(),
            "ungraded_error": _statistics(native_delta * 1023),
            "color_difference": _statistics((local_delta - native_delta) * 1023),
            "raw_color_difference": _statistics((top_pixels - native_pixels) * 1023),
            "topos_export": telemetry,
        }
    (output / "comparison.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _path(raw: Any, directory: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"Missing {label} path")
    path = (directory / raw).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Missing {label}: {path}")
    return path


def _read_tiff16(path: Path, size: tuple[int, int]) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint16 or image.shape != (size[1], size[0], 3):
        raise ValueError(f"Expected a {size[0]}×{size[1]} RGB 16-bit TIFF: {path}")
    return image


def _ints(raw: Any, count: int, label: str) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) != count or any(type(value) is not int for value in raw):
        raise ValueError(f"{label} must contain {count} integers")
    return tuple(raw)


def _numbers(raw: Any, count: int, label: str) -> tuple[float, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) != count or any(
            type(value) not in (int, float) or not np.isfinite(value) for value in raw):
        raise ValueError(f"{label} must contain {count} finite numbers")
    return tuple(float(value) for value in raw)


def _regions(manifest: Mapping[str, Any], size: tuple[int, int]) -> dict[str, _Region]:
    regions = {}
    for group in ("anchors", "groups", "clips"):
        entries = manifest.get(group, {})
        if not isinstance(entries, Mapping):
            raise ValueError(f"Invalid manifest section {group}")
        for name, raw in entries.items():
            label = f"{group}.{name}"
            if not isinstance(raw, Mapping):
                raise ValueError(f"Invalid manifest entry {label}")
            x, y, w, h = _ints(raw.get("roi"), 4, label)
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > size[0] or y + h > size[1]:
                raise ValueError(f"ROI is outside the image: {label}")
            raw_points = raw.get("sample_points", ())
            if not isinstance(raw_points, (list, tuple)):
                raise ValueError(f"Invalid sample points for {label}")
            points = tuple(_ints(point, 2, label) for point in raw_points)
            if any(not (x <= px < x + w and y <= py < y + h) for px, py in points):
                raise ValueError(f"Sample point is outside its ROI: {label}")
            regions[label] = _Region((x, y, w, h), points)
    if not regions:
        raise ValueError("Manifest needs at least one ROI")
    return regions


def _manifest_cases(path: Path, anchors: Mapping[str, Any], source: np.ndarray,
                    selected: set[str]) -> list[_ManifestCase]:
    config = json.loads(path.read_text(encoding="utf-8"))
    entries = config.get("cases") if isinstance(config, Mapping) else config
    common_neutral = config.get("native_neutral") if isinstance(config, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("Expected a nonempty JSON case list or an object with a cases list")
    seen: set[str] = set()
    cases = []
    for entry in entries:
        case_id = entry.get("id") if isinstance(entry, Mapping) else None
        if not isinstance(case_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", case_id):
            raise ValueError(f"Invalid case ID: {case_id!r}")
        if case_id in seen:
            raise ValueError(f"Duplicate case ID: {case_id}")
        seen.add(case_id)
        if selected and case_id not in selected:
            continue
        anchor = entry.get("anchor")
        if "anchor_xy" in entry:
            if anchor is not None:
                raise ValueError(f"Specify anchor or anchor_xy in {case_id}")
            x, y = _ints(entry["anchor_xy"], 2, f"{case_id}.anchor_xy")
            if not (0 <= x < source.shape[1] and 0 <= y < source.shape[0]):
                raise ValueError(f"Anchor is outside the image: {case_id}")
            anchor = (source[y, x, ::-1] / chart.MAX_CODE).tolist()
        if isinstance(anchor, str):
            if anchor not in anchors or not isinstance(anchors[anchor], Mapping):
                raise ValueError(f"Unknown anchor {anchor!r} in {case_id}")
            anchor = anchors[anchor].get("rgb")
        rgb = _numbers(anchor, 3, f"{case_id}.anchor")
        width, stops = _numbers([entry.get("width", 0.2), entry.get("stops", 0.0)], 2, case_id)
        if not all(0.0 <= value <= 1.0 for value in rgb) or not (0.0 <= width <= 5.0 and -8.0 <= stops <= 8.0):
            raise ValueError(f"Anchor or controls outside the X Grade domain: {case_id}")
        offset = entry.get("scope_offset")
        reference = entry.get("scope_reference_pixels", 900.0)
        offset = _numbers(offset, 2, f"{case_id}.scope_offset") if offset is not None else None
        if type(reference) in (int, float):
            reference = (reference, reference)
        reference = _numbers(reference, 2, f"{case_id}.scope_reference_pixels")
        if min(reference) <= 0:
            raise ValueError(f"Scope reference dimensions must be positive: {case_id}")
        fit = entry.get("fit")
        if fit is not None and type(fit) is not bool:
            raise ValueError(f"fit must be a boolean: {case_id}")
        cases.append(_ManifestCase(
            case_id, _Adjustment(rgb, width, stops, offset, reference, fit),
            _path(entry.get("native"), path.parent, f"{case_id}.native"),
            _path(entry.get("native_neutral", common_neutral), path.parent, f"{case_id}.native_neutral"),
        ))
    if missing := selected - seen:
        raise ValueError(f"Unknown case IDs: {', '.join(sorted(missing))}")
    return cases


def _pixel_statistics(code_delta: np.ndarray) -> dict[str, float]:
    delta = code_delta.astype(np.float32, copy=False) * (1023.0 / chart.MAX_CODE)
    maximum_channel = np.abs(delta).max(axis=-1)
    return {
        "rmse_10bit": float(np.sqrt(np.mean(delta**2, dtype=np.float64))),
        "p95_max_channel_10bit": float(np.quantile(maximum_channel, 0.95)),
        "max_10bit": float(maximum_channel.max()),
    }


def _pixel_differences(topos: np.ndarray, native: np.ndarray, topos_neutral: np.ndarray,
                       native_neutral: np.ndarray) -> dict[str, dict[str, float]]:
    raw = np.subtract(topos, native, dtype=np.int32)
    neutral = np.subtract(topos_neutral, native_neutral, dtype=np.int32)
    return {
        "raw_color_difference": _pixel_statistics(raw),
        "color_difference": _pixel_statistics(raw - neutral),
        "neutral_difference": _pixel_statistics(neutral),
        "baselight_change": _pixel_statistics(np.subtract(native, native_neutral, dtype=np.int32)),
        "topos_change": _pixel_statistics(np.subtract(topos, topos_neutral, dtype=np.int32)),
    }


def _compare_region(region: _Region, images: tuple[np.ndarray, ...]) -> dict[str, Any]:
    x, y, w, h = region.roi
    result: dict[str, Any] = {"roi": region.roi, "pixels": w * h}
    result.update(_pixel_differences(*(image[y:y + h, x:x + w] for image in images)))
    if region.sample_points:
        xs, ys = np.asarray(region.sample_points, dtype=np.intp).T
        result["center_samples"] = {
            "pixels": len(xs),
            **_pixel_differences(*(image[ys, xs] for image in images)),
        }
    return result


def _previous_metrics(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    metrics = {}
    for name in ("raw_color_difference", "color_difference"):
        if name in previous:
            metrics[name] = {
                "previous": previous[name],
                "delta_from_previous": {key: value - previous[name][key] for key, value in current[name].items()},
            }
    if "center_samples" in current and "center_samples" in previous:
        metrics["center_samples"] = _previous_metrics(current["center_samples"], previous["center_samples"])
    return metrics


def compare_manifest(manifest_path: Path, cases_path: Path, output: Path, *,
                     selected: set[str] | None = None, compare_to: Path | None = None) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise ValueError("Expected a version 1 image manifest")
    image = manifest.get("image")
    if not isinstance(image, Mapping):
        raise ValueError("Image metadata missing from manifest")
    size = _ints([image.get("width"), image.get("height")], 2, "image size")
    if min(size) <= 0:
        raise ValueError("Image dimensions must be positive")
    source = _path(image.get("path") or image.get("filename"), manifest_path.parent, "source")
    source_digest = _hash(source)
    if image.get("sha256") and image["sha256"] != source_digest:
        raise ValueError(f"Source digest does not match manifest: {source}")
    regions = _regions(manifest, size)
    cases = _manifest_cases(cases_path, manifest.get("anchors", {}), _read_tiff16(source, size), selected or set())
    previous = json.loads(compare_to.read_text(encoding="utf-8")) if compare_to else None
    manifest_digest = _hash(manifest_path)
    if previous is not None and (not isinstance(previous, Mapping) or
            previous.get("schema_version") != 2 or previous.get("source_sha256") != source_digest
            or previous.get("manifest_sha256") != manifest_digest):
        raise ValueError("Previous report requires the identical source TIFF and version 1 ROI manifest")

    top_neutral, telemetry = _export_topos(source, "neutral", output / "topos-neutral", size, None)
    top_neutral_pixels = _read_tiff16(top_neutral, size)
    native_neutrals: dict[Path, tuple[str, np.ndarray]] = {}
    report: dict[str, Any] = {
        "schema_version": 2,
        "color_space": "Rec.709 Gamma 2.4 Full",
        "source": str(source),
        "source_sha256": source_digest,
        "manifest_sha256": manifest_digest,
        "topos_neutral": {"path": str(top_neutral), "sha256": _hash(top_neutral), "topos_export": telemetry},
        "cases": {},
    }
    for case in cases:
        if case.native_neutral not in native_neutrals:
            native_neutrals[case.native_neutral] = (_hash(case.native_neutral), _read_tiff16(case.native_neutral, size))
        neutral_digest, native_neutral = native_neutrals[case.native_neutral]
        native_pixels = _read_tiff16(case.native, size)
        native_digest = _hash(case.native)
        settings = json.loads(json.dumps(asdict(case.adjustment)))
        old = previous["cases"].get(case.case_id) if previous is not None else None
        if old is not None:
            # Calibration membership can change without changing the render inputs.
            previous_controls = {key: value for key, value in old["settings"].items() if key != "fit"}
            controls = {key: value for key, value in settings.items() if key != "fit"}
            if (previous_controls != controls or old["baselight"]["sha256"] != native_digest
                    or old["baselight_neutral"]["sha256"] != neutral_digest):
                raise ValueError(f"Previous report uses different controls or native TIFFs for {case.case_id}")
        candidate, telemetry = _export_topos(source, case.case_id, output / "cases" / case.case_id, size,
                                             case.adjustment)
        images = _read_tiff16(candidate, size), native_pixels, top_neutral_pixels, native_neutral
        samples = {name: _compare_region(region, images) for name, region in regions.items()}
        data = {
            "settings": settings,
            "baselight": {"path": str(case.native), "sha256": native_digest},
            "baselight_neutral": {"path": str(case.native_neutral), "sha256": neutral_digest},
            "topos": {"path": str(candidate), "sha256": _hash(candidate), "topos_export": telemetry},
            "regions": samples,
        }
        if old is not None:
            data["comparison_to_previous"] = {name: _previous_metrics(region, old["regions"][name])
                                              for name, region in samples.items()}
        report["cases"][case.case_id] = data
    report_path = output / "comparison.json"
    report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    chart_parser = subcommands.add_parser("chart")
    chart_parser.add_argument("path", type=Path)
    compare_parser = subcommands.add_parser("compare")
    compare_parser.add_argument("work", type=Path)
    compare_parser.add_argument("output", type=Path)
    batch = subcommands.add_parser("compare-manifest", help="Compare selected TIFF cases over the manifest ROIs")
    batch.add_argument("manifest", type=Path)
    batch.add_argument("cases", type=Path)
    batch.add_argument("output", type=Path)
    batch.add_argument("--case", action="append", default=[], help="Limit to a named case. Can be repeated.")
    batch.add_argument("--compare-to", type=Path, help="Compare errors to a previous report for the same TIFFs.")
    args = parser.parse_args()
    if args.command == "chart":
        write_anchors_chart(args.path)
    elif args.command == "compare":
        compare(args.work, args.output)
    else:
        print(compare_manifest(args.manifest, args.cases, args.output,
                               selected=set(args.case), compare_to=args.compare_to))


if __name__ == "__main__":
    main()
