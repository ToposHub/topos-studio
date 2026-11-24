"""Compare Topos TIFF delivery to a temporary Resolve RCM project."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
import time
from uuid import uuid4

import numpy as np
from PIL import Image

from tools import resolve_primary_calibration as chart


_COMBINED = {
    "rec709-g24": "Rec.709 Gamma 2.4",
    "rec709-bt709": "Rec.709 (Scene)",
    "srgb": "sRGB",
    "rec2020-g24": "Rec.2020 Gamma 2.4",
    "rec2020-bt2020": "Rec.2020 (Scene)",
}
_SEPARATE = {
    "display-p3": ("P3-D65", "sRGB"),
    "p3-d65-g24": ("P3-D65", "Gamma 2.4"),
    "dci-p3-g26": ("P3-DCI", "Gamma 2.6"),
}


def set_value(project: Any, key: str, expected: str) -> None:
    if project.GetSetting(key) != expected:
        chart._require(project.SetSetting(key, expected), f"set {key} to {expected}")
    actual = str(project.GetSetting(key))
    if actual != expected and (key, actual, expected) != ("timelineFrameRate", "24.0", "24"):
        raise RuntimeError(f"Resolve returned {key}={actual!r}, expected {expected!r}")


def render_reference(project: Any, timeline: Any, directory: Path) -> Path:
    directory.mkdir(parents=True)
    start = timeline.GetStartFrame()
    chart._require(project.SetRenderSettings({
        "SelectAllFrames": False, "MarkIn": start, "MarkOut": start + 1,
        "TargetDir": str(directory), "CustomName": directory.name,
        "ExportVideo": True, "ExportAudio": False, "DataLevel": "Full",
        "FormatWidth": chart.WIDTH, "FormatHeight": chart.HEIGHT,
    }), "configure the 16-bit TIFF render")
    job = chart._require(project.AddRenderJob(), "queue the calibration frame")
    try:
        chart._require(project.StartRendering([job]), "start the calibration render")
        deadline = time.monotonic() + 30
        while project.IsRenderingInProgress():
            if time.monotonic() >= deadline:
                project.StopRendering()
                raise TimeoutError(f"Resolve TIFF render timed out for {directory.name}")
            time.sleep(0.1)
        state = project.GetRenderJobStatus(job)
        if state.get("JobStatus") != "Complete":
            raise RuntimeError(f"Resolve TIFF render failed for {directory.name} ({state})")
        frames = sorted(directory.glob("*.tif"))
        if not frames:
            raise OSError(f"Resolve wrote no frames in {directory}")
        return frames[0]
    finally:
        if not project.IsRenderingInProgress():
            project.DeleteRenderJob(job)


def capture_resolve(
    source: Path, roundtrip: dict[str, Path] | None = None,
) -> tuple[str, dict[str, tuple[Path, dict[str, Any]]]]:
    resolve = chart._resolve()
    manager = resolve.GetProjectManager()
    original = chart._require(manager.GetCurrentProject(), "identify the current project").GetName()
    page = resolve.GetCurrentPage()
    chart._require(manager.SaveProject(), "save the current project")
    project = chart._require(manager.CreateProject(f"Topos RCM comparison {uuid4().hex[:8]}"),
                             "create the separate RCM test project")
    try:
        settings = {
            **chart.PROJECT_SETTINGS,
            "colorScienceMode": "davinciYRGBColorManaged",
            "isAutoColorManage": "0", "rcmPresetMode": "Custom",
            "colorSpaceInput": _COMBINED["rec709-g24"],
            "colorSpaceOutputToneMapping": "None", "colorSpaceOutputGamutMapping": "None",
        }
        for key, value in settings.items():
            set_value(project, key, value)
        pool = project.GetMediaPool()
        if roundtrip is not None:
            return resolve.GetVersionString(), capture_roundtrip(project, pool, source.parent, roundtrip)
        media = chart._require(pool.ImportMedia([str(source)]), "import the RGB chart")[0]
        chart._require(media.SetClipProperty("Data Level", "Full"), "interpret the chart as Full range")
        timeline = chart._require(pool.CreateTimelineFromClips("RGB chart", [media]), "create the timeline")
        chart._require(project.SetCurrentTimeline(timeline), "select the test timeline")
        chart._require(project.SetCurrentRenderMode(1), "select the render mode")
        chart._require(project.SetCurrentRenderFormatAndCodec("tif", "RGB16"), "select 16-bit RGB TIFF")
        references = {}
        for preset, name in _COMBINED.items():
            set_value(project, "colorSpaceOutput", name)
            frame = render_reference(project, timeline, source.parent / "resolve" / preset)
            references[preset] = (frame, project.GetSetting())
        set_value(project, "separateColorSpaceAndGamma", "1")
        for scope in ("Input", "Timeline"):
            set_value(project, f"colorSpace{scope}", "Rec.709")
            set_value(project, f"colorSpace{scope}Gamma", "Gamma 2.4")
        for preset, (gamut, gamma) in _SEPARATE.items():
            set_value(project, "colorSpaceOutput", gamut)
            set_value(project, "colorSpaceOutputGamma", gamma)
            frame = render_reference(project, timeline, source.parent / "resolve" / preset)
            references[preset] = (frame, project.GetSetting())
        return resolve.GetVersionString(), references
    finally:
        chart._require(manager.LoadProject(original), "restore the original project")
        if page:
            chart._require(resolve.OpenPage(page), "restore the original page")


def capture_roundtrip(
    project: Any, pool: Any, work: Path, sources: dict[str, Path],
) -> dict[str, tuple[Path, dict[str, Any]]]:
    references = {}
    for preset, source in sources.items():
        if preset in _COMBINED:
            set_value(project, "separateColorSpaceAndGamma", "0")
            for scope in ("Input", "Timeline"):
                set_value(project, f"colorSpace{scope}", _COMBINED[preset])
            set_value(project, "colorSpaceOutput", _COMBINED["rec709-g24"])
        else:
            set_value(project, "separateColorSpaceAndGamma", "1")
            gamut, gamma = _SEPARATE[preset]
            for scope in ("Input", "Timeline"):
                set_value(project, f"colorSpace{scope}", gamut)
                set_value(project, f"colorSpace{scope}Gamma", gamma)
            set_value(project, "colorSpaceOutput", "Rec.709")
            set_value(project, "colorSpaceOutputGamma", "Gamma 2.4")
        media = chart._require(pool.ImportMedia([str(source)]), f"reimport the {preset} TIFF")[0]
        chart._require(media.SetClipProperty("Data Level", "Full"), "interpret reimported TIFF as Full")
        if media.GetClipProperty("Input Color Space") != "Project":
            chart._require(media.SetClipProperty("Input Color Space", "Project"),
                           "apply the known profile's equivalent project input space")
        timeline = chart._require(pool.CreateTimelineFromClips(preset, [media]), "create reimport timeline")
        chart._require(project.SetCurrentTimeline(timeline), "select the reimport timeline")
        chart._require(project.SetCurrentRenderMode(1), "select the render mode")
        chart._require(project.SetCurrentRenderFormatAndCodec("tif", "RGB16"), "select 16-bit RGB TIFF")
        frame = render_reference(project, timeline, work / "resolve-roundtrip" / preset)
        references[preset] = (frame, project.GetSetting())
    return references


def export_topos(
    source: Path, preset: str, *, timeline_preset: str = "rec709-g24",
    directory: Path | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import (
        ExportCodec, TimelineExportConfig, create_timeline_exporter,
    )
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational

    asset = MediaAsset("chart", str(source), "image", "RGB chart", {})
    project = EditProject("comparison", "Color comparison", assets={asset.asset_id: asset})
    project = project.with_color_management({
        "color.timeline_space": timeline_preset, "color.output_space": preset,
    })
    clip = ClipInstance("chart", asset.asset_id, "v0", 0, 2, 0, 2)
    sequence = TimelineSequence(
        timeline_id="chart", name="RGB chart", width=chart.WIDTH, height=chart.HEIGHT,
        fps=Rational(24, 1), video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
    )
    directory = directory or source.parent / "topos" / preset
    directory.mkdir(parents=True)
    config = TimelineExportConfig(
        output_path=str(directory / preset), codec=ExportCodec.TIFF_SEQUENCE, bit_depth=16,
        timeline_id=sequence.timeline_id, include_audio=False, burn_subtitles=False,
    )
    outcome = create_timeline_exporter(project, sequence).export_timeline(config)
    if not outcome.success:
        raise RuntimeError(f"Topos TIFF render failed for {preset} ({outcome.cause})")
    return sorted(directory.glob("*.tif"))[0], outcome.stage_telemetry["stages"]


def measure(reference: np.ndarray, output: np.ndarray) -> dict[str, Any]:
    delta = np.abs(reference.astype(np.int32) - output.astype(np.int32))
    groups = {
        "all": delta,
        "holdout_rgb": delta[chart.GRID**3:chart.GRID**3 + chart.HOLDOUT],
        "gray": delta[-chart.GRAY:],
    }
    return {name: {
        "samples": len(values),
        "rmse_16bit": float(np.sqrt(np.mean(values.astype(np.float64)**2))),
        "p95_max_channel_16bit": float(np.quantile(values.max(axis=-1), 0.95)),
        "max_16bit": int(values.max()),
    } for name, values in groups.items()}


def compare(work: Path) -> None:
    from src.shared.color.color_space_descriptor import descriptor_for_space_preset
    from src.shared.color.color_transform_provider import BuiltinColorProvider
    from src.shared.color.video_delivery_profiles import SDR_VIDEO_OUTPUTS, WIDE_GAMUT_TIFF_OUTPUTS

    work.mkdir(parents=True, exist_ok=True)
    source = work / chart.CHART_NAME
    chart.write_chart(source)
    version, references = capture_resolve(source)
    report = {"resolve_version": version, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "input_data_level": "Full", "cases": {}}
    profiled_outputs: dict[str, Path] = {}
    for preset, (frame, settings) in references.items():
        expected = chart.read_patches(frame, flat_tolerance=4)
        case = {"resolve": str(frame.relative_to(work)), "resolve_settings": settings}
        if preset in (*SDR_VIDEO_OUTPUTS, *WIDE_GAMUT_TIFF_OUTPUTS):
            output, stages = export_topos(source, preset)
            actual = chart.read_patches(output, flat_tolerance=4)
            with Image.open(output) as image:
                profile = image.info.get("icc_profile")
                case["bits_per_sample"] = image.tag_v2.get(258)
                case["topos_icc_sha256"] = hashlib.sha256(profile).hexdigest() if profile else None
                if profile:
                    profiled_outputs[preset] = output
            case.update(topos=str(output.relative_to(work)), topos_stages=stages)
        else:
            provider = BuiltinColorProvider()
            converted = provider.transform_signal(
                chart.chart_codes().astype(np.float32) / chart.MAX_CODE,
                descriptor_for_space_preset("rec709-g24"), descriptor_for_space_preset(preset),
            )
            actual = np.rint(np.clip(converted, 0.0, 1.0) * chart.MAX_CODE).astype(np.uint16)
            case["topos"] = "CPU reference signal"
        case["difference"] = measure(expected, actual)
        report["cases"][preset] = case
    if profiled_outputs:
        _, reimported = capture_resolve(source, roundtrip=profiled_outputs)
        report["roundtrip"] = {}
        for preset, (frame, settings) in reimported.items():
            output, stages = export_topos(
                profiled_outputs[preset], "rec709-g24", timeline_preset=preset,
                directory=work / "topos-roundtrip" / preset,
            )
            reference = chart.read_patches(frame, flat_tolerance=4)
            actual = chart.read_patches(output, flat_tolerance=4)
            report["roundtrip"][preset] = {
                "resolve": str(frame.relative_to(work)), "resolve_settings": settings,
                "topos": str(output.relative_to(work)), "topos_stages": stages,
                "difference": measure(reference, actual),
                "difference_from_original": measure(chart.chart_codes(), actual),
            }
    (work / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=lambda value: Path(value).resolve())
    compare(parser.parse_args().work)
