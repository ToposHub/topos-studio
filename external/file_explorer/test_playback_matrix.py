"""file_explorer ToposPlayer 全素材播放测试。

阶段1：8 个源格式各取前 240 帧编码为 .topos（ingest 路径冒烟）
阶段2：10 个 .topos（2 现成 + 8 新编码）逐一无头播放：
  - 纯解码吞吐（frame_rgb）
  - UI 显示帧率（背压节奏，应 ≈ 源 fps）
  - seek 烟测 + play_failed 采集
判定：显示帧率 ≥ 源 fps×0.9 → 实时 PASS
"""
import os, sys, time, glob
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# [U10] 相对定位：此前硬编码旧仓库绝对路径（/Users/testuser/Documents/vscode/
# topos），换仓库/换机器即不可运行。file_explorer 位于 <repo>/external/ 下
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

OUT = "/tmp/topos_playtest"
os.makedirs(OUT, exist_ok=True)
SEG_FRAMES = 240

SOURCES = sorted(glob.glob(f"{REPO}/tests/test_videos/*.mp4") +
                 glob.glob(f"{REPO}/tests/test_videos/*.mov"))
SOURCES = [f for f in SOURCES if "_topos" not in os.path.basename(f)]

# ---------------- 阶段1：编码段 ----------------
def encode_segment(src: str, out: str, frames: int) -> None:
    import av
    from src.shared.media.metadata import Rational
    from src.shared.export.video_encoder import ColorMetadataConfig, FFmpegEncoderConfig
    from src.shared.export.topos_encoder import ToposVideoEncoder
    c = av.open(src)
    s = c.streams.video[0]
    fps = float(s.average_rate or 25)
    s.thread_type = "AUTO"
    w, h = s.codec_context.width, s.codec_context.height
    cfg = FFmpegEncoderConfig(codec="topos", width=w, height=h,
                              fps=Rational(int(round(fps * 1000)), 1000),
                              pix_fmt="bgr24", profile="standard",
                              color_metadata=ColorMetadataConfig.sdr(), has_alpha=False)
    enc = ToposVideoEncoder(out, cfg, slice_threads=16)
    enc.open()
    n = 0
    for frame in c.decode(s):
        if n >= frames:
            break
        bgr = frame.reformat(format="bgr24").to_ndarray()
        f = type("F", (), {})()
        f.data = bgr; f.height, f.width = h, w; f.pixel_format = "bgr24"
        enc.encode_frame(f)
        n += 1
    enc.close(); c.close()
    print(f"  编码 {os.path.basename(src)} → {n} 帧", flush=True)

print("=== 阶段1：源素材编码为 .topos（前 240 帧） ===", flush=True)
targets = []
for src in SOURCES:
    base = os.path.basename(src)
    out = os.path.join(OUT, base.rsplit(".", 1)[0] + "_240f.topos")
    if not (os.path.exists(out) and os.path.getsize(out) > 1_000_000):
        encode_segment(src, out, SEG_FRAMES)
    targets.append(out)
targets += [f"{REPO}/tests/test_videos/2K_topos.mov",
            f"{REPO}/tests/test_videos/4K_topos.mov"]

# ---------------- 阶段2：播放矩阵 ----------------
print("\n=== 阶段2：ToposPlayer 无头播放矩阵 ===", flush=True)
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, QEventLoop
app = QApplication([])
from plugins.video_pro.topos_player import ToposPlayer

def wait_ms(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

def spin_until(cond, timeout_s):
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if cond():
            return True
        wait_ms(30)
    return cond()

results = []
p = ToposPlayer(); p.resize(1280, 720)
failed_msgs = []
p.play_failed.connect(lambda m: failed_msgs.append(m))

for path in targets:
    name = os.path.basename(path)
    row = {"file": name, "errors": []}
    try:
        from plugins.video_pro.topos_player import ToposMovieSession
        s = ToposMovieSession(path)
        w, h, n, fps = s.width, s.height, s.frame_count, s.fps
        row.update(res=f"{w}x{h}", fps=fps, frames=n)
        # 纯解码吞吐（≤60 帧）
        probe = min(60, n)
        t0 = time.perf_counter()
        for i in range(probe):
            s.frame_rgb(i)
        t_dec = (time.perf_counter() - t0) / probe
        s.close()
        row["decode_fps"] = 1.0 / t_dec

        # UI 播放：测到 90% 进度的时间
        failed_msgs.clear()
        p.play_video(path)
        target = max(4, int(n * 0.9))
        t0 = time.perf_counter()
        ok = spin_until(lambda: p._shown_index >= target,
                        timeout_s=min(n / fps * 1.6 + 8, 40))
        t90 = time.perf_counter() - t0
        row["display_fps"] = target / t90 if ok else (p._shown_index / t90 if t90 else 0)
        row["played_ok"] = ok
        row["errors"] += list(failed_msgs)

        # seek 烟测（中部）
        mid = n // 2
        p.toggle_play() if p._playing else None
        p._request_seek(mid)
        ok_seek = spin_until(lambda: p._shown_index == mid, timeout_s=3)
        row["seek_ok"] = ok_seek and p._shown_index == mid
        img = p.canvas.current_image()
        row["frame_ok"] = img is not None and not img.isNull()
        p.stop_decoder()
        rt = row["display_fps"] >= fps * 0.9
        row["realtime"] = rt
        status = "✅" if (rt and row["seek_ok"] and row["frame_ok"] and not row["errors"]) else "❌"
        print(f"{status} {name:34s} {row['res']:9s} {fps:4.0f}fps目标 | "
              f"解码 {row['decode_fps']:5.1f}fps | 显示 {row['display_fps']:5.1f}fps | "
              f"seek={'√' if row['seek_ok'] else '×'} | "
              f"{'实时' if rt else '不实时'}{' | ' + '; '.join(row['errors']) if row['errors'] else ''}",
              flush=True)
    except Exception as e:
        import traceback; traceback.print_exc()
        row["errors"].append(str(e))
        print(f"❌ {name}: 异常 {e}", flush=True)
    results.append(row)

p.stop_decoder()
print("\n=== 汇总 ===")
n_ok = sum(1 for r in results if r.get("realtime") and r.get("seek_ok")
           and r.get("frame_ok") and not r.get("errors"))
print(f"{n_ok}/{len(results)} 通过（实时 + seek + 无错误）")
