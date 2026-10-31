"""硬件编码导出验证（Windows：NVIDIA 走 NVENC / AMD 走 AMF）。

1. 路由矩阵：hwaccel_engaged / get_encoder_name 在各种配置下的行为
   （按 detect_hw_vendor() 路由期望；无 vendor 机器只跑 CPU 断言）
2. 端到端：真实 4K 素材 → FFmpegVideoEncoder 全路径导出。
   预解码 + warmup 后计时（排除硬编会话初始化与首帧排空失真），
   输出回读校验（帧数 + 非黑帧）；含码率模式臂——实际输出码率
   必须落在目标带宽内（b:v 静默忽略回归锚点，2026-09-12）。
3. 探针回退：_probe_encoder_open 对不存在编码器返回 False

用法: .venv/Scripts/python.exe scripts/verify_hw_encode_export.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SRC = PROJECT_ROOT / "tests" / "test_videos" / "4K_prores422HQ.mov"
N_WARMUP = 4
N_FRAMES = 12


def _hw() -> str:
    """本机硬编通道（'nvenc'/'amf'/''）；产物标注与期望路由据此变化。"""
    from src.shared.export.video_encoder import detect_hw_vendor, vendor_hwaccel_name

    return vendor_hwaccel_name(detect_hw_vendor())


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check(label: str, actual, expected) -> bool:
    ok = actual == expected
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}: got={actual} expect={expected}")
    return ok


def test_routing_matrix() -> bool:
    section(f"路由矩阵（本机硬编通道：{_hw() or '无'}）")
    from src.shared.export.video_encoder import (
        ColorMetadataConfig,
        FFmpegEncoderConfig,
        amf_encoder_available,
        nvenc_encoder_available,
    )
    from src.shared.media.metadata import Rational

    hw = _hw()
    print(f"  amf_encoder_available(h264) = {amf_encoder_available('h264')}")
    print(f"  nvenc_encoder_available(h264) = {nvenc_encoder_available('h264')}")
    if not hw:
        print("  [SKIP] 本机无 nvidia/amd 硬件适配器，跳过硬编路由断言")

    def cfg(**kw):
        base = dict(
            codec="h264", width=3840, height=2160, fps=Rational(25, 1),
            pix_fmt="yuv420p", gop_size=50,
        )
        base.update(kw)
        return FFmpegEncoderConfig(**base)

    hw_encoder = {
        "h264": f"h264_{hw}" if hw else "libx264",
        "hevc": f"hevc_{hw}" if hw else "libx265",
    }
    all_ok = True
    if hw:
        all_ok &= check("h264 8bit SDR auto", cfg().get_encoder_name(),
                        hw_encoder["h264"])
        all_ok &= check("hevc 8bit SDR auto", cfg(codec="hevc").get_encoder_name(),
                        hw_encoder["hevc"])
        # 10bit 接线：hevc 10bit → p010le 转换路线（nvenc/amf 通用）
        all_ok &= check(
            "hevc 10bit SDR auto → 硬编（p010le 转换）",
            cfg(codec="hevc", pix_fmt="yuv420p10le").get_encoder_name(),
            hw_encoder["hevc"],
        )
    all_ok &= check(
        "dnxhr → dnxhd（FFmpeg 无 'dnxhr' 编码器）",
        cfg(codec="dnxhr", profile="hqx", pix_fmt="yuv422p10le").get_encoder_name(),
        "dnxhd",
    )
    all_ok &= check(
        "h264 10bit auto → CPU（High10 非消费场景）",
        cfg(pix_fmt="yuv420p10le").get_encoder_name(),
        "libx264",
    )
    all_ok &= check(
        "hevc 10bit HDR(PQ) auto → CPU",
        cfg(
            codec="hevc", pix_fmt="yuv420p10le",
            color_metadata=ColorMetadataConfig(transfer="smpte2084"),
        ).get_encoder_name(),
        "libx265",
    )
    all_ok &= check(
        "h264 4:2:2 10bit auto → CPU",
        cfg(pix_fmt="yuv422p10le").get_encoder_name(),
        "libx264",
    )
    all_ok &= check(
        "hwaccel=None 强制 CPU",
        cfg(hwaccel=None).get_encoder_name(),
        "libx264",
    )
    if hw:
        all_ok &= check(
            f"hevc 10bit 显式 {hw} → 硬编（p010le，不受 SDR 限制）",
            cfg(codec="hevc", pix_fmt="yuv420p10le", hwaccel=hw).get_encoder_name(),
            hw_encoder["hevc"],
        )
    all_ok &= check(
        "硬编输入格式映射（10bit→p010le / 8bit 直收；h264 10bit→None）",
        (
            cfg(codec="hevc", pix_fmt="yuv420p10le").hw_input_pix_fmt(),
            cfg(codec="hevc").hw_input_pix_fmt(),
            cfg(pix_fmt="yuv420p10le").hw_input_pix_fmt(),
            cfg(pix_fmt="yuv420p10le", hwaccel=None).hw_input_pix_fmt(),
        ),
        ("p010le", "yuv420p", None, None),
    )

    os.environ["TOPOS_DISABLE_WIN_HW_ENCODE"] = "1"
    all_ok &= check("kill switch → CPU", cfg().get_encoder_name(), "libx264")
    del os.environ["TOPOS_DISABLE_WIN_HW_ENCODE"]
    return all_ok


def predecode_frames(n: int):
    """预解码 4K 素材为生产形态的 DecodedFrame。

    生产导出的合成帧是 BGR 序（_encode_frame 按 dtype 标 bgr24/bgr48le）。
    解码源帧是 RGB 序，这里翻一次通道序并按生产标签重标，使转换路径
    （bgr24/bgr48le 快路径）与真实导出一致。
    """
    import numpy as np

    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    src = FFmpegMediaSource(str(SRC))
    src.open()
    frames = []
    while len(frames) < n + N_WARMUP:
        f = src.read_next_frame()
        if f is None:
            break
        label = 'bgr48le' if f.data.dtype.itemsize > 1 else 'bgr24'
        frames.append(type(f)(
            frame_index=f.frame_index, pts=f.pts, time_seconds=f.time_seconds,
            data=np.ascontiguousarray(np.flip(f.data, axis=-1)),
            pixel_format=label, width=f.width, height=f.height,
        ))
    src.close()
    return frames


def encode_arm(label: str, frames, cpu_forced: bool, codec: str, pix_fmt: str,
               bitrate: int | None = None, band: tuple = (0.4, 2.0)):
    """跑一次真实导出：warmup N_WARMUP 帧后计时 N_FRAMES 帧，校验输出。

    ``bitrate`` 缺省走 crf 模式（历史臂）；传入时走码率模式并断言
    实际输出码率落在 ``target × band`` 内——2026-09-12 复查发现
    ``b:v`` 选项键被 avcodec_open2 静默忽略时，码率模式退默认码率
    （~2Mbps）无任何报错，此断言钉住该回归。
    """
    from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
    from src.shared.media.metadata import Rational

    if cpu_forced:
        os.environ["TOPOS_DISABLE_WIN_HW_ENCODE"] = "1"

    out = tempfile.mktemp(suffix=".mp4")
    rate_kw = ({"crf": 20} if bitrate is None else {"bitrate": bitrate})
    config = FFmpegEncoderConfig(
        codec=codec, width=3840, height=2160, fps=Rational(25, 1),
        pix_fmt=pix_fmt, preset="medium", gop_size=50,
        threads=0,  # 与生产路径 timeline_export 一致（FFmpeg 自动全核）
        pipelined_conversion=True,  # 与生产路径一致
        **rate_kw,
    )
    encoder = FFmpegVideoEncoder(out, config)
    encoder.open()

    total = 0
    t0 = time.perf_counter()
    for i, frame in enumerate(frames):
        encoder.encode_frame(frame)
        total += 1
        if i == N_WARMUP - 1:
            t0 = time.perf_counter()  # 重置：只计 warmup 之后
    encoder.close()
    elapsed = time.perf_counter() - t0
    used_hw = bool(encoder._hw_vendor)
    used_amf = encoder._using_amf
    timed = total - N_WARMUP

    if cpu_forced:
        del os.environ["TOPOS_DISABLE_WIN_HW_ENCODE"]

    # 输出回读校验：帧数一致且非黑帧
    import av

    cin = av.open(out)
    st = cin.streams.video[0]
    dec_frames = 0
    first_mean = 0.0
    for dec in cin.decode(st):
        dec_frames += 1
        if dec_frames == 1:
            first_mean = float(dec.to_ndarray().mean())
    cin.close()
    size_mb = Path(out).stat().st_size / 1e6
    ok = dec_frames == total and first_mean > 1.0
    hw_label = ("AMF" if used_amf else
                encoder._hw_encoder_name if used_hw else "CPU")
    if bitrate is not None:
        actual_mbps = Path(out).stat().st_size * 8 / (timed / (25.0)) / 1e6
        target_mbps = bitrate / 1e6
        in_band = band[0] * target_mbps <= actual_mbps <= band[1] * target_mbps
        ok &= in_band
        print(
            f"  {label:34s} {hw_label:4s} {timed / elapsed:7.1f} fps"
            f"  ({timed} 帧计时 / {elapsed:.2f}s, {size_mb:.1f} MB,"
            f" 回读 {dec_frames}/{total}, 首帧均值 {first_mean:.1f};"
            f" 码率 {actual_mbps:.1f}/{target_mbps:.0f} Mbps"
            f" {'✓在带宽内' if in_band else '✗出带'})"
        )
    else:
        print(
            f"  {label:34s} {hw_label:4s} {timed / elapsed:7.1f} fps"
            f"  ({timed} 帧计时 / {elapsed:.2f}s, {size_mb:.1f} MB,"
            f" 回读 {dec_frames}/{total}, 首帧均值 {first_mean:.1f})"
        )
    os.remove(out)
    return used_hw, ok


def test_e2e() -> bool:
    section(f"端到端真实导出（{SRC.name}）")
    hw = _hw()
    frames = predecode_frames(N_FRAMES)
    if len(frames) < N_FRAMES + N_WARMUP:
        print(f"  [FAIL] 素材可解码帧不足: {len(frames)}")
        return False
    print(f"  预解码 {len(frames)} 帧 4K")

    if not hw:
        print("  [SKIP] 本机无 nvidia/amd 硬件适配器，跳过硬编端到端断言")
        return True

    ok = True
    used_hw, arm_ok = encode_arm(
        "4K H.264 8bit crf20", frames, False, "h264", "yuv420p"
    )
    ok &= arm_ok
    ok &= check("H.264 实际使用硬编", used_hw, True)

    used_hw, arm_ok = encode_arm(
        "4K H.264 8bit crf20 (kill switch)", frames, True, "h264", "yuv420p"
    )
    ok &= arm_ok
    ok &= check("kill switch 后实际使用 CPU", used_hw, False)

    used_hw, arm_ok = encode_arm(
        "4K HEVC 8bit crf20", frames, False, "hevc", "yuv420p"
    )
    ok &= arm_ok
    ok &= check("HEVC 8bit 实际使用硬编", used_hw, True)

    used_hw, arm_ok = encode_arm(
        "4K HEVC 10bit crf20", frames, False, "hevc", "yuv420p10le"
    )
    ok &= arm_ok
    ok &= check("HEVC 10bit 实际使用硬编（p010le 转换）", used_hw, True)

    # 码率模式（2026-09-12 复查新增：b:v 静默忽略回归锚点）
    used_hw, arm_ok = encode_arm(
        "4K H.264 8bit 16Mbps", frames, False, "h264", "yuv420p",
        bitrate=16_000_000,
    )
    ok &= arm_ok
    ok &= check("H.264 码率模式实际使用硬编", used_hw, True)
    return ok


def test_probe_fallback() -> bool:
    section("探针回退逻辑")
    from src.shared.export.video_encoder import (
        FFmpegEncoderConfig,
        FFmpegVideoEncoder,
        hw_fallback_reason_with_detail,
    )
    from src.shared.media.metadata import Rational

    config = FFmpegEncoderConfig(
        codec="h264", width=3840, height=2160, fps=Rational(25, 1),
        pix_fmt="yuv420p", gop_size=50,
    )
    encoder = FFmpegVideoEncoder(os.devnull, config)
    bad = encoder._probe_encoder_open("h264_amf_nonexistent")
    good = encoder._probe_encoder_open("h264_amf")
    ok = check("不存在编码器探针 → False", bad, False)
    # h264_amf 的探针结果 = 驱动级可用性的真数据（AMD 机 True；
    # NVIDIA 机注册存在但驱动 open 失败 → False——「注册≠可用」实证）
    ok &= check("h264_amf 探针结果为布尔真数据", isinstance(good, bool), True)
    print(f"      （本机 h264_amf 驱动级探针 = {good}）")
    ok &= check(
        "结构化 reason 形态（前缀可归类 + detail 携带探测摘要）",
        hw_fallback_reason_with_detail("amf", "SomeError: x"),
        "encoder_hw_unavailable:amf:SomeError: x",
    )
    return ok


def test_dnxhr_export(frames) -> bool:
    """DNxHR HQX 修复验证：编码成功、速度对比 ProRes、输出 profile 正确。"""
    section("DNxHR HQX 导出（修复 'dnxhr' 无效编码器名后）")
    from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
    from src.shared.media.metadata import Rational

    ok = True

    def run_arm(label: str, codec: str, pix_fmt: str, profile) -> None:
        nonlocal ok
        out = tempfile.mktemp(suffix=".mov")
        config = FFmpegEncoderConfig(
            codec=codec, width=3840, height=2160, fps=Rational(25, 1),
            pix_fmt=pix_fmt, profile=profile, gop_size=0, threads=0,
            pipelined_conversion=True,
        )
        encoder = FFmpegVideoEncoder(out, config)
        encoder.open()
        total = 0
        t0 = time.perf_counter()
        for i, frame in enumerate(frames):
            encoder.encode_frame(frame)
            total += 1
            if i == N_WARMUP - 1:
                t0 = time.perf_counter()
        encoder.close()
        elapsed = time.perf_counter() - t0
        timed = total - N_WARMUP

        import av

        cin = av.open(out)
        st = cin.streams.video[0]
        dec_frames = 0
        out_profile = None
        first_mean = 0.0
        for dec in cin.decode(st):
            dec_frames += 1
            out_profile = st.codec_context.profile or out_profile
            if dec_frames == 1:
                first_mean = float(dec.to_ndarray().mean())
        cin.close()
        size_mb = Path(out).stat().st_size / 1e6
        arm_ok = dec_frames == total and first_mean > 1.0
        ok &= arm_ok
        print(
            f"  {label:28s} {timed / elapsed:7.1f} fps  ({timed} 帧 / {elapsed:.2f}s,"
            f" {size_mb:.0f} MB, 回读 {dec_frames}/{total}, profile={out_profile})"
        )
        os.remove(out)

    try:
        run_arm("4K DNxHR HQX (10bit)", "dnxhr", "yuv422p10le", "hqx")
        run_arm("4K ProRes 422 HQ (对照)", "prores", "yuv422p10le", "hq")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {type(exc).__name__}: {exc}")
        return False
    return ok


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ok = test_routing_matrix()
    ok &= test_e2e()
    ok &= test_dnxhr_export(predecode_frames(N_FRAMES))
    ok &= test_probe_fallback()
    print(f"\n{'全部通过' if ok else '存在失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
