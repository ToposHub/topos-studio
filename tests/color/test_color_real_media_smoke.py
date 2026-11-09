"""CV-B11 真机素材冒烟（offscreen 可跑的真实子集）。

用 tests/test_videos 的真实媒体验证 CV-B02 时间域链路端到端：
probe（真实 FFmpeg 容器解析）→ SourceTimecodeContext（Rational fps、
起始 TC、DF/NDF 判定）→ 精确 TC 格式化/解析往返。素材缺失按
``real_media`` 语义 skip（不计通过）。

真机专属项（4K60 吞吐、外屏 HDR 监看、音画听感、50 次切换循环）不
在本文件范围——见 docs/validation 验收记录的未执行边界。
"""

from __future__ import annotations

import pytest

from tests.real_media import require_video

pytestmark = pytest.mark.real_media

from src.features.color.core.color_time_domain import (  # noqa: E402
    SourceTimecodeContext,
    local_to_source_frame,
    source_to_local_frame,
)
from src.shared.media.factory import MediaSourceFactory  # noqa: E402


def test_real_prores_timecode_domain_end_to_end() -> None:
    path = require_video("2K_prores422.mov")
    metadata = MediaSourceFactory.probe_video_metadata(str(path))
    assert metadata is not None, "真实素材 probe 失败"
    context = SourceTimecodeContext.from_metadata(metadata)
    assert context is not None, "真实素材应给出 fps（Rational）"

    assert context.total_frames == metadata.total_frames
    # 帧 0 的往返：格式化 → 解析 → 同一帧（含起始 TC 偏移）。
    formatted = context.format_frame(0)
    assert len(formatted) == 11  # HH:MM:SS:FF 或 HH:MM:SS;FF
    assert context.parse_to_frame(formatted) == 0
    # 末帧同样往返（含 total 夹取）。
    last = context.total_frames - 1
    assert context.parse_to_frame(context.format_frame(last)) == last
    # 25fps 素材（冻结清单）判定 NDF、秒进位正确。
    fps = float(context.fps)
    if fps == 25.0:
        assert str(context.tc_type).endswith("NON_DROP_FRAME")
        assert context.format_frame(25).endswith(":01:00")


def test_real_clip_mapping_roundtrip_on_source_frames() -> None:
    """源帧→本地→源 在真实素材总长内往返（无 clip＝恒等域的基础链）。"""
    path = require_video("2K_prores422.mov")
    metadata = MediaSourceFactory.probe_video_metadata(str(path))
    assert metadata is not None
    total = int(metadata.total_frames or 0)
    assert total > 0
    for frame in (0, total // 2, total - 1):
        assert local_to_source_frame(None, frame) == frame
        assert source_to_local_frame(None, frame) == frame
