#!/usr/bin/env python3
"""X-TEST-00：冻结 tests/test_videos 环境 + 素材不可变 manifest。

总计划（docs/ai_executable_optimization_upgrade_plan_2026-09-17.md §3.5）要求：
为 tests/test_videos 中每个原始素材生成不可变 manifest——绝对路径/内容哈希、
codec、profile、pix_fmt、位深、分辨率、FPS、帧数、时长、音频流；同时记录
macOS、GPU、Metal/GL、FFmpeg、TOPOS dylib ABI、git revision。

约束：
- 清单可重复生成：同一输入得同一 content hash 与探测结果（canonical digest
  不含生成时间/绝对路径等易变字段）。
- 缺失库、无 GPU、探测冲突必须带稳定 reason_code，不能静默跳过。
- 禁止写入/修改原始素材。

探测后端：
- PyAV（容器+流级元数据；TOPOS TPIC codec 无解码器，仅容器信息）。
- topos native（TPIC MOV 走 ToposMediaSource.probe；.topos 图像走
  ToposImageSource.probe）。PyAV 与 native 同时可得时做字段级冲突检测，
  冲突单独登记为 defect，不崩溃、不静默。

用法：
    .venv/bin/python tools/freeze_ai_test_asset_manifest.py            # 全量 sha256 + 生成证据
    .venv/bin/python tools/freeze_ai_test_asset_manifest.py --fast     # 首尾1MB+size 指纹
    .venv/bin/python tools/freeze_ai_test_asset_manifest.py --check    # 校验已冻结清单
    .venv/bin/python tools/freeze_ai_test_asset_manifest.py --output-dir DIR

退出码：0=全部条目有显式状态且无 FAIL/CONFLICT；3=存在 FAIL/CONFLICT/UNPROBED；
2=用法/环境错误；1=--check 漂移。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_MEDIA_DIR = REPO_ROOT / "tests" / "test_videos"
MANIFEST_PATH = DEFAULT_MEDIA_DIR / "ai_test_asset_manifest.json"

MANIFEST_SCHEMA = "xtest00_asset_manifest@1"
MANIFEST_VERSION = 1

#: 非素材条目（与 freeze_benchmark_media_manifest.py 保持一致并扩展）。
SKIP_FILES = {".DS_Store"}
SKIP_DIRS = {".cache", "__pycache__"}
SKIP_SUFFIXES = {".json"}
MEDIA_SUFFIXES = {".mov", ".mp4", ".mxf", ".mkv", ".avi", ".webm", ".topos"}

_FPS_RE = re.compile(r"p(8|10|12|14|16)(le|be)?$")


# ---------------------------------------------------------------------------
# 内容指纹
# ---------------------------------------------------------------------------

def sha256_fingerprint(path: Path, mode: str) -> str:
    """按 mode 计算内容指纹。fast=首尾各1MB+size；full=全文件 sha256。"""
    h = hashlib.sha256()
    if mode == "sha256_head1mb_tail1mb+size":
        size = path.stat().st_size
        head = 1 << 20
        h.update(str(size).encode())
        with path.open("rb") as f:
            h.update(f.read(head))
            if size > head * 2:
                f.seek(-head, 2)
                h.update(f.read(head))
        return h.hexdigest()
    if mode == "sha256_full":
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    raise ValueError(f"未知指纹模式: {mode}")


# ---------------------------------------------------------------------------
# 环境采集
# ---------------------------------------------------------------------------

def _git_info() -> Dict[str, Any]:
    def run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True,
                timeout=10, check=True,
            )
            return out.stdout.decode().strip()
        except (subprocess.SubprocessError, OSError):
            return "unknown"
    return {
        "revision": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_file_count": len(run("status", "--porcelain").splitlines()),
    }


def _gpu_info() -> Dict[str, Any]:
    """macOS GPU/Metal 信息（system_profiler）；失败带 reason_code。"""
    try:
        out = subprocess.run(
            ["system_profiler", "-json", "SPDisplaysDataType"],
            capture_output=True, timeout=30, check=True,
        )
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        return {
            "status": "UNAVAILABLE",
            "reason_code": "GPU_INFO_SYSTEM_PROFILER_FAILED",
            "detail": str(exc),
            "devices": [],
        }
    devices: List[Dict[str, Any]] = []
    for gpu in data.get("SPDisplaysDataType", []) or []:
        metal = (gpu.get("spdisplays_metal")
                 or gpu.get("spdisplays_mtlgpufamilysupport"))
        devices.append({
            "model": gpu.get("sppci_model"),
            "vendor": gpu.get("spdisplays_vendor"),
            "vram": gpu.get("spdisplays_vram_shared") or gpu.get("spdisplays_vram"),
            "cores": gpu.get("spdisplays_cores"),
            "metal_support": metal,
        })
    if not devices:
        return {
            "status": "UNAVAILABLE",
            "reason_code": "GPU_INFO_EMPTY",
            "detail": "system_profiler 未返回显示设备",
            "devices": [],
        }
    return {"status": "AVAILABLE", "reason_code": None, "detail": None,
            "devices": devices}


def _gl_info() -> Dict[str, Any]:
    """GL 上下文不在 X-TEST-00 内创建（避免脚本线程触碰 GPU/挂死），
    显式 DEFERRED 并给 reason_code——运行时 GL 由 T-P0-00 snapshot 验证。"""
    return {
        "status": "DEFERRED",
        "reason_code": "GL_CONTEXT_DEFERRED_TO_RUNTIME",
        "detail": "X-TEST-00 只冻结环境事实；GL 版本/渲染器由运行时 capability snapshot 采集",
    }


def _metal_info(gpu: Dict[str, Any]) -> Dict[str, Any]:
    """Metal 支持从 GPU 设备信息推导；GPU 信息缺失时同样带 reason_code。"""
    if gpu.get("status") != "AVAILABLE":
        return {
            "status": "UNAVAILABLE",
            "reason_code": "METAL_INFO_REQUIRES_GPU_INFO",
            "detail": None,
        }
    metals = sorted({d.get("metal_support") for d in gpu["devices"]
                     if d.get("metal_support")})
    if not metals:
        return {"status": "UNAVAILABLE", "reason_code": "METAL_SUPPORT_NOT_REPORTED",
                "detail": None}
    return {"status": "AVAILABLE", "reason_code": None,
            "detail": ", ".join(metals)}


def _ffmpeg_info() -> Dict[str, Any]:
    """PyAV（内嵌 FFmpeg）+ ffmpeg/ffprobe CLI 发现结果。"""
    info: Dict[str, Any] = {}
    try:
        import av  # noqa: PLC0415
        info["pyav"] = {
            "status": "AVAILABLE", "reason_code": None,
            "pyav_version": av.__version__,
            "ffmpeg_version": getattr(av, "ffmpeg_version_info", None),
        }
    except Exception as exc:  # ImportError 及库加载失败均视为不可用
        info["pyav"] = {"status": "UNAVAILABLE", "reason_code": "PYAV_IMPORT_FAILED",
                        "detail": str(exc)}

    from src.shared.ffmpeg_locator import find_ffmpeg, find_ffprobe
    for key, finder in (("ffmpeg_cli", find_ffmpeg), ("ffprobe_cli", find_ffprobe)):
        exe = finder()
        if exe:
            info[key] = {"status": "AVAILABLE", "reason_code": None, "path": str(exe)}
        else:
            info[key] = {
                "status": "UNAVAILABLE",
                "reason_code": f"{key.upper()}_NOT_FOUND",
                "detail": "TOPOS_FFMPEG_HOME/打包 bin/PATH 均未发现；探测仅走 PyAV/native",
            }
    return info


def _topos_codec_info() -> Dict[str, Any]:
    """TOPOS dylib 四态信息：路径、版本、ABI、能力；失败带 reason_code。"""
    from src.shared.codec import topos_binding

    explicit = os.environ.get("TOPOS_CODEC_LIB", "").strip()
    lib_path: Optional[Path] = None
    if explicit:
        p = Path(explicit)
        if p.is_file():
            lib_path = p
        else:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "TOPOS_CODEC_LIB_NOT_FOUND",
                "detail": f"TOPOS_CODEC_LIB 指定的库不存在: {p}",
            }
    else:
        for cand in topos_binding.candidate_library_paths():
            if cand.is_file():
                lib_path = cand
                break
        if lib_path is None:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "TOPOS_CODEC_LIB_NOT_FOUND",
                "detail": "candidate_library_paths() 未发现 libtopos_codec",
            }
    try:
        codec = topos_binding.ToposCodec(topos_binding.load_library(str(lib_path)))
    except topos_binding.ToposCodecError as exc:
        return {
            "status": "INCOMPATIBLE",
            "reason_code": "TOPOS_CODEC_INCOMPATIBLE",
            "library_path": str(lib_path),
            "detail": str(exc),
        }
    ver = codec.version()
    info: Dict[str, Any] = {
        "status": "AVAILABLE",
        "reason_code": None,
        "library_path": str(lib_path),
        "version": {
            "major": ver.major, "minor": ver.minor, "patch": ver.patch,
            "git_commit": ver.git_commit, "build_target": ver.build_target,
        },
        "abi_version": ver.abi_version,
    }
    try:
        cap = codec.capabilities()
        info["capabilities"] = {
            "abi_version": cap.abi_version,
            "max_width": cap.max_width, "max_height": cap.max_height,
            "full_decode": cap.full_decode,
            "fixed_reduced_decode": cap.fixed_reduced_decode,
            "auto2k_decode": cap.auto2k_decode,
            "cpu_surface_output": cap.cpu_surface_output,
            "host_visible_gpu_surface": cap.host_visible_gpu_surface,
        }
    except topos_binding.ToposCodecError as exc:
        info["capabilities_error"] = str(exc)
    return info


def collect_environment() -> Dict[str, Any]:
    mac_ver, _, arch = platform.mac_ver()
    env: Dict[str, Any] = {
        "platform": {
            "os": "macOS" if sys.platform == "darwin" else platform.system(),
            "os_version": mac_ver or platform.release(),
            "arch": arch or platform.machine(),
            "python": platform.python_version(),
        },
        "git": _git_info(),
        "gpu": _gpu_info(),
        "metal": None,  # 在下方依据 gpu 填充
        "gl": _gl_info(),
        "ffmpeg": _ffmpeg_info(),
        "topos_codec": _topos_codec_info(),
    }
    env["metal"] = _metal_info(env["gpu"])
    return env


# ---------------------------------------------------------------------------
# 探测后端
# ---------------------------------------------------------------------------

def _fps_pair(rate: Any) -> Optional[List[int]]:
    """Fraction→[num, den]；0/None 返回 None。"""
    try:
        f = Fraction(rate) if not isinstance(rate, Fraction) else rate
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if f <= 0:
        return None
    return [f.numerator, f.denominator]


def _fps_float(pair: Optional[List[int]]) -> Optional[float]:
    if not pair or pair[1] == 0:
        return None
    return round(pair[0] / pair[1], 4)


def _bit_depth_from_format(vf: Any, pix_fmt: Optional[str]) -> Optional[int]:
    """优先 PyAV format.components.bits；失败按 pix_fmt 名解析。"""
    try:
        bits = {c.bits for c in vf.components if c.bits}
        if len(bits) == 1:
            return bits.pop()
    except Exception:  # noqa: BLE001 — components 在个别 wrap 格式不可用
        pass
    if pix_fmt:
        m = _FPS_RE.search(pix_fmt)
        if m:
            return int(m.group(1))
        if pix_fmt.endswith(("le", "be")):
            m2 = re.search(r"(\d+)", pix_fmt[:-2])
            if m2:
                return int(m2.group(1))
    return None


#: AVCOL_Range 枚举 → 色彩范围命名（与 VideoMetadata 的 limited/full 对齐）。
_COLOR_RANGE_NAMES = {1: "tv", 2: "pc"}


def _stream_attr(obj: Any, name: str, default: Any = None) -> Any:
    """PyAV 18 对无解码器流（如 TPIC）的属性经 __getattr__ 委托
    codec_context 抛 AttributeError——统一防御性读取。"""
    try:
        value = getattr(obj, name)
    except (AttributeError, ValueError):
        return default
    return default if value is None else value


def pyav_probe(path: Path) -> Dict[str, Any]:
    """PyAV 容器/流级探测。TOPOS TPIC 无解码器：容器/帧数/帧率可得，
    codec 级字段为 None（宽度/像素格式只能来自 native 探测）。"""
    import av  # noqa: PLC0415

    result: Dict[str, Any] = {"backend": "pyav", "status": "AVAILABLE",
                              "reason_code": None, "detail": None}
    try:
        with av.open(str(path)) as c:
            fmt = c.format
            result["container"] = {
                "format_name": fmt.name,
                "format_long_name": fmt.long_name,
                "size_bytes": path.stat().st_size,
                "bit_rate": int(getattr(c, "bit_rate", 0) or 0),
            }
            duration_s = (c.duration / 1_000_000.0) if c.duration else None
            result["duration_s"] = round(duration_s, 3) if duration_s else None

            videos = c.streams.video
            if not videos:
                result["status"] = "FAILED"
                result["reason_code"] = "NO_VIDEO_STREAM"
                return result
            v = videos[0]
            cc = v.codec_context
            codec_name = _stream_attr(cc, "name") if cc is not None else None
            pix_fmt = None
            pf = None
            if cc is not None:
                try:
                    pf = cc.format  # VideoCodecContext.format → VideoFormat
                    pix_fmt = pf.name if pf is not None else None
                except (AttributeError, ValueError):
                    pix_fmt = None
            bit_depth = (None if pf is None
                         else _bit_depth_from_format(pf, pix_fmt))
            color_range = _stream_attr(cc, "color_range") if cc is not None else None
            result["video"] = {
                "codec": codec_name,
                "codec_tag": None,  # PyAV 18 codec_tag 经 codec_context 委托，无解码器不可得
                "profile": _stream_attr(v, "profile"),
                "pix_fmt": pix_fmt,
                "bit_depth": bit_depth,
                "width": _stream_attr(v, "width"),
                "height": _stream_attr(v, "height"),
                "fps": _fps_pair(_stream_attr(v, "average_rate")
                                 or _stream_attr(v, "base_rate")),
                "frame_count": int(_stream_attr(v, "frames", 0) or 0) or None,
                "color_range": _COLOR_RANGE_NAMES.get(
                    color_range) if isinstance(color_range, int) else color_range,
                "container_metadata_encoder": (v.metadata or {}).get("encoder"),
            }
            audio: List[Dict[str, Any]] = []
            for a in c.streams.audio:
                tb = _stream_attr(a, "time_base")
                dur = _stream_attr(a, "duration")
                audio.append({
                    "codec": _stream_attr(a.codec_context, "name")
                    if a.codec_context is not None else None,
                    "channels": int(_stream_attr(a, "channels", 0) or 0),
                    "sample_rate": int(_stream_attr(a, "rate", 0) or 0),
                    "duration_s": round(float(dur * tb), 3)
                    if dur and tb else None,
                })
            result["audio_streams"] = audio
            result["stream_counts"] = {
                "video": len(videos), "audio": len(c.streams.audio),
                "subtitle": len(getattr(c.streams, "subtitles", ())),
                "data": len(getattr(c.streams, "data", ())),
                "other": len(getattr(c.streams, "other", ())),
            }
    except av.error.FFmpegError as exc:  # PyAV 18 移除了 av.AVError 别名
        result["status"] = "FAILED"
        result["reason_code"] = "PYAV_OPEN_FAILED"
        result["detail"] = f"{type(exc).__name__}: {exc}"
    return result


def native_topos_probe(path: Path) -> Dict[str, Any]:
    """TOPOS native 探测：TPIC MOV → ToposMediaSource；.topos → ToposImageSource。

    非 TOPOS 文件返回 NOT_APPLICABLE（不是失败）。
    """
    try:
        from src.shared.media.topos_source import is_topos_movie
    except Exception as exc:  # noqa: BLE001 — 模块依赖缺失也要可见
        return {"backend": "topos_native", "status": "UNAVAILABLE",
                "reason_code": "TOPOS_SOURCE_IMPORT_FAILED", "detail": str(exc)}

    is_tpic = False
    try:
        is_tpic = bool(is_topos_movie(str(path)))
    except Exception as exc:  # noqa: BLE001
        return {"backend": "topos_native", "status": "UNAVAILABLE",
                "reason_code": "TOPOS_SNIFF_FAILED", "detail": str(exc)}
    if not is_tpic and path.suffix.lower() != ".topos":
        return {"backend": "topos_native", "status": "NOT_APPLICABLE",
                "reason_code": None, "detail": None}

    try:
        if path.suffix.lower() == ".topos":
            from src.shared.media.topos_image_source import ToposImageSource
            source = ToposImageSource(str(path))  # type: ignore[call-arg]
        else:
            from src.shared.media.topos_source import ToposMediaSource
            source = ToposMediaSource(str(path))
        try:
            source.open()  # ToposMediaSource.probe 需先 open（ToposImageSource 幂等）
            meta = source.probe()
        finally:
            try:
                source.close()
            except Exception:  # noqa: BLE001 — close 失败不影响探测结果
                pass
    except Exception as exc:  # noqa: BLE001 — ToposCodecError/IO 都归为 UNAVAILABLE
        reason = "TOPOS_NATIVE_PROBE_FAILED"
        text = str(exc)
        if "TOPOS_CODEC_LIB" in text or "未找到 libtopos_codec" in text:
            reason = "TOPOS_CODEC_LIB_NOT_FOUND"
        elif "不匹配" in text or "缺少" in text:
            reason = "TOPOS_CODEC_INCOMPATIBLE"
        return {"backend": "topos_native", "status": "UNAVAILABLE",
                "reason_code": reason, "detail": text}

    fps_pair = [meta.fps.num, meta.fps.den] if meta.fps else None
    return {
        "backend": "topos_native",
        "status": "AVAILABLE",
        "reason_code": None,
        "detail": None,
        "video": {
            "codec": meta.codec_name or "topos",
            "profile": meta.codec_profile,
            "pix_fmt": meta.pixel_format,
            "bit_depth": meta.bit_depth,
            "width": meta.width,
            "height": meta.height,
            "fps": fps_pair,
            "frame_count": meta.total_frames,
            "color_range": meta.color_range,
            "color_matrix": meta.color_matrix,
            "color_primaries": meta.color_primaries,
            "color_transfer": meta.color_transfer,
            "has_alpha": meta.has_alpha,
        },
        "audio_streams": [],
    }


#: 参与冲突检测的字段（video 级，双方可比时才比对）。
_CONFLICT_FIELDS = ("width", "height", "fps_float", "bit_depth", "frame_count")


def _video_comparable(video: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not video or not video.get("width") or not video.get("height"):
        return None
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "fps_float": _fps_float(video.get("fps")),
        "bit_depth": video.get("bit_depth"),
        "frame_count": video.get("frame_count") or None,
    }


def detect_probe_conflicts(probes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对同一文件的多个 AVAILABLE 探测结果做字段级冲突检测。"""
    comparable: List[Tuple[str, Dict[str, Any]]] = []
    for p in probes:
        if p.get("status") != "AVAILABLE":
            continue
        vc = _video_comparable(p.get("video"))
        if vc:
            comparable.append((p["backend"], vc))
    conflicts: List[Dict[str, Any]] = []
    for i in range(len(comparable)):
        for j in range(i + 1, len(comparable)):
            a_name, a = comparable[i]
            b_name, b = comparable[j]
            for field in _CONFLICT_FIELDS:
                va, vb = a.get(field), b.get(field)
                if va is None or vb is None:
                    continue
                if field == "fps_float":
                    differs = abs(float(va) - float(vb)) > 0.01
                else:
                    differs = int(va) != int(vb)
                if differs:
                    conflicts.append({
                        "field": field,
                        "backends": [a_name, b_name],
                        "values": {a_name: va, b_name: vb},
                        "reason_code": "PROBE_METADATA_CONFLICT",
                    })
    return conflicts


# ---------------------------------------------------------------------------
# 资产枚举与合并
# ---------------------------------------------------------------------------

def enumerate_assets(media_dir: Path) -> List[Path]:
    files: List[Path] = []
    for p in sorted(media_dir.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(media_dir).parts[:-1]):
            continue
        if p.name in SKIP_FILES or p.suffix.lower() in SKIP_SUFFIXES:
            continue
        if p.suffix.lower() in MEDIA_SUFFIXES:
            files.append(p)
    return files


def _is_zip_package(path: Path) -> bool:
    """Topos 工程包（.topos）是 ZIP 容器（PK 魔数），不是媒体流。"""
    try:
        with path.open("rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def probe_asset(path: Path, fingerprint_mode: str) -> Dict[str, Any]:
    """对单个素材执行指纹 + 多后端探测 + 冲突检测，返回完整条目。"""
    rel = str(path.relative_to(REPO_ROOT))
    if path.suffix.lower() == ".topos" and _is_zip_package(path):
        # 工程包不是媒体素材：显式分类，不做探测，也不算失败。
        return {
            "file": rel,
            "absolute_path": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "content_hash": {
                "mode": fingerprint_mode,
                "digest": sha256_fingerprint(path, fingerprint_mode),
            },
            "probe_status": "NOT_MEDIA",
            "probe_reason_codes": ["TOPOS_PROJECT_PACKAGE_DETECTED"],
            "probe_backends": [],
            "probe_conflicts": [],
            "container": {"format_name": "zip"},
            "metadata_source": None,
        }
    probes = [pyav_probe(path), native_topos_probe(path)]
    conflicts = detect_probe_conflicts(probes)

    # 条目级状态：
    # - 任一后端 AVAILABLE → PASS（TPIC 文件 pyav 容器可得 + native 可得）
    # - native NOT_APPLICABLE 且 pyav FAILED → FAILED
    # - 全部 UNAVAILABLE/DEFERRED → UNPROBED
    statuses = [p["status"] for p in probes]
    if any(s == "AVAILABLE" for s in statuses):
        probe_status = "PASS"
    elif any(s == "NOT_APPLICABLE" for s in statuses) and any(
            s == "FAILED" for s in statuses):
        probe_status = "FAILED"
    else:
        probe_status = "UNPROBED"

    reasons = sorted({p["reason_code"] for p in probes
                      if p.get("reason_code")})
    if conflicts:
        probe_status = "CONFLICT"

    entry: Dict[str, Any] = {
        "file": rel,
        "absolute_path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "content_hash": {
            "mode": fingerprint_mode,
            "digest": sha256_fingerprint(path, fingerprint_mode),
        },
        "probe_status": probe_status,
        "probe_reason_codes": reasons,
        "probe_backends": probes,
        "probe_conflicts": conflicts,
    }
    # 汇总一份"权威 video 元数据"：native 优先（TOPOS 域内权威），否则 PyAV。
    by_backend = {p["backend"]: p for p in probes if p.get("status") == "AVAILABLE"}
    for backend in ("topos_native", "pyav"):
        if backend in by_backend:
            entry["video"] = by_backend[backend].get("video")
            entry["container"] = by_backend[backend].get("container")
            entry["audio_streams"] = by_backend[backend].get("audio_streams", [])
            entry["duration_s"] = by_backend[backend].get("duration_s")
            entry["stream_counts"] = by_backend[backend].get("stream_counts")
            entry["metadata_source"] = backend
            break
    return entry


def canonical_digest(manifest: Dict[str, Any]) -> str:
    """对清单的稳定内容（除 generated_at/absolute_path/git dirty 外）做 sha256，
    保证"同一输入得同一 digest"，跨机器可对比。"""
    env = json.loads(json.dumps(manifest.get("environment", {})))
    env.setdefault("git", {})["dirty_file_count"] = 0
    assets = []
    for entry in manifest.get("assets", []):
        e = json.loads(json.dumps(entry))
        e.pop("absolute_path", None)
        assets.append(e)
    canonical = {
        "schema": manifest.get("$schema"),
        "environment": env,
        "assets": assets,
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 构建与校验
# ---------------------------------------------------------------------------

def build_manifest(media_dir: Path, fingerprint_mode: str,
                   log) -> Dict[str, Any]:
    env = collect_environment()
    files = enumerate_assets(media_dir)
    log(f"发现 {len(files)} 个素材文件（fingerprint={fingerprint_mode}）")
    assets: List[Dict[str, Any]] = []
    for i, path in enumerate(files, 1):
        t0 = time.monotonic()
        entry = probe_asset(path, fingerprint_mode)
        log(f"[{i}/{len(files)}] {path.name} -> {entry['probe_status']} "
            f"({time.monotonic() - t0:.2f}s)")
        assets.append(entry)
    manifest = {
        "$schema": MANIFEST_SCHEMA,
        "$schema_version": MANIFEST_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "media_dir": str(media_dir),
        "fingerprint_mode": fingerprint_mode,
        "environment": env,
        "asset_count": len(assets),
        "status_counts": _status_counts(assets),
        "assets": assets,
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def _status_counts(assets: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in assets:
        counts[e["probe_status"]] = counts.get(e["probe_status"], 0) + 1
    return counts


def check_manifest(path: Path, log) -> int:
    if not path.exists():
        log(f"manifest missing: {path}")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    drift = 0
    known: set[str] = set()
    for entry in data.get("assets", []):
        f = REPO_ROOT / entry["file"]
        known.add(str(f.relative_to(REPO_ROOT)))
        if not f.exists():
            log(f"MISSING: {entry['file']}")
            drift += 1
            continue
        mode = entry["content_hash"]["mode"]
        current = sha256_fingerprint(f, mode)
        if current != entry["content_hash"]["digest"]:
            log(f"DRIFT: {entry['file']}")
            drift += 1
    media_dir = data.get("media_dir")
    if media_dir and Path(media_dir).is_dir():
        for p in enumerate_assets(Path(media_dir)):
            rel = str(p.relative_to(REPO_ROOT))
            if rel not in known:
                log(f"NEW: {rel}")
                drift += 1
    log(f"checked {len(data.get('assets', []))} entries, drift={drift}")
    return 1 if drift else 0


# ---------------------------------------------------------------------------
# 证据输出
# ---------------------------------------------------------------------------

def write_evidence(output_dir: Path, manifest: Dict[str, Any],
                   command_line: List[str], log_lines: List[str]) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    statuses = manifest["status_counts"]
    blocking = (statuses.get("FAILED", 0) + statuses.get("CONFLICT", 0)
                + statuses.get("UNPROBED", 0))
    status = "PASS" if blocking == 0 else "FAIL"
    result = {
        "task_id": "X-TEST-00",
        "status": status,
        "git_revision": manifest["environment"]["git"]["revision"],
        "generated_at": manifest["generated_at"],
        "manifest_path": str(MANIFEST_PATH),
        "manifest_digest": manifest["manifest_digest"],
        "asset_count": manifest["asset_count"],
        "status_counts": statuses,
        "blocking_definitions": {
            "FAILED": "所有可用后端都未能给出元数据（含 NO_VIDEO_STREAM/打开失败）",
            "CONFLICT": "多个可用后端对同一字段给出不一致值（PROBE_METADATA_CONFLICT）",
            "UNPROBED": "没有任何后端产出可用元数据（如缺 dylib/缺 PyAV）",
        },
        "artifacts": {
            "manifest_copy": str(output_dir / "asset_manifest.json"),
            "environment": str(output_dir / "environment.json"),
            "command_log": str(output_dir / "command.log"),
        },
    }
    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "asset_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "environment.json").write_text(
        json.dumps(manifest["environment"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (output_dir / "command.log").write_text(
        "$ " + " ".join(command_line) + "\n" + "\n".join(log_lines) + "\n",
        encoding="utf-8")

    env = manifest["environment"]
    md = [
        "# X-TEST-00 环境与素材不可变 manifest",
        "",
        f"- 状态：**{status}**（asset_count={manifest['asset_count']}, "
        f"status_counts={statuses}）",
        f"- manifest：`{MANIFEST_PATH}`（digest `{manifest['manifest_digest'][:16]}…`）",
        f"- git：{env['git']['revision'][:12]}（{env['git']['branch']}）",
        f"- OS：{env['platform']['os']} {env['platform']['os_version']} "
        f"({env['platform']['arch']}), Python {env['platform']['python']}",
        f"- GPU：{env['gpu']['status']}"
        + (f" — {', '.join(str(d.get('model')) for d in env['gpu']['devices'])}"
           if env['gpu']['devices'] else
           f"（reason_code={env['gpu']['reason_code']}）"),
        f"- Metal：{env['metal']['status']}"
        + (f"" if env['metal']['status'] == 'AVAILABLE'
           else f"（reason_code={env['metal']['reason_code']}）"),
        f"- GL：{env['gl']['status']}（reason_code={env['gl']['reason_code']}）",
        f"- PyAV：{env['ffmpeg']['pyav']['status']} "
        f"{env['ffmpeg']['pyav'].get('pyav_version', '')}; "
        f"ffmpeg_cli={env['ffmpeg']['ffmpeg_cli']['status']}"
        f"/ffprobe_cli={env['ffmpeg']['ffprobe_cli']['status']}",
        f"- TOPOS codec：{env['topos_codec']['status']}"
        + (f" abi={env['topos_codec'].get('abi_version')}"
           if env['topos_codec']['status'] == 'AVAILABLE' else
           f"（reason_code={env['topos_codec']['reason_code']}）"),
        "",
        "## 条目状态",
        "",
    ]
    for e in manifest["assets"]:
        line = f"- `{e['file']}` — {e['probe_status']}"
        if e["probe_reason_codes"]:
            line += f"（reasons: {', '.join(e['probe_reason_codes'])}）"
        md.append(line)
    (output_dir / "result.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return result


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", type=Path, default=DEFAULT_MEDIA_DIR)
    parser.add_argument("--fast", action="store_true",
                        help="首尾1MB+size 指纹（默认全文件 sha256）")
    parser.add_argument("--check", action="store_true",
                        help="校验已冻结清单（指纹漂移/新增/缺失）")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="证据目录（默认 bench_out/ai_upgrade/X-TEST-00_<ts>）")
    args = parser.parse_args()
    command_line = [sys.executable, *sys.argv[1:]]

    if args.check:
        lines: List[str] = []

        def log(msg: str) -> None:
            print(msg)
            lines.append(msg)
        return check_manifest(MANIFEST_PATH, log)

    if not args.media_dir.is_dir():
        print(f"media dir not found: {args.media_dir}", file=sys.stderr)
        return 2

    ts = time.strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or (REPO_ROOT / "bench_out" / "ai_upgrade"
                                     / f"X-TEST-00_{ts}")
    log_lines: List[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    fingerprint_mode = ("sha256_head1mb_tail1mb+size" if args.fast
                        else "sha256_full")
    manifest = build_manifest(args.media_dir, fingerprint_mode, log)

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    log(f"frozen {manifest['asset_count']} assets -> {MANIFEST_PATH}")

    result = write_evidence(output_dir, manifest, command_line, log_lines)
    log(f"evidence -> {output_dir} (status={result['status']})")
    return 0 if result["status"] == "PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
