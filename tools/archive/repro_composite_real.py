"""真实 CompositeRenderer 环境（QThread + Qt 共享 GL 上下文）复现脚本。

复现用户报告的三个症状（2K H.264 25fps 素材）：
  1. 静止时画面上下颠倒、播放时正常
  2. 播放中偶发插入"其他画面"的帧
  3. 播放感觉卡顿
外加日志中的 VT 桥接 GL texture setup rejected / GpuDecodedFrame 泄漏。

输出经 renderer.readback_output()（渲染线程读回，CPU top-left BGR float32），
与 ffmpeg CLI 地面真值逐帧对比方向与内容。

用法：
  QT_QPA_PLATFORM=cocoa .venv/bin/python tools/repro_composite_real.py [视频路径]
"""
from __future__ import annotations

import os
import sys
import time
import logging
import subprocess
import tempfile

sys.path.insert(0, "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01")

logging.basicConfig(level=logging.WARNING)

import numpy as np


def ffmpeg_ref(path: str, frame: int, w: int, h: int) -> np.ndarray:
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
        tmp = f.name
    try:
        subprocess.run(
            ["/usr/local/bin/ffmpeg", "-y", "-loglevel", "error", "-i", path,
             "-vf", f"select=eq(n\\,{frame})", "-vframes", "1",
             "-pix_fmt", "rgb48le", "-f", "rawvideo", tmp], check=True)
        rgb = (np.fromfile(tmp, dtype="<u2") / 65535.0).reshape(h, w, 3)
        return rgb[:, :, ::-1].copy()  # → BGR 与 readback 一致
    finally:
        os.unlink(tmp)


def main(path: str) -> None:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface, QSurfaceFormat

    app = QApplication.instance() or QApplication([])

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    main_ctx = QOpenGLContext()
    main_ctx.setFormat(fmt)
    main_ctx.create()
    assert main_ctx.makeCurrent(surface)

    from src.features.composite.render.composite_renderer import (
        CompositeRenderer, CompositeCommand)
    from src.features.composite.render.render_scheduler import RenderRequest
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.video_frame_node import VideoFrameNode
    from src.features.composite.nodes.output_node import OutputNode

    renderer = CompositeRenderer(main_ctx)
    renderer.start()
    for _ in range(300):
        if renderer._initialized:
            break
        app.processEvents()
        time.sleep(0.02)

    graph = NodeGraph()
    video = VideoFrameNode("v")
    video.parameters["file_path"] = path
    out = OutputNode("o")
    graph.add_node(video)
    graph.add_node(out)
    graph.connect("v", "image", "o", "image")
    video._open_video(path)
    W, H = video.parameters["video_width"], video.parameters["video_height"]
    FPS = video.parameters["fps"]
    print(f"素材: {path.split('/')[-1]} {W}x{H} @{FPS}fps", flush=True)

    # 模拟真实页面：VideoContext.notify_frame_ready → 主线程立即重求值
    import threading as _th
    notify_evt = _th.Event()
    latest_req = {"f": 0}
    def _on_frame_ready():
        notify_evt.set()
    from src.features.composite.core.video_context import VideoContext
    VideoContext.get_instance().set_frame_ready_handler(_on_frame_ready)

    rid = [0]
    def submit(frame: int) -> None:
        latest_req["f"] = frame
        rid[0] += 1
        snap = graph.snapshot(frame_number=frame, fps=float(FPS))
        req = RenderRequest(snapshot=snap, request_id=rid[0],
                            timestamp=time.monotonic(), render_mode="preview")
        renderer.submit(CompositeCommand.EVALUATE_GRAPH, {"request": req})

    # ── 1. 静止帧方向检查 ──
    submit(100)
    for _ in range(300):  # 等 worker 解码 → notify → 重求值
        app.processEvents()
        if notify_evt.is_set():
            notify_evt.clear()
            submit(100)
            time.sleep(0.15)
            break
        time.sleep(0.01)
    time.sleep(0.3)
    img = renderer.readback_output()
    ref = ffmpeg_ref(path, 100, W, H)
    print(f"[节点状态] current={video._current_frame} load={video._load_state}", flush=True)
    if img is None:
        print("!! 静止帧无输出")
    else:
        d_n = np.abs(img - ref).mean()
        d_f = np.abs(np.flipud(img) - ref).mean()
        verdict = "正常" if d_n < d_f else "上下颠倒"
        print(f"静止帧(100): {verdict} (正向diff={d_n:.4f} 翻转diff={d_f:.4f})", flush=True)
        # 判别路径：CPU packed（过饱和）参照
        video2 = VideoFrameNode("ref-cpu")
        video2.parameters["file_path"] = path
        video2._open_video(path)
        video2._vt_gpu_disabled = True
        cpu = video2._decode_frame(100, "preview")
        if cpu is not None:
            cpu_bgr = cpu[:, :, :3][:, :, ::-1]
            d_cpu_n = np.abs(img - cpu_bgr).mean()
            d_cpu_f = np.abs(np.flipud(img) - cpu_bgr).mean()
            verdict = ("CPU packed 路径" if d_cpu_n < 0.05
                       else "与两参照等距（合成链恒定色变换，无法区分路径）")
            print(f"  vs CPU-packed 参照: 正向={d_cpu_n:.4f} 翻转={d_cpu_f:.4f} "
                  f"（{verdict}）", flush=True)
        video2.cleanup()

    # 渲染器自己的节点实例状态 + 静止帧 PNG
    rn = renderer._render_nodes.get("v")
    if rn is not None:
        print(f"[渲染器节点] current={rn._current_frame} load={rn._load_state}", flush=True)
    if img is not None:
        import struct, zlib
        h_, w_ = img.shape[:2]
        rgb = np.clip(img[:, :, ::-1], 0, 1)
        img8 = (rgb * 255).astype(np.uint8)
        raw = b''.join(b'\x00' + img8[y].tobytes() for y in range(h_))
        def chunk(t, d):
            c = struct.pack('>I', len(d)) + t + d
            return c + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w_, h_, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
        open("/tmp/real_static.png", "wb").write(png)
        print("saved /tmp/real_static.png", flush=True)

    # ── 1b. 对照：强制渲染器节点走 CPU 路径再渲染同一帧 ──
    import os as _os2
    if rn is not None and hasattr(rn, "_vt_gpu_disabled") and not _os2.environ.get("TC_BASELINE"):
        rn._vt_gpu_disabled = True
        rn._clear_cache()
        submit(101)
        for _ in range(300):
            app.processEvents()
            if notify_evt.is_set():
                notify_evt.clear()
                submit(101)
                time.sleep(0.15)
                break
            time.sleep(0.01)
        time.sleep(0.3)
        img2 = renderer.readback_output()
        ref101 = ffmpeg_ref(path, 101, W, H)
        if img2 is not None:
            d_n = np.abs(img2 - ref101).mean()
            d_f = np.abs(np.flipud(img2) - ref101).mean()
            print(f"CPU路径帧(101): {'正常' if d_n < d_f else '上下颠倒'} "
                  f"(正向={d_n:.4f} 翻转={d_f:.4f})", flush=True)
        # 同帧 VT vs CPU 相对方向对比（决定性实验）
        img_cpu = img2.copy()
        rn._vt_gpu_disabled = False
        rn._clear_cache()
        submit(101)
        t0w = time.perf_counter()
        while time.perf_counter() - t0w < 6.0:
            app.processEvents()
            if notify_evt.is_set():
                notify_evt.clear()
                submit(101)
            if getattr(rn, "_current_frame", -1) == 101 and rn.is_ready:
                time.sleep(0.15)
                break
            time.sleep(0.01)
        img_vt = renderer.readback_output()
        if img_vt is not None and rn._vt_bridge is not None:
            same = np.abs(img_vt - img_cpu).mean()
            opp = np.abs(np.flipud(img_vt) - img_cpu).mean()
            print(f"同帧(101) VT vs CPU: 同向diff={same:.4f} 反向diff={opp:.4f} → "
                  f"{'方向一致' if same < opp else '方向相反!!'} "
                  f"内容差={min(same, opp):.4f}", flush=True)
        else:
            print(f"VT 重渲染失败 (bridge={rn._vt_bridge is not None})", flush=True)

    # ── 2. 播放 3 秒：方向翻转帧 / 外来画面 / 帧率 ──
    import os as _os
    if _os.environ.get("TC_FORCE_CPU"):
        rn = renderer._render_nodes.get("v")
        if rn is not None:
            rn._vt_gpu_disabled = True
            rn._clear_cache()
    print("播放模拟 75 帧…", flush=True)
    displayed, flashes = 0, 0
    prev_sample = None
    t_start = time.perf_counter()
    playhead, next_t = 101, time.perf_counter()
    last_sample_t = 0.0
    samples = []
    mut_meta = []
    while playhead < 176:
        now = time.perf_counter()
        if now >= next_t:
            submit(playhead)
            playhead += 1
            next_t += 1.0 / FPS
        app.processEvents()
        if notify_evt.is_set():
            notify_evt.clear()
            submit(latest_req["f"])
        if time.perf_counter() - last_sample_t > 0.08:
            img = renderer.readback_output(timeout=1.0)
            if img is not None:
                small = img[::4, ::4, :3]
                if prev_sample is not None:
                    d = np.abs(small - prev_sample).mean()
                    # 相邻采样(2帧间隔)正常应连续；突变=闪帧/垃圾帧
                    if d > 0.12:
                        flashes += 1
                        print(f"  t+{time.perf_counter()-t_start:.2f}s: 内容突变 diff={d:.3f} "
                              f"req={latest_req['f']} playhead={playhead} shown={video._current_frame}")
                        import struct, zlib
                        def _png(arr8, name):
                            h_, w_ = arr8.shape[:2]
                            raw = b''.join(b'\x00' + arr8[y].tobytes() for y in range(h_))
                            def chunk(t, dd):
                                c = struct.pack('>I', len(dd)) + t + dd
                                return c + struct.pack('>I', zlib.crc32(t + dd) & 0xffffffff)
                            open(name, "wb").write(
                                b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w_, h_, 8, 2, 0, 0, 0))
                                + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
                        _png((np.clip(prev_sample, 0, 1) * 255).astype(np.uint8), f"/tmp/flash_{flashes}_prev.png")
                        _png((np.clip(small, 0, 1) * 255).astype(np.uint8), f"/tmp/flash_{flashes}_cur.png")
                        mut_meta.append({"idx": flashes, "req": latest_req["f"],
                                         "ph": playhead, "shown": video._current_frame})
                prev_sample = small
                samples.append((time.perf_counter(), small.mean()))
                displayed += 1
                last_sample_t = time.perf_counter()
        time.sleep(0.001)
    elapsed = time.perf_counter() - t_start
    print(f"播放 {elapsed:.1f}s: 采样 {displayed} 次, 内容突变(闪帧) {flashes}")

    # ── 突变帧溯源：与 ffmpeg 逐帧真值最优匹配 ──
    for m in mut_meta:
        best = (9.9, None, 9.9, 9.9)
        cur = np.frombuffer(
            open(f"/tmp/flash_{m['idx']}_cur.png", "rb").read(), dtype=np.uint8)  # 占位，下面用解码
        import zlib as _zl
        def _load_png(p):
            data = open(p, "rb").read()
            pos, idat = 8, b""
            while pos < len(data):
                ln = int.from_bytes(data[pos:pos+4], "big")
                if data[pos+4:pos+8] == b"IHDR":
                    w_, h_ = int.from_bytes(data[pos+8:pos+12], "big"), int.from_bytes(data[pos+12:pos+16], "big")
                if data[pos+4:pos+8] == b"IDAT":
                    idat += data[pos+8:pos+8+ln]
                pos += 12 + ln
            raw = _zl.decompress(idat)
            stride = w_ * 3 + 1
            return np.frombuffer(b"".join(raw[y*stride+1:(y+1)*stride] for y in range(h_)),
                                 dtype=np.uint8).reshape(h_, w_, 3).astype(np.float32) / 255
        cur = _load_png(f"/tmp/flash_{m['idx']}_cur.png")
        for fr in range(max(0, m["req"] - 12), m["req"] + 3):
            ref = ffmpeg_ref(path, fr, W, H)[::4, ::4, :]
            raw = np.abs(cur - ref).mean()
            # 通道均值对齐后的残差：剔除全局色偏后看内容是否吻合
            align = np.abs((cur - cur.mean(axis=(0, 1))) - (ref - ref.mean(axis=(0, 1)))).mean()
            cast = np.abs(cur.mean(axis=(0, 1)) - ref.mean(axis=(0, 1))).mean()
            if align < best[0]:
                best = (align, fr, raw, cast)
        print(f"  突变#{m['idx']} req={m['req']} "
              f"→ 最佳匹配帧 {best[1]} (内容残差={best[0]:.4f} 原始diff={best[2]:.4f} 色偏={best[3]:.4f})")

    renderer.stop_thread()
    print("done", flush=True)


if __name__ == "__main__":
    root = "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/tests/test_videos"
    p = sys.argv[1] if len(sys.argv) > 1 else f"{root}/2K_h264.mp4"
    main(p)
