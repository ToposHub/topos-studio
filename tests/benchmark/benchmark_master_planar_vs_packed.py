"""基准：调色页 master 解码 packed RGB vs planar YUV 输出。

背景：SourceManager 的 master 源以 preserve_planes=False 打开（packed RGB），
4K 10-bit ProRes/DNxHR 解码后要走 swscale 单线程转 rgb48le（4096x2160x3
uint16 ≈ 53MB/帧），这是拖拽预览的 CPU 瓶颈。planar 模式保留 YUV planes
（无 target_size 时零 reformat），由渲染器 YUVGPUUploader 在 GPU 做色彩
转换。

用法：
    .venv/bin/python tests/benchmark/benchmark_master_planar_vs_packed.py
"""

import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("TOPOS_CONFIG_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".topos", "config.json"
))

from src.shared.media.factory import MediaSourceFactory  # noqa: E402

VIDEO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_videos")

FILES = [
    "4K_prores422HQ.mov",
    "4K_DNxHR HQX 12-bit.mov",
    "2K_prores422HQ.mov",
]


def run_pattern(source, frames, label):
    """按给定帧序列取帧，返回 (总数, 每帧耗时ms 列表)。"""
    lat = []
    for idx in frames:
        t0 = time.perf_counter()
        f = source.read_frame(idx)
        dt = (time.perf_counter() - t0) * 1000.0
        if f is None:
            raise RuntimeError(f"{label}: get_frame({idx}) -> None")
        lat.append(dt)
    return lat


def main():
    rng = random.Random(42)
    for name in FILES:
        path = os.path.join(VIDEO_DIR, name)
        if not os.path.isfile(path):
            print(f"!! 缺少素材: {name}")
            continue

        packed = MediaSourceFactory.create(path, preserve_planes=False, auto_open=False)
        packed.open()
        planar = MediaSourceFactory.create(path, preserve_planes=True, auto_open=False)
        planar.open()

        total = int(packed.probe().total_frames)
        planar_mode = bool(getattr(planar, "is_planar_mode", False))

        # 拖拽模式：随机起点 + 短步进序列（模拟拖拽中的局部顺序 walk + 跳变）
        seq = []
        pos = rng.randrange(0, max(1, total // 4))
        for _ in range(120):
            seq.append(pos % total)
            if rng.random() < 0.35:
                pos += rng.randrange(40, 400)  # 跳变
            else:
                pos += rng.randrange(1, 6)  # 局部步进

        lat_packed = run_pattern(packed, seq, f"{name}/packed")
        lat_planar = run_pattern(planar, seq, f"{name}/planar")

        def stats(lat):
            return (
                statistics.median(lat),
                sorted(lat)[int(len(lat) * 0.95)],
                1000.0 / statistics.mean(lat),
            )

        p50_p, p95_p, ups_p = stats(lat_packed)
        p50_l, p95_l, ups_l = stats(lat_planar)

        meta = packed.probe()
        print(f"\n== {name}  {meta.width}x{meta.height} {meta.codec_name} "
              f"{meta.pixel_format} (planar_mode={planar_mode})")
        print(f"   packed rgb48le : p50={p50_p:7.1f}ms p95={p95_p:7.1f}ms  {ups_p:6.1f} fps")
        print(f"   planar yuv     : p50={p50_l:7.1f}ms p95={p95_l:7.1f}ms  {ups_l:6.1f} fps")
        print(f"   提速           : x{ups_l / ups_p:.2f}")

        packed.close()
        planar.close()


if __name__ == "__main__":
    main()
