"""音频 cue 导出 —— 复用既有 ``interchange.CueSheet``（T11 验收项落地）。

计划 T11：「音频cue复用既有CueSheet能力，不重写音频cue算法」。
CueSheet 构建需要宿主 ``TimelineSequence`` 对象（快照 DTO 不持有模型），
因此本模块从注入的 ``capture_state_fn``（与 InjectedHostAdapter 同源）取
真实序列后**纯委托**给 ``interchange.build_cue_sheet_from_sequence``；
不复制、不修补其算法（含其已知的 float fps 与字符串排序口径——那是
既有能力的行为，不是本包的缺陷）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.features.edit.insights.dto import LOAD_STATE_LOADED, InsightSnapshot


def export_audio_cue_csvs(
    capture_state_fn: Callable[[], object | None],
    snapshot: InsightSnapshot,
    directory: str | Path,
) -> list[str]:
    """为快照中每个含音频轨的已加载序列导出 cue CSV。

    Args:
        capture_state_fn: 宿主两层状态提供函数（返回 ProjectSequences
            形态或 None；与 InjectedHostAdapter.capture_state_fn 同源）。
        snapshot: 当前捕获（决定导出哪些序列与工程名）。
        directory: 输出目录。

    Returns:
        成功写出的 CSV 路径列表（序列缺宿主对象/无音频轨时跳过）。
    """
    from src.features.edit.core.interchange import build_cue_sheet_from_sequence

    state = capture_state_fn() if capture_state_fn is not None else None
    sequences_map = getattr(state, "sequences", None) or {}
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for seq in snapshot.sequences:
        if seq.load_state != LOAD_STATE_LOADED:
            continue
        if not any(t.track_type == "audio" for t in seq.tracks):
            continue
        host_sequence = sequences_map.get(seq.sequence_id)
        if host_sequence is None:
            continue  # 捕获后宿主序列被卸载：跳过，不伪造
        sheet = build_cue_sheet_from_sequence(
            host_sequence, project_name=snapshot.project.name
        )
        path = out_dir / f"audio_cues_{seq.sequence_id[:12]}.csv"
        if sheet.to_csv(str(path)):
            written.append(str(path))
    return written
