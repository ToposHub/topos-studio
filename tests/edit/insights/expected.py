"""T04 · 黄金数据的独立预期（全部人工手算，禁止调用被测函数生成）。

本文件是"答案纸"：每个数字都附手算过程。任何实现（capture/search/
report/stats）对不上这里就是错。合成数据本体见 ``synthetic.py``。

手算底稿（S1 "主时间线"，24fps NDF）：

V1 四个 clip（半开区间）：
    A1  [0,120)   A2  [120,200)   B1  [200,320)   B2  [400,460)
    → 依次相接成 [0,320)，之后 80 帧间隙，再 [400,460)
    → 覆盖并集 = 320 + 60            = 380 帧
    → 间隙      = 400 - 320          = 80 帧
    → 镜头长    = 120-0, 200-120, 320-200, 460-400 = 120/80/120/60
    → 均值 = (120+80+120+60)/4 = 380/4 = 95.0
    → 中位 = sorted=[60,80,120,120] → (80+120)/2 = 100.0

A1 音频轨：SFX [0,200)（与 V1 时间重叠——叠轨，不是串行节目时长）。

SUB1 字幕：[10,70) 与 [80,150) → 覆盖 = 60 + 70 = 130 帧。

S1 duration = max(V1=460, A1=200, SUB1=150) = 460。

计数：
    S1 clips = 4(视频) + 1(音频) = 5；S2 clips = 1
    全工程 clip 出现次数 = 6；唯一素材（被引用）= 3（CamA×3、CamB×2、SFX×1）
    cue：S1=2、S2=1（其中"你好，世界"跨序列重复 2 次）
    marker：S1=2（standard 1 + chapter 1）

时间码（显示口径，复用既有 Timecode 公式——此处为独立手算）：
    S1 24fps NDF：frame 100 → 100 = 4*24 + 4 → 00:00:04:04
    S2 30000/1001 DF：frame 100 < 1800（第 1 分钟内不丢帧）
        → 100 = 3*30 + 10 → 00:00:03;10

搜索（T10 语义：字面子串、大小写不敏感；命中字段独立枚举）：
    "你好"   → S1/CUE_ZH + S2/CUE_SHARED = 2 条（跨序列重复独立保留）
    "CamA"   → 素材名 1 + 引用该素材的 clip 4（S1 A1/A2 + S2 NTSC1 + …
               CamA 引用共 3 处：A1、A2、NTSC1）
    "quote"  → S1/CUE_QUOTE 1 条
    空串/纯空白 → 不命中
"""

from __future__ import annotations

# ── S1 覆盖/节奏（手算见模块 docstring）─────────────────────────

S1_V1_COVERAGE_FRAMES = 380
S1_V1_GAP_FRAMES = 80
S1_V1_SHOT_LENGTHS = (120, 80, 120, 60)
S1_V1_MEAN_SHOT = 95.0
S1_V1_MEDIAN_SHOT = 100.0
S1_CUE_COVERAGE_FRAMES = 130
S1_DURATION = 460

# V1 ∪ A1 并集仍 380（A1 [0,200) ⊂ V1 [0,320) 段内）
S1_V1_A1_COVERAGE_FRAMES = 380

# ── 计数 ────────────────────────────────────────────────────────

CLIP_COUNTS = {"main": 5, "ntsc": 1}
TOTAL_CLIP_OCCURRENCES = 6
UNIQUE_USED_ASSETS = 3          # CamA / CamB / SFX（素材池共 3，无 unused）
CUE_COUNTS = {"main": 2, "ntsc": 1}
MARKER_COUNTS = {"main": 2, "ntsc": 0}

# ── 搜索预期（query → 命中 (sequence_id, entity_id) 集合）──────

SEARCH_NIHAO = {
    ("gp-seq-main", "gp-cue-zh"),
    ("gp-seq-ntsc", "gp-cue-shared"),
}
SEARCH_CAMA_CLIPS = {
    ("gp-seq-main", "gp-clip-a1"),
    ("gp-seq-main", "gp-clip-a2"),
    ("gp-seq-ntsc", "gp-clip-ntsc1"),
}
SEARCH_CAMA_ASSET = (None, "gp-asset-camA")
SEARCH_QUOTE = {("gp-seq-main", "gp-cue-quote")}

# ── 时间码显示 ──────────────────────────────────────────────────

TC_S1_FRAME100 = "00:00:04:04"   # 24fps NDF
TC_S2_FRAME100 = "00:00:03;10"   # 30000/1001 DF（第 1 分钟内无丢帧）


def brute_force_union_frames(intervals) -> int:
    """独立并集算法（逐帧集合暴力法）——与任何实现不共享代码。

    intervals: iterable of (start, end) 半开区间。
    """
    frames: set[int] = set()
    for start, end in intervals:
        frames.update(range(start, end))
    return len(frames)
