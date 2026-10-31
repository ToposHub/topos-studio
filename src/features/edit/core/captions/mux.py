"""Safe ffmpeg command construction for caption sidecar embedding."""

from __future__ import annotations

from pathlib import Path


def build_caption_mux_command(
    video_path: str | Path,
    subtitle_path: str | Path,
    output_path: str | Path,
    *,
    container: str = "mov",
    language: str = "und",
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build a non-shell ffmpeg command for MOV/MKV caption embedding.

    The caller owns process execution and progress reporting. Keeping this
    function argv-only prevents paths or caption text from becoming shell code.
    ``mov_text`` is the widely interoperable MOV/MP4 subtitle stream; MKV
    (matroska muxer) carries the srt text stream natively.

    AUD-15909: ``container="mxf"`` 显式拒绝——ffmpeg 的 mxf muxer 不支持
    srt/mov_text 文本字幕流（其字幕支持是 eia_608 隐闭 captions，需要
    专用 caption encoder，无法从文本 sidecar 直转）。旧实现放行 mxf 并
    产出 ``-c:s srt -f mxf`` 的必败命令，交付在写头阶段才失败。
    """
    normalized = str(container).lower().lstrip(".")
    if normalized == "mxf":
        # AUD-15909: 在构造期显式拒绝，而不是产出运行期必败的命令。
        raise ValueError(
            "MXF does not support text subtitle streams (ffmpeg mxf muxer "
            "only accepts eia_608 closed captions); use mov/mp4/mkv instead"
        )
    if normalized not in {"mov", "mp4", "mkv"}:
        raise ValueError(f"Unsupported caption container: {container!r}")
    video = Path(video_path)
    subtitles = Path(subtitle_path)
    output = Path(output_path)
    if not str(language).strip():
        raise ValueError("language cannot be empty")
    codec = "mov_text" if normalized in {"mov", "mp4"} else "srt"
    # AUD-15908: mkv 是容器俗称而非 muxer 名——ffmpeg 没有名为 mkv 的
    # muxer（`-f mkv` 报 "Requested output format 'mkv' is not known"，
    # 实测退出码 234）；映射到真实 muxer 名 matroska。
    muxer = "matroska" if normalized == "mkv" else normalized
    return [
        str(ffmpeg_bin), "-y", "-i", str(video), "-i", str(subtitles),
        "-map", "0:v?", "-map", "0:a?", "-map", "1:0",
        "-c:v", "copy", "-c:a", "copy", "-c:s", codec,
        "-metadata:s:s:0", f"language={str(language).strip()}",
        "-f", muxer, str(output),
    ]


__all__ = ["build_caption_mux_command"]
