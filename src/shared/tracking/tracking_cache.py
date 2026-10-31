"""
跟踪结果缓存（阶段 3）。

缓存键 = 素材指纹 + 帧范围 + 参考帧 + 方向 + 算法版本 + 模式/质量参数
         + pattern 区域 + search 区域 + 遮罩形状几何（shape_geometry）。

任一要素变化（换素材、改参数、移动 pattern、改遮罩形状、算法升级）都
构成不同键，保证不会把旧结果当作新任务的结果返回。容量受限（LRU 淘汰）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from src.shared.tracking.tracker_2d import TrackRequest
from src.shared.tracking.types import TRACKING_ALGORITHM_VERSION

logger = logging.getLogger(__name__)

Region = Tuple[float, float, float, float]

# AUD-37411: 容器探测的限时上限。本地文件通常毫秒级；网络盘/慢速媒体/
# 损坏容器可能长时间阻塞，超时后按探测失败降级（daemon 线程自然消亡）。
_MEDIA_PROBE_TIMEOUT_S = 3.0


def _normalize_shape_geometry(value: object) -> object:
    """shape_geometry 的键规范化：浮点统一 round(…, 6) 规避抖动。

    dict 按键排序、tuple 视同 list，None/空 dict 分别编码（不同语义）；
    非有限浮点与其余不可 JSON 化的值以 ``repr`` 字符串参与键，保证键
    构造对任意几何都可成功（键构造失败比陈旧缓存更糟）。
    """
    if isinstance(value, dict):
        return {
            str(k): _normalize_shape_geometry(value[k])
            for k in sorted(value, key=str)
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_shape_geometry(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return repr(value)
        return round(float(value), 6)
    return repr(value)


class TrackingCache:
    """内存级跟踪结果缓存（线程约定：只在 UI 线程访问）。

    键必须包含影响结果的全部输入：素材指纹、帧范围、参考帧、方向、
    模式、质量、算法版本、pattern/search 区域以及遮罩形状几何。
    """

    def __init__(self, max_entries: int = 32) -> None:
        self._max_entries = max(1, int(max_entries))
        self._entries: "OrderedDict[str, object]" = OrderedDict()

    # ---- 键 ----

    @staticmethod
    def make_key(
        fingerprint: str,
        frame_start: int,
        frame_end: int,
        reference_frame: int,
        direction: str,
        mode: str,
        quality: str,
        pattern: Region,
        search: Region,
        algorithm_version: int = TRACKING_ALGORITHM_VERSION,
        shape_geometry: Optional[dict] = None,
    ) -> str:
        """构造缓存键。所有影响结果的要素参与，四舍五入避免浮点抖动。"""
        payload = {
            "fp": fingerprint,
            "fs": int(frame_start),
            "fe": int(frame_end),
            "ref": int(reference_frame),
            "dir": direction,
            "mode": mode,
            "q": quality,
            "alg": int(algorithm_version),
            "pattern": [round(float(v), 6) for v in pattern],
            "search": [round(float(v), 6) for v in search],
            # 遮罩形状几何实质改变初始特征检测（tracker_2d 光栅化
            # shape_geometry 限制特征点落点），必须参与键：同一包围盒
            # 内改形状后不得命中按旧形状计算的缓存结果（AUD-37410）。
            "sg": None if shape_geometry is None
            else _normalize_shape_geometry(shape_geometry),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def key_for_request(request: TrackRequest, fingerprint: str) -> str:
        return TrackingCache.make_key(
            fingerprint=fingerprint,
            frame_start=request.frame_start,
            frame_end=request.frame_end,
            reference_frame=request.reference_frame,
            direction=request.direction.value,
            mode=request.mode.value,
            quality=request.quality.value,
            pattern=request.pattern_region,
            search=request.search_region,
            shape_geometry=request.shape_geometry,
        )

    # ---- 读写 ----

    def get(self, key: str) -> Optional[object]:
        """命中时返回值并刷新 LRU 位置；未命中返回 None。"""
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, key: str, result: object) -> None:
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def contains(self, key: str) -> bool:
        return key in self._entries

    def remove(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def _probe_container_meta(media_path: str) -> str:
    """读取容器级视频元信息（可能阻塞，仅供限时线程调用）。"""
    import av

    with av.open(media_path) as container:
        streams = container.streams.video
        if streams:
            s = streams[0]
            return (
                f"{s.codec_context.width}x{s.codec_context.height}:"
                f"{s.frames}:{s.codec_context.name}"
            )
    return "no-video-stream"


def compute_media_fingerprint(media_path: str) -> str:
    """素材指纹：文件身份（大小 + mtime）+ 容器元信息。

    不做内容哈希（成本过高）；同一文件的替换通常伴随大小或修改时间变化。
    文件不存在时返回空串，调用方按未命中处理。

    AUD-37411: 容器探测在限时 daemon 线程内执行——本函数位于
    ``TrackingService.start()``（通常为 UI 线程槽）的同步调用链上，
    ``av.open`` 在网络盘/慢速媒体/损坏容器上可能长时间阻塞并卡死 UI。
    超时按 ``probe-failed`` 降级为纯文件身份指纹（指纹变弱好过卡死）。
    """
    try:
        stat = os.stat(media_path)
    except OSError:
        return ""
    result: Dict[str, str] = {"meta": "probe-failed"}

    def _run() -> None:
        try:
            result["meta"] = _probe_container_meta(media_path)
        except Exception:  # noqa: BLE001 - 无法探测时退化为纯文件身份
            pass

    probe_thread = threading.Thread(
        target=_run, name="tracking-media-probe", daemon=True)
    probe_thread.start()
    probe_thread.join(_MEDIA_PROBE_TIMEOUT_S)
    if probe_thread.is_alive():
        logger.warning(
            "compute_media_fingerprint: container probe exceeded %.1fs, "
            "degrading to file-identity fingerprint: %s",
            _MEDIA_PROBE_TIMEOUT_S, media_path)
    meta = result["meta"]
    raw = f"{media_path}|{stat.st_size}|{int(stat.st_mtime_ns)}|{meta}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
