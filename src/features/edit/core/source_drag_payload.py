"""VP-B08：Source 拖拽内部 MIME——有版本载荷（asset 身份 + 源区间 + 流意图）。

缺口（计划 VP-B08）：媒体池的 asset MIME 只表达资产 ID 列表，不携带
viewer 当前源区间与只视频/只音频意图；把机器绝对路径当资产身份、信任
拖拽文本都不可靠。Source 画面／视频图标／音频图标拖放需要同一结构、
不同 ``stream_intent`` 的内部载荷。

契约（``source_drag_payload``，纯领域；Qt 装配在 UI 层）：

- MIME：``application/x-topos-source-clip``，载荷 JSON 带 ``version``；
- 字段：``asset_id``（项目内资产身份，非路径）、``in_frame``/
  ``out_frame``（**Out 包含**语义，与 B04 校验一致）、
  ``stream_intent`` ∈ {"av","video","audio"}；
- 解析永不抛错：损坏 JSON / 非法 intent / 越界区间 / 未来版本 →
  ``None``（消费端按通用 asset MIME 回退，URI fallback 不会重复导入
  内部素材）；
- ``source_fps_num/den`` 可选携带（跨帧率插入的时间域换算用）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Tuple

MIME_TYPE_SOURCE_CLIP = "application/x-topos-source-clip"
PAYLOAD_VERSION = 1

STREAM_INTENT_AV = "av"
STREAM_INTENT_VIDEO = "video"
STREAM_INTENT_AUDIO = "audio"
_VALID_INTENTS = (STREAM_INTENT_AV, STREAM_INTENT_VIDEO, STREAM_INTENT_AUDIO)


@dataclass(frozen=True)
class SourceDragPayload:
    """Source → Timeline 拖拽载荷（Out 包含语义）。"""

    asset_id: str
    in_frame: int
    out_frame: int
    stream_intent: str = STREAM_INTENT_AV
    source_fps_num: Optional[int] = None
    source_fps_den: Optional[int] = None

    @property
    def source_length(self) -> int:
        """Out 包含：长度 = out − in + 1。"""
        return self.out_frame - self.in_frame + 1

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": PAYLOAD_VERSION,
            "asset_id": self.asset_id,
            "in_frame": int(self.in_frame),
            "out_frame": int(self.out_frame),
            "stream_intent": self.stream_intent,
        }
        if self.source_fps_num is not None and self.source_fps_den:
            data["source_fps_num"] = int(self.source_fps_num)
            data["source_fps_den"] = int(self.source_fps_den)
        return data

    def encode(self) -> bytes:
        return json.dumps(self.to_dict()).encode("utf-8")


def build_source_drag_payload(
        asset_id: str, in_frame: int, out_frame: int,
        stream_intent: str = STREAM_INTENT_AV,
        source_fps: Optional[Tuple[int, int]] = None,
) -> Optional[SourceDragPayload]:
    """构造载荷；非法参数（空资产/坏区间/坏意图）返回 None 而非抛错。"""
    if not asset_id:
        return None
    if stream_intent not in _VALID_INTENTS:
        return None
    in_frame, out_frame = int(in_frame), int(out_frame)
    if in_frame < 0 or out_frame < in_frame:
        return None
    fps_num = fps_den = None
    if source_fps is not None:
        fps_num, fps_den = int(source_fps[0]), int(source_fps[1])
        if fps_num <= 0 or fps_den <= 0:
            return None  # 坏帧率整单拒绝（调用方 bug 信号）
    return SourceDragPayload(
        asset_id=asset_id, in_frame=in_frame, out_frame=out_frame,
        stream_intent=stream_intent,
        source_fps_num=fps_num, source_fps_den=fps_den)


def decode_source_drag_payload(raw: Any) -> Optional[SourceDragPayload]:
    """解析载荷；损坏/未来版本/非法值 → None（消费端回退通用 MIME）。

    ``raw`` 接受 str / bytes / bytearray / QByteArray（``QMimeData.data()``
    的返回类型；经 ``bytes()`` 鸭子转换，本模块不 import Qt）。
    """
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = bytes(raw).decode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if version != PAYLOAD_VERSION:
        return None  # 未来/未知版本：不猜结构
    asset_id = data.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        return None
    intent = data.get("stream_intent", STREAM_INTENT_AV)
    if intent not in _VALID_INTENTS:
        return None
    in_frame = data.get("in_frame")
    out_frame = data.get("out_frame")
    if not isinstance(in_frame, int) or isinstance(in_frame, bool):
        return None
    if not isinstance(out_frame, int) or isinstance(out_frame, bool):
        return None
    if in_frame < 0 or out_frame < in_frame:
        return None
    fps_num = data.get("source_fps_num")
    fps_den = data.get("source_fps_den")
    if fps_num is not None and (not isinstance(fps_num, int)
                                or isinstance(fps_num, bool) or fps_num <= 0):
        fps_num = fps_den = None
    return SourceDragPayload(
        asset_id=asset_id, in_frame=in_frame, out_frame=out_frame,
        stream_intent=intent, source_fps_num=fps_num,
        source_fps_den=fps_den if fps_num is not None else None)
