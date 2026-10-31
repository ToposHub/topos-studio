"""DESK-05A · 跨端时间精度共享向量（生成器 + 独立 Fraction oracle）。

目的
----
手机（Dart）与桌面（Python）对同一工程必须算出**逐帧相同**的映射：
帧→源帧、帧→样本、分割→采样分区。桌面侧实现散在
``timeline_frame_mapper`` / ``sample_time``（Fraction 精确算术），但移动
侧无法"照读实现"——需要一份**数据向量**：输入点 + 独立 oracle 算出的
期望值。双端各自跑一遍全部向量，全绿即时间语义对齐。

三层保证
--------
1. **oracle 独立**：期望值全部用 ``fractions.Fraction`` 从冻结规则
   （见下）重算，不 import 生产编辑内核。
2. **文件新鲜**：``data/timebase_vectors_v1.json`` 是生成器产物并入库；
   合同测试再生成并逐字节比对，防手改漂移。
3. **生产对拍**：合同测试（``tests/shared/test_timebase_contract_vectors.py``）
   把向量逐点喂给桌面生产实现。

冻结的映射规则（与 docs/adr/0005 §5、F0.4 ADR 一致）
-----------------------------------------------------
CFR（恒定帧率）::

    local        = timeline_frame - clip_timeline_start
    t            = local / (tl_fps_num/tl_fps_den)          # 精确有理数
    src_local    = t * speed                                # speed 为有理数
    src_frac     = src_local * (src_fps_num/src_fps_den) + source_frame_phase
    total        = src_frac + source_start_frame
    reverse 时   total = source_start + source_duration - 1 - src_frac
    source_frame = half_up(total)                           # 正负域对称
                 并钳位到 [source_start, source_start + source_duration - 1]

half-up：``floor(x + 1/2)``（正数域）；**不是**银行家舍入——精确 .5
界值必须远离零取整。

VFR（可变帧率）::

    target = t * speed（精确有理数；实现可用 float，但判定必须与精确
    有理数一致）；source_frame = 距 target 最近的 PTS 条目下标，
    距离平局（|target-p_i| == |target-p_{i+1}|）取**较早**条目；
    越界（target 先于首个/晚于末个 PTS）钳位到端点。

音频采样::

    samples_per_frame(frame) = sample_rate * frame / fps      # 精确有理数
    block_bounds(N 帧)      = 端点一次求值取 floor 后均分：
                              base = D // N，前 r = D - base*N 帧各多 1 样本
    split 分区（切点样本 floor 归左片；左闭右开）：
        left_len  = floor(split_pos - clip_start_pos)
        total_len = floor(clip_end_pos  - clip_start_pos)
        left  = (offset, max(0, left_len - offset))
        right = (offset + left.count, max(0, total_len - offset - left.count))
    subframe 分解：frame = floor(sec * fps)；offset = (sec - frame/fps) * sr
    （Fraction，不进位）；合成严格互逆。
    声明率→48000 参考率：whole = floor(offset * 48000 / declared)，
    残差 = converted - whole（<1 参考样本，记账不静默丢）。

序列化约定：有理数一律写成 ``"<num>/<den>"`` 字符串（与
``edit/timeline_sequences.json`` 的 fps 字段同型）。
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

__all__ = [
    "VECTOR_SET_ID",
    "build_vectors",
    "vectors_digest",
    "write_vectors_file",
    "load_vectors_file",
    "vectors_path",
]

#: 向量集标识。语义变更（规则/新增维度）→ 新 set id + 新文件；
#: 纯加向量点（规则不变）→ 同文件更新并同步双端。
VECTOR_SET_ID = "timebase_v1"

#: 入库向量文件名（与 set id 解耦：加向量点不换文件名，只换内容）。
VECTORS_FILENAME = "timebase_vectors_v1.json"

_DATA_DIR = Path(__file__).parent / "data"


# ------------------------------------------------------------------
# 有理数序列化
# ------------------------------------------------------------------

def _frac_str(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def parse_frac(text: str | int | Fraction) -> Fraction:
    """解析 ``"num/den"`` / int / Fraction 为 Fraction（双端约定入口）。"""
    if isinstance(text, Fraction):
        return text
    if isinstance(text, int):
        return Fraction(text, 1)
    num, _, den = str(text).partition("/")
    return Fraction(int(num), int(den) if den else 1)


def half_up(value: Fraction) -> int:
    """半-up 取整（正负域对称：floor(x+1/2) / -floor(-x+1/2)）。"""
    if value >= 0:
        return (value + Fraction(1, 2)).__floor__()
    return -((-value + Fraction(1, 2)).__floor__())


# ------------------------------------------------------------------
# 独立 oracle（冻结规则直译，不 import 生产代码）
# ------------------------------------------------------------------

def oracle_cfr_source_frame(
    *,
    timeline_frame: int,
    timeline_fps: Fraction,
    clip_timeline_start: int,
    clip_timeline_duration: int,
    source_start_frame: int,
    source_duration_frames: int,
    source_fps: Fraction | None,
    speed: Fraction,
    reverse: bool,
    source_frame_phase: Fraction,
) -> int:
    local = timeline_frame - clip_timeline_start
    if local < 0 or local >= clip_timeline_duration:
        raise ValueError("timeline frame outside clip window")
    t = Fraction(local, 1) / timeline_fps
    src_local = t * speed
    src_fps = source_fps if source_fps is not None else timeline_fps
    src_frac = src_local * src_fps + source_frame_phase
    if reverse:
        total = Fraction(
            source_start_frame + source_duration_frames - 1, 1) - src_frac
    else:
        total = src_frac + Fraction(source_start_frame, 1)
    frame = half_up(total)
    return max(
        source_start_frame,
        min(frame, source_start_frame + source_duration_frames - 1),
    )


def oracle_vfr_source_index(
    *,
    timeline_frame: int,
    timeline_fps: Fraction,
    clip_timeline_start: int,
    clip_timeline_duration: int,
    source_pts_seconds: list[Fraction],
    speed: Fraction,
) -> int:
    local = timeline_frame - clip_timeline_start
    if local < 0 or local >= clip_timeline_duration:
        raise ValueError("timeline frame outside clip window")
    target = Fraction(local, 1) / timeline_fps * speed
    if target <= source_pts_seconds[0]:
        return 0
    if target >= source_pts_seconds[-1]:
        return len(source_pts_seconds) - 1
    for i in range(len(source_pts_seconds) - 1):
        before = source_pts_seconds[i]
        after = source_pts_seconds[i + 1]
        if before <= target <= after:
            dist_before = target - before
            dist_after = after - target
            # 平局归较早条目（与生产 ``<=`` 判定一致）
            return i if dist_before <= dist_after else i + 1
    return len(source_pts_seconds) - 1


def _simulate_production_vfr_float(
    *,
    timeline_frame: int,
    timeline_fps: Fraction,
    clip_timeline_start: int,
    clip_timeline_duration: int,
    source_pts_seconds: list[Fraction],
    speed: Fraction,
) -> int:
    """复刻生产实现的 float bisect 判定（仅用于生成器一致性自检）。

    生产 ``map_vfr_frame``：target = float(t*speed)，bisect_left 后比较
    ``abs(target-before) <= abs(target-after)``。若某向量点上 float 舍入
    会翻转精确有理数判定，该点不能进共享向量集（生成器直接抛错）。
    """
    local = timeline_frame - clip_timeline_start
    if local < 0 or local >= clip_timeline_duration:
        raise ValueError("timeline frame outside clip window")
    target = float(Fraction(local, 1) / timeline_fps * speed)
    pts = [float(p) for p in source_pts_seconds]
    idx = 0
    # bisect_left
    lo, hi = 0, len(pts)
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    idx = lo
    if idx == 0:
        return 0
    if idx >= len(pts):
        return len(pts) - 1
    before = pts[idx - 1]
    after = pts[idx]
    return idx - 1 if abs(target - before) <= abs(target - after) else idx


def oracle_block_bounds(
    *, fps: Fraction, sample_rate: int, start_frame: int, frame_count: int,
) -> list[int]:
    """N 帧块边界（相对首样本，整数）：端点一次求值 + 前 r 帧各多 1。"""
    first = Fraction(sample_rate) * start_frame / fps
    total = Fraction(sample_rate) * (start_frame + frame_count) / fps
    n0, n1 = first.__floor__(), total.__floor__()
    span = n1 - n0
    if span < frame_count:
        span = frame_count
    base = span // frame_count
    r = span - base * frame_count
    bounds = [0] * (frame_count + 1)
    for i in range(frame_count):
        bounds[i] = i * base + min(i, r)
    bounds[frame_count] = span
    return bounds


def oracle_split_partition(
    *,
    fps: Fraction,
    sample_rate: int,
    clip_timeline_start_frame: int,
    clip_timeline_duration_frames: int,
    split_timeline_frame: int,
    source_sample_offset: int,
) -> tuple[tuple[int, int], tuple[int, int]]:
    clock = Fraction(sample_rate, 1)
    start_pos = clock * clip_timeline_start_frame / fps
    split_pos = clock * split_timeline_frame / fps
    end_pos = clock * (clip_timeline_start_frame
                       + clip_timeline_duration_frames) / fps
    left_len = (split_pos - start_pos).__floor__()
    total_len = (end_pos - start_pos).__floor__()
    left_offset = int(source_sample_offset)
    left_count = max(0, left_len - left_offset)
    right_offset = left_offset + left_count
    right_count = max(0, total_len - right_offset)
    return (left_offset, left_count), (right_offset, right_count)


def oracle_subframe_decompose(
    *, fps: Fraction, sample_rate: int, seconds: Fraction,
) -> tuple[int, Fraction]:
    frame = (seconds * fps).__floor__()
    residue = seconds - Fraction(frame) / fps
    return frame, residue * sample_rate


def oracle_offset_to_reference(
    *, offset_samples: Fraction, declared_rate: int, reference_rate: int = 48000,
) -> tuple[int, Fraction]:
    converted = offset_samples * reference_rate / declared_rate
    whole = converted.__floor__()
    return whole, converted - whole


# ------------------------------------------------------------------
# 向量生成
# ------------------------------------------------------------------

_CFR_DEFAULTS = {
    "timeline_fps": "24/1",
    "clip_timeline_start": 0,
    "clip_timeline_duration": 10 ** 9,
    "source_start_frame": 0,
    "source_duration_frames": 10 ** 9,
    "source_fps": "24/1",
    "speed": "1/1",
    "reverse": False,
    "source_frame_phase": "0/1",
}

_FPS_SET = [
    ("24/1", Fraction(24, 1)),
    ("25/1", Fraction(25, 1)),
    ("30000/1001", Fraction(30000, 1001)),
    ("60000/1001", Fraction(60000, 1001)),
    ("24000/1001", Fraction(24000, 1001)),
]


def _cfr_vector(
    vector_id: str, *, timeline_frame: int, **overrides: Any,
) -> dict[str, Any]:
    """构造一个 CFR 向量并用 oracle 填入期望源帧。"""
    params = dict(_CFR_DEFAULTS)
    params.update(overrides)
    parsed = {
        "timeline_fps": parse_frac(params["timeline_fps"]),
        "clip_timeline_start": params["clip_timeline_start"],
        "clip_timeline_duration": params["clip_timeline_duration"],
        "source_start_frame": params["source_start_frame"],
        "source_duration_frames": params["source_duration_frames"],
        "source_fps": parse_frac(params["source_fps"]),
        "speed": parse_frac(params["speed"]),
        "reverse": params["reverse"],
        "source_frame_phase": parse_frac(params["source_frame_phase"]),
    }
    expected = oracle_cfr_source_frame(
        timeline_frame=timeline_frame, **parsed)
    return {
        "id": vector_id,
        "timeline_frame": timeline_frame,
        **params,
        "expected_source_frame": expected,
    }


def _build_cfr_vectors() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []

    # 1. 同帧率恒等：任意长度（含 NTSC 长片）零累计漂移
    for fps_str, fps in _FPS_SET:
        for frame in (0, 1, 5, 1000, 107_892, 431_568):
            vectors.append(_cfr_vector(
                f"identity_{fps_str.replace('/', '-').replace('1001', '1001')}_f{frame}",
                timeline_frame=frame,
                timeline_fps=fps_str,
                source_fps=fps_str,
            ))

    # 2. 混帧率（half-up 主区）
    cross_cases = [
        ("24_to_ntsc", "24/1", "30000/1001", (1, 7, 13, 100, 3600)),
        ("25_to_ntsc60", "25/1", "60000/1001", (1, 3, 41, 999)),
        ("ntsc_to_24", "24000/1001", "24/1", (1, 5, 29, 777)),
        ("24_to_60", "24/1", "60000/1001", (1, 2, 11, 500)),
        ("ntsc_to_ntsc60", "30000/1001", "60000/1001", (1, 9, 101, 4004)),
    ]
    for tag, tl, src, locals_ in cross_cases:
        for local in locals_:
            vectors.append(_cfr_vector(
                f"cross_{tag}_l{local}",
                timeline_frame=local, timeline_fps=tl, source_fps=src,
            ))

    # 3. 精确 .5 界值：half-up（不是银行家舍入/截断）
    tie_cases = [
        ("tie_speed_3q_24", 2, {"speed": "3/4"}),          # 2*3/4 = 1.5 → 2
        ("tie_speed_half_25", 5, {"timeline_fps": "25/1",
                                  "source_fps": "25/1",
                                  "speed": "1/2"}),        # 2.5 → 3
        ("tie_phase_half_24", 0, {"source_frame_phase": "1/2"}),   # 0.5 → 1
        ("tie_phase_3half_24", 0, {"source_frame_phase": "3/2"}),  # 1.5 → 2
        ("tie_cross_24_ntsc", 1, {"timeline_fps": "24/1",
                                  "source_fps": "30000/1001"}),    # 1.24875 → 1
    ]
    for tag, frame, overrides in tie_cases:
        vectors.append(_cfr_vector(f"half_up_{tag}", timeline_frame=frame,
                                   **overrides))

    # 4. 变速 / 倒放 / 钳位
    vectors.append(_cfr_vector("speed_half_24_l10", timeline_frame=10,
                               speed="1/2"))                       # → 5
    vectors.append(_cfr_vector("speed_double_24_l7", timeline_frame=7,
                               speed="2/1"))                       # → 14
    vectors.append(_cfr_vector("reverse_24_l0", timeline_frame=0,
                               source_duration_frames=48, reverse=True))  # → 47
    vectors.append(_cfr_vector(
        "reverse_offset_l0", timeline_frame=0,
        source_start_frame=100, source_duration_frames=48, reverse=True))  # → 147
    vectors.append(_cfr_vector(
        "reverse_speed_half_l4", timeline_frame=4,
        source_duration_frames=48, speed="1/2", reverse=True))      # → 45
    vectors.append(_cfr_vector(
        "clamp_tail_ntsc", timeline_frame=100,
        timeline_fps="24/1", source_fps="30000/1001",
        source_duration_frames=48))                                  # → ≤47

    # 5. 非零入点 + 有理相位
    phase_cases = [
        (1, "1/3"), (2, "1/3"), (4, "1/3"),
        (3, "1/2"),          # 3.5 → 4（half-up 界值 + 入点偏移）
        (5, "2/3"), (11, "5/8008"),
    ]
    for local, phase in phase_cases:
        vectors.append(_cfr_vector(
            f"phase_ntsc_l{local}_p{phase.replace('/', '-')}",
            timeline_frame=local,
            timeline_fps="30000/1001", source_fps="30000/1001",
            source_start_frame=37, source_duration_frames=25,
            source_frame_phase=phase,
        ))

    # 6. 连续分割一致性：在父窗 local=7 处切分（跨帧率 24←NTSC）。
    #    切点的精确源位置 = 7*1250/1001 = 8 + 742/1001（有理数）。
    #    子窗必须携带**有理相位**（edit_content v4 的
    #    source_frame_phase）而非独立取整的整数起点，否则 half-up
    #    会在 0.2587 宽的界带内分叉（这正是相位列存在的理由）。
    #    子窗 = (start 108, phase 742/1001) ↔ 父窗 local k 逐帧一致。
    split_cut_phase = Fraction(7) * Fraction(1250, 1001) - 8   # 742/1001
    assert split_cut_phase == Fraction(742, 1001)
    for k in range(7, 13):
        parent = _cfr_vector(
            f"split_parent_k{k}", timeline_frame=k,
            timeline_fps="24/1", source_fps="30000/1001",
            source_start_frame=100, source_duration_frames=30)
        child = _cfr_vector(
            f"split_child_k{k}", timeline_frame=k,
            timeline_fps="24/1", source_fps="30000/1001",
            source_start_frame=108, source_duration_frames=22,
            clip_timeline_start=7,
            source_frame_phase=_frac_str(split_cut_phase))
        assert parent["expected_source_frame"] == child["expected_source_frame"], (
            f"split continuity broken at k={k}: "
            f"{parent['expected_source_frame']} != {child['expected_source_frame']}"
        )
        vectors.extend([parent, child])

    # 6b. 同帧率分割（基线）：整数切点，相位 0，天然连续
    for k in (4, 5, 9):
        parent = _cfr_vector(
            f"split_same_fps_parent_k{k}", timeline_frame=k,
            source_start_frame=50, source_duration_frames=20)
        child = _cfr_vector(
            f"split_same_fps_child_k{k}", timeline_frame=k,
            source_start_frame=54, source_duration_frames=16,
            clip_timeline_start=4)
        assert parent["expected_source_frame"] == child["expected_source_frame"]
        vectors.extend([parent, child])

    return vectors


def _build_vfr_vectors() -> list[dict[str, Any]]:
    """VFR 向量：PTS 全部取 dyadic 有理数（float 可精确表示）。"""
    pts = [Fraction(k, 8) for k in range(11)]  # 0, 1/8, ..., 10/8
    pts_str = [_frac_str(p) for p in pts]
    tl = Fraction(24, 1)

    cases: list[tuple[str, int, str]] = [
        ("vfr_first", 0, "1/1"),        # target 0 → idx 0
        ("vfr_exact_1_8", 3, "1/1"),    # 3/24 = 1/8 → idx 1
        ("vfr_exact_1_4", 6, "1/1"),    # → idx 2
        ("vfr_exact_1_2", 12, "1/1"),   # → idx 4
        ("vfr_near_before", 13, "1/1"),  # 13/24≈0.5417 → 距 1/2 近 → idx 4
        ("vfr_exact_5_8", 15, "1/1"),   # 15/24 = 5/8 → 精确命中 idx 5
        ("vfr_after_last", 100, "1/1"),  # 超出末条 → 钳位末索引
        ("vfr_half_speed", 24, "1/2"),   # 24/24*1/2 = 1/2 → idx 4
        ("vfr_half_speed_2", 30, "1/2"),  # 30/24*1/2 = 5/8 → idx 5
        # 真平局：local=27, speed=1/2 → target = 27/24/2 = 9/16，
        # 距相邻 1/2(8/16) 与 5/8(10/16) 均为 1/16，中间无条目 → 归较早 idx 4
        ("vfr_tie_9_16_to_half", 27, "1/2"),
    ]

    vectors = []
    for vector_id, local, speed in cases:
        speed_frac = parse_frac(speed)
        expected = oracle_vfr_source_index(
            timeline_frame=local, timeline_fps=tl, clip_timeline_start=0,
            clip_timeline_duration=10 ** 9, source_pts_seconds=pts,
            speed=speed_frac,
        )
        float_decision = _simulate_production_vfr_float(
            timeline_frame=local, timeline_fps=tl, clip_timeline_start=0,
            clip_timeline_duration=10 ** 9, source_pts_seconds=pts,
            speed=speed_frac,
        )
        if float_decision != expected:
            raise RuntimeError(
                f"VFR 向量 {vector_id} 的 float 判定({float_decision})与精确"
                f"有理数判定({expected})不一致——该点不能进入共享向量集，"
                f"请更换输入点"
            )
        vectors.append({
            "id": vector_id,
            "timeline_frame": local,
            "timeline_fps": "24/1",
            "clip_timeline_start": 0,
            "clip_timeline_duration": 10 ** 9,
            "source_pts_seconds": pts_str,
            "speed": speed,
            "expected_source_frame": expected,
        })
    return vectors


def _build_audio_vectors() -> dict[str, Any]:
    # 1. 帧起点样本（精确有理数）
    frame_starts = []
    for fps_str, fps in (("24/1", Fraction(24, 1)),
                         ("30000/1001", Fraction(30000, 1001))):
        for frame in (0, 1, 2, 5, 1001, 5000):
            position = Fraction(48000) * frame / fps
            frame_starts.append({
                "id": f"fs_{fps_str.replace('/', '-')}_f{frame}",
                "fps": fps_str,
                "sample_rate": 48000,
                "frame": frame,
                "expected_start_sample": _frac_str(position),
            })

    # 2. 块边界（端点一次求值 + 前 r 帧各多 1 样本）
    blocks = []
    for fps_str, fps, start, count in (
        ("30000/1001", Fraction(30000, 1001), 3, 5),
        ("30000/1001", Fraction(30000, 1001), 0, 8),
        ("24/1", Fraction(24, 1), 0, 10),
        ("60000/1001", Fraction(60000, 1001), 7, 11),
    ):
        blocks.append({
            "id": f"block_{fps_str.replace('/', '-')}_s{start}_n{count}",
            "fps": fps_str,
            "sample_rate": 48000,
            "start_frame": start,
            "frame_count": count,
            "expected_bounds": oracle_block_bounds(
                fps=fps, sample_rate=48000, start_frame=start,
                frame_count=count),
        })

    # 3. split 采样分区（切点 floor 归左；连续无孔无缝）
    splits = []
    for fps_str, fps, clip_start, dur, split, offset in (
        ("30000/1001", Fraction(30000, 1001), 2, 9, 6, 0),
        ("30000/1001", Fraction(30000, 1001), 2, 9, 6, 7),
        ("24/1", Fraction(24, 1), 0, 12, 5, 0),
        ("24/1", Fraction(24, 1), 10, 20, 17, 48000),
    ):
        (lo, lc), (ro, rc) = oracle_split_partition(
            fps=fps, sample_rate=48000, clip_timeline_start_frame=clip_start,
            clip_timeline_duration_frames=dur, split_timeline_frame=split,
            source_sample_offset=offset,
        )
        splits.append({
            "id": (f"split_{fps_str.replace('/', '-')}_c{clip_start}"
                   f"_d{dur}_s{split}_o{offset}"),
            "fps": fps_str,
            "sample_rate": 48000,
            "clip_timeline_start_frame": clip_start,
            "clip_timeline_duration_frames": dur,
            "split_timeline_frame": split,
            "source_sample_offset": offset,
            "expected_left": [lo, lc],
            "expected_right": [ro, rc],
        })

    # 4. 子帧分解/合成（严格互逆）
    subframes = []
    for seconds_str in ("3/8", "1001/30000", "7/24", "5/6"):
        seconds = parse_frac(seconds_str)
        frame, offset = oracle_subframe_decompose(
            fps=Fraction(24, 1), sample_rate=48000, seconds=seconds)
        recomposed = (Fraction(frame) / Fraction(24, 1)
                      + offset / 48000)
        assert recomposed == seconds
        subframes.append({
            "id": f"subframe_{seconds_str.replace('/', '-')}",
            "fps": "24/1",
            "sample_rate": 48000,
            "seconds": seconds_str,
            "expected_frame": frame,
            "expected_offset_samples": _frac_str(offset),
        })

    # 5. 声明率 → 参考率换算（残差记账）
    conversions = []
    for offset_str, declared in (("1/1", 44100), ("147/160", 44100),
                                 ("1001/1", 48000), ("3/2", 96000)):
        offset = parse_frac(offset_str)
        whole, residual = oracle_offset_to_reference(
            offset_samples=offset, declared_rate=declared)
        conversions.append({
            "id": f"rate_{offset_str.replace('/', '-')}_{declared}",
            "offset_samples": offset_str,
            "declared_rate": declared,
            "reference_rate": 48000,
            "expected_whole_samples": whole,
            "expected_residual": _frac_str(residual),
        })

    return {
        "frame_start_samples": frame_starts,
        "block_bounds": blocks,
        "split_partitions": splits,
        "subframe_roundtrips": subframes,
        "rate_conversions": conversions,
    }


def build_vectors() -> dict[str, Any]:
    """生成完整向量集（确定性：无随机、无时间依赖、固定遍历序）。"""
    cfr = _build_cfr_vectors()
    vfr = _build_vfr_vectors()
    audio = _build_audio_vectors()
    return {
        "vector_set": VECTOR_SET_ID,
        "rounding": "half_up",
        "rationals": "num/den strings",
        "notes": (
            "CFR 向量：与 defaults 合并后为完整输入（未出现的键取 "
            "defaults 值）。VFR 平局归较早条目。音频块边界 = 端点一次"
            "求值 + 前 r 帧各多 1 样本。生成器 oracle 与本文件期望值由"
            "合同测试双向锁定。"
        ),
        "cfr_defaults": dict(_CFR_DEFAULTS),
        "cfr": cfr,
        "vfr": vfr,
        "audio": audio,
    }


# ------------------------------------------------------------------
# 文件 I/O 与哈希
# ------------------------------------------------------------------

def vectors_path() -> Path:
    """入库向量文件的路径（tests/project 合同测试与移动侧工具共用）。"""
    return _DATA_DIR / VECTORS_FILENAME


def write_vectors_file(path: Path | None = None) -> Path:
    """生成向量并写出（排序键、缩进 2、无 NaN——字节级确定性）。"""
    target = path or vectors_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    vectors = build_vectors()
    target.write_text(
        json.dumps(vectors, indent=2, sort_keys=True, ensure_ascii=False,
                   allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return target


def load_vectors_file(path: Path | None = None) -> dict[str, Any]:
    target = path or vectors_path()
    return json.loads(target.read_text(encoding="utf-8"))


def vectors_digest(vectors: dict[str, Any] | None = None) -> str:
    """向量集摘要（canonical JSON 的 SHA-256）：双端核对同一份向量。"""
    import hashlib

    data = vectors if vectors is not None else build_vectors()
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
