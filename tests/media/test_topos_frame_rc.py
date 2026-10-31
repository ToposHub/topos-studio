"""P4 帧级码控测试（画质对齐计划 §5 P4，2026-09-21）。

- ToposRateFeedback：短窗 VBV / 切换检测 / 借贷封顶 / 预算模式 / 遥测；
- ToposVideoEncoder：复杂度前瞻重锚（合成复杂度阶跃序列——切换首帧
  即重锚，无质量悬崖）+ 预算硬上限仍成立。
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.color_metadata_config import ColorMetadataConfig  # noqa: E402
from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile  # noqa: E402
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402
from src.shared.export.topos_rate_control import ToposRateFeedback  # noqa: E402

FPS = Fraction(25, 1)
W, H = 256, 144


def _config(**kw):
    base = dict(
        width=W, height=H, fps=FPS, pix_fmt="yuv422p10le",
        profile="proxy", crf=None, has_alpha=False,
        entropy_mode="rans2", aq_mode="off", rdo_mode="off", gop="intra",
        color_metadata=ColorMetadataConfig(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


class Frame:
    """planar 帧：kind 决定内容复杂度（flat=全平 / noise=全频噪声）。"""

    def __init__(self, index: int, kind: str):
        rng = np.random.default_rng(index)
        cw = (W + 1) // 2
        if kind == "noise":
            y = rng.integers(64, 941, size=(H, W), dtype=np.uint16)
        else:
            y = np.full((H, W), 400, dtype=np.uint16)
        u = np.full((H, cw), 512, dtype=np.uint16)
        v = np.full((H, cw), 512, dtype=np.uint16)
        self.planes = tuple(p.astype("<u2") for p in (y, u, v))
        self.pixel_format = "yuv422p10le"
        self.width, self.height = W, H
        self.frame_index = index
        self.is_planar = True


def _video_bytes(path: Path, codec: ToposCodec) -> int:
    m = ToposMovieFile(codec, str(path))
    try:
        return sum(m.packet_into(i)[1]
                   for i in range(int(m.info().sample_count)))
    finally:
        m.close()


# ————————————————————————— 控制器单测 —————————————————————————

def test_controller_cut_detection_and_lending():
    """切换帧：检测置位 + 借贷入账 + 下一帧钳位放宽。"""
    rc = ToposRateFeedback(1000)
    rc.seed(40, 1000)
    # 稳态段（窗口建立）
    for _ in range(10):
        rc.note(1000)
    assert not rc.last_frame_was_cut and not rc.reseed_hint
    # 切换帧：4× 中位数 → 检测 + 借贷（超支 3000 ≤ 封顶 3×target 全额入账）
    rc.note(4000)
    assert rc.last_frame_was_cut
    assert rc.reseed_hint
    t = rc.telemetry()
    assert t["debt_bytes"] == 3000
    assert t["cut_frames"] == [12]
    anchored = rc.anchored
    rc.anchored(50, 1000)   # 编码器重锚消费 hint
    assert rc.reseed_hint is False
    assert rc.qp == 50
    assert rc.frames == 13  # seed+10+切换+重锚：anchored 记账不清累计
    assert anchored


def test_controller_lend_cap():
    """超封顶（>lend_cap_factor×target）借贷只记封顶值并触发重锚信号。"""
    rc = ToposRateFeedback(1000)
    rc.seed(40, 1000)
    for _ in range(8):
        rc.note(1000)
    rc.note(20000)   # 20× target：capped 3000 入账
    assert rc.telemetry()["debt_bytes"] == 3000
    assert rc.reseed_hint


def test_controller_budget_mode_determinism():
    """预算模式：同字节序列 → 同 qp 序列（确定性）+ 硬上限判定。"""
    seq = [1200, 800, 1500, 900, 1100, 700, 1300, 1000]

    def run():
        rc = ToposRateFeedback(1000, total_budget_bytes=10_000,
                               total_frames=10)
        rc.seed(50, seq[0])
        out = []
        for s in seq[1:]:
            out.append(rc.note(s))
        return out, rc.total

    a, ta = run()
    b, tb = run()
    assert a == b and ta == tb
    rc = ToposRateFeedback(1000, total_budget_bytes=10_000, total_frames=10)
    rc.seed(50, 6000)
    assert rc.over_budget is False
    rc.note(6000)
    assert rc.over_budget is True


def test_controller_repay_is_gentle():
    """借贷偿还温和：切换后简单帧的 qp 上调 ≤ lend_repay_clamp + 常规项。"""
    rc = ToposRateFeedback(1000)
    rc.seed(40, 1000)
    for _ in range(10):
        rc.note(1000)
    qp_before = rc.qp
    rc.note(3000)                    # 切换（借贷 2000；下一帧钳位放宽 ±8）
    qp_after = rc.note(1000)         # 再下一帧（正常内容，回常规 ±4）
    # 两步合计界：放宽 8 + 常规 4 + 偿还 2（对比无借贷的纯爬行：切换后
    # 每帧仅 ±4，追回 3× 超支需多帧——复杂帧后的简单帧不被过度惩罚）
    assert qp_after - qp_before <= 8 + 4 + rc.lend_repay_clamp


# ————————————————————————— 编码器集成 —————————————————————————

def test_encoder_reanchors_on_complexity_step(tmp_path):
    """复杂度阶跃：切换首帧 sized 重锚（无质量悬崖），预算仍守。"""
    frames = [("flat", 12), ("noise", 8), ("flat", 12)]
    budget = 260_000
    out = tmp_path / "step.mov"
    enc = ToposVideoEncoder(
        str(out), _config(max_video_bytes=budget, budget_frames=32),
        slice_threads=2)
    enc.open()
    idx = 0
    qp_trace = []
    kinds = []
    for kind, n in frames:
        for _ in range(n):
            enc.encode_frame(Frame(idx, kind))
            rc = enc._rc
            if rc is not None and rc.active:
                qp_trace.append(rc.qp)
                kinds.append(kind)
            idx += 1
    enc.close()
    codec = ToposCodec()
    assert _video_bytes(out, codec) <= budget
    # 遥测可得且记录了切换
    t = enc.rate_control_telemetry()
    assert t is not None and t["qp_max"] >= t["qp_min"]
    assert t["frames_recorded"] == 32
    # 阶跃首帧即重锚（P4 核心）：noise 段第一帧的 qp 已接近该段收敛值
    #（sized 搜索直接落点，无 ±4/帧爬行）；回落段首帧也即时回到平坦档
    noise_qps = [q for q, k in zip(qp_trace, kinds) if k == "noise"]
    n_start = kinds.index("noise")
    assert noise_qps
    assert qp_trace[n_start] >= max(noise_qps) - 8
    f2_start = kinds.index("flat", n_start)
    assert qp_trace[f2_start] <= 8
    # 借贷在段内消化：noise 段无失控 qp（≤95 域内且远离顶格）
    assert max(noise_qps) < 95


def test_encoder_telemetry_none_for_fixed_qp(tmp_path):
    """固定 qp（crf）路径无码控 → 遥测 None。"""
    out = tmp_path / "crf.mov"
    enc = ToposVideoEncoder(str(out), _config(crf=42), slice_threads=1)
    enc.open()
    for i in range(4):
        enc.encode_frame(Frame(i, "noise"))
    enc.close()
    assert enc.rate_control_telemetry() is None


def test_complexity_estimate_deterministic(tmp_path):
    """复杂度前瞻是平面的纯函数（两次编码逐字节一致）。"""
    outs = []
    for run in range(2):
        out = tmp_path / f"det{run}.mov"
        enc = ToposVideoEncoder(
            str(out), _config(bitrate=20_000_000), slice_threads=2)
        enc.open()
        for i in range(10):
            kind = "noise" if i % 5 == 4 else "flat"
            enc.encode_frame(Frame(i, kind))
        enc.close()
        outs.append(out)
    assert outs[0].read_bytes() == outs[1].read_bytes()
