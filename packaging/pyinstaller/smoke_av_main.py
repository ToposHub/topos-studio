"""PyInstaller 打包冒烟入口（W7-03）：验证 av.libs/ 布局在打包产物内真实解析。

三步全是显式标记，供父进程断言：
1. import av（delvewheel 重定位的 FFmpeg DLL 是否随 av.libs/ 布局加载）；
2. FFmpeg 全链路 roundtrip（libx264 编 4 帧 → 回解 4 帧）；
3. 任一步失败：打印显式诊断并以非零码退出（「扩展缺失时程序仍可启动
   并显式回退」的 micro 形态），绝不静默。
"""
import sys


def main() -> int:
    print("[smoke] step 1: import av", flush=True)
    try:
        import av  # noqa: PLC0415
        import numpy  # noqa: F401,PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] FAIL import: {type(exc).__name__}: {exc}", flush=True)
        return 2
    print(f"[smoke] av {av.__version__} imported", flush=True)

    print("[smoke] step 2: encode/decode roundtrip", flush=True)
    try:
        import pathlib
        import tempfile

        p = pathlib.Path(tempfile.gettempdir()) / "smoke_av_roundtrip.mp4"
        c = av.open(str(p), "w")
        s = c.add_stream("libx264", rate=24)
        s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
        for i in range(4):
            f = av.VideoFrame(320, 240, format="yuv420p")
            f.planes[0].update(bytes([(i * 8) % 256]) * (240 * 320))
            f.planes[1].update(bytes([128]) * (120 * 160))
            f.planes[2].update(bytes([128]) * (120 * 160))
            for pkt in s.encode(f):
                c.mux(pkt)
        for pkt in s.encode():
            c.mux(pkt)
        c.close()
        n = 0
        with av.open(str(p)) as cin:
            for _ in cin.decode(video=0):
                n += 1
        print(f"[smoke] roundtrip frames={n}", flush=True)
        if n != 4:
            print("[smoke] FAIL roundtrip frame count", flush=True)
            return 3
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] FAIL ffmpeg: {type(exc).__name__}: {exc}", flush=True)
        return 3

    print("[smoke] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
