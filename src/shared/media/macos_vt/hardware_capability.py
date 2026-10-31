"""
macOS VideoToolbox hardware-decode capability probe.

The zero-copy capability gate (capability.py) proves the decode → IOSurface →
GL pipeline works; it does NOT tell whether VideoToolbox actually uses a
hardware decoder for a given codec/bit-depth. VideoToolbox silently falls
back to its (much slower) software decoder — e.g. 10-bit HEVC on Intel
integrated graphics has no fixed-function decoder, so a "working" VT session
decodes 4K at ~42ms/frame where FFmpeg frame-threading manages ~4ms.

This module answers exactly that question:

    probe = vt_hardware_decode_available(path, codec, bit_depth, w, h)

    True   — a hardware-required VTDecompressionSession can be created
    False  — no hardware decoder covers this format (VT would be software)
    None   — unknown (probe infrastructure unavailable; keep VT as before)

Results are cached per (codec, bit-depth, size-bucket) with a machine
fingerprint (CPU brand string, model, OS) so switching hardware — a
hackintosh upgraded to a new machine, or a move to Apple Silicon —
re-probes automatically instead of trusting a stale verdict.

Playback policy (``playback_should_prefer_ffmpeg``) consumes the probe:
when VT is software-only for an H.264/H.265 stream, sequential playback is
much faster on the FFmpeg planar path (frame-threaded decode + planar GPU
upload). Scrub/interactive preview keeps the VT zero-copy path regardless:
random-access decode cannot amortize the frame-threading pipeline, and the
IOSurface zero-copy output is worth more there.

Environment overrides (debugging / benchmarking):
- TOPOS_PLAYBACK_VT_POLICY=auto|force_vt|force_ffmpeg
- TOPOS_VT_HW_CACHE_DIR / TOPOS_VT_HW_CACHE_FILE
- TOPOS_VT_HW_PROBE=0   (disable probing: treat every format as unknown)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_HW_CACHE_SCHEMA_VERSION = 1
_HW_CACHE_TTL_SECONDS = 14 * 24 * 60 * 60

_hw_lock = threading.Lock()
_hw_cache: Dict[Tuple[str, int, str], Optional[bool]] = {}
_hw_machine_fingerprint: Optional[Dict[str, Any]] = None
# AUD-35011：机器指纹懒初始化的专用锁（与 _hw_lock 固定顺序：
# _hw_lock → _FINGERPRINT_LOCK，无死锁风险）
_FINGERPRINT_LOCK = threading.Lock()


def _cpu_brand_string() -> str:
    """Best-effort CPU identifier (macOS sysctl; distinguishes machines)."""
    if platform.system() != 'Darwin':
        return platform.processor() or 'unknown'
    try:
        out = subprocess.run(
            ['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'],
            capture_output=True, text=True, timeout=2.0,
        )
        brand = (out.stdout or '').strip()
        if brand:
            return brand
    except Exception:
        pass
    return platform.processor() or 'unknown'


def _machine_fingerprint() -> Dict[str, Any]:
    """Stable-ish identity of the current machine for cache keying."""
    global _hw_machine_fingerprint
    # AUD-35011：懒初始化持锁，并发首查不再重复计算（sysctl 子进程）
    with _FINGERPRINT_LOCK:
        if _hw_machine_fingerprint is None:
            mac_ver = platform.mac_ver()
            _hw_machine_fingerprint = {
                'platform': platform.system(),
                'machine': platform.machine(),
                'mac_ver': mac_ver[0] or '',
                'model': mac_ver[2] or '',
                'cpu': _cpu_brand_string(),
            }
        return _hw_machine_fingerprint


def _hw_cache_enabled() -> bool:
    if os.environ.get('TOPOS_VT_HW_PROBE', '').lower() in ('0', 'false', 'no', 'off'):
        return False
    if os.environ.get('TOPOS_VT_HW_CACHE', '').lower() in ('1', 'true', 'yes', 'on'):
        return True
    if os.environ.get('PYTEST_CURRENT_TEST'):
        return False
    return True


def _hw_cache_path() -> Path:
    override = os.environ.get('TOPOS_VT_HW_CACHE_FILE')
    if override:
        return Path(override).expanduser()
    cache_dir_override = os.environ.get('TOPOS_VT_HW_CACHE_DIR')
    if cache_dir_override:
        base_dir = Path(cache_dir_override).expanduser()
    elif platform.system() == 'Darwin':
        base_dir = Path.home() / 'Library' / 'Caches' / 'ToposColor'
    else:
        base_dir = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'topos-color'
    return base_dir / 'macos_vt_hw_decode.json'


def _size_bucket(width: int, height: int) -> str:
    pixels = max(0, int(width)) * max(0, int(height))
    if pixels <= 1920 * 1088:
        return 'hd'
    if pixels <= 4096 * 2304:
        return 'uhd'
    return 'large'


def _hw_cache_key(codec: str, bit_depth: int, width: int, height: int) -> Dict[str, Any]:
    return {
        'schema_version': _HW_CACHE_SCHEMA_VERSION,
        'machine': _machine_fingerprint(),
        'codec': str(codec).lower(),
        'bit_depth': int(bit_depth),
        'size_bucket': _size_bucket(width, height),
    }


def _load_hw_cache() -> Dict[Any, Any]:
    """Read the persistent {cache_key-json: bool} verdict map (best-effort)."""
    if not _hw_cache_enabled():
        return {}
    try:
        with open(_hw_cache_path(), 'r') as f:
            payload = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.debug(f"VT hw-decode cache read failed: {e}")
        return {}
    entries = payload.get('entries')
    if payload.get('schema_version') != _HW_CACHE_SCHEMA_VERSION or not isinstance(entries, dict):
        return {}
    created_at = float(payload.get('created_at', 0.0) or 0.0)
    if time.time() - created_at > _HW_CACHE_TTL_SECONDS:
        return {}
    return entries


def _store_hw_cache(entries: Dict[str, Any]) -> None:
    if not _hw_cache_enabled() or not entries:
        return
    cache_path = _hw_cache_path()
    payload = {
        'schema_version': _HW_CACHE_SCHEMA_VERSION,
        'created_at': time.time(),
        'entries': entries,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + '.tmp')
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp_path, cache_path)
    except Exception as e:
        logger.debug(f"VT hw-decode cache write failed: {e}")


def probe_vt_hardware_decode_for_file(path: str) -> Optional[bool]:
    """
    Live probe: can a *hardware-required* VT session decode this file?

    Opens the container's first video stream, builds the same format
    description the decode path uses, then creates a VTDecompressionSession
    with kVTVideoDecoder_RequireHardwareAcceleratedVideoDecoder. Session
    creation fails when no hardware decoder covers the format, which is the
    exact condition under which normal sessions silently use software.

    Returns True/False on a definitive answer, None when the probe itself
    could not run (demux error, PyObjC missing, non-macOS).
    """
    if platform.system() != 'Darwin':
        return None
    try:
        from .packet_demux import PacketDemuxer
        from .vt_session import VtSession, is_vt_available
    except ImportError as e:
        logger.debug(f"VT hw probe import failed: {e}")
        return None
    if not is_vt_available():
        return None

    demuxer = PacketDemuxer()
    try:
        info = demuxer.open(path)
        codec = str(info.codec_name or '').lower()
        session = VtSession()
        verdict = session.probe_hardware_support(
            width=info.width,
            height=info.height,
            codec=codec,
            extradata=info.extradata,
            codec_tag=getattr(info, 'codec_tag', '') or '',
        )
        if verdict is False:
            # kVTVideoDecoderNotSupportedNow (-12906): the hardware decoder
            # pool can be momentarily exhausted by live sessions, which makes
            # a hardware-required create fail even when hardware exists.
            # Retry once before recording a definitive "software-only" verdict.
            time.sleep(0.1)
            retry = VtSession()
            verdict = retry.probe_hardware_support(
                width=info.width,
                height=info.height,
                codec=codec,
                extradata=info.extradata,
                codec_tag=getattr(info, 'codec_tag', '') or '',
            )
        return verdict
    except Exception as e:
        logger.debug(f"VT hw probe failed for {path}: {e}")
        return None
    finally:
        try:
            demuxer.close()
        except Exception:
            pass


def vt_hardware_decode_available(
    path: str,
    codec: str,
    bit_depth: int,
    width: int = 0,
    height: int = 0,
) -> Optional[bool]:
    """
    Cached hardware-decode verdict for (codec, bit-depth, size-bucket).

    Returns True (hardware available) / False (VT would be software-only)
    / None (unknown — probe infrastructure unavailable or disabled).
    """
    if not _hw_cache_enabled():
        return probe_vt_hardware_decode_for_file(path) if path else None

    key = (
        str(codec).lower(),
        int(bit_depth),
        _size_bucket(width, height),
    )
    with _hw_lock:
        if key in _hw_cache:
            return _hw_cache[key]

        # AUD-35011：持久化 verdict 的 load→probe→store 整体纳入 _hw_lock
        # ——并发探测新 codec 时不再整体覆写文件、丢失先写者的 verdict
        entries = _load_hw_cache()
        cache_key_json = json.dumps(
            _hw_cache_key(codec, bit_depth, width, height), sort_keys=True,
        )
        if cache_key_json in entries:
            verdict = entries[cache_key_json]
            if isinstance(verdict, bool):
                _hw_cache[key] = verdict
                return verdict

        verdict = probe_vt_hardware_decode_for_file(path) if path else None
        if verdict is None:
            return None
        _hw_cache[key] = verdict
        entries[cache_key_json] = verdict
        _store_hw_cache(entries)
    logger.info(
        f"VT hardware decode probe: codec={codec} bit_depth={bit_depth} "
        f"bucket={_size_bucket(width, height)} -> "
        f"{'hardware' if verdict else 'software-only'}"
    )
    return verdict


def playback_should_prefer_ffmpeg(
    codec: str,
    pix_fmt: str,
    path: str = '',
    width: int = 0,
    height: int = 0,
) -> bool:
    """
    Playback-only routing policy: prefer the FFmpeg planar decoder?

    True when the stream is H.264/H.265 AND VideoToolbox has no hardware
    decoder for its bit-depth (VT software decode is 5-10x slower than
    FFmpeg frame-threading for sequential playback on those formats).
    Scrub/interactive preview never consults this policy.
    """
    mode = os.environ.get('TOPOS_PLAYBACK_VT_POLICY', 'auto').lower()
    if mode == 'force_ffmpeg':
        return True
    if mode == 'force_vt':
        return False

    codec_l = str(codec or '').lower()
    if not any(c in codec_l for c in ('h264', 'h265', 'hevc', 'avc')):
        return False

    from src.shared.media.ffmpeg_source import _infer_bit_depth
    bit_depth = _infer_bit_depth(str(pix_fmt or ''))
    if bit_depth <= 8:
        # 8-bit H.264/H.265 has hardware decode on every Mac VT supports;
        # skip the probe entirely (also keeps a probe off the first play).
        return False

    verdict = vt_hardware_decode_available(
        path, codec_l, bit_depth, width=width, height=height,
    )
    return verdict is False


def clear_hardware_capability_cache() -> None:
    """Drop in-memory verdicts (persistent cache stays on disk)."""
    global _hw_machine_fingerprint
    with _hw_lock:
        _hw_cache.clear()
    # AUD-35011：指纹置空与懒初始化同锁（顺序 _hw_lock → _FINGERPRINT_LOCK）
    with _FINGERPRINT_LOCK:
        _hw_machine_fingerprint = None
