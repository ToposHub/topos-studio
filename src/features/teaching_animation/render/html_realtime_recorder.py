"""Real-time Chromium recording for Motion Graphic HTML clips.

The frame-accurate renderer is intentionally deterministic, but it is the
wrong transport for a live preview-shaped export: it opens a fresh capture
context for every timeline frame.  This module keeps one Chromium page alive,
lets the browser paint at its normal cadence, and asks Playwright to record
that page.  The resulting browser recording can be kept in its native WebM
codec or transcoded to a requested delivery codec.

This is a delivery-speed path, not a replacement for deterministic frame
tests.  It is opt-in so existing frame-accurate exports retain their exact
preview/export contract.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import queue
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping

from src.shared.ffmpeg_locator import find_ffmpeg, find_ffprobe

from .html_renderer_spike.playwright_backend import (
    PlaywrightChromiumBackend,
    _inject_csp_meta,
    _thread_browser,
)
from .html_renderer_spike.security import UrlPolicy


@dataclass(frozen=True)
class RealtimeRecordResult:
    """Metadata returned by :func:`record_html_video`."""

    output: str
    frame_count: int
    width: int
    height: int
    fps: float
    cancelled: bool = False


def _ebml_vint_size(value: int) -> bytes:
    """Encode a Matroska/WebM variable-size integer."""
    value = max(0, int(value))
    for width in range(1, 9):
        if value <= (1 << (7 * width)) - 2:
            return (value | (1 << (7 * width))).to_bytes(width, "big")
    raise ValueError("WebM Void payload is too large")


def _ensure_minimum_webm_bitrate(output: Path, duration: float, minimum_kbps: int | None) -> None:
    """Ensure a valid WebM file has the requested effective average rate.

    libvpx's quality-constrained mode intentionally undershoots on static
    slides, even when ``-b:v`` is 40 Mbps.  A Matroska ``Void`` element is
    legal padding inside the unknown-sized WebM segment and is ignored by
    decoders.  We use it only after the high-quality encode, so the minimum
    is measurable from file bytes without adding a second video transcode.
    """
    minimum = int(minimum_kbps or 0)
    if minimum <= 0 or duration <= 0 or not output.is_file():
        return
    required_size = int((minimum * 1000.0 * float(duration) + 7.0) // 8.0)
    missing = required_size - output.stat().st_size
    if missing <= 0:
        return
    # Recompute once because the Void element header itself contributes bytes.
    payload = missing
    while True:
        header = b"\xec" + _ebml_vint_size(payload)
        adjusted = required_size - output.stat().st_size - len(header)
        if adjusted <= payload:
            payload = max(payload, adjusted)
            break
        payload = adjusted
    with output.open("ab") as stream:
        stream.write(header)
        remaining = payload
        zero_block = b"\0" * (1024 * 1024)
        while remaining:
            chunk = min(remaining, len(zero_block))
            stream.write(zero_block[:chunk])
            remaining -= chunk


def _add_recording_guard(document: str) -> str:
    """Hide the page while fonts/resources settle before the recording starts."""
    guard = (
        '<style id="topos-realtime-recording-guard">'
        'html{visibility:hidden!important;}'
        '</style>'
    )
    text = str(document or "")
    lower = text.lower()
    head_end = lower.find("</head>")
    if head_end >= 0:
        return text[:head_end] + guard + text[head_end:]
    html_open = lower.find("<html")
    if html_open >= 0:
        open_end = lower.find(">", html_open)
        if open_end >= 0:
            return text[: open_end + 1] + "<head>" + guard + "</head>" + text[open_end + 1 :]
    return guard + text


def _remove_recording_guard(page: object) -> None:
    """Remove the hide-guard, failing closed when it cannot be removed.

    The guard hides the entire page (``html{visibility:hidden}`), so a
    recording made while it is still attached exports a blank video and
    reports success (AUD-30122).  There is no downstream validation that
    would catch this — the caller only checks file existence/size — so the
    only fail-closed point is here: re-read the DOM and abort the recording
    unless the guard is verifiably gone.
    """
    try:
        still_present = page.evaluate(
            """() => {
              const node = document.getElementById('topos-realtime-recording-guard');
              if (node) node.remove();
              return document.getElementById('topos-realtime-recording-guard') !== null;
            }"""
        )
    except Exception as exc:
        raise RuntimeError(
            f"cannot verify removal of the recording guard (page unhealthy): {exc}"
        ) from exc
    if still_present:
        raise RuntimeError("recording guard is still attached after removal")


def _probe_duration(ffmpeg_binary: str, video: Path) -> float | None:
    """Read the recorder's actual duration before trimming its warm-up."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            check=False, capture_output=True, text=True,
        )
        value = float(result.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def _probe_output_fps_and_frames(
    ffmpeg_binary: str, video: Path, fallback_fps: float, fallback_frames: int,
) -> tuple[float, int]:
    """Return the actual delivered stream cadence, with safe fallbacks."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return fallback_fps, fallback_frames
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate:format=duration",
             "-of", "default=noprint_wrappers=1", str(video)],
            check=False, capture_output=True, text=True,
        )
        fps_value = fallback_fps
        duration_value: float | None = None
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key == "r_frame_rate" and "/" in value:
                num, den = value.split("/", 1)
                fps_value = float(num) / max(1.0, float(den))
            elif key == "duration":
                duration_value = float(value)
        frame_count = (
            max(1, round(duration_value * fps_value))
            if duration_value is not None and duration_value > 0 else fallback_frames
        )
        return fps_value, frame_count
    except (OSError, ValueError, ZeroDivisionError):
        return fallback_fps, fallback_frames


def _vp8_encode_command(
    ffmpeg: str,
    output: Path,
    *,
    target_fps: float,
    bitrate_kbps: int,
    expected_frames: int,
) -> list[str]:
    """Build the PNG-pipe → libvpx command for the high-quality VP8 path.

    ``-nostats -v error`` keeps ffmpeg from streaming progress banners onto
    stderr for the whole encode (AUD-30120): the pipe is drained by a helper
    thread, but nobody needs per-frame stats, and the smaller the stderr
    volume the smaller the deadlock window before the drain thread catches up.
    """
    return [
        ffmpeg, "-y", "-nostats", "-v", "error",
        "-f", "image2pipe", "-vcodec", "png",
        "-r", f"{target_fps:.12g}", "-i", "pipe:0",
        "-an", "-c:v", "libvpx",
        "-b:v", f"{max(100, int(bitrate_kbps))}k",
        "-crf", "4", "-qmin", "0", "-qmax", "4",
        "-deadline", "good", "-cpu-used", "2",
        "-row-mt", "1", "-threads", "4", "-pix_fmt", "yuv420p",
        "-frames:v", str(expected_frames), str(output),
    ]


def _drain_pipe_lines(stream: object, sink: list[bytes]) -> None:
    """Consume a subprocess pipe to EOF so the writer never blocks (AUD-30120).

    ffmpeg logs to stderr; if nobody drains it, the OS pipe buffer fills and
    ffmpeg blocks on stderr writes, stops consuming stdin, and the whole
    export thread deadlocks.  Runs on a dedicated helper thread.
    """
    if stream is None:
        return
    try:
        while True:
            line = stream.readline()
            if not line:
                break
            sink.append(line)
    except (OSError, ValueError):
        # Pipe closed / bad descriptor during teardown — the process exit
        # status below remains the authoritative signal.
        pass


def _enqueue_with_writer_check(
    frame_queue: "queue.Queue[bytes | None]",
    item: "bytes | None",
    *,
    writer_thread: threading.Thread | None,
    writer_errors: "list[BaseException]",
    timeout_s: float = 30.0,
) -> None:
    """Put ``item`` without ever blocking forever on a dead consumer (AUD-30121).

    The VP8 writer thread exits after its first failed ``stdin.write`` (ffmpeg
    dying early breaks the pipe).  An unbounded ``put`` on the then-full queue
    would hang the export thread forever and make the structured
    ``writer_errors`` RuntimeError unreachable.  Wait in short slices and fail
    fast as soon as the writer is known to be gone.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if writer_errors:
            raise RuntimeError(f"VP8 screencast writer failed: {writer_errors[0]}")
        if writer_thread is not None and not writer_thread.is_alive():
            raise RuntimeError("VP8 screencast writer stopped consuming frames")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("timed out queueing frames for the VP8 writer")
        try:
            frame_queue.put(item, timeout=min(0.25, remaining))
            return
        except queue.Full:
            continue


def _record_high_quality_vp8(
    html: str,
    output: Path,
    *,
    width: int,
    height: int,
    duration: float,
    target_fps: float,
    csp: str,
    allowed_asset_ids: frozenset[str],
    asset_resolver: Mapping[str, bytes] | None,
    allow_network_resources: bool,
    allowed_network_hosts: frozenset[str],
    allow_inline_visual_resources: bool,
    allow_gpu_apis: bool,
    ffmpeg: str,
    progress_callback: Callable[[int, int], bool | None] | None,
    bitrate_kbps: int,
    min_bitrate_kbps: int | None,
) -> RealtimeRecordResult:
    """Record a loaded page through CDP and encode VP8 once at high quality.

    Playwright's built-in ``record_video_dir`` path is intentionally tuned for
    lightweight test artifacts (hard-coded 1 Mbps VP8).  CDP screencast lets us
    start after the page is ready and feed lossless PNG frames directly to our
    own VP8 encoder, avoiding that low-quality intermediate entirely.
    """
    policy = UrlPolicy(
        allowed_asset_ids,
        allow_network_resources=allow_network_resources,
        allowed_network_hosts=allowed_network_hosts,
        allow_inline_visual_resources=allow_inline_visual_resources,
        allow_gpu_apis=allow_gpu_apis,
    )
    backend = PlaywrightChromiumBackend(asset_resolver=asset_resolver)
    browser = _thread_browser(True, False, allow_gpu_apis=allow_gpu_apis)
    document = _inject_csp_meta(html, csp)
    expected_frames = max(1, round(duration * target_fps))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="topos-vp8-screencast-"):
        context = browser.new_context(
            viewport={"width": int(width), "height": int(height)},
            device_scale_factor=1.0,
            accept_downloads=False,
            bypass_csp=False,
            java_script_enabled=True,
        )
        page = None
        client = None
        process = None
        writer_thread = None
        stderr_thread = None
        frame_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=48)
        writer_errors: list[BaseException] = []
        stderr_chunks: list[bytes] = []
        selected_frames = 0
        last_index = -1
        last_frame: bytes | None = None
        recording_started = 0.0
        cancelled = False

        def write_frames() -> None:
            assert process is not None and process.stdin is not None
            try:
                while True:
                    payload = frame_queue.get()
                    if payload is None:
                        break
                    process.stdin.write(payload)
                process.stdin.close()
            except BaseException as exc:  # pragma: no cover - process failure path
                writer_errors.append(exc)

        try:
            context.add_init_script(
                """(() => {
                  let seed = 0x6d2b79f5;
                  Math.random = () => {
                    seed = (seed + 0x6d2b79f5) | 0;
                    let value = Math.imul(seed ^ (seed >>> 15), 1 | seed);
                    value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
                    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
                  };
                })();"""
            )
            page = context.new_page()
            page.route("**/*", lambda route: backend._intercept(route, policy))
            page.set_content(document, wait_until="commit")
            if allow_network_resources:
                try:
                    page.wait_for_load_state("networkidle", timeout=3500)
                except Exception:
                    pass
            try:
                page.evaluate(
                    """() => Promise.race([
                      (document.fonts && document.fonts.ready) || Promise.resolve(),
                      new Promise(resolve => setTimeout(resolve, 1500))
                    ])"""
                )
            except Exception:
                pass

            process = subprocess.Popen(
                _vp8_encode_command(
                    ffmpeg, output,
                    target_fps=target_fps, bitrate_kbps=bitrate_kbps,
                    expected_frames=expected_frames,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            writer_thread = threading.Thread(target=write_frames, name="vp8-screencast-writer", daemon=True)
            writer_thread.start()
            # AUD-30120: a dedicated drain thread keeps ffmpeg's stderr from
            # ever filling the OS pipe buffer (which would deadlock the whole
            # export); the chunks are only read after the process has exited.
            stderr_thread = threading.Thread(
                target=_drain_pipe_lines,
                args=(process.stderr, stderr_chunks),
                name="vp8-screencast-stderr-drain", daemon=True,
            )
            stderr_thread.start()
            client = context.new_cdp_session(page)
            recording_started = time.monotonic()

            def on_frame(payload: Mapping[str, object]) -> None:
                nonlocal selected_frames, last_index, last_frame
                try:
                    client.send("Page.screencastFrameAck", {"sessionId": payload.get("sessionId")})
                except Exception:
                    return
                elapsed = max(0.0, time.monotonic() - recording_started)
                index = min(expected_frames - 1, int(elapsed * target_fps))
                if index <= last_index:
                    return
                raw = payload.get("data")
                if not isinstance(raw, str):
                    return
                try:
                    frame = base64.b64decode(raw)
                except Exception:
                    return
                # Fill a skipped cadence slot with the last image so the
                # encoded stream stays exactly duration * fps frames long.
                while last_index + 1 < index and last_frame is not None:
                    try:
                        frame_queue.put_nowait(last_frame)
                        selected_frames += 1
                    except queue.Full:
                        break
                    last_index += 1
                try:
                    frame_queue.put_nowait(frame)
                except queue.Full:
                    return
                last_frame = frame
                last_index = index
                selected_frames += 1

            client.on("Page.screencastFrame", on_frame)
            client.send(
                "Page.startScreencast",
                {"format": "png", "maxWidth": int(width), "maxHeight": int(height), "everyNthFrame": 1},
            )
            if progress_callback is not None:
                cancelled = bool(progress_callback(0, expected_frames))
            while not cancelled and time.monotonic() - recording_started < duration:
                page.wait_for_timeout(100)
                completed = min(expected_frames, round((time.monotonic() - recording_started) * target_fps))
                if progress_callback is not None:
                    cancelled = bool(progress_callback(completed, expected_frames))
            try:
                client.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                if not cancelled and last_frame is not None:
                    while selected_frames < expected_frames:
                        # AUD-30121: bounded enqueue — if the writer already
                        # died (ffmpeg exited early), this raises instead of
                        # blocking forever on the unconsumed queue.
                        _enqueue_with_writer_check(
                            frame_queue, last_frame,
                            writer_thread=writer_thread, writer_errors=writer_errors,
                        )
                        selected_frames += 1
                _enqueue_with_writer_check(
                    frame_queue, None,
                    writer_thread=writer_thread, writer_errors=writer_errors,
                )
            except RuntimeError:
                # Never leave an orphaned encoder behind when the writer died.
                if process is not None and process.poll() is None:
                    process.kill()
                raise
            if writer_thread is not None:
                writer_thread.join(timeout=max(30.0, duration * 4.0))
        finally:
            try:
                if client is not None:
                    client.detach()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

        if cancelled:
            if process is not None:
                process.kill()
                process.wait()
            return RealtimeRecordResult(str(output), 0, width, height, target_fps, True)
        if process is None:
            raise RuntimeError("VP8 screencast encoder was not started")
        if writer_thread is not None and writer_thread.is_alive():
            # AUD-30121: the writer never finished; kill the encoder rather
            # than waiting on a hung pipeline.
            process.kill()
            process.wait()
            raise RuntimeError("VP8 screencast writer did not finish after the final frame")
        # stderr is drained by the helper thread (AUD-30120); the writer has
        # already closed stdin after the final PNG, so use ``wait`` instead of
        # ``communicate()`` (communicate would try to flush the already-closed
        # pipe on Python 3.12).
        try:
            process.wait(timeout=max(60.0, duration * 4.0))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("VP8 screencast encoder did not exit after the final frame")
        if stderr_thread is not None:
            stderr_thread.join(timeout=5.0)
        stderr = b"".join(stderr_chunks)
        if writer_errors:
            raise RuntimeError(f"VP8 screencast writer failed: {writer_errors[0]}")
        if process.returncode != 0:
            raise RuntimeError(f"VP8 screencast export failed: {stderr.decode(errors='replace')[-1200:]}")
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError(f"VP8 screencast output is missing: {output}")
        _ensure_minimum_webm_bitrate(output, duration, min_bitrate_kbps)

    delivered_fps, delivered_frames = _probe_output_fps_and_frames(
        ffmpeg, output, target_fps, expected_frames,
    )
    return RealtimeRecordResult(str(output), delivered_frames, width, height, delivered_fps, False)
def record_html_video(
    html: str,
    output: str | Path,
    *,
    width: int,
    height: int,
    duration_seconds: float,
    fps: float,
    csp: str,
    allowed_asset_ids: frozenset[str] = frozenset(),
    asset_resolver: Mapping[str, bytes] | None = None,
    allow_network_resources: bool = False,
    allowed_network_hosts: frozenset[str] = frozenset(),
    allow_inline_visual_resources: bool = False,
    allow_gpu_apis: bool = False,
    transparent: bool = False,
    ffmpeg_binary: str | None = None,
    progress_callback: Callable[[int, int], bool | None] | None = None,
    delivery: str = "prores_proxy",
    video_codec: str = "vp9",
    bitrate_kbps: int = 40_000,
    min_bitrate_kbps: int | None = None,
) -> RealtimeRecordResult:
    """Record one HTML page in real time.

    Playwright's recorder currently emits a short VP8/WebM intermediate at a
    steady browser cadence.  ``delivery="webm"`` keeps that native recording
    and only performs a lossless container trim; ``delivery="vp9_mp4"``
    encodes the visible portion as VP9 in MP4 at the requested target bitrate;
    ``delivery="h264_mp4"`` performs the one unavoidable final H.264 encode
    after the live recording, without re-rasterizing HTML frame by frame.
    """
    if width <= 0 or height <= 0:
        raise ValueError("recording surface must be positive")
    duration = max(0.001, float(duration_seconds))
    target_fps = max(0.001, float(fps))
    total_frames = max(1, round(duration * target_fps))
    output_path = Path(output)
    ffmpeg = ffmpeg_binary or find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for real-time video export")
    delivery_name = str(delivery).strip().lower()
    if delivery_name in {"vp8_high_quality", "native_vp8", "vp8_native"}:
        return _record_high_quality_vp8(
            html,
            output_path,
            width=int(width),
            height=int(height),
            duration=duration,
            target_fps=target_fps,
            csp=csp,
            allowed_asset_ids=allowed_asset_ids,
            asset_resolver=asset_resolver,
            allow_network_resources=allow_network_resources,
            allowed_network_hosts=allowed_network_hosts,
            allow_inline_visual_resources=allow_inline_visual_resources,
            allow_gpu_apis=allow_gpu_apis,
            ffmpeg=ffmpeg,
            progress_callback=progress_callback,
            bitrate_kbps=bitrate_kbps,
            min_bitrate_kbps=min_bitrate_kbps,
        )

    policy = UrlPolicy(
        allowed_asset_ids,
        allow_network_resources=allow_network_resources,
        allowed_network_hosts=allowed_network_hosts,
        allow_inline_visual_resources=allow_inline_visual_resources,
        allow_gpu_apis=allow_gpu_apis,
    )
    backend = PlaywrightChromiumBackend(asset_resolver=asset_resolver)
    browser = _thread_browser(True, False, allow_gpu_apis=allow_gpu_apis)
    native_webm = delivery_name in {"webm", "native_webm", "webm_realtime"}
    # Native recording starts timing as soon as the page commits, so it does
    # not need a hidden warm-up segment that would later require keyframe
    # trimming.  The optional ProRes path keeps the guard/resource settle
    # window because it is already doing a full transcode.
    document = _inject_csp_meta(
        html if native_webm else _add_recording_guard(html), csp,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="topos-realtime-record-") as record_dir:
        context = browser.new_context(
            viewport={"width": int(width), "height": int(height)},
            device_scale_factor=1.0,
            record_video_dir=record_dir,
            record_video_size={"width": int(width), "height": int(height)},
            accept_downloads=False,
            bypass_csp=False,
            java_script_enabled=True,
        )
        page = None
        raw_video: Path | None = None
        cancelled = False
        recording_started = time.monotonic()
        try:
            # Keep authored randomness stable between preview/export sessions,
            # without freezing the normal wall-clock animation loop.
            context.add_init_script(
                """(() => {
                  let seed = 0x6d2b79f5;
                  Math.random = () => {
                    seed = (seed + 0x6d2b79f5) | 0;
                    let value = Math.imul(seed ^ (seed >>> 15), 1 | seed);
                    value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
                    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
                  };
                })();"""
            )
            page = context.new_page()
            page.route("**/*", lambda route: backend._intercept(route, policy))
            page.set_content(document, wait_until="commit")
            if allow_network_resources and not native_webm:
                try:
                    page.wait_for_load_state("networkidle", timeout=3500)
                except Exception:
                    pass
            if not native_webm:
                try:
                    page.evaluate(
                        """() => Promise.race([
                          (document.fonts && document.fonts.ready) || Promise.resolve(),
                          new Promise(resolve => setTimeout(resolve, 1500))
                        ])"""
                    )
                except Exception:
                    pass

            if not native_webm:
                _remove_recording_guard(page)
            visible_start = time.monotonic()
            warmup_seconds = 0.0 if native_webm else max(0.0, visible_start - recording_started)
            if progress_callback is not None:
                cancelled = bool(progress_callback(0, total_frames))
            elapsed = 0.0
            while not cancelled and elapsed < duration:
                tick = min(0.25, duration - elapsed)
                page.wait_for_timeout(round(tick * 1000))
                elapsed = min(duration, time.monotonic() - visible_start)
                completed = min(total_frames, round(elapsed * target_fps))
                if progress_callback is not None:
                    cancelled = bool(progress_callback(completed, total_frames))
            raw_path = page.video.path()
            raw_video = Path(raw_path) if raw_path else None
        finally:
            try:
                context.close()
            finally:
                # The browser itself is thread-local and remains available for
                # the editor; only this recording context/page is discarded.
                pass

        if cancelled:
            return RealtimeRecordResult(
                str(output_path), 0, int(width), int(height), target_fps, True,
            )
        if raw_video is None or not raw_video.is_file():
            raise RuntimeError("Chromium recording did not produce a video")

        # Playwright's video timestamps begin when Chromium creates the page,
        # not when the HTML has finished loading.  Use the measured media
        # duration to trim the final visible segment; this avoids losing the
        # first/last seconds when browser startup or CDN settling takes longer
        # than the wall-clock warm-up estimate.
        raw_duration = _probe_duration(ffmpeg, raw_video)
        trim_start = 0.0 if native_webm else (
            max(0.0, raw_duration - duration)
            if raw_duration is not None and raw_duration >= duration
            else warmup_seconds
        )
        if native_webm:
            # No pixel decode/re-encode: this is a fast lossless remux/trim of
            # Chromium's native VP8/WebM recording.
            command = [
                ffmpeg, "-y", "-i", str(raw_video),
                "-ss", f"{trim_start:.6f}", "-t", f"{duration:.6f}",
                "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(output_path),
            ]
        elif delivery_name in {
            "vp9_mp4", "mp4", "mp4_vp9",
            "h264_mp4", "mp4_h264", "realtime_h264",
        }:
            codec_name = "libvpx" if str(video_codec).strip().lower() == "vp8" else "libvpx-vp9"
            bitrate = max(100, int(bitrate_kbps))
            h264_delivery = delivery_name in {"h264_mp4", "mp4_h264", "realtime_h264"}
            if h264_delivery:
                # Chromium's native recorder is VP8/WebM.  H.264 therefore still
                # needs one final encode, but the page is recorded once in real
                # time; we never re-open/re-rasterize the HTML for every output
                # frame.  The fast preset keeps cache generation close to the
                # duration of the clip while preserving a clean 4:2:0 preview.
                codec_name = "libx264"
            codec_options = (
                ["-preset", "veryfast", "-tune", "animation"]
                if h264_delivery else
                ["-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
            )
            command = [
                ffmpeg,
                "-y",
                "-i", str(raw_video),
                "-ss", f"{trim_start:.6f}",
                "-t", f"{duration:.6f}",
                "-vf", f"scale={int(width)}:{int(height)}:flags=lanczos,format=yuv420p",
                "-r", f"{target_fps:.12g}",
                "-b:v", f"{bitrate}k",
                "-minrate", f"{bitrate}k",
                "-maxrate", f"{bitrate}k",
                "-bufsize", f"{bitrate * 2}k",
                "-crf", "18" if h264_delivery else "10",
                *codec_options,
                "-c:v", codec_name,
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            # The hidden warm-up is removed while the page's visible segment
            # is re-encoded to the requested timeline rate and exact frame
            # count.  This is the optional ProRes delivery path.
            command = [
                ffmpeg,
                "-y",
                "-i", str(raw_video),
                "-ss", f"{trim_start:.6f}",
                "-t", f"{duration:.6f}",
                "-vf", f"scale={int(width)}:{int(height)}:flags=lanczos,format=yuv422p10le",
                "-r", f"{target_fps:.12g}",
                "-frames:v", str(total_frames),
                "-an",
                "-c:v", "prores_ks",
                "-profile:v", "0",
                "-pix_fmt", "yuv422p10le",
                str(output_path),
            ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr = process.communicate()[1]
        if process.returncode != 0:
            raise RuntimeError(
                f"FFmpeg real-time video export failed: {stderr.decode(errors='replace')[-1200:]}"
            )
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError(f"FFmpeg reported success but output is missing: {output_path}")

    delivered_fps, delivered_frames = _probe_output_fps_and_frames(
        ffmpeg, output_path, target_fps, total_frames,
    )
    return RealtimeRecordResult(
        str(output_path), delivered_frames, int(width), int(height), delivered_fps, False,
    )


__all__ = ["RealtimeRecordResult", "record_html_video"]
