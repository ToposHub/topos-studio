"""临时诊断脚本：复现 AudioPlaybackThread 播放循环，验证音频断裂/杂音假设。

模拟路径与 audio_playback.py 完全一致：
preroll(_prefill_ring_to) → 稳态 _play_cycle（prefill + 电平兜底 + ring 读/直混兜底）

测量：
1. 稳态播放中 AudioDecoder.seek_to_time 触发次数（应为 0）。
2. 产出的样本流与"连续参考解码"逐样本对齐比较，找出断裂点。
3. scrub 音频预览（随机帧 mix）插入后恢复播放的断裂。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from src.features.edit.core.audio_playback import AudioDecoder, AudioMixer, AudioTrackState
from src.features.edit.core.realtime_playback.audio_ring_buffer import AudioRingBuffer

MEDIA = str(ROOT / "tests/test_videos/2K_h264.mp4")
FPS = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
SIM_SCRUB = "--scrub" in sys.argv
SIM_BLOCKED = "--blocked" in sys.argv

# ---------- 参考流：无 seek 的连续解码 ----------
def build_reference(total_samples: int) -> np.ndarray:
    dec = AudioDecoder()
    assert dec.open(Path(MEDIA), FPS, source_start=0, source_fps=0.0, output_sample_rate=48000)
    chunks = []
    got = 0
    while got < total_samples:
        s = dec.decode_next_samples(1920)
        if s is None:
            break
        chunks.append(s)
        got += s.shape[-1]
    dec.close()
    ref = np.concatenate(chunks, axis=-1)
    return ref[:, :total_samples]


def main() -> None:
    seek_count = 0
    seek_frames: list = []
    orig_seek = AudioDecoder.seek_to_time

    def counting_seek(self, t):
        nonlocal seek_count
        seek_count += 1
        if len(seek_frames) < 20:
            seek_frames.append(round(t, 4))
        return orig_seek(self, t)

    AudioDecoder.seek_to_time = counting_seek

    # ---------- mixer 组装（与 AudioPlaybackManager.add_track 一致） ----------
    mixer = AudioMixer()
    dec = AudioDecoder()
    assert dec.open(Path(MEDIA), FPS, source_start=0, source_fps=0.0, output_sample_rate=48000)
    dec._timeline_start = 0
    dec._timeline_duration = 0
    mixer.add_track("t1", dec, AudioTrackState(track_id="t1"), replace_existing=True)

    # ---------- 线程侧状态（复制修复后 AudioPlaybackThread 逻辑） ----------
    samples_per_frame_exact = 48000.0 / FPS
    samples_per_frame = max(1, int(samples_per_frame_exact))
    ring = AudioRingBuffer(channels=2, capacity=24000)
    prefill_target = 9600
    ring_write_frame_float = 0.0
    ring_write_frame_int = 0
    prefill_chunk_fraction = 0.0
    next_audio_frame = 0
    fractional_frame = 0.0
    latest_peaks: dict = {}
    sample_chunk_fraction = 0.0
    produced: list[np.ndarray] = []

    def advance_cursor_from(frame: int, samples: int) -> None:
        nonlocal ring_write_frame_float, ring_write_frame_int
        consumed_end = frame + samples / samples_per_frame_exact
        ring_write_frame_float = max(ring_write_frame_float, consumed_end)
        ring_write_frame_int = max(ring_write_frame_int, int(consumed_end + 0.999999999))

    def prefill_ring_to(target: int) -> None:
        # 修复后逻辑：整数帧游标 + 分数累积器，不 clip 到 needed，无电平兜底
        nonlocal ring_write_frame_int, ring_write_frame_float
        nonlocal prefill_chunk_fraction, latest_peaks
        iterations = 0
        max_iterations = max(8, int(target / samples_per_frame_exact) + 8)
        while ring.filled < target:
            iterations += 1
            if iterations > max_iterations:
                break
            prefill_chunk_fraction += samples_per_frame_exact
            n = int(prefill_chunk_fraction)
            prefill_chunk_fraction -= n
            n = max(1, n)
            cur = int(ring_write_frame_int)
            samples, per_track = mixer.mix_samples(cur, FPS, n, 2)
            if samples is None or samples.size == 0:
                break
            if per_track:
                latest_peaks = per_track
            written = ring.write(samples)
            if written <= 0:
                break
            ring_write_frame_int = cur + 1
            ring_write_frame_float = float(ring_write_frame_int)

    def prefill_ring() -> None:
        prefill_ring_to(prefill_target)

    # ---------- preroll（与 preroll 命令一致） ----------
    prefill_ring_to(prefill_target)
    preroll_seeks = seek_count
    print(f"== fps={FPS} preroll done: filled={ring.filled} seeks={preroll_seeks} ==")

    # ---------- 稳态播放循环 ----------
    CYCLES = 300
    blocked_every = 4 if SIM_BLOCKED else 0
    for cyc in range(CYCLES):
        # 模拟 bytes_free 阻塞周期（不消费）
        skip_consume = blocked_every and (cyc % blocked_every == blocked_every - 1)

        prefill_ring()

        # _next_chunk_samples
        sample_chunk_fraction += samples_per_frame_exact
        chunk = int(sample_chunk_fraction)
        sample_chunk_fraction -= chunk
        chunk = max(1, chunk)

        if skip_consume:
            continue

        cur_frame = next_audio_frame
        if ring.filled >= chunk:
            samples = ring.read(chunk)
            used_direct = False
        else:
            samples, _ = mixer.mix_samples(cur_frame, FPS, chunk, 2)
            ring.clear()  # 修复：direct-mix 后清空 ring 残留
            advance_cursor_from(cur_frame, samples.shape[-1])
            used_direct = True

        produced.append(np.asarray(samples, dtype=np.float32))

        # 电平 emit 会清空缓存（30ms 节流；25fps 周期 40ms → 每周期都 emit）
        if latest_peaks:
            latest_peaks = {}

        # 帧推进（与 _play_cycle 一致：分数累积器）
        written_n = samples.shape[-1]
        if used_direct:
            pass  # 游标已在 advance_cursor_from 处理
        fractional_frame += written_n / samples_per_frame_exact
        adv = int(fractional_frame)
        fractional_frame -= adv
        next_audio_frame += adv

        # 模拟 scrub 音频预览 worker 在暂停/拖拽时共享 mixer 混一帧
        if SIM_SCRUB and cyc == 100:
            mixer.mix_samples(500, FPS, 3456, 2)  # 随机位置 72ms
            print(f"-- scrub mix injected at cyc=100, seeks so far={seek_count}")

    steady_seeks = seek_count - preroll_seeks
    stream = np.concatenate(produced, axis=-1)
    print(f"cycles={CYCLES} produced={stream.shape[-1]} steady_seeks={steady_seeks}")
    if seek_frames:
        print("seek times(first 20):", seek_frames)

    # ---------- 与参考流比较 ----------
    ref = build_reference(stream.shape[-1] + 1920)
    # 找最佳对齐偏移（preroll 可能有初段差异），在 0..4800 内搜索
    best_off, best_err = 0, None
    probe = stream[:, :1920]
    for off in range(0, 4801, 64):
        if off + 1920 > ref.shape[-1]:
            break
        err = float(np.mean(np.abs(ref[:, off:off + 1920] - probe)))
        if best_err is None or err < best_err:
            best_err, best_off = err, off
    print(f"align offset={best_off} err={best_err:.6f}")

    n = min(stream.shape[-1], ref.shape[-1] - best_off)
    diff = np.abs(ref[:, best_off:best_off + n] - stream[:, :n])
    max_per_block = []
    B = 1920
    for i in range(0, n - B, B):
        max_per_block.append((i, float(diff[:, i:i + B].max())))
    bad = [(i, m) for i, m in max_per_block if m > 0.01]
    print(f"blocks={len(max_per_block)} mismatch_blocks(>0.01)={len(bad)}")
    for i, m in bad[:10]:
        print(f"  block@sample {i} (frame≈{i / samples_per_frame_exact:.1f}) maxdiff={m:.4f}")
    if not bad:
        print("STREAM CONTINUOUS: 与参考流完全对齐")
    AudioDecoder.seek_to_time = orig_seek


if __name__ == "__main__":
    main()
