"""Project HDR input interpretation and BT.2100 reference conditions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, ColorSpaceDescriptor, descriptor_for_space_preset,
    project_output_space, project_timeline_space,
)
from src.shared.color.color_transform_provider import VideoSignalLuminance
from src.shared.color.types import ColorPrimaries, ColorRange, ColorTransfer, is_hdr_transfer


HDR_INPUT_KEY = "color.hdr.input"
HDR_WHITE_KEY = "color.hdr.reference_white"
HDR_HLG_PEAK_KEY = "color.hdr.hlg_peak"
HDR_MONITOR_KEY = "color.hdr.monitor"
HDR_MASTER_PEAK_KEY = "color.hdr.master_peak"
HDR_MASTER_GAMUT_KEY = "color.hdr.master_gamut"
HDR_OUTPUT_SPACES = ("rec2100-pq", "rec2100-hlg")
HDR_WORKING_SPACES = ("davinci-wide-gamut-intermediate", *HDR_OUTPUT_SPACES)
HDR_INPUT_SPACES = ("auto", "timeline", *HDR_WORKING_SPACES, "rec709-g24", "rec709-bt709",
                    "srgb", "linear-rec709")


@dataclass(frozen=True)
class ProjectHdrSettings:
    input: str
    luminance: VideoSignalLuminance
    monitor: str
    master_peak_nits: int
    master_gamut: str


def project_hdr_settings(settings: Mapping[str, Any] | None) -> ProjectHdrSettings | None:
    """Read the HDR recipe only for timelines using the new project presets."""
    if not settings or (project_timeline_space(settings) not in HDR_WORKING_SPACES
                        and project_output_space(settings) not in HDR_OUTPUT_SPACES):
        return None
    input_space = settings.get(HDR_INPUT_KEY, "auto")
    monitor = settings.get(HDR_MONITOR_KEY, "signal")
    if input_space not in HDR_INPUT_SPACES or monitor not in ("signal", "rec709"):
        raise ValueError("Invalid project HDR input or monitoring mode")
    try:
        white = float(settings.get(HDR_WHITE_KEY, 100))
        peak = float(settings.get(HDR_HLG_PEAK_KEY, 1000))
        master_peak = int(settings.get(HDR_MASTER_PEAK_KEY, 1000))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid HDR reference luminance") from error
    if not (80.0 <= white <= 400.0 and 300.0 <= peak <= 10000.0):
        raise ValueError("HDR reference white must be 80–400 nit and the HLG peak 300–10000 nit")
    gamut = settings.get(HDR_MASTER_GAMUT_KEY, "bt2020")
    if not 100 <= master_peak <= 10000 or gamut not in ("bt2020", "p3-d65"):
        raise ValueError("Invalid HDR10 mastering display peak or gamut")
    return ProjectHdrSettings(
        input_space, VideoSignalLuminance(white, peak, 1.2 + 0.42 * math.log10(peak / 1000.0)),
        monitor, master_peak, gamut,
    )


def hdr_delivery_is_supported(codec: str, container: str, pixel_format: str) -> bool:
    """Qualified BT.2020 YCbCr encoder routes for HDR project output."""
    if codec == "hevc":
        return container in ("mov", "mp4") and pixel_format == "yuv420p10le"
    return (codec in ("prores_422", "prores_422hq") and container == "mov"
            and pixel_format in ("", "yuv422p10le"))


def hdr_monitor_space(
    output: ColorSpaceDescriptor, hdr: ProjectHdrSettings | None,
) -> ColorSpaceDescriptor:
    """Select the software view independently of the delivery transform."""
    if hdr is not None and hdr.monitor == "rec709" and is_hdr_transfer(output.transfer):
        return descriptor_for_space_preset("rec709-g24")
    return output


def hdr_input_descriptor(
    hdr: ProjectHdrSettings, timeline: ColorSpaceDescriptor, *, media_type: str,
    metadata: Mapping[str, Any] | None = None, path: str = "",
) -> ColorSpaceDescriptor:
    """Interpret straight RGB after decoder matrix and range expansion."""
    if hdr.input == "timeline":
        return timeline
    if hdr.input == "linear-rec709":
        return LINEAR_SCENE
    if hdr.input != "auto":
        return descriptor_for_space_preset(hdr.input)
    if Path(path).suffix.lower() == ".exr":
        from src.shared.color.project_video_signal import ProjectAcesSettings, aces_input_descriptor
        from src.shared.io.openexr_io import read_exr_header

        header = read_exr_header(path)
        if header.get("chromaticities") is None:
            return LINEAR_SCENE
        return aces_input_descriptor(
            ProjectAcesSettings("1.3", "rec709-g24", "auto", False),
            path, media_type="image", exr_header=header,
        )
    from src.shared.media.color_metadata import normalize_color_transfer

    metadata = metadata or {}
    transfer_tag = normalize_color_transfer(metadata.get("color_transfer"))
    primaries_tag = str(metadata.get("color_primaries") or "")
    if media_type == "video" and path and transfer_tag in (None, "unknown"):
        # Old imports may predate the stream color tags in Media Pool metadata.
        # Only render/export workers supply a video path to this fallback.
        from src.shared.media.factory import MediaSourceFactory

        observed = MediaSourceFactory.probe_video_metadata(path)
        if observed is not None:
            transfer_tag = normalize_color_transfer(observed.color_transfer)
            primaries_tag = observed.color_primaries or primaries_tag
    if transfer_tag in ("smpte2084", "arib-std-b67"):
        hlg = transfer_tag == "arib-std-b67"
        primaries = (ColorPrimaries.P3_DISPLAY if not hlg and primaries_tag == "smpte432"
                     else ColorPrimaries.BT2020)
        return ColorSpaceDescriptor(
            primaries=primaries, transfer=ColorTransfer.HLG if hlg else ColorTransfer.PQ,
            range=ColorRange.FULL,
        )
    if media_type in ("image", "image_sequence"):
        return descriptor_for_space_preset("srgb")
    if primaries_tag == "bt2020":
        return descriptor_for_space_preset("rec2020-bt2020")
    return descriptor_for_space_preset("rec709-g24")
