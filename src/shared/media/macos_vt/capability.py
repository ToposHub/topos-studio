"""
macOS Zero-Copy Capability Gate
缓存 probe 结果，给主工程 gate 使用。

## 职责

- 检测 macOS VT 零拷贝是否可用
- 缓存 probe 结果
- 提供 gate 判定给 MediaFactory

## 环境变量

- TOPOS_VIDEO_ZERO_COPY=auto|off|force
- TOPOS_VIDEO_ZERO_COPY_PROBE=1
- TOPOS_VIDEO_ZERO_COPY_PROBE_FILE=/path/to/h264_or_hevc.mov
- TOPOS_VIDEO_FORCE_CPU_UPLOAD=1
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MacOSZeroCopyCapability:
    """
    macOS 零拷贝能力描述。

    Attributes:
        available: 零拷贝是否可用
        reason: 不可用原因（None 表示可用）
        supported_codecs: 支持零拷贝的编码器集合
        supported_pixel_formats: 支持零拷贝的像素格式集合
        gl_texture_target: GL texture target 类型（2d, rectangle）
        last_probe_json: 最近一次 probe 的 JSON 结果
        last_probe_file: 最近一次 live probe 使用的视频文件
    """
    available: bool = False
    reason: str = "not_probed"
    supported_codecs: set = field(default_factory=lambda: {'h264', 'hevc', 'h265', 'prores'})
    supported_pixel_formats: set = field(default_factory=lambda: {'nv12', 'p010'})
    gl_texture_target: str = "2d"
    last_probe_json: str | None = None
    last_probe_file: str | None = None
    implementation_ready: bool = False


_zero_copy_env_var = os.environ.get('TOPOS_VIDEO_ZERO_COPY', '').lower()
_force_cpu_upload = os.environ.get('TOPOS_VIDEO_FORCE_CPU_UPLOAD', '').lower() in ('1', 'true', 'yes')
_probe_requested = os.environ.get('TOPOS_VIDEO_ZERO_COPY_PROBE', '').lower() in ('1', 'true', 'yes')
_probe_json_path = os.environ.get('TOPOS_VIDEO_ZERO_COPY_PROBE_JSON', '')
_probe_file_path = os.environ.get('TOPOS_VIDEO_ZERO_COPY_PROBE_FILE', '')

_cached_capability: MacOSZeroCopyCapability | None = None
_capability_lock = threading.Lock()
# AUD-35001：自动模式探测可能运行最长 30s 的子进程 probe——
# 探测单飞在独立锁上进行，_capability_lock 只保护缓存读写，
# invalidate/快路径查询不再被探测串行阻塞
_capability_probe_lock = threading.Lock()
_prewarm_lock = threading.Lock()
_prewarm_thread: threading.Thread | None = None

_PERSISTENT_CACHE_SCHEMA_VERSION = 3
_PERSISTENT_CACHE_DEFAULT_TTL_SECONDS = 14 * 24 * 60 * 60


def _persistent_cache_ttl_seconds() -> int:
    try:
        value = int(os.environ.get(
            'TOPOS_VIDEO_ZERO_COPY_CAPABILITY_CACHE_TTL',
            str(_PERSISTENT_CACHE_DEFAULT_TTL_SECONDS),
        ))
        return max(60, value)
    except Exception:
        return _PERSISTENT_CACHE_DEFAULT_TTL_SECONDS


def _persistent_cache_enabled() -> bool:
    mode = os.environ.get('TOPOS_VIDEO_ZERO_COPY_PERSISTENT_CACHE', '').lower()
    if mode in ('0', 'false', 'no', 'off'):
        return False
    if _probe_json_path or _probe_requested:
        return False
    # Unit tests should keep the live probe deterministic unless a test opts in.
    if os.environ.get('PYTEST_CURRENT_TEST') and mode not in ('1', 'true', 'yes', 'on'):
        return False
    return True


def _capability_cache_path() -> Path:
    override = os.environ.get('TOPOS_VIDEO_ZERO_COPY_CAPABILITY_CACHE_FILE')
    if override:
        return Path(override).expanduser()

    cache_dir_override = os.environ.get('TOPOS_VIDEO_ZERO_COPY_CAPABILITY_CACHE_DIR')
    if cache_dir_override:
        base_dir = Path(cache_dir_override).expanduser()
    elif platform.system() == 'Darwin':
        base_dir = Path.home() / 'Library' / 'Caches' / 'ToposColor'
    else:
        base_dir = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'topos-color'

    return base_dir / 'macos_vt_zero_copy_capability.json'


def _probe_file_fingerprint(probe_file: str | None) -> dict[str, Any] | None:
    if not probe_file:
        return None
    path = Path(probe_file).expanduser()
    try:
        stat = path.stat()
        resolved = str(path.resolve())
        return {
            'path': resolved,
            'size': int(stat.st_size),
            'mtime_ns': int(stat.st_mtime_ns),
        }
    except Exception:
        return {
            'path': str(path),
            'missing': True,
        }


def _capability_cache_key(probe_file: str | None) -> dict[str, Any]:
    return {
        'schema_version': _PERSISTENT_CACHE_SCHEMA_VERSION,
        'platform': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'python': f"{sys.version_info.major}.{sys.version_info.minor}",
        'probe_file': _probe_file_fingerprint(probe_file),
    }


def _load_persistent_capability_cache(probe_file: str | None) -> MacOSZeroCopyCapability | None:
    if not _persistent_cache_enabled():
        return None

    cache_path = _capability_cache_path()
    try:
        with open(cache_path, 'r') as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"macOS zero-copy capability cache read failed: {e}")
        return None

    try:
        if payload.get('schema_version') != _PERSISTENT_CACHE_SCHEMA_VERSION:
            return None
        created_at = float(payload.get('created_at', 0.0) or 0.0)
        if time.time() - created_at > _persistent_cache_ttl_seconds():
            return None
        if payload.get('cache_key') != _capability_cache_key(probe_file):
            return None

        cap_data = payload.get('capability') or {}
        available = bool(cap_data.get('available'))
        reason = str(cap_data.get('reason') or '')
        # A completed color self-test is stable for this machine/driver and
        # should fail closed without taxing every subsequent startup.
        if not available and 'probe_color_verification_failed' not in reason:
            return None

        cap = MacOSZeroCopyCapability(
            available=available,
            reason=reason,
            supported_codecs=set(cap_data.get('supported_codecs') or {'h264', 'hevc', 'h265', 'prores'}),
            supported_pixel_formats=set(cap_data.get('supported_pixel_formats') or {'nv12', 'p010'}),
            gl_texture_target=str(cap_data.get('gl_texture_target') or 'unknown'),
            last_probe_json=cap_data.get('last_probe_json'),
            last_probe_file=cap_data.get('last_probe_file') or probe_file,
            implementation_ready=bool(cap_data.get('implementation_ready', True)),
        )
        logger.info(
            "macOS zero-copy capability: loaded persistent cache "
            f"({cache_path}, available={cap.available}, target={cap.gl_texture_target})"
        )
        return cap
    except Exception as e:
        logger.debug(f"macOS zero-copy capability cache invalid: {e}")
        return None


def _store_persistent_capability_cache(
    cap: MacOSZeroCopyCapability,
    probe_file: str | None,
    probe_result: dict[str, Any] | None,
) -> None:
    cacheable_color_failure = 'probe_color_verification_failed' in cap.reason
    if (not cap.available and not cacheable_color_failure) or not _persistent_cache_enabled():
        return

    cache_path = _capability_cache_path()
    payload = {
        'schema_version': _PERSISTENT_CACHE_SCHEMA_VERSION,
        'created_at': time.time(),
        'cache_key': _capability_cache_key(probe_file),
        'capability': {
            'available': cap.available,
            'reason': cap.reason,
            'supported_codecs': sorted(cap.supported_codecs),
            'supported_pixel_formats': sorted(cap.supported_pixel_formats),
            'gl_texture_target': cap.gl_texture_target,
            'last_probe_json': cap.last_probe_json,
            'last_probe_file': cap.last_probe_file,
            'implementation_ready': cap.implementation_ready,
        },
        'probe_result': probe_result,
    }

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + '.tmp')
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp_path, cache_path)
    except Exception as e:
        logger.debug(f"macOS zero-copy capability cache write failed: {e}")


def _get_zero_copy_mode() -> str:
    """
    获取 VT zero-copy 模式配置。

    优先级：
    1. 环境变量 TOPOS_VIDEO_ZERO_COPY（调试用）
    2. 配置文件 video_macos_vt_zero_copy（用户偏好）
    3. 默认值 'auto'

    Returns:
        'auto' | 'off' | 'force'
    """
    # Environment variable takes highest priority (for debugging)
    if _zero_copy_env_var in ('auto', 'off', 'force'):
        return _zero_copy_env_var

    # Read from config manager (user preference)
    try:
        from src.shared.config.config_manager import ConfigManager
        cfg = ConfigManager()
        mode = cfg.get('video_macos_vt_zero_copy', 'auto')
        if mode in ('auto', 'off', 'force'):
            return mode
    except Exception:
        pass

    return 'auto'


def clear_capability_cache() -> None:
    """清除能力缓存，使下次查询重新读取配置。"""
    global _cached_capability
    with _capability_lock:
        _cached_capability = None


def _probe_int_env(name: str, default: int) -> int:
    """Read a positive integer probe tuning env var."""
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except Exception:
        return default


def _find_default_probe_file() -> str | None:
    """
    Find a H.264/HEVC fixture for the auto zero-copy probe.

    Auto mode must prove decode -> IOSurface -> GL bind.  In development and
    tests we use the checked-in video fixtures unless the caller provides
    TOPOS_VIDEO_ZERO_COPY_PROBE_FILE.
    """
    if _probe_file_path:
        return _probe_file_path if os.path.exists(_probe_file_path) else None

    root = Path(__file__).resolve().parents[4]
    candidates = [
        root / "tests" / "test_videos" / "2K_h264.mp4",
        root / "tests" / "test_videos" / "2K_H265.mov",
        root / "tests" / "test_videos" / "4K_H264.mov",
        root / "tests" / "test_videos" / "4K_H265.mov",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def _probe_result_is_zero_copy_proven(probe: dict[str, Any]) -> tuple[bool, str]:
    """
    Validate that a probe proves the real hot path, not just importability.
    """
    if not probe.get('probe_success'):
        return False, "probe_failed"
    color_verification = probe.get('color_verification') or {}
    if not color_verification:
        return False, "probe_missing_color_verification"
    if not bool(color_verification.get('passed')):
        return False, "probe_color_verification_failed"
    if not probe.get('zero_copy'):
        return False, "probe_reports_not_zero_copy"
    if int(probe.get('decoded_pixel_buffers', 0) or 0) <= 0:
        return False, "probe_decoded_no_frames"
    if not probe.get('cvpixelbuffer_iosurface_backed') or not probe.get('iosurface_present'):
        return False, "probe_missing_iosurface"
    if not probe.get('bridge_initialized'):
        return False, "probe_bridge_not_initialized"
    if not probe.get('gl_texture_created'):
        return False, "probe_gl_texture_not_created"
    if int(probe.get('planes_bound', 0) or 0) <= 0:
        return False, "probe_no_gl_planes_bound"
    if int(probe.get('cpu_download_frames', 0) or 0) != 0:
        return False, "probe_reports_cpu_download"
    if int(probe.get('texture_write_frames', 0) or 0) != 0:
        return False, "probe_reports_texture_write"
    return True, ""


def _run_auto_zero_copy_probe() -> tuple[bool, str, str | None, dict[str, Any] | None]:
    """
    Run the real decode-to-RGB proof used by auto mode in a subprocess.

    The capability gate needs decode -> IOSurface -> GL texture bind -> RGB
    conversion. A bridge that only binds but corrupts chroma is unavailable.

    The native VT/CGL bridge can crash the interpreter if the CGL context or
    IOSurface binding is invalid.  Running the probe out-of-process keeps the
    app/test process alive and turns crashes into a clean unavailable result.
    """
    probe_file = _find_default_probe_file()
    if not probe_file:
        return False, "probe_file_not_found", None, None

    root = Path(__file__).resolve().parents[4]
    frames = _probe_int_env('TOPOS_VIDEO_ZERO_COPY_PROBE_FRAMES', 3)
    warmup = _probe_int_env('TOPOS_VIDEO_ZERO_COPY_PROBE_WARMUP', 1)
    timeout = _probe_int_env('TOPOS_VIDEO_ZERO_COPY_PROBE_TIMEOUT', 30)

    with tempfile.NamedTemporaryFile(prefix="topos_vt_zero_copy_probe_", suffix=".json", delete=False) as tmp:
        output_json = tmp.name

    try:
        env = os.environ.copy()
        env['PYTHONPATH'] = (
            str(root)
            if not env.get('PYTHONPATH')
            else f"{root}{os.pathsep}{env['PYTHONPATH']}"
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.shared.media.macos_vt.vt_zero_copy_probe",
                "--file",
                probe_file,
                "--frames",
                str(frames),
                "--warmup",
                str(warmup),
                "--stage",
                "full",
                "--verify-color",
                "--output-json",
                output_json,
            ],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        probe = None
        try:
            with open(output_json, 'r') as f:
                probe = json.load(f)
        except Exception:
            pass
        if proc.returncode != 0:
            if probe is not None:
                ok, reason = _probe_result_is_zero_copy_proven(probe)
                return False, reason or probe.get('fallback_reason') or "probe_not_proven", probe_file, probe
            return False, f"probe_process_failed: {proc.returncode}", probe_file, None
        if probe is None:
            return False, "probe_result_missing", probe_file, None
        ok, reason = _probe_result_is_zero_copy_proven(probe)
        if not ok:
            return False, reason or probe.get('fallback_reason') or "probe_not_proven", probe_file, probe
        return True, "", probe_file, probe
    except subprocess.TimeoutExpired:
        return False, "probe_process_timeout", probe_file, None
    except Exception as e:
        logger.warning(f"macOS zero-copy auto probe failed: {e}")
        return False, f"probe_error: {e}", probe_file, None
    finally:
        try:
            os.unlink(output_json)
        except Exception:
            pass


def get_zero_copy_capability() -> MacOSZeroCopyCapability:
    """
    获取 macOS 零拷贝能力（带缓存）。

    启用条件：
    - platform.system() == "Darwin"
    - VT decode implementation ready
    - 用户未禁用 zero-copy
    - IOSurface GL bridge available

    配置来源（优先级从高到低）：
    - TOPOS_VIDEO_ZERO_COPY 环境变量（调试用）
    - video_macos_vt_zero_copy 用户偏好设置
    - TOPOS_VIDEO_FORCE_CPU_UPLOAD=1: 强制 CPU upload

    Returns:
        MacOSZeroCopyCapability
    """
    global _cached_capability

    # AUD-35001：缓存命中走快路径，仅短暂持有 _capability_lock——
    # 不会被在途探测（自动模式最长 30s 的子进程 probe）阻塞
    with _capability_lock:
        if _cached_capability is not None:
            return _cached_capability

    return _compute_zero_copy_capability()


def _compute_zero_copy_capability() -> MacOSZeroCopyCapability:
    """计算（并缓存）零拷贝能力（探测在 _capability_probe_lock 上单飞）。

    AUD-35001：子进程 probe 全程不持有 _capability_lock；并发首查在
    探测锁上串行，防止重复 probe（与原先持 _capability_lock 等价）。
    """
    global _cached_capability

    with _capability_probe_lock:
        with _capability_lock:
            if _cached_capability is not None:
                return _cached_capability

        cap = MacOSZeroCopyCapability()
        mode = _get_zero_copy_mode()

        if platform.system() != 'Darwin':
            cap.reason = "not_macos"
            cap.available = False
            _cached_capability = cap
            return cap

        if _force_cpu_upload:
            cap.reason = "env_force_cpu_upload"
            cap.available = False
            _cached_capability = cap
            return cap

        if mode == 'off':
            cap.reason = "user_disabled"
            cap.available = False
            _cached_capability = cap
            return cap

        try:
            from .vt_session import is_vt_available, is_vt_decode_implemented
            if not is_vt_available():
                cap.reason = "pyobjc_not_available"
                cap.available = False
                _cached_capability = cap
                return cap
            if not is_vt_decode_implemented():
                cap.reason = "vt_decode_impl_not_available"
                cap.available = False
                _cached_capability = cap
                return cap
        except (ImportError, Exception) as e:
            cap.reason = f"vt_module_import_error: {e}"
            cap.available = False
            _cached_capability = cap
            return cap

        try:
            from .iosurface_gl_bridge import is_iosurface_bridge_implemented
            if not is_iosurface_bridge_implemented():
                cap.reason = "iosurface_gl_bridge_impl_not_available"
                cap.available = False
                _cached_capability = cap
                return cap
        except (ImportError, Exception) as e:
            cap.reason = f"iosurface_bridge_module_import_error: {e}"
            cap.available = False
            _cached_capability = cap
            return cap

        cap.implementation_ready = True

        if mode == 'force':
            cap.available = True
            cap.reason = "user_force"
            cap.gl_texture_target = "auto"
            _cached_capability = cap
            return cap

        probe_result: dict[str, Any] | None = None
        probe_file: str | None = None
        if _probe_json_path:
            try:
                with open(_probe_json_path, 'r') as f:
                    probe = json.load(f)
                probe_result = probe
                cap.last_probe_json = _probe_json_path
                ok, reason = _probe_result_is_zero_copy_proven(probe)
                if not ok:
                    cap.reason = f"probe_json_not_proven: {reason}"
                    cap.available = False
                    _cached_capability = cap
                    return cap
            except Exception as e:
                cap.reason = f"probe_json_error: {e}"
                cap.available = False
                _cached_capability = cap
                return cap
        else:
            probe_file = _find_default_probe_file()
            cached = _load_persistent_capability_cache(probe_file)
            if cached is not None:
                _cached_capability = cached
                return cached

            ok, reason, live_probe_file, probe_result = _run_auto_zero_copy_probe()
            probe_file = live_probe_file or probe_file
            cap.last_probe_file = probe_file
            if not ok:
                cap.reason = f"auto_probe_not_proven: {reason}"
                cap.available = False
                _store_persistent_capability_cache(cap, probe_file, probe_result)
                _cached_capability = cap
                return cap

        cap.available = True
        cap.reason = ""
        texture_target = (probe_result or {}).get("texture_target") or "unknown"
        cap.gl_texture_target = texture_target if texture_target in {"2d", "rectangle"} else "unknown"

        logger.info(
            f"macOS zero-copy capability: available={cap.available}, "
            f"codecs={cap.supported_codecs}, "
            f"formats={cap.supported_pixel_formats}"
        )

        _store_persistent_capability_cache(cap, probe_file or cap.last_probe_file, probe_result)
        _cached_capability = cap
        return cap


def prewarm_zero_copy_capability(blocking: bool = False) -> None:
    """
    Start macOS zero-copy capability initialization before first playback.

    In auto mode the first call may launch the decode-to-RGB subprocess probe.
    Running it from app/timeline setup keeps that work off the first rendered
    frame. Calls are deduplicated by the module-level capability lock/cache.
    """
    global _prewarm_thread

    if blocking:
        get_zero_copy_capability()
        return

    if _cached_capability is not None:
        return

    with _prewarm_lock:
        if _cached_capability is not None:
            return
        if _prewarm_thread is not None and _prewarm_thread.is_alive():
            return

        def _run() -> None:
            try:
                get_zero_copy_capability()
            except Exception as e:
                logger.debug(f"macOS zero-copy capability prewarm failed: {e}")

        _prewarm_thread = threading.Thread(
            target=_run,
            daemon=True,
            name="MacOSVtCapabilityPrewarm",
        )
        _prewarm_thread.start()


def is_zero_copy_available(codec: str = "", pixel_format: str = "") -> bool:
    """
    快速判断零拷贝是否可用。

    Args:
        codec: 编码器名称（可选过滤）
        pixel_format: 像素格式（可选过滤）

    Returns:
        True 如果零拷贝可用
    """
    if _zero_copy_env_var == 'off' or _force_cpu_upload:
        return False

    cap = get_zero_copy_capability()

    if not cap.available:
        return False

    if codec and cap.supported_codecs and codec.lower() not in cap.supported_codecs:
        return False

    if pixel_format and cap.supported_pixel_formats and pixel_format.lower() not in cap.supported_pixel_formats:
        return False

    return True


def invalidate_capability_cache() -> None:
    """清除能力缓存（用于重新 probe）"""
    global _cached_capability
    with _capability_lock:
        _cached_capability = None
