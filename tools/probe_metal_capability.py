#!/usr/bin/env python3
"""Metal capability probe for macOS (plan task P1-01 / P1-02 / P1-04).

Answers, on the *current* machine and without touching the app:

  1. Is there a usable Metal device, and what is its feature set?   (P1-01)
  2. Can CVPixelBuffer -> CVMetalTextureCache -> MTLTexture zero-copy?
     For which pixel formats?                                       (P1-02)
  3. Does the CoreVideo interop have to go through ctypes rather
     than PyObjC?                                                    (P1-04)

Run:  python3 tools/probe_metal_capability.py

------------------------------------------------------------
CRITICAL GOTCHA (measured 2026-09-11, cost one false-positive run)
------------------------------------------------------------
CVReturn failure codes are NEGATIVE (kCVReturnInvalidArgument = -6661).
A non-zero int is TRUTHY in Python, so

    tex, err = CVMetalTextureCacheCreateTextureFromImage(...)
    if tex:            # WRONG - -6661 passes this check
        ...            # runs on a failure and prints plausible output

silently reports success on a hard failure. Every CVReturn in this file is
compared with `== 0` explicitly. Keep it that way.

The ctypes path returns 0 and yields valid textures; the PyObjC binding for
CVMetalTextureCacheCreateTextureFromImage returns -6661. Per plan P1-04, the
CoreVideo interop uses ctypes and does not mix FFI layers across the
VideoToolbox decode callback boundary.
"""

from __future__ import annotations

import ctypes
import argparse
import contextlib
import io
import json
import struct
import sys
from typing import Any

from src.shared.media.macos_vt.metal_capability_report import (
    build_report,
    write_report,
)

CV_FRAMEWORK = "/System/Library/Frameworks/CoreVideo.framework/CoreVideo"

# MTLPixelFormat raw values, verified against the macOS SDK header
# (MTLPixelFormat.h). Hard-invalid values make Metal's validation layer
# abort the process, so these must be exact.
MTL_PIXEL_FORMAT_R8_UNORM = 10
MTL_PIXEL_FORMAT_R16_UNORM = 20
MTL_PIXEL_FORMAT_R32_UINT = 53
MTL_PIXEL_FORMAT_RGBA16_UINT = 113
MTL_PIXEL_FORMAT_BGRA8_UNORM = 80
MTL_PIXEL_FORMAT_GBGR422 = 240
MTL_PIXEL_FORMAT_BGRG422 = 241

STORAGE_MODE_NAMES = {0: "Shared", 1: "Managed", 2: "Private", 3: "Memoryless"}


def _load_corevideo() -> ctypes.CDLL:
    cv = ctypes.CDLL(CV_FRAMEWORK)
    V, U, Z = ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t

    cv.CVMetalTextureCacheCreate.restype = ctypes.c_int32
    cv.CVMetalTextureCacheCreate.argtypes = [V, V, V, V, ctypes.POINTER(V)]

    cv.CVMetalTextureCacheCreateTextureFromImage.restype = ctypes.c_int32
    cv.CVMetalTextureCacheCreateTextureFromImage.argtypes = [
        V, V, V, V, U, Z, Z, Z, ctypes.POINTER(V)
    ]

    cv.CVMetalTextureGetTexture.restype = V
    cv.CVMetalTextureGetTexture.argtypes = [V]
    return cv


def probe_device(Metal) -> object | None:
    print("== Metal device (P1-01) ==")
    dev = Metal.MTLCreateSystemDefaultDevice()
    if dev is None:
        print("  FAIL: no Metal device")
        return None
    print(f"  name                : {dev.name()}")
    print(f"  hasUnifiedMemory    : {dev.hasUnifiedMemory()}")
    print(f"  maxThreadsPerGroup  : {dev.maxThreadsPerThreadgroup()}")
    for fam in ("Mac2", "Common3", "Apple7"):
        attr = getattr(Metal, f"MTLGPUFamily{fam}", None)
        if attr is not None:
            print(f"  supportsFamily({fam:<7}): {dev.supportsFamily_(attr)}")
    # Tier 1 lacks nested argument buffers and has low resource ceilings;
    # Apple Silicon is Tier 2. See plan P7-02.
    print(f"  argumentBuffersTier : {dev.argumentBuffersSupport()}  (Apple Silicon = 2)")
    print(f"  maxBufferLength     : {dev.maxBufferLength() / 1e9:.1f} GB")
    return dev


def device_facts(Metal, dev) -> dict[str, Any]:
    """Return JSON-safe device facts after the human-readable probe."""
    facts: dict[str, Any] = {
        "name": str(dev.name()),
        "has_unified_memory": bool(dev.hasUnifiedMemory()),
        "max_threads_per_group": str(dev.maxThreadsPerThreadgroup()),
        "argument_buffers_tier": int(dev.argumentBuffersSupport()),
        "max_buffer_length_bytes": int(dev.maxBufferLength()),
    }
    families: dict[str, bool] = {}
    for family in ("Mac2", "Common3", "Apple7"):
        attr = getattr(Metal, f"MTLGPUFamily{family}", None)
        if attr is not None:
            families[family] = bool(dev.supportsFamily_(attr))
    facts["gpu_families"] = families
    return facts


def probe_compute(Metal, dev) -> dict[str, Any]:
    print("== MSL compile + compute dispatch (P1-01) ==")
    result: dict[str, Any] = {
        "compile": False,
        "dispatch": False,
        "correct": False,
        "status": "failed",
    }
    src = """
    #include <metal_stdlib>
    using namespace metal;
    kernel void addone(device float* io [[buffer(0)]],
                       uint gid [[thread_position_in_grid]]) { io[gid] += 1.0f; }
    """
    lib, err = dev.newLibraryWithSource_options_error_(src, None, None)
    if lib is None:
        print(f"  FAIL compile: {err}")
        result["reason"] = f"msl_compile_failed:{err}"
        return result
    result["compile"] = True
    print("  compile             : OK")

    queue = dev.newCommandQueue()
    pso, perr = dev.newComputePipelineStateWithFunction_error_(
        lib.newFunctionWithName_("addone"), None
    )
    if pso is None:
        print(f"  FAIL pipeline: {perr}")
        result["reason"] = f"compute_pipeline_failed:{perr}"
        return result

    seed = (1.0, 2.0, 3.0, 4.0)
    buf = dev.newBufferWithBytes_length_options_(
        struct.pack("4f", *seed), 16, Metal.MTLResourceStorageModeShared
    )
    cb = queue.commandBuffer()
    enc = cb.computeCommandEncoder()
    enc.setComputePipelineState_(pso)
    enc.setBuffer_offset_atIndex_(buf, 0, 0)
    size = Metal.MTLSizeMake(4, 1, 1)
    enc.dispatchThreads_threadsPerThreadgroup_(size, size)
    enc.endEncoding()
    cb.commit()
    cb.waitUntilCompleted()

    out = struct.unpack("4f", buf.contents().as_buffer(16).tobytes())
    expected = tuple(v + 1.0 for v in seed)
    print(f"  dispatch            : {seed} -> {tuple(round(v, 1) for v in out)}")
    print(f"  correct             : {out == expected}  err={cb.error()}")
    result["dispatch"] = cb.error() is None
    result["correct"] = out == expected
    result["error"] = str(cb.error()) if cb.error() is not None else None
    result["status"] = "supported" if result["dispatch"] and result["correct"] else "failed"
    if not result["dispatch"]:
        result["reason"] = "compute_dispatch_failed"
    elif not result["correct"]:
        result["reason"] = "compute_dispatch_incorrect"
    return result


def probe_cv_interop(Metal, Quartz, dev, cv) -> list[dict[str, Any]]:
    print("== CVPixelBuffer -> CVMetalTextureCache -> MTLTexture (P1-02 / P1-04) ==")
    import objc

    results: list[dict[str, Any]] = []

    dev_ptr = objc.pyobjc_id(dev)
    cache = ctypes.c_void_p(0)
    rc = cv.CVMetalTextureCacheCreate(
        None, None, ctypes.c_void_p(dev_ptr), None, ctypes.byref(cache)
    )
    print(f"  CVMetalTextureCacheCreate rc={rc} (ctype route)")
    if rc != 0:
        print("  FAIL: cache create")
        for label in ("NV12  8-bit", "P010 10-bit", "BGRA  8-bit"):
            results.append({
                "label": label,
                "status": "unknown",
                "reason": f"corevideo_cache_create_failed:{rc}",
            })
        return results

    cases = [
        ("NV12  8-bit", Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
         MTL_PIXEL_FORMAT_R8_UNORM),
        ("P010 10-bit", Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange,
         MTL_PIXEL_FORMAT_R16_UNORM),
        ("BGRA  8-bit", Quartz.kCVPixelFormatType_32BGRA,
         MTL_PIXEL_FORMAT_BGRA8_UNORM),
    ]
    for label, fmt, mfmt in cases:
        status, pb = Quartz.CVPixelBufferCreate(
            None, 1920, 1080, fmt,
            {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {}}, None,
        )
        if status != 0 or pb is None:
            print(f"  {label}: pixel buffer create FAILED status={status}")
            results.append({
                "label": label,
                "status": "unsupported",
                "reason": f"pixel_buffer_create_failed:{status}",
            })
            continue

        iosurface_backed = Quartz.CVPixelBufferGetIOSurface(pb) is not None
        pb_ptr = ctypes.cast(objc.pyobjc_id(pb), ctypes.c_void_p)
        out = ctypes.c_void_p(0)
        rc = cv.CVMetalTextureCacheCreateTextureFromImage(
            None, cache, pb_ptr, None, mfmt, 1920, 1080, 0, ctypes.byref(out)
        )
        # NOTE: `rc == 0`, never `if rc`. -6661 is truthy.
        if rc != 0 or not out.value:
            print(f"  {label}: import FAILED rc={rc}")
            results.append({
                "label": label,
                "status": "unsupported",
                "reason": f"cv_return_unsupported:{rc}",
                "iosurface_backed": iosurface_backed,
            })
            continue

        mtl_ptr = cv.CVMetalTextureGetTexture(ctypes.c_void_p(out.value))
        tex = objc.objc_object(c_void_p=mtl_ptr)
        mode = STORAGE_MODE_NAMES.get(tex.storageMode(), tex.storageMode())
        print(
            f"  {label}: IOSurface={iosurface_backed} -> "
            f"MTLTexture {tex.width()}x{tex.height()} "
            f"fmt={tex.pixelFormat()} storage={tex.storageMode()}({mode}) => ZERO-COPY OK"
        )
        results.append({
            "label": label,
            "status": "supported",
            "iosurface_backed": iosurface_backed,
            "texture": {
                "width": int(tex.width()),
                "height": int(tex.height()),
                "pixel_format": int(tex.pixelFormat()),
                "storage_mode": int(tex.storageMode()),
                "storage_mode_name": mode,
            },
        })
    return results


def probe_cv_interop_422(Metal, Quartz, dev, cv) -> list[dict[str, Any]]:
    """P1-02 remainder: 4:2:2 (8/10-bit) import matrix (plan open question #1).

    ProRes decodes to 4:2:2; this machine has no ProRes HW decoder, so we
    probe with plain CVPixelBuffers of the same pixel formats — what matters
    here is the CVMetalTextureCache format acceptance, not the decoder.
    """
    import objc

    results: list[dict[str, Any]] = []

    dev_ptr = objc.pyobjc_id(dev)
    cache = ctypes.c_void_p(0)
    rc = cv.CVMetalTextureCacheCreate(
        None, None, ctypes.c_void_p(dev_ptr), None, ctypes.byref(cache)
    )
    if rc != 0:
        print(f"  FAIL: cache create rc={rc}")
        return [{
            "label": "422 cache",
            "status": "unknown",
            "reason": f"corevideo_cache_create_failed:{rc}",
        }]

    fmt_2vuy = int.from_bytes(b"2vuy", "big")
    fmt_v210 = int.from_bytes(b"v210", "big")
    # NOTE: candidates whose per-pixel stride exceeds the IOSurface row
    # (R32Uint needs 4B/px = 7680, RGBA16Uint 8B/px; v210 surface = 5120)
    # trip a HARD Metal validation abort (not a CVReturn), killing the
    # process — they cannot be probed in-process and are excluded here.
    cases = [
        ("422 8-bit  (2vuy, 2B/px)", fmt_2vuy, [
            (MTL_PIXEL_FORMAT_GBGR422, "GBGR422"),
            (MTL_PIXEL_FORMAT_BGRG422, "BGRG422"),
        ]),
        ("422 10-bit (v210, 8/3B/px)", fmt_v210, [
            (MTL_PIXEL_FORMAT_GBGR422, "GBGR422(8bit reinterpret)"),
            (MTL_PIXEL_FORMAT_BGRG422, "BGRG422(8bit reinterpret)"),
        ]),
    ]
    for label, fourcc, candidates in cases:
        for mfmt, mname in candidates:
            status, pb = Quartz.CVPixelBufferCreate(
                None, 1920, 1080, fourcc,
                {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {}}, None,
            )
            if status != 0 or pb is None:
                print(f"  {label} as {mname}: pixel buffer create FAILED status={status}")
                results.append({
                    "label": f"{label} as {mname}",
                    "status": "unsupported",
                    "reason": f"pixel_buffer_create_failed:{status}",
                })
                continue
            pb_ptr = ctypes.cast(objc.pyobjc_id(pb), ctypes.c_void_p)
            out = ctypes.c_void_p(0)
            rc = cv.CVMetalTextureCacheCreateTextureFromImage(
                None, cache, pb_ptr, None, mfmt, 1920, 1080, 0, ctypes.byref(out)
            )
            # NOTE: `rc == 0`, never `if rc`. -6661 is truthy.
            if rc == 0 and out.value:
                mtl_ptr = cv.CVMetalTextureGetTexture(ctypes.c_void_p(out.value))
                tex = objc.objc_object(c_void_p=mtl_ptr)
                print(f"  {label} as {mname}: OK -> {tex.width()}x{tex.height()} "
                      f"fmt={tex.pixelFormat()} storage={tex.storageMode()}")
                results.append({
                    "label": f"{label} as {mname}",
                    "status": "supported",
                    "texture": {
                        "width": int(tex.width()),
                        "height": int(tex.height()),
                        "pixel_format": int(tex.pixelFormat()),
                        "storage_mode": int(tex.storageMode()),
                    },
                })
            else:
                print(f"  {label} as {mname}: rc={rc} (unsupported)")
                results.append({
                    "label": f"{label} as {mname}",
                    "status": "unsupported",
                    "reason": f"cv_return_unsupported:{rc}",
                })
    return results


def probe_pyobjc_trap(Metal, Quartz, dev) -> dict[str, Any]:
    """Document the PyObjC binding failure that motivates the ctypes path."""
    print("== PyObjC route (expected to FAIL - motivates P1-04 decision) ==")
    cache, cerr = Quartz.CVMetalTextureCacheCreate(None, None, dev, None, None)
    if cache is None:
        print(f"  cache create failed: {cerr}")
        return {"status": "unusable", "reason": "pyobjc_corevideo_route_unusable"}
    _, pb = Quartz.CVPixelBufferCreate(
        None, 1920, 1080,
        Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
        {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {}}, None,
    )
    result = Quartz.CVMetalTextureCacheCreateTextureFromImage(
        None, cache, pb, None, MTL_PIXEL_FORMAT_R8_UNORM, 1920, 1080, 0, None
    )
    rc = result[0] if isinstance(result, tuple) else result
    if rc == 0:
        print("  rc=0 -> PyObjC route WORKS on this machine (revisit P1-04)")
        return {"status": "supported", "cv_return": int(rc)}
    else:
        print(f"  rc={rc} -> PyObjC route unusable; use ctypes (as planned)")
        return {
            "status": "unusable",
            "cv_return": int(rc),
            "reason": "pyobjc_corevideo_route_unusable",
        }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-json", type= str,
        help="write a machine-readable capability report",
    )
    parser.add_argument(
        "--output-md", type=str,
        help="write the matching human-readable capability report",
    )
    return parser.parse_args(argv)


def _write_probe_report(report: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        write_report(
            report,
            json_path=args.output_json,
            markdown_path=args.output_md,
        )
    except OSError as exc:
        print(f"Failed to write probe report: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    transcript = io.StringIO()
    report: dict[str, Any]
    try:
        import Metal
        import Quartz
    except ImportError as exc:
        print(f"PyObjC Metal/Quartz unavailable: {exc}")
        print("Install: pip install pyobjc-framework-Metal pyobjc-framework-Quartz")
        report = build_report(
            status="unavailable",
            failure_reasons=[f"pyobjc_unavailable:{exc}"],
            error=str(exc),
        )
        return 1 if _write_probe_report(report, args) else 1

    with contextlib.redirect_stdout(transcript):
        print(f"CoreVideo: {CV_FRAMEWORK}\n")
        cv = _load_corevideo()

        dev = probe_device(Metal)
        if dev is None:
            report = build_report(
                status="failed",
                failure_reasons=["metal_device_unavailable"],
                transcript=transcript.getvalue(),
            )
        else:
            print()
            compute = probe_compute(Metal, dev)
            print()
            formats = probe_cv_interop(Metal, Quartz, dev, cv)
            print()
            formats.extend(probe_cv_interop_422(Metal, Quartz, dev, cv))
            print()
            pyobjc_route = probe_pyobjc_trap(Metal, Quartz, dev)

            failures: list[str] = []
            if compute.get("status") != "supported":
                failures.append(str(compute.get("reason", "compute_pipeline_failed")))
            if pyobjc_route.get("status") != "supported":
                failures.append(str(pyobjc_route.get("reason", "pyobjc_corevideo_route_unusable")))
            status = "supported" if not failures else "partial"
            report = build_report(
                status=status,
                device=device_facts(Metal, dev),
                compute=compute,
                format_matrix=formats,
                pyobjc_route=pyobjc_route,
                failure_reasons=failures,
                transcript=transcript.getvalue(),
            )

    print(transcript.getvalue(), end="")
    return _write_probe_report(report, args)


if __name__ == "__main__":
    sys.exit(main())
